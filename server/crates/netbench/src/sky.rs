//! A synthetic sky that talks like readsb: the JSON position port (one
//! line per aircraft per second) and the HTTP files the API polls
//! (aircraft.json, clients.json, receivers.json).
//!
//! Aircraft cluster where traffic is dense in the real world, fly
//! straight with small turns and climbs, and are paced so every aircraft
//! reports once a second, spread evenly over the second.

use std::f64::consts::PI;
use std::fmt::Write as _;
use std::sync::{Arc, Mutex};
use std::time::{Duration, SystemTime, UNIX_EPOCH};

use rand::{Rng, SeedableRng};
use rand_chacha::ChaCha8Rng;
use tokio::io::{AsyncReadExt, AsyncWriteExt};
use tokio::net::TcpListener;
use tokio::sync::broadcast;

/// (lat, lon, share of traffic, spread in degrees)
pub const CLUSTERS: &[(f64, f64, f64, f64)] = &[
    (50.0, 8.0, 0.30, 9.0),     // Europe
    (39.0, -80.0, 0.18, 7.0),   // US east
    (37.0, -116.0, 0.10, 6.0),  // US west
    (38.0, -95.0, 0.08, 7.0),   // US central
    (33.0, 120.0, 0.12, 8.0),   // East Asia
    (6.0, 105.0, 0.06, 7.0),    // South-East Asia
    (22.0, 78.0, 0.04, 6.0),    // India
    (25.0, 50.0, 0.04, 5.0),    // Gulf
    (-20.0, -50.0, 0.03, 8.0),  // South America
    (-30.0, 145.0, 0.02, 7.0),  // Australia
    (0.0, 20.0, 0.02, 14.0),    // Africa
    (52.0, -30.0, 0.01, 10.0),  // North Atlantic
];

const TYPES: &[(&str, &str)] = &[
    ("A320", "A3"), ("B738", "A3"), ("A20N", "A3"), ("A321", "A3"),
    ("B77W", "A5"), ("A359", "A5"), ("B789", "A5"), ("E190", "A3"),
    ("AT76", "A2"), ("C172", "A1"), ("B38M", "A3"), ("A388", "A5"),
];

const AIRLINES: &[&str] = &[
    "DLH", "BAW", "AFR", "KLM", "UAL", "DAL", "AAL", "SIA", "CPA",
    "UAE", "QTR", "RYR", "EZY", "SWA", "ANA", "JAL", "QFA", "THY",
];

pub struct Plane {
    hex: u32,
    flight: String,
    reg: String,
    typ: &'static str,
    cat: &'static str,
    lat: f64,
    lon: f64,
    alt: f64,      // ft
    gs: f64,       // kt
    track: f64,    // deg
    rate: f64,     // ft/min
    squawk: u16,
    messages: u64,
}

pub fn pick_cluster(rng: &mut impl Rng) -> (f64, f64, f64) {
    let total: f64 = CLUSTERS.iter().map(|c| c.2).sum();
    let mut x = rng.gen::<f64>() * total;
    for &(lat, lon, w, spread) in CLUSTERS {
        if x < w {
            return (lat, lon, spread);
        }
        x -= w;
    }
    let c = CLUSTERS[0];
    (c.0, c.1, c.3)
}

fn gauss(rng: &mut impl Rng) -> f64 {
    // Box-Muller; one draw is plenty here
    let u: f64 = rng.gen_range(1e-9..1.0);
    let v: f64 = rng.gen();
    (-2.0 * u.ln()).sqrt() * (2.0 * PI * v).cos()
}

impl Plane {
    fn new(i: usize, rng: &mut impl Rng) -> Plane {
        let (clat, clon, spread) = pick_cluster(rng);
        let (typ, cat) = TYPES[rng.gen_range(0..TYPES.len())];
        let airline = AIRLINES[rng.gen_range(0..AIRLINES.len())];
        let cruise = rng.gen_bool(0.7);
        Plane {
            hex: 0x400000 + i as u32 * 7 + rng.gen_range(0..7),
            flight: format!("{airline}{}", rng.gen_range(1..9999)),
            reg: format!("N{}", rng.gen_range(100..99999)),
            typ,
            cat,
            lat: (clat + gauss(rng) * spread).clamp(-80.0, 80.0),
            lon: wrap(clon + gauss(rng) * spread * 1.4),
            alt: if cruise { rng.gen_range(30000.0..41000.0) } else { rng.gen_range(1000.0..30000.0) },
            gs: if cruise { rng.gen_range(420.0..520.0) } else { rng.gen_range(180.0..420.0) },
            track: rng.gen_range(0.0..360.0),
            rate: if cruise { 0.0 } else { rng.gen_range(-2000.0..2500.0) },
            squawk: rng.gen_range(0o1000..0o7777),
            messages: 0,
        }
    }

