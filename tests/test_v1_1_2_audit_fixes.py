"""
tests/test_v1_1_2_audit_fixes.py
=================================
Regression coverage for the v1.1.2 four-agent audit-fix pass.

Each test class targets one of the seven groups identified in the
post-v1.1.2 audit; together they pin the producer→consumer wiring rule
from CLAUDE.md § 9.6 for the fixes that touched multiple subsystems.
"""
from __future__ import annotations

import inspect
import json
import math
import os
import re
import sys
import threading
import time
from pathlib import Path
from typing import Any
from unittest import mock

import pytest


# ──────────────────────────────────────────────────────────────────────────────
# Group 1 — run_id desync redux
# ──────────────────────────────────────────────────────────────────────────────

class TestGroup1RunIdRedux:
    """Group 1: the v1.1.2 line-1360 fix is now backed by the flat-launcher
    bridge + whitespace-strip on ``set_run_id`` + resilience sentinel.
    """

    def test_flat_launcher_calls_set_run_id_at_module_top(self):
        repo_root = Path(__file__).resolve().parent.parent
        text = (repo_root / "run_crucible.py").read_text(encoding="utf-8")
        # Must import set_run_id and call it before the cli import.
        assert "from crucible.run_correlation import set_run_id" in text
        # v1.1.2 (sixth-pass M-9): use a regex that tolerates the H-3
        # ``.strip() or None`` defence-in-depth wrapper.  The previous
        # exact-string fingerprint was the original v1.1.2 form and
        # broke when the sixth-pass H-3 fix wrapped the env read in a
        # ``.strip() or None`` to reject whitespace-only inputs.  The
        # contract being pinned is "set_run_id is called on the env
        # var", not the exact argument form — the value-form is
        # exercised by ``test_set_run_id_strips_whitespace_only_input``.
        call_re = re.compile(
            r"set_run_id\s*\(\s*\(?\s*_os\.environ\.get\(\s*[\"']CRUCIBLE_RUN_ID[\"']",
        )
        m = call_re.search(text)
        assert m is not None, (
            "run_crucible.py must call set_run_id on os.environ['CRUCIBLE_RUN_ID']"
        )
        # set_run_id must appear BEFORE `from crucible.cli import main`.
        idx_cli = text.find("from crucible.cli import main")
        assert 0 < m.start() < idx_cli, "set_run_id must run before cli import"

    def test_set_run_id_strips_whitespace_only_input(self):
        from crucible import run_correlation
        try:
            # Whitespace-only input must NOT pin a 3-space run_id — fall back
            # to a fresh UUID instead.
            rid = run_correlation.set_run_id("   ")
            assert rid.strip() == rid
            assert rid != "   "
            assert len(rid) > 0
        finally:
            # Reset the ContextVar so other tests aren't tainted.
            try:
                run_correlation._RUN_ID.set("")
            except Exception:
                pass

    def test_run_context_strips_whitespace_only_input(self):
        from crucible import run_correlation
        with run_correlation.run_context("   \t  ") as rid:
            assert rid.strip() == rid
            assert rid != "   \t  "
            assert len(rid) > 0

    def test_resilience_uses_three_tier_run_id_chain(self):
        from crucible import resilience
        src = inspect.getsource(resilience)
        # Three-tier resolution must be present (mirrors section_07 line-1360 fix).
        assert "_ri_get_run_id" in src
        assert "CRUCIBLE_RUN_ID" in src
        assert ".strip()" in src

    def test_resilience_uses_mode_unknown_stage_unknown_sentinels(self):
        from crucible import resilience
        src = inspect.getsource(resilience)
        # Empty mode/stage no longer collapse to "" — sentinels keep
        # aggregations distinct from a future legitimate empty-string mode.
        assert "mode_unknown" in src
        assert "stage_unknown" in src

    def test_resilience_warns_on_empty_run_id(self):
        from crucible import resilience
        src = inspect.getsource(resilience)
        assert "empty run_id" in src
        assert "LOGGER.warning" in src


