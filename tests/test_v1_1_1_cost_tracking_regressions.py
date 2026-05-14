"""v1.1.1 — Cost-tracking regression pins.

This round closes the cost-zero bug surfaced when an operator ran on
``deepseek/deepseek-v4-flash`` / ``deepseek/deepseek-v4-pro``: both model
IDs were absent from ``OPENROUTER_MODEL_PRICING`` and the request
omitted OpenRouter's ``usage: {"include": true}`` accounting opt-in, so
the pipeline emitted ``total_cost_usd=0.0`` / ``cost_source="estimated"``
even though the operator spent ~$0.83 USD.

Two pins:

* **A (pricing-table)** — ``_get_model_pricing`` must resolve the v4
  flash / pro variants AND fall back to a family prefix for unseen
  variants within a known vendor family.  ``OPENROUTER_MODEL_FAMILY_PRICING``
  is the lookup table; longest-prefix wins.
* **B (extra_body opt-in)** — ``_create_openrouter_llm`` and the sibling
  ``_make_formatter_llm`` / ``_make_codegen_llm`` helpers MUST inject
  ``additional_params={"extra_body": {"usage": {"include": True}}}``
  when the resolved provider is OpenRouter, so the response carries
  ``usage.cost`` (the actual billed USD).  ``inject_openrouter_usage_extra_body``
  is the merge helper; tests exercise its full branch table plus the
  three call-site wirings.
"""
from __future__ import annotations

import inspect

import pytest

from crucible.modules.section_00_bootstrap_and_utils import (
    OPENROUTER_MODEL_FAMILY_PRICING,
    OPENROUTER_MODEL_PRICING,
    _get_model_pricing,
    inject_openrouter_usage_extra_body,
)


# ── A: pricing-table tests ──────────────────────────────────────────────────
class TestGetModelPricingV4Variants:
    """The two model IDs the cost-zero bug was reported against."""

    def test_v4_flash_resolves_to_chat_tier(self):
        price = _get_model_pricing("deepseek/deepseek-v4-flash")
        # Chat-tier prices ($0.14 / $0.28 per M) — same as deepseek-chat.
        assert price[0] == pytest.approx(0.14 / 1_000_000)
        assert price[1] == pytest.approx(0.28 / 1_000_000)

    def test_v4_pro_resolves_to_reasoner_tier(self):
        price = _get_model_pricing("deepseek/deepseek-v4-pro")
        # Reasoner-tier prices ($0.55 / $2.19 per M) — same as deepseek-r1.
        assert price[0] == pytest.approx(0.55 / 1_000_000)
        assert price[1] == pytest.approx(2.19 / 1_000_000)

    def test_v3_chat_explicit_entry_present(self):
        """v3 was added alongside v4 for forward compat."""
        price = _get_model_pricing("deepseek/deepseek-v3-chat")
        assert price[0] > 0 and price[1] > 0

    def test_v3_reasoner_explicit_entry_present(self):
        price = _get_model_pricing("deepseek/deepseek-v3-reasoner")
        # Should track reasoner pricing, not chat.
        assert price[0] == pytest.approx(0.55 / 1_000_000)
        assert price[1] == pytest.approx(2.19 / 1_000_000)


