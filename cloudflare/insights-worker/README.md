# Crucible Run Insights — Cloudflare Worker (v1.2.0 cloud backend, **Phase 0**)

The cloud half of the Run Insights ledger. The Python client
(`DualWriteBackend`, **Phase 1 — not in this scaffold**) writes events to the
local JSONL ledger for durability and asynchronously batches them to this
Worker, which stores indexed metadata in **D1** (edge SQLite) and large
payloads in **R2** (object storage, zero egress fees).

This directory is **self-contained and additive** — it does not touch the
`crucible/` Python runtime and does not affect the pytest baseline.

---

## Why this shape

- **Local is the source of truth.** The pipeline never blocks on the network;
  the cloud copy is eventually-consistent.
- **`content_id` (`sha256` of canonical JSON) is the D1 `PRIMARY KEY`.**
  `INSERT OR IGNORE` makes re-sending an event a no-op, so the client can use
  cheap at-least-once delivery and still get effectively-once storage.
- **The canonical-JSON algorithm is frozen** in
  `crucible/features/run_insights/backends.py` and must be **byte-identical**
  on both sides, or the same event stored from two paths would dedup as two
  rows. Parity is enforced by `npm test` here and
  `tests/test_run_insights/test_js_canonical_parity.py` in the Python repo
  (shared fixtures + cross-language `content_id` anchors).

---

## Layout

```
cloudflare/insights-worker/
├── wrangler.toml            # Worker + D1 + R2 bindings, vars
├── package.json             # scripts; only devDependency is wrangler
├── .dev.vars.example        # local secret template (copy → .dev.vars)
├── migrations/0001_init.sql # frozen D1 schema (idempotent)
├── src/
│   ├── canonical.js         # FROZEN canonicalJson + contentId (parity-critical)
│   ├── auth.js              # constant-time Bearer check (fail-closed)
│   ├── ingest.js            # validate + tamper-check + R2 spill + D1 insert
│   └── index.js             # router / HTTP API
├── test/canonical.test.js   # parity golden vectors (node --test, zero deps)
└── scripts/smoke.mjs        # end-to-end smoke vs a running Worker (zero deps)
```

---

## Prerequisites

- **Node ≥ 20** (global `crypto.subtle` / `fetch` used by tests and smoke).
- A **Cloudflare account** (free tier is sufficient for a single operator).
- `npm install` (installs `wrangler` locally — no global install needed).

---

## 1. Run the parity test first (no account needed)

```bash
cd cloudflare/insights-worker
npm install
npm test
```

This runs the canonical-JSON golden vectors and the cross-language
`content_id` anchors. It must pass before deploying — it is the single most
important correctness guard for cloud dedup.

---

## 2. Provision D1 + R2

```bash
npx wrangler login

# D1 — copy the printed database_id into wrangler.toml (database_id = "...")
npm run db:create

# R2 bucket for blob spillover
npx wrangler r2 bucket create crucible-insights-blobs

# Apply the schema (local emulator AND remote)
npm run db:migrate:local
npm run db:migrate:remote
```

## 3. Set the API token (secret)

```bash
# Production secret (used by `wrangler deploy`):
npx wrangler secret put CRUCIBLE_RUN_INSIGHTS_API_TOKEN
# → paste a long random token (e.g. `openssl rand -hex 32`)

# Local dev: copy the template and edit the value
cp .dev.vars.example .dev.vars        # .dev.vars is gitignored
```

## 4. Local dev + smoke

```bash
# Terminal A
npm run dev          # wrangler dev on http://127.0.0.1:8787 (uses local D1/R2)

# Terminal B
npm run smoke        # exercises auth, ingest, dedup, gzip batch, R2 spill, query
# Against a deployed URL instead:
#   INSIGHTS_URL=https://crucible-insights.<sub>.workers.dev \
#   INSIGHTS_TOKEN=<your token> npm run smoke
```

## 5. Deploy

```bash
npm run deploy
npm run tail         # live logs (errors are logged here, never in responses)
```