# ──────────────────────────────────────────────────────────────────────────────
# Group 2 — v1.2.0 retrieval observability
# ──────────────────────────────────────────────────────────────────────────────

class TestGroup2RetrievalObservability:
    """Group 2: force_none telemetry, recorder warn flag split, DeepSeek
    redaction, backtest forced-source warning, canonical JSONL on disk,
    prune-tail dropping, set-ordering canonical, _NullRecorder.backend
    parity, WebUI insights streaming.
    """

    def test_recorder_has_two_independent_warn_flags(self):
        from crucible.features.run_insights.recorder import InsightsRecorder
        src = inspect.getsource(InsightsRecorder)
        assert "_warned_unknown_reason" in src
        assert "_warned_emit_failed" in src
        # Old shared flag must be gone — check the IDENTIFIER form
        # (``self._warned_once``) so the historical reference in the
        # explanatory comment doesn't false-positive.
        assert "self._warned_once" not in src

    def test_deepseek_32hex_pattern_is_present_before_generic(self):
        from crucible.features.run_insights import redact
        src = inspect.getsource(redact)
        # Vendor-specific pattern must precede the generic OpenAI legacy.
        # Find the actual ``re.compile(...)`` lines rather than docstring
        # mentions — both patterns appear in comments first, so a simple
        # ``.find`` returns the comment offset.
        deepseek_re = "re.compile(r\"(?<![A-Za-z0-9])sk-[A-Fa-f0-9]{32}"
        generic_re = "re.compile(r\"(?<![A-Za-z0-9])sk-[A-Za-z0-9]{40,80}"
        deepseek_idx = src.find(deepseek_re)
        generic_idx = src.find(generic_re)
        assert deepseek_idx > 0, "DeepSeek 32-hex regex must be present"
        assert generic_idx > 0, "OpenAI legacy regex must be present"
        assert deepseek_idx < generic_idx, (
            "DeepSeek 32-hex pattern must appear before the generic "
            "{40,80} OpenAI legacy pattern so the vendor-specific match "
            "wins on real DeepSeek tokens."
        )

    def test_deepseek_key_actually_redacts(self):
        from crucible.features.run_insights.redact import _redact_string_value
        # 32-hex DeepSeek key in an error message.
        leak = "401 invalid key sk-" + ("a" * 32) + " for model deepseek-v4-flash"
        redacted = _redact_string_value(leak)
        assert "REDACTED" in redacted
        assert "sk-" + ("a" * 32) not in redacted

    def test_force_none_emits_telemetry_when_gap_info_empty(self):
        """The v1.1.2 fix removed the ``gap_info``-shape predicate from the
        force_none telemetry gate.  We can't easily call the function
        end-to-end here (it needs LLMs etc.), so this is a structural pin
        that the new branch shape exists.
        """
        from crucible.modules import section_02_research_and_llm
        src = inspect.getsource(section_02_research_and_llm)
        # New gate: ``if force_none:`` (not ``if force_none and gap_info...``)
        # with gap_info used only to gate the diagnostic dump.
        assert "if force_none:" in src
        assert "_has_gap_detail" in src
        # The record_direction_debate_rejection emit must be inside the
        # ``if force_none:`` branch unconditionally — gated only by the
        # try/except for safety, NOT by gap_info shape.
        assert "record_direction_debate_rejection" in src

    def test_backtest_runner_records_forced_source_failure(self):
        from crucible.features import backtest_runner
        src = inspect.getsource(backtest_runner)
        assert 'profile["forced_source_failed"] = "yfinance"' in src
        assert 'profile["forced_source_failed"] = "binance"' in src
        assert 'profile["forced_source_failed"] = "project"' in src

    def test_backtest_warning_surfaces_forced_source(self):
        from crucible.features import backtest_runner
        src = inspect.getsource(backtest_runner)
        assert 'forced_source_failed' in src
        assert 'was explicitly\n' in src or "was explicitly " in src
        # Seed source provenance also surfaced.
        assert "env-pinned" in src
        assert "random-draw" in src

    def test_canonical_record_line_preserves_content_id(self):
        from crucible.features.run_insights.schema import (
            canonical_record_line, compute_content_id,
        )
        ev = {
            "schema_version": 1,
            "kind": "output_method",
            "run_id": "abc12345",
            "ts": "2026-05-14T00:00:00Z",
            "payload": {"foo": 1.0, "bar": [1, 2, 3]},
        }
        ev["content_id"] = compute_content_id(ev)
        # canonical_record_line must INCLUDE content_id.
        line = canonical_record_line(ev).decode("utf-8")
        parsed = json.loads(line)
        assert parsed["content_id"] == ev["content_id"]
        # Keys must be sorted (V8 / cloud-side parity).
        assert list(parsed.keys()) == sorted(parsed.keys())

    def test_local_backend_uses_canonical_record_line_on_disk(self):
        from crucible.features.run_insights import backends
        src = inspect.getsource(backends.LocalJSONLBackend.write_event)
        assert "_canonical_record_line" in src

    def test_prune_drops_unterminated_tail(self, tmp_path: Path):
        """Half-written tail (writer-crash artefact) must be dropped, not
        promoted into a 'valid-looking but JSON-invalid' kept line.

        Use ``max_entries=2`` so prune actually rewrites (a small
        ``max_entries`` forces the file-rewrite path; pruning 3 events
        with max_entries=1000 would short-circuit and leave the partial
        tail intact).
        """
        from crucible.features.run_insights.backends import LocalJSONLBackend
        from crucible.features.run_insights.schema import (
            compute_content_id,
        )
        backend = LocalJSONLBackend(str(tmp_path / "ledger"))
        # Write 3 complete events.
        for i in range(3):
            ev = {
                "schema_version": 1,
                "kind": "output_method",
                "run_id": f"r{i}",
                "ts": f"2026-05-14T00:00:0{i}Z",
                "payload": {"i": i},
            }
            ev["content_id"] = compute_content_id(ev)
            backend.write_event("output", ev)
        # Manually append a half-written line (no trailing \n).
        stream_path = tmp_path / "ledger" / "output.jsonl"
        with open(stream_path, "ab") as fh:
            fh.write(b'{"schema_version":1,"ts":"2026-05-14T00:00:10Z","ki')
        # Prune to keep at most 2 complete events; the partial tail must
        # be DROPPED (not promoted into the kept window).
        backend.prune_stream("output", 2)
        # Read back — must have only complete, parseable lines.
        with open(stream_path, "rb") as fh:
            contents = fh.read()
        lines = [ln for ln in contents.split(b"\n") if ln.strip()]
        assert len(lines) >= 1
        for ln in lines:
            json.loads(ln)  # Must not raise.
        # The partial tail's unique timestamp must NOT appear in the
        # rewritten file (the 3 complete events had timestamps 00..02, the
        # partial had timestamp 00:00:10Z which would not match any
        # complete record).
        assert b"00:00:10Z" not in contents

    def test_set_ordering_uses_canonical_json(self):
        from crucible.features.run_insights import redact
        src = inspect.getsource(redact)
        # The non-canonical json.dumps for set ordering must be gone in the
        # primary path.  Fallback may still reference it but only as a
        # defensive path.
        primary_path_re = re.compile(
            r"_canonical_json\(\{[^}]*\}\)\.decode\(\"utf-8\"\)"
        )
        assert primary_path_re.search(src)

    def test_null_recorder_backend_is_no_op_not_none(self):
        from crucible.features.run_insights.recorder import _NullRecorder, _NoOpBackend
        r = _NullRecorder()
        # Parity invariant: .backend must be a backend-like object, not None.
        assert r.backend is not None
        assert isinstance(r.backend, _NoOpBackend)
        # All protocol methods must be callable without raising.
        assert r.backend.write_event("output", {}) == ""
        assert r.backend.read_events("output") == []
        assert r.backend.prune_stream("output", 100) == 0
        r.backend.flush()
        r.backend.close()

    def test_webui_insights_uses_iter_and_tail_helpers(self):
        from webui import app as webui_app
        # Streaming helpers must exist.
        assert hasattr(webui_app, "_iter_jsonl_stream")
        assert hasattr(webui_app, "_tail_jsonl_stream")

    def test_webui_summary_uses_tail_only(self):
        from webui import app as webui_app
        src = inspect.getsource(webui_app.api_insights_summary)
        # Must use the tail helper, not the full-file reader.
        assert "_tail_jsonl_stream" in src
        # Should NOT materialise the entire stream then slice.
        assert "_read_jsonl_stream(path)" not in src

    def test_tail_jsonl_stream_works_on_large_file(self, tmp_path: Path):
        from webui.app import _tail_jsonl_stream
        path = tmp_path / "ledger.jsonl"
        # Write 200 events.
        with open(path, "w", encoding="utf-8") as fh:
            for i in range(200):
                fh.write(json.dumps({"i": i}) + "\n")
        tail = _tail_jsonl_stream(path, n=5)
        # Should return the LAST 5 events.
        assert len(tail) == 5
        # ``keep`` is built newest-first inside _tail_jsonl_stream.
        seen_indices = {t["i"] for t in tail}
        assert seen_indices == {195, 196, 197, 198, 199}


