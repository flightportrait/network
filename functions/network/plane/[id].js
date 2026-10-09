// /network/plane/{hex}: the airframe page, rendered at the edge.
// /network/plane/{registration} redirects to its hex, which is the
// stable address (a registration can move to another airframe).
import { airframePage, hexForReg } from "../../_lib/plane.js";

export async function onRequestGet(context) {
  const id = String(context.params.id || "");
  const hex = id.toLowerCase();
  if (/^[0-9a-f]{6}$/.test(hex)) {
    if (id !== hex) return Response.redirect(new URL(`/network/plane/${hex}`, context.request.url), 301);
    return airframePage(context, hex);
  }
  if (/^[A-Za-z0-9-]{2,10}$/.test(id)) {
    let found;
    try {
      found = await hexForReg(id);
    } catch (e) {
      return new Response("Busy, try again shortly.", { status: 503, headers: { "Retry-After": "600" } });
    }
    if (found) return Response.redirect(new URL(`/network/plane/${found}`, context.request.url), 301);
  }
  return context.env.ASSETS.fetch(new URL("/404", context.request.url))
    .then((r) => new Response(r.body, { status: 404, headers: r.headers }));
}
