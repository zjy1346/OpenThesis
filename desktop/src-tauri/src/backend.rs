use serde_json::{json, Value};
use std::io::{BufRead, BufReader, Write};
#[cfg(target_os = "windows")]
use std::os::windows::process::CommandExt;
use std::path::{Path, PathBuf};
use std::process::{Child, ChildStdin, ChildStdout, Command, Stdio};
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::Mutex;
use tauri::{AppHandle, Manager, State};

static REQUEST_ID: AtomicU64 = AtomicU64::new(1);

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
    stdout: BufReader<ChildStdout>,
}

enum BackendError {
    Transport(String),
    Rpc(String),
}

impl BackendProcess {
    fn start(app: &AppHandle) -> Result<Self, String> {
        let mut command = backend_command(app)?;
        configure_backend_command(&mut command);
        let mut child = command
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::null())
            .spawn()
            .map_err(|_| "research core could not be started".to_string())?;
        let stdin = child
            .stdin
            .take()
            .ok_or_else(|| "research core input is unavailable".to_string())?;
        let stdout = child
            .stdout
            .take()
            .ok_or_else(|| "research core output is unavailable".to_string())?;
        Ok(Self {
            child,
            stdin,
            stdout: BufReader::new(stdout),
        })
    }

    fn request(&mut self, method: String, params: Value) -> Result<Value, BackendError> {
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

        let mut response_line = String::new();
        let bytes_read = self
            .stdout
            .read_line(&mut response_line)
            .map_err(|_| BackendError::Transport("research core did not respond".to_string()))?;
        if bytes_read == 0 {
            return Err(BackendError::Transport(
                "research core stopped unexpectedly".to_string(),
            ));
        }
        let response: Value = serde_json::from_str(&response_line).map_err(|_| {
            BackendError::Transport("research core returned an invalid response".to_string())
        })?;
        if response.get("id") != Some(&json!(request_id)) {
            return Err(BackendError::Transport(
                "research core returned an unexpected response".to_string(),
            ));
        }
        if let Some(error) = response.get("error") {
            let message = error
                .get("message")
                .and_then(Value::as_str)
                .unwrap_or("research core request failed");
            return Err(BackendError::Rpc(message.to_string()));
        }
        response.get("result").cloned().ok_or_else(|| {
            BackendError::Transport("research core response is missing a result".to_string())
        })
    }
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
        *process = Some(BackendProcess::start(&app)?);
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
    }
}

fn backend_command(app: &AppHandle) -> Result<Command, String> {
    if let Ok(explicit_path) = std::env::var("OPENTHESIS_SIDECAR_PATH") {
        return Ok(Command::new(explicit_path));
    }

    if cfg!(debug_assertions) {
        let python = std::env::var("OPENTHESIS_PYTHON").unwrap_or_else(|_| "python".to_string());
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
    use super::sidecar_candidates;
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
}
