mod engine;
mod gui_verify;
mod process_tree;

use engine::{EngineError, EngineSupervisor};
use serde_json::Value;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Arc;
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
                "owned_engine_shutdown_completed": true,
                "engine_path": binary,
                "handshake": handshake,
                "status": status,
                "restart_handshake": restarted,
            }))
        }
        .await;
        supervisor.shutdown().await.map_err(|error| error.message)?;
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
    run_with_verification(None).expect("Could not initialize Q22 Flight Lab desktop shell");
}

pub fn verify_native_gui(
    data_dir: std::path::PathBuf,
    report: std::path::PathBuf,
    nonce: String,
) -> Result<(), String> {
    if cfg!(debug_assertions) {
        return Err(
            "Native GUI qualification requires a release build with bundled assets.".into(),
        );
    }
    run_with_verification(Some(gui_verify::Verification::new(
        data_dir, report, nonce,
    )?))
}

fn run_with_verification(
    verification: Option<Arc<gui_verify::Verification>>,
) -> Result<(), String> {
    let exiting = AtomicBool::new(false);
    let mut context = tauri::generate_context!();
    let verification_window = if verification.is_some() {
        let main = context
            .config()
            .app
            .windows
            .iter()
            .find(|window| window.label == "main")
            .cloned()
            .ok_or("Native GUI qualification requires the production main window.")?;
        for window in &mut context.config_mut().app.windows {
            window.create = false;
        }
        Some(main)
    } else {
        None
    };
    let setup_verification = verification.clone();
    let page_verification = verification.clone();
    let app = tauri::Builder::default()
        .plugin(tauri_plugin_dialog::init())
        .manage(gui_verify::VerificationState(verification.clone()))
        .invoke_handler(tauri::generate_handler![
            engine_request,
            gui_verify::native_gui_smoke_complete
        ])
        .on_page_load(move |webview, payload| {
            let local = (payload.url().scheme() == "tauri"
                && payload.url().host_str() == Some("localhost"))
                || (matches!(payload.url().scheme(), "http" | "https")
                    && payload.url().host_str() == Some("tauri.localhost"));
            if local
                && webview.label() == "main"
                && payload.event() == tauri::webview::PageLoadEvent::Finished
            {
                if let Some(verification) = &page_verification {
                    if let Some(script) = verification.script() {
                        if let Err(error) = webview.eval(&script) {
                            let handle = webview.app_handle().clone();
                            let verification = verification.clone();
                            tauri::async_runtime::spawn(gui_verify::finish(
                                handle,
                                verification,
                                Err(error.to_string()),
                            ));
                        }
                    }
                }
            }
        })
        .setup(move |app| {
            let binary =
                engine::sidecar_path().map_err(|error| std::io::Error::other(error.message))?;
            let data_dir = match &setup_verification {
                Some(verification) => verification.data_dir.clone(),
                None => app.path().app_data_dir()?,
            };
            app.manage(EngineSupervisor::new(binary, data_dir));
            if let (Some(verification), Some(window)) = (&setup_verification, &verification_window)
            {
                // A qualification launch must not read or update the user's
                // normal localStorage/cache. macOS ignores data_directory;
                // incognito selects its ephemeral WKWebView data store.
                tauri::WebviewWindowBuilder::from_config(app, window)?
                    .data_directory(verification.webview_data_dir.clone())
                    .incognito(true)
                    .build()?;
            }
            Ok(())
        })
        .build(context)
        .map_err(|error| error.to_string())?;

    if let Some(verification) = verification.clone() {
        let handle = app.handle().clone();
        tauri::async_runtime::spawn(async move {
            tokio::time::sleep(std::time::Duration::from_secs(60)).await;
            gui_verify::finish(
                handle,
                verification,
                Err("Native GUI qualification exceeded 60 seconds.".into()),
            )
            .await;
        });
    }

    app.run(move |handle, event| {
        if let tauri::RunEvent::ExitRequested { api, code, .. } = event {
            if !exiting.swap(true, Ordering::SeqCst) {
                api.prevent_exit();
                let handle = handle.clone();
                let verification = verification.clone();
                tauri::async_runtime::spawn(async move {
                    if let Some(verification) = verification {
                        if !verification.completed() {
                            gui_verify::finish(
                                handle,
                                verification,
                                Err(
                                    "Application closed before native GUI qualification completed."
                                        .into(),
                                ),
                            )
                            .await;
                            return;
                        }
                    }
                    let exit_code = match handle.state::<EngineSupervisor>().shutdown().await {
                        Ok(()) => code.unwrap_or(0),
                        Err(error) => {
                            eprintln!("Engine shutdown could not be confirmed: {}", error.message);
                            1
                        }
                    };
                    handle.exit(exit_code);
                });
            }
        }
    });
    Ok(())
}
