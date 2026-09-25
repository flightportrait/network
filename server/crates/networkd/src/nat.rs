//! The North Atlantic Organised Track System, read from the track
//! message (the Python service's nat.py): fetched every FETCH_S from the
//! FAA's North Atlantic Tracks page and each message kept once, so past
//! days can be scored and replayed. A track reads
//!
//!     C MALOT 54/20 56/30 5630/40 55/50 LOMSI
//!     EAST LVLS NIL
//!     WEST LVLS 340 350 360 370 380 390 400
//!
//! a letter, the entry fix, one point per 10 degrees of longitude (54/20
//! is 54N 20W, 5630/40 is 56 deg 30'N 40W), the exit fix, and the flight
//! levels allowed each way. The parser follows nat.py line for line, and
//! rows are written as its SQLAlchemy JSON column writes them.

use std::time::Duration;

use chrono::{DateTime, Utc};
use indexmap::IndexMap;
use serde_json::{Map, Value};

use crate::pyjson::write_dumps;

pub const NAT_URL: &str = "https://nms.aim.faa.gov/datanat/nat.json";
const FETCH_S: u64 = 1800;
const USER_AGENT: &str = "flightportrait-network (+https://flightportrait.com/network/)";

/// `^(\d{2})(\d{2})?/(\d{2,3})$`: '54/20' -> (54.0, -20.0), '5630/40' ->
/// (56.5, -40.0); None outside the ocean's box.
fn parse_point(token: &str) -> Option<(f64, f64)> {
    let (lat, lon) = token.split_once('/')?;
    let digits = |s: &str| !s.is_empty() && s.bytes().all(|b| b.is_ascii_digit());
    if !digits(lat) || !(lat.len() == 2 || lat.len() == 4) || !digits(lon) || !(2..=3).contains(&lon.len()) {
        return None;
    }
    let deg: i64 = lat[..2].parse().ok()?;
    let min = if lat.len() == 4 { lat[2..].parse::<i64>().ok()? as f64 / 60.0 } else { 0.0 };
    let lat = deg as f64 + min;
    let lon = -(lon.parse::<i64>().ok()? as f64);
    ((40.0..=80.0).contains(&lat) && (-80.0..=0.0).contains(&lon)).then_some((lat, lon))
}

fn levels(text: &str) -> Vec<i64> {
    let t = text.trim();
    if t.is_empty() || t.starts_with("NIL") {
        return vec![];
    }
    t.split_whitespace().filter(|w| w.bytes().all(|b| b.is_ascii_digit())).filter_map(|w| w.parse().ok()).collect()
}

/// `TMI IS (\d+)` anywhere in a line.
fn tmi_of(line: &str) -> Option<i64> {
    let i = line.find("TMI IS ")?;
    let rest = &line[i + 7..];
    let n: String = rest.chars().take_while(|c| c.is_ascii_digit()).collect();
    n.parse().ok()
}

/// `^(EAST|WEST)\s+LVLS\s+(.*)$`
fn levels_line(line: &str) -> Option<(bool, &str)> {
    let (dir, rest) = if let Some(r) = line.strip_prefix("EAST") {
        (true, r)
    } else {
        (false, line.strip_prefix("WEST")?)
    };
    let r = rest.strip_prefix(char::is_whitespace)?.trim_start();
    let r = r.strip_prefix("LVLS")?;
    let r = r.strip_prefix(char::is_whitespace)?.trim_start();
    Some((dir, r))
}

/// `^([A-Z])\s+(.+)$`
fn track_line(line: &str) -> Option<(char, &str)> {
    let mut chars = line.chars();
    let letter = chars.next().filter(|c| c.is_ascii_uppercase())?;
    let rest = chars.as_str();
    let rest = rest.strip_prefix(char::is_whitespace)?.trim_start();
    (!rest.is_empty()).then_some((letter, rest))
}

/// One message's issuer, validity and (part number, text) parts.
type Parts = (Value, Value, Value, Vec<(i64, String)>);

fn text_of(v: Option<&Value>) -> Value {
    v.cloned().unwrap_or(Value::Null)
}

