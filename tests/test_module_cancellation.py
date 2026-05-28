"""
Regression tests: pipeline module functions must propagate OperationCancelledError
from kickoff_crew_with_retry rather than swallowing it.

Previously several ``except Exception`` handlers in the module files caught
OperationCancelledError and either:
- ``continue``d the retry loop (librarian research, direction debate)
- returned a fallback/None value (direction seed plan, LLM problem breakdown,
  smart search queries)
- ``break``ed out of the optimisation loop (auto-optimize critic)

This allowed the pipeline to keep running (or silently degrade) after the user
requested cooperative cancellation.

Test strategy
-------------
* **Source-code structure tests** — parse each fixed function body and assert
  that ``except _OperationCancelledError:`` appears immediately before the
  ``except Exception`` that wraps ``kickoff_crew_with_retry``.  This is the
  most reliable check because it does not depend on being able to exercise
  the full CrewAI pipeline in unit-test context.

* **Import propagation tests** — verify that ``_OperationCancelledError`` is
  available in all three module namespaces (sections 02, 04, 05) and resolves
  to the same class.
"""
from __future__ import annotations

import ast
import os
import sys
import textwrap

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest

_REPO_ROOT = os.path.normpath(os.path.join(os.path.dirname(__file__), ".."))

# ── helpers ───────────────────────────────────────────────────────────────────

def _module_source(rel_path: str) -> str:
    return open(os.path.join(_REPO_ROOT, rel_path), encoding="utf-8").read()


def _has_cancel_guard_before_except_exc(source: str, context_str: str) -> bool:
    """
    Return True when the source fragment contains the pattern:

        except _OperationCancelledError:
            raise
        [...optional re-raise / continue guards (e.g. _CooldownSkipError) ...]
        except Exception...

    i.e., the cancellation guard precedes a broad exception handler,
    possibly with other ``except X: raise`` guards stacked between them
    (v1.1.9 added ``except _CooldownSkipError: raise`` in section_04's
    ``_search_websearch`` for the H2 wire-in, which is structurally
    equivalent — neither swallows OperationCancelledError, so the
    cancellation contract is still satisfied).

    The check is performed on the *source* string, which must contain
    the relevant try/except block.  ``context_str`` is used only in
    assertion messages.
    """
    # Normalise whitespace to make pattern matching robust across indent levels
    lines = [ln.strip() for ln in source.splitlines()]
    for i, line in enumerate(lines):
        if line.startswith("except _OperationCancelledError"):
            # Next non-empty, non-comment line should be ``raise``
            j = i + 1
            while j < len(lines) and (not lines[j] or lines[j].startswith("#")):
                j += 1
            if j < len(lines) and lines[j] == "raise":
                # Scan forward past any intermediate ``except X: raise``
                # guards.  An ``except Exception`` clause anywhere in
                # the same try block satisfies the contract — the cancel
                # guard above it ensures cancellation is never swallowed.
                k = j + 1
                while k < len(lines):
                    # Skip blanks / comments
                    while k < len(lines) and (
                        not lines[k] or lines[k].startswith("#")
                    ):
                        k += 1
                    if k >= len(lines):
                        break
                    if lines[k].startswith("except Exception"):
                        return True
                    # If the next clause is another ``except X:`` whose body is
                    # a sole ``raise`` or ``continue``, accept it and keep
                    # scanning.  ``continue`` is safe too: it only fires for
                    # that clause's specific type (e.g. v1.1.11 added
                    # ``except _CooldownSkipError: continue`` in the section_04
                    # dispatcher to skip a cooled-down provider's lane) and
                    # never catches _OperationCancelledError, which the guard
                    # above already re-raised.
                    if lines[k].startswith("except "):
                        m = k + 1
                        while m < len(lines) and (
                            not lines[m] or lines[m].startswith("#")
                        ):
                            m += 1
                        if m < len(lines) and lines[m] in ("raise", "continue"):
                            k = m + 1
                            continue
                        # Some other body — give up on this site, the
                        # caller may match a later cancel guard in the
                        # function.
                        break
                    # Anything else terminates the chain.
                    break
    return False


