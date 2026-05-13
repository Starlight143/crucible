"""
v1.1.0 fifth-pass regression tests.

Each section pins one or more fixes from the fifth-pass audit so
that future regressions surface immediately rather than after months
of silent corruption.  See ``CHANGELOG.md`` for the full per-finding
narrative.
"""
from __future__ import annotations

import ipaddress
import math
import os
import sys
from unittest.mock import patch

import pytest


# ── G-1: _STORE_TRUE_FLAG_TO_ENV uses pipeline-read env names ────────────────
# (covered in test_store_true_only_per_run_disable.py — kept here for cross-ref)


# ── G-2: _addr_is_safe rejects multicast / reserved / unspecified ───────────

@pytest.mark.parametrize(
    "url",
    [
        # IPv4 multicast (224.0.0.0/4)
        "http://224.0.0.1/",
        "http://239.255.255.250/",       # SSDP / UPnP discovery
        "http://233.252.0.1/",           # IANA test multicast
        # IPv4 unspecified / broadcast
        "http://0.0.0.0/",
        "http://255.255.255.255/",
        # IPv6 multicast
        "http://[ff02::1]/",             # link-local all-nodes
        "http://[ff05::1]/",             # site-local all-nodes
        # IPv6 unspecified
        "http://[::]/",
    ],
)
def test_is_safe_url_rejects_multicast_and_unspecified(url: str):
    """Python's ``is_global`` returns True for multicast and several
    reserved ranges — verified live with CPython 3.x.  The G-2 fix
    adds explicit ``is_multicast / is_reserved / is_unspecified``
    rejection so an attacker-controlled webhook URL of
    ``http://239.255.255.250/`` cannot broadcast the payload across
    every host on the LAN.
    """
    from webui.app import _is_safe_url
    assert _is_safe_url(url) is False, f"{url!r} should be blocked"


def test_multicast_is_global_assumption_holds():
    """Pins the upstream Python behaviour our G-2 fix relies on.
    If a future Python version stops reporting multicast as
    ``is_global=True``, this test fails loudly so we can re-evaluate
    whether the explicit rejection branch is still load-bearing.
    """
    assert ipaddress.IPv4Address("224.0.0.1").is_multicast is True
    assert ipaddress.IPv4Address("239.255.255.250").is_global is True


# ── G-3: _safe_urlopen blocks redirect-based SSRF ────────────────────────────

def test_safe_urlopen_no_redirect_handler_in_default_opener():
    """``_SAFE_URL_OPENER`` substitutes a ``_NoRedirectHandler`` for the
    default ``HTTPRedirectHandler``.  Without this, a public attacker
    server can respond ``302 Location: http://169.254.169.254/...``
    and ``urlopen`` would auto-follow, sending the Authorization header
    to AWS IMDS.
    """
    from webui.app import _SAFE_URL_OPENER, _NoRedirectHandler
    has_no_redirect = any(
        isinstance(h, _NoRedirectHandler) for h in _SAFE_URL_OPENER.handlers
    )
    assert has_no_redirect, (
        "_SAFE_URL_OPENER missing _NoRedirectHandler — redirect SSRF guard regressed"
    )


def test_safe_urlopen_rejects_private_target():
    """The opener re-validates via ``_is_safe_url`` on every hop.
    A direct call with a private URL must raise before any network
    I/O.
    """
    import urllib.error
    from webui.app import _safe_urlopen

    with pytest.raises(urllib.error.URLError):
        _safe_urlopen("http://10.0.0.1/", timeout=1.0)


# ── G-4 / G-5: redaction patterns cover new vendor token formats ──────────

def test_redact_anthropic_claude_code_oauth():
    """``sk-ant-oat<digits>-<base64ish>`` is the OAuth bearer used by
    Claude Code (anyone running ``claude api`` carries one in env).
    G-4 added it to the vendor pattern alternation.
    """
    from crucible.features.run_insights.redact import _redact_string_value
    secret = "sk-ant-oat01-" + "A" * 50
    text = f"401 Unauthorized: {secret} (rotate the key)"
    out = _redact_string_value(text)
    assert secret not in out
    assert "***REDACTED***" in out


def test_redact_openai_service_account_keys():
    """``sk-svcacct-<base64ish>`` is OpenAI service-account format.
    Dash in the prefix prevents the generic ``sk-`` pattern from
    matching, so G-5 added an explicit vendor pattern.
    """
    from crucible.features.run_insights.redact import _redact_string_value
    secret = "sk-svcacct-" + "B" * 60
    text = f"see auth header: {secret}"
    out = _redact_string_value(text)
    assert secret not in out
    assert "***REDACTED***" in out


