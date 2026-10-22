use serde::Serialize;
use serde_json::{json, Value};
use std::io::{BufRead, BufReader, Read, Write};
#[cfg(target_os = "windows")]
use std::os::windows::process::CommandExt;
use std::path::{Path, PathBuf};
use std::process::{Child, ChildStdin, Command, Stdio};
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::mpsc::{self, Receiver, RecvTimeoutError};
use std::sync::{Arc, Mutex};
use std::thread;
use std::time::{Duration, Instant};
use tauri::{AppHandle, Manager, State};

static REQUEST_ID: AtomicU64 = AtomicU64::new(1);
const INITIALIZATION_DEADLINE_SECONDS: u64 = 120;
const INITIALIZATION_TIMEOUT: Duration = Duration::from_secs(INITIALIZATION_DEADLINE_SECONDS);
const RESPONSE_TIMEOUT: Duration = Duration::from_secs(600);
const MAX_RESPONSE_BYTES: usize = 16 * 1024 * 1024;

/// Owns the single long-lived research-core process for a desktop window.
///
/// The Tauri command is intentionally the only public seam. Process startup,
/// JSON-RPC framing, response correlation, and restart-on-transport-failure are
/// hidden here so another desktop adapter can reuse the same lifecycle rules.
#[derive(Clone, Default)]
pub(crate) struct BackendState {
    process: Arc<Mutex<Option<BackendProcess>>>,
    initialization: Arc<Mutex<InitializationState>>,
}

impl BackendState {
    fn begin_initialization(&self) -> Instant {
        let started_at = Instant::now();
        if let Ok(mut state) = self.initialization.lock() {
            state.phase = InitializationPhase::Starting;
            state.started_at = Some(started_at);
            state.elapsed_seconds = 0;
            state.safe_failure_code = None;
            state.stage = None;
            state.completed_units = None;
            state.total_units = None;
        }
        started_at
    }

    fn finish_initialization(&self) {
        if let Ok(mut state) = self.initialization.lock() {
            state.elapsed_seconds = state
                .started_at
                .take()
                .map(|started| started.elapsed().as_secs())
                .unwrap_or(state.elapsed_seconds);
            state.phase = InitializationPhase::Ready;
            state.safe_failure_code = None;
        }
    }

    fn fail_initialization(&self, code: SafeFailureCode) {
        if let Ok(mut state) = self.initialization.lock() {
            state.elapsed_seconds = state
                .started_at
                .take()
                .map(|started| started.elapsed().as_secs())
                .unwrap_or(state.elapsed_seconds);
            state.phase = InitializationPhase::Failed;
            state.safe_failure_code = Some(code);
        }
    }

    fn initialization_snapshot(&self) -> BackendInitializationSnapshot {
        match self.initialization.lock() {
            Ok(state) => BackendInitializationSnapshot {
                phase: state.phase,
                elapsed_seconds: state
                    .started_at
                    .map(|started| started.elapsed().as_secs())
                    .unwrap_or(state.elapsed_seconds),
                deadline_seconds: INITIALIZATION_DEADLINE_SECONDS,
                safe_failure_code: state.safe_failure_code,
                stage: state.stage.clone(),
                completed_units: state.completed_units,
                total_units: state.total_units,
            },
            Err(_) => BackendInitializationSnapshot {
                phase: InitializationPhase::Failed,
                elapsed_seconds: 0,
                deadline_seconds: INITIALIZATION_DEADLINE_SECONDS,
                safe_failure_code: Some(SafeFailureCode::StatusUnavailable),
                stage: None,
                completed_units: None,
                total_units: None,
            },
        }
    }
}

#[derive(Clone, Copy, Default, Serialize)]
#[serde(rename_all = "snake_case")]
enum InitializationPhase {
    #[default]
    Idle,
    Starting,
    Ready,
    Failed,
}

#[derive(Clone, Copy, Serialize)]
#[serde(rename_all = "snake_case")]
enum SafeFailureCode {
    SpawnFailed,
    ProcessExited,
    StartupTimeout,
    RequestTimeout,
    TransportFailure,
    ProtocolFailure,
    StartupFailure,
    StatusUnavailable,
}

#[derive(Serialize)]
pub(crate) struct BackendInitializationSnapshot {
    phase: InitializationPhase,
    elapsed_seconds: u64,
    deadline_seconds: u64,
    safe_failure_code: Option<SafeFailureCode>,
    stage: Option<String>,
    completed_units: Option<u64>,
    total_units: Option<u64>,
}

