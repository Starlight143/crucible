"""
features/api_version_autopatch.py
==================================
Auto-patch engine for deprecated API calls in generated code.

Reads the ``ApiVersionReport`` persisted inside ``run_snapshot.json`` and
generates LLM-powered patches for every MEDIUM/HIGH/CRITICAL severity issue.

Each matched source file is re-written in-place (unless ``dry_run=True``).
A ``api_autopatch_report.json`` is written to *run_dir*.

Only issues with a non-empty ``deprecated_api`` *and* ``recommended_api``
are processed.  The engine finds candidate files by searching for the
deprecated symbol name and skips files where the symbol is absent.

Usage::

    from crucible.features.api_version_autopatch import run_api_version_autopatch
    report = run_api_version_autopatch("/path/to/run_dir", llm)
    print(f"{report.patches_applied}/{report.patches_attempted} patches applied")
"""
from __future__ import annotations

import ast
import json
import os
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from crucible.output_validation import strip_reasoning_blocks

# ── Data models ───────────────────────────────────────────────────────────────

@dataclass
class AutoPatchResult:
    success: bool
    library: str
    deprecated_api: str
    recommended_api: str
    file_patched: str
    applied: bool = False
    error: str = ""


@dataclass
class AutoPatchReport:
    success: bool
    patches_attempted: int
    patches_applied: int
    patches_failed: int
    results: List[AutoPatchResult] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "success": self.success,
            "patches_attempted": self.patches_attempted,
            "patches_applied": self.patches_applied,
            "patches_failed": self.patches_failed,
            "results": [
                {
                    "library": r.library,
                    "deprecated_api": r.deprecated_api,
                    "recommended_api": r.recommended_api,
                    "file_patched": r.file_patched,
                    "applied": r.applied,
                    "error": r.error,
                }
                for r in self.results
            ],
            "errors": self.errors,
        }


# ── Helpers ───────────────────────────────────────────────────────────────────

_PATCHABLE_SEVERITIES = {"medium", "high", "critical"}


def _load_api_version_issues(run_dir: str) -> List[Dict[str, Any]]:
    """Extract ApiVersionReport issues from run_snapshot.json."""
    snapshot_path = os.path.join(run_dir, "run_snapshot.json")
    if not os.path.isfile(snapshot_path):
        return []
    try:
        with open(snapshot_path, "r", encoding="utf-8") as fh:
            snapshot = json.load(fh)
        api_report = snapshot.get("api_version_report") or {}
        issues = api_report.get("issues") or []
        return [i for i in issues if isinstance(i, dict)]
    except (json.JSONDecodeError, OSError):
        return []


def _collect_python_files(code_dir: str) -> List[str]:
    files: List[str] = []
    for dirpath, _, filenames in os.walk(code_dir):
        for fname in sorted(filenames):
            if fname.endswith(".py"):
                files.append(os.path.join(dirpath, fname))
    return files


def _find_symbol_in_source(source: str, deprecated_api: str) -> bool:
    """
    Quick check: does *source* reference the deprecated symbol at all?
    Checks the last component of a dotted name (e.g. 'fetch_ohlcv' from
    'ccxt.exchange.fetch_ohlcv').
    """
    symbol = deprecated_api.split(".")[-1] if "." in deprecated_api else deprecated_api
    # Word-boundary search to avoid false positives like 'fetch_ohlcv2'
    pattern = re.compile(r"\b" + re.escape(symbol) + r"\b")
    return bool(pattern.search(source))


def _strip_code_fences(text: str) -> str:
    # Reasoning-model defence: DeepSeek-V4 / GLM-5.1 / o1-class judges emit
    # chain-of-thought inside <think>…</think> ahead of the patched file.
    # The reasoning text often contains its own fenced sample blocks; without
    # stripping, the fence regex below would bleed the reasoning text into
    # the returned code and ast.parse() would reject it on the leading "<".
    text = strip_reasoning_blocks(text or "").strip()
    match = re.match(r"^```(?:python)?\n?(.*?)```\s*$", text, re.DOTALL)
    if match:
        return match.group(1).strip()
    text = re.sub(r"^```\w*\n?", "", text)
    text = re.sub(r"\n?```\s*$", "", text)
    return text.strip()


def _is_valid_python(code: str) -> bool:
    try:
        ast.parse(code)
        return True
    except SyntaxError:
        return False


def _call_llm(llm: Any, prompt: str) -> Optional[str]:
    """
    Call *llm* with *prompt*.  Returns None when the response is empty/None.
    Guards against str(None) == "None" which passes ast.parse() and would
    overwrite source files with the literal text "None".
    """
    try:
        if hasattr(llm, "invoke"):
            response = llm.invoke(prompt)
            if hasattr(response, "content"):
                content = response.content
                return str(content) if content else None
            return str(response) if response else None
        if hasattr(llm, "complete"):
            result = llm.complete(prompt)
            return str(result) if result else None
        if callable(llm):
            result = llm(prompt)
            return str(result) if result else None
    except Exception:
        pass
    return None


