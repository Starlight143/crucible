"""
features/interactive_mode.py
============================
Pre-run interactive context-gathering session.

When ``--interactive`` is enabled the enhanced runner pauses before invoking
the core pipeline and prompts the user for:

  1. Risk tolerance preference (conservative / moderate / aggressive)
  2. Additional research focus areas / constraints
  3. Hard constraints or deal-breaker requirements
  4. Specific hypotheses to test
  5. Free-text additional notes

The collected context is:

  a) Written to ``_interactive_context.txt`` in the working directory so the
     pipeline's librarian / research stages can read it as project context.
  b) Stored in the ``PIPELINE_INTERACTIVE_CONTEXT`` env var for tracing.
  c) Cleaned up automatically after the run completes via
     ``cleanup_interactive_context()``.

Non-interactive fallback
------------------------
When stdin is not a TTY (CI environment, piped input, batch mode) the session
returns an empty ``InteractiveContext`` immediately so callers behave
identically in automated environments — no prompts, no file written.

Usage::

    from crucible.features.interactive_mode import (
        run_interactive_pre_run,
        cleanup_interactive_context,
    )

    # Before running the core pipeline:
    context_path = run_interactive_pre_run(workspace_dir)

    # ... invoke core pipeline ...

    # After the pipeline finishes:
    cleanup_interactive_context(context_path)
"""
from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import List, Optional

# ── Constants ─────────────────────────────────────────────────────────────────

_CONTEXT_FILENAME = "_interactive_context.txt"
_ENV_VAR = "PIPELINE_INTERACTIVE_CONTEXT"

_RISK_MAP = {
    "1": "conservative",
    "2": "moderate",
    "3": "aggressive",
    "conservative": "conservative",
    "moderate": "moderate",
    "aggressive": "aggressive",
}


# ── Data model ────────────────────────────────────────────────────────────────

@dataclass
class InteractiveContext:
    """Structured user input collected from an interactive pre-run session."""

    focus_areas: List[str] = field(default_factory=list)
    constraints: List[str] = field(default_factory=list)
    risk_tolerance: str = "moderate"  # conservative | moderate | aggressive
    hypotheses: List[str] = field(default_factory=list)
    free_text: str = ""
    collected_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )

    def is_empty(self) -> bool:
        """Return True when the user provided no meaningful input."""
        return (
            not self.focus_areas
            and not self.constraints
            and not self.hypotheses
            and not self.free_text.strip()
        )

    def to_text(self) -> str:
        """Render as a human-readable / LLM-readable guidance block."""
        lines: List[str] = [
            "=== Interactive Research Guidance ===",
            f"Collected at: {self.collected_at}",
            f"Risk Tolerance: {self.risk_tolerance}",
        ]
        if self.focus_areas:
            lines.append("\nFocus Areas:")
            for item in self.focus_areas:
                lines.append(f"  - {item}")
        if self.constraints:
            lines.append("\nConstraints / Hard Limits:")
            for item in self.constraints:
                lines.append(f"  - {item}")
        if self.hypotheses:
            lines.append("\nHypotheses to Test:")
            for item in self.hypotheses:
                lines.append(f"  - {item}")
        if self.free_text.strip():
            lines.append("\nAdditional Notes:")
            lines.append(self.free_text.strip())
        lines.append("=== End of Interactive Guidance ===")
        return "\n".join(lines)


# ── Input helpers ─────────────────────────────────────────────────────────────

def _prompt_line(prompt: str, default: str = "") -> str:
    """Print *prompt* and read one line; return *default* on empty input."""
    try:
        value = input(prompt).strip()
    except (EOFError, KeyboardInterrupt):
        return default
    return value if value else default


