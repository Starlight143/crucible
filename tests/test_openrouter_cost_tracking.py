"""Tests for OpenRouter cost tracking functionality."""

from __future__ import annotations

import os
from types import SimpleNamespace
from unittest import mock

from crucible import resilience
from crucible.module_runtime import get_runtime


class _ProviderIsolationMixin:
    # Use setUp/tearDown (unittest-compatible) AND setup_method/teardown_method
    # (pytest-compatible) so the mixin works correctly under both runners.

    def _do_setup(self) -> None:
        self._original_llm_provider_env = os.environ.get("LLM_PROVIDER")
        os.environ["LLM_PROVIDER"] = "openrouter"
        runtime = get_runtime()
        bootstrap_globals = runtime.set_openrouter_usage.__globals__
        self._bootstrap_globals = bootstrap_globals
        self._original_active_provider = bootstrap_globals.get("ACTIVE_LLM_PROVIDER")
        bootstrap_globals["ACTIVE_LLM_PROVIDER"] = "openrouter"

    def _do_teardown(self) -> None:
        if getattr(self, "_original_llm_provider_env", None) is None:
            os.environ.pop("LLM_PROVIDER", None)
        else:
            os.environ["LLM_PROVIDER"] = self._original_llm_provider_env
        if hasattr(self, "_bootstrap_globals"):
            self._bootstrap_globals["ACTIVE_LLM_PROVIDER"] = getattr(
                self,
                "_original_active_provider",
                "openrouter",
            )

    # unittest lifecycle
    def setUp(self) -> None:
        self._do_setup()

    def tearDown(self) -> None:
        self._do_teardown()

    # pytest lifecycle (called instead of setUp/tearDown when running via pytest)
    def setup_method(self, _method: object = None) -> None:
        self._do_setup()

    def teardown_method(self, _method: object = None) -> None:
        self._do_teardown()


class TestOpenRouterUsageData(_ProviderIsolationMixin):
    """Tests for OpenRouterUsageData dataclass."""

    def test_default_values(self):
        rt = get_runtime()
        usage = rt.OpenRouterUsageData()
        assert usage.input_tokens == 0
        assert usage.output_tokens == 0
        assert usage.total_tokens == 0
        assert usage.cached_tokens == 0
        assert usage.reasoning_tokens == 0
        assert usage.input_cost_usd == 0.0
        assert usage.output_cost_usd == 0.0
        assert usage.cache_cost_usd == 0.0
        assert usage.total_cost_usd == 0.0
        assert usage.model_id == ""
        assert usage.cost_source == "estimated"

    def test_custom_values(self):
        rt = get_runtime()
        usage = rt.OpenRouterUsageData(
            input_tokens=100,
            output_tokens=50,
            total_tokens=150,
            cached_tokens=25,
            reasoning_tokens=10,
            input_cost_usd=0.001,
            output_cost_usd=0.002,
            cache_cost_usd=0.0005,
            total_cost_usd=0.0035,
            model_id="openai/gpt-4",
            cost_source="openrouter_api",
            llm_provider="openrouter",
        )
        assert usage.input_tokens == 100
        assert usage.output_tokens == 50
        assert usage.total_tokens == 150
        assert usage.cached_tokens == 25
        assert usage.reasoning_tokens == 10
        assert usage.input_cost_usd == 0.001
        assert usage.output_cost_usd == 0.002
        assert usage.cache_cost_usd == 0.0005
        assert usage.total_cost_usd == 0.0035
        assert usage.model_id == "openai/gpt-4"
        assert usage.cost_source == "openrouter_api"
        assert usage.llm_provider == "openrouter"


