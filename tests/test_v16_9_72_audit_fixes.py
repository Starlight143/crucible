"""Regression tests for v16.9.72 four-agent log-audit fixes.

Five distinct bugs surfaced by parallel audit of a 5607-line production log:

1. **Pydantic checkpoint serialisation warning** — both
   ``_research_task_callback`` and ``_analyst_task_callback`` were
   defined as nested closures inside their builder functions.  Pydantic
   (via CrewAI's model layer) refuses to pickle closures, emitting
   ``UserWarning: function callbacks cannot be serialized and will
   prevent checkpointing`` on every crew kickoff.  Both are now
   module-level functions with stable ``__qualname__``.

2. **DEBUG-level third-party logger flood + stdout/stderr interleaving**
   — the production log contained 71 ``DEBUG asyncio: Using proactor:
   IocpProactor`` lines plus hundreds of ``DEBUG httpcore.http11``
   entries that raced with CrewAI's verbose ``Printer`` (the
   ``┌──── 🤖 Agent Started ────┐`` box-drawing output ended up
   physically interleaved with DEBUG records, producing corrupted lines
   like ``┌─2026-04-27T... DEBUG openai._base_client: Sending HTTP
   Request``).  ``runtime_logging.configure_logging`` now pins the noisy
   loggers to WARNING.

3. **``_should_force_direction_none`` second branch**
   (``max(scores) <= 12 AND grounded_claims < 3``) — the same
   under-counting problem fixed in v16.9.71 also affected this branch.
   Now also defers to grounded_summary_claims and claim_attribution
   counts.

4. **``provider_errors`` injected into ``key_risks``** —
   ``_build_fallback_research_context`` previously dumped raw HTTP error
   strings (``Client error '429 Too Many Requests'…``, ``Circuit
   breaker '…' is open``) into the ``key_risks`` field, which every
   downstream debate agent reads as a product-level risk.  Now ``[]``;
   ``provider_errors`` is preserved as its own field for observability.

5. **``_search_github_repositories`` missing token guard** —
   anonymous ``/search/repositories`` quota is 10 req/hr, easily
   exhausted by a single librarian run.  Now mirrors the guard already
   present in ``_search_github_code``: returns ``[]`` when no GitHub
   token is configured.
"""

from __future__ import annotations

import logging
import pickle
from unittest.mock import patch

import pytest

from crucible.modules import (
    section_04_web_research_and_direction as s4,
    section_05_analysis_and_codegen as s5,
)
from crucible.modules.section_03_models_and_context import (
    DirectionDecision,
    DirectionOption,
    ResearchCitation,
    ResearchContext,
)
from crucible.modules.section_04_web_research_and_direction import (
    _build_fallback_research_context,
    _research_task_callback,
    _search_github_repositories,
    _should_force_direction_none,
)
from crucible.modules.section_05_analysis_and_codegen import (
    _analyst_task_callback,
)
from crucible.runtime_logging import (
    _NOISY_THIRD_PARTY_LOGGERS,
    configure_logging,
)


# ─────────────────────────────────────────────────────────────────────
# 1) Module-level task_callbacks (pydantic checkpoint serialisation)
# ─────────────────────────────────────────────────────────────────────


class TestTaskCallbacksModuleLevel:
    def test_research_callback_is_module_level(self):
        # `__qualname__` of a closure is "<outer>.<inner>"; module-level
        # functions have just "<inner>".
        assert _research_task_callback.__qualname__ == "_research_task_callback", (
            "Closure form re-introduced — pydantic will warn about checkpoint "
            "serialisation"
        )
        # The exported attribute must point at the same function used by
        # build_research_swarm_crew.
        assert s4._research_task_callback is _research_task_callback

    def test_analyst_callback_is_module_level(self):
        assert _analyst_task_callback.__qualname__ == "_analyst_task_callback"
        assert s5._analyst_task_callback is _analyst_task_callback

    def test_research_callback_is_picklable(self):
        # Pydantic's checkpoint path uses pickle/dill; closures fail this.
        pickled = pickle.dumps(_research_task_callback)
        restored = pickle.loads(pickled)
        assert restored is _research_task_callback

    def test_analyst_callback_is_picklable(self):
        pickled = pickle.dumps(_analyst_task_callback)
        restored = pickle.loads(pickled)
        assert restored is _analyst_task_callback

    def test_research_callback_swallows_exceptions(self):
        # Per spec: "Never let the callback break the crew run."
        class _BadOutput:
            @property
            def name(self):
                raise RuntimeError("simulated CrewAI corruption")

            @property
            def description(self):
                raise RuntimeError("simulated CrewAI corruption")

        # Must NOT raise.
        _research_task_callback(_BadOutput())


# ─────────────────────────────────────────────────────────────────────
# 2) Noisy third-party logger silencer
# ─────────────────────────────────────────────────────────────────────