def _extract_function_source(source: str, func_name: str) -> str:
    """
    Extract the source of the first function definition named *func_name*
    from *source* using the AST to find the line range, then return the raw
    text slice.
    """
    return _extract_nth_function_source(source, func_name, 0)


def _extract_nth_function_source(source: str, func_name: str, n: int) -> str:
    """
    Extract the source of the *n*-th (0-indexed) function definition named
    *func_name* from *source*.  Useful when a name is defined multiple times
    (e.g. a legacy alias assigned before a new definition that shadows it).
    Returns "" if fewer than n+1 definitions exist.
    """
    # ast.walk order is not top-to-bottom, so collect (lineno, body) pairs and sort.
    tree = ast.parse(source)
    lines = source.splitlines(keepends=True)
    matches_with_line: list = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name == func_name:
                end = getattr(node, "end_lineno", None)
                if end is None:
                    end = node.lineno + 200
                matches_with_line.append((node.lineno, "".join(lines[node.lineno - 1: end])))
    matches_with_line.sort(key=lambda t: t[0])
    if n < len(matches_with_line):
        return matches_with_line[n][1]
    return ""


# ── Import propagation ────────────────────────────────────────────────────────

class TestCancelledErrorPropagation:
    """Verify _OperationCancelledError is available in all module namespaces."""

    def test_section_01_has_cancel_error_via_sync(self):
        """section_01 gets _OperationCancelledError via _sync_module_namespaces() at runtime."""
        from crucible.modules import section_01_extraction_and_reformat as m
        from crucible.cancellation import OperationCancelledError
        assert hasattr(m, "_OperationCancelledError"), (
            "section_01_extraction_and_reformat must have _OperationCancelledError "
            "injected by module_runtime._sync_module_namespaces() so the except guard "
            "can reference it at runtime."
        )
        assert m._OperationCancelledError is OperationCancelledError

    def test_section_02_has_cancel_error(self):
        from crucible.modules import section_02_research_and_llm as m
        from crucible.cancellation import OperationCancelledError
        assert hasattr(m, "_OperationCancelledError"), (
            "section_02_research_and_llm must export _OperationCancelledError "
            "so the except guard can reference it."
        )
        assert m._OperationCancelledError is OperationCancelledError

    def test_section_04_has_cancel_error_via_globals_update(self):
        """section_04 gets _OperationCancelledError via globals().update(_prev_02.__dict__)."""
        from crucible.modules import section_04_web_research_and_direction as m
        from crucible.cancellation import OperationCancelledError
        assert hasattr(m, "_OperationCancelledError"), (
            "section_04_web_research_and_direction must inherit _OperationCancelledError "
            "from the globals().update(_prev_02.__dict__) chain."
        )
        assert m._OperationCancelledError is OperationCancelledError

    def test_section_05_has_cancel_error_via_globals_update(self):
        """section_05 gets _OperationCancelledError via globals().update() chain."""
        from crucible.modules import section_05_analysis_and_codegen as m
        from crucible.cancellation import OperationCancelledError
        assert hasattr(m, "_OperationCancelledError"), (
            "section_05_analysis_and_codegen must inherit _OperationCancelledError "
            "from the globals().update() chain (section_02 → section_04 → section_05)."
        )
        assert m._OperationCancelledError is OperationCancelledError


# ── Source-structure guards ───────────────────────────────────────────────────

