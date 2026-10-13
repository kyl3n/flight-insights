//! Administrator-installed Windows service. No RPC controls paths or commands.
use super::*;
use crate::safety::{NativeObserver, PassiveHeartbeat};
use serde_json::{json, Value};
use sha2::{Digest, Sha256};
use std::{
    ffi::OsStr,
    fs::{self, File, OpenOptions},
    io::{Read, Write},
    os::windows::{
        ffi::OsStrExt,
        fs::{MetadataExt, OpenOptionsExt},
        io::AsRawHandle,
    },
    path::{Path, PathBuf},
    process::{Command, Stdio},
    ptr::null_mut,
    sync::{
        atomic::{AtomicBool, AtomicUsize, Ordering},
        Arc, Mutex,
    },
    thread,
    time::{Instant, SystemTime, UNIX_EPOCH},
};
use windows_sys::Win32::{
    Foundation::*,
    Security::{Authorization::*, *},
    Storage::FileSystem::*,
    System::{
        Com::CoTaskMemFree, Pipes::*, Services::*, SystemInformation::GetSystemDirectoryW,
        Threading::*,
    },
    UI::Shell::{FOLDERID_ProgramData, SHGetKnownFolderPath},
};

pub const PIPE: &str = r"\\.\pipe\Q22NetworkRepair-v1";
const SERVICE: &str = "Q22NetworkRepair";
const ADMIN: &str = "S-1-5-32-544";
const SYSTEM: &str = "S-1-5-18";
const TRUSTED_INSTALLER: &str = "S-1-5-80-956008885-3418522649-1831038044-1853292631-2271478464";
fn wide(value: impl AsRef<OsStr>) -> Vec<u16> {
    value.as_ref().encode_wide().chain(Some(0)).collect()
}
fn error(label: &str) -> String {
    format!("{label}: {}", std::io::Error::last_os_error())
}
struct Handle(HANDLE);
unsafe impl Send for Handle {}
impl Drop for Handle {
    fn drop(&mut self) {
        if !self.0.is_null() && self.0 != INVALID_HANDLE_VALUE {
            unsafe {
                CloseHandle(self.0);
            }
        }
    }
}
struct Local(*mut std::ffi::c_void);
impl Drop for Local {
    fn drop(&mut self) {
        if !self.0.is_null() {
            unsafe {
                LocalFree(self.0);
            }
        }
    }
}
fn sid_text(sid: PSID) -> Result<String> {
    let mut text = null_mut();
    if unsafe { ConvertSidToStringSidW(sid, &mut text) } == 0 {
        return Err(error("SID conversion failed"));
    }
    let _guard = Local(text.cast());
    let mut count = 0;
    unsafe {
        while *text.add(count) != 0 {
            count += 1;
            if count > 256 {
                return Err("SID exceeds bound".into());
            }
        }
        Ok(String::from_utf16_lossy(std::slice::from_raw_parts(
            text, count,
        )))
    }
}
fn token_sid(token: HANDLE) -> Result<String> {
    let mut length = 0;
    unsafe {
        GetTokenInformation(token, TokenUser, null_mut(), 0, &mut length);
    }
    if length == 0 || length > 65536 {
        return Err("Token user unavailable".into());
    }
    let mut storage = vec![0usize; (length as usize).div_ceil(std::mem::size_of::<usize>())];
    if unsafe {
        GetTokenInformation(
            token,
            TokenUser,
            storage.as_mut_ptr().cast(),
            length,
            &mut length,
        )
    } == 0
    {
        return Err(error("Token user unavailable"));
    }
    let user = unsafe { &*(storage.as_ptr().cast::<TOKEN_USER>()) };
    sid_text(user.User.Sid)
}
fn process_sid(process: HANDLE) -> Result<String> {
    let mut token = null_mut();
    if unsafe { OpenProcessToken(process, TOKEN_QUERY, &mut token) } == 0 {
        return Err(error("Process token unavailable"));
    }
    let token = Handle(token);
    token_sid(token.0)
}
fn trusted_sid(sid: &str) -> bool {
    matches!(sid, SYSTEM | ADMIN | TRUSTED_INSTALLER)
}

