"""
Regression pins for the v1.1.2 sixth-pass audit fixes.

This batch landed eight HIGH and twelve MEDIUM / sixteen LOW fixes on top of
the v1.1.2 audit-fix work documented in CHANGELOG.md.  Each test below pins
either a behavioural contract or a structural invariant (CLAUDE.md § 9.6
producer→consumer wiring style) so a future refactor / reformat cannot
silently regress the fix.

Grouping mirrors the H-N / M-N labels used in the CHANGELOG entry.
"""

from __future__ import annotations

import inspect
import io
import math
import os
import re
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


# ──────────────────────────────────────────────────────────────────────────────
# H-1  Subnormal sweep across quant features
# ──────────────────────────────────────────────────────────────────────────────

class TestH1SubnormalSweep:
    """Every ``_load_returns`` / ``_equity_to_returns`` / ``_align_*`` style
    helper in ``crucible/features/`` must use ``> 1e-14`` (not ``> 0``) when
    floor-checking a denominator.  v1.1.0 fifth-pass G-16 fixed some files;
    the sixth-pass fixed the sibling siblings (monte_carlo, risk_attribution,
    tearsheet, regime_detector, dynamic_correlation, portfolio_backtest,
    transaction_cost_model).
    """

    _FILES = [
        "crucible/features/monte_carlo.py",
        "crucible/features/risk_attribution.py",
        "crucible/features/tearsheet.py",
        "crucible/features/regime_detector.py",
        "crucible/features/dynamic_correlation.py",
        "crucible/features/portfolio_backtest.py",
        "crucible/features/transaction_cost_model.py",
        "crucible/features/quant_analytics.py",
        "crucible/features/factor_analyzer.py",
    ]

    # Detects a return-series comprehension or loop that floor-checks the
    # previous-bar value with the loose ``> 0`` predicate.  Three siblings:
    # ``prev > 0``, ``values[i - 1] > 0``, ``equity[i - 1] > 0``.
    _BAD_PATTERN = re.compile(
        r"(?:prev|values\[\s*i\s*-\s*1\s*\]|equity\[\s*i\s*-\s*1\s*\])\s*>\s*0\b",
    )

    @pytest.mark.parametrize("relpath", _FILES)
    def test_no_loose_zero_denominator_floor(self, relpath: str) -> None:
        src = (_REPO_ROOT / relpath).read_text(encoding="utf-8")
        bad_lines = []
        for lineno, line in enumerate(src.splitlines(), start=1):
            # Skip comment-only lines.
            stripped = line.lstrip()
            if stripped.startswith("#"):
                continue
            if self._BAD_PATTERN.search(line):
                bad_lines.append((lineno, line.strip()))
        assert not bad_lines, (
            f"{relpath} still uses ``> 0`` for a denominator floor — must be "
            "``> 1e-14`` per CLAUDE.md § 9.3:\n"
            + "\n".join(f"  line {ln}: {txt}" for ln, txt in bad_lines)
        )

    def test_regime_detector_std_floor_subnormal_safe(self) -> None:
        src = (_REPO_ROOT / "crucible/features/regime_detector.py").read_text(
            encoding="utf-8"
        )
        # The std-floor guard must use ``> 1e-14`` not ``> 0``.
        assert "global_std > 1e-14" in src, (
            "regime_detector._STD_FLOOR must gate on ``global_std > 1e-14``; "
            "v1.1.2 sixth-pass H-1 regression."
        )

    def test_dynamic_correlation_uses_nan_sentinel(self) -> None:
        src = (_REPO_ROOT / "crucible/features/dynamic_correlation.py").read_text(
            encoding="utf-8"
        )
        # _align_return_series must fill missing observations with NaN, not 0.0,
        # so the cross-asset Pearson r doesn't see correlated flat days.
        m = re.search(
            r"def _align_return_series.*?return sorted_ts, aligned",
            src,
            re.DOTALL,
        )
        assert m is not None, "_align_return_series body not found"
        body = m.group(0)
        assert "float(\"nan\")" in body, (
            "_align_return_series must substitute float('nan') for missing "
            "observations (v1.1.2 sixth-pass H-1)."
        )
        assert "ts_map.get(ts, 0.0)" not in body, (
            "_align_return_series still uses 0.0 fallback — regression."
        )


