"""Tests for crucible.context_budget"""
from __future__ import annotations

import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
from crucible.context_budget import (
    ContextBudgetManager,
    CompactionResult,
    estimate_tokens,
    estimate_messages_tokens,
    _message_text,
    _generate_compaction_summary,
    get_default_manager,
)


# ── estimate_tokens ───────────────────────────────────────────────────────────

class TestEstimateTokens:
    def test_empty_string(self):
        assert estimate_tokens("") == 0

    def test_single_char(self):
        assert estimate_tokens("a") == 1

    def test_default_ratio(self):
        # estimate_tokens now delegates to count_tokens (CJK-aware / tiktoken).
        # For ASCII-only text the result is >= 1 and proportional to length.
        result = estimate_tokens("x" * 100)
        assert result >= 1, "Non-empty text must produce at least 1 token"

    def test_custom_ratio(self):
        # chars_per_token is now a backwards-compat parameter only; the
        # actual result comes from count_tokens regardless of the ratio.
        result = estimate_tokens("x" * 100, chars_per_token=10)
        assert result >= 1

    def test_ratio_clamped_minimum(self):
        # count_tokens always returns >= 1 for non-empty input.
        result = estimate_tokens("x" * 10, chars_per_token=0.1)
        assert result >= 1


# ── _message_text ─────────────────────────────────────────────────────────────

class TestMessageText:
    def test_string_content(self):
        assert _message_text({"role": "user", "content": "hello"}) == "hello"

    def test_list_content_strings(self):
        msg = {"role": "user", "content": ["part1", "part2"]}
        assert _message_text(msg) == "part1\npart2"

    def test_list_content_dicts(self):
        msg = {"role": "user", "content": [{"text": "hello"}, {"content": "world"}]}
        result = _message_text(msg)
        assert "hello" in result
        assert "world" in result

    def test_missing_content(self):
        assert _message_text({"role": "user"}) == ""

    def test_non_string_content(self):
        result = _message_text({"role": "user", "content": 42})
        assert result == "42"


# ── estimate_messages_tokens ──────────────────────────────────────────────────

class TestEstimateMessagesTokens:
    def test_empty_list(self):
        assert estimate_messages_tokens([]) == 0

    def test_single_message(self):
        # estimate_messages_tokens now uses count_tokens internally.
        # For non-empty ASCII content the result is >= 1.
        msgs = [{"role": "user", "content": "x" * 40}]
        assert estimate_messages_tokens(msgs) >= 1

    def test_multiple_messages(self):
        # Two messages produce more tokens than one.
        msgs = [
            {"role": "user", "content": "x" * 40},
            {"role": "assistant", "content": "y" * 40},
        ]
        single = estimate_messages_tokens([msgs[0]])
        combined = estimate_messages_tokens(msgs)
        assert combined >= single


# ── ContextBudgetManager ──────────────────────────────────────────────────────

