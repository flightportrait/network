//! The per-airframe legs artifact (SQLite, read-only). The live core
//! needs only its edge: the newest observed day, reported by /v1/now as
//! `archive_through`. The file is re-checked every five minutes and
//! reopened when it changes; a missing file means the archive is dark.

use std::sync::Mutex;
use std::time::{Duration, Instant, SystemTime};

const RELOAD: Duration = Duration::from_secs(300);

struct Inner {
    loaded_mtime: Option<SystemTime>,
    next_check: Option<Instant>,
    through: Option<String>,
}

pub struct LegBook {
    path: String,
    inner: Mutex<Inner>,
}

impl LegBook {
    pub fn new(path: &str) -> LegBook {
        LegBook {
            path: path.to_string(),
            inner: Mutex::new(Inner { loaded_mtime: None, next_check: None, through: None }),
        }
    }

    fn open(&self) -> Option<Option<String>> {
        let conn = rusqlite::Connection::open_with_flags(
            &self.path,
            rusqlite::OpenFlags::SQLITE_OPEN_READ_ONLY | rusqlite::OpenFlags::SQLITE_OPEN_NO_MUTEX,
        )
        .ok()?;
        conn.query_row("SELECT 1 FROM legs LIMIT 1", [], |_| Ok(())).ok()?;
        Some(conn.query_row("SELECT MAX(date) FROM legs", [], |r| r.get::<_, Option<String>>(0)).ok().flatten())
    }

    /// ISO date of the newest leg, None while the archive is dark.
    pub fn archive_through(&self) -> Option<String> {
        let mut g = self.inner.lock().unwrap();
        let now = Instant::now();
        if g.next_check.is_some_and(|t| now < t) && g.loaded_mtime.is_some() {
            return g.through.clone();
        }
        g.next_check = Some(now + RELOAD);
        let Ok(mtime) = std::fs::metadata(&self.path).and_then(|m| m.modified()) else {
            g.loaded_mtime = None;
            g.through = None;
            return None;
        };
        if g.loaded_mtime != Some(mtime) {
            // a file that will not open keeps whatever worked last
            if let Some(through) = self.open() {
                g.through = through;
                g.loaded_mtime = Some(mtime);
            }
        }
        g.loaded_mtime.and(g.through.clone())
    }
}
