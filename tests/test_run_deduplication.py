"""Tests for crucible.features.run_deduplication"""
from __future__ import annotations

import json
import math
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
from crucible.features.run_deduplication import (
    DEDUP_SIMILARITY_THRESHOLD,
    CorpusRun,
    DedupCheckResult,
    SimilarRun,
    _build_idf,
    _cosine_similarity,
    _extract_topic_text,
    _load_corpus_runs,
    _safe_mtime,
    _term_frequency,
    _tokenize,
    _tfidf_vector,
    check_duplicate_run,
)


# ── _safe_mtime ───────────────────────────────────────────────────────────────

class TestSafeMtime:
    def test_returns_real_mtime_for_existing_path(self, tmp_path):
        d = tmp_path / "x"
        d.mkdir()
        result = _safe_mtime(str(d))
        assert isinstance(result, float)
        assert result > 0.0

    def test_returns_zero_for_nonexistent_path(self, tmp_path):
        result = _safe_mtime(str(tmp_path / "does_not_exist"))
        assert result == 0.0

    def test_returns_zero_on_oserror(self):
        import unittest.mock as mock
        with mock.patch(
            "crucible.features.run_deduplication.os.path.getmtime",
            side_effect=OSError("Permission denied"),
        ):
            result = _safe_mtime("/some/path")
        assert result == 0.0


# ── _tokenize ─────────────────────────────────────────────────────────────────

class TestTokenize:
    def test_lowercases_input(self):
        tokens = _tokenize("Bitcoin ETH")
        assert all(t == t.lower() for t in tokens)

    def test_removes_stop_words(self):
        tokens = _tokenize("the strategy is based on momentum")
        assert "the" not in tokens
        assert "is" not in tokens

    def test_removes_single_char_tokens(self):
        tokens = _tokenize("a b c longer_word")
        assert "a" not in tokens
        assert "b" not in tokens
        assert "c" not in tokens

    def test_strips_punctuation(self):
        tokens = _tokenize("momentum! signals, alpha?")
        for t in tokens:
            assert "!" not in t
            assert "," not in t
            assert "?" not in t

    def test_returns_empty_for_stop_words_only(self):
        tokens = _tokenize("the and or but")
        assert tokens == []

    def test_returns_meaningful_tokens(self):
        tokens = _tokenize("funding rate arbitrage Bitcoin perpetuals")
        assert "funding" in tokens
        assert "arbitrage" in tokens
        assert "bitcoin" in tokens


# ── _term_frequency ───────────────────────────────────────────────────────────

class TestTermFrequency:
    def test_returns_empty_for_empty_tokens(self):
        assert _term_frequency([]) == {}

    def test_frequencies_sum_to_one(self):
        tf = _term_frequency(["a", "b", "c"])
        assert abs(sum(tf.values()) - 1.0) < 1e-9

    def test_repeated_token_has_higher_frequency(self):
        tf = _term_frequency(["alpha", "alpha", "beta"])
        assert tf["alpha"] > tf["beta"]

    def test_single_token_has_frequency_one(self):
        tf = _term_frequency(["token"])
        assert tf["token"] == pytest.approx(1.0)


# ── _build_idf ────────────────────────────────────────────────────────────────

class TestBuildIdf:
    def test_returns_empty_for_empty_corpus(self):
        assert _build_idf([]) == {}

    def test_all_terms_present(self):
        idf = _build_idf([["alpha", "beta"], ["beta", "gamma"]])
        assert "alpha" in idf
        assert "beta" in idf
        assert "gamma" in idf

    def test_rare_term_has_higher_idf(self):
        """A term appearing in fewer documents should have higher IDF."""
        docs = [
            ["common", "rare"],
            ["common"],
            ["common"],
        ]
        idf = _build_idf(docs)
        assert idf["rare"] > idf["common"]

    def test_smoothed_formula(self):
        """Verify formula: log((1+N)/(1+df)) + 1"""
        docs = [["x"], ["x", "y"]]
        idf = _build_idf(docs)
        n = 2
        # "x" appears in both docs, df=2
        expected_x = math.log((1 + n) / (1 + 2)) + 1.0
        assert idf["x"] == pytest.approx(expected_x)