class TestSection01GuardStructure:
    """
    Verify that the kickoff_crew_with_retry call site in section_01 has an
    ``except _OperationCancelledError: raise`` guard before ``except Exception``.
    """

    SOURCE = _module_source(
        "crucible/modules/section_01_extraction_and_reformat.py"
    )

    def test_kickoff_reformat_crew_has_cancel_guard(self):
        """
        _kickoff_reformat_crew() is the shared reformat helper called by every
        extraction crew in section_01.  Its ``except Exception as e: return None``
        previously swallowed OperationCancelledError, allowing the pipeline to
        continue running with a None result after the user cancelled.
        """
        body = _extract_function_source(self.SOURCE, "_kickoff_reformat_crew")
        assert body, "Could not find _kickoff_reformat_crew in source"
        assert _has_cancel_guard_before_except_exc(body, "_kickoff_reformat_crew"), (
            "_kickoff_reformat_crew: missing 'except _OperationCancelledError: raise' "
            "before 'except Exception' in the kickoff_crew_with_retry try block."
        )

    def test_run_schema_reformatter_has_cancel_guard(self):
        """
        _run_schema_reformatter() calls _kickoff_reformat_crew() which now
        re-raises OperationCancelledError.  Without a guard the caller's
        ``except Exception as e: return None`` would swallow it, hiding the
        cancellation and causing the caller to receive None as if parsing failed.
        """
        body = _extract_function_source(self.SOURCE, "_run_schema_reformatter")
        assert body, "Could not find _run_schema_reformatter in source"
        assert _has_cancel_guard_before_except_exc(body, "_run_schema_reformatter"), (
            "_run_schema_reformatter: missing 'except _OperationCancelledError: raise' "
            "before 'except Exception' in the _kickoff_reformat_crew try block."
        )

    def test_legacy_reformat_gate_decision_has_cancel_guard(self):
        """
        _legacy_reformat_gate_decision() calls _kickoff_reformat_crew().
        The cascading caller pattern: callee now re-raises, caller must guard.
        """
        body = _extract_function_source(self.SOURCE, "_legacy_reformat_gate_decision")
        assert body, "Could not find _legacy_reformat_gate_decision in source"
        assert _has_cancel_guard_before_except_exc(body, "_legacy_reformat_gate_decision"), (
            "_legacy_reformat_gate_decision: missing 'except _OperationCancelledError: raise' "
            "before 'except Exception' in the _kickoff_reformat_crew try block."
        )

    def test_legacy_reformat_analysis_report_has_cancel_guard(self):
        """
        _legacy_reformat_analysis_report() calls _kickoff_reformat_crew().
        The cascading caller pattern: callee now re-raises, caller must guard.
        """
        body = _extract_function_source(self.SOURCE, "_legacy_reformat_analysis_report")
        assert body, "Could not find _legacy_reformat_analysis_report in source"
        assert _has_cancel_guard_before_except_exc(body, "_legacy_reformat_analysis_report"), (
            "_legacy_reformat_analysis_report: missing 'except _OperationCancelledError: raise' "
            "before 'except Exception' in the _kickoff_reformat_crew try block."
        )

    def test_legacy_reformat_review_report_has_cancel_guard(self):
        """
        _legacy_reformat_review_report() calls _kickoff_reformat_crew().
        The cascading caller pattern: callee now re-raises, caller must guard.
        """
        body = _extract_function_source(self.SOURCE, "_legacy_reformat_review_report")
        assert body, "Could not find _legacy_reformat_review_report in source"
        assert _has_cancel_guard_before_except_exc(body, "_legacy_reformat_review_report"), (
            "_legacy_reformat_review_report: missing 'except _OperationCancelledError: raise' "
            "before 'except Exception' in the _kickoff_reformat_crew try block."
        )

    def test_legacy_reformat_code_bundle_has_cancel_guard(self):
        """
        _legacy_reformat_code_bundle() calls _kickoff_reformat_crew().
        The cascading caller pattern: callee now re-raises, caller must guard.
        """
        body = _extract_function_source(self.SOURCE, "_legacy_reformat_code_bundle")
        assert body, "Could not find _legacy_reformat_code_bundle in source"
        assert _has_cancel_guard_before_except_exc(body, "_legacy_reformat_code_bundle"), (
            "_legacy_reformat_code_bundle: missing 'except _OperationCancelledError: raise' "
            "before 'except Exception' in the _kickoff_reformat_crew try block."
        )

    def test_legacy_reformat_research_context_has_cancel_guard(self):
        """
        _legacy_reformat_research_context() calls _kickoff_reformat_crew().
        The cascading caller pattern: callee now re-raises, caller must guard.
        """
        body = _extract_function_source(self.SOURCE, "_legacy_reformat_research_context")
        assert body, "Could not find _legacy_reformat_research_context in source"
        assert _has_cancel_guard_before_except_exc(body, "_legacy_reformat_research_context"), (
            "_legacy_reformat_research_context: missing 'except _OperationCancelledError: raise' "
            "before 'except Exception' in the _kickoff_reformat_crew try block."
        )

    def test_legacy_reformat_direction_decision_has_cancel_guard(self):
        """
        _legacy_reformat_direction_decision() calls _kickoff_reformat_crew().
        The cascading caller pattern: callee now re-raises, caller must guard.
        """
        body = _extract_function_source(self.SOURCE, "_legacy_reformat_direction_decision")
        assert body, "Could not find _legacy_reformat_direction_decision in source"
        assert _has_cancel_guard_before_except_exc(body, "_legacy_reformat_direction_decision"), (
            "_legacy_reformat_direction_decision: missing 'except _OperationCancelledError: raise' "
            "before 'except Exception' in the _kickoff_reformat_crew try block."
        )


