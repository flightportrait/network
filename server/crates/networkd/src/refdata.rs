//! Reference routes served from the nightly snapshot (refdb): airlines
//! and alliances. Ports of the Python service's `routes_refdata.py`,
//! byte for byte; each response is computed once per snapshot. Without a
//! snapshot the request goes to the fallback, as it did before.

use std::collections::{BTreeMap, HashMap, HashSet};
use std::sync::Arc;

use axum::extract::{Request, State};
use axum::response::{IntoResponse, Response};
use bytes::Bytes;
use rusqlite::{params_from_iter, OptionalExtension};

use crate::http::{client_ip, json, peer_of, throttle, ApiError};
use crate::pyjson::{write_str, write_value, Obj};
use crate::refdb::Conn;
use crate::state::App;

const CACHE: &str = "public, s-maxage=3600";

/// What a handler computes from the snapshot: a body worth caching, or
/// an error envelope.
type Built = Result<String, ApiError>;

/// Serve `key` from the snapshot: cached if already computed, else
/// computed by `build` off the async threads; forwarded when there is no
/// snapshot.
async fn serve<F>(app: Arc<App>, req: Request, needs: &[&str], key: String, build: F) -> Response
where
    F: FnOnce(&Conn) -> rusqlite::Result<Built> + Send + 'static,
{
    if !app.refdb.has(needs) {
        return crate::proxy::forward(State(app), req).await;
    }
    let ip = client_ip(&app, req.headers(), peer_of(&req));
    if let Err(e) = throttle(&app, &ip, "refdata", app.settings.refdata_rate_limit) {
        return e.into_response();
    }
    if let Some(body) = app.refdb.cached(&key) {
        return json(body, CACHE);
    }
    let db = app.refdb.clone();
    let done = tokio::task::spawn_blocking(move || -> rusqlite::Result<(u64, Built)> {
        let conn = db.conn()?;
        Ok((conn.generation(), build(&conn)?))
    })
    .await;
    match done {
        Ok(Ok((generation, Ok(body)))) => {
            let body = Bytes::from(body);
            app.refdb.remember(key, generation, body.clone());
            json(body, CACHE)
        }
        Ok(Ok((_, Err(api)))) => api.into_response(),
        Ok(Err(e)) => {
            eprintln!("refdata {key}: {e}");
            ApiError::new(500, "internal_error", "internal error").into_response()
        }
        Err(e) => {
            eprintln!("refdata {key}: {e}");
            ApiError::new(500, "internal_error", "internal error").into_response()
        }
    }
}

fn opt_str(out: &mut String, v: &Option<String>) {
    match v {
        Some(s) => write_str(out, s),
        None => out.push_str("null"),
    }
}

/// A JSON column as the Python service returns it: parsed, re-encoded
/// compactly; `or_empty`: None and JSON null become `[]`.
fn json_col(out: &mut String, raw: &Option<String>, or_empty: bool) {
    let v = raw.as_deref().and_then(|r| serde_json::from_str::<serde_json::Value>(r).ok());
    match v {
        Some(v) if !(or_empty && (v.is_null() || v.as_array().is_some_and(|a| a.is_empty()))) => {
            write_value(out, &v)
        }
        _ if or_empty => out.push_str("[]"),
        _ => out.push_str("null"),
    }
}

// ---- memberships ----------------------------------------------------------

struct Membership {
    status: String,
    name: String,
    json: String,
}

/// airline icao -> its alliance memberships, active first, then by name.
fn memberships_by_airline(c: &Conn, only: Option<&str>) -> rusqlite::Result<HashMap<String, Vec<Membership>>> {
    let mut sql = String::from(
        "SELECT m.airline_icao, a.slug, a.name, m.status, m.relationship, m.sponsor_icao, \
         m.effective_from, m.effective_to, m.source_url, m.source_checked_at \
         FROM ref_alliance_memberships m JOIN ref_alliances a ON a.slug = m.alliance_slug",
    );
    if only.is_some() {
        sql.push_str(" WHERE m.airline_icao = ?1");
    }
    let mut stmt = c.prepare_cached(&sql)?;
    let mut rows = match only {
        Some(icao) => stmt.query([icao])?,
        None => stmt.query([])?,
    };
    let mut out: HashMap<String, Vec<Membership>> = HashMap::new();
    while let Some(r) = rows.next()? {
        let airline: String = r.get(0)?;
        let (slug, name, status, relationship): (String, String, String, String) =
            (r.get(1)?, r.get(2)?, r.get(3)?, r.get(4)?);
        let mut s = String::new();
        let mut o = Obj::new(&mut s);
        o.str("slug", &slug).str("name", &name).str("status", &status).str("relationship", &relationship);
        opt_str(o.key("sponsor_icao"), &r.get(5)?);
        opt_str(o.key("effective_from"), &r.get(6)?);
        opt_str(o.key("effective_to"), &r.get(7)?);
        opt_str(o.key("source_url"), &r.get(8)?);
        opt_str(o.key("source_checked_at"), &r.get(9)?);
        o.end();
        out.entry(airline).or_default().push(Membership { status, name, json: s });
    }
    for list in out.values_mut() {
        // Python: sort(key=(status != "active", name)); str order is code
        // point order, which is UTF-8 byte order
        list.sort_by(|a, b| (a.status != "active", &a.name).cmp(&(b.status != "active", &b.name)));
    }
    Ok(out)
}