    /// `dt` seconds of flight.
    fn step(&mut self, dt: f64, rng: &mut impl Rng) {
        let d_m = self.gs * 0.514444 * dt;
        let t = self.track.to_radians();
        self.lat += d_m * t.cos() / 111_320.0;
        self.lon = wrap(self.lon + d_m * t.sin() / (111_320.0 * self.lat.to_radians().cos().max(0.05)));
        if self.lat.abs() > 80.0 {
            self.lat = self.lat.signum() * 80.0;
            self.track = (540.0 - self.track) % 360.0;
        }
        if rng.gen_bool(0.02 * dt) {
            self.track = (self.track + rng.gen_range(-15.0..15.0) + 360.0) % 360.0;
        }
        self.alt = (self.alt + self.rate / 60.0 * dt).clamp(0.0, 45000.0);
        if self.alt >= 41000.0 || self.alt <= 1000.0 || rng.gen_bool(0.005 * dt) {
            self.rate = if self.alt > 20000.0 { rng.gen_range(-2000.0..0.0) } else { rng.gen_range(0.0..2500.0) };
            if rng.gen_bool(0.5) {
                self.rate = 0.0;
            }
        }
        self.messages += rng.gen_range(2..12);
    }

    /// A readsb JSON-port line (or an aircraft.json entry), fields in
    /// readsb's order.
    fn write_json(&self, out: &mut String, seen: f64) {
        let _ = write!(
            out,
            "{{\"hex\":\"{:06x}\",\"type\":\"adsb_icao\",\"flight\":\"{:<8}\",\"r\":\"{}\",\"t\":\"{}\",\
             \"alt_baro\":{},\"alt_geom\":{},\"gs\":{:.1},\"track\":{:.2},\"baro_rate\":{},\
             \"squawk\":\"{:04o}\",\"emergency\":\"none\",\"category\":\"{}\",\"nav_qnh\":1013.6,\
             \"lat\":{:.6},\"lon\":{:.6},\"nic\":8,\"rc\":186,\"seen_pos\":{:.1},\"version\":2,\
             \"nic_baro\":1,\"nac_p\":9,\"nac_v\":1,\"sil\":3,\"sil_type\":\"perhour\",\"gva\":2,\
             \"sda\":2,\"alert\":0,\"spi\":0,\"mlat\":[],\"tisb\":[],\"messages\":{},\"seen\":{:.1},\
             \"rssi\":-21.4}}",
            self.hex, self.flight, self.reg, self.typ,
            (self.alt / 25.0).round() as i64 * 25, (self.alt / 25.0).round() as i64 * 25 + 150,
            self.gs, self.track, (self.rate / 64.0).round() as i64 * 64,
            self.squawk, self.cat, self.lat, self.lon, seen, self.messages, seen,
        );
    }
}

fn wrap(lon: f64) -> f64 {
    let mut l = lon;
    while l > 180.0 {
        l -= 360.0;
    }
    while l < -180.0 {
        l += 360.0;
    }
    l
}

pub fn now_s() -> f64 {
    SystemTime::now().duration_since(UNIX_EPOCH).unwrap().as_secs_f64()
}

/// Aircraft objects that exercise the serializer: nulls, ints where
/// floats usually are, "ground", exponents, escapes, non-ASCII, and
/// entries without a position or without a hex.
const ODD_AIRCRAFT: &[&str] = &[
    r#"{"hex":"a00001","flight":"  PAD  ","lat":10,"lon":20,"alt_baro":"ground","gs":0,"track":90,"seen":0,"seen_pos":0}"#,
    r#"{"hex":"a00002","lat":null,"lon":null,"alt_baro":null,"seen":1.25}"#,
    r#"{"hex":"a00003","lat":0.00001,"lon":-0.0001,"gs":1e16,"track":359.999999999,"baro_rate":-0.0,"seen":2}"#,
    r#"{"hex":"a00004","flight":"Q\"uo\\te\u0001","r":"Ü-ÄBC","t":"A20N","lat":45.5,"lon":179.999,"emergency":"general","squawk":"7700","category":"A3"}"#,
    r#"{"hex":"a00005","flight":"NOPOS"}"#,
    r#"{"flight":"NOHEX","lat":1,"lon":1}"#,
    r#"{"hex":"a00006","lat":-33.8688,"lon":151.2093,"alt_baro":37000,"extra":{"nested":[1,2.50,{"x":null}]},"seen":0.05,"seen_pos":0.15}"#,
    r#"{"hex":"A00007","lat":51.47,"lon":-0.4543,"alt_baro":1200.5,"track":null,"seen":59.9}"#,
];

pub struct Sky {
    planes: Vec<Plane>,
    rng: ChaCha8Rng,
}

impl Sky {
    pub fn new(n: usize, seed: u64) -> Sky {
        let mut rng = ChaCha8Rng::seed_from_u64(seed);
        let planes = (0..n).map(|i| Plane::new(i, &mut rng)).collect();
        Sky { planes, rng }
    }

