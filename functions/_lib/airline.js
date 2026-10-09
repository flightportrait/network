// One airline page as HTML a crawler can read: the static airline.html
// with the airline's codes and figures, its aircraft (each a link to the
// airframe page) and its routes written into the panels. The page's own
// script then renders the full view over them.
import { SITE, esc, api, breadcrumbs, cachedPage, headRewriter, SetText, SetHtml, RemoveAttr } from "./edge.js";

export function canonical(icao) {
  return `${SITE}/network/airline/${icao}`;
}

function cadence(perWeek) {
  if (!perWeek) return "";
  if (perWeek >= 6.5) return Math.round(perWeek / 7) > 1 ? Math.round(perWeek / 7) + "× daily" : "daily";
  if (perWeek >= 0.9) return Math.round(perWeek) + "× a week";
  return "occasional";
}

function aircraftHtml(planes) {
  if (!planes.length) return "";
  const byType = {};
  for (const p of planes) (byType[p.type || "Other"] ||= []).push(p);
  const order = Object.keys(byType).sort((a, b) => byType[b].length - byType[a].length);
  let html = "<h2>Aircraft</h2>";
  for (const t of order) {
    const list = byType[t].slice().sort((a, b) => String(a.reg).localeCompare(String(b.reg)));
    html += `<h3>${esc(t)} <span class="c">${list.length}</span></h3><p class="regs">` +
      list.map((p) => `<a href="plane/${esc(p.hex)}">${esc(p.reg || p.hex.toUpperCase())}</a>`).join(" ") +
      "</p>";
  }
  return html;
}

function routesHtml(legs, airports) {
  if (!legs.length) return "";
  const name = (c) => (airports[c] && airports[c].name) || "";
  let html = "<h2>Routes</h2><ul class='routes'>";
  for (const l of legs) {
    const types = (l.aircraft || []).map((a) => a.name || a.type).filter(Boolean).join(", ");
    html += "<li>" + esc(l.org) + " – " + esc(l.dst) +
      (name(l.org) || name(l.dst) ? ` <span class="q">${esc(name(l.org))} to ${esc(name(l.dst))}</span>` : "") +
      (l.per_week ? ` · ${esc(cadence(l.per_week))}` : "") +
      (types ? ` · ${esc(types)}` : "") + "</li>";
  }
  return html + "</ul>";
}

export function airlinePage(context, icao) {
  return cachedPage(context, canonical(icao), "/network/airline", async (page) => {
    const a = await api("/v1/airlines/" + icao);
    if (!a) return null;
    const [fleet, routes] = await Promise.all([
      api(`/v1/airlines/${icao}/airframes`),
      api(`/v1/airlines/${icao}/routes`),
    ]);
    const planes = (fleet && fleet.airframes) || [];
    const legs = (routes && routes.legs) || [];
    const airports = (routes && routes.airports) || {};
    const flights = legs.reduce((n, l) => n + (l.n || 0), 0);
    const byAirport = {};
    for (const l of legs) byAirport[l.org] = (byAirport[l.org] || 0) + (l.n || 0);
    const hub = Object.keys(byAirport).sort((x, y) => byAirport[y] - byAirport[x])[0] || "";
    const top = legs.slice().sort((x, y) => (y.n || 0) - (x.n || 0))[0];

    const codes = [a.iata, a.icao].filter(Boolean).join(" / ");
    const title = `${a.name} fleet and routes. FlightPortrait network`;
    const facts = [];
    if (planes.length) facts.push(`${planes.length} aircraft`);
    if (legs.length) facts.push(`${legs.length} routes`);
    let desc = `${a.name} (${codes})` + (facts.length ? `: ${facts.join(" and ")}` : "") +
      (a.n_countries ? ` in ${a.n_countries} countries` : "") +
      " observed by the FlightPortrait network." +
      (top ? ` Busiest route ${top.org}–${top.dst}.` : "");
    const alliances = (a.alliances || []).map((x) => x.name || x);
    const crumbs = breadcrumbs([
      { name: "Network", item: `${SITE}/network/` },
      { name: a.name, item: canonical(icao) },
    ]);

    return headRewriter(title, desc, canonical(icao), crumbs, planes.length > 0 || legs.length > 0)
      .on("#name", new SetText(a.name))
      .on("#codes", new SetHtml(
        "<span>" + esc([a.icao, a.iata].filter(Boolean).join(" · ")) + "</span>" +
        (alliances.length ? "<span>" + esc(alliances.join(", ")) + "</span>" : "") +
        `<a href="/network/?a=${encodeURIComponent(icao)}">On the map</a>`))
      .on("#f-aircraft", new SetText(planes.length ? planes.length.toLocaleString("en-US") : "–"))
      .on("#f-routes", new SetText(legs.length ? legs.length.toLocaleString("en-US") : "–"))
      .on("#f-countries", new SetText(a.n_countries != null ? String(a.n_countries) : "–"))
      .on("#f-flights", new SetText(flights ? flights.toLocaleString("en-US") : "–"))
      .on("#f-hub", new SetText(hub || "–"))
      .on("#figs", new RemoveAttr("hidden"))
      .on("#p-aircraft", new SetHtml(aircraftHtml(planes)))
      .on("#p-aircraft", new RemoveAttr("hidden"))
      .on("#p-routes", new SetHtml(routesHtml(legs, airports)))
      .on("#p-routes", new RemoveAttr("hidden"))
      .transform(page);
  });
}