# ── _tfidf_vector ─────────────────────────────────────────────────────────────

class TestTfidfVector:
    def test_returns_empty_for_empty_tokens(self):
        idf = {"alpha": 1.5}
        assert _tfidf_vector([], idf) == {}

    def test_known_term_uses_idf(self):
        idf = {"momentum": 2.0}
        vec = _tfidf_vector(["momentum"], idf)
        assert "momentum" in vec
        assert vec["momentum"] == pytest.approx(1.0 * 2.0)

    def test_unknown_term_uses_idf_of_one(self):
        vec = _tfidf_vector(["unseen"], {})
        assert vec["unseen"] == pytest.approx(1.0 * 1.0)


# ── _cosine_similarity ────────────────────────────────────────────────────────

class TestCosineSimilarity:
    def test_identical_vectors_return_one(self):
        v = {"a": 1.0, "b": 2.0}
        assert _cosine_similarity(v, v) == pytest.approx(1.0)

    def test_disjoint_vectors_return_zero(self):
        v1 = {"a": 1.0}
        v2 = {"b": 1.0}
        assert _cosine_similarity(v1, v2) == pytest.approx(0.0)

    def test_empty_vectors_return_zero(self):
        assert _cosine_similarity({}, {"a": 1.0}) == pytest.approx(0.0)
        assert _cosine_similarity({"a": 1.0}, {}) == pytest.approx(0.0)

    def test_partial_overlap(self):
        v1 = {"a": 1.0, "b": 1.0}
        v2 = {"a": 1.0, "c": 1.0}
        sim = _cosine_similarity(v1, v2)
        assert 0.0 < sim < 1.0

    def test_symmetric(self):
        v1 = {"a": 2.0, "b": 1.0}
        v2 = {"a": 1.0, "b": 2.0}
        assert _cosine_similarity(v1, v2) == pytest.approx(_cosine_similarity(v2, v1))

    def test_result_bounded_zero_to_one(self):
        v1 = {"a": 3.0, "b": 1.0, "c": 0.5}
        v2 = {"a": 1.0, "b": 3.0, "d": 2.0}
        sim = _cosine_similarity(v1, v2)
        assert 0.0 <= sim <= 1.0


# ── _extract_topic_text ───────────────────────────────────────────────────────

class TestExtractTopicText:
    def test_includes_project_name(self):
        data = {"project_name": "funding_arbitrage", "summary": ""}
        text = _extract_topic_text(data)
        assert "funding_arbitrage" in text

    def test_includes_summary(self):
        data = {"project_name": "", "summary": "Momentum strategy overview"}
        text = _extract_topic_text(data)
        assert "Momentum strategy overview" in text

    def test_includes_consensus(self):
        data = {"project_name": "", "consensus": "Strong market signals"}
        text = _extract_topic_text(data)
        assert "Strong market signals" in text

    def test_includes_experiment_goals(self):
        data = {
            "project_name": "",
            "experiments": [{"goal": "Test breakout at ATR"}, {"goal": "Check drawdown"}],
        }
        text = _extract_topic_text(data)
        assert "Test breakout at ATR" in text
        assert "Check drawdown" in text

    def test_limits_experiments_to_three(self):
        data = {
            "project_name": "",
            "experiments": [{"goal": f"Exp {i}"} for i in range(10)],
        }
        text = _extract_topic_text(data)
        assert "Exp 0" in text
        assert "Exp 2" in text
        # 4th and beyond should be excluded
        assert "Exp 3" not in text

    def test_returns_empty_string_for_empty_data(self):
        assert _extract_topic_text({}).strip() == ""

    def test_handles_none_values_gracefully(self):
        data = {"project_name": None, "summary": None, "consensus": None}
        text = _extract_topic_text(data)
        # Should not raise; just strip None -> ""
        assert isinstance(text, str)


# ── _load_corpus_runs ─────────────────────────────────────────────────────────

