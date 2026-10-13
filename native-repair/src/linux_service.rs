//! Installed private-bench service. Fixed root-owned policy/socket/journal;
//! authenticated frozen engine; autonomous timer survives UI/engine exit.
use crate::*;
use crate::{
    enrollment::Enrollment,
    journal::FileJournal,
    linux::{LinuxHost, SystemRunner},
    safety::{NativeObserver, PassiveHeartbeat},
};
use serde_json::{json, Value};
use std::{
    fs,
    io::{BufRead, Read, Write},
    os::{
        fd::AsRawFd,
        unix::{
            fs::{MetadataExt, PermissionsExt},
            net::{UnixListener, UnixStream},
        },
    },
    path::Path,
    sync::{
        atomic::{AtomicBool, AtomicI32, AtomicUsize, Ordering},
        Arc, Mutex,
    },
    time::{Duration, Instant, SystemTime, UNIX_EPOCH},
};

pub const CONFIG: &str = "/etc/q22-network-helper/enrollment.json";
pub const SOCKET: &str = "/run/q22-network-helper/control.sock";
pub const JOURNAL: &str = "/var/lib/q22-network-helper";
pub struct NativeClock {
    pub start: Instant,
}
impl Clock for NativeClock {
    fn utc_ms(&self) -> u64 {
        SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .map(|d| d.as_millis() as u64)
            .unwrap_or(0)
    }
    fn monotonic_ms(&self) -> u64 {
        self.start.elapsed().as_millis() as u64
    }
}
pub struct EnrolledPeer {
    pub principal: String,
}
impl Authorizer for EnrolledPeer {
    fn authorize(&self, peer: &Peer) -> Result<()> {
        if peer.principal == self.principal {
            Ok(())
        } else {
            Err("Unenrolled native peer".into())
        }
    }
}
type ServiceBroker =
    Broker<LinuxHost<SystemRunner>, FileJournal, NativeObserver, NativeClock, EnrolledPeer>;

/// Root-owned file + ancestor protection, no symlinks or writable ancestry.
pub fn protected(path: &Path, private: bool) -> Result<()> {
    for (index, part) in path.ancestors().enumerate() {
        let metadata = fs::symlink_metadata(part).map_err(|e| e.to_string())?;
        if metadata.file_type().is_symlink()
            || metadata.uid() != 0
            || metadata.mode() & 0o022 != 0
            || (index == 0
                && (!metadata.is_file()
                    || metadata.nlink() != 1
                    || (private && metadata.mode() & 0o077 != 0)))
        {
            return Err(
                "Enrollment, signing keys and native images must be root-owned and protected"
                    .into(),
            );
        }
    }
    Ok(())
}
pub fn digest_file(path: &Path) -> Result<String> {
    let file = fs::File::open(path).map_err(|e| e.to_string())?;
    if file.metadata().map_err(|e| e.to_string())?.len() > 512 * 1024 * 1024 {
        return Err("Native image exceeds limit".into());
    }
    let mut hash = Sha256::new();
    let mut read = file.take(512 * 1024 * 1024 + 1);
    let mut buffer = [0; 65536];
    loop {
        let count = read.read(&mut buffer).map_err(|e| e.to_string())?;
        if count == 0 {
            break;
        }
        hash.update(&buffer[..count]);
    }
    Ok(format!("{:x}", hash.finalize()))
}
fn image(path: &Path, expected: &str) -> Result<()> {
    protected(path, false)?;
    let mut magic = [0; 4];
    fs::File::open(path)
        .and_then(|mut f| f.read_exact(&mut magic))
        .map_err(|e| e.to_string())?;
    if magic != *b"\x7fELF" || digest_file(path)? != expected {
        return Err("Enrolled frozen native image hash differs".into());
    }
    Ok(())
}
pub fn authenticate(stream: &UnixStream, policy: &Enrollment) -> Result<Peer> {
    let mut credentials = libc::ucred {
        pid: 0,
        uid: 0,
        gid: 0,
    };
    let mut size = std::mem::size_of::<libc::ucred>() as libc::socklen_t;
    if unsafe {
        libc::getsockopt(
            stream.as_raw_fd(),
            libc::SOL_SOCKET,
            libc::SO_PEERCRED,
            (&mut credentials as *mut libc::ucred).cast(),
            &mut size,
        )
    } != 0
        || size as usize != std::mem::size_of::<libc::ucred>()
    {
        return Err("Native peer credentials unavailable".into());
    }
    if credentials.uid.to_string() != policy.client_principal || credentials.pid <= 0 {
        return Err("Unenrolled native peer UID".into());
    }
    let process = std::path::PathBuf::from(format!("/proc/{}/exe", credentials.pid));
    let actual = fs::read_link(&process).map_err(|_| "Native peer image unavailable")?;
    if actual != Path::new(&policy.client_image) || digest_file(&process)? != policy.client_sha256 {
        return Err("Peer is not the enrolled frozen engine image".into());
    }
    image(Path::new(&policy.client_image), &policy.client_sha256)?;
    Ok(Peer {
        principal: policy.client_principal.clone(),
    })
}
/// Frozen managed workers run the enrolled image too. A second engine or any
/// surviving recorder worker blocks even if another client declares inactive.
pub fn only_engine_process(image: &Path, expected_pid: i32) -> bool {
    if expected_pid <= 0 {
        return false;
    }
    let entries = match fs::read_dir("/proc") {
        Ok(v) => v,
        Err(_) => return false,
    };
    let mut matches = Vec::new();
    let mut count = 0;
    for entry in entries {
        let entry = match entry {
            Ok(e) => e,
            Err(_) => return false,
        };
        let pid = match entry.file_name().to_string_lossy().parse::<i32>() {
            Ok(v) => v,
            Err(_) => continue,
        };
        count += 1;
        if count > 65536 {
            return false;
        }
        match fs::read_link(entry.path().join("exe")) {
            Ok(path) if path == image => matches.push(pid),
            Ok(_) => {}
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => {}
            Err(_) => return false,
        }
        if matches.len() > 2 {
            return false;
        }
    }
    let argv = match fs::read(format!("/proc/{expected_pid}/cmdline")) {
        Ok(v) if v.len() <= 8192 => v,
        _ => return false,
    };
    if matches == vec![expected_pid] {
        return census_matches(matches, expected_pid, None, &argv, None);
    }
    let status = match fs::read_to_string(format!("/proc/{expected_pid}/status")) {
        Ok(v) => v,
        Err(_) => return false,
    };
    let parent = status.lines().find_map(|line| {
        line.strip_prefix("PPid:")
            .and_then(|v| v.trim().parse::<i32>().ok())
    });
    if let Some(parent) = parent {
        let parent_argv = fs::read(format!("/proc/{parent}/cmdline")).ok();
        return census_matches(
            matches,
            expected_pid,
            Some(parent),
            &argv,
            parent_argv.as_deref(),
        );
    }
    false
}

