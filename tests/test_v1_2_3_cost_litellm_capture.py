"""Regression tests for v1.2.3 — LiteLLM-native cost/usage capture.

Background
----------
Cost/token accounting used to be fed by a CrewAI ``BaseInterceptor`` + a
langchain ``BaseCallbackHandler`` wired through ``crewai.LLM(interceptor=...,
callbacks=...)``.  crewai 1.14.x ``LLM.__init__`` does not accept those kwargs
and silently drops them, so neither hook ever fired in real runs.  Cost/tokens
then fell back to the lossy CrewAI usage-metrics path
(``extract_and_set_usage_from_crew``), whose ContextVar skip-guard fails across
CrewAI worker threads and re-counts ``crew.calculate_usage_metrics()``
(cumulative across retries) — inflating BOTH the USD and token headline by a
large multiple (the operator saw ~7M tokens / inflated USD for a run that
OpenRouter billed at 899K tokens / $0.132).

v1.2.3 removes the crewai/langchain hooks entirely and captures cost/usage at
the one chokepoint CrewAI actually routes through: the LiteLLM success callback
(``_LiteLLMUsageLogger`` → ``_record_litellm_success``).  It fires once per
completion in the call's own context, dedups by response id, writes the
thread-safe ``AgentCostAccountant`` and (for billed OpenRouter responses) the
authoritative billed-cost ledger.  ``_reconcile_cost_summary_with_billing`` then
promotes the ledger's USD *and* token totals to the headline.

These tests pin: the capture records exactly once (dedup), tokens/cost are
exact, provider resolution + cost-source priority, stage attribution, the
reconcile token override (the operator's symptom), idempotent registration, the
real LiteLLM firing path via ``mock_response``, and the structural removal of the
crewai/langchain hooks.
"""
from __future__ import annotations

import inspect
import unittest

from crucible.module_runtime import get_runtime


def _fake_response(resp_id, prompt, completion, *, cost=None, cached=0, reasoning=0,
                   model="openrouter/z-ai/glm-4.6"):
    """Minimal stand-in for a LiteLLM ModelResponse (``.id`` / ``.model`` / ``.usage``)."""
    usage = {
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "total_tokens": prompt + completion,
        "prompt_tokens_details": {"cached_tokens": cached},
        "completion_tokens_details": {"reasoning_tokens": reasoning},
    }
    if cost is not None:
        usage["cost"] = cost
    ns = type("Resp", (), {})()
    ns.id = resp_id
    ns.model = model
    ns.usage = usage
    return ns


_OR_KWARGS = {
    "model": "openrouter/z-ai/glm-4.6",
    "litellm_params": {"api_base": "https://openrouter.ai/api/v1"},
}


class _CostIsolatedTestCase(unittest.TestCase):
    """Reset all process-global cost state around every test."""

    def setUp(self) -> None:
        self.rt = get_runtime()
        self.rt.reset_cost_accountant()
        self.rt.reset_openrouter_billed_ledger()
        self.rt.reset_llm_usage_dedup()

    def tearDown(self) -> None:
        self.rt.reset_cost_accountant()
        self.rt.reset_openrouter_billed_ledger()
        self.rt.reset_llm_usage_dedup()


class TestLiteLLMSuccessCapture(_CostIsolatedTestCase):
    def test_records_tokens_and_cost_to_accountant_and_ledger(self) -> None:
        self.rt._record_litellm_success(_OR_KWARGS, _fake_response("g1", 1000, 200, cost=0.05, cached=100, reasoning=50))
        self.rt._record_litellm_success(_OR_KWARGS, _fake_response("g2", 3000, 400, cost=0.082))
        summ = self.rt.get_cost_accountant().get_summary()
        self.assertEqual(summ["total_tokens"], 4600)
        self.assertAlmostEqual(summ["total_cost_usd"], 0.132, places=9)
        self.assertEqual(summ["cost_source"], "openrouter_api")
        # Billed ledger mirrors the OpenRouter dashboard exactly.
        self.assertEqual(self.rt.get_openrouter_billed_count(), 2)
        self.assertAlmostEqual(self.rt.get_openrouter_billed_total(), 0.132, places=9)
        bt = self.rt.get_openrouter_billed_tokens()
        self.assertEqual(bt["total_tokens"], 4600)
        self.assertEqual(bt["input_tokens"], 4000)
        self.assertEqual(bt["output_tokens"], 600)
        self.assertEqual(bt["cached_tokens"], 100)
        self.assertEqual(bt["reasoning_tokens"], 50)

    def test_dedup_same_response_id_never_double_counts(self) -> None:
        self.rt._record_litellm_success(_OR_KWARGS, _fake_response("dup", 1000, 200, cost=0.05))
        self.rt._record_litellm_success(_OR_KWARGS, _fake_response("dup", 1000, 200, cost=0.05))
        self.rt._record_litellm_success(_OR_KWARGS, _fake_response("dup", 1000, 200, cost=0.05))
        self.assertEqual(self.rt.get_openrouter_billed_count(), 1)
        self.assertAlmostEqual(self.rt.get_openrouter_billed_total(), 0.05, places=9)
        self.assertEqual(self.rt.get_cost_accountant().get_summary()["total_tokens"], 1200)

    def test_zero_token_control_call_is_skipped(self) -> None:
        self.rt._record_litellm_success(_OR_KWARGS, _fake_response("z", 0, 0, cost=0.0))
        self.assertEqual(self.rt.get_cost_accountant().get_summary().get("total_tokens", 0), 0)
        self.assertEqual(self.rt.get_openrouter_billed_count(), 0)

    def test_stage_attribution_flows_into_breakdown(self) -> None:
        token = self.rt.set_cost_attribution("direction_debate", "judge")
        try:
            self.rt._record_litellm_success(_OR_KWARGS, _fake_response("s1", 500, 100, cost=0.01))
        finally:
            self.rt.reset_cost_attribution(token)
        summ = self.rt.get_cost_accountant().get_summary()
        self.assertIn("direction_debate", summ.get("by_stage", {}))
        self.assertEqual(summ["by_stage"]["direction_debate"]["total_tokens"], 600)


