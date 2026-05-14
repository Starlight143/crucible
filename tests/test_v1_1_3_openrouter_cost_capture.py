"""Regression tests for v1.1.3 — OpenRouter cost capture end-to-end.

v1.1.1 wired ``inject_openrouter_usage_extra_body`` into three LLM
construction sites (section_02 main, section_01 formatter, section_05
codegen) so every OpenRouter request body opts into the
``usage: {include: true}`` accounting feature.  But only section_02
*also* registered the HTTP interceptor + langchain callback handler that
actually captures the returned ``usage.cost``.  Sections 01 and 05 sent
the opt-in flag but had no hook to read the response, so OpenRouter's
authoritative cost-in-USD silently fell through to the local pricing
table — ``cost_source="crewai_metrics_with_pricing"`` rather than
``"openrouter_api"``.  Codegen is the single largest cost sink in a
Quant run, so the discrepancy could be tens of percent.  The user
discovered this after switching from ``deepseek/deepseek-v4-pro`` to
``deepseek/deepseek-v4-flash``: the local table's $0.14/$0.28 estimate
for v4-flash diverged visibly from the actual OpenRouter bill.

Separately, the interceptor's ``on_inbound`` / ``aon_inbound`` hooks
delegated to a sync helper that calls ``response.json()`` — but the
httpx response handed in is unread, so ``.json()`` raises
``ResponseNotRead`` which the helper's broad except swallows.  v1.1.3
forces a ``read()`` / ``aread()`` before the capture call so the body
is loaded.

Structure of the test file:
- ``TestSection01InterceptorWiring`` — section_01 calls
  ``get_openrouter_http_interceptor`` and ``get_openrouter_callback_handler``
  inside its OpenRouter branch.
- ``TestSection05InterceptorWiring`` — same for section_05.
- ``TestInterceptorBodyReadFix`` — ``on_inbound`` reads the body before
  delegating; ``aon_inbound`` awaits aread before delegating.
- ``TestEndToEndCostCapture`` — feed a realistic OpenRouter response
  (with ``usage.cost`` populated) through the interceptor and assert
  the final usage context has ``cost_source="openrouter_api"``.
"""

from __future__ import annotations

import asyncio
import inspect
import re
import unittest

import httpx

from crucible.module_runtime import get_runtime
from crucible.modules.section_00_bootstrap_and_utils import (
    _OPENROUTER_HTTP_INTERCEPTOR,
    _capture_openrouter_usage_from_http_response,
    clear_openrouter_usage,
    get_openrouter_callback_handler,
    get_openrouter_http_interceptor,
)


def _build_openrouter_response(
    cost: float = 0.00123,
    prompt_tokens: int = 100,
    completion_tokens: int = 50,
    model: str = "deepseek/deepseek-v4-flash",
    request_host: str = "openrouter.ai",
) -> httpx.Response:
    """Build a mock OpenRouter chat-completion response with usage.cost.

    The response is unread on construction (mimicking what the httpx
    transport hands to the interceptor before openai SDK reads it).
    """
    import json

    body = json.dumps(
        {
            "id": "chatcmpl-test",
            "model": model,
            "choices": [{"message": {"role": "assistant", "content": "ok"}}],
            "usage": {
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "total_tokens": prompt_tokens + completion_tokens,
                "cost": cost,
            },
        }
    ).encode("utf-8")
    req = httpx.Request(
        "POST",
        f"https://{request_host}/api/v1/chat/completions",
    )
    return httpx.Response(
        status_code=200,
        headers={"content-type": "application/json"},
        content=body,
        request=req,
    )


