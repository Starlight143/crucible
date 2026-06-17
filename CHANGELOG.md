# Changelog

All notable changes to this project are documented in this file.
Versioning follows [Semantic Versioning](https://semver.org/). The first public release was **v1.0.0**.

---

## [v1.2.2] — 2026-06-17

Fixes a direction-debate extraction failure where the Judge model emitted the
`DirectionDecision.options` payload in a shape strict validation rejected. This
caused repeated `_extract_pydantic_from_result` lenient-retry debug spam and
burned the reformat/salvage LLM budget on every affected run. Defensive and
additive only — well-formed payloads are untouched and there are no schema, env,
CLI, or API changes.

### Fixed
- **`DirectionDecision.options` shape coercion.** A new
  `model_validator(mode="before")` on `DirectionDecision`
  (`crucible/modules/section_03_models_and_context.py`) normalises two
  recoverable LLM deviations *before* field validation, so every construction
  path benefits at once (primary extraction, salvage, reformat, crewAI
  `output_pydantic`, and cache deserialisation):
  1. `options` emitted as a **mapping keyed by direction letter**
     (`{"A": {...}, …}`) instead of the declared list → unwrapped to a list,
     backfilling each item's `key` from the mapping key only when it is absent.
  2. `options` emitted as a **list whose items omit `key`** → `key` injected by
     position (index 0 → `"A"`).
  The validator never overwrites a `key` the model already supplied, leaves
  non-dict items in place for the schema to reject with a clear error, and
  defers final A–G completeness to the existing `_normalize_direction_decision`.
  A malformed or partial payload therefore degrades to the prior behaviour
  (extraction returns `None` and the existing fallbacks run) rather than
  producing a wrong decision.

### Tests
- `tests/test_direction_decision_options_coercion.py` (15 checks): both failure
  shapes, dict-keyed values with and without `key`, list-missing-`key` keyed by
  position, the never-overwrite and byte-for-byte-untouched guarantees for
  well-formed input, non-dict rejection, and the full `extract_direction_decision`
  path no longer emitting the lenient-retry debug line.

### Validation
- `python -m pytest tests -q -p no:cacheprovider` → **3364 passed, 2 skipped**.
  `smoke_test.py` and `run_crucible.py --self-check` OK.

### Compatibility
- Fully additive. Behaviour for well-formed payloads is byte-for-byte identical;
  the only change is that two previously-unparseable `options` shapes now parse
  instead of falling through to the fallback path.

## [v1.2.1] — 2026-06-17

Multi-contributor **self-service opening-up** for the Run Insights cloud backend
(Phase A): per-contributor tokens, a self-service GitHub-OAuth signup that issues
a `contributor` token, a strict event-format gate on untrusted uploads, an
aggregate/metadata-only contributor read surface, an operator delete + stats
surface, and the signup + admin pages served directly from the Worker. All
additive — the default `local` backend and any existing single-admin-token
deployment are unchanged, and the operator's own dual-write path is unaffected
(admin writes bypass the new enum gate so a future `EventKind` can never
self-lock the operator).

### Added — contributor scope + self-service signup (Worker)
- **`contributor` scope** (= ingest + read). `migrations/0007_contributor_scope.sql`
  rebuilds the `api_tokens` `CHECK` to add it, preserving every existing row.
  Contributor uploads that pass the format gate **auto-approve**
  (`trust_state='approved'`); the single-purpose `ingest` scope still quarantines
  to `staged`.
- **GitHub-OAuth self-service signup** (`src/oauth_github.js` + `GET
  /oauth/github/start` + `GET /oauth/github/callback`): CSRF `state` cookie,
  authorization-code exchange, a stable `contributor_id = gh_<numeric-id>`,
  banned-account refusal, and revoke-then-reissue of a single `contributor` token
  shown exactly once. Disabled (HTTP 503) until the operator sets the
  `GITHUB_OAUTH_CLIENT_ID` / `GITHUB_OAUTH_CLIENT_SECRET` Worker secrets.
- **Pages served from the Worker** (`src/pages.js`, same-origin ⇒ no CORS): `GET
  /` signup landing (GitHub sign-in) and `GET /console` operator admin console
  (corpus counts, a filterable records browser, multi-select delete, and bulk
  delete). The admin token is entered client-side and kept only in
  `sessionStorage`.

### Added — ingest gate, contributor reads, admin tools (Worker)
- **Strict event-format gate** (`src/event_shape.js`) on every **non-admin**
  write: `mode`, `kind`, `kind`↔`stream` consistency, ISO-8601 `ts`, integer
  `schema_version`, and `outcome` shape are validated against the values Crucible
  actually emits (pinned to `schema.py`). Admin/owner writes **bypass** the enum
  allowlist so a future `EventKind`/mode can never self-lock the operator's
  dual-write.
- **Contributor read surface** (`src/corpus.js`): `GET /v1/insights/corpus/stats`
  (totals + by stream/mode/outcome + top projects) and `GET /v1/insights/corpus`
  (paginated index columns). Both are **approved-only** and never expose raw
  payloads or contributor identity; raw `GET /v1/insights/events` stays
  admin-only.
- **Operator tools** (`src/admin.js`): `POST /v1/admin/events/delete` removes ANY
  row (by `content_ids` | `run_id` | `contributor_id`, any `trust_state`, with
  best-effort R2 cleanup) — distinct from the staged-only reject — and `GET
  /v1/admin/stats` returns corpus-wide counts (by stream/trust_state/mode/
  contributor).

### Changed
- `src/ingest.js` `prepareEvent` gained an `opts.strict` flag; the router
  (`src/index.js`) passes `strict = !isAdmin`, stamps contributor writes
  `approved`, and treats `contributor` as both ingest and read for scope checks.
- `GET /` now serves the signup page (HTML); liveness is `GET /health` only (the
  Python client already polls `/health`).
- `wrangler.toml`: added the `SIGNUP_DAILY_QUOTA` var and documented the two
  GitHub OAuth Worker secrets.

### Tests
- Worker (`node --test`) → **127** checks: new `test/event_shape.test.js` (gate +
  structural pins against the nine `schema.py` EventKinds),
  `test/contributor_scope.test.js` (full access-control matrix incl. the admin
  gate-bypass), `test/delete.test.js`, `test/corpus.test.js`, and
  `test/oauth.test.js` (helpers with an injectable fetch).

### Validation
- `npm test` → **127/127** green; `wrangler deploy --dry-run` bundles cleanly.
- Deployed to a live account (D1-only, no R2) with migration 0007 applied
  **before** the deploy. Live end-to-end verified: GitHub sign-in → contributor
  token → write (format-gated, auto-approved) → read aggregates; raw read and
  delete correctly return 403; signup returns 503 until the OAuth secrets are
  set. Smoke data was purged so the corpus stays clean.

### Compatibility
- Drop-in for v1.2.0. No new dependency; the `local` default is unchanged. A
  pre-Phase-A single-admin-token deployment keeps working (legacy secret →
  admin), and existing `ingest`/`read` tokens are unaffected. Apply migration
  0007 **before** deploying the new Worker (the rebuilt `api_tokens` `CHECK`).

## [v1.2.0] — 2026-06-09

Cloud backend for the Run Insights ledger — a **Cloudflare Worker + D1** mirror
of the local JSONL ledger (**R2 optional**, see below). The local ledger stays
the **source of truth**
(synchronous fsync on the hot path); a background daemon asynchronously batches
un-synced events to the Worker, which dedups on `content_id`
(`INSERT OR IGNORE`) so at-least-once delivery becomes effectively-once storage.
Cloud failure only delays sync — it never blocks the pipeline and never drops
local data. Default is unchanged (`CRUCIBLE_RUN_INSIGHTS_BACKEND=local`); the
cloud path is inert unless an operator opts in with `dual`/`cloudflare` plus an
API URL + token. All changes are additive.

### Added — Cloudflare Worker (Phase 0, `cloudflare/insights-worker/`)
- Self-contained Worker (`wrangler.toml`, `package.json` — the only
  devDependency is `wrangler`, Node ≥ 20) backed by a **D1** database
  (`crucible_insights`, indexed metadata + the full event JSON stored inline).
  **R2 is optional** and ships commented-out: the Worker auto-detects the `BLOBS`
  binding — present ⇒ events above `INLINE_MAX_BYTES` (4096) spill to
  `crucible-insights-blobs`; absent (**the default — so deploy needs no credit
  card**) ⇒ every event stores inline in D1, with an event above a safe per-value
  ceiling (`D1_VALUE_CEILING`, ~950 KB) rejected so it never breaks the batch (it
  stays in the durable local ledger).
- `migrations/0001_init.sql` — the frozen `insight_events` schema (`content_id`
  `PRIMARY KEY`) plus four query indexes, idempotent (`IF NOT EXISTS`).
- `src/canonical.js` — the **frozen** `canonicalJson` + `contentId` algorithm,
  byte-identical to the Python side; parity is pinned on both sides
  (`test/canonical.test.js`: 17 fixtures + 3 cross-language `sha256` anchors, and
  `tests/test_run_insights/test_js_canonical_parity.py`).
- `src/auth.js` (constant-time Bearer check, fail-closed when no token is
  configured), `src/ingest.js` (validate → recompute `content_id` tamper-check →
  inline-or-R2 routing → `INSERT OR IGNORE`, storing the **full** event JSON
  losslessly), and `src/index.js` (router: unauthed `GET /` + `/health`;
  Bearer-gated `POST /v1/insights/events|batch` with gzip, `GET
  /v1/insights/events` with a stable `(ts, content_id)` cursor,
  `…/events/:content_id`, and `…/runs/:run_id/summary`). All D1 queries are
  parameterised via `.bind()` — no string-built SQL.
- `scripts/smoke.mjs` — a zero-dependency end-to-end check (auth, ingest, dedup,
  tamper rejection, gzip batch, large-payload store + read-back, query, summary)
  and a `README.md` deploy guide.

### Added — Python cloud client (Phase 1)
- `crucible/features/run_insights/cloud_sync.py` — `CloudSyncClient`
  (`http(s)`-only, redirects **not** followed, bearer token never logged, gzip
  `post_batch`, `health`, `get_events`/`get_event`) and `CloudSyncWorker` (a
  daemon that drains the local ledger, persists a per-stream `(ts, content_id)`
  cursor for crash-safe resume, and exposes an exact `unsynced_count`).
- `backends.py` — real `DualWriteBackend` (writes persist locally and nudge the
  daemon; the hot path **never** posts to the cloud) and `CloudflareBackend`
  (cloud-primary reads with local fallback). `prune_stream` refuses to trim below
  the un-synced high-water mark, so events are never deleted before upload.
  `make_backend` gained `timeout_seconds` / `max_retries` / `flush_seconds` /
  `batch_size`; `dual`/`cloudflare` without an API URL + token now raise
  `ValueError`.
- `recorder.py` — the factory reads the new tuning env vars and **degrades to a
  local backend with a one-time warning** if `dual`/`cloudflare` is selected
  without an API URL/token (a misconfigured ledger must never break a run).
- Six `CRUCIBLE_RUN_INSIGHTS_API_*` env keys (`_URL`, `_TOKEN`,
  `_TIMEOUT_SECONDS`, `_MAX_RETRIES`, `_BATCH_FLUSH_SECONDS`, `_BATCH_SIZE`) with
  full 3-layer Settings sync (uncommented in `.env.example`; `run_insights` group
  in `SETTINGS_SCHEMA`; bilingual `KEY_META`, with `_TOKEN` masked as a
  password). `CRUCIBLE_RUN_INSIGHTS_BACKEND` options are now
  `local`/`dual`/`cloudflare`.

### Added — clean-exit final flush (Phase 1 follow-up)
- The background sync daemon is a `daemon` thread, so the **last** batch of a run
  previously waited for the *next* run's cursor-resume to upload.
  `CloudSyncWorker.flush_and_stop` now performs a **bounded, single-attempt final
  flush before** signalling stop — it must precede `_stop`, or `_flush_stream` /
  `_post_with_retry` early-out on `if self._stop` and nothing flushes. The drain
  holds the flush lock (no race with the daemon), uses `max_retries=0`, and is
  wall-clock budgeted, so a clean exit stays responsive even when the cloud is
  unreachable (anything un-synced stays durable locally and resumes next run).
- `recorder.py` registers an `atexit` hook that closes the process-global
  recorder on clean shutdown → `DualWriteBackend.close` → the final flush. It is
  guarded against `None` / disabled / forked-child / no-op recorders and never
  raises during shutdown.

### Security — Worker request hardening
- **Bounded body / gzip-bomb guard.** `index.js` reads the (optionally gzip'd)
  request body through a streaming reader that aborts once the *decoded* size
  exceeds `MAX_BATCH_BYTES` (default 8 MiB), returning HTTP 413 — a small gzip
  bomb can no longer inflate to gigabytes and exhaust Worker memory. Applies to
  the single-event and batch endpoints (`readBodyText` / `BodyTooLargeError`).
  Defense-in-depth on the authenticated endpoints that also caps the blast radius
  if the bearer token leaks.
- Audited the full Cloudflare-facing surface: D1 queries fully parameterised (no
  SQL injection); auth fail-closed + constant-time; `content_id` recomputed
  server-side (tamper → 422); no CORS + Bearer-only (no CSRF); the Python client
  refuses 3xx redirects and never logs the token (no token-exfil). No
  unauthenticated write/DoS path beyond static liveness.

### Tests
- `tests/test_v1_2_0_cloud_backend.py` — **38** pins: HTTP client
  (gzip/auth/`http(s)`-only/no-redirect/non-2xx → `None`), worker
  flush/cursor/partial-failure/resume, the **never-block** write path and the
  **prune-respects-unsynced** data-safety invariant, the clean-exit final flush
  (drains + advances the cursor, single attempt on failure, swallows client
  errors, no re-send on a second call, `close()` drains and is idempotent),
  `make_backend` + recorder-factory degrade wiring, the 3-layer Settings sync,
  and structural producer→consumer pins (CLAUDE.md §9.6).
- Worker (`cloudflare/insights-worker/`, `node --test`) — **34** checks:
  canonical/`content_id` parity (incl. cross-language anchors),
  `test/ingest.test.js` (R2-optional routing: D1-only inline, oversized reject,
  R2 spill when bound, tamper reject), and `test/body_limit.test.js` (bounded
  reader: under/over cap, gzip-bomb rejected by decoded size).

### Validation
- `python -m pytest tests/test_v1_2_0_cloud_backend.py -q` → **38 passed**;
  `tests/test_run_insights/` + cloud + `tests/test_v1_1_11_regressions.py` →
  **270 passed**. Full suite (`python -m pytest tests -q -p no:cacheprovider`) →
  **3 349 passed, 2 skipped** in 272.8 s — the 2 skips are the documented optional
  ones (`SyntheticGoldenRun` missing `run_meta.json`; `h2` not installed).
- Worker: `npm test` → **34 checks** green. Deployed to a live account
  (**D1-only**, no R2); `npm run smoke` → **12/12** green against the deployed
  URL; a live ~9 MiB gzip-bomb returned **413** (DoS guard verified end-to-end);
  D1 rows confirmed, then test data purged so the ledger starts clean.

### Compatibility
- Drop-in for v1.1.13. With the default `local` backend the entire cloud path is
  dead code — no new runtime dependency is imported, the hot write path is
  unchanged, and the daemon's normal retry behaviour is untouched (the new
  `max_retries` parameter defaults to the configured value). Opt in by setting
  `CRUCIBLE_RUN_INSIGHTS_BACKEND=dual` + `…_API_URL` + `…_API_TOKEN`.

---

## [v1.1.13] — 2026-06-03

Optional Tavily web-search provider, added as a clean in-house reimplementation
instead of merging two automated third-party bot PRs (#5, #6). Tavily is an
opt-in `general`-class fallback that sits between DuckDuckGo and SearXNG. Default
is OFF — it activates only when `TAVILY_API_KEY` holds a real value **and**
`tavily` is added to `LIBRARIAN_EXTRA_PROVIDERS`; otherwise behaviour is
unchanged bit-for-bit. All changes are additive — no env default flipped, no
public schema or CLI flag changed.

### Added
- **Tavily Search provider** (`crucible/web_research/providers/tavily.py`) —
  follows the package's unified `search_<provider>` contract (up to `limit`
  `ResearchCitation`s, `[]` on any error, never raises for routine failures).
  Unlike the rejected bot PRs it adds **zero new dependencies**: the request is a
  `POST` through the existing SSRF-checked `safe_http_json` helper (per-host
  circuit breaker + manual-redirect validation), and
  `LIBRARIAN_HTTP_TIMEOUT_SECONDS` is forwarded so a hung call cannot blow the
  stage budget. The key is resolved with the same placeholder hygiene as
  `_resolve_context7_token` (`replace_*`/`your_*`/`xxxx*`/`placeholder*`/
  `changeme*` → treated as unset, so a copied `.env.example` never sends a
  sentinel to the live API).
- **`TAVILY_API_KEY`** env key with full Settings sync — uncommented (filterable)
  placeholder in `.env.example`, `librarian_auth` group membership in
  `SETTINGS_SCHEMA`, and a bilingual `type:'password'` `KEY_META` entry, so the
  value is masked by the backend `_mask_secret_env` (the key matches the
  `api.?key` secret pattern) and rendered as a password field. Registered in
  `providers.PROVIDERS`, `fallback._EXTRA_PROVIDERS`, and the section_04
  dispatcher (both tri-modal import blocks).
- **`tests/test_v1_1_13_provider_tavily.py`** — 36 pins: behaviour contract,
  key-placeholder filtering, result parsing / URL-scheme filtering, POST request
  shape + forwarded timeout + bounded body + stable circuit-breaker name,
  no-SDK / no-new-dependency structural guards, registry + fallback-chain +
  opt-in-filtering wiring, section_04 dispatch/import pins, and the 3-layer
  Settings sync.

### Changed
- **`general` fallback-chain ordering** is now `websearch → tavily → searxng →
  wikipedia`. `tavily` is filtered out of the resolved chain unless explicitly
  enabled, so with default extras the effective chain is unchanged. The existing
  exact-match `test_v118_fallback_chain` general-chain assertion still holds.

### Validation
- `python -m pytest tests/ -q` → **3 311 passed, 2 skipped** (+36 from the new
  file; prior v1.1.12 baseline 3 275 / 2). `smoke_test.py` 5/5; pipeline
  `--self-check` OK.
- `crucible.__version__` / `pyproject.toml` lock-step bumped to 1.1.13.

### Compatibility
- Drop-in for v1.1.12. No new runtime dependency. With Tavily not enabled (the
  default), the librarian dispatch, fallback chain, and citation pool are
  byte-for-byte identical to v1.1.12. Removing `tavily` from
  `LIBRARIAN_EXTRA_PROVIDERS` fully disables it at any time.

---

## [v1.1.12] — 2026-06-01

Cost-accuracy release: the headline `total_cost_usd` (the `--cost-report` console
output and `run_meta.json` → WebUI dashboard) no longer diverges from the
OpenRouter billing dashboard.  A four-agent read-only audit traced the gap to the
per-stage attribution path, not the capture itself.  All changes are additive —
no env-var default flipped, no public schema or CLI flag changed, and runs that
never bill OpenRouter (Alibaba coding-plan, fully-estimated) are byte-for-byte
unchanged.

### Added
- **Authoritative OpenRouter billed-cost ledger** (`section_00`) — the HTTP
  interceptor now feeds a lock-guarded, append-only module-global ledger with one
  row per billed response carrying the exact `usage.cost` OpenRouter returned, so
  `get_openrouter_billed_total()` equals the precise Σ(usage.cost) for the run.
  It is a plain global (NOT a ContextVar, so cross-thread writes are visible),
  reset only at run start (`reset_openrouter_billed_ledger()`), never by the
  per-stage `clear_openrouter_usage()`.
- **`tests/test_v1_1_12_cost_reconciliation.py`** — 16 pins: exact-sum, survives a
  per-stage clear, rejects 0/NaN/±inf/non-OpenRouter rows, per-response
  idempotency, thread-safety, reconciliation + breakdown scaling, and
  orphan-kickoff recovery.

### Fixed
- **Headline `total_cost_usd` under-reported and blended estimates behind an
  "actual billing" label** — it was rebuilt from the lossy `_record_cost`
  accumulate→read→clear dance.  Several `crew.kickoff()` sites (section_01 reformat
  crews, section_02 direction-seed plan, section_04 problem-breakdown /
  smart-queries, section_06 api-version, the external Critic) have no matching
  `_record_cost`, so their real cost was mis-attributed to an adjacent stage — or
  dropped when a `clear` ran first — and the summed total mixed actual with
  locally-estimated rows.  `section_07._reconcile_cost_summary_with_billing` now
  promotes the billed-ledger sum to the headline whenever real billing was
  captured (scaling the input/output/cache breakdown to match) and surfaces the
  old per-stage figure as `total_cost_usd_attributed` for reconciliation.
- **Latent interceptor double-count** —
  `_capture_openrouter_usage_from_http_response` is now idempotent per HTTP
  response (sentinel guard), so the interceptor + langchain-callback pair can
  never bill one response twice.

### Validation
- pytest: **3 275 passed, 2 skipped** under `-p no:cacheprovider` (+16 in
  `tests/test_v1_1_12_cost_reconciliation.py`; the 126 existing cost tests
  unchanged).  Two skips unchanged (`SyntheticGoldenRun` optional `run_meta.json`,
  HTTP/2 gated on the `h2` package).
- `crucible/smoke_test.py`: 5/5 OK; `run_crucible.py --self-check`: OK.
- `pyproject.toml` and `crucible.__version__` bumped to `"1.1.12"` in lock-step so
  `test_pyproject_version_matches_package_version` stays green.

### Compatibility
- Drop-in for v1.1.11 — `pip install -U` is safe.  No env-var default flipped, no
  public API / CLI flag / ledger schema changed.  `AgentCostAccountant` and the
  per-stage breakdown are untouched (still emitted for diagnostics); runs with no
  captured OpenRouter billing keep the prior total exactly.  All v1.1.8–v1.1.11
  invariants preserved (4-stream ledger, canonical JSON via `_V8FloatJSONEncoder`,
  cross-process sidecar locks, two-sided permutation default, `_atomic_io`
  helpers, degrade-not-die, `_try_build` lenient retry).

---

## [v1.1.11] — 2026-05-28

Audit-fix release: a ten-agent read-only audit of the frontend and backend
surfaced ~50 findings (4 high, ~18 medium, the rest low) across WebUI
behaviour, accessibility, backend security, the core pipeline, the run-insights
ledger, and web-research / durability infrastructure.  All changes are
additive — no env-var default flipped, no public schema or CLI flag changed,
and runtime behaviour matches v1.1.10 unless an operator opts into a new surface.

### Added
- **Responsive + accessible WebUI** (`app.css` / `index.html` / `app.js`) —
  900 px / 600 px breakpoints collapse the multi-column grids and turn the
  hover-only sidebar rail into a tap-to-open off-canvas drawer (`☰` + scrim);
  modal focus-trap/restore, keyboard-reachable tooltips, an `aria-live` toast
  region, `<label for>` on 12 inputs, `role="log"` terminals, and a global
  "run in progress" pill.  Destructive actions (clearing a populated terminal,
  stopping a run, closing an active-run tab) now require confirmation.
- **Atomic-write durability** — eight writers (`checkpoint`, `agent_metrics`,
  `citation_verifier`, `api_version_autopatch`, `auth_manager`, `celery_worker`,
  `alt_data_connectors`, and `section_00`'s debug dump) route through
  `crucible._atomic_io.atomic_write_text` (parent-dir fsync); the helper gained
  a `newline=` passthrough so generated sources stay LF-only on Windows.
- **Settings exposure** — `BACKTEST_PARAM_SEED` /
  `BACKTEST_FETCH_HARD_TIMEOUT_SEC` added to the `backtest` group with bilingual
  `KEY_META` (they previously fell into the unlabelled "Other" group); seven
  `LIBRARIAN_*` keys plus `BACKTEST_PARAM_SEARCH` / `BACKTEST_BAYESIAN_N_TRIALS`
  uncommented in `.env.example`.
- **Transaction-cost annotation** — `transaction_cost_model.py` flags synthesised
  trade signals in `result.warnings` (per CLAUDE.md §8).
- **`tests/test_v1_1_11_regressions.py`** — 66 behavioural + structural pins.

### Changed
- **`grep_app` dropped from the default `code` fallback chain**
  (`web_research/fallback.py`) — still reachable via explicit
  `LIBRARIAN_SEARCH_PROVIDERS` (`_CORE_PROVIDERS` keeps it).
- **SearXNG default instance list emptied** (`providers/searxng.py`) — the
  provider no-ops without an operator-pinned `https://` instance instead of
  querying hard-coded public hosts (the shipped `domain_pins.json` still supplies
  instances, so the default deployment is unchanged).
- **WebUI `run_id`** widened from 8 to 12 hex chars; status labels unified
  through one `_STATUS_LABELS` map; `_ab_tests` records evicted on the same TTL
  as `_runs` (was an unbounded leak); `_evict_stale_runs` is streamer-aware.
- **`DualWriteBackend` stub** given its real keyword-only signature (still
  `NotImplementedError` until v1.2.0); **`SpecialistFinding.confidence`** gained
  an explicit non-finite guard.
- **README test badge refreshed** — `README.md` / `README_zh.md` bumped from the
  stale `3104+` / `2 588+` to `3 255+`; the `_FULL` READMEs already defer to this
  file for the count.

### Fixed
- **`GET /api/env` leaked secrets** (`webui/app.py`) — the endpoint returned the
  whole `.env` (every `sk-*` token, `WEBHOOK_SECRET`) in plaintext to any
  same-origin caller; secret-named keys now return a `********` sentinel and the
  POST handler treats an unchanged sentinel as "keep stored value", so the
  Settings save round-trip is intact (`_load_env()` is unchanged internally).
- **`POST /api/env` accepted arbitrary keys** (`webui/app.py`) — keys must now
  match `^[A-Z][A-Z0-9_]*$` and are rejected against a denylist
  (`PATH`/`PYTHONPATH`/`LD_PRELOAD`/`DYLD_*`/…), closing a process-hijack vector
  via the inherited `_child_env`.
- **`sk-` redaction gap** (`run_insights/redact.py`) — the DeepSeek `sk-`+hex
  pattern was widened `{32}` → `{32,}`; a 33–39-char pure-hex key matched neither
  it nor the generic `{40,80}` pattern and reached ledger error rows.
- **`http_retry` SSRF** — the dormant `safe_get`/`safe_post` helpers reject
  non-public targets via a lazily-imported `_is_public_http_url` and set
  `follow_redirects=False` (blocks a 30x → `169.254.169.254` metadata hop).
- **Client error strings leaked paths** (`webui/app.py`) — `[WEBUI ERROR]`, the
  stdin-write warning, and `api_env_validate` errors route through
  `_redact_for_client`.
- **Cancel left a process tree running** (`webui/app.py`) — the pipeline runs in
  its own group/session and `DELETE /api/run/<id>` escalates SIGTERM→SIGKILL
  (Windows `taskkill /F /T`), so LLM/git grandchildren stop spending credits.
- **`_NoOpBackend` protocol parity** (`run_insights/recorder.py`) — `read_events`
  now returns `([], None)` (callers unpack a 2-tuple), `read_blob` exists, and
  `write_blob`'s signature matches `StorageBackend`; the unknown-decision warning
  uses its own `_warned_unknown_decision` flag.
- **`GateVerdict` mutual-exclusion** (`section_03`) — the decision-shape validator
  now forbids the off-shape fields for every decision (PROCEED/KILL forbid
  `branched_paths`; NEEDS_MORE_DATA/BRANCH forbid `selected_direction`; …), and
  the critic coercer gates each field by decision so a chatty model can't burn a
  retry.
- **Degraded-proceed ledger score** (`section_02`) — `final_score` now reflects
  the pre-clamp confidence band instead of a hard-coded `0`.
- **Cooldown-skip prompt pollution** (`section_04`) — a benign provider cooldown
  is caught (`except _CooldownSkipError: continue`) instead of being logged as a
  research failure and fed to the direction LLM.
- **WebUI JS** (`app.js`) — the Run button no longer double-submits (gated by
  `_modeHasLiveSession` through the `_setSessionStatus` chokepoint); Dashboard /
  Settings load failures show an error state instead of a stuck "Loading…";
  `runCompare()` drops stale pairs via `_compareToken`; `_submitHitl` validates
  `runId` before clearing; `domain-badge` is null-guarded; `_try_build`
  (`section_01`) logs a real lenient-retry failure.

### Validation
- pytest: **3 255 passed, 2 skipped** under `-p no:cacheprovider` (was 3 189 + 2
  at v1.1.10; +66 in `tests/test_v1_1_11_regressions.py`).  Two skips unchanged:
  `SyntheticGoldenRun` missing optional `run_meta.json`, HTTP/2 test gated on the
  `h2` package.
- `crucible/smoke_test.py`: 5/5 OK; `run_crucible.py --self-check`: OK.
- `pyproject.toml` and `crucible.__version__` bumped to `"1.1.11"` in lock-step so
  `test_pyproject_version_matches_package_version` stays green.

### Compatibility
- Drop-in for v1.1.10 — `pip install -U` is safe.  No env-var default flipped, no
  public API / CLI flag / ledger schema changed.
- Two client-visible deltas, neither needing an opt-out: GET `/api/env` masks
  secret values (`********`; the Settings save round-trip is unaffected) and POST
  `/api/env` rejects non-`UPPER_SNAKE` / process-hijack keys (all legitimate
  crucible keys are uppercase-snake and unaffected).
- All v1.1.8 / v1.1.9 invariants preserved (4-stream ledger, canonical JSON via
  `_V8FloatJSONEncoder`, cross-process sidecar locks, two-sided permutation
  default, `_atomic_io` helpers, `init_run_correlation_from_env`,
  `_ENHANCED_FLAG_TO_ENV` wiring, degrade-not-die, `_try_build` lenient retry,
  and the H2 dispatcher cooldown/health wire-in).

---

## [v1.1.10] — 2026-05-28

Librarian-provider hardening release: five findings (S1–S5) from a
live-log triage of the post-v1.1.9 librarian dispatcher.  All changes
are additive; no env-var default flipped, no public schema broken, no
CLI flag removed.  Defaults match v1.1.9 bit-for-bit unless the
operator opts into the new behaviour (only configuring real values for
the two new `CONTEXT7_API_KEY` / `GITHUB_TOKEN` placeholders changes
runtime behaviour, and both fall back to the v1.1.9 anonymous tier
when left at their `.env.example` placeholder values).

### Added
- **`_resolve_context7_token()` + `_context7_api_headers()`** in
  `section_04_web_research_and_direction.py` (S1) — mirrors the
  existing `_resolve_github_token()` + `_github_api_headers()` pair so
  `_search_context7` can inject `Authorization: Bearer <token>` when
  the operator configures one.  context7.com enforces a per-IP monthly
  anonymous quota; once exhausted every search call returns HTTP 429
  with a `Retry-After` of multiple days, which previously caused the
  librarian to silently lose context7 for the remainder of every
  session on that IP.  Configuring an API key via
  https://context7.com/dashboard lifts the quota to the dashboard
  tier.  Token check order: `CONTEXT7_API_KEY` → `CONTEXT7_TOKEN`;
  placeholder sentinels (`your_*`, `xxxx*`, `placeholder*`,
  `changeme*`, `replace_*`) are filtered out so a fresh `.env.example`
  copy does not pretend to have a key.
- **`.env.example` ships `CONTEXT7_API_KEY` and `GITHUB_TOKEN` as
  uncommented placeholder entries** (S4) — both keys are now visible
  to the `/api/env` reader on a fresh install, so the Settings UI
  surfaces input boxes for them out-of-the-box.  Placeholder values
  (`replace_with_context7_api_key`, `replace_with_github_personal_access_token`)
  are filtered out by the backend resolvers, so operators who leave
  them untouched get the v1.1.9 anonymous behaviour bit-for-bit.
  `GITHUB_TOKEN` was previously documented in README only and had to
  be added manually to `.env`; now it is discoverable through the UI.
- **WebUI Settings `librarian_auth` group** (S5) — new `SETTINGS_SCHEMA`
  entry just after `librarian_providers`, containing both
  `CONTEXT7_API_KEY` and `GITHUB_TOKEN`.  Two new `KEY_META` entries
  with bilingual `desc:{en, zh}` per the v1.1.0 KEY_META bilingual
  contract; both are `type:'password'` so the masked input behaviour
  matches the existing `OPENROUTER_API_KEY` / `ALPHA_VANTAGE_API_KEY`
  entries.
- **`tests/test_v1_1_10_regressions.py`** — 36 new tests organised by
  finding (S1 x 11, S2 x 7, S3 x 8, S4 x 4, S5 x 4, plus 2
  cross-cutting), each with both behavioural assertions and structural
  `inspect.getsource` / regex pins per CLAUDE.md § 9.6.

### Changed
- **`OPENCODE_LIBRARIAN_DEFAULT_PROVIDERS` no longer includes
  `grep_app`** (S2) — grep.app is fronted by Vercel Bot Protection
  which now serves a JS proof-of-work challenge to every
  unauthenticated client (status 429 with `X-Vercel-Mitigated:
  challenge`); no pure-HTTP client can solve it, so every request
  burns the 3-attempt retry budget and triggers a 60s cooldown for
  the whole session.  grep.app also offers no API-key tier, so the
  block cannot be lifted by configuring credentials.  `github` is the
  natural replacement for the "code" query class — its `search/code`
  endpoint requires authentication anyway and its 30 req/min
  authenticated quota is comfortably above what the librarian needs.
  `_search_grep_app` itself is preserved, the alias map still
  recognises `grep_app`, and the `code` fallback chain still lists it
  as position 2 — so operators who pin `grep_app` explicitly in
  `LIBRARIAN_SEARCH_PROVIDERS` keep their behaviour, they just stop
  paying the cooldown tax on a fresh install.  `fallback.py`'s own
  literal default string is updated in lockstep, pinned by
  `tests/test_v1_1_10_regressions.py::TestS2GrepAppRemovedFromDefaults::
  test_fallback_default_matches_canonical_default`.
- **`crucible/config/domain_pins.json` — six pin URLs replaced** (S3)
  after agent-driven live-fetch testing confirmed they returned
  non-200 for every unauthenticated HTTP client (CrucibleCrew UA AND
  a full Chrome UA), exhausting the 3-attempt safe_http_text retry
  budget on every prefetch:
  - `www.binance.com/en/support/faq/introduction-to-binance-futures-funding-rates-...`
    (AWS-WAF 202 + JS challenge) →
    `developers.binance.com/docs/derivatives/usds-margined-futures/general-info`
    (official developer portal, 200).
  - `www.coingecko.com/en/api/documentation` (Cloudflare 403) →
    `www.coingecko.com/learn` (free-tier docs hub, 200).
  - `www.cmegroup.com/education.html` (CDN WAF 403) → two Wikipedia
    substitutes covering the same conceptual material (Chicago
    Mercantile Exchange + Futures contract).
  - `cookbook.openai.com/` (308 permanent redirect) →
    `developers.openai.com/cookbook` (canonical destination, no
    trailing slash to avoid re-308).
  - `python.langchain.com/docs/introduction/` (3-hop 308 chain that
    exceeds the manual-redirect helper's `_MAX_REDIRECTS=3` budget) →
    `docs.langchain.com/oss/python/langchain/overview` (canonical
    destination).
  - `search.brave4u.com` (DNS NXDOMAIN — host no longer resolves) →
    `paulgo.io` (long-running public SearXNG instance).
  All replacements verified 200 against the librarian's
  `CrucibleCrew/14 librarian` UA in the v1.1.10 audit pass.

### Validation
- Full pytest suite: **3 189 passed, 2 skipped** (was 3 153 + 2 at
  v1.1.9; +36 new tests in `tests/test_v1_1_10_regressions.py`
  covering all five findings).  Two skips unchanged: `SyntheticGoldenRun`
  missing optional `run_meta.json`, and HTTP/2 SSRF test gated on the
  `h2` package.
- `pyproject.toml` and `crucible.__version__` bumped to `"1.1.10"` in
  lock-step so `test_pyproject_version_matches_package_version` stays
  green.

### Compatibility
- Drop-in for v1.1.9.  No env-var default flipped, no public schema
  broken, no CLI flag removed.  All five findings are additive:
  - S1 / S4 / S5 only matter when the operator configures a real
    `CONTEXT7_API_KEY` value; left at the placeholder, context7
    keeps the v1.1.9 anonymous behaviour.
  - S2 changes the default provider list but the v1.1.9 user can
    restore the pre-v1.1.10 default by setting
    `LIBRARIAN_SEARCH_PROVIDERS=websearch,context7,grep_app,github,arxiv,paperswithcode`
    in `.env` (the `_search_grep_app` helper and the alias map still
    accept `grep_app`).
  - S3 replaces pin URLs that were already non-functional (they all
    returned non-200 in v1.1.9 too — operators were silently losing
    those Tier-1 anchors regardless).  All replacement URLs use the
    same `tier: "Tier-1"` and provide equivalent or richer content;
    no upgrade step required.
- v1.1.8 invariants (4-stream ledger, canonical_json via
  `_V8FloatJSONEncoder`, cross-process sidecar locks, two-sided
  permutation default) all preserved.
- v1.1.9 invariants (`_atomic_io` helpers, `init_run_correlation_from_env`,
  `_ENHANCED_FLAG_TO_ENV` mapping, P5 degrade-not-die,
  `_try_build` lenient retry, H2 dispatcher cooldown/health wire-in)
  all preserved.

---

## [v1.1.9] — 2026-05-24

Audit-fix release: 8 findings (H1/H2/M1/M2/M3/M4/L1/L2) from the
post-v1.1.8 review.  All changes are additive; no env-var default
flipped, no public schema broken, no CLI flag removed.  Defaults match
v1.1.8 bit-for-bit unless the operator opts into the new behaviour
(only `CRUCIBLE_DEBATE_TOLERATE_UNVERIFIABLE_EVIDENCE`, which v1.1.8
already shipped as observation-only).

### Added
- **`crucible/_atomic_io.py`** (H1) — shared `atomic_write_text()` +
  `fsync_dir()` helpers.  `os.replace` on POSIX needs a parent-dir
  fsync to durably commit the rename; without it a power loss after
  the replace can leave the new bytes on disk while the directory
  entry still points at the old inode.  `section_07._atomic_write_text`
  (the writer behind every final-stage artefact — `final_output.json`,
  `quality_report.json`, `gate_decision.json`, README, etc.) and
  `quant_analytics.py` (walk-forward + analytics report writers) now
  route through it.  Windows is a no-op (NTFS commits metadata on the
  file handle).
- **`init_run_correlation_from_env()`** in `crucible/run_correlation.py`
  (L1) — single shared bootstrap helper that all three CLI entry points
  (`crucible/__main__.py`, `run_crucible.py`, `run_crucible_enhanced.py`)
  now call instead of carrying the same inline
  `_set_run_id((os.environ.get("CRUCIBLE_RUN_ID") or "").strip() or None)`
  pattern.  Behaviour identical; v1.1.2 sixth-pass H-3 whitespace-strip
  contract preserved by `set_run_id`'s own `.strip()` defence.
- **`_ENHANCED_FLAG_TO_ENV`** in `webui/app.py` (L2) — 35 per-run flag
  toggles (`security_scan`, `html_report`, `quant_analytics`,
  `backtest_runner`, `gate_control`, `selective_rerun`,
  `api_version_check`, all 12 `ENHANCED_*` post-processing flags, the
  10 Quant Analytics Suite flags, etc.) now actually reach the
  subprocess.  Pre-v1.1.9 these were **visual-only** — `ENV_BACKED_FLAGS`
  on the frontend synced the initial checkbox state from `.env`, but
  the backend resolver only translated the 11 run-insights /
  store-true flags into subprocess env overrides, so unchecking any of
  the other 35 boxes changed the panel but not the run.  Now wired in
  lockstep, pinned by `tests/test_v1_1_9_regressions.py::
  TestL2EnhancedFlagWiring` (4-layer producer→consumer wiring per
  CLAUDE.md § 9.6).

### Changed
- **`run_direction_debate()` degrade-not-die now active** (M3 / P5) — when
  `CRUCIBLE_DEBATE_TOLERATE_UNVERIFIABLE_EVIDENCE=1` and the force-none
  refinement loop exhausts, the outer caller now returns the pre-clamp
  candidate decision (with `confidence` capped to "low") instead of
  None.  v1.1.8 emitted the `direction_debate_degraded_proceed` ledger
  event but kept returning None; v1.1.9 honours the toggle for real.
  The `original_decision` field of the emitted event distinguishes
  active rows (`"degraded_proceed"`) from v1.1.8 observation rows
  (`"force_none"`).  Default (toggle off) returns None unchanged.
  Additive: `_run_single_direction_debate` now stashes the pre-clamp
  decision into `gap_info["preclamp_decision"]`; the return-tuple shape
  is unchanged so legacy callers ignore the new key.
- **`section_01._try_build` lenient pydantic retry** (M4 / P1) — when
  strict `model_cls(**d)` fails because the LLM emitted extra keys the
  schema doesn't know about, filter to declared `model_fields` and try
  once more.  Strict validation still wins on clean payloads; the
  lenient pass only fires after the strict attempt fails.  Closes the
  v1.1.8 known limitation where one stray chatty-model key would burn
  the entire retry budget.
- **`section_04` dispatcher wire-in for cooldown + health** (H2) —
  the Q2 (`crucible/web_research/cooldown.py`) and Q7
  (`crucible/web_research/health.py`) modules shipped in v1.1.8 but
  never reached the dispatcher.  `_safe_http_json` / `_safe_http_text`
  now accept an optional `provider_name=` kwarg; when given they
  check `CooldownRegistry.is_cooling_down(provider)` before the call
  (raising `_CooldownSkipError` if cooling), record the request on the
  health tracker, classify any failure into 429 / 202 / timeout /
  other and trigger the cooldown + tracker events appropriately.  All
  seven existing dispatcher call sites (`_search_websearch` x2,
  `_search_context7`, `_search_github_*` x2, `_search_arxiv`,
  `_search_grep_app`) pass `provider_name=`.  Cache hits record to
  `tracker.record_cache_hit` so end-of-stage summary doesn't undercount
  provider activity.  At end of `_collect_librarian_search_materials`
  the dispatcher prints the per-provider summary lines and emits a
  `record_provider_health_summary` ledger event when
  `LIBRARIAN_PROVIDER_HEALTH_SUMMARY` is on (default).  Q3 fallback
  chain + Q5 async fan-out wire-in remain deferred — they need a loop
  restructure that's out of scope for this audit-fix release.
- **`auto_remediator._call_llm` exception logging** (M1) — the
  previously silent `except Exception: pass` now logs at DEBUG via a
  module-level `LOGGER`.  Operators staring at "no patch generated"
  can now flip `CRUCIBLE_LOG_LEVEL=DEBUG` and see whether the LLM
  call hit a timeout, expired credential, or rate limit.
- **`requirements.txt` optional-dep version floors** (M2) — every
  actively-imported optional dependency now carries a minimum version
  (`python-dotenv>=1.0`, `pyyaml>=6.0`, `appdirs>=1.4`, `yfinance>=0.2.0`,
  `ccxt>=4.0`, `optuna>=3.0`, `fpdf2>=2.7`, `pypdf>=3.0`,
  `python-docx>=0.8.11`, `chromadb>=0.4`, `scikit-learn>=1.0`,
  `watchdog>=3.0`, `mlflow>=2.0`, `scipy>=1.7`, `statsmodels>=0.14`,
  `pandas-datareader>=0.10`, `quantstats>=0.0.59`,
  `opentelemetry-{api,sdk,exporter-otlp}>=1.20`, `APScheduler>=3.10`,
  `redis>=4.0`, `prometheus_client>=0.15`, `PyJWT>=2.0`, `bcrypt>=4.0`,
  `celery>=5.0`, `python-telegram-bot>=20.0`, `discord.py>=2.0`).
  Same rationale as the v1.1.2 sixth-pass M-11 floors on the core
  deps: `pip-audit` results and `actions/setup-python` cache keys
  stay reproducible from one CI run to the next.

### Fixed
- **Five structural tests realigned to the v1.1.9 patterns** —
  `test_v1_1_2_sixth_pass.py::TestH3RunIdStripProducers` and
  `test_v1_1_2_audit_fixes.py::TestGroup1RunIdRedux::
  test_flat_launcher_calls_set_run_id_at_module_top` updated to accept
  either the pre-v1.1.9 inline `.strip()` pattern or the new
  `init_run_correlation_from_env()` helper (whichever the entry point
  uses).  The H-3 strip contract is still re-tested at the function
  level by `TestL1RunCorrelationHelper`.
  `test_module_cancellation.py::_has_cancel_guard_before_except_exc`
  loosened to allow intermediate `except X: raise` clauses between the
  cancellation guard and the broad `except Exception` — needed for
  `_search_websearch` after H2 added `except _CooldownSkipError: raise`,
  which is structurally equivalent (re-raises, doesn't swallow
  cancellation).

### Validation
- Full pytest suite: **3 153 passed, 2 skipped** (was 3 104 + 2 at
  v1.1.8; +49 new tests in `tests/test_v1_1_9_regressions.py` covering
  all eight findings).  Two skips unchanged: `SyntheticGoldenRun`
  missing optional `run_meta.json`, and HTTP/2 SSRF test gated on the
  `h2` package.
- `crucible/smoke_test.py`: 5/5 OK.
- `python run_crucible.py --self-check`: OK.
- `pyproject.toml` and `crucible.__version__` bumped to `"1.1.9"` in
  lock-step so `test_pyproject_version_matches_package_version`
  stays green.

### Compatibility
- Drop-in for v1.1.8.  No env-var default flipped, no public schema
  broken, no CLI flag removed.  All eight findings are additive — the
  default behaviour matches v1.1.8 exactly unless the operator opts
  into the new path (only `CRUCIBLE_DEBATE_TOLERATE_UNVERIFIABLE_EVIDENCE=1`
  changes runtime behaviour, and that toggle was already opt-in in
  v1.1.8).
- L2 mapping additions are bidirectional but only emit env overrides
  when the per-run flag is **explicitly** True or False.  Missing /
  `None` values still leave the parent process's `.env` defaults
  untouched, matching the pre-v1.1.9 "untouched panel" semantics.
- H1 `atomic_write_text(fsync_parent=True)` is the default; pass
  `fsync_parent=False` to suppress the parent fsync on callers that
  intentionally want the pre-v1.1.9 no-fsync behaviour.

---

## [v1.1.8] — 2026-05-20

Two-theme release: Direction Debate Audit Mode (the per-specialist
disagreement log Mira Chen's v1.1.7 feedback identified as the gate's
most-valuable output) plus Web Research Hardening + Direction Gate
Tuning (resolves the "3 iterations all force-none" diagnostic and the
DuckDuckGo / GitHub anonymous-search rate-limit problems).  Default is
OFF for every new audit feature — pre-v1.1.7 sequential debate flow is
preserved bit-for-bit when audit mode is disabled.

### Added
- **Direction Debate Audit Mode** — 8 new env keys
  (`CRUCIBLE_DEBATE_AUDIT_MODE`, `_REQUIRE_STRUCTURED_FINDINGS`,
  `_ISOLATION_MODE`, `_EXTERNAL_CRITIC`, `_CRITIC_OVERRIDE_PROCEED`,
  `_CONSENSUS_RISK_THRESHOLD`, `_CRITIC_MAX_ATTEMPTS`, plus 2 ledger
  toggles), 5 CLI flags (`--audit-mode`, `--debate-isolation`,
  `--external-critic`/`--no-external-critic`,
  `--critic-can-override`/`--no-critic-override`), and 8 new pydantic
  models in `section_03_models_and_context.py` (`SpecialistFinding`,
  `GateVerdict`, `ConsensusRiskReport`, `Disagreement`, `Concern`,
  `EvidenceRef`, `BranchSpec`, `AuditTrail`).  `GateVerdict` enforces
  per-decision required-field invariants so the model cannot silently
  downgrade a hard `KILL` into a vague `NEEDS_MORE_DATA`.
- **`crucible/features/direction_debate/` package** — `consensus.py`
  computes deterministic, embedding-free Jaccard-based concern diversity
  + assumption overlap + confidence variance + weighted
  `groupthink_score` ∈ [0, 1].  `critic.py` adds the Stage 0 sixth
  agent External Critic that re-judges the Judge's verdict using ONLY
  raw evidence + the Judge's decision token (no CoT exposure → no
  anchoring bias).  Critic parse failures fall back to
  `NEEDS_MORE_DATA`, never `KILL`.
- **Two audit-mode ledger event kinds** (`DIRECTION_DEBATE_FINDING`,
  `DIRECTION_DEBATE_VERDICT`) and matching recorder methods.  Both share
  the existing `debate.jsonl` stream so `_STREAM_FILENAMES` /
  `_VALID_STREAMS` invariants are unchanged.
- **Web Research Hardening** (`crucible/web_research/`):
  - Q1: disk-persistent search cache (SQLite, per-provider TTL 12-168 h)
    at `saved_projects/.cache/search_cache.sqlite3`; two-tier L1 memory
    + L2 disk lookup wired into `_qcache_get` / `_qcache_set` in
    section_04 so all providers benefit.  Cuts repeat-run HTTP cost 80 %+.
  - Q2: per-provider adaptive cooldown that doubles on consecutive 429
    (rate limit) or 202 (DDG bot-detection) responses, capped at 30 min.
  - Q3: per-query-class fallback chain (general / code / academic /
    docs) with `classify_query()` heuristic on
    `site:` / `filetype:` / arxiv / github keywords.
  - Q4: four new zero-auth providers — OpenAlex, Crossref, Wikipedia,
    SearXNG.  Default `LIBRARIAN_EXTRA_PROVIDERS=openalex,crossref,wikipedia`;
    SearXNG opt-in (public instance reliability varies).
  - Q5: parallel multi-provider fan-out via `ThreadPoolExecutor` with
    synchronous public API; env-gated `LIBRARIAN_ASYNC_FANOUT_ENABLED=1`
    (default ON) with sequential fallback.
  - Q6: cross-provider query deduplication via
    `(normalised_query, query_class)` coverage map; ~30 % HTTP-call
    savings on typical runs.
  - Q7: per-provider health observability (counts for requests / 200 /
    429 / 202 / timeouts / errors / citations / cache hits) emitted as
    `PROVIDER_HEALTH_SUMMARY` ledger event at end-of-stage.
  - Q8: domain authoritative-source pinning via
    `crucible/config/domain_pins.json` (JSON, not YAML — repo doesn't
    depend on PyYAML).  Initial pin set covers 10 domains across crypto
    perpetuals, ethereum on-chain, tradfi metrics, tradfi market
    structure, scientific methods, SaaS payments, SaaS auth, SaaS cloud
    arch, and agent LLM cookbooks.  All URLs https-only (SSRF safety,
    structurally pinned by tests).
  - Q9: HTTP/2 + keep-alive in `http_clients._http_client()` —
    HTTP/2 when `h2` package is installed, graceful degrade to HTTP/1.1
    otherwise.  SSRF invariant (`follow_redirects=False` + manual
    redirect walker) preserved under HTTP/2.
  - Q10: bilingual query expansion for CJK queries.  Unicode-range
    detection (no extra deps), caller-supplied translation function
    produces English mirror when native results fall below
    `LIBRARIAN_BILINGUAL_QUERY_THRESHOLD=3`.
- **Three additional EventKind values + recorder methods**:
  `PROVIDER_COOLDOWN_ENGAGED` (→ `error` stream),
  `PROVIDER_HEALTH_SUMMARY` (→ `output` stream),
  `DIRECTION_DEBATE_DEGRADED_PROCEED` (→ `debate` stream).  Still 4
  stream files total (`_VALID_STREAMS` invariant unchanged).
- **23 new env keys** in `.env.example` (Librarian Search Cache,
  Provider Resilience, Extra Providers, Query Quality, Direction Gate
  Tuning groups) plus the 8 audit-mode keys.  All bilingual-described
  in WebUI `KEY_META`; 6 new Settings groups; 3 per-run flags
  (`debate_audit_mode`, `debate_external_critic`,
  `debate_tolerate_unverifiable_evidence`) in the Idea/Path mode flag
  panel.  Frontend `ENV_BACKED_FLAGS` and backend
  `_RUN_INSIGHTS_FLAG_TO_ENV` stay in lockstep per CLAUDE.md § 1.
- **`ClaimAttribution` schema migration (P2)** — two new Optional
  fields `direction_key: Literal["A".."G"]` and
  `field_name: Literal["thesis","primary_metric","fastest_test",
  "major_risk","data_sources"]`, both `default=None` for backward
  compatibility.  Existing ledger entries parse cleanly; the auditor
  falls back to semantic matching when tags are absent.

### Changed
- **`section_04:build_direction_debate_crew()`** gained keyword-only
  `audit_mode` + `isolation_mode` params.  When `audit_mode=True`, task
  descriptions append a structured
  `<<<AUDIT_FINDING_BEGIN>>>…<<<END>>>` block requirement plus a
  Judge-only `<<<GATE_VERDICT_BEGIN>>>…<<<END>>>` block.  When
  `isolation_mode="hybrid"`, agents are told to treat prior CoT as
  untrusted and rely on structured findings only — reduces sequential
  anchoring without parallel execution.
- **`section_02:_run_single_direction_debate()`** parses audit blocks,
  computes consensus risk, optionally invokes the External Critic, and
  emits `record_debate_finding` (N) + `record_gate_verdict` (1) ledger
  events per attempt.  v1.1.8 is **observation-only**: the legacy
  `force_none` decision flow is unchanged, so audit mode can run in
  production without changing which directions are selected.
- **Evidence Auditor prompt rewritten (P2)** — auditor now explicitly
  (a) reads `direction_key` / `field_name` tags when present, and
  (b) falls back to semantic matching from claim text when tags are
  absent.  Producer→consumer wiring pinned by
  `tests/test_v118_extended_phase7.py`.
- **Force-none warning UX cleanup (P4)** — the
  `[Warn] Direction debate exhausted N iteration(s)` message no longer
  prints misleading `grounded_claims_needed=0 citations_needed=0` when
  the firing gate branch didn't set them.  When both are 0 the warning
  now surfaces
  `structural_failure=supported_fields_empty_across_directions` to
  point operators at the real fix (per-direction anchoring) instead
  of "add more citations".
- **Degrade-not-die observability (P5)** — when force-none exhausts
  refinement and `CRUCIBLE_DEBATE_TOLERATE_UNVERIFIABLE_EVIDENCE=1`,
  the loop emits a `direction_debate_degraded_proceed` ledger event.
  Observation-only in v1.1.8; the actual behavioural reroute is v1.1.9
  work (non-trivial signature change to `_run_single_direction_debate`).
- **`record_direction_debate_rejection()`** extended its known
  `rejection_reason` set with three new audit-mode values:
  `judge_explicit_kill`, `judge_branch`, `needs_more_data`.
- **WebUI Settings page** surfaces all new env keys under bilingual
  `KEY_META` entries; Direction Debate Audit group placed adjacent to
  the existing Direction & Gate Control group.

### Fixed
- **`_NullRecorder` interface parity** — pre-existing v1.1.8 audit
  oversight where `record_debate_finding` / `record_gate_verdict` were
  added to `InsightsRecorder` but NOT to `_NullRecorder`.  Same gap
  reproduced for the three v1.1.8 extension ledger methods; now all
  five exist on both classes so `CRUCIBLE_RUN_INSIGHTS_ENABLED=0` no
  longer AttributeError's on the new calls.

### Validation
- Full pytest suite: **3 104 passed, 2 skipped** (was 2 588 + 1 at
  v1.1.7; +516 new tests across the v1.1.8 audit-mode tests and the
  v1.1.8 extension tests).  Skipped are: `SyntheticGoldenRun` missing
  optional `run_meta.json`, and HTTP/2 SSRF test gated on `h2` package
  not installed in this environment.
- New test files: `tests/test_direction_debate_audit/` (7 files) for
  audit-mode Pydantic invariants, ledger emit + swallow, consensus-risk
  metric correctness, External Critic prompt + JSON extraction +
  fallback, audit-mode appendix + parser wiring, producer→consumer
  structural pins, and regression hard cases.  Plus 16 root-level
  `tests/test_v118_*` files for extension coverage (search cache,
  cooldown, health, openalex / crossref / wikipedia / searxng
  providers, domain pins, fallback chain, dedup, HTTP/2 + keepalive,
  async runner, translate, Phase 7 producer→consumer wiring, 4-layer
  Settings sync, Phase 2 ledger events).
- Producer→consumer wiring tests follow CLAUDE.md § 9.6: scan
  `.env.example` keys appear in `SETTINGS_SCHEMA`, scan mapping RHS
  env names appear in actual section_02 / section_07 / recorder.py
  read sites, and `inspect.getsource()`-style checks pin the CLI → env
  translation block in `cmd_run`.
- `pyproject.toml` and `crucible.__version__` bumped to `"1.1.8"` in
  lock-step so `test_pyproject_version_matches_package_version` stays
  green.

### Compatibility
- Drop-in for v1.1.7.  When `CRUCIBLE_DEBATE_AUDIT_MODE=0` (default),
  direction-debate behaviour is identical to v1.1.7.  No env-var
  default flipped; no CLI flag removed; no public schema broken.
- All 23 web-research env vars have backward-compatible defaults.
  `ClaimAttribution` schema migration is purely additive (Optional
  fields with `default=None`).
- `_STREAM_FILENAMES` stays at `{output, error, debate, params}` — no
  new ledger file (CLAUDE.md § 11.9 invariant).
- Downstream readers should branch on `kind` (not stream name) to
  distinguish three debate event types: `direction_debate_rejection`
  (legacy, v1.1.0+), `direction_debate_finding` (v1.1.8+, audit mode),
  `direction_debate_verdict` (v1.1.8+, audit mode).
- The `BRANCH` decision is audit-only in v1.1.8: the ledger event
  preserves branch info for v1.2.0 retrieval, but the current run still
  picks `branched_paths[0].direction_id` as PROCEED.  Spawning parallel
  sub-runs is a v1.3.0 capability gated on cost-control work.
- `critic_model_family` on `AuditTrail` is reserved for v1.3.0
  cross-family Critics; in v1.1.8 always `None`.  v1.3.0 may populate
  without schema migration.

### Known limitations / deferred to follow-up
- Q5 (async fan-out) full wire-in to section_04's 5 000-line dispatcher
  loop; Q2 cooldown + Q7 health hooks at every `safe_http_*` call site;
  Q3 fallback chain replacement of `LIBRARIAN_SEARCH_PROVIDERS` order —
  modules + tests are in; the dispatcher restructure is its own PR.
- P5 actual behavioural degrade (returning low-confidence direction
  instead of `None`) — observability in v1.1.8, decision reroute in
  v1.1.9.
- P1 comparator lenient pydantic coercion + P6 enhanced refinement
  targeting from `audit_report.summary_only_fields` — deferred; current
  comparator typically succeeds and existing
  `_build_refinement_research_queries` already covers the high-impact
  cases.

---

## [v1.1.7] — 2026-05-19

### Changed
- **Short README rewritten for new-user onboarding** — `README.md`
  and `README_zh.md` restructured around a 5-question decision path
  (what is it / what for / how to start / how it works / can I trust
  the output).  Length cut from 344 → 162 lines (EN) and
  448 → 162 lines (ZH).  Adds three status badges (License / Python
  / test count), a 4-role grid (Quant / SaaS / Scientist / Agent),
  and replaces the ASCII pipeline diagram with a mermaid LR flow
  that renders natively in the GitHub README preview.  The WebUI
  launch path is now the recommended first step; CLI flag reference
  and Gunicorn deployment fold into `<details>` blocks.  Long
  content (full mode descriptions, every CLI flag, stage-by-stage
  details) stays in `README_FULL.md` / `README_FULL_zh.md`; the EN
  and ZH short variants remain structurally aligned section for
  section.  No code or config touched; `README_FULL.md`,
  `README_FULL_zh.md`, and `Crucible.png` are unchanged.

### Validation
- `pyproject.toml` and `crucible.__version__` bumped to `"1.1.7"`
  in lock-step so `test_pyproject_version_matches_package_version`
  stays green.
- Docs-only release — pytest / smoke / self-check intentionally
  skipped (only `README.md`, `README_zh.md`, and the version pin
  files were touched).

### Compatibility
- Drop-in for v1.1.6.  Pure documentation change — no Python source
  touched, no env-var defaults flipped, no public schema breaks, no
  public API rename, no CLI flag change.

---

## [v1.1.6] — 2026-05-19

### Fixed
- **`webui.app._save_env` silently reset every untouched key to its
  `.env.example` default on every Settings save** — high-impact data-
  loss bug that wiped operator-set API keys whenever an unrelated
  toggle was changed.  Reproducer: operator's `.env` carries
  `OPENROUTER_API_KEY=sk-or-v1-<real>`; they open Settings, flip a
  single boolean (e.g. `STRICT_JSON`), and click Save.  Since v1.1.0
  the front-end `saveSettings` only POSTs the *dirty* subset of keys
  (it explicitly stopped sending the full snapshot to avoid the
  separate "empty input persists `KEY=""`" bug at
  `webui/static/js/app.js:3231`), so the payload contains only
  `{"STRICT_JSON":"0"}`.  The previous `_save_env` body iterated
  `.env.example` and treated "key not in POST payload" as "emit the
  raw template line" — which for `OPENROUTER_API_KEY` is the
  documentation placeholder `sk-or-v1-xxxxxxxxxxxxxxxxxxxx`.  Net
  effect: the real OpenRouter key was overwritten by the placeholder
  on every Save, forcing the operator to re-paste it after every
  unrelated edit.  Same failure mode applied to every other key
  documented in `.env.example` (Alibaba key, OpenAI key, model
  overrides, budget thresholds, etc.).  **Git push had nothing to do
  with this** — `.env` is gitignored and has never been part of any
  commit; the timing correlation was incidental (operators tend to
  visit Settings before / after a release).  Fix loads the current
  `.env` via the existing `_load_env()` helper at the top of
  `_save_env`, builds `merged = {**current, **data}`, and iterates
  the template against `merged` instead of `data`.  Unchanged keys
  now resolve to their on-disk value; dirty keys still resolve to
  the POSTed value; keys present only in `.env` (operator-only
  overrides not declared in the template) are appended after the
  template body instead of being dropped on the floor.  The
  fallback branch for "template file missing" also operates on
  `merged` so a deleted `.env.example` no longer nukes the operator's
  existing values.

### Validation
- pytest: new `tests/test_webui_env_save_preserves_unchanged.py`
  (8 tests across the four scenarios + a comment-preservation pin
  + a `inspect.getsource()` structural pin that the
  `_load_env()` + `merged` construction stays in `_save_env`).
  All 8 pass; the unchanged-key preservation test fails loudly on
  the pre-v1.1.6 code path, confirming the regression guard.
- `tests/test_v1_1_2_audit_fixes.py::test_pyproject_version_matches_package_version`
  + `test_pyproject_version_is_at_least_1_1_2` both pass: `pyproject.toml`
  and `crucible.__version__` updated in lock-step to `"1.1.6"`.
- `crucible/smoke_test.py`: 5/5 OK.

### Compatibility
- Drop-in for v1.1.5.  Pure bug fix — no env-var defaults flipped,
  no public schema breaks, no public API rename, no CLI flag change.
- Operators who previously saved Settings on v1.1.0 – v1.1.5 and lost
  their API keys will need to re-paste those keys once on v1.1.6;
  every subsequent Save will preserve them correctly.  No migration
  script — the bug is on the *write* path, not the *read* path, so
  there is nothing on disk to migrate.
- Keys that exist in `.env` but not in `.env.example` (operator-only
  overrides — common for internal `CRUCIBLE_*` debug knobs) are now
  preserved on Save where previously they could be silently dropped
  if the POST payload didn't carry them.  This is additive.

---

## [v1.1.5] — 2026-05-19

### Added
- **README cross-document navigation + project banner image** — the four
  user-facing README variants (`README.md`, `README_zh.md`,
  `README_FULL.md`, `README_FULL_zh.md`) now carry a centred
  `Crucible.png` banner at the top followed by a two-line language /
  manual switcher.  Each variant marks its own page as
  "current / 目前頁面" (bold, non-clickable) and links the remaining
  three with descriptive labels: short variants offer `English` ↔ `中文`
  plus jumps to either full manual; long variants offer the matching
  full-manual language switch plus jumps back to either short README.
  Image is referenced via a repo-relative path so GitHub renders it
  inline on every README page without an external host or a release
  asset upload.  Layout uses HTML `<p align="center">` so the banner
  centres in the GitHub-rendered view; markdown body remains the
  single source of truth.

### Validation
- Existing pytest suite unaffected — the regression test in
  `tests/test_v1_1_2_audit_fixes.py::test_no_readme_has_stale_1747_test_count`
  still passes against all four updated READMEs (the navigation
  preamble adds no test-count claims that could go stale).
- `tests/test_v1_1_2_audit_fixes.py::test_pyproject_version_matches_package_version`
  + `test_pyproject_version_is_at_least_1_1_2` both pass: `pyproject.toml`
  and `crucible.__version__` updated in lock-step to `"1.1.5"`.

### Compatibility
- Drop-in for v1.1.4.  Pure documentation / packaging change — no
  Python source touched, no env-var defaults flipped, no public
  schema breaks, no public API rename.
- `Crucible.png` already lives at the repo root and is tracked by git;
  no `.gitattributes` / `.gitignore` adjustment needed.  Mirror
  forks / vendored copies that omit the image will see a broken-image
  placeholder but the textual content is unaffected.

---

## [v1.1.4] — 2026-05-16

### Fixed
- **Run Insights ledger polluted by test runs** — empirical inspection of
  the operator's real `.crucible_insights/` ledger at v1.1.4 ship time
  found **897 of 952 (94 %) output events were test pollution**:
  `project_name ∈ {banner_test, Quant_analysis, agent_analysis, test,
  phase0_validation_*, ...}` with `run_id=""`, versus only 3 real user
  runs.  Origin: ~6 test files (`test_failure_banner.py`,
  `test_v105_round2_extras.py`, `test_crucible_runtime.py`,
  `test_run_registry.py`, `test_integration_stage_flow.py`,
  `test_direction_gate_feedback.py`) exercise `save_project_output` or
  `record_*` paths without redirecting `CRUCIBLE_RUN_INSIGHTS_DIR` to
  `tmp_path` per CLAUDE.md § 9.5.  v1.2.0 retrieval would have seen this
  as 897 orphaned `run_id=""` rows mixed with the actual signal — a
  fatal signal-to-noise inversion for any avoidance-hint synthesis.
  Closed structurally with an **autouse pytest fixture in
  `tests/conftest.py`** that sets `CRUCIBLE_RUN_INSIGHTS_DIR` to a
  per-test `tmp_path / "_crucible_insights"` directory and resets the
  module-level recorder singleton so the env var takes effect on the
  next `get_recorder()` call.  Tests that already monkeypatch the env
  var per-test (well-isolated recorder tests) are unaffected — their
  explicit monkeypatch overrides this autouse one cleanly.  Belt-and-
  braces fix that doesn't require touching every test file.
- **Crypto classifier vocabulary expansion** — v1.1.0's pattern only
  matched explicit ticker tokens (`btc|eth|sol|...`) and CEX names
  (`binance|bybit|okx`), so real operator inputs like
  `cross-exchange options arbitrage` (got `asset:crypto` + spurious
  `asset:uncategorized`) and `liquidity_mining_market_making` (got
  `asset:uncategorized` outright) classified incorrectly.  v1.1.4
  pattern adds: DeFi terms (`defi`/`dex`/`cex`/`amm`/`stablecoin`),
  multi-word crypto idioms (`liquidity mining`/`liquidity pool`/
  `yield farming`/`market making`/`market maker`/`on-chain`/
  `off-chain`/`lp tokens`), EVM chain names (`ethereum`/`solana`/
  `polygon`/`avalanche`/`arbitrum`/`optimism`/`base`/`polkadot`/
  `cosmos`/`tron`/`ton`), unique-named protocols (`uniswap`/`aave`/
  `gmx`/`dydx`/`pancakeswap`/`sushiswap`/`makerdao`/`lido`/`raydium`),
  additional stablecoins (`usdc`/`dai`/`frax`/`lusd`/`weth`/`wbtc`),
  and additional CEX/DEX names (`kraken`/`kucoin`/`gate.io`/`bitfinex`/
  `htx`).  `curve` / `compound` / `jupiter` are intentionally
  **excluded** because they collide with English / finance vocabulary
  (`yield curve` / `compound interest` / planet Jupiter); operators
  mentioning these protocols typically include other unique DeFi
  context tokens that still trigger `crypto` correctly.  Gold / forex /
  equity / bonds / oil / futures classifications unchanged.
- **Instrument matcher word boundaries + `instrument:options` added** —
  v1.1.0's substring match `"perp" in text` misfired on
  `"perpendicular"` / `"perpetually"`, and `"spot" in text` on
  `"spotify"` / `"spotlight"`.  v1.1.4 uses `\b`-anchored regex so only
  the intended tokens trigger.  Adds `instrument:options` (was missing
  entirely — direction-debate emits for `cross_exchange_options_*`
  strategies previously left the instrument facet blank or got an
  unrelated `instrument:perpetual` from a stray substring match).
  Priority order **options > perpetual > futures > spot** so that at
  most one `instrument:*` tag emits per event — eliminating the
  co-occurrence noise (`asset:crypto` + `asset:uncategorized` for the
  same run) observed in real ledger records.
- **One-shot orphan-pruning maintenance helper** — new module
  `crucible.features.run_insights.maintenance` exports
  `prune_orphan_events(root=".crucible_insights", dry_run=False)`.
  Walks every JSONL stream, drops events with empty / whitespace
  `run_id`, rewrites each file atomically via `tempfile.mkstemp` →
  `os.replace`.  Per-stream sidecar lock acquired so a concurrent
  writer cannot interleave a half-line during the rewrite.
  Idempotent: second invocation reports zero removals.  `dry_run=True`
  reports counts without writing.  Skips the `blobs/` subdirectory —
  blob GC is a separate concern handled by the v1.1.0
  `_cleanup_orphan_tempfiles` path.  Local ledger cleanup applied at
  v1.1.4 ship time: 1690 of 1696 polluted output + params events
  removed, all 6 debate events and 3 real-run output / params events
  preserved.

### Validation
- pytest: **2 635 passed, 1 skipped** (+30 over the v1.1.3 baseline of
  2 605).  Primary new coverage is
  `tests/test_v1_1_4_classifier_and_isolation.py` (26 tests across
  four classes): `TestCryptoVocabExpansion` (9 — DeFi / market-making /
  liquidity-mining / chain-name / unambiguous-protocol classification
  + the "non-crypto inputs still classify correctly" regression guard
  + the `yield curve → bonds` disambiguation pin),
  `TestInstrumentDisambiguation` (8 — options / call / put / CJK
  options / word-bounded perp + spot / priority order + end-to-end
  through `extract_signals`), `TestConftestLedgerIsolation` (2 — env
  var points at a per-test tmp_path under pytest's tmp root +
  structural pin that conftest.py defines the autouse fixture),
  `TestPruneOrphanEvents` (5 — orphan removed / real preserved /
  idempotent / dry-run no-op / whitespace-only run_id treated as
  orphan / missing root → empty summary).
- `crucible/smoke_test.py`: 5/5 OK.
- `run_crucible.py --self-check`: OK.

### Compatibility
- Drop-in for v1.1.3.  No env-var defaults flipped, no public schema
  breaks, no public API rename.
- The classifier vocabulary expansion is additive: every operator
  input that classified to a non-`uncategorized` category at v1.1.3
  classifies to the same category at v1.1.4.  Some inputs that
  previously fell to `uncategorized` will now classify to `crypto`
  (intended).
- Existing ledger files predating v1.1.4 with `run_id=""` orphan rows
  remain on disk until `maintenance.prune_orphan_events()` is invoked.
  The autouse conftest fixture only prevents *new* test pollution.
  Operators wishing to clean their existing ledger should run:
  `python -c "from crucible.features.run_insights.maintenance import
  prune_orphan_events; print(prune_orphan_events('.crucible_insights'))"`.

---

## [v1.1.3] — 2026-05-15

### Fixed
- **OpenRouter `usage.cost` was silently dropped for every codegen +
  formatter call**, leaving the run summary's
  `cost_source="crewai_metrics_with_pricing"` (token × local-table
  estimate) instead of the authoritative `"openrouter_api"` (actual
  billed USD).  v1.1.1 wired
  `inject_openrouter_usage_extra_body` at three LLM construction
  sites — `section_02._create_openrouter_llm` (main / direction-judge
  / librarian), `section_01._make_formatter_llm`, and
  `section_05._make_codegen_llm` — so every request body opted into
  `usage: {include: true}`.  But only section_02 *also* registered the
  HTTP interceptor (`get_openrouter_http_interceptor()`) and langchain
  callback handler (`get_openrouter_callback_handler()`) that actually
  capture the returned `usage.cost`.  Sections 01 and 05 sent the
  opt-in flag with no reader on the other end → response cost field
  silently elided → fallback to local pricing table.  Codegen is the
  single largest cost sink in a Quant run, so a missing interceptor
  there under-reports the entire summary by a wide margin and the
  divergence becomes glaringly visible when switching model tiers
  (e.g. `deepseek/deepseek-v4-pro` → `-flash`: the local table's
  `(0.14/M, 0.28/M)` estimate for v4-flash diverges from the actual
  OpenRouter bill in a way the v4-pro `(0.55/M, 2.19/M)` row happens
  to mask).  Section_01 and section_05 now register
  `interceptor=` + `callbacks=[...]` inside the same
  `if provider_tag == LLM_PROVIDER_OPENROUTER:` branch as the
  pre-existing opt-in injection — identical wiring to section_02:2157-2163,
  scoped so non-OpenRouter providers (Alibaba, Ollama) don't get a
  spurious interceptor attached.  Idempotent merge into pre-existing
  `kwargs["callbacks"]` and `kwargs["interceptor"]` (operator overrides
  preserved).
- **`OpenRouterUsageHTTPInterceptor.on_inbound` / `aon_inbound`
  silently swallowed every capture attempt for unread response
  streams.**  Both hooks delegated straight to the synchronous
  `_capture_openrouter_usage_from_http_response`, which calls
  `response.json()` — but crewai's `HTTPTransport` hands the
  interceptor a `httpx.Response` whose body has NOT been read yet.
  `response.json()` then raises `httpx.ResponseNotRead`, which the
  capture helper's broad `except Exception: return False` swallows,
  and OpenRouter's `usage.cost` is dropped on the floor.  Both hooks
  now force-load the body before delegating — sync `message.read()`
  in `on_inbound`, `await message.aread()` in `aon_inbound`.  Reads
  are idempotent (no-op when already loaded) and skipped for
  `content-type: text/event-stream` responses so streaming chat
  completions are unaffected.

### Validation
- pytest: **2 605 passed, 1 skipped** (+17 over the v1.1.2 baseline of
  2 588).  Primary new coverage is
  `tests/test_v1_1_3_openrouter_cost_capture.py`: 6 structural pins on
  section_01 / section_05 interceptor wiring inside the OpenRouter
  branch, 5 behavioural pins on the body-read fix (sync + async +
  event-stream skip + source-order check), 2 end-to-end pins driving
  the interceptor with a realistic OpenRouter response and asserting
  `cost_source="openrouter_api"` with exact `usage.cost`, plus the
  token-pricing fallback when `usage.cost` is omitted upstream.
- `crucible/smoke_test.py`: 5/5 OK.
- `run_crucible.py --self-check`: OK.

### Compatibility
- Drop-in for v1.1.2.  No env-var defaults flipped, no public schema
  breaks, no public API rename.  Operators with a custom
  `_make_formatter_llm` / `_make_codegen_llm` override that passes
  pre-built `callbacks=[...]` or `interceptor=...` are honoured —
  v1.1.3 only appends the OpenRouter handler to an existing callback
  list and only sets `interceptor=` when the kwarg is absent.
- Existing pre-v1.1.3 saved projects carry `cost_source="crewai_metrics_with_pricing"`
  or `"estimated"`; v1.1.3+ runs will start emitting `"openrouter_api"`
  once OpenRouter responses successfully reach the interceptor.  Run
  history with both source labels coexists; the higher priority
  (`openrouter_api`) wins in `_summarize_cost_source` aggregation
  whenever any record has it.

---

## [v1.1.2] — 2026-05-14

### Fixed
- **`run_meta.json["run_id"]` desynchronised from the Run Insights
  ledger** (`crucible/modules/section_07_selfcheck_output_main.py`).
  Section_07 minted a fresh `uuid.uuid4().hex` (32-char) instead of
  resolving the run-correlation ContextVar (8-char hex from
  `webui/app.py`'s `uuid.uuid4().hex[:8]`), so `run_meta["run_id"]`
  and the ledger's `run_id` diverged silently — breaking the v1.2.0
  retrieval join that associates a saved project with its own
  Stage 0 debate rejections. Now resolves via the canonical
  three-tier chain `_get_run_id() →
  os.environ.get("CRUCIBLE_RUN_ID").strip() → fresh
  uuid.uuid4().hex[:8]` (matches `record_output_method` /
  `record_runtime_params`); the defensive fallback calls
  `_set_run_id(...)` so any later emit converges on the same id.
  Non-retroactive — pre-v1.1.2 saved projects keep their mismatched
  ids; v1.2.0 retrieval falls back to `(project_name, timestamp)`
  join with a tolerance window for those rows.

### Audit-fix sweep (two 4-agent post-release passes)

Two thematic 4-agent audits ran after the initial v1.1.2 fix landed.
All findings are attributed to v1.1.2 (no version bump); grouped by
area below.

- **Run-id discipline at every producer site.** `run_crucible.py`
  flat launcher now bridges `CRUCIBLE_RUN_ID` (was missing — only
  `__main__.py` and `run_crucible_enhanced.py:main()` did, so
  `error_record` emits under the flat launcher wrote `run_id=""`).
  `set_run_id` / `run_context` apply `.strip()` before truthiness
  check so a whitespace-only env value can no longer mint a
  3-space id. `resilience.error_record` and `section_02`
  direction-debate emits use the canonical three-tier chain with
  `LOGGER.warning` on empty resolution; `mode` / `stage` defaults
  switched to `mode_unknown` / `stage_unknown` sentinels for
  v1.2.0 retrieval aggregation parity. `recorder._emit` strips
  before the 64-char truncate.
- **v1.2.0 retrieval observability.**
  `record_direction_debate_rejection` no longer gated on
  `gap_info` content — every `force_none` verdict telemetered
  (was silently dropped for non-evidence-shaped reasons like
  `judge_explicit_none` / `unanimous_reject`).
  `InsightsRecorder._warned_once` split into
  `_warned_unknown_reason` + `_warned_emit_failed` (a benign
  first log was muting every subsequent backend WARN). DeepSeek
  `sk-` + 32 hex matches a vendor-specific tier before the
  generic `{40,80}` pattern. `LocalJSONLBackend.write_event`
  uses canonical JSON for byte-for-byte parity with the v1.2.0
  Cloudflare `DualWriteBackend`. `prune_stream` drops
  writer-crash partial tails (was promoting them with a
  synthetic `\n`). Set-of-floats redaction uses `canonical_json`
  for cross-platform `content_id` stability.
  `_NullRecorder.backend` is now `_NoOpBackend` (was `None`,
  breaking the `.backend.X` parity contract). WebUI insights
  endpoints switched to lazy `_iter_jsonl_stream` /
  `_tail_jsonl_stream` so multi-week ledgers cannot OOM the
  dashboard.
- **WebUI memory ceiling, concurrent cap, and exception hygiene.**
  New `_runs_semaphore = BoundedSemaphore(_RUNS_MAX_CONCURRENT)`
  (default 4, env `CRUCIBLE_WEBUI_MAX_CONCURRENT_RUNS`, ceiling
  64) caps concurrent runs; `acquire(timeout=60)` re-checks
  `status` after acquire so a cancelled run doesn't spawn
  subprocess; release guarded by an `acquired` flag against
  `BoundedSemaphore` over-release. Per-run output buffer capped
  at `_RUNS_MAX_OUTPUT_LINES` (default 50 000) via FIFO
  eviction; SSE resume index adjusts cumulative `sent` and
  emits a one-shot truncation notice.
  `_periodic_evict_runs` daemon timer fires every 60 s for
  headless deployments. `socket.getaddrinfo` in `_is_safe_url`
  bounded at 3 s (Slowloris-on-DNS defence).
  `X-Forwarded-Host` gated on `CRUCIBLE_TRUST_FORWARDED`
  opt-in, split on first comma for multi-hop proxy chains.
  Nine endpoints (`api_save_env`, `api_list_projects`,
  `api_budget_status`, `api_webhook_history`, `api_notify_test`,
  `api_run_signal`, `api_v169_metrics` × 2, `cost_trend`,
  grafana dashboard) route through a new `_safe_500` helper
  that logs the full exception via `LOGGER.exception` and
  returns a `log_id`-stamped generic 500.
  `_redact_for_client` strips secrets via
  `_VALUE_SECRET_PATTERNS` + Windows/POSIX absolute-path
  patterns; applied to `last_error`, `error_msg`, DB write
  side, and captured pipeline stdout (single-point redact at
  the `_run_worker` capture boundary, covering 15+
  `print(f"[Error] ... {e}")` call sites across
  `section_01/02/04/05/06`). SSE `__done__` pre-padded with a
  2 KB SSE comment to force proxy flush.
- **Subnormal floor sweep across quant features.** v1.1.0
  fifth-pass tightened `prev > 0` → `prev > 1e-14` in
  `quant_analytics`/`regime_detector`/`dynamic_correlation`/
  `factor_analyzer`; the sibling paths in `monte_carlo`,
  `risk_attribution`, `tearsheet._equity_to_returns`,
  `regime_detector._STD_FLOOR`, `portfolio_backtest`, and
  `transaction_cost_model` kept the loose floor and could
  detonate on an IEEE 754 subnormal `prev` (5e-324 → return
  ≈ 1e+300 after the division). All seven now use `> 1e-14`.
  `dynamic_correlation._align_return_series` switched from
  `0.0` missing-observation default to `float("nan")` so the
  existing `_pearson_r` NaN-strip (v1.1.0 G-9) sees the gap
  correctly.
- **SSRF hardening in `web_research/http_clients.py`.** LLM
  citation-fetch path used a weaker SSRF guard than the WebUI:
  `_is_public_http_url` only rejected via `ip.is_global`,
  allowing multicast, IPv4-mapped IPv6, 6to4 / NAT64 wrapping
  private v4, reserved ranges, userinfo smuggling
  (`http://victim@evil.com/`), and IPv6 scope-id. Helper now
  mirrors `webui.app._addr_is_safe` (v1.1.0 fifth-pass G-2).
  `httpx.Client(follow_redirects=True)` disabled; new
  `_request_with_safe_redirects` revalidates every hop, caps at
  3 hops, and per RFC 7231 §6.4 demotes 301/302/303 +
  POST/PUT/PATCH to GET-without-body so the original body
  cannot replay against the redirected endpoint.
- **`section_03` REDACT_RULES ReDoS hardening.** Eight unbounded
  `{N,}` quantifiers (sk-/gh prefix, credentials × 4, password,
  JWT three segments) bounded to realistic limits matching the
  v1.1.0 third-pass `_VALUE_SECRET_PATTERNS` discipline (sk-/gh
  ≤ 200, credentials ≤ 500, JWT 300/2000/300). DeepSeek
  `sk-[A-Fa-f0-9]{32}` vendor-specific pattern added before the
  generic `sk-[A-Za-z0-9]{20,200}`.
- **env_bool / env_int whitelist unification.** Four hand-rolled
  call sites (`CODEX_REQUIRE_SNAPSHOT`, `CRUCIBLE_UNIVERSAL_CROSSREF`,
  `CRUCIBLE_QUANT_REQUIRE_TESTS` in `section_06`, plus the
  generated smoke-test stub) routed through `_env_bool` for
  consistent `{1, true, yes, on}` semantics. Two raw
  `int(os.environ.get(...))` paths (`CRUCIBLE_PRE_CODEGEN_MIN_SCORE`
  in `section_03`, `CRUCIBLE_QUANT_DRYRUN_TIMEOUT` in
  `section_06`) routed through `_env_int` so typo sentinels
  (`unlimted`) trip the project-wide whitelist warning instead
  of silently flooring.
- **Schema-first non-staged path + finite-only float gates.**
  `build_codegen_crew` now passes `_extract_quant_schema_signatures`
  into its prompt template (only the staged batch worker did
  before; small Quant projects on the single-shot path bypassed
  the v1.0.5 schema-first contract).
  `output_validation._coerce(float)` rejects NaN / Inf via
  `math.isfinite` (symmetric with `_env.env_float`'s
  `finite_only`); `section_07._outcome_score` gated through the
  same `math.isfinite`; `_coerce_json_dict` uses
  `json.dumps(allow_nan=False)` so NaN / Infinity literals
  cannot round-trip through the LLM-controlled path.
- **Cross-process file locking + content-addressable short-circuit.**
  `read_events` now acquires the sidecar lock `_stream_lock_path`
  for the duration of the read (was racing against
  `prune_stream`'s `os.replace` on Windows, where the held-open
  file rejected the rename and silently aborted with
  `PermissionError`). `write_blob` adds a `path.exists()`
  short-circuit before `tempfile.mkstemp` — content-addressable
  storage makes this correctness-preserving and halves disk I/O
  for retry-heavy emit paths.
- **`streaming._worker` re-raises `(SystemExit, KeyboardInterrupt)`.**
  The previous `except BaseException` clause posted the
  operator signal onto the result queue as an "error" chunk,
  hiding Ctrl-C / SIGTERM intent. Now narrowed to `Exception`
  after the explicit signal re-raise.
- **`run_meta.json["timestamp_utc"]` additive seam.** v1.2.0
  retrieval joins across machines; a naive local-time
  `timestamp` goes ambiguous across DST / TZ. Now emits an
  additive `timestamp_utc` field (`YYYY-MM-DDTHH:MM:SS.ffffffZ`);
  folder name stays local-time for operator readability.
- **Defence-in-depth on the Google Fonts CDN**
  (`webui/templates/index.html`). SRI does not apply (CSS body
  varies by UA per MDN); hardened via
  `referrerpolicy="no-referrer"` + `crossorigin="anonymous"` +
  `onerror` graceful-degrade to system fonts if the CDN is
  blocked.
- **WebUI bilingual + CSRF coverage.** Three `stage_models`
  tooltips (`librarian_model`, `primary_model`,
  `direction_judge_model`) converted to `{en, zh}` per CLAUDE.md
  § 10 bilingual invariant. `setLanguage` POST now sets an
  explicit `X-Requested-With` header so a future early-load
  script using native XHR cannot silently 403 the
  language-save call.
- **Version + docs + CI hygiene.** `pyproject.toml::project.version`
  bumped `1.0.0` → `1.1.2` (was stuck at `1.0.0` despite six
  follow-on releases; wheels built from this tree advertised
  the wrong version). Added `crucible.__version__` mirror in
  `crucible/__init__.py`; regression test pins both equal on
  every release. 4 README variants no longer cite `1747 tests`
  (version-agnostic pointer to `CHANGELOG.md`).
  `ARCHITECTURE.md` removed three references to never-shipped
  `v1.0.6`. `.github/workflows/ci.yml::compileall -x` extended
  to `(saved_projects|skill_staging|\.crucible_insights)`.
  `requirements.txt` carries `>=` minimum-version floors for
  `crewai`, `langchain-openai`, `litellm`, `pydantic`,
  `httpx`, `tiktoken`, `flask`.
  `test_streaming.py::test_error_chunk_has_elapsed_seconds`
  now uses a measurable 50 ms delay + `> 0.0` matching the
  sibling Windows-resolution pattern.

### Observed but not patched (rationale)

- **WebUI per-line `_runs_lock` acquisition** — batching to
  50 ms / N-line flushes would touch the worker hot loop; the
  perf win at 3+ concurrent runs does not outweigh regression
  risk. Deferred until a representative load test exists.
- **`cost_tracker.py` integer micro-cents refactor** —
  docstring promises integer micro-cents; implementation uses
  floats end-to-end. Functionally correct for the cost
  magnitudes this project handles; integer conversion is a
  v1.2.0-scope refactor.
- **`_create_openrouter_llm` mutates `os.environ`** —
  subprocess isolation contains the leak in the current
  WebUI architecture; in-process re-entrant LLM usage is not
  on the v1.2.0 roadmap.
- **DSR sample-moment Bessel correction** — verified against
  Bailey & López de Prado (2014) §3.1 which explicitly uses
  biased (population) moments per the paper's stated
  rationale; current code matches the paper and is
  intentionally NOT Bessel-corrected.
- **DNS-rebinding hardening for `_is_public_http_url`** —
  adding `socket.getaddrinfo` per-call would break the
  existing offline-mocked-httpx test suite (`api.example.com`
  is a common synthetic hostname). Deferred behind a future
  `CRUCIBLE_WEB_RESEARCH_STRICT_DNS` env flag.

### Validation

- pytest: **2 588 passed, 1 skipped** (+101 over the v1.1.1
  baseline of 2 487). New regression coverage in
  `tests/test_v1_1_2_run_id_consistency.py` (14 tests for the
  section_07 line-1360 fix),
  `tests/test_v1_1_2_audit_fixes.py` (48 tests across seven
  groups), `tests/test_v1_1_2_sixth_pass.py` (39 tests across
  twelve classes for the second-pass H-N / M-N fixes).
- `crucible/smoke_test.py`: 5/5 OK.
- `run_crucible.py --self-check`: OK.

### Compatibility

- Drop-in for v1.1.1. No env-var defaults flipped, no public
  schema breaks, no public API rename.
- Two new env knobs (`CRUCIBLE_WEBUI_MAX_CONCURRENT_RUNS=4`,
  `CRUCIBLE_WEBUI_MAX_OUTPUT_LINES_PER_RUN=50000`) have safe
  defaults larger than any historical real-world workload.
- `pyproject.toml::project.version` corrected `1.0.0` →
  `1.1.2`: wheels built from this tree now advertise the
  right version; `importlib.metadata.version("crucible")`
  returns `1.1.2`. Operators pinning the previous (wrong)
  `1.0.0` should update — package contents have always been
  v1.0.5+ regardless of the wheel label.
- `run_meta.json` gains an additive `timestamp_utc` field
  (existing local-time `timestamp` unchanged).
- Pre-v1.1.2 saved projects keep their mismatched `run_id`
  values; v1.2.0 retrieval should fall back to
  `(project_name, timestamp)` join with a tolerance window
  for those rows.

---

## [v1.1.1] — 2026-05-14

### Fixed
- **Dashboard cost was $0.00 on DeepSeek v3/v4 models despite real
  OpenRouter spend** (`crucible/modules/section_00_bootstrap_and_utils.py`).
  Two-fold root cause: (a) `OPENROUTER_MODEL_PRICING` had no entries for
  `deepseek-v4-flash` / `v4-pro` / `v3-chat` / `v3-coder` /
  `v3-reasoner` / `r1`, so `_get_model_pricing()` fell through to
  `(0.0, 0.0)`, `extract_and_set_usage_from_crew()` took the
  `pricing_known=False` branch, and `total_cost_usd=0.0` /
  `cost_source="estimated"` was the source data the v1.0.5 promotion
  path correctly copied into `run_meta.json` (the promotion wasn't
  broken; the source was zero); (b) OpenRouter only populates
  `response.usage.cost` (actual billed USD) when the request body
  carries `"usage": {"include": true}`, which crewai/litellm LLM
  construction never set — so even when the local table DID resolve,
  the estimate was `tokens × table_price`, not the real OpenRouter
  charge.
- **A — Pricing table extended + family-prefix fallback.** Explicit
  entries added for all v3/v4 DeepSeek IDs. New
  `OPENROUTER_MODEL_FAMILY_PRICING` keyed by vendor prefix
  (`deepseek/deepseek-r`, `deepseek/`, `openai/gpt-5`, `openai/gpt-4o`,
  `anthropic/`, `google/`, `z-ai/`, `minimax/`, `meta-llama/`,
  `mistralai/`) so a brand-new variant within a known family (e.g.
  future `deepseek-v5-flash`, `openai/gpt-6.0`) gets a non-zero
  estimate rather than silently collapsing to zero. Longest-matching
  prefix wins, so `deepseek-r1-distill-future` resolves to reasoner
  tier rather than generic chat. Family entries are CONSERVATIVE
  (cheapest in-family variant) so under-reporting is preferred over
  over-billing surprise.
- **B — Request body opts into OpenRouter usage accounting.** New
  `inject_openrouter_usage_extra_body()` helper merges
  `additional_params={"extra_body": {"usage": {"include": True}}}` into
  crewai.LLM kwargs → litellm → openai SDK → request body, so the
  response now carries `usage.cost` and the existing
  `set_openrouter_usage()` picks it up automatically. Idempotent, merges
  with pre-existing `extra_body` / `usage` keys, preserves operator-set
  `include=False` overrides, defensive against non-dict
  `additional_params`. Wired into three LLM construction sites:
  `section_02_research_and_llm._create_openrouter_llm` (main /
  direction-judge / librarian), `section_01_extraction_and_reformat._make_formatter_llm`
  (schema reformatter), and `section_05_analysis_and_codegen._make_codegen_llm`
  (codegen — single largest cost sink in a Quant run; without this,
  summaries under-reported by ~70 %). The two sibling helpers detect
  OpenRouter via the `_quant_llm_provider` attribute stamped by
  `_create_openrouter_llm`.
- **Cost-source labelling correctness preserved.** `usage.cost` from
  OpenRouter → `cost_source="openrouter_api"` (highest priority in
  `_USAGE_COST_SOURCE_PRIORITY`); local table fallback →
  `"openrouter_tokens_with_pricing"` / `"crewai_metrics_with_pricing"`;
  remains `"estimated"` only when BOTH the API opt-in failed AND the
  model is outside every known vendor family — a genuinely-untracked
  model that the operator should add explicitly.

### Validation
- pytest: **2 487 passed, 1 skipped** (up from 2 451; +31 in
  `tests/test_v1_1_1_cost_tracking_regressions.py`: 4 v3/v4 explicit-
  entry resolutions, 7 family-fallback cases (longest-prefix tie-break,
  unknown vendors still return `(0,0)`, exact entries beat family),
  2 family-table invariants (positive prices, v4-flash/v4-pro are
  EXPLICIT not fallback-only), 9 `inject_openrouter_usage_extra_body`
  branch tests (idempotence, three nesting levels, operator-False
  override preservation, three malformed-input classes), 3
  `inspect.getsource` structural pins on the three LLM construction
  sites per CLAUDE.md § 9.6 producer→consumer wiring rule, 6
  parametrised cost-zero regression pins for v3/v4 IDs). Two
  pre-existing tests in `test_openrouter_cost_tracking.py` updated for
  the new family-fallback contract (`gpt-5.4-pro` now resolves via
  family pricing instead of zero; `cohere/` retained as the truly-
  unknown-vendor zero case). Zero regressions.
- `crucible/smoke_test.py`: 5/5 OK; `run_crucible.py --self-check`: OK.

### Compatibility
- Drop-in for v1.1.0. No env-var defaults flipped, no public schema
  changes. OpenRouter responses now ~100 bytes larger (server-pre-
  computed `usage.cost` / `usage.cost_details`); no measurable latency
  impact. Operators with custom IDs outside the vendor families in
  `OPENROUTER_MODEL_FAMILY_PRICING` should add explicit
  `OPENROUTER_MODEL_PRICING` entries.

---

## [v1.1.0] — 2026-05-14

Major release: **Run Insights ledger** (cross-run telemetry with content-
addressable events, evomap.ai-aligned schema, Cloudflare/D1/R2 seam frozen
for v1.2.0), **15-point backtest hardening** (real-data integrity guard
default-on), and **five consecutive four-agent audits** that landed ~110
fixes across security, numerics, ledger atomicity, and WebUI. Final test
baseline: **2 451 passed, 1 skipped** (from 2 229 at v1.1.0 inception).

### Added

- **Run Insights ledger** (`crucible/features/run_insights/`): cross-run
  telemetry on four JSONL streams (output / error / debate / params) under
  `.crucible_insights/`. Every event carries
  `content_id = "sha256:" + sha256(canonical_json(event \ content_id))`,
  a tag-style `signals[]` index, an `env_fingerprint`
  (python/platform/arch/model_id/llm_provider), and an `outcome` block.
  Five emit points wired (Stage 0 force-none + parse-fail debates,
  `save_project_output` output_method + Quant runtime_params,
  retry-exhausted error in `resilience.py`); all best-effort and
  exception-swallowing so the ledger can never break the pipeline.
  Asset-category classifier (Quant-only) auto-tags `signals[]` with
  `asset:gold|silver|oil|crypto|forex|futures|options|equity|bonds|
  uncategorized` via deterministic dictionary lookup — zero LLM cost,
  feeds v1.2.0 retrieval scoping.
- **Cloudflare Workers + D1 + R2 backend seam**
  (`run_insights/backends.py`): `StorageBackend` Protocol with
  `LocalJSONLBackend` shipped + `CloudflareBackend` / `DualWriteBackend`
  stubs that fail fast at construction time. Module docstring freezes
  the v1.2.0+ cloud contract: D1 `insight_events` schema, R2 object key
  layout, `POST /v1/insights/events|batch` + `GET /v1/insights/events`
  Workers HTTP surface, and the JavaScript canonical-JSON algorithm so
  the Worker computes byte-identical `content_id` values. Setting
  `CRUCIBLE_RUN_INSIGHTS_BACKEND=cloudflare` without API URL/token
  fails fast — operators expecting uploads cannot accidentally land in
  disk-only mode.
- **Sovereign-portability archive CLI**
  (`run_insights/export.py`): `python -m
  crucible.features.run_insights.export <out.tar.gz>` bundles the
  ledger into a gzipped tar with per-stream sha256 + line counts.
  Archive layout matches the future R2 hierarchy so `crucible insights
  upload` becomes a tar-walk + PUT.
- **Run Insights view tab + dashboard widget** (`webui/`): each session
  panel gains a 📚 Insights tab alongside ⬛ Terminal and ⬡ Agent Flow,
  rendering per-run insights grouped by stream with content-id, env
  fingerprint, outcome badge, and signal tags. Dashboard gains a "Run
  Insights Ledger" card. Three new read-only endpoints back it:
  `GET /api/insights/summary`,
  `GET /api/insights/events?stream=&run_id=&project_name=&since=&kind=&limit=`,
  `GET /api/run/<run_id>/insights`.
- **21 new env vars** under "Run Insights ledger" in `.env.example` —
  14 active (toggles + per-stream record flags + backend + ledger dir +
  inline-blob threshold + max entries per stream + redact toggle + 5
  Cloudflare API keys) plus 7 v1.2.0 retrieval/distillation
  placeholders shipped listed-but-ignored. `RECORD_PARAMS=auto`
  (default) records `runtime_params` only on Quant; typos return
  `auto` (not truthy-coerced) per the env-bool whitelist rule.
- **Backtest synthetic-data integrity guard**: new
  `BACKTEST_REQUIRE_REAL_DATA=1` (default ON) + new
  `BacktestDataIntegrityError`. Both synthetic exit paths in
  `prepare_data()` (explicit `BACKTEST_DATA_SOURCE=synthetic` AND the
  auto-cascade fallback after yfinance / ccxt / Binance / project
  `data_provider.py` all return empty) raise with a multi-line message
  listing attempted providers, install commands, and the explicit
  opt-out path. With the guard off, `report.warnings` carries a loud
  persistent annotation. Three new tests cover the guard; five
  pre-existing synthetic tests updated to opt out via
  `patch.dict(os.environ, {"BACKTEST_REQUIRE_REAL_DATA": "0"})`.
- **Backtest 15-point hardening** (`crucible/features/backtest_runner.py`):
  (HIGH-1) non-crypto symbols no longer fall back to BTCUSDT under any
  cascade — Sharpe / drawdown / win-rate stay attached to the
  requested asset; (HIGH-2) forced-source failure respects the
  integrity guard; (HIGH-3) `_has_data_file` validates OHLCV columns +
  row count; (HIGH-4) partial fetched data rejected with profile-aware
  row-count threshold; (HIGH-5) `_run_project_data_provider` sandboxed
  (1 MB stdout cap, realpath-inside-`code_dir` enforcement, non-zero
  rc → failure, cross-drive paths rejected on Windows); (MED-6)
  disk-level data cache with TTL (`~/.crucible/data_cache/`);
  (MED-7) `fallback_rows` sentinel fixed (now `Optional[int]`);
  (MED-8) symbol + timeframe detection share a single file walk;
  (MED-9) `FetchOutcome(csv_text, error_kind, detail)` classified
  diagnostics — operator sees `yfinance: rate_limit — HTTP 429`
  instead of `no data returned`; (MED-10) `data_start_date` /
  `data_end_date` / `data_staleness_days` surfaced + staleness
  warning when last bar > `BACKTEST_DATA_MAX_STALENESS_DAYS` (default
  7); (LOW-11) optional parallel-fetch cascade
  (`BACKTEST_PARALLEL_FETCH=1`); (LOW-12) stricter `_is_crypto_symbol`
  — `BRK-B` / bare `BTC` now classified non-crypto; (LOW-13)
  configurable `BACKTEST_SYNTHETIC_SEED` (`42` / `"random"` / typo
  → 42 + actual seed recorded in `result.profile`); (LOW-14)
  `BACKTEST_PREPARE_DATA_ONLY` dry-run mode; (LOW-15) provider-profile
  limit regression tests. `PrepareDataResult` NamedTuple replaces the
  prior 3-tuple return (breaking for direct API consumers).
  `BacktestReport.data_actual_symbol` records the exact symbol form
  passed to the succeeding provider.
- **WebUI Settings + flag panels env-sync machinery**
  (`webui/static/js/app.js` + `webui/app.py`): `_ENV_CACHE` populated
  on init by one `GET /api/env`; `ENV_BACKED_FLAGS` maps ~40 frontend
  flag keys to backend env var names (`cache`→`LOCAL_CACHE`,
  `strict_json`→`STRICT_JSON`, `gate_control`→`GATE_CONTROL_ENABLED`,
  14 `ENHANCED_*` post-processing flags, 13 `ENHANCED_*` Quant
  Analytics flags, etc.); `_resolveFlagInitialChecked()` reads live
  `.env` so Idea / Path panels no longer ignore Settings-page state.
  `_resolve_run_insights_env_overrides(flags)` + `_run_worker(...,
  env_overrides=...)` plumb per-run checkbox toggles into
  `_child_env` while whitelisting `CRUCIBLE_RUN_ID` from override.
  All four call sites (`api_start_run`, `webhook_trigger`, two A/B
  variants) pass the resolved dict through.
- **WebUI Settings — full bilingual sweep of `KEY_META`**: all 187
  entries are now `desc:{en,zh}` so the top-right language toggle
  flips every group together (was only 9 entries previously). New
  entries must use `{en, zh}` going forward (CLAUDE.md § 10
  documents the enforcement).
- **WebUI static-asset cache busting**:
  `_static_asset_hash()` computes `sha1(file_bytes)[:10]` for
  `js/app.js` and `css/app.css`, embedded as `?v=<hash>` in
  template `<script>` / `<link>` tags. Editing JS/CSS + restarting
  Flask now forces browsers to fetch the new bundle on next page
  load without operator Ctrl+F5.
- **Run-correlation ID bridge**: WebUI `run_id` and pipeline
  `run_correlation` ContextVar wired together at three points —
  (1) `_run_worker` injects `CRUCIBLE_RUN_ID` into child env,
  (2) `run_crucible_enhanced.main()` + `crucible/__main__.py`
  call `set_run_id(os.environ.get("CRUCIBLE_RUN_ID") or None)` at
  entry, (3) all five emit points chain through `_get_run_id()` →
  `os.environ.get("CRUCIBLE_RUN_ID")` → local-meta fallback so a
  missing ContextVar still resolves the WebUI session ID.
- **Synthetic golden-run regression fixture**
  (`tests/regression/fixtures/SyntheticGoldenRun/`): adds the
  first real constraint so the regression harness runs (not just
  skips) on every CI invocation.

### Fixed

A five-pass cross-cutting audit (initial 15-point backtest pass +
2nd / 3rd / 4th / 5th four-agent runs) landed ~110 fixes. Findings
are grouped by area below; tagged with the round (B = backtest,
H/M = 2nd-pass HIGH/MEDIUM, T = 3rd-pass, F = 4th-pass, G = 5th-pass)
so future audits can cross-reference.

#### Security (WebUI + ledger redaction)

- **SSRF guard fully closed across all known IPv4/IPv6 embeddings.**
  Earlier rounds caught IPv4-mapped IPv6 (`::ffff:10.0.0.1`,
  T16). Fourth-pass F-2 added recursive `_ipv6_embedded_v4()` that
  rejects (a) `::w.x.y.z` deprecated compat, (b) `2002:wxyz:abcd::`
  6to4, (c) `64:ff9b::w.x.y.z` NAT64 — all three previously had
  `is_global=True` despite embedding RFC1918 v4. Fifth-pass G-2
  closed the remaining hole: Python's `is_global` returns True for
  **multicast (224.0.0.0/4)**, broadcast, and several reserved
  ranges; `_addr_is_safe` now also rejects `is_multicast` /
  `is_reserved` / `is_unspecified` / `is_loopback` /
  `is_link_local`. Affects `/api/notify/test` and pipeline
  notification retries.
- **Redirect-based SSRF blocked (G-3).** `_do_request` and
  `_send_notification_with_retry` previously used the default
  `urllib.request` opener which auto-follows 30x — an
  attacker-controlled HTTPS endpoint passing `_is_safe_url` could
  respond `302 Location: http://169.254.169.254/...` (AWS IMDS) or
  `http://127.0.0.1:5000/api/env` and the request would auto-follow,
  sending the operator's Authorization header to the internal host.
  New `_NoRedirectHandler` suppresses auto-follow; new
  `_safe_urlopen` helper re-validates every URL through
  `_is_safe_url` per hop (DNS-rebinding tight), clears the request
  body on 301/302/303 per RFC 7231 §6.4, and caps redirects at 3.
- **DNS rebinding mid-retry closed (T17).** `_send_notification_with_retry`
  now calls `_is_safe_url()` on **every** attempt inside the retry
  loop, shrinking the rebinding window from ~7 s (full retry budget)
  to milliseconds.
- **`_is_safe_url` rejects userinfo + IPv6 scope-id (T16).**
  `http://victim@evil.com/` previously slipped through because
  `urlparse` returned `hostname="evil.com"` while userinfo travelled
  in Authorization headers downstream. Link-local IPv6 with
  scope-id (`fe80::1%eth0`) also rejected.
- **CSRF gate handles `Origin: null` + reverse proxies (T15 + F-3/10).**
  Sandboxed iframes / `data:` / Safari send literal `Origin: null` —
  now untrusted. Absent `Origin` falls back to `Referer.netloc` vs
  `request.host`. Reverse-proxy false-positive (where `request.host`
  is the internal `127.0.0.1:5000` but `Referer` carries the public
  host) eliminated by also consulting `X-Forwarded-Host` AND a new
  opt-in `CRUCIBLE_TRUST_FORWARDED=1` env that wires Werkzeug's
  `ProxyFix` for end-to-end forwarded-header trust.
- **`MAX_CONTENT_LENGTH` 1 MB cap (H6) → env-configurable (4th pass).**
  Operators pasting long idea briefs can raise via
  `CRUCIBLE_MAX_CONTENT_LENGTH_MB` (clamped to [1, 64]).
- **`X-Requested-With` enforced for cross-origin state changes (H7).**
  Global `before_request` rejects POST/PUT/PATCH/DELETE to `/api/*`
  from cross-origin browsers that omit the header. Drive-by attacks
  from a malicious tab are blocked at the routing layer; server-to-
  server callers (curl, schedulers — no `Origin`) pass through.
  Frontend `fetch` shim auto-attaches the header but is pathname-
  strict and same-origin: `new URL(...).origin === location.origin
  && pathname.startsWith('/api/')` (T18 + F4) — privacy leak via
  third-party URLs containing `/api/` closed. Malformed URLs now
  fail closed (4th pass).
- **Chart.js CDN pinned with SRI (M5).** `<script>` carries
  `integrity="sha384-..."` + `crossorigin="anonymous"` +
  `referrerpolicy="no-referrer"`. A jsDelivr compromise can no
  longer inject JS into the WebUI origin.
- **`Content-Type` reverted to bare `application/json`** (4th pass).
  The `; charset=utf-8` parameter (T17) caused strict
  Slack/Discord-style receivers to 400; body bytes are still UTF-8.
- **PII redaction now value-aware, not just field-name (H4).**
  `_VALUE_SECRET_PATTERNS` covers Anthropic Claude
  (`sk-ant-(?:api|sid|admin|oat)\d+-...` — `oat\d+` added in G-4
  for Claude Code OAuth tokens), OpenAI legacy + project +
  service-account (`sk-svcacct-` added in G-5), OpenRouter
  (`sk-or-v1-`), Google Gemini (`AIza`), xAI (`xai-`), Slack
  (`xox[bparseu]-`), GitHub PAT/App, JWTs (including
  URL-percent-encoded `%2E` variants per T20), Bearer/Basic auth,
  Stripe (`(sk|rk|pk)_(test|live)_`), AWS keys (`AKIA/ASIA`), and
  generic `password=` / `api_key=` URL fragments (upper bound
  raised from 200 → 2000 so long session JWTs are fully redacted).
  All vendor patterns carry left-AND-right word boundaries
  (T7 + F1) and explicit upper bounds to prevent catastrophic
  backtracking. Single-pass short-circuit via
  `_ANY_SECRET_PREFIX.search()` first — strings with no
  recognised prefix skip the 14-pattern loop entirely (4th pass).
- **Redact walks tuples / sets / frozensets / bytes / bytearray +
  cycles (G-6/G-7/G-8).** Previously only `Mapping` and `list`;
  tuples leaked secrets, bytes crashed `json.dumps` inside `_emit`
  silently dropping the event, self-referential containers
  triggered swallowed `RecursionError`. Sets sorted by
  canonical-JSON repr for deterministic content-id; bytes decoded
  via `utf-8 errors='replace'`; cycles become the sentinel
  `"<cycle>"`.
- **Settings secret-detection regex broadened (G-24).** Previous
  `/api.?key|secret|token/i` missed webhook URLs (Slack / Discord
  / Teams), routing keys (PagerDuty), DSNs (Sentry), bot tokens,
  bearer credentials, private keys — real operator webhook URLs
  with auth tokens displayed cleartext in the "Other" group.
  Regex now covers all of the above plus `password`/`passwd`/`auth`.

#### Numerics & quant correctness

- **NaN sentinel contract extended uniformly (M6 + G-9).** v1.0.x
  `_equity_to_returns` substituted `0.0` for invalid bars,
  contaminating Sharpe / max-dd / win-rate with synthetic flat
  days. Now `float('nan')`. Extended in fifth-pass to three
  other modules:
  - `regime_detector._equity_to_returns` + `_rolling_std`
    (HMM no longer sees synthetic zeros biasing Viterbi toward
    low-vol "bull");
  - `factor_analyzer._load_returns` (both JSON + CSV paths —
    FF3 / AR(1) no longer treats bad bars as zero-return days
    suppressing alpha and inflating R²);
  - `dynamic_correlation._compute_returns` + `_pearson_r` (rolling
    correlation no longer sees correlated zeros across all assets
    producing spurious cross-asset correlation and a deflated
    `diversification_score`).
  Consumers filter NaN via `_finite_returns()` /
  `_finite_only()` helpers before aggregation; `_pearson_r` does
  pairwise-finite filtering; `run_factor_regression` builds a
  finite-only mask and aborts if <5 finite bars survive.
  Divisor floors tightened from `> 0` to `> 1e-14` per CLAUDE.md
  § 9.3 so IEEE 754 subnormals (5e-324) can't poison results
  (M17 / G-16).
- **Monte Carlo bootstrap pool guards (M7 + G-12).** Filter only
  non-finite (legitimate zero returns preserved for cash-heavy
  strategies); empty pool fails loud (M7); single-unique-value
  pool refused with explicit error (G-12) — previously every
  simulated path was identical → `std=0`, `var_5pct=0`,
  `cvar_5pct=0`, `prob_loss=0` falsely advertising perfect
  strategies.
- **HMM hardening (M18 / T14 / G-10 / G-11).** Std floor aligned
  to `1e-14` and made scale-aware (`max(global_std × 1e-6,
  1e-14)`) so a single outlier can't pull a regime's std to
  the global floor and smear boundaries. Insufficient data
  (T < K×2) now raises `HMMInsufficientDataError` which
  `detect_regimes` catches and falls back to volatility with an
  explicit warning surfaced in `result.warnings`. EM convergence
  switched to relative tolerance (`tol × max(|log_lik|, 1.0)`)
  matching the M9 power-iteration fix — previously absolute
  `1e-4` against `log_lik ≈ -10000` meant EM never converged
  inside `max_iter=100`.
- **Power-iteration relative tolerance (M9).**
  `dynamic_correlation._power_iteration` uses
  `abs(new - old) < tol * max(abs(new), 1.0)` so 1e+6
  eigenvalues converge in `max_iter` and 1e-3 eigenvalues
  don't accept loose 0.1 %-relative changes.
- **Significance testing (H9 / T2 / T3 + 4th-pass tail expose).**
  Permutation p-value uses Phipson-Smyth +1 correction
  `(count_ge + 1) / (n_perm + 1)` (H9 — previously could report
  exact 0 → `is_significant=True`). Default now **two-sided**
  (T3) so short-bias strategies are evaluated against
  `|SR| ≥ |obs|`; both `p_value_one_sided` /
  `p_value_two_sided` exposed plus `p_value_greater` /
  `p_value_less` (4th pass) for pre-registered directional
  tests. DSR `denom_sq` floor raised to `1e-8` (T2 — matches
  practical `sr_hat × sqrt(T)` scale) and `dsr_z` clipped to
  `±6` (4th pass — `Φ(6) ≈ 1 - 9.9e-10` stays
  finite-distinguishable, `Φ(10)` round-trips through
  `json.dumps` as exact `1.0`). Both clips annotated in
  `result.errors` when fired. Independent RNGs (seed 42
  permutation, 43 bootstrap) so reordering can't silently
  change the bootstrap CI.
- **Factor-analyzer near-singular detection (M17 + T12 + F4).**
  `_xtx_inv_diagonal` negative entries no longer silently
  clamped (M17 — was producing `t-stat=inf` →
  `p≈0` → false-positive alpha). Tightened to also reject
  smallest absolute diag < 1e-15 OR max/min ratio > 1e10
  (T12). Fourth-pass added the scale-aware
  `s² × min_diag < 1e-20` check (decimal-scaled inputs
  evaded the existing guards while still producing
  `se ≈ 1e-8` → `t ≈ 1e+6` false-positive). `near_singular`
  surfaced via explicit `result.errors` entry — operator no
  longer sees all-None t-stats with no explanation.
- **AR(1) CAPM-fallback no longer mislabels self-lag as market
  beta (H11).** New `autocorrelation_beta` field;
  `market_beta` stays `None` when FF3 unavailable; loud
  warning + summary text annotation.
- **`_inv_normal` continuous at p=0.5 (H10).** Beasley-Springer-
  Moro evaluated to ≈±1.5e-5 at p=0.5±; now special-cased to
  exact 0.0 within 1e-12. Mattered for DSR at `n_trials=2`.
- **Subnormal-poisoning floors raised to `1e-14`** across
  `dynamic_correlation._std`, `cointegration_analyzer._std`,
  `quant_analytics._sharpe_from_returns`, factor-analyzer
  `ss_tot` (M17), and the walk-forward Sharpe-decay-ratio
  denominator (G-15 — was `1e-10`, admitted
  `oos_sharpe / is_sharpe` → ~1e+8 explosion).
- **DSR z-score clip annotation (4th pass).** When clamp fires,
  `result.errors` carries `"dsr_z clipped to ±6 (raw |z| = ...)"`
  so consumers can distinguish "huge real signal" from "denom
  near floor".
- **Backtest `_PARAM_RNG` deterministic (G-13).** Was
  `random.Random()` (OS-time seed at import). Optuna path used
  `seed=42` but the random-search fallback + `strategy="random"`
  searches produced different `best_params` every run — broke
  retrospective analysis and v1.2.0 rank-stability. Now reads
  `BACKTEST_PARAM_SEED` (default `4242`); `"random"` / `"none"`
  / empty restore legacy non-deterministic behaviour.
- **Parallel real-data fetch hard timeout (G-14).** `fut.result()`
  was called without `timeout=` — yfinance's internal read
  timeout varies by version, so a slowloris endpoint could
  hang the pipeline. Now `result(timeout=90)` (env-configurable
  via `BACKTEST_FETCH_HARD_TIMEOUT_SEC`) with
  `concurrent.futures.TimeoutError` mapped to a synthetic
  `FetchOutcome("", "timeout", ...)` so the cascade continues.
- **FF3 per-chunk timeout via `concurrent.futures` (F5).**
  Third-pass T13 reached into `resp.fp.raw._sock.settimeout(10)`
  but on HTTPS the `raw` attribute is a
  `LengthReadBufferedReader` (not `SocketIO`) → AttributeError
  swallowed → 10 s per-chunk cap effectively dead. Each
  `resp.read(_CHUNK)` now wrapped in
  `ThreadPoolExecutor.submit(...).result(timeout=10.0)` — pure
  Python timing that works across HTTP / HTTPS / asyncio
  transports. Also (H12) per-`read` socket-default timeout +
  60 s wall-clock cap + 64 KB streaming with 10 MB hard
  payload limit. Tearsheet `duration_days` renamed
  `duration_bars` (M8 — semantically correct for intraday
  strategies; legacy properties retained for back-compat).
- **Backtest data cache key drops `today_utc` (M10).** Previous
  `sha1(symbol|period|interval|today)` + 24 h TTL fence-post
  forced cache misses at 00:00 UTC for APAC operators every
  morning. Now keyed solely by `sha1(symbol|period|interval)`;
  expiry driven entirely by mtime + TTL.
- **Backtest staleness probe tz-aware (M4).** End-date parsed
  from data CSV is now stamped with `tzinfo=_UTC` before
  `.date()` subtraction.

#### Ledger atomicity & redaction

- **JSONL writes durable on Windows Ctrl-C (H2).**
  `write_event` / `write_blob` / `prune_stream` follow
  `fh.flush()` with `os.fsync(fh.fileno())`.
- **Cross-process JSONL writes safe on Windows (H3).** Module
  `_file_lock_ctx` no longer falls back to `_NoOpLock` on
  Windows; uses `msvcrt.locking(LK_LOCK, 1)` on a sentinel
  byte at offset 0 with bounded retry on transient
  `EDEADLK`/`EACCES`. `_WindowsLock.__exit__` gives every
  phase its own `try` so unlock always runs (T6); POSIX
  counterpart `_PosixLock.__exit__` widened to catch
  `ValueError` from closed-handle GC interleavings (F7);
  cross-process test now uses `subprocess.Popen([sys.executable,
  "-c", script])` instead of `multiprocessing.spawn` (F8 —
  Python <3.13 spawn-pickle hangs on Windows).
- **Sidecar lock for prune (T5).** Second-pass H8 added a
  cross-process file lock around the prune scan, but the lock
  released BEFORE the temp-file `os.replace` — a concurrent
  writer could append after the scan and have its append
  clobbered. Now per-stream sidecar `.<stream>.jsonl.lock`
  held across the full read → write → replace cycle.
  `write_event` also takes the sidecar so writers are blocked
  while prune is rewriting. Sidecar exists because Windows
  `os.replace` refuses to overwrite a file the same process
  holds open.
- **POSIX `_fsync_dir` after atomic renames (T10).** Otherwise
  a power loss after `replace` could leave file contents on
  disk while the directory still pointed at the old inode →
  file vanishes on reboot.
- **Schema marker is lock-protected, atomic, forward-compatible,
  and BOM-tolerant.** `_init_layout` creates `.schema_version.lock`,
  acquires platform exclusive lock, re-checks under the lock,
  writes via `tempfile.mkstemp` + fsync + `os.replace` (M19).
  T9 made the write forward-compatible: parses content as
  `int(content)` and treats `>= expected` as no-write so a
  v1.2 process won't roll the marker back. G-17 added
  `utf-8-sig` decode tolerance so a Windows Notepad-saved
  marker with BOM no longer triggers ValueError → re-write on
  every startup.
- **Orphan tempfile cleanup on backend init (G-18).**
  `tempfile.mkstemp` in three places leaves `.prune_*.jsonl` /
  `.blob_*.tmp` / `.schema_*.tmp` when the process is SIGKILL'd
  between mkstemp and `os.replace`. Backend now sweeps files
  older than 24 h on init (best-effort, swallows all errors).
- **`_LAST_FETCH_DIAGNOSTICS` thread-local (H5 + T1).** Concurrent
  `prepare_data()` no longer clobber each other's provider
  outcomes. T1 fixed the parallel-fetch worker case where
  worker-thread TLS was invisible to the main-thread call to
  `_build_cascade_diagnostic_lines` — each worker now snapshots
  its TLS dict at return time and the main thread merges them
  back before building the error message.
- **`_init_layout` failure now substitutes `_NullRecorder` (F6).**
  Read-only filesystem / parent is a regular file / EACCES
  previously left the backend instance live with `_root` pointing
  nowhere — every `write_event` silently returned `""` with no
  diagnostic. Now sets `_init_failed=True` + `_closed=True`;
  factory falls back to `_NullRecorder` and logs a loud "ledger
  DISABLED for this process (events will be lost)" warning.
- **Recorder safe across POSIX `os.fork()` (M13).**
  `os.register_at_fork(after_in_child=_reset_recorder_after_fork)`
  replaces inherited `_RECORDER` / `_RECORDER_LOCK` globals in
  the child so pytest-xdist forks get a fresh recorder.
- **Prune is O(1) memory (M12).** Previously `readlines()`
  loaded the whole stream file. Now two-pass byte-scan in 64 KB
  chunks — bounded memory at `MAX_ENTRIES=20 000 × ~500 B/line
  ≈ 10 MB`.
- **`_writes_since_prune` counter lock-protected; `_lock` →
  `RLock` (4th pass).** Concurrent emits could lose increments
  or double-prune; `RLock` cost is identical for uncontended
  paths and prevents self-recording error-path deadlocks.
- **`MAX_ENTRIES_PER_STREAM` clamp_max (T4).** Operator typo
  `2000000000` would have triggered `collections.deque(maxlen=2e9)`
  attempting a 1 TB allocation during prune. Now clamped to
  1 000 000.
- **`_v8_float_repr` ECMA-262-conformant (M3 + T8 + T11 + 4th pass).**
  `schema.canonical_json` uses a custom `_V8FloatJSONEncoder`
  matching V8 `Number.prototype.toString` rules (`1.0` → `1`,
  `1e-7` → `1e-7` not `1e-07`, `-0.0` → `0`). T8 re-implemented
  the full ECMA-262 §6.1.6.1.13 algorithm after the heuristic
  diverged from V8 at 1e-6, 1e16, and integer-valued floats —
  every record containing a float would have produced a
  divergent `content_id` from the future Cloudflare Worker.
  T11 added a graceful fallback when `_make_iterencode` rename
  happens in future CPython. Fourth pass raises `ValueError` on
  non-finite floats instead of silently emitting `"null"` —
  NaN payload no longer hash-collides with `None`.
- **JS↔Python canonical JSON parity test (M3).** 14 edge-case
  fixtures (integer-valued floats, exponent notation, NaN/Inf,
  key ordering, unicode, control char escapes, negative zero)
  pinned against the JS spec frozen in `backends.py` docstring.
- **`record_output_method` propagates `data_source` +
  `data_actual_symbol` (G-20).** Without this, v1.2.0 retrieval
  would have to re-open `backtest_report.json` from disk for
  every ledger row to filter synthetic-data runs. Recorder
  signature accepts the optional fields; section_07 reads
  `backtest_report.json` just-in-time and forwards; also
  mirrored into `signals[]` as `data_source:{value}` for
  one-line retrieval filtering.
- **Backend `LOGGER.warning` rate-limited per `(scope, key)`
  tuple (G-19).** Eight previously-unrate-limited WARNING sites
  on a read-only mount / full disk produced hundreds of warnings
  per minute drowning real diagnostics. New `_warn_once()`
  records first occurrence; capped at 100 entries to bound its
  own memory.
- **Manifest timestamps unified (M20).** `export.py` uses
  `schema.utc_now_iso()` (ms precision + `Z` suffix) so
  manifest and event timestamps share format.

#### Per-run flag plumbing — **release blocker fixed in G-1**

- **`_STORE_TRUE_FLAG_TO_ENV` now writes env names the core
  pipeline reads (G-1).** Fourth-pass F-9 mapped
  `cache → CRUCIBLE_CACHE`, `strict_json → CRUCIBLE_STRICT_JSON`,
  `cost_trace → CRUCIBLE_COST_TRACE` — but
  `section_07_selfcheck_output_main.py:323-325` (and mirrors in
  sections 02 / 05 / 06) reads the **un-prefixed legacy names**
  (`LOCAL_CACHE`, `STRICT_JSON`, `COST_TRACE`) via
  `_env.env_bool()`. So an operator unchecking `strict_json` in
  the idea/path panel got `CRUCIBLE_STRICT_JSON=0` in the
  subprocess while the pipeline read `STRICT_JSON` (still `1`
  from `.env`). Tests passed only because they verified the
  mapping was internally self-consistent — never that the RHS
  keys matched what the pipeline actually reads. A textbook
  "producer is tested, consumer wiring is not" trap. Mapping
  corrected to bare legacy names + new
  `test_mapping_rhs_keys_match_actual_pipeline_reads`
  structurally scans the section_* read sites for
  `env_bool("NAME", ...)` calls and asserts every mapping RHS
  appears as one. CLAUDE.md § 9.6 codifies the
  producer→consumer testing pattern.

#### `.env.example` parser

- **Group-header heuristic tightened (G-21).** Old rule (any
  1-6 token comment = group header) caused lines like
  `# Synthetic-data seed used when BACKTEST_REQUIRE_REAL_DATA=0
  (plumbing` — exactly 6 tokens — to BECOME the group name
  for adjacent env keys (`BACKTEST_SYNTHETIC_SEED`, etc.).
  CLAUDE.md § 1 already documented the trap; the audit found
  three live hijacks. New heuristic accepts 1-3 token
  comments as headers OR requires explicit divider syntax
  (`===`, all-caps, ≥4 tokens with surrounding
  `=`/`─`/`━`/`*`). Two existing description sentences
  lengthened to ≥7 tokens.
- **Seven new backtest env keys uncommented (M1).** `BACKTEST_
  MIN_REAL_DATA_ROWS`, `_CACHE_TTL_HOURS`, `_CACHE_DIR`,
  `_MAX_STALENESS_DAYS`, `_SYNTHETIC_SEED`, `_PREPARE_DATA_ONLY`,
  `_PARALLEL_FETCH` — render in Settings with defaults
  instead of empty fields.

#### WebUI polish

- **`saveSettings()` only POSTs dirty values (M2).**
  Baseline snapshot via `_snapshotSettingsBaseline()`
  immediately after `renderSettings()`; Save computes which
  keys differ. Previously every input was serialised including
  freshly-rendered empty fields for commented-out keys, silently
  persisting `KEY=""` and nuking shell-export overrides. Toast
  reports the dirty-key count; "No changes" short-circuit.
- **Empty-file sha1 hash never cached (G-22).** Editor
  truncate-then-write could leave `app.js` 0 bytes momentarily;
  a Flask request hitting `index()` in that window would
  permanently cache `sha1(b"")[:10]="da39a3ee5e"`, defeating
  cache-busting until Flask restart. Zero-byte reads now return
  ephemeral sentinel `"x"` without caching.
- **JS error path uses centralised `_escapeHtml` (G-23).**
  Ad-hoc `replace(/[<>&]/g,'')` missed `"` and `'`; not
  exploitable in text-node context but drift from policy.

### Changed

- **`SignificanceTestResult` gained 5 new fields**:
  `p_value_one_sided` + `p_value_two_sided` + `alternative` (T3) +
  `p_value_greater` + `p_value_less` (4th pass). All populated
  by `to_dict()`. Downstream consumers using strict-schema
  `additionalProperties: false` validation need updates;
  `.get(key)` consumers unaffected.
- **`DrawdownPeriod.duration_bars` / `recovery_bars`** are now the
  canonical fields (M8 — `_days` retained as property aliases
  and in `to_dict()` for v1.0.x consumers).
- **`PrepareDataResult` NamedTuple** replaces the 3-tuple
  return of `prepare_data` (breaking for direct API consumers;
  legacy unpacking documented in `__doc__`).
- **`BacktestDataIntegrityError` message** now splices
  per-provider `FetchOutcome` diagnostics into its body —
  operator sees `yfinance: rate_limit — HTTP 429` rather than
  the generic `no data returned`.
- **`.gitignore`** adds `.crucible_insights/`,
  `.crucible_insights.tar.gz`, `CLAUDE.md`.

### Validation

- pytest: **2 451 passed, 1 skipped** (final, fifth-pass).
  Baseline evolution: 2 229 (v1.1.0 inception) → 2 269
  (backtest-runner 15-pt audit) → 2 281 (2nd-pass 34 fixes)
  → 2 380 (3rd-pass 20 fixes + 99 new tests) → 2 407
  (4th-pass 30 fixes + 27 new tests) → **2 451** (5th-pass
  24 fixes + 44 new tests).
- New test files: `test_run_insights/` (10 files covering
  canonicalisation, signals extraction, local backend with
  schema-marker race + clamp_max pin, recorder, redaction
  field-name + end-to-end + 13-regex-pattern bank,
  concurrency with cross-process spawn variant, emit-points
  four-stream swallow coverage, JS canonical parity, V8 float
  repr with 24 ECMA-262 boundaries); `test_webui_security.py`
  (MAX_CONTENT_LENGTH / X-Requested-With / SSRF / SRI);
  `test_backtest_require_real_data_default.py` (env-bool
  whitelist pin); `test_quant_v1_1_0_regressions.py` (NaN
  sentinel, power-iter, AR(1), DSR clip, permutation
  two-sided); `test_store_true_only_per_run_disable.py` (G-1
  release-blocker pin with the structural
  producer→consumer wiring test);
  `test_v1_1_0_fifth_pass_regressions.py` (29 G-N regression
  pins covering multicast SSRF, redirect SSRF, `sk-ant-oat`
  redact, HMM insufficient-data, Monte Carlo unique-value,
  `_PARAM_RNG` seed, ledger orphan cleanup, `.env.example`
  parser strengthening).
- `crucible/smoke_test.py`: 5/5 OK;
  `run_crucible.py --self-check`: OK.
- Cross-process lock test passes on Windows + Linux,
  multiprocessing spawn/fork + subprocess.Popen variants.
- `_safe_urlopen` redirect-rejection verified live against a
  local stub HTTPS server.

### Compatibility

- Python ≥ 3.10 (unchanged).
- Drop-in replacement for v1.0.5 — `pip install -U` is safe.
- Behavioural changes operators should know about:
  - `BACKTEST_REQUIRE_REAL_DATA=1` is **default on**. Set to
    `0` to opt back into synthetic-GBM fallback (and accept
    the loud `report.warnings` annotation).
  - `_STORE_TRUE_FLAG_TO_ENV` writes `LOCAL_CACHE` /
    `STRICT_JSON` / `COST_TRACE` (not `CRUCIBLE_*`) — the
    fourth-pass mapping was wrong and silently no-op'd.
  - Permutation tests default to **two-sided**; consumers
    relying on the prior one-sided semantics should read
    `p_value_one_sided` explicitly.
  - `PrepareDataResult` NamedTuple breaks direct 3-tuple
    unpacking of `prepare_data`'s return.
  - 5 new fields on `SignificanceTestResult` (see Changed).
  - `DrawdownPeriod.duration_bars` is canonical;
    `duration_days` is a property alias.

---

## [v1.0.5] — 2026-05-09

### Fixed
- **Quant runtime validation was a no-op** — detector only saw FastAPI
  apps so Quant slipped past `py_compile`. New `section_06` track:
  import smoke + AST cross-ref (`cross_reference_check.py`,
  **X001-X004** + escape **W001-W003**) + domain lint (`quant_lint.py`,
  **Q001-Q004**, `# noqa: Q00x`) + GBM-OHLCV dry-run (`quant_smoke.py`,
  **Q010-Q015**) + ccxt-stubbed live_trader smoke (**Q020-Q024**).
- **Q001 closes all look-ahead escapes** — direct (`row['open']`,
  `.open`), wrapped (`float`/`int`/`Decimal`/`round`), function-wrapped
  (`compute_entry(row)` with param aliasing), column-iter literal
  (`for col in ['open']`), positional on row aliases (`row.values[0]`,
  MEDIUM), async/closure. Vars: `entry_price`/`fill_price`/etc.
- **Q024 live_trader behavioural SL** — price ramp 100→64 over 10 ticks
  with `_opened`/`_closed` lists + wrappers on `close_position`/
  `_close`/`exit_pos`/`manage_pos`/`check_stop`. `stop_loss` source +
  open + zero closes → high. Catches the silent-SL class (no exception,
  just always-False time gate / off-by-one) that Q020-Q023 miss.
- **W001-W003 escape warnings** — `Trade(**signal_dict)` → W001 (med);
  `getattr(cfg, "LIT"[, default])` → W002 (high without default, med
  with); `cfg['LIT']` → W003 (high). Closes the LLM's three favourite
  shortcuts when X001/X002 fires in the fix loop.
- **Schema-first Quant codegen** (`section_05`) — manifest hoists
  schema files to batch 0; prompt gets AST-extracted "Approved schema
  signatures". Shape-based detection (`@dataclass`/`BaseModel`/
  `TypedDict`/`NamedTuple`/Enum/`@attr.s`/canonical names) survives
  merging into `models.py` or renaming. Trade-kwargs mismatch eliminated
  at source.
- **Dirty-data fixture** (`CRUCIBLE_QUANT_DRYRUN_DIRTY_DATA=1`) —
  NaN/zero volume + partial-NaN OHLC + 3-day gap exercise data-cleaning
  branches.
- **Pre-codegen gate floor** (`CRUCIBLE_PRE_CODEGEN_MIN_SCORE`, default
  60) — forces `ready_for_codegen=False` below floor unless
  validation-only scope.
- **Quality-loop stagnation: structured + strict** —
  `ReviewReport.failure_type` is Pydantic-validated against an explicit
  allowed-set (typo → ValueError at write); section_07 substring
  fallback removed; `save_project_output` promotes `quality_passed` +
  `quality_loop_failure_type` to `run_meta.json`. README renders a
  `quality_pass=False` blockquote (EN + zh-TW). Migration script
  `scripts/migrate_review_failure_type.py` back-fills legacy runs.
- **Quant correctness checklist** (`section_04`) — one prompt bullet
  codifies entry@t+1-open, `range(1, N+1)` stop loops, flat frozen
  Trade dataclass, env-bool whitelist, `not (x > 1e-14)` denominators,
  cross-file name existence, size-dependent dynamic slippage.
- **Production-scope `tests/` enforcement** — opt-in via
  `CRUCIBLE_QUANT_REQUIRE_TESTS=1` or `codegen_scope='production'`;
  missing `tests/*.py` becomes high.
- **Mode-specific validation matrix** (`mode_validation_matrix.py`) —
  single source of truth (active/opt-in/deferred). Universal cross-ref
  (X001-X004 + W001-W003) now runs on all modes (default ON, opt-out
  `CRUCIBLE_UNIVERSAL_CROSSREF=0`) plus per-mode lint: SaaS **H001**
  (web framework imported but undeclared); Agent **A001/A002**
  (role/goal/backstory missing; Tool without description); Scientist
  **S001/S002** (no seed; no requirements manifest). Rendered table in
  `ARCHITECTURE.md` makes deferred defences visible debt.
- **Pipeline integration regression suite** — 9 fixture bundles
  (R01-R09 covering X001-X003, Q001-Q004, W001-W002) with hard CI floor
  ≥ 7/9 caught; guards silent pipeline short-circuits.
- **WebUI surfaces structured quality outcome** — `_extract_run_row`
  reads `run_meta.quality_passed` (canonical, `review_report.json`
  fallback for legacy runs) and strictly-validated
  `quality_loop_failure_type`; SQLite index gains two idempotently-
  migrated columns. Dashboard tri-state badge (✓ Passed / ⚠ Gave up /
  ✗ Failed); run-detail modal adds Quality Status, Review Summary,
  severity-grouped Issues (high → med → low, cap 20 + overflow tail).
  Substring matching `QUALITY_LOOP_GAVE_UP` against summary text
  forbidden by regression test (mirrors backend strict validation).
- **Agent-flow ↔ backend SSE alignment** — `project_fix_kickoff_*`
  (the entire quality-loop re-codegen area) was unmapped in `evMap`,
  leaving the panel silent for 30-60s × N rounds. Added: `start` →
  `code_gen` active; `done` → reuses `codegen_phase_done`; `failed` →
  `code_gen` error. Also wired `librarian_kickoff_failed` and
  `analysis_kickoff_failed` (new `analysis_phase_error` handler errors
  stages 5-7 in one shot). Shape tests pin exact mapping rows.
- **Dashboard "Total Cost" was always $0** — `save_project_output`
  never wrote `total_cost_usd` / `total_cost` / `total_tokens` to
  `run_meta.json`, so `_extract_run_row` read `None` → SQLite NULL →
  dashboard summed to $0 even when runs spent real money. Section_07
  now promotes the cost ledger from `run_snapshot.cost_summary`
  (frozen authoritative state) or the live cost accountant; `setdefault`
  preserves caller overrides. Webui extraction prefers `total_cost_usd`
  over the legacy units field, falls back to `run_snapshot.json`
  for legacy saved_projects/, and preserves full IEEE 754 precision
  end-to-end (no rounding before persistence — a per-call OpenRouter
  cost at the 6th decimal survives 100× summation intact). Dashboard
  rounds to **6 decimals** (was 5); all four frontend cost formatters
  (`toFixed(4)`/`toFixed(5)`) raised to `toFixed(6)` to match
  cost_tracker precision. `scripts/migrate_run_meta_cost.py`
  back-fills cost into legacy `run_meta.json` from `run_snapshot.json`.

### Validation
- pytest: 2 149 passed, 1 skipped (`-m "not slow and not network"`);
  201 new v1.0.5 tests, 30 webui frontend/backend alignment tests,
  12 cost-surfacing tests covering all three layers (section_07
  promotion / webui extraction / display precision).
- `crucible/smoke_test.py`: 5/5 OK; `run_crucible.py --self-check`: OK.

---

## [v1.0.4] — 2026-05-08

### Fixed
- **Reasoning-model `<think>` blocks corrupted every structured-output
  extractor**: DeepSeek-V3/V4, GLM-5.1, Qwen-3.5 and o1-class models emit
  chain-of-thought inside `<think>…</think>` (and `<thinking>` /
  `<reasoning>` / `<reflection>` / `<scratchpad>` aliases) ahead of the
  answer. Any brace- or fence-shape token in the reasoning text was
  captured as the first outermost JSON object / longest fenced block, so
  the real answer was discarded. The most visible symptom was Stage 0
  Direction Debate falling back to `[Warn] Direction debate could not
  produce a valid decision`; the same defect silently truncated every
  adversarial-review run, every auto-remediation patch, every generated
  test file, and every multi-language translation. New shared helper
  `crucible.output_validation.strip_reasoning_blocks` now runs ahead of
  every structured-output scan: `_extract_first_json_object` /
  `_extract_first_json_array`, `extract_json`, the section_06 API-version
  regex, `independent_validator._extract_json_from_response`,
  `backtest_runner._extract_code_block`, and the four feature-module
  `_strip_code_fences` helpers. 15 new regression tests.
- **Direction Debate force-none gate had no diagnostic surface**
  (`crucible/modules/section_02_research_and_llm.py`):
  `_should_force_direction_none` returned silently with no print and no
  dump, so users had no way to tell which gate fired. The path now prints
  `[Warn] … gate fired (reason=… citations=… grounded_claims=…
  weak_directions=…)` and writes a JSON debug dump under
  `saved_projects/direction_debug/` tagged `note=force_none:<reason>`.
- **`run_direction_debate` final exit summary**
  (`crucible/modules/section_02_research_and_llm.py`): after all
  refinement iterations exhaust, the loop now prints one summary line
  with the last `gap_info` (or points at the latest dump file for
  JSON-parse failures) before returning None.
- **Runner [Warn] message points at diagnostics**
  (`crucible/modules/section_07_selfcheck_output_main.py`): the generic
  fallback message now references the preceding `[Warn]` line(s) and
  `saved_projects/direction_debug/`.

### Validation
- pytest: 1 941 passed, 1 skipped under `-m "not slow and not network"`.
- `crucible/smoke_test.py`: 5/5 OK.
- `run_crucible.py --self-check`: OK.

---

## [v1.0.3] — 2026-05-08

### Changed
- **Centralised env-var parsing** (new `crucible/_env.py`): the
  `_env_int` / `_env_float` / `_env_bool` / `_env_str` / `_env_optional_*`
  helpers duplicated across 26 production files now delegate to one
  canonical implementation. Per-file shims keep call sites unchanged;
  central helpers expose `clamp_min` / `clamp_max` / `finite_only` and
  sentinel-aware (`unlimited`, `inf`, `none`) behaviour as flags. Backed
  by 73 new tests in `tests/test_env_helpers.py`.
- **CI workflow split into `lint` / `test` / `security` jobs**
  (`.github/workflows/ci.yml`): lint and security run as independent
  ubuntu-only jobs installing only `ruff`/`mypy` and `bandit`/`pip-audit`
  respectively. The test job keeps the 2×3 OS-by-Python matrix but
  invokes pytest with `-n auto --durations=20 -m "not slow and not
  network"` (suite drops from ~200 s → ~25 s). Lint/mypy scope is read
  from `pyproject.toml`.
- **mypy coverage expanded from 25 → 74 source files**
  (`pyproject.toml [tool.mypy]`): fixed previously-masked type errors
  in `runtime_logging.py`, `hooks.py`, `features/checkpoint.py`,
  `features/portfolio_backtest.py`, `convergence_guard.py`, and
  `web_research/http_clients.py`.
- **Silent `except Exception: pass` swallows now logged**
  (`webui/app.py` × 9, `run_crucible_enhanced.py` × 3): each bare-pass
  swallow records its traceback at `LOGGER.debug` level under the new
  `crucible.webui` / `crucible.runner` loggers, surfaced when
  `CRUCIBLE_LOG_LEVEL=DEBUG`.
- **`output_validation.extract_json` refactored**
  (`crucible/output_validation.py`): the 100-line three-strategy chain
  becomes four named helpers (`_coerce_to_str`, `_try_direct_json`,
  `_try_markdown_block`, `_scan_outermost_json`) plus a 25-line
  orchestrator.
- **Test marker discipline + parallel runner**
  (`tests/conftest.py`, `pyproject.toml`): registered `slow`,
  `network`, `integration` markers; tagged the 7 tests >5 s wall-clock
  as `slow`. Added `pytest-xdist` + `pytest-cov` to
  `requirements-dev.txt` with a `[tool.coverage.run]` config.
- **WebUI SQLite connection cached per worker thread**
  (`webui/app.py`): one `sqlite3.connect` per thread via
  `threading.local()` instead of re-opening on every request and
  re-running schema DDL. Bootstrap connection reaped at interpreter
  exit via `atexit`; `_reset_db_threadlocal()` test helper exposed.
- **WebUI frontend assets split out of the template**
  (`webui/templates/index.html` → `webui/static/css/app.css` +
  `webui/static/js/app.js`): the inline `<style>` (1 092 lines) and
  `<script>` (3 439 lines) blocks become sidecar files served by
  Flask's default `/static/` route. The HTML shell drops to 555 lines,
  browsers cache CSS/JS independently of the template, and
  `Content-Security-Policy: script-src 'self'` becomes achievable. The
  lone Jinja expression in the original inline script
  (`{{ webui_url | tojson }}`) is bridged through `window.WEBUI_URL`:
  a 1-line inline `<script>` in `index.html` sets the global before
  `app.js` loads, and `app.js` reads `window.WEBUI_URL ||
  window.location.host`. New `tests/test_webui_frontend_integration.py`
  (28 tests) pins the bridge ordering, rejects Jinja artifacts in
  served assets, asserts every canonical agent-flow SSE event matches
  an `evMap` regex, checks every state-handler key has a matching
  branch, proves every inline `onclick` symbol resolves to a top-level
  function, and verifies the SSE `EventSource` wiring — all against
  the served assets via Flask's `test_client()`.
- **Internal version-tag scrubbing** (~110 occurrences across 25 files):
  removed pre-1.0 audit-batch references (`v16.x.y` / `v16.0.x`
  prefixes in inline comments, `Regression (v16.X.Y):` test docstring
  prefixes, `# ── v16.X feature_name ──` section dividers, the
  `Generated by Crucible v16.9` HTML report footer, and the
  `OLD_version/crucible_v14.py` provenance line in
  `crucible/SECTION_MANIFEST.md` plus the auto-generated section-module
  headers).  CLI flag `--v169-features` and env var `V169_FEATURES`
  remain as public-API names (renaming would break downstream user
  scripts).  Comments and docstrings only — no behaviour change.

### Validation
- pytest: 1 926 passed, 1 skipped under `-m "not slow and not network"`.
- pytest `-m slow`: 7 passed.
- `crucible/smoke_test.py`: 5/5 OK.
- `run_crucible.py --self-check`: OK.
- `python -m mypy`: 0 errors across 74 files.
- `python -m ruff check`: clean.

---

## [v1.0.2] — 2026-05-06

### Fixed
- **WebUI long-session freeze** (`webui/templates/index.html`): four independent
  leaks that progressively froze the WebUI on multi-hour runs.
  - Terminal DOM trimmed symmetrically with `sess.lines` (cap 5 000); previously
    DOM nodes accumulated forever while the array was capped.
  - `visibilitychange` handler reopens any `EventSource` killed by background-tab
    throttling (Chrome Memory Saver, OS sleep) on tab refocus.
  - `pagehide` handler closes all `EventSource` connections on tab close so
    server-side SSE generators release immediately instead of waiting 30 min.
  - `showPage()` clears `_ABState.pollTimer` when navigating away from the
    A/B-test page; `_initABTest()` re-arms it on return.

### Validation
- pytest: 1825 passed, 1 skipped.
- `crucible/smoke_test.py`: 5/5 OK.
- `run_crucible.py --self-check`: OK.

---

## [v1.0.1] — 2026-05-04

### Changed
- **Test-suite naming hygiene**: Removed pre-1.0 internal version tags from test
  filenames and docstrings so the test layout reflects what each module covers,
  not which audit batch produced it. Test discovery, collection rules, and
  assertion logic are unchanged — only filenames and human-readable comments
  were touched. Renames (tracked via `git mv` to preserve history):
  - `tests/test_v16_9_68_audit_fixes.py` → `tests/test_security_and_safety_guards.py`
  - `tests/test_v16_9_69_audit_fixes.py` → `tests/test_subprocess_and_runtime_guards.py`
  - `tests/test_v16_9_71_cjk_grounding.py` → `tests/test_cjk_grounding.py`
  - `tests/test_v16_9_72_audit_fixes.py` → `tests/test_research_synthesizer_guards.py`
  - `tests/test_v16_9_73_audit_fixes.py` → `tests/test_codegen_fix_loop_and_dotenv.py`
  - `tests/test_v16_9_74_env_hot_reload.py` → `tests/test_env_hot_reload.py`
- **Pre-1.0 version tags scrubbed** from incidental docstrings/comments in
  `tests/test_http_clients.py`, `tests/test_crucible_runtime.py`, and
  `tests/test_resilience.py`. Production source files retain their inline
  historical annotations untouched (no behaviour change).

### Validation
- Full pytest suite: 1821 passed, 5 skipped.
- `crucible/smoke_test.py`: all 5 checks pass.
- `run_crucible.py --self-check`: OK.

---

## [v1.0.0] — 2026-05-04

### Added
- First public release. See README and feature documentation for the full
  capability surface.

---

## [v0.9.0] — 2026-05-01

### Fixed
- **WebUI hot-reload**: `POST /api/env` now mutates `os.environ` in-place so saved settings take effect immediately without a process restart.

---

## [v0.8.6] — 2026-04

### Fixed
- **Direction Debate (CJK)**: Direction Debate now reliably produces a valid decision on Traditional Chinese runs; resolved `[Warn] Direction debate could not produce a valid decision` regression.
- **WebUI agent-flow**: `research_synthesizer` node now lights up correctly; fixed `format_checker` false `self_check` activation.
- **Quant backtest LLM-fix loop**: Added syntax gate to prevent malformed patches from entering the auto-fix retry cycle.
- **Pearson r**: NaN clamp applied after correlation computation to prevent invalid values propagating downstream.
- **`.env` DEBUG propagation**: `DEBUG` flag now correctly forwarded to sub-processes spawned by the enhanced runner.
- **Parallel log audit**: Fixed pydantic checkpoint warning, DEBUG-logger stdout flood and interleaving, force-none second branch, `provider_errors` → `key_risks` field contamination, GitHub repo-search token guard.

---

## [v0.8.5] — 2026-03 / 2026-04

### Fixed
- **CodeGen token exhaustion**: Raised token cap to 65 536; added supplement retry when primary generation is truncated. Reduced `CODEGEN_BATCH_SIZE` 3 → 2 to prevent truncation mid-batch.
- **CodeBundle formatter starvation**: Formatter LLM cap raised to `CODEGEN_MAX_TOKENS`; formatter also capped at 8 192 tokens maximum.
- **Never-terminate codegen**: Convergence guard now fires correctly when the codegen loop stalls; project-wide subnormal-divisor sweep applied.
- **CJK / fullwidth-punctuation sanitisation**: `_sanitize_code_bundle` now deterministically repairs fullwidth punctuation in generated Python source.
- **Double/triple-escaped LLM output**: `_unescape_llm_code_content` uses iterative unescaping to handle layered escape sequences from some providers.
- **Web search hardening**: Stripped `site:` qualifiers and truncated oversized queries; `grep.app` CJK skip; DDG CAPTCHA filter; paperswithcode truncation guard.
- **ResearchContext extraction**: Tolerates GLM-5.1 and other non-standard format mismatches without crashing.
- **WebUI SSE disconnect**: Client disconnect no longer kills the subprocess; run continues and results remain retrievable.
- **Reasoning-model empty response**: Empty LLM responses from reasoning models now classified as transient (retried) rather than fatal.
- **HITL SSE reconnect**: Human-in-the-loop SSE stream restores correctly after client reconnect.
- **Subnormal-divisor sweep**: All division paths across infrastructure modules guarded with `not (x > 1e-14)` pattern.
- **Fail-closed defaults**: Paper-mode and auth-manager now default to the safe/closed state on configuration parse failure.
- **env-bool whitelist**: All `os.environ` boolean reads switched to explicit `{"1","true","yes","on"}` whitelist; ambiguous values return the default rather than truthy.
- **`BaseException` narrowing**: Overly broad `except BaseException` replaced with specific exception types across worker threads.
- **Cost tracker O(n²) → O(1)**: Stage-cost accumulation loop refactored to constant-time append.

---

## [v0.8.4] — 2026-03

### Fixed
- **Agent-flow visibility**: `research_synthesizer` and all crew-agent nodes now light up in real time on the WebUI flow panel.
- **Research phase hardening**: Belt-and-braces retry on research swarm lane failures; arxiv XML parse guard added.
- **Librarian hardening**: Per-host circuit breakers, retry classification improvements, `grep.app` CJK skip.
- **HTTP 202 bot-detection**: `safe_http_text` now treats HTTP 202 responses from known bot-detection gateways as errors.
- **GitHub search auth guard**: `search/code` endpoint skipped when no `GITHUB_TOKEN` is configured (avoids guaranteed 401).
- **Analyst terminal log visibility**: Analyst agent output now visible in terminal during live runs.
- **`format_checker` field mapping**: Fixed field mapping regression that caused `format_checker` to silently drop analyst findings.
- **Reformat fallback**: `_run_schema_reformatter` and all cascading callers now propagate `OperationCancelledError` correctly.

---

## [v0.8.3] — 2026-02 / 2026-03

### Fixed
- **Quant analytics correctness**: Fixes across walk-forward validation, signal half-life, OLS t-stat, Sharpe subnormal guard, CAGR annualisation, Calmar formula, spread P&L denominator.
- **Telemetry deadlock**: Background telemetry thread no longer deadlocks when the main process is under high load.
- **Stale-warn flood**: `StaleLoopWarning` flood suppressed; only fires once per convergence guard context.
- **Thread-safety sweep**: DCL (double-checked locking) races fixed in `run_registry`, `bootstrap`, `auth_manager`; ABBA deadlock in hook registry resolved.
- **Atomic writes**: All file-write paths switched to `tmp → rename` atomic pattern to prevent partial writes on crash.
- **Path traversal**: `_resolve_entrypoint_path` input sanitised; SSRF redirect bypass blocked.
- **`OperationCancelledError` propagation**: Cancellation correctly propagates through all stage boundaries; cancelled runs no longer recorded as pipeline failures.
- **Checkpoint state precedence**: COMPLETED state from prior checkpoints correctly overrides in-progress state on resume.

---

## [v0.8.2] — 2026-01 / 2026-02

### Fixed
- **Infrastructure hardening**: Comprehensive multi-pass audit of all infrastructure modules covering thread-safety, near-zero / NaN / Inf guards, atomic writes, env-var safety, div-zero guards, circuit breaker races, and iterable safety.
- **HTTP Retry**: `@with_http_retry` falsy-zero edge case fixed; HTTP 408 (Request Timeout) added to retryable set.
- **Checkpoint atomicity**: Checkpoint state transitions are now atomic; partial state files can no longer be observed by concurrent readers.
- **Signal half-life**: Signal decay computation corrected for edge cases at boundary days.
- **XSS / HTML injection**: All user-supplied strings rendered in HTML reports are now properly escaped.
- **Bool-int coercion**: `bool` subtype of `int` no longer passes numeric-type guards accidentally.
- **DSR (Dynamic Sharpe Ratio)**: Reverted unstable DSR estimator to a validated reference implementation.

---

## [v0.8.1] — 2025-12 / 2026-01

### Fixed
- Comprehensive initial audit of v0.8.0 feature suite: formula errors, double-counted metrics, thread-safety, CSV column search, mutable ContextVar defaults, sanitization bypass, class name collisions, data loss in token tracking.

---

## [v0.8.0] — 2025-12

### Added
- **Quant Analytics Suite** — 25 new post-processing modules:
  - `walk_forward_validator.py` — Walk-forward cross-validation for quant strategies
  - `signal_analyzer.py` — Signal decay and IC analysis
  - `regime_detector.py` — Market regime detection (volatility / trend / HMM)
  - `monte_carlo.py` — Monte Carlo simulation and stress testing
  - `factor_analyzer.py` — CAPM / Fama-French factor exposure regression
  - `transaction_cost_model.py` — Transaction cost sensitivity analysis
  - `tearsheet_generator.py` — Strategy tearsheet (Markdown + HTML)
  - `cointegration_analyzer.py` — Cointegrated pair-trading analysis
  - `dynamic_correlation.py` — Rolling correlation matrix + PCA decomposition
  - `code_lockfile_generator.py` — `pyproject.toml` + pinned `requirements.txt` generation
  - `citation_verifier.py` — Evidence citation grounding and URL verification
  - `chat_bot.py` — Interactive in-run Q&A chatbot
  - `config_wizard.py` — Guided configuration setup
  - `grafana_dashboard.py` — Grafana dashboard JSON export
  - `global_knowledge_base.py` — Cross-run knowledge accumulation
  - `few_shot_injector.py` — Few-shot example injection into prompts
  - `alt_data_connectors.py` — Alternative data source connectors
  - `celery_worker.py` — Celery async task worker integration
  - `auth_manager.py` — Pluggable authentication manager
  - `report_annotations.py` — Structured annotation overlay for HTML reports
  - `notion_export.py` — Notion page export integration
  - `mlflow_sink.py` — MLflow experiment tracking sink
  - `prometheus_exporter.py` — Prometheus metrics endpoint
  - `julia_codegen.py` — Julia language code generation target
  - `lockfile_gen_runner.py` — Lockfile generation runner
- **WebUI**: Feature Bundle checkbox grid replaced free-text input for analytics flags.
- **Interactive HTML reports**: Quant analytics results exported as self-contained interactive HTML.

---

## [v0.7.0] — 2025-11

### Added
- **Enhanced runner** (`run_crucible_enhanced.py`) with full post-processing pipeline:
  - Security scan (bandit + built-in regex rules)
  - Deployment artifact generation (Dockerfile, docker-compose, K8s manifests, Helm chart)
  - Automated backtest runner with real market data (yfinance / Binance / CCXT)
  - Independent validation agent (adversarial LLM review + subprocess test execution)
  - Auto-remediation loop (LLM patch → syntax check → re-scan, up to N rounds)
  - Dependency audit (pip-audit)
  - HTML report aggregator
  - Run registry (SQLite-backed cross-run query)
  - Notification webhooks (Slack / Discord / generic HTTP)
  - A/B test runner for comparing pipeline variants
  - Watch mode (file-change trigger with debounce)
  - Batch mode (multi-project parallel execution)
  - Post-analysis chat (interactive Q&A grounded on run artifacts)

---

## [v0.6.0] — 2025-10

### Added
- **WebUI** — Flask-based single-page application:
  - Idea Mode and Project Path pages with full flag selector panels
  - Real-time SSE streaming of pipeline output
  - Dashboard with cost trends, quality distribution, and stage radar chart
  - Leaderboard for backtest strategy rankings
  - Compare Runs and A/B Test pages
  - Settings page with live API key validation and `.env` editing
  - Human-in-the-loop (HITL) approval flow

---

## [v0.5.0] — 2025-09

### Added
- **Direction Debate stage** (Stage 2):
  - Direction Proposer generates 7 mutually exclusive strategic directions
  - Evidence Auditor scores evidence quality per direction
  - Multi-axis Comparator ranks directions across 6 dimensions
  - Direction Judge selects the winner with go-conditions and kill-criteria

---

## [v0.4.0] — 2025-08

### Added
- **Research Swarm** (Stage 1): three parallel research lanes (Market, Technical, Competitor) with independent evidence extraction rules.
- **Research Synthesizer**: cross-validates findings across lanes; unsupported claims moved to `unknowns` or flagged as `hallucination_flags`.
- **Librarian** (Stage 0): web search agent with two-layer caching (per-query + per-ResearchContext).

---

## [v0.3.0] — 2025-07

### Added
- **Analysis Crew** (Stage 3): five specialist analysts (Research, Risk, Ops, Biz, Critic) running in parallel.
- **Gate Controller**: decides `proceed`, `targeted analyst rerun`, or `kill` based on evidence quality.
- **Format Checker**: assembles the final `AnalysisReport` without adding new information.

---

## [v0.2.0] — 2025-06

### Added
- **CodeGen + Quality Loop** (Stage 4): multi-file code generation with dependency graph, `py_compile` + smoke validation, LLM-backed quality loop, and Review & Fix pass.
- **Auto-optimize**: `codegen_critic` generate → critique → refine loop with plateau detection and budget guard.
- **Output scopes**: `mvp` / `full` / `production` controlling the completeness of generated code.

---

## [v0.1.0] — 2025-05

### Added
- Initial pipeline prototype: single-agent research → single-agent codegen flow.
- Basic Pydantic output models (`AnalysisReport`, `CodeBundle`).
- CLI entry point (`run_crucible.py`) with `--dry-run` and `--self-check`.
- Support for OpenRouter as the first LLM provider.