class TestContextBudgetManager:
    # ContextBudgetManager clamps token_budget to min 1_000 (see source).
    # With tiktoken, repeated ASCII chars are highly compressed, so we need
    # many large messages to reliably exceed the 800-token compaction threshold
    # (1000 * 0.8 = 800 tokens).
    # 100 messages × 800 chars ≈ 10 000 tokens → always exceeds threshold.
    _LARGE_N: int = 100
    _LARGE_CHARS: int = 800

    def _make_mgr(self, budget: int = 1_000, ratio: float = 0.8, keep: int = 2) -> ContextBudgetManager:
        return ContextBudgetManager(
            token_budget=budget,
            compact_threshold_ratio=ratio,
            keep_recent=keep,
        )

    def _msgs(self, n: int, chars_each: int = 800) -> list:
        return [{"role": "assistant", "content": "x" * chars_each} for _ in range(n)]

    def _large_msgs(self, keep: int = 2) -> list:
        """Return a message list that reliably triggers compaction."""
        return self._msgs(self._LARGE_N, chars_each=self._LARGE_CHARS)

    def test_needs_compaction_false_when_under(self):
        # Single very short message should never exceed a large budget.
        mgr = ContextBudgetManager(token_budget=1_000_000, compact_threshold_ratio=0.9)
        msgs = [{"role": "user", "content": "hi"}]
        assert not mgr.needs_compaction(msgs)

    def test_needs_compaction_true_when_over(self):
        # 100 messages × 800 chars ≈ 10 000 tokens >> threshold of 800.
        mgr = self._make_mgr(budget=1_000, ratio=0.8, keep=2)
        msgs = self._large_msgs()
        assert mgr.needs_compaction(msgs)

    def test_compact_returns_result_type(self):
        mgr = self._make_mgr(budget=1_000, ratio=0.8, keep=2)
        msgs = self._large_msgs()
        result = mgr.compact(msgs, stage_name="test_stage")
        assert isinstance(result, CompactionResult)

    def test_compact_keeps_recent(self):
        mgr = self._make_mgr(budget=1_000, ratio=0.8, keep=3)
        msgs = self._large_msgs()
        result = mgr.compact(msgs, stage_name="test")
        # compacted_messages = 1 summary + 3 recent
        assert len(result.compacted_messages) == 4

    def test_compact_injects_summary_as_system(self):
        mgr = self._make_mgr(budget=1_000, ratio=0.8, keep=2)
        msgs = self._large_msgs()
        result = mgr.compact(msgs, stage_name="test")
        first = result.compacted_messages[0]
        assert first["role"] == "system"
        assert "CONTEXT SUMMARY" in first["content"]

    def test_compact_no_op_when_not_needed(self):
        mgr = self._make_mgr(budget=1_000_000, ratio=0.9, keep=2)
        msgs = self._msgs(2, chars_each=10)
        result = mgr.compact(msgs, stage_name="test")
        assert result.messages_compacted == 0
        assert result.compacted_messages == msgs

    def test_compact_if_needed_returns_list(self):
        mgr = self._make_mgr(budget=1_000, ratio=0.8, keep=2)
        msgs = self._large_msgs()
        result = mgr.compact_if_needed(msgs, stage_name="test")
        assert isinstance(result, list)
        assert len(result) < len(msgs)

    def test_compact_tokens_after_less_than_before(self):
        mgr = self._make_mgr(budget=1_000, ratio=0.8, keep=2)
        msgs = self._large_msgs()
        result = mgr.compact(msgs, stage_name="test")
        assert result.tokens_after < result.tokens_before

    def test_compression_ratio_between_0_and_1(self):
        mgr = self._make_mgr(budget=1_000, ratio=0.8, keep=2)
        msgs = self._large_msgs()
        result = mgr.compact(msgs, stage_name="test")
        assert 0.0 < result.compression_ratio <= 1.0

    def test_constructor_clamps_ratio(self):
        mgr = ContextBudgetManager(compact_threshold_ratio=0.0)
        assert mgr.compact_threshold_ratio >= 0.5

    def test_constructor_clamps_budget(self):
        mgr = ContextBudgetManager(token_budget=1)
        assert mgr.token_budget >= 1_000

    def test_constructor_clamps_keep_recent(self):
        mgr = ContextBudgetManager(keep_recent=0)
        assert mgr.keep_recent >= 1

    def test_singleton_returns_same_instance(self):
        m1 = get_default_manager()
        m2 = get_default_manager()
        assert m1 is m2

    def test_stage_name_in_summary(self):
        mgr = self._make_mgr(budget=1_000, ratio=0.8, keep=2)
        msgs = self._large_msgs()
        result = mgr.compact(msgs, stage_name="my_special_stage")
        assert "my_special_stage" in result.compacted_messages[0]["content"]


# ── _generate_compaction_summary ──────────────────────────────────────────────

class TestGenerateCompactionSummary:
    def test_returns_string(self):
        msgs = [{"role": "assistant", "content": "some findings"}]
        out = _generate_compaction_summary(msgs, stage_name="s1")
        assert isinstance(out, str)
        assert len(out) > 0

    def test_contains_header(self):
        msgs = [{"role": "assistant", "content": "data"}]
        out = _generate_compaction_summary(msgs, stage_name="s1")
        assert "CONTEXT SUMMARY" in out
        assert "END CONTEXT SUMMARY" in out

    def test_respects_max_chars(self):
        msgs = [{"role": "assistant", "content": "x" * 50_000}]
        out = _generate_compaction_summary(msgs, stage_name="s1", max_summary_chars=500)
        assert len(out) < 2000   # header + truncated content still reasonable

    def test_empty_messages(self):
        out = _generate_compaction_summary([], stage_name="s1")
        assert "CONTEXT SUMMARY" in out