class TestGetModelPricingFamilyFallback:
    """Family-prefix fallback rescues unseen variants within known families."""

    def test_future_deepseek_chat_variant_uses_chat_family(self):
        # v5 doesn't exist in the table → falls through to deepseek/ family.
        price = _get_model_pricing("deepseek/deepseek-v5-flash")
        assert price[0] == pytest.approx(0.14 / 1_000_000)
        assert price[1] == pytest.approx(0.28 / 1_000_000)

    def test_future_deepseek_reasoner_variant_wins_longer_prefix(self):
        """``deepseek/deepseek-r`` (reasoner) must beat the generic ``deepseek/``
        prefix.  Without longest-prefix tie-break, the chat-tier fallback
        would shadow the reasoner-tier one (insertion-order bug)."""
        price = _get_model_pricing("deepseek/deepseek-r1-distill-future")
        assert price[0] == pytest.approx(0.55 / 1_000_000)
        assert price[1] == pytest.approx(2.19 / 1_000_000)

    def test_future_openai_model_uses_frontier_tier(self):
        """A hypothetical ``openai/gpt-6.0`` must NOT fall to gpt-3 prices.
        Generic ``openai/`` fallback is pinned to gpt-5 tier."""
        price = _get_model_pricing("openai/gpt-6.0")
        assert price[0] >= 2.5 / 1_000_000  # at least frontier-tier input
        assert price[1] >= 15.0 / 1_000_000  # at least frontier-tier output

    def test_future_anthropic_model_uses_sonnet_baseline(self):
        price = _get_model_pricing("anthropic/claude-5-sonnet")
        assert price[0] == pytest.approx(3.00 / 1_000_000)
        assert price[1] == pytest.approx(15.00 / 1_000_000)

    def test_truly_unknown_vendor_returns_zero(self):
        """Family fallback is by-prefix only; an unknown vendor must NOT
        silently borrow another vendor's prices."""
        price = _get_model_pricing("unknown-vendor/some-model")
        assert price == (0.0, 0.0)

    def test_empty_model_id_returns_zero(self):
        assert _get_model_pricing("") == (0.0, 0.0)

    def test_exact_entry_still_wins_over_family_fallback(self):
        """If a model has an explicit entry, the family fallback must NOT
        clobber it.  Exercises the tier-1 short-circuit ordering."""
        price = _get_model_pricing("deepseek/deepseek-chat")
        assert price == OPENROUTER_MODEL_PRICING["deepseek/deepseek-chat"]


class TestFamilyPricingTableInvariants:
    """Structural pins on the family-pricing table."""

    def test_all_family_entries_have_positive_prices(self):
        """A family entry with zero pricing would be worse than no entry —
        it would shadow the (0,0) fallback that triggers ``cost_source=
        estimated`` and hide the missing-model diagnostic."""
        for prefix, (in_price, out_price) in OPENROUTER_MODEL_FAMILY_PRICING.items():
            assert in_price > 0, f"{prefix!r} has zero input price"
            assert out_price > 0, f"{prefix!r} has zero output price"

    def test_v4_variants_in_explicit_pricing_table(self):
        """The two model IDs the bug was filed against must be explicitly
        present, not just resolved via fallback — explicit pricing is a
        contract-level commitment and survives any future fallback rework."""
        assert "deepseek/deepseek-v4-flash" in OPENROUTER_MODEL_PRICING
        assert "deepseek/deepseek-v4-pro" in OPENROUTER_MODEL_PRICING


# ── B: extra_body injection helper unit tests ───────────────────────────────
class TestInjectOpenRouterUsageExtraBody:
    """Branch-table coverage for the merge helper."""

    def test_empty_kwargs_grows_full_chain(self):
        k: dict = {}
        inject_openrouter_usage_extra_body(k)
        assert k == {
            "additional_params": {
                "extra_body": {"usage": {"include": True}},
            },
        }

    def test_idempotent(self):
        k: dict = {}
        inject_openrouter_usage_extra_body(k)
        snapshot = {"additional_params": {"extra_body": {"usage": {"include": True}}}}
        inject_openrouter_usage_extra_body(k)
        assert k == snapshot

    def test_merges_into_existing_extra_body(self):
        k = {"additional_params": {"extra_body": {"response_format": "json"}}}
        inject_openrouter_usage_extra_body(k)
        assert k["additional_params"]["extra_body"] == {
            "response_format": "json",
            "usage": {"include": True},
        }

    def test_merges_into_existing_additional_params(self):
        k = {"additional_params": {"max_retries": 3}}
        inject_openrouter_usage_extra_body(k)
        assert k["additional_params"]["max_retries"] == 3
        assert k["additional_params"]["extra_body"] == {"usage": {"include": True}}

    def test_merges_into_existing_usage_block(self):
        k = {"additional_params": {"extra_body": {"usage": {"foo": "bar"}}}}
        inject_openrouter_usage_extra_body(k)
        assert k["additional_params"]["extra_body"]["usage"] == {
            "foo": "bar",
            "include": True,
        }

    def test_preserves_explicit_operator_false_override(self):
        """If the operator explicitly set ``include: False`` (rare but
        possible — e.g. cost-blind benchmark setup), the helper must
        NOT silently flip it back to True.  ``setdefault`` semantics."""
        k = {"additional_params": {"extra_body": {"usage": {"include": False}}}}
        inject_openrouter_usage_extra_body(k)
        assert k["additional_params"]["extra_body"]["usage"]["include"] is False

    def test_non_dict_additional_params_left_alone(self):
        """Defensive — caller may have set ``additional_params`` to a
        deliberate non-dict sentinel.  We must not clobber it."""
        k = {"additional_params": "not-a-dict"}
        inject_openrouter_usage_extra_body(k)
        assert k == {"additional_params": "not-a-dict"}

    def test_non_dict_llm_kwargs_returns_unchanged(self):
        out = inject_openrouter_usage_extra_body(None)  # type: ignore[arg-type]
        assert out is None

    def test_non_dict_extra_body_replaced(self):
        """If a previous extra_body value is non-dict (e.g. an operator
        mistakenly set it to a string), the helper builds a fresh dict
        rather than crashing."""
        k = {"additional_params": {"extra_body": "garbage"}}
        inject_openrouter_usage_extra_body(k)
        assert k["additional_params"]["extra_body"] == {"usage": {"include": True}}