#[derive(Default)]
struct InitializationState {
    phase: InitializationPhase,
    started_at: Option<Instant>,
    elapsed_seconds: u64,
    safe_failure_code: Option<SafeFailureCode>,
    stage: Option<String>,
    completed_units: Option<u64>,
    total_units: Option<u64>,
}

fn record_initialization_progress(state: &Arc<Mutex<InitializationState>>, line: &str) {
    let Some(body) = line.strip_prefix("OT_STARTUP ") else {
        return;
    };
    let Ok(record) = serde_json::from_str::<Value>(body) else {
        return;
    };
    let Some(stage) = record.get("stage").and_then(Value::as_str) else {
        return;
    };
    if !matches!(
        stage,
        "checking_schema"
            | "checking_integrity"
            | "backing_up"
            | "migrating_schema"
            | "backfilling_facts"
            | "building_read_models"
            | "storage_ready"
            | "preparing_services"
            | "ready"
    ) {
        return;
    }
    if let Ok(mut status) = state.lock() {
        if !matches!(status.phase, InitializationPhase::Starting) {
            return;
        }
        status.stage = Some(stage.to_string());
        let completed = record.get("completed_units").and_then(Value::as_u64);
        let total = record.get("total_units").and_then(Value::as_u64);
        let valid = matches!((completed,total), (Some(done),Some(all)) if all > 0 && done <= all && all <= 1_000_000_000);
        status.completed_units = if valid { completed } else { None };
        status.total_units = if valid { total } else { None };
    }
}

struct BackendProcess {
    child: Child,
    stdin: ChildStdin,
    responses: Receiver<String>,
}

enum BackendError {
    Transport(String),
    Rpc(String),
    Exited,
    NoResponse,
    InvalidJson,
    Protocol,
}

struct StartupFailure {
    message: String,
    retryable: bool,
    safe_failure_code: SafeFailureCode,
}

impl StartupFailure {
    fn fatal(message: impl Into<String>) -> Self {
        Self {
            message: message.into(),
            retryable: false,
            safe_failure_code: SafeFailureCode::StartupFailure,
        }
    }

    fn fatal_with_code(message: impl Into<String>, safe_failure_code: SafeFailureCode) -> Self {
        Self {
            message: message.into(),
            retryable: false,
            safe_failure_code,
        }
    }

    fn transient(message: impl Into<String>, safe_failure_code: SafeFailureCode) -> Self {
        Self {
            message: message.into(),
            retryable: true,
            safe_failure_code,
        }
    }
}

fn startup_retry_allowed(failure: &StartupFailure, attempt: usize) -> bool {
    attempt == 0
        && failure.retryable
        && matches!(
            failure.safe_failure_code,
            SafeFailureCode::SpawnFailed | SafeFailureCode::ProcessExited
        )
}

