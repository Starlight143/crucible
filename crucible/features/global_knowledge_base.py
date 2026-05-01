from __future__ import annotations
"""features/global_knowledge_base.py
=====================================
Cross-run persistent knowledge accumulation.

Reads analysis_result.json from the current run, extracts structured insights
(market, technical, risk, codegen), appends them to a global JSONL ledger at
{workspace_root}/global_knowledge.jsonl, then retrieves the N most relevant
past entries for the current run's domain.

workspace_root is determined by walking up from run_dir until finding a
directory that is not named "saved_projects" (i.e. the repo root).

JSONL entry schema:
  timestamp, run_dir, domain, insight_type (market|technical|risk|codegen),
  content, score, tags, user_problem_snippet

Outputs
-------
  {run_dir}/knowledge_base_report.json
    entries_added, entries_retrieved, top_relevant_entries

Env vars (all optional)
-----------------------
  GLOBAL_KB_ENABLED              default 1
  GLOBAL_KB_MAX_ENTRIES_PER_RUN  default 10
  GLOBAL_KB_MAX_RETRIEVE         default 5
  GLOBAL_KB_MIN_SCORE            default 60
"""  # noqa: E501

import json
import os
import re
import threading
import time
from datetime import datetime, timezone
from typing import Any, Dict, Generator, List, Optional

from crucible.feature_registry import (
    BaseFeature,
    FeatureConfig,
    FeatureResult,
    register,
)


# ---------------------------------------------------------------------------
# Module-level lock for concurrent JSONL append safety
# ---------------------------------------------------------------------------

_KB_APPEND_LOCK: threading.Lock = threading.Lock()

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _load_json_safe(path: str) -> Dict[str, Any]:
    if not os.path.isfile(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _write_json_safe(path: str, data: Any) -> None:
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, ensure_ascii=False)
        os.replace(tmp, path)
    except OSError as exc:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise RuntimeError(
            f"global_knowledge_base: cannot write {path!r}: {exc}"
        ) from exc


def _find_workspace_root(run_dir: str) -> str:
    """Walk up from run_dir to find the workspace root (not saved_projects).

    Logic:
    - If run_dir is inside saved_projects/, the workspace root is one level above.
    - Otherwise the parent of run_dir is used.
    - Safety: stop if we reach the filesystem root.
    """
    p = os.path.normpath(run_dir)
    for _ in range(10):  # max 10 levels up
        parent = os.path.dirname(p)
        if parent == p:  # reached filesystem root
            break
        if os.path.basename(p) == "saved_projects":
            return parent
        if os.path.basename(parent) == "saved_projects":
            return os.path.dirname(parent)
        p = parent
    # Fallback: use parent of run_dir
    return os.path.dirname(os.path.normpath(run_dir))


def _iter_jsonl(
    path: str,
) -> Generator[Dict[str, Any], None, None]:
    """Yield each valid JSON object from a JSONL file."""
    if not os.path.isfile(path):
        return
    try:
        with open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                    if isinstance(obj, dict):
                        yield obj
                except json.JSONDecodeError:
                    pass
    except OSError:
        pass


def _append_jsonl(path: str, entries: List[Dict[str, Any]]) -> None:
    """Append *entries* to the JSONL file at *path*, creating it if needed."""
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    except OSError:
        pass
    try:
        with _KB_APPEND_LOCK:
            with open(path, "a", encoding="utf-8") as fh:
                for entry in entries:
                    fh.write(json.dumps(entry, ensure_ascii=False) + chr(10))
    except OSError as exc:
        raise RuntimeError(
            f"global_knowledge_base: cannot append to {path!r}: {exc}"
        ) from exc


# ---------------------------------------------------------------------------
# Insight extraction
# ---------------------------------------------------------------------------

_INSIGHT_FIELD_MAP: Dict[str, str] = {
    "market": "market_insights",
    "technical": "technical_insights",
    "risk": "risk_summary",
    "codegen": "codegen_scope",
}

_KEYWORDS: Dict[str, List[str]] = {
    "market": ["market", "price", "volume", "trend", "momentum", "alpha", "signal"],
    "technical": ["model", "backtest", "strategy", "algorithm", "sharpe", "return"],
    "risk": ["risk", "drawdown", "volatility", "exposure", "loss", "margin"],
    "codegen": ["code", "implement", "deploy", "function", "class", "module"],
}