---

## HTTP API (frozen)

| Method | Path | Auth | Body / Query |
|---|---|---|---|
| `GET` | `/` , `/health` | none | liveness `{service,version,status}` |
| `POST` | `/v1/insights/events` | Bearer | `{ "event": { … } }` |
| `POST` | `/v1/insights/batch` | Bearer | gzip of `{ "events": [ … ] }` (`Content-Encoding: gzip`) |
| `GET` | `/v1/insights/events` | Bearer | `?run_id=&stream=&since=&cursor=&limit=` |
| `GET` | `/v1/insights/events/:content_id` | Bearer | returns row + reconstructed full `event` |
| `GET` | `/v1/insights/runs/:run_id/summary` | Bearer | totals + per-stream/outcome breakdown |

- **Tamper-evidence:** the Worker recomputes `content_id` from the canonical
  bytes; a client-supplied `content_id` that disagrees is rejected (`422
  content_id_mismatch`). A client may also omit `content_id` and let the
  Worker compute it.
- **Pagination:** `next_cursor` is an opaque, stable `(ts, content_id)` token;
  pass it back as `?cursor=`.

---

## Connecting the Python client (Phase 1 — preview)

Phase 1 fills in `DualWriteBackend` in
`crucible/features/run_insights/backends.py`. Once shipped, the operator sets
(already reserved in `.env.example`):

```ini
CRUCIBLE_RUN_INSIGHTS_BACKEND=dual
CRUCIBLE_RUN_INSIGHTS_API_URL=https://crucible-insights.<sub>.workers.dev
CRUCIBLE_RUN_INSIGHTS_API_TOKEN=<same token as the Worker secret>
# optional tuning (defaults shown):
# CRUCIBLE_RUN_INSIGHTS_API_TIMEOUT_SECONDS=10
# CRUCIBLE_RUN_INSIGHTS_API_MAX_RETRIES=3
# CRUCIBLE_RUN_INSIGHTS_API_BATCH_FLUSH_SECONDS=30
```

The client writes locally (synchronous + fsync), then a background daemon
batches un-synced events to `/v1/insights/batch`, advancing a persisted
`(ts, content_id)` sync cursor on success. Cloud failures only delay sync —
they never block the pipeline. Local prune must respect the un-synced
high-water mark so events are never deleted before upload.

---

## Operational notes

- **Subrequest limit.** Each R2 spill = 1 subrequest; the Workers **free** plan
  caps subrequests at **50** per invocation (1000 on paid). `MAX_BATCH_SIZE`
  (default 100) bounds batch size; if your events are large (many R2 spills),
  lower it so `(spills + 1 D1 batch)` stays under the cap.
- **Number precision.** Payloads must not contain integers above
  `2^53 - 1` — JSON round-tripping through a JS number would lose precision and
  the recomputed `content_id` would mismatch (rejected loudly, not silently).
  Object keys must be **ASCII** (key-sort parity). The ledger schema satisfies
  both already.
- **Security.** The Bearer token is the only gate — use a long random value,
  rotate it (`wrangler secret put` + update the client), and never commit
  `.dev.vars`. For defense-in-depth you can additionally front the Worker with
  Cloudflare Access. Responses never leak internals; details go to
  `wrangler tail`.
- **Cost.** Batched uploads keep a single operator comfortably inside the
  Workers / D1 / R2 free tiers (R2 has no egress fees). Verify current limits
  on the Cloudflare pricing page before relying on them.

---

## Regenerating the parity anchors (only if the algorithm legitimately changes)

The `content_id` anchors in `test/canonical.test.js` come from the Python side:

```bash
# from the crucible repo root
python -c "from crucible.features.run_insights.schema import compute_content_id; print(compute_content_id({'a':1,'b':'x'}))"
```

If you ever change the canonical algorithm, you must change **both**
`src/canonical.js` and `crucible/features/run_insights/schema.py`, then
regenerate the anchors and the shared fixtures on both sides and bump
`SCHEMA_VERSION`.
