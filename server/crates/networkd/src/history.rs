//! History routes served from the nightly artifacts: the airport page
//! (registry identity from the snapshot, observed totals from legs.db,
//! inferred boards from the schedule, today's published board from
//! boards.db). Port of `routes_history.py`'s `airport`.
//!
//! One deliberate difference: the arrivals board's rows tied at its
//! 80-row cut, which Python's database picks differently from call to
//! call (a parallel scan); here ties fall back to callsign, origin,
//! destination, so the page is the same on every request.

use std::collections::HashMap;
use std::sync::Arc;

use axum::extract::{Path, Request, State};
use axum::response::{IntoResponse, Response};
use rusqlite::OptionalExtension;

use crate::boards;
use crate::http::{client_ip, json, peer_of, throttle, ApiError};
use crate::pyjson::{write_float, write_str, Obj};
use crate::state::App;

const CACHE: &str = "public, s-maxage=3600";

struct Airport {
    ident: String,
    name: Option<String>,
    kind: Option<String>,
    role: Option<String>,
    lat: Option<f64>,
    lon: Option<f64>,
    iso_country: Option<String>,
    municipality: Option<String>,
    iata: Option<String>,
    tz: Option<String>,
}

const AIRPORT_COLS: &str = "ident, name, kind, role, lat, lon, iso_country, municipality, iata, tz";

fn read_airport(r: &rusqlite::Row) -> rusqlite::Result<Airport> {
    Ok(Airport {
        ident: r.get(0)?,
        name: r.get(1)?,
        kind: r.get(2)?,
        role: r.get(3)?,
        lat: r.get(4)?,
        lon: r.get(5)?,
        iso_country: r.get(6)?,
        municipality: r.get(7)?,
        iata: r.get(8)?,
        tz: r.get(9)?,
    })
}

#[derive(Clone)]
struct Leg {
    callsign: String,
    org: String,
    dst: String,
    airline: Option<String>,
    dep_min: Option<i64>,
    arr_min: Option<i64>,
    type_code: Option<String>,
    flight: Option<String>,
    source: String,
    n_flights: i64,
}

const LEG_COLS: &str = "callsign, org, dst, airline_icao, dep_min, arr_min, type_code, flight, source, n_flights";

fn read_leg(r: &rusqlite::Row) -> rusqlite::Result<Leg> {
    Ok(Leg {
        callsign: r.get(0)?,
        org: r.get(1)?,
        dst: r.get(2)?,
        airline: r.get(3)?,
        dep_min: r.get(4)?,
        arr_min: r.get(5)?,
        type_code: r.get(6)?,
        flight: r.get(7)?,
        source: r.get(8)?,
        n_flights: r.get::<_, Option<i64>>(9)?.unwrap_or(0),
    })
}

fn opt(out: &mut String, v: &Option<String>) {
    match v {
        Some(s) => write_str(out, s),
        None => out.push_str("null"),
    }
}

fn opt_f(out: &mut String, v: Option<f64>) {
    match v {
        Some(x) => write_float(out, x),
        None => out.push_str("null"),
    }
}

fn hhmm(out: &mut String, v: Option<i64>) {
    match v {
        Some(m) => write_str(out, &boards::hhmm(m)),
        None => out.push_str("null"),
    }
}

/// `flight or callsign`: an empty flight number counts as none.
fn flight_name(l: &Leg) -> &str {
    match l.flight.as_deref() {
        Some(f) if !f.is_empty() => f,
        _ => &l.callsign,
    }
}

fn board_item(out: &mut String, l: &Leg, other: (&str, &str)) {
    let mut o = Obj::new(out);
    o.str("flight", flight_name(l)).str(other.0, other.1);
    hhmm(o.key("dep"), l.dep_min);
    hhmm(o.key("arr"), l.arr_min);
    opt(o.key("type"), &l.type_code);
    o.int("flights", l.n_flights).str("source", &l.source);
    o.end();
}

const AIRPORT_NEEDS: &[&str] = &["ref_airports", "rank_ref_airports_ident", "ref_schedule"];

pub async fn airport(State(app): State<Arc<App>>, Path(code): Path<String>, req: Request) -> Response {
    if !app.refdb.has(AIRPORT_NEEDS) {
        return crate::proxy::forward(State(app), req).await;
    }
    let ip = client_ip(&app, req.headers(), peer_of(&req));
    if let Err(e) = throttle(&app, &ip, "airport", app.settings.airport_rate_limit) {
        return e.into_response();
    }
    let code = code.trim().to_uppercase();
    let n = code.chars().count();
    if !(3..=4).contains(&n) || !code.chars().all(char::is_alphanumeric) {
        return ApiError::new(422, "invalid_request", "invalid airport code").into_response();
    }
    let app2 = app.clone();
    let done = tokio::task::spawn_blocking(move || build(&app2, &code)).await;
    match done {
        Ok(Ok(Ok(body))) => json(body, CACHE),
        Ok(Ok(Err(api))) => api.into_response(),
        Ok(Err(e)) => {
            eprintln!("airport: {e}");
            ApiError::new(500, "internal_error", "internal error").into_response()
        }
        Err(e) => {
            eprintln!("airport: {e}");
            ApiError::new(500, "internal_error", "internal error").into_response()
        }
    }
}

