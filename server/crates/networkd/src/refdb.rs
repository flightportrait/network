//! The read-only reference snapshot (`refdata.sqlite`, written nightly
//! by the API's refdata_export) and the responses computed from it.
//!
//! Everything read from it changes only when a new file lands, so a
//! response is computed once per snapshot and served from memory until
//! the next one. The file is re-checked every 30 s; a new one (the export
//! renames it into place) starts a new generation and empties the cache.
//! No file: `available()` is false and routes forward to the fallback.

use std::collections::HashMap;
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant, SystemTime};

use bytes::Bytes;
use rusqlite::{Connection, OpenFlags};

const RECHECK: Duration = Duration::from_secs(30);
const CACHE_MAX: usize = 20_000;

struct Inner {
    mtime: Option<SystemTime>,
    next_check: Option<Instant>,
    generation: u64,
    pool: Vec<Connection>,
    cache: HashMap<String, Bytes>,
    /// the snapshot's table names, read once per generation
    tables: Option<std::collections::HashSet<String>>,
}

pub struct RefDb {
    path: String,
    inner: Mutex<Inner>,
}

/// A connection borrowed from the pool, returned when dropped (unless the
/// snapshot changed meanwhile).
pub struct Conn<'a> {
    db: &'a RefDb,
    generation: u64,
    conn: Option<Connection>,
}

impl std::ops::Deref for Conn<'_> {
    type Target = Connection;
    fn deref(&self) -> &Connection {
        self.conn.as_ref().unwrap()
    }
}

impl Drop for Conn<'_> {
    fn drop(&mut self) {
        let mut g = self.db.inner.lock().unwrap();
        if g.generation == self.generation && g.pool.len() < 16 {
            g.pool.push(self.conn.take().unwrap());
        }
    }
}

impl RefDb {
    pub fn new(path: &str) -> Arc<RefDb> {
        Arc::new(RefDb {
            path: path.to_string(),
            inner: Mutex::new(Inner {
                mtime: None,
                next_check: None,
                generation: 0,
                pool: vec![],
                cache: HashMap::new(),
                tables: None,
            }),
        })
    }

    fn refresh(&self, g: &mut Inner) {
        let now = Instant::now();
        if g.next_check.is_some_and(|t| now < t) {
            return;
        }
        g.next_check = Some(now + RECHECK);
        let mtime = std::fs::metadata(&self.path).and_then(|m| m.modified()).ok();
        if mtime != g.mtime {
            g.mtime = mtime;
            g.generation += 1;
            g.pool.clear();
            g.cache.clear();
            g.tables = None;
            if mtime.is_some() {
                eprintln!("refdata: {} (generation {})", self.path, g.generation);
            }
        }
    }

    pub fn available(&self) -> bool {
        let mut g = self.inner.lock().unwrap();
        self.refresh(&mut g);
        g.mtime.is_some()
    }

    /// The snapshot is there and holds every table in `needed`: a route
    /// newer than the file on disk forwards until the next export.
    pub fn has(&self, needed: &[&str]) -> bool {
        let mut g = self.inner.lock().unwrap();
        self.refresh(&mut g);
        if g.mtime.is_none() {
            return false;
        }
        if g.tables.is_none() {
            let read = Connection::open_with_flags(&self.path, OpenFlags::SQLITE_OPEN_READ_ONLY)
                .and_then(|c| {
                    let mut stmt = c.prepare("SELECT name FROM sqlite_master WHERE type = 'table'")?;
                    let names = stmt.query_map([], |r| r.get::<_, String>(0))?.collect();
                    names
                });
            match read {
                Ok(names) => g.tables = Some(names),
                Err(_) => return false,
            }
        }
        let tables = g.tables.as_ref().unwrap();
        needed.iter().all(|t| tables.contains(*t))
    }

    pub fn conn(&self) -> rusqlite::Result<Conn<'_>> {
        let (generation, pooled) = {
            let mut g = self.inner.lock().unwrap();
            self.refresh(&mut g);
            (g.generation, g.pool.pop())
        };
        let conn = match pooled {
            Some(c) => c,
            None => {
                let c = Connection::open_with_flags(
                    &self.path,
                    OpenFlags::SQLITE_OPEN_READ_ONLY | OpenFlags::SQLITE_OPEN_NO_MUTEX,
                )?;
                c.pragma_update(None, "query_only", true)?;
                c.pragma_update(None, "cache_size", -32_000)?;
                c
            }
        };
        Ok(Conn { db: self, generation, conn: Some(conn) })
    }

    /// The cached body for `key` in the current snapshot, if any.
    pub fn cached(&self, key: &str) -> Option<Bytes> {
        let mut g = self.inner.lock().unwrap();
        self.refresh(&mut g);
        g.cache.get(key).cloned()
    }

    /// Remember `body` for `key`, if the snapshot has not changed since
    /// `generation` (the one the body was computed from).
    pub fn remember(&self, key: String, generation: u64, body: Bytes) {
        let mut g = self.inner.lock().unwrap();
        if g.generation != generation {
            return;
        }
        if g.cache.len() >= CACHE_MAX {
            g.cache.clear();
        }
        g.cache.insert(key, body);
    }
}

impl Conn<'_> {
    pub fn generation(&self) -> u64 {
        self.generation
    }
}
