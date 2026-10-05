mod engine;
mod process_tree;

use engine::{EngineError, EngineSupervisor};
use serde_json::Value;
use std::sync::atomic::{AtomicBool, Ordering};
use tauri::{Manager, State};

/// Native packaging diagnostic: exercise the actual supervisor without opening
/// a webview. The caller supplies an isolated data directory; no host networking
/// changes or imported user files are involved.
pub fn verify_bundled_engine(data_dir: std::path::PathBuf) -> Result<Value, String> {
    let binary = engine::sidecar_path().map_err(|error| error.message)?;
    let supervisor = EngineSupervisor::new(binary.clone(), data_dir);
    tauri::async_runtime::block_on(async {
        let result = async {
            let handshake = supervisor
                .request("handshake".into(), serde_json::json!({}))
                .await?;
            let status = supervisor
                .request("engine.status".into(), serde_json::json!({}))
                .await?;
            let restarted = supervisor
                .request("engine.restart".into(), serde_json::json!({}))
                .await?;
            Ok::<_, EngineError>(serde_json::json!({
                "native_supervisor_verified": true,
                "engine_path": binary,
                "handshake": handshake,
                "status": status,
                "restart_handshake": restarted,
            }))
        }
        .await;
        supervisor.shutdown().await;
        result.map_err(|error| error.message)
    })
}

#[tauri::command]
async fn engine_request(
    method: String,
    params: Value,
    engine: State<'_, EngineSupervisor>,
) -> Result<Value, EngineError> {
    engine.request(method, params).await
}

pub fn run() {
    let exiting = AtomicBool::new(false);
    let app = tauri::Builder::default()
        .plugin(tauri_plugin_dialog::init())
        .invoke_handler(tauri::generate_handler![engine_request])
        .setup(|app| {
            let binary =
                engine::sidecar_path().map_err(|error| std::io::Error::other(error.message))?;
            let data_dir = app.path().app_data_dir()?;
            app.manage(EngineSupervisor::new(binary, data_dir));
            Ok(())
        })
        .build(tauri::generate_context!())
        .expect("Could not initialize Q22 Flight Lab desktop shell");

    app.run(move |handle, event| {
        if let tauri::RunEvent::ExitRequested { api, .. } = event {
            if !exiting.swap(true, Ordering::SeqCst) {
                api.prevent_exit();
                let handle = handle.clone();
                tauri::async_runtime::spawn(async move {
                    handle.state::<EngineSupervisor>().shutdown().await;
                    handle.exit(0);
                });
            }
        }
    });
}
