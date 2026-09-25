//! One aircraft (the Python service's /v1/airframes/{hex}): registry
//! identity from the nightly snapshot, the observed flight log, and the
//! airframe's lifetime record (registrations, operators, public events).
//! The record is written during the day (emergency squawks land within
//! seconds), so it is read from Postgres.

use std::sync::Arc;

use axum::extract::{Path, Request, State};
use axum::response::{IntoResponse, Response};
use chrono::{DateTime, NaiveDate, Utc};
use serde_json::{Map, Value};

use crate::catalog::{dark, CACHE};
use crate::http::{client_ip, json, peer_of, throttle, ApiError};
use crate::legs::sql_json;
use crate::pyjson::write_value;
use crate::state::App;
use crate::stations::isoformat;

/// Rows of the flight log an airframe page shows, newest first.
const MAX_LEGS: i64 = 200;

/// What a registry states about the airframe, newest statement per
/// field; owner names are company owners only.
const REGISTRY_FIELDS: [&str; 7] =
    ["model", "engine", "certificate_date", "airworthiness_date", "registry_status", "owner", "registration"];

fn day(d: Option<NaiveDate>) -> Value {
    d.map_or(Value::Null, |d| Value::String(d.format("%Y-%m-%d").to_string()))
}

fn text(s: Option<String>) -> Value {
    s.map_or(Value::Null, Value::String)
}

/// `LegBook.get`: the airframe's legs, newest first, and the identity the
/// freshest rows carry. None when the log never saw it.
fn log_of(c: &rusqlite::Connection, hex: &str) -> rusqlite::Result<Option<(Value, Value, Value)>> {
    let mut stmt = c.prepare_cached(
        "SELECT date, org, dst, dep_ts, arr_ts, max_alt, reg, type, callsign FROM legs WHERE hex = ? \
         ORDER BY date DESC, dep_ts DESC LIMIT ?",
    )?;
    let mut rows = stmt.query(rusqlite::params![hex, MAX_LEGS])?;
    let (mut legs, mut reg, mut type_code) = (vec![], Value::Null, Value::Null);
    while let Some(r) = rows.next()? {
        let mut m = Map::new();
        for (i, k) in ["date", "org", "dst", "dep_ts", "arr_ts", "max_alt"].iter().enumerate() {
            m.insert(k.to_string(), sql_json(r.get_ref(i)?));
        }
        m.insert("callsign".into(), sql_json(r.get_ref(8)?));
        legs.push(Value::Object(m));
        let truthy = |v: &Value| !matches!(v, Value::Null) && v.as_str() != Some("") && v != &Value::from(0);
        if reg.is_null() {
            let v = sql_json(r.get_ref(6)?);
            if truthy(&v) {
                reg = v;
            }
        }
        if type_code.is_null() {
            let v = sql_json(r.get_ref(7)?);
            if truthy(&v) {
                type_code = v;
            }
        }
    }
    Ok((!legs.is_empty()).then_some((Value::Array(legs), reg, type_code)))
}

/// The operator's airline (name, JSON) and the type's (name, category).
type Names = (Option<(String, String)>, Option<(String, Value)>);

/// The registry row, the flight log, and names from the snapshot.
struct Facts {
    /// registration, type_code, operator_name, operator_icao, year, source
    frame: Option<[Value; 6]>,
    log: Option<(Value, Value, Value)>,
}

fn facts(app: &App, hex: &str) -> rusqlite::Result<Facts> {
    let mut frame = None;
    if app.refdb.has(&["ref_airframes"]) {
        let c = app.refdb.conn()?;
        let mut stmt = c.prepare_cached(
            "SELECT registration, type_code, operator_name, operator_icao, year, source FROM ref_airframes WHERE hex = ?1",
        )?;
        let mut rows = stmt.query([hex])?;
        if let Some(r) = rows.next()? {
            frame = Some([
                sql_json(r.get_ref(0)?),
                sql_json(r.get_ref(1)?),
                sql_json(r.get_ref(2)?),
                sql_json(r.get_ref(3)?),
                sql_json(r.get_ref(4)?),
                sql_json(r.get_ref(5)?),
            ]);
        }
    }
    let log = if app.legs.available() { app.legs.with_conn(|c| log_of(c, hex)).flatten() } else { None };
    Ok(Facts { frame, log })
}