class TestProviderAndCostResolution(_CostIsolatedTestCase):
    def test_openrouter_usage_cost_is_authoritative(self) -> None:
        self.rt._record_litellm_success(_OR_KWARGS, _fake_response("a", 100, 50, cost=0.0042))
        self.assertEqual(self.rt.get_cost_accountant().get_summary()["cost_source"], "openrouter_api")
        self.assertAlmostEqual(self.rt.get_openrouter_billed_total(), 0.0042, places=9)

    def test_response_cost_kwarg_used_when_usage_cost_absent(self) -> None:
        kwargs = dict(_OR_KWARGS, response_cost=0.0033)
        self.rt._record_litellm_success(kwargs, _fake_response("b", 100, 50))  # no usage.cost
        summ = self.rt.get_cost_accountant().get_summary()
        self.assertAlmostEqual(summ["total_cost_usd"], 0.0033, places=9)
        self.assertEqual(summ["cost_source"], "litellm_computed")
        # litellm_computed is NOT a real OpenRouter bill → stays out of the ledger.
        self.assertEqual(self.rt.get_openrouter_billed_count(), 0)

    def test_alibaba_is_token_only_cost_forced_zero(self) -> None:
        ak = {"model": "dashscope/qwen", "litellm_params": {"api_base": "https://coding-intl.dashscope.aliyuncs.com/v1"}}
        self.rt._record_litellm_success(ak, _fake_response("c", 500, 100, cost=0.9, model="dashscope/qwen"))
        summ = self.rt.get_cost_accountant().get_summary()
        self.assertEqual(summ["total_tokens"], 600)
        self.assertAlmostEqual(summ["total_cost_usd"], 0.0, places=12)
        self.assertEqual(summ["cost_source"], "alibaba_coding_plan_tokens_only")
        self.assertEqual(self.rt.get_openrouter_billed_count(), 0)


class TestReconcileTokenOverride(_CostIsolatedTestCase):
    def test_reconcile_promotes_ledger_tokens_and_usd_to_headline(self) -> None:
        # Two real billed responses == the dashboard (0.132 / 4600 tokens).
        self.rt._record_litellm_success(_OR_KWARGS, _fake_response("g1", 1000, 200, cost=0.05, cached=100))
        self.rt._record_litellm_success(_OR_KWARGS, _fake_response("g2", 3000, 400, cost=0.082, reasoning=50))
        # Simulate an inflated per-stage summary (the operator's 7M-token symptom).
        inflated = {"total_cost_usd": 1.03, "total_tokens": 7_000_000,
                    "cached_tokens": 9, "reasoning_tokens": 9}
        rec = self.rt._reconcile_cost_summary_with_billing(inflated)
        self.assertEqual(rec["total_tokens"], 4600)
        self.assertAlmostEqual(rec["total_cost_usd"], 0.132, places=9)
        self.assertEqual(rec["total_tokens_attributed"], 7_000_000)
        self.assertEqual(rec["total_tokens_billed"], 4600)
        self.assertEqual(rec["total_input_tokens"], 4000)
        self.assertEqual(rec["total_output_tokens"], 600)
        self.assertEqual(rec["cached_tokens"], 100)
        self.assertEqual(rec["reasoning_tokens"], 50)
        self.assertEqual(rec["cost_source"], "openrouter_api")

    def test_reconcile_no_op_when_ledger_empty(self) -> None:
        original = {"total_cost_usd": 0.0, "total_tokens": 600, "cost_source": "alibaba_coding_plan_tokens_only"}
        rec = self.rt._reconcile_cost_summary_with_billing(dict(original))
        self.assertEqual(rec["total_tokens"], 600)
        self.assertEqual(rec["cost_source"], "alibaba_coding_plan_tokens_only")
        self.assertNotIn("total_tokens_billed", rec)


