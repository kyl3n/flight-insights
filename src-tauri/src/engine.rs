//! One supervised packaged process, with bounded newline JSON-RPC frames.
//! Renderer input can select a known operation, never a program or shell command.
use crate::process_tree::{self, ProcessTree};
use serde::Serialize;
use serde_json::{json, Value};
use std::{
    io::Write,
    path::{Path, PathBuf},
    time::Duration,
};
use tokio::{
    io::{AsyncBufRead, AsyncBufReadExt, AsyncReadExt, AsyncWriteExt, BufReader},
    process::{Child, ChildStdin, ChildStdout, Command},
    sync::{mpsc, oneshot, watch, Mutex, Semaphore},
    task::JoinHandle,
};

const MAX_REQUEST: usize = 1024 * 1024;
const MAX_RESPONSE: usize = 4 * 1024 * 1024;
const REQUEST_TIMEOUT: Duration = Duration::from_secs(120);
const HANDSHAKE_TIMEOUT: Duration = Duration::from_secs(15);
const QUEUE_CAPACITY: usize = 8;
const ADMISSION_TIMEOUT: Duration = Duration::from_secs(15);
// Include callers awaiting admission, queued jobs, and the active request in
// one bound. Startup can mount several panels concurrently without turning the
// eight-slot dispatch queue into an unbounded collection of request payloads.
const MAX_PENDING_REQUESTS: usize = 32;
const METHODS: &[&str] = &[
    "handshake",
    "engine.status",
    "sources.capabilities",
    "catalog.status",
    "catalog.verify",
    "network.inspect",
    "network.probe",
    "network.plan",
    "network.apply",
    "network.rollback",
    "network.recovery",
    "network.profile",
    "network.capabilities",
    "network.bench_opt_in",
    "session.preview",
    "session.prepare_and_watch",
    "session.status",
    "session.detail",
    "session.stop",
    "associations.evaluate",
    "associations.review",
    "parameters.import",
    "parameters.list",
    "parameters.diff",
    "sessions.import",
    "sessions.list",
    "comparison.run",
    "logs.import",
    "logs.list",
    "logs.detail",
    "logs.session",
    "insights.report",
    "insights.list",
    "insights.detail",
    "insights.review_packet",
    "insights.export_review",
    "acquisition.preview",
    "acquisition.parameters.capture",
    "acquisition.logs.list",
    "acquisition.logs.download",
    "acquisition.list",
    "acquisition.detail",
    "orchestration.preview",
    "orchestration.policy.save",
    "orchestration.policy.list",
    "orchestration.status",
    "orchestration.retrieval.select",
    "reconciliation.scan_folder",
    "reconciliation.index_source",
    "reconciliation.get",
    "reconciliation.list",
    "alignment.reference",
    "alignment.analyze",
    "alignment.get",
    "alignment.list",
    "alignment.attach",
    "radio.capture",
    "radio.list",
    "radio.detail",
    "jobs.get",
    "jobs.list",
    "jobs.cancel",
];

#[derive(Clone, Debug, Serialize)]
pub struct EngineError {
    pub code: String,
    pub message: String,
}

impl EngineError {
    fn new(code: &str, message: impl Into<String>) -> Self {
        Self {
            code: code.into(),
            message: message.into(),
        }
    }
    fn io(error: std::io::Error) -> Self {
        Self::new(
            "engine_io",
            format!("Analysis engine connection failed: {error}"),
        )
    }
}

type Reply = Result<Value, EngineError>;
struct Job {
    method: String,
    params: Value,
    result: oneshot::Sender<Reply>,
}

pub struct EngineSupervisor {
    queue: mpsc::Sender<Job>,
    pending: Semaphore,
    admission_timeout: Duration,
    shutdown: watch::Sender<bool>,
    task: Mutex<ShutdownTask>,
}

struct ShutdownTask {
    handle: Option<tauri::async_runtime::JoinHandle<Result<(), EngineError>>>,
    outcome: Option<Result<(), EngineError>>,
}

impl EngineSupervisor {
    pub fn new(binary: PathBuf, data_dir: PathBuf) -> Self {
        let (queue, receiver) = mpsc::channel(QUEUE_CAPACITY);
        let (shutdown, closed) = watch::channel(false);
        let task = tauri::async_runtime::spawn(supervise(binary, data_dir, receiver, closed));
        Self {
            queue,
            pending: Semaphore::new(MAX_PENDING_REQUESTS),
            admission_timeout: ADMISSION_TIMEOUT,
            shutdown,
            task: Mutex::new(ShutdownTask {
                handle: Some(task),
                outcome: None,
            }),
        }
    }

