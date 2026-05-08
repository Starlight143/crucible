"""Regression tests for the codegen LLM-fix loop, ``.env`` propagation,
WebUI agent-flow display, and Pearson-r NaN handling.

Bugs pinned by these tests:

1. **Quant-mode auto-backtest crashes on LLM-emitted SyntaxError and the
   round-1 LLM-fix loop fails to repair it** — the previous
   ``_try_llm_fix`` path only checked that the extracted code block was
   non-empty (``len(fixed_code.strip()) < 20``).  When the LLM returned
   *equally-broken* code (same ``SyntaxError: unterminated string
   literal`` class), the file would be overwritten with the broken
   replacement, the next subprocess call would fail again, and three
   fix rounds would be burned with zero progress.  A hard ``compile()``
   syntax gate plus deterministic JSON-escape repair now run before
   writing.  The ``backtest_report.json`` failure path was also
   tightened: the regex-based ``_parse_backtest_output`` no longer
   scrapes phantom metrics out of a Python traceback after a crashed
   subprocess; only fresh JSON files written *during this run* are
   honoured.

2. **``.env`` DEBUG mode silently ignored by the WebUI** —
   ``CRUCIBLE_LOG_LEVEL=DEBUG`` set in ``.env`` was never loaded into
   the Python process because no module called ``load_dotenv``.  Both
   ``runtime_logging`` and ``webui/app.py`` are now wired up to
   ``python-dotenv`` (when installed), with the loader protected by a
   double-checked ``threading.Lock`` so two threads cannot race the load
   (a real risk under Python 3.13+ free-threaded mode).

3. **WebUI agent flow display stuck after codegen** —
   ``codegen_kickoff_done`` previously only set ``self_check`` to
   ``active``, leaving every stage-8 node (code_arch, code_gen, …)
   stuck showing ``active`` for the rest of the run.  Plus
   ``direction_feedback_start`` had no front-end mapping, so the graph
   appeared frozen during gate-driven debate refinement.  Both fixed.

4. **Pearson r NaN propagation** — both
   ``dynamic_correlation._pearson_r`` and
   ``cointegration_analyzer._pearson_r`` clamped via
   ``max(-1.0, min(1.0, raw))``, which is order-sensitive in Python
   (``min(1.0, nan) == 1.0`` but ``min(nan, 1.0) == nan``).  When an
   intermediate yielded NaN (typically because the input series
   contained NaN values that propagated through the mean/covariance
   helpers), the clamp could leak NaN into downstream metric tables.
   Both helpers now explicitly check ``math.isfinite(raw)`` and
   short-circuit to ``0.0`` for non-finite results.
"""

from __future__ import annotations

import math
import os
import threading
from pathlib import Path
from unittest.mock import patch

import pytest

from crucible.features import backtest_runner as br
from crucible.features import dynamic_correlation as dc
from crucible.features import cointegration_analyzer as ca
from crucible import runtime_logging as rtl


# ════════════════════════════════════════════════════════════════════════════
# Bug 1: codegen syntax-error sanitization + fail-loud backtest fallback
# ════════════════════════════════════════════════════════════════════════════


class TestValidatePythonSyntax:
    """Verify the compile-based syntax gate catches the LLM-emitted bug
    class — unterminated string literals, invalid escape sequences,
    mismatched brackets."""

    def test_clean_code_returns_none(self) -> None:
        assert br._validate_python_syntax("x = 1\nprint(x)\n") is None

    def test_unterminated_string_literal_caught(self) -> None:
        # The exact bug class from the user's report.
        bad = 'msg = "hello\nprint(msg)\n'
        err = br._validate_python_syntax(bad)
        assert err is not None
        assert "line" in err  # error string carries the line number

    def test_mismatched_brackets_caught(self) -> None:
        err = br._validate_python_syntax("x = (1 + 2\nprint(x)\n")
        assert err is not None

    def test_null_byte_caught_via_valueerror(self) -> None:
        # ``compile()`` raises ValueError (not SyntaxError) for null
        # bytes; the helper handles both.
        err = br._validate_python_syntax("x = 1\x00\n")
        assert err is not None

    def test_empty_input_caught(self) -> None:
        assert br._validate_python_syntax("") == "empty code"
        assert br._validate_python_syntax(None) == "empty code"  # type: ignore[arg-type]