class TestOpenRouterUsageContext(_ProviderIsolationMixin):
    """Tests for OpenRouter usage context management."""

    def test_set_and_get_usage(self):
        rt = get_runtime()
        rt.clear_openrouter_usage()

        rt.set_openrouter_usage(
            {
                "prompt_tokens": 100,
                "completion_tokens": 50,
                "total_tokens": 150,
                "cost": 0.00123,
                "prompt_tokens_details": {"cached_tokens": 25},
                "completion_tokens_details": {"reasoning_tokens": 15},
            },
            model_id="openai/gpt-4",
        )

        usage = rt.get_last_openrouter_usage()
        assert usage.input_tokens == 100
        assert usage.output_tokens == 50
        assert usage.total_tokens == 150
        assert usage.cached_tokens == 25
        assert usage.reasoning_tokens == 15
        assert usage.total_cost_usd == 0.00123
        assert usage.model_id == "openai/gpt-4"
        assert usage.cost_source == "openrouter_api"
        assert usage.llm_provider == "openrouter"

    def test_alibaba_usage_tracks_tokens_without_usd(self):
        rt = get_runtime()
        rt.clear_openrouter_usage()

        rt.set_openrouter_usage(
            {
                "prompt_tokens": 120,
                "completion_tokens": 30,
                "total_tokens": 150,
                "prompt_tokens_details": {"cached_tokens": 20},
                "completion_tokens_details": {"reasoning_tokens": 8},
                "cost": 12.34,
            },
            model_id="qwen3.5-plus",
            provider="alibaba_coding_plan",
        )

        usage = rt.get_last_openrouter_usage()
        assert usage.input_tokens == 120
        assert usage.output_tokens == 30
        assert usage.total_tokens == 150
        assert usage.cached_tokens == 20
        assert usage.reasoning_tokens == 8
        assert usage.input_cost_usd == 0.0
        assert usage.output_cost_usd == 0.0
        assert usage.total_cost_usd == 0.0
        assert usage.cost_source == "alibaba_coding_plan_tokens_only"
        assert usage.llm_provider == "alibaba_coding_plan"


    def test_usage_provider_prefers_active_runtime_selection_over_env(self):
        rt = get_runtime()
        globals_dict = rt._resolve_usage_provider.__globals__
        original_active_provider = globals_dict.get("ACTIVE_LLM_PROVIDER")
        with mock.patch.dict(os.environ, {"LLM_PROVIDER": "openrouter"}, clear=False):
            try:
                globals_dict["ACTIVE_LLM_PROVIDER"] = "alibaba_coding_plan"
                assert rt._resolve_usage_provider() == "alibaba_coding_plan"
                assert rt._resolve_usage_provider("openrouter") == "openrouter"
            finally:
                globals_dict["ACTIVE_LLM_PROVIDER"] = original_active_provider

    def test_clear_usage(self):
        rt = get_runtime()
        rt.set_openrouter_usage(
            {
                "prompt_tokens": 100,
                "completion_tokens": 50,
                "cost": 0.001,
            }
        )

        rt.clear_openrouter_usage()
        usage = rt.get_last_openrouter_usage()
        assert usage.input_tokens == 0
        assert usage.output_tokens == 0
        assert usage.total_cost_usd == 0.0

    def test_cost_calculation_from_tokens(self):
        rt = get_runtime()
        rt.clear_openrouter_usage()

        rt.set_openrouter_usage(
            {
                "prompt_tokens": 100,
                "completion_tokens": 50,
                "cost": 0.003,
            }
        )

        usage = rt.get_last_openrouter_usage()
        assert usage.total_cost_usd == 0.003
        assert usage.input_cost_usd > 0
        assert usage.output_cost_usd > 0
        assert abs(usage.input_cost_usd + usage.output_cost_usd - 0.003) < 0.0001

    def test_estimated_cost_source_when_no_cost(self):
        rt = get_runtime()
        rt.clear_openrouter_usage()

        rt.set_openrouter_usage(
            {
                "prompt_tokens": 100,
                "completion_tokens": 50,
            }
        )

        usage = rt.get_last_openrouter_usage()
        assert usage.cost_source == "estimated"

    def test_estimates_cost_from_known_model_when_api_cost_missing(self):
        rt = get_runtime()
        rt.clear_openrouter_usage()

        rt.set_openrouter_usage(
            {
                "prompt_tokens": 1_000,
                "completion_tokens": 500,
                "total_tokens": 1_500,
            },
            model_id="minimax/minimax-m2.5-20260212",
        )

        usage = rt.get_last_openrouter_usage()
        assert usage.model_id == "minimax/minimax-m2.5"
        assert usage.cost_source == "openrouter_tokens_with_pricing"
        assert usage.total_cost_usd > 0.0
        assert abs(
            usage.total_cost_usd
            - (
                (1_000 * (0.20 / 1_000_000))
                + (500 * (1.17 / 1_000_000))
            )
        ) < 1e-12

    def test_unpriced_model_alias_uses_family_fallback_v1_1_1(self):
        """v1.1.1 behaviour change.

        Pre-v1.1.1, ``gpt-5.4-pro`` canonicalised to ``openai/gpt-5.4-pro``
        (no explicit entry in ``OPENROUTER_MODEL_PRICING``) and produced
        ``cost_source="estimated"`` with zero cost.  That was a defensive
        assertion against aliases bleeding into wrong prices.

        v1.1.1 layers a family-prefix fallback on top of explicit lookup:
        ``openai/gpt-5.4-pro`` matches the ``openai/gpt-5`` family entry
        in ``OPENROUTER_MODEL_FAMILY_PRICING`` and resolves to frontier
        tier pricing.  This is intentional — without it, any new model
        variant the operator runs (e.g. a forthcoming pro / turbo flavour)
        silently emits zero cost and skews the run dashboard.

        The defensive intent of the original test is preserved by
        ``test_truly_unknown_vendor_still_returns_zero`` below, which
        uses a vendor outside every family-fallback prefix.
        """
        rt = get_runtime()
        rt.clear_openrouter_usage()

        rt.set_openrouter_usage(
            {
                "prompt_tokens": 1_000,
                "completion_tokens": 500,
                "total_tokens": 1_500,
            },
            model_id="gpt-5.4-pro",
        )

        usage = rt.get_last_openrouter_usage()
        assert usage.model_id == "openai/gpt-5.4-pro"
        # Family fallback fires → cost_source upgrades to the
        # openrouter-tokens-with-pricing tier (set_openrouter_usage
        # path; extract_and_set_usage_from_crew path would label it
        # "crewai_metrics_with_pricing" — both indicate table-based
        # pricing rather than API-reported real cost).
        # total_cost_usd computed from tokens × frontier-tier per-million.
        assert usage.cost_source == "openrouter_tokens_with_pricing"
        # Frontier-tier prices: $2.50/M input, $15.00/M output.
        # 1000 input × 2.5e-6 + 500 output × 1.5e-5 = 0.0025 + 0.0075 = 0.0100
        assert abs(usage.input_cost_usd - 0.0025) < 1e-12
        assert abs(usage.output_cost_usd - 0.0075) < 1e-12
        assert abs(usage.total_cost_usd - 0.0100) < 1e-12

    def test_truly_unknown_vendor_still_returns_zero(self):
        """Preserves the original defensive intent of
        ``test_does_not_estimate_cost_from_unpriced_model_alias``:
        a model whose canonical form falls outside every known vendor
        family (i.e. ``cohere/`` is not in ``OPENROUTER_MODEL_FAMILY_PRICING``)
        must still emit ``cost_source="estimated"`` with zero cost so
        the operator gets a clear signal to add the model to the table.
        """
        rt = get_runtime()
        rt.clear_openrouter_usage()

        rt.set_openrouter_usage(
            {
                "prompt_tokens": 1_000,
                "completion_tokens": 500,
                "total_tokens": 1_500,
            },
            model_id="cohere/command-r-plus",
        )

        usage = rt.get_last_openrouter_usage()
        assert usage.cost_source == "estimated"
        assert usage.input_cost_usd == 0.0
        assert usage.output_cost_usd == 0.0
        assert usage.total_cost_usd == 0.0

    def test_accumulated_usage_preserves_higher_confidence_cost_source(self):
        rt = get_runtime()
        rt.clear_openrouter_usage()

        rt.set_openrouter_usage(
            {
                "prompt_tokens": 120,
                "completion_tokens": 80,
                "total_tokens": 200,
                "cost": 0.0042,
            },
            model_id="openai/gpt-5.4",
        )
        rt.set_openrouter_usage(
            {
                "prompt_tokens": 50,
                "completion_tokens": 25,
                "total_tokens": 75,
            },
            model_id="unknown/provider-model",
            accumulate=True,
        )

        usage = rt.get_last_openrouter_usage()
        assert usage.total_tokens == 275
        assert usage.total_cost_usd == 0.0042
        assert "openai/gpt-5.4" in usage.model_id
        assert "unknown/provider-model" in usage.model_id
        assert usage.cost_source == "openrouter_api"

    def test_accumulated_usage_does_not_mix_providers_into_one_context(self):
        rt = get_runtime()
        rt.clear_openrouter_usage()

        rt.set_openrouter_usage(
            {
                "prompt_tokens": 120,
                "completion_tokens": 80,
                "total_tokens": 200,
                "cost": 0.0042,
            },
            model_id="openai/gpt-5.4",
            provider="openrouter",
        )
        rt.set_openrouter_usage(
            {
                "prompt_tokens": 50,
                "completion_tokens": 25,
                "total_tokens": 75,
            },
            model_id="qwen3.5-plus",
            provider="alibaba_coding_plan",
            accumulate=True,
        )

        usage = rt.get_last_openrouter_usage()
        records = rt.get_usage_records()
        assert usage.total_tokens == 75
        assert usage.total_cost_usd == 0.0
        assert usage.model_id == "qwen3.5-plus"
        assert usage.cost_source == "alibaba_coding_plan_tokens_only"
        assert usage.llm_provider == "alibaba_coding_plan"
        assert len(records) == 2
        assert records[0].llm_provider == "openrouter"
        assert records[1].llm_provider == "alibaba_coding_plan"


    def test_merge_usage_model_ids_uses_exact_tokens_not_substring_matches(self):
        rt = get_runtime()
        merged = rt._merge_usage_model_ids("vendor/model-alpha-pro", "vendor/model-alpha")
        assert merged.split(",") == ["vendor/model-alpha-pro", "vendor/model-alpha"]

