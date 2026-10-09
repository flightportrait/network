// The old address, /network/plane.html?hex={hex} (served as
// /network/plane), moves to /network/plane/{hex}. Without a hex it stays
// the plain page, which still reads a #hex link in the browser.
export async function onRequestGet(context) {
  const hex = (new URL(context.request.url).searchParams.get("hex") || "").toLowerCase();
  if (/^[0-9a-f]{6}$/.test(hex)) {
    return Response.redirect(new URL(`/network/plane/${hex}`, context.request.url), 301);
  }
  return context.env.ASSETS.fetch(context.request);
}
