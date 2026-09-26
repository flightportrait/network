//! The published boards artifact (data/boards.db): what airports publish
//! for today, one row per flight and direction. Port of `boards_db.py`:
//! a fresh read-only connection per call, absent file means no board.

use chrono::Utc;
use chrono_tz::Tz;
use rusqlite::{Connection, OpenFlags};

pub struct BoardRow {
    pub flight: String,
    /// the other end: destination of a departure, origin of an arrival
    pub counterpart: Option<String>,
    pub hhmm: String,
}

pub struct Today {
    pub day: String,
    pub departures: Vec<BoardRow>,
    pub arrivals: Vec<BoardRow>,
}

pub fn hhmm(minutes: i64) -> String {
    format!("{:02}:{:02}", minutes.div_euclid(60), minutes.rem_euclid(60))
}

/// The airport's local date now (an unknown or absent zone reads as UTC).
pub fn local_day(tz: Option<&str>) -> String {
    match tz.and_then(|z| z.parse::<Tz>().ok()) {
        Some(zone) => Utc::now().with_timezone(&zone).date_naive().to_string(),
        None => Utc::now().date_naive().to_string(),
    }
}

pub fn mtime(path: &str) -> Option<std::time::SystemTime> {
    std::fs::metadata(path).and_then(|m| m.modified()).ok()
}

/// Today's published board for `iata`, or None (no file, no rows, or an
/// unreadable file).
pub fn today(path: &str, iata: &str, day: &str) -> Option<Today> {
    if path.is_empty() || iata.is_empty() || mtime(path).is_none() {
        return None;
    }
    let c = Connection::open_with_flags(path, OpenFlags::SQLITE_OPEN_READ_ONLY | OpenFlags::SQLITE_OPEN_NO_MUTEX).ok()?;
    c.busy_timeout(std::time::Duration::from_secs(5)).ok()?;
    let mut stmt = c
        .prepare(
            "SELECT kind, flight, counterpart, sched_min FROM boards \
             WHERE airport = ? AND day = ? AND sched_min IS NOT NULL ORDER BY sched_min, flight",
        )
        .ok()?;
    let rows: Vec<(String, String, Option<String>, i64)> = stmt
        .query_map((iata, day), |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?, r.get(3)?)))
        .ok()?
        .collect::<rusqlite::Result<_>>()
        .ok()?;
    if rows.is_empty() {
        return None;
    }
    let mut seen = std::collections::HashSet::new();
    let (mut departures, mut arrivals) = (vec![], vec![]);
    for (kind, flight, counterpart, sched) in rows {
        if !seen.insert((kind.clone(), flight.clone())) {
            continue;
        }
        let row = BoardRow { flight, counterpart, hhmm: hhmm(sched) };
        if kind == "dep" {
            departures.push(row);
        } else {
            arrivals.push(row);
        }
    }
    Some(Today { day: day.to_string(), departures, arrivals })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn minutes_read_like_python() {
        assert_eq!(hhmm(0), "00:00");
        assert_eq!(hhmm(605), "10:05");
        assert_eq!(hhmm(1500), "25:00"); // past midnight stays readable
    }

    #[test]
    fn zones() {
        assert_eq!(local_day(Some("Nowhere/Nothing")), Utc::now().date_naive().to_string());
        assert_eq!(local_day(None), Utc::now().date_naive().to_string());
        let sg = local_day(Some("Asia/Singapore"));
        assert_eq!(sg.len(), 10);
    }
}


const DAY_NAMES: [&str; 7] = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];

/// ref_schedule.weekdays (JSON text, {"4": [dep_min, arr_min]}) as the
/// API writes it: Monday first, [{"day": "Fri", "dep": "18:00", "arr":
/// "23:10"}]. None when the row keeps one slot every day.
pub fn weekday_times(text: Option<&str>) -> Option<serde_json::Value> {
    use serde_json::{json, Value};
    let map: serde_json::Map<String, Value> = serde_json::from_str(text?).ok()?;
    let mut days: Vec<(usize, &Value)> = map
        .iter()
        .filter_map(|(k, v)| k.parse::<usize>().ok().filter(|d| *d < 7).map(|d| (d, v)))
        .collect();
    days.sort_by_key(|(d, _)| *d);
    let at = |v: &Value, i: usize| v.get(i).and_then(Value::as_i64).map_or(Value::Null, |m| Value::String(hhmm(m)));
    let out: Vec<Value> = days
        .into_iter()
        .map(|(d, v)| json!({"day": DAY_NAMES[d], "dep": at(v, 0), "arr": at(v, 1)}))
        .collect();
    (!out.is_empty()).then_some(Value::Array(out))
}

/// "weekdays" when the snapshot has the column, NULL otherwise, for a
/// SELECT list over ref_schedule.
pub fn weekdays_col(refdb: &crate::refdb::RefDb) -> &'static str {
    if refdb.has(&["ref_schedule.weekdays"]) { "weekdays" } else { "NULL" }
}

#[cfg(test)]
mod weekday_tests {
    use super::*;

    #[test]
    fn weekday_times_monday_first() {
        let v = weekday_times(Some(r#"{"6": [600, null], "4": [1080, 1390]}"#)).unwrap();
        assert_eq!(
            serde_json::to_string(&v).unwrap(),
            r#"[{"day":"Fri","dep":"18:00","arr":"23:10"},{"day":"Sun","dep":"10:00","arr":null}]"#
        );
        assert!(weekday_times(None).is_none());
        assert!(weekday_times(Some("{}")).is_none());
    }
}