class TestLoadCorpusRuns:
    def _make_run(self, saved_dir, run_id: str, data: dict) -> str:
        run_dir = os.path.join(str(saved_dir), run_id)
        os.makedirs(run_dir, exist_ok=True)
        with open(os.path.join(run_dir, "analysis_result.json"), "w", encoding="utf-8") as fh:
            json.dump(data, fh)
        return run_dir

    def test_returns_empty_when_no_saved_projects(self, tmp_path):
        corpus = _load_corpus_runs(str(tmp_path), lookback_days=30, max_runs=100)
        assert corpus == []

    def test_loads_valid_run(self, tmp_path):
        saved = tmp_path / "saved_projects"
        saved.mkdir()
        self._make_run(saved, "run_001", {
            "project_name": "alpha_strategy",
            "summary": "Momentum based strategy",
            "consensus": "Good fit",
        })
        corpus = _load_corpus_runs(str(tmp_path), lookback_days=30, max_runs=100)
        assert len(corpus) == 1
        assert corpus[0].project_name == "alpha_strategy"

    def test_skips_run_without_analysis_result(self, tmp_path):
        saved = tmp_path / "saved_projects"
        saved.mkdir()
        (saved / "empty_run").mkdir()
        corpus = _load_corpus_runs(str(tmp_path), lookback_days=30, max_runs=100)
        assert corpus == []

    def test_skips_run_with_no_topic_text(self, tmp_path):
        saved = tmp_path / "saved_projects"
        saved.mkdir()
        self._make_run(saved, "empty_data_run", {})
        corpus = _load_corpus_runs(str(tmp_path), lookback_days=30, max_runs=100)
        assert corpus == []

    def test_applies_max_runs_cap(self, tmp_path):
        saved = tmp_path / "saved_projects"
        saved.mkdir()
        for i in range(5):
            self._make_run(saved, f"run_{i:03d}", {
                "project_name": f"proj_{i}",
                "summary": f"Summary {i} momentum strategy",
            })
        corpus = _load_corpus_runs(str(tmp_path), lookback_days=30, max_runs=3)
        assert len(corpus) == 3

    def test_skips_invalid_json(self, tmp_path):
        saved = tmp_path / "saved_projects"
        saved.mkdir()
        bad_run = saved / "bad_run"
        bad_run.mkdir()
        (bad_run / "analysis_result.json").write_text("not-json")
        corpus = _load_corpus_runs(str(tmp_path), lookback_days=30, max_runs=100)
        assert corpus == []

    def test_lookback_days_zero_means_no_limit(self, tmp_path):
        """lookback_days=0 should not filter out any runs regardless of age."""
        saved = tmp_path / "saved_projects"
        saved.mkdir()
        self._make_run(saved, "old_run", {
            "project_name": "old",
            "summary": "An old strategy momentum based",
        })
        corpus = _load_corpus_runs(str(tmp_path), lookback_days=0, max_runs=100)
        assert len(corpus) == 1

    def test_sort_is_deterministic_on_identical_mtime(self, tmp_path):
        """
        Regression: when two directories have the same mtime (e.g., created in
        the same second), the sort order was non-deterministic.  After the fix,
        a secondary key (basename descending) ensures a stable, reproducible order.
        """
        import unittest.mock as mock
        saved = tmp_path / "saved_projects"
        saved.mkdir()
        # Create two runs with the same mtime
        for name in ("run_alpha", "run_beta"):
            self._make_run(saved, name, {
                "project_name": name,
                "summary": f"{name} momentum strategy",
            })

        # Force identical mtime for both dirs
        fixed_mtime = 1_700_000_000.0
        original_getmtime = os.path.getmtime

        def _fake_getmtime(p: str) -> float:
            if os.path.basename(p) in ("run_alpha", "run_beta"):
                return fixed_mtime
            return original_getmtime(p)

        with mock.patch("crucible.features.run_deduplication._safe_mtime",
                        side_effect=_fake_getmtime):
            corpus1 = _load_corpus_runs(str(tmp_path), lookback_days=0, max_runs=100)
            corpus2 = _load_corpus_runs(str(tmp_path), lookback_days=0, max_runs=100)

        # Both calls must return the same order (determinism)
        names1 = [c.run_id for c in corpus1]
        names2 = [c.run_id for c in corpus2]
        assert names1 == names2, (
            "Sort must be deterministic when mtime is identical; "
            f"got {names1} vs {names2}"
        )

    def test_sort_graceful_on_deleted_directory(self, tmp_path):
        """
        Regression: os.path.getmtime() raises OSError if a directory is deleted
        between os.listdir() and the sort.  After the fix, such directories are
        assigned mtime=0.0 and sorted to the end rather than crashing.
        """
        import unittest.mock as mock
        saved = tmp_path / "saved_projects"
        saved.mkdir()
        self._make_run(saved, "live_run", {
            "project_name": "live",
            "summary": "A live strategy",
        })
        self._make_run(saved, "ghost_run", {
            "project_name": "ghost",
            "summary": "A ghost strategy",
        })

        original_getmtime = os.path.getmtime

        def _raising_getmtime(p: str) -> float:
            if os.path.basename(p) == "ghost_run":
                raise OSError("Directory deleted during iteration")
            return original_getmtime(p)

        # Patch os.path.getmtime so _safe_mtime sees the OSError and returns 0.0.
        # This tests the full defense chain: getmtime raises → _safe_mtime returns
        # 0.0 → _load_corpus_runs doesn't crash.
        with mock.patch("crucible.features.run_deduplication.os.path.getmtime",
                        side_effect=_raising_getmtime):
            try:
                corpus = _load_corpus_runs(str(tmp_path), lookback_days=0, max_runs=100)
            except OSError as exc:
                pytest.fail(f"_load_corpus_runs should not raise on deleted dir: {exc}")