# ──────────────────────────────────────────────────────────────────────────────
# H-2  SSRF hardening on web_research/http_clients.py
# ──────────────────────────────────────────────────────────────────────────────

class TestH2HttpClientsSSRF:

    def test_rejects_multicast_and_private_v6_embedded_v4(self) -> None:
        from crucible.web_research.http_clients import _is_public_http_url

        # The webui ``_addr_is_safe`` contract: reject multicast, reserved,
        # unspecified, loopback, link-local, AND IPv6-embedded private v4.
        for bad in [
            "http://239.255.255.250/ssdp",         # SSDP multicast
            "http://224.0.0.1/",                    # IPv4 multicast
            "http://0.0.0.0/",                      # unspecified
            "http://169.254.169.254/latest/meta",  # link-local (AWS IMDS)
            "http://[::1]/",                        # IPv6 loopback
            "http://[::ffff:10.0.0.1]/",            # IPv4-mapped → private
            "http://[2002:a00:1::]/",               # 6to4 of 10.0.0.1
            "http://[64:ff9b::a00:1]/",             # NAT64 of 10.0.0.1
            "http://[fe80::1%eth0]/",               # scope-id
            "http://attacker.com@10.0.0.1/",        # userinfo smuggling
            "ftp://example.com/",                   # non-HTTP scheme
        ]:
            assert not _is_public_http_url(bad), (
                f"_is_public_http_url accepted {bad!r}; SSRF guard regression."
            )

    def test_http_client_disables_auto_redirect(self) -> None:
        from crucible.web_research.http_clients import _http_client

        src = inspect.getsource(_http_client)
        # The httpx client MUST be constructed with follow_redirects=False so
        # the manual SSRF-checked handler runs.
        assert "follow_redirects=False" in src, (
            "httpx client must disable auto redirect (v1.1.2 sixth-pass H-2)."
        )

    def test_safe_redirect_helper_revalidates_per_hop(self) -> None:
        from crucible.web_research.http_clients import _request_with_safe_redirects

        src = inspect.getsource(_request_with_safe_redirects)
        # Per-hop revalidation: _is_public_http_url called inside the loop.
        assert "_is_public_http_url(current_url)" in src, (
            "_request_with_safe_redirects must revalidate every hop "
            "(v1.1.2 sixth-pass H-2)."
        )
        # 301/302/303 must demote method to GET + clear body per RFC 7231 §6.4.
        assert "current_method = \"GET\"" in src
        assert "current_payload = None" in src


# ──────────────────────────────────────────────────────────────────────────────
# H-3  Run-id .strip() at 5 producer sites
# ──────────────────────────────────────────────────────────────────────────────