impl BackendProcess {
    fn start(
        app: &AppHandle,
        initialization_deadline: Instant,
        initialization: Arc<Mutex<InitializationState>>,
    ) -> Result<Self, StartupFailure> {
        if Instant::now() >= initialization_deadline {
            return Err(StartupFailure::fatal_with_code(
                "research core initialization deadline expired",
                SafeFailureCode::StartupTimeout,
            ));
        }
        let mut command = backend_command(app).map_err(StartupFailure::fatal)?;
        configure_model_gateway_environment(app, &mut command).map_err(StartupFailure::fatal)?;
        configure_backend_command(&mut command);
        let mut child = command
            .env("OPENTHESIS_STARTUP_PROGRESS", "1")
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped())
            .spawn()
            .map_err(|_| {
                StartupFailure::transient(
                    "research core could not be started (spawn)",
                    SafeFailureCode::SpawnFailed,
                )
            })?;
        if let Some(stderr) = child.stderr.take() {
            // Dedicated stderr telemetry; stdout still has exactly one reader.
            // Drain unknown logging without retaining it or exposing secrets.
            thread::spawn(move || {
                let mut reader = BufReader::new(stderr);
                loop {
                    let mut line = String::new();
                    match Read::by_ref(&mut reader).take(2049).read_line(&mut line) {
                        Ok(0) | Err(_) => break,
                        Ok(_) if line.len() <= 2048 && line.ends_with('\n') => {
                            record_initialization_progress(&initialization, &line)
                        }
                        Ok(_) => {}
                    }
                }
            });
        }
        let stdin = child.stdin.take().ok_or_else(|| {
            stop_child(&mut child);
            StartupFailure::fatal("research core input is unavailable")
        })?;
        let stdout = child.stdout.take().ok_or_else(|| {
            stop_child(&mut child);
            StartupFailure::fatal("research core output is unavailable")
        })?;
        let (sender, responses) = mpsc::channel();
        thread::spawn(move || {
            let mut reader = BufReader::new(stdout);
            loop {
                let mut line = String::new();
                match reader.read_line(&mut line) {
                    Ok(0) | Err(_) => break,
                    Ok(_) => {
                        if line.len() > MAX_RESPONSE_BYTES {
                            let _ = sender.send(String::new());
                            break;
                        }
                        if sender.send(line).is_err() {
                            break;
                        }
                    }
                }
            }
        });
        let mut process = Self {
            child,
            stdin,
            responses,
        };
        let hello = process
            .request_with_timeout(
                "system.hello".to_string(),
                json!({}),
                initialization_deadline.saturating_duration_since(Instant::now()),
            )
            .map_err(startup_failure_from_backend)?;
        validate_hello_result(&hello).map_err(StartupFailure::fatal)?;
        Ok(process)
    }

    fn request(&mut self, method: String, params: Value) -> Result<Value, BackendError> {
        self.request_with_timeout(method, params, RESPONSE_TIMEOUT)
    }

    fn request_with_timeout(
        &mut self,
        method: String,
        params: Value,
        timeout: Duration,
    ) -> Result<Value, BackendError> {
        let request_id = REQUEST_ID.fetch_add(1, Ordering::Relaxed);
        let request = json!({
            "jsonrpc": "2.0",
            "id": request_id,
            "method": method,
            "params": params,
        });
        writeln!(self.stdin, "{request}")
            .and_then(|_| self.stdin.flush())
            .map_err(|_| {
                BackendError::Transport("research core request could not be sent".to_string())
            })?;

        let response_line = match self.responses.recv_timeout(timeout) {
            Ok(line) if line.is_empty() => return Err(BackendError::InvalidJson),
            Ok(line) => line,
            Err(RecvTimeoutError::Timeout) => {
                if self.child.try_wait().ok().flatten().is_some() {
                    return Err(BackendError::Exited);
                }
                return Err(BackendError::NoResponse);
            }
            Err(RecvTimeoutError::Disconnected) => return Err(BackendError::Exited),
        };
        let response: Value =
            serde_json::from_str(&response_line).map_err(|_| BackendError::InvalidJson)?;
        if response.get("id") != Some(&json!(request_id)) {
            return Err(BackendError::Protocol);
        }
        if let Some(error) = response.get("error") {
            return Err(BackendError::Rpc(safe_rpc_failure(error)));
        }
        response
            .get("result")
            .cloned()
            .ok_or_else(|| BackendError::Protocol)
    }
}

fn safe_rpc_failure(error: &Value) -> String {
    let code = error.get("code").and_then(Value::as_i64);
    let message = error.get("message").and_then(Value::as_str);
    // Preserve only host-owned actionable codes needed by the UI. Arbitrary
    // exception strings can contain stored data, filesystem paths or secrets.
    if matches!(
        (code, message),
        (Some(-32040), Some("STORAGE_HEALTH_RECOVERY_REQUIRED"))
            | (Some(-32010), Some("MODEL_BUDGET_PLAN_CHANGED"))
            | (Some(-32010), Some("MODEL_BUDGET_GAP_CHANGED"))
            | (Some(-32010), Some("CORE_OPERATION_BUSY"))
            | (Some(-32010), Some("CORE_OPERATION_EXPIRED"))
    ) {
        return message.unwrap().to_string();
    }
    format!(
        "research core request failed ({})",
        code.map(|value| value.to_string())
            .unwrap_or_else(|| "unknown".to_string())
    )
}

fn startup_failure_from_backend(error: BackendError) -> StartupFailure {
    match error {
        BackendError::Transport(_) => StartupFailure::fatal_with_code(
            "research core startup transport failed",
            SafeFailureCode::TransportFailure,
        ),
        BackendError::Exited => StartupFailure::transient(
            "research core exited during startup",
            SafeFailureCode::ProcessExited,
        ),
        BackendError::NoResponse => StartupFailure::fatal_with_code(
            "research core did not respond to startup handshake",
            SafeFailureCode::StartupTimeout,
        ),
        BackendError::InvalidJson => StartupFailure::fatal_with_code(
            "research core returned invalid startup JSON",
            SafeFailureCode::ProtocolFailure,
        ),
        BackendError::Protocol | BackendError::Rpc(_) => StartupFailure::fatal_with_code(
            "research core startup protocol error",
            SafeFailureCode::ProtocolFailure,
        ),
    }
}

