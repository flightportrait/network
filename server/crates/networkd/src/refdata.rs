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

/// `_serialize_airline`, plus the two counts where the route adds them.
fn write_airline(out: &mut String, a: &AirlineRow, memberships: Option<&Vec<Membership>>, counts: Option<(i64, i64)>) {
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
    if let Some(counts) = counts {
        o.int("n_routes", counts.0).int("n_countries", counts.1);
    }
    o.end();
}

/// One airline as `_serialize_airline` writes it: (name, JSON), or None
/// when the registry does not know the code.
pub fn airline_object(c: &Conn, icao: &str) -> rusqlite::Result<Option<(String, String)>> {
    let row = c
        .prepare_cached("SELECT icao, iata, name, palette FROM ref_airlines WHERE icao = ?1")?
        .query_row([icao], AirlineRow::read)
        .optional()?;
    let Some(a) = row else { return Ok(None) };
    let memberships = memberships_by_airline(c, Some(&a.icao))?;
    let mut out = String::with_capacity(256);
    write_airline(&mut out, &a, memberships.get(&a.icao), None);
    Ok(Some((a.name, out)))
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
            write_airline(&mut out, &a, memberships.get(&a.icao), Some(n));
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
        write_airline(&mut out, &a, memberships.get(&a.icao), Some(counts));
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

// ---- airline sub-pages, types ---------------------------------------------

fn airline_or_404(c: &Conn, icao: &str) -> rusqlite::Result<Result<AirlineRow, ApiError>> {
    let row = c
        .prepare_cached("SELECT icao, iata, name, palette FROM ref_airlines WHERE icao = ?1")?
        .query_row([icao.trim().to_uppercase()], AirlineRow::read)
        .optional()?;
    Ok(row.ok_or_else(|| ApiError::new(404, "not_found", "unknown airline")))
}

/// {designator: name} for the given type codes.
fn type_names<'a>(c: &Conn, codes: impl Iterator<Item = &'a str>) -> rusqlite::Result<HashMap<String, Option<String>>> {
    let codes: Vec<&str> = {
        let mut v: Vec<&str> = codes.collect();
        v.sort_unstable();
        v.dedup();
        v
    };
    let mut out = HashMap::new();
    for chunk in codes.chunks(500) {
        let list = vec!["?"; chunk.len()].join(",");
        let mut s = c.prepare(&format!("SELECT designator, name FROM ref_types WHERE designator IN ({list})"))?;
        let mut rows = s.query(params_from_iter(chunk))?;
        while let Some(r) = rows.next()? {
            out.insert(r.get::<_, String>(0)?, r.get::<_, Option<String>>(1)?);
        }
    }
    Ok(out)
}

fn json_value(raw: &Option<String>) -> Option<serde_json::Value> {
    raw.as_deref().and_then(|r| serde_json::from_str(r).ok())
}

fn write_json(out: &mut String, v: &serde_json::Value) {
    crate::pyjson::Val::from_json(v).write(out)
}

fn opt_i(out: &mut String, v: Option<i64>) {
    match v {
        Some(i) => out.push_str(&i.to_string()),
        None => out.push_str("null"),
    }
}

fn opt_hhmm(out: &mut String, v: Option<i64>) {
    match v {
        Some(m) => write_str(out, &crate::boards::hhmm(m)),
        None => out.push_str("null"),
    }
}

/// `_resolve_airports`: {code: {lat, lon, name, iso_country, tz}} in the
/// order the table yields them, the first airport winning a shared code.
fn resolve_airports(c: &Conn, codes: &HashSet<String>) -> rusqlite::Result<(Vec<String>, HashMap<String, String>)> {
    let mut order = vec![];
    let mut found: HashMap<String, String> = HashMap::new();
    let list: Vec<&String> = codes.iter().filter(|c| !c.is_empty()).collect();
    if list.is_empty() {
        return Ok((order, found));
    }
    let marks = vec!["?"; list.len()].join(",");
    let mut s = c.prepare(&format!(
        "SELECT iata, ident, lat, lon, name, iso_country, tz FROM ref_airports \
         WHERE (iata IN ({marks}) OR ident IN ({marks})) AND lat IS NOT NULL ORDER BY rowid"
    ))?;
    let params: Vec<&String> = list.iter().chain(list.iter()).copied().collect();
    let mut rows = s.query(params_from_iter(params))?;
    while let Some(r) = rows.next()? {
        let (iata, ident): (Option<String>, String) = (r.get(0)?, r.get(1)?);
        for code in [iata, Some(ident)].into_iter().flatten() {
            if codes.contains(&code) && !found.contains_key(&code) {
                let mut s = String::new();
                let mut o = Obj::new(&mut s);
                let lat: Option<f64> = r.get(2)?;
                let lon: Option<f64> = r.get(3)?;
                match lat {
                    Some(x) => o.f64("lat", x),
                    None => o.null("lat"),
                };
                match lon {
                    Some(x) => o.f64("lon", x),
                    None => o.null("lon"),
                };
                opt_str(o.key("name"), &r.get(4)?);
                opt_str(o.key("iso_country"), &r.get(5)?);
                opt_str(o.key("tz"), &r.get(6)?);
                o.end();
                order.push(code.clone());
                found.insert(code, s);
            }
        }
    }
    Ok((order, found))
}

