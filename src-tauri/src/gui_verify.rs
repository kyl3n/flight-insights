//! Opt-in native WebView qualification. The webview never supplies a path or
//! selects a host action; the CLI owns an empty data directory and report file.
use crate::engine::{self, EngineSupervisor};
use serde::Deserialize;
use serde_json::{json, Value};
use std::fs::{File, OpenOptions};
use std::io::Write;
use std::path::PathBuf;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};
use tauri::{AppHandle, Manager, State};

pub struct Verification {
    pub data_dir: PathBuf,
    pub webview_data_dir: PathBuf,
    nonce: String,
    report: Mutex<File>,
    started: AtomicBool,
    completed: AtomicBool,
}

pub struct VerificationState(pub Option<Arc<Verification>>);

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Evidence {
    title: String,
    root_children: u32,
    navigation_buttons: u32,
    rendered_width: f64,
    rendered_height: f64,
    animation_frames: u32,
    engine_connected: bool,
    handshake: Value,
    status: Value,
    restart_handshake: Value,
    phase: String,
    error: Option<String>,
}

impl Verification {
    pub fn new(data_dir: PathBuf, report: PathBuf, nonce: String) -> Result<Arc<Self>, String> {
        if !data_dir.is_absolute() || !report.is_absolute() {
            return Err("GUI verification paths must be absolute.".into());
        }
        if nonce.len() != 64 || !nonce.bytes().all(|value| value.is_ascii_hexdigit()) {
            return Err("GUI verification requires a fresh 32-byte hexadecimal nonce.".into());
        }
        let data_dir = data_dir.canonicalize().map_err(|error| error.to_string())?;
        if data_dir
            .read_dir()
            .map_err(|error| error.to_string())?
            .next()
            .is_some()
        {
            return Err(
                "GUI verification requires an existing empty isolated data directory.".into(),
            );
        }
        let parent = report
            .parent()
            .ok_or("Report parent is missing.")?
            .canonicalize()
            .map_err(|error| error.to_string())?;
        if parent != data_dir || report.file_name().is_none() {
            return Err(
                "GUI report must be a new file directly inside its isolated directory.".into(),
            );
        }
        let report = OpenOptions::new()
            .write(true)
            .create_new(true)
            .open(report)
            .map_err(|error| error.to_string())?;
        Ok(Arc::new(Self {
            data_dir: data_dir.join("engine"),
            webview_data_dir: data_dir.join("webview"),
            nonce,
            report: Mutex::new(report),
            started: AtomicBool::new(false),
            completed: AtomicBool::new(false),
        }))
    }

    pub fn script(&self) -> Option<String> {
        if self.started.swap(true, Ordering::SeqCst) {
            return None;
        }
        Some(
            include_str!("gui_verify.js")
                .replace("__Q22_NONCE_JSON__", &json!(self.nonce).to_string()),
        )
    }

    pub fn completed(&self) -> bool {
        self.completed.load(Ordering::SeqCst)
    }
}

fn validate(evidence: &Evidence) -> Result<(), String> {
    if !matches!(
        evidence.phase.as_str(),
        "waiting_for_ui"
            | "animation_frames"
            | "handshake"
            | "engine_status"
            | "engine_restart"
            | "completion"
    ) {
        return Err("WebView self-test reported an unknown phase.".into());
    }
    if let Some(error) = &evidence.error {
        return Err(format!(
            "WebView self-test failed during {}: {}",
            evidence.phase,
            error.chars().take(1024).collect::<String>()
        ));
    }
    if evidence.title.len() > 256
        || !evidence.title.contains("Q22 Flight Lab")
        || evidence.root_children == 0
        || evidence.navigation_buttons < 6
        || !(evidence.rendered_width > 0.0 && evidence.rendered_width < 100_000.0)
        || !(evidence.rendered_height > 0.0 && evidence.rendered_height < 100_000.0)
        || evidence.animation_frames != 2
        || !evidence.engine_connected
    {
        return Err("Production WebView did not render the connected application.".into());
    }
    if evidence.handshake.get("protocol_version") != Some(&json!(1))
        || evidence.handshake.get("product") != Some(&json!("Q22 Flight Lab"))
        || evidence.status.get("state") != Some(&json!("available"))
        || evidence.restart_handshake.get("protocol_version") != Some(&json!(1))
        || evidence.restart_handshake.get("product") != Some(&json!("Q22 Flight Lab"))
    {
        return Err("Real WebView-to-native-to-engine IPC did not pass.".into());
    }
    if serde_json::to_vec(&json!([
        evidence.handshake,
        evidence.status,
        evidence.restart_handshake
    ]))
    .map_err(|error| error.to_string())?
    .len()
        > 64 * 1024
    {
        return Err("GUI verification evidence exceeds its bound.".into());
    }
    Ok(())
}

pub async fn finish(
    app: AppHandle,
    verification: Arc<Verification>,
    result: Result<Value, String>,
) {
    finish_with_diagnostic(app, verification, result, None).await;
}