    pub async fn request(&self, method: String, params: Value) -> Reply {
        if method != "engine.restart" && !METHODS.contains(&method.as_str()) {
            return Err(EngineError::new(
                "method_not_allowed",
                "Unknown desktop engine operation.",
            ));
        }
        if !params.is_object() {
            return Err(EngineError::new(
                "invalid_params",
                "Engine parameters must be a JSON object.",
            ));
        }
        let encoded = serde_json::to_vec(&params)
            .map_err(|_| EngineError::new("invalid_params", "Parameters could not be encoded."))?;
        // Reserve room for the envelope and prevent expensive work from filling the queue.
        if encoded.len() > MAX_REQUEST - 1024 {
            return Err(EngineError::new(
                "request_too_large",
                "Request exceeds the 1 MiB IPC limit. Import a file through the native picker.",
            ));
        }
        drop(encoded);
        let mut shutdown = self.shutdown.subscribe();
        if *shutdown.borrow() {
            return Err(EngineError::new(
                "engine_stopped",
                "The application is shutting down.",
            ));
        }
        if self.queue.is_closed() {
            return Err(EngineError::new(
                "engine_stopped",
                "The engine supervisor has stopped. The operation was not dispatched.",
            ));
        }
        let _pending = self.pending.try_acquire().map_err(|_| {
            EngineError::new(
                "engine_busy",
                "Too many engine requests are pending. This operation was not dispatched.",
            )
        })?;
        let (result, receiver) = oneshot::channel();
        // reserve() is cancellation-safe: a timeout or dropped invoke before
        // admission cannot leave a job behind. After send, there is exactly one
        // dispatch attempt; a transport failure never repeats the operation.
        let slot = tokio::select! {
            biased;
            _ = shutdown.changed() => return Err(EngineError::new(
                "engine_stopped", "Application shutdown cancelled request admission. The operation was not dispatched.")),
            slot = tokio::time::timeout(self.admission_timeout, self.queue.reserve()) => {
                match slot {
                    Ok(Ok(slot)) => slot,
                    Ok(Err(_)) => return Err(EngineError::new(
                        "engine_stopped", "The engine supervisor stopped before admission. The operation was not dispatched.")),
                    Err(_) => return Err(EngineError::new(
                        "engine_busy", "The engine request queue remained full until the admission deadline. This operation was not dispatched.")),
                }
            },
        };
        if *shutdown.borrow() {
            return Err(EngineError::new(
                "engine_stopped",
                "Application shutdown cancelled request admission. The operation was not dispatched.",
            ));
        }
        slot.send(Job {
            method,
            params,
            result,
        });
        receiver.await.unwrap_or_else(|_| Err(EngineError::new("engine_stopped", "The engine stopped before the operation completed. No operation was automatically repeated.")))
    }

    pub async fn shutdown(&self) -> Result<(), EngineError> {
        let _ = self.shutdown.send(true);
        let mut state = self.task.lock().await;
        if let Some(outcome) = &state.outcome {
            return outcome.clone();
        }
        // Keep the handle in the state while awaiting it: cancellation of a
        // caller must not detach teardown or turn a later failure into success.
        let outcome = match state.handle.as_mut() {
            Some(task) => task
                .await
                .map_err(|error| {
                    EngineError::new(
                        "engine_shutdown",
                        format!("Engine supervisor did not complete: {error}"),
                    )
                })
                .and_then(|result| result),
            None => Err(EngineError::new(
                "engine_shutdown",
                "Engine shutdown outcome is unavailable.",
            )),
        };
        state.handle = None;
        state.outcome = Some(outcome.clone());
        outcome
    }
}

async fn supervise(
    binary: PathBuf,
    data_dir: PathBuf,
    mut jobs: mpsc::Receiver<Job>,
    mut shutdown: watch::Receiver<bool>,
) -> Result<(), EngineError> {
    let mut engine: Option<EngineProcess> = None;
    let mut failed = false;
    loop {
        let job = tokio::select! {
            biased;
            _ = shutdown.changed() => break,
            job = jobs.recv() => match job { Some(job) => job, None => break },
        };
        if job.result.is_closed() {
            continue;
        }
        if failed && job.method != "engine.restart" {
            let _ = job.result.send(Err(EngineError::new("engine_unavailable", "The analysis engine is unavailable. Restart it explicitly, then inspect operation status before retrying.")));
            continue;
        }
        // Cancelling this future drops any in-flight I/O before stop() is invoked below.
        let mut result = tokio::select! {
            biased;
            _ = shutdown.changed() => break,
            result = async {
                if job.method == "engine.restart" {
                    if let Some(process) = engine.as_mut() { process.stop().await?; }
                    engine = None;
                }
                if engine.is_none() {
                    engine = Some(EngineProcess::spawn(&binary, &data_dir).await?);
                }
                let process = engine.as_mut().expect("engine was initialized above");
                if job.method == "engine.restart" || job.method == "handshake" {
                    process.ensure_running()?;
                    Ok(process.handshake.clone())
                } else {
                    process.call(&job.method, job.params, REQUEST_TIMEOUT).await
                }
            } => result,
        };
        // Application-level errors leave the transport usable. Protocol/I/O/timeouts do not.
        let fatal = result
            .as_ref()
            .err()
            .is_some_and(|error| error.code != "engine_error");
        if fatal {
            let stopped = match engine.as_mut() {
                Some(process) => process.stop().await,
                None => Ok(()),
            };
            match stopped {
                Ok(()) => engine = None,
                Err(error) => result = Err(error),
            }
            failed = true;
        } else {
            failed = false;
        }
        let _ = job.result.send(result);
    }
    jobs.close();
    let outcome = match engine {
        Some(mut process) => process.stop().await,
        None => Ok(()),
    };
    while let Some(job) = jobs.recv().await {
        let _ = job.result.send(Err(EngineError::new(
            "engine_stopped",
            "Application shutdown cancelled the queued operation.",
        )));
    }
    outcome
}