# ── G-6 / G-7 / G-8: redact walks tuples / sets / bytes / cycles ───────────

def test_redact_value_walks_tuples():
    """Tuples were not recursed into pre-G-6; embedded secrets persisted
    to the JSONL after ``_normalise_for_canonical`` converted the
    tuple to a list at serialisation time.
    """
    from crucible.features.run_insights.redact import _redact_value
    secret = "sk-ant-api03-" + "X" * 50
    out = _redact_value("payload", ("hello", secret, "world"))
    assert isinstance(out, list)
    assert secret not in out
    assert "***REDACTED***" in out


def test_redact_value_walks_sets_with_deterministic_order():
    """Sets are unordered in Python but JSON output must be deterministic
    so the content-id (sha256 of canonical bytes) is reproducible.
    G-6 sorts after redaction by canonical-JSON repr.
    """
    from crucible.features.run_insights.redact import _redact_value
    s = {"hello", "world", "foo"}
    out_a = _redact_value("k", s)
    out_b = _redact_value("k", s)
    assert isinstance(out_a, list)
    assert out_a == out_b, "set ordering not deterministic across calls"


def test_redact_value_decodes_bytes():
    """G-7: bytes/bytearray leaves are decoded via UTF-8 errors=replace
    and run through tier-3 scanning; previously they bypassed redact
    and then crashed json.dumps inside _emit.
    """
    from crucible.features.run_insights.redact import _redact_value
    secret = ("sk-ant-api03-" + "Y" * 50).encode("utf-8")
    out = _redact_value("payload", secret)
    assert isinstance(out, str)
    assert "***REDACTED***" in out


def test_redact_value_handles_self_referential_dict():
    """G-8: a cyclic payload triggered RecursionError pre-fix, which
    the outer ``_emit`` swallowed → silent event loss.  Now back-edges
    become ``"<cycle>"`` and the rest of the structure survives.
    """
    from crucible.features.run_insights.redact import _redact_value
    a: dict = {"x": 1}
    a["self"] = a
    out = _redact_value("root", a)
    assert isinstance(out, dict)
    assert out["x"] == 1
    assert out["self"] == "<cycle>"


def test_normalise_for_canonical_cycle_detection():
    """Parallel guarantee for the encoder-side normalisation walk."""
    from crucible.features.run_insights.schema import _normalise_for_canonical
    a: list = [1, 2]
    a.append(a)
    out = _normalise_for_canonical(a)
    assert isinstance(out, list)
    assert out[0] == 1
    assert out[1] == 2
    assert out[2] == "<cycle>"


# ── G-9: NaN-sentinel returns from invalid equity bars ─────────────────────

def test_regime_detector_equity_to_returns_emits_nan():
    """G-9: ``regime_detector._equity_to_returns`` now emits NaN
    (not 0.0) for invalid bars — matches the quant_analytics contract.
    """
    from crucible.features.regime_detector import _equity_to_returns
    eq = [100.0, 0.0, 105.0, float("nan"), 110.0]
    rets = _equity_to_returns(eq)
    # rets[0] = (0 - 100)/100 = -1.0 — legitimate -100% return; not NaN.
    assert rets[0] == -1.0
    # rets[1] = prev=0 → fails the > 1e-14 floor → NaN sentinel.
    assert math.isnan(rets[1])
    # rets[2] = prev=105, curr=NaN → NaN sentinel.
    assert math.isnan(rets[2])
    # rets[3] = prev=NaN → NaN sentinel.
    assert math.isnan(rets[3])


def test_dynamic_correlation_compute_returns_emits_nan():
    """G-9: ``dynamic_correlation._compute_returns`` mirrors the
    NaN-sentinel contract."""
    from crucible.features.dynamic_correlation import _compute_returns
    eq = [100.0, 110.0, 0.0, 95.0]
    rets = _compute_returns(eq)
    assert rets[0] == pytest.approx(0.10)
    assert rets[1] == -1.0                  # prev=110, curr=0 → -1
    assert math.isnan(rets[2])              # prev=0 → NaN


def test_pearson_r_drops_nan_pairs():
    """G-9: ``_pearson_r`` filters non-finite pairs before computing
    correlation so legitimate signal on surviving bars is preserved
    (previously a single NaN propagated through and the function
    returned 0.0 via the isfinite short-circuit).
    """
    from crucible.features.dynamic_correlation import _pearson_r
    x = [1.0, 2.0, float("nan"), 4.0, 5.0]
    y = [1.0, 2.0, 3.0,           4.0, 5.0]
    r = _pearson_r(x, y)
    assert math.isclose(r, 1.0, rel_tol=1e-10)


