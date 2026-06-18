"""Regression tests for the authoritative OpenRouter billed-cost ledger.

Originally v1.1.12 (HTTP-interceptor feeder).  In v1.2.3 the feeder changed to
the LiteLLM success callback (``_record_litellm_success``) — the one chokepoint
CrewAI actually routes through; the previous crewai ``BaseInterceptor`` never
fired because crewai 1.14.x ``LLM`` silently drops the ``interceptor=`` kwarg, so
the ledger stayed empty and the headline fell back to the lossy, multi-x-inflated
CrewAI usage-metrics path.

The LEDGER and ``_reconcile_cost_summary_with_billing`` are otherwise unchanged
(plus a v1.2.3 token override): every billed OpenRouter response contributes
exactly one row carrying the precise ``usage.cost`` + tokens OpenRouter returned,
deduped by response id, on a lock-guarded module global (NOT a ContextVar) so
cross-thread writes are visible, reset only at run start.
``get_openrouter_billed_total()`` therefore equals the exact Σ(usage.cost) — the
number on the dashboard — and reconcile promotes both USD and tokens to the
headline.  These tests pin the ledger guards, thread-safety, capture gating, and
USD reconciliation; the token override + capture mechanics live in
``test_v1_2_3_cost_litellm_capture.py``.
"""
from __future__ import annotations

import threading
import unittest

from crucible.module_runtime import get_runtime
from crucible.modules.section_00_bootstrap_and_utils import (
    _append_openrouter_billed_entry,
    _record_litellm_success,
    clear_openrouter_usage,
    get_openrouter_billed_count,
    get_openrouter_billed_ledger,
    get_openrouter_billed_total,
    reset_llm_usage_dedup,
    reset_openrouter_billed_ledger,
)
from crucible.modules.section_07_selfcheck_output_main import (
    _reconcile_cost_summary_with_billing,
)

_OR_KWARGS = {"model": "openrouter/deepseek/deepseek-v4-flash",
              "litellm_params": {"api_base": "https://openrouter.ai/api/v1"}}
_ALIBABA_KWARGS = {"model": "dashscope/qwen",
                   "litellm_params": {"api_base": "https://coding-intl.dashscope.aliyuncs.com/v1"}}


def _fake_response(resp_id, prompt, completion, *, cost=None, model="deepseek/deepseek-v4-flash"):
    usage = {"prompt_tokens": prompt, "completion_tokens": completion,
             "total_tokens": prompt + completion}
    if cost is not None:
        usage["cost"] = cost
    ns = type("Resp", (), {})()
    ns.id = resp_id
    ns.model = model
    ns.usage = usage
    return ns


def _feed(resp_id, *, cost=0.00123, prompt_tokens=100, completion_tokens=50,
          model="deepseek/deepseek-v4-flash", kwargs=None):
    """Drive one completion through the real LiteLLM-callback feeder → ledger."""
    _record_litellm_success(
        kwargs if kwargs is not None else dict(_OR_KWARGS, model=f"openrouter/{model}"),
        _fake_response(resp_id, prompt_tokens, completion_tokens, cost=cost, model=model),
    )


class _LedgerIsolatedTestCase(unittest.TestCase):
    """Reset the process-global ledger / dedup / usage around every test."""

    def setUp(self) -> None:
        get_runtime()  # ensure cross-module namespace sync so the feeder resolves
        reset_openrouter_billed_ledger()
        reset_llm_usage_dedup()
        clear_openrouter_usage()

    def tearDown(self) -> None:
        reset_openrouter_billed_ledger()
        reset_llm_usage_dedup()
        clear_openrouter_usage()


