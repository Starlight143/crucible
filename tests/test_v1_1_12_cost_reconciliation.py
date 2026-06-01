"""Regression tests for v1.1.12 — authoritative OpenRouter billed-cost ledger.

Background
----------
Before v1.1.12 the headline ``total_cost_usd`` the operator saw (in the
``--cost-report`` console output and in ``run_meta.json`` → WebUI dashboard)
was reconstructed *solely* from ``AgentCostAccountant``, which is built by the
per-stage ``_record_cost`` "accumulate-into-ContextVar → read → clear" dance.

That dance is lossy:

* Several ``crew.kickoff()`` call sites (section_01 reformat crews, the
  section_02 direction-seed plan success path, section_04 problem-breakdown /
  smart-queries, section_06 api-version analysis, the external Critic) have **no
  matching ``_record_cost``**.  Their real OpenRouter cost is accumulated into
  the usage ContextVar but is then either mis-attributed to an adjacent stage —
  or **dropped entirely** when a ``clear_openrouter_usage()`` runs first (a
  cache hit, a pipeline reset).  Net effect: the headline reads *lower* than the
  OpenRouter dashboard.
* The summed total blends actual (``openrouter_api``) rows with locally
  estimated rows (``openrouter_tokens_with_pricing`` / ``crewai_metrics_with_pricing``
  / ``estimated``) while labelling the whole thing "OpenRouter API (actual
  billing)".  A stale/incomplete local pricing table then drags estimated rows
  away from the real bill.

The fix feeds an **authoritative billed-cost ledger** straight from the single
correct chokepoint — the HTTP interceptor
(``_capture_openrouter_usage_from_http_response``).  Every billed OpenRouter
response contributes exactly one ledger row carrying the precise ``usage.cost``
OpenRouter returned, idempotency-guarded, independent of stage attribution.
``get_openrouter_billed_total()`` therefore equals the exact Σ(usage.cost) — the
number on the dashboard.  The ledger is a lock-guarded module global (NOT a
ContextVar) so cross-thread writes are visible, and it is reset only at run
start, never by the per-stage ``clear_openrouter_usage()``.

``section_07._reconcile_cost_summary_with_billing`` promotes the ledger sum to
the headline whenever real billing was captured.
"""

from __future__ import annotations

import json
import threading
import unittest

import httpx

from crucible.modules.section_00_bootstrap_and_utils import (
    _append_openrouter_billed_entry,
    _capture_openrouter_usage_from_http_response,
    clear_openrouter_usage,
    get_openrouter_billed_count,
    get_openrouter_billed_ledger,
    get_openrouter_billed_total,
    get_openrouter_http_interceptor,
    reset_openrouter_billed_ledger,
)
from crucible.modules.section_07_selfcheck_output_main import (
    _reconcile_cost_summary_with_billing,
)


def _build_openrouter_response(
    cost: float = 0.00123,
    prompt_tokens: int = 100,
    completion_tokens: int = 50,
    model: str = "deepseek/deepseek-v4-flash",
    request_host: str = "openrouter.ai",
    include_cost: bool = True,
) -> httpx.Response:
    """Build a mock OpenRouter chat-completion response (unread on construction)."""
    usage: dict = {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": prompt_tokens + completion_tokens,
    }
    if include_cost:
        usage["cost"] = cost
    body = json.dumps(
        {
            "id": "chatcmpl-test",
            "model": model,
            "choices": [{"message": {"role": "assistant", "content": "ok"}}],
            "usage": usage,
        }
    ).encode("utf-8")
    req = httpx.Request("POST", f"https://{request_host}/api/v1/chat/completions")
    return httpx.Response(
        status_code=200,
        headers={"content-type": "application/json"},
        content=body,
        request=req,
    )


class _LedgerIsolatedTestCase(unittest.TestCase):
    """Reset the process-global ledger (and per-stage usage) around every test
    so the module-global state never leaks between cases."""

    def setUp(self) -> None:
        reset_openrouter_billed_ledger()
        clear_openrouter_usage()

    def tearDown(self) -> None:
        reset_openrouter_billed_ledger()
        clear_openrouter_usage()


