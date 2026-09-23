/* Top-down aircraft silhouettes for MapLibre, one per silhouette
   category of assets/types.json (narrow, wide, wide4, bizjet, ga, prop,
   heli). Drawn parametrically, nose north so icon-rotate is the track.
   The same geometry as the live map's icons.

   fpPlaneIcons.image(kind, size, colour)  ImageData for map.addImage
   fpPlaneIcons.kinds                      every category
   fpPlaneIcons.kindOf(aircraft, types)    category from type, else emitter */
(function () {
  "use strict";
  function drawAirframe(g, p) {
    // wings and tailplane as one symmetric polygon, then the fuselage
    // capsule over it
    g.beginPath();
    g.moveTo(p.wingX + p.wingChord, p.fus / 2);
    g.lineTo(p.wingX - p.sweep, p.span);
    g.lineTo(p.wingX - p.sweep - p.tipChord, p.span);
    g.lineTo(p.wingX - p.wingChord * 0.4, p.fus / 2);
    g.lineTo(p.tailX + p.tailChord, p.fus / 2);
    g.lineTo(p.tailX - p.tailSweep, p.tailSpan);
    g.lineTo(p.tailX - p.tailSweep - p.tailChord, p.tailSpan);
    g.lineTo(p.tailX - p.tailChord, p.fus / 2);
    g.lineTo(-p.len / 2, p.fus / 2);
    g.lineTo(-p.len / 2, -p.fus / 2);
    g.lineTo(p.tailX - p.tailChord, -p.fus / 2);
    g.lineTo(p.tailX - p.tailSweep - p.tailChord, -p.tailSpan);
    g.lineTo(p.tailX - p.tailSweep, -p.tailSpan);
    g.lineTo(p.tailX + p.tailChord, -p.fus / 2);
    g.lineTo(p.wingX - p.wingChord * 0.4, -p.fus / 2);
    g.lineTo(p.wingX - p.sweep - p.tipChord, -p.span);
    g.lineTo(p.wingX - p.sweep, -p.span);
    g.lineTo(p.wingX + p.wingChord, -p.fus / 2);
    // nose
    g.lineTo(p.len / 2 - p.fus, -p.fus / 2);
    g.quadraticCurveTo(p.len / 2, -p.fus / 2, p.len / 2, 0);
    g.quadraticCurveTo(p.len / 2, p.fus / 2, p.len / 2 - p.fus, p.fus / 2);
    g.closePath();
    g.fill(); g.stroke();
  }
  var AIRFRAMES = {
    // len: nose-tail; span: half wingspan; wingX: wing root center;
    // sweep: tip set-back; chords: root/tip width; tail mirrors wing
    narrow: { len: 24, fus: 3.0, span: 11.5, wingX: 1.5, sweep: 6,
              wingChord: 3.4, tipChord: 1.2, tailX: -9.5, tailSpan: 4.6,
              tailSweep: 2.6, tailChord: 1.6 },
    wide:   { len: 27, fus: 3.8, span: 13, wingX: 1.5, sweep: 7,
              wingChord: 4.2, tipChord: 1.3, tailX: -10.5, tailSpan: 5.2,
              tailSweep: 3, tailChord: 1.8 },
    wide4:  { len: 28, fus: 4.4, span: 14.5, wingX: 1, sweep: 8,
              wingChord: 5, tipChord: 1.4, tailX: -11, tailSpan: 5.6,
              tailSweep: 3.2, tailChord: 1.9 },
    bizjet: { len: 15, fus: 2.2, span: 7.5, wingX: -1, sweep: 4,
              wingChord: 2.2, tipChord: 0.9, tailX: -6, tailSpan: 3.4,
              tailSweep: 1.8, tailChord: 1.1 },
    ga:     { len: 11, fus: 2.0, span: 8, wingX: 1.5, sweep: 0.6,
              wingChord: 2.2, tipChord: 1.5, tailX: -4.4, tailSpan: 3.4,
              tailSweep: 0.5, tailChord: 1.1 },
    prop:   { len: 16, fus: 2.6, span: 10, wingX: 1.5, sweep: 1,
              wingChord: 2.6, tipChord: 1.6, tailX: -6.5, tailSpan: 4.2,
              tailSweep: 0.8, tailChord: 1.3 }
  };
  function iconImage(kind, size, colour) {
    var c = document.createElement("canvas");
    c.width = c.height = size;
    var g = c.getContext("2d");
    g.translate(size / 2, size / 2);
    g.rotate(-Math.PI / 2);            // icon-rotate 0 = pointing north
    g.scale(size / 64, size / 64);
    g.scale(2, 2);                     // geometry is in 32-unit space
    g.fillStyle = colour || "#B8402E";
    g.strokeStyle = "rgba(245,241,230,.9)";
    g.lineWidth = 1.1;
    if (kind === "heli") {
      g.strokeStyle = "rgba(184,64,46,.55)";
      g.beginPath(); g.arc(1, 0, 8, 0, Math.PI * 2); g.stroke();
      g.strokeStyle = "rgba(245,241,230,.9)";
      g.beginPath();
      g.moveTo(-3, 1.1); g.lineTo(-11, 0.7); g.lineTo(-11, -0.7);
      g.lineTo(-3, -1.1);
      g.moveTo(5, 0);
      g.quadraticCurveTo(5, 2.4, 1, 2.4); g.lineTo(-2, 1.6);
      g.lineTo(-2, -1.6); g.lineTo(1, -2.4);
      g.quadraticCurveTo(5, -2.4, 5, 0);
      g.fill(); g.stroke();
    } else {
      drawAirframe(g, AIRFRAMES[kind]);
    }
    return g.getImageData(-size / 2, -size / 2, size, size);
  }
  var KINDS = ["narrow", "wide", "wide4", "bizjet", "ga", "prop", "heli"];
  var EMITTER = { A1: "ga", A2: "bizjet", A3: "narrow", A4: "narrow",
                  A5: "wide", A6: "bizjet", A7: "heli" };
  function kindOf(a, types) {
    var entry = a.t && types && types[a.t];
    var k = (entry && entry[1]) || EMITTER[a.category] || "narrow";
    return KINDS.indexOf(k) >= 0 ? k : "narrow";
  }
  window.fpPlaneIcons = { image: iconImage, kinds: KINDS, kindOf: kindOf };
})();
