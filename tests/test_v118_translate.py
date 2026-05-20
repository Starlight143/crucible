"""v1.1.8 extended Phase 6 (Q10) — Bilingual query expansion tests.

Coverage:

* ``contains_cjk`` detects Chinese, Japanese, Korean characters and
  symbols.  ASCII-only returns False.
* ``translate_cjk_to_en`` invokes the caller-supplied function and
  caches the result.
* Repeated calls hit cache (translate_fn not invoked again).
* Translation failure (return None / exception) returns None and
  doesn't cache.
* Result that still contains CJK is rejected (LLM echoed input).
* ``should_translate_for_query`` honours env + threshold + CJK gate.
* Cache clear / size accessor.
"""

from __future__ import annotations

import pytest

from crucible.web_research.translate import (
    bilingual_enabled,
    bilingual_threshold,
    clear_translation_cache,
    contains_cjk,
    should_translate_for_query,
    translate_cjk_to_en,
    translation_cache_size,
)


@pytest.fixture(autouse=True)
def reset_cache():
    """Per-test cache reset so tests don't bleed state."""
    clear_translation_cache()
    yield
    clear_translation_cache()


class TestContainsCjk:
    def test_chinese_traditional(self) -> None:
        assert contains_cjk("資金費率均值回歸") is True

    def test_chinese_simplified(self) -> None:
        assert contains_cjk("资金费率均值回归") is True

    def test_japanese_hiragana(self) -> None:
        assert contains_cjk("こんにちは世界") is True

    def test_japanese_katakana(self) -> None:
        assert contains_cjk("カタカナ") is True

    def test_korean(self) -> None:
        assert contains_cjk("안녕하세요") is True

    def test_cjk_punctuation(self) -> None:
        # Full-width brackets, ideographic comma, etc.
        assert contains_cjk("「test」") is True
        assert contains_cjk("ETH，funding rate") is True  # full-width comma

    def test_mixed_cjk_english(self) -> None:
        assert contains_cjk("Binance 永續合約") is True

    def test_ascii_only(self) -> None:
        assert contains_cjk("Hello World") is False
        assert contains_cjk("ETH funding rate analysis") is False

    def test_european_diacritics_not_cjk(self) -> None:
        # ñ, ö, é etc are Latin Extended — NOT CJK.
        assert contains_cjk("café résumé naïve") is False

    def test_empty(self) -> None:
        assert contains_cjk("") is False
        assert contains_cjk(None) is False  # type: ignore[arg-type]


class TestTranslateCjkToEn:
    def test_basic_translation(self) -> None:
        def fake_translate(text: str) -> str:
            return "ETH funding rate mean reversion"

        result = translate_cjk_to_en("ETH 資金費率均值回歸", fake_translate)
        assert result == "ETH funding rate mean reversion"

    def test_cached(self) -> None:
        calls = []

        def fake_translate(text: str) -> str:
            calls.append(text)
            return "translated"

        translate_cjk_to_en("資金費率", fake_translate)
        translate_cjk_to_en("資金費率", fake_translate)
        translate_cjk_to_en("資金費率", fake_translate)
        # Only invoked once thanks to cache.
        assert len(calls) == 1

    def test_normalised_for_cache(self) -> None:
        calls = []

        def fake_translate(text: str) -> str:
            calls.append(text)
            return "translated"

        translate_cjk_to_en("資金費率", fake_translate)
        # Different whitespace / case (case is meaningless for CJK but
        # still normalised) → same cache key.
        translate_cjk_to_en("  資金費率  ", fake_translate)
        assert len(calls) == 1

    def test_non_cjk_input_returns_none(self) -> None:
        called = []

        def fake_translate(text: str) -> str:
            called.append(text)
            return "should not be called"

        result = translate_cjk_to_en("ETH funding rate", fake_translate)
        assert result is None
        # translate_fn was never called.
        assert called == []

    def test_empty_input(self) -> None:
        def fake_translate(text: str) -> str:
            return "should not be called"

        assert translate_cjk_to_en("", fake_translate) is None
        assert translate_cjk_to_en(None, fake_translate) is None  # type: ignore[arg-type]

    def test_translate_fn_returns_none(self) -> None:
        def fake_translate(text: str) -> None:
            return None

        assert translate_cjk_to_en("資金費率", fake_translate) is None
        # Failed translation NOT cached.
        assert translation_cache_size() == 0

    def test_translate_fn_returns_empty(self) -> None:
        def fake_translate(text: str) -> str:
            return ""

        assert translate_cjk_to_en("資金費率", fake_translate) is None
        assert translation_cache_size() == 0

    def test_translate_fn_raises(self) -> None:
        def fake_translate(text: str) -> str:
            raise RuntimeError("LLM error")

        # Must not crash — return None gracefully.
        assert translate_cjk_to_en("資金費率", fake_translate) is None

    def test_translate_fn_returns_cjk_rejected(self) -> None:
        """LLM that echoes the input still has CJK → reject the
        translation to avoid spamming the same CJK query a second time
        marked as 'English'."""
        def echo_translate(text: str) -> str:
            return text  # CJK still in output

        assert translate_cjk_to_en("資金費率", echo_translate) is None
        assert translation_cache_size() == 0

    def test_disabled_returns_none(self, monkeypatch) -> None:
        monkeypatch.setenv("LIBRARIAN_BILINGUAL_QUERY_EXPANSION", "0")
        called = []

        def fake_translate(text: str) -> str:
            called.append(text)
            return "trans"

        assert translate_cjk_to_en("資金費率", fake_translate) is None
        assert called == []