class TestH3RunIdStripProducers:
    """v1.1.2 sixth-pass H-3 contract:  the three CLI entry points must
    bind ``CRUCIBLE_RUN_ID`` into the run-correlation ContextVar with
    whitespace-only values rejected (no silent "   " run_id pollution).

    v1.1.9 (L1):  the three entry points used to duplicate the
    ``_set_run_id((os.environ.get(...) or "").strip() or None)`` literal
    inline.  They now share the canonical
    ``init_run_correlation_from_env()`` helper which delegates to
    ``set_run_id`` (whose own ``.strip()`` + UUID fallback satisfies the
    H-3 contract).  The structural pins below were rewritten to assert
    the new entry-point shape; the underlying contract is unchanged and
    still re-tested at the function level inside
    ``test_v1_1_9_regressions.py::TestL1RunCorrelationHelper``.
    """

    def test_run_crucible_bridge_uses_helper(self) -> None:
        src = (_REPO_ROOT / "run_crucible.py").read_text(encoding="utf-8")
        assert "init_run_correlation_from_env" in src, (
            "run_crucible.py must call the v1.1.9 shared helper "
            "init_run_correlation_from_env() — see CLAUDE.md § 2 for the "
            "lockstep contract across the three entry points."
        )

    def test_main_module_bridge_uses_helper(self) -> None:
        src = (_REPO_ROOT / "crucible/__main__.py").read_text(encoding="utf-8")
        assert "init_run_correlation_from_env" in src, (
            "crucible/__main__.py must call the v1.1.9 shared helper "
            "init_run_correlation_from_env()."
        )

    def test_run_crucible_enhanced_bridge_uses_helper(self) -> None:
        src = (_REPO_ROOT / "run_crucible_enhanced.py").read_text(encoding="utf-8")
        assert "init_run_correlation_from_env" in src, (
            "run_crucible_enhanced.py:main() must call the v1.1.9 shared "
            "helper init_run_correlation_from_env()."
        )

    def test_init_helper_still_strips_whitespace_only_run_id(self) -> None:
        """Behavioural pin for the H-3 contract: the shared helper used
        by all three entry points must still reject whitespace-only
        ``CRUCIBLE_RUN_ID`` values (this is what v1.1.2 H-3 originally
        guarded; v1.1.9 keeps the contract by delegating to ``set_run_id``
        which already strips)."""
        import os as _os
        from crucible.run_correlation import (
            get_run_id,
            init_run_correlation_from_env,
            set_run_id,
        )
        set_run_id("")  # reset
        prev = _os.environ.get("CRUCIBLE_RUN_ID")
        try:
            _os.environ["CRUCIBLE_RUN_ID"] = "   "
            rid = init_run_correlation_from_env()
            assert rid and rid.strip() == rid, (
                "Whitespace-only CRUCIBLE_RUN_ID must fall back to a fresh "
                "UUID, not pin a blank-looking run_id."
            )
            assert get_run_id() == rid
        finally:
            if prev is None:
                _os.environ.pop("CRUCIBLE_RUN_ID", None)
            else:
                _os.environ["CRUCIBLE_RUN_ID"] = prev

    def test_recorder_emit_strips_run_id_before_truncate(self) -> None:
        from crucible.features.run_insights import recorder

        src = inspect.getsource(recorder.InsightsRecorder._emit)
        # Must call .strip() before [:64] truncation.
        assert re.search(
            r"str\(run_id\s*or\s*[\"']{2}\)\.strip\(\)\[:\s*64\s*\]", src
        ) is not None, (
            "recorder._emit must apply .strip() before [:64] truncate "
            "(v1.1.2 sixth-pass H-3)."
        )

    def test_section_02_helper_has_three_tier_fallback(self) -> None:
        from crucible.modules import section_02_research_and_llm as s02

        helper = getattr(s02, "_resolve_run_id_for_ledger_emit", None)
        assert helper is not None, (
            "section_02 must export _resolve_run_id_for_ledger_emit "
            "(v1.1.2 sixth-pass H-3)."
        )
        src = inspect.getsource(helper)
        # Three tiers: ContextVar → env var → fresh uuid + set_run_id back.
        assert "_get_run_id" in src
        assert "CRUCIBLE_RUN_ID" in src
        assert "uuid.uuid4().hex[:8]" in src or "_uuid_local.uuid4().hex[:8]" in src
        assert "_set_local" in src or "set_run_id" in src

    def test_section_02_helper_strips_and_synthesises(self) -> None:
        from crucible.modules import section_02_research_and_llm as s02

        # Empty CRUCIBLE_RUN_ID + whitespace-only contextvar → fresh uuid.
        import crucible.run_correlation as rc
        _tok = rc._RUN_ID.set("   ")
        try:
            os.environ.pop("CRUCIBLE_RUN_ID", None)
            rid = s02._resolve_run_id_for_ledger_emit()
            assert isinstance(rid, str) and rid.strip(), (
                "resolver must return a non-empty trimmed run_id"
            )
            assert len(rid) >= 8, (
                "fresh-uuid fallback must be at least 8 chars"
            )
        finally:
            rc._RUN_ID.reset(_tok)


# ──────────────────────────────────────────────────────────────────────────────
# H-M3  section_03 REDACT_RULES bounded quantifiers
# ──────────────────────────────────────────────────────────────────────────────

class TestHM3RedactRulesBounded:

    def test_no_unbounded_quantifiers_in_redact_rules(self) -> None:
        from crucible.modules import section_03_models_and_context as s03

        # Grab the literal REDACT_RULES source slice.
        src = inspect.getsource(s03)
        m = re.search(
            r"REDACT_RULES\s*=\s*\[(.*?)\n\]\n",
            src,
            re.DOTALL,
        )
        assert m is not None, "REDACT_RULES list not found in section_03"
        block = m.group(1)
        # Every ``{N,}`` quantifier (no upper bound) must be gone.
        unbounded = re.findall(r"\{\d+,\s*\}", block)
        assert not unbounded, (
            "REDACT_RULES still contains unbounded ``{N,}`` quantifiers "
            f"(v1.1.2 sixth-pass H-M3): {unbounded}"
        )

    def test_deepseek_pattern_present_in_redact_rules(self) -> None:
        from crucible.modules import section_03_models_and_context as s03

        src = inspect.getsource(s03)
        assert "sk-[A-Fa-f0-9]{32}" in src, (
            "DeepSeek 32-hex vendor pattern must appear in section_03 "
            "REDACT_RULES so the codebase-wide twin matches Run Insights' "
            "redactor."
        )