fn census_matches(
    mut matches: Vec<i32>,
    expected_pid: i32,
    parent: Option<i32>,
    argv: &[u8],
    parent_argv: Option<&[u8]>,
) -> bool {
    if expected_pid <= 0
        || argv.is_empty()
        || argv.len() > 8192
        || argv
            .split(|b| *b == 0)
            .any(|arg| arg == b"--recorder-worker" || arg == b"--parse-worker")
    {
        return false;
    }
    if matches == vec![expected_pid] {
        return true;
    }
    if let Some(parent) = parent.filter(|p| *p > 0 && *p != expected_pid) {
        matches.sort();
        let mut allowed = vec![parent, expected_pid];
        allowed.sort();
        return matches == allowed && parent_argv == Some(argv);
    }
    false
}

fn caps() -> Value {
    json!({"protocol_version":1,"platform":"Linux","state":"bench_enrolled","qualified":false,"signed_helper":false,"admin_enrolled":true,"manager_aware":true,"supervised_rollback":true,"automatic_apply":false,"bench_available":true,"supported_operations":["host_route"],"identity_assurance":"admin_enrolled_image_hash","warning":"Experimental private bench service. Public signing and native hardware acceptance are not established. Requires explicit app opt-in and per-plan approval."})
}
pub fn serve() -> Result<()> {
    if unsafe { libc::geteuid() } != 0 {
        return Err("Service mode requires an administrator-installed service; the app must remain unprivileged".into());
    }
    protected(Path::new(CONFIG), false)?;
    let bytes = fs::read(CONFIG).map_err(|e| e.to_string())?;
    if bytes.len() > 65536 {
        return Err("Enrollment exceeds limit".into());
    }
    let policy: Enrollment = serde_json::from_slice(&bytes).map_err(|e| e.to_string())?;
    policy.validate()?;
    image(Path::new(&policy.client_image), &policy.client_sha256)?;
    let own = std::env::current_exe().map_err(|e| e.to_string())?;
    image(&own, &policy.helper_sha256)?;
    let key = if let Some(path) = &policy.observer.signing_key_path {
        protected(Path::new(path), true)?;
        let bytes = fs::read(path).map_err(|e| e.to_string())?;
        Some(
            <[u8; 32]>::try_from(bytes)
                .map_err(|_| "Signing key file must contain exactly 32 bytes")?,
        )
    } else {
        None
    };
    let mut journal = FileJournal::open(Path::new(JOURNAL))?;
    let records = journal.load()?;
    let known = records
        .iter()
        .filter(|r| r.post_state.is_some())
        .map(|r| (r.plan.plan_id.clone(), r.plan.clone()))
        .collect();
    let start = Instant::now();
    let mut observer = NativeObserver::unavailable(start);
    observer.set_signature_policy(policy.observer.require_signed);
    let engine_pid = Arc::new(AtomicI32::new(0));
    let owner = engine_pid.clone();
    let enrolled_image = policy.client_image.clone();
    observer.set_recording_guard(Arc::new(move || {
        only_engine_process(Path::new(&enrolled_image), owner.load(Ordering::Acquire))
    }));
    let stop = Arc::new(AtomicBool::new(false));
    let validator = PassiveHeartbeat::new(policy.observer.clone(), key)?;
    let _listener = observer.listen(
        (policy.observer.local_address, policy.observer.port).into(),
        validator,
        stop.clone(),
    )?;
    let host = LinuxHost {
        runner: SystemRunner,
        policy: policy.clone(),
        known,
        observer: Some(observer.clone()),
    };
    let broker = Arc::new(Mutex::new(Broker::open(
        host,
        journal,
        observer.clone(),
        NativeClock { start },
        EnrolledPeer {
            principal: policy.client_principal.clone(),
        },
    )?));
    // Parent lives under /run, owned by root; only this process can replace the
    // socket. Directory grants traversal, socket grants connect; SO_PEERCRED and
    // frozen-image enrollment independently authorize every request.
    let directory = Path::new(SOCKET)
        .parent()
        .ok_or("Invalid installed socket path")?;
    if !directory.exists() {
        fs::create_dir(directory).map_err(|e| e.to_string())?;
        fs::set_permissions(directory, fs::Permissions::from_mode(0o755))
            .map_err(|e| e.to_string())?;
    }
    let metadata = fs::symlink_metadata(directory).map_err(|e| e.to_string())?;
    if !metadata.is_dir()
        || metadata.file_type().is_symlink()
        || metadata.uid() != 0
        || metadata.mode() & 0o022 != 0
    {
        return Err("Untrusted installed socket directory".into());
    }
    if Path::new(SOCKET).exists() {
        let old = fs::symlink_metadata(SOCKET).map_err(|e| e.to_string())?;
        if old.uid() != 0 {
            return Err("Untrusted existing control socket".into());
        }
        fs::remove_file(SOCKET).map_err(|e| e.to_string())?;
    }
    let listener = UnixListener::bind(SOCKET).map_err(|e| e.to_string())?;
    fs::set_permissions(SOCKET, fs::Permissions::from_mode(0o666)).map_err(|e| e.to_string())?;
    let supervisor = broker.clone();
    std::thread::spawn(move || loop {
        if let Ok(mut broker) = supervisor.lock() {
            if let Err(error) = broker.supervise() {
                eprintln!("Native repair supervisor unresolved: {error}");
            }
        }
        std::thread::sleep(Duration::from_millis(500));
    });
    let active = Arc::new(AtomicUsize::new(0));
    let policy = Arc::new(policy);
    for stream in listener.incoming() {
        let stream = stream.map_err(|e| e.to_string())?;
        if active.fetch_add(1, Ordering::AcqRel) >= 16 {
            active.fetch_sub(1, Ordering::AcqRel);
            continue;
        }
        let (active, broker, policy, observer, engine_pid) = (
            active.clone(),
            broker.clone(),
            policy.clone(),
            observer.clone(),
            engine_pid.clone(),
        );
        std::thread::spawn(move || {
            let _ = handle(stream, &policy, &broker, &observer, &engine_pid);
            active.fetch_sub(1, Ordering::AcqRel);
        });
    }
    Ok(())
}
fn handle(
    mut stream: UnixStream,
    policy: &Enrollment,
    broker: &Arc<Mutex<ServiceBroker>>,
    observer: &NativeObserver,
    engine_pid: &AtomicI32,
) -> Result<()> {
    stream
        .set_read_timeout(Some(Duration::from_secs(3)))
        .map_err(|e| e.to_string())?;
    stream
        .set_write_timeout(Some(Duration::from_secs(3)))
        .map_err(|e| e.to_string())?;
    let peer = authenticate(&stream, policy)?;
    let mut frame = Vec::new();
    std::io::BufReader::new((&stream).take(65537))
        .read_until(b'\n', &mut frame)
        .map_err(|e| e.to_string())?;
    if frame.len() > 65536 || !frame.ends_with(b"\n") {
        return Err("Native request exceeds limit".into());
    }
    let request: Request =
        serde_json::from_slice(&frame).map_err(|_| "Invalid typed native request")?;
    let result: Result<Value> = match request {
        Request::Capabilities(_) => {
            let mut value = caps();
            value["repair_safety"] = observer.public_safety();
            value["observer_assurance"] = json!(if policy.observer.require_signed {
                "signed_mavlink2_source_uid_boot"
            } else {
                "crc_source_uid_boot_unauthenticated"
            });
            Ok(value)
        }
        Request::RecordingLease(lease) => {
            let mut cred = libc::ucred {
                pid: 0,
                uid: 0,
                gid: 0,
            };
            let mut size = std::mem::size_of::<libc::ucred>() as libc::socklen_t;
            if unsafe {
                libc::getsockopt(
                    stream.as_raw_fd(),
                    libc::SOL_SOCKET,
                    libc::SO_PEERCRED,
                    (&mut cred as *mut libc::ucred).cast(),
                    &mut size,
                )
            } != 0
            {
                return Err("Native recorder peer disappeared".into());
            }
            let reserve = if lease.active {
                Some(broker.lock().map_err(|_| "Native broker unavailable")?)
            } else {
                None
            };
            engine_pid.store(cred.pid, Ordering::Release);
            let result = observer
                .recording_lease(lease.active)
                .map(|_| json!({"state":"renewed","expires_in_ms":3000}));
            drop(reserve);
            result
        }
        Request::Prepare(request) => {
            if !matches!(request.candidate.operation, Operation::HostRoute { .. }) {
                Err(
                    "This private bench adapter enables only versioned NetworkManager host routes"
                        .into(),
                )
            } else {
                policy.permits(&request.candidate)?;
                broker
                    .lock()
                    .map_err(|_| "Native broker unavailable")?
                    .prepare(&peer, request)
                    .and_then(|p| serde_json::to_value(p).map_err(|e| e.to_string()))
            }
        }
        Request::Apply(request) => broker
            .lock()
            .map_err(|_| "Native broker unavailable")?
            .apply(&peer, request)
            .and_then(|r| serde_json::to_value(r).map_err(|e| e.to_string())),
        Request::Rollback(request) => broker
            .lock()
            .map_err(|_| "Native broker unavailable")?
            .rollback(&peer, request)
            .and_then(|r| serde_json::to_value(r).map_err(|e| e.to_string())),
        Request::Recovery(_) => broker
            .lock()
            .map_err(|_| "Native broker unavailable")?
            .recovery_checked(&peer)
            .and_then(|r| serde_json::to_value(r).map_err(|e| e.to_string())),
    };
    let response = match result {
        Ok(result) => json!({"result":result}),
        Err(error) => json!({"error":error}),
    };
    let bytes = serde_json::to_vec(&response).map_err(|e| e.to_string())?;
    if bytes.len() > 1_048_576 {
        return Err("Native response exceeds limit".into());
    }
    stream
        .write_all(&bytes)
        .and_then(|_| stream.write_all(b"\n"))
        .map_err(|e| e.to_string())
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn frozen_parent_child_pair_is_not_a_second_engine_but_workers_are() {
        let argv = b"/opt/q22/q22-engine\0--data-dir\0/var/tmp/q22\0";
        assert!(census_matches(vec![42], 42, None, argv, None));
        assert!(census_matches(vec![41, 42], 42, Some(41), argv, Some(argv)));
        assert!(!census_matches(
            vec![40, 42],
            42,
            Some(41),
            argv,
            Some(argv)
        ));
        assert!(!census_matches(
            vec![41, 42, 43],
            42,
            Some(41),
            argv,
            Some(argv)
        ));
        assert!(!census_matches(
            vec![41, 42],
            42,
            Some(41),
            argv,
            Some(b"other")
        ));
        assert!(!census_matches(
            vec![42],
            42,
            None,
            b"q22-engine\0--recorder-worker\0",
            None
        ));
        assert!(!census_matches(
            vec![42],
            42,
            None,
            b"q22-engine\0--parse-worker\0",
            None
        ));
        assert!(!census_matches(vec![42], 42, None, b"", None));
    }
}
