"""
crucible/context_budget.py
==================================
Context window budget management for the multi-stage research pipeline.

Borrowed from Claude Code's auto-compact strategy: when accumulated research
context exceeds a configurable token-budget threshold, early messages are
compressed into a structured summary so that downstream stages receive the
essential signal without hitting the model's context limit.

Key design decisions
--------------------
* CJK-aware token estimation via ``count_tokens()``.  Uses tiktoken
  (cl100k_base) when available; falls back to a heuristic that counts
  CJK characters (Unicode ranges 4E00-9FFF, 3000-303F, FF00-FFEF,
  3400-4DBF, 20000-2A6DF) as 1 token each and remaining characters as
  1/4 token.  The ``chars_per_token`` parameter is retained for backwards
  compatibility but acts as a fallback coefficient only.
* Compact is *lossless in structure*: the output is always a typed
  ``CompactionResult`` that callers can inspect and log.
* Safe to call on any list[dict] message sequence; never raises.
* Threshold defaults are env-var configurable so operators can tune without
  code changes.

Public API
----------
* ``count_tokens(text)`` — CJK-aware token estimator (importable directly).
* ``estimate_tokens(text)`` — thin wrapper around ``count_tokens`` that
  accepts an optional ``chars_per_token`` for backwards compatibility.
* ``estimate_messages_tokens(messages)`` — aggregate token estimate.
* ``ContextBudgetManager`` — stateful compaction manager.

Usage::

    from crucible.context_budget import ContextBudgetManager, count_tokens

    n = count_tokens("Hello 世界")
    mgr = ContextBudgetManager()
    messages = [{"role": "user", "content": "..."}, ...]

    if mgr.needs_compaction(messages):
        result = mgr.compact(messages, stage_name="research_swarm")
        messages = result.compacted_messages
        # result.tokens_before / result.tokens_after for logging
"""
from __future__ import annotations

import math
import textwrap
import threading
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

if __package__ == "crucible":
    from .runtime_logging import get_logger, log_event
else:  # pragma: no cover - direct script fallback
    from runtime_logging import get_logger, log_event  # type: ignore[no-redef]

LOGGER = get_logger(__name__)

# ── Defaults (all env-var overridable) ──────────────────────────────────────

_DEFAULT_TOKEN_BUDGET = 80_000       # max tokens before compaction triggers
_DEFAULT_COMPACT_RATIO = 0.85        # trigger at 85 % of budget
_DEFAULT_CHARS_PER_TOKEN = 4         # char-based token estimate coefficient
_DEFAULT_KEEP_RECENT = 6             # always keep the N most-recent messages intact
_SUMMARY_ROLE = "system"             # role used for the compaction-summary message


try:
    from . import _env
except ImportError:  # pragma: no cover - script-mode fallback
    import _env  # type: ignore[no-redef]


def _env_int(name: str, default: int) -> int:
    return _env.env_int(name, default)


def _env_float(name: str, default: float) -> float:
    return _env.env_float(name, default)


# ── Public data model ────────────────────────────────────────────────────────

@dataclass
class CompactionResult:
    """Result of a context compaction operation."""

    stage_name: str
    tokens_before: int
    tokens_after: int
    messages_before: int
    messages_after: int
    messages_compacted: int          # how many were replaced by the summary
    compacted_messages: List[Dict[str, Any]] = field(default_factory=list)
    summary_snippet: str = ""        # first 200 chars of generated summary

    @property
    def compression_ratio(self) -> float:
        if self.tokens_before <= 0:
            return 1.0
        return round(self.tokens_after / self.tokens_before, 4)

    def to_log_fields(self) -> Dict[str, Any]:
        return {
            "stage": self.stage_name,
            "tokens_before": self.tokens_before,
            "tokens_after": self.tokens_after,
            "messages_before": self.messages_before,
            "messages_after": self.messages_after,
            "messages_compacted": self.messages_compacted,
            "compression_ratio": self.compression_ratio,
        }


# ── Token estimation ─────────────────────────────────────────────────────────

# CJK Unicode ranges treated as 1 token per character
_CJK_RANGES: tuple = (
    (0x4E00, 0x9FFF),    # CJK Unified Ideographs
    (0x3400, 0x4DBF),    # CJK Extension A
    (0x3000, 0x303F),    # CJK Symbols and Punctuation
    (0xFF00, 0xFFEF),    # Halfwidth and Fullwidth Forms
    (0x20000, 0x2A6DF),  # CJK Extension B (supplementary)
)


def _is_cjk(cp: int) -> bool:
    """Return True if Unicode codepoint *cp* falls in a CJK range."""
    for lo, hi in _CJK_RANGES:
        if lo <= cp <= hi:
            return True
    return False