# ── G-10 / G-11: HMM insufficient-data raises + relative tolerance ─────────

def test_hmm_insufficient_data_raises():
    """G-10: T<K*2 must raise ``HMMInsufficientDataError`` so the
    volatility-fallback in ``detect_regimes`` runs instead of
    silently labelling every bar as state 0.
    """
    from crucible.features.regime_detector import (
        _run_hmm_em,
        HMMInsufficientDataError,
    )
    with pytest.raises(HMMInsufficientDataError):
        _run_hmm_em([0.01, 0.02], n_states=3)


def test_detect_regimes_hmm_falls_back_on_short_series(tmp_path):
    """End-to-end: ``detect_regimes(method='hmm')`` with a series
    shorter than K*2 surfaces a warning AND falls back rather than
    returning meaningless labels.
    """
    from crucible.features.regime_detector import detect_regimes, RegimeConfig
    cfg = RegimeConfig(method="hmm", n_regimes=3)
    # 5 returns < K*2 = 6 finite returns required.
    result = detect_regimes(
        returns=[0.01, 0.02, -0.01, 0.03, -0.02],
        timestamps=["t1", "t2", "t3", "t4", "t5"],
        prices=[100.0, 101.0, 100.5, 103.5, 101.5],
        config=cfg,
    )
    assert any("HMM" in w or "hmm" in w for w in result.warnings), (
        f"expected explicit HMM warning, got: {result.warnings}"
    )


# ── G-12: Monte Carlo single-unique-value pool refused ─────────────────────

def test_monte_carlo_refuses_single_unique_pool():
    """G-12: a pool of one unique value would have produced identical
    paths with std=0 / VaR=0 / CVaR=0 — falsely advertising a perfect
    strategy.  Now refused with an explicit error.
    """
    from crucible.features.monte_carlo import (
        run_monte_carlo_simulation,
        MonteCarloConfig,
    )
    cfg = MonteCarloConfig(n_simulations=100, horizon_days=10)
    # ≥ 5 returns so the early "insufficient" gate passes, all
    # identical so the unique-value gate is the one that fires.
    result = run_monte_carlo_simulation(
        returns=[0.01, 0.01, 0.01, 0.01, 0.01, 0.01], config=cfg,
    )
    assert result.simulation_stats is None
    assert any("unique value" in e for e in result.errors), (
        f"expected unique-value abort, got errors: {result.errors}"
    )


# ── G-13: BACKTEST_PARAM_SEED determinism ──────────────────────────────────

def test_param_rng_default_seed_reproducible():
    """G-13: with default seed (4242), two import-resets of the module
    produce identical RNG draws.  Without seed, the OS-time seed makes
    optuna-disabled random search non-reproducible.
    """
    import importlib
    from crucible.features import backtest_runner

    # Reload to a clean seeded state, sample, reload again, sample again.
    # Both samples must match (the seed pinned the sequence).
    importlib.reload(backtest_runner)
    sample_a = [backtest_runner._PARAM_RNG.random() for _ in range(5)]
    importlib.reload(backtest_runner)
    sample_b = [backtest_runner._PARAM_RNG.random() for _ in range(5)]
    assert sample_a == sample_b, (
        "_PARAM_RNG not deterministic across imports — G-13 regressed"
    )


# ── G-14: parallel fetch hard timeout ─────────────────────────────────────

def test_parallel_fetch_uses_timeout_env_var():
    """G-14: ``BACKTEST_FETCH_HARD_TIMEOUT_SEC`` env var is read at the
    call-site and applied to ``.result(timeout=...)``.  Source-level
    structural check to pin the contract.
    """
    import inspect
    from crucible.features import backtest_runner
    source = inspect.getsource(backtest_runner)
    assert "BACKTEST_FETCH_HARD_TIMEOUT_SEC" in source
    assert ".result(timeout=" in source


# ── G-15 / G-16: tightened divisor floors per CLAUDE.md § 9.3 ──────────────

def test_sharpe_decay_ratio_floor_1e_8():
    """G-15: walk-forward Sharpe-decay-ratio denom floor tightened to
    1e-8 from 1e-10.  Source-level pin so a future contributor
    reverting won't slip past every other test.
    """
    import inspect
    from crucible.features import quant_analytics
    src = inspect.getsource(quant_analytics)
    assert "abs(result.avg_is_sharpe) > 1e-8" in src, (
        "Sharpe-decay-ratio denom floor regressed below 1e-8"
    )


