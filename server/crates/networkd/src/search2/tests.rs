//! The index built from a small snapshot, and /v2/search over it.

use std::path::{Path, PathBuf};
use std::sync::Arc;

use axum::body::Body;
use serde_json::Value;
use tower::Service;

use super::build::{build, Options};
use crate::state::App;

fn snapshot(path: &Path, created_at: &str) {
    let c = rusqlite::Connection::open(path).unwrap();
    c.execute_batch(
        "CREATE TABLE snapshot_meta (key TEXT PRIMARY KEY, value TEXT);
         CREATE TABLE ref_airlines (icao TEXT PRIMARY KEY, iata TEXT, name TEXT);
         CREATE TABLE ref_airports (ident TEXT PRIMARY KEY, name TEXT, kind TEXT, lat REAL, lon REAL, iso_country TEXT,
             municipality TEXT, iata TEXT, tz TEXT);
         CREATE TABLE ref_types (designator TEXT PRIMARY KEY, name TEXT);
         CREATE TABLE ref_airframes (hex TEXT PRIMARY KEY, registration TEXT, type_code TEXT, operator_name TEXT,
             operator_icao TEXT);
         CREATE TABLE ref_schedule (callsign TEXT, org TEXT, dst TEXT, airline_icao TEXT, dep_min INTEGER, arr_min INTEGER,
             type_code TEXT, flight TEXT, source TEXT, n_flights INTEGER);",
    )
    .unwrap();
    c.execute("INSERT INTO snapshot_meta VALUES ('created_at', ?1)", [created_at]).unwrap();
    for (icao, iata, name) in [
        ("SIA", Some("SQ"), "Singapore Airlines"),
        ("TGW", Some("TR"), "Scoot"),
        ("BAW", Some("BA"), "British Airways"),
        ("QFA", Some("QF"), "Qantas"),
        ("AAL", Some("AA"), "American Airlines"),
        ("UAE", Some("EK"), "Emirates"),
        ("IGO", Some("6E"), "IndiGo"),
        ("SQC", None, "Singapore Airlines Cargo"),
    ] {
        c.execute("INSERT INTO ref_airlines VALUES (?1, ?2, ?3)", (icao, iata, name)).unwrap();
    }
    for (ident, name, kind, lat, lon, country, city, iata, tz) in [
        ("WSSS", "Singapore Changi Airport", "large_airport", 1.35, 103.99, "SG", "Singapore", Some("SIN"), "Asia/Singapore"),
        ("WSAC", "Changi Air Base (East)", "medium_airport", 1.34, 104.0, "SG", "Singapore", None, "Asia/Singapore"),
        ("EGLL", "London Heathrow Airport", "large_airport", 51.47, -0.45, "GB", "London", Some("LHR"), "Europe/London"),
        ("EGKK", "London Gatwick Airport", "large_airport", 51.15, -0.18, "GB", "London", Some("LGW"), "Europe/London"),
        ("KJFK", "John F Kennedy International Airport", "large_airport", 40.64, -73.78, "US", "New York", Some("JFK"), "America/New_York"),
        ("KLGA", "LaGuardia Airport", "large_airport", 40.78, -73.87, "US", "New York", Some("LGA"), "America/New_York"),
        ("KEWR", "Newark Liberty International Airport", "large_airport", 40.69, -74.17, "US", "Newark", Some("EWR"), "America/New_York"),
        ("RJTT", "Tokyo Haneda International Airport", "large_airport", 35.55, 139.78, "JP", "Tokyo", Some("HND"), "Asia/Tokyo"),
        ("RJAA", "Narita International Airport", "large_airport", 35.76, 140.39, "JP", "Narita", Some("NRT"), "Asia/Tokyo"),
        ("KSNA", "John Wayne Airport-Orange County Airport", "large_airport", 33.68, -117.87, "US", "Santa Ana", Some("SNA"), "America/Los_Angeles"),
        ("YSSY", "Sydney Kingsford Smith International Airport", "large_airport", -33.95, 151.18, "AU", "Sydney", Some("SYD"), "Australia/Sydney"),
        ("EDDM", "Munich Airport", "large_airport", 48.35, 11.79, "DE", "Munich", Some("MUC"), "Europe/Berlin"),
        ("OMDB", "Dubai International Airport", "large_airport", 25.25, 55.36, "AE", "Dubai", Some("DXB"), "Asia/Dubai"),
    ] {
        c.execute("INSERT INTO ref_airports VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9)", (ident, name, kind, lat, lon, country, city, iata, tz))
            .unwrap();
    }
    for (d, n) in [("A388", "Airbus A380-800"), ("A359", "Airbus A350-900"), ("B77W", "Boeing 777-300ER"), ("B789", "Boeing 787-9")] {
        c.execute("INSERT INTO ref_types VALUES (?1, ?2)", (d, n)).unwrap();
    }
    for (hex, reg, t, op_name, op) in [
        ("76cda1", "9V-SMA", "A359", None, Some("SIA")),
        ("76cd01", "9V-SKA", "A388", None, Some("SIA")),
        ("76cd11", "9V-SWA", "B77W", None, Some("SIA")),
        ("40688b", "G-XLEA", "A388", Some("British Airways"), Some("BAW")),
        ("7c4920", "VH-OQA", "A388", None, Some("QFA")),
        ("896180", "A6-EDA", "A388", None, Some("UAE")),
        ("a00001", "N12005", "B789", Some("United Airlines"), None),
        ("3c4b21", "D-UBAI", "C172", None, None),
    ] {
        c.execute("INSERT INTO ref_airframes VALUES (?1, ?2, ?3, ?4, ?5)", (hex, reg, t, op_name, op)).unwrap();
    }
    for (cs, org, dst, al, dep, arr, t, flight, source, n) in [
        ("SIA322", "SIN", "LHR", "SIA", Some(23 * 60 + 35), Some(6 * 60 + 25), Some("A388"), None, "both", 26),
        ("SIA317", "LHR", "SIN", "SIA", Some(11 * 60 + 5), Some(7 * 60 + 25), Some("A388"), None, "observed", 40),
        ("BAW16", "SYD", "SIN", "BAW", Some(16 * 60), Some(22 * 60), Some("B77W"), None, "observed", 31),
        ("BAW16", "SIN", "LHR", "BAW", Some(23 * 60 + 20), Some(5 * 60 + 40), Some("B77W"), None, "observed", 31),
        ("BAW1611", "LHR", "JFK", "BAW", None, None, None, None, "published", 5),
        ("AAL100", "JFK", "LHR", "AAL", Some(18 * 60), Some(6 * 60 + 5), Some("B77W"), Some("AA100"), "both", 53),
        ("BAW1", "LGA", "LHR", "BAW", None, None, None, None, "observed", 2),
        ("UAE1", "DXB", "LHR", "UAE", None, None, None, None, "observed", 30),
        ("UAE110", "DXB", "SIN", "UAE", None, None, None, None, "observed", 55),
        ("TGW12", "SIN", "HND", "TGW", None, None, None, None, "observed", 40),
        ("TGW808", "SIN", "NRT", "TGW", None, None, None, None, "observed", 30),
        ("QFA1", "SYD", "SIN", "QFA", None, None, None, None, "observed", 20),
        ("QFA1", "SIN", "LHR", "QFA", None, None, None, None, "observed", 20),
        ("IGO1", "SIN", "DXB", "IGO", None, None, None, None, "observed", 3),
        ("SIA25", "MUC", "SIN", "SIA", None, None, None, None, "observed", 9),
        ("TGW1", "SNA", "SIN", "TGW", None, None, None, None, "observed", 1),
    ] {
        c.execute(
            "INSERT INTO ref_schedule VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10)",
            (cs, org, dst, al, dep, arr, t, flight, source, n),
        )
        .unwrap();
    }
}