# ──────────────────────────────────────────────────────────────────────────────
# H-4  WebUI _redact_for_client + 4 leaking endpoints
# ──────────────────────────────────────────────────────────────────────────────

class TestH4WebUIRedactExposed:

    def test_redact_for_client_strips_secrets_and_paths(self) -> None:
        from webui.app import _redact_for_client

        # sk-or-v1 key — must be redacted.
        assert "sk-or-v1-" not in _redact_for_client(
            "boom: sk-or-v1-aaaabbbbccccddddeeeeffff0000111122223333"
        )
        # Windows absolute path — basename preserved, directory stripped.
        out = _redact_for_client("error: C:\\Users\\johns\\secret\\token.txt missing")
        assert "Users\\johns\\secret" not in out
        assert "token.txt" in out, f"basename should survive, got {out!r}"
        # POSIX absolute path — same.
        out2 = _redact_for_client("ENOENT /home/op/secrets/key.pem")
        assert "/home/op/secrets" not in out2
        assert "key.pem" in out2
        # max_len truncation.
        out3 = _redact_for_client("x" * 1000, max_len=64)
        assert len(out3) <= 64

    def test_redact_for_client_handles_none_and_empty(self) -> None:
        from webui.app import _redact_for_client

        assert _redact_for_client(None) == ""
        assert _redact_for_client("") == ""

    def test_api_signal_routes_500_through_safe_500(self) -> None:
        from webui import app as webui_app

        src = inspect.getsource(webui_app.api_run_signal)
        # The stdin-write failure path must go through _safe_500.
        assert "_safe_500(exc, \"api_signal stdin write\")" in src

    def test_api_v169_metrics_does_not_format_raw_exc(self) -> None:
        from webui import app as webui_app

        src = inspect.getsource(webui_app.api_v169_metrics)
        # No raw ``{exc}`` substitution in the text/plain Response.
        assert "Error reading metrics.prom: {exc}" not in src
        assert "Error generating metrics: {exc}" not in src
        # log_id pattern must appear.
        assert "log_id=" in src

    def test_cost_trend_iterdir_path_does_not_leak(self) -> None:
        from webui import app as webui_app

        # The leaking endpoint is the cost_trend route (mounted at
        # ``/api/cost-trend``) — the audit referred to it by descriptive
        # name "list projects" but the actual function is ``cost_trend``.
        src = inspect.getsource(webui_app.cost_trend)
        # No raw ``str(exc)`` in the OSError 200-with-error path.
        # Find the iterdir except branch and inspect its body.
        m = re.search(
            r"entries = sorted\(saved_dir\.iterdir.*?except OSError as exc:\s*(.+?)\n\s{0,4}for\s",
            src,
            re.DOTALL,
        )
        assert m is not None
        body = m.group(1)
        assert "str(exc)" not in body, (
            "cost_trend must not return str(exc) in the iterdir "
            "except branch (v1.1.2 sixth-pass H-4)."
        )
        assert "directory enumeration failed" in body

    def test_send_notification_redacts_error_msg_before_db_insert(self) -> None:
        from webui import app as webui_app

        src = inspect.getsource(webui_app._send_notification_with_retry)
        # Must redact error_msg before assignment to last_error / DB INSERT.
        # Look for the _redact_for_client call wrapping error_msg.
        assert "_redact_for_client(error_msg" in src, (
            "_send_notification_with_retry must redact error_msg before "
            "the DB INSERT (v1.1.2 sixth-pass H-4)."
        )


# ──────────────────────────────────────────────────────────────────────────────
# H-5  _runs_semaphore acquire timeout + cancelled race
# ──────────────────────────────────────────────────────────────────────────────