def _build_patch_prompt(
    library: str,
    deprecated_api: str,
    recommended_api: str,
    suggestion: str,
    rel_filepath: str,
    source_code: str,
) -> str:
    return (
        f"You are a Python migration assistant.\n\n"
        f"Replace ALL uses of the deprecated API in the file below.\n\n"
        f"Library        : {library}\n"
        f"Deprecated API : {deprecated_api}\n"
        f"Replacement API: {recommended_api}\n"
        f"Migration hint : {suggestion or 'See library documentation.'}\n\n"
        f"File: {rel_filepath}\n\n"
        f"```python\n{source_code[:5000]}\n```\n\n"
        f"Return ONLY the complete patched Python file.  "
        f"Do NOT include any explanation or markdown — just the raw Python source."
    )


# ── Main entry point ──────────────────────────────────────────────────────────

def run_api_version_autopatch(
    run_dir: str,
    llm: Any,
    *,
    dry_run: bool = False,
) -> AutoPatchReport:
    """
    Read ApiVersionReport issues from *run_dir* and auto-patch deprecated API
    usage in the generated code.

    Args:
        run_dir:  Path to a completed run output directory.
        llm:      LLM object used to generate replacement code.
        dry_run:  When True, generate patches but do NOT write files.

    Returns:
        AutoPatchReport summarising what was (or would be) patched.
    """
    code_dir = os.path.join(run_dir, "code")
    if not os.path.isdir(code_dir):
        return AutoPatchReport(
            success=False,
            patches_attempted=0,
            patches_applied=0,
            patches_failed=0,
            errors=["No code/ directory found in run output."],
        )

    issues = _load_api_version_issues(run_dir)
    if not issues:
        # Nothing to patch
        return AutoPatchReport(
            success=True,
            patches_attempted=0,
            patches_applied=0,
            patches_failed=0,
        )

    py_files = _collect_python_files(code_dir)
    results: List[AutoPatchResult] = []
    global_errors: List[str] = []

    for issue in issues:
        library = str(issue.get("library") or "")
        deprecated_api = str(issue.get("deprecated_api") or "").strip()
        recommended_api = str(issue.get("recommended_api") or "").strip()
        suggestion = str(issue.get("suggestion") or "")
        severity = str(issue.get("severity") or "low").lower()

        if not deprecated_api or not recommended_api:
            continue
        if severity not in _PATCHABLE_SEVERITIES:
            continue  # Skip LOW / INFO issues

        for filepath in py_files:
            try:
                with open(filepath, "r", encoding="utf-8", errors="replace") as fh:
                    source = fh.read()
            except OSError as exc:
                global_errors.append(f"Read error {filepath}: {exc}")
                continue

            if not _find_symbol_in_source(source, deprecated_api):
                continue  # Symbol not present – skip this file

            rel_path = os.path.relpath(filepath, code_dir)
            prompt = _build_patch_prompt(
                library, deprecated_api, recommended_api,
                suggestion, rel_path, source,
            )

            raw = _call_llm(llm, prompt)
            if not raw or not raw.strip():
                results.append(AutoPatchResult(
                    success=False,
                    library=library,
                    deprecated_api=deprecated_api,
                    recommended_api=recommended_api,
                    file_patched=rel_path,
                    applied=False,
                    error="LLM returned empty response.",
                ))
                continue

            patched = _strip_code_fences(raw)

            if not _is_valid_python(patched):
                results.append(AutoPatchResult(
                    success=False,
                    library=library,
                    deprecated_api=deprecated_api,
                    recommended_api=recommended_api,
                    file_patched=rel_path,
                    applied=False,
                    error="Patched code has syntax errors — skipped.",
                ))
                continue

            # Skip the write if the LLM returned the same code — avoids
            # unnecessary I/O and misleading "applied" counts.
            if patched == source:
                results.append(AutoPatchResult(
                    success=True,
                    library=library,
                    deprecated_api=deprecated_api,
                    recommended_api=recommended_api,
                    file_patched=rel_path,
                    applied=False,
                    error="LLM returned unchanged code — no patch needed.",
                ))
                continue

            if not dry_run:
                try:
                    from .._atomic_io import atomic_write_text
                except ImportError:  # flat-launcher mode
                    from _atomic_io import atomic_write_text  # type: ignore[no-redef]
                try:
                    # v1.1.11: shared atomic writer (parent-dir fsync). This
                    # rewrites the operator's actual source file, so durability
                    # matters (CLAUDE.md §13.1).
                    atomic_write_text(filepath, patched)
                except Exception as exc:
                    results.append(AutoPatchResult(
                        success=False,
                        library=library,
                        deprecated_api=deprecated_api,
                        recommended_api=recommended_api,
                        file_patched=rel_path,
                        applied=False,
                        error=f"Write error: {exc}",
                    ))
                    continue

            results.append(AutoPatchResult(
                success=True,
                library=library,
                deprecated_api=deprecated_api,
                recommended_api=recommended_api,
                file_patched=rel_path,
                applied=not dry_run,
            ))

    attempted = len(results)
    applied = sum(1 for r in results if r.applied)
    failed = sum(1 for r in results if not r.success)

    report = AutoPatchReport(
        success=failed == 0,
        patches_attempted=attempted,
        patches_applied=applied,
        patches_failed=failed,
        results=results,
        errors=global_errors,
    )

    report_path = os.path.join(run_dir, "api_autopatch_report.json")
    try:
        from .._atomic_io import atomic_write_text
    except ImportError:  # flat-launcher mode
        from _atomic_io import atomic_write_text  # type: ignore[no-redef]
    try:
        atomic_write_text(
            report_path,
            json.dumps(report.to_dict(), ensure_ascii=False, indent=2),
        )
    except OSError:
        pass

    return report
