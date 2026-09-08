# The contribution door

A Cloudflare Worker with a D1 table. `POST /contributions` takes one
answer to an open question after a Turnstile check, or a key sent as
`X-Contribute-Key`; `GET /pull` hands the stored answers to the network
API, which files and checks them. Secrets: `TURNSTILE_SECRET`,
`PULL_TOKEN`, `CONTRIBUTE_KEYS` (JSON, sha256 of key to name).

    npx wrangler d1 execute fp-contribute --remote --file schema.sql
    npx wrangler deploy
