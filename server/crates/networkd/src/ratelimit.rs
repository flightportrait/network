//! Per-IP, per-bucket sliding-window rate limiting, as the Python
//! service does it: every route throttles before any other work, each
//! bucket on its own, idle addresses swept so memory does not grow with
//! the number of clients.

use std::collections::{HashMap, VecDeque};
use std::sync::Mutex;
use std::time::Instant;

const SWEEP_EVERY: u64 = 4096;

pub struct Limited {
    pub limit: usize,
    pub retry_s: u64,
}

struct Inner {
    windows: HashMap<&'static str, HashMap<String, VecDeque<Instant>>>,
    calls: u64,
}

pub struct RateLimiter(Mutex<Inner>);

impl RateLimiter {
    pub fn new() -> RateLimiter {
        RateLimiter(Mutex::new(Inner { windows: HashMap::new(), calls: 0 }))
    }

    pub fn check(&self, bucket: &'static str, ip: &str, limit: usize, window_s: u64) -> Result<(), Limited> {
        let now = Instant::now();
        let window = std::time::Duration::from_secs(window_s);
        let mut g = self.0.lock().unwrap();
        g.calls += 1;
        if g.calls >= SWEEP_EVERY {
            g.calls = 0;
            for b in g.windows.values_mut() {
                b.retain(|_, w| w.back().is_some_and(|t| now.duration_since(*t) < window));
            }
        }
        let w = g.windows.entry(bucket).or_default().entry(ip.to_string()).or_default();
        while w.front().is_some_and(|t| now.duration_since(*t) >= window) {
            w.pop_front();
        }
        if w.len() >= limit {
            let retry_s = match w.front() {
                Some(first) => {
                    let left = window.saturating_sub(now.duration_since(*first)).as_secs_f64();
                    (left.ceil() as u64).max(1)
                }
                None => window_s,
            };
            return Err(Limited { limit, retry_s });
        }
        w.push_back(now);
        Ok(())
    }
}

impl Default for RateLimiter {
    fn default() -> Self {
        Self::new()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn limits_per_bucket_and_ip() {
        let r = RateLimiter::new();
        assert!(r.check("now", "a", 2, 600).is_ok());
        assert!(r.check("now", "a", 2, 600).is_ok());
        let e = r.check("now", "a", 2, 600).unwrap_err();
        assert_eq!(e.limit, 2);
        assert!(e.retry_s >= 599 && e.retry_s <= 600);
        assert!(r.check("now", "b", 2, 600).is_ok());
        assert!(r.check("aircraft", "a", 2, 600).is_ok());
    }
}
