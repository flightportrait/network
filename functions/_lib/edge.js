// What every page rendered at the edge shares: the API, escaping, the
// HTMLRewriter handlers, and the cache in front of it all.

export const API = "https://data.flightportrait.com";
export const SITE = "https://flightportrait.com";
export const TTL = 3600; // matches the API's s-maxage for reference data

export function esc(s) {
  return String(s == null ? "" : s).replace(/[&<>"']/g, (c) => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

export function jsonLd(obj) {
  return `<script type="application/ld+json">${JSON.stringify(obj).replace(/</g, "\\u003c")}</script>`;
}

export function breadcrumbs(items) {
  return jsonLd({
    "@context": "https://schema.org",
    "@type": "BreadcrumbList",
    itemListElement: items.map((x, i) => ({ "@type": "ListItem", position: i + 1, ...x })),
  });
}

export class SetText {
  constructor(value) { this.value = value; }
  element(e) { e.setInnerContent(this.value); }
}
export class SetHtml {
  constructor(value) { this.value = value; }
  element(e) { e.setInnerContent(this.value, { html: true }); }
}
export class SetAttr {
  constructor(name, value) { this.attr = name; this.value = value; }
  element(e) { e.setAttribute(this.attr, this.value); }
}
export class RemoveAttr {
  constructor(name) { this.attr = name; }
  element(e) { e.removeAttribute(this.attr); }
}
export class Append {
  constructor(value) { this.value = value; }
  element(e) { e.append(this.value, { html: true }); }
}

// The head every rendered page gets: title, description, canonical, and
// noindex when the entity has nothing to show yet.
export function headRewriter(title, desc, url, extraHead, indexable) {
  return new HTMLRewriter()
    .on("title", new SetText(title))
    .on('meta[name="description"]', new SetAttr("content", desc))
    .on('meta[property="og:title"]', new SetAttr("content", title))
    .on('meta[property="og:description"]', new SetAttr("content", desc))
    .on("head", new Append(
      `<link rel="canonical" href="${url}">` +
      `<meta property="og:url" content="${url}">` +
      (indexable ? "" : '<meta name="robots" content="noindex">') +
      (extraHead || "")));
}

// null for a 404; throws when the API is busy or down
export async function api(path) {
  const r = await fetch(API + path, { cf: { cacheTtl: TTL, cacheEverything: true } });
  if (r.status === 404) return null;
  if (!r.ok) throw new Error(`api ${r.status}`);
  return r.json();
}

// Serve from the cache, else build. build(page) returns the rendered
// Response, null for "no such entity", or throws when the API fails.
export async function cachedPage(context, url, staticPath, build) {
  const cache = caches.default;
  const key = new Request(url);
  const hit = await cache.match(key);
  if (hit) return hit;

  const page = await context.env.ASSETS.fetch(new URL(staticPath, context.request.url));
  let body;
  try {
    body = await build(page);
  } catch (e) {
    // API busy or down. A 503 tells crawlers to come back; a visitor's
    // browser still shows the page, its script fetching on its own.
    const res = new Response(page.body, { status: 503, headers: page.headers });
    res.headers.set("Retry-After", "600");
    return res;
  }
  if (!body) {
    const res = new Response(page.body, { status: 404, headers: page.headers });
    res.headers.set("X-Robots-Tag", "noindex");
    return res;
  }
  const res = new Response(body.body, {
    status: 200,
    headers: {
      "Content-Type": "text/html; charset=utf-8",
      "Cache-Control": `public, max-age=300, s-maxage=${TTL}`,
    },
  });
  context.waitUntil(cache.put(key, res.clone()));
  return res;
}

export function notFound(context) {
  return context.env.ASSETS.fetch(new URL("/404", context.request.url))
    .then((r) => new Response(r.body, { status: 404, headers: r.headers }));
}