class TestSection01InterceptorWiring(unittest.TestCase):
    """Section_01 ``_make_formatter_llm`` must register the OpenRouter
    HTTP interceptor + callback handler in addition to the
    ``inject_openrouter_usage_extra_body`` opt-in.  Without these two,
    the response's ``usage.cost`` is silently dropped on the floor."""

    def test_section_01_make_formatter_llm_registers_http_interceptor(self) -> None:
        from crucible.modules import section_01_extraction_and_reformat as s01

        src = inspect.getsource(s01)
        # The OpenRouter branch must call get_openrouter_http_interceptor.
        self.assertIn(
            "get_openrouter_http_interceptor",
            src,
            "section_01._make_formatter_llm must register the OpenRouter HTTP "
            "interceptor so usage.cost responses are captured at the transport "
            "layer (v1.1.3 cost-capture fix).",
        )

    def test_section_01_registers_callback_handler(self) -> None:
        from crucible.modules import section_01_extraction_and_reformat as s01

        src = inspect.getsource(s01)
        self.assertIn(
            "get_openrouter_callback_handler",
            src,
            "section_01._make_formatter_llm must register the OpenRouter "
            "callback handler (langchain on_llm_end fallback) so cost capture "
            "survives even when the HTTP interceptor doesn't fire.",
        )

    def test_section_01_interceptor_wiring_inside_openrouter_branch(self) -> None:
        """Both wires must live inside the ``if provider_tag == OPENROUTER:``
        branch so non-OpenRouter providers (Alibaba, Ollama) don't get a
        spurious interceptor attached."""
        from crucible.modules import section_01_extraction_and_reformat as s01

        src = inspect.getsource(s01)
        # Find the formatter LLM construction block — extract the OpenRouter branch.
        # Pattern: ``if provider_tag == LLM_PROVIDER_OPENROUTER:`` block.
        m = re.search(
            r"if provider_tag == LLM_PROVIDER_OPENROUTER:(.+?)(?:except Exception:|formatter_llm = )",
            src,
            re.DOTALL,
        )
        self.assertIsNotNone(
            m,
            "Could not locate the OpenRouter branch in section_01._make_formatter_llm.",
        )
        branch = m.group(1)
        self.assertIn("get_openrouter_http_interceptor", branch)
        self.assertIn("get_openrouter_callback_handler", branch)
        self.assertIn("inject_openrouter_usage_extra_body", branch)


class TestSection05InterceptorWiring(unittest.TestCase):
    """Section_05 ``_make_codegen_llm`` must register the OpenRouter
    HTTP interceptor + callback handler.  Codegen is the largest cost
    sink in a Quant run, so a missing interceptor here under-reports
    the entire summary by a wide margin."""

    def test_section_05_make_codegen_llm_registers_http_interceptor(self) -> None:
        from crucible.modules import section_05_analysis_and_codegen as s05

        src = inspect.getsource(s05)
        self.assertIn(
            "get_openrouter_http_interceptor",
            src,
            "section_05._make_codegen_llm must register the OpenRouter HTTP "
            "interceptor (v1.1.3 cost-capture fix).",
        )

    def test_section_05_registers_callback_handler(self) -> None:
        from crucible.modules import section_05_analysis_and_codegen as s05

        src = inspect.getsource(s05)
        self.assertIn(
            "get_openrouter_callback_handler",
            src,
            "section_05._make_codegen_llm must register the OpenRouter "
            "callback handler (v1.1.3 cost-capture fix).",
        )

    def test_section_05_interceptor_wiring_inside_openrouter_branch(self) -> None:
        from crucible.modules import section_05_analysis_and_codegen as s05

        src = inspect.getsource(s05)
        # The codegen branch may have helpers between provider_tag check and
        # codegen_llm = ; scan for the relevant guarded block.
        m = re.search(
            r"if provider_tag == LLM_PROVIDER_OPENROUTER:(.+?)(?:except Exception:|codegen_llm = )",
            src,
            re.DOTALL,
        )
        self.assertIsNotNone(
            m,
            "Could not locate the OpenRouter branch in section_05._make_codegen_llm.",
        )
        branch = m.group(1)
        self.assertIn("get_openrouter_http_interceptor", branch)
        self.assertIn("get_openrouter_callback_handler", branch)
        self.assertIn("inject_openrouter_usage_extra_body", branch)


