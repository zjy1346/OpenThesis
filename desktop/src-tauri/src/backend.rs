use serde_json::{json, Value};
use std::io::{BufRead, BufReader, Write};
#[cfg(target_os = "windows")]
use std::os::windows::process::CommandExt;
use std::path::{Path, PathBuf};
use std::process::{Child, ChildStdin, Command, Stdio};
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::mpsc::{self, Receiver, RecvTimeoutError};
use std::sync::Mutex;
use std::thread;
use std::time::Duration;
use tauri::{AppHandle, Manager, State};

static REQUEST_ID: AtomicU64 = AtomicU64::new(1);
const STARTUP_TIMEOUT: Duration = Duration::from_secs(3);
const RESPONSE_TIMEOUT: Duration = Duration::from_secs(600);
const MAX_RESPONSE_BYTES: usize = 16 * 1024 * 1024;

/// Owns the single long-lived research-core process for a desktop window.
///
/// The Tauri command is intentionally the only public seam. Process startup,
/// JSON-RPC framing, response correlation, and restart-on-transport-failure are
/// hidden here so another desktop adapter can reuse the same lifecycle rules.
#[derive(Default)]
pub(crate) struct BackendState {
    process: Mutex<Option<BackendProcess>>,
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
}

impl StartupFailure {
    fn fatal(message: impl Into<String>) -> Self {
        Self {
            message: message.into(),
            retryable: false,
        }
    }

    fn transient(message: impl Into<String>) -> Self {
        Self {
            message: message.into(),
            retryable: true,
        }
    }
}

impl BackendProcess {
    fn start(app: &AppHandle) -> Result<Self, StartupFailure> {
        let mut command = backend_command(app).map_err(StartupFailure::fatal)?;
        configure_model_gateway_environment(app, &mut command).map_err(StartupFailure::fatal)?;
        configure_backend_command(&mut command);
        let mut child = command
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::null())
            .spawn()
            .map_err(|_| StartupFailure::transient("research core could not be started (spawn)"))?;
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
            .request_with_timeout("system.hello".to_string(), json!({}), STARTUP_TIMEOUT)
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
            let code = error
                .get("code")
                .and_then(Value::as_i64)
                .map(|value| value.to_string())
                .unwrap_or_else(|| "unknown".to_string());
            return Err(BackendError::Rpc(format!(
                "research core request failed ({code})"
            )));
        }
        response
            .get("result")
            .cloned()
            .ok_or_else(|| BackendError::Protocol)
    }
}

fn startup_failure_from_backend(error: BackendError) -> StartupFailure {
    match error {
        BackendError::Transport(_) => {
            StartupFailure::transient("research core startup transport failed")
        }
        BackendError::Exited => StartupFailure::transient("research core exited during startup"),
        BackendError::NoResponse => {
            StartupFailure::transient("research core did not respond to startup handshake")
        }
        BackendError::InvalidJson => {
            StartupFailure::fatal("research core returned invalid startup JSON")
        }
        BackendError::Protocol | BackendError::Rpc(_) => {
            StartupFailure::fatal("research core startup protocol error")
        }
    }
}

fn validate_hello_result(response: &Value) -> Result<(), String> {
    let result = response
        .as_object()
        .ok_or_else(|| "research core startup protocol error".to_string())?;
    if result.get("contract_version").and_then(Value::as_str) != Some("2.0") {
        return Err("research core startup protocol error".to_string());
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
pub(crate) fn backend_request(
    app: AppHandle,
    state: State<'_, BackendState>,
    method: String,
    params: Value,
) -> Result<Value, String> {
    let mut process = state
        .process
        .lock()
        .map_err(|_| "research core state is unavailable".to_string())?;
    if process.is_none() {
        let mut last_failure = None;
        for attempt in 0..2 {
            match BackendProcess::start(&app) {
                Ok(started) => {
                    *process = Some(started);
                    break;
                }
                Err(failure) if failure.retryable && attempt == 0 => {
                    last_failure = Some(failure.message);
                }
                Err(failure) => {
                    last_failure = Some(failure.message);
                    break;
                }
            }
        }
        if process.is_none() {
            return Err(
                last_failure.unwrap_or_else(|| "research core could not be started".to_string())
            );
        }
    }
    let result = process
        .as_mut()
        .ok_or_else(|| "research core could not be started".to_string())?
        .request(method, params);
    match result {
        Ok(value) => Ok(value),
        Err(BackendError::Rpc(message)) => Err(message),
        Err(BackendError::Transport(message)) => {
            *process = None;
            Err(message)
        }
        Err(BackendError::Exited) => {
            *process = None;
            Err("research core stopped unexpectedly".to_string())
        }
        Err(BackendError::NoResponse) => {
            *process = None;
            Err("research core did not respond".to_string())
        }
        Err(BackendError::InvalidJson) => {
            *process = None;
            Err("research core returned an invalid response".to_string())
        }
        Err(BackendError::Protocol) => {
            *process = None;
            Err("research core returned an invalid protocol response".to_string())
        }
    }
}

fn backend_command(app: &AppHandle) -> Result<Command, String> {
    if let Ok(explicit_path) = std::env::var("OPENTHESIS_SIDECAR_PATH") {
        return Ok(Command::new(validate_explicit_sidecar_path(Some(
            &explicit_path,
        ))?));
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
    Ok(Command::new(sidecar_path))
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
        sidecar_candidates, startup_failure_from_backend, validate_debug_python,
        validate_explicit_sidecar_path, validate_hello_result, BackendError,
    };
    use std::path::PathBuf;

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
                BackendError::NoResponse,
                true,
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
