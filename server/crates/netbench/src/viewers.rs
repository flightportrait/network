//! A crowd of map viewers on a live stream (`/v1/stream` or
//! `/v2/stream`): each connects the way a browser does (offering
//! permessage-deflate), sends its viewport, and reads frames. The harness
//! counts bytes on the wire and decoded, measures how old each frame is
//! on arrival, and samples the server's CPU and memory from /proc.

use std::sync::atomic::{AtomicU64, Ordering::Relaxed};
use std::sync::Arc;
use std::time::{Duration, Instant};

use rand::{Rng, SeedableRng};
use rand_chacha::ChaCha8Rng;

use crate::sky::{now_s, pick_cluster};
use crate::wsclient::{Client, Msg};

const BUCKET_MS: u64 = 10;
const BUCKETS: usize = 1001; // 0..10 s in 10 ms steps, last = overflow

pub struct Stats {
    connected: AtomicU64,
    failed: AtomicU64,
    closed: AtomicU64,
    frames: AtomicU64,
    bytes: AtomicU64,
    wire: AtomicU64,
    stale: AtomicU64,
    hist: Vec<AtomicU64>,
}

impl Stats {
    fn new() -> Stats {
        Stats {
            connected: AtomicU64::new(0),
            failed: AtomicU64::new(0),
            closed: AtomicU64::new(0),
            frames: AtomicU64::new(0),
            bytes: AtomicU64::new(0),
            wire: AtomicU64::new(0),
            stale: AtomicU64::new(0),
            hist: (0..BUCKETS).map(|_| AtomicU64::new(0)).collect(),
        }
    }

    fn hist_snapshot(&self) -> Vec<u64> {
        self.hist.iter().map(|h| h.load(Relaxed)).collect()
    }
}

#[derive(Clone, Copy, Debug)]
pub struct Mix {
    pub regional: u32,
    pub country: u32,
    pub world: u32,
}

impl std::str::FromStr for Mix {
    type Err = String;
    /// `regional=70,country=25,world=5`
    fn from_str(s: &str) -> Result<Mix, String> {
        let mut m = Mix { regional: 0, country: 0, world: 0 };
        for part in s.split(',') {
            let (k, v) = part.split_once('=').ok_or("mix is kind=weight,…")?;
            let v: u32 = v.parse().map_err(|_| format!("bad weight {v}"))?;
            match k {
                "regional" => m.regional = v,
                "country" => m.country = v,
                "world" => m.world = v,
                _ => return Err(format!("unknown viewer kind {k}")),
            }
        }
        if m.regional + m.country + m.world == 0 {
            return Err("mix weights sum to zero".into());
        }
        Ok(m)
    }
}

/// A viewport `[w, s, e, n]` of the given kind, centred on busy sky.
fn viewport(mix: Mix, rng: &mut impl Rng) -> [f64; 4] {
    let total = mix.regional + mix.country + mix.world;
    let x = rng.gen_range(0..total);
    let (half_w, half_h) = if x < mix.regional {
        (2.5, 1.75)
    } else if x < mix.regional + mix.country {
        (9.0, 6.0)
    } else {
        return [-180.0, -85.0, 180.0, 85.0];
    };
    let (lat, lon, spread) = pick_cluster(rng);
    let clat = (lat + rng.gen_range(-0.5..0.5) * spread).clamp(-80.0, 80.0);
    let clon = lon + rng.gen_range(-0.5..0.5) * spread;
    let r3 = |v: f64| (v * 1000.0).round() / 1000.0;
    [
        r3((clon - half_w).max(-180.0)),
        r3((clat - half_h).max(-85.0)),
        r3((clon + half_w).min(180.0)),
        r3((clat + half_h).min(85.0)),
    ]
}

/// The frame's `t` (the sky's clock), read without parsing the frame:
/// every frame starts `{"t":` or carries `"t":` near the front.
fn frame_t(text: &str) -> Option<f64> {
    let i = text.find("\"t\":")? + 4;
    let rest = &text[i..];
    let end = rest.find([',', '}']).unwrap_or(rest.len());
    rest[..end].trim().parse().ok()
}