class TestInterceptorBodyReadFix(unittest.TestCase):
    """The interceptor hooks must force-load the response body before
    delegating to ``_capture_openrouter_usage_from_http_response``,
    which calls ``response.json()`` synchronously.  Without this,
    async-transport responses arrive with unread streams and the
    capture helper silently swallows ``ResponseNotRead``."""

    def setUp(self) -> None:
        clear_openrouter_usage()

    def tearDown(self) -> None:
        clear_openrouter_usage()

    def test_on_inbound_loads_body_for_unread_sync_response(self) -> None:
        """Constructing a Response without calling .read() leaves the body
        accessible via ``.content`` only if the response was built from
        bytes (our test case); the interceptor still must invoke read()
        defensively so future httpx versions (where unread is more strict)
        keep working."""
        interceptor = get_openrouter_http_interceptor()
        self.assertIsNotNone(interceptor)
        resp = _build_openrouter_response(cost=0.005)
        # Sanity: response.json() should work on this response (sync, content
        # supplied directly).
        out = interceptor.on_inbound(resp)
        self.assertIs(out, resp)
        # Body was either read OR readable; the capture must have succeeded
        # AND produced cost_source="openrouter_api".
        rt = get_runtime()
        usage = rt.get_last_openrouter_usage()
        self.assertEqual(usage.cost_source, "openrouter_api")
        self.assertAlmostEqual(usage.total_cost_usd, 0.005, places=6)

    def test_aon_inbound_awaits_aread_before_capture(self) -> None:
        """``aon_inbound`` is an async coroutine; running it through asyncio
        must result in a successful capture (``cost_source="openrouter_api"``)
        for an httpx Response that was constructed without an explicit
        ``.aread()``.

        Note: ``set_openrouter_usage`` writes to a ``ContextVar`` whose scope
        is the asyncio task's context — when ``asyncio.run()`` returns, the
        ContextVar reverts to the pre-run value.  So the assertion has to
        live INSIDE the coroutine where the capture happened."""
        interceptor = get_openrouter_http_interceptor()
        self.assertIsNotNone(interceptor)
        resp = _build_openrouter_response(cost=0.007)
        clear_openrouter_usage()
        rt = get_runtime()

        captured: dict = {}

        async def run() -> httpx.Response:
            out = await interceptor.aon_inbound(resp)
            # Read the ContextVar while still inside the asyncio task scope.
            u = rt.get_last_openrouter_usage()
            captured["cost_source"] = u.cost_source
            captured["total_cost_usd"] = u.total_cost_usd
            return out

        out = asyncio.run(run())
        self.assertIs(out, resp)
        self.assertEqual(captured["cost_source"], "openrouter_api")
        self.assertAlmostEqual(captured["total_cost_usd"], 0.007, places=6)

    def test_on_inbound_source_calls_read_before_capture(self) -> None:
        """Structural pin: ``on_inbound`` must invoke ``.read()`` (sync)
        before ``_capture_openrouter_usage_from_http_response``."""
        interceptor = get_openrouter_http_interceptor()
        self.assertIsNotNone(interceptor)
        src = inspect.getsource(interceptor.__class__.on_inbound)
        # The read() call must precede the capture call in source order.
        read_idx = src.find(".read()")
        capture_idx = src.find("_capture_openrouter_usage_from_http_response")
        self.assertGreater(read_idx, -1, "on_inbound must call .read() defensively")
        self.assertGreater(capture_idx, read_idx,
                           "on_inbound must call .read() BEFORE delegating to the capture helper")

    def test_aon_inbound_source_calls_aread_before_capture(self) -> None:
        """Structural pin: ``aon_inbound`` must invoke ``await .aread()`` before
        ``_capture_openrouter_usage_from_http_response``."""
        interceptor = get_openrouter_http_interceptor()
        self.assertIsNotNone(interceptor)
        src = inspect.getsource(interceptor.__class__.aon_inbound)
        aread_idx = src.find(".aread()")
        capture_idx = src.find("_capture_openrouter_usage_from_http_response")
        self.assertGreater(aread_idx, -1, "aon_inbound must call .aread() defensively")
        self.assertGreater(capture_idx, aread_idx,
                           "aon_inbound must call .aread() BEFORE delegating to the capture helper")

    def test_event_stream_responses_skip_force_read(self) -> None:
        """Streaming chat completions return ``content-type: text/event-stream``;
        force-reading the body would block until the stream is fully consumed
        and break the openai SDK's streaming iteration.  The interceptor must
        skip its defensive read for these."""
        interceptor = get_openrouter_http_interceptor()
        self.assertIsNotNone(interceptor)
        src_sync = inspect.getsource(interceptor.__class__.on_inbound)
        src_async = inspect.getsource(interceptor.__class__.aon_inbound)
        # Both hooks must reference event-stream guard so streaming is preserved.
        self.assertIn("event-stream", src_sync)
        self.assertIn("event-stream", src_async)


