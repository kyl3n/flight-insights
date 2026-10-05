//! PyInstaller one-file programs have a launcher and a Python child. Own the
//! whole process tree so a timed-out launcher cannot leave an active engine.
use std::io;
use tokio::process::{Child, Command};

#[cfg(unix)]
pub fn configure(command: &mut Command) {
    command.process_group(0);
}

#[cfg(windows)]
pub fn configure(command: &mut Command) {
    // Suspend the first thread until the launcher belongs to our private job.
    // Its Python descendant therefore cannot escape in a spawn/assign race.
    command.creation_flags(0x08000004); // CREATE_NO_WINDOW | CREATE_SUSPENDED
}

#[cfg(unix)]
pub struct ProcessTree {
    group: Option<i32>,
}

#[cfg(unix)]
impl ProcessTree {
    pub fn attach(child: &Child) -> io::Result<Self> {
        let group = child
            .id()
            .and_then(|id| i32::try_from(id).ok())
            .filter(|id| *id > 0)
            .ok_or_else(|| io::Error::other("Engine process has no valid process group"))?;
        Ok(Self { group: Some(group) })
    }

    pub fn terminate(&mut self) {
        if let Some(group) = self.group.take() {
            // SAFETY: configure() made this owned child the leader of a new
            // process group. The strictly positive ID can never target ours.
            unsafe {
                libc::kill(-group, libc::SIGKILL);
            }
        }
    }

    pub async fn terminate_and_wait(&mut self) -> io::Result<()> {
        self.terminate();
        Ok(())
    }
}

#[cfg(unix)]
impl Drop for ProcessTree {
    fn drop(&mut self) {
        self.terminate();
    }
}

#[cfg(windows)]
pub struct ProcessTree {
    job: std::os::windows::io::OwnedHandle,
}

#[cfg(windows)]
impl ProcessTree {
    pub fn attach(child: &Child) -> io::Result<Self> {
        use std::os::windows::io::{AsRawHandle, FromRawHandle, OwnedHandle};
        use windows_sys::Win32::System::JobObjects::{
            AssignProcessToJobObject, CreateJobObjectW, JobObjectExtendedLimitInformation,
            SetInformationJobObject, JOBOBJECT_EXTENDED_LIMIT_INFORMATION,
            JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE,
        };
        // SAFETY: new unnamed job, no caller-provided handles or names. The
        // OwnedHandle closes it on every error path, terminating assigned children.
        unsafe {
            let raw = CreateJobObjectW(std::ptr::null(), std::ptr::null());
            if raw.is_null() {
                return Err(io::Error::last_os_error());
            }
            let job = OwnedHandle::from_raw_handle(raw);
            let mut limits: JOBOBJECT_EXTENDED_LIMIT_INFORMATION = std::mem::zeroed();
            limits.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE;
            if SetInformationJobObject(
                job.as_raw_handle(),
                JobObjectExtendedLimitInformation,
                &limits as *const _ as *const _,
                std::mem::size_of_val(&limits) as u32,
            ) == 0
            {
                return Err(io::Error::last_os_error());
            }
            let process = child
                .raw_handle()
                .ok_or_else(|| io::Error::other("Missing engine process handle"))?;
            if AssignProcessToJobObject(job.as_raw_handle(), process) == 0 {
                return Err(io::Error::last_os_error());
            }
            resume_primary_thread(
                child
                    .id()
                    .ok_or_else(|| io::Error::other("Missing engine PID"))?,
            )?;
            Ok(Self { job })
        }
    }

    pub fn terminate(&mut self) {
        use std::os::windows::io::AsRawHandle;
        // SAFETY: handle is the owned private job created in attach().
        unsafe {
            windows_sys::Win32::System::JobObjects::TerminateJobObject(self.job.as_raw_handle(), 1);
        }
    }

    pub fn active_processes(&self) -> io::Result<u32> {
        use std::os::windows::io::AsRawHandle;
        use windows_sys::Win32::System::JobObjects::{
            JobObjectBasicAccountingInformation, QueryInformationJobObject,
            JOBOBJECT_BASIC_ACCOUNTING_INFORMATION,
        };
        // SAFETY: owned job handle and correctly sized output structure.
        unsafe {
            let mut accounting: JOBOBJECT_BASIC_ACCOUNTING_INFORMATION = std::mem::zeroed();
            if QueryInformationJobObject(
                self.job.as_raw_handle(),
                JobObjectBasicAccountingInformation,
                &mut accounting as *mut _ as *mut _,
                std::mem::size_of_val(&accounting) as u32,
                std::ptr::null_mut(),
            ) == 0
            {
                return Err(io::Error::last_os_error());
            }
            Ok(accounting.ActiveProcesses)
        }
    }

    pub async fn terminate_and_wait(&mut self) -> io::Result<()> {
        self.terminate();
        // TerminateJobObject starts asynchronous teardown. Do not start a new
        // engine or remove its files while the old Python child still owns them.
        let deadline = tokio::time::Instant::now() + std::time::Duration::from_secs(3);
        loop {
            if self.active_processes()? == 0 {
                return Ok(());
            }
            if tokio::time::Instant::now() >= deadline {
                return Err(io::Error::new(
                    io::ErrorKind::TimedOut,
                    "The engine process tree has not finished shutting down",
                ));
            }
            tokio::time::sleep(std::time::Duration::from_millis(10)).await;
        }
    }
}

#[cfg(windows)]
fn resume_primary_thread(process_id: u32) -> io::Result<()> {
    use std::os::windows::io::{AsRawHandle, FromRawHandle, OwnedHandle};
    use windows_sys::Win32::{
        Foundation::INVALID_HANDLE_VALUE,
        System::{
            Diagnostics::ToolHelp::{
                CreateToolhelp32Snapshot, Thread32First, Thread32Next, TH32CS_SNAPTHREAD,
                THREADENTRY32,
            },
            Threading::{OpenThread, ResumeThread, THREAD_SUSPEND_RESUME},
        },
    };
    // SAFETY: enumerate thread metadata, select only the newly created owned
    // process, then resume its initial thread. No process code has run yet.
    unsafe {
        let raw_snapshot = CreateToolhelp32Snapshot(TH32CS_SNAPTHREAD, 0);
        if raw_snapshot == INVALID_HANDLE_VALUE {
            return Err(io::Error::last_os_error());
        }
        let snapshot = OwnedHandle::from_raw_handle(raw_snapshot);
        let mut entry: THREADENTRY32 = std::mem::zeroed();
        entry.dwSize = std::mem::size_of_val(&entry) as u32;
        let mut present = Thread32First(snapshot.as_raw_handle(), &mut entry);
        while present != 0 {
            if entry.th32OwnerProcessID == process_id {
                let raw_thread = OpenThread(THREAD_SUSPEND_RESUME, 0, entry.th32ThreadID);
                if raw_thread.is_null() {
                    return Err(io::Error::last_os_error());
                }
                let thread = OwnedHandle::from_raw_handle(raw_thread);
                if ResumeThread(thread.as_raw_handle()) == u32::MAX {
                    return Err(io::Error::last_os_error());
                }
                return Ok(());
            }
            present = Thread32Next(snapshot.as_raw_handle(), &mut entry);
        }
        Err(io::Error::other(
            "Engine's suspended primary thread could not be located",
        ))
    }
}