class TestAgentCostRecord(_ProviderIsolationMixin):
    """Tests for AgentCostRecord with OpenRouter fields."""

    def test_default_values(self):
        rt = get_runtime()
        record = rt.AgentCostRecord(agent_name="test_agent", stage="test_stage")
        assert record.input_tokens == 0
        assert record.output_tokens == 0
        assert record.cached_tokens == 0
        assert record.reasoning_tokens == 0
        assert record.input_cost_usd == 0.0
        assert record.output_cost_usd == 0.0
        assert record.cache_cost_usd == 0.0
        assert record.total_cost_usd == 0.0
        assert record.model_id == ""
        assert record.cost_source == "estimated"

    def test_openrouter_values(self):
        rt = get_runtime()
        record = rt.AgentCostRecord(
            agent_name="test_agent",
            stage="test_stage",
            input_tokens=100,
            output_tokens=50,
            cached_tokens=25,
            reasoning_tokens=10,
            input_cost_usd=0.001,
            output_cost_usd=0.002,
            cache_cost_usd=0.0005,
            total_cost_usd=0.0035,
            model_id="openai/gpt-4",
            cost_source="openrouter_api",
        )
        assert record.input_tokens == 100
        assert record.output_tokens == 50
        assert record.cached_tokens == 25
        assert record.reasoning_tokens == 10
        assert record.total_cost_usd == 0.0035
        assert record.model_id == "openai/gpt-4"
        assert record.cost_source == "openrouter_api"