def _prompt_list(prompt_header: str, item_prompt: str) -> List[str]:
    """
    Prompt for a variable-length list of text items.

    An empty entry terminates input.  Returns a (possibly empty) list of
    non-empty strings.
    """
    print(prompt_header, flush=True)
    items: List[str] = []
    index = 1
    while True:
        try:
            val = input(f"  [{index}] {item_prompt} (blank to finish): ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if not val:
            break
        items.append(val)
        index += 1
    return items


def _is_interactive_tty() -> bool:
    """Return True only when both stdin and stdout are real terminals."""
    return sys.stdin.isatty() and sys.stdout.isatty()


# ── Core session logic ────────────────────────────────────────────────────────

def collect_interactive_context(workspace_dir: str) -> InteractiveContext:
    """
    Run an interactive stdin prompt session and return the collected context.

    When stdin / stdout are not TTYs (CI, piped, batch mode) returns an empty
    ``InteractiveContext`` immediately without printing anything.

    Parameters
    ----------
    workspace_dir:
        Workspace root (used only for displaying recent project memory
        information if available in the future — not written to here).
    """
    if not _is_interactive_tty():
        return InteractiveContext()

    print("\n" + "=" * 60, flush=True)
    print(" Interactive Research Guidance Session", flush=True)
    print(" Press ENTER at any prompt to skip that section.", flush=True)
    print("=" * 60, flush=True)

    # ── Risk tolerance ────────────────────────────────────────────────────────
    print("\nRisk tolerance for this analysis?", flush=True)
    print("  1. conservative  — emphasis on capital preservation, low downside", flush=True)
    print("  2. moderate      — balanced risk/reward  (default)", flush=True)
    print("  3. aggressive    — high upside potential, accepts higher risk", flush=True)
    rt_input = _prompt_line("Choose [1/2/3, default 2]: ", "2").lower()
    risk_tolerance = _RISK_MAP.get(rt_input, "moderate")

    # ── Focus areas ───────────────────────────────────────────────────────────
    focus_areas = _prompt_list(
        "\nWhat specific aspects should the research emphasise?",
        "Focus area",
    )

    # ── Hard constraints ──────────────────────────────────────────────────────
    constraints = _prompt_list(
        "\nAny hard constraints or deal-breaker requirements?",
        "Constraint",
    )

    # ── Hypotheses ────────────────────────────────────────────────────────────
    hypotheses = _prompt_list(
        "\nSpecific hypotheses you want the analysis to test?",
        "Hypothesis",
    )

    # ── Free text ─────────────────────────────────────────────────────────────
    print("\nAdditional notes / context (single line, blank to skip):", flush=True)
    free_text = _prompt_line("  > ", "")

    ctx = InteractiveContext(
        focus_areas=focus_areas,
        constraints=constraints,
        risk_tolerance=risk_tolerance,
        hypotheses=hypotheses,
        free_text=free_text,
    )
    print("\n[Interactive] Context collected.", flush=True)
    return ctx


# ── File I/O ──────────────────────────────────────────────────────────────────

def write_context_file(ctx: InteractiveContext, workspace_dir: str) -> Optional[str]:
    """
    Serialise *ctx* to ``_interactive_context.txt`` in *workspace_dir*.

    Sets the ``PIPELINE_INTERACTIVE_CONTEXT`` env var to the absolute file path.
    Because the enhanced runner invokes the core pipeline **in-process** via
    ``from crucible.cli import main as _core_main; _core_main()``, the
    ``os.environ`` mutation is immediately visible to all code in the current
    process — no subprocess spawn is required for this to work.

    Returns the absolute path of the written file, or ``None`` if the context
    is empty (no useful input provided) or writing fails.
    """
    if ctx.is_empty():
        return None
    path = os.path.join(workspace_dir, _CONTEXT_FILENAME)
    _tmp_hist = path + ".tmp"
    try:
        with open(_tmp_hist, "w", encoding="utf-8") as fh:
            fh.write(ctx.to_text())
            fh.write("\n")
        os.replace(_tmp_hist, path)
        os.environ[_ENV_VAR] = path
        return path
    except OSError:
        try:
            os.unlink(_tmp_hist)
        except OSError:
            pass
        return None


def cleanup_interactive_context(context_path: Optional[str]) -> None:
    """
    Remove the temporary guidance file and unset the env var.

    Safe to call even when *context_path* is ``None``, the file no longer
    exists (deleted by the pipeline itself or by a crash), or the path is
    not a regular file.
    """
    os.environ.pop(_ENV_VAR, None)
    if context_path and os.path.isfile(context_path):
        try:
            os.remove(context_path)
        except OSError:
            pass


# ── Public high-level API ─────────────────────────────────────────────────────

def run_interactive_pre_run(workspace_dir: str) -> Optional[str]:
    """
    Collect interactive user context and write the guidance file.

    This is the single entry-point used by the enhanced runner.

    Returns the path to the written context file, or ``None`` when the user
    skipped all prompts or the environment is non-interactive.
    """
    ctx = collect_interactive_context(workspace_dir)
    return write_context_file(ctx, workspace_dir)