pub async fn airline_routes(State(app): State<Arc<App>>, axum::extract::Path(icao): axum::extract::Path<String>, req: Request) -> Response {
    let code = icao.trim().to_uppercase();
    serve(app, req, &["ref_airlines", "ref_leg_stats", "ref_routes", "ref_types", "ref_airports"], format!("airline_routes:{code}"), move |c| {
        let a = match airline_or_404(c, &code)? {
            Ok(a) => a,
            Err(e) => return Ok(Err(e)),
        };
        // legs as (org, dst, n, json)
        let mut legs: Vec<(String, String, i64, String)> = vec![];
        let mut codes = HashSet::new();
        type Stat = (String, String, i64, i64, f64, Option<i64>, Option<String>);
        let stats: Vec<Stat> = c
            .prepare_cached(
                "SELECT o, d, n_flights, n_days, per_week, avg_min, types FROM ref_leg_stats \
                 WHERE airline_icao = ?1 ORDER BY rowid",
            )?
            .query_map([&a.icao], |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?, r.get(3)?, r.get(4)?, r.get(5)?, r.get(6)?)))?
            .collect::<rusqlite::Result<_>>()?;
        let source = if stats.is_empty() { "chains" } else { "flightlog" };
        if !stats.is_empty() {
            let parsed: Vec<Vec<serde_json::Value>> = stats
                .iter()
                .map(|s| json_value(&s.6).and_then(|v| v.as_array().cloned()).unwrap_or_default())
                .collect();
            let names = type_names(c, parsed.iter().flatten().filter_map(|p| p.get(0).and_then(|t| t.as_str())))?;
            for (s, types) in stats.iter().zip(&parsed) {
                codes.insert(s.0.clone());
                codes.insert(s.1.clone());
                let mut j = String::new();
                let mut o = Obj::new(&mut j);
                o.str("org", &s.0).str("dst", &s.1).int("n", s.2).int("days", s.3).f64("per_week", s.4);
                opt_i(o.key("avg_min"), s.5);
                let buf = o.key("aircraft");
                buf.push('[');
                for (k, p) in types.iter().enumerate() {
                    if k > 0 {
                        buf.push(',');
                    }
                    let t = p.get(0).cloned().unwrap_or(serde_json::Value::Null);
                    let mut po = Obj::new(buf);
                    write_json(po.key("type"), &t);
                    let name = t.as_str().and_then(|t| names.get(t).cloned().flatten());
                    opt_str(po.key("name"), &name);
                    write_json(po.key("n"), &p.get(1).cloned().unwrap_or(serde_json::Value::Null));
                    po.end();
                }
                buf.push(']');
                o.end();
                legs.push((s.0.clone(), s.1.clone(), s.2, j));
            }
        } else {
            let mut agg: Vec<((String, String), i64)> = vec![];
            let mut at: HashMap<(String, String), usize> = HashMap::new();
            let chains: Vec<Option<String>> = c
                .prepare_cached("SELECT chain FROM ref_routes WHERE airline_icao = ?1 ORDER BY rowid")?
                .query_map([&a.icao], |r| r.get(0))?
                .collect::<rusqlite::Result<_>>()?;
            for chain in chains {
                let chain: Vec<Option<String>> = json_value(&chain)
                    .and_then(|v| v.as_array().cloned())
                    .unwrap_or_default()
                    .iter()
                    .map(|x| x.as_str().map(str::to_string))
                    .collect();
                for w in chain.windows(2) {
                    let (Some(x), Some(y)) = (&w[0], &w[1]) else { continue };
                    if x.is_empty() || y.is_empty() || x == y {
                        continue;
                    }
                    let key = if x < y { (x.clone(), y.clone()) } else { (y.clone(), x.clone()) };
                    match at.get(&key) {
                        Some(&k) => agg[k].1 += 1,
                        None => {
                            at.insert(key.clone(), agg.len());
                            agg.push((key, 1));
                        }
                    }
                }
            }
            for ((x, y), n) in agg {
                codes.insert(x.clone());
                codes.insert(y.clone());
                let mut j = String::new();
                let mut o = Obj::new(&mut j);
                o.str("org", &x).str("dst", &y).int("n", n).null("per_week").null("aircraft");
                o.end();
                legs.push((x, y, n, j));
            }
        }
        let (order, airports) = resolve_airports(c, &codes)?;
        legs.retain(|l| airports.contains_key(&l.0) && airports.contains_key(&l.1));
        legs.sort_by_key(|l| -l.2);
        let mut out = String::with_capacity(64 * 1024);
        let mut o = Obj::new(&mut out);
        o.str("icao", &a.icao).str("source", source);
        let buf = o.key("airports");
        buf.push('{');
        for (k, code) in order.iter().enumerate() {
            if k > 0 {
                buf.push(',');
            }
            write_str(buf, code);
            buf.push(':');
            buf.push_str(&airports[code]);
        }
        buf.push('}');
        let buf = o.key("legs");
        buf.push('[');
        for (k, l) in legs.iter().enumerate() {
            if k > 0 {
                buf.push(',');
            }
            buf.push_str(&l.3);
        }
        buf.push(']');
        o.end();
        Ok(Ok(out))
    })
    .await
}

