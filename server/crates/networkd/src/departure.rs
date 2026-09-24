//! When did this aircraft take off, and has it landed: read from the
//! day's trace the hub keeps (readsb's trace_full). The current flight
//! is the last stretch of the trace, back from its end until a gap of
//! twenty minutes or a point on the ground. Port of `departure.py`.

use serde_json::Value;

use crate::pyjson::{round_to, Obj, Val};

const GAP_S: f64 = 1200.0;
const LOW_FT: f64 = 5000.0;

/// `{"departure": …, "arrival": …}` pieces, already encoded (`null` when
/// there is nothing to say).
#[derive(Clone, Debug, PartialEq)]
pub struct Bounds {
    pub departure: String,
    pub arrival: String,
}

impl Default for Bounds {
    fn default() -> Self {
        Bounds { departure: "null".into(), arrival: "null".into() }
    }
}

struct Row {
    t: f64,
    lat: Val,
    lon: Val,
    alt: Val,
}

impl Row {
    fn ground(&self) -> bool {
        matches!(&self.alt, Val::Str(s) if &**s == "ground")
    }
}

fn is_num(v: &Value) -> bool {
    v.is_number()
}

fn rows(trace: &Value) -> Vec<Row> {
    let Some(t0) = trace.get("timestamp").filter(|v| is_num(v)).and_then(Value::as_f64) else {
        return vec![];
    };
    let mut out = vec![];
    for p in trace.get("trace").and_then(Value::as_array).into_iter().flatten() {
        let Some(a) = p.as_array() else { continue };
        if a.len() >= 4 && is_num(&a[0]) {
            out.push(Row {
                t: t0 + a[0].as_f64().unwrap_or(0.0),
                lat: Val::from_json(&a[1]),
                lon: Val::from_json(&a[2]),
                alt: Val::from_json(&a[3]),
            });
        }
    }
    out
}

pub fn flight_bounds(trace: &Value) -> Bounds {
    let rows = rows(trace);
    let mut b = Bounds::default();
    if rows.is_empty() {
        return b;
    }
    let mut i = rows.len() - 1;
    while i > 0 {
        let (r, p) = (&rows[i], &rows[i - 1]);
        if r.t - p.t > GAP_S {
            break;
        }
        if p.ground() && !r.ground() {
            break;
        }
        i -= 1;
    }
    let seg = &rows[i..];
    let airborne: Vec<&Row> = seg.iter().filter(|r| !r.ground()).collect();
    let Some(take) = airborne.first() else { return b };
    let from_ground = (i > 0 && rows[i - 1].ground()) || rows[i].ground();
    let alt = match take.alt {
        Val::Int(_) | Val::UInt(_) | Val::Float(_) | Val::Bool(_) => Some(&take.alt),
        _ => None,
    };
    if from_ground || alt.and_then(Val::as_f64).is_some_and(|a| a <= LOW_FT) {
        let mut s = String::new();
        let mut o = Obj::new(&mut s);
        o.f64("at", round_to(take.t, 1));
        take.lat.write(o.key("lat"));
        take.lon.write(o.key("lon"));
        match alt {
            Some(a) => a.write(o.key("alt_ft")),
            None => {
                o.int("alt_ft", 0);
            }
        }
        o.end();
        b.departure = s;
    }
    if seg.last().is_some_and(Row::ground) {
        let mut j = seg.len() - 1;
        while j > 0 && seg[j - 1].ground() {
            j -= 1;
        }
        let land = &seg[j];
        let mut s = String::new();
        let mut o = Obj::new(&mut s);
        o.f64("at", round_to(land.t, 1));
        land.lat.write(o.key("lat"));
        land.lon.write(o.key("lon"));
        o.end();
        b.arrival = s;
    }
    b
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn take_off_and_landing() {
        let t: Value = serde_json::from_str(
            r#"{"timestamp": 1000, "trace": [
                [0, 1.0, 2.0, "ground"], [10, 1.1, 2.1, 1200], [600, 1.5, 2.5, 30000],
                [1200, 1.9, 2.9, 800], [1210, 2.0, 3.0, "ground"], [1220, 2.0, 3.0, "ground"]]}"#,
        )
        .unwrap();
        let b = flight_bounds(&t);
        assert_eq!(b.departure, r#"{"at":1010.0,"lat":1.1,"lon":2.1,"alt_ft":1200}"#);
        assert_eq!(b.arrival, r#"{"at":2210.0,"lat":2.0,"lon":3.0}"#);
    }

    #[test]
    fn first_heard_at_cruise_has_no_departure() {
        let t: Value = serde_json::from_str(r#"{"timestamp": 0, "trace": [[0, 1, 2, 35000], [5, 1, 2, 35000]]}"#).unwrap();
        assert_eq!(flight_bounds(&t), Bounds::default());
    }
}
