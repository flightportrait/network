/* One search box for every page. Airports and airlines answer from two
   small files already in the browser; registrations, flight numbers,
   routes and fleets come from /v1/search. The query is normalised before
   it leaves, so every spelling of a prefix shares one cache entry at the
   edge. Attach with fpSearch(inputElement, listElement, opts). */
(function () {
  "use strict";
  var API = window.FP_API || "https://data.flightportrait.com";
  function esc(s) {
    return String(s).replace(/[&<>"'\/]/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;", "/": "&#47;" }[c];
    });
  }
  function norm(q) { return q.trim().toUpperCase().replace(/\s+/g, " "); }
  function hrefFor(r) {
    if (r.kind === "aircraft") return "plane.html?hex=" + encodeURIComponent(r.id);
    if (r.kind === "flight") return "flight.html?callsign=" + encodeURIComponent(r.id);
    if (r.kind === "airport") return "/network/?airport=" + encodeURIComponent(r.id);
    if (r.kind === "airline") return "airline.html?icao=" + encodeURIComponent(r.id);
    if (r.kind === "live") return "/network/#" + encodeURIComponent(r.id);
    return "#";
  }
  var KIND = { live: "In the air now", flight: "Flight", aircraft: "Aircraft", airport: "Airport", airline: "Airline" };
  var EXACT = 100, PREFIX = 60, WORD = 40;

  // ---- the reference index, loaded once per page --------------------
  var apts = null, airlines = null, loading = null;
  function load() {
    if (loading) return loading;
    loading = Promise.all([
      fetch("assets/airports.json").then(function (r) { return r.json(); }).catch(function () { return {}; }),
      fetch("assets/airlines.json").then(function (r) { return r.json(); }).catch(function () { return []; })
    ]).then(function (both) {
      // airports.json keys every field under both its codes; keep one
      // entry per field, the IATA code when it has one
      var raw = both[0], byPos = {};
      Object.keys(raw).forEach(function (code) {
        var a = raw[code], k = a[1] + "," + a[2];
        if (!byPos[k] || code.length === 3) byPos[k] = { code: code, city: a[0], rank: a[4] || 99999, tier: a[3] || 9 };
      });
      apts = Object.keys(byPos).map(function (k) { return byPos[k]; });
      airlines = both[1].map(function (r) { return { icao: r[0], iata: r[1], name: r[2], routes: r[3] || 0 }; });
    });
    return loading;
  }
  function lift(n) { return Math.log10(1 + (n || 0)) * 4; }
  function localResults(q) {
    if (!apts || !airlines) return [];
    var out = [], alpha = /^[A-Z ]+$/.test(q);
    apts.forEach(function (a) {
      var score = 0, cityU = a.city.toUpperCase();
      if (a.code === q) score = EXACT;
      else if (alpha && (cityU.indexOf(q) === 0)) score = PREFIX;
      else if (alpha && q.length >= 3 && cityU.indexOf(" " + q) > 0) score = WORD;
      if (!score || a.tier > 2) return;
      out.push({ kind: "airport", id: a.code, label: a.city + " (" + a.code + ")",
                 detail: null, score: score + (30000 - Math.min(a.rank, 30000)) / 30000 * 6 });
    });
    airlines.forEach(function (a) {
      var score = 0, nameU = a.name.toUpperCase();
      if (a.icao === q || a.iata === q) score = EXACT;
      else if (alpha && nameU.indexOf(q) === 0) score = PREFIX;
      else if (alpha && q.length >= 3 && nameU.indexOf(" " + q) > 0) score = WORD;
      if (!score) return;
      out.push({ kind: "airline", id: a.icao, label: a.name,
                 detail: [a.icao, a.iata].filter(Boolean).join(" · "),
                 score: score + lift(a.routes) + (a.iata ? 2 : 0) });
    });
    out.sort(function (x, y) { return y.score - x.score; });
    return out.slice(0, 8);
  }
  function merge(first, second) {
    var seen = {}, out = [];
    first.concat(second).forEach(function (r) {
      var k = r.kind + ":" + r.id;
      if (seen[k]) return;
      seen[k] = 1; out.push(r);
    });
    out.sort(function (x, y) { return (y.score || 0) - (x.score || 0); });
    return out;
  }

  // ---- the box --------------------------------------------------------
  // opts.local(q) -> results shown first, instantly (the map's live sky);
  // opts.pick(result) -> true when it handled the choice itself.
  window.fpSearch = function (input, list, opts) {
    opts = opts || {};
    var timer = null, last = "", active = -1, items = [], warmed = false;
    function close() { list.hidden = true; list.innerHTML = ""; items = []; active = -1; }
    function render(results) {
      items = results;
      if (!results.length) {
        list.innerHTML = "<div class='s-empty'>Nothing by that name in the archive.</div>";
        list.hidden = false; return;
      }
      var html = "", kind = "";
      results.forEach(function (r, i) {
        if (r.kind !== kind) { kind = r.kind; html += "<div class='s-kind caps'>" + KIND[kind] + "</div>"; }
        html += "<a class='s-row' data-i='" + i + "' href='" + esc(hrefFor(r)) + "'>" +
          "<span class='s-label'>" + esc(r.label) + "</span>" +
          (r.detail ? "<span class='s-detail'>" + esc(r.detail) + "</span>" : "") + "</a>";
      });
      list.innerHTML = html; list.hidden = false; active = -1;
    }
    function group(results) {
      // the ranked list, but each kind's rows kept together under its
      // heading, ordered by the best row of the kind
      var best = {}, order = [];
      results.forEach(function (r) { if (!(r.kind in best)) { best[r.kind] = r.score || 0; order.push(r.kind); } });
      order.sort(function (a, b) { return best[b] - best[a]; });
      var out = [];
      order.forEach(function (k) { results.forEach(function (r) { if (r.kind === k) out.push(r); }); });
      return out;
    }
    function query() {
      var q = norm(input.value);
      if (q === last) return;
      last = q;
      if (q.length < 2) { close(); return; }
      var live = opts.local ? opts.local(q).map(function (r) { r.score = 200; return r; }) : [];
      var here = localResults(q);
      var shown = merge(live, here);
      if (shown.length) render(group(shown));
      fetch(API + "/v1/search?q=" + encodeURIComponent(q))
        .then(function (r) { return r.ok ? r.json() : { results: [] }; })
        .then(function (d) {
          if (norm(input.value) !== q) return;
          render(group(merge(shown, d.results || [])));
        })
        .catch(function () { if (!shown.length) close(); });
    }
    function mark() {
      var rows = list.querySelectorAll(".s-row");
      rows.forEach(function (el, i) { el.classList.toggle("active", i === active); });
    }
    input.addEventListener("focus", function () {
      load();
      if (!warmed) {
        // open the connection now so the first keystroke pays no handshake
        warmed = true;
        var l = document.createElement("link");
        l.rel = "preconnect"; l.href = API; document.head.appendChild(l);
      }
      if (items.length) list.hidden = false;
    });
    input.addEventListener("input", function () {
      load();
      clearTimeout(timer); timer = setTimeout(query, 150);
    });
    input.addEventListener("keydown", function (e) {
      if (e.key === "ArrowDown") { active = Math.min(active + 1, items.length - 1); mark(); e.preventDefault(); }
      else if (e.key === "ArrowUp") { active = Math.max(active - 1, 0); mark(); e.preventDefault(); }
      else if (e.key === "Enter") {
        var r = items[active >= 0 ? active : 0];
        if (r) { e.preventDefault(); if (!(opts.pick && opts.pick(r))) location.href = hrefFor(r); }
      } else if (e.key === "Escape") { close(); input.blur(); }
    });
    list.addEventListener("click", function (e) {
      var row = e.target.closest(".s-row");
      if (!row) return;
      var r = items[Number(row.dataset.i)];
      if (r && opts.pick && opts.pick(r)) e.preventDefault();
    });
    document.addEventListener("click", function (e) {
      if (!list.contains(e.target) && e.target !== input) list.hidden = true;
    });
    if (document.activeElement === input) load();
  };
})();