pub async fn airline_leg(
    State(app): State<Arc<App>>,
    axum::extract::Path((icao, org, dst)): axum::extract::Path<(String, String, String)>,
    req: Request,
) -> Response {
    let code = icao.trim().to_uppercase();
    let (mut o1, mut d1) = (org.trim().to_uppercase(), dst.trim().to_uppercase());
    if o1 > d1 {
        std::mem::swap(&mut o1, &mut d1);
    }
    serve(app, req, &["ref_airlines", "ref_leg_stats", "ref_airframes"], format!("airline_leg:{code}:{o1}:{d1}"), move |c| {
        let a = match airline_or_404(c, &code)? {
            Ok(a) => a,
            Err(e) => return Ok(Err(e)),
        };
        let st: Option<(i64, i64, Option<String>)> = c
            .prepare_cached("SELECT n_flights, n_days, airframes FROM ref_leg_stats WHERE airline_icao = ?1 AND o = ?2 AND d = ?3")?
            .query_row((&a.icao, &o1, &d1), |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?)))
            .optional()?;
        let Some((flights, days, frames)) = st else {
            return Ok(Err(ApiError::new(404, "not_observed", "unknown leg")));
        };
        let mut out = String::with_capacity(4096);
        let mut o = Obj::new(&mut out);
        o.str("icao", &a.icao).str("org", &o1).str("dst", &d1).int("flights", flights).int("days", days);
        let buf = o.key("airframes");
        buf.push('[');
        let entries = json_value(&frames).and_then(|v| v.as_array().cloned()).unwrap_or_default();
        let mut stmt = c.prepare_cached("SELECT registration, type_code FROM ref_airframes WHERE hex = ?1")?;
        for (k, e) in entries.iter().enumerate() {
            if k > 0 {
                buf.push(',');
            }
            let hex = e.get(0).cloned().unwrap_or(serde_json::Value::Null);
            let af: Option<(Option<String>, Option<String>)> = match hex.as_str() {
                Some(h) => stmt.query_row([h], |r| Ok((r.get(0)?, r.get(1)?))).optional()?,
                None => None,
            };
            let mut fo = Obj::new(buf);
            write_json(fo.key("hex"), &hex);
            opt_str(fo.key("reg"), &af.as_ref().and_then(|a| a.0.clone()));
            opt_str(fo.key("type"), &af.as_ref().and_then(|a| a.1.clone()));
            match e.as_array().filter(|a| a.len() > 1) {
                Some(a) => write_json(fo.key("flights"), &a[1]),
                None => {
                    fo.null("flights");
                }
            }
            fo.end();
        }
        buf.push(']');
        o.end();
        Ok(Ok(out))
    })
    .await
}

pub async fn airline_schedule(
    State(app): State<Arc<App>>,
    axum::extract::Path((icao, org, dst)): axum::extract::Path<(String, String, String)>,
    req: Request,
) -> Response {
    let code = icao.trim().to_uppercase();
    let (o1, d1) = (org.trim().to_uppercase(), dst.trim().to_uppercase());
    let min = app.settings.schedule_min_flights;
    serve(app, req, &["ref_airlines", "ref_schedule", "ref_types"], format!("airline_schedule:{code}:{o1}:{d1}"), move |c| {
        let a = match airline_or_404(c, &code)? {
            Ok(a) => a,
            Err(e) => return Ok(Err(e)),
        };
        type Row = (String, Option<String>, String, String, String, Option<i64>, Option<i64>, Option<String>, i64);
        let rows: Vec<Row> = c
            .prepare_cached(
                "SELECT callsign, flight, source, org, dst, dep_min, arr_min, type_code, n_flights FROM ref_schedule \
                 WHERE airline_icao = ?1 AND n_flights >= ?2 AND ((org = ?3 AND dst = ?4) OR (org = ?4 AND dst = ?3)) \
                 ORDER BY rowid",
            )?
            .query_map((&a.icao, min, &o1, &d1), |r| {
                Ok((r.get(0)?, r.get(1)?, r.get(2)?, r.get(3)?, r.get(4)?, r.get(5)?, r.get(6)?, r.get(7)?, r.get(8)?))
            })?
            .collect::<rusqlite::Result<_>>()?;
        // ORDER BY org, dep_min (NULLs last), as Postgres sorts the rows
        // its scan yields: ties land where they land there
        let mut rows = rows;
        crate::pgsort::sort(&mut rows, &|x: &Row, y: &Row| {
            x.3.cmp(&y.3).then_with(|| match (x.5, y.5) {
                (Some(p), Some(q)) => p.cmp(&q),
                (None, None) => std::cmp::Ordering::Equal,
                (None, _) => std::cmp::Ordering::Greater,
                (_, None) => std::cmp::Ordering::Less,
            })
        });
        let names = type_names(c, rows.iter().filter_map(|r| r.7.as_deref()).filter(|t| !t.is_empty()))?;
        let mut out = String::with_capacity(8192);
        let mut o = Obj::new(&mut out);
        o.str("icao", &a.icao).str("org", &o1).str("dst", &d1);
        let buf = o.key("departures");
        buf.push('[');
        for (k, r) in rows.iter().enumerate() {
            if k > 0 {
                buf.push(',');
            }
            // IATA form of the number when the airline has one (EK1)
            let num: String = if r.0.chars().count() > 3 {
                r.0.chars().skip(3).collect::<String>().trim_start_matches('0').to_string()
            } else {
                String::new()
            };
            let flight = match r.1.as_deref() {
                Some(f) if !f.is_empty() => f.to_string(),
                _ => match a.iata.as_deref() {
                    Some(i) if !i.is_empty() && !num.is_empty() => format!("{i}{num}"),
                    _ => r.0.clone(),
                },
            };
            let mut ro = Obj::new(buf);
            ro.str("callsign", &r.0).str("flight", &flight).str("source", &r.2).str("org", &r.3).str("dst", &r.4);
            opt_hhmm(ro.key("dep"), r.5);
            opt_i(ro.key("dep_min"), r.5);
            opt_hhmm(ro.key("arr"), r.6);
            opt_i(ro.key("arr_min"), r.6);
            opt_str(ro.key("type"), &r.7);
            let tn = r.7.as_ref().and_then(|t| names.get(t).cloned().flatten());
            opt_str(ro.key("type_name"), &tn);
            ro.int("n_flights", r.8);
            ro.end();
        }
        buf.push(']');
        o.end();
        Ok(Ok(out))
    })
    .await
}

