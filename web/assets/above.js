/* Planes above me: the aircraft within a radius of one point, nearest
   first, from /v2/point. The position is the visitor's approximate one,
   written onto #above by the edge (functions/network/above.js) from the
   request's own geolocation, until the visitor asks for the exact one.
   It stays in this page: never stored, never sent anywhere but the
   point query. The browser asks the API itself, so each visitor spends
   their own rate allowance; one query every 20 s, none while hidden.
   Words come from the table below, chosen by <html lang>. */
(function () {
  "use strict";
  var API = window.FP_API || "https://data.flightportrait.com";
  var NM_PER = { mi: 0.868976, km: 0.539957 };
  var WORDS = {
    en: { unit: "mi", radii: [10, 25, 50], near: "Near you", around: "Around ",
          here: "At your location", within: "within", exact: "Use my exact location",
          denied: "Location not shared", ft: "ft" },
    de: { unit: "km", radii: [20, 50, 100], near: "In deiner Nähe", around: "Rund um ",
          here: "An deinem Standort", within: "im Umkreis von", exact: "Meinen genauen Standort verwenden",
          denied: "Standort nicht freigegeben", ft: "ft" },
    fr: { unit: "km", radii: [20, 50, 100], near: "Près de vous", around: "Autour de ",
          here: "À votre position", within: "dans un rayon de", exact: "Utiliser ma position exacte",
          denied: "Position non partagée", ft: "ft" }
  };
  var lang = (document.documentElement.lang || "en").slice(0, 2);
  var W = WORDS[lang] || WORDS.en;
  var fmt = new Intl.NumberFormat(lang === "en" ? "en-US" : lang);

  function el(id) { return document.getElementById(id); }
  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }
  function json(url) {
    return fetch(url).then(function (r) { return r.ok ? r.json() : null; });
  }

  var root = el("above");
  var pos = null;
  var lat = parseFloat(root.getAttribute("data-lat"));
  var lon = parseFloat(root.getAttribute("data-lon"));
  if (isFinite(lat) && isFinite(lon)) pos = { lat: lat, lon: lon };
  var city = root.getAttribute("data-city") || "";
  var radius = W.radii[1];

  // names, loaded once; rows fill in as they arrive
  var types = {}, airlines = {}, operators = {}, airports = null;
  var routes = {};
  json("assets/types.json").then(function (d) { types = d || {}; draw(); }).catch(function () {});
  json(API + "/v1/airlines").then(function (d) {
    ((d && d.airlines) || []).forEach(function (a) { airlines[a.icao] = a.name; });
    draw();
  }).catch(function () {});
  json("assets/operators.json").then(function (d) { operators = d || {}; draw(); }).catch(function () {});
  function airportsSoon() {
    if (airports) return;
    airports = {};
    json("assets/airports.json").then(function (d) { airports = d || {}; draw(); }).catch(function () {});
  }

  function operator(cs) {
    var m = /^([A-Z]{3})\d/.exec(cs || "");
    return m ? (airlines[m[1]] || operators[m[1]] || "") : "";
  }
  function cityOf(code) {
    var a = airports && airports[code];
    return a ? String(a[0]).replace(/\s*\(.*\)$/, "") : "";
  }
  function wantRoutes(list) {
    var batch = list.map(function (a) { return a.flight; })
      .filter(function (cs) { return cs && !(cs in routes); }).slice(0, 40);
    if (!batch.length) return;
    batch.forEach(function (cs) { routes[cs] = null; });
    json(API + "/v1/routes?cs=" + encodeURIComponent(batch.join(","))).then(function (d) {
      var got = (d && d.routes) || {};
      batch.forEach(function (cs) { routes[cs] = got[cs.toUpperCase()] || null; });
      airportsSoon();
      draw();
    }).catch(function () {});
  }

  var planes = [];
  function row(a) {
    var cs = a.flight || "";
    var type = (a.t && types[a.t] && types[a.t][0]) || a.t || "";
    var chain = cs && routes[cs];
    var route = "";
    if (chain) {
      var cities = chain.map(cityOf).filter(Boolean);
      route = esc(chain.join(" → ")) +
        (cities.length === chain.length ? "<span class='city'>" + esc(cities.join(" → ")) + "</span>" : "");
    }
    var alt = a.alt_baro === "ground" ? "" :
      (typeof a.alt_baro === "number" ? fmt.format(a.alt_baro) + " " + W.ft : "");
    var dist = typeof a.dst === "number" ? fmt.format(Math.round(a.dst / NM_PER[W.unit])) + " " + W.unit : "";
    return "<tr><td class='flight'>" +
      (cs ? "<a href='flight.html?callsign=" + encodeURIComponent(cs) + "'>" + esc(cs) + "</a>" : "") +
      "</td><td>" + esc(operator(cs)) + "</td><td>" + esc(type) + "</td><td class='flight'>" +
      (a.r ? "<a href='plane/" + esc(a.hex) + "'>" + esc(a.r) + "</a>" : "") +
      "</td><td>" + route + "</td><td class='num'>" + alt + "</td><td class='num'>" + dist + "</td></tr>";
  }
  function draw() {
    el("rows").innerHTML = planes.map(row).join("");
    el("list").hidden = !planes.length;
  }

  var timer = null;
  function poll() {
    clearTimeout(timer);
    timer = null;
    if (!pos || document.hidden) return;
    var nm = Math.round(radius * NM_PER[W.unit]);
    var p = pos;
    fetch(API + "/v2/point/" + p.lat.toFixed(3) + "/" + p.lon.toFixed(3) + "/" + nm)
      .then(function (r) {
        if (r.status === 429) return null;
        return r.ok ? r.json() : null;
      })
      .then(function (d) {
        if (!d || p !== pos) return;
        planes = (d.ac || []).filter(function (a) { return a.alt_baro !== "ground"; });
        wantRoutes(planes);
        draw();
      })
      .catch(function () {})
      .then(function () { if (!timer) timer = setTimeout(poll, 20000); });
  }
  document.addEventListener("visibilitychange", function () { if (!document.hidden) poll(); });

  function where(text) { el("where").textContent = text; }
  function around() {
    if (!city) return W.near;
    if (lang === "fr" && /^[aeiouyhàâéèêîôû]/i.test(city)) return "Autour d\u2019" + city;
    return W.around + city;
  }
  where(pos ? around() : "");

  var sel = el("radius");
  W.radii.forEach(function (r) {
    var o = document.createElement("option");
    o.value = r; o.textContent = r + " " + W.unit;
    if (r === radius) o.selected = true;
    sel.appendChild(o);
  });
  sel.addEventListener("change", function () { radius = +sel.value; poll(); });

  var btn = el("exact");
  if (!navigator.geolocation) btn.hidden = true;
  btn.addEventListener("click", function () {
    navigator.geolocation.getCurrentPosition(function (g) {
      pos = { lat: g.coords.latitude, lon: g.coords.longitude };
      where(W.here);
      btn.hidden = true;
      poll();
    }, function () { where(pos ? around() : W.denied); },
    { maximumAge: 300000, timeout: 15000 });
  });

  poll();
})();