class TestBilledLedgerBasics(_LedgerIsolatedTestCase):
    def test_single_response_records_exact_cost(self) -> None:
        _feed("g1", cost=0.004200)
        self.assertEqual(get_openrouter_billed_count(), 1)
        self.assertAlmostEqual(get_openrouter_billed_total(), 0.004200, places=9)

    def test_multiple_responses_sum_exactly(self) -> None:
        costs = [0.001, 0.0025, 0.5, 0.000003]
        for i, c in enumerate(costs):
            _feed(f"g{i}", cost=c)
        self.assertEqual(get_openrouter_billed_count(), len(costs))
        self.assertAlmostEqual(get_openrouter_billed_total(), sum(costs), places=9)

    def test_ledger_rows_carry_model_and_tokens(self) -> None:
        _feed("g1", cost=0.01, prompt_tokens=321, completion_tokens=123,
              model="deepseek/deepseek-v4-flash")
        rows = get_openrouter_billed_ledger()
        self.assertEqual(len(rows), 1)
        self.assertAlmostEqual(rows[0]["total_cost_usd"], 0.01, places=9)
        self.assertEqual(rows[0]["input_tokens"], 321)
        self.assertEqual(rows[0]["output_tokens"], 123)
        self.assertIn("deepseek", rows[0]["model_id"])

    def test_reset_clears_the_ledger(self) -> None:
        _append_openrouter_billed_entry(total_cost_usd=0.5)
        self.assertEqual(get_openrouter_billed_count(), 1)
        reset_openrouter_billed_ledger()
        self.assertEqual(get_openrouter_billed_count(), 0)
        self.assertEqual(get_openrouter_billed_total(), 0.0)


class TestLedgerSurvivesPerStageClear(_LedgerIsolatedTestCase):
    """The per-stage ``clear_openrouter_usage()`` must NOT wipe the authoritative
    billed ledger — the boundary at which orphan-kickoff costs used to be lost."""

    def test_clear_openrouter_usage_does_not_touch_billed_ledger(self) -> None:
        _feed("g1", cost=0.0150)
        self.assertAlmostEqual(get_openrouter_billed_total(), 0.0150, places=9)
        clear_openrouter_usage()
        clear_openrouter_usage()
        self.assertEqual(get_openrouter_billed_count(), 1)
        self.assertAlmostEqual(get_openrouter_billed_total(), 0.0150, places=9)


class TestBilledLedgerRejectsBadInput(_LedgerIsolatedTestCase):
    def test_zero_and_negative_cost_not_recorded(self) -> None:
        _append_openrouter_billed_entry(total_cost_usd=0.0)
        _append_openrouter_billed_entry(total_cost_usd=-1.0)
        self.assertEqual(get_openrouter_billed_count(), 0)

    def test_nan_and_inf_cost_not_recorded(self) -> None:
        _append_openrouter_billed_entry(total_cost_usd=float("nan"))
        _append_openrouter_billed_entry(total_cost_usd=float("inf"))
        _append_openrouter_billed_entry(total_cost_usd=float("-inf"))
        self.assertEqual(get_openrouter_billed_count(), 0)

    def test_response_without_cost_field_not_recorded(self) -> None:
        # usage present but no `cost` key → tokens still tracked, but the billed
        # ledger (actual-billing only) stays empty.
        _feed("g1", cost=None)
        self.assertEqual(get_openrouter_billed_count(), 0)

    def test_non_openrouter_provider_not_recorded(self) -> None:
        # Alibaba coding-plan: provider != OpenRouter, cost forced 0 → not billed.
        _feed("ali", cost=0.01, model="qwen", kwargs=_ALIBABA_KWARGS)
        self.assertEqual(get_openrouter_billed_count(), 0)


class TestIdempotency(_LedgerIsolatedTestCase):
    def test_same_response_id_captured_once(self) -> None:
        for _ in range(3):
            _feed("same-id", cost=0.02)  # same response id every time
        self.assertEqual(get_openrouter_billed_count(), 1)
        self.assertAlmostEqual(get_openrouter_billed_total(), 0.02, places=9)