class TestNoisyLoggerSilencer:
    def test_asyncio_logger_pinned_to_warning(self):
        # Simulate CrewAI / litellm calling logging.basicConfig(level=DEBUG).
        logging.getLogger("asyncio").setLevel(logging.DEBUG)
        configure_logging(force=True)
        assert logging.getLogger("asyncio").level == logging.WARNING

    def test_all_noisy_loggers_pinned(self):
        for name in _NOISY_THIRD_PARTY_LOGGERS:
            logging.getLogger(name).setLevel(logging.DEBUG)
        configure_logging(force=True)
        for name in _NOISY_THIRD_PARTY_LOGGERS:
            assert logging.getLogger(name).level == logging.WARNING, (
                f"{name} should be pinned to WARNING, "
                f"got {logging.getLevelName(logging.getLogger(name).level)}"
            )

    def test_explicit_warning_or_above_not_lowered(self):
        # If an operator already set a logger to ERROR, we must not
        # *lower* it to WARNING.
        logging.getLogger("httpcore").setLevel(logging.ERROR)
        configure_logging(force=True)
        assert logging.getLogger("httpcore").level == logging.ERROR

    def test_crucible_quiet_thirdparty_off_disables_silencer(self, monkeypatch):
        monkeypatch.setenv("CRUCIBLE_QUIET_THIRDPARTY", "0")
        logging.getLogger("asyncio").setLevel(logging.DEBUG)
        configure_logging(force=True)
        assert logging.getLogger("asyncio").level == logging.DEBUG

    def teardown_method(self, method):
        # Reset for the next test class — leave loggers in a clean state.
        for name in _NOISY_THIRD_PARTY_LOGGERS:
            logging.getLogger(name).setLevel(logging.NOTSET)


# ─────────────────────────────────────────────────────────────────────
# 3) _should_force_direction_none — second branch defence-in-depth
# ─────────────────────────────────────────────────────────────────────


class TestForceNoneSecondBranch:
    def _decision(self) -> DirectionDecision:
        opts = [
            DirectionOption(
                key=k,
                name=f"Direction {k}",
                thesis=f"Thesis for {k}",
                primary_metric="metric",
                fastest_test="test",
                major_risk="risk",
            )
            for k in "ABCDEFG"
        ]
        return DirectionDecision(
            selected_direction="A",
            options=opts,
            backup_candidates=[],
            go_conditions=["go"],
            kill_criteria=["kill"],
            verify_plan=["verify"],
            confidence="medium",
            summary="…",
        )

    def _ctx(
        self,
        *,
        citations: int,
        grounded_claims: int,
        grounded_summary_claims: int = 0,
        claim_attributions: int = 0,
    ) -> ResearchContext:
        from crucible.modules.section_03_models_and_context import (
            ClaimAttribution,
        )

        return ResearchContext(
            user_problem="x",
            search_strategy="x",
            providers_used=["websearch"],
            suggested_search_queries=[],
            citations=[
                ResearchCitation(
                    title=f"c{i}",
                    snippet="s",
                    url=f"https://example.com/{i}",
                    provider="websearch",
                    evidence_type="news",
                    verification_status="unverified",
                )
                for i in range(citations)
            ],
            market_examples=[],
            existing_tools=[],
            technical_patterns=[],
            key_risks=[],
            unknowns=[],
            hallucination_flags=[],
            claim_attributions=[
                ClaimAttribution(
                    category="technical_patterns",
                    claim=f"claim-{i}",
                    citation_indices=[0],
                    citation_urls=[],
                    support_score=1,
                )
                for i in range(claim_attributions)
            ],
            evidence_coverage={
                "citations": citations,
                "grounded_claims": grounded_claims,
                "grounded_summary_claims": grounded_summary_claims,
            },
            synthesized_summary="",
            provider_errors={},
        )

    def _make_audit_with_low_scores(self):
        # Build comparator/audit with composite scores <= 12 to trigger
        # the second branch.
        from crucible.modules.section_03_models_and_context import (
            DirectionComparatorReport,
            DirectionComparatorItem,
            EvidenceAuditReport,
            EvidenceAuditItem,
        )

        # The structured score formula in
        # `_structured_direction_option_score` is roughly
        # ``composite_score*3 + evidence_score*4 - unsupported_count*5``.
        # Pick composite=2, evidence=1, unsupported=0 → score = 10
        # (>0 so the "no defendable structured support" early-return
        # branch is skipped, but ≤ 12 so the second-branch
        # ``max(scores) <= 12`` predicate fires).
        comparator = DirectionComparatorReport(
            items=[
                DirectionComparatorItem(
                    key=k,
                    composite_score=2,
                    rationale="…",
                )
                for k in "ABC"
            ],
            top_keys=["A", "B", "C"],
            global_warnings=[],
        )
        audit = EvidenceAuditReport(
            items=[
                EvidenceAuditItem(
                    key=k,
                    evidence_score=1,
                    supported_fields=["thesis"],
                    summary_only_fields=[],
                    unsupported_fields=[],
                    unsupported_count=0,
                    decision_critical_unknowns=[],
                )
                for k in "ABC"
            ],
            top_keys=["A", "B", "C"],
            global_warnings=[],
        )
        return comparator, audit

    def test_weak_scores_with_attributions_does_not_force_none(self):
        comparator, audit = self._make_audit_with_low_scores()
        # 5 citations, 0 grounded_claims (the v16.9.71 path already
        # handles citations>=3), but ALSO 4 claim_attributions emitted by
        # the synthesizer's structured path.  Pre-fix, the second branch
        # still fired because grounded_claims < 3.  Post-fix, we look at
        # the whole structured-evidence picture.
        ctx = self._ctx(
            citations=5,
            grounded_claims=0,
            grounded_summary_claims=0,
            claim_attributions=4,
        )
        force_none, _, _ = _should_force_direction_none(
            self._decision(),
            research_context=ctx,
            comparator_report=comparator,
            audit_report=audit,
        )
        assert force_none is False

    def test_weak_scores_with_summary_claims_does_not_force_none(self):
        comparator, audit = self._make_audit_with_low_scores()
        ctx = self._ctx(
            citations=5,
            grounded_claims=0,
            grounded_summary_claims=3,
            claim_attributions=0,
        )
        force_none, _, _ = _should_force_direction_none(
            self._decision(),
            research_context=ctx,
            comparator_report=comparator,
            audit_report=audit,
        )
        assert force_none is False

    def test_weak_scores_with_no_evidence_at_all_still_forces_none(self):
        # Original safety property preserved: weak scores AND zero of
        # every signal still force "none".
        comparator, audit = self._make_audit_with_low_scores()
        ctx = self._ctx(
            citations=5,
            grounded_claims=0,
            grounded_summary_claims=0,
            claim_attributions=0,
        )
        force_none, reason, _ = _should_force_direction_none(
            self._decision(),
            research_context=ctx,
            comparator_report=comparator,
            audit_report=audit,
        )
        assert force_none is True
        assert "weakly supported" in reason