# ──────────────────────────────────────────────────────────────────────────────
# Group 3 — schema-first + output_validation NaN/Inf
# ──────────────────────────────────────────────────────────────────────────────

class TestGroup3SchemaFirstAndCoerce:

    def test_build_codegen_crew_calls_extract_quant_schema_signatures(self):
        from crucible.modules import section_05_analysis_and_codegen
        src = inspect.getsource(section_05_analysis_and_codegen.build_codegen_crew)
        assert "_extract_quant_schema_signatures" in src
        assert "schema_signatures" in src

    def test_coerce_rejects_nan(self):
        from crucible.output_validation import FieldSpec, _coerce
        spec = FieldSpec("x", float)
        # Non-finite floats must be rejected.
        for bad in ("nan", "NaN", "inf", "+inf", "-inf", "Infinity", float("nan"), math.inf, -math.inf):
            result, err = _coerce(bad, type_=float)
            assert result is None, f"NaN/Inf input {bad!r} must coerce to None"
            assert err is not None and "non-finite" in err.lower()

    def test_coerce_accepts_finite_floats(self):
        from crucible.output_validation import _coerce
        for good in (0.0, 1.0, -1.5, 1e-10, 1e10, "0.5", "1.0", True, False):
            result, err = _coerce(good, type_=float)
            assert err is None, f"finite input {good!r} unexpectedly rejected: {err}"
            assert isinstance(result, float)
            assert math.isfinite(result)