pub async fn airline_countries(State(app): State<Arc<App>>, axum::extract::Path(icao): axum::extract::Path<String>, req: Request) -> Response {
    let code = icao.trim().to_uppercase();
    serve(app, req, &["ref_airlines", "ref_airline_countries"], format!("airline_countries:{code}"), move |c| {
        let a = match airline_or_404(c, &code)? {
            Ok(a) => a,
            Err(e) => return Ok(Err(e)),
        };
        let mut out = String::with_capacity(2048);
        let mut o = Obj::new(&mut out);
        o.str("icao", &a.icao);
        let buf = o.key("countries");
        buf.push('[');
        let mut rows: Vec<(Option<String>, Option<i64>)> = c
            .prepare_cached("SELECT iso_country, n_routes FROM ref_airline_countries WHERE airline_icao = ?1 ORDER BY rowid")?
            .query_map([&a.icao], |r| Ok((r.get(0)?, r.get(1)?)))?
            .collect::<rusqlite::Result<_>>()?;
        // ORDER BY n_routes DESC (Postgres: NULLs first), ties as it sorts them
        crate::pgsort::sort(&mut rows, &|x: &(Option<String>, Option<i64>), y: &(Option<String>, Option<i64>)| match (x.1, y.1) {
            (Some(p), Some(q)) => q.cmp(&p),
            (None, None) => std::cmp::Ordering::Equal,
            (None, _) => std::cmp::Ordering::Less,
            (_, None) => std::cmp::Ordering::Greater,
        });
        for (k, (iso, n)) in rows.iter().enumerate() {
            if k > 0 {
                buf.push(',');
            }
            let mut co = Obj::new(buf);
            opt_str(co.key("iso_country"), iso);
            opt_i(co.key("n_routes"), *n);
            co.end();
        }
        buf.push(']');
        o.end();
        Ok(Ok(out))
    })
    .await
}

pub async fn airline_fleet(State(app): State<Arc<App>>, axum::extract::Path(icao): axum::extract::Path<String>, req: Request) -> Response {
    let code = icao.trim().to_uppercase();
    serve(app, req, &["ref_airlines", "ref_airframes", "ref_types"], format!("airline_fleet:{code}"), move |c| {
        let a = match airline_or_404(c, &code)? {
            Ok(a) => a,
            Err(e) => return Ok(Err(e)),
        };
        let rows: Vec<(Option<String>, i64)> = c
            .prepare_cached(
                "SELECT type_code, COUNT(*) FROM ref_airframes WHERE operator_icao = ?1 OR operator_norm = ?2 \
                 GROUP BY type_code ORDER BY 2 DESC, type_code IS NULL, type_code",
            )?
            .query_map((&a.icao, a.name.to_uppercase()), |r| Ok((r.get(0)?, r.get(1)?)))?
            .collect::<rusqlite::Result<_>>()?;
        let names = type_names(c, rows.iter().filter_map(|r| r.0.as_deref()).filter(|t| !t.is_empty()))?;
        let known = |t: &Option<String>| t.as_deref().is_some_and(|t| !t.is_empty());
        let mut out = String::with_capacity(4096);
        let mut o = Obj::new(&mut out);
        o.str("icao", &a.icao).int("n_airframes", rows.iter().map(|r| r.1).sum());
        let buf = o.key("fleet");
        buf.push('[');
        for (k, (t, n)) in rows.iter().filter(|r| known(&r.0)).enumerate() {
            if k > 0 {
                buf.push(',');
            }
            let mut fo = Obj::new(buf);
            opt_str(fo.key("type"), t);
            let tn = t.as_ref().and_then(|t| names.get(t).cloned().flatten());
            opt_str(fo.key("type_name"), &tn);
            fo.int("count", *n);
            fo.end();
        }
        buf.push(']');
        o.int("unknown_type", rows.iter().filter(|r| !known(&r.0)).map(|r| r.1).sum());
        o.end();
        Ok(Ok(out))
    })
    .await
}

