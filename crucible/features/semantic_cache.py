from __future__ import annotations
"""features/semantic_cache.py
============================
Semantic similarity-based research result cache.

Compares the current run's user_problem against the last N run snapshots in
saved_projects/ using TF-IDF cosine similarity (scikit-learn) or character-
level Jaccard similarity as a zero-dependency fallback.  The result is
advisory only -- the pipeline is never stopped, only warned.

Outputs
-------
  {run_dir}/semantic_cache_report.json
    similarity_score, matched_run_dir, cache_hit, recommendation_text

Env vars (all optional)
-----------------------
  SEMANTIC_CACHE_ENABLED     default 1
  SEMANTIC_CACHE_THRESHOLD   default 0.85  (similarity >= this => cache hit)
  SEMANTIC_CACHE_MAX_HISTORY default 50    (max past runs to compare)
"""  # noqa: E501

import json
import os
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from crucible.feature_registry import (
    BaseFeature,
    FeatureConfig,
    FeatureResult,
    register,
)


# ---------------------------------------------------------------------------
# Similarity helpers
# ---------------------------------------------------------------------------


def _jaccard_similarity(a: str, b: str, ngram: int = 3) -> float:
    """Character n-gram Jaccard similarity between strings *a* and *b*.

    Returns a float in [0.0, 1.0].  Uses trigrams by default (ngram=3).
    Falls back to simple set-of-words when min(len(a), len(b)) < ngram.
    """
    if not a or not b:
        return 0.0
    if min(len(a), len(b)) < ngram:
        set_a = set(a.lower().split())
        set_b = set(b.lower().split())
    else:
        set_a = {a[i:i + ngram].lower() for i in range(len(a) - ngram + 1)}
        set_b = {b[i:i + ngram].lower() for i in range(len(b) - ngram + 1)}
    if not set_a or not set_b:
        return 0.0
    return len(set_a & set_b) / len(set_a | set_b)


def _tfidf_cosine_similarity(query: str, documents: List[str]) -> List[float]:
    """Compute TF-IDF cosine similarity of *query* against each document in *documents*.

    Returns a list of floats in [0.0, 1.0], one per document.
    Falls back to Jaccard if scikit-learn is not installed.
    """
    try:
        from sklearn.feature_extraction.text import TfidfVectorizer  # type: ignore
        from sklearn.metrics.pairwise import cosine_similarity  # type: ignore
        # numpy is NOT imported here: cosine_similarity already returns an ndarray
        # and its .flatten() / float() conversion works without a numpy import.
        # Importing numpy separately caused an unnecessary ImportError fallback
        # to Jaccard when numpy was absent even though sklearn was installed.

        corpus = [query] + documents
        vec = TfidfVectorizer(analyzer="word", ngram_range=(1, 2), min_df=1)
        tfidf = vec.fit_transform(corpus)
        sims = cosine_similarity(tfidf[0:1], tfidf[1:]).flatten()
        return [float(s) for s in sims]
    except (ImportError, ValueError):
        # ImportError: sklearn not installed.
        # ValueError: empty vocabulary (e.g. all tokens filtered as stop-words).
        return [_jaccard_similarity(query, doc) for doc in documents]


# ---------------------------------------------------------------------------
# I/O helpers
# ---------------------------------------------------------------------------