class TestBilledLedgerBasics(_LedgerIsolatedTestCase):
    def test_single_response_records_exact_cost(self) -> None:
        interceptor = get_openrouter_http_interceptor()
        self.assertIsNotNone(interceptor)
        interceptor.on_inbound(_build_openrouter_response(cost=0.004200))
        self.assertEqual(get_openrouter_billed_count(), 1)
        self.assertAlmostEqual(get_openrouter_billed_total(), 0.004200, places=9)

    def test_multiple_responses_sum_exactly(self) -> None:
        interceptor = get_openrouter_http_interceptor()
        costs = [0.001, 0.0025, 0.5, 0.000003]
        for c in costs:
            # fresh response objects (idempotency sentinel is per-response)
            interceptor.on_inbound(_build_openrouter_response(cost=c))
        self.assertEqual(get_openrouter_billed_count(), len(costs))
        self.assertAlmostEqual(get_openrouter_billed_total(), sum(costs), places=9)

    def test_ledger_rows_carry_model_and_tokens(self) -> None:
        interceptor = get_openrouter_http_interceptor()
        interceptor.on_inbound(
            _build_openrouter_response(
                cost=0.01, prompt_tokens=321, completion_tokens=123,
                model="deepseek/deepseek-v4-flash",
            )
        )
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
    """THE core invariant: the per-stage ``clear_openrouter_usage()`` (called
    after every ``_record_cost``) must NOT wipe the authoritative billed
    ledger.  This is exactly the boundary at which orphan-kickoff costs used to
    be dropped from the headline."""

    def test_clear_openrouter_usage_does_not_touch_billed_ledger(self) -> None:
        interceptor = get_openrouter_http_interceptor()
        interceptor.on_inbound(_build_openrouter_response(cost=0.0150))
        self.assertAlmostEqual(get_openrouter_billed_total(), 0.0150, places=9)
        # Simulate a per-stage record's clear, then another stage's clear.
        clear_openrouter_usage()
        clear_openrouter_usage()
        # The authoritative ledger is unaffected.
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
        interceptor = get_openrouter_http_interceptor()
        # usage present but no `cost` key → token capture still happens but the
        # billed ledger (actual-billing only) stays empty.
        interceptor.on_inbound(_build_openrouter_response(include_cost=False))
        self.assertEqual(get_openrouter_billed_count(), 0)

    def test_non_openrouter_host_not_recorded(self) -> None:
        interceptor = get_openrouter_http_interceptor()
        # Alibaba coding-plan host: provider != OpenRouter → not in billed ledger.
        interceptor.on_inbound(
            _build_openrouter_response(
                cost=0.01, request_host="coding-intl.dashscope.aliyuncs.com",
            )
        )
        self.assertEqual(get_openrouter_billed_count(), 0)


class TestIdempotency(_LedgerIsolatedTestCase):
    def test_same_response_captured_once(self) -> None:
        interceptor = get_openrouter_http_interceptor()
        resp = _build_openrouter_response(cost=0.02)
        interceptor.on_inbound(resp)
        interceptor.on_inbound(resp)  # second call on the SAME response object
        interceptor.on_inbound(resp)
        self.assertEqual(get_openrouter_billed_count(), 1)
        self.assertAlmostEqual(get_openrouter_billed_total(), 0.02, places=9)


class TestThreadSafety(_LedgerIsolatedTestCase):
    """The ledger is a lock-guarded module global, NOT a ContextVar — so writes
    from worker threads are visible to the reader.  This pins both the lock
    correctness and the cross-thread visibility that motivated abandoning a
    ContextVar for the authoritative total."""

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
        _append_openrouter_billed_entry(total_cost_usd=1.0)
        summary = {
            "total_cost_usd": 0.5,
            "input_cost_usd": 0.2,
            "output_cost_usd": 0.3,
            "cache_cost_usd": 0.0,
            "cost_source": "crewai_metrics_with_pricing",
            "total_tokens": 1500,
        }
        out = _reconcile_cost_summary_with_billing(summary)
        self.assertAlmostEqual(out["total_cost_usd"], 1.0, places=9)
        self.assertAlmostEqual(out["total_cost_usd_billed"], 1.0, places=9)
        self.assertAlmostEqual(out["total_cost_usd_attributed"], 0.5, places=9)
        self.assertEqual(out["billed_request_count"], 1)
        self.assertEqual(out["cost_source"], "openrouter_api")
        # breakdown scaled by billed/attributed == 2.0
        self.assertAlmostEqual(out["input_cost_usd"], 0.4, places=9)
        self.assertAlmostEqual(out["output_cost_usd"], 0.6, places=9)
        # input + output parts sum to the authoritative headline
        self.assertAlmostEqual(
            out["input_cost_usd"] + out["output_cost_usd"], out["total_cost_usd"], places=9
        )
        # non-cost fields preserved
        self.assertEqual(out["total_tokens"], 1500)

    def test_zero_attributed_still_sets_headline(self) -> None:
        # Orphan-only run: accountant recorded nothing, but billing was captured.
        _append_openrouter_billed_entry(total_cost_usd=0.0731)
        summary = {"total_cost_usd": 0.0, "input_cost_usd": 0.0, "output_cost_usd": 0.0}
        out = _reconcile_cost_summary_with_billing(summary)
        self.assertAlmostEqual(out["total_cost_usd"], 0.0731, places=9)
        self.assertEqual(out["cost_source"], "openrouter_api")
        self.assertAlmostEqual(out["total_cost_usd_attributed"], 0.0, places=9)


class TestOrphanKickoffRecovered(_LedgerIsolatedTestCase):
    """End-to-end pin for the actual bug: a billed OpenRouter response captured
    by the interceptor with NO matching ``_record_cost`` (an orphan kickoff)
    still lands in the authoritative total, so the headline no longer reads
    lower than the OpenRouter dashboard."""

    def test_orphan_cost_is_recovered_by_billed_total(self) -> None:
        interceptor = get_openrouter_http_interceptor()
        # An orphan kickoff: HTTP response intercepted, but the caller never
        # invoked _record_cost, so the accountant has no row for it.
        interceptor.on_inbound(_build_openrouter_response(cost=0.0123))
        # Accountant-derived summary (as if no _record_cost ran for this call).
        accountant_summary = {"total_cost_usd": 0.0, "cost_source": "estimated"}
        reconciled = _reconcile_cost_summary_with_billing(accountant_summary)
        self.assertAlmostEqual(reconciled["total_cost_usd"], 0.0123, places=9)
        self.assertEqual(reconciled["cost_source"], "openrouter_api")
        self.assertEqual(reconciled["billed_request_count"], 1)


if __name__ == "__main__":
    unittest.main()
