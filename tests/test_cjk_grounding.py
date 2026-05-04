"""Regression tests for CJK grounding in the Direction Debate stage.

The Direction Debate phase historically failed with::

    [Warn] Direction debate could not produce a valid decision
    (evidence gaps or JSON parse failure after all retries).
    Continuing without direction preamble.

…on every Traditional Chinese run.  The two layered root causes:

1. ``_tokenize_for_grounding`` used an ASCII-only regex
   (``[a-z0-9][a-z0-9_./+-]{2,}``) that produced an empty token set for
   any Chinese-language claim.  Every Chinese claim then failed
   ``_filter_claims_by_citations``, the synthesizer's
   ``evidence_coverage.grounded_claims`` collapsed to 0, and the rest of
   the gating logic treated the run as evidence-empty.

2. ``_should_force_direction_none`` short-circuited to ``"none"`` whenever
   ``grounded_claims <= 0 OR citation_count <= 0`` — so any single
   synthesizer hiccup that emptied the structured claim arrays force-
   killed the judge even when the librarian had returned a healthy
   citation pool.

Both layers are exercised below.
"""

from __future__ import annotations

import pytest

from crucible.modules.section_04_web_research_and_direction import (
    _citation_support_score,
    _filter_claims_by_citations,
    _should_force_direction_none,
    _tokenize_for_grounding,
)
from crucible.modules.section_03_models_and_context import (
    DirectionDecision,
    DirectionOption,
    ResearchCitation,
    ResearchContext,
)


# ─────────────────────────────────────────────────────────────────────
# 1) _tokenize_for_grounding — CJK shingle extraction
# ─────────────────────────────────────────────────────────────────────


class TestTokenizerCJK:
    def test_pure_chinese_claim_produces_tokens(self):
        # An ASCII-only tokenizer would return set() and the caller would
        # see zero overlap; the CJK shingle pass must produce real tokens.
        tokens = _tokenize_for_grounding("使用ATR與布林帶寬度識別資金費率波動率區間切換")
        assert tokens, "Chinese-only claim must produce non-empty token set"
        # The full ASCII identifier survives the CJK pass.
        assert "atr" in tokens
        # 2-char Chinese shingles appear and are searchable.
        assert "資金" in tokens
        assert "費率" in tokens
        assert "波動" in tokens
        # 3-char window captures multi-char compounds like 資金費 / 費率波.
        assert "資金費" in tokens or "費率波" in tokens

    def test_english_only_unchanged(self):
        # Make sure the new CJK pass does not regress English-only input.
        tokens = _tokenize_for_grounding(
            "CCXT (binancefutures) supports OHLCV via REST API"
        )
        assert "ccxt" in tokens
        assert "binancefutures" in tokens
        assert "ohlcv" in tokens
        assert "rest" in tokens

    def test_single_cjk_char_filtered(self):
        # Single Chinese characters ("的", "是", "了", …) are too generic
        # to ground anything — they would dominate token sets and produce
        # spurious matches.  We drop them.
        assert _tokenize_for_grounding("的") == set()
        assert _tokenize_for_grounding("是") == set()

    def test_mixed_text_extracts_both(self):
        tokens = _tokenize_for_grounding("ETH永續合約 with CCXT integration")
        assert "ccxt" in tokens
        assert "eth" in tokens
        assert "integration" in tokens
        # CJK shingles also present.
        assert "永續" in tokens
        assert "合約" in tokens
        assert "永續合約" in tokens

    def test_japanese_kana(self):
        # Hiragana / Katakana ranges are covered too.
        tokens = _tokenize_for_grounding("仮想通貨の永続契約バックテスト")
        assert tokens, "Japanese mixed kanji/kana should produce tokens"

    def test_korean_hangul(self):
        tokens = _tokenize_for_grounding("암호화폐 영구계약 백테스트")
        # Hangul syllables fall in U+AC00–U+D7AF; 2-char shingles must appear.
        assert any(len(t) == 2 for t in tokens), (
            f"Korean text should yield 2-char shingles, got: {sorted(tokens)[:8]}"
        )

    def test_empty_and_none(self):
        assert _tokenize_for_grounding("") == set()
        assert _tokenize_for_grounding(None) == set()