fn validate_hello_result(response: &Value) -> Result<(), String> {
    let result = response
        .as_object()
        .ok_or_else(|| "research core startup protocol error".to_string())?;
    if result.get("contract_version").and_then(Value::as_str) != Some("2.0") {
        return Err("research core startup protocol error".to_string());
    }
    let desktop_build: Value = serde_json::from_str(include_str!("../resources/build-info.json"))
        .map_err(|_| "desktop build identity is invalid".to_string())?;
    let expected = desktop_build
        .get("build_id")
        .and_then(Value::as_str)
        .unwrap_or("unknown");
    let actual = result
        .get("build_info")
        .and_then(|value| value.get("build_id"))
        .and_then(Value::as_str)
        .unwrap_or("unknown");
    if expected != "development" && expected != actual {
        return Err(format!(
            "mixed application components: desktop build {expected}, research core build {actual}"
        ));
    }
    Ok(())
}

fn stop_child(child: &mut Child) {
    let _ = child.kill();
    let _ = child.wait();
}

fn resolve_gateway_data_dir(
    override_dir: Option<&str>,
    app_data_dir: PathBuf,
) -> Result<PathBuf, String> {
    let path = match override_dir.map(str::trim) {
        None | Some("") => app_data_dir,
        Some(value) => PathBuf::from(value),
    };
    if !path.is_absolute() {
        return Err("model gateway data directory must be absolute".to_string());
    }
    Ok(path)
}

fn configure_model_gateway_environment(
    app: &AppHandle,
    command: &mut Command,
) -> Result<(), String> {
    let executable = std::env::current_exe()
        .map_err(|_| "model gateway executable is unavailable".to_string())?;
    let app_data_dir = app
        .path()
        .app_local_data_dir()
        .map_err(|_| "model gateway data directory is unavailable".to_string())?;
    let override_dir = std::env::var("OPENTHESIS_DATA_DIR").ok();
    let data_dir = resolve_gateway_data_dir(override_dir.as_deref(), app_data_dir)?;
    command
        .env("OPENTHESIS_MODEL_GATEWAY_PATH", executable)
        .env("OPENTHESIS_DATA_DIR", data_dir);
    Ok(())
}

#[cfg(target_os = "windows")]
fn configure_backend_command(command: &mut Command) {
    const CREATE_NO_WINDOW: u32 = 0x0800_0000;
    command.creation_flags(CREATE_NO_WINDOW);
}

#[cfg(not(target_os = "windows"))]
fn configure_backend_command(_command: &mut Command) {}

impl Drop for BackendProcess {
    fn drop(&mut self) {
        let _ = self.child.kill();
        let _ = self.child.wait();
    }
}

#[tauri::command]
pub(crate) async fn backend_request(
    app: AppHandle,
    state: State<'_, BackendState>,
    method: String,
    params: Value,
) -> Result<Value, String> {
    let backend_state = state.inner().clone();
    drop(state);
    run_backend_blocking(move || backend_request_blocking(app, backend_state, method, params))
        .await?
}

#[tauri::command]
pub(crate) fn backend_initialization_status(
    state: State<'_, BackendState>,
) -> BackendInitializationSnapshot {
    state.initialization_snapshot()
}

async fn run_backend_blocking<T, F>(operation: F) -> Result<T, String>
where
    T: Send + 'static,
    F: FnOnce() -> T + Send + 'static,
{
    tauri::async_runtime::spawn_blocking(operation)
        .await
        .map_err(|_| "research core request worker stopped unexpectedly".to_string())
}