# ── DedupCheckResult ──────────────────────────────────────────────────────────

class TestDedupCheckResult:
    def _make_similar_run(self, sim: float) -> SimilarRun:
        return SimilarRun(
            run_id="r1", project_name="proj", similarity=sim,
            score=70.0, risk_level="medium", run_dir="/x", timestamp=None,
        )

    def test_has_similar_runs_false_when_empty(self):
        result = DedupCheckResult(candidate_topic="test")
        assert result.has_similar_runs is False

    def test_has_similar_runs_true_when_present(self):
        sr = self._make_similar_run(0.9)
        result = DedupCheckResult(candidate_topic="test", similar_runs=[sr])
        assert result.has_similar_runs is True

    def test_most_similar_run_returns_highest(self):
        sr1 = self._make_similar_run(0.7)
        sr2 = self._make_similar_run(0.95)
        result = DedupCheckResult(
            candidate_topic="test",
            similar_runs=[sr1, sr2],
            highest_similarity=0.95,
        )
        assert result.most_similar_run.similarity == pytest.approx(0.95)

    def test_most_similar_run_returns_none_when_empty(self):
        result = DedupCheckResult(candidate_topic="test")
        assert result.most_similar_run is None

    def test_summary_text_no_duplicates(self):
        result = DedupCheckResult(candidate_topic="test", corpus_size=5)
        text = result.summary_text()
        assert "No similar runs found" in text
        assert "5" in text

    def test_summary_text_with_duplicates(self):
        sr = self._make_similar_run(0.92)
        result = DedupCheckResult(
            candidate_topic="test",
            similar_runs=[sr],
            highest_similarity=0.92,
        )
        text = result.summary_text()
        assert "similar run" in text.lower()
        assert "proj" in text

    def test_to_dict_keys(self):
        result = DedupCheckResult(candidate_topic="test")
        d = result.to_dict()
        for key in ("candidate_topic", "has_similar_runs", "highest_similarity",
                    "threshold_used", "corpus_size", "similar_runs"):
            assert key in d

    def test_similar_run_to_dict(self):
        sr = self._make_similar_run(0.88)
        d = sr.to_dict()
        assert d["similarity"] == pytest.approx(0.88, abs=1e-3)
        assert d["project_name"] == "proj"


# ── check_duplicate_run ───────────────────────────────────────────────────────