    /// Advance planes `range` by `dt` seconds each and render their lines.
    fn step_lines(&mut self, range: std::ops::Range<usize>, dt: f64) -> String {
        let mut out = String::with_capacity(range.len() * 620);
        for i in range {
            let p = &mut self.planes[i];
            p.step(dt, &mut self.rng);
            let seen = self.rng.gen_range(0.0..0.5);
            p.write_json(&mut out, seen);
            out.push('\n');
        }
        out
    }

    fn aircraft_json(&self, frozen_at: Option<f64>) -> String {
        let mut out = String::with_capacity(self.planes.len() * 620 + 64);
        let _ = write!(out, "{{\"now\":{:.3},\"messages\":1,\"aircraft\":[", frozen_at.unwrap_or_else(now_s));
        for (i, p) in self.planes.iter().enumerate() {
            if i > 0 {
                out.push(',');
            }
            p.write_json(&mut out, 0.3);
        }
        if frozen_at.is_some() {
            // the corners a byte-for-byte comparison should cover
            for odd in ODD_AIRCRAFT {
                out.push(',');
                out.push_str(odd);
            }
        }
        out.push_str("]}");
        out
    }
}

pub async fn run(aircraft: usize, json_port: u16, http_port: u16, seed: u64, bind: &str, frozen: bool, interval_s: f64) -> anyhow::Result<()> {
    let sky = Arc::new(Mutex::new(Sky::new(aircraft, seed)));
    let (tx, _) = broadcast::channel::<Arc<String>>(64);
    // frozen: nothing moves and aircraft.json always says the same thing,
    // so two servers polling it must answer byte for byte alike
    let frozen_at = frozen.then(|| now_s().floor());

    // pacer: every 100 ms, a tenth of the sky moves one second and reports
    let pacer_sky = sky.clone();
    let pacer_tx = tx.clone();
    tokio::spawn(async move {
        if frozen {
            return;
        }
        // every aircraft reports once per interval (readsb's
        // --net-json-port-interval), spread evenly across it
        const SLICES: usize = 10;
        let mut tick = tokio::time::interval(Duration::from_secs_f64(interval_s / SLICES as f64));
        tick.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Delay);
        let mut slice = 0;
        loop {
            tick.tick().await;
            let lines = {
                let mut s = pacer_sky.lock().unwrap();
                let n = s.planes.len();
                let lo = n * slice / SLICES;
                let hi = n * (slice + 1) / SLICES;
                s.step_lines(lo..hi, interval_s)
            };
            let _ = pacer_tx.send(Arc::new(lines));
            slice = (slice + 1) % SLICES;
        }
    });

    let json_listener = TcpListener::bind((bind, json_port)).await?;
    let http_listener = TcpListener::bind((bind, http_port)).await?;
    eprintln!(
        "sky: {aircraft} aircraft{}; JSON port {bind}:{json_port}, HTTP {bind}:{http_port}",
        if frozen { ", frozen" } else { "" }
    );

    let json_tx = tx.clone();
    tokio::spawn(async move {
        loop {
            let Ok((mut sock, peer)) = json_listener.accept().await else { continue };
            let mut rx = json_tx.subscribe();
            eprintln!("sky: JSON port client {peer}");
            tokio::spawn(async move {
                loop {
                    match rx.recv().await {
                        Ok(lines) => {
                            if sock.write_all(lines.as_bytes()).await.is_err() {
                                break;
                            }
                        }
                        Err(broadcast::error::RecvError::Lagged(n)) => {
                            eprintln!("sky: {peer} lagged {n} slices");
                        }
                        Err(_) => break,
                    }
                }
                eprintln!("sky: JSON port client {peer} gone");
            });
        }
    });

    loop {
        let Ok((mut sock, _)) = http_listener.accept().await else { continue };
        let sky = sky.clone();
        tokio::spawn(async move {
            let mut buf = vec![0u8; 4096];
            let Ok(n) = sock.read(&mut buf).await else { return };
            let req = String::from_utf8_lossy(&buf[..n]);
            let path = req.split_whitespace().nth(1).unwrap_or("/").to_string();
            let (status, body) = match path.as_str() {
                "/data/aircraft.json" => ("200 OK", sky.lock().unwrap().aircraft_json(frozen_at)),
                "/data/clients.json" => ("200 OK", "{\"clients\":[]}".to_string()),
                "/data/receivers.json" => ("200 OK", "{\"receivers\":[]}".to_string()),
                _ => ("404 Not Found", "{}".to_string()),
            };
            let head = format!(
                "HTTP/1.1 {status}\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n",
                body.len()
            );
            let _ = sock.write_all(head.as_bytes()).await;
            let _ = sock.write_all(body.as_bytes()).await;
        });
    }
}