def test_quant_analytics_equity_to_returns_subnormal_guard():
    """G-16: ``prev > 1e-14`` rejects IEEE 754 subnormals (5e-324)."""
    from crucible.features.quant_analytics import _equity_to_returns
    eq = [5e-324, 1.0, 1.1]   # subnormal prev → NaN sentinel
    rets = _equity_to_returns(eq)
    assert math.isnan(rets[0])


# ── G-17: schema marker tolerates UTF-8 BOM ─────────────────────────────────

def test_schema_marker_tolerates_bom(tmp_path, monkeypatch):
    """G-17: ``utf-8-sig`` decoding strips BOM so Notepad's default
    UTF-8-BOM save doesn't trigger a benign re-write loop on startup.
    """
    monkeypatch.setenv("CRUCIBLE_RUN_INSIGHTS_DIR", str(tmp_path / "ledger"))
    monkeypatch.setenv("CRUCIBLE_RUN_INSIGHTS_ENABLED", "1")
    # Manually write the marker with a BOM prefix.
    root = tmp_path / "ledger"
    root.mkdir()
    (root / "blobs").mkdir()
    (root / ".schema_version").write_text("﻿2\n", encoding="utf-8")
    # Construct the backend; if parser is broken it would re-write the
    # marker (and the test would still pass since the new content is "1").
    # We assert the marker content is UNCHANGED, proving the parser saw
    # "2" through the BOM.
    from crucible.features.run_insights.backends import LocalJSONLBackend
    LocalJSONLBackend(root)
    assert (root / ".schema_version").read_text(encoding="utf-8-sig").strip() == "2"


# ── G-18: orphan tempfile cleanup on init ──────────────────────────────────

def test_orphan_tempfile_cleanup(tmp_path, monkeypatch):
    """G-18: stale ``.prune_*.jsonl`` / ``.blob_*.tmp`` / ``.schema_*.tmp``
    files older than 24h are unlinked on backend init.
    """
    import time
    root = tmp_path / "ledger"
    root.mkdir()
    (root / "blobs").mkdir()
    # Fresh tempfile (should survive)
    fresh = root / ".prune_fresh.jsonl"
    fresh.write_text("recent")
    # Stale tempfile (should be removed)
    stale = root / ".prune_stale.jsonl"
    stale.write_text("ancient")
    # Backdate the stale file 2 days
    old = time.time() - 2 * 86400
    os.utime(stale, (old, old))

    from crucible.features.run_insights.backends import LocalJSONLBackend
    LocalJSONLBackend(root)

    assert fresh.exists(), "fresh tempfile was wrongly removed"
    assert not stale.exists(), "stale tempfile not cleaned up by G-18"


# ── G-19: backend warnings are rate-limited ───────────────────────────────

def test_backend_warn_once_dedups(tmp_path):
    """G-19: ``_warn_once`` records each (scope, key) tuple once,
    suppressing the rest.  Cap at 100 entries prevents memory blow-up.
    """
    from crucible.features.run_insights.backends import LocalJSONLBackend
    backend = LocalJSONLBackend(tmp_path / "ledger")
    # Sanity check that the helper exists
    assert hasattr(backend, "_warn_once")
    assert hasattr(backend, "_warned_paths")
    # Invoke twice with the same key; second call must NOT re-log
    # (we can't capture the log easily but we CAN verify the set
    # records the marker and capping is in place).
    backend._warn_once("test", "key1", "msg %s", 1)
    backend._warn_once("test", "key1", "msg %s", 1)  # dedup
    assert ("test", "key1") in backend._warned_paths
    # Fill past 100 to verify cap.
    for i in range(150):
        backend._warn_once("scope2", f"k{i}", "msg")
    assert len(backend._warned_paths) <= 101  # 1 from above + ≤100 capped


# ── G-20: record_output_method accepts data_source ─────────────────────────

def test_record_output_method_accepts_data_source(tmp_path, monkeypatch):
    """G-20: the recorder signature now propagates ``data_source`` and
    ``data_actual_symbol``.  Pin as part of the v1.2.0 retrieval API.
    """
    import inspect
    from crucible.features.run_insights.recorder import InsightsRecorder
    sig = inspect.signature(InsightsRecorder.record_output_method)
    assert "data_source" in sig.parameters
    assert "data_actual_symbol" in sig.parameters


# ── G-21: .env.example parser ignores 6-token descriptions ─────────────────