# ─────────────────────────────────────────────────────────────────────
# 4) _build_fallback_research_context — provider_errors not in key_risks
# ─────────────────────────────────────────────────────────────────────


class TestProviderErrorsNotInKeyRisks:
    def test_key_risks_is_empty_in_fallback(self):
        materials = {
            "search_strategy": "x",
            "providers_used": ["websearch"],
            "suggested_search_queries": [],
            "provider_errors": {
                "grep_app": (
                    "[market] query='crypto regime switching strategy "
                    "performance backtest validation' "
                    "error=Client error '429 Too Many Requests'"
                ),
                "github": (
                    "[competitor] query='site:github.com crypto bot' "
                    "error=Circuit breaker is open after 3 failures."
                ),
            },
            "citations": [],
        }
        ctx = _build_fallback_research_context(
            user_problem="x",
            materials=materials,
        )
        # The fallback used to dump these HTTP error strings into key_risks
        # and they would be read verbatim by every downstream debate agent.
        assert ctx.key_risks == []
        # provider_errors is preserved as its own field for observability.
        assert "grep_app" in (ctx.provider_errors or {})
        assert "github" in (ctx.provider_errors or {})

    def test_key_risks_string_contents_were_problematic(self):
        # Sanity check: the strings we no longer surface really did look
        # like HTTP transport errors, not product risks.  This guards
        # against a future regression where someone re-pipes them.
        materials = {
            "search_strategy": "x",
            "providers_used": [],
            "suggested_search_queries": [],
            "provider_errors": {
                "grep_app": "error=Client error '429 Too Many Requests'",
            },
            "citations": [],
        }
        ctx = _build_fallback_research_context(
            user_problem="x",
            materials=materials,
        )
        assert all(
            "429" not in risk and "Client error" not in risk
            for risk in (ctx.key_risks or [])
        )


# ─────────────────────────────────────────────────────────────────────
# 5) _search_github_repositories — token guard
# ─────────────────────────────────────────────────────────────────────


class TestGithubRepoSearchTokenGuard:
    def test_returns_empty_when_no_token(self, monkeypatch):
        # All known token env vars cleared.
        for name in ("GITHUB_TOKEN", "GH_TOKEN", "GITHUB_API_TOKEN"):
            monkeypatch.delenv(name, raising=False)
        # _search_github_repositories must short-circuit before any HTTP
        # call.  We patch _safe_http_json defensively so a regression
        # would visibly fail this test even if the guard is removed.
        with patch.object(s4, "_safe_http_json") as mock_fetch:
            result = _search_github_repositories("crypto regime switching")
        assert result == []
        mock_fetch.assert_not_called()

    def test_calls_api_when_token_present(self, monkeypatch):
        monkeypatch.setenv("GITHUB_TOKEN", "ghp_dummy_test_token_12345")
        with patch.object(s4, "_safe_http_json") as mock_fetch:
            mock_fetch.return_value = {"items": []}
            _search_github_repositories("crypto regime switching")
        mock_fetch.assert_called_once()
        # First positional arg is the URL.
        url_arg = mock_fetch.call_args[0][0]
        assert url_arg.startswith(
            "https://api.github.com/search/repositories?"
        )


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
