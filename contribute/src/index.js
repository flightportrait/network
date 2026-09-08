// The contribution door. Runs at the edge; the network's box never
// accepts a write from the internet, it pulls from here.
//
//   POST /contributions   one answer to an open question
//   GET  /pull            the box, with its token, pages through them
//
// Stored: the answer, the name the sender chose, their note, the key
// name if they sent a key. Never an address.

const CALLSIGN = /^[A-Z]{3}\d{1,4}[A-Z]{0,2}$/;
const CODE = /^[A-Z]{3,4}$/;
const DAY_CEILING = 5000;
const PULL_MAX = 500;

const CORS = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Methods": "POST, OPTIONS",
  "Access-Control-Allow-Headers": "Content-Type, X-Contribute-Key",
  "Access-Control-Max-Age": "600",
};

function reply(status, body, extra = {}) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json", "Cache-Control": "no-store",
               ...CORS, ...extra },
  });
}

function refuse(status, code, detail) {
  return reply(status, { error: code, detail });
}

function clean(value, max) {
  if (typeof value !== "string") return null;
  const text = value.replace(/\s+/g, " ").trim().slice(0, max);
  return text || null;
}

async function sha256(text) {
  const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(text));
  return [...new Uint8Array(digest)].map((b) => b.toString(16).padStart(2, "0")).join("");
}

async function keyName(env, header) {
  if (!header || !env.CONTRIBUTE_KEYS) return null;
  let keys;
  try { keys = JSON.parse(env.CONTRIBUTE_KEYS); } catch { return null; }
  return keys[await sha256(header.trim())] || null;
}

async function human(env, token, ip) {
  if (!env.TURNSTILE_SECRET) return false;
  const form = new FormData();
  form.append("secret", env.TURNSTILE_SECRET);
  form.append("response", token || "");
  if (ip) form.append("remoteip", ip);
  const res = await fetch("https://challenges.cloudflare.com/turnstile/v0/siteverify",
                          { method: "POST", body: form });
  const out = await res.json().catch(() => ({}));
  return out.success === true;
}

async function question(env, callsign) {
  const res = await fetch(`${env.API}/v1/gaps/${callsign}`,
                          { cf: { cacheTtl: 600, cacheEverything: true } });
  if (res.status === 404) return null;
  if (!res.ok) throw new Error("api");
  return res.json();
}

async function commercial(env, code) {
  const res = await fetch(`${env.API}/v1/airports/${code}`,
                          { cf: { cacheTtl: 86400, cacheEverything: true } });
  if (res.status === 404) return null;
  if (!res.ok) throw new Error("api");
  const airport = await res.json();
  return airport.role === "commercial" ? airport : null;
}

async function contribute(request, env) {
  let body;
  try { body = await request.json(); } catch { return refuse(422, "invalid_request", "JSON body expected"); }
  const callsign = clean(body.callsign, 12)?.toUpperCase();
  if (!callsign || !CALLSIGN.test(callsign)) {
    return refuse(422, "invalid_request", "callsign must be a flight number");
  }
  const origin = clean(body.origin, 4)?.toUpperCase() || null;
  const dest = clean(body.dest, 4)?.toUpperCase() || null;
  if ((origin && !CODE.test(origin)) || (dest && !CODE.test(dest))) {
    return refuse(422, "invalid_request", "airport codes are 3 or 4 letters");
  }
  const key = await keyName(env, request.headers.get("X-Contribute-Key"));
  if (!key) {
    const ok = await human(env, body.turnstile, request.headers.get("CF-Connecting-IP"));
    if (!ok) return refuse(403, "not_verified", "verification failed");
  }

  let gap;
  try { gap = await question(env, callsign); } catch { return refuse(503, "upstream_unavailable", "the network is not answering"); }
  if (!gap) return refuse(422, "invalid_request", "no open question for this callsign");
  const missing = gap.side === "dest" ? dest : origin;
  if (!missing) return refuse(422, "invalid_request", `the missing ${gap.side} is required`);
  const given = gap.side === "dest" ? origin : dest;
  if (given && given !== gap.known) {
    return refuse(422, "invalid_request", `the ${gap.side === "dest" ? "origin" : "destination"} is ${gap.known}`);
  }
  let airport;
  try { airport = await commercial(env, missing); } catch { return refuse(503, "upstream_unavailable", "the network is not answering"); }
  if (!airport) return refuse(422, "invalid_request", `${missing} is not a commercial airport we know`);
  const code = airport.iata || airport.ident;
  if (code === gap.known) return refuse(422, "invalid_request", "a flight does not go where it came from");

  const today = await env.DB.prepare(
    "SELECT COUNT(*) AS n FROM submissions WHERE received_at > datetime('now', '-1 day')"
  ).first("n");
  if (today >= DAY_CEILING) {
    return refuse(503, "backlog", "too many answers today, try tomorrow",
                  { "Retry-After": "3600" });
  }

  const validFrom = clean(body.valid_from, 10);
  const row = await env.DB.prepare(
    "INSERT INTO submissions (callsign, origin, dest, note, handle, key_name, valid_from)" +
    " VALUES (?, ?, ?, ?, ?, ?, ?) RETURNING id"
  ).bind(callsign,
         gap.side === "dest" ? gap.known : code,
         gap.side === "dest" ? code : gap.known,
         clean(body.note, 280), clean(body.handle, 40), key,
         validFrom && /^\d{4}-\d{2}-\d{2}$/.test(validFrom) ? validFrom : null).first();
  return reply(202, { id: row.id, status: "received", callsign,
                      origin: gap.side === "dest" ? gap.known : code,
                      dest: gap.side === "dest" ? code : gap.known });
}

async function pull(request, env) {
  const auth = request.headers.get("Authorization") || "";
  if (!env.PULL_TOKEN || auth !== `Bearer ${env.PULL_TOKEN}`) {
    return refuse(401, "unauthorized", "pull token required");
  }
  const url = new URL(request.url);
  const after = Math.max(0, parseInt(url.searchParams.get("after") || "0", 10) || 0);
  const limit = Math.min(PULL_MAX, Math.max(1, parseInt(url.searchParams.get("limit") || "500", 10) || 500));
  const { results } = await env.DB.prepare(
    "SELECT id, received_at, callsign, origin, dest, note, handle, key_name, valid_from" +
    " FROM submissions WHERE id > ? ORDER BY id LIMIT ?"
  ).bind(after, limit).all();
  return reply(200, { submissions: results });
}

export default {
  async fetch(request, env) {
    const { pathname } = new URL(request.url);
    if (request.method === "OPTIONS") return new Response(null, { status: 204, headers: CORS });
    if (pathname === "/contributions" && request.method === "POST") return contribute(request, env);
    if (pathname === "/pull" && request.method === "GET") return pull(request, env);
    if (pathname === "/healthz") return reply(200, { ok: true });
    return refuse(404, "not_found", "no such path");
  },
};