/// The first reason a viewer went away, said once: a crowd that drops
/// should explain itself.
fn note_first_close(why: &str) {
    static SAID: std::sync::atomic::AtomicBool = std::sync::atomic::AtomicBool::new(false);
    if !SAID.swap(true, Relaxed) {
        eprintln!("first viewer lost: {why}");
    }
}

async fn viewer(url: String, bbox: [f64; 4], deflate: bool, stats: Arc<Stats>) {
    let mut ws = match tokio::time::timeout(Duration::from_secs(15), Client::connect(&url, deflate)).await {
        Ok(Ok(ws)) => ws,
        _ => {
            stats.failed.fetch_add(1, Relaxed);
            return;
        }
    };
    stats.connected.fetch_add(1, Relaxed);
    stats.wire.fetch_add(ws.wire_bytes, Relaxed);
    let hello = format!("{{\"bbox\":[{},{},{},{}]}}", bbox[0], bbox[1], bbox[2], bbox[3]);
    if ws.send_text(&hello).await.is_err() {
        stats.closed.fetch_add(1, Relaxed);
        return;
    }
    let mut wire_seen = ws.wire_bytes;
    loop {
        let msg = ws.recv().await;
        stats.wire.fetch_add(ws.wire_bytes - wire_seen, Relaxed);
        wire_seen = ws.wire_bytes;
        let text = match msg {
            Ok(Msg::Text(t)) => t,
            Ok(Msg::Binary(n)) => {
                stats.frames.fetch_add(1, Relaxed);
                stats.bytes.fetch_add(n as u64, Relaxed);
                continue;
            }
            Ok(Msg::Closed) => {
                note_first_close("server closed the socket");
                break;
            }
            Err(e) => {
                note_first_close(&e.to_string());
                break;
            }
        };
        stats.frames.fetch_add(1, Relaxed);
        stats.bytes.fetch_add(text.len() as u64, Relaxed);
        if text.starts_with("{\"stale\"") {
            stats.stale.fetch_add(1, Relaxed);
            continue;
        }
        if let Some(t) = frame_t(&text) {
            let age_ms = ((now_s() - t) * 1000.0).max(0.0) as u64;
            let b = ((age_ms / BUCKET_MS) as usize).min(BUCKETS - 1);
            stats.hist[b].fetch_add(1, Relaxed);
        }
    }
    stats.connected.fetch_sub(1, Relaxed);
    stats.closed.fetch_add(1, Relaxed);
}

// ---- server process sampling ----------------------------------------------

#[derive(Clone, Copy, Default)]
struct ProcSample {
    cpu_ticks: u64,
    rss_kb: u64,
}

fn sample_pids(pids: &[u32]) -> ProcSample {
    let mut s = ProcSample::default();
    for pid in pids {
        if let Ok(stat) = std::fs::read_to_string(format!("/proc/{pid}/stat")) {
            // fields after the ")" of comm: state is field 3; utime 14, stime 15
            if let Some(rest) = stat.rsplit_once(')').map(|x| x.1) {
                let f: Vec<&str> = rest.split_whitespace().collect();
                let ut: u64 = f.get(11).and_then(|v| v.parse().ok()).unwrap_or(0);
                let st: u64 = f.get(12).and_then(|v| v.parse().ok()).unwrap_or(0);
                s.cpu_ticks += ut + st;
            }
        }
        if let Ok(status) = std::fs::read_to_string(format!("/proc/{pid}/status")) {
            for line in status.lines() {
                if let Some(v) = line.strip_prefix("VmRSS:") {
                    s.rss_kb += v.trim().trim_end_matches(" kB").trim().parse::<u64>().unwrap_or(0);
                }
            }
        }
    }
    s
}