/// Reject reparse points, replaceable ancestors and untrusted write grants.
/// Private journals/keys grant no access to non-administrative principals.
pub fn protected(path: &Path, private: bool) -> Result<()> {
    if !path.is_absolute() {
        return Err("Installed path must be absolute".into());
    }
    for (index, part) in path.ancestors().enumerate() {
        let metadata = fs::symlink_metadata(part).map_err(|e| e.to_string())?;
        if metadata.file_attributes() & FILE_ATTRIBUTE_REPARSE_POINT != 0
            || (index > 0 && !metadata.is_dir())
        {
            return Err("Installed path has reparse or invalid ancestry".into());
        }
        let mut owner = null_mut();
        let mut acl = null_mut();
        let mut descriptor = null_mut();
        let code = unsafe {
            GetNamedSecurityInfoW(
                wide(part).as_ptr(),
                SE_FILE_OBJECT,
                OWNER_SECURITY_INFORMATION | DACL_SECURITY_INFORMATION,
                &mut owner,
                null_mut(),
                &mut acl,
                null_mut(),
                &mut descriptor,
            )
        };
        if code != 0 {
            return Err(format!("Installed ACL unavailable: {code}"));
        }
        let _guard = Local(descriptor);
        if owner.is_null() || !trusted_sid(&sid_text(owner)?) || acl.is_null() {
            return Err("Installed object owner/DACL is not administrator-controlled".into());
        }
        let mut control = 0;
        let mut revision = 0;
        if unsafe { GetSecurityDescriptorControl(descriptor, &mut control, &mut revision) } == 0 {
            return Err(error("ACL control unavailable"));
        }
        if index == 0 && private && control & SE_DACL_PROTECTED == 0 {
            return Err("Private object requires a protected DACL".into());
        }
        let count = unsafe { (*acl).AceCount };
        for i in 0..count {
            let mut pointer = null_mut();
            if unsafe { GetAce(acl, i as u32, &mut pointer) } == 0 {
                return Err(error("ACL entry unavailable"));
            }
            let header = unsafe { &*pointer.cast::<ACE_HEADER>() };
            if u32::from(header.AceFlags) & INHERIT_ONLY_ACE != 0 {
                continue;
            }
            if header.AceType == 0 {
                let ace = unsafe { &*pointer.cast::<ACCESS_ALLOWED_ACE>() };
                let sid = (&ace.SidStart as *const u32).cast_mut().cast();
                let principal = sid_text(sid)?;
                let write_mask = if index == 0 { 0x500d_0156 } else { 0x500d_0150 };
                if !trusted_sid(&principal)
                    && ((index == 0 && private) || ace.Mask & write_mask != 0)
                {
                    return Err("Installed object can be changed by an untrusted principal".into());
                }
            } else if header.AceType != 1 {
                return Err("Unsupported installed ACL entry; cannot prove protection".into());
            }
        }
    }
    Ok(())
}
fn secure_read(path: &Path, private: bool, max: u64) -> Result<Vec<u8>> {
    protected(path, private)?;
    let file = OpenOptions::new()
        .read(true)
        .share_mode(FILE_SHARE_READ)
        .custom_flags(FILE_FLAG_OPEN_REPARSE_POINT)
        .open(path)
        .map_err(|e| e.to_string())?;
    let metadata = file.metadata().map_err(|e| e.to_string())?;
    if !metadata.is_file()
        || metadata.file_attributes() & FILE_ATTRIBUTE_REPARSE_POINT != 0
        || metadata.len() > max
    {
        return Err("Installed file is not ordinary or exceeds bounds".into());
    }
    let mut info: BY_HANDLE_FILE_INFORMATION = unsafe { std::mem::zeroed() };
    if unsafe { GetFileInformationByHandle(file.as_raw_handle(), &mut info) } == 0
        || info.nNumberOfLinks != 1
    {
        return Err("Installed file must have a single link".into());
    }
    let mut data = Vec::new();
    file.take(max + 1)
        .read_to_end(&mut data)
        .map_err(|e| e.to_string())?;
    if data.len() as u64 > max {
        return Err("Installed file exceeds bounds".into());
    }
    Ok(data)
}
fn verify_image(path: &Path, expected: &str) -> Result<()> {
    protected(path, false)?;
    let mut file = OpenOptions::new()
        .read(true)
        .share_mode(FILE_SHARE_READ)
        .open(path)
        .map_err(|e| e.to_string())?;
    if file.metadata().map_err(|e| e.to_string())?.len() > 512 * 1024 * 1024 {
        return Err("Installed image exceeds bound".into());
    }
    let mut hash = Sha256::new();
    let mut buffer = [0; 65536];
    let mut first = true;
    loop {
        let n = file.read(&mut buffer).map_err(|e| e.to_string())?;
        if n == 0 {
            break;
        }
        if first && (n < 2 || buffer[..2] != *b"MZ") {
            return Err("Enrolled image is not a native Windows executable".into());
        }
        first = false;
        hash.update(&buffer[..n]);
    }
    if first || format!("{:x}", hash.finalize()) != expected {
        return Err("Installed executable hash differs from enrollment".into());
    }
    Ok(())
}
fn installed_root() -> Result<PathBuf> {
    let mut pointer = null_mut();
    if unsafe { SHGetKnownFolderPath(&FOLDERID_ProgramData, 0, null_mut(), &mut pointer) } < 0 {
        return Err("ProgramData known folder unavailable".into());
    }
    let mut n = 0;
    unsafe {
        while *pointer.add(n) != 0 {
            n += 1
        }
    }
    let value = unsafe { String::from_utf16_lossy(std::slice::from_raw_parts(pointer, n)) };
    unsafe { CoTaskMemFree(pointer.cast()) };
    Ok(PathBuf::from(value).join("Q22").join("NetworkRepair"))
}