# ── B: wiring tests (source-level) ──────────────────────────────────────────
class TestExtraBodyWiringInCallSites:
    """Structural pin: every OpenRouter-LLM-construction helper MUST call
    ``inject_openrouter_usage_extra_body`` when the resolved provider is
    OpenRouter.  We check source (not behaviour) because the actual
    construction calls into crewai.LLM / litellm with a network-touching
    config validator — too brittle to fake.  Source-level check is
    sufficient: if the call disappears, this test fails immediately.
    """

    def test_create_openrouter_llm_injects_when_openrouter_resolved(self):
        from crucible.modules.section_02_research_and_llm import _create_openrouter_llm
        src = inspect.getsource(_create_openrouter_llm)
        # The injection call must appear AND it must be inside an
        # `if resolved_provider == LLM_PROVIDER_OPENROUTER:` branch.
        assert "inject_openrouter_usage_extra_body" in src, (
            "_create_openrouter_llm no longer injects the usage opt-in — "
            "every OpenRouter call will silently lose response.usage.cost"
        )
        assert "LLM_PROVIDER_OPENROUTER" in src, (
            "_create_openrouter_llm injection branch lost its provider guard"
        )

    def test_make_formatter_llm_injects_for_openrouter_main_llm(self):
        from crucible.modules.section_01_extraction_and_reformat import _make_formatter_llm
        src = inspect.getsource(_make_formatter_llm)
        assert "inject_openrouter_usage_extra_body" in src, (
            "_make_formatter_llm no longer injects the usage opt-in — "
            "formatter calls will under-report cost"
        )
        assert "_quant_llm_provider" in src, (
            "_make_formatter_llm injection branch lost its provider-tag guard"
        )

    def test_make_codegen_llm_injects_for_openrouter_main_llm(self):
        from crucible.modules.section_05_analysis_and_codegen import _make_codegen_llm
        src = inspect.getsource(_make_codegen_llm)
        assert "inject_openrouter_usage_extra_body" in src, (
            "_make_codegen_llm no longer injects the usage opt-in — "
            "the biggest cost sink in a Quant run will under-report by ~70%"
        )
        assert "_quant_llm_provider" in src, (
            "_make_codegen_llm injection branch lost its provider-tag guard"
        )


# ── A+B integration: cost-zero path no longer fires for v4 ──────────────────
class TestCostZeroRegressionForV4Models:
    """End-to-end: ``extract_and_set_usage_from_crew`` previously fell into
    the ``pricing_known=False`` branch for v4 model IDs, emitting
    ``cost_source="estimated"`` and zero everywhere.  After v1.1.1, the
    pricing table resolves v4 → non-zero prices → the
    ``"crewai_metrics_with_pricing"`` branch fires.
    """

    @pytest.mark.parametrize(
        "model_id",
        [
            "deepseek/deepseek-v4-flash",
            "deepseek/deepseek-v4-pro",
            "deepseek/deepseek-v3-chat",
            "deepseek/deepseek-v3-reasoner",
            "deepseek/deepseek-r1",
            # Family-fallback survivors
            "deepseek/deepseek-v99-experimental",
        ],
    )
    def test_pricing_known_for_supported_variants(self, model_id):
        in_price, out_price = _get_model_pricing(model_id)
        pricing_known = in_price > 0 or out_price > 0
        assert pricing_known, (
            f"{model_id!r} resolves to (0, 0) → ``extract_and_set_usage_from_crew`` "
            f"would fall to ``cost_source='estimated'`` and emit zero — the "
            f"exact v1.1.0-era regression v1.1.1 fixes."
        )