pub async fn airline_fleet_type(
    State(app): State<Arc<App>>,
    axum::extract::Path((icao, designator)): axum::extract::Path<(String, String)>,
    req: Request,
) -> Response {
    let code = icao.trim().to_uppercase();
    let designator = designator.trim().to_uppercase();
    let legs = app.legs.clone();
    let stamp = crate::boards::mtime(&app.settings.legs_path).map(|t| format!("{t:?}")).unwrap_or_default();
    serve(
        app,
        req,
        &["ref_airlines", "ref_airframes", "ref_types", "rank_ref_airframes_registration"],
        format!("airline_fleet_type:{code}:{designator}:{stamp}"),
        move |c| {
            let a = match airline_or_404(c, &code)? {
                Ok(a) => a,
                Err(e) => return Ok(Err(e)),
            };
            // ORDER BY registration LIMIT 300 over the scan's storage order
            let all: Vec<(String, Option<String>, i64)> = c
                .prepare_cached(
                    "SELECT f.hex, f.registration, r.rank FROM ref_airframes f \
                     JOIN rank_ref_airframes_registration r ON r.key = f.hex \
                     WHERE (f.operator_icao = ?1 OR f.operator_norm = ?2) AND f.type_code = ?3 ORDER BY f.rowid",
                )?
                .query_map((&a.icao, a.name.to_uppercase(), &designator), |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?)))?
                .collect::<rusqlite::Result<_>>()?;
            let frames: Vec<(String, Option<String>)> =
                crate::pgsort::top_n(all, 300, &|x: &(String, Option<String>, i64), y: &(String, Option<String>, i64)| x.2.cmp(&y.2))
                    .into_iter()
                    .map(|(h, r, _)| (h, r))
                    .collect();
            if frames.is_empty() {
                return Ok(Err(ApiError::new(404, "not_observed", "no airframes")));
            }
            let name: Option<String> = c
                .prepare_cached("SELECT name FROM ref_types WHERE designator = ?1")?
                .query_row([&designator], |r| r.get(0))
                .optional()?
                .flatten();
            let summaries = legs
                .with_conn(|lc| {
                    frames.iter().map(|(h, _)| crate::legs::airframe_summary(lc, h)).collect::<rusqlite::Result<Vec<_>>>()
                })
                .unwrap_or_default();
            let window = if legs.available() { legs.window_days() } else { None };
            let mut out = String::with_capacity(frames.len() * 200 + 256);
            let mut o = Obj::new(&mut out);
            o.str("icao", &a.icao).str("type", &designator);
            opt_str(o.key("type_name"), &name);
            opt_i(o.key("window_days"), window);
            let buf = o.key("airframes");
            buf.push('[');
            for (k, (hex, reg)) in frames.iter().enumerate() {
                if k > 0 {
                    buf.push(',');
                }
                let s = summaries.get(k).and_then(|s| s.as_ref());
                let mut fo = Obj::new(buf);
                fo.str("hex", hex);
                opt_str(fo.key("reg"), reg);
                fo.int("legs", s.map(|s| s.legs).unwrap_or(0));
                opt_str(fo.key("last_date"), &s.and_then(|s| s.last_date.clone()));
                opt_str(fo.key("last_org"), &s.and_then(|s| s.last_org.clone()));
                opt_str(fo.key("last_dst"), &s.and_then(|s| s.last_dst.clone()));
                opt_str(fo.key("where"), &s.and_then(|s| s.where_.clone()));
                match s.and_then(|s| s.top_route.as_ref()) {
                    Some((org, dst, n)) => {
                        let b = fo.key("top_route");
                        b.push('[');
                        opt_str(b, org);
                        b.push(',');
                        opt_str(b, dst);
                        b.push(',');
                        b.push_str(&n.to_string());
                        b.push(']');
                    }
                    None => {
                        fo.null("top_route");
                    }
                }
                fo.end();
            }
            buf.push(']');
            o.end();
            Ok(Ok(out))
        },
    )
    .await
}

pub async fn aircraft_type(State(app): State<Arc<App>>, axum::extract::Path(designator): axum::extract::Path<String>, req: Request) -> Response {
    let code = designator.trim().to_uppercase();
    serve(app, req, &["ref_types"], format!("type:{code}"), move |c| {
        let row: Option<(String, Option<String>, Option<String>)> = c
            .prepare_cached("SELECT designator, name, category FROM ref_types WHERE designator = ?1")?
            .query_row([&code], |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?)))
            .optional()?;
        let Some((d, name, category)) = row else {
            return Ok(Err(ApiError::new(404, "not_found", "unknown type")));
        };
        let mut out = String::new();
        let mut o = Obj::new(&mut out);
        o.str("designator", &d);
        opt_str(o.key("name"), &name);
        opt_str(o.key("category"), &category);
        o.end();
        Ok(Ok(out))
    })
    .await
}