fn legs(path: &Path) {
    let c = rusqlite::Connection::open(path).unwrap();
    c.execute_batch(
        "CREATE TABLE legs (hex TEXT, reg TEXT, type TEXT, callsign TEXT, org TEXT, dst TEXT, date TEXT, dep_ts INTEGER, arr_ts INTEGER);
         CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
         INSERT INTO meta VALUES ('window_days', '400');
         INSERT INTO legs VALUES (NULL, NULL, NULL, 'SIA322', 'SIN', 'LHR', '2026-09-24', 0, 0);
         INSERT INTO legs VALUES (NULL, NULL, NULL, 'SIA322', 'SIN', 'LHR', '2026-09-25', 0, 0);
         INSERT INTO legs VALUES (NULL, NULL, NULL, 'SIA908', 'SIN', 'MUC', '2026-09-20', 0, 0);
         INSERT INTO legs VALUES (NULL, NULL, NULL, 'SIA908', 'SIN', 'MUC', '2026-09-21', 0, 0);
         INSERT INTO legs VALUES (NULL, NULL, NULL, 'ZZZ1', 'SIN', 'MUC', '2026-09-21', 0, 0);",
    )
    .unwrap();
}

fn dir(name: &str) -> PathBuf {
    let d = std::env::temp_dir().join(format!("networkd-search2-{}-{name}", std::process::id()));
    let _ = std::fs::remove_dir_all(&d);
    std::fs::create_dir_all(&d).unwrap();
    d
}