/// The FAA JSON (a list of message parts) -> the messages, as nat.py's
/// parse_parts returns them.
pub fn parse_parts(parts: &Value) -> Vec<Map<String, Value>> {
    // parts of one message share issuer and validity
    let mut by_msg: IndexMap<String, Parts> = IndexMap::new();
    for p in parts.as_array().into_iter().flatten() {
        let (issuer, from, to) = (text_of(p.get("icao_id")), text_of(p.get("start_datetime")), text_of(p.get("end_datetime")));
        let key = format!("{issuer}\u{0}{from}\u{0}{to}");
        let entry = by_msg.entry(key).or_insert_with(|| (issuer, from, to, vec![]));
        let no = p.get("part_no").and_then(|n| n.as_i64()).unwrap_or(0);
        let text = p.get("condition_message").and_then(|t| t.as_str()).unwrap_or("").to_string();
        entry.3.push((no, text));
    }
    let mut out = vec![];
    for (_, (issuer, from, to, mut texts)) in by_msg {
        texts.sort();
        let text = texts.iter().map(|t| t.1.as_str()).collect::<Vec<_>>().join("\n").replace('\r', "");
        let mut tmi = Value::Null;
        let mut tracks: Vec<Map<String, Value>> = vec![];
        let mut current: Option<usize> = None;
        for raw in text.split('\n') {
            let line = raw.trim().trim_end_matches('-').trim();
            if let Some(n) = tmi_of(line) {
                tmi = Value::from(n);
            }
            if let (Some((east, lv)), Some(i)) = (levels_line(line), current) {
                let key = if east { "east_levels" } else { "west_levels" };
                tracks[i].insert(key.into(), Value::from(levels(lv)));
                continue;
            }
            let Some((letter, rest)) = track_line(line) else { continue };
            let tokens: Vec<&str> = rest.split_whitespace().collect();
            let points: Vec<Option<(f64, f64)>> = tokens.iter().map(|t| parse_point(t)).collect();
            let coords: Vec<(f64, f64)> = points.iter().flatten().copied().collect();
            if coords.len() < 2 {
                continue; // not a track line
            }
            let mut t = Map::new();
            t.insert("letter".into(), letter.to_string().into());
            t.insert("entry".into(), if points[0].is_none() { tokens[0].into() } else { Value::Null });
            t.insert(
                "exit".into(),
                if points[points.len() - 1].is_none() { tokens[tokens.len() - 1].into() } else { Value::Null },
            );
            t.insert("points".into(), coords.iter().map(|(a, b)| Value::from(vec![*a, *b])).collect());
            t.insert("east_levels".into(), Value::Array(vec![]));
            t.insert("west_levels".into(), Value::Array(vec![]));
            tracks.push(t);
            current = Some(tracks.len() - 1);
        }
        for t in &mut tracks {
            let has = |k: &str| t.get(k).and_then(|v| v.as_array()).is_some_and(|a| !a.is_empty());
            let (w, e) = (has("west_levels"), has("east_levels"));
            let dir = if w && !e {
                Value::from("W")
            } else if e && !w {
                Value::from("E")
            } else {
                Value::Null
            };
            t.insert("direction".into(), dir);
        }
        if tracks.is_empty() {
            continue;
        }
        let mut m = Map::new();
        m.insert("issuer".into(), issuer);
        m.insert("valid_from".into(), from);
        m.insert("valid_to".into(), to);
        m.insert("tmi".into(), tmi);
        m.insert("tracks".into(), Value::Array(tracks.into_iter().map(Value::Object).collect()));
        m.insert("raw".into(), text.into());
        out.push(m);
    }
    out
}

/// `datetime.fromisoformat(s.replace("Z", "+00:00"))` for the FAA's stamps.
fn when(v: &Value) -> Option<DateTime<Utc>> {
    let s = v.as_str()?.replace('Z', "+00:00");
    DateTime::parse_from_rfc3339(&s)
        .or_else(|_| DateTime::parse_from_str(&s, "%Y-%m-%dT%H:%M%:z"))
        .ok()
        .map(|d| d.with_timezone(&Utc))
}

