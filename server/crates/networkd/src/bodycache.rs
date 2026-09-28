//! Response bodies kept in memory, bounded by bytes, not by count: one
//! airline's routes weigh a hundred times a flight's, so a count bound
//! says nothing about memory. Past the budget the cache starts over
//! (the callers' entries all go stale together at the next snapshot
//! anyway), and says so, so a log shows when and how full.

use std::collections::HashMap;

use bytes::Bytes;

pub struct BodyCache {
    name: &'static str,
    budget: usize,
    bytes: usize,
    map: HashMap<String, Bytes>,
}

impl BodyCache {
    pub fn new(name: &'static str, budget: usize) -> BodyCache {
        BodyCache { name, budget, bytes: 0, map: HashMap::new() }
    }

    pub fn get(&self, key: &str) -> Option<Bytes> {
        self.map.get(key).cloned()
    }

    pub fn insert(&mut self, key: String, body: Bytes) {
        let size = key.len() + body.len();
        if size > self.budget {
            return;
        }
        if self.bytes + size > self.budget {
            eprintln!(
                "{}: cache full ({} entries, {} MB), cleared",
                self.name,
                self.map.len(),
                self.bytes / (1024 * 1024)
            );
            self.clear();
        }
        let key_len = key.len();
        if let Some(old) = self.map.insert(key, body) {
            self.bytes -= key_len + old.len();
        }
        self.bytes += size;
    }

    pub fn clear(&mut self) {
        self.map.clear();
        self.map.shrink_to_fit();
        self.bytes = 0;
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn stays_within_budget() {
        let mut c = BodyCache::new("test", 100);
        for i in 0..50 {
            c.insert(format!("k{i}"), Bytes::from(vec![b'x'; 20]));
            assert!(c.bytes <= 100);
        }
        assert!(c.get("k49").is_some());
    }

    #[test]
    fn replacing_a_key_counts_once() {
        let mut c = BodyCache::new("test", 100);
        c.insert("a".into(), Bytes::from_static(b"12345"));
        c.insert("a".into(), Bytes::from_static(b"123"));
        assert_eq!(c.bytes, 4);
        assert_eq!(c.get("a").unwrap(), Bytes::from_static(b"123"));
    }

    #[test]
    fn a_body_over_budget_is_not_kept() {
        let mut c = BodyCache::new("test", 10);
        c.insert("a".into(), Bytes::from_static(b"1234"));
        c.insert("big".into(), Bytes::from(vec![0; 64]));
        assert!(c.get("big").is_none());
        assert!(c.get("a").is_some());
    }
}
