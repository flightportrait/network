// Planes above me: the static page with the visitor's approximate
// position (Cloudflare's own geolocation of the request) written onto
// #above, so the list fills without a permission prompt. The position is
// rounded, never stored or logged, and the response is never cached:
// it belongs to one visitor. The browser asks the API itself.
import { SetAttr } from "./edge.js";

export async function abovePage(context, staticPath) {
  const page = await context.env.ASSETS.fetch(new URL(staticPath, context.request.url));
  const cf = context.request.cf || {};
  const lat = parseFloat(cf.latitude), lon = parseFloat(cf.longitude);
  let rw = new HTMLRewriter();
  if (isFinite(lat) && isFinite(lon)) {
    rw = rw.on("#above", new SetAttr("data-lat", lat.toFixed(2)))
      .on("#above", new SetAttr("data-lon", lon.toFixed(2)));
    if (cf.city) rw = rw.on("#above", new SetAttr("data-city", String(cf.city)));
  }
  const res = new Response(rw.transform(page).body, {
    status: page.status,
    headers: page.headers,
  });
  res.headers.set("Content-Type", "text/html; charset=utf-8");
  res.headers.set("Cache-Control", "private, no-store");
  return res;
}