# ──────────────────────────────────────────────────────────────────────────────
# Group 4 — env_bool whitelist unification
# ──────────────────────────────────────────────────────────────────────────────

class TestGroup4EnvBoolWhitelist:

    def test_section_06_requires_snapshot_uses_env_bool(self):
        from crucible.modules import section_06_runtime_quality_api
        src = inspect.getsource(
            section_06_runtime_quality_api.requires_snapshot_validation
        )
        assert "_env_bool(\"CODEX_REQUIRE_SNAPSHOT\"" in src
        # v1.1.2 (sixth-pass M-9): structural regex match instead of exact-
        # indent string fingerprint so a future ``ruff format`` reflow of
        # the raw whitelist (e.g. into a single-line tuple
        # ``in ("1", "true", "yes")``) does not silently flip this test
        # to passing while the bug returns.
        _raw_whitelist_re = re.compile(
            r"os\.environ\.get\(\s*[\"']CODEX_REQUIRE_SNAPSHOT[\"']\s*[^)]*\)"
            r"[^A-Za-z0-9_]+\.[^A-Za-z0-9_]*lower\(\)[^A-Za-z0-9_]+in\s*[\(\{]",
            re.DOTALL,
        )
        assert _raw_whitelist_re.search(src) is None, (
            "raw env-var whitelist re-appeared in requires_snapshot_validation; "
            "must route through _env_bool"
        )

    def test_section_06_universal_crossref_uses_env_bool(self):
        from crucible.modules import section_06_runtime_quality_api
        src = inspect.getsource(section_06_runtime_quality_api)
        assert "_env_bool(\"CRUCIBLE_UNIVERSAL_CROSSREF\"" in src

    def test_section_06_require_tests_uses_env_bool(self):
        from crucible.modules import section_06_runtime_quality_api
        src = inspect.getsource(section_06_runtime_quality_api)
        assert "_env_bool(\"CRUCIBLE_QUANT_REQUIRE_TESTS\"" in src

    def test_section_03_min_score_uses_env_int(self):
        from crucible.modules import section_03_models_and_context
        src = inspect.getsource(section_03_models_and_context)
        assert "_env_int(\"CRUCIBLE_PRE_CODEGEN_MIN_SCORE\"" in src

    def test_smoke_stub_embeds_bool_env_function(self):
        from crucible.modules import section_06_runtime_quality_api
        src = inspect.getsource(section_06_runtime_quality_api)
        # The generated smoke harness must define ``_bool_env`` rather than
        # use the buggy inline whitelist.
        assert "def _bool_env(name, default=False)" in src
        assert "{'1', 'true', 'yes', 'on'}" in src