class TestShouldTranslateForQuery:
    def test_under_threshold_cjk(self, monkeypatch) -> None:
        monkeypatch.setenv("LIBRARIAN_BILINGUAL_QUERY_THRESHOLD", "3")
        assert should_translate_for_query("資金費率", 0) is True
        assert should_translate_for_query("資金費率", 2) is True

    def test_at_threshold_no_translate(self, monkeypatch) -> None:
        monkeypatch.setenv("LIBRARIAN_BILINGUAL_QUERY_THRESHOLD", "3")
        # Threshold is inclusive — count == threshold means "enough",
        # no translation needed.
        assert should_translate_for_query("資金費率", 3) is False

    def test_over_threshold_no_translate(self, monkeypatch) -> None:
        monkeypatch.setenv("LIBRARIAN_BILINGUAL_QUERY_THRESHOLD", "3")
        assert should_translate_for_query("資金費率", 10) is False

    def test_english_query_no_translate(self, monkeypatch) -> None:
        monkeypatch.setenv("LIBRARIAN_BILINGUAL_QUERY_THRESHOLD", "3")
        # No CJK → no translation regardless of count.
        assert should_translate_for_query("funding rate", 0) is False

    def test_disabled_no_translate(self, monkeypatch) -> None:
        monkeypatch.setenv("LIBRARIAN_BILINGUAL_QUERY_EXPANSION", "0")
        assert should_translate_for_query("資金費率", 0) is False


class TestBilingualThreshold:
    def test_default(self, monkeypatch) -> None:
        monkeypatch.delenv("LIBRARIAN_BILINGUAL_QUERY_THRESHOLD", raising=False)
        assert bilingual_threshold() == 3

    def test_env_override(self, monkeypatch) -> None:
        monkeypatch.setenv("LIBRARIAN_BILINGUAL_QUERY_THRESHOLD", "5")
        assert bilingual_threshold() == 5

    def test_zero_or_negative_falls_back_to_default(self, monkeypatch) -> None:
        monkeypatch.setenv("LIBRARIAN_BILINGUAL_QUERY_THRESHOLD", "0")
        assert bilingual_threshold() == 3
        monkeypatch.setenv("LIBRARIAN_BILINGUAL_QUERY_THRESHOLD", "-1")
        assert bilingual_threshold() == 3

    def test_clamped_to_50(self, monkeypatch) -> None:
        monkeypatch.setenv("LIBRARIAN_BILINGUAL_QUERY_THRESHOLD", "100")
        assert bilingual_threshold() == 50


class TestCacheAdmin:
    def test_clear(self) -> None:
        def f(t):
            return "trans"
        translate_cjk_to_en("資金費率", f)
        translate_cjk_to_en("永續合約", f)
        assert translation_cache_size() == 2
        clear_translation_cache()
        assert translation_cache_size() == 0

    def test_size_tracks_unique_keys(self) -> None:
        def f(t):
            return "trans"
        translate_cjk_to_en("資金費率", f)
        translate_cjk_to_en("資金費率", f)  # Same key — no growth
        translate_cjk_to_en("永續合約", f)
        assert translation_cache_size() == 2


class TestBilingualEnabledFlag:
    def test_default(self, monkeypatch) -> None:
        monkeypatch.delenv("LIBRARIAN_BILINGUAL_QUERY_EXPANSION", raising=False)
        assert bilingual_enabled() is True

    def test_explicit_disabled(self, monkeypatch) -> None:
        monkeypatch.setenv("LIBRARIAN_BILINGUAL_QUERY_EXPANSION", "0")
        assert bilingual_enabled() is False
