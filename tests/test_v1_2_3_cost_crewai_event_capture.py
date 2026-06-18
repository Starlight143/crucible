"""v1.2.3 — CrewAI-native cost/usage capture + anti-double-count guards.

crucible's LLM is CrewAI's native ``OpenAICompletion`` provider (OpenAI SDK, often
streaming) which does NOT route through LiteLLM — so the LiteLLM success callback
never fires for it (that was why earlier "fixes" captured nothing).  The PRIMARY
capture is now CrewAI's per-call ``LLMCallCompletedEvent`` on its event bus, fed
into the single ``_record_llm_usage`` chokepoint.

This file pins both the behaviour (exact tokens + authoritative OpenRouter
``usage.cost`` + dedup) AND the structural invariants that keep future *additive*
changes from re-introducing the double-counting / wrong-total problems:

* exactly ONE accountant feeder (``_record_llm_usage``),
* the per-stage feeders stay no-ops,
* the listener is wired at run start and registered idempotently,
* the cost-passthrough wrap's CrewAI target still exists,
* the billed-ledger reconcile remains the headline authority.
"""
from __future__ import annotations

import inspect
import re
import unittest
from pathlib import Path
from types import SimpleNamespace

from crucible.module_runtime import get_runtime

_SECTION_DIR = Path(__file__).resolve().parent.parent / "crucible" / "modules"


def _event(usage, *, model="openrouter/deepseek/deepseek-v4-flash", response_id="gen-1",
           call_id="call-1", event_id="ev-1"):
    return SimpleNamespace(usage=usage, model=model, response_id=response_id,
                           call_id=call_id, event_id=event_id)


class _CostIsolated(unittest.TestCase):
    def setUp(self):
        self.rt = get_runtime()
        self.rt.reset_cost_accountant()
        self.rt.reset_openrouter_billed_ledger()
        self.rt.reset_llm_usage_dedup()

    def tearDown(self):
        self.rt.reset_cost_accountant()
        self.rt.reset_openrouter_billed_ledger()
        self.rt.reset_llm_usage_dedup()


