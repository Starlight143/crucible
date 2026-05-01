"""
features/post_analysis_chat.py
================================
Interactive post-analysis Q&A mode for the Crucible pipeline.

After a pipeline run completes, this module launches an interactive terminal
session where the user can ask follow-up questions about the analysis.  The
LLM answers within the context of the specific run's outputs — so questions
like "explain the kill-criteria" or "which risk should I tackle first?" are
grounded in the actual analysis rather than generic knowledge.

Session context
---------------
The session loads the following files from the run directory:
* ``analysis_result.json``   — analysis scores, consensus, risks, experiments
* ``run_snapshot.json``       — direction decision
* ``run_meta.json``           — provider, mode, timestamp
* ``security_report.json``    — security scan summary (if present)
* ``code/*.py``               — up to 5 generated code files (first 2 000 chars each)

Context is capped at ``POST_CHAT_CONTEXT_CHARS`` env var (default 12 000
chars) to stay within the LLM's context window.

Usage::

    # As a library
    from crucible.features.post_analysis_chat import start_post_analysis_chat

    start_post_analysis_chat(
        run_dir="/path/to/saved_projects/my_run",
        llm=my_llm_instance,
    )

    # Or via the enhanced runner (runs automatically after a pipeline run):
    python run_crucible_enhanced.py run --post-chat

Non-interactive use
-------------------
Pass ``questions`` to ``ask_question`` for programmatic single-question mode
(useful in tests or automation)::

    from crucible.features.post_analysis_chat import ask_question

    answer = ask_question(
        run_dir="...",
        question="What is the main technical risk?",
        llm=llm,
    )
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

# ── Configuration ─────────────────────────────────────────────────────────────

def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, ""))
    except (ValueError, TypeError):
        return default


_CONTEXT_CHARS: int = _env_int("POST_CHAT_CONTEXT_CHARS", 12_000)
_MAX_CODE_FILES: int = 5
_MAX_CODE_CHARS: int = 2_000

# ── Data models ───────────────────────────────────────────────────────────────

@dataclass
class ChatMessage:
    role: str     # "user" | "assistant" | "system"
    content: str

    def to_dict(self) -> Dict[str, str]:
        return {"role": self.role, "content": self.content}


@dataclass
class PostAnalysisChatSession:
    """
    Manages a multi-turn Q&A session grounded in a specific run's outputs.
    """

    run_dir: str
    llm: Any                          # LangChain ChatOpenAI-compatible LLM
    history: List[ChatMessage] = field(default_factory=list)
    context_text: str = ""            # Loaded once at init
    error: Optional[str] = None
    session_file: Optional[str] = None  # Path to persist history JSON

    def __post_init__(self) -> None:
        self.context_text = _build_run_context(self.run_dir)
        # Auto-set session file path if not explicitly provided
        if self.session_file is None:
            self.session_file = os.path.join(self.run_dir, ".postchat_history.json")
        # Load persisted history if file exists and history is currently empty
        if not self.history and self.session_file and os.path.isfile(self.session_file):
            self.history = _load_chat_history(self.session_file)

    def ask(self, question: str) -> str:
        """
        Send *question* to the LLM with run context and conversation history.

        Returns the assistant's response string.  Raises RuntimeError if the
        LLM call fails.
        """
        if not question.strip():
            return ""

        self.history.append(ChatMessage(role="user", content=question.strip()))

        messages = _build_messages(self.context_text, self.history)

        try:
            response = _invoke_llm(self.llm, messages)
        except Exception as exc:
            self.history.pop()  # remove unanswered user message
            raise RuntimeError(f"LLM call failed: {exc}") from exc

        self.history.append(ChatMessage(role="assistant", content=response))
        self._save_history()
        return response

    def _save_history(self) -> None:
        """Persist conversation history to disk."""
        if not self.session_file:
            return
        try:
            data = [m.to_dict() for m in self.history]
            tmp = self.session_file + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(data, fh, indent=2, ensure_ascii=False)
            os.replace(tmp, self.session_file)
        except OSError as exc:
            import warnings
            warnings.warn(
                f"[PostChat] Failed to persist chat history to {self.session_file!r}: {exc}",
                stacklevel=2,
            )

    def reset(self) -> None:
        """Clear conversation history (context is retained) and delete persisted file."""
        self.history.clear()
        if self.session_file and os.path.isfile(self.session_file):
            try:
                os.remove(self.session_file)
            except OSError:
                pass


# ── Context builder ───────────────────────────────────────────────────────────

def _load_chat_history(path: str) -> List[ChatMessage]:
    """Load persisted chat history from *path*; return [] on any error."""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        if not isinstance(data, list):
            return []
        messages = []
        for item in data:
            if isinstance(item, dict) and item.get("role") and item.get("content"):
                messages.append(ChatMessage(role=str(item["role"]), content=str(item["content"])))
        return messages
    except (OSError, json.JSONDecodeError, KeyError):
        return []


def _load_json(path: str) -> Dict[str, Any]:
    if not os.path.isfile(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _load_code_snippets(run_dir: str, max_files: int, max_chars: int) -> str:
    code_dir = os.path.join(run_dir, "code")
    if not os.path.isdir(code_dir):
        return ""

    snippets: List[str] = []
    count = 0
    # `followlinks=False` is the os.walk default but make it explicit:
    # symlink loops in user-provided code dirs would otherwise cause
    # infinite traversal under a future default change.
    for dirpath, _dirs, filenames in os.walk(code_dir, followlinks=False):
        for fname in sorted(filenames):
            if count >= max_files:
                break
            if not fname.endswith(".py"):
                continue
            fpath = os.path.join(dirpath, fname)
            try:
                with open(fpath, "r", encoding="utf-8", errors="replace") as fh:
                    full_content = fh.read()
                truncated = len(full_content) > max_chars
                content = full_content[:max_chars]
                rel = os.path.relpath(fpath, code_dir)
                header = f"# --- {rel}{' (truncated)' if truncated else ''} ---"
                snippets.append(f"{header}\n{content}")
                count += 1
            except OSError:
                pass
        if count >= max_files:
            break

    if not snippets:
        return ""
    return "\n\n".join(snippets)


def _build_run_context(run_dir: str) -> str:
    """Load and format run outputs into a single context string."""
    analysis = _load_json(os.path.join(run_dir, "analysis_result.json"))
    snapshot = _load_json(os.path.join(run_dir, "run_snapshot.json"))
    meta = _load_json(os.path.join(run_dir, "run_meta.json"))
    security = _load_json(os.path.join(run_dir, "security_report.json"))

    parts: List[str] = []

    # Header
    project = (
        analysis.get("project_name")
        or meta.get("project_name")
        or os.path.basename(run_dir)
    )
    score = analysis.get("score")
    risk = analysis.get("risk_level", "unknown")
    mode = meta.get("mode") or analysis.get("mode_used", "N/A")
    provider = meta.get("llm_provider", "N/A")
    ts = meta.get("timestamp", "N/A")

    parts.append(
        f"PROJECT: {project}\n"
        f"Score: {score}/100 | Risk: {risk} | Mode: {mode} | Provider: {provider} | {ts}"
    )

    # Gate decision
    gate = analysis.get("gate_decision") or snapshot.get("gate_decision")
    if gate:
        parts.append(f"Gate Decision: {gate}")

    # Direction
    dd = snapshot.get("direction_decision") or {}
    if isinstance(dd, dict):
        direction = dd.get("selected_direction") or dd.get("direction")
        if direction:
            parts.append(f"Selected Direction: {direction}")
        go_cond = dd.get("go_conditions") or dd.get("go_condition")
        if go_cond:
            parts.append(f"Go Conditions: {go_cond}")
        kill_crit = dd.get("kill_criteria")
        if kill_crit:
            parts.append(f"Kill Criteria: {kill_crit}")

    # Consensus / Disagreement
    consensus = str(analysis.get("consensus") or "")[:800]
    if consensus:
        parts.append(f"CONSENSUS:\n{consensus}")

    disagreement = str(analysis.get("disagreement") or "")[:500]
    if disagreement:
        parts.append(f"DISAGREEMENT:\n{disagreement}")

    # Blocking risks
    risks = list(analysis.get("blocking_risks") or [])
    if not risks:
        gate_snap = analysis.get("gate_context_snapshot") or {}
        if isinstance(gate_snap, dict):
            risks = list(gate_snap.get("blocking_risks") or [])
    if risks:
        parts.append(
            "BLOCKING RISKS:\n" + "\n".join(f"  - {r}" for r in risks[:8])
        )

    # Experiments
    experiments = list(analysis.get("experiments") or [])
    if experiments:
        exp_lines = []
        for e in experiments[:5]:
            if isinstance(e, dict):
                goal = e.get("goal") or e.get("name") or str(e)[:80]
                exp_lines.append(f"  - {goal}")
        if exp_lines:
            parts.append("PROPOSED EXPERIMENTS:\n" + "\n".join(exp_lines))

    # Security summary
    if security:
        sec_passed = security.get("passed", True)
        high = len([
            i for i in (security.get("issues") or [])
            if isinstance(i, dict)
            and str(i.get("severity", "")).upper() in ("HIGH", "CRITICAL")
        ])
        parts.append(
            f"SECURITY: {'PASSED' if sec_passed else 'FAILED'} "
            f"(HIGH/CRITICAL issues: {high})"
        )

    # Code snippets
    code_text = _load_code_snippets(run_dir, _MAX_CODE_FILES, _MAX_CODE_CHARS)
    if code_text:
        parts.append(f"GENERATED CODE SAMPLES:\n{code_text}")

    full_context = "\n\n".join(parts)

    # Apply total character budget
    if len(full_context) > _CONTEXT_CHARS:
        _suffix = "\n...[context truncated]"
        _budget = max(0, _CONTEXT_CHARS - len(_suffix))
        full_context = full_context[:_budget] + _suffix

    return full_context


# ── LLM adapter ───────────────────────────────────────────────────────────────

def _build_messages(
    context: str,
    history: List[ChatMessage],
) -> List[Dict[str, str]]:
    """Build the message list for the LLM call."""
    system_prompt = (
        "You are a precise analysis assistant with deep knowledge of the "
        "Crucible pipeline output shown below.\n\n"
        "You have access to the full analysis results, direction decision, "
        "blocking risks, proposed experiments, and generated code samples.\n\n"
        "Answer questions concisely and accurately based on this data. "
        "If information is not present in the context, say so clearly — "
        "do NOT hallucinate.\n\n"
        "=== RUN CONTEXT ===\n"
        f"{context}\n"
        "=== END RUN CONTEXT ==="
    )

    messages: List[Dict[str, str]] = [
        {"role": "system", "content": system_prompt}
    ]
    for msg in history:
        messages.append(msg.to_dict())
    return messages


def _invoke_llm(llm: Any, messages: List[Dict[str, str]]) -> str:
    """
    Invoke the LLM with *messages* and return the response text.

    Supports:
    - LangChain ChatOpenAI (``llm.invoke(messages)``)
    - Any callable that accepts a list of message dicts
    """
    # Try LangChain-style invoke with HumanMessage/SystemMessage objects
    try:
        from langchain_core.messages import HumanMessage, SystemMessage, AIMessage

        lc_messages = []
        for m in messages:
            role = m["role"]
            content = m["content"]
            if role == "system":
                lc_messages.append(SystemMessage(content=content))
            elif role == "user":
                lc_messages.append(HumanMessage(content=content))
            elif role == "assistant":
                lc_messages.append(AIMessage(content=content))

        result = llm.invoke(lc_messages)
        if hasattr(result, "content"):
            return str(result.content)
        return str(result)
    except ImportError:
        pass

    # Fallback: try direct dict-based invoke (OpenAI-compatible)
    try:
        result = llm.invoke(messages)
        if hasattr(result, "content"):
            return str(result.content)
        return str(result)
    except Exception:
        pass

    # Last resort: treat llm as a callable
    try:
        result = llm(messages)
        if hasattr(result, "content"):
            return str(result.content)
        return str(result)
    except Exception as exc:
        raise RuntimeError(f"All LLM invocation strategies failed: {exc}") from exc


# ── Public API ────────────────────────────────────────────────────────────────

def ask_question(
    run_dir: str,
    question: str,
    *,
    llm: Any,
    history: Optional[List[ChatMessage]] = None,
) -> str:
    """
    Ask a single question about a completed pipeline run.

    This function operates in **stateless single-question mode**: it never
    reads from or writes to the persistent ``.postchat_history.json`` file.
    Each call starts with a clean conversation slate (unless ``history`` is
    explicitly provided).

    To use persistent multi-turn sessions, use
    :class:`PostAnalysisChatSession` or :func:`start_post_analysis_chat`
    directly.

    Parameters
    ----------
    run_dir:
        Path to a completed pipeline run directory.
    question:
        The question to ask.
    llm:
        A LangChain-compatible LLM instance.
    history:
        Optional list of prior ``ChatMessage`` objects to maintain conversational
        context across multiple ``ask_question`` calls.  When ``None`` (the
        default) the session starts with no prior history.

    Returns
    -------
    str
        The LLM's answer.
    """
    session = PostAnalysisChatSession(
        run_dir=run_dir,
        llm=llm,
        history=list(history) if history else [],
        session_file="",  # Empty string disables disk persistence in __post_init__
    )
    return session.ask(question)


def start_post_analysis_chat(
    run_dir: str,
    llm: Any,
    *,
    banner: bool = True,
) -> None:
    """
    Start an interactive terminal Q&A session for a completed pipeline run.

    Reads from stdin and writes to stdout.  The session ends when the user
    types ``exit``, ``quit``, or presses Ctrl+C / Ctrl+D.

    Parameters
    ----------
    run_dir:
        Path to a completed pipeline run directory.
    llm:
        A LangChain-compatible LLM instance.
    banner:
        If True (default), print a welcome banner with session info.
    """
    if not os.path.isdir(run_dir):
        print(
            f"[PostChat] Run directory not found: {run_dir}",
            file=sys.stderr,
            flush=True,
        )
        return

    session = PostAnalysisChatSession(run_dir=run_dir, llm=llm)
    if session.history and banner:
        print(
            f"[PostChat] Resuming session with {len(session.history)} prior message(s). "
            "Type 'reset' to clear history.\n",
            flush=True,
        )

    if not session.context_text.strip():
        print(
            "[PostChat] No analysis data found in run directory — "
            "ensure analysis_result.json exists.",
            file=sys.stderr,
            flush=True,
        )
        return

    if banner:
        project_line = session.context_text.split("\n")[0]
        print(
            "\n"
            "┌─────────────────────────────────────────────┐\n"
            "│  Crucible Post-Analysis Chat                │\n"
            "│  Ask any question about this run's results   │\n"
            "│  Type 'exit' or press Ctrl+C to quit         │\n"
            "└─────────────────────────────────────────────┘\n"
            f"  {project_line}\n",
            flush=True,
        )

    while True:
        try:
            raw = input("You: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n[PostChat] Session ended.", flush=True)
            break

        if not raw:
            continue

        if raw.lower() in ("exit", "quit", "q", ":q"):
            print("[PostChat] Session ended.", flush=True)
            break

        if raw.lower() in ("reset", "clear"):
            session.reset()
            print("[PostChat] Conversation history cleared.", flush=True)
            continue

        try:
            answer = session.ask(raw)
            print(f"\nAssistant: {answer}\n", flush=True)
        except RuntimeError as exc:
            print(f"[PostChat] Error: {exc}", file=sys.stderr, flush=True)
