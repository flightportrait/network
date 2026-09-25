//! One aircraft (the Python service's /v1/airframes/{hex}): registry
//! identity from the nightly snapshot, the observed flight log, and the
//! airframe's lifetime record (registrations, operators, public events).
//! The record is written during the day (emergency squawks land within
//! seconds), so it is read from Postgres.

use std::sync::Arc;

use axum::extract::{Path, Request, State};
use rusqlite::OptionalExtension;
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

// ---- /v1/airlines/{icao}/airframes -----------------------------------------------

/// An operator spell counts as current when it was seen within this many
/// days of the record's newest observation.
const CURRENT_DAYS: i64 = 60;

/// airframes row: type_code, built_year, msn, first_observed
type FrameRow = (Option<String>, Option<i32>, Option<String>, Option<NaiveDate>);
/// ref_airframes row: registration, type_code, year
type RegistryRow = (Option<String>, Option<String>, Option<i64>);

struct Spell {
    kind: String,
    value: String,
    first: Option<NaiveDate>,
    last: Option<NaiveDate>,
}

/// The spell of `kind` that started last (then ended last); the first of
/// equals, as Python's max keeps it.
fn latest<'a>(items: &'a [Spell], kind: &str) -> Option<&'a Spell> {
    let key = |s: &Spell| (s.first.unwrap_or(NaiveDate::MIN), s.last.unwrap_or(NaiveDate::MIN));
    let mut best: Option<&Spell> = None;
    for s in items.iter().filter(|s| s.kind == kind) {
        if best.is_none_or(|b| key(s) > key(b)) {
            best = Some(s);
        }
    }
    best
}