class TestSection02GuardStructure:
    """
    Verify that each kickoff_crew_with_retry call site in section_02 has an
    ``except _OperationCancelledError: raise`` guard before ``except Exception``.
    """

    SOURCE = _module_source(
        "crucible/modules/section_02_research_and_llm.py"
    )

    def test_run_librarian_research_has_cancel_guard(self):
        """
        run_librarian_research() retry loop must not continue on cancellation.
        Previously ``except Exception as exc: ... continue`` swallowed it.
        """
        body = _extract_function_source(self.SOURCE, "run_librarian_research")
        assert body, "Could not find run_librarian_research in source"
        assert _has_cancel_guard_before_except_exc(body, "run_librarian_research"), (
            "run_librarian_research: missing 'except _OperationCancelledError: raise' "
            "before 'except Exception' in the kickoff_crew_with_retry try block."
        )

    def test_run_single_direction_debate_has_cancel_guard(self):
        """
        _run_single_direction_debate() calls kickoff_crew_with_retry() and is
        called in a retry loop from the direction debate flow.  Previously
        ``except Exception as e: ... continue`` swallowed OperationCancelledError.
        """
        body = _extract_function_source(self.SOURCE, "_run_single_direction_debate")
        assert body, "Could not find _run_single_direction_debate in source"
        assert _has_cancel_guard_before_except_exc(body, "_run_single_direction_debate"), (
            "_run_single_direction_debate: missing 'except _OperationCancelledError: raise' "
            "before 'except Exception' in the kickoff_crew_with_retry try block."
        )

    def test_build_direction_seed_plan_has_cancel_guard(self):
        """
        _build_direction_seed_plan() must not return a fallback on cancellation.
        Previously ``except Exception as exc: return _fallback_...`` swallowed it.
        """
        body = _extract_function_source(self.SOURCE, "_build_direction_seed_plan")
        assert body, "Could not find _build_direction_seed_plan in source"
        assert _has_cancel_guard_before_except_exc(body, "_build_direction_seed_plan"), (
            "_build_direction_seed_plan: missing 'except _OperationCancelledError: raise' "
            "before 'except Exception' in the kickoff_crew_with_retry try block."
        )