class TestDeterministicRepairLLMCode:
    """The LLM sometimes returns code with JSON-style ``\\n`` escapes,
    BOMs, or stray triple-backtick wrappers that the regex-only
    extractor can miss.  This helper applies deterministic fixes
    before the syntax gate."""

    def test_returns_unmodified_when_already_valid(self) -> None:
        clean = "import os\n\ndef main():\n    print('ok')\n"
        # Strip BOM/fence behaviour adds no changes when source is clean.
        repaired = br._deterministic_repair_llm_code(clean)
        # Compile-clean equivalent (line-stripped is fine).
        assert br._validate_python_syntax(repaired) is None

    def test_strips_bom(self) -> None:
        with_bom = "﻿import os\nprint(os.getcwd())\n"
        repaired = br._deterministic_repair_llm_code(with_bom)
        assert not repaired.startswith("﻿")
        assert br._validate_python_syntax(repaired) is None

    def test_strips_trailing_fence(self) -> None:
        fenced = "import os\nprint(1)\n```"
        repaired = br._deterministic_repair_llm_code(fenced)
        assert "```" not in repaired
        assert br._validate_python_syntax(repaired) is None

    def test_strips_leading_fence_line(self) -> None:
        fenced = "```\nimport os\nprint(1)\n"
        repaired = br._deterministic_repair_llm_code(fenced)
        assert not repaired.startswith("```")
        assert br._validate_python_syntax(repaired) is None

    def test_unescapes_json_style_newlines(self) -> None:
        # A real LLM response shape: a single line with literal ``\n``
        # backslash+n pairs instead of real newlines.
        escaped = "import os\\nprint(1)\\n"
        repaired = br._deterministic_repair_llm_code(escaped)
        assert "\n" in repaired  # real newline now present
        assert br._validate_python_syntax(repaired) is None


class TestExtractCodeBlock:
    """The improved extractor handles the two LLM "almost-correct"
    response shapes the audit identified."""

    def test_picks_longest_when_multiple_fences(self) -> None:
        # An example "tiny first block + real second block" response.
        text = (
            "Here is a small example:\n"
            "```python\n"
            "x = 1\n"
            "```\n"
            "And here is the actual fix:\n"
            "```python\n"
            "import os\nimport sys\n\ndef main():\n    print(os.getcwd())\n\nif __name__ == '__main__':\n    main()\n"
            "```\n"
        )
        block = br._extract_code_block(text)
        assert "import sys" in block  # picked the real one
        assert "def main" in block

    def test_handles_truncated_response_no_closing_fence(self) -> None:
        text = "Here is the fix:\n```python\nimport os\nprint(os.getcwd())\n"
        block = br._extract_code_block(text)
        assert "import os" in block
        assert "```" not in block

    def test_returns_empty_for_pure_prose(self) -> None:
        text = "I think the fix should adjust the variable name and ..."
        assert br._extract_code_block(text) == ""


