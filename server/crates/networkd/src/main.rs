//! networkd: the FlightPortrait network server.
//!
//! One binary: it reads the aggregator (readsb's JSON position port,
//! with aircraft.json as the fallback) and serves the open data API and
//! the live stream. Configured by the same NETWORK_API_* environment as
//! the Python service it replaces; NETWORKD_BIND sets the listen address.

mod address_blocks;
mod airframe;
mod beacon;
mod boards;
mod catalog;
mod departure;
mod estimate;
mod gapbook;
mod gaps;
mod history;
mod http;
mod legs;
mod live;
mod pg;
mod pgsort;
mod proxy;
mod range_km;
mod pyjson;
mod refdata;
mod refdb;
mod routebook;
mod search;
mod squawks;
mod stations;
mod ratelimit;
mod settings;
mod sky;
mod state;
mod stream;
mod traces;
mod upstream;
mod ws;

use std::future::Future;
use std::net::SocketAddr;
use std::sync::Arc;
use std::time::Duration;

use axum::routing::get;
use axum::Router;
use tower::Layer;

use crate::settings::Settings;
use crate::state::{now_s, App};

/// Run `f` forever: each failure logs and backs off (doubling up to five
/// minutes), a success resets the interval.
async fn every<F, Fut>(name: &'static str, interval_s: f64, mut f: F)
where
    F: FnMut() -> Fut,
    Fut: Future<Output = anyhow::Result<()>>,
{
    let mut backoff = interval_s;
    loop {
        match f().await {
            Ok(()) => backoff = interval_s,
            Err(e) => {
                eprintln!("{name} poll failed: {e}");
                backoff = (backoff * 2.0).min(300.0);
            }
        }
        tokio::time::sleep(Duration::from_secs_f64(backoff)).await;
    }
}

/// clients.json -> the number of stations connected now. readsb invents
/// a half-zero UUID for connections that sent none; those are plumbing,
/// not stations.
fn station_count(body: &[u8]) -> anyhow::Result<usize> {
    let v: serde_json::Value = serde_json::from_slice(body)?;
    let mut ids = std::collections::HashSet::new();
    for row in v.get("clients").and_then(|c| c.as_array()).into_iter().flatten() {
        let Some(a) = row.as_array().filter(|a| a.len() >= 9) else { continue };
        let Some(uuid) = a[0].as_str() else { continue };
        let n: String = uuid.trim().to_lowercase().replace('-', "");
        if n.len() != 32 || !n.bytes().all(|b| b.is_ascii_hexdigit()) || n.ends_with(&"0".repeat(16)) {
            continue;
        }
        ids.insert(n[..16].to_string());
    }
    Ok(ids.len())
}

async fn poll_stations_once(app: &App) -> anyhow::Result<()> {
    match app.upstream.get("/data/clients.json").await {
        Ok(body) => {
            if let Some(reg) = &app.stations {
                app.presence.lock().unwrap().available = true;
                return stations::poll_clients(app, reg, &body).await;
            }
            let n = station_count(&body)?;
            let mut p = app.presence.lock().unwrap();
            p.count = n;
            p.available = true;
            p.at = now_s();
            Ok(())
        }
        Err(e) if upstream::status_of(&e) == Some(404) => {
            // this readsb does not serve clients.json: counts go null
            app.presence.lock().unwrap().available = false;
            Ok(())
        }
        Err(e) => Err(e),
    }
}

fn start_tasks(app: &Arc<App>) {
    let s = &app.settings;
    if !s.live_json.is_empty() && !s.point_mode() {
        tokio::spawn(sky::read_lines(app.clone(), s.live_json.clone()));
        tokio::spawn(sky::publish(app.clone()));
    }
    let snap_every = if s.point_mode() { s.snapshot_poll_s.max(60.0) } else { s.snapshot_poll_s };
    let a = app.clone();
    tokio::spawn(async move {
        every("snapshot", snap_every, || sky::poll_snapshot_once(&a)).await;
    });
    tokio::spawn(app.routes.clone().keep());
    tokio::spawn(app.gaps.clone().keep());
    if let Some(est) = &app.estimates {
        tokio::spawn(est.clone().keep());
    }
    if let Some(w) = &app.squawks {
        // our own sky only: emergency squawks become airframe events
        tokio::spawn(squawks::flush_loop(w.clone(), s.database_url.clone()));
    }
    if s.point_mode() {
        app.presence.lock().unwrap().available = false;
    } else {
        let a = app.clone();
        let every_s = s.station_poll_s;
        tokio::spawn(async move {
            every("stations", every_s, || poll_stations_once(&a)).await;
        });
        if let Some(reg) = app.stations.clone() {
            let a = app.clone();
            tokio::spawn(async move {
                every("receivers", a.settings.receivers_poll_s, || stations::poll_receivers_once(&a, &reg)).await;
            });
        }
    }
}

pub fn router(app: Arc<App>) -> Router {
    let routes = Router::new()
        .route("/", get(live::index))
        .route("/healthz", get(live::healthz))
        .route("/v1/now", get(live::now))
        .route("/v1/aircraft", get(live::aircraft))
        .route("/v1/trace/{hex}", get(live::trace))
        .route("/v1/estimated", get(estimate::estimated))
        .route("/v1/routes", get(catalog::routes_bulk))
        .route("/v1/flights/{callsign}", get(catalog::flight))
        .route("/v1/airframes/{hex}", get(airframe::airframe))
        .route("/v1/gaps", get(gaps::gaps))
        .route("/v1/gaps/{callsign}", get(gaps::gap))
        .route("/v1/contributors", get(gaps::contributors))
        .route("/v1/setup/beacon", get(beacon::find).post(beacon::report).fallback(beacon::other_method))
        .route("/v2/point/{lat}/{lon}/{radius}", get(live::point))
        .route("/v1/airlines", get(refdata::airlines))
        .route("/v1/airlines/{icao}", get(refdata::airline))
        .route("/v1/alliances", get(refdata::alliances))
        .route("/v1/alliances/{slug}", get(refdata::alliance))
        .route("/v1/alliances/{slug}/routes", get(refdata::alliance_routes))
        .route("/v1/airlines/{icao}/routes", get(refdata::airline_routes))
        .route("/v1/airlines/{icao}/leg/{org}/{dst}", get(refdata::airline_leg))
        .route("/v1/airlines/{icao}/schedule/{org}/{dst}", get(refdata::airline_schedule))
        .route("/v1/airlines/{icao}/countries", get(refdata::airline_countries))
        .route("/v1/airlines/{icao}/airframes", get(airframe::airline_airframes))
        .route("/v1/airlines/{icao}/fleet", get(refdata::airline_fleet))
        .route("/v1/airlines/{icao}/fleet/{designator}", get(refdata::airline_fleet_type))
        .route("/v1/types/{designator}", get(refdata::aircraft_type))
        .route("/v1/search", get(search::search))
        .route("/v1/airports/{code}", get(history::airport))
        .route("/v1/stations", get(stations::roster))
        .route("/v1/stations/{uuid}", get(stations::self_view))
        .route("/v1/stream", get(stream::stream_v1))
        .route("/v2/stream", get(stream::stream_v2))
        .fallback(proxy::forward)
        .method_not_allowed_fallback(http::method_not_allowed)
        .with_state(app.clone());
    // CORS wraps the router, so it sees requests before routing does
    Router::new().fallback_service(axum::middleware::from_fn_with_state(app, http::cors).layer(routes))
}

/// `networkd --health`: exit 0 when this instance answers /healthz.
/// For container health checks on images without curl.
fn health(bind: &str) -> ! {
    use std::io::{Read, Write};
    let port = bind.rsplit(':').next().unwrap_or("8092");
    let ok = std::net::TcpStream::connect(("127.0.0.1", port.parse().unwrap_or(8092)))
        .and_then(|mut s| {
            s.set_read_timeout(Some(Duration::from_secs(3)))?;
            s.write_all(b"GET /healthz HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n")?;
            let mut head = [0u8; 12];
            s.read_exact(&mut head)?;
            Ok(head.starts_with(b"HTTP/1.1 200"))
        })
        .unwrap_or(false);
    std::process::exit(if ok { 0 } else { 1 })
}

#[tokio::main]
async fn main() -> anyhow::Result<()> {
    let settings = Settings::from_env();
    if std::env::args().nth(1).as_deref() == Some("--health") {
        health(&settings.bind);
    }
    let bind = settings.bind.clone();
    let app = App::new(settings);
    start_tasks(&app);
    // docker stop: keep what only this process knows, then go
    let a = app.clone();
    tokio::spawn(async move {
        let Ok(mut term) = tokio::signal::unix::signal(tokio::signal::unix::SignalKind::terminate()) else { return };
        term.recv().await;
        if let Some(est) = &a.estimates {
            let _ = tokio::time::timeout(Duration::from_secs(5), est.save()).await;
        }
        std::process::exit(0);
    });
    let listener = tokio::net::TcpListener::bind(&bind).await?;
    eprintln!("networkd listening on {bind}");
    axum::serve(listener, router(app).into_make_service_with_connect_info::<SocketAddr>()).await?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn counts_stations_like_the_registry() {
        let body = br#"{"clients":[
            ["0123456789abcdef0123456789abcdef","1.2.3.4 port 1",1,2,3,4,5,6,7],
            ["0123-4567-89AB-CDEF-0123456789abcdef","x",1,2,3,4,5,6,7],
            ["abcdef0123456789-0000-0000-000000000000","x",1,2,3,4,5,6,7],
            ["short","x",1,2,3,4,5,6,7],
            ["fedcba98765432100123456789abcdef","x",1]
        ]}"#;
        assert_eq!(station_count(body).unwrap(), 1);
    }
}
