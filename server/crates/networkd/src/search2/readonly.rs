//! A generation opened as the immutable thing it is: memory-mapped,
//! never written, no lock files. Tantivy takes a lock file to read an
//! index's metadata safely while a writer may commit; a generation is
//! renamed into place whole and never written again, and networkd reads
//! it from a read-only mount, where that lock file cannot be created.

use std::io;
use std::path::{Path, PathBuf};
use std::sync::Arc;

use tantivy::directory::error::{DeleteError, LockError, OpenReadError, OpenWriteError};
use tantivy::directory::{Directory, DirectoryLock, FileHandle, Lock, MmapDirectory, WatchCallback, WatchHandle, WritePtr};

#[derive(Clone, Debug)]
pub struct ReadOnly(MmapDirectory);

impl ReadOnly {
    pub fn open(dir: &Path) -> tantivy::Result<ReadOnly> {
        Ok(ReadOnly(MmapDirectory::open(dir)?))
    }
}

fn denied(path: &Path) -> (Arc<io::Error>, PathBuf) {
    (Arc::new(io::Error::new(io::ErrorKind::PermissionDenied, "read-only index")), path.to_path_buf())
}

impl Directory for ReadOnly {
    fn get_file_handle(&self, path: &Path) -> Result<Arc<dyn FileHandle>, OpenReadError> {
        self.0.get_file_handle(path)
    }

    fn delete(&self, path: &Path) -> Result<(), DeleteError> {
        let (io_error, filepath) = denied(path);
        Err(DeleteError::IoError { io_error, filepath })
    }

    fn exists(&self, path: &Path) -> Result<bool, OpenReadError> {
        self.0.exists(path)
    }

    fn open_write(&self, path: &Path) -> Result<WritePtr, OpenWriteError> {
        let (io_error, filepath) = denied(path);
        Err(OpenWriteError::IoError { io_error, filepath })
    }

    fn atomic_read(&self, path: &Path) -> Result<Vec<u8>, OpenReadError> {
        self.0.atomic_read(path)
    }

    fn atomic_write(&self, _path: &Path, _data: &[u8]) -> io::Result<()> {
        Err(io::Error::new(io::ErrorKind::PermissionDenied, "read-only index"))
    }

    fn sync_directory(&self) -> io::Result<()> {
        Ok(())
    }

    /// Nothing writes a generation: no lock to take.
    fn acquire_lock(&self, _lock: &Lock) -> Result<DirectoryLock, LockError> {
        Ok(DirectoryLock::from(Box::new(())))
    }

    fn watch(&self, _watch_callback: WatchCallback) -> tantivy::Result<WatchHandle> {
        Ok(WatchHandle::empty())
    }
}