struct EngineProcess {
    child: Child,
    process_tree: ProcessTree,
    input: Option<ChildStdin>,
    output: BufReader<ChildStdout>,
    stderr_task: JoinHandle<()>,
    next_id: u64,
    handshake: Value,
}

impl EngineProcess {
    fn ensure_running(&mut self) -> Result<(), EngineError> {
        if self.child.try_wait().map_err(EngineError::io)?.is_some() {
            return Err(EngineError::new("engine_crashed", "The analysis engine exited. Completed archives remain on disk; inspect status after restarting."));
        }
        Ok(())
    }

    async fn spawn(binary: &PathBuf, data_dir: &PathBuf) -> Result<Self, EngineError> {
        std::fs::create_dir_all(data_dir).map_err(EngineError::io)?;
        let mut command = Command::new(binary);
        command
            .arg("--data-dir")
            .arg(data_dir)
            .stdin(std::process::Stdio::piped())
            .stdout(std::process::Stdio::piped())
            .stderr(std::process::Stdio::piped())
            .kill_on_drop(true);
        process_tree::configure(&mut command);
        let mut child = command.spawn().map_err(|error| EngineError::new("engine_start", format!("Could not start the bundled engine: {error}. Reinstall a matching application bundle; developers can run scripts/package-engine.py.")))?;
        let process_tree = ProcessTree::attach(&child).map_err(EngineError::io)?;
        let input = child
            .stdin
            .take()
            .ok_or_else(|| EngineError::new("engine_start", "Missing engine input pipe."))?;
        let output = BufReader::new(
            child
                .stdout
                .take()
                .ok_or_else(|| EngineError::new("engine_start", "Missing engine output pipe."))?,
        );
        let mut stderr = child
            .stderr
            .take()
            .ok_or_else(|| EngineError::new("engine_start", "Missing engine diagnostic pipe."))?;
        // Drain diagnostic output separately, using fixed storage. It must never become RPC.
        // Do not expose raw source contents or credentials through a webview console.
        let log_path = data_dir.join("logs").join("engine-stderr.log");
        let stderr_task = tokio::spawn(async move {
            let mut buffer = [0u8; 4096];
            while let Ok(size) = stderr.read(&mut buffer).await {
                if size == 0 {
                    break;
                }
                write_diagnostic_log(&log_path, &buffer[..size]);
            }
        });
        let mut process = Self {
            child,
            process_tree,
            input: Some(input),
            output,
            stderr_task,
            next_id: 1,
            handshake: Value::Null,
        };
        let response = process.call("handshake", json!({"protocol_version": 1, "client": "q22-desktop", "client_version": env!("CARGO_PKG_VERSION")}), HANDSHAKE_TIMEOUT).await;
        match response.and_then(validate_handshake) {
            Ok(handshake) => {
                process.handshake = handshake;
                Ok(process)
            }
            Err(error) => {
                let _ = process.stop().await;
                Err(error)
            }
        }
    }

    async fn call(&mut self, method: &str, params: Value, duration: Duration) -> Reply {
        let id = self.next_id;
        self.next_id = self.next_id.checked_add(1).ok_or_else(|| {
            EngineError::new("protocol_error", "Engine request counter exhausted.")
        })?;
        let mut request = serde_json::to_vec(
            &json!({"jsonrpc": "2.0", "id": id, "method": method, "params": params}),
        )
        .map_err(|_| EngineError::new("protocol_error", "Request encoding failed."))?;
        if request.len() > MAX_REQUEST {
            return Err(EngineError::new(
                "request_too_large",
                "Engine request exceeds 1 MiB.",
            ));
        }
        request.push(b'\n');
        tokio::time::timeout(duration, async {
            self.ensure_running()?;
            let input = self.input.as_mut().ok_or_else(|| EngineError::new("engine_stopped", "Engine input is closed."))?;
            input.write_all(&request).await.map_err(EngineError::io)?;
            input.flush().await.map_err(EngineError::io)?;
            let response = read_frame(&mut self.output, MAX_RESPONSE).await?;
            parse_response(&response, id)
        }).await.map_err(|_| EngineError::new("engine_timeout", "The engine exceeded its response timeout and was stopped. The operation was not retried; inspect its outcome after restarting."))?
    }