class TestRegistration(_CostIsolatedTestCase):
    def test_register_is_idempotent_and_lands_in_litellm_callbacks(self) -> None:
        import litellm

        logger = self.rt.get_litellm_usage_logger()
        self.assertIsNotNone(logger)
        self.rt.ensure_litellm_usage_logger_registered()
        self.rt.ensure_litellm_usage_logger_registered()
        count = sum(1 for c in litellm.callbacks if c is logger)
        self.assertEqual(count, 1, "logger must be registered exactly once (idempotent)")

    def test_litellm_mock_completion_fires_callback(self) -> None:
        """The real proof: a litellm.completion (offline mock) drives the callback."""
        import litellm

        self.rt.ensure_litellm_usage_logger_registered()
        token = self.rt.set_cost_attribution("codegen", "codegen")
        try:
            litellm.completion(
                model="openrouter/z-ai/glm-4.6",
                messages=[{"role": "user", "content": "a token-bearing prompt for the mock"}],
                mock_response="a mock completion response body",
                api_base="https://openrouter.ai/api/v1",
                api_key="sk-or-test",
            )
        finally:
            self.rt.reset_cost_attribution(token)
        summ = self.rt.get_cost_accountant().get_summary()
        self.assertGreaterEqual(summ.get("total_executions", 0), 1)
        self.assertGreater(summ.get("total_tokens", 0), 0)


class TestCrewaiLangchainHooksRemoved(unittest.TestCase):
    """Structural pins: the crewai/langchain cost hooks are gone and the new
    LiteLLM logger is wired in their place (CLAUDE.md §9.6 producer→consumer)."""

    def test_section_00_no_crewai_interceptor_or_langchain_callback(self) -> None:
        from crucible.modules import section_00_bootstrap_and_utils as s0

        src = inspect.getsource(s0)
        # Check for actual code (class/def/import statements), not the historical
        # mentions that legitimately remain in explanatory comments.
        for dead in (
            "class OpenRouterUsageHTTPInterceptor",
            "class OpenRouterUsageCallbackHandler",
            "def _capture_openrouter_usage_from_http_response",
            "def extract_and_set_usage_from_crew(",
            "from crewai.llms.hooks.base import BaseInterceptor",
            "from langchain_core.callbacks import BaseCallbackHandler",
        ):
            self.assertNotIn(dead, src, f"{dead!r} must be removed in v1.2.3")
        # The new capture machinery must be present.
        self.assertIn("class _LiteLLMUsageLogger", src)
        self.assertIn("def _record_litellm_success", src)
        self.assertIn("def ensure_litellm_usage_logger_registered", src)

    def test_record_cost_is_a_noop(self) -> None:
        from crucible.modules import section_00_bootstrap_and_utils as s0

        body = inspect.getsource(s0._record_cost)
        self.assertIn("return None", body)
        # Must NOT feed the accountant any more (would double-count the callback).
        self.assertNotIn("get_cost_accountant().record", body)
        self.assertNotIn("accountant.record", body)

    def test_llm_builders_register_litellm_logger_not_interceptor(self) -> None:
        from crucible.modules import section_01_extraction_and_reformat as s01
        from crucible.modules import section_02_research_and_llm as s02
        from crucible.modules import section_05_analysis_and_codegen as s05

        for mod in (s01, s02, s05):
            src = inspect.getsource(mod)
            self.assertIn("ensure_litellm_usage_logger_registered", src,
                          f"{mod.__name__} must register the LiteLLM usage logger")
            self.assertNotIn("get_openrouter_http_interceptor", src,
                             f"{mod.__name__} must not reference the removed crewai interceptor")
            self.assertNotIn("get_openrouter_callback_handler", src,
                             f"{mod.__name__} must not reference the removed langchain callback")

    def test_resilience_sets_cost_attribution_around_kickoff(self) -> None:
        from crucible import resilience

        src = inspect.getsource(resilience.kickoff_crew_with_retry)
        self.assertIn("set_cost_attribution", src,
                      "kickoff_crew_with_retry must tag LLM calls with (stage, agent)")
        self.assertNotIn("extract_and_set_usage_from_crew", src,
                         "the crewai usage-metrics extraction must be removed")


if __name__ == "__main__":
    unittest.main()