# ── prune_raw_tool_results ────────────────────────────────────────────────────

class TestPruneRawToolResults:
    def _mgr(self):
        from crucible.context_budget import ContextBudgetManager
        return ContextBudgetManager()

    def test_non_tool_messages_unchanged(self):
        mgr = self._mgr()
        msgs = [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "world"},
        ]
        out = mgr.prune_raw_tool_results(msgs)
        assert out == msgs

    def test_short_tool_role_unchanged(self):
        mgr = self._mgr()
        msgs = [{"role": "tool", "content": "short result"}]
        out = mgr.prune_raw_tool_results(msgs, max_tool_result_chars=100)
        assert out[0]["content"] == "short result"

    def test_long_tool_role_truncated(self):
        mgr = self._mgr()
        content = "x" * 5000
        msgs = [{"role": "tool", "content": content}]
        out = mgr.prune_raw_tool_results(msgs, max_tool_result_chars=100)
        assert len(out[0]["content"]) < len(content)
        assert "truncated" in out[0]["content"]

    def test_input_not_mutated(self):
        mgr = self._mgr()
        content = "x" * 5000
        msgs = [{"role": "tool", "content": content}]
        original_content = msgs[0]["content"]
        mgr.prune_raw_tool_results(msgs, max_tool_result_chars=100)
        assert msgs[0]["content"] == original_content

    def test_anthropic_tool_result_block_truncated(self):
        mgr = self._mgr()
        long_text = "y" * 5000
        msgs = [
            {
                "role": "user",
                "content": [
                    {"type": "tool_result", "content": long_text}
                ],
            }
        ]
        out = mgr.prune_raw_tool_results(msgs, max_tool_result_chars=100)
        block = out[0]["content"][0]
        assert len(block["content"]) < len(long_text)
        assert "truncated" in block["content"]

    def test_anthropic_nested_text_block_truncated(self):
        mgr = self._mgr()
        long_text = "z" * 5000
        msgs = [
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "content": [{"type": "text", "text": long_text}],
                    }
                ],
            }
        ]
        out = mgr.prune_raw_tool_results(msgs, max_tool_result_chars=200)
        item = out[0]["content"][0]["content"][0]
        assert len(item["text"]) < len(long_text)
        assert "truncated" in item["text"]

    def test_returns_new_list(self):
        mgr = self._mgr()
        msgs = [{"role": "user", "content": "hi"}]
        out = mgr.prune_raw_tool_results(msgs)
        assert out is not msgs

    def test_multiple_messages_mixed(self):
        mgr = self._mgr()
        long_content = "a" * 3000
        msgs = [
            {"role": "user", "content": "question"},
            {"role": "tool", "content": long_content},
            {"role": "assistant", "content": "answer"},
        ]
        out = mgr.prune_raw_tool_results(msgs, max_tool_result_chars=100)
        assert out[0]["content"] == "question"
        assert len(out[1]["content"]) < len(long_content)
        assert out[2]["content"] == "answer"


# ── Regression: get_default_manager thread safety ─────────────────────────────

class TestGetDefaultManagerThreadSafety:
    """Regression: get_default_manager() must return the same instance under
    concurrent access (previously lacked double-checked locking)."""

    def test_concurrent_calls_return_same_instance(self, monkeypatch):
        import threading
        import crucible.context_budget as cb
        # Reset singleton so each test starts clean
        monkeypatch.setattr(cb, "_DEFAULT_MANAGER", None)
        results = []
        barrier = threading.Barrier(10)

        def get():
            barrier.wait()
            results.append(cb.get_default_manager())

        threads = [threading.Thread(target=get) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(results) == 10
        # All threads must have received the same singleton instance
        assert all(r is results[0] for r in results)