    async fn stop(&mut self) -> Result<(), EngineError> {
        // EOF gives Python time to close SQLite and complete shutdown without a special RPC.
        drop(self.input.take());
        if tokio::time::timeout(Duration::from_secs(3), self.child.wait())
            .await
            .is_err()
        {
            self.process_tree.terminate();
            let _ = self.child.kill().await;
            let _ = self.child.wait().await;
        }
        // Also clean up descendants if the launcher itself crashed first.
        let terminated = self.process_tree.terminate_and_wait().await;
        self.stderr_task.abort();
        terminated.map_err(|error| EngineError::new("engine_shutdown", error.to_string()))
    }
}

fn write_diagnostic_log(path: &Path, bytes: &[u8]) {
    // Two bounded local files. Diagnostics stay outside the webview and may
    // contain local source paths; no upload or renderer access is provided.
    let Some(parent) = path.parent() else {
        return;
    };
    if std::fs::create_dir_all(parent).is_err() {
        return;
    }
    if std::fs::metadata(path)
        .is_ok_and(|metadata| metadata.len() + bytes.len() as u64 > 1024 * 1024)
    {
        let previous = path.with_extension("previous.log");
        let _ = std::fs::remove_file(&previous);
        if std::fs::rename(path, previous).is_err() {
            return;
        }
    }
    let mut options = std::fs::OpenOptions::new();
    options.create(true).append(true);
    #[cfg(unix)]
    {
        use std::os::unix::fs::OpenOptionsExt;
        options.mode(0o600);
    }
    if let Ok(mut file) = options.open(path) {
        let _ = file.write_all(bytes);
    }
}

async fn read_frame<R: AsyncBufRead + Unpin>(
    reader: &mut R,
    limit: usize,
) -> Result<Vec<u8>, EngineError> {
    let mut frame = Vec::new();
    loop {
        let data = reader.fill_buf().await.map_err(EngineError::io)?;
        if data.is_empty() {
            return Err(EngineError::new(
                "engine_crashed",
                "Engine output ended before a complete response.",
            ));
        }
        let newline = data.iter().position(|byte| *byte == b'\n');
        let size = newline.unwrap_or(data.len());
        if frame.len() + size > limit {
            return Err(EngineError::new(
                "response_too_large",
                "Engine response exceeds 4 MiB. Narrow the requested data scope.",
            ));
        }
        frame.extend_from_slice(&data[..size]);
        reader.consume(size + usize::from(newline.is_some()));
        if newline.is_some() {
            return Ok(frame);
        }
    }
}

fn validate_handshake(value: Value) -> Reply {
    if value.get("protocol_version").and_then(Value::as_u64) != Some(1)
        || value.get("product").and_then(Value::as_str) != Some("Q22 Flight Lab")
        || value
            .get("engine_version")
            .and_then(Value::as_str)
            .is_none()
        || value
            .get("capabilities")
            .and_then(Value::as_array)
            .is_none()
    {
        return Err(EngineError::new("protocol_mismatch", "The packaged engine does not support Q22 Flight Lab protocol version 1. Reinstall a matching application bundle."));
    }
    Ok(value)
}

fn parse_response(bytes: &[u8], id: u64) -> Reply {
    let value: Value = serde_json::from_slice(bytes)
        .map_err(|_| EngineError::new("protocol_error", "Engine response is not valid JSON."))?;
    if value.get("jsonrpc").and_then(Value::as_str) != Some("2.0")
        || value.get("id").and_then(Value::as_u64) != Some(id)
        || value.get("result").is_some() == value.get("error").is_some()
    {
        return Err(EngineError::new(
            "protocol_error",
            "Engine response does not match the pending request.",
        ));
    }
    if let Some(error) = value.get("error") {
        let message = error
            .get("message")
            .and_then(Value::as_str)
            .ok_or_else(|| {
                EngineError::new(
                    "protocol_error",
                    "Engine returned an invalid error envelope.",
                )
            })?;
        if error.get("code").and_then(Value::as_i64).is_none() {
            return Err(EngineError::new(
                "protocol_error",
                "Engine error has no numeric JSON-RPC code.",
            ));
        }
        return Err(EngineError::new("engine_error", message));
    }
    Ok(value["result"].clone())
}