class TestTryLLMFixSyntaxGate:
    """Verify the hard syntax gate: a fix that introduces a fresh
    ``SyntaxError`` is rejected as ``produced no valid code`` instead
    of being written to disk."""

    def _write_entrypoint(self, tmp_path: Path, content: str) -> str:
        ep = tmp_path / "backtest.py"
        ep.write_text(content, encoding="utf-8")
        return str(ep)

    def test_rejects_fix_with_syntax_error(self, tmp_path: Path) -> None:
        # Original entrypoint with the user-reported bug class.
        entrypoint = self._write_entrypoint(
            tmp_path, 'msg = "broken\nprint(msg)\n'
        )

        class _BrokenLLM:
            def invoke(self, _prompt: str) -> object:  # noqa: D401
                # The "fix" still has an unterminated string literal.
                class _Result:
                    content = '```python\nmsg = "still broken\nprint(msg)\n```'
                return _Result()

        ok = br._try_llm_fix(
            _BrokenLLM(), str(tmp_path), entrypoint, "SyntaxError", "problem"
        )
        assert ok is False  # rejected by the new syntax gate
        # Verify the file was NOT overwritten with broken code.
        assert (tmp_path / "backtest.py").read_text(encoding="utf-8").startswith('msg = "broken')

    def test_accepts_fix_with_valid_syntax(self, tmp_path: Path) -> None:
        entrypoint = self._write_entrypoint(
            tmp_path, 'msg = "broken\nprint(msg)\n'
        )

        class _GoodLLM:
            def invoke(self, _prompt: str) -> object:
                class _Result:
                    content = '```python\nmsg = "fixed"\nprint(msg)\n```'
                return _Result()

        ok = br._try_llm_fix(
            _GoodLLM(), str(tmp_path), entrypoint, "SyntaxError", "problem"
        )
        assert ok is True
        # File replaced with valid code.
        new_content = (tmp_path / "backtest.py").read_text(encoding="utf-8")
        assert "fixed" in new_content
        # And it now compiles.
        assert br._validate_python_syntax(new_content) is None

    def test_rejects_fix_identical_to_input(self, tmp_path: Path) -> None:
        # If the LLM returns the *same* code we already have, this would
        # burn a round with no progress; the helper short-circuits.
        original = "import os\nprint(os.getcwd())\n"
        entrypoint = self._write_entrypoint(tmp_path, original)

        class _LazyLLM:
            def invoke(self, _prompt: str) -> object:
                class _Result:
                    content = f"```python\n{original}```"
                return _Result()

        ok = br._try_llm_fix(
            _LazyLLM(), str(tmp_path), entrypoint, "irrelevant", "problem"
        )
        assert ok is False


class TestPurgeStaleResultFiles:
    """Verify pre-flight cleanup so a stale ``backtest_results.json``
    from a previous successful run cannot poison the failure-path
    parser of a later crashing run."""

    def test_purges_all_three_canonical_filenames(self, tmp_path: Path) -> None:
        for fname in ("backtest_results.json", "results.json", "output.json"):
            (tmp_path / fname).write_text('{"sharpe_ratio": 99.0}', encoding="utf-8")
        br._purge_stale_result_files(str(tmp_path))
        for fname in ("backtest_results.json", "results.json", "output.json"):
            assert not (tmp_path / fname).exists()

    def test_safe_when_no_files_exist(self, tmp_path: Path) -> None:
        # Should not raise on an empty directory.
        br._purge_stale_result_files(str(tmp_path))


class TestReadMetricsFromResultFile:
    """The failure branch consults this strict-gated reader instead of
    the regex parser so phantom metrics from a traceback cannot leak."""

    def test_returns_none_when_no_file(self, tmp_path: Path) -> None:
        assert br._read_metrics_from_result_file(str(tmp_path)) is None

    def test_returns_metrics_when_fresh(self, tmp_path: Path) -> None:
        import time
        anchor = time.time()
        # Sleep briefly so the new file's mtime is unambiguously after
        # the anchor (Windows file mtime resolution is ~10 ms).
        time.sleep(0.05)
        (tmp_path / "backtest_results.json").write_text(
            '{"sharpe_ratio": 1.5, "total_return_pct": 12.3}', encoding="utf-8"
        )
        result = br._read_metrics_from_result_file(
            str(tmp_path), written_after_wall=anchor
        )
        assert result is not None
        assert result.sharpe_ratio == 1.5
        assert result.total_return_pct == 12.3

    def test_returns_none_when_file_is_stale(self, tmp_path: Path) -> None:
        import time
        # Create the file FIRST, then capture an anchor in the future.
        (tmp_path / "backtest_results.json").write_text(
            '{"sharpe_ratio": 99.0}', encoding="utf-8"
        )
        time.sleep(0.05)
        future_anchor = time.time() + 60.0  # file mtime is well before
        result = br._read_metrics_from_result_file(
            str(tmp_path), written_after_wall=future_anchor
        )
        assert result is None  # stale file rejected

    def test_returns_none_when_no_metrics_in_dict(self, tmp_path: Path) -> None:
        # An empty JSON dict (no recognised metric fields) must be
        # treated as "no metrics" rather than a default-zero report.
        import time
        anchor = time.time()
        time.sleep(0.05)
        (tmp_path / "backtest_results.json").write_text(
            '{"unrelated_field": "yes"}', encoding="utf-8"
        )
        result = br._read_metrics_from_result_file(
            str(tmp_path), written_after_wall=anchor
        )
        assert result is None