fn backend_request_blocking(
    app: AppHandle,
    state: BackendState,
    method: String,
    params: Value,
) -> Result<Value, String> {
    let mut process = state.process.lock().map_err(|_| {
        state.fail_initialization(SafeFailureCode::StartupFailure);
        "research core state is unavailable".to_string()
    })?;
    if process.is_none() {
        let initialization_deadline = state.begin_initialization() + INITIALIZATION_TIMEOUT;
        let mut last_failure: Option<StartupFailure> = None;
        for attempt in 0..2 {
            match BackendProcess::start(&app, initialization_deadline, state.initialization.clone())
            {
                Ok(started) => {
                    *process = Some(started);
                    state.finish_initialization();
                    break;
                }
                Err(failure) if startup_retry_allowed(&failure, attempt) => {
                    last_failure = Some(failure);
                }
                Err(failure) => {
                    last_failure = Some(failure);
                    break;
                }
            }
        }
        if process.is_none() {
            let startup_failure = last_failure
                .unwrap_or_else(|| StartupFailure::fatal("research core could not be started"));
            state.fail_initialization(startup_failure.safe_failure_code);
            return Err(startup_failure.message);
        }
    }
    let result = process
        .as_mut()
        .ok_or_else(|| "research core could not be started".to_string())?
        .request(method, params);
    match result {
        Ok(value) => Ok(value),
        Err(error) => Err(backend_request_error(error, &state, &mut process)),
    }
}

fn backend_request_error(
    error: BackendError,
    state: &BackendState,
    process: &mut Option<BackendProcess>,
) -> String {
    match error {
        BackendError::Rpc(message) => message,
        BackendError::Transport(message) => {
            *process = None;
            state.fail_initialization(SafeFailureCode::TransportFailure);
            message
        }
        BackendError::Exited => {
            *process = None;
            state.fail_initialization(SafeFailureCode::ProcessExited);
            "research core stopped unexpectedly".to_string()
        }
        BackendError::NoResponse => {
            *process = None;
            state.fail_initialization(SafeFailureCode::RequestTimeout);
            "research core did not respond".to_string()
        }
        BackendError::InvalidJson => {
            *process = None;
            state.fail_initialization(SafeFailureCode::ProtocolFailure);
            "research core returned an invalid response".to_string()
        }
        BackendError::Protocol => {
            *process = None;
            state.fail_initialization(SafeFailureCode::ProtocolFailure);
            "research core returned an invalid protocol response".to_string()
        }
    }
}

fn backend_command(app: &AppHandle) -> Result<Command, String> {
    if let Ok(explicit_path) = std::env::var("OPENTHESIS_SIDECAR_PATH") {
        let sidecar_path = validate_explicit_sidecar_path(Some(&explicit_path))?;
        prepare_sidecar_runtime(&sidecar_path)?;
        return Ok(Command::new(sidecar_path));
    }

    if cfg!(debug_assertions) {
        let python = validate_debug_python(std::env::var("OPENTHESIS_PYTHON").ok().as_deref())?;
        let source_root = PathBuf::from(env!("CARGO_MANIFEST_DIR"))
            .join("..")
            .join("..")
            .join("src");
        let mut command = Command::new(python);
        command
            .arg("-m")
            .arg("openthesis.sidecar")
            .env("PYTHONPATH", source_root);
        return Ok(command);
    }

    let executable_name = if cfg!(target_os = "windows") {
        "openthesis-sidecar.exe"
    } else {
        "openthesis-sidecar"
    };
    let resource_dir = app
        .path()
        .resource_dir()
        .map_err(|_| "desktop resource directory is unavailable".to_string())?;
    let executable_dir = std::env::current_exe()
        .ok()
        .and_then(|path| path.parent().map(Path::to_path_buf));
    let candidates = sidecar_candidates(Some(resource_dir), executable_dir, executable_name);
    let sidecar_path = candidates
        .iter()
        .find(|path| path.is_file())
        .or_else(|| candidates.first())
        .ok_or_else(|| "desktop resource directory is unavailable".to_string())?;
    prepare_sidecar_runtime(sidecar_path)?;
    Ok(Command::new(sidecar_path))
}

fn prepare_sidecar_runtime(sidecar_path: &Path) -> Result<(), String> {
    #[cfg(target_os = "windows")]
    {
        let internal_dir = sidecar_path
            .parent()
            .ok_or_else(|| "sidecar directory is unavailable".to_string())?
            .join("_internal");
        if internal_dir.is_dir() {
            for entry in std::fs::read_dir(&internal_dir)
                .map_err(|error| format!("sidecar runtime directory could not be read: {error}"))?
            {
                let entry = entry
                    .map_err(|error| format!("sidecar runtime entry could not be read: {error}"))?;
                let staged_runtime = entry.path();
                if staged_runtime.extension().and_then(|value| value.to_str()) != Some("bin")
                    || !staged_runtime
                        .file_stem()
                        .and_then(|value| value.to_str())
                        .is_some_and(|value| {
                            value.starts_with("VCRUNTIME") || value.starts_with("MSVCP")
                        })
                {
                    continue;
                }
                let runtime = staged_runtime.with_extension("dll");
                if !runtime.exists() {
                    std::fs::rename(&staged_runtime, &runtime).map_err(|error| {
                        format!(
                            "sidecar runtime could not be restored from {} to {}: {error}",
                            staged_runtime.display(),
                            runtime.display()
                        )
                    })?;
                }
            }
        }
    }
    let _ = sidecar_path;
    Ok(())
}