fn built(name: &str) -> PathBuf {
    let d = dir(name);
    snapshot(&d.join("refdata.sqlite"), "2026-09-26T02:40:00+00:00");
    legs(&d.join("legs.db"));
    let o = Options { refdata: d.join("refdata.sqlite"), legs: Some(d.join("legs.db")), out: d.join("search"), keep: 2, force: false };
    build(&o).unwrap();
    d
}

fn app(d: &Path) -> Arc<App> {
    let mut s = crate::settings::Settings::from_env();
    s.search_index_path = d.join("search").to_string_lossy().into();
    s.refdata_path = d.join("none.sqlite").to_string_lossy().into();
    s.legs_path = d.join("none.db").to_string_lossy().into();
    s.routes_path = d.join("routes.json.gz").to_string_lossy().into();
    s.gaps_path = d.join("gaps.json.gz").to_string_lossy().into();
    s.boards_path = d.join("boards.db").to_string_lossy().into();
    s.site_url = "https://flightportrait.com/network".into();
    s.database_url = String::new();
    s.fallback = String::new();
    s.squawks = false;
    s.stations = false;
    s.estimates = false;
    s.nat = false;
    App::new(s)
}

async fn get(r: &mut axum::Router, path: &str) -> (u16, axum::http::HeaderMap, Value) {
    let req = axum::http::Request::builder().uri(path).body(Body::empty()).unwrap();
    let resp = r.call(req).await.unwrap();
    let (status, headers) = (resp.status().as_u16(), resp.headers().clone());
    let bytes = axum::body::to_bytes(resp.into_body(), 1 << 20).await.unwrap();
    (status, headers, serde_json::from_slice(&bytes).unwrap_or(Value::Null))
}

async fn ask(r: &mut axum::Router, q: &str) -> Value {
    let q = crate::search::norm(q);
    let (status, _, v) = get(r, &format!("/v2/search?q={}", crate::search::quote(&q))).await;
    assert_eq!(status, 200, "{q}: {v}");
    v
}

async fn ids(r: &mut axum::Router, q: &str) -> Vec<String> {
    ask(r, q).await["results"].as_array().unwrap().iter().map(|h| format!("{}/{}", h["kind"].as_str().unwrap(), h["id"].as_str().unwrap())).collect()
}

#[test]
fn builds_a_generation_points_to_it_and_prunes() {
    let d = built("build");
    let out = d.join("search");
    assert_eq!(std::fs::read_to_string(out.join("CURRENT")).unwrap().trim(), "20260926T024000Z");
    let g = out.join("20260926T024000Z");
    assert!(g.join("meta.json").exists() && g.join("lexicon.json").exists() && g.join("build.json").exists());
    let report: Value = serde_json::from_slice(&std::fs::read(g.join("build.json")).unwrap()).unwrap();
    assert_eq!(report["docs"]["flight"], 15, "{report}");
    assert_eq!(report["docs"]["aircraft"], 8);
    // the same snapshot again: nothing rebuilt
    let o = Options { refdata: d.join("refdata.sqlite"), legs: None, out: out.clone(), keep: 2, force: false };
    assert!(build(&o).unwrap().contains("already built"));
    // two newer snapshots: the oldest generation goes
    for t in ["2026-09-27T02:40:00+00:00", "2026-09-28T02:40:00+00:00"] {
        let p = d.join(format!("refdata-{t}.sqlite"));
        snapshot(&p, t);
        build(&Options { refdata: p, legs: None, out: out.clone(), keep: 2, force: false }).unwrap();
    }
    let mut gens: Vec<String> =
        std::fs::read_dir(&out).unwrap().map(|e| e.unwrap().file_name().to_string_lossy().to_string()).filter(|n| n.ends_with('Z')).collect();
    gens.sort();
    assert_eq!(gens, ["20260927T024000Z", "20260928T024000Z"]);
    assert_eq!(std::fs::read_to_string(out.join("CURRENT")).unwrap().trim(), "20260928T024000Z");
}