class TestPreFlightSyntaxCheckShortCircuit:
    """The subprocess wrapper runs ``compile()`` over the entrypoint
    *before* spending the ~1 s startup cost on a Python interpreter
    that we already know will crash with SyntaxError."""

    def test_short_circuits_on_syntax_error(self, tmp_path: Path) -> None:
        ep = tmp_path / "backtest.py"
        ep.write_text('msg = "oops\nprint(msg)\n', encoding="utf-8")
        rc, out, err = br._run_backtest_subprocess(
            str(tmp_path), str(ep), timeout=10
        )
        assert rc == 1
        assert out == ""
        assert "SyntaxError" in err

    def test_runs_subprocess_when_syntax_clean(self, tmp_path: Path) -> None:
        ep = tmp_path / "backtest.py"
        ep.write_text('print("hello")\n', encoding="utf-8")
        rc, out, err = br._run_backtest_subprocess(
            str(tmp_path), str(ep), timeout=15
        )
        assert rc == 0
        assert "hello" in out


# ════════════════════════════════════════════════════════════════════════════
# Bug 2: .env DEBUG mode propagation
# ════════════════════════════════════════════════════════════════════════════


class TestDotenvLoaderConcurrencyAndIdempotence:
    """The new ``_load_dotenv_once`` helper must be safe to call from
    many threads without re-loading ``.env`` more than once, and must
    never leave ``_DOTENV_LOADED == True`` while ``load_dotenv`` is
    still in flight."""

    def test_idempotent_under_repeated_calls(self) -> None:
        # Reset the module's flag for a clean run.  Direct attribute
        # mutation is fine in test code; we restore the value at the
        # end so other tests see the production-state singleton.
        original = rtl._DOTENV_LOADED
        try:
            rtl._DOTENV_LOADED = False
            rtl._load_dotenv_once()
            first_state = rtl._DOTENV_LOADED
            rtl._load_dotenv_once()
            second_state = rtl._DOTENV_LOADED
            assert first_state is True
            assert second_state is True
        finally:
            rtl._DOTENV_LOADED = original

    def test_double_checked_lock_prevents_double_load(self) -> None:
        # Spin up many threads that all race to load — the lock must
        # serialise the first one through and short-circuit the rest.
        original = rtl._DOTENV_LOADED
        load_count = 0
        load_count_lock = threading.Lock()

        # Stub the actual python-dotenv import path so we can count
        # how many times the loader body executes its work.  We patch
        # the attribute the function imports lazily.
        original_isfile = os.path.isfile

        def _counting_isfile(path: str) -> bool:
            nonlocal load_count
            with load_count_lock:
                load_count += 1
            return original_isfile(path)

        try:
            rtl._DOTENV_LOADED = False
            with patch("os.path.isfile", side_effect=_counting_isfile):
                threads = [
                    threading.Thread(target=rtl._load_dotenv_once)
                    for _ in range(20)
                ]
                for t in threads:
                    t.start()
                for t in threads:
                    t.join(timeout=5.0)
            # The body should have run exactly once; subsequent threads
            # observed _DOTENV_LOADED == True at the outer fast-path
            # check or at the inner re-check inside the lock.  Allow up
            # to a small bound (≤ 2 candidate paths × 1 winning thread)
            # because the loader walks 2 candidate paths.
            assert load_count <= 2
        finally:
            rtl._DOTENV_LOADED = original

    def test_load_dotenv_called_at_module_import(self) -> None:
        # Sanity: the module-level call to _load_dotenv_once at import
        # time must have completed by the time tests run.
        assert rtl._DOTENV_LOADED is True