class TestThreadSafety(_LedgerIsolatedTestCase):
    """The ledger is a lock-guarded module global, NOT a ContextVar — so writes
    from worker threads are visible to the reader."""

    def test_concurrent_appends_are_lossless(self) -> None:
        n_threads = 8
        per_thread = 200
        unit = 0.001

        def worker() -> None:
            for _ in range(per_thread):
                _append_openrouter_billed_entry(total_cost_usd=unit)

        threads = [threading.Thread(target=worker) for _ in range(n_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(get_openrouter_billed_count(), n_threads * per_thread)
        self.assertAlmostEqual(
            get_openrouter_billed_total(), n_threads * per_thread * unit, places=6
        )


class TestReconciliation(_LedgerIsolatedTestCase):
    def test_empty_ledger_returns_summary_unchanged(self) -> None:
        summary = {"total_cost_usd": 0.5, "cost_source": "crewai_metrics_with_pricing"}
        out = _reconcile_cost_summary_with_billing(summary)
        self.assertEqual(out["total_cost_usd"], 0.5)
        self.assertEqual(out["cost_source"], "crewai_metrics_with_pricing")
        self.assertNotIn("total_cost_usd_billed", out)

    def test_non_dict_passthrough(self) -> None:
        self.assertIsNone(_reconcile_cost_summary_with_billing(None))

    def test_billed_total_overrides_headline_and_scales_breakdown(self) -> None:
        _append_openrouter_billed_entry(total_cost_usd=1.0, input_tokens=1000, output_tokens=500)
        summary = {
            "total_cost_usd": 0.5,
            "input_cost_usd": 0.2,
            "output_cost_usd": 0.3,
            "cache_cost_usd": 0.0,
            "cost_source": "crewai_metrics_with_pricing",
            "total_tokens": 9_999_999,
        }
        out = _reconcile_cost_summary_with_billing(summary)
        self.assertAlmostEqual(out["total_cost_usd"], 1.0, places=9)
        self.assertAlmostEqual(out["total_cost_usd_billed"], 1.0, places=9)
        self.assertAlmostEqual(out["total_cost_usd_attributed"], 0.5, places=9)
        self.assertEqual(out["billed_request_count"], 1)
        self.assertEqual(out["cost_source"], "openrouter_api")
        # USD breakdown scaled by billed/attributed == 2.0
        self.assertAlmostEqual(out["input_cost_usd"], 0.4, places=9)
        self.assertAlmostEqual(out["output_cost_usd"], 0.6, places=9)
        self.assertAlmostEqual(
            out["input_cost_usd"] + out["output_cost_usd"], out["total_cost_usd"], places=9
        )
        # v1.2.3 — tokens are promoted from the ledger too (1500, not the 9.9M)
        self.assertEqual(out["total_tokens"], 1500)
        self.assertEqual(out["total_tokens_attributed"], 9_999_999)

    def test_zero_attributed_still_sets_headline(self) -> None:
        _append_openrouter_billed_entry(total_cost_usd=0.0731)
        summary = {"total_cost_usd": 0.0, "input_cost_usd": 0.0, "output_cost_usd": 0.0}
        out = _reconcile_cost_summary_with_billing(summary)
        self.assertAlmostEqual(out["total_cost_usd"], 0.0731, places=9)
        self.assertEqual(out["cost_source"], "openrouter_api")
        self.assertAlmostEqual(out["total_cost_usd_attributed"], 0.0, places=9)


class TestOrphanKickoffRecovered(_LedgerIsolatedTestCase):
    """A billed OpenRouter response with NO matching per-stage record still lands
    in the authoritative total, so the headline never reads below the dashboard."""

    def test_orphan_cost_is_recovered_by_billed_total(self) -> None:
        _feed("orphan", cost=0.0123)
        accountant_summary = {"total_cost_usd": 0.0, "cost_source": "estimated"}
        reconciled = _reconcile_cost_summary_with_billing(accountant_summary)
        self.assertAlmostEqual(reconciled["total_cost_usd"], 0.0123, places=9)
        self.assertEqual(reconciled["cost_source"], "openrouter_api")
        self.assertEqual(reconciled["billed_request_count"], 1)


if __name__ == "__main__":
    unittest.main()