/// Kernel TCP buffer memory, all sockets, in KiB.
fn tcp_mem_kb() -> u64 {
    std::fs::read_to_string("/proc/net/sockstat")
        .ok()
        .and_then(|s| {
            s.lines().find(|l| l.starts_with("TCP:")).and_then(|l| {
                let f: Vec<&str> = l.split_whitespace().collect();
                f.iter().position(|x| *x == "mem").and_then(|i| f.get(i + 1)?.parse::<u64>().ok())
            })
        })
        .map(|pages| pages * 4)
        .unwrap_or(0)
}

const TICKS_PER_S: f64 = 100.0; // Linux USER_HZ

fn percentile(hist: &[u64], p: f64) -> Option<f64> {
    let total: u64 = hist.iter().sum();
    if total == 0 {
        return None;
    }
    let want = (total as f64 * p).ceil() as u64;
    let mut acc = 0;
    for (i, n) in hist.iter().enumerate() {
        acc += n;
        if acc >= want {
            return Some(((i as u64 + 1) * BUCKET_MS) as f64);
        }
    }
    None
}

pub struct Plan {
    pub url: String,
    pub count: usize,
    pub ramp_per_s: f64,
    pub mix: Mix,
    pub warmup_s: u64,
    pub duration_s: u64,
    pub pids: Vec<u32>,
    pub seed: u64,
    pub deflate: bool,
    pub label: String,
    pub out: Option<std::path::PathBuf>,
}