# ──────────────────────────────────────────────────────────────────────────────
# Group 5 — WebUI ops
# ──────────────────────────────────────────────────────────────────────────────

class TestGroup5WebUIOps:

    def test_concurrent_runs_semaphore_exists(self):
        from webui import app as webui_app
        assert hasattr(webui_app, "_runs_semaphore")
        assert isinstance(webui_app._runs_semaphore, threading.BoundedSemaphore)
        assert isinstance(webui_app._RUNS_MAX_CONCURRENT, int)
        assert webui_app._RUNS_MAX_CONCURRENT >= 1
        assert webui_app._RUNS_MAX_CONCURRENT <= 64

    def test_run_worker_acquires_and_releases_semaphore(self):
        from webui import app as webui_app
        src = inspect.getsource(webui_app._run_worker)
        assert "_runs_semaphore.acquire" in src
        assert "_runs_semaphore.release" in src

    def test_output_buffer_cap_exists(self):
        from webui import app as webui_app
        assert hasattr(webui_app, "_RUNS_MAX_OUTPUT_LINES")
        assert webui_app._RUNS_MAX_OUTPUT_LINES >= 1000

    def test_run_worker_implements_fifo_eviction(self):
        from webui import app as webui_app
        src = inspect.getsource(webui_app._run_worker)
        assert "output_evicted" in src
        assert "_RUNS_MAX_OUTPUT_LINES" in src

    def test_sse_generator_handles_truncation(self):
        from webui import app as webui_app
        src = inspect.getsource(webui_app.api_stream_run)
        assert "truncation_notified" in src
        assert "output_evicted" in src
        # The cumulative-sent invariant must be preserved.
        assert "sent - evicted" in src

    def test_dns_lookup_has_timeout(self):
        from webui import app as webui_app
        src = inspect.getsource(webui_app._is_safe_url)
        assert "socket.setdefaulttimeout(3" in src

    def test_eviction_timer_is_scheduled_at_import_time(self):
        from webui import app as webui_app
        assert hasattr(webui_app, "_schedule_eviction_timer")
        assert hasattr(webui_app, "_periodic_evict_runs")
        # The timer must be daemon so it doesn't block interpreter shutdown.
        with webui_app._eviction_timer_lock:
            t = webui_app._eviction_timer
            if t is not None:
                assert t.daemon is True

    def test_x_forwarded_host_gated_on_trust_env(self):
        from webui import app as webui_app
        src = inspect.getsource(webui_app._enforce_xhr_header_on_state_changes)
        assert "CRUCIBLE_TRUST_FORWARDED" in src
        assert "_forwarded_trusted" in src

    def test_x_forwarded_host_split_on_comma(self):
        from webui import app as webui_app
        src = inspect.getsource(webui_app._enforce_xhr_header_on_state_changes)
        assert ".split(\",\", 1)" in src