pub struct WindowsJournal {
    root: PathBuf,
    _lock: File,
}
impl WindowsJournal {
    pub fn open(root: &Path) -> Result<Self> {
        protected(root, true)?;
        if !root.is_dir() {
            return Err("Journal is not an installed directory".into());
        }
        let lock = OpenOptions::new()
            .read(true)
            .write(true)
            .create(true)
            .truncate(false)
            .share_mode(0)
            .custom_flags(FILE_FLAG_OPEN_REPARSE_POINT)
            .open(root.join("service.lock"))
            .map_err(|e| format!("Journal already owned or inaccessible: {e}"))?;
        Ok(Self {
            root: root.to_owned(),
            _lock: lock,
        })
    }
    fn read_record(&self, path: &Path) -> Result<Record> {
        let record: Record = serde_json::from_slice(&secure_read(path, false, 1_048_576)?)
            .map_err(|e| e.to_string())?;
        record.validate()?;
        if path.file_stem().and_then(|s| s.to_str()) != Some(record.plan.plan_id.as_str()) {
            return Err("Journal filename identity differs".into());
        }
        Ok(record)
    }
}
impl Journal for WindowsJournal {
    fn load(&mut self) -> Result<Vec<Record>> {
        protected(&self.root, true)?;
        let mut records = vec![];
        for item in fs::read_dir(&self.root).map_err(|e| e.to_string())? {
            let path = item.map_err(|e| e.to_string())?.path();
            if path.extension().and_then(|s| s.to_str()) != Some("json") {
                continue;
            }
            if records.len() >= MAX_RECORDS {
                return Err("Journal record capacity exceeded".into());
            }
            records.push(self.read_record(&path)?);
        }
        Ok(records)
    }
    fn save(&mut self, record: &Record) -> Result<()> {
        record.validate()?;
        protected(&self.root, true)?;
        let path = self.root.join(format!("{}.json", record.plan.plan_id));
        if path.exists() {
            let old = self.read_record(&path)?;
            if old.plan != record.plan || !record.events.starts_with(&old.events) {
                return Err("Journal original plan and history are immutable".into());
            }
        }
        let bytes = serde_json::to_vec(record).map_err(|e| e.to_string())?;
        if bytes.len() > 1_048_576 {
            return Err("Journal record exceeds limit".into());
        }
        let temporary = self.root.join(format!(".{}.tmp", Uuid::new_v4().simple()));
        let result = (|| {
            let mut file = OpenOptions::new()
                .create_new(true)
                .write(true)
                .share_mode(0)
                .custom_flags(FILE_FLAG_OPEN_REPARSE_POINT | FILE_FLAG_WRITE_THROUGH)
                .open(&temporary)
                .map_err(|e| e.to_string())?;
            file.write_all(&bytes)
                .and_then(|_| file.sync_all())
                .map_err(|e| e.to_string())?;
            drop(file);
            protected(&temporary, false)?;
            if unsafe {
                MoveFileExW(
                    wide(&temporary).as_ptr(),
                    wide(&path).as_ptr(),
                    MOVEFILE_REPLACE_EXISTING | MOVEFILE_WRITE_THROUGH,
                )
            } == 0
            {
                return Err(error("Durable journal replacement failed"));
            }
            Ok(())
        })();
        if result.is_err() {
            let _ = fs::remove_file(temporary);
        }
        result
    }
}

fn system_powershell() -> Result<PathBuf> {
    let mut buffer = vec![0u16; 32768];
    let n = unsafe { GetSystemDirectoryW(buffer.as_mut_ptr(), buffer.len() as u32) } as usize;
    if n == 0 || n >= buffer.len() {
        return Err("Native system directory unavailable".into());
    }
    Ok(PathBuf::from(String::from_utf16_lossy(&buffer[..n]))
        .join("WindowsPowerShell/v1.0/powershell.exe"))
}
fn execute(script: &str) -> Result<Vec<u8>> {
    let executable = system_powershell()?;
    protected(&executable, false)?;
    let mut child = Command::new(executable)
        .args([
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            &format!("[Console]::OutputEncoding=[Text.UTF8Encoding]::new($false); {script}"),
        ])
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .map_err(|e| e.to_string())?;
    let output = child.stdout.take().ok_or("Missing native stdout")?;
    let errors = child.stderr.take().ok_or("Missing native stderr")?;
    let out = thread::spawn(move || {
        let mut data = Vec::new();
        output.take(2_097_153).read_to_end(&mut data).map(|_| data)
    });
    let err = thread::spawn(move || {
        let mut data = Vec::new();
        errors.take(16_385).read_to_end(&mut data).map(|_| data)
    });
    let start = Instant::now();
    let status = loop {
        if let Some(status) = child.try_wait().map_err(|e| e.to_string())? {
            break status;
        }
        if start.elapsed() > Duration::from_secs(15) {
            let _ = child.kill();
            let _ = child.wait();
            let _ = out.join();
            let _ = err.join();
            return Err("Native operation timed out; outcome requires recovery inspection".into());
        }
        thread::sleep(Duration::from_millis(20));
    };
    let output = out
        .join()
        .map_err(|_| "Native stdout reader failed")?
        .map_err(|e| e.to_string())?;
    let errors = err
        .join()
        .map_err(|_| "Native stderr reader failed")?
        .map_err(|e| e.to_string())?;
    if output.len() > 2_097_152 || errors.len() > 16_384 {
        return Err("Native output exceeded bounds".into());
    }
    if !status.success() {
        return Err(format!(
            "Native cmdlet failed: {}",
            String::from_utf8_lossy(&errors)
        ));
    }
    Ok(output)
}
pub(super) fn powershell(script: &str) -> Result<Value> {
    serde_json::from_slice(&execute(script)?).map_err(|e| format!("Native JSON invalid: {e}"))
}
pub(super) fn run_generated(command: &platform::Command) -> Result<()> {
    if command.program != platform::Program::PowerShell
        || command.args.len() != 4
        || command.args[..3] != ["-NoProfile", "-NonInteractive", "-Command"]
    {
        return Err("Native generated command rejected".into());
    }
    execute(&command.args[3]).map(|_| ())
}