class TestH5SemaphoreTimeout:

    def test_run_worker_uses_bounded_acquire(self) -> None:
        from webui import app as webui_app

        src = inspect.getsource(webui_app._run_worker)
        # Bounded acquire with a timeout.
        assert re.search(
            r"_runs_semaphore\.acquire\(\s*timeout\s*=", src
        ) is not None, (
            "_run_worker must call _runs_semaphore.acquire(timeout=...) "
            "(v1.1.2 sixth-pass H-5)."
        )
        # Cancellation re-check before subprocess spawn.
        assert "_cancelled_pre_spawn" in src
        # Conditional release (acquired-flag guarded).
        assert "if acquired:" in src


# ──────────────────────────────────────────────────────────────────────────────
# H-6  Google Fonts referrerpolicy / crossorigin hardening
# ──────────────────────────────────────────────────────────────────────────────

class TestH6GoogleFontsHardened:

    def test_index_html_fonts_link_carries_referrerpolicy_and_crossorigin(self) -> None:
        html = (_REPO_ROOT / "webui/templates/index.html").read_text(encoding="utf-8")
        # The Google Fonts stylesheet link must carry crossorigin AND
        # referrerpolicy="no-referrer".
        m = re.search(
            r'<link[^>]*href="https://fonts\.googleapis\.com/css2[^"]*"[^>]*>',
            html,
            re.DOTALL,
        )
        assert m is not None, "Google Fonts <link> tag not found"
        tag = m.group(0)
        assert 'crossorigin="anonymous"' in tag, (
            "Google Fonts <link> must declare crossorigin=anonymous "
            "(v1.1.2 sixth-pass H-6)."
        )
        assert 'referrerpolicy="no-referrer"' in tag, (
            "Google Fonts <link> must declare referrerpolicy=no-referrer "
            "(v1.1.2 sixth-pass H-6)."
        )


# ──────────────────────────────────────────────────────────────────────────────
# M-1  Ledger lock for read_events + write_blob short-circuit
# ──────────────────────────────────────────────────────────────────────────────

class TestM1LedgerLockAndBlob:

    def test_read_events_acquires_sidecar_lock(self) -> None:
        from crucible.features.run_insights import backends

        src = inspect.getsource(backends.LocalJSONLBackend.read_events)
        # The sidecar lock must be held for the duration of the read.
        assert "_stream_lock_path" in src
        assert "_file_lock_ctx" in src

    def test_write_blob_short_circuits_on_existing_path(self) -> None:
        from crucible.features.run_insights import backends

        src = inspect.getsource(backends.LocalJSONLBackend.write_blob)
        # Content-addressable short-circuit must run BEFORE tempfile.mkstemp.
        idx_exists = src.find("path.exists()")
        idx_mkstemp = src.find("tempfile.mkstemp")
        assert idx_exists != -1 and idx_mkstemp != -1
        assert idx_exists < idx_mkstemp, (
            "write_blob must check path.exists() BEFORE tempfile.mkstemp "
            "to short-circuit duplicate concurrent writers "
            "(v1.1.2 sixth-pass M-1)."
        )


# ──────────────────────────────────────────────────────────────────────────────
# H-8  streaming SystemExit / KeyboardInterrupt handling
# ──────────────────────────────────────────────────────────────────────────────

class TestH8StreamingBaseException:

    def test_stream_worker_reraises_systemexit_and_keyboardinterrupt(self) -> None:
        from crucible import streaming

        src = inspect.getsource(streaming.stream_crew)
        # The worker function body must include an explicit re-raise of
        # SystemExit / KeyboardInterrupt before the generic catch.
        assert "except (SystemExit, KeyboardInterrupt):" in src
        assert "raise" in src
        # Generic catch must be Exception (not BaseException) so the
        # earlier re-raise is observed.
        assert "except Exception as exc:" in src


# ──────────────────────────────────────────────────────────────────────────────
# M-5  env_int for CRUCIBLE_QUANT_DRYRUN_TIMEOUT
# ──────────────────────────────────────────────────────────────────────────────

class TestM5EnvIntDryRunTimeout:

    def test_quant_smoke_timeout_uses_env_int(self) -> None:
        from crucible.modules import section_06_runtime_quality_api as s06

        src = inspect.getsource(s06)
        # Must contain ``_env_int("CRUCIBLE_QUANT_DRYRUN_TIMEOUT"...``.
        assert re.search(
            r"_env_int\(\s*[\"']CRUCIBLE_QUANT_DRYRUN_TIMEOUT[\"']", src
        ) is not None, (
            "CRUCIBLE_QUANT_DRYRUN_TIMEOUT must route through _env_int "
            "(v1.1.2 sixth-pass M-5)."
        )
        # The raw ``int(os.environ.get("CRUCIBLE_QUANT_DRYRUN_TIMEOUT"`` form
        # must be gone.
        assert re.search(
            r"int\(\s*os\.environ\.get\(\s*[\"']CRUCIBLE_QUANT_DRYRUN_TIMEOUT[\"']",
            src,
        ) is None