/// One ref_airlines row: icao, iata, name, palette (JSON text).
struct AirlineRow {
    icao: String,
    iata: Option<String>,
    name: String,
    palette: Option<String>,
}

impl AirlineRow {
    fn read(r: &rusqlite::Row) -> rusqlite::Result<AirlineRow> {
        Ok(AirlineRow { icao: r.get(0)?, iata: r.get(1)?, name: r.get(2)?, palette: r.get(3)? })
    }
}

/// `_serialize_airline` plus the two counts.
fn write_airline(out: &mut String, a: &AirlineRow, memberships: Option<&Vec<Membership>>, counts: (i64, i64)) {
    let mut o = Obj::new(out);
    o.str("icao", &a.icao);
    opt_str(o.key("iata"), &a.iata);
    o.str("name", &a.name);
    json_col(o.key("palette"), &a.palette, true);
    let buf = o.key("alliances");
    buf.push('[');
    for (i, m) in memberships.into_iter().flatten().enumerate() {
        if i > 0 {
            buf.push(',');
        }
        buf.push_str(&m.json);
    }
    buf.push(']');
    o.int("n_routes", counts.0).int("n_countries", counts.1);
    o.end();
}

fn counts(c: &Conn, sql: &str) -> rusqlite::Result<HashMap<String, i64>> {
    let mut stmt = c.prepare_cached(sql)?;
    let rows = stmt.query_map([], |r| Ok((r.get::<_, String>(0)?, r.get::<_, i64>(1)?)))?;
    rows.collect()
}

// ---- routes ---------------------------------------------------------------

pub async fn airlines(State(app): State<Arc<App>>, req: Request) -> Response {
    serve(app, req, &["ref_airlines", "rank_ref_airlines_icao", "ref_alliance_memberships", "ref_routes", "ref_airline_countries"], "airlines".into(), |c| {
        let memberships = memberships_by_airline(c, None)?;
        let routes = counts(c, "SELECT airline_icao, COUNT(*) FROM ref_routes WHERE airline_icao IS NOT NULL GROUP BY airline_icao")?;
        let countries = counts(c, "SELECT airline_icao, COUNT(*) FROM ref_airline_countries GROUP BY airline_icao")?;
        let mut stmt = c.prepare_cached(
            "SELECT a.icao, a.iata, a.name, a.palette FROM ref_airlines a \
             JOIN rank_ref_airlines_icao r ON r.key = a.icao ORDER BY r.rank",
        )?;
        let mut rows = stmt.query([])?;
        let mut out = String::with_capacity(96 * 1024);
        out.push_str("{\"airlines\":[");
        let mut first = true;
        while let Some(r) = rows.next()? {
            if !first {
                out.push(',');
            }
            first = false;
            let a = AirlineRow::read(r)?;
            let n = (*routes.get(&a.icao).unwrap_or(&0), *countries.get(&a.icao).unwrap_or(&0));
            write_airline(&mut out, &a, memberships.get(&a.icao), n);
        }
        out.push_str("]}");
        Ok(Ok(out))
    })
    .await
}

pub async fn airline(State(app): State<Arc<App>>, axum::extract::Path(icao): axum::extract::Path<String>, req: Request) -> Response {
    let code = icao.trim().to_uppercase();
    serve(app, req, &["ref_airlines", "ref_alliance_memberships", "ref_routes", "ref_airline_countries"], format!("airline:{code}"), move |c| {
        let row = c
            .prepare_cached("SELECT icao, iata, name, palette FROM ref_airlines WHERE icao = ?1")?
            .query_row([&code], AirlineRow::read)
            .optional()?;
        let Some(a) = row else {
            return Ok(Err(ApiError::new(404, "not_found", "unknown airline")));
        };
        let memberships = memberships_by_airline(c, Some(&a.icao))?;
        let n = |sql: &str| c.prepare_cached(sql)?.query_row([&a.icao], |r| r.get::<_, i64>(0));
        let counts = (
            n("SELECT COUNT(*) FROM ref_routes WHERE airline_icao = ?1")?,
            n("SELECT COUNT(*) FROM ref_airline_countries WHERE airline_icao = ?1")?,
        );
        let mut out = String::with_capacity(512);
        write_airline(&mut out, &a, memberships.get(&a.icao), counts);
        Ok(Ok(out))
    })
    .await
}