fn validate_explicit_sidecar_path(value: Option<&str>) -> Result<PathBuf, String> {
    let value = value
        .map(str::trim)
        .filter(|value| !value.is_empty())
        .ok_or_else(|| "explicit sidecar path is empty".to_string())?;
    let path = PathBuf::from(value);
    if !path.is_absolute() {
        return Err("explicit sidecar path must be absolute".to_string());
    }
    if !path.is_file() {
        return Err("explicit sidecar path is unavailable".to_string());
    }
    Ok(path)
}

fn validate_debug_python(value: Option<&str>) -> Result<PathBuf, String> {
    let value = value.ok_or_else(|| "debug Python path is missing".to_string())?;
    let value = value
        .trim()
        .strip_prefix('\u{feff}')
        .unwrap_or(value)
        .trim();
    if value.is_empty() {
        return Err("debug Python path is empty".to_string());
    }
    let path = PathBuf::from(value);
    if !path.is_absolute() {
        return Err("debug Python path must be absolute".to_string());
    }
    if !path.is_file() {
        return Err("debug Python path is unavailable".to_string());
    }
    Ok(path)
}

fn sidecar_candidates(
    resource_dir: Option<PathBuf>,
    executable_dir: Option<PathBuf>,
    executable_name: &str,
) -> Vec<PathBuf> {
    let mut candidates = Vec::new();
    for base in [resource_dir, executable_dir].into_iter().flatten() {
        let candidate = base
            .join("bin")
            .join("openthesis-sidecar")
            .join(executable_name);
        if !candidates.contains(&candidate) {
            candidates.push(candidate);
        }
    }
    candidates
}

#[cfg(test)]
mod tests {
    use super::{
        prepare_sidecar_runtime, run_backend_blocking, sidecar_candidates,
        startup_failure_from_backend, startup_retry_allowed, validate_debug_python,
        validate_explicit_sidecar_path, validate_hello_result, BackendError, BackendState,
        SafeFailureCode, StartupFailure,
    };
    use std::fs;
    use std::path::PathBuf;
    use std::sync::mpsc;
    use std::thread;
    use std::time::Duration;

    #[test]
    fn actionable_rpc_codes_survive_without_leaking_arbitrary_messages() {
        for (code, message) in [
            (-32040, "STORAGE_HEALTH_RECOVERY_REQUIRED"),
            (-32010, "MODEL_BUDGET_PLAN_CHANGED"),
            (-32010, "MODEL_BUDGET_GAP_CHANGED"),
            (-32010, "CORE_OPERATION_BUSY"),
            (-32010, "CORE_OPERATION_EXPIRED"),
        ] {
            assert_eq!(
                super::safe_rpc_failure(&serde_json::json!({"code": code, "message": message})),
                message
            );
        }
        assert_eq!(
            super::safe_rpc_failure(&serde_json::json!({"code": -32603,
            "message": "private-key-and-local-file"})),
            "research core request failed (-32603)"
        );
        assert_eq!(
            super::safe_rpc_failure(&serde_json::json!({"code": -32603,
            "message": "STORAGE_HEALTH_RECOVERY_REQUIRED"})),
            "research core request failed (-32603)"
        );
    }

    #[test]
    fn startup_progress_accepts_only_host_stages_and_bounded_counters() {
        let state = BackendState::default();
        state.begin_initialization();
        super::record_initialization_progress(
            &state.initialization,
            "OT_STARTUP {\"stage\":\"backing_up\",\"completed_units\":4,\"total_units\":10}\n",
        );
        let status = state.initialization_snapshot();
        assert_eq!(status.stage.as_deref(), Some("backing_up"));
        assert_eq!(status.completed_units, Some(4));
        super::record_initialization_progress(
            &state.initialization,
            "OT_STARTUP {\"stage\":\"secret-provider-url\",\"total_units\":100}\n",
        );
        assert_eq!(
            state.initialization_snapshot().stage.as_deref(),
            Some("backing_up")
        );
        super::record_initialization_progress(&state.initialization, "OT_STARTUP {\"stage\":\"migrating_schema\",\"completed_units\":20,\"total_units\":10}\n");
        assert_eq!(state.initialization_snapshot().completed_units, None);
        state.finish_initialization();
        super::record_initialization_progress(
            &state.initialization,
            "OT_STARTUP {\"stage\":\"checking_schema\"}\n",
        );
        assert_eq!(
            state.initialization_snapshot().stage.as_deref(),
            Some("migrating_schema")
        );
    }

