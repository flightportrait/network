//! Reading the day-written tables from the snapshot, for an instance
//! with no Postgres (a self-hosted Pi): the public cuts refdata_export
//! writes, stored the way SQLAlchemy stores them in SQLite (dates as
//! 'YYYY-MM-DD', timestamps as 'YYYY-MM-DD HH:MM:SS.ffffff' in UTC), in
//! the order the source table held them (rowid = Postgres's ctid order,
//! which its scans return ties in).

use chrono::{DateTime, NaiveDate, NaiveDateTime, Utc};

pub fn date(s: Option<String>) -> Option<NaiveDate> {
    s.and_then(|s| NaiveDate::parse_from_str(s.get(..10)?, "%Y-%m-%d").ok())
}

pub fn ts(s: &str) -> Option<DateTime<Utc>> {
    let s = s.trim().trim_end_matches("+00:00");
    NaiveDateTime::parse_from_str(s, "%Y-%m-%d %H:%M:%S%.f")
        .or_else(|_| NaiveDateTime::parse_from_str(s, "%Y-%m-%dT%H:%M:%S%.f"))
        .ok()
        .map(|t| t.and_utc())
}

/// A list of ids as a JSON array, for `IN (SELECT value FROM json_each(?))`.
pub fn ids(ids: &[i64]) -> String {
    serde_json::to_string(ids).unwrap_or_else(|_| "[]".into())
}

#[cfg(test)]
mod tests {
    #[test]
    fn times_as_sqlalchemy_stores_them() {
        let t = super::ts("2026-09-25 03:00:05.760215").unwrap();
        assert_eq!(crate::stations::isoformat(t), "2026-09-25T03:00:05.760215+00:00");
        let t = super::ts("2026-09-25 03:00:05.000000").unwrap();
        assert_eq!(crate::stations::isoformat(t), "2026-09-25T03:00:05+00:00");
        assert_eq!(super::date(Some("2026-09-25".into())).unwrap().to_string(), "2026-09-25");
    }
}