class TestSection04GuardStructure:
    """
    Verify that each kickoff_crew_with_retry call site in section_04 has an
    ``except _OperationCancelledError: raise`` guard, and that HTTP search
    helper functions using safe_http_text/safe_http_json also propagate
    cancellation rather than silently continuing.
    """

    SOURCE = _module_source(
        "crucible/modules/section_04_web_research_and_direction.py"
    )

    def test_build_llm_problem_breakdown_has_cancel_guard(self):
        """
        _build_llm_problem_breakdown() must not return None on cancellation.
        Previously ``except Exception as e: return None`` swallowed it.
        """
        body = _extract_function_source(
            self.SOURCE, "_build_llm_problem_breakdown"
        )
        assert body, "Could not find _build_llm_problem_breakdown in source"
        assert _has_cancel_guard_before_except_exc(
            body, "_build_llm_problem_breakdown"
        ), (
            "_build_llm_problem_breakdown: missing 'except _OperationCancelledError: raise' "
            "before 'except Exception' in the kickoff_crew_with_retry try block."
        )

    def test_build_smart_search_queries_has_cancel_guard(self):
        """
        _build_smart_search_queries() must not return None on cancellation.
        Previously ``except Exception as e: return None`` swallowed it.
        """
        body = _extract_function_source(
            self.SOURCE, "_build_smart_search_queries"
        )
        assert body, "Could not find _build_smart_search_queries in source"
        assert _has_cancel_guard_before_except_exc(
            body, "_build_smart_search_queries"
        ), (
            "_build_smart_search_queries: missing 'except _OperationCancelledError: raise' "
            "before 'except Exception' in the kickoff_crew_with_retry try block."
        )

    def test_fetch_citation_excerpt_has_cancel_guard(self):
        """
        _fetch_citation_excerpt() calls _safe_http_text() which delegates to
        execute_with_retry() → raise_if_cancelled().  The surrounding
        ``except Exception: return ""`` would swallow OperationCancelledError.
        """
        body = _extract_function_source(
            self.SOURCE, "_fetch_citation_excerpt"
        )
        assert body, "Could not find _fetch_citation_excerpt in source"
        assert _has_cancel_guard_before_except_exc(
            body, "_fetch_citation_excerpt"
        ), (
            "_fetch_citation_excerpt: missing 'except _OperationCancelledError: raise' "
            "before 'except Exception' in the _safe_http_text try block."
        )

    def test_search_websearch_has_cancel_guard(self):
        """
        _search_websearch() calls _safe_http_text() for the primary DDG URL.
        The surrounding ``except Exception: pass`` would swallow
        OperationCancelledError and fall through to the lite-URL fallback.
        """
        body = _extract_function_source(
            self.SOURCE, "_search_websearch"
        )
        assert body, "Could not find _search_websearch in source"
        assert _has_cancel_guard_before_except_exc(
            body, "_search_websearch"
        ), (
            "_search_websearch: missing 'except _OperationCancelledError: raise' "
            "before 'except Exception' in the primary _safe_http_text try block."
        )

    def test_search_github_has_cancel_guard(self):
        """
        _search_github() calls _search_github_code() which internally calls
        _safe_http_json() → execute_with_retry() → raise_if_cancelled().
        The surrounding ``except Exception: code_hits = []`` would swallow it.
        """
        body = _extract_function_source(
            self.SOURCE, "_search_github"
        )
        assert body, "Could not find _search_github in source"
        assert _has_cancel_guard_before_except_exc(
            body, "_search_github"
        ), (
            "_search_github: missing 'except _OperationCancelledError: raise' "
            "before 'except Exception' in the _search_github_code try block."
        )

    def test_collect_librarian_search_materials_has_cancel_guard(self):
        """
        _collect_librarian_search_materials() loops over providers/queries;
        each iteration calls HTTP search functions.  The per-query
        ``except Exception as exc: ... continue`` would swallow
        OperationCancelledError and continue the search loop.
        """
        body = _extract_function_source(
            self.SOURCE, "_collect_librarian_search_materials"
        )
        assert body, "Could not find _collect_librarian_search_materials in source"
        assert _has_cancel_guard_before_except_exc(
            body, "_collect_librarian_search_materials"
        ), (
            "_collect_librarian_search_materials: missing "
            "'except _OperationCancelledError: raise' before 'except Exception' "
            "in the provider/query dispatch try block."
        )


