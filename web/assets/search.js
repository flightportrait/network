/* One search box for the pages off the map. Talks to /v1/search as
   you type and turns a result into the page that shows it. Attach with
   fpSearch(inputElement, listElement). */
(function () {
  "use strict";
  var API = window.FP_API || "https://data.flightportrait.com";
  function esc(s) {
    return String(s).replace(/[&<>"'\/]/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;", "/": "&#47;" }[c];
    });
  }
  function hrefFor(r) {
    if (r.kind === "aircraft") return "plane.html?hex=" + encodeURIComponent(r.id);
    if (r.kind === "flight") return "flight.html?callsign=" + encodeURIComponent(r.id);
    if (r.kind === "airport") return "/network/?airport=" + encodeURIComponent(r.id);
    if (r.kind === "airline") return "/network/?a=" + encodeURIComponent(r.id);
    if (r.kind === "live") return "/network/#" + encodeURIComponent(r.id);
    return "#";
  }
  var KIND = { live: "In the air now", aircraft: "Aircraft", flight: "Flight", airport: "Airport", airline: "Airline" };

  // opts.local(q) -> results shown first, instantly (the map's live sky);
  // opts.pick(result) -> true when it handled the choice itself.
  window.fpSearch = function (input, list, opts) {
    opts = opts || {};
    var timer = null, last = "", active = -1, items = [];
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
    function query() {
      var q = input.value.trim();
      if (q === last) return;
      last = q;
      if (q.length < 2) { close(); return; }
      var local = opts.local ? opts.local(q) : [];
      if (local.length) render(local);
      fetch(API + "/v1/search?q=" + encodeURIComponent(q))
        .then(function (r) { return r.ok ? r.json() : { results: [] }; })
        .then(function (d) { if (input.value.trim() === q) render(local.concat(d.results || [])); })
        .catch(function () { if (!local.length) close(); });
    }
    function mark() {
      var rows = list.querySelectorAll(".s-row");
      rows.forEach(function (el, i) { el.classList.toggle("active", i === active); });
    }
    input.addEventListener("input", function () {
      clearTimeout(timer); timer = setTimeout(query, 180);
    });
    input.addEventListener("keydown", function (e) {
      if (e.key === "ArrowDown") { active = Math.min(active + 1, items.length - 1); mark(); e.preventDefault(); }
      else if (e.key === "ArrowUp") { active = Math.max(active - 1, 0); mark(); e.preventDefault(); }
      else if (e.key === "Enter") {
        var r = items[active >= 0 ? active : 0];
        if (r) { e.preventDefault(); if (!(opts.pick && opts.pick(r))) location.href = hrefFor(r); }
      } else if (e.key === "Escape") { close(); input.blur(); }
    });
    input.addEventListener("focus", function () { if (items.length) list.hidden = false; });
    list.addEventListener("click", function (e) {
      var row = e.target.closest(".s-row");
      if (!row) return;
      var r = items[Number(row.dataset.i)];
      if (r && opts.pick && opts.pick(r)) e.preventDefault();
    });
    document.addEventListener("click", function (e) {
      if (!list.contains(e.target) && e.target !== input) list.hidden = true;
    });
  };
})();