def count_tokens(text: str) -> int:
    """
    Estimate the number of tokens in *text*.

    Strategy (in priority order):

    1. **tiktoken** — if ``tiktoken`` is installed, uses the ``cl100k_base``
       encoding for an exact token count.
    2. **CJK-aware heuristic** — counts CJK characters (Unicode ranges
       4E00-9FFF, 3000-303F, FF00-FFEF, 3400-4DBF, 20000-2A6DF) as 1 token
       each; counts remaining characters as 1/4 token.  Result is ceiling'd
       and clamped to a minimum of 1.

    Args:
        text: Input text (may be empty).

    Returns:
        Estimated token count (>= 1 for non-empty text, 0 for empty).
    """
    if not text:
        return 0
    # Attempt tiktoken first
    try:
        import tiktoken
        enc = tiktoken.get_encoding("cl100k_base")
        return len(enc.encode(text))
    except Exception:
        pass
    # CJK-aware heuristic fallback
    cjk_count = 0
    non_cjk_count = 0
    for ch in text:
        if _is_cjk(ord(ch)):
            cjk_count += 1
        else:
            non_cjk_count += 1
    raw = cjk_count + non_cjk_count / 4.0
    return max(1, math.ceil(raw))


def _message_text(msg: Dict[str, Any]) -> str:
    """Extract plain text content from a message dict (handles str and list content)."""
    content = msg.get("content", "")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: List[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                text = item.get("text", "") or item.get("content", "") or ""
                parts.append(str(text))
        return "\n".join(parts)
    return str(content)


def estimate_tokens(text: str, *, chars_per_token: float = _DEFAULT_CHARS_PER_TOKEN) -> int:
    """
    Estimate token count from raw text.

    Delegates to ``count_tokens()`` for CJK-aware estimation.  The
    ``chars_per_token`` parameter is accepted for backwards compatibility
    but is no longer used as the primary estimation path; it is superseded
    by the CJK-aware heuristic in ``count_tokens``.
    """
    return count_tokens(text)


def estimate_messages_tokens(
    messages: List[Dict[str, Any]],
    *,
    chars_per_token: float = _DEFAULT_CHARS_PER_TOKEN,
) -> int:
    """
    Sum estimated tokens across all messages using ``count_tokens()``.

    The ``chars_per_token`` parameter is accepted for backwards compatibility
    but is no longer used as the primary estimation path.
    """
    total = 0
    for msg in messages:
        total += count_tokens(_message_text(msg))
    return total


# ── Summary generator ────────────────────────────────────────────────────────

def _generate_compaction_summary(
    messages: List[Dict[str, Any]],
    *,
    stage_name: str,
    max_summary_chars: int = 6_000,
) -> str:
    """
    Build a structured text summary of *messages* suitable for injection as a
    system context message.

    Strategy:
    1. Collect all ``assistant`` role messages (research outputs, debate results,
       analysis findings) — these carry the highest signal density.
    2. Truncate each to a proportional budget so the total summary stays within
       *max_summary_chars*.
    3. Wrap in a clearly labelled CONTEXT SUMMARY block so downstream prompts
       can identify compacted content.
    """
    # Separate assistant content from user prompts
    assistant_texts: List[str] = []
    user_texts: List[str] = []

    for msg in messages:
        role = str(msg.get("role", "")).lower()
        text = _message_text(msg).strip()
        if not text:
            continue
        if role == "assistant":
            assistant_texts.append(text)
        elif role == "user":
            user_texts.append(text)

    # Budget: 70 % to assistant content, 30 % to user context
    assistant_budget = int(max_summary_chars * 0.70)
    user_budget = int(max_summary_chars * 0.30)

    def _truncate_proportional(texts: List[str], budget: int) -> str:
        if not texts or budget <= 0:
            return ""
        per_item = max(100, budget // len(texts))
        parts = []
        for t in texts:
            parts.append(textwrap.shorten(t, width=per_item, placeholder=" [...]"))
        joined = "\n\n---\n\n".join(parts)
        # Hard-enforce the budget: when per_item was clamped to the 100-char
        # minimum, total output can be N× over budget.  Trim the final string
        # so the 50%-compression guarantee is never violated.
        if len(joined) > budget:
            joined = joined[: max(0, budget - 5)] + " [...]"
        return joined

    assistant_summary = _truncate_proportional(assistant_texts, assistant_budget)
    user_summary = _truncate_proportional(user_texts, user_budget)

    lines = [
        f"[CONTEXT SUMMARY — stage: {stage_name}]",
        "The following is a compressed summary of prior conversation context.",
        "Treat this as established background; do not re-derive these findings.",
        "",
    ]
    if assistant_summary:
        lines += ["## Prior Analysis & Findings", "", assistant_summary, ""]
    if user_summary:
        lines += ["## Prior Requests & Instructions", "", user_summary, ""]
    lines.append("[END CONTEXT SUMMARY]")
    return "\n".join(lines)


# ── Core manager ─────────────────────────────────────────────────────────────

class ContextBudgetManager:
    """
    Manages context window budget across pipeline stages.

    Thread-safe: each instance uses only immutable config; no shared mutable state.

    Parameters
    ----------
    token_budget:
        Hard token cap.  Triggers compaction at ``compact_threshold_ratio * token_budget``.
        Defaults to ``CONTEXT_BUDGET_TOKENS`` env var → 80 000.
    compact_threshold_ratio:
        Fraction of *token_budget* at which compaction is triggered.
        Defaults to ``CONTEXT_BUDGET_COMPACT_RATIO`` env var → 0.85.
    keep_recent:
        Number of most-recent messages always preserved intact after compaction.
        Defaults to ``CONTEXT_BUDGET_KEEP_RECENT`` env var → 6.
    chars_per_token:
        Character-to-token ratio for estimation.
        Defaults to ``CONTEXT_BUDGET_CHARS_PER_TOKEN`` env var → 4.
    """

    def __init__(
        self,
        *,
        token_budget: Optional[int] = None,
        compact_threshold_ratio: Optional[float] = None,
        keep_recent: Optional[int] = None,
        chars_per_token: Optional[float] = None,
    ) -> None:
        self.token_budget: int = int(
            token_budget
            if token_budget is not None
            else _env_int("CONTEXT_BUDGET_TOKENS", _DEFAULT_TOKEN_BUDGET)
        )
        self.compact_threshold_ratio: float = float(
            compact_threshold_ratio
            if compact_threshold_ratio is not None
            else _env_float("CONTEXT_BUDGET_COMPACT_RATIO", _DEFAULT_COMPACT_RATIO)
        )
        self.keep_recent: int = int(
            keep_recent
            if keep_recent is not None
            else _env_int("CONTEXT_BUDGET_KEEP_RECENT", _DEFAULT_KEEP_RECENT)
        )
        self.chars_per_token: float = float(
            chars_per_token
            if chars_per_token is not None
            else _env_float("CONTEXT_BUDGET_CHARS_PER_TOKEN", _DEFAULT_CHARS_PER_TOKEN)
        )
        # Clamp values to sane ranges
        self.token_budget = max(1_000, self.token_budget)
        self.compact_threshold_ratio = max(0.5, min(1.0, self.compact_threshold_ratio))
        self.keep_recent = max(1, self.keep_recent)
        self.chars_per_token = max(1.0, self.chars_per_token)

    @property
    def compact_threshold_tokens(self) -> int:
        return int(self.token_budget * self.compact_threshold_ratio)

    def estimate(self, messages: List[Dict[str, Any]]) -> int:
        """Return estimated token count for *messages*."""
        return estimate_messages_tokens(messages, chars_per_token=self.chars_per_token)

    def needs_compaction(self, messages: List[Dict[str, Any]]) -> bool:
        """Return True when messages exceed the compaction threshold."""
        return self.estimate(messages) >= self.compact_threshold_tokens

    def compact(
        self,
        messages: List[Dict[str, Any]],
        *,
        stage_name: str = "unknown",
    ) -> CompactionResult:
        """
        Compact *messages* by replacing early messages with a structured summary.

        The last ``keep_recent`` messages are always preserved so the model
        retains immediate conversational context.

        Returns a ``CompactionResult`` even when no compaction is needed (in that
        case ``messages_compacted == 0`` and ``compacted_messages`` equals the
        input).
        """
        tokens_before = self.estimate(messages)
        n = len(messages)

        if n <= self.keep_recent or not self.needs_compaction(messages):
            return CompactionResult(
                stage_name=stage_name,
                tokens_before=tokens_before,
                tokens_after=tokens_before,
                messages_before=n,
                messages_after=n,
                messages_compacted=0,
                compacted_messages=list(messages),
                summary_snippet="",
            )

        # Split: early messages to compress vs. recent messages to keep
        split_idx = max(0, n - self.keep_recent)
        early_messages = messages[:split_idx]
        recent_messages = messages[split_idx:]

        # Cap summary size to 50 % of the early-message char total so the
        # compacted context is guaranteed to be shorter than the original.
        early_chars = sum(len(_message_text(m)) for m in early_messages)
        max_summary_chars = max(200, int(early_chars * 0.50))

        # Generate summary of early messages
        summary_text = _generate_compaction_summary(
            early_messages,
            stage_name=stage_name,
            max_summary_chars=max_summary_chars,
        )
        summary_message: Dict[str, Any] = {
            "role": _SUMMARY_ROLE,
            "content": summary_text,
        }

        compacted: List[Dict[str, Any]] = [summary_message] + list(recent_messages)
        tokens_after = self.estimate(compacted)

        result = CompactionResult(
            stage_name=stage_name,
            tokens_before=tokens_before,
            tokens_after=tokens_after,
            messages_before=n,
            messages_after=len(compacted),
            messages_compacted=len(early_messages),
            compacted_messages=compacted,
            summary_snippet=summary_text[:200],
        )

        log_event(
            LOGGER,
            20,
            "context_compacted",
            (
                f"Context compacted for stage '{stage_name}': "
                f"{tokens_before} → {tokens_after} tokens "
                f"({result.compression_ratio:.0%} of original)."
            ),
            **result.to_log_fields(),
        )

        return result

    def compact_if_needed(
        self,
        messages: List[Dict[str, Any]],
        *,
        stage_name: str = "unknown",
    ) -> List[Dict[str, Any]]:
        """
        Convenience wrapper: compact *messages* if needed and return the
        (possibly unchanged) message list.

        Use this in pipeline stage dispatch when you don't need the full
        ``CompactionResult`` metadata.
        """
        if not self.needs_compaction(messages):
            return messages
        return self.compact(messages, stage_name=stage_name).compacted_messages

    def prune_raw_tool_results(
        self,
        messages: List[Dict[str, Any]],
        *,
        max_tool_result_chars: int = 2_000,
    ) -> List[Dict[str, Any]]:
        """
        Return a new message list with oversized tool-result content truncated.

        Tool-use results often carry large raw payloads (full web pages, long
        code outputs) that inflate token counts without adding new signal once
        the stage has processed them.

        This method is **non-destructive**: a new list is returned; the input
        is not mutated.

        Parameters
        ----------
        messages:
            List of message dicts (OpenAI / Anthropic format).
        max_tool_result_chars:
            Maximum character length for any tool-result text block.
            Content exceeding this limit is replaced with a truncation notice.
            Defaults to 2 000.

        Returns
        -------
        List[Dict[str, Any]]
            New message list with tool results truncated as needed.
        """
        max_chars = max(100, int(max_tool_result_chars))
        pruned: List[Dict[str, Any]] = []

        for msg in messages:
            role = str(msg.get("role", "")).lower()
            content = msg.get("content")

            # OpenAI function-call result: role="tool", content is a plain string
            if role == "tool" and isinstance(content, str):
                if len(content) > max_chars:
                    msg = dict(msg)
                    msg["content"] = (
                        content[:max_chars]
                        + f" [...truncated {len(content) - max_chars} chars]"
                    )
                pruned.append(msg)
                continue

            # Anthropic tool_result content blocks
            if isinstance(content, list):
                new_blocks: List[Any] = []
                changed = False
                for block in content:
                    if not isinstance(block, dict):
                        new_blocks.append(block)
                        continue
                    if block.get("type") == "tool_result":
                        inner = block.get("content", "")
                        if isinstance(inner, str) and len(inner) > max_chars:
                            block = dict(block)
                            block["content"] = (
                                inner[:max_chars]
                                + f" [...truncated {len(inner) - max_chars} chars]"
                            )
                            changed = True
                        elif isinstance(inner, list):
                            new_inner: List[Any] = []
                            inner_changed = False
                            for item in inner:
                                if (
                                    isinstance(item, dict)
                                    and item.get("type") == "text"
                                ):
                                    text = item.get("text", "")
                                    if isinstance(text, str) and len(text) > max_chars:
                                        item = dict(item)
                                        item["text"] = (
                                            text[:max_chars]
                                            + f" [...truncated {len(text) - max_chars} chars]"
                                        )
                                        inner_changed = True
                                new_inner.append(item)
                            if inner_changed:
                                block = dict(block)
                                block["content"] = new_inner
                                changed = True
                    new_blocks.append(block)
                if changed:
                    msg = dict(msg)
                    msg["content"] = new_blocks
                pruned.append(msg)
                continue

            pruned.append(msg)

        return pruned


# ── Module-level singleton ────────────────────────────────────────────────────

_DEFAULT_MANAGER: Optional[ContextBudgetManager] = None
_MANAGER_LOCK = threading.Lock()


def get_default_manager() -> ContextBudgetManager:
    """Return the process-wide default ``ContextBudgetManager`` (lazy-init, thread-safe)."""
    global _DEFAULT_MANAGER
    with _MANAGER_LOCK:
        if _DEFAULT_MANAGER is None:
            _DEFAULT_MANAGER = ContextBudgetManager()
    return _DEFAULT_MANAGER