    #[test]
    fn initialization_snapshot_is_readable_while_process_io_mutex_is_held() {
        let state = BackendState::default();
        let (locked_tx, locked_rx) = mpsc::channel();
        let (release_tx, release_rx) = mpsc::channel();
        let lock_state = state.clone();
        let lock_worker = thread::spawn(move || {
            let _process_guard = lock_state.process.lock().unwrap();
            locked_tx.send(()).unwrap();
            release_rx.recv().unwrap();
        });
        locked_rx.recv_timeout(Duration::from_secs(2)).unwrap();

        let (snapshot_tx, snapshot_rx) = mpsc::channel();
        let snapshot_state = state.clone();
        let snapshot_worker = thread::spawn(move || {
            snapshot_tx
                .send(snapshot_state.initialization_snapshot())
                .unwrap();
        });
        let snapshot_result = snapshot_rx.recv_timeout(Duration::from_secs(2));
        release_tx.send(()).unwrap();
        lock_worker.join().unwrap();
        snapshot_worker.join().unwrap();

        let snapshot = snapshot_result.expect("status snapshot must not wait on process I/O");
        let value = serde_json::to_value(snapshot).unwrap();
        assert_eq!(value["phase"], "idle");
        assert_eq!(value["elapsed_seconds"], 0);
        assert_eq!(value["deadline_seconds"], 120);
        assert!(value["safe_failure_code"].is_null());
    }

    #[test]
    fn startup_retry_is_limited_to_one_confirmed_spawn_or_exit_failure() {
        let spawn_failure = StartupFailure::transient("spawn failed", SafeFailureCode::SpawnFailed);
        let exited_failure = startup_failure_from_backend(BackendError::Exited);
        let healthy_timeout = startup_failure_from_backend(BackendError::NoResponse);
        let transport_failure =
            startup_failure_from_backend(BackendError::Transport("send failed".to_string()));

        assert!(startup_retry_allowed(&spawn_failure, 0));
        assert!(!startup_retry_allowed(&spawn_failure, 1));
        assert!(startup_retry_allowed(&exited_failure, 0));
        assert!(!startup_retry_allowed(&healthy_timeout, 0));
        assert!(!startup_retry_allowed(&transport_failure, 0));
    }

    #[test]
    fn initialization_snapshot_reports_safe_failure_code_and_deadline() {
        let state = BackendState::default();
        state.begin_initialization();
        state.fail_initialization(SafeFailureCode::StartupTimeout);

        let value = serde_json::to_value(state.initialization_snapshot()).unwrap();
        assert_eq!(value["phase"], "failed");
        assert_eq!(value["deadline_seconds"], 120);
        assert_eq!(value["safe_failure_code"], "startup_timeout");
        assert!(value["elapsed_seconds"].as_u64().is_some());
    }

    #[test]
    fn blocking_backend_io_does_not_starve_async_status_polling() {
        let (started_tx, started_rx) = mpsc::channel();
        let (release_tx, release_rx) = mpsc::channel();
        let worker = tauri::async_runtime::spawn(run_backend_blocking(move || {
            started_tx.send(()).unwrap();
            release_rx.recv().unwrap();
            17
        }));
        started_rx.recv_timeout(Duration::from_secs(2)).unwrap();

        let poll = tauri::async_runtime::spawn(async {
            tokio::time::sleep(Duration::from_millis(20)).await;
            "status poll ran"
        });
        assert_eq!(
            tauri::async_runtime::block_on(poll).unwrap(),
            "status poll ran"
        );

        release_tx.send(()).unwrap();
        assert_eq!(tauri::async_runtime::block_on(worker).unwrap().unwrap(), 17);
    }

    #[test]
    fn packaged_resource_precedes_portable_directory() {
        let candidates = sidecar_candidates(
            Some(PathBuf::from("resources")),
            Some(PathBuf::from("portable")),
            "openthesis-sidecar.exe",
        );
        assert_eq!(
            candidates,
            vec![
                PathBuf::from("resources/bin/openthesis-sidecar/openthesis-sidecar.exe"),
                PathBuf::from("portable/bin/openthesis-sidecar/openthesis-sidecar.exe"),
            ]
        );
    }

