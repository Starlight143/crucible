"""
Tests for asset classification and signal extraction (Quant + non-Quant).
"""
from __future__ import annotations

import pytest

from crucible.features.run_insights.schema import (
    classify_asset_category,
    extract_signals,
)


# ── Asset classifier ──────────────────────────────────────────────────────────

@pytest.mark.parametrize("text,expected", [
    ("FTMO ctrader 黃金 EMA ATR", "gold"),
    ("XAUUSD intraday strategy", "gold"),
    ("白銀 mean-reversion", "silver"),
    ("crude oil futures", "oil"),
    ("BTCUSDT perpetual on Binance", "crypto"),
    ("ETH/USD spot momentum", "crypto"),
    ("EURUSD H1 trend", "forex"),
    ("nasdaq 100 index trend", "equity"),
    ("S&P 500 monthly rotation", "equity"),
    ("options IV crush strategy", "options"),
    ("Treasury yield curve", "bonds"),
    ("Random text with no match", "uncategorized"),
    ("", "uncategorized"),
])
def test_classify_asset_category(text, expected):
    assert classify_asset_category(text) == expected


def test_classify_uses_run_meta_fields():
    assert classify_asset_category(
        "",
        run_meta={"project_name": "btc_perp_rotation"},
    ) == "crypto"


# ── Signal extraction ────────────────────────────────────────────────────────

def test_signals_always_contain_mode_and_provider():
    signals = extract_signals(
        mode="Quant",
        user_problem="anything",
        run_meta={"llm_provider": "openrouter"},
    )
    assert "mode:quant" in signals
    assert "provider:openrouter" in signals


def test_signals_quant_extracts_asset():
    signals = extract_signals(
        mode="Quant",
        user_problem="FTMO 黃金 ctrader strategy",
        run_meta={"llm_provider": "openrouter"},
    )
    assert "asset:gold" in signals
    assert "venue:ftmo" in signals
    assert "framework:ctrader" in signals


def test_signals_non_quant_skips_asset():
    """SaaS/Agent/Scientist modes don't trade assets — no asset:* tag."""
    for mode in ("SaaS", "Agent", "Scientist"):
        signals = extract_signals(
            mode=mode,
            user_problem="build a stripe webhook handler for gold tier",
            run_meta={"llm_provider": "openrouter"},
        )
        # "gold tier" should NOT mis-classify as asset:gold for non-Quant.
        assert not any(s.startswith("asset:") for s in signals), (
            f"{mode}: unexpected asset tag in {signals}"
        )


def test_signals_dedup_preserves_first_seen_order():
    signals = extract_signals(
        mode="Quant",
        user_problem="FTMO ftmo FTMO 黃金",
        run_meta={"llm_provider": "openrouter"},
    )
    # 'venue:ftmo' must appear at most once.
    assert signals.count("venue:ftmo") == 1


def test_signals_extra_tags_appended():
    signals = extract_signals(
        mode="Quant",
        user_problem="gold strategy",
        run_meta={"llm_provider": "openrouter"},
        extra=["risk:low_drawdown", "BAD_TAG_NO_COLON"],
    )
    assert "risk:low_drawdown" in signals
    # tags without a colon should be silently dropped.
    assert "bad_tag_no_colon" not in signals


def test_signals_uncategorized_fallback_for_quant_with_no_asset_hint():
    signals = extract_signals(
        mode="Quant",
        user_problem="just some random words",
        run_meta={"llm_provider": "openrouter"},
    )
    assert "asset:uncategorized" in signals


def test_signals_lowercase_invariant():
    signals = extract_signals(
        mode="QuAnT",
        user_problem="GOLD",
        run_meta={"llm_provider": "OpenRouter"},
    )
    assert "mode:quant" in signals
    assert "provider:openrouter" in signals