class TestAgentCostAccountant(_ProviderIsolationMixin):
    """Tests for AgentCostAccountant with OpenRouter fields."""

    def test_record_with_openrouter_data(self):
        rt = get_runtime()
        accountant = rt.AgentCostAccountant()

        accountant.record(
            agent_name="test_agent",
            stage="test_stage",
            input_tokens=100,
            output_tokens=50,
            cached_tokens=25,
            reasoning_tokens=10,
            input_cost_usd=0.001,
            output_cost_usd=0.002,
            cache_cost_usd=0.0005,
            total_cost_usd=0.0035,
            model_id="openai/gpt-4",
            cost_source="openrouter_api",
        )

        summary = accountant.get_summary()
        assert summary["total_cost_usd"] == 0.0035
        assert summary["cached_tokens"] == 25
        assert summary["reasoning_tokens"] == 10
        assert summary["cost_source"] == "openrouter_api"
        assert "openai/gpt-4" in summary["models_used"]

    def test_record_multiple_entries(self):
        rt = get_runtime()
        accountant = rt.AgentCostAccountant()

        accountant.record(
            agent_name="agent_1",
            stage="stage_1",
            input_tokens=100,
            output_tokens=50,
            total_cost_usd=0.001,
            model_id="openai/gpt-4",
            cost_source="openrouter_api",
        )

        accountant.record(
            agent_name="agent_2",
            stage="stage_2",
            input_tokens=200,
            output_tokens=100,
            total_cost_usd=0.002,
            model_id="anthropic/claude-3",
            cost_source="openrouter_api",
        )

        summary = accountant.get_summary()
        assert summary["total_cost_usd"] == 0.003
        assert summary["total_tokens"] == 450
        assert len(summary["models_used"]) == 2

    def test_get_agent_cost_with_usd(self):
        rt = get_runtime()
        accountant = rt.AgentCostAccountant()

        accountant.record(
            agent_name="test_agent",
            stage="stage_1",
            input_tokens=100,
            output_tokens=50,
            total_cost_usd=0.001,
            model_id="openai/gpt-4",
            cost_source="openrouter_api",
        )

        accountant.record(
            agent_name="test_agent",
            stage="stage_2",
            input_tokens=200,
            output_tokens=100,
            total_cost_usd=0.002,
            model_id="openai/gpt-4",
            cost_source="openrouter_api",
        )

        agent_cost = accountant.get_agent_cost("test_agent")
        assert agent_cost["total_cost_usd"] == 0.003
        assert agent_cost["executions"] == 2
        assert agent_cost["avg_cost_usd_per_execution"] == 0.0015
        assert "openai/gpt-4" in agent_cost["models_used"]

    def test_get_stage_cost_with_usd(self):
        rt = get_runtime()
        accountant = rt.AgentCostAccountant()

        accountant.record(
            agent_name="agent_1",
            stage="test_stage",
            input_tokens=100,
            output_tokens=50,
            total_cost_usd=0.001,
            cost_source="openrouter_api",
        )

        stage_cost = accountant.get_stage_cost("test_stage")
        assert stage_cost["total_cost_usd"] == 0.001
        assert stage_cost["total_tokens"] == 150

    def test_get_top_cost_agents(self):
        rt = get_runtime()
        accountant = rt.AgentCostAccountant()

        accountant.record(
            agent_name="cheap_agent",
            stage="stage",
            input_tokens=50,
            total_cost_usd=0.001,
        )

        accountant.record(
            agent_name="expensive_agent",
            stage="stage",
            input_tokens=500,
            total_cost_usd=0.010,
        )

        top_agents = accountant.get_top_cost_agents(limit=5)
        assert len(top_agents) == 2
        assert top_agents[0]["agent"] == "expensive_agent"
        assert top_agents[0]["total_cost_usd"] == 0.010

    def test_get_top_cost_agents_prefers_usd_cost_when_available(self):
        rt = get_runtime()
        accountant = rt.AgentCostAccountant()

        accountant.record(
            agent_name="high_units_low_usd",
            stage="stage",
            input_tokens=5_000,
            output_tokens=0,
            total_cost_usd=0.001,
            cost_source="openrouter_tokens_with_pricing",
        )

        accountant.record(
            agent_name="low_units_high_usd",
            stage="stage",
            input_tokens=10,
            output_tokens=0,
            total_cost_usd=0.010,
            cost_source="openrouter_api",
        )

        top_agents = accountant.get_top_cost_agents(limit=5)
        assert top_agents[0]["agent"] == "low_units_high_usd"
        assert top_agents[0]["total_cost_usd"] == 0.010

    def test_get_top_cost_agents_falls_back_to_tokens_for_alibaba_source(self):
        rt = get_runtime()
        accountant = rt.AgentCostAccountant()

        accountant.record(
            agent_name="lower_tokens",
            stage="stage",
            input_tokens=20,
            output_tokens=5,
            cost_source="alibaba_coding_plan_tokens_only",
        )
        accountant.record(
            agent_name="higher_tokens",
            stage="stage",
            input_tokens=200,
            output_tokens=50,
            cost_source="alibaba_coding_plan_tokens_only",
        )

        top_agents = accountant.get_top_cost_agents(limit=5)
        assert top_agents[0]["agent"] == "higher_tokens"
        assert top_agents[0]["total_tokens"] == 250

    def test_summary_marks_alibaba_token_only_source_as_primary_cost_source(self):
        rt = get_runtime()
        accountant = rt.AgentCostAccountant()

        accountant.record(
            agent_name="alibaba_agent",
            stage="stage",
            input_tokens=100,
            output_tokens=40,
            model_id="qwen3.5-plus",
            cost_source="alibaba_coding_plan_tokens_only",
        )

        summary = accountant.get_summary()
        agent_cost = accountant.get_agent_cost("alibaba_agent")
        assert summary["cost_source"] == "alibaba_coding_plan_tokens_only"
        assert agent_cost["cost_source"] == "alibaba_coding_plan_tokens_only"
        assert summary["total_cost_usd"] == 0.0

    def test_backward_compatibility_with_legacy_record(self):
        rt = get_runtime()
        accountant = rt.AgentCostAccountant()

        accountant.record(
            agent_name="legacy_agent",
            stage="legacy_stage",
            input_tokens=100,
            output_tokens=50,
            success=True,
            cache_hit=False,
        )

        summary = accountant.get_summary()
        assert summary["total_cost_usd"] == 0.0
        assert summary["total_tokens"] == 150
        assert summary["cost_source"] == "estimated"

    def test_summary_marks_token_pricing_source_as_billable(self):
        rt = get_runtime()
        accountant = rt.AgentCostAccountant()

        accountant.record(
            agent_name="priced_from_tokens",
            stage="stage",
            input_tokens=100,
            output_tokens=50,
            total_cost_usd=0.00123,
            model_id="minimax/minimax-m2.5",
            cost_source="openrouter_tokens_with_pricing",
        )

        summary = accountant.get_summary()
        agent_cost = accountant.get_agent_cost("priced_from_tokens")
        assert summary["cost_source"] == "openrouter_tokens_with_pricing"
        assert agent_cost["cost_source"] == "openrouter_tokens_with_pricing"