def _detect_domain(analysis: Dict[str, Any], snapshot: Dict[str, Any]) -> str:
    """Infer the run's domain from analysis and snapshot fields."""
    user_problem = str(
        snapshot.get("user_problem") or snapshot.get("query") or ""
    ).lower()
    project_name = str(analysis.get("project_name") or "").lower()
    combined = user_problem + " " + project_name
    scores: Dict[str, int] = {}
    for domain, keywords in _KEYWORDS.items():
        scores[domain] = sum(1 for kw in keywords if kw in combined)
    best = max(scores, key=lambda d: scores[d])
    return best if scores[best] > 0 else "general"


def _extract_insights(
    analysis: Dict[str, Any],
    snapshot: Dict[str, Any],
    run_dir: str,
    run_score: float,
    max_entries: int,
) -> List[Dict[str, Any]]:
    """Extract up to *max_entries* structured insight records from the analysis."""
    domain = _detect_domain(analysis, snapshot)
    user_problem_snippet = str(
        snapshot.get("user_problem") or snapshot.get("query") or ""
    )[:200]
    now = datetime.now(timezone.utc).isoformat()

    entries: List[Dict[str, Any]] = []
    insight_type_to_content: List[tuple] = []

    # consensus -> market insight
    consensus = str(analysis.get("consensus") or "").strip()
    if consensus:
        insight_type_to_content.append(("market", consensus[:500]))

    # technical_insights field
    tech = str(analysis.get("technical_insights") or "").strip()
    if tech:
        insight_type_to_content.append(("technical", tech[:500]))

    # risk_summary / blocking_risks -> risk insight
    risk_raw = analysis.get("risk_summary") or analysis.get("blocking_risks")
    if isinstance(risk_raw, list):
        risk_text = "; ".join(str(r) for r in risk_raw[:5])
    else:
        risk_text = str(risk_raw or "").strip()
    if risk_text:
        insight_type_to_content.append(("risk", risk_text[:500]))

    # codegen_scope -> codegen insight
    codegen = str(analysis.get("codegen_scope") or "").strip()
    if codegen:
        insight_type_to_content.append(("codegen", codegen[:500]))

    # experiments -> technical insights
    experiments = analysis.get("experiments") or []
    if isinstance(experiments, list) and experiments:
        exp_text = "; ".join(str(e) for e in experiments[:3])
        insight_type_to_content.append(("technical", f"Experiments: {exp_text}"[:500]))

    # Build structured entries (deduplicate by type, cap at max_entries)
    seen_types: set = set()
    for insight_type, content in insight_type_to_content:
        if len(entries) >= max_entries:
            break
        key = (insight_type, content[:50])
        if key in seen_types:
            continue
        seen_types.add(key)
        tags = [domain, insight_type]
        if analysis.get("risk_level"):
            tags.append(str(analysis["risk_level"]))
        entries.append({
            "timestamp": now,
            "run_dir": run_dir,
            "domain": domain,
            "insight_type": insight_type,
            "content": content,
            "score": run_score,
            "tags": tags,
            "user_problem_snippet": user_problem_snippet,
        })
    return entries


# ---------------------------------------------------------------------------
# Retrieval
# ---------------------------------------------------------------------------


def _tokenize(text: str) -> List[str]:
    """Lowercase and split on non-alphanumeric characters."""
    return [t for t in re.split(r"[^a-z0-9]+", text.lower()) if t]


def _retrieve_relevant(
    jsonl_path: str,
    user_problem: str,
    domain: str,
    max_retrieve: int,
    exclude_run_dir: str,
) -> List[Dict[str, Any]]:
    """Retrieve the most relevant entries from the JSONL knowledge base.

    Scoring: token overlap between user_problem and entry content + domain bonus.
    """
    query_tokens = set(_tokenize(user_problem)) if user_problem else set()
    scored: List[tuple] = []
    for entry in _iter_jsonl(jsonl_path):
        if entry.get("run_dir") == exclude_run_dir:
            continue
        content_tokens = set(_tokenize(entry.get("content", "")))
        if not content_tokens:
            continue
        overlap = len(query_tokens & content_tokens) if query_tokens else 0
        domain_bonus = 2 if entry.get("domain") == domain else 0
        score = overlap + domain_bonus
        scored.append((score, entry))
    scored.sort(key=lambda x: x[0], reverse=True)
    return [e for _, e in scored[:max_retrieve]]