class TestDotenvLoaderToleratesMissingPackage:
    """When ``python-dotenv`` is not installed (the package is marked
    optional in requirements.txt), the loader must degrade silently
    rather than crashing the import chain."""

    def test_graceful_degradation_on_import_error(self) -> None:
        original = rtl._DOTENV_LOADED
        try:
            rtl._DOTENV_LOADED = False
            # Simulate "package not installed" by making the dynamic
            # import inside _load_dotenv_once raise.  We patch
            # builtins.__import__ to raise for ``dotenv`` only.
            real_import = __builtins__["__import__"] if isinstance(__builtins__, dict) else __builtins__.__import__

            def _fake_import(name: str, *args: object, **kwargs: object) -> object:
                if name == "dotenv":
                    raise ImportError("dotenv not installed (simulated)")
                return real_import(name, *args, **kwargs)  # type: ignore[arg-type]

            with patch("builtins.__import__", side_effect=_fake_import):
                # Must not raise.
                rtl._load_dotenv_once()
            # Flag still set so future calls fast-path.
            assert rtl._DOTENV_LOADED is True
        finally:
            rtl._DOTENV_LOADED = original


# ════════════════════════════════════════════════════════════════════════════
# Bug 3: WebUI agent-flow display
# ════════════════════════════════════════════════════════════════════════════


class TestAgentFlowEvMapHasCorrectMappings:
    """The frontend evMap must contain the agent-flow display fixes:
    - ``codegen_kickoff_done`` → ``codegen_phase_done`` (not
      ``self_check`` → ``active``)
    - ``direction_feedback_start`` → ``dir_judge`` → ``active``
    - ``codegen_phase_done`` state handler exists in the for-loop

    v1.0.3: the WebUI's <script> block was extracted to a sidecar file at
    ``webui/static/js/app.js``; this fixture now reads that file instead of
    ``index.html``.
    """

    @pytest.fixture(scope="class")
    def webui_js(self) -> str:
        path = (
            Path(__file__).resolve().parent.parent
            / "webui" / "static" / "js" / "app.js"
        )
        return path.read_text(encoding="utf-8")

    def test_codegen_kickoff_done_maps_to_phase_done(self, webui_js: str) -> None:
        # Old (buggy): /codegen_kickoff_done/i, 'self_check', 'active'
        # New: /codegen_kickoff_done/i, null, 'codegen_phase_done'
        assert "'codegen_phase_done'" in webui_js
        # The buggy mapping (kickoff_done → self_check active) must be gone.
        assert (
            "[/codegen_kickoff_done/i,                             'self_check', 'active'      ]"
            not in webui_js
        )

    def test_direction_feedback_start_mapped(self, webui_js: str) -> None:
        # The bug was that direction_feedback_start had no mapping at all.
        assert "/direction_feedback_start/i" in webui_js
        assert "/direction_feedback_failed/i" in webui_js

    def test_codegen_phase_done_state_handler_exists(self, webui_js: str) -> None:
        # The state handler block that closes stage-8 nodes and
        # activates self_check.
        assert "state === 'codegen_phase_done'" in webui_js


# ════════════════════════════════════════════════════════════════════════════
# Audit fix: Pearson r NaN propagation
# ════════════════════════════════════════════════════════════════════════════