class TestModelPricingResolution(_ProviderIsolationMixin):
    def test_get_model_pricing_handles_known_aliases_and_snapshots(self):
        rt = get_runtime()

        assert rt._get_model_pricing("openai/gpt-5.4") == (
            2.50 / 1_000_000,
            15.00 / 1_000_000,
        )
        assert rt._get_model_pricing("gpt-5.4-2026-03-05") == (
            2.50 / 1_000_000,
            15.00 / 1_000_000,
        )
        assert rt._get_model_pricing("z-ai/glm-5-20260211") == (
            0.72 / 1_000_000,
            2.30 / 1_000_000,
        )
        assert rt._get_model_pricing("minimax/minimax-m2.5-20260212") == (
            0.20 / 1_000_000,
            1.17 / 1_000_000,
        )
        # v1.1.1 — ``gpt-5.4-pro`` canonicalises to ``openai/gpt-5.4-pro``
        # which has no explicit entry but matches the ``openai/gpt-5``
        # family-prefix fallback.  Returns frontier-tier pricing instead
        # of (0, 0).  The original protective assertion ("alias doesn't
        # bleed into wrong price") is preserved by the next assertion:
        # truly out-of-family vendors still resolve to (0, 0).
        assert rt._get_model_pricing("gpt-5.4-pro") == (
            2.50 / 1_000_000,
            15.00 / 1_000_000,
        )
        # Truly unknown vendor (cohere/ is not in family-fallback) → zero.
        assert rt._get_model_pricing("cohere/command-r-plus") == (0.0, 0.0)


class TestResilienceModelResolution(_ProviderIsolationMixin):
    def test_resolve_crew_model_id_reads_model_name_fallback(self):
        class MockLLM:
            model_name = "minimax/minimax-m2.5"

        class MockAgent:
            llm = MockLLM()

        class MockCrew:
            agents = [MockAgent()]

        assert resilience._resolve_crew_model_id(MockCrew()) == "minimax/minimax-m2.5"