#[tokio::test]
async fn typed_results() {
    let d = built("typed");
    let mut r = crate::router(app(&d));
    let v = ask(&mut r, "SQ 322").await;
    assert_eq!(v["q"], "SQ 322");
    assert_eq!(v["intent"]["kind"], "flight");
    assert_eq!((v["index"].as_str(), v["as_of"].as_str()), (Some("20260926T024000Z"), Some("2026-09-26T02:40:00Z")));
    let f = &v["results"][0];
    assert_eq!((f["kind"].as_str(), f["id"].as_str(), f["callsign"].as_str()), (Some("flight"), Some("SIA322"), Some("SIA322")));
    assert_eq!(f["flight"], "SQ322");
    assert_eq!(f["airline"], serde_json::json!({"icao": "SIA", "iata": "SQ", "name": "Singapore Airlines"}));
    assert_eq!(f["route"], serde_json::json!(["SIN", "LHR"]));
    assert_eq!(f["cities"], serde_json::json!(["Singapore", "London"]));
    let leg = &f["legs"][0];
    assert_eq!((leg["dep"].as_str(), leg["arr"].as_str(), leg["block_min"].as_i64()), (Some("23:35"), Some("06:25"), Some(830)));
    assert_eq!((leg["dep_tz"].as_str(), leg["arr_tz"].as_str()), (Some("Asia/Singapore"), Some("Europe/London")));
    assert_eq!((leg["type"].as_str(), leg["times"].as_str()), (Some("A388"), Some("both")));
    assert_eq!((f["flights"].as_i64(), f["window_days"].as_i64(), f["last_seen"].as_str()), (Some(26), Some(400), Some("2026-09-25")));
    assert_eq!(f["url"], "https://flightportrait.com/network/flight.html?callsign=SIA322");
    assert!(f["score"].as_f64().unwrap() >= 1000.0);
    // a board's number finds its operating callsign
    assert_eq!(ids(&mut r, "AA100").await[0], "flight/AAL100");

    let v = ask(&mut r, "SIN").await;
    let a = &v["results"][0];
    assert_eq!(v["intent"]["kind"], "code");
    assert_eq!(
        (a["kind"].as_str(), a["id"].as_str(), a["iata"].as_str(), a["icao"].as_str()),
        (Some("airport"), Some("SIN"), Some("SIN"), Some("WSSS"))
    );
    assert_eq!((a["name"].as_str(), a["city"].as_str(), a["country"].as_str()), (Some("Singapore Changi Airport"), Some("Singapore"), Some("SG")));
    assert_eq!(a["url"], "https://flightportrait.com/network/?airport=SIN");
    assert!(a.get("_m").is_none() && a.get("_ll").is_none());

    let v = ask(&mut r, "SIA").await;
    let al = v["results"].as_array().unwrap().iter().find(|h| h["kind"] == "airline").unwrap().clone();
    assert_eq!((al["id"].as_str(), al["icao"].as_str(), al["iata"].as_str(), al["name"].as_str()), (Some("SIA"), Some("SIA"), Some("SQ"), Some("Singapore Airlines")));

    let v = ask(&mut r, "9V-SMA").await;
    let p = &v["results"][0];
    assert_eq!(
        (p["kind"].as_str(), p["id"].as_str(), p["hex"].as_str(), p["reg"].as_str(), p["type"].as_str()),
        (Some("aircraft"), Some("76cda1"), Some("76cda1"), Some("9V-SMA"), Some("A359"))
    );
    assert_eq!((p["type_name"].as_str(), p["operator"].as_str()), (Some("Airbus A350-900"), Some("Singapore Airlines")));
    assert!(p["score"].as_f64().unwrap() >= 1000.0, "{p}");

    let v = ask(&mut r, "A380").await;
    let t = &v["results"][0];
    assert_eq!(v["intent"]["kind"], "type");
    assert_eq!(
        (t["kind"].as_str(), t["id"].as_str(), t["designator"].as_str(), t["name"].as_str(), t["manufacturer"].as_str()),
        (Some("type"), Some("A388"), Some("A388"), Some("Airbus A380-800"), Some("Airbus"))
    );
    assert_eq!((t["airframes"].as_i64(), t["url"].is_null()), (Some(4), true));
}