fn authenticate(pipe: HANDLE, policy: &Enrollment) -> Result<(Peer, Handle)> {
    let mut pid = 0;
    if unsafe { GetNamedPipeClientProcessId(pipe, &mut pid) } == 0 || pid == 0 {
        return Err("Native pipe peer PID unavailable".into());
    }
    let process = Handle(unsafe { OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, 0, pid) });
    if process.0.is_null() {
        return Err(error("Native peer process unavailable"));
    }
    if unsafe { ImpersonateNamedPipeClient(pipe) } == 0 {
        return Err(error("Pipe token unavailable"));
    }
    let sid = (|| {
        let mut token = null_mut();
        if unsafe { OpenThreadToken(GetCurrentThread(), TOKEN_QUERY, 1, &mut token) } == 0 {
            return Err(error("Pipe token unavailable"));
        }
        let token = Handle(token);
        token_sid(token.0)
    })();
    if unsafe { RevertToSelf() } == 0 {
        std::process::abort()
    }
    if sid? != policy.client_principal || process_sid(process.0)? != policy.client_principal {
        return Err("Native client SID is not enrolled".into());
    }
    let mut buffer = vec![0u16; 32768];
    let mut count = buffer.len() as u32;
    if unsafe { QueryFullProcessImageNameW(process.0, 0, buffer.as_mut_ptr(), &mut count) } == 0 {
        return Err(error("Native peer image unavailable"));
    }
    let actual = PathBuf::from(String::from_utf16_lossy(&buffer[..count as usize]));
    let enrolled = fs::canonicalize(&policy.client_image).map_err(|e| e.to_string())?;
    let actual_path = fs::canonicalize(&actual).map_err(|e| e.to_string())?;
    if actual_path.to_string_lossy().to_lowercase() != enrolled.to_string_lossy().to_lowercase() {
        return Err("Native peer image path is not enrolled".into());
    }
    verify_image(&actual, &policy.client_sha256)?;
    let mut code = 0;
    if unsafe { GetExitCodeProcess(process.0, &mut code) } == 0 || code != 259 {
        return Err("Native peer exited before authorization".into());
    }
    Ok((
        Peer {
            principal: policy.client_principal.clone(),
        },
        process,
    ))
}
struct NativeClock {
    start: Instant,
}
impl Clock for NativeClock {
    fn utc_ms(&self) -> u64 {
        SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .map(|v| v.as_millis() as u64)
            .unwrap_or(0)
    }
    fn monotonic_ms(&self) -> u64 {
        self.start.elapsed().as_millis() as u64
    }
}
struct EnrolledPeer(String);
impl Authorizer for EnrolledPeer {
    fn authorize(&self, peer: &Peer) -> Result<()> {
        if peer.principal == self.0 {
            Ok(())
        } else {
            Err("Unenrolled native peer".into())
        }
    }
}
type ServiceBroker = Broker<WindowsHost, WindowsJournal, NativeObserver, NativeClock, EnrolledPeer>;
fn new_pipe(policy: &Enrollment, first: bool) -> Result<Handle> {
    if !policy.client_principal.starts_with("S-1-")
        || !policy
            .client_principal
            .chars()
            .all(|c| c.is_ascii_digit() || matches!(c, 'S' | '-'))
    {
        return Err("Invalid enrolled SID".into());
    }
    let sddl = wide(format!(
        "D:P(A;;GA;;;SY)(A;;GA;;;BA)(A;;0x0012018b;;;{})",
        policy.client_principal
    ));
    let mut descriptor = null_mut();
    if unsafe {
        ConvertStringSecurityDescriptorToSecurityDescriptorW(
            sddl.as_ptr(),
            1,
            &mut descriptor,
            null_mut(),
        )
    } == 0
    {
        return Err(error("Pipe DACL rejected"));
    }
    let _guard = Local(descriptor);
    let attributes = SECURITY_ATTRIBUTES {
        nLength: std::mem::size_of::<SECURITY_ATTRIBUTES>() as u32,
        lpSecurityDescriptor: descriptor,
        bInheritHandle: 0,
    };
    let handle = unsafe {
        CreateNamedPipeW(
            wide(PIPE).as_ptr(),
            PIPE_ACCESS_DUPLEX
                | if first {
                    FILE_FLAG_FIRST_PIPE_INSTANCE
                } else {
                    0
                },
            PIPE_TYPE_BYTE | PIPE_READMODE_BYTE | PIPE_NOWAIT | PIPE_REJECT_REMOTE_CLIENTS,
            18,
            65536,
            65536,
            3000,
            &attributes,
        )
    };
    if handle == INVALID_HANDLE_VALUE {
        return Err(error("Installed pipe unavailable"));
    }
    Ok(Handle(handle))
}
fn read_frame(pipe: HANDLE) -> Result<Vec<u8>> {
    let mut frame = Vec::new();
    let start = Instant::now();
    let mut buffer = [0; 4096];
    while start.elapsed() < Duration::from_secs(3) {
        let mut n = 0;
        let ok = unsafe {
            ReadFile(
                pipe,
                buffer.as_mut_ptr(),
                buffer.len() as u32,
                &mut n,
                null_mut(),
            )
        };
        if n > 0 {
            frame.extend_from_slice(&buffer[..n as usize]);
            if frame.len() > 65536 {
                return Err("Native request limit".into());
            }
            if let Some(index) = frame.iter().position(|v| *v == b'\n') {
                if index + 1 != frame.len() {
                    return Err("Exactly one native request permitted".into());
                }
                return Ok(frame);
            }
        } else if ok == 0 && unsafe { GetLastError() } != ERROR_NO_DATA {
            return Err(error("Native pipe read failed"));
        }
        thread::sleep(Duration::from_millis(10));
    }
    Err("Native request timeout".into())
}
fn write_frame(pipe: HANDLE, value: Value) -> Result<()> {
    let mut bytes = serde_json::to_vec(&value).map_err(|e| e.to_string())?;
    bytes.push(b'\n');
    if bytes.len() > 1_048_576 {
        return Err("Native response limit".into());
    }
    let start = Instant::now();
    let mut offset = 0;
    while offset < bytes.len() && start.elapsed() < Duration::from_secs(3) {
        let mut n = 0;
        let ok = unsafe {
            WriteFile(
                pipe,
                bytes[offset..].as_ptr(),
                (bytes.len() - offset).min(16384) as u32,
                &mut n,
                null_mut(),
            )
        };
        if ok == 0 && unsafe { GetLastError() } != ERROR_NO_DATA {
            return Err(error("Native pipe write failed"));
        }
        offset += n as usize;
        if n == 0 {
            thread::sleep(Duration::from_millis(10));
        }
    }
    if offset != bytes.len() {
        return Err("Native response timeout".into());
    }
    // Keep the pipe alive until the client consumes/closes it. Blocking flush can
    // hang forever on a malicious client; this wait is bounded and nonblocking.
    let finish = Instant::now();
    let mut buffer = [0u8; 1];
    while finish.elapsed() < Duration::from_secs(3) {
        let mut n = 0;
        let ok = unsafe { ReadFile(pipe, buffer.as_mut_ptr(), 1, &mut n, null_mut()) };
        if ok == 0 && unsafe { GetLastError() } != ERROR_NO_DATA {
            break;
        }
        if n > 0 {
            break;
        }
        thread::sleep(Duration::from_millis(10));
    }
    Ok(())
}
#[repr(C)]
struct NativeUnicodeString {
    length: u16,
    maximum_length: u16,
    buffer: *const u16,
}
#[link(name = "ntdll")]
unsafe extern "system" {
    fn NtQueryInformationProcess(
        process: HANDLE,
        class: u32,
        buffer: *mut std::ffi::c_void,
        length: u32,
        returned: *mut u32,
    ) -> i32;
}
fn process_arguments(process: HANDLE) -> Result<Vec<String>> {
    // Windows 8.1+ ProcessCommandLineInformation copies the string into this
    // caller buffer. Unavailable queries fail closed; no remote PEB dereference.
    let mut required = 0;
    unsafe {
        NtQueryInformationProcess(process, 60, null_mut(), 0, &mut required);
    }
    if required < std::mem::size_of::<NativeUnicodeString>() as u32 || required > 131072 {
        return Err("Native process command line unavailable".into());
    }
    let mut data = vec![0usize; (required as usize).div_ceil(std::mem::size_of::<usize>())];
    let capacity = data.len() * std::mem::size_of::<usize>();
    if unsafe {
        NtQueryInformationProcess(
            process,
            60,
            data.as_mut_ptr().cast(),
            capacity as u32,
            &mut required,
        )
    } < 0
    {
        return Err("Native process command line query failed".into());
    }
    let value = unsafe { &*data.as_ptr().cast::<NativeUnicodeString>() };
    let base = data.as_ptr() as usize;
    let pointer = value.buffer as usize;
    if value.length % 2 != 0
        || value.length > value.maximum_length
        || pointer < base
        || pointer
            .checked_add(value.length as usize)
            .is_none_or(|end| end > base + capacity)
    {
        return Err("Native process command line bounds invalid".into());
    }
    let mut text =
        unsafe { std::slice::from_raw_parts(value.buffer, value.length as usize / 2) }.to_vec();
    text.push(0);
    let mut count = 0;
    let args =
        unsafe { windows_sys::Win32::UI::Shell::CommandLineToArgvW(text.as_ptr(), &mut count) };
    if args.is_null() {
        return Err("Native process argv unavailable".into());
    }
    let _guard = Local(args.cast());
    if !(1..=512).contains(&count) {
        return Err("Native process argv exceeds bounds".into());
    }
    let mut result = Vec::new();
    for i in 0..count as usize {
        let pointer = unsafe { *args.add(i) };
        let mut n = 0;
        unsafe {
            while *pointer.add(n) != 0 {
                n += 1;
                if n > 32768 {
                    return Err("Native process argument exceeds bounds".into());
                }
            }
            result.push(String::from_utf16_lossy(std::slice::from_raw_parts(
                pointer, n,
            )));
        }
    }
    Ok(result)
}
fn sole_enrolled_process(path: &Path, lease_pid: u32) -> bool {
    use windows_sys::Win32::System::Diagnostics::ToolHelp::*;
    if lease_pid == 0 {
        return false;
    }
    let snapshot = Handle(unsafe { CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0) });
    if snapshot.0 == INVALID_HANDLE_VALUE {
        return false;
    }
    let expected = match fs::canonicalize(path) {
        Ok(value) => value.to_string_lossy().to_lowercase(),
        Err(_) => return false,
    };
    let mut entry: PROCESSENTRY32W = unsafe { std::mem::zeroed() };
    entry.dwSize = std::mem::size_of::<PROCESSENTRY32W>() as u32;
    if unsafe { Process32FirstW(snapshot.0, &mut entry) } == 0 {
        return false;
    }
    let mut matches = Vec::new();
    loop {
        let length = entry
            .szExeFile
            .iter()
            .position(|v| *v == 0)
            .unwrap_or(entry.szExeFile.len());
        if String::from_utf16_lossy(&entry.szExeFile[..length])
            .eq_ignore_ascii_case("q22-engine.exe")
        {
            let process = Handle(unsafe {
                OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, 0, entry.th32ProcessID)
            });
            if process.0.is_null() {
                return false;
            }
            let mut image = vec![0u16; 32768];
            let mut length = image.len() as u32;
            if unsafe { QueryFullProcessImageNameW(process.0, 0, image.as_mut_ptr(), &mut length) }
                == 0
            {
                return false;
            }
            let actual = match fs::canonicalize(PathBuf::from(String::from_utf16_lossy(
                &image[..length as usize],
            ))) {
                Ok(value) => value.to_string_lossy().to_lowercase(),
                Err(_) => return false,
            };
            if actual == expected {
                let arguments = match process_arguments(process.0) {
                    Ok(value) => value,
                    Err(_) => return false,
                };
                if arguments
                    .iter()
                    .any(|v| matches!(v.as_str(), "--recorder-worker" | "--parse-worker"))
                {
                    return false;
                }
                matches.push((entry.th32ProcessID, entry.th32ParentProcessID, arguments));
                if matches.len() > 2 {
                    return false;
                }
            }
        }
        if unsafe { Process32NextW(snapshot.0, &mut entry) } == 0 {
            if unsafe { GetLastError() } != ERROR_NO_MORE_FILES {
                return false;
            }
            break;
        }
    }
    let Some(engine) = matches.iter().find(|(pid, _, _)| *pid == lease_pid) else {
        return false;
    };
    if matches.len() == 1 {
        return true;
    }
    // PyInstaller onefile idle runtime has an exact same-argv parent bootloader.
    // A second arbitrary process, worker or child is not an allowed pair.
    matches
        .iter()
        .any(|(pid, _, args)| *pid == engine.1 && *pid != lease_pid && args == &engine.2)
}