# ──────────────────────────────────────────────────────────────────────────────
# M-7  math.isfinite gates (outcome_score / _coerce_json_dict)
# ──────────────────────────────────────────────────────────────────────────────

class TestM7FiniteGates:

    def test_outcome_score_rejects_non_finite(self) -> None:
        from crucible.modules import section_07_selfcheck_output_main as s07

        src = inspect.getsource(s07)
        # math.isfinite gate must appear in the outcome_score computation.
        assert "math.isfinite(_outcome_score_candidate)" in src, (
            "outcome_score must be gated through math.isfinite "
            "(v1.1.2 sixth-pass M-7)."
        )

    def test_coerce_json_dict_disallows_nan(self) -> None:
        from crucible.modules import section_00_bootstrap_and_utils as s00

        # Behavioural: passing a dict-like value with a NaN must drop the
        # NaN entirely (return None) rather than round-tripping NaN through.
        # _coerce_json_dict's first branch returns the dict as-is when value
        # is already dict — so test the OTHER branch (non-dict, non-str).
        # Use a list containing a NaN: json.dumps(list_with_nan,
        # allow_nan=False) raises ValueError → return None.
        result = s00._coerce_json_dict([float("nan")])
        assert result is None, (
            "_coerce_json_dict must reject inputs containing NaN/Inf "
            "(v1.1.2 sixth-pass M-7)."
        )


# ──────────────────────────────────────────────────────────────────────────────
# M-10  timestamp_utc seam in run_meta.json
# ──────────────────────────────────────────────────────────────────────────────

class TestM10TimestampUTC:

    def test_save_emits_timestamp_utc_field(self) -> None:
        from crucible.modules import section_07_selfcheck_output_main as s07

        src = inspect.getsource(s07)
        # The save function must compute a UTC timestamp AND setdefault it
        # into run_meta_payload as ``timestamp_utc``.
        assert "datetime.now(_utc_tz.utc)" in src or "timezone.utc" in src
        assert 'setdefault("timestamp_utc"' in src, (
            "run_meta.json must carry a ``timestamp_utc`` field for v1.2.0 "
            "cross-machine retrieval joins (v1.1.2 sixth-pass M-10)."
        )


# ──────────────────────────────────────────────────────────────────────────────
# M-4  Pipeline print() redact via _run_worker capture boundary
# ──────────────────────────────────────────────────────────────────────────────

class TestM4PipelineCaptureRedacts:

    def test_run_worker_redacts_each_captured_line(self) -> None:
        from webui import app as webui_app

        src = inspect.getsource(webui_app._run_worker)
        # The captured stdout must pass through _redact_for_client BEFORE
        # being appended to the output buffer.
        m = re.search(
            r"stripped\s*=\s*_redact_for_client\(\s*stripped",
            src,
        )
        assert m is not None, (
            "_run_worker must redact each captured stdout line via "
            "_redact_for_client (v1.1.2 sixth-pass M-4)."
        )


# ──────────────────────────────────────────────────────────────────────────────
# M-11  requirements.txt minimum-version pinning
# ──────────────────────────────────────────────────────────────────────────────

class TestM11RequirementsPinned:

    def test_core_deps_have_minimum_version_floors(self) -> None:
        req = (_REPO_ROOT / "requirements.txt").read_text(encoding="utf-8")
        # Every core dep must carry an explicit ``>=`` floor so the
        # CI cache key (content hash) is semantically meaningful.
        for dep_re in (
            r"^crewai\s*>=\s*\d",
            r"^pydantic\s*>=\s*\d",
            r"^httpx\s*>=\s*\d",
            r"^litellm\s*>=\s*\d",
            r"^flask\s*>=\s*\d",
        ):
            assert re.search(dep_re, req, re.MULTILINE) is not None, (
                f"requirements.txt missing pin matching {dep_re!r} "
                "(v1.1.2 sixth-pass M-11)."
            )