#[tokio::test]
async fn readings_find_what_was_meant() {
    let d = built("readings");
    let mut r = crate::router(app(&d));
    for q in ["SQ322", "sq 322", "SIA 322"] {
        assert_eq!(ids(&mut r, q).await[0], "flight/SIA322", "{q}");
    }
    // the exact number before busier ones that start with it
    let got = ids(&mut r, "BA16").await;
    assert_eq!(got[0], "flight/BAW16", "{got:?}");
    assert!(got.contains(&"flight/BAW1611".to_string()));
    assert_eq!(ids(&mut r, "EK1").await[0], "flight/UAE1");
    // routes in words, a city standing for all its airports
    for q in ["SIN LHR", "Singapore to London", "Changi to Heathrow", "from SIN to LHR", "SIN-LHR"] {
        assert_eq!(ids(&mut r, q).await[0], "flight/SIA322", "{q}");
    }
    let v = ask(&mut r, "New York to London").await;
    assert_eq!(v["intent"]["kind"], "route");
    assert_eq!(v["intent"]["from"]["airports"], serde_json::json!(["JFK", "EWR", "LGA"]));
    assert_eq!(v["results"][0]["id"], "AAL100");
    assert!(v["results"].as_array().unwrap().iter().any(|h| h["id"] == "BAW1"));
    // an airline and a place; an airline and a family
    let v = ask(&mut r, "Scoot Tokyo").await;
    assert_eq!(v["intent"]["kind"], "airline_place");
    let got: Vec<&str> = v["results"].as_array().unwrap().iter().take(2).map(|h| h["id"].as_str().unwrap()).collect();
    assert_eq!(got, ["TGW12", "TGW808"]);
    let v = ask(&mut r, "Singapore Airlines London").await;
    assert_eq!((v["intent"]["kind"].as_str(), v["results"][0]["id"].as_str()), (Some("airline_place"), Some("SIA317")));
    let v = ask(&mut r, "SQ A380").await;
    assert_eq!((v["intent"]["kind"].as_str(), v["results"][0]["id"].as_str()), (Some("fleet"), Some("76cd01")));
    assert_eq!(ids(&mut r, "BA A380").await[0], "aircraft/40688b");
    assert_eq!(ids(&mut r, "SQ 777").await[0], "aircraft/76cd11");
    // a city: every airport, the main one first, its busiest flights after
    let got = ids(&mut r, "Tokyo").await;
    assert_eq!(got[..2], ["airport/HND", "airport/NRT"]);
    let got = ids(&mut r, "New York").await;
    assert_eq!(got[..3], ["airport/JFK", "airport/EWR", "airport/LGA"]);
    let got = ids(&mut r, "London").await;
    assert_eq!(got[..3], ["airport/LHR", "airport/LGW", "flight/BAW16"], "{got:?}");
    assert_eq!(ids(&mut r, "München").await[0], "airport/MUC");
    // registrations however typed, hexes, codes with a digit, types
    for q in ["9V SMA", "9VSMA", "9v-sma", "76CDA1"] {
        assert_eq!(ids(&mut r, q).await[0], "aircraft/76cda1", "{q}");
    }
    assert_eq!(ids(&mut r, "GXLEA").await[0], "aircraft/40688b");
    assert_eq!(ids(&mut r, "6E").await[0], "airline/IGO");
    assert_eq!(ids(&mut r, "Boeing 777").await[0], "type/B77W");
    // a flight only the log knows
    assert_eq!(ids(&mut r, "SIA908").await[0], "flight/SIA908");
}