/// `_alliance_summary`: official member counts, observed coverage.
fn alliance_summary(c: &Conn, out: &mut String, slug: &str) -> rusqlite::Result<()> {
    let (name, website, logo, source, checked): (String, String, Option<String>, String, String) = c
        .prepare_cached("SELECT name, website_url, logo_asset_url, source_url, source_checked_at FROM ref_alliances WHERE slug = ?1")?
        .query_row([slug], |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?, r.get(3)?, r.get(4)?)))?;
    let mut direct = HashSet::new();
    let mut codes = BTreeMap::new();
    let mut stmt = c.prepare_cached(
        "SELECT airline_icao, relationship, status FROM ref_alliance_memberships WHERE alliance_slug = ?1",
    )?;
    let mut rows = stmt.query([slug])?;
    while let Some(r) = rows.next()? {
        let (icao, rel, status): (String, String, String) = (r.get(0)?, r.get(1)?, r.get(2)?);
        if status == "active" {
            if rel == "member" {
                direct.insert(icao.clone());
            }
            if rel == "member" || rel == "group-brand" {
                codes.insert(icao, ());
            }
        }
    }
    let codes: Vec<String> = codes.into_keys().collect();
    let (mut routes, mut countries, mut legs) = (HashMap::new(), HashSet::new(), HashSet::new());
    let (mut n_flights, mut n_airframes) = (0i64, 0i64);
    if !codes.is_empty() {
        let list = vec!["?"; codes.len()].join(",");
        let mut s = c.prepare(&format!(
            "SELECT airline_icao, COUNT(*) FROM ref_routes WHERE airline_icao IN ({list}) GROUP BY airline_icao"
        ))?;
        let mut r = s.query(params_from_iter(&codes))?;
        while let Some(row) = r.next()? {
            routes.insert(row.get::<_, String>(0)?, row.get::<_, i64>(1)?);
        }
        let mut s = c.prepare(&format!("SELECT iso_country FROM ref_airline_countries WHERE airline_icao IN ({list})"))?;
        let mut r = s.query(params_from_iter(&codes))?;
        while let Some(row) = r.next()? {
            countries.insert(row.get::<_, Option<String>>(0)?);
        }
        let mut s = c.prepare(&format!("SELECT o, d, n_flights FROM ref_leg_stats WHERE airline_icao IN ({list})"))?;
        let mut r = s.query(params_from_iter(&codes))?;
        while let Some(row) = r.next()? {
            legs.insert((row.get::<_, Option<String>>(0)?, row.get::<_, Option<String>>(1)?));
            n_flights += row.get::<_, Option<i64>>(2)?.unwrap_or(0);
        }
        n_airframes = c
            .prepare(&format!("SELECT COUNT(*) FROM ref_airframes WHERE operator_icao IN ({list})"))?
            .query_row(params_from_iter(&codes), |r| r.get(0))?;
    }
    let observed = direct.iter().filter(|d| routes.get(*d).is_some_and(|n| *n > 0)).count();
    let mut o = Obj::new(out);
    o.str("slug", slug).str("name", &name).str("website_url", &website);
    opt_str(o.key("logo_asset_url"), &logo);
    o.str("source_url", &source).str("source_checked_at", &checked);
    o.int("n_members", direct.len() as i64)
        .int("n_members_observed", observed as i64)
        .int("n_airlines", codes.len() as i64)
        .int("n_routes", routes.values().sum())
        .int("n_legs", legs.len() as i64)
        .int("n_flights", n_flights)
        .int("n_countries", countries.len() as i64)
        .int("n_airframes", n_airframes);
    o.end();
    Ok(())
}

pub async fn alliances(State(app): State<Arc<App>>, req: Request) -> Response {
    serve(app, req, &["ref_alliances", "rank_ref_alliances_name", "ref_alliance_memberships", "ref_leg_stats", "ref_airframes"], "alliances".into(), |c| {
        let slugs: Vec<String> = c
            .prepare_cached("SELECT a.slug FROM ref_alliances a JOIN rank_ref_alliances_name r ON r.key = a.slug ORDER BY r.rank")?
            .query_map([], |r| r.get(0))?
            .collect::<rusqlite::Result<_>>()?;
        let mut out = String::from("{\"alliances\":[");
        for (i, slug) in slugs.iter().enumerate() {
            if i > 0 {
                out.push(',');
            }
            alliance_summary(c, &mut out, slug)?;
        }
        out.push_str("]}");
        Ok(Ok(out))
    })
    .await
}