/// externalBin is installed beside the desktop executable. Development uses the
/// same frozen executable with Tauri's target-triple suffix; no Python fallback.
pub fn sidecar_path() -> Result<PathBuf, EngineError> {
    let suffix = if cfg!(windows) { ".exe" } else { "" };
    let current = std::env::current_exe().map_err(EngineError::io)?;
    let packaged = current
        .parent()
        .ok_or_else(|| {
            EngineError::new(
                "engine_start",
                "Application executable has no parent directory.",
            )
        })?
        .join(format!("q22-engine{suffix}"));
    if packaged.is_file() {
        return Ok(packaged);
    }
    #[cfg(debug_assertions)]
    {
        let target = if cfg!(target_os = "windows") {
            "x86_64-pc-windows-msvc"
        } else if cfg!(target_os = "macos") && cfg!(target_arch = "aarch64") {
            "aarch64-apple-darwin"
        } else if cfg!(target_os = "macos") {
            "x86_64-apple-darwin"
        } else {
            "x86_64-unknown-linux-gnu"
        };
        let development = PathBuf::from(env!("CARGO_MANIFEST_DIR"))
            .join("binaries")
            .join(format!("q22-engine-{target}{suffix}"));
        Ok(development)
    }
    #[cfg(not(debug_assertions))]
    Ok(packaged)
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::Arc;

    fn admission_fixture(
        timeout: Duration,
        limit: usize,
    ) -> (
        Arc<EngineSupervisor>,
        mpsc::Receiver<Job>,
        watch::Receiver<bool>,
    ) {
        let (queue, receiver) = mpsc::channel(1);
        let (shutdown, closed) = watch::channel(false);
        let engine = Arc::new(EngineSupervisor {
            queue,
            pending: Semaphore::new(limit),
            admission_timeout: timeout,
            shutdown,
            task: Mutex::new(ShutdownTask {
                handle: None,
                outcome: None,
            }),
        });
        (engine, receiver, closed)
    }

    async fn occupy_queue(engine: &EngineSupervisor) -> oneshot::Receiver<Reply> {
        let (result, receiver) = oneshot::channel();
        engine
            .queue
            .send(Job {
                method: "handshake".into(),
                params: json!({}),
                result,
            })
            .await
            .unwrap();
        receiver
    }

    async fn await_pending(engine: &EngineSupervisor, remaining: usize) {
        tokio::time::timeout(Duration::from_secs(5), async {
            while engine.pending.available_permits() != remaining {
                tokio::task::yield_now().await;
            }
        })
        .await
        .expect("request reached bounded admission");
    }

    #[tokio::test]
    async fn admission_waits_for_capacity_and_dispatches_mutation_exactly_once() {
        let (engine, mut queue, _closed) = admission_fixture(Duration::from_secs(5), 1);
        let _occupied = occupy_queue(&engine).await;
        let caller = engine.clone();
        let request = tokio::spawn(async move {
            caller
                .request("network.apply".into(), json!({"plan_id":"one-attempt"}))
                .await
        });
        await_pending(&engine, 0).await;
        assert!(!request.is_finished());
        assert_eq!(queue.recv().await.unwrap().method, "handshake");
        let job = tokio::time::timeout(Duration::from_secs(5), queue.recv())
            .await
            .unwrap()
            .unwrap();
        assert_eq!(job.method, "network.apply");
        assert_eq!(job.params, json!({"plan_id":"one-attempt"}));
        // A dispatched request retains its pending permit until its reply.
        assert_eq!(engine.pending.available_permits(), 0);
        job.result.send(Ok(json!({"attempts":1}))).unwrap();
        assert_eq!(request.await.unwrap().unwrap(), json!({"attempts":1}));
        assert!(matches!(
            queue.try_recv(),
            Err(mpsc::error::TryRecvError::Empty)
        ));
        assert_eq!(engine.pending.available_permits(), 1);
    }

    #[tokio::test]
    async fn admission_timeout_and_cancellation_never_leave_mutations_queued() {
        let (engine, mut queue, _closed) = admission_fixture(Duration::from_millis(20), 1);
        let _occupied = occupy_queue(&engine).await;
        let error = engine
            .request("network.apply".into(), json!({}))
            .await
            .unwrap_err();
        assert_eq!(error.code, "engine_busy");
        assert!(error.message.contains("not dispatched"));
        queue.recv().await.unwrap();
        assert!(matches!(
            queue.try_recv(),
            Err(mpsc::error::TryRecvError::Empty)
        ));
        assert_eq!(engine.pending.available_permits(), 1);

        let (engine, mut queue, _closed) = admission_fixture(Duration::from_secs(5), 1);
        let _occupied = occupy_queue(&engine).await;
        let caller = engine.clone();
        let request =
            tokio::spawn(async move { caller.request("network.apply".into(), json!({})).await });
        await_pending(&engine, 0).await;
        request.abort();
        assert!(request.await.unwrap_err().is_cancelled());
        queue.recv().await.unwrap();
        assert!(matches!(
            queue.try_recv(),
            Err(mpsc::error::TryRecvError::Empty)
        ));
        assert_eq!(engine.pending.available_permits(), 1);
    }

    #[tokio::test]
    async fn pending_admission_is_bounded_without_enlarging_the_dispatch_queue() {
        let (engine, mut queue, _closed) = admission_fixture(Duration::from_secs(5), 1);
        let _occupied = occupy_queue(&engine).await;
        let caller = engine.clone();
        let first =
            tokio::spawn(async move { caller.request("engine.status".into(), json!({})).await });
        await_pending(&engine, 0).await;
        let error = engine
            .request("network.apply".into(), json!({}))
            .await
            .unwrap_err();
        assert_eq!(error.code, "engine_busy");
        assert!(error.message.contains("Too many"));
        assert_eq!(queue.len(), 1);
        first.abort();
        assert!(first.await.unwrap_err().is_cancelled());
        queue.recv().await.unwrap();
        assert!(matches!(
            queue.try_recv(),
            Err(mpsc::error::TryRecvError::Empty)
        ));
    }

    #[tokio::test]
    async fn closed_queue_is_stopped_both_before_and_during_admission() {
        let (engine, queue, _closed) = admission_fixture(Duration::from_secs(5), 1);
        drop(queue);
        assert_eq!(
            engine
                .request("engine.status".into(), json!({}))
                .await
                .unwrap_err()
                .code,
            "engine_stopped"
        );

        let (engine, mut queue, _closed) = admission_fixture(Duration::from_secs(5), 1);
        let _occupied = occupy_queue(&engine).await;
        let caller = engine.clone();
        let request =
            tokio::spawn(async move { caller.request("network.apply".into(), json!({})).await });
        await_pending(&engine, 0).await;
        queue.close();
        assert_eq!(request.await.unwrap().unwrap_err().code, "engine_stopped");
        assert_eq!(queue.recv().await.unwrap().method, "handshake");
        assert!(queue.recv().await.is_none());
    }

    #[tokio::test]
    async fn shutdown_cancels_admission_even_when_a_slot_becomes_available() {
        let (engine, mut queue, _closed) = admission_fixture(Duration::from_secs(5), 1);
        let _occupied = occupy_queue(&engine).await;
        let caller = engine.clone();
        let request =
            tokio::spawn(async move { caller.request("network.apply".into(), json!({})).await });
        await_pending(&engine, 0).await;
        engine.shutdown.send(true).unwrap();
        queue.recv().await.unwrap();
        assert_eq!(request.await.unwrap().unwrap_err().code, "engine_stopped");
        assert!(matches!(
            queue.try_recv(),
            Err(mpsc::error::TryRecvError::Empty)
        ));
        assert_eq!(
            engine
                .request("handshake".into(), json!({}))
                .await
                .unwrap_err()
                .code,
            "engine_stopped"
        );
    }

    #[tokio::test]
    async fn lost_reply_does_not_repeat_an_admitted_mutation() {
        let (engine, mut queue, _closed) = admission_fixture(Duration::from_secs(5), 1);
        let caller = engine.clone();
        let request =
            tokio::spawn(async move { caller.request("network.apply".into(), json!({})).await });
        let job = queue.recv().await.unwrap();
        assert_eq!(job.method, "network.apply");
        drop(job); // The worker disappeared after receiving the one dispatch.
        let error = request.await.unwrap().unwrap_err();
        assert_eq!(error.code, "engine_stopped");
        assert!(error
            .message
            .contains("No operation was automatically repeated"));
        assert!(matches!(
            queue.try_recv(),
            Err(mpsc::error::TryRecvError::Empty)
        ));
    }

    #[test]
    fn requires_matching_response_identity_and_exact_envelope() {
        assert_eq!(
            parse_response(br#"{"jsonrpc":"2.0","id":7,"result":{"ok":true}}"#, 7).unwrap(),
            json!({"ok": true})
        );
        assert!(parse_response(br#"{"jsonrpc":"2.0","id":8,"result":null}"#, 7).is_err());
        assert!(
            parse_response(br#"{"jsonrpc":"2.0","id":7,"result":null,"error":{}}"#, 7).is_err()
        );
        assert!(
            parse_response(br#"{"jsonrpc":"2.0","id":7,"error":{"message":"bad"}}"#, 7).is_err()
        );
        assert_eq!(
            parse_response(
                br#"{"jsonrpc":"2.0","id":7,"error":{"code":-32000,"message":"bad"}}"#,
                7
            )
            .unwrap_err()
            .code,
            "engine_error"
        );
    }

    #[test]
    fn requires_versioned_engine_identity() {
        assert!(validate_handshake(json!({"protocol_version": 1, "product": "Q22 Flight Lab", "engine_version": "0.1.0", "capabilities": []})).is_ok());
        assert!(validate_handshake(json!({"protocol_version": 2, "product": "Q22 Flight Lab", "engine_version": "0.1.0", "capabilities": []})).is_err());
        assert!(validate_handshake(json!({"protocol_version": "1"})).is_err());
    }

    #[tokio::test]
    async fn frame_reader_rejects_oversize_and_truncated_frames() {
        let mut reader = BufReader::new(&b"abc\nrest\n"[..]);
        assert_eq!(read_frame(&mut reader, 3).await.unwrap(), b"abc");
        assert!(read_frame(&mut reader, 3).await.is_err());
        let mut reader = BufReader::new(&b"abc"[..]);
        assert!(read_frame(&mut reader, 3).await.is_err());
    }

    #[tokio::test]
    async fn shutdown_failure_survives_cancelled_waiter_and_repeated_calls() {
        let (queue, _) = mpsc::channel(1);
        let (shutdown, _) = watch::channel(false);
        let (finish, done) = oneshot::channel::<()>();
        let engine = EngineSupervisor {
            queue,
            pending: Semaphore::new(MAX_PENDING_REQUESTS),
            admission_timeout: ADMISSION_TIMEOUT,
            shutdown,
            task: Mutex::new(ShutdownTask {
                handle: Some(tauri::async_runtime::spawn(async move {
                    let _ = done.await;
                    Err(EngineError::new(
                        "engine_shutdown",
                        "Injected process-tree timeout",
                    ))
                })),
                outcome: None,
            }),
        };
        assert!(
            tokio::time::timeout(Duration::from_millis(10), engine.shutdown())
                .await
                .is_err()
        );
        finish.send(()).unwrap();
        for _ in 0..2 {
            let error = engine.shutdown().await.unwrap_err();
            assert_eq!(error.code, "engine_shutdown");
            assert_eq!(error.message, "Injected process-tree timeout");
        }
    }

    #[tokio::test]
    async fn supervisor_task_failure_never_reports_successful_shutdown() {
        let (queue, _) = mpsc::channel(1);
        let (shutdown, _) = watch::channel(false);
        let engine = EngineSupervisor {
            queue,
            pending: Semaphore::new(MAX_PENDING_REQUESTS),
            admission_timeout: ADMISSION_TIMEOUT,
            shutdown,
            task: Mutex::new(ShutdownTask {
                handle: Some(tauri::async_runtime::spawn(async {
                    panic!("Injected supervisor failure")
                })),
                outcome: None,
            }),
        };
        assert_eq!(engine.shutdown().await.unwrap_err().code, "engine_shutdown");
        assert_eq!(engine.shutdown().await.unwrap_err().code, "engine_shutdown");
    }

    #[tokio::test]
    async fn packaged_engine_accepts_concurrent_cold_start_burst() {
        let stamp = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .unwrap()
            .as_nanos();
        let directory =
            std::env::temp_dir().join(format!("q22-native-startup-{}-{stamp}", std::process::id()));
        let engine = Arc::new(EngineSupervisor::new(
            sidecar_path().expect("Package the engine before running native tests"),
            directory.clone(),
        ));
        const CALLERS: usize = QUEUE_CAPACITY * 3;
        let barrier = Arc::new(tokio::sync::Barrier::new(CALLERS + 1));
        let mut requests = tokio::task::JoinSet::new();
        for index in 0..CALLERS {
            let caller = engine.clone();
            let barrier = barrier.clone();
            requests.spawn(async move {
                barrier.wait().await;
                let method = if index == 0 {
                    "handshake"
                } else {
                    "engine.status"
                };
                (method, caller.request(method.into(), json!({})).await)
            });
        }
        barrier.wait().await;
        let mut replies = Vec::new();
        while let Some(reply) = requests.join_next().await {
            replies.push(reply.unwrap());
        }
        engine.shutdown().await.unwrap();
        std::fs::remove_dir_all(directory).unwrap();
        assert_eq!(replies.len(), CALLERS);
        for (method, result) in replies {
            let value = result.unwrap_or_else(|error| panic!("Cold-start {method}: {error:?}"));
            if method == "handshake" {
                assert_eq!(value["product"], "Q22 Flight Lab");
            } else {
                assert_eq!(value["state"], "available");
            }
        }
        assert_eq!(engine.pending.available_permits(), MAX_PENDING_REQUESTS);
    }

    #[tokio::test]
    async fn failed_engine_start_does_not_replay_later_requests() {
        let stamp = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .unwrap()
            .as_nanos();
        let directory =
            std::env::temp_dir().join(format!("q22-native-missing-{}-{stamp}", std::process::id()));
        let engine = EngineSupervisor::new(directory.join("missing-engine"), directory.clone());
        assert_eq!(
            engine
                .request("handshake".into(), json!({}))
                .await
                .unwrap_err()
                .code,
            "engine_start"
        );
        assert_eq!(
            engine
                .request("network.apply".into(), json!({}))
                .await
                .unwrap_err()
                .code,
            "engine_unavailable"
        );
        engine.shutdown().await.unwrap();
        std::fs::remove_dir_all(directory).unwrap();
    }

    #[tokio::test]
    async fn packaged_engine_supervisor_handshake_restart_and_shutdown() {
        let stamp = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .unwrap()
            .as_nanos();
        let directory = std::env::temp_dir().join(format!(
            "q22-native-supervisor-{}-{stamp}",
            std::process::id()
        ));
        let engine = EngineSupervisor::new(
            sidecar_path().expect("Package the engine before running native tests"),
            directory.clone(),
        );
        let handshake = engine.request("handshake".into(), json!({})).await.unwrap();
        assert_eq!(handshake["protocol_version"], 1);
        let capabilities = handshake["capabilities"].as_array().unwrap();
        for method in METHODS {
            assert!(
                capabilities
                    .iter()
                    .any(|value| value.as_str() == Some(method)),
                "Bundled engine omitted the allowed RPC {method}"
            );
        }
        let status = engine
            .request("engine.status".into(), json!({}))
            .await
            .unwrap();
        assert_eq!(status["state"], "available");
        assert_eq!(
            engine
                .request("execute".into(), json!({"command": "anything"}))
                .await
                .unwrap_err()
                .code,
            "method_not_allowed"
        );
        assert_eq!(
            engine
                .request("engine.status".into(), json!([]))
                .await
                .unwrap_err()
                .code,
            "invalid_params"
        );
        let restarted = engine
            .request("engine.restart".into(), json!({}))
            .await
            .unwrap();
        assert_eq!(restarted["product"], "Q22 Flight Lab");
        engine.shutdown().await.unwrap();
        assert_eq!(
            engine
                .request("engine.status".into(), json!({}))
                .await
                .unwrap_err()
                .code,
            "engine_stopped"
        );
        std::fs::remove_dir_all(directory).unwrap();
    }

    #[tokio::test]
    async fn packaged_engine_crash_is_reported_without_replay() {
        let stamp = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .unwrap()
            .as_nanos();
        let directory =
            std::env::temp_dir().join(format!("q22-native-crash-{}-{stamp}", std::process::id()));
        let mut process = EngineProcess::spawn(
            &sidecar_path().expect("Package the engine before running native tests"),
            &directory,
        )
        .await
        .unwrap();
        #[cfg(target_os = "linux")]
        let descendants = {
            let id = process.child.id().unwrap();
            // /proc/PID/task/PID/children is absent on some container kernels.
            // Inspect parent IDs without depending on that optional interface.
            let children: Vec<u32> = std::fs::read_dir("/proc")
                .unwrap()
                .filter_map(|entry| {
                    let entry = entry.ok()?;
                    let pid = entry.file_name().to_string_lossy().parse().ok()?;
                    let status = std::fs::read_to_string(entry.path().join("stat")).ok()?;
                    let (_, fields) = status.split_once(") ")?;
                    let parent: u32 = fields.split_whitespace().nth(1)?.parse().ok()?;
                    (parent == id).then_some(pid)
                })
                .collect();
            assert!(
                !children.is_empty(),
                "The frozen one-file runtime should have a Python child"
            );
            children
        };
        process.child.kill().await.unwrap();
        process.child.wait().await.unwrap();
        let error = process
            .call("engine.status", json!({}), Duration::from_secs(5))
            .await
            .unwrap_err();
        assert_eq!(error.code, "engine_crashed");
        process.stop().await.unwrap();
        #[cfg(windows)]
        assert_eq!(process.process_tree.active_processes().unwrap(), 0);
        #[cfg(target_os = "linux")]
        for child in descendants {
            // Reaping by PID 1 can lag; a zombie is terminated and cannot keep
            // recording, write data, or retain listening sockets.
            let deadline = tokio::time::Instant::now() + Duration::from_secs(2);
            loop {
                let state = std::fs::read_to_string(format!("/proc/{child}/stat"));
                let stopped = match state {
                    Err(_) => true,
                    Ok(text) => text
                        .split_once(") ")
                        .is_some_and(|(_, rest)| rest.starts_with('Z')),
                };
                if stopped {
                    break;
                }
                assert!(
                    tokio::time::Instant::now() < deadline,
                    "Frozen Python descendant survived engine shutdown"
                );
                tokio::time::sleep(Duration::from_millis(10)).await;
            }
        }
        std::fs::remove_dir_all(directory).unwrap();
    }
}