// ---- one alliance ----------------------------------------------------------

/// `_alliance_memberships`: an alliance's rows by status, relationship,
/// airline.
struct MemberRow {
    airline_icao: String,
    relationship: String,
    status: String,
    json_rest: String,
}

fn alliance_members(c: &Conn, slug: &str) -> rusqlite::Result<Vec<MemberRow>> {
    let mut stmt = c.prepare_cached(
        "SELECT airline_icao, relationship, status, sponsor_icao, effective_from, effective_to, source_url, \
         source_checked_at, note FROM ref_alliance_memberships WHERE alliance_slug = ?1 \
         ORDER BY status, relationship, airline_icao",
    )?;
    let mut rows = stmt.query([slug])?;
    let mut out = vec![];
    while let Some(r) = rows.next()? {
        let (icao, rel, status): (String, String, String) = (r.get(0)?, r.get(1)?, r.get(2)?);
        // the membership's own fields after "airline", as the route lists them
        let mut s = String::new();
        s.push_str(",\"status\":");
        write_str(&mut s, &status);
        s.push_str(",\"relationship\":");
        write_str(&mut s, &rel);
        for (k, i) in [("sponsor_icao", 3), ("effective_from", 4), ("effective_to", 5), ("source_url", 6), ("source_checked_at", 7), ("note", 8)] {
            s.push_str(&format!(",\"{k}\":"));
            opt_str(&mut s, &r.get(i)?);
        }
        out.push(MemberRow { airline_icao: icao, relationship: rel, status, json_rest: s });
    }
    Ok(out)
}

fn coverage_codes(members: &[MemberRow]) -> Vec<String> {
    let mut codes: Vec<String> = members
        .iter()
        .filter(|m| m.status == "active" && (m.relationship == "member" || m.relationship == "group-brand"))
        .map(|m| m.airline_icao.clone())
        .collect();
    codes.sort();
    codes.dedup();
    codes
}

fn alliance_slug(c: &Conn, slug: &str) -> rusqlite::Result<Option<String>> {
    c.prepare_cached("SELECT slug FROM ref_alliances WHERE slug = ?1")?
        .query_row([slug.trim().to_lowercase()], |r| r.get(0))
        .optional()
}

fn counts_in(c: &Conn, table: &str, codes: &[String]) -> rusqlite::Result<HashMap<String, i64>> {
    if codes.is_empty() {
        return Ok(HashMap::new());
    }
    let list = vec!["?"; codes.len()].join(",");
    let mut s = c.prepare(&format!("SELECT airline_icao, COUNT(*) FROM {table} WHERE airline_icao IN ({list}) GROUP BY airline_icao"))?;
    let rows = s.query_map(params_from_iter(codes), |r| Ok((r.get::<_, String>(0)?, r.get::<_, i64>(1)?)))?;
    rows.collect()
}

pub async fn alliance(State(app): State<Arc<App>>, axum::extract::Path(slug): axum::extract::Path<String>, req: Request) -> Response {
    let key = format!("alliance:{}", slug.trim().to_lowercase());
    serve(
        app,
        req,
        &["ref_alliances", "ref_alliance_memberships", "ref_airlines", "ref_routes", "ref_airline_countries", "ref_leg_stats", "ref_airframes"],
        key,
        move |c| {
            let Some(slug) = alliance_slug(c, &slug)? else {
                return Ok(Err(ApiError::new(404, "not_found", "unknown alliance")));
            };
            let members = alliance_members(c, &slug)?;
            let mut codes: Vec<String> = members.iter().map(|m| m.airline_icao.clone()).collect();
            codes.sort();
            codes.dedup();
            let routes = counts_in(c, "ref_routes", &codes)?;
            let countries = counts_in(c, "ref_airline_countries", &codes)?;
            let mut out = String::with_capacity(4096);
            out.push_str("{\"alliance\":");
            alliance_summary(c, &mut out, &slug)?;
            out.push_str(",\"memberships\":[");
            for (i, m) in members.iter().enumerate() {
                if i > 0 {
                    out.push(',');
                }
                out.push_str("{\"airline\":");
                let row = c
                    .prepare_cached("SELECT icao, iata, name, palette FROM ref_airlines WHERE icao = ?1")?
                    .query_row([&m.airline_icao], AirlineRow::read)
                    .optional()?;
                match row {
                    Some(a) => write_airline(&mut out, &a, None, None),
                    None => {
                        let mut o = Obj::new(&mut out);
                        o.str("icao", &m.airline_icao).null("iata").str("name", &m.airline_icao).raw("palette", "[]").raw("alliances", "[]");
                        o.end();
                    }
                }
                out.push_str(&m.json_rest);
                out.push_str(&format!(
                    ",\"n_routes\":{},\"n_countries\":{}}}",
                    routes.get(&m.airline_icao).unwrap_or(&0),
                    countries.get(&m.airline_icao).unwrap_or(&0)
                ));
            }
            out.push_str("],\"countries\":[");
            let active = coverage_codes(&members);
            let mut rollup: Vec<(Option<String>, i64)> = vec![];
            if !active.is_empty() {
                let list = vec!["?"; active.len()].join(",");
                let mut s = c.prepare(&format!(
                    "SELECT iso_country, SUM(n_routes) FROM ref_airline_countries WHERE airline_icao IN ({list}) GROUP BY iso_country"
                ))?;
                let rows = s.query_map(params_from_iter(&active), |r| Ok((r.get(0)?, r.get::<_, Option<i64>>(1)?.unwrap_or(0))))?;
                rollup = rows.collect::<rusqlite::Result<_>>()?;
            }
            rollup.sort_by(|a, b| b.1.cmp(&a.1).then_with(|| a.0.cmp(&b.0)));
            for (i, (country, n)) in rollup.iter().enumerate() {
                if i > 0 {
                    out.push(',');
                }
                out.push_str("{\"iso_country\":");
                opt_str(&mut out, country);
                out.push_str(&format!(",\"n_routes\":{n}}}"));
            }
            out.push_str("]}");
            Ok(Ok(out))
        },
    )
    .await
}