/// The lifetime record behind a hex: registrations, operators and public
/// events, oldest spell first, newest event first. None when the record
/// holds no airframe for this hex.
async fn history(db: &tokio_postgres::Client, hex: &str) -> Result<Option<Value>, tokio_postgres::Error> {
    let Some(row) = db
        .query_opt(
            "SELECT airframe_id FROM airframe_spells WHERE kind = $1 AND value = $2 ORDER BY first_date DESC LIMIT 1",
            &[&"hex", &hex],
        )
        .await?
    else {
        return Ok(None);
    };
    let id: i64 = row.get(0);
    let Some(frame) = db
        .query_opt(
            "SELECT id, msn, built_year, manufacturer, first_observed, last_observed FROM airframes WHERE id = $1",
            &[&id],
        )
        .await?
    else {
        return Ok(None);
    };
    let spells = db
        .query(
            "SELECT kind, value, first_date, last_date, n_obs, source FROM airframe_spells \
             WHERE airframe_id = $1 ORDER BY first_date",
            &[&id],
        )
        .await?;
    let events = db
        .query(
            "SELECT kind, at, lat, lon, detail::text, source FROM airframe_events \
             WHERE airframe_id = $1 AND visibility = $2 ORDER BY at DESC",
            &[&id, &"public"],
        )
        .await?;
    let fields: Vec<String> = REGISTRY_FIELDS.iter().map(|f| f.to_string()).collect();
    let claims = db
        .query(
            "SELECT field, value, source FROM airframe_claims WHERE airframe_id = $1 AND field = ANY($2) ORDER BY last_seen",
            &[&id, &fields],
        )
        .await?;
    let registry = if claims.is_empty() {
        Value::Null
    } else {
        let mut m = Map::new();
        m.insert("source".into(), Value::Null);
        for c in &claims {
            m.insert(c.get::<_, String>(0), Value::String(c.get(1)));
            m.insert("source".into(), Value::String(c.get(2)));
        }
        Value::Object(m)
    };
    let icaos: Vec<String> = spells
        .iter()
        .filter(|s| s.get::<_, String>(0) == "operator")
        .map(|s| s.get::<_, String>(1))
        .collect();
    let names: std::collections::HashMap<String, String> = db
        .query("SELECT icao, name FROM ref_airlines WHERE icao = ANY($1)", &[&icaos])
        .await?
        .iter()
        .map(|r| (r.get(0), r.get(1)))
        .collect();
    let spell = |s: &tokio_postgres::Row, key: &str, extra: Option<Value>| {
        let mut m = Map::new();
        m.insert(key.into(), Value::String(s.get(1)));
        if let Some(name) = extra {
            m.insert("name".into(), name);
        }
        m.insert("from".into(), day(s.get(2)));
        m.insert("to".into(), day(s.get(3)));
        m.insert("legs".into(), s.get::<_, Option<i32>>(4).map_or(Value::Null, Value::from));
        m.insert("source".into(), Value::String(s.get(5)));
        Value::Object(m)
    };
    let of_kind = |kind: &'static str| spells.iter().filter(move |s| s.get::<_, String>(0) == kind);
    let mut out = Map::new();
    out.insert("airframe_id".into(), Value::from(frame.get::<_, i64>(0)));
    out.insert("msn".into(), text(frame.get(1)));
    out.insert("built_year".into(), frame.get::<_, Option<i32>>(2).map_or(Value::Null, Value::from));
    out.insert("manufacturer".into(), text(frame.get(3)));
    out.insert("registry".into(), registry);
    out.insert("first_observed".into(), day(frame.get(4)));
    out.insert("last_observed".into(), day(frame.get(5)));
    out.insert("hexes".into(), Value::Array(of_kind("hex").map(|s| spell(s, "hex", None)).collect()));
    out.insert("registrations".into(), Value::Array(of_kind("registration").map(|s| spell(s, "reg", None)).collect()));
    out.insert(
        "operators".into(),
        Value::Array(
            of_kind("operator")
                .map(|s| spell(s, "icao", Some(text(names.get(&s.get::<_, String>(1)).cloned()))))
                .collect(),
        ),
    );
    let float = |v: Option<f64>| v.and_then(serde_json::Number::from_f64).map_or(Value::Null, Value::Number);
    out.insert(
        "events".into(),
        Value::Array(
            events
                .iter()
                .map(|e| {
                    let mut m = Map::new();
                    m.insert("kind".into(), Value::String(e.get(0)));
                    m.insert("at".into(), Value::String(isoformat(e.get::<_, DateTime<Utc>>(1))));
                    m.insert("lat".into(), float(e.get(2)));
                    m.insert("lon".into(), float(e.get(3)));
                    let detail: Option<String> = e.get(4);
                    m.insert("detail".into(), detail.and_then(|t| serde_json::from_str(&t).ok()).unwrap_or(Value::Null));
                    m.insert("source".into(), Value::String(e.get(5)));
                    Value::Object(m)
                })
                .collect(),
        ),
    );
    Ok(Some(Value::Object(out)))
}

