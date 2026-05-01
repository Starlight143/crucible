"""
features/ci_cd.py
=================
GitHub Actions output formatter.

Converts run output (analysis score, blocking risks, review issues, security
scan) into:

1. GitHub Actions workflow commands (``::error``, ``::warning``, ``::notice``)
   printed to stdout when running inside GitHub Actions.
2. A Markdown step summary written to ``$GITHUB_STEP_SUMMARY`` (if set).
3. ``{run_dir}/github_annotations.txt`` — one annotation command per line,
   always written regardless of CI environment.
4. ``{run_dir}/ci_summary.md`` — human-readable Markdown summary.

Usage::

    from crucible.features.ci_cd import write_github_outputs
    write_github_outputs("/path/to/run_dir")

Or from inside GitHub Actions::

    python run_crucible_enhanced.py run --ci-output
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

# ── Data model ────────────────────────────────────────────────────────────────

@dataclass
class GitHubAnnotation:
    """A single GitHub Actions workflow annotation command."""

    level: str               # "notice" | "warning" | "error"
    message: str
    file: Optional[str] = None
    line: Optional[int] = None
    title: Optional[str] = None

    def to_workflow_command(self) -> str:
        """Serialise to GitHub Actions ``::level params::message`` format."""
        params: List[str] = []
        if self.file:
            # Normalise path separators for GitHub
            normalised = self.file.replace("\\", "/")
            params.append(f"file={normalised}")
        if self.line is not None:
            # Guard against string line numbers from JSON (e.g. "12") which
            # would cause TypeError in `self.line > 0`.
            try:
                line_int = int(self.line)
                if line_int > 0:
                    params.append(f"line={line_int}")
            except (TypeError, ValueError):
                pass
        if self.title:
            # Percent-encode special characters in title
            # GitHub Actions params are comma-delimited key=value pairs, so
            # "," and ":" in values must also be encoded.
            safe_title = (
                self.title
                .replace("%", "%25")
                .replace("\r", "%0D")
                .replace("\n", "%0A")
                .replace(":", "%3A")
                .replace(",", "%2C")
            )
            params.append(f"title={safe_title}")
        # GitHub Actions format: ::level file=x,line=n,title=t::message
        # The separator between the level keyword and the first parameter is a
        # SPACE, not a comma.  Subsequent parameters are comma-separated.
        param_str = " " + ",".join(params) if params else ""
        # The message body (after the closing "::") only requires %/\r/\n
        # encoding.  Encoding ":" and "," in the message is incorrect and
        # produces garbled output like "score%3A 85" instead of "score: 85".
        # Only parameter *values* (file=, line=, title=) need the full set.
        safe_msg = (
            self.message
            .replace("%", "%25")
            .replace("\r", "%0D")
            .replace("\n", "%0A")
        )
        return f"::{self.level}{param_str}::{safe_msg}"


# ── Helpers ───────────────────────────────────────────────────────────────────

def _load_json(path: str) -> Dict[str, Any]:
    if not os.path.isfile(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError):
        return {}


def _score_level(score: Any) -> str:
    try:
        s = float(score)
        if s < 50:
            return "error"
        if s < 70:
            return "warning"
        return "notice"
    except (TypeError, ValueError):
        return "notice"


def _severity_to_level(severity: str) -> str:
    s = severity.lower()
    if s in ("high", "critical"):
        return "error"
    if s == "medium":
        return "warning"
    return "notice"


def _safe_int(value: Any) -> Optional[int]:
    """Convert *value* to int, returning None on failure.

    Guards against JSON line numbers that arrive as strings ("12") rather
    than integers.  Without this, ``self.line > 0`` in
    ``GitHubAnnotation.to_workflow_command`` raises TypeError.
    """
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


# ── Annotation builders ───────────────────────────────────────────────────────

def build_github_annotations(run_dir: str) -> List[GitHubAnnotation]:
    """Build the full list of GitHub Actions annotations for a run."""
    annotations: List[GitHubAnnotation] = []

    analysis = _load_json(os.path.join(run_dir, "analysis_result.json"))
    review = _load_json(os.path.join(run_dir, "review_report.json"))
    security = _load_json(os.path.join(run_dir, "security_report.json"))

    # Overall score
    score = analysis.get("score")
    risk = str(analysis.get("risk_level") or "unknown")
    project_name = str(analysis.get("project_name") or "")
    if score is not None:
        annotations.append(GitHubAnnotation(
            level=_score_level(score),
            message=f"Analysis score: {score}/100 | risk: {risk}",
            title=f"Crucible: {project_name}" if project_name else "Crucible Analysis",
        ))

    # Blocking risks
    gate_snap = analysis.get("gate_context_snapshot") or {}
    for risk_text in (gate_snap.get("blocking_risks") or [])[:5]:
        annotations.append(GitHubAnnotation(
            level="error",
            message=str(risk_text),
            title="Blocking Risk",
        ))

    # Code review issues
    for issue in (review.get("issues") or [])[:10]:
        sev = str(issue.get("severity") or "")
        annotations.append(GitHubAnnotation(
            level=_severity_to_level(sev),
            message=str(issue.get("description") or ""),
            file=issue.get("file"),
            line=_safe_int(issue.get("line")),
            title=f"Review [{issue.get('category', '')}]",
        ))

    # Security scan issues
    for issue in (security.get("issues") or [])[:10]:
        sev = str(issue.get("severity") or "")
        rule_id = str(issue.get("rule_id") or "")
        annotations.append(GitHubAnnotation(
            level=_severity_to_level(sev),
            message=f"[{rule_id}] {issue.get('description', '')}",
            file=issue.get("file"),
            line=_safe_int(issue.get("line")),
            title=f"Security [{sev.upper()}]",
        ))

    return annotations


# ── Step summary builder ──────────────────────────────────────────────────────

def build_step_summary_markdown(run_dir: str) -> str:
    """Build a Markdown document suitable for GITHUB_STEP_SUMMARY."""
    analysis = _load_json(os.path.join(run_dir, "analysis_result.json"))
    meta = _load_json(os.path.join(run_dir, "run_meta.json"))
    review = _load_json(os.path.join(run_dir, "review_report.json"))
    security = _load_json(os.path.join(run_dir, "security_report.json"))

    score = analysis.get("score", "N/A")
    risk = analysis.get("risk_level", "N/A")
    mode = meta.get("mode", analysis.get("mode_used", "N/A"))
    provider = meta.get("llm_provider", "N/A")
    timestamp = meta.get("timestamp", "N/A")
    project_name = analysis.get("project_name") or meta.get("project_name") or "Unknown"

    try:
        score_f = float(score)
        score_emoji = "🔴" if score_f < 50 else ("🟡" if score_f < 70 else "🟢")
        score_display = f"{score_f}/100"
    except (TypeError, ValueError):
        score_emoji = "⚪"
        score_display = str(score)

    lines: List[str] = [
        f"# Crucible Analysis: {project_name}",
        "",
        "| Field | Value |",
        "|-------|-------|",
        f"| Score | {score_emoji} **{score_display}** |",
        f"| Risk Level | {risk} |",
        f"| Mode | {mode} |",
        f"| Provider | {provider} |",
        f"| Timestamp | {timestamp} |",
        "",
    ]

    summary = str(analysis.get("summary") or "").strip()
    if summary:
        lines += ["## Summary", "", summary, ""]

    consensus = str(analysis.get("consensus") or "").strip()
    if consensus:
        lines += ["## Consensus", "", consensus, ""]

    gate_snap = analysis.get("gate_context_snapshot") or {}
    blocking = list(gate_snap.get("blocking_risks") or [])
    if blocking:
        lines += ["## ⛔ Blocking Risks", ""]
        for r in blocking[:10]:
            lines.append(f"- {r}")
        lines.append("")

    # Code review
    if review:
        rev_passed = bool(review.get("passes", True))
        issue_count = len(review.get("issues") or [])
        rev_status = "✅ PASSED" if rev_passed else "❌ FAILED"
        lines += [
            "## Code Review",
            "",
            f"**Status:** {rev_status} ({issue_count} issue(s))",
            "",
        ]
        for issue in (review.get("issues") or [])[:5]:
            sev = str(issue.get("severity") or "").upper()
            desc = str(issue.get("description") or "")
            fname = str(issue.get("file") or "")
            lines.append(f"- `[{sev}]` {desc}" + (f" — `{fname}`" if fname else ""))
        if issue_count > 5:
            lines.append(f"- *…and {issue_count - 5} more*")
        lines.append("")

    # Security scan
    if security:
        sec_passed = bool(security.get("passed", True))
        try:
            high_count = int(float(security.get("high_severity_count") or 0))
        except (ValueError, TypeError):
            high_count = 0
        scanner = str(security.get("scanner_used") or "pattern")
        sec_status = "✅ PASSED" if sec_passed else "❌ FAILED"
        lines += [
            "## Security Scan",
            "",
            f"**Status:** {sec_status} | scanner: `{scanner}` | HIGH issues: {high_count}",
            "",
        ]
        for issue in (security.get("issues") or [])[:5]:
            sev = str(issue.get("severity") or "").upper()
            if sev not in ("HIGH", "CRITICAL"):
                continue
            rule = str(issue.get("rule_id") or "")
            desc = str(issue.get("description") or "")
            lines.append(f"- `[{sev}][{rule}]` {desc}")
        lines.append("")

    return "\n".join(lines)


# ── Main entry point ──────────────────────────────────────────────────────────

def write_github_outputs(run_dir: str) -> None:
    """
    Write CI/CD output artefacts for *run_dir*.

    Always writes:
      - ``{run_dir}/github_annotations.txt``
      - ``{run_dir}/ci_summary.md``

    When ``GITHUB_ACTIONS=true`` also:
      - Prints annotation commands to stdout (GitHub picks them up).
      - Appends Markdown to ``$GITHUB_STEP_SUMMARY`` (if env var is set).
    """
    is_github_actions = os.environ.get("GITHUB_ACTIONS", "").lower() == "true"

    annotations = build_github_annotations(run_dir)
    summary_md = build_step_summary_markdown(run_dir)

    # Always write annotation file
    ann_path = os.path.join(run_dir, "github_annotations.txt")
    _tmp_ann = ann_path + ".tmp"
    try:
        with open(_tmp_ann, "w", encoding="utf-8") as fh:
            for ann in annotations:
                fh.write(ann.to_workflow_command() + "\n")
        os.replace(_tmp_ann, ann_path)
    except OSError:
        try:
            os.unlink(_tmp_ann)
        except OSError:
            pass

    # Always write ci_summary.md
    summary_path = os.path.join(run_dir, "ci_summary.md")
    _tmp_summary = summary_path + ".tmp"
    try:
        with open(_tmp_summary, "w", encoding="utf-8") as fh:
            fh.write(summary_md + "\n")
        os.replace(_tmp_summary, summary_path)
    except OSError:
        try:
            os.unlink(_tmp_summary)
        except OSError:
            pass

    if is_github_actions:
        # Emit annotations to stdout for GitHub Actions to parse
        for ann in annotations:
            print(ann.to_workflow_command(), flush=True)

        # Append to GITHUB_STEP_SUMMARY
        step_summary_file = os.environ.get("GITHUB_STEP_SUMMARY", "")
        if step_summary_file:
            try:
                with open(step_summary_file, "a", encoding="utf-8") as fh:
                    fh.write(summary_md + "\n")
            except OSError as exc:
                import sys
                print(f"[ci_cd] Warning: could not write GITHUB_STEP_SUMMARY: {exc}", file=sys.stderr)