def _load_json_safe(path: str) -> Dict[str, Any]:
    """Load *path* as JSON dict; return {} on any error."""
    if not os.path.isfile(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _write_json_safe(path: str, data: Any) -> None:
    """Write *data* as indented JSON; raise RuntimeError on failure."""
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
        raise RuntimeError(f"semantic_cache: cannot write {path!r}: {exc}") from exc


def _discover_run_snapshots(
    saved_dir: str, max_history: int, exclude_run_dir: str
) -> List[Tuple[str, str]]:
    """Scan *saved_dir* for run directories that have a run_snapshot.json.

    Returns a list of (run_dir, user_problem) tuples, newest first,
    capped at *max_history*, excluding *exclude_run_dir*.
    """
    if not os.path.isdir(saved_dir):
        return []
    results: List[Tuple[str, str, float]] = []
    try:
        entries = os.listdir(saved_dir)
    except OSError:
        return []
    exclude_name = os.path.basename(os.path.normpath(exclude_run_dir))
    for entry in entries:
        if entry == exclude_name:
            continue
        run_dir = os.path.join(saved_dir, entry)
        if not os.path.isdir(run_dir):
            continue
        snap_path = os.path.join(run_dir, "run_snapshot.json")
        if not os.path.isfile(snap_path):
            continue
        snap = _load_json_safe(snap_path)
        user_problem = str(snap.get("user_problem") or snap.get("query") or "").strip()
        if not user_problem:
            continue
        mtime = os.path.getmtime(run_dir)
        results.append((run_dir, user_problem, mtime))
    results.sort(key=lambda x: x[2], reverse=True)
    return [(rd, up) for rd, up, _ in results[:max_history]]


# ---------------------------------------------------------------------------
# Core logic
# ---------------------------------------------------------------------------


def run_semantic_cache(run_dir: str) -> Dict[str, Any]:
    """Compare current run against historical runs and write semantic_cache_report.json.

    Reads run_snapshot.json from *run_dir*, scans saved_projects/ for past runs,
    computes similarity, and writes the report.

    Returns the report dict.
    """
    try:
        threshold = float(os.environ.get("SEMANTIC_CACHE_THRESHOLD", "0.85"))
    except ValueError:
        threshold = 0.85
    try:
        max_history = int(os.environ.get("SEMANTIC_CACHE_MAX_HISTORY", "50"))
    except ValueError:
        max_history = 50

    # Read current run snapshot
    snap = _load_json_safe(os.path.join(run_dir, "run_snapshot.json"))
    user_problem = str(snap.get("user_problem") or snap.get("query") or "").strip()

    if not user_problem:
        report: Dict[str, Any] = {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "run_dir": run_dir,
            "skipped": True,
            "skip_reason": "run_snapshot.json missing or has no user_problem field.",
            "cache_hit": False,
            "similarity_score": None,
            "matched_run_dir": None,
            "recommendation_text": "Cannot check cache: no user_problem available.",
        }
        _write_json_safe(os.path.join(run_dir, "semantic_cache_report.json"), report)
        return report

    # Locate saved_projects dir
    parent = os.path.dirname(os.path.normpath(run_dir))
    if os.path.basename(parent) == "saved_projects":
        saved_dir = parent
    else:
        saved_dir = os.path.join(parent, "saved_projects")

    past_runs = _discover_run_snapshots(saved_dir, max_history, run_dir)

    if not past_runs:
        report = {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "run_dir": run_dir,
            "user_problem_snippet": user_problem[:200],
            "past_runs_checked": 0,
            "cache_hit": False,
            "similarity_score": None,
            "matched_run_dir": None,
            "threshold": threshold,
            "recommendation_text": "No historical runs found for comparison.",
        }
        _write_json_safe(os.path.join(run_dir, "semantic_cache_report.json"), report)
        return report

    past_dirs, past_problems = zip(*past_runs)
    scores = _tfidf_cosine_similarity(user_problem, list(past_problems))

    best_idx = max(range(len(scores)), key=lambda i: scores[i])
    best_score = scores[best_idx]
    best_run_dir = past_dirs[best_idx]
    cache_hit = best_score >= threshold

    if cache_hit:
        rec = (
            f"CACHE HIT (similarity={best_score:.3f} >= threshold={threshold:.2f}). "
            f"Consider reusing results from: {best_run_dir}"
        )
    else:
        rec = (
            f"No cache hit. Best similarity {best_score:.3f} < threshold {threshold:.2f}. "
            "Proceeding with fresh analysis is appropriate."
        )

    # Build top-5 similarity list for diagnostics
    top_matches = sorted(
        zip(past_dirs, scores), key=lambda x: x[1], reverse=True
    )[:5]

    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "run_dir": run_dir,
        "user_problem_snippet": user_problem[:200],
        "past_runs_checked": len(past_runs),
        "threshold": threshold,
        "cache_hit": cache_hit,
        "similarity_score": round(best_score, 4),
        "matched_run_dir": best_run_dir if cache_hit else None,
        "recommendation_text": rec,
        "top_matches": [
            {"run_dir": rd, "score": round(sc, 4)} for rd, sc in top_matches
        ],
    }
    _write_json_safe(os.path.join(run_dir, "semantic_cache_report.json"), report)
    return report


# ---------------------------------------------------------------------------
# Feature class
# ---------------------------------------------------------------------------


@register("semantic_cache")
class SemanticCacheFeature(BaseFeature):
    """Post-processing feature: semantic_cache.

    Compares the current run against historical runs using TF-IDF cosine
    similarity and reports a cache hit when a similar run is found.
    Advisory only -- does not stop or alter the pipeline.
    """

    name = "semantic_cache"
    label = "Semantic Cache Check"
    requires: list[str] = []

    def run(self, run_dir: str, config: FeatureConfig) -> FeatureResult:
        """Execute semantic cache check for run_dir."""
        if os.environ.get("SEMANTIC_CACHE_ENABLED", "1").strip().lower() in ("0", "false", "no", "off"):
            return FeatureResult(
                feature=self.name, success=True, skipped=True,
                skip_reason="SEMANTIC_CACHE_ENABLED is not 1.",
            )
        t0 = time.monotonic()
        try:
            report = run_semantic_cache(run_dir)
        except Exception as exc:
            return FeatureResult(
                feature=self.name, success=False,
                summary=f"semantic_cache failed: {exc}",
                duration_seconds=time.monotonic() - t0, error=str(exc),
            )
        duration = time.monotonic() - t0
        cache_hit = report.get("cache_hit", False)
        score = report.get("similarity_score")
        matched = report.get("matched_run_dir")
        summary_parts = []
        if report.get("skipped"):
            summary_parts.append(report.get("skip_reason", "skipped"))
        elif cache_hit:
            score_str = f"{score:.3f}" if score is not None else "N/A"
            summary_parts.append(f"CACHE HIT similarity={score_str}; matched={matched}")
        else:
            checked = report.get("past_runs_checked", 0)
            score_str = f"{score:.3f}" if score is not None else "N/A"
            summary_parts.append(
                f"No cache hit; best similarity={score_str} ({checked} runs checked)."
            )
        return FeatureResult(
            feature=self.name, success=True,
            summary=" ".join(summary_parts),
            duration_seconds=duration,
            details={
                "cache_hit": cache_hit,
                "similarity_score": score,
                "matched_run_dir": matched,
                "past_runs_checked": report.get("past_runs_checked", 0),
            },
        )
