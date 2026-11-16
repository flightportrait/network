/* Planes above me: the aircraft within a radius of one point, nearest
   first, from /v2/point. The position is the visitor's approximate one,
   written onto #above by the edge (functions/network/above.js) from the
   request's own geolocation, until the visitor asks for the exact one.
   It stays in this page: never stored, never sent anywhere but the
   point query and the partner layer's box around it. The browser asks the API itself, so each visitor spends
   their own rate allowance; one query every 20 s, none while hidden.
   Words come from the table below, chosen by <html lang>. */
(function () {
  "use strict";
  var API = window.FP_API || "https://data.flightportrait.com";
  var NM_PER = { mi: 0.868976, km: 0.539957 };
  var WORDS = {
    en: { unit: "mi", radii: [10, 25, 50], near: "Near you", around: "Around ",
          here: "At your location", within: "within", exact: "Use my exact location",
          denied: "Location not shared", ft: "ft",
          none: "No planes within {r} right now. The sky may be quiet, or our antennas don't reach it yet.",
          add: "Add an antenna", via: "Also via " },
    de: { unit: "km", radii: [20, 50, 100], near: "In deiner Nähe", around: "Rund um ",
          here: "An deinem Standort", within: "im Umkreis von", exact: "Meinen genauen Standort verwenden",
          denied: "Standort nicht freigegeben", ft: "ft",
          none: "Gerade keine Flugzeuge im Umkreis von {r}. Vielleicht ist der Himmel ruhig, oder unsere Antennen reichen noch nicht bis hierher.",
          add: "Eine Antenne beitragen", via: "Auch über " },
    fr: { unit: "km", radii: [20, 50, 100], near: "Près de vous", around: "Autour de ",
          here: "À votre position", within: "dans un rayon de", exact: "Utiliser ma position exacte",
          denied: "Position non partagée", ft: "ft",
          none: "Aucun avion dans un rayon de {r} en ce moment. Le ciel est peut-être calme, ou nos antennes ne l\u2019atteignent pas encore.",
          add: "Ajouter une antenne", via: "Aussi via " }
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
  var answered = false; // the API has answered for the current position
  // Said out loud when the answer is empty: a bare page read as broken,
  // most often where the network has no antennas yet.
  var none = document.createElement("p");
  none.id = "none";
  none.hidden = true;
  none.style.cssText = "margin:18px 0 22px;max-width:46ch;font-size:15px;line-height:1.45;color:var(--quiet,#6b665c)";
  el("list").parentNode.insertBefore(none, el("list"));
  // partner networks' aircraft fill the skies our antennas don't reach,
  // credited under the table
  var credits = [];
  var credit = document.createElement("p");
  credit.hidden = true;
  credit.style.cssText = "margin:10px 0 0;font-size:13px;color:var(--quiet,#6b665c)";
  el("list").appendChild(credit);
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
    // a partner's aircraft is shown, never linked to history (DECISIONS.md,
    // partner data: display only)
    var link = cs && a.via == null;
    return "<tr><td class='flight'>" +
      (link ? "<a href='flight.html?callsign=" + encodeURIComponent(cs) + "'>" + esc(cs) + "</a>" : esc(cs)) +
      "</td><td>" + esc(operator(cs)) + "</td><td>" + esc(type) + "</td><td class='flight'>" +
      (a.r && a.via == null ? "<a href='plane/" + esc(a.hex) + "'>" + esc(a.r) + "</a>" : esc(a.r || "")) +
      "</td><td>" + route + "</td><td class='num'>" + alt + "</td><td class='num'>" + dist + "</td></tr>";
  }
  function draw() {
    el("rows").innerHTML = planes.map(row).join("");
    el("list").hidden = !planes.length;
    none.hidden = !(answered && pos && !planes.length);
    var vias = {};
    planes.forEach(function (a) { if (a.via != null && credits[a.via]) vias[a.via] = credits[a.via]; });
    var names = Object.keys(vias).map(function (k) {
      return "<a href='" + esc(vias[k].url) + "' rel='noopener' target='_blank'>" + esc(vias[k].name) + "</a>";
    });
    credit.innerHTML = names.length ? esc(W.via) + names.join(", ") : "";
    credit.hidden = !names.length;
    if (!none.hidden) {
      none.innerHTML = esc(W.none.replace("{r}", radius + " " + W.unit)) +
        " <a href='/network/join'>" + esc(W.add) + "</a>";
    }
  }

  // The partner layer for a box around the point, cut to the radius here
  // (its answer has no distance of its own).
  function partners(p, nm) {
    var dlat = nm / 60, dlon = nm / (60 * Math.max(0.2, Math.cos(p.lat * Math.PI / 180)));
    var box = [p.lon - dlon, p.lat - dlat, p.lon + dlon, p.lat + dlat]
      .map(function (v) { return v.toFixed(3); }).join(",");
    return fetch(API + "/v1/map/partner?bbox=" + box)
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (d) {
        if (!d) return [];
        credits = d.credits || (d.credit ? [d.credit] : []);
        return (d.aircraft || []).map(function (a) {
          a.dst = nmBetween(p.lat, p.lon, a.lat, a.lon);
          return a;
        }).filter(function (a) { return isFinite(a.dst) && a.dst <= nm; });
      });
  }
  function nmBetween(la1, lo1, la2, lo2) {
    var r = Math.PI / 180, dla = (la2 - la1) * r, dlo = (lo2 - lo1) * r;
    var h = Math.sin(dla / 2) * Math.sin(dla / 2) +
      Math.cos(la1 * r) * Math.cos(la2 * r) * Math.sin(dlo / 2) * Math.sin(dlo / 2);
    return 2 * 3440.065 * Math.asin(Math.min(1, Math.sqrt(h)));
  }

  var timer = null;
  function poll() {
    clearTimeout(timer);
    timer = null;
    if (!pos || document.hidden) return;
    var nm = Math.round(radius * NM_PER[W.unit]);
    var p = pos;
    var ours = fetch(API + "/v2/point/" + p.lat.toFixed(3) + "/" + p.lon.toFixed(3) + "/" + nm)
      .then(function (r) {
        if (r.status === 429) return null;
        return r.ok ? r.json() : null;
      });
    var theirs = partners(p, nm).catch(function () { return []; });
    Promise.all([ours, theirs])
      .then(function (both) {
        var d = both[0], extra = both[1];
        if (!d || p !== pos) return;
        answered = true;
        var seen = {};
        (d.ac || []).forEach(function (a) { seen[a.hex] = true; });
        planes = (d.ac || []).concat(extra.filter(function (a) { return !seen[a.hex]; }))
          .filter(function (a) { return a.alt_baro !== "ground"; })
          .sort(function (x, y) { return (x.dst || 0) - (y.dst || 0); });
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
  sel.addEventListener("change", function () { radius = +sel.value; answered = false; draw(); poll(); });

  var btn = el("exact");
  if (!navigator.geolocation) btn.hidden = true;
  btn.addEventListener("click", function () {
    navigator.geolocation.getCurrentPosition(function (g) {
      pos = { lat: g.coords.latitude, lon: g.coords.longitude };
      answered = false;
      planes = [];
      draw();
      where(W.here);
      btn.hidden = true;
      poll();
    }, function () { where(pos ? around() : W.denied); },
    { maximumAge: 300000, timeout: 15000 });
  });

  poll();
})();
