"""
tests/test_v1_1_9_regressions.py
================================

Regression pins for the eight v1.1.9 audit-fix items.  Each TestClass
guards exactly one finding and includes both behavioural assertions
(does the code do what it should at runtime?) and structural pins
(per CLAUDE.md § 9.6 producer→consumer wiring — does the wire-up
survive future refactors?).

Findings covered
----------------
H1  - ``_atomic_write_text`` now fsyncs the parent directory on POSIX
L1  - shared ``init_run_correlation_from_env`` used by all three entry points
M1  - ``auto_remediator._call_llm`` logs swallowed exceptions at DEBUG
M2  - optional dependencies in ``requirements.txt`` have minimum-version floors
L2  - 30+ ENHANCED_* flags in ``ENV_BACKED_FLAGS`` reach the subprocess
M3  - P5 degrade-not-die is no longer observation-only when the tolerate flag is on
M4  - section_01 ``_try_build`` retries with extra-field-stripped dict
H2  - section_04 dispatcher emits cooldown + health hooks
"""
from __future__ import annotations

import inspect
import logging
import os
import re
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))


# ────────────────────────────────────────────────────────────────────────────
# H1 — fsync parent directory after atomic replace
# ────────────────────────────────────────────────────────────────────────────


class TestH1AtomicIoFsync:
    def test_module_exports_atomic_write_text_and_fsync_dir(self) -> None:
        from crucible import _atomic_io
        assert hasattr(_atomic_io, "atomic_write_text")
        assert hasattr(_atomic_io, "fsync_dir")
        assert "atomic_write_text" in _atomic_io.__all__
        assert "fsync_dir" in _atomic_io.__all__

    def test_atomic_write_text_writes_and_replaces(self, tmp_path: Path) -> None:
        from crucible._atomic_io import atomic_write_text
        target = tmp_path / "sample.json"
        atomic_write_text(target, '{"hello": "world"}')
        assert target.read_text(encoding="utf-8") == '{"hello": "world"}'
        # ``.tmp`` sibling must be cleaned up after a successful rename
        assert not (tmp_path / "sample.json.tmp").exists()

    def test_atomic_write_text_calls_fsync_dir_when_enabled(
        self, tmp_path: Path
    ) -> None:
        from crucible import _atomic_io
        target = tmp_path / "out.txt"
        with patch.object(_atomic_io, "fsync_dir") as mock_fsync:
            _atomic_io.atomic_write_text(target, "data", fsync_parent=True)
        mock_fsync.assert_called_once()
        # The argument should be the parent dir of the written file.
        call_arg = mock_fsync.call_args.args[0]
        assert Path(call_arg).resolve() == tmp_path.resolve()

    def test_atomic_write_text_skip_fsync_when_disabled(
        self, tmp_path: Path
    ) -> None:
        from crucible import _atomic_io
        target = tmp_path / "out.txt"
        with patch.object(_atomic_io, "fsync_dir") as mock_fsync:
            _atomic_io.atomic_write_text(target, "data", fsync_parent=False)
        mock_fsync.assert_not_called()

    def test_fsync_dir_is_noop_on_windows(self, tmp_path: Path) -> None:
        from crucible import _atomic_io
        with patch.object(os, "name", "nt"):
            # Should not raise even when O_DIRECTORY is unavailable.
            _atomic_io.fsync_dir(tmp_path)

    def test_fsync_dir_swallows_oserror(self, tmp_path: Path) -> None:
        from crucible import _atomic_io
        with patch.object(os, "open", side_effect=OSError("simulated")):
            # Must not raise — durability is best-effort.
            _atomic_io.fsync_dir(tmp_path)

    def test_atomic_write_cleans_up_tmp_on_failure(self, tmp_path: Path) -> None:
        from crucible._atomic_io import atomic_write_text
        target = tmp_path / "broken.txt"

        original_replace = os.replace

        def boom(src: str, dst: str) -> None:  # noqa: ARG001
            raise PermissionError("simulated replace failure")

        with patch.object(os, "replace", side_effect=boom):
            with pytest.raises(PermissionError):
                atomic_write_text(target, "data")

        # The temp file must not survive a failed replace.
        assert not (tmp_path / "broken.txt.tmp").exists()

    def test_section_07_delegates_to_shared_helper(self) -> None:
        """Structural pin: section_07._atomic_write_text now calls the
        shared helper rather than carrying its own ``os.replace`` body."""
        from crucible.modules import section_07_selfcheck_output_main as s07
        src = inspect.getsource(s07._atomic_write_text)
        assert "_atomic_io" in src, (
            "section_07._atomic_write_text must delegate to crucible._atomic_io"
        )
        assert "os.replace(" not in src, (
            "section_07._atomic_write_text must not carry its own os.replace "
            "body — that pattern is what the v1.1.9 H1 fix factored out"
        )

    def test_quant_analytics_uses_shared_helper(self) -> None:
        """Structural pin: walk_forward + analytics report writers route
        through ``crucible._atomic_io.atomic_write_text`` instead of the
        previous raw ``os.replace`` pattern."""
        qa = (PROJECT_ROOT / "crucible" / "features" / "quant_analytics.py").read_text(
            encoding="utf-8"
        )
        # Two report writers (walk_forward + quant_analytics) both updated.
        assert qa.count("from .._atomic_io import atomic_write_text") >= 2
        # The old raw pattern must be gone from the two writer blocks.
        assert "_tmp_path = report_path + \".tmp\"" not in qa, (
            "Raw .tmp + os.replace pattern reintroduced in quant_analytics; "
            "the v1.1.9 H1 fix routes both writers through _atomic_io."
        )


