"""
features/diff_aware.py
======================
Git-diff context injection for incremental analysis.

Discovers files changed since a given git ref and builds a compact context
summary that can be prepended to user prompts before the pipeline runs.
This allows analysts to focus on changed files instead of re-analysing the
entire codebase from scratch.

Usage (from enhanced runner)::

    from crucible.features.diff_aware import build_diff_aware_prompt_prefix
    prefix = build_diff_aware_prompt_prefix("/path/to/project", "HEAD~1")
    if prefix:
        print(prefix)

No optional dependencies required.
"""
from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

# ── Public data model ────────────────────────────────────────────────────────

@dataclass
class DiffContext:
    """Structured summary of changes between the working tree and a git ref."""

    base_ref: str
    project_dir: str
    added_files: List[str] = field(default_factory=list)
    modified_files: List[str] = field(default_factory=list)
    deleted_files: List[str] = field(default_factory=list)
    is_git_repo: bool = True

    @property
    def changed_files(self) -> List[str]:
        """Files that were added or modified (relevant for re-analysis)."""
        return self.added_files + self.modified_files

    @property
    def total_changed(self) -> int:
        return len(self.added_files) + len(self.modified_files) + len(self.deleted_files)

    def is_empty(self) -> bool:
        return self.total_changed == 0

    def as_context_text(self) -> str:
        """Compact multi-line text summarising the diff."""
        lines = [f"[Git Diff Context — base ref: {self.base_ref}]"]
        if self.added_files:
            preview = ", ".join(self.added_files[:20])
            suffix = f" (…+{len(self.added_files) - 20} more)" if len(self.added_files) > 20 else ""
            lines.append(f"  Added   ({len(self.added_files)}): {preview}{suffix}")
        if self.modified_files:
            preview = ", ".join(self.modified_files[:20])
            suffix = f" (…+{len(self.modified_files) - 20} more)" if len(self.modified_files) > 20 else ""
            lines.append(f"  Modified({len(self.modified_files)}): {preview}{suffix}")
        if self.deleted_files:
            preview = ", ".join(self.deleted_files[:10])
            lines.append(f"  Deleted ({len(self.deleted_files)}): {preview}")
        return "\n".join(lines)


# ── Internal helpers ──────────────────────────────────────────────────────────

def _run_git(args: List[str], cwd: str) -> Optional[str]:
    """Run a git sub-command; return stdout on success, None on failure."""
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode == 0:
            return result.stdout.strip()
        return None
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return None


def _is_git_repo(directory: str) -> bool:
    return _run_git(["rev-parse", "--git-dir"], directory) is not None


def _ref_exists(directory: str, ref: str) -> bool:
    return _run_git(["rev-parse", "--verify", ref], directory) is not None


def _resolve_base_ref(directory: str, requested_ref: str) -> Optional[str]:
    """
    Resolve the requested git ref to a usable base.
    Falls back to the initial commit if HEAD~1 doesn't exist
    (i.e. repo has only one commit).
    """
    if _ref_exists(directory, requested_ref):
        return requested_ref
    # Try the very first commit as a universal fallback
    first_commit = _run_git(["rev-list", "--max-parents=0", "HEAD"], directory)
    if first_commit:
        return first_commit
    return None


def _parse_name_status(output: str) -> DiffContext:
    """Parse `git diff --name-status` output into a DiffContext."""
    ctx = DiffContext(base_ref="", project_dir="")
    for line in output.splitlines():
        parts = line.split("\t", 1)
        if len(parts) != 2:
            continue
        status_raw, filepath = parts[0].strip(), parts[1].strip()
        # Status codes: A=Added, M=Modified, D=Deleted, R=Renamed, C=Copied, T=Type-changed
        if status_raw.startswith("A"):
            ctx.added_files.append(filepath)
        elif status_raw.startswith("M") or status_raw.startswith("T"):
            ctx.modified_files.append(filepath)
        elif status_raw.startswith("D"):
            ctx.deleted_files.append(filepath)
        elif status_raw.startswith("R") or status_raw.startswith("C"):
            # For renames/copies the format is "R<score>\t<old>\t<new>"
            # After split("\t", 1) the second part may be "old\tnew"
            subparts = filepath.split("\t", 1)
            new_path = subparts[-1].strip()
            ctx.modified_files.append(new_path)
    return ctx


# ── Public API ────────────────────────────────────────────────────────────────

def get_diff_context(project_dir: str, base_ref: str = "HEAD~1") -> DiffContext:
    """
    Build a DiffContext for *project_dir* relative to *base_ref*.

    Returns an empty DiffContext (is_git_repo=False) when the directory is
    not a git repository, or when no changes are found.
    """
    project_dir = str(Path(project_dir).resolve())

    if not _is_git_repo(project_dir):
        return DiffContext(base_ref=base_ref, project_dir=project_dir, is_git_repo=False)

    resolved_ref = _resolve_base_ref(project_dir, base_ref)
    if resolved_ref is None:
        return DiffContext(base_ref=base_ref, project_dir=project_dir)

    # Try committed diff first
    output = _run_git(["diff", "--name-status", resolved_ref, "HEAD"], project_dir)
    # Also include uncommitted changes against HEAD
    unstaged = _run_git(["diff", "--name-status", "HEAD"], project_dir) or ""

    combined = "\n".join(filter(None, [output, unstaged]))
    if not combined.strip():
        return DiffContext(base_ref=resolved_ref, project_dir=project_dir)

    ctx = _parse_name_status(combined)
    ctx.base_ref = resolved_ref
    ctx.project_dir = project_dir
    # De-duplicate (a file may appear in both committed and uncommitted)
    ctx.added_files = sorted(set(ctx.added_files))
    ctx.modified_files = sorted(set(ctx.modified_files) - set(ctx.added_files))
    ctx.deleted_files = sorted(set(ctx.deleted_files) - set(ctx.added_files) - set(ctx.modified_files))
    return ctx


def build_diff_aware_prompt_prefix(
    project_dir: str,
    base_ref: str = "HEAD~1",
) -> str:
    """
    Return a prompt prefix string describing changed files.

    Returns an empty string when there are no changes or the directory is not
    a git repo (so it is safe to unconditionally prepend to any prompt).
    """
    ctx = get_diff_context(project_dir, base_ref)
    if ctx.is_empty():
        return ""
    # Use explicit concatenation instead of .format() so that brace characters
    # inside the diff content (e.g. Python dict literals, f-strings) do not
    # cause a KeyError or IndexError.
    return (
        "\n"
        "--- Incremental Analysis Context (git diff from "
        + ctx.base_ref
        + ") ---\n"
        + ctx.as_context_text()
        + "\n"
        "Focus analysis on the above changed files and their downstream\n"
        "dependencies.  Unchanged files may be referenced for context only.\n"
        "---\n"
    )