    #[test]
    fn duplicate_directories_are_checked_once() {
        let candidates = sidecar_candidates(
            Some(PathBuf::from("same")),
            Some(PathBuf::from("same")),
            "openthesis-sidecar",
        );
        assert_eq!(
            candidates,
            vec![PathBuf::from(
                "same/bin/openthesis-sidecar/openthesis-sidecar"
            )]
        );
    }

    #[test]
    fn configured_sidecar_path_rejects_blank_relative_and_missing_values() {
        assert_eq!(
            validate_explicit_sidecar_path(Some("  ")).unwrap_err(),
            "explicit sidecar path is empty"
        );
        assert_eq!(
            validate_explicit_sidecar_path(Some("relative-sidecar.exe")).unwrap_err(),
            "explicit sidecar path must be absolute"
        );
        let missing = std::env::temp_dir().join("openthesis-sidecar-does-not-exist.exe");
        assert_eq!(
            validate_explicit_sidecar_path(missing.to_str()).unwrap_err(),
            "explicit sidecar path is unavailable"
        );
    }

    #[cfg(target_os = "windows")]
    #[test]
    fn staged_windows_runtime_is_restored_before_startup() {
        let root = tempfile::tempdir().unwrap();
        let internal = root.path().join("_internal");
        fs::create_dir_all(&internal).unwrap();
        let staged = internal.join("VCRUNTIME140.bin");
        let runtime = internal.join("VCRUNTIME140.dll");
        fs::write(&staged, b"runtime").unwrap();

        prepare_sidecar_runtime(&root.path().join("openthesis-sidecar.exe")).unwrap();

        assert!(!staged.exists());
        assert_eq!(fs::read(runtime).unwrap(), b"runtime");
    }

    #[cfg(target_os = "windows")]
    #[test]
    fn staged_msvc_runtime_is_restored_before_startup() {
        let root = tempfile::tempdir().unwrap();
        let internal = root.path().join("_internal");
        fs::create_dir_all(&internal).unwrap();
        let staged = internal.join("MSVCP140.bin");
        let runtime = internal.join("MSVCP140.dll");
        fs::write(&staged, b"runtime").unwrap();

        prepare_sidecar_runtime(&root.path().join("openthesis-sidecar.exe")).unwrap();

        assert!(!staged.exists());
        assert_eq!(fs::read(runtime).unwrap(), b"runtime");
    }

    #[test]
    fn debug_python_path_rejects_blank_relative_and_missing_values() {
        assert_eq!(
            validate_debug_python(Some("\t")).unwrap_err(),
            "debug Python path is empty"
        );
        assert_eq!(
            validate_debug_python(Some("python.exe")).unwrap_err(),
            "debug Python path must be absolute"
        );
        let missing = std::env::temp_dir().join("openthesis-python-does-not-exist.exe");
        assert_eq!(
            validate_debug_python(missing.to_str()).unwrap_err(),
            "debug Python path is unavailable"
        );
        assert_eq!(
            validate_debug_python(None).unwrap_err(),
            "debug Python path is missing"
        );
    }

    #[test]
    fn startup_hello_result_validation_distinguishes_protocol_failures() {
        assert_eq!(
            validate_hello_result(&serde_json::json!(null)).unwrap_err(),
            "research core startup protocol error"
        );
        assert_eq!(
            validate_hello_result(&serde_json::json!({})).unwrap_err(),
            "research core startup protocol error"
        );
        assert!(validate_hello_result(&serde_json::json!({
            "contract_version":"2.0"
        }))
        .is_ok());
    }

    #[test]
    fn startup_transport_failures_have_stable_categories() {
        let cases = [
            (
                BackendError::Exited,
                true,
                "research core exited during startup",
            ),
            (
                BackendError::Transport("send failed".to_string()),
                false,
                "research core startup transport failed",
            ),
            (
                BackendError::NoResponse,
                false,
                "research core did not respond to startup handshake",
            ),
            (
                BackendError::InvalidJson,
                false,
                "research core returned invalid startup JSON",
            ),
            (
                BackendError::Protocol,
                false,
                "research core startup protocol error",
            ),
        ];
        for (error, retryable, message) in cases {
            let failure = startup_failure_from_backend(error);
            assert_eq!(failure.retryable, retryable);
            assert_eq!(failure.message, message);
        }
    }
}
