use anyhow::{Context, Result};
use sha2::{Digest, Sha256};
use std::fs::{self, File, OpenOptions, TryLockError};
use std::path::Path;
use std::time::{Duration, Instant};

/// An OS lock released on drop or process death. Never unlink a lock file:
/// another process could already hold its inode while a third creates a new one.
pub struct OperationLock {
    _file: File,
}

impl OperationLock {
    pub fn repository(root: &Path, timeout: Duration) -> Result<Self> {
        let root = root
            .canonicalize()
            .context("resolve repository lock identity")?;
        Self::acquire("repo", &root.to_string_lossy(), timeout)
    }

    pub fn device(serial: &str, timeout: Duration) -> Result<Self> {
        Self::acquire("device", serial, timeout)
    }

    pub fn runtime_directory() -> Result<std::path::PathBuf> {
        let user = std::env::var_os("HOME")
            .or_else(|| std::env::var_os("USERPROFILE"))
            .unwrap_or_default();
        let owner = format!("{:x}", Sha256::digest(user.to_string_lossy().as_bytes()));
        // A stable host directory keeps device locks shared across repo-specific TMPDIRs.
        #[cfg(unix)]
        let base = std::path::PathBuf::from("/tmp");
        #[cfg(not(unix))]
        let base = std::env::temp_dir();
        let directory = base.join(format!("minimap-locks-{}", &owner[..16]));
        fs::create_dir_all(&directory)?;
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            fs::set_permissions(&directory, fs::Permissions::from_mode(0o700))?;
        }
        Ok(directory)
    }

    fn acquire(namespace: &str, key: &str, timeout: Duration) -> Result<Self> {
        let directory = Self::runtime_directory()?;
        let digest = format!("{:x}", Sha256::digest(key.as_bytes()));
        let path = directory.join(format!("{namespace}-{digest}.lock"));
        let mut options = OpenOptions::new();
        options.read(true).write(true).create(true).truncate(false);
        #[cfg(unix)]
        {
            use std::os::unix::fs::OpenOptionsExt;
            options.mode(0o600);
        }
        let file = options.open(path)?;
        let start = Instant::now();
        loop {
            match file.try_lock() {
                Ok(()) => return Ok(Self { _file: file }),
                Err(TryLockError::WouldBlock) if start.elapsed() < timeout => {
                    std::thread::sleep(Duration::from_millis(20));
                }
                Err(TryLockError::WouldBlock) => {
                    return Err(std::io::Error::new(
                        std::io::ErrorKind::WouldBlock,
                        format!("{namespace} is busy; retry after the active Minimap operation finishes"),
                    ).into());
                }
                Err(TryLockError::Error(error)) => return Err(error.into()),
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn contention_is_bounded_and_drop_releases_the_lock() {
        let root = tempfile::tempdir().unwrap();
        let first = OperationLock::repository(root.path(), Duration::ZERO).unwrap();
        assert!(OperationLock::repository(root.path(), Duration::ZERO).is_err());
        drop(first);
        assert!(OperationLock::repository(root.path(), Duration::ZERO).is_ok());
    }

    #[test]
    fn independent_repository_locks_do_not_conflict() {
        let a = tempfile::tempdir().unwrap();
        let b = tempfile::tempdir().unwrap();
        let _a = OperationLock::repository(a.path(), Duration::ZERO).unwrap();
        let _b = OperationLock::repository(b.path(), Duration::ZERO).unwrap();
    }
}