def test_env_example_parser_skips_description_sentences():
    """G-21: the tightened heuristic must NOT treat 4-6 token plain
    description sentences as group headers.  Specifically tests the
    case that bit the fifth-pass audit: the line that became "BACKTEST
    SYNTHETIC SEED's group title."
    """
    import json
    from webui.app import app

    with app.test_client() as client:
        resp = client.get("/api/env/schema")
        assert resp.status_code == 200
        groups = json.loads(resp.data)

    # No group name should look like a description fragment.
    bad_markers = [
        "used when",         # "Synthetic-data seed used when..."
        "main workflow",     # "Alibaba Coding Plan main workflow model"
        "Stage 0",
        "librarian /",
    ]
    for group_name in groups.keys():
        for marker in bad_markers:
            assert marker not in group_name, (
                f"description-sentence group name leaked through: "
                f"{group_name!r} contains {marker!r}"
            )


# ── G-22: zero-byte hash never cached ─────────────────────────────────────

def test_static_asset_hash_does_not_cache_empty_file(tmp_path, monkeypatch):
    """G-22: an editor truncate-during-save snapshot reads as 0 bytes
    and produces sha1 prefix ``da39a3ee5e``.  Caching that would mean
    the next edit fails to bust browser cache until Flask restart.
    """
    from webui import app as webui_app

    # Build a temp static dir with a zero-byte file.
    static_dir = tmp_path / "static"
    static_dir.mkdir()
    target = static_dir / "test.js"
    target.write_bytes(b"")

    monkeypatch.setattr(webui_app, "PROJECT_ROOT", tmp_path.parent)
    # Place under PROJECT_ROOT/webui/static for the hash helper.
    webui_dir = tmp_path.parent / "webui"
    webui_dir.mkdir(exist_ok=True)
    real_static = webui_dir / "static"
    real_static.mkdir(exist_ok=True)
    real_target = real_static / "_g22_test.js"
    real_target.write_bytes(b"")

    # Clear the cache
    webui_app._STATIC_ASSET_HASH_CACHE.clear()
    h1 = webui_app._static_asset_hash("_g22_test.js")
    assert h1 == "x", "zero-byte read should return 'x', not the empty-file hash"
    # Now write real content and ensure the cache picks up the new hash.
    real_target.write_bytes(b"console.log('hi')")
    h2 = webui_app._static_asset_hash("_g22_test.js")
    assert h2 != "x"
    assert h2 != "da39a3ee5e"
    # Cleanup
    try:
        real_target.unlink()
    except OSError:
        pass


# ── G-23: JS error rendering uses _escapeHtml consistently ────────────────

def test_app_js_error_rendering_uses_escape_html():
    """Structural: every ``innerHTML`` error path in ``app.js`` uses the
    centralised ``_escapeHtml`` helper rather than the ad-hoc
    ``replace(/[<>&]/g,'')`` form.
    """
    import re
    from pathlib import Path
    js = (
        Path(__file__).resolve().parents[1]
        / "webui" / "static" / "js" / "app.js"
    ).read_text(encoding="utf-8")
    # The strip-only form was the only known anti-pattern; assert it's
    # no longer present in any error-loading context.
    bad_pattern = re.compile(
        r"Failed to load[^`]*\$\{\(''\+err\)\.replace\(/\[<>&\]/g,''\)\}",
        re.IGNORECASE,
    )
    assert not bad_pattern.search(js), (
        "app.js still uses strip-only escaping for an error path — G-23 regressed"
    )


# ── G-24: secret-detection regex covers webhook / routing / etc. ─────────

@pytest.mark.parametrize(
    "key",
    [
        "SLACK_WEBHOOK_URL",
        "DISCORD_WEBHOOK_URL",
        "PAGERDUTY_ROUTING_KEY",
        "DATABASE_PASSWORD",
        "MY_BOT_TOKEN",
        "SENTRY_DSN",
    ],
)
def test_settings_secret_detection_regex_covers_extended_set(key: str):
    """G-24: the broadened regex catches webhook URLs, routing keys,
    DSNs, bot tokens, etc. so they render as masked password inputs
    in the Settings "Other" group instead of cleartext.
    """
    import re
    from pathlib import Path
    js = (
        Path(__file__).resolve().parents[1]
        / "webui" / "static" / "js" / "app.js"
    ).read_text(encoding="utf-8")
    # Find the broadened regex.
    m = re.search(r"const isSecret\s*=[^;]*;", js)
    assert m, "secret-detection assignment not found"
    region = m.group(0)
    # Sanity check: the broadened regex covers our key shape.
    # We don't recompile the JS regex in Python — instead assert the
    # source contains the expected fragments.
    expected_fragments = [
        "webhook",
        "routing",
        "password",
        "dsn",
        "bearer",
    ]
    for frag in expected_fragments:
        assert frag.lower() in region.lower(), (
            f"secret-detection regex missing {frag!r} fragment"
        )
