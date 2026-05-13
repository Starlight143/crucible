# Changelog

All notable changes to this project are documented in this file.
Versioning follows [Semantic Versioning](https://semver.org/). The first public release was **v1.0.0**.

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
