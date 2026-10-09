// One airframe page as HTML a crawler can read: the static plane.html
// with the airframe's identity, stats and flight log written in, plus
// title, description, canonical and breadcrumbs. The page's own script
// then renders the full view over it, as it does for visitors.
import { SITE, esc, api, breadcrumbs, cachedPage, headRewriter, SetText, SetHtml, SetAttr } from "./edge.js";

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

function crumbs(d) {
  const { reg, operator, icao } = ident(d);
  const items = [{ name: "Network", item: `${SITE}/network/` }];
  if (operator && icao) items.push({ name: operator, item: `${SITE}/network/airline/${icao}` });
  items.push({ name: reg, item: canonical(d.hex) });
  return breadcrumbs(items);
}

function identHtml(d) {
  const { type, operator, icao } = ident(d);
  let html = [type, d.hex.toUpperCase()].filter(Boolean).map(esc).join(" · ");
  if (operator) {
    html += " · " + (icao
      ? `<a href="airline/${esc(icao)}" style="color:inherit;text-decoration:none">${esc(operator)}</a>`
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

export function airframePage(context, hex) {
  return cachedPage(context, canonical(hex), "/network/plane", async (page) => {
    const d = await api("/v1/airframes/" + hex);
    if (!d) return null;
    const s = summary(d);
    const { title, desc } = head(d, s);
    let rw = headRewriter(title, desc, canonical(hex), crumbs(d), s.legs > 0)
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
  });
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