class TestPearsonRNaNPropagation:
    """Both ``_pearson_r`` helpers used to clamp via
    ``max(-1.0, min(1.0, raw))``, which is order-sensitive in Python:
    ``min(1.0, nan) == 1.0`` but ``min(nan, 1.0) == nan``.  Now both
    explicitly check ``math.isfinite(raw)`` and short-circuit to 0.0
    for non-finite results."""

    def test_dynamic_correlation_handles_nan_input(self) -> None:
        x = [1.0, 2.0, float("nan"), 4.0, 5.0]
        y = [2.0, 4.0, 6.0, 8.0, 10.0]
        r = dc._pearson_r(x, y)
        # Must be finite and within [-1, 1].
        assert math.isfinite(r)
        assert -1.0 <= r <= 1.0

    def test_dynamic_correlation_handles_inf_input(self) -> None:
        x = [1.0, 2.0, float("inf"), 4.0, 5.0]
        y = [2.0, 4.0, 6.0, 8.0, 10.0]
        r = dc._pearson_r(x, y)
        assert math.isfinite(r)
        assert -1.0 <= r <= 1.0

    def test_dynamic_correlation_clean_input_still_works(self) -> None:
        x = [1.0, 2.0, 3.0, 4.0, 5.0]
        y = [2.0, 4.0, 6.0, 8.0, 10.0]
        r = dc._pearson_r(x, y)
        # Perfect positive correlation.
        assert abs(r - 1.0) < 1e-10

    def test_cointegration_handles_nan_input(self) -> None:
        x = [1.0, 2.0, float("nan"), 4.0, 5.0]
        y = [2.0, 4.0, 6.0, 8.0, 10.0]
        r = ca._pearson_r(x, y)
        assert math.isfinite(r)
        assert -1.0 <= r <= 1.0

    def test_cointegration_clean_input_still_works(self) -> None:
        x = [1.0, 2.0, 3.0, 4.0, 5.0]
        y = [2.0, 4.0, 6.0, 8.0, 10.0]
        r = ca._pearson_r(x, y)
        assert abs(r - 1.0) < 1e-10

    def test_cointegration_zero_variance_returns_zero(self) -> None:
        # Constant series → std == 0 → must return 0.0 cleanly.
        x = [3.0] * 10
        y = [1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 9.0, 10.0]
        r = ca._pearson_r(x, y)
        assert r == 0.0


# ════════════════════════════════════════════════════════════════════════════
# Smoke-test: end-to-end wiring of the new helpers
# ════════════════════════════════════════════════════════════════════════════


class TestEndToEndBacktestFailureLeavesCleanReport:
    """When the backtest crashes with a syntax error and no LLM is
    provided, the resulting ``BacktestReport`` must:
    - have ``success == False``
    - have a non-empty ``errors`` list
    - have ``baseline_metrics is None`` (no phantom metrics from
      regex-parsing the traceback)
    """

    def test_failed_run_has_no_phantom_metrics(self, tmp_path: Path) -> None:
        # Synthesise a minimal "run directory" layout that
        # ``run_backtest_pipeline`` can ingest.  ``code/backtest.py``
        # has the user-reported syntax error.
        run_dir = tmp_path / "run"
        code_dir = run_dir / "code"
        code_dir.mkdir(parents=True)
        # Use a known canonical entrypoint name from _find_backtest_entry.
        (code_dir / "backtest.py").write_text(
            'msg = "oops\nprint("Sharpe ratio: 9.99")\n',
            encoding="utf-8",
        )
        # Shim a tiny analysis_report.json so mode detection succeeds.
        (run_dir / "analysis_report.json").write_text(
            '{"mode_used": "quant", "summary": "test"}', encoding="utf-8"
        )
        # Provide a stub data file so prepare_data isn't called.
        data_dir = code_dir / "data"
        data_dir.mkdir()
        (data_dir / "sample.csv").write_text(
            "date,open,high,low,close,volume\n"
            "2024-01-01,1,1,1,1,1\n",
            encoding="utf-8",
        )
        report = br.run_backtest_pipeline(
            str(run_dir),
            llm=None,  # no fix loop
            timeout=10,
            fix_max_rounds=0,
        )
        assert report.success is False
        assert report.errors  # at least one error message
        # Phantom-metric guard: the traceback string contains
        # "Sharpe ratio: 9.99" but the strict result-file reader
        # ignores it.
        assert report.baseline_metrics is None