/// Keep each message once (issuer + start of validity); how many were new.
pub async fn store(db: &tokio_postgres::Client, messages: &[Map<String, Value>]) -> Result<usize, tokio_postgres::Error> {
    let mut new = 0;
    for m in messages {
        let (Some(a), Some(b)) = (m.get("valid_from").and_then(when), m.get("valid_to").and_then(when)) else { continue };
        let issuer = m.get("issuer").and_then(|i| i.as_str()).filter(|s| !s.is_empty()).unwrap_or("?").to_string();
        let seen = db
            .query_opt("SELECT id FROM nat_messages WHERE issuer = $1 AND valid_from = $2", &[&issuer, &a])
            .await?;
        if seen.is_some() {
            continue;
        }
        let mut tracks = String::new();
        write_dumps(&mut tracks, m.get("tracks").unwrap_or(&Value::Null));
        let tmi: Option<i32> = m.get("tmi").and_then(|t| t.as_i64()).map(|t| t as i32);
        let raw: Option<String> = m.get("raw").and_then(|r| r.as_str()).map(str::to_string);
        db.execute(
            "INSERT INTO nat_messages (issuer, tmi, valid_from, valid_to, tracks, raw, fetched_at) \
             VALUES ($1, $2, $3, $4, $5::text::json, $6, now())",
            &[&issuer, &tmi, &a, &b, &tracks, &raw],
        )
        .await?;
        new += 1;
    }
    Ok(new)
}

/// Fetch the current message every FETCH_S and keep what is new.
pub async fn collect(database_url: String) {
    let client = reqwest::Client::builder().timeout(Duration::from_secs(30)).user_agent(USER_AGENT).build().unwrap();
    let db = crate::pg::Lazy::new(&database_url);
    loop {
        let round = async {
            let body = client.get(NAT_URL).header("Accept", "application/json").send().await?.error_for_status()?.bytes().await?;
            let messages = parse_parts(&serde_json::from_slice(&body)?);
            let g = db.get().await?;
            let n = store(g.as_ref().unwrap(), &messages).await?;
            anyhow::Ok(n)
        };
        match round.await {
            Ok(n) if n > 0 => eprintln!("nat: {n} new track message(s)"),
            Ok(_) => {}
            Err(e) => eprintln!("nat: fetch failed: {e}"),
        }
        tokio::time::sleep(Duration::from_secs(FETCH_S)).await;
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn points_and_levels() {
        assert_eq!(parse_point("54/20"), Some((54.0, -20.0)));
        assert_eq!(parse_point("5630/40"), Some((56.5, -40.0)));
        assert_eq!(parse_point("35/20"), None);
        assert_eq!(parse_point("54/2"), None);
        assert_eq!(parse_point("MALOT"), None);
        assert_eq!(levels(" NIL"), Vec::<i64>::new());
        assert_eq!(levels("340 350 X"), vec![340, 350]);
        assert_eq!(tmi_of("TMI IS 268 AND"), Some(268));
    }

    /// NAT_FIXTURE (the FAA JSON) parsed and written as `json.dumps`
    /// would write the list, for comparison with nat.py.
    #[test]
    fn parses_a_fixture() {
        let (Ok(path), Ok(out)) = (std::env::var("NAT_FIXTURE"), std::env::var("NAT_OUT")) else { return };
        let parts: Value = serde_json::from_slice(&std::fs::read(path).unwrap()).unwrap();
        let msgs = Value::Array(parse_parts(&parts).into_iter().map(Value::Object).collect());
        let mut s = String::new();
        write_dumps(&mut s, &msgs);
        std::fs::write(out, s).unwrap();
    }

    /// NAT_FIXTURE stored into NAT_TEST_DB twice: each message once.
    #[tokio::test]
    async fn stores_each_message_once() {
        let (Ok(path), Ok(url)) = (std::env::var("NAT_FIXTURE"), std::env::var("NAT_TEST_DB")) else { return };
        let parts: Value = serde_json::from_slice(&std::fs::read(path).unwrap()).unwrap();
        let msgs = parse_parts(&parts);
        let db = crate::pg::connect(&url).await.unwrap();
        assert_eq!(store(&db, &msgs).await.unwrap(), msgs.len());
        assert_eq!(store(&db, &msgs).await.unwrap(), 0);
    }
}