class TestCrewaiEventCapture(_CostIsolated):
    def test_event_records_exact_tokens_and_authoritative_cost(self):
        # Mirrors a real OpenRouter generation (deepseek-v4-flash): native tokens
        # + the authoritative usage.cost the passthrough wrap restores.
        self.rt._on_crewai_llm_call_completed(None, _event({
            "prompt_tokens": 2323, "completion_tokens": 5144, "total_tokens": 7467,
            "cost": 0.00176554, "cached_prompt_tokens": 0, "reasoning_tokens": 2994,
        }))
        s = self.rt.get_cost_accountant().get_summary()
        self.assertEqual(s["total_tokens"], 7467)
        self.assertAlmostEqual(s["total_cost_usd"], 0.00176554, places=9)
        self.assertEqual(s["cost_source"], "openrouter_api")
        self.assertEqual(s["reasoning_tokens"], 2994)
        # authoritative cost -> billed ledger (the headline authority)
        self.assertEqual(self.rt.get_openrouter_billed_count(), 1)
        self.assertAlmostEqual(self.rt.get_openrouter_billed_total(), 0.00176554, places=9)
        self.assertEqual(self.rt.get_openrouter_billed_tokens()["total_tokens"], 7467)

    def test_flat_crewai_cached_and_reasoning_keys_are_read(self):
        # CrewAI flattens token details: cached_prompt_tokens / reasoning_tokens.
        self.rt._on_crewai_llm_call_completed(None, _event({
            "prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150,
            "cost": 0.001, "cached_prompt_tokens": 20, "reasoning_tokens": 8,
        }))
        s = self.rt.get_cost_accountant().get_summary()
        self.assertEqual(s["cached_tokens"], 20)
        self.assertEqual(s["reasoning_tokens"], 8)

    def test_dedup_same_response_id_recorded_once(self):
        ev = _event({"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150, "cost": 0.001})
        for _ in range(4):
            self.rt._on_crewai_llm_call_completed(None, ev)
        self.assertEqual(self.rt.get_cost_accountant().get_summary()["total_executions"], 1)

    def test_distinct_calls_counted_separately(self):
        self.rt._on_crewai_llm_call_completed(None, _event(
            {"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150, "cost": 0.001},
            response_id="gen-A"))
        self.rt._on_crewai_llm_call_completed(None, _event(
            {"prompt_tokens": 200, "completion_tokens": 60, "total_tokens": 260, "cost": 0.002},
            response_id="gen-B"))
        s = self.rt.get_cost_accountant().get_summary()
        self.assertEqual(s["total_executions"], 2)
        self.assertEqual(s["total_tokens"], 410)
        self.assertAlmostEqual(s["total_cost_usd"], 0.003, places=9)

    def test_no_stable_id_is_skipped_not_double_counted(self):
        # No response_id/call_id/event_id -> cannot dedup -> skip (undercount-safe).
        self.rt._on_crewai_llm_call_completed(None, SimpleNamespace(
            usage={"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15, "cost": 0.001},
            model="openrouter/x", response_id="", call_id="", event_id=""))
        self.assertEqual(self.rt.get_cost_accountant().get_summary().get("total_executions", 0), 0)

    def test_missing_cost_falls_back_to_pricing_estimate_not_zero(self):
        # No usage.cost (wrap absent / provider didn't return it): tokens stay
        # exact, USD becomes a *labelled* pricing estimate, never silently 0.
        self.rt._on_crewai_llm_call_completed(None, _event({
            "prompt_tokens": 1000, "completion_tokens": 500, "total_tokens": 1500,
        }, model="openrouter/deepseek/deepseek-v4-flash"))
        s = self.rt.get_cost_accountant().get_summary()
        self.assertEqual(s["total_tokens"], 1500)
        self.assertIn(s["cost_source"], ("openrouter_tokens_with_pricing", "estimated"))
        # estimate is NOT promoted to the authoritative billed ledger
        self.assertEqual(self.rt.get_openrouter_billed_count(), 0)


class TestCostPassthroughWrap(_CostIsolated):
    def test_wrap_reattaches_openrouter_cost_crewai_drops(self):
        self.rt._install_crewai_usage_cost_passthrough()
        from crewai.llms.providers.openai.completion import OpenAICompletion
        from openai.types import CompletionUsage
        resp = SimpleNamespace(usage=CompletionUsage(
            prompt_tokens=10, completion_tokens=5, total_tokens=15, cost=0.009))

        class _Dummy:
            pass

        out = OpenAICompletion._extract_openai_token_usage(_Dummy(), resp)
        self.assertIsInstance(out, dict)
        self.assertEqual(out.get("cost"), 0.009)
        self.assertEqual(out.get("prompt_tokens"), 10)


class TestRealBusDelivery(_CostIsolated):
    def test_real_event_bus_delivers_to_handler_once(self):
        self.assertTrue(self.rt.ensure_crewai_usage_listener_registered())
        self.assertTrue(self.rt.ensure_crewai_usage_listener_registered())  # idempotent
        from crewai.events import crewai_event_bus
        from crewai.events.types.llm_events import LLMCallCompletedEvent, LLMCallType
        event = LLMCallCompletedEvent(
            model="openrouter/deepseek/deepseek-v4-flash",
            usage={"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150, "cost": 0.0012},
            response_id="gen-real-unique-1", call_id="call-real-1",
            call_type=LLMCallType.LLM_CALL, messages=[], response="x")
        crewai_event_bus.emit(None, event)
        s = self.rt.get_cost_accountant().get_summary()
        self.assertEqual(s["total_executions"], 1)
        self.assertEqual(s["total_tokens"], 150)
        self.assertAlmostEqual(s["total_cost_usd"], 0.0012, places=9)


class TestReconcileHeadlineAuthority(_CostIsolated):
    def test_inflated_accountant_is_overridden_by_billed_ledger(self):
        from crucible.modules.section_07_selfcheck_output_main import (
            _reconcile_cost_summary_with_billing,
        )
        # Real billed call captured via the event:
        self.rt._on_crewai_llm_call_completed(None, _event({
            "prompt_tokens": 2323, "completion_tokens": 5144, "total_tokens": 7467, "cost": 0.00176554,
        }))
        # Even if some summary were inflated, reconcile pins it to the ledger.
        inflated = {"total_cost_usd": 9.99, "total_tokens": 7_000_000,
                    "cached_tokens": 1, "reasoning_tokens": 1}
        rec = _reconcile_cost_summary_with_billing(inflated)
        self.assertEqual(rec["total_tokens"], 7467)
        self.assertAlmostEqual(rec["total_cost_usd"], 0.00176554, places=9)
        self.assertEqual(rec["total_tokens_attributed"], 7_000_000)
        self.assertEqual(rec["cost_source"], "openrouter_api")


class TestAntiDoubleCountStructuralGuards(unittest.TestCase):
    """Structural pins so future *additive* changes can't silently break the
    cost/token totals or re-introduce double counting."""

    def _section_sources(self):
        return {p.name: p.read_text(encoding="utf-8") for p in _SECTION_DIR.glob("section_*.py")}

    def test_single_accountant_feeder(self):
        # ``get_cost_accountant().record(`` may appear in EXACTLY ONE place across
        # all section modules — inside ``_record_llm_usage``.  A second feeder is
        # exactly how the original multi-x double count happened.
        total = 0
        for name, src in self._section_sources().items():
            total += len(re.findall(r"get_cost_accountant\(\)\.record\(", src))
            total += len(re.findall(r"\baccountant\.record\(", src))
        self.assertEqual(total, 1,
                         "exactly one accountant.record feeder expected (the _record_llm_usage chokepoint)")
        from crucible.modules import section_00_bootstrap_and_utils as s0
        body = inspect.getsource(s0._record_llm_usage)
        self.assertIn("get_cost_accountant().record(", body,
                      "_record_llm_usage must be THE accountant feeder")

    def test_per_stage_feeders_are_noops(self):
        from crucible.modules import section_00_bootstrap_and_utils as s0
        from crucible.modules import section_05_analysis_and_codegen as s05
        rc = inspect.getsource(s0._record_cost)
        self.assertNotIn("accountant.record", rc)
        self.assertIn("return None", rc)
        slice_src = inspect.getsource(s05._record_codegen_usage_slice)
        self.assertNotIn("get_cost_accountant().record(", slice_src)
        self.assertIn("return None", slice_src)

    def test_listener_registered_at_run_start(self):
        from crucible.modules import section_07_selfcheck_output_main as s07
        reset_src = inspect.getsource(s07._reset_pipeline_runtime_state)
        self.assertIn("ensure_crewai_usage_listener_registered()", reset_src)
        self.assertIn("reset_llm_usage_dedup()", reset_src)

    def test_listener_registration_is_idempotent(self):
        from crucible.modules import section_00_bootstrap_and_utils as s0
        src = inspect.getsource(s0.ensure_crewai_usage_listener_registered)
        # Must short-circuit on the module flag before subscribing again.
        self.assertIn("_CREWAI_USAGE_LISTENER_REGISTERED", src)
        self.assertIn("register_handler", src)

    def test_crewai_usage_extractor_target_still_exists(self):
        # The cost-passthrough wrap targets these CrewAI methods; if a CrewAI
        # upgrade renames them the wrap silently degrades to a pricing estimate —
        # catch that here at test time rather than in production.
        from crewai.llms.providers.openai.completion import OpenAICompletion
        self.assertTrue(hasattr(OpenAICompletion, "_extract_openai_token_usage"))

    def test_dedup_key_is_shared_across_capture_paths(self):
        # Both the event listener and the LiteLLM callback funnel through
        # _record_llm_usage, which dedups on the shared _LLM_USAGE_SEEN_KEYS set,
        # so one call seen by both paths is never double counted.
        from crucible.modules import section_00_bootstrap_and_utils as s0
        for fn in (s0._on_crewai_llm_call_completed, s0._record_litellm_success):
            self.assertIn("_record_llm_usage", inspect.getsource(fn))
        self.assertIn("_LLM_USAGE_SEEN_KEYS", inspect.getsource(s0._record_llm_usage))


if __name__ == "__main__":
    unittest.main()