pub async fn alliance_routes(State(app): State<Arc<App>>, axum::extract::Path(slug): axum::extract::Path<String>, req: Request) -> Response {
    let key = format!("alliance_routes:{}", slug.trim().to_lowercase());
    serve(
        app,
        req,
        &["ref_alliances", "ref_alliance_memberships", "ref_leg_stats", "ref_routes", "ref_types", "ref_airports"],
        key,
        move |c| {
            let Some(slug) = alliance_slug(c, &slug)? else {
                return Ok(Err(ApiError::new(404, "not_found", "unknown alliance")));
            };
            let codes = coverage_codes(&alliance_members(c, &slug)?);
            let list = vec!["?"; codes.len()].join(",");
            // Postgres walks the airline index for an alliance's few
            // airlines: rows by airline, then as stored
            struct Agg {
                org: String,
                dst: String,
                n: i64,
                per_week: f64,
                avg_num: i64,
                avg_den: i64,
                types: Vec<(String, i64)>,
            }
            let mut leg_map: indexmap::IndexMap<(String, String), Agg> = indexmap::IndexMap::new();
            let mut airport_codes: HashSet<String> = HashSet::new();
            let mut type_counts: HashSet<String> = HashSet::new();
            if !codes.is_empty() {
                let mut s = c.prepare(&format!(
                    "SELECT o, d, n_flights, per_week, avg_min, types FROM ref_leg_stats WHERE airline_icao IN ({list}) \
                     ORDER BY airline_icao, rowid"
                ))?;
                let mut rows = s.query(params_from_iter(&codes))?;
                while let Some(r) = rows.next()? {
                    let (o, d): (String, String) = (r.get(0)?, r.get(1)?);
                    let n: i64 = r.get(2)?;
                    let agg = leg_map.entry((o.clone(), d.clone())).or_insert_with(|| Agg {
                        org: o.clone(),
                        dst: d.clone(),
                        n: 0,
                        per_week: 0.0,
                        avg_num: 0,
                        avg_den: 0,
                        types: vec![],
                    });
                    agg.n += n;
                    agg.per_week += r.get::<_, f64>(3)?;
                    if let Some(avg) = r.get::<_, Option<i64>>(4)? {
                        agg.avg_num += avg * n;
                        agg.avg_den += n;
                    }
                    let types: Option<String> = r.get(5)?;
                    if let Some(serde_json::Value::Array(pairs)) = types.and_then(|t| serde_json::from_str(&t).ok()) {
                        for p in pairs {
                            let (Some(t), Some(k)) = (p.get(0).and_then(|t| t.as_str()), p.get(1).and_then(|k| k.as_i64())) else {
                                continue;
                            };
                            match agg.types.iter_mut().find(|x| x.0 == t) {
                                Some(x) => x.1 += k,
                                None => agg.types.push((t.to_string(), k)),
                            }
                            type_counts.insert(t.to_string());
                        }
                    }
                    airport_codes.insert(o);
                    airport_codes.insert(d);
                }
            }
            // (org, dst, n, per_week JSON, avg_min JSON, aircraft JSON)
            let mut legs: Vec<(String, String, i64, String)> = vec![];
            let source = if !leg_map.is_empty() { "flightlog" } else { "chains" };
            if !leg_map.is_empty() {
                let mut names: HashMap<String, String> = HashMap::new();
                for t in &type_counts {
                    if let Some(n) = c
                        .prepare_cached("SELECT name FROM ref_types WHERE designator = ?1")?
                        .query_row([t], |r| r.get::<_, String>(0))
                        .optional()?
                    {
                        names.insert(t.clone(), n);
                    }
                }
                for agg in leg_map.values() {
                    let mut aircraft = agg.types.clone();
                    aircraft.sort_by(|a, b| b.1.cmp(&a.1).then_with(|| a.0.cmp(&b.0)));
                    let mut rest = String::new();
                    rest.push_str(",\"per_week\":");
                    crate::pyjson::write_float(&mut rest, crate::pyjson::round_to(agg.per_week, 2));
                    rest.push_str(",\"avg_min\":");
                    if agg.avg_den != 0 {
                        let v = (agg.avg_num as f64 / agg.avg_den as f64).round_ties_even() as i64;
                        rest.push_str(&v.to_string());
                    } else {
                        rest.push_str("null");
                    }
                    rest.push_str(",\"aircraft\":[");
                    for (i, (t, n)) in aircraft.iter().take(6).enumerate() {
                        if i > 0 {
                            rest.push(',');
                        }
                        rest.push_str("{\"type\":");
                        write_str(&mut rest, t);
                        rest.push_str(",\"name\":");
                        opt_str(&mut rest, &names.get(t).cloned());
                        rest.push_str(&format!(",\"n\":{n}}}"));
                    }
                    rest.push(']');
                    legs.push((agg.org.clone(), agg.dst.clone(), agg.n, rest));
                }
            } else {
                let mut counts: indexmap::IndexMap<(String, String), i64> = indexmap::IndexMap::new();
                if !codes.is_empty() {
                    let mut s = c.prepare(&format!("SELECT chain FROM ref_routes WHERE airline_icao IN ({list}) ORDER BY rowid"))?;
                    let mut rows = s.query(params_from_iter(&codes))?;
                    while let Some(r) = rows.next()? {
                        let chain: String = r.get(0)?;
                        let chain: Vec<serde_json::Value> = serde_json::from_str(&chain).unwrap_or_default();
                        for w in chain.windows(2) {
                            let (Some(a), Some(b)) = (w[0].as_str(), w[1].as_str()) else { continue };
                            if a.is_empty() || b.is_empty() || a == b {
                                continue;
                            }
                            let key = if a < b { (a.to_string(), b.to_string()) } else { (b.to_string(), a.to_string()) };
                            airport_codes.insert(key.0.clone());
                            airport_codes.insert(key.1.clone());
                            *counts.entry(key).or_insert(0) += 1;
                        }
                    }
                }
                for ((a, b), n) in counts {
                    legs.push((a, b, n, ",\"per_week\":null,\"aircraft\":null".to_string()));
                }
            }
            // the airports the legs touch, first airport winning a shared code
            let mut airports: indexmap::IndexMap<String, String> = indexmap::IndexMap::new();
            let wanted: Vec<String> = airport_codes.into_iter().filter(|c| !c.is_empty()).collect();
            if !wanted.is_empty() {
                let list = vec!["?"; wanted.len()].join(",");
                let mut s = c.prepare(&format!(
                    "SELECT iata, ident, lat, lon, name, iso_country, tz FROM ref_airports \
                     WHERE (iata IN ({list}) OR ident IN ({list})) AND lat IS NOT NULL ORDER BY rowid"
                ))?;
                let both: Vec<&String> = wanted.iter().chain(wanted.iter()).collect();
                let mut rows = s.query(params_from_iter(both))?;
                while let Some(r) = rows.next()? {
                    let (iata, ident): (Option<String>, Option<String>) = (r.get(0)?, r.get(1)?);
                    for code in [iata, ident].into_iter().flatten() {
                        if wanted.contains(&code) && !airports.contains_key(&code) {
                            let mut o = String::new();
                            let mut ob = Obj::new(&mut o);
                            ob.f64("lat", r.get(2)?);
                            match r.get::<_, Option<f64>>(3)? {
                                Some(v) => ob.f64("lon", v),
                                None => ob.null("lon"),
                            };
                            opt_str(ob.key("name"), &r.get(4)?);
                            opt_str(ob.key("iso_country"), &r.get(5)?);
                            opt_str(ob.key("tz"), &r.get(6)?);
                            ob.end();
                            airports.insert(code, o);
                        }
                    }
                }
            }
            legs.retain(|l| airports.contains_key(&l.0) && airports.contains_key(&l.1));
            legs.sort_by_key(|l| -l.2);
            let mut out = String::with_capacity(16384);
            out.push_str("{\"slug\":");
            write_str(&mut out, &slug);
            out.push_str(&format!(",\"source\":\"{source}\",\"airports\":{{"));
            for (i, (code, v)) in airports.iter().enumerate() {
                if i > 0 {
                    out.push(',');
                }
                write_str(&mut out, code);
                out.push(':');
                out.push_str(v);
            }
            out.push_str("},\"legs\":[");
            for (i, (a, b, n, rest)) in legs.iter().enumerate() {
                if i > 0 {
                    out.push(',');
                }
                out.push_str("{\"org\":");
                write_str(&mut out, a);
                out.push_str(",\"dst\":");
                write_str(&mut out, b);
                out.push_str(&format!(",\"n\":{n}"));
                out.push_str(rest);
                out.push('}');
            }
            out.push_str("]}");
            Ok(Ok(out))
        },
    )
    .await
}