fn diagnostic(evidence: &Evidence) -> Value {
    // Preserve only bounded qualification fields, never arbitrary IPC payloads,
    // stack traces, data paths, invocation tokens or application records.
    let text = |value: &str| value.chars().take(1024).collect::<String>();
    let field = |value: &Value, name: &str| value.get(name).and_then(Value::as_str).map(text);
    json!({
        "phase":evidence.phase.chars().take(32).collect::<String>(),
        "error":evidence.error.as_deref().map(text),
        "title":evidence.title.chars().take(256).collect::<String>(),
        "root_children":evidence.root_children,"navigation_buttons":evidence.navigation_buttons,
        "rendered_width":evidence.rendered_width,"rendered_height":evidence.rendered_height,
        "animation_frames":evidence.animation_frames,"engine_connected":evidence.engine_connected,
        "handshake_product":field(&evidence.handshake,"product"),
        "handshake_protocol_version":evidence.handshake.get("protocol_version").and_then(Value::as_u64),
        "engine_state":field(&evidence.status,"state"),
        "restart_product":field(&evidence.restart_handshake,"product"),
        "restart_protocol_version":evidence.restart_handshake.get("protocol_version").and_then(Value::as_u64),
    })
}

async fn finish_with_diagnostic(
    app: AppHandle,
    verification: Arc<Verification>,
    result: Result<Value, String>,
    diagnostic: Option<Value>,
) {
    if verification.completed.swap(true, Ordering::SeqCst) {
        return;
    }
    let shutdown = app.state::<EngineSupervisor>().shutdown().await;
    let passed = result.is_ok() && shutdown.is_ok();
    let report = json!({
        "schema_version":1, "native_gui_rendering": if passed {"verified"} else {"failed"},
        "production_webview":true, "engine_path":engine::sidecar_path().ok(),
        "isolated_data":true, "isolated_engine_data":true, "isolated_webview_data":true,
        "owned_engine_shutdown_completed":shutdown.is_ok(),
        "shutdown_error":shutdown.as_ref().err().map(|error| &error.message),
        "external_network_actions_requested":false, "security_bypasses":false,
        "nonce":verification.nonce,
        "evidence":result.as_ref().ok(), "error":result.as_ref().err(),
        "diagnostic":diagnostic,
        "limitations":["Basic native WebView rendering and IPC; not physical display, hardware-stream, permission-dialog or accessibility qualification."]
    });
    let written = (|| -> Result<(), String> {
        let mut file = verification
            .report
            .lock()
            .map_err(|_| "GUI report lock failed.")?;
        serde_json::to_writer_pretty(&mut *file, &report).map_err(|error| error.to_string())?;
        file.write_all(b"\n")
            .and_then(|_| file.sync_all())
            .map_err(|error| error.to_string())
    })();
    app.exit(if passed && written.is_ok() { 0 } else { 1 });
}

#[tauri::command]
pub async fn native_gui_smoke_complete(
    nonce: String,
    evidence: Evidence,
    app: AppHandle,
    state: State<'_, VerificationState>,
) -> Result<(), String> {
    let verification = state
        .0
        .as_ref()
        .ok_or("Native GUI qualification is not active.")?
        .clone();
    if nonce != verification.nonce || verification.completed() {
        return Err("GUI verification invocation does not match.".into());
    }
    let details = diagnostic(&evidence);
    let result = validate(&evidence).map(|()| {
        json!({
            "title":evidence.title, "root_children":evidence.root_children,
            "navigation_buttons":evidence.navigation_buttons,
            "rendered_width":evidence.rendered_width,"rendered_height":evidence.rendered_height,
            "animation_frames":evidence.animation_frames,"engine_connected":true,
            "handshake":evidence.handshake,"status":evidence.status,
            "restart_handshake":evidence.restart_handshake,
        })
    });
    finish_with_diagnostic(app, verification, result, Some(details)).await;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn invocation_cannot_reuse_data_or_choose_an_external_report() {
        let parent = std::env::temp_dir().join(format!(
            "q22-gui-scope-{}-{}",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ));
        let data = parent.join("isolated");
        std::fs::create_dir_all(&data).unwrap();
        let nonce = "a".repeat(64);
        assert!(
            Verification::new(data.clone(), parent.join("outside.json"), nonce.clone()).is_err()
        );
        assert!(!parent.join("outside.json").exists());
        assert!(
            Verification::new(data.clone(), data.join("invalid.json"), "short".into()).is_err()
        );
        assert!(!data.join("invalid.json").exists());
        let valid =
            Verification::new(data.clone(), data.join("report.json"), nonce.clone()).unwrap();
        assert!(Verification::new(data.clone(), data.join("second.json"), nonce).is_err());
        drop(valid);
        std::fs::remove_dir_all(parent).unwrap();
    }

    #[test]
    fn rejects_disconnected_or_fabricated_empty_render() {
        let mut evidence: Evidence = serde_json::from_value(json!({
            "title":"Q22 Flight Lab", "root_children":1,"navigation_buttons":6,
            "rendered_width":880,"rendered_height":640,"animation_frames":2,
            "engine_connected":true,"handshake":{"product":"Q22 Flight Lab","protocol_version":1},
            "status":{"state":"available"},"restart_handshake":{"product":"Q22 Flight Lab","protocol_version":1},"phase":"completion","error":null
        })).unwrap();
        assert!(validate(&evidence).is_ok());
        evidence.rendered_width = 0.0;
        assert!(validate(&evidence).is_err());
        evidence.rendered_width = 880.0;
        evidence.engine_connected = false;
        assert!(validate(&evidence).is_err());
        evidence.engine_connected = true;
        evidence.restart_handshake = json!({"product":"Other","protocol_version":1});
        assert!(validate(&evidence).is_err());
        evidence.phase = "engine_restart".into();
        evidence.error = Some("x".repeat(2000));
        let details = diagnostic(&evidence);
        assert_eq!(details["phase"], "engine_restart");
        assert_eq!(details["error"].as_str().unwrap().len(), 1024);
        assert!(details.get("handshake").is_none());
        assert!(validate(&evidence).unwrap_err().contains("engine_restart"));
    }
}
