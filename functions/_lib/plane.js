// One airframe page as HTML a crawler can read: the static plane.html
// with the airframe's identity, stats and flight log written in, plus
// title, description, canonical and breadcrumbs. The page's own script
// then renders the full view over it, as it does for visitors.

const API = "https://data.flightportrait.com";
const SITE = "https://flightportrait.com";
const TTL = 3600; // matches the API's s-maxage for /v1/airframes

function esc(s) {
  return String(s == null ? "" : s).replace(/[&<>"']/g, (c) => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

export function canonical(hex) {
  return `${SITE}/network/plane/${hex}`;
}

function summary(d) {
  const legs = d.legs || [];
  const airports = new Set(), pairs = {}, days = new Set();
  for (const l of legs) {
    airports.add(l.org); airports.add(l.dst); days.add(l.date);
    const key = [l.org, l.dst].sort().join("–");
    pairs[key] = (pairs[key] || 0) + 1;
  }
  const pair = Object.keys(pairs).sort((a, b) => pairs[b] - pairs[a])[0] || "";
  return {
    legs: legs.length, airports: airports.size, days: days.size, pair,
    first: legs.length ? legs[legs.length - 1].date : "",
    last: legs.length ? legs[0].date : "",
  };
}

function ident(d) {
  const reg = d.reg || d.hex.toUpperCase();
  const type = d.type_name || d.type || "";
  const operator = d.operator || (d.airline && d.airline.name) || "";
  const icao = d.operator_icao || (d.airline && d.airline.icao) || "";
  return { reg, type, operator, icao };
}

function head(d, s) {
  const { reg, type, operator } = ident(d);
  const title = [reg, type, operator].filter(Boolean).join(" · ") +
    ". FlightPortrait network";
  let desc = reg + (operator || type ? ", " + [operator, type].filter(Boolean).join(" ") : "") +
    " (" + d.hex.toUpperCase() + ")";
  if (s.legs) {
    desc += `: ${s.legs} flights observed at ${s.airports} airports` +
      (s.pair ? `, most often ${s.pair}` : "") + `. Last seen ${s.last}.`;
  } else {
    desc += ".";
  }
  return { title, desc };
}

function breadcrumbs(d) {
  const { reg, operator, icao } = ident(d);
  const items = [{ name: "Network", item: `${SITE}/network/` }];
  if (operator && icao) {
    items.push({ name: operator, item: `${SITE}/network/airline.html?icao=${icao}` });
  }
  items.push({ name: reg, item: canonical(d.hex) });
  return JSON.stringify({
    "@context": "https://schema.org",
    "@type": "BreadcrumbList",
    itemListElement: items.map((x, i) => ({ "@type": "ListItem", position: i + 1, ...x })),
  }).replace(/</g, "\\u003c");
}

function identHtml(d) {
  const { type, operator, icao } = ident(d);
  let html = [type, d.hex.toUpperCase()].filter(Boolean).map(esc).join(" · ");
  if (operator) {
    html += " · " + (icao
      ? `<a href="airline.html?icao=${esc(icao)}" style="color:inherit;text-decoration:none">${esc(operator)}</a>`
      : esc(operator));
  }
  return html;
}

function logHtml(d) {
  const legs = d.legs || [];
  if (!legs.length) return "";
  let html = "<h2>Flight log</h2><div class='tablewrap'><table class='legs'>" +
    "<thead><tr><th>Date</th><th>Flight</th><th>From</th><th>To</th>" +
    "<th class='num'>Max altitude</th></tr></thead><tbody>";
  for (const l of legs) {
    html += "<tr><td class='date'>" + esc(l.date) + "</td>" +
      "<td class='flight'>" + (l.callsign
        ? `<a href="flight.html?callsign=${esc(l.callsign)}">${esc(l.callsign)}</a>` : "") + "</td>" +
      "<td>" + esc(l.org) + "</td><td>" + esc(l.org === l.dst ? "circuit" : l.dst) + "</td>" +
      "<td class='num'>" + (l.max_alt ? Number(l.max_alt).toLocaleString("en-US") + " ft" : "") +
      "</td></tr>";
  }
  return html + "</tbody></table></div>";
}

class SetText {
  constructor(value) { this.value = value; }
  element(e) { e.setInnerContent(this.value); }
}
class SetHtml {
  constructor(value) { this.value = value; }
  element(e) { e.setInnerContent(this.value, { html: true }); }
}
class SetAttr {
  constructor(name, value) { this.attr = name; this.value = value; }
  element(e) { e.setAttribute(this.attr, this.value); }
}
class Append {
  constructor(value) { this.value = value; }
  element(e) { e.append(this.value, { html: true }); }
}

export function render(page, d) {
  const s = summary(d);
  const { title, desc } = head(d, s);
  const url = canonical(d.hex);
  let rw = new HTMLRewriter()
    .on("title", new SetText(title))
    .on('meta[name="description"]', new SetAttr("content", desc))
    .on('meta[property="og:title"]', new SetAttr("content", title))
    .on('meta[property="og:description"]', new SetAttr("content", desc))
    .on("head", new Append(
      `<link rel="canonical" href="${url}">` +
      `<meta property="og:url" content="${url}">` +
      (s.legs ? "" : '<meta name="robots" content="noindex">') +
      `<script type="application/ld+json">${breadcrumbs(d)}</script>`))
    .on("#reg", new SetText(ident(d).reg))
    .on("#ident", new SetHtml(identHtml(d)))
    .on("#log", new SetHtml(logHtml(d)));
  if (s.legs) {
    rw = rw
      .on("#s-legs", new SetText(String(s.legs)))
      .on("#s-airports", new SetText(String(s.airports)))
      .on("#s-pair", new SetText(s.pair || "–"))
      .on("#s-days", new SetText(String(s.days)))
      .on("#s-first", new SetText(s.first))
      .on("#stats", new SetAttr("style", ""));
  }
  return rw.transform(page);
}

async function api(path) {
  const r = await fetch(API + path, { cf: { cacheTtl: TTL, cacheEverything: true } });
  if (r.status === 404) return null;
  if (!r.ok) throw new Error(`api ${r.status}`);
  return r.json();
}

// A registration typed as a URL ("9V-SMF") resolves to its hex through
// the network's search; only an exact registration match counts.
export async function hexForReg(reg) {
  const d = await api("/v1/search?q=" + encodeURIComponent(reg));
  const want = reg.toUpperCase();
  const hit = ((d && d.results) || []).find((r) =>
    r.kind === "aircraft" && String(r.label).toUpperCase() === want);
  return hit ? hit.id : null;
}

export async function airframePage(context, hex) {
  const cache = caches.default;
  const key = new Request(canonical(hex));
  const hit = await cache.match(key);
  if (hit) return hit;

  const page = await context.env.ASSETS.fetch(new URL("/network/plane", context.request.url));
  let d;
  try {
    d = await api("/v1/airframes/" + hex);
  } catch (e) {
    // API busy or down. A 503 tells crawlers to come back; a visitor's
    // browser still shows the page, its script fetching on its own.
    const res = new Response(page.body, { status: 503, headers: page.headers });
    res.headers.set("Retry-After", "600");
    return res;
  }
  if (!d) {
    const res = new Response(page.body, { status: 404, headers: page.headers });
    res.headers.set("X-Robots-Tag", "noindex");
    return res;
  }
  const res = new Response(render(page, d).body, {
    status: 200,
    headers: {
      "Content-Type": "text/html; charset=utf-8",
      "Cache-Control": `public, max-age=300, s-maxage=${TTL}`,
    },
  });
  context.waitUntil(cache.put(key, res.clone()));
  return res;
}