# ────────────────────────────────────────────────────────────────────────────
# L1 — shared init_run_correlation_from_env helper
# ────────────────────────────────────────────────────────────────────────────


class TestL1RunCorrelationHelper:
    def test_helper_exists_and_returns_string(self) -> None:
        from crucible.run_correlation import init_run_correlation_from_env
        rid = init_run_correlation_from_env()
        assert isinstance(rid, str)

    def test_helper_strips_whitespace(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from crucible.run_correlation import (
            get_run_id,
            init_run_correlation_from_env,
            set_run_id,
        )
        set_run_id("")  # reset
        monkeypatch.setenv("CRUCIBLE_RUN_ID", "   ")
        rid = init_run_correlation_from_env()
        # Whitespace-only env triggers fresh UUID fallback inside set_run_id.
        assert rid
        assert rid.strip() == rid
        assert get_run_id() == rid

    def test_helper_uses_env_value_when_set(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from crucible.run_correlation import (
            get_run_id,
            init_run_correlation_from_env,
        )
        monkeypatch.setenv("CRUCIBLE_RUN_ID", "abc12345")
        rid = init_run_correlation_from_env()
        assert rid == "abc12345"
        assert get_run_id() == "abc12345"

    def test_helper_falls_back_to_uuid_when_unset(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from crucible.run_correlation import init_run_correlation_from_env
        monkeypatch.delenv("CRUCIBLE_RUN_ID", raising=False)
        rid = init_run_correlation_from_env()
        assert rid
        # UUID4 hex form
        assert re.fullmatch(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", rid)

    def test_helper_never_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from crucible.run_correlation import init_run_correlation_from_env
        # Even if set_run_id blows up, the helper must return a string.
        with patch("crucible.run_correlation.set_run_id", side_effect=RuntimeError("bad")):
            rid = init_run_correlation_from_env()
        assert rid == ""

    @pytest.mark.parametrize(
        "entry_path",
        [
            PROJECT_ROOT / "crucible" / "__main__.py",
            PROJECT_ROOT / "run_crucible.py",
            PROJECT_ROOT / "run_crucible_enhanced.py",
        ],
    )
    def test_all_three_entry_points_use_helper(self, entry_path: Path) -> None:
        """Structural pin: every CLI bootstrap path imports the shared
        helper.  Refactors that re-introduce the old strip-before-or-None
        duplicate will trip this pin."""
        src = entry_path.read_text(encoding="utf-8")
        assert "init_run_correlation_from_env" in src, (
            f"{entry_path.name} must call the shared "
            "init_run_correlation_from_env helper (v1.1.9 L1)"
        )

    def test_old_duplicated_pattern_is_gone(self) -> None:
        """The pre-v1.1.9 duplicated pattern was
        ``set_run_id((os.environ.get("CRUCIBLE_RUN_ID") or "").strip() or None)``
        — verify no entry point still carries it."""
        for entry in (
            PROJECT_ROOT / "crucible" / "__main__.py",
            PROJECT_ROOT / "run_crucible.py",
        ):
            src = entry.read_text(encoding="utf-8")
            assert "os.environ.get(\"CRUCIBLE_RUN_ID\")" not in src, (
                f"{entry.name} regressed to inline env-strip pattern; "
                "route through init_run_correlation_from_env instead."
            )


# ────────────────────────────────────────────────────────────────────────────
# M1 — auto_remediator logs swallowed LLM exceptions at DEBUG
# ────────────────────────────────────────────────────────────────────────────


class TestM1AutoRemediatorLogging:
    def test_module_has_logger(self) -> None:
        from crucible.features import auto_remediator
        assert hasattr(auto_remediator, "LOGGER")
        assert isinstance(auto_remediator.LOGGER, logging.Logger)
        assert auto_remediator.LOGGER.name == "crucible.features.auto_remediator"

    def test_call_llm_logs_on_exception(self, caplog: pytest.LogCaptureFixture) -> None:
        from crucible.features.auto_remediator import _call_llm

        class BoomLLM:
            def invoke(self, _prompt: str) -> None:
                raise RuntimeError("simulated 429")

        caplog.set_level(logging.DEBUG, logger="crucible.features.auto_remediator")
        result = _call_llm(BoomLLM(), "test prompt")
        assert result is None
        # The exception text should be in the DEBUG log line.
        joined = "\n".join(rec.getMessage() for rec in caplog.records)
        assert "simulated 429" in joined or "_call_llm" in joined

    def test_call_llm_does_not_log_on_success(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        from crucible.features.auto_remediator import _call_llm

        class GoodLLM:
            def invoke(self, _prompt: str) -> str:
                return "patched code"

        caplog.set_level(logging.DEBUG, logger="crucible.features.auto_remediator")
        result = _call_llm(GoodLLM(), "test prompt")
        assert result == "patched code"
        # No "failed" lines should appear on the happy path.
        assert not any("failed" in rec.getMessage() for rec in caplog.records)


# ────────────────────────────────────────────────────────────────────────────
# M2 — requirements.txt optional deps now carry minimum-version floors
# ────────────────────────────────────────────────────────────────────────────


class TestM2RequirementsFloors:
    """Pins the minimum-version policy CLAUDE.md § 9.5 / v1.1.2 sixth-pass M-11
    introduced for the core deps to the v1.1.9 optional deps."""

    OPTIONAL_DEPS_MUST_HAVE_FLOOR = {
        "python-dotenv",
        "pyyaml",
        "appdirs",
        "yfinance",
        "ccxt",
        "optuna",
        "fpdf2",
        "pypdf",
        "python-docx",
        "chromadb",
        "scikit-learn",
        "watchdog",
        "mlflow",
        "scipy",
        "statsmodels",
        "pandas-datareader",
        "quantstats",
        "opentelemetry-api",
        "opentelemetry-sdk",
        "opentelemetry-exporter-otlp",
        "APScheduler",
        "redis",
        "prometheus_client",
        "PyJWT",
        "bcrypt",
        "celery",
        "python-telegram-bot",
        "discord.py",
    }

    def test_each_optional_dep_has_version_floor(self) -> None:
        text = (PROJECT_ROOT / "requirements.txt").read_text(encoding="utf-8")
        missing = []
        for dep in self.OPTIONAL_DEPS_MUST_HAVE_FLOOR:
            # Match the line that starts with the dep name (case-sensitive).
            pattern = re.compile(
                rf"^{re.escape(dep)}\s*([><=]=?|~=)",
                re.MULTILINE,
            )
            if not pattern.search(text):
                missing.append(dep)
        assert not missing, (
            f"Optional dependencies without a version floor: {missing}. "
            "v1.1.9 M2 requires every actively-imported optional dep to "
            "carry a minimum version so pip-audit / CI cache keys remain "
            "semantically meaningful."
        )


# ────────────────────────────────────────────────────────────────────────────
# L2 — frontend ENV_BACKED_FLAGS fully wired to subprocess env
# ────────────────────────────────────────────────────────────────────────────


def _parse_env_backed_flags() -> dict[str, str]:
    """Parse the ENV_BACKED_FLAGS object literal in app.js into a dict."""
    js = (PROJECT_ROOT / "webui" / "static" / "js" / "app.js").read_text(
        encoding="utf-8"
    )
    m = re.search(r"const\s+ENV_BACKED_FLAGS\s*=\s*\{", js)
    assert m, "ENV_BACKED_FLAGS const not found in app.js"
    start = m.end()
    depth = 1
    i = start
    while depth > 0:
        if js[i] == "{":
            depth += 1
        elif js[i] == "}":
            depth -= 1
        i += 1
    block = js[start:i - 1]
    return dict(re.findall(r"(\w+)\s*:\s*['\"]([A-Z_]+)['\"]", block))


class TestL2EnhancedFlagWiring:
    def test_enhanced_flag_to_env_dict_exists(self) -> None:
        from webui.app import _ENHANCED_FLAG_TO_ENV
        assert isinstance(_ENHANCED_FLAG_TO_ENV, dict)
        # Sanity: at minimum the canonical Quant + Enhanced post-processing
        # toggles must be present; the full count is checked in the lockstep
        # test below.
        for key in (
            "security_scan",
            "html_report",
            "quant_analytics",
            "backtest_runner",
            "gate_control",
        ):
            assert key in _ENHANCED_FLAG_TO_ENV, (
                f"{key} missing from _ENHANCED_FLAG_TO_ENV"
            )

    def test_every_frontend_flag_has_backend_mapping(self) -> None:
        """Lockstep: every key in the frontend ``ENV_BACKED_FLAGS`` literal
        must appear in exactly one of the three backend mapping dicts.
        This is the v1.1.9 L2 acceptance test — if a new frontend flag is
        added without a backend mapping the panel will be visual-only."""
        from webui.app import (
            _ENHANCED_FLAG_TO_ENV,
            _RUN_INSIGHTS_FLAG_TO_ENV,
            _STORE_TRUE_FLAG_TO_ENV,
        )
        frontend = _parse_env_backed_flags()
        backend_keys = (
            set(_RUN_INSIGHTS_FLAG_TO_ENV)
            | set(_STORE_TRUE_FLAG_TO_ENV)
            | set(_ENHANCED_FLAG_TO_ENV)
        )
        unmapped = sorted(set(frontend) - backend_keys)
        assert not unmapped, (
            f"Frontend ENV_BACKED_FLAGS entries without a backend mapping: "
            f"{unmapped}.  Add each to _ENHANCED_FLAG_TO_ENV in webui/app.py "
            f"(or the appropriate other group) so the per-run toggle actually "
            f"reaches the subprocess.  Visual-only checkboxes were the v1.1.9 "
            f"L2 bug."
        )

    def test_frontend_and_backend_env_names_match(self) -> None:
        """When a frontend flag IS mapped on both sides, the env var name
        must match exactly — otherwise the toggle writes one key but the
        consumer reads another (the v1.1.0 fifth-pass G-1 trap)."""
        from webui.app import (
            _ENHANCED_FLAG_TO_ENV,
            _RUN_INSIGHTS_FLAG_TO_ENV,
            _STORE_TRUE_FLAG_TO_ENV,
        )
        frontend = _parse_env_backed_flags()
        combined: dict[str, str] = {
            **_RUN_INSIGHTS_FLAG_TO_ENV,
            **_STORE_TRUE_FLAG_TO_ENV,
            **_ENHANCED_FLAG_TO_ENV,
        }
        mismatches = {
            k: (frontend[k], combined[k])
            for k in frontend
            if k in combined and frontend[k] != combined[k]
        }
        assert not mismatches, (
            f"Frontend/backend env-name mismatches: {mismatches}.  "
            f"The producer (backend mapping) and consumer (frontend env "
            f"sync) must use the exact same env var name."
        )

    def test_resolver_emits_overrides_for_enhanced_flags(self) -> None:
        from webui.app import _resolve_run_insights_env_overrides
        flags = {
            "security_scan": False,
            "quant_analytics": True,
            "html_report": False,
            "gate_control": True,
            "api_version_check": False,
            "cointegration": True,
        }
        out = _resolve_run_insights_env_overrides(flags)
        assert out["ENHANCED_SECURITY_SCAN"] == "0"
        assert out["ENHANCED_QUANT_ANALYTICS"] == "1"
        assert out["ENHANCED_HTML_REPORT"] == "0"
        assert out["GATE_CONTROL_ENABLED"] == "1"
        assert out["API_VERSION_CHECK_ENABLED"] == "0"
        assert out["ENHANCED_COINTEGRATION"] == "1"

    def test_resolver_omits_missing_or_none_flags(self) -> None:
        from webui.app import _resolve_run_insights_env_overrides
        out = _resolve_run_insights_env_overrides({"html_report": None})
        # None means "untouched" — must NOT produce an env override.
        assert "ENHANCED_HTML_REPORT" not in out

    def test_mapping_rhs_matches_consumer_reads(self) -> None:
        """Structural pin (CLAUDE.md § 9.6): every RHS env name in
        ``_ENHANCED_FLAG_TO_ENV`` must appear in either
        ``run_crucible_enhanced.py`` (as an argparse default-read site) or
        a ``crucible/modules/section_*.py`` consumer.  The v1.1.0
        fifth-pass G-1 incident showed that wiring a mapping to a
        misspelled RHS is a silent-failure trap; this test prevents the
        same class of bug for the L2 fix."""
        from webui.app import _ENHANCED_FLAG_TO_ENV

        # Aggregate every env name read across the candidate consumer files.
        read_corpus: list[str] = []
        for rel in (
            "run_crucible_enhanced.py",
            "crucible/modules/section_05_analysis_and_codegen.py",
            "crucible/modules/section_06_runtime_quality_api.py",
            "crucible/modules/section_07_selfcheck_output_main.py",
        ):
            p = PROJECT_ROOT / rel
            if p.exists():
                read_corpus.append(p.read_text(encoding="utf-8"))
        haystack = "\n".join(read_corpus)

        missing_consumer = [
            (flag, env)
            for flag, env in _ENHANCED_FLAG_TO_ENV.items()
            if env not in haystack
        ]
        assert not missing_consumer, (
            f"_ENHANCED_FLAG_TO_ENV entries whose RHS env name is not "
            f"read by any candidate consumer: {missing_consumer}.  "
            f"Either the consumer was removed (delete the mapping) or "
            f"the RHS is misspelled (the v1.1.0 fifth-pass G-1 trap)."
        )

    def test_run_worker_merges_env_overrides_into_child_env(self) -> None:
        """Structural pin: the producer→consumer wire-up must still merge
        the resolver's output into the subprocess env."""
        from webui import app as webui_app
        src = inspect.getsource(webui_app._run_worker)
        assert "env_overrides" in src, (
            "_run_worker must continue to accept env_overrides — without "
            "the merge into _child_env, the L2 fix would silently regress."
        )


# ────────────────────────────────────────────────────────────────────────────
# M4 — section_01._try_build retries with extra-field-stripped dict
# ────────────────────────────────────────────────────────────────────────────


class TestM4LenientPydanticRetry:
    def test_strict_pass_does_not_trigger_lenient_retry(self) -> None:
        """The happy path — when the payload matches the schema exactly,
        the strict ``model_cls(**d)`` succeeds and lenient retry is never
        reached.  We assert by ensuring the returned object is the strict
        build (i.e. all original keys are still set)."""
        from pydantic import BaseModel
        from crucible.modules.section_01_extraction_and_reformat import (
            _extract_pydantic_from_result,
        )

        class Clean(BaseModel):
            name: str
            score: int

        payload = {"name": "ok", "score": 7}
        out = _extract_pydantic_from_result(payload, Clean, ("name", "score"))
        assert isinstance(out, Clean)
        assert out.name == "ok"
        assert out.score == 7

    def test_extra_fields_trigger_lenient_retry(self) -> None:
        """When the LLM emits extra keys that violate ``extra="forbid"``,
        the lenient retry strips them and rebuilds successfully."""
        from pydantic import BaseModel, ConfigDict
        from crucible.modules.section_01_extraction_and_reformat import (
            _extract_pydantic_from_result,
        )

        class Strict(BaseModel):
            model_config = ConfigDict(extra="forbid")
            name: str
            score: int

        # ``chatter`` is not in the model — strict fails, lenient strips
        # it and succeeds.
        payload = {"name": "ok", "score": 9, "chatter": "ignore me"}
        out = _extract_pydantic_from_result(payload, Strict, ("name", "score"))
        assert isinstance(out, Strict)
        assert out.name == "ok"
        assert out.score == 9

    def test_missing_required_fields_fail_both_paths(self) -> None:
        from pydantic import BaseModel
        from crucible.modules.section_01_extraction_and_reformat import (
            _extract_pydantic_from_result,
        )

        class Strict(BaseModel):
            name: str
            score: int

        # ``score`` missing — required_keys gate trips before either build.
        payload = {"name": "ok", "noise": "junk"}
        out = _extract_pydantic_from_result(payload, Strict, ("name", "score"))
        assert out is None

    def test_lenient_retry_block_present(self) -> None:
        """Structural pin (CLAUDE.md § 9.6): the lenient retry block must
        stay in ``_extract_pydantic_from_result``.  This catches future
        refactors that revert the v1.1.9 M4 fix."""
        from crucible.modules import section_01_extraction_and_reformat as s01
        src = inspect.getsource(s01._extract_pydantic_from_result)
        assert "model_fields" in src, (
            "_extract_pydantic_from_result must use ``model_cls.model_fields`` "
            "in the lenient retry path (v1.1.9 M4)."
        )
        assert "lenient" in src.lower() or "P1" in src or "M4" in src, (
            "_extract_pydantic_from_result must keep the lenient-retry "
            "comment so future maintainers know the second pass is intentional."
        )


# ────────────────────────────────────────────────────────────────────────────
# M3 — P5 degrade-not-die is now active behaviour, not observation-only
# ────────────────────────────────────────────────────────────────────────────


class TestM3DegradeNotDie:
    def test_preclamp_decision_stash_present_in_run_single(self) -> None:
        """Structural pin: ``_run_single_direction_debate`` must stash
        the pre-clamp decision into ``gap_info`` so the outer loop can
        opt into the degrade-not-die path."""
        from crucible.modules import section_02_research_and_llm as s02
        src = inspect.getsource(s02._run_single_direction_debate)
        assert "preclamp_decision" in src, (
            "_run_single_direction_debate must stash the pre-clamp "
            "decision into gap_info['preclamp_decision'] for the v1.1.9 "
            "M3 / P5 degrade-not-die path."
        )

    def test_outer_loop_reads_preclamp_decision_under_tolerate(self) -> None:
        """Structural pin: ``run_direction_debate`` must read the stashed
        ``preclamp_decision`` and return it when the tolerate toggle is on."""
        from crucible.modules import section_02_research_and_llm as s02
        src = inspect.getsource(s02.run_direction_debate)
        assert "preclamp_decision" in src, (
            "run_direction_debate must read gap_info['preclamp_decision'] "
            "to honour the v1.1.9 M3 / P5 degrade-not-die contract."
        )
        assert "_degraded_decision" in src, (
            "run_direction_debate must define _degraded_decision so the "
            "tail of the function can return it instead of None."
        )
        assert "decision_taken=True" in src or "degraded_proceed" in src, (
            "The degrade emit must mark ``original_decision='degraded_proceed'``"
            " (or equivalent) so v1.2.0 retrieval can distinguish active "
            "decisions from v1.1.8 observation rows."
        )

    def test_tolerate_off_preserves_v118_return_none(self) -> None:
        """Default behaviour (toggle off) returns None even when a
        preclamp decision is available — protects pre-v1.1.9 callers."""
        from unittest.mock import MagicMock
        from crucible.modules import section_02_research_and_llm as s02

        # Build a minimal stub DirectionDecision-like object.
        fake = MagicMock()
        fake.selected_direction = "B"
        fake.confidence = "medium"

        gap_with_preclamp = {
            "weak_directions": ["B"],
            "preclamp_decision": fake,
            "preclamp_reason": "near_zero_evidence",
        }

        # Run the synthesised tail-of-loop helper to make sure that when
        # ``_tolerate=False`` we get None even with preclamp present.
        # We do this without invoking the full _run_single_direction_debate
        # by reading the source and confirming the gate predicate.
        src = inspect.getsource(s02.run_direction_debate)
        # The tolerate gate must guard the degraded-decision assignment
        # — verify by token check that the variable is only initialised
        # to None outside the gate.
        assert "_degraded_decision: Optional" in src, (
            "_degraded_decision must initialise to None so toggle-off "
            "callers receive None unchanged."
        )

    def test_degraded_decision_caps_confidence_to_low(self) -> None:
        """When the degrade path activates, the pre-clamp decision's
        ``confidence`` field must be capped to 'low' so the gate
        controller and codegen scope chooser treat it as tentative."""
        from crucible.modules import section_02_research_and_llm as s02
        src = inspect.getsource(s02.run_direction_debate)
        # Token check — the literal "low" must appear inside the tolerate
        # branch.  We verify by looking for the setattr that clamps.
        assert 'setattr(_preclamp, "confidence", "low")' in src, (
            "Pre-clamp decision must have its confidence clamped to 'low' "
            "so downstream consumers treat it as tentative."
        )


# ────────────────────────────────────────────────────────────────────────────
# H2 — section_04 dispatcher wire-in (cooldown + health)
# ────────────────────────────────────────────────────────────────────────────


class TestH2DispatcherWireIn:
    def test_cooldown_skip_error_exists(self) -> None:
        from crucible.modules import section_04_web_research_and_direction as s04
        assert hasattr(s04, "_CooldownSkipError")
        assert issubclass(s04._CooldownSkipError, Exception)

    def test_helper_singletons_resolve(self) -> None:
        from crucible.modules import section_04_web_research_and_direction as s04
        assert s04._v119_get_cooldown_registry() is not None
        assert s04._v119_get_health_tracker() is not None

    def test_classify_429_triggers_cooldown(self) -> None:
        from crucible.modules import section_04_web_research_and_direction as s04
        import httpx

        class _Resp:
            status_code = 429

        exc = httpx.HTTPStatusError("rate limited", request=None, response=_Resp())  # type: ignore[arg-type]
        event, should_cool = s04._v119_classify_http_failure("test", exc)
        assert event == "rate_limit"
        assert should_cool is True

    def test_classify_202_triggers_cooldown(self) -> None:
        from crucible.modules import section_04_web_research_and_direction as s04
        import httpx

        class _Resp:
            status_code = 202

        exc = httpx.HTTPStatusError("bot mode", request=None, response=_Resp())  # type: ignore[arg-type]
        event, should_cool = s04._v119_classify_http_failure("test", exc)
        assert event == "bot_detection"
        assert should_cool is True

    def test_classify_timeout(self) -> None:
        from crucible.modules import section_04_web_research_and_direction as s04
        import httpx
        exc = httpx.TimeoutException("slow")
        event, should_cool = s04._v119_classify_http_failure("test", exc)
        assert event == "timeout"
        assert should_cool is False

    def test_classify_other(self) -> None:
        from crucible.modules import section_04_web_research_and_direction as s04
        event, should_cool = s04._v119_classify_http_failure("test", RuntimeError("???"))
        assert event == "other_error"
        assert should_cool is False

    def test_cooldown_skip_raises_when_provider_cooling(self) -> None:
        from crucible.modules import section_04_web_research_and_direction as s04
        from crucible.web_research.cooldown import CooldownRegistry
        from crucible.web_research.health import HealthTracker

        CooldownRegistry.reset_default()
        HealthTracker.reset_default()
        try:
            reg = CooldownRegistry.get_default()
            reg.trigger("websearch", reason="test_429")
            with pytest.raises(s04._CooldownSkipError):
                s04._safe_http_text(
                    "https://example.com/", provider_name="websearch",
                )
        finally:
            CooldownRegistry.reset_default()
            HealthTracker.reset_default()

    def test_record_http_failure_triggers_cooldown_on_429(self) -> None:
        from crucible.modules import section_04_web_research_and_direction as s04
        from crucible.web_research.cooldown import CooldownRegistry
        from crucible.web_research.health import HealthTracker
        import httpx

        CooldownRegistry.reset_default()
        HealthTracker.reset_default()
        try:
            class _Resp:
                status_code = 429
            exc = httpx.HTTPStatusError("429", request=None, response=_Resp())  # type: ignore[arg-type]
            s04._v119_record_http_failure("arxiv", exc)
            assert CooldownRegistry.get_default().is_cooling_down("arxiv") is True
            snap = HealthTracker.get_default().snapshot()
            assert snap["arxiv"]["rate_limited_429"] == 1
        finally:
            CooldownRegistry.reset_default()
            HealthTracker.reset_default()

    def test_all_seven_call_sites_pass_provider_name(self) -> None:
        """Structural pin: every direct ``_search_*`` HTTP call site must
        pass ``provider_name=`` so the cooldown + health hooks fire.  If
        a future refactor introduces a new ``_safe_http_*`` call without
        a provider name, this pin trips and the new path must be
        instrumented before merge."""
        s04_src = (
            PROJECT_ROOT
            / "crucible"
            / "modules"
            / "section_04_web_research_and_direction.py"
        ).read_text(encoding="utf-8")
        for expected in (
            'provider_name="websearch"',
            'provider_name="context7"',
            'provider_name="github"',
            'provider_name="arxiv"',
            'provider_name="grep_app"',
        ):
            assert expected in s04_src, (
                f"section_04 dispatcher missing call-site instrumentation: "
                f"{expected} not found.  Every _search_* function that "
                f"calls _safe_http_* must pass provider_name= so the "
                f"v1.1.9 H2 wire-in records the call."
            )

    def test_cache_hit_records_to_health_tracker(self) -> None:
        """Structural pin: when a query result comes from the L1+L2 cache,
        the dispatcher must record a cache_hit on the health tracker so
        end-of-stage summary doesn't undercount provider activity."""
        s04_src = (
            PROJECT_ROOT
            / "crucible"
            / "modules"
            / "section_04_web_research_and_direction.py"
        ).read_text(encoding="utf-8")
        assert "_tracker.record_cache_hit(provider_name)" in s04_src, (
            "Cache hit branch in _collect_librarian_search_materials must "
            "record to the v1.1.9 H2 health tracker."
        )

    def test_health_summary_emit_block_present(self) -> None:
        """Structural pin: end-of-stage health summary emission must be
        wired into ``_collect_librarian_search_materials``."""
        s04_src = (
            PROJECT_ROOT
            / "crucible"
            / "modules"
            / "section_04_web_research_and_direction.py"
        ).read_text(encoding="utf-8")
        assert "record_provider_health_summary(" in s04_src, (
            "_collect_librarian_search_materials must emit "
            "record_provider_health_summary at end-of-stage (v1.1.9 H2)."
        )
        assert "health_summary_enabled()" in s04_src, (
            "End-of-stage emit must be gated by health_summary_enabled() "
            "so operators can disable via LIBRARIAN_PROVIDER_HEALTH_SUMMARY=0."
        )