fn same_process(a: &Handle, b: &Handle) -> bool {
    unsafe { GetProcessId(a.0) == GetProcessId(b.0) }
}
fn process_alive(process: &Handle) -> bool {
    let mut code = 259;
    unsafe { GetExitCodeProcess(process.0, &mut code) == 0 || code == 259 }
}
fn require_lease_owner(owner: &Arc<Mutex<Option<Handle>>>, process: &Handle) -> Result<()> {
    let owner = owner.lock().map_err(|_| "Lease owner unavailable")?;
    if owner
        .as_ref()
        .is_none_or(|current| !same_process(current, process) || !process_alive(current))
    {
        return Err("Requests require the single authenticated recording-lease owner".into());
    }
    Ok(())
}
fn handle(
    pipe: &Handle,
    policy: &Enrollment,
    broker: &Arc<Mutex<ServiceBroker>>,
    observer: &NativeObserver,
    lease_process: &Arc<Mutex<Option<Handle>>>,
    lease_pid: &Arc<AtomicUsize>,
) -> Result<()> {
    let frame = read_frame(pipe.0)?;
    let (peer, process) = authenticate(pipe.0, policy)?;
    let request: Request =
        serde_json::from_slice(&frame).map_err(|_| "Invalid native typed request")?;
    let result: Result<Value> = match request {
        Request::Capabilities(_) => Ok(
            json!({"protocol_version":1,"platform":"Windows","state":"bench_enrolled","qualified":false,"signed_helper":false,"admin_enrolled":true,"manager_aware":true,"supervised_rollback":true,"automatic_apply":false,"bench_available":true,"supported_operations":["host_route","firewall_udp"],"identity_assurance":"admin_enrolled_image_hash","repair_safety":observer.public_safety(),"warning":"Experimental private bench enrollment. Native hardware acceptance and public signing are not established."}),
        ),
        Request::RecordingLease(lease) => {
            let mut owner = lease_process
                .lock()
                .map_err(|_| "Lease process state unavailable")?;
            if owner
                .as_ref()
                .is_some_and(|current| process_alive(current) && !same_process(current, &process))
            {
                return Err("Another authenticated engine owns the recording lease".into());
            }
            lease_pid.store(
                unsafe { GetProcessId(process.0) } as usize,
                Ordering::Release,
            );
            *owner = Some(process);
            let reserve = if lease.active {
                Some(broker.lock().map_err(|_| "Native broker unavailable")?)
            } else {
                None
            };
            let result = observer
                .recording_lease(lease.active)
                .map(|_| json!({"state":"renewed","expires_in_ms":3000}));
            drop(reserve);
            result
        }
        Request::Prepare(request) => {
            require_lease_owner(lease_process, &process)?;
            policy.permits(&request.candidate)?;
            broker
                .try_lock()
                .map_err(|_| "Native broker busy; review and retry")?
                .prepare(&peer, request)
                .and_then(|v| serde_json::to_value(v).map_err(|e| e.to_string()))
        }
        Request::Apply(request) => {
            require_lease_owner(lease_process, &process)?;
            broker
                .try_lock()
                .map_err(|_| "Native broker busy; review and retry")?
                .apply(&peer, request)
                .and_then(|v| serde_json::to_value(v).map_err(|e| e.to_string()))
        }
        Request::Rollback(request) => {
            require_lease_owner(lease_process, &process)?;
            broker
                .try_lock()
                .map_err(|_| "Native broker busy; review and retry")?
                .rollback(&peer, request)
                .and_then(|v| serde_json::to_value(v).map_err(|e| e.to_string()))
        }
        Request::Recovery(_) => broker
            .try_lock()
            .map_err(|_| "Native broker busy; review and retry")?
            .recovery_checked(&peer)
            .and_then(|v| serde_json::to_value(v).map_err(|e| e.to_string())),
    };
    let response = match result {
        Ok(value) => json!({"result":value}),
        Err(error) => json!({"error":error}),
    };
    write_frame(pipe.0, response)
}
fn listen(
    observer: NativeObserver,
    policy: &Enrollment,
    key: Option<[u8; 32]>,
    stop: Arc<AtomicBool>,
) -> Result<thread::JoinHandle<()>> {
    let socket = exclusive_udp(policy.observer.local_address, policy.observer.port)?;
    let mut validator = PassiveHeartbeat::new(policy.observer.clone(), key)?;
    socket
        .set_read_timeout(Some(Duration::from_millis(250)))
        .map_err(|e| e.to_string())?;
    Ok(thread::spawn(move || {
        let mut data = [0; 65507];
        while !stop.load(Ordering::Acquire) {
            if let Ok((n, std::net::SocketAddr::V4(sender))) = socket.recv_from(&mut data) {
                let now = SystemTime::now()
                    .duration_since(UNIX_EPOCH)
                    .map(|v| v.as_micros() as u64)
                    .unwrap_or(0);
                for state in validator.observe_all(&data[..n], *sender.ip(), now) {
                    let _ = observer.accept(state);
                }
            }
        }
    }))
}
fn exclusive_udp(address: Ipv4Addr, port: u16) -> Result<std::net::UdpSocket> {
    use std::os::windows::io::FromRawSocket;
    use windows_sys::Win32::Networking::WinSock::*;
    // Initialize Winsock through std, then create the unbound exclusive socket.
    let initializer =
        std::net::UdpSocket::bind((Ipv4Addr::LOCALHOST, 0)).map_err(|e| e.to_string())?;
    drop(initializer);
    let raw = unsafe { socket(AF_INET as i32, SOCK_DGRAM, IPPROTO_UDP) };
    if raw == INVALID_SOCKET {
        return Err("Native observer socket unavailable".into());
    }
    let owned = unsafe { std::net::UdpSocket::from_raw_socket(raw as u64) };
    let enabled = 1i32;
    if unsafe {
        setsockopt(
            raw,
            SOL_SOCKET,
            SO_EXCLUSIVEADDRUSE,
            (&enabled as *const i32).cast(),
            4,
        )
    } == SOCKET_ERROR
    {
        return Err("Exclusive native observer socket option failed".into());
    }
    let native = SOCKADDR_IN {
        sin_family: AF_INET,
        sin_port: port.to_be(),
        sin_addr: IN_ADDR {
            S_un: IN_ADDR_0 {
                S_addr: u32::from_ne_bytes(address.octets()),
            },
        },
        sin_zero: [0; 8],
    };
    if unsafe {
        bind(
            raw,
            (&native as *const SOCKADDR_IN).cast(),
            std::mem::size_of::<SOCKADDR_IN>() as i32,
        )
    } == SOCKET_ERROR
    {
        return Err("Dedicated native observer endpoint unavailable".into());
    }
    Ok(owned)
}
static STOP: AtomicBool = AtomicBool::new(false);
pub fn serve() -> Result<()> {
    if process_sid(unsafe { GetCurrentProcess() })? != SYSTEM {
        return Err("Service mode requires the explicitly installed LocalSystem service".into());
    }
    let root = installed_root()?;
    protected(&root, true)?;
    let bytes = secure_read(&root.join("enrollment.json"), false, 65536)?;
    let policy: Enrollment = serde_json::from_slice(&bytes).map_err(|e| e.to_string())?;
    policy.validate()?;
    verify_image(Path::new(&policy.client_image), &policy.client_sha256)?;
    verify_image(
        &std::env::current_exe().map_err(|e| e.to_string())?,
        &policy.helper_sha256,
    )?;
    let key = match &policy.observer.signing_key_path {
        Some(path) => Some(
            <[u8; 32]>::try_from(secure_read(Path::new(path), true, 32)?)
                .map_err(|_| "Signing key must be exactly 32 bytes")?,
        ),
        None => None,
    };
    let mut journal = WindowsJournal::open(&root.join("journal"))?;
    let records = journal.load()?;
    let start = Instant::now();
    let mut observer = NativeObserver::unavailable(start);
    observer.set_signature_policy(policy.observer.require_signed);
    let lease_pid = Arc::new(AtomicUsize::new(0));
    let guarded_pid = lease_pid.clone();
    let guarded_image = PathBuf::from(&policy.client_image);
    observer.set_recording_guard(Arc::new(move || {
        sole_enrolled_process(&guarded_image, guarded_pid.load(Ordering::Acquire) as u32)
    }));
    let stop = Arc::new(AtomicBool::new(false));
    let listener = listen(observer.clone(), &policy, key, stop.clone())?;
    let host = WindowsHost::new(policy.clone(), &records, observer.clone());
    let broker = Arc::new(Mutex::new(Broker::open(
        host,
        journal,
        observer.clone(),
        NativeClock { start },
        EnrolledPeer(policy.client_principal.clone()),
    )?));
    let supervisor = broker.clone();
    let supervisor_stop = stop.clone();
    let lease_process = Arc::new(Mutex::new(None::<Handle>));
    let timer = thread::spawn(move || {
        while !supervisor_stop.load(Ordering::Acquire) {
            if let Ok(mut broker) = supervisor.try_lock() {
                if let Err(error) = broker.supervise() {
                    eprintln!("Native recovery requires review: {error}");
                }
            }
            thread::sleep(Duration::from_millis(250));
        }
    });
    let policy = Arc::new(policy);
    let active = Arc::new(AtomicUsize::new(0));
    let mut pipe = new_pipe(&policy, true)?;
    while !STOP.load(Ordering::Acquire) {
        let ok = unsafe { ConnectNamedPipe(pipe.0, null_mut()) };
        let code = if ok == 0 {
            unsafe { GetLastError() }
        } else {
            ERROR_PIPE_CONNECTED
        };
        if code == ERROR_PIPE_CONNECTED {
            let next = new_pipe(&policy, false)?;
            let connected = std::mem::replace(&mut pipe, next);
            if active.fetch_add(1, Ordering::AcqRel) >= 16 {
                active.fetch_sub(1, Ordering::AcqRel);
                drop(connected);
                continue;
            }
            let (policy, broker, observer, active, lease_process, lease_pid) = (
                policy.clone(),
                broker.clone(),
                observer.clone(),
                active.clone(),
                lease_process.clone(),
                lease_pid.clone(),
            );
            thread::spawn(move || {
                if let Err(error) = handle(
                    &connected,
                    &policy,
                    &broker,
                    &observer,
                    &lease_process,
                    &lease_pid,
                ) {
                    let _ = write_frame(connected.0, json!({"error":error}));
                }
                active.fetch_sub(1, Ordering::AcqRel);
            });
        } else if code != ERROR_PIPE_LISTENING && code != ERROR_NO_DATA {
            return Err(error("Native pipe accept failed"));
        }
        thread::sleep(Duration::from_millis(10));
    }
    while active.load(Ordering::Acquire) > 0 {
        report(SERVICE_STOP_PENDING, 0);
        thread::sleep(Duration::from_millis(100));
    }
    stop.store(true, Ordering::Release);
    let _ = listener.join();
    let _ = timer.join();
    Ok(())
}
static STOP_CHECKPOINT: AtomicUsize = AtomicUsize::new(1);
static SERVICE_HANDLE: AtomicUsize = AtomicUsize::new(0);
fn report(state: u32, exit_code: u32) {
    let handle = SERVICE_HANDLE.load(Ordering::Acquire) as SERVICE_STATUS_HANDLE;
    if !handle.is_null() {
        let status = SERVICE_STATUS {
            dwServiceType: SERVICE_WIN32_OWN_PROCESS,
            dwCurrentState: state,
            dwControlsAccepted: if state == SERVICE_RUNNING {
                SERVICE_ACCEPT_STOP | SERVICE_ACCEPT_SHUTDOWN
            } else {
                0
            },
            dwWin32ExitCode: exit_code,
            dwServiceSpecificExitCode: 0,
            dwCheckPoint: if state == SERVICE_STOP_PENDING {
                STOP_CHECKPOINT.fetch_add(1, Ordering::AcqRel) as u32
            } else {
                0
            },
            dwWaitHint: 30000,
        };
        unsafe {
            SetServiceStatus(handle, &status);
        }
    }
}
unsafe extern "system" fn control(code: u32) {
    if matches!(code, SERVICE_CONTROL_STOP | SERVICE_CONTROL_SHUTDOWN) {
        STOP.store(true, Ordering::Release);
        report(SERVICE_STOP_PENDING, 0)
    }
}
unsafe extern "system" fn service_main(_count: u32, _args: *mut *mut u16) {
    let handle = RegisterServiceCtrlHandlerW(wide(SERVICE).as_ptr(), Some(control));
    if handle.is_null() {
        return;
    }
    SERVICE_HANDLE.store(handle as usize, Ordering::Release);
    report(SERVICE_RUNNING, 0);
    let result = serve();
    if let Err(error) = &result {
        eprintln!("{error}")
    }
    report(
        SERVICE_STOPPED,
        if result.is_ok() {
            0
        } else {
            ERROR_SERVICE_SPECIFIC_ERROR
        },
    );
}
pub fn service_dispatch() -> Result<()> {
    let mut name = wide(SERVICE);
    let table = [
        SERVICE_TABLE_ENTRYW {
            lpServiceName: name.as_mut_ptr(),
            lpServiceProc: Some(service_main),
        },
        SERVICE_TABLE_ENTRYW {
            lpServiceName: null_mut(),
            lpServiceProc: None,
        },
    ];
    if unsafe { StartServiceCtrlDispatcherW(table.as_ptr()) } == 0 {
        Err(error("Windows service dispatcher unavailable"))
    } else {
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn protected_system_command_is_readonly_and_uses_actual_windows_apis() {
        let path = system_powershell().unwrap();
        protected(&path, false).expect("System PowerShell must be protected");
        assert_eq!(
            powershell("@{probe='native-readonly'} | ConvertTo-Json -Compress").unwrap(),
            json!({"probe":"native-readonly"})
        );
    }
    #[test]
    fn actual_windows_snapshot_is_bounded_native_schema_without_mutation() {
        let raw = powershell(SNAPSHOT_SCRIPT).expect("Read-only Windows inspection");
        let snapshot: Snapshot =
            serde_json::from_value(raw).expect("Actual cmdlet output matches native schema");
        assert!(!snapshot.adapters.is_empty());
        assert_eq!(snapshot.profiles.len(), 3);
        assert!(snapshot.adapters.len() <= 256 && snapshot.routes.len() <= 8192);
    }
    #[test]
    fn exclusive_observer_rejects_second_binding_without_sending() {
        let first = exclusive_udp(Ipv4Addr::LOCALHOST, 0).unwrap();
        let port = first.local_addr().unwrap().port();
        assert!(exclusive_udp(Ipv4Addr::LOCALHOST, port).is_err());
        assert!(std::net::UdpSocket::bind((Ipv4Addr::LOCALHOST, port)).is_err());
    }
    #[test]
    fn native_process_command_line_is_readonly_and_bounded() {
        let arguments = process_arguments(unsafe { GetCurrentProcess() }).unwrap();
        assert!(!arguments.is_empty());
    }
    #[test]
    fn unknown_lease_pid_never_proves_recorder_inactive() {
        assert!(!sole_enrolled_process(
            Path::new(r"C:\Program Files\Q22\q22-engine.exe"),
            0
        ));
        let mut observer = NativeObserver::unavailable(Instant::now());
        observer.set_recording_guard(Arc::new(|| false));
        observer.accept(FlightState::Disarmed).unwrap();
        observer.recording_lease(false).unwrap();
        assert!(observer.observe().unwrap().recording_active);
    }
}
