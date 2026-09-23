/* An airframe's life, worded once for every page. Reads the `history`
   section of /v1/airframes/{hex}: airline stints from the network's
   evidence, then every public event the record holds. Worded as each
   source saw or states it, nothing inferred. The plane page shows all of
   it, the map's cards the latest few.

   fpHistory.flag(iso)          the state of registration as its flag
   fpHistory.facts(d)           "Built 1982 · Serial 22194 · engine"
   fpHistory.rows(d)            [{at, src, what, more, hi}], newest first
   fpHistory.list(rows, limit)  <ol class='tl'> of those rows
   fpHistory.notable(d)         incident reports and squawks, newest first */
(function () {
  "use strict";
  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"'\/]/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;", "/": "&#47;" }[c];
    });
  }
  function countryName(code) {
    try {
      return new Intl.DisplayNames(["en"], { type: "region" }).of(code) || code;
    } catch (e) { return code; }
  }
  // The two regional-indicator letters of the ISO code, named for hover
  // and screen readers. Windows draws the letters instead of a flag.
  function flag(code) {
    if (!/^[A-Z]{2}$/.test(code || "")) return "";
    var f = String.fromCodePoint.apply(null, code.split("").map(function (c) {
      return 0x1F1E6 + c.charCodeAt(0) - 65;
    }));
    var name = esc(countryName(code));
    return "<span class='flag' role='img' title='" + name + "' aria-label='" +
      name + "'>" + f + "</span>";
  }
  function day(iso) {
    var d = new Date(String(iso).slice(0, 10) + "T00:00:00Z");
    if (isNaN(d)) return "";
    return d.toLocaleDateString("en-GB", { day: "numeric", month: "short",
      year: "numeric", timeZone: "UTC" });
  }
  var SOURCES = { observed: "Network", live: "Network", faa: "FAA",
                  cadors: "Transport Canada" };
  var EMERGENCY = { general: "an emergency", lifeguard: "a medical emergency",
                    minfuel: "minimum fuel", nordo: "no radio",
                    downed: "a downed aircraft" };

  function facts(d) {
    var h = d.history || {}, out = [];
    if (h.built_year || d.year) out.push("Built " + (h.built_year || d.year));
    if (h.msn) out.push("Serial " + esc(h.msn));
    if (h.registry && h.registry.engine) out.push(esc(h.registry.engine));
    return out.join(" · ");
  }

  function event(e) {
    var x = e.detail || {}, what = "", more = "", hi = false;
    switch (e.kind) {
      case "first_observed": what = "First seen by the network"; break;
      case "registration_change":
        what = "Re-registered as " + esc(x.to);
        more = x.from ? "was " + esc(x.from) : ""; break;
      case "not_observed":
        what = "Not seen for " + esc(x.days) + " days";
        more = x.seen_again ? "until " + day(x.seen_again) : ""; break;
      case "squawk":
        hi = true;
        what = x.code ? "Squawked " + esc(x.code)
                      : "Reported " + esc(EMERGENCY[x.emergency] || "an emergency");
        more = [x.callsign ? "as " + esc(x.callsign) : "",
                x.alt_baro ? "at " + esc(Number(x.alt_baro).toLocaleString()) + " ft" : ""]
               .filter(Boolean).join(" ");
        break;
      case "occurrence":
        hi = true;
        what = esc(x.type || "Occurrence") + " reported" +
          (x.aerodrome ? " at " + esc(x.aerodrome)
                       : x.location ? ", " + esc(x.location) : "");
        more = [(x.events || []).join(", "),
                x.phase ? String(x.phase).toLowerCase() : "",
                x.damage && x.damage !== "No Damage" ? String(x.damage).toLowerCase() : "",
                x.fatalities ? x.fatalities + " fatal" : "",
                x.injuries ? x.injuries + " injured" : "",
                x.report ? "report " + x.report : ""]
               .filter(Boolean).map(esc).join(" · ");
        break;
      case "registered":
        what = "Registration certificate issued";
        more = x.registration ? esc(x.registration) : ""; break;
      case "airworthiness": what = "Airworthiness certificate issued"; break;
      default: return null;              // operator changes: the stints say it
    }
    return { at: e.at, src: e.source, what: what, more: more, hi: hi };
  }

  function rows(d) {
    var h = d.history;
    if (!h) return [];
    var out = [], seen = {};
    (h.operators || []).forEach(function (o, i) {
      var link = "<a href='airline.html?icao=" + esc(o.icao) + "'>" +
        esc(o.name || o.icao) + "</a>";
      // the first stint starts where the network's view starts, not
      // where the airline's does
      out.push({ at: o.from, src: "observed",
        what: (i === 0 ? "Seen flying for " : seen[o.icao] ? "Back with "
               : "Flying for ") + link });
      seen[o.icao] = 1;
    });
    (h.events || []).forEach(function (e) {
      var r = event(e);
      if (r) out.push(r);
    });
    out.sort(function (a, b) { return String(b.at).localeCompare(String(a.at)); });
    return out;
  }

  function list(items, limit) {
    var shown = limit ? items.slice(0, limit) : items;
    return "<ol class='tl'>" + shown.map(function (r) {
      return "<li" + (r.hi ? " class='hi'" : "") + ">" +
        "<span class='when'>" + day(r.at) + "</span>" +
        "<span class='what'>" + r.what +
        (r.more ? "<span class='more'>" + r.more + "</span>" : "") + "</span>" +
        "<span class='src caps'>" + esc(SOURCES[r.src] || r.src) + "</span></li>";
    }).join("") + "</ol>";
  }

  function notable(d) {
    return rows(d).filter(function (r) { return r.hi; });
  }

  window.fpHistory = { flag: flag, facts: facts, rows: rows, list: list,
                       notable: notable, day: day };
})();