# ─────────────────────────────────────────────────────────────────────
# 2) _citation_support_score / _filter_claims_by_citations — Chinese
#    claims now ground against Chinese citations
# ─────────────────────────────────────────────────────────────────────


class TestCitationGroundingCJK:
    def _zh_citation(
        self,
        title: str = "幣安永續合約資金費率分析",
        snippet: str = "ETH永續合約的資金費率波動率聚類研究與回測。",
        url: str = "https://example.com/funding",
    ) -> ResearchCitation:
        return ResearchCitation(
            title=title,
            snippet=snippet,
            url=url,
            provider="websearch",
            evidence_type="news",
            verification_status="unverified",
        )

    def test_chinese_claim_grounds_to_chinese_citation(self):
        score = _citation_support_score(
            "資金費率波動率區間切換", self._zh_citation()
        )
        assert score > 0, (
            "Chinese claim sharing 資金費率/波動率 substrings with citation must score > 0"
        )

    def test_unrelated_chinese_claim_does_not_ground(self):
        # No 2-char CJK shingle in common.
        score = _citation_support_score("量子物理糾纏實驗報告", self._zh_citation())
        assert score == 0

    def test_filter_keeps_chinese_grounded_claims(self):
        citations = [self._zh_citation()]
        flags: list[str] = []
        grounded, attributions = _filter_claims_by_citations(
            ["資金費率波動率區間切換", "量子物理糾纏實驗報告"],
            citations,
            category="technical_patterns",
            hallucination_flags=flags,
        )
        # First (Chinese) claim grounds; second one is unrelated.
        assert "資金費率波動率區間切換" in grounded
        assert len(attributions) == 1
        assert flags == ["technical_patterns:量子物理糾纏實驗報告"]


# ─────────────────────────────────────────────────────────────────────
# 3) _should_force_direction_none — defence in depth so a partial
#    synthesizer failure does not kill the judge
# ─────────────────────────────────────────────────────────────────────


class TestForceNoneDefenseInDepth:
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
        claim_attributions_count: int = 0,
    ) -> ResearchContext:
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
            claim_attributions=[],  # partial: real attributions vary
            evidence_coverage={
                "citations": citations,
                "grounded_claims": grounded_claims,
                "grounded_summary_claims": grounded_summary_claims,
            },
            synthesized_summary="",
            provider_errors={},
        )

    def test_zero_grounded_claims_with_many_citations_does_not_force_none(self):
        # The exact production scenario the user reported: 12 citations, 0
        # grounded_claims (because the synthesizer's structured arrays were
        # empty).  The legacy gate force-killed the judge here; with
        # citations >= 3 the gate must defer to the judge instead.
        ctx = self._ctx(citations=12, grounded_claims=0)
        force_none, reason, _ = _should_force_direction_none(
            self._decision(),
            research_context=ctx,
            comparator_report=None,
            audit_report=None,
        )
        assert force_none is False, (
            f"With 12 citations the gate must defer to the judge, "
            f"got force_none=True reason={reason!r}"
        )

    def test_truly_empty_evidence_still_forces_none(self):
        # The original safety property is preserved.
        ctx = self._ctx(citations=0, grounded_claims=0)
        force_none, reason, gap_info = _should_force_direction_none(
            self._decision(),
            research_context=ctx,
            comparator_report=None,
            audit_report=None,
        )
        assert force_none is True
        assert "near-zero" in reason
        assert gap_info["citations_needed"] >= 3
        assert gap_info["grounded_claims_needed"] >= 3

    def test_two_citations_zero_grounding_still_forces_none(self):
        # Below the citations>=3 threshold + no other grounding signals
        # → still force "none" so the refinement loop has a chance to enrich.
        ctx = self._ctx(citations=2, grounded_claims=0)
        force_none, _, _ = _should_force_direction_none(
            self._decision(),
            research_context=ctx,
            comparator_report=None,
            audit_report=None,
        )
        assert force_none is True

    def test_summary_claims_alone_keep_judge_alive(self):
        # The synthesizer produced grounded_summary_claims even though the
        # structured arrays were empty — that is still enough material for
        # the judge to call low/medium-confidence.
        ctx = self._ctx(citations=5, grounded_claims=0, grounded_summary_claims=3)
        force_none, _, _ = _should_force_direction_none(
            self._decision(),
            research_context=ctx,
            comparator_report=None,
            audit_report=None,
        )
        assert force_none is False


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