#[tokio::test]
async fn ranking_invariants() {
    let d = built("rank");
    let mut r = crate::router(app(&d));
    // a typo of an airline beats near misses of busier airports
    let got = ids(&mut r, "Qantsa").await;
    assert_eq!(got[0], "airline/QFA", "{got:?}");
    // matched whole: no near misses beside it
    let v = ask(&mut r, "Scoot").await;
    assert_eq!(v["results"][0]["id"], "TGW");
    assert!(v["results"].as_array().unwrap().iter().all(|h| h["kind"] != "airport"));
    // the civil airport over the air base
    assert_eq!(ids(&mut r, "Changi").await[..2], ["airport/SIN", "airport/WSAC"]);
    // a word that is a place is not a tail
    let got = ids(&mut r, "Dubai").await;
    assert_eq!(got[0], "airport/DXB", "{got:?}");
    assert!(got.iter().position(|x| x == "aircraft/3c4b21").is_none_or(|p| p > 0));
    // every score in order, exact codes first
    let v = ask(&mut r, "SIN").await;
    let scores: Vec<f64> = v["results"].as_array().unwrap().iter().map(|h| h["score"].as_f64().unwrap()).collect();
    assert!(scores.windows(2).all(|w| w[0] >= w[1]) || scores.len() < 2, "{scores:?}");
    assert!(scores[0] >= 1000.0);
    // kinds and limit
    let (_, _, v) = get(&mut r, "/v2/search?q=SINGAPORE&kinds=airline").await;
    assert!(v["results"].as_array().unwrap().iter().all(|h| h["kind"] == "airline"));
    let (_, _, v) = get(&mut r, "/v2/search?q=SINGAPORE&limit=1").await;
    assert_eq!(v["results"].as_array().unwrap().len(), 1);
}

#[tokio::test]
async fn canonical_spellings_redirect() {
    let d = built("redirect");
    let mut r = crate::router(app(&d));
    for (path, want) in [
        ("/v2/search?q=singapore", "/v2/search?q=SINGAPORE"),
        ("/v2/search?q=%20sq%20%20322", "/v2/search?q=SQ%20322"),
        ("/v2/search?x=1&q=sin&kinds=airport,flight", "/v2/search?x=1&q=SIN&kinds=flight,airport"),
        ("/v2/search?q=SIN&near=1.3521,103.8198", "/v2/search?q=SIN&near=1.25,103.75"),
        ("/v2/search?q=SIN&limit=05", "/v2/search?q=SIN&limit=5"),
        ("/v2/search?q=a&q=SIN", "/v2/search?q=SIN"),
    ] {
        let (status, h, _) = get(&mut r, path).await;
        assert_eq!(status, 301, "{path}");
        assert_eq!(h["location"], want, "{path}");
        assert_eq!(h["cache-control"], crate::search::CACHE);
    }
    let (status, h, _) = get(&mut r, "/v2/search?q=SIN&near=1.25,103.75&kinds=airport").await;
    assert_eq!((status, h["cache-control"].to_str().unwrap()), (200, crate::search::CACHE));
    for path in [
        "/v2/search",
        "/v2/search?q=",
        "/v2/search?q=s",
        "/v2/search?q=SIN&kinds=boat",
        "/v2/search?q=SIN&kinds=",
        "/v2/search?q=SIN&limit=0",
        "/v2/search?q=SIN&limit=51",
        "/v2/search?q=SIN&near=91,0",
        "/v2/search?q=SIN&near=abc",
    ] {
        let (status, h, v) = get(&mut r, path).await;
        assert_eq!(status, 422, "{path}");
        assert_eq!(v["error"], "invalid_request");
        assert_eq!(h["cache-control"], "no-store");
    }
}

#[tokio::test]
async fn serves_a_generation_it_cannot_write() {
    let d = built("readonly");
    let g = d.join("search").join("20260926T024000Z");
    for e in std::fs::read_dir(&g).unwrap() {
        let p = e.unwrap().path();
        if p.file_name().unwrap().to_string_lossy().ends_with(".lock") {
            std::fs::remove_file(&p).unwrap();
        }
    }
    let ro = |p: &Path, on: bool| {
        let mut perm = std::fs::metadata(p).unwrap().permissions();
        perm.set_readonly(on);
        std::fs::set_permissions(p, perm).unwrap();
    };
    ro(&g, true);
    let mut r = crate::router(app(&d));
    assert_eq!(ids(&mut r, "SIN").await[0], "airport/SIN");
    ro(&g, false);
}

#[tokio::test]
async fn no_index_is_503_not_another_answer() {
    let d = dir("none");
    let mut r = crate::router(app(&d));
    let (status, h, v) = get(&mut r, "/v2/search?q=SIN").await;
    assert_eq!(status, 503);
    assert_eq!((v["error"].as_str(), h["retry-after"].to_str().unwrap()), (Some("artifact_unavailable"), "60"));
    // /v1 is another route: untouched
    let (status, _, _) = get(&mut r, "/v1/search?q=SIN").await;
    assert_ne!(status, 503);
}
