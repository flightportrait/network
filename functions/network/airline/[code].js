// /network/airline/{ICAO}: the airline page, rendered at the edge.
import { airlinePage } from "../../_lib/airline.js";
import { notFound } from "../../_lib/edge.js";

export async function onRequestGet(context) {
  const code = String(context.params.code || "");
  const icao = code.toUpperCase();
  if (!/^[A-Z0-9]{3}$/.test(icao)) return notFound(context);
  if (code !== icao) return Response.redirect(new URL(`/network/airline/${icao}`, context.request.url), 301);
  return airlinePage(context, icao);
}