/// GET /v1/airlines/{icao}/airframes: the aircraft an airline flies now,
/// from the airframe record (written during the day), by registration.
pub async fn airline_airframes(State(app): State<Arc<App>>, Path(icao): Path<String>, req: Request) -> Response {
    let Some(db) = &app.db else {
        return crate::proxy::forward(State(app), req).await.into_response();
    };
    if !app.refdb.has(&["ref_airlines", "ref_airframes"]) {
        return crate::proxy::forward(State(app), req).await.into_response();
    }
    let ip = client_ip(&app, req.headers(), peer_of(&req));
    if let Err(e) = throttle(&app, &ip, "refdata", app.settings.refdata_rate_limit) {
        return e.into_response();
    }
    let internal = |e: &dyn std::fmt::Display| {
        eprintln!("airline airframes: {e}");
        ApiError::new(500, "internal_error", "internal error").into_response()
    };
    let code = icao.trim().to_uppercase();
    let a = app.clone();
    let c2 = code.clone();
    let known = tokio::task::spawn_blocking(move || -> rusqlite::Result<Option<String>> {
        a.refdb.conn()?.prepare_cached("SELECT icao FROM ref_airlines WHERE icao = ?1")?.query_row([&c2], |r| r.get(0)).optional()
    })
    .await;
    let icao = match known {
        Ok(Ok(Some(i))) => i,
        Ok(Ok(None)) => return ApiError::new(404, "not_found", "unknown airline").into_response(),
        Ok(Err(e)) => return internal(&e),
        Err(e) => return internal(&e),
    };
    let g = match db.get().await {
        Ok(g) => g,
        Err(e) => return internal(&e),
    };
    let c = g.as_ref().unwrap();
    let newest: Option<NaiveDate> = match c.query_one("SELECT max(last_observed) FROM airframes", &[]).await {
        Ok(r) => r.get(0),
        Err(e) => return internal(&e),
    };
    let Some(newest) = newest else {
        return ApiError::new(404, "not_observed", "no airframes").into_response();
    };
    let cutoff = newest - chrono::TimeDelta::days(CURRENT_DAYS);
    // rows as the tables hold them (ctid): the order Postgres's scans
    // return equal keys in, which the ties below follow
    let cand = match c
        .query(
            "SELECT airframe_id FROM airframe_spells WHERE kind = 'operator' AND value = $1 AND last_date >= $2 ORDER BY ctid",
            &[&icao, &cutoff],
        )
        .await
    {
        Ok(r) => r,
        Err(e) => return internal(&e),
    };
    let mut ids: Vec<i64> = vec![];
    for r in &cand {
        let id: i64 = r.get(0);
        if !ids.contains(&id) {
            ids.push(id);
        }
    }
    if ids.is_empty() {
        return ApiError::new(404, "not_observed", "no airframes").into_response();
    }
    let mut spells: std::collections::HashMap<i64, Vec<Spell>> = std::collections::HashMap::new();
    let rows = match c
        .query(
            "SELECT airframe_id, kind, value, first_date, last_date FROM airframe_spells WHERE airframe_id = ANY($1) ORDER BY ctid",
            &[&ids],
        )
        .await
    {
        Ok(r) => r,
        Err(e) => return internal(&e),
    };
    for r in rows {
        spells.entry(r.get(0)).or_default().push(Spell { kind: r.get(1), value: r.get(2), first: r.get(3), last: r.get(4) });
    }
    let mut frames: std::collections::HashMap<i64, FrameRow> = std::collections::HashMap::new();
    match c.query("SELECT id, type_code, built_year, msn, first_observed FROM airframes WHERE id = ANY($1)", &[&ids]).await {
        Ok(rows) => {
            for r in rows {
                frames.insert(r.get(0), (r.get(1), r.get(2), r.get(3), r.get(4)));
            }
        }
        Err(e) => return internal(&e),
    }
    let mut notable: std::collections::HashMap<i64, i64> = std::collections::HashMap::new();
    match c
        .query(
            "SELECT airframe_id, count(*) FROM airframe_events WHERE airframe_id = ANY($1) AND visibility = 'public' \
             AND kind IN ('squawk', 'occurrence') GROUP BY airframe_id",
            &[&ids],
        )
        .await
    {
        Ok(rows) => {
            for r in rows {
                notable.insert(r.get(0), r.get(1));
            }
        }
        Err(e) => return internal(&e),
    }
    drop(g);
    struct Item {
        hex: Option<String>,
        reg: Option<String>,
        ty: Option<String>,
        built_year: Option<i64>,
        msn: Option<String>,
        since: Option<NaiveDate>,
        since_first_seen: bool,
        last_seen: Option<NaiveDate>,
        airlines: usize,
        notable: i64,
    }
    let empty = vec![];
    let mut items = vec![];
    for id in &ids {
        let mine = spells.get(id).unwrap_or(&empty);
        let Some(current) = latest(mine, "operator").filter(|s| s.value == icao) else {
            continue; // moved on to another airline
        };
        let (hx, reg) = (latest(mine, "hex"), latest(mine, "registration"));
        let frame = frames.get(id);
        let first_observed = frame.and_then(|f| f.3);
        let operators: std::collections::HashSet<&str> =
            mine.iter().filter(|s| s.kind == "operator").map(|s| s.value.as_str()).collect();
        items.push(Item {
            hex: hx.map(|s| s.value.clone()),
            reg: reg.map(|s| s.value.clone()),
            ty: frame.and_then(|f| f.0.clone()),
            built_year: frame.and_then(|f| f.1).map(i64::from),
            msn: frame.and_then(|f| f.2.clone()),
            since: current.first,
            since_first_seen: first_observed.is_some() && current.first.is_some() && current.first == first_observed,
            last_seen: current.last,
            airlines: operators.len(),
            notable: notable.get(id).copied().unwrap_or(0),
        });
    }
    // registration, type and build year from the registry where the
    // record's own spells are silent
    let hexes: Vec<String> = items.iter().filter_map(|i| i.hex.clone()).collect();
    let a = app.clone();
    let reg_rows = tokio::task::spawn_blocking(move || -> rusqlite::Result<std::collections::HashMap<String, RegistryRow>> {
        let c = a.refdb.conn()?;
        let mut stmt = c.prepare_cached("SELECT registration, type_code, year FROM ref_airframes WHERE hex = ?1")?;
        let mut out = std::collections::HashMap::new();
        for h in hexes {
            let mut rows = stmt.query([&h])?;
            if let Some(r) = rows.next()? {
                out.insert(h, (r.get(0)?, r.get(1)?, r.get(2)?));
            }
        }
        Ok(out)
    })
    .await;
    let reg_rows = match reg_rows {
        Ok(Ok(r)) => r,
        Ok(Err(e)) => return internal(&e),
        Err(e) => return internal(&e),
    };
    let mut kept = vec![];
    for mut it in items {
        let (r, t, y) = it.hex.as_ref().and_then(|h| reg_rows.get(h)).cloned().unwrap_or((None, None, None));
        if it.reg.as_deref().is_none_or(str::is_empty) {
            it.reg = r;
        }
        if it.ty.as_deref().is_none_or(str::is_empty) {
            it.ty = t;
        }
        if it.built_year.is_none() {
            it.built_year = y;
        }
        // no registration anywhere: a mis-decoded or anonymous address
        if it.reg.as_deref().is_none_or(str::is_empty) {
            continue;
        }
        kept.push(it);
    }
    kept.sort_by(|a, b| a.reg.cmp(&b.reg));
    let list: Vec<Value> = kept
        .into_iter()
        .map(|it| {
            let mut m = Map::new();
            m.insert("hex".into(), text(it.hex.clone()));
            m.insert("reg".into(), text(it.reg));
            m.insert("type".into(), text(it.ty));
            m.insert("built_year".into(), it.built_year.map_or(Value::Null, Value::from));
            m.insert("msn".into(), text(it.msn));
            m.insert("since".into(), day(it.since));
            m.insert("since_first_seen".into(), Value::Bool(it.since_first_seen));
            m.insert("last_seen".into(), day(it.last_seen));
            m.insert("airlines".into(), Value::from(it.airlines));
            m.insert("notable".into(), Value::from(it.notable));
            m.insert(
                "country".into(),
                it.hex.as_deref().and_then(crate::address_blocks::state_of).map_or(Value::Null, |s| s.into()),
            );
            Value::Object(m)
        })
        .collect();
    let mut m = Map::new();
    m.insert("icao".into(), Value::String(icao));
    m.insert("as_of".into(), day(Some(newest)));
    m.insert("current_days".into(), Value::from(CURRENT_DAYS));
    m.insert("airframes".into(), Value::Array(list));
    let mut out = String::with_capacity(65536);
    write_value(&mut out, &Value::Object(m));
    json(out, CACHE)
}