pub async fn run(plan: Plan) -> anyhow::Result<()> {
    let stats = Arc::new(Stats::new());
    let mut rng = ChaCha8Rng::seed_from_u64(plan.seed);

    // ramp
    let spawner = {
        let stats = stats.clone();
        let url = plan.url.clone();
        let count = plan.count;
        let deflate = plan.deflate;
        let gap = Duration::from_secs_f64(1.0 / plan.ramp_per_s.max(1.0));
        let boxes: Vec<[f64; 4]> = (0..count).map(|_| viewport(plan.mix, &mut rng)).collect();
        tokio::spawn(async move {
            let mut tick = tokio::time::interval(gap);
            for bbox in boxes {
                tick.tick().await;
                tokio::spawn(viewer(url.clone(), bbox, deflate, stats.clone()));
            }
        })
    };

    let started = Instant::now();
    let mut last = (Instant::now(), stats.frames.load(Relaxed), stats.wire.load(Relaxed), sample_pids(&plan.pids));
    let mut every = tokio::time::interval(Duration::from_secs(1));
    every.tick().await;

    // report each second until the ramp is done and the warm-up has passed
    let print_line = |label: &str, last: &mut (Instant, u64, u64, ProcSample)| {
        let now = Instant::now();
        let dt = now.duration_since(last.0).as_secs_f64();
        let (f, b, p) = (stats.frames.load(Relaxed), stats.wire.load(Relaxed), sample_pids(&plan.pids));
        let cpu = (p.cpu_ticks.saturating_sub(last.3.cpu_ticks)) as f64 / TICKS_PER_S / dt;
        eprintln!(
            "{label:>7} {:>6.0}s  viewers {:>6} (failed {}, closed {})  frames/s {:>8.0}  egress {:>7.2} MB/s  server cpu {:>5.2} cores  rss {:>6.0} MB  tcp mem {:>6.0} MB",
            started.elapsed().as_secs_f64(),
            stats.connected.load(Relaxed),
            stats.failed.load(Relaxed),
            stats.closed.load(Relaxed),
            (f - last.1) as f64 / dt,
            (b - last.2) as f64 / dt / 1e6,
            cpu,
            p.rss_kb as f64 / 1024.0,
            tcp_mem_kb() as f64 / 1024.0,
        );
        *last = (now, f, b, p);
    };

    loop {
        every.tick().await;
        print_line("ramp", &mut last);
        let settled = stats.connected.load(Relaxed) + stats.failed.load(Relaxed) + stats.closed.load(Relaxed)
            >= plan.count as u64;
        if spawner.is_finished() && settled {
            break;
        }
    }
    for _ in 0..plan.warmup_s {
        every.tick().await;
        print_line("warmup", &mut last);
    }

    // measurement window
    let w0 = (
        Instant::now(),
        stats.frames.load(Relaxed),
        stats.bytes.load(Relaxed),
        sample_pids(&plan.pids),
        stats.hist_snapshot(),
        stats.stale.load(Relaxed),
        stats.wire.load(Relaxed),
    );
    let mut rss_max = w0.3.rss_kb;
    let mut tcp_max = tcp_mem_kb();
    let mut viewers_min = stats.connected.load(Relaxed);
    for _ in 0..plan.duration_s {
        every.tick().await;
        print_line("measure", &mut last);
        rss_max = rss_max.max(last.3.rss_kb);
        tcp_max = tcp_max.max(tcp_mem_kb());
        viewers_min = viewers_min.min(stats.connected.load(Relaxed));
    }
    let dt = w0.0.elapsed().as_secs_f64();
    let p1 = sample_pids(&plan.pids);
    let hist: Vec<u64> = stats.hist_snapshot().iter().zip(&w0.4).map(|(a, b)| a - b).collect();
    let frames = stats.frames.load(Relaxed) - w0.1;
    let bytes = stats.bytes.load(Relaxed) - w0.2;
    let wire = stats.wire.load(Relaxed) - w0.6;
    let viewers_end = stats.connected.load(Relaxed);
    let cpu = (p1.cpu_ticks - w0.3.cpu_ticks) as f64 / TICKS_PER_S / dt;
    let egress = wire as f64 / dt;
    let per_viewer = if viewers_end > 0 { egress / viewers_end as f64 } else { 0.0 };
    let decoded = bytes as f64 / dt;
    let pct = |p| percentile(&hist, p);

    let report = serde_json::json!({
        "label": plan.label,
        "url": plan.url,
        "viewers_target": plan.count,
        "viewers_end": viewers_end,
        "viewers_min": viewers_min,
        "failed": stats.failed.load(Relaxed),
        "closed": stats.closed.load(Relaxed),
        "mix": { "regional": plan.mix.regional, "country": plan.mix.country, "world": plan.mix.world },
        "window_s": dt,
        "frames_per_s": frames as f64 / dt,
        "deflate_offered": plan.deflate,
        "egress_mb_s": egress / 1e6,
        "egress_kb_s_per_viewer": per_viewer / 1e3,
        "decoded_mb_s": decoded / 1e6,
        "stale_frames": stats.stale.load(Relaxed) - w0.5,
        "latency_ms": { "p50": pct(0.50), "p90": pct(0.90), "p99": pct(0.99), "max_bucket": pct(1.0) },
        "server": { "pids": plan.pids, "cpu_cores": cpu, "rss_mb_max": rss_max as f64 / 1024.0,
                    "tcp_mem_mb_max": tcp_max as f64 / 1024.0 },
    });
    println!("{}", serde_json::to_string_pretty(&report)?);
    if let Some(path) = plan.out {
        std::fs::write(path, serde_json::to_string_pretty(&report)? + "\n")?;
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn reads_t_from_frames() {
        assert_eq!(frame_t("{\"t\":1758700000.25,\"total\":3}"), Some(1758700000.25));
        assert_eq!(frame_t("{\"t\":12.5}"), Some(12.5));
        assert_eq!(frame_t("{\"stale\":true}"), None);
    }

    #[test]
    fn percentiles_from_buckets() {
        let mut h = vec![0u64; BUCKETS];
        h[0] = 50;
        h[9] = 49;
        h[99] = 1;
        assert_eq!(percentile(&h, 0.5), Some(10.0));
        assert_eq!(percentile(&h, 0.99), Some(100.0));
        assert_eq!(percentile(&h, 1.0), Some(1000.0));
    }

    #[test]
    fn mix_parses() {
        let m: Mix = "regional=70,country=25,world=5".parse().unwrap();
        assert_eq!((m.regional, m.country, m.world), (70, 25, 5));
        assert!("planet=1".parse::<Mix>().is_err());
    }
}