class TestSection05GuardStructure:
    """
    Verify that all kickoff_crew_with_retry call sites in section_05 have an
    ``except _OperationCancelledError: raise`` guard before ``except Exception``.
    """

    SOURCE = _module_source(
        "crucible/modules/section_05_analysis_and_codegen.py"
    )

    def test_run_codegen_auto_optimize_critic_has_cancel_guard(self):
        """
        run_codegen_auto_optimize() critic loop must not break on cancellation.
        Previously ``except Exception as critic_exc: ... break`` swallowed it,
        silently completing optimisation with partial results.
        """
        body = _extract_function_source(self.SOURCE, "run_codegen_auto_optimize")
        assert body, "Could not find run_codegen_auto_optimize in source"
        assert _has_cancel_guard_before_except_exc(
            body, "run_codegen_auto_optimize"
        ), (
            "run_codegen_auto_optimize: missing 'except _OperationCancelledError: raise' "
            "before 'except Exception' in the critic kickoff_crew_with_retry try block."
        )

    def test_run_analysis_with_selective_rerun_has_cancel_guard(self):
        """
        run_analysis_with_selective_rerun() analysis kickoff: previously
        ``except Exception as e`` logged cancellation as 'Analysis crew kickoff failed',
        recorded a failed cost entry, and wrote a failed snapshot stage before re-raising,
        misclassifying a cooperative cancellation as an analysis failure.
        """
        body = _extract_function_source(
            self.SOURCE, "run_analysis_with_selective_rerun"
        )
        assert body, "Could not find run_analysis_with_selective_rerun in source"
        assert _has_cancel_guard_before_except_exc(
            body, "run_analysis_with_selective_rerun"
        ), (
            "run_analysis_with_selective_rerun: missing "
            "'except _OperationCancelledError: raise' before 'except Exception' "
            "in the kickoff_crew_with_retry try block."
        )

    def test_kickoff_codegen_with_timeout_recovery_has_cancel_guard(self):
        """
        _kickoff_codegen_with_timeout_recovery() must not attempt the fallback crew
        on cancellation.  Previously relied on is_transient_retryable_error returning
        False for OperationCancelledError (correct, but implicit); guard makes it
        explicit and defends against future changes to the transient classifier.
        """
        body = _extract_function_source(
            self.SOURCE, "_kickoff_codegen_with_timeout_recovery"
        )
        assert body, "Could not find _kickoff_codegen_with_timeout_recovery in source"
        assert _has_cancel_guard_before_except_exc(
            body, "_kickoff_codegen_with_timeout_recovery"
        ), (
            "_kickoff_codegen_with_timeout_recovery: missing "
            "'except _OperationCancelledError: raise' before 'except Exception' "
            "in the kickoff_crew_with_retry try block."
        )

    def test_kickoff_codegen_substage_with_recovery_has_cancel_guard(self):
        """
        _kickoff_codegen_substage_with_recovery() must not attempt the fallback substage
        on cancellation.  Same defensive-explicit-guard rationale as the timeout-recovery
        counterpart above.
        """
        body = _extract_function_source(
            self.SOURCE, "_kickoff_codegen_substage_with_recovery"
        )
        assert body, "Could not find _kickoff_codegen_substage_with_recovery in source"
        assert _has_cancel_guard_before_except_exc(
            body, "_kickoff_codegen_substage_with_recovery"
        ), (
            "_kickoff_codegen_substage_with_recovery: missing "
            "'except _OperationCancelledError: raise' before 'except Exception' "
            "in the kickoff_crew_with_retry try block."
        )

    def test_run_analysis_with_selective_rerun_direction_feedback_has_cancel_guard(self):
        """
        run_analysis_with_selective_rerun() direction-feedback inner block:
        ``run_direction_debate()`` does not catch exceptions internally, so any
        OperationCancelledError raised in its call chain (e.g. from
        _build_direction_seed_plan, run_librarian_research,
        _run_single_direction_debate) propagates out.  The surrounding
        ``except Exception as exc: log_exception(...); print(...)`` in the
        feedback try-block previously swallowed it, allowing the pipeline to
        continue the analysis rerun loop after the user cancelled.
        """
        body = _extract_function_source(
            self.SOURCE, "run_analysis_with_selective_rerun"
        )
        assert body, "Could not find run_analysis_with_selective_rerun in source"
        # The function contains multiple except Exception blocks (outer kickoff +
        # direction feedback).  _has_cancel_guard_before_except_exc returns True if
        # ANY guard/except-Exception pair exists.  The outer analysis kickoff guard
        # is tested by test_run_analysis_with_selective_rerun_has_cancel_guard.
        # Here we verify the direction-feedback guard exists by checking the count
        # of "except _OperationCancelledError" occurrences matches expectations:
        # at least 2 guards should be present (outer kickoff + direction feedback).
        stripped = [ln.strip() for ln in body.splitlines()]
        guard_count = sum(
            1 for ln in stripped if ln.startswith("except _OperationCancelledError")
        )
        assert guard_count >= 2, (
            f"run_analysis_with_selective_rerun: expected at least 2 "
            f"'except _OperationCancelledError' guards (outer analysis kickoff + "
            f"direction feedback loop) but found {guard_count}.  The direction "
            f"feedback try-block wrapping run_direction_debate() is missing its guard."
        )

    def test_run_codegen_stage_legacy_has_cancel_guard(self):
        """
        The first (legacy) definition of run_codegen_stage() — captured as
        _LEGACY_RUN_CODEGEN_STAGE — calls _kickoff_codegen_with_timeout_recovery()
        which re-raises OperationCancelledError.  Without a guard the caller's
        ``except Exception as e: return None, None`` would swallow it.
        """
        body = _extract_nth_function_source(self.SOURCE, "run_codegen_stage", 0)
        assert body, "Could not find first run_codegen_stage definition in source"
        assert _has_cancel_guard_before_except_exc(body, "run_codegen_stage[0]"), (
            "run_codegen_stage (legacy/first definition): missing "
            "'except _OperationCancelledError: raise' before 'except Exception' "
            "in the _kickoff_codegen_with_timeout_recovery try block."
        )

    def test_run_codegen_stage_staged_has_cancel_guard(self):
        """
        The second (staged) definition of run_codegen_stage() — the actual runtime
        function since it shadows the first — calls _run_staged_codegen_pipeline()
        which internally calls _kickoff_codegen_substage_with_recovery() and can
        re-raise OperationCancelledError.  Without a guard, ``except Exception as e:
        return None, None`` on the staged path would swallow the cancellation.
        This definition is selected at runtime when CODEGEN_STAGED_ENABLED is True.
        """
        body = _extract_nth_function_source(self.SOURCE, "run_codegen_stage", 1)
        assert body, "Could not find second (staged) run_codegen_stage definition in source"
        assert _has_cancel_guard_before_except_exc(body, "run_codegen_stage[1]"), (
            "run_codegen_stage (staged/second definition): missing "
            "'except _OperationCancelledError: raise' before 'except Exception' "
            "in the _run_staged_codegen_pipeline try block."
        )