fn build(app: &App, code: &str) -> rusqlite::Result<Result<String, ApiError>> {
    let c = app.refdb.conn()?;
    // registry: ICAO ident first, then IATA (lowest ident, in the
    // database's collation)
    let mut reg = c
        .prepare_cached(&format!("SELECT {AIRPORT_COLS} FROM ref_airports WHERE ident = ?1"))?
        .query_row([code], read_airport)
        .optional()?;
    if reg.is_none() && code.chars().count() == 3 {
        reg = c
            .prepare_cached(&format!(
                "SELECT {AIRPORT_COLS} FROM ref_airports a JOIN rank_ref_airports_ident r ON r.key = a.ident \
                 WHERE a.iata = ?1 ORDER BY r.rank LIMIT 1"
            ))?
            .query_row([code], read_airport)
            .optional()?;
    }
    let iata: Option<String> = reg
        .as_ref()
        .and_then(|r| r.iata.clone().filter(|s| !s.is_empty()))
        .or_else(|| (code.chars().count() == 3).then(|| code.to_string()));

    let legs_up = app.legs.available();
    // the page is a function of the snapshot (the cache's generation), the
    // legs and boards files, and the airport's local day
    let day = boards::local_day(reg.as_ref().and_then(|r| r.tz.as_deref()));
    let stamp = |p: &str| boards::mtime(p).map(|t| format!("{t:?}")).unwrap_or_default();
    let key = format!(
        "airport:{code}:{day}:{}:{}",
        stamp(&app.settings.legs_path),
        stamp(&app.settings.boards_path)
    );
    if let Some(body) = app.refdb.cached(&key) {
        return Ok(Ok(String::from_utf8_lossy(&body).into_owned()));
    }
    let observed = match (&iata, legs_up) {
        (Some(i), true) => app.legs.airport(i),
        _ => None,
    };
    if reg.is_none() && observed.is_none() {
        if !legs_up && code.chars().count() == 3 {
            return Ok(Err(ApiError::new(503, "artifact_unavailable", "history artifacts are not loaded")
                .header("Retry-After", "300")));
        }
        return Ok(Err(ApiError::new(404, "not_found", "unknown airport")));
    }

    let min = app.settings.schedule_min_flights;
    // ORDER BY n_flights DESC LIMIT 80. Departures: Postgres reads the
    // schedule through its (org, dst) index, in storage order, and
    // pgsort::top_n picks among ties exactly as it does. Arrivals: it
    // scans the whole table in parallel, so Python's own pick among ties
    // changes from call to call; here it is stable.
    let side = |col: &str, exact: bool| -> rusqlite::Result<Vec<Leg>> {
        let Some(i) = &iata else { return Ok(vec![]) };
        if exact {
            let rows: Vec<Leg> = c
                .prepare_cached(&format!("SELECT {LEG_COLS} FROM ref_schedule WHERE {col} = ?1 AND n_flights >= ?2 ORDER BY rowid"))?
                .query_map((i, min), read_leg)?
                .collect::<rusqlite::Result<_>>()?;
            return Ok(crate::pgsort::top_n(rows, 80, &|x: &Leg, y: &Leg| y.n_flights.cmp(&x.n_flights)));
        }
        c.prepare_cached(&format!(
            "SELECT {LEG_COLS} FROM ref_schedule WHERE {col} = ?1 AND n_flights >= ?2 \
             ORDER BY n_flights DESC, callsign, org, dst LIMIT 80"
        ))?
        .query_map((i, min), read_leg)?
        .collect()
    };
    let mut deps = side("org", true)?;
    let mut arrs = side("dst", false)?;
    // airlines by flights, in first-seen order among equals (Python's
    // dict order through a stable sort)
    let mut airlines: Vec<(String, i64)> = vec![];
    let mut at: HashMap<String, usize> = HashMap::new();
    for l in &deps {
        if let Some(a) = l.airline.as_ref().filter(|a| !a.is_empty()) {
            match at.get(a) {
                Some(&k) => airlines[k].1 += l.n_flights,
                None => {
                    at.insert(a.clone(), airlines.len());
                    airlines.push((a.clone(), l.n_flights));
                }
            }
        }
    }
    airlines.sort_by_key(|(_, n)| -n);
    airlines.truncate(20);
    // boards by local time, rows without one last (stable)
    deps.sort_by_key(|l| (l.dep_min.is_none(), l.dep_min.map(boards::hhmm)));
    arrs.sort_by_key(|l| (l.arr_min.is_none(), l.arr_min.map(boards::hhmm)));

    // today's published board, with the observed service that carries each flight
    let today = iata.as_ref().and_then(|i| boards::today(&app.settings.boards_path, i, &day));
    let mut carried: HashMap<(String, String, String), (String, Option<String>)> = HashMap::new();
    if let (Some(t), Some(i)) = (&today, &iata) {
        let mut names: Vec<&str> = t.departures.iter().chain(&t.arrivals).map(|r| r.flight.as_str()).collect();
        names.sort_unstable();
        names.dedup();
        for chunk in names.chunks(500) {
            let marks = vec!["?"; chunk.len()].join(",");
            let mut stmt = c.prepare(&format!(
                "SELECT {LEG_COLS} FROM ref_schedule WHERE flight IN ({marks}) AND (org = ? OR dst = ?) \
                 ORDER BY callsign, org, dst"
            ))?;
            let mut params: Vec<&dyn rusqlite::ToSql> = chunk.iter().map(|s| s as &dyn rusqlite::ToSql).collect();
            params.push(i);
            params.push(i);
            let mut rows = stmt.query(params.as_slice())?;
            while let Some(r) = rows.next()? {
                let l = read_leg(r)?;
                if l.source == "both" {
                    if let Some(f) = l.flight.clone() {
                        carried.insert((f, l.org.clone(), l.dst.clone()), (l.callsign.clone(), l.type_code.clone()));
                    }
                }
            }
        }
    }

    let mut out = String::with_capacity(16 * 1024);
    let mut o = Obj::new(&mut out);
    opt(o.key("iata"), &iata);
    match (&today, &iata) {
        (Some(t), Some(i)) => {
            let buf = o.key("today");
            let mut td = Obj::new(buf);
            td.str("day", &t.day);
            let write_rows = |buf: &mut String, rows: &[boards::BoardRow], dep: bool| {
                buf.push('[');
                for (k, r) in rows.iter().enumerate() {
                    if k > 0 {
                        buf.push(',');
                    }
                    let other = r.counterpart.clone().unwrap_or_default();
                    let key = if dep {
                        (r.flight.clone(), i.clone(), other.clone())
                    } else {
                        (r.flight.clone(), other.clone(), i.clone())
                    };
                    let hit = carried.get(&key);
                    let mut ro = Obj::new(buf);
                    ro.str("flight", &r.flight);
                    opt(ro.key(if dep { "dst" } else { "org" }), &r.counterpart);
                    ro.str(if dep { "dep" } else { "arr" }, &r.hhmm);
                    opt(ro.key("callsign"), &hit.map(|h| h.0.clone()));
                    opt(ro.key("type"), &hit.and_then(|h| h.1.clone()));
                    ro.end();
                }
                buf.push(']');
            };
            write_rows(td.key("departures"), &t.departures, true);
            write_rows(td.key("arrivals"), &t.arrivals, false);
            td.str("source", "published");
            td.end();
        }
        _ => {
            o.null("today");
        }
    }
    let r = reg.as_ref();
    opt(o.key("ident"), &r.map(|a| a.ident.clone()));
    opt(o.key("name"), &r.and_then(|a| a.name.clone()));
    opt(o.key("kind"), &r.and_then(|a| a.kind.clone()));
    opt(o.key("role"), &r.and_then(|a| a.role.clone()));
    opt_f(o.key("lat"), r.and_then(|a| a.lat));
    opt_f(o.key("lon"), r.and_then(|a| a.lon));
    opt(o.key("iso_country"), &r.and_then(|a| a.iso_country.clone()));
    opt(o.key("municipality"), &r.and_then(|a| a.municipality.clone()));
    opt(o.key("tz"), &r.and_then(|a| a.tz.clone()));
    match &observed {
        Some(obs) => {
            o.raw("observed", obs);
        }
        None => {
            o.null("observed");
        }
    }
    let buf = o.key("board");
    buf.push('[');
    for (k, l) in deps.iter().enumerate() {
        if k > 0 {
            buf.push(',');
        }
        board_item(buf, l, ("dst", &l.dst));
    }
    buf.push(']');
    let buf = o.key("arrivals");
    buf.push('[');
    for (k, l) in arrs.iter().enumerate() {
        if k > 0 {
            buf.push(',');
        }
        board_item(buf, l, ("org", &l.org));
    }
    buf.push(']');
    let buf = o.key("airlines");
    buf.push('[');
    for (k, (icao, n)) in airlines.iter().enumerate() {
        if k > 0 {
            buf.push(',');
        }
        let mut a = Obj::new(buf);
        a.str("icao", icao).int("flights", *n);
        a.end();
    }
    buf.push(']');
    o.str("times", "local");
    match app.legs.window_days().filter(|_| legs_up) {
        Some(d) => o.int("window_days", d),
        None => o.null("window_days"),
    };
    o.str("coverage", "observed");
    o.end();
    app.refdb.remember(key, c.generation(), bytes::Bytes::from(out.clone()));
    Ok(Ok(out))
}
