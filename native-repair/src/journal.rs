//! Helper-owned durable records. This is never the app's `network-repairs/`.
//! There is no RPC field or shipped CLI option selecting a journal directory.
use crate::{Journal, Record, Result, MAX_RECORDS};
use std::fs::{self, File, OpenOptions};
use std::io::{Read, Write};
use std::path::{Path, PathBuf};
use uuid::Uuid;

const MAX_RECORD_BYTES: u64 = 1024 * 1024;
pub struct FileJournal {
    root: PathBuf,
    _lock: File,
}

#[cfg(unix)]
fn secure(path: &Path, directory: bool) -> Result<()> {
    use std::os::unix::fs::MetadataExt;
    let m = fs::symlink_metadata(path).map_err(|e| e.to_string())?;
    if m.file_type().is_symlink()
        || (directory && !m.is_dir())
        || (!directory && !m.is_file())
        || m.uid() != unsafe { libc::geteuid() }
        || m.mode() & 0o077 != 0
        || (!directory && m.nlink() != 1)
    {
        return Err(
            "Helper journal must be private, ordinary, single-link and service-owned".into(),
        );
    }
    Ok(())
}
#[cfg(not(unix))]
fn secure(_path: &Path, _directory: bool) -> Result<()> {
    Err("Windows service-owned ACL journal enrollment is not installed".into())
}
impl FileJournal {
    /// Called only by an enrolled service host with an installation-owned path.
    /// Windows fails closed until the service host supplies an ACL-qualified
    /// journal provider; ordinary app-data files cannot become authority.
    pub fn open(root: &Path) -> Result<Self> {
        Self::open_checked(root, true)
    }
    /// Debug-only filesystem fixture; never called by the installed service.
    #[cfg(debug_assertions)]
    pub fn open_fixture(root: &Path) -> Result<Self> {
        Self::open_checked(root, false)
    }
    fn open_checked(root: &Path, enforce_ancestors: bool) -> Result<Self> {
        if !root.is_absolute() {
            return Err("Helper journal path must be installation-owned and absolute".into());
        }
        #[cfg(unix)]
        {
            use std::os::unix::fs::{DirBuilderExt, MetadataExt, OpenOptionsExt};
            // Reject writable ancestors (apart from root-owned sticky /tmp for
            // disposable tests) and symlinks before creating a service directory.
            for ancestor in root.ancestors().skip(1).filter(|_| enforce_ancestors) {
                let m = fs::symlink_metadata(ancestor).map_err(|e| e.to_string())?;
                let sticky_tmp =
                    ancestor == Path::new("/tmp") && m.uid() == 0 && m.mode() & 0o1000 != 0;
                if m.file_type().is_symlink()
                    || !m.is_dir()
                    || (!sticky_tmp
                        && (m.uid() != unsafe { libc::geteuid() } && m.uid() != 0
                            || m.mode() & 0o022 != 0))
                {
                    return Err("Untrusted helper journal ancestor".into());
                }
            }
            match fs::DirBuilder::new().mode(0o700).create(root) {
                Ok(()) => {}
                Err(error) if error.kind() == std::io::ErrorKind::AlreadyExists => {}
                Err(error) => return Err(error.to_string()),
            }
            secure(root, true)?;
            let lock_path = root.join("service.lock");
            let lock = OpenOptions::new()
                .read(true)
                .write(true)
                .create(true)
                .truncate(false)
                .mode(0o600)
                .custom_flags(libc::O_NOFOLLOW | libc::O_CLOEXEC)
                .open(&lock_path)
                .map_err(|e| e.to_string())?;
            secure(&lock_path, false)?;
            use std::os::fd::AsRawFd;
            if unsafe { libc::flock(lock.as_raw_fd(), libc::LOCK_EX | libc::LOCK_NB) } != 0 {
                return Err("Another native helper owns this journal".into());
            }
            Ok(Self {
                root: root.to_path_buf(),
                _lock: lock,
            })
        }
        #[cfg(not(unix))]
        {
            let _ = (root, enforce_ancestors);
            Err("Windows service-owned ACL journal enrollment is not installed".into())
        }
    }
    fn path(&self, id: &str) -> Result<PathBuf> {
        if !crate::valid_id(id) {
            return Err("Invalid helper receipt identity".into());
        }
        Ok(self.root.join(format!("{id}.json")))
    }
    fn read_record(&self, path: &Path) -> Result<Record> {
        secure(path, false)?;
        let mut options = OpenOptions::new();
        options.read(true);
        #[cfg(unix)]
        {
            use std::os::unix::fs::OpenOptionsExt;
            options.custom_flags(libc::O_NOFOLLOW | libc::O_CLOEXEC);
        }
        let file = options.open(path).map_err(|e| e.to_string())?;
        if file.metadata().map_err(|e| e.to_string())?.len() > MAX_RECORD_BYTES {
            return Err("Helper receipt exceeds limit".into());
        }
        let mut bytes = Vec::new();
        file.take(MAX_RECORD_BYTES + 1)
            .read_to_end(&mut bytes)
            .map_err(|e| e.to_string())?;
        if bytes.len() as u64 > MAX_RECORD_BYTES {
            return Err("Helper receipt exceeds limit".into());
        }
        let record: Record = serde_json::from_slice(&bytes).map_err(|e| e.to_string())?;
        record.validate()?;
        if path.file_stem().and_then(|s| s.to_str()) != Some(record.plan.plan_id.as_str()) {
            return Err("Helper receipt identity mismatch".into());
        }
        Ok(record)
    }
}
impl Journal for FileJournal {
    fn load(&mut self) -> Result<Vec<Record>> {
        secure(&self.root, true)?;
        let mut records = Vec::new();
        for entry in fs::read_dir(&self.root).map_err(|e| e.to_string())? {
            let path = entry.map_err(|e| e.to_string())?.path();
            if path.extension().and_then(|v| v.to_str()) != Some("json") {
                continue;
            }
            if records.len() >= MAX_RECORDS {
                return Err("Helper receipt capacity exceeded".into());
            }
            records.push(self.read_record(&path)?);
        }
        Ok(records)
    }
    fn save(&mut self, record: &Record) -> Result<()> {
        record.validate()?;
        secure(&self.root, true)?;
        let path = self.path(&record.plan.plan_id)?;
        if path.exists() {
            let previous = self.read_record(&path)?;
            if previous.plan != record.plan || !record.events.starts_with(&previous.events) {
                return Err("Helper plan and original event history are immutable".into());
            }
        }
        let bytes = serde_json::to_vec(record).map_err(|e| e.to_string())?;
        if bytes.len() as u64 > MAX_RECORD_BYTES {
            return Err("Helper receipt exceeds limit".into());
        }
        let temporary = self.root.join(format!(".{}.tmp", Uuid::new_v4().simple()));
        let result = (|| {
            let mut options = OpenOptions::new();
            options.create_new(true).write(true);
            #[cfg(unix)]
            {
                use std::os::unix::fs::OpenOptionsExt;
                options
                    .mode(0o600)
                    .custom_flags(libc::O_NOFOLLOW | libc::O_CLOEXEC);
            }
            let mut file = options.open(&temporary).map_err(|e| e.to_string())?;
            file.write_all(&bytes).map_err(|e| e.to_string())?;
            file.sync_all().map_err(|e| e.to_string())?;
            drop(file);
            fs::rename(&temporary, &path).map_err(|e| e.to_string())?;
            File::open(&self.root)
                .and_then(|d| d.sync_all())
                .map_err(|e| e.to_string())?;
            Ok(())
        })();
        if result.is_err() {
            let _ = fs::remove_file(&temporary);
        }
        result
    }
}