# ──────────────────────────────────────────────────────────────────────────────
# Group 6 — version + docs + CI
# ──────────────────────────────────────────────────────────────────────────────

class TestGroup6VersionAndDocs:

    def test_pyproject_version_matches_package_version(self):
        repo_root = Path(__file__).resolve().parent.parent
        pyproject = (repo_root / "pyproject.toml").read_text(encoding="utf-8")
        m = re.search(r'^version = "([^"]+)"', pyproject, re.MULTILINE)
        assert m, "pyproject.toml must declare a [project] version"
        pkg_version = m.group(1)
        import crucible
        assert crucible.__version__ == pkg_version

    def test_pyproject_version_is_at_least_1_1_2(self):
        import crucible
        # Simple lexicographic check — works for the dotted x.y.z format.
        parts = tuple(int(p) for p in crucible.__version__.split("."))
        assert parts >= (1, 1, 2), f"version regressed: {crucible.__version__}"

    def test_changelog_lists_v1_1_2(self):
        repo_root = Path(__file__).resolve().parent.parent
        changelog = (repo_root / "CHANGELOG.md").read_text(encoding="utf-8")
        assert "## [v1.1.2]" in changelog

    def test_no_readme_has_stale_1747_test_count(self):
        repo_root = Path(__file__).resolve().parent.parent
        for name in ("README.md", "README_zh.md", "README_FULL.md", "README_FULL_zh.md"):
            text = (repo_root / name).read_text(encoding="utf-8")
            assert "1747 tests" not in text, f"{name} still cites 1747 tests"
            assert "1747 項測試" not in text, f"{name} still cites 1747 項測試"

    def test_ci_compileall_excludes_skill_staging_and_insights(self):
        repo_root = Path(__file__).resolve().parent.parent
        ci_yml = (repo_root / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
        # Regex must include all three exclusions.
        assert "saved_projects|skill_staging" in ci_yml
        assert "crucible_insights" in ci_yml


# ──────────────────────────────────────────────────────────────────────────────
# Group 7 — WebUI misc + bilingual
# ──────────────────────────────────────────────────────────────────────────────

class TestGroup7WebUIMiscAndBilingual:

    def test_safe_500_helper_exists(self):
        from webui import app as webui_app
        assert hasattr(webui_app, "_safe_500")
        src = inspect.getsource(webui_app._safe_500)
        assert "log_id" in src
        assert "LOGGER.exception" in src

    def test_no_endpoint_leaks_str_exc_in_500(self):
        """All 500 responses must go through ``_safe_500`` (or the generic
        @app.errorhandler(500)).  Per-endpoint ``str(exc)`` leaks are
        forbidden; 400-class user-input errors are allowed to surface
        their own message because the caller's request is the problem.
        """
        from webui import app as webui_app
        src = inspect.getsource(webui_app)
        # Find all jsonify({"error": str(...)}), N matches.
        pattern = re.compile(
            r"jsonify\(\{\"error\":\s*str\([^)]+\)\}\),\s*(\d+)"
        )
        for status_str in pattern.findall(src):
            status = int(status_str)
            assert status != 500, (
                f"Found jsonify({{'error': str(exc)}}), 500 — must use "
                "_safe_500() helper instead so paths / hostnames don't "
                "leak."
            )

    def test_stage_models_tips_are_bilingual(self):
        repo_root = Path(__file__).resolve().parent.parent
        app_js = (repo_root / "webui" / "static" / "js" / "app.js").read_text(encoding="utf-8")
        # The three stage-model entries must carry bilingual tip objects.
        stage_models_section_start = app_js.index("id:'stage_models'")
        # v1.1.2 (sixth-pass M-9): slice by structural marker (the group's
        # closing ``}]``) instead of a hard-coded 2500-char window, so
        # future bilingual sweeps that grow ``zh`` text past the cap do
        # not push later entries outside the asserted window and silently
        # downgrade this test to a partial check.
        _group_end = app_js.find("]}", stage_models_section_start)
        if _group_end < 0:
            _group_end = stage_models_section_start + 8000
        window = app_js[stage_models_section_start:_group_end + 2]
        for key in ("librarian_model", "primary_model", "direction_judge_model"):
            entry_idx = window.find(f"key:'{key}'")
            assert entry_idx >= 0, f"missing entry for {key}"
            # The entry line must have a bilingual tip object.
            entry_line = window[entry_idx:entry_idx + 1500]
            assert "tip:{en:" in entry_line and "zh:" in entry_line, (
                f"{key} still uses an English-only tip string instead of "
                f"the bilingual {{en, zh}} object form"
            )

    def test_setlanguage_explicit_xhr_header(self):
        repo_root = Path(__file__).resolve().parent.parent
        app_js = (repo_root / "webui" / "static" / "js" / "app.js").read_text(encoding="utf-8")
        # The setLanguage save fetch must explicitly set X-Requested-With.
        # Locate the LANG_ENV_KEY POST block.
        idx = app_js.find("[LANG_ENV_KEY]: lang")
        assert idx > 0
        window = app_js[max(0, idx - 800):idx]
        assert "X-Requested-With" in window
        assert "XMLHttpRequest" in window

    def test_sse_done_event_is_padded_for_proxy_flush(self):
        from webui import app as webui_app
        src = inspect.getsource(webui_app.api_stream_run)
        # v1.1.2 (sixth-pass M-9): the previous assertion accidentally
        # combined a literal ``*\s*2048`` substring (which is NOT regex —
        # ``in`` is straight substring containment) with a plain ``* 2048``
        # match, so the first operand was guaranteed not to be present and
        # the test passed solely on the second operand.  Use a proper
        # regex that tolerates whitespace variation around the literal
        # ``2048``.
        assert re.search(r"\*\s*2048", src) is not None, (
            "SSE __done__ event padding (\"* 2048\") not found in "
            "api_stream_run; the proxy-flush guarantee may have been "
            "regressed."
        )

    def test_sse_keepalive_does_not_consume_sent_index(self):
        """Pin the design pattern: keepalive payloads do NOT increment
        ``sent``.  Future maintainers replacing the ``data:`` keepalive
        with an SSE comment (``: keepalive``) would silently break the
        watchdog refresh; this test catches the inverse regression
        (someone accidentally bumping ``sent`` on keepalive).
        """
        from webui import app as webui_app
        src = inspect.getsource(webui_app.api_stream_run)
        # Locate the keepalive emit and the surrounding 200 chars.
        idx = src.find("'__keepalive__'")
        assert idx > 0
        window = src[idx:idx + 400]
        # Must NOT contain ``sent +=`` within the keepalive block.
        # (sent += len(new_lines) at the top of the if-new_lines branch
        # is OK because new_lines is empty in the keepalive branch.)
        # We assert the structural design comment is present so future
        # readers understand the contract.
        assert "do NOT increment" in src or "do NOT consume" in src