def run_global_knowledge_base(run_dir: str) -> Dict[str, Any]:
    """Extract insights from run_dir, append to global JSONL, retrieve relevant entries.

    Returns a summary dict.  Raises RuntimeError on write failure.
    """
    try:
        max_entries = int(os.environ.get("GLOBAL_KB_MAX_ENTRIES_PER_RUN", "10"))
    except ValueError:
        max_entries = 10
    try:
        max_retrieve = int(os.environ.get("GLOBAL_KB_MAX_RETRIEVE", "5"))
    except ValueError:
        max_retrieve = 5
    try:
        min_score = float(os.environ.get("GLOBAL_KB_MIN_SCORE", "60"))
    except ValueError:
        min_score = 60.0

    analysis = _load_json_safe(os.path.join(run_dir, "analysis_result.json"))
    snapshot = _load_json_safe(os.path.join(run_dir, "run_snapshot.json"))

    try:
        _raw_score = next(
            (analysis[k] for k in ("score", "final_score", "consensus_score")
             if analysis.get(k) is not None),
            0,
        )
        run_score = float(_raw_score)
    except (TypeError, ValueError):
        run_score = 0.0

    workspace_root = _find_workspace_root(run_dir)
    jsonl_path = os.path.join(workspace_root, "global_knowledge.jsonl")
    user_problem = str(
        snapshot.get("user_problem") or snapshot.get("query") or ""
    ).strip()
    domain = _detect_domain(analysis, snapshot)

    # Retrieve relevant entries BEFORE appending (so we don't retrieve own entries)
    retrieved = _retrieve_relevant(
        jsonl_path, user_problem, domain, max_retrieve, run_dir
    )

    # Append new entries only if run_score >= min_score
    entries_added = 0
    if run_score >= min_score:
        new_entries = _extract_insights(
            analysis, snapshot, run_dir, run_score, max_entries
        )
        if new_entries:
            _append_jsonl(jsonl_path, new_entries)
            entries_added = len(new_entries)

    report: Dict[str, Any] = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "run_dir": run_dir,
        "workspace_root": workspace_root,
        "jsonl_path": jsonl_path,
        "run_score": run_score,
        "min_score_for_storage": min_score,
        "domain": domain,
        "entries_added": entries_added,
        "entries_retrieved": len(retrieved),
        "top_relevant_entries": retrieved[:max_retrieve],
    }
    _write_json_safe(os.path.join(run_dir, "knowledge_base_report.json"), report)
    return report


@register("global_knowledge_base")
class GlobalKnowledgeBaseFeature(BaseFeature):
    """Post-processing feature: global_knowledge_base.

    Extracts insights from the current run, appends them to a global JSONL
    knowledge ledger, and retrieves the most relevant past entries.
    """

    name = "global_knowledge_base"
    label = "Global Knowledge Base"
    requires: list[str] = []

    def run(self, run_dir: str, config: FeatureConfig) -> FeatureResult:
        """Execute knowledge base update for run_dir."""
        if os.environ.get("GLOBAL_KB_ENABLED", "1").strip().lower() in ("0", "false", "no", "off"):
            return FeatureResult(
                feature=self.name, success=True, skipped=True,
                skip_reason="GLOBAL_KB_ENABLED is not 1.",
            )
        t0 = time.monotonic()
        try:
            report = run_global_knowledge_base(run_dir)
        except Exception as exc:
            return FeatureResult(
                feature=self.name, success=False,
                summary=f"global_knowledge_base failed: {exc}",
                duration_seconds=time.monotonic() - t0, error=str(exc),
            )
        duration = time.monotonic() - t0
        added = report.get("entries_added", 0)
        retrieved = report.get("entries_retrieved", 0)
        return FeatureResult(
            feature=self.name, success=True,
            summary=(
                f"{added} insight(s) added to KB; "
                f"{retrieved} relevant entry(ies) retrieved."
            ),
            duration_seconds=duration,
            details={
                "entries_added": added,
                "entries_retrieved": retrieved,
                "domain": report.get("domain"),
                "jsonl_path": report.get("jsonl_path"),
            },
        )
