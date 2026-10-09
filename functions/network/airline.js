// The old address, /network/airline.html?icao={ICAO} (served as
// /network/airline), moves to /network/airline/{ICAO}. Without a code it
// stays the plain page, which still reads a #code link in the browser.
export async function onRequestGet(context) {
  const icao = (new URL(context.request.url).searchParams.get("icao") || "").toUpperCase();
  if (/^[A-Z0-9]{3}$/.test(icao)) {
    return Response.redirect(new URL(`/network/airline/${icao}`, context.request.url), 301);
  }
  return context.env.ASSETS.fetch(context.request);
}