class TestEndToEndCostCapture(unittest.TestCase):
    """Drive the interceptor with a realistic OpenRouter response and
    confirm the final cost-tracking context reports
    ``cost_source="openrouter_api"`` with the exact ``usage.cost``
    from the payload — the contract operators rely on for billing
    reconciliation."""

    def setUp(self) -> None:
        clear_openrouter_usage()

    def tearDown(self) -> None:
        clear_openrouter_usage()

    def test_realistic_openrouter_response_yields_openrouter_api_source(self) -> None:
        interceptor = get_openrouter_http_interceptor()
        self.assertIsNotNone(interceptor)
        # Simulate a v4-flash response with non-zero cost.
        resp = _build_openrouter_response(
            cost=0.000812,
            prompt_tokens=4321,
            completion_tokens=1234,
            model="deepseek/deepseek-v4-flash",
        )
        interceptor.on_inbound(resp)
        rt = get_runtime()
        usage = rt.get_last_openrouter_usage()
        self.assertEqual(usage.cost_source, "openrouter_api")
        self.assertAlmostEqual(usage.total_cost_usd, 0.000812, places=6)
        self.assertEqual(usage.input_tokens, 4321)
        self.assertEqual(usage.output_tokens, 1234)
        self.assertEqual(usage.model_id, "deepseek/deepseek-v4-flash")

    def test_missing_cost_field_falls_back_to_token_pricing(self) -> None:
        """When OpenRouter omits ``usage.cost`` (e.g. opt-in flag stripped
        upstream), the helper should still record token counts and use the
        local pricing table — ``cost_source="openrouter_tokens_with_pricing"``."""
        interceptor = get_openrouter_http_interceptor()
        self.assertIsNotNone(interceptor)
        # Build a response without `cost`.
        import json

        body = json.dumps(
            {
                "id": "chatcmpl-test",
                "model": "deepseek/deepseek-v4-flash",
                "choices": [{"message": {"role": "assistant", "content": "ok"}}],
                "usage": {
                    "prompt_tokens": 1000,
                    "completion_tokens": 500,
                    "total_tokens": 1500,
                    # NOTE: no `cost` field
                },
            }
        ).encode("utf-8")
        req = httpx.Request("POST", "https://openrouter.ai/api/v1/chat/completions")
        resp = httpx.Response(
            status_code=200,
            headers={"content-type": "application/json"},
            content=body,
            request=req,
        )
        interceptor.on_inbound(resp)
        rt = get_runtime()
        usage = rt.get_last_openrouter_usage()
        # cost==0 → fall back to local pricing table for v4-flash (0.14/M input,
        # 0.28/M output): 1000 × 0.14e-6 + 500 × 0.28e-6 = 0.00014 + 0.00014 = 0.00028
        self.assertEqual(usage.cost_source, "openrouter_tokens_with_pricing")
        self.assertAlmostEqual(usage.total_cost_usd, 0.00028, places=7)


if __name__ == "__main__":
    unittest.main()