/// GET /v1/airframes/{hex}
pub async fn airframe(State(app): State<Arc<App>>, Path(hex): Path<String>, req: Request) -> Response {
    let Some(db) = &app.db else {
        return crate::proxy::forward(State(app), req).await.into_response();
    };
    let ip = client_ip(&app, req.headers(), peer_of(&req));
    if let Err(e) = throttle(&app, &ip, "airframe", app.settings.airframe_rate_limit) {
        return e.into_response();
    }
    let hex = hex.trim().to_lowercase();
    if hex.len() != 6 || !hex.bytes().all(|b| b.is_ascii_hexdigit()) {
        return ApiError::new(422, "invalid_request", "invalid hex").into_response();
    }
    let internal = || ApiError::new(500, "internal_error", "internal error").into_response();
    let (a, h) = (app.clone(), hex.clone());
    let Ok(Ok(f)) = tokio::task::spawn_blocking(move || facts(&a, &h)).await else { return internal() };
    let hist = {
        let g = match db.get().await {
            Ok(g) => g,
            Err(e) => {
                eprintln!("airframes: database: {e}");
                return internal();
            }
        };
        match history(g.as_ref().unwrap(), &hex).await {
            Ok(h) => h,
            Err(e) => {
                eprintln!("airframes: database: {e}");
                return internal();
            }
        }
    };
    let legs_up = app.legs.available();
    if f.frame.is_none() && f.log.is_none() && hist.is_none() {
        if !legs_up {
            // the registry is silent and the log is dark: unknowable
            return dark().into_response();
        }
        return ApiError::new(404, "not_found", "unknown airframe").into_response();
    }
    let fr = |i: usize| f.frame.as_ref().map_or(Value::Null, |row| row[i].clone());
    let truthy = |v: &Value| !matches!(v, Value::Null) && v.as_str() != Some("");
    let mut reg = fr(0);
    let mut type_code = fr(1);
    let mut operator = fr(2);
    let operator_icao = fr(3);
    let (mut legs, mut window) = (Value::Null, Value::Null);
    if let Some((log_legs, log_reg, log_type)) = f.log {
        legs = log_legs;
        window = app.legs.window_days().map_or(Value::Null, Value::from);
        // the log can carry identity the registry lacks
        if !truthy(&reg) {
            reg = log_reg;
        }
        if !truthy(&type_code) {
            type_code = log_type;
        }
    } else if legs_up {
        legs = Value::Array(vec![]);
        window = app.legs.window_days().map_or(Value::Null, Value::from);
    }
    // the operator's airline and the type's name, from the snapshot
    let (icao, code) = (operator_icao.as_str().filter(|s| !s.is_empty()).map(str::to_string), type_code.as_str().map(str::to_string));
    let a = app.clone();
    let names = tokio::task::spawn_blocking(move || -> rusqlite::Result<Names> {
        let (mut airline, mut ty) = (None, None);
        if let Some(icao) = icao.filter(|_| a.refdb.has(&["ref_airlines", "ref_alliance_memberships", "ref_alliances"])) {
            airline = crate::refdata::airline_object(&a.refdb.conn()?, &icao)?;
        }
        if let Some(code) = code.filter(|_| a.refdb.has(&["ref_types"])) {
            let c = a.refdb.conn()?;
            let mut stmt = c.prepare_cached("SELECT name, category FROM ref_types WHERE designator = ?1")?;
            let mut rows = stmt.query([&code])?;
            if let Some(r) = rows.next()? {
                ty = Some((r.get(0)?, sql_json(r.get_ref(1)?)));
            }
        }
        Ok((airline, ty))
    })
    .await;
    let Ok(Ok((airline, ty))) = names else { return internal() };
    let mut airline_json = None;
    if let Some((name, json)) = airline {
        if !truthy(&operator) {
            operator = Value::String(name);
        }
        airline_json = Some(json);
    }
    let (type_name, category) = match ty {
        Some((n, c)) => (Value::String(n), c),
        None => (Value::Null, Value::Null),
    };
    let mut out = String::with_capacity(8192);
    let mut m = Map::new();
    m.insert("hex".into(), Value::String(hex.clone()));
    m.insert("reg".into(), reg);
    m.insert("type".into(), type_code);
    m.insert("type_name".into(), type_name);
    m.insert("category".into(), category);
    m.insert("operator".into(), operator);
    m.insert("operator_icao".into(), operator_icao);
    m.insert("year".into(), fr(4));
    m.insert("country".into(), crate::address_blocks::state_of(&hex).map_or(Value::Null, |s| s.into()));
    m.insert("source".into(), fr(5));
    // the airline object is already JSON: spliced in below
    m.insert("airline".into(), Value::Null);
    m.insert("legs".into(), legs);
    m.insert("window_days".into(), window);
    m.insert("coverage".into(), "observed".into());
    m.insert("history".into(), hist.unwrap_or(Value::Null));
    if let Some(j) = airline_json {
        m.insert("airline".into(), serde_json::from_str(&j).unwrap_or(Value::Null));
    }
    write_value(&mut out, &Value::Object(m));
    json(out, CACHE)
}