class TestSection06GuardStructure:
    """
    Verify that each kickoff_crew_with_retry call site in section_06 has an
    ``except _OperationCancelledError: raise`` guard.
    """

    SOURCE = _module_source(
        "crucible/modules/section_06_runtime_quality_api.py"
    )

    def test_run_quality_review_has_cancel_guard(self):
        """
        run_quality_review() retry loop: ``except Exception as e: ... continue``
        previously swallowed OperationCancelledError and kept retrying.
        """
        body = _extract_function_source(self.SOURCE, "run_quality_review")
        assert body, "Could not find run_quality_review in source"
        assert _has_cancel_guard_before_except_exc(body, "run_quality_review"), (
            "run_quality_review: missing 'except _OperationCancelledError: raise' "
            "before 'except Exception' in the kickoff_crew_with_retry try block."
        )

    def test_run_quality_fix_has_cancel_guard(self):
        """
        run_quality_fix() retry loop: ``except Exception as e: ... continue``
        previously swallowed OperationCancelledError and kept retrying,
        possibly also entering patch-recovery logic on a cancelled request.
        """
        body = _extract_function_source(self.SOURCE, "run_quality_fix")
        assert body, "Could not find run_quality_fix in source"
        assert _has_cancel_guard_before_except_exc(body, "run_quality_fix"), (
            "run_quality_fix: missing 'except _OperationCancelledError: raise' "
            "before 'except Exception' in the kickoff_crew_with_retry try block."
        )

    def test_analyze_code_for_deprecated_apis_has_cancel_guard(self):
        """
        _analyze_code_for_deprecated_apis() outer try: ``except Exception as e``
        previously swallowed OperationCancelledError and returned [] as if the
        analysis produced no deprecated-API findings.
        """
        body = _extract_function_source(
            self.SOURCE, "_analyze_code_for_deprecated_apis"
        )
        assert body, "Could not find _analyze_code_for_deprecated_apis in source"
        assert _has_cancel_guard_before_except_exc(
            body, "_analyze_code_for_deprecated_apis"
        ), (
            "_analyze_code_for_deprecated_apis: missing "
            "'except _OperationCancelledError: raise' before 'except Exception' "
            "in the outer kickoff_crew_with_retry try block."
        )

    def test_search_ccxt_official_sources_has_cancel_guard(self):
        """
        _search_ccxt_official_sources() search loop calls _search_websearch()
        which internally guards OperationCancelledError and re-raises it.  The
        surrounding ``except Exception: continue`` previously swallowed the
        cancellation and continued the query loop, wasting time and hiding the
        cancellation from the caller (run_api_version_check).
        """
        body = _extract_function_source(
            self.SOURCE, "_search_ccxt_official_sources"
        )
        assert body, "Could not find _search_ccxt_official_sources in source"
        assert _has_cancel_guard_before_except_exc(
            body, "_search_ccxt_official_sources"
        ), (
            "_search_ccxt_official_sources: missing "
            "'except _OperationCancelledError: raise' before 'except Exception' "
            "in the _search_websearch try block."
        )

    def test_search_library_latest_version_has_cancel_guard(self):
        """
        _search_library_latest_version() search loop calls _search_websearch()
        which re-raises OperationCancelledError.  The surrounding
        ``except Exception: continue`` previously swallowed the cancellation and
        continued the multi-query loop, allowing API version checks to proceed
        after the user cancelled the pipeline run.
        """
        body = _extract_function_source(
            self.SOURCE, "_search_library_latest_version"
        )
        assert body, "Could not find _search_library_latest_version in source"
        assert _has_cancel_guard_before_except_exc(
            body, "_search_library_latest_version"
        ), (
            "_search_library_latest_version: missing "
            "'except _OperationCancelledError: raise' before 'except Exception' "
            "in the _search_websearch try block."
        )

    def test_maybe_run_api_version_check_has_cancel_guard(self):
        """
        _maybe_run_api_version_check() wraps run_api_version_check() which calls
        through the search and analysis stack; cancellation propagates back up.
        The surrounding ``except Exception as api_check_err: return None``
        previously swallowed OperationCancelledError, silently returning None
        and allowing the caller to treat the check as if it completed without
        results, rather than propagating the cancellation.
        """
        body = _extract_function_source(
            self.SOURCE, "_maybe_run_api_version_check"
        )
        assert body, "Could not find _maybe_run_api_version_check in source"
        assert _has_cancel_guard_before_except_exc(
            body, "_maybe_run_api_version_check"
        ), (
            "_maybe_run_api_version_check: missing "
            "'except _OperationCancelledError: raise' before 'except Exception' "
            "in the run_api_version_check try block."
        )


class TestSection07GuardStructure:
    """
    Verify that kickoff_crew_with_retry call sites in section_07 (the main
    pipeline entry point) have proper cancellation guards.
    """

    SOURCE = _module_source(
        "crucible/modules/section_07_selfcheck_output_main.py"
    )

    def test_main_project_fix_kickoff_has_cancel_guard(self):
        """
        main() project-fix kickoff: ``except Exception as e`` previously logged
        OperationCancelledError as 'Project fix crew failed' before re-raising,
        misclassifying a cooperative cancellation as a pipeline failure.
        """
        body = _extract_function_source(self.SOURCE, "main")
        assert body, "Could not find main in source"
        assert _has_cancel_guard_before_except_exc(body, "main"), (
            "main: missing 'except _OperationCancelledError: raise' before "
            "'except Exception' in the project_fix kickoff_crew_with_retry try block."
        )