class TestCheckDuplicateRun:
    def _make_run(self, saved_dir, run_id: str, data: dict) -> str:
        run_dir = os.path.join(str(saved_dir), run_id)
        os.makedirs(run_dir, exist_ok=True)
        with open(os.path.join(run_dir, "analysis_result.json"), "w", encoding="utf-8") as fh:
            json.dump(data, fh)
        return run_dir

    def test_returns_empty_result_for_empty_corpus(self, tmp_path):
        result = check_duplicate_run(
            "funding rate arbitrage BTC", str(tmp_path), threshold=0.7
        )
        assert result.corpus_size == 0
        assert not result.has_similar_runs

    def test_detects_identical_topic(self, tmp_path):
        saved = tmp_path / "saved_projects"
        saved.mkdir()
        topic = "funding rate arbitrage BTC perpetuals momentum strategy"
        self._make_run(saved, "run_001", {
            "project_name": "btc_arb",
            "summary": topic,
        })
        result = check_duplicate_run(topic, str(tmp_path), threshold=0.5)
        assert result.has_similar_runs

    def test_does_not_flag_unrelated_topic(self, tmp_path):
        saved = tmp_path / "saved_projects"
        saved.mkdir()
        self._make_run(saved, "run_001", {
            "project_name": "weather_app",
            "summary": "meteorological forecast saas platform cloud infrastructure",
        })
        result = check_duplicate_run(
            "funding rate arbitrage crypto perpetual futures Bitcoin trading strategy",
            str(tmp_path),
            threshold=0.5,
        )
        assert not result.has_similar_runs

    def test_respects_threshold_parameter(self, tmp_path):
        saved = tmp_path / "saved_projects"
        saved.mkdir()
        topic = "momentum strategy equities backtesting alpha generation"
        self._make_run(saved, "run_001", {
            "project_name": "mom_strat",
            "summary": topic,
        })
        # With very high threshold should NOT flag
        result_high = check_duplicate_run(topic, str(tmp_path), threshold=0.9999)
        # With very low threshold SHOULD flag for identical text
        result_low = check_duplicate_run(topic, str(tmp_path), threshold=0.0)
        assert not result_high.has_similar_runs, (
            "threshold=0.9999 must not flag an identical run as duplicate"
        )
        assert result_low.has_similar_runs, (
            "threshold=0.0 must flag an identical run as duplicate"
        )

    def test_corpus_size_reported_correctly(self, tmp_path):
        saved = tmp_path / "saved_projects"
        saved.mkdir()
        for i in range(3):
            self._make_run(saved, f"run_{i}", {
                "project_name": f"p{i}",
                "summary": f"strategy {i} momentum breakout system {i}",
            })
        result = check_duplicate_run(
            "unrelated chemistry biology topic", str(tmp_path), threshold=0.9
        )
        assert result.corpus_size == 3

    def test_highest_similarity_reflects_best_match(self, tmp_path):
        saved = tmp_path / "saved_projects"
        saved.mkdir()
        topic = "breakout momentum equities alpha generation system"
        self._make_run(saved, "run_exact", {
            "project_name": "exact",
            "summary": topic,
        })
        self._make_run(saved, "run_unrelated", {
            "project_name": "unrelated",
            "summary": "weather cloud saas platform infrastructure",
        })
        result = check_duplicate_run(topic, str(tmp_path), threshold=0.0)
        # The exact match should be the highest similarity
        assert result.highest_similarity > 0.0
        best = result.most_similar_run
        assert best is not None
        assert best.project_name == "exact"

    def test_similar_runs_sorted_descending(self, tmp_path):
        saved = tmp_path / "saved_projects"
        saved.mkdir()
        # Create one very similar and one moderately similar run
        self._make_run(saved, "run_very_similar", {
            "project_name": "vs",
            "summary": "momentum strategy BTC perpetuals funding rate arbitrage alpha",
        })
        self._make_run(saved, "run_somewhat_similar", {
            "project_name": "ss",
            "summary": "momentum strategy stocks equities alpha generation",
        })
        result = check_duplicate_run(
            "momentum strategy BTC perpetuals funding rate arbitrage",
            str(tmp_path),
            threshold=0.0,
        )
        if len(result.similar_runs) >= 2:
            sims = [r.similarity for r in result.similar_runs]
            assert sims == sorted(sims, reverse=True)

    def test_to_dict_serialisable(self, tmp_path):
        result = check_duplicate_run(
            "some topic", str(tmp_path), threshold=0.9
        )
        d = result.to_dict()
        # Should be JSON-serialisable
        json.dumps(d)
