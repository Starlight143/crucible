from __future__ import annotations
"""features/few_shot_injector.py
================================
Few-shot example injector.

Scans saved_projects/ for historical runs with a quality score >= min_score,
extracts golden examples (consensus, codegen_scope, key experiments), ranks
them by cosine similarity to the current run's user_problem, and writes the
top N examples to two output files:

  few_shot_examples.json   structured JSON list of golden examples
  few_shot_context.txt     formatted text for prompt injection via
                           PIPELINE_INTERACTIVE_CONTEXT

Env vars (all optional)
-----------------------
  FEW_SHOT_ENABLED       default 1
  FEW_SHOT_MIN_SCORE     default 75  (minimum run score to qualify as golden)
  FEW_SHOT_MAX_EXAMPLES  default 3   (max examples to inject)
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
# Similarity helpers (same pattern as semantic_cache)
# ---------------------------------------------------------------------------


def _jaccard_similarity(a: str, b: str, ngram: int = 3) -> float:
    """Character n-gram Jaccard similarity."""
    if not a or not b:
        return 0.0
    if min(len(a), len(b)) < ngram:
        sa = set(a.lower().split())
        sb = set(b.lower().split())
    else:
        sa = {a[i:i + ngram].lower() for i in range(len(a) - ngram + 1)}
        sb = {b[i:i + ngram].lower() for i in range(len(b) - ngram + 1)}
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / len(sa | sb)


def _cosine_similarities(query: str, documents: List[str]) -> List[float]:
    """TF-IDF cosine similarity of *query* vs each document; Jaccard fallback."""
    if not documents:
        return []
    try:
        from sklearn.feature_extraction.text import TfidfVectorizer  # type: ignore
        from sklearn.metrics.pairwise import cosine_similarity  # type: ignore

        corpus = [query] + documents
        vec = TfidfVectorizer(analyzer="word", ngram_range=(1, 2), min_df=1)
        tfidf = vec.fit_transform(corpus)
        sims = cosine_similarity(tfidf[0:1], tfidf[1:]).flatten()
        return [float(s) for s in sims]
    except (ImportError, ValueError):
        return [_jaccard_similarity(query, d) for d in documents]


# ---------------------------------------------------------------------------
# I/O helpers
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
        raise RuntimeError(f"few_shot_injector: cannot write {path!r}: {exc}") from exc


# ---------------------------------------------------------------------------
# Golden example extraction
# ---------------------------------------------------------------------------


def _extract_golden_example(
    run_dir: str, analysis: Dict[str, Any], snapshot: Dict[str, Any]
) -> Optional[Dict[str, Any]]:
    """Extract a structured golden example from a high-scoring run.

    Returns None if the minimum required fields are absent.
    """
    user_problem = str(
        snapshot.get("user_problem") or snapshot.get("query") or ""
    ).strip()
    consensus = str(analysis.get("consensus") or "").strip()
    if not user_problem or not consensus:
        return None

    # Extract key experiments (first 3)
    raw_experiments = analysis.get("experiments") or []
    if isinstance(raw_experiments, list):
        key_experiments = [
            str(e) for e in raw_experiments[:3] if e
        ]
    else:
        key_experiments = []

    return {
        "run_dir": run_dir,
        "user_problem": user_problem[:400],
        "score": analysis.get("score"),
        "consensus": consensus[:800],
        "codegen_scope": str(analysis.get("codegen_scope") or "")[:400],
        "key_experiments": key_experiments,
        "risk_level": analysis.get("risk_level"),
        "gate_decision": analysis.get("gate_decision"),
    }


def _format_example_for_prompt(idx: int, example: Dict[str, Any]) -> str:
    """Format one golden example as a human-readable block for prompt injection."""
    problem = example.get("user_problem", "")
    score = example.get("score")
    risk = example.get("risk_level", "")
    gate = example.get("gate_decision", "")
    consensus = example.get("consensus", "")
    codegen = example.get("codegen_scope", "")
    key_experiments = example.get("key_experiments") or []
    lines = []
    lines.append(f"### Few-Shot Example {idx + 1}")
    lines.append(f"Problem: {problem}")
    lines.append(f"Score: {score}  Risk: {risk}  Gate: {gate}")
    lines.append(f"Consensus: {consensus}")
    if codegen:
        lines.append(f"Codegen scope: {codegen}")
    if key_experiments:
        lines.append("Key experiments:")
        for exp in key_experiments:
            lines.append(f"  - {exp}")
    lines.append("")
    return chr(10).join(lines)


def run_few_shot_injector(run_dir: str) -> Dict[str, Any]:
    """Gather golden examples and write few_shot_examples.json + few_shot_context.txt.

    Returns a summary dict.
    """
    try:
        min_score = float(os.environ.get("FEW_SHOT_MIN_SCORE", "75"))
    except ValueError:
        min_score = 75.0
    try:
        max_examples = int(os.environ.get("FEW_SHOT_MAX_EXAMPLES", "3"))
    except ValueError:
        max_examples = 3

    # Read current run snapshot
    snap = _load_json_safe(os.path.join(run_dir, "run_snapshot.json"))
    user_problem = str(snap.get("user_problem") or snap.get("query") or "").strip()

    # Locate saved_projects
    parent = os.path.dirname(os.path.normpath(run_dir))
    if os.path.basename(parent) == "saved_projects":
        saved_dir = parent
    else:
        saved_dir = os.path.join(parent, "saved_projects")

    # Scan for qualifying golden runs
    golden_candidates: List[Tuple[str, Dict[str, Any]]] = []
    if os.path.isdir(saved_dir):
        try:
            entries = os.listdir(saved_dir)
        except OSError:
            entries = []
        exclude_name = os.path.basename(os.path.normpath(run_dir))
        for entry in entries:
            if entry == exclude_name:
                continue
            candidate_dir = os.path.join(saved_dir, entry)
            if not os.path.isdir(candidate_dir):
                continue
            analysis = _load_json_safe(
                os.path.join(candidate_dir, "analysis_result.json")
            )
            try:
                score = float(analysis.get("score") or 0)
            except (TypeError, ValueError):
                score = 0.0
            if score < min_score:
                continue
            candidate_snap = _load_json_safe(
                os.path.join(candidate_dir, "run_snapshot.json")
            )
            example = _extract_golden_example(candidate_dir, analysis, candidate_snap)
            if example is not None:
                golden_candidates.append((example["user_problem"], example))

    if not golden_candidates:
        result: Dict[str, Any] = {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "run_dir": run_dir,
            "examples_found": 0,
            "examples_injected": 0,
            "message": f"No qualifying runs found (min_score={min_score}).",
        }
        _write_json_safe(os.path.join(run_dir, "few_shot_examples.json"), [])
        _ctx_path = os.path.join(run_dir, "few_shot_context.txt")
        _ctx_tmp = _ctx_path + ".tmp"
        try:
            with open(_ctx_tmp, "w", encoding="utf-8") as fh:
                fh.write("No few-shot examples available." + chr(10))
            os.replace(_ctx_tmp, _ctx_path)
        except OSError:
            try:
                os.unlink(_ctx_tmp)
            except OSError:
                pass
        return result

    # Rank by similarity to current problem
    problems = [p for p, _ in golden_candidates]
    examples_list = [e for _, e in golden_candidates]

    if user_problem:
        scores = _cosine_similarities(user_problem, problems)
        ranked = sorted(zip(scores, examples_list), key=lambda x: x[0], reverse=True)
        top_examples = [ex for _, ex in ranked[:max_examples]]
    else:
        # No current problem -- sort by run score descending
        top_examples = sorted(
            examples_list,
            key=lambda e: float(e.get("score") or 0),
            reverse=True,
        )[:max_examples]

    # Write structured examples
    _write_json_safe(os.path.join(run_dir, "few_shot_examples.json"), top_examples)

    # Write formatted context text
    ctx_lines = ["=== Few-Shot Examples (inject before main analysis prompt) ===", ""]
    for idx, ex in enumerate(top_examples):
        ctx_lines.append(_format_example_for_prompt(idx, ex))
    ctx_lines.append("=== End Few-Shot Examples ===")
    ctx_text = chr(10).join(ctx_lines)
    _ctx_path = os.path.join(run_dir, "few_shot_context.txt")
    _ctx_tmp = _ctx_path + ".tmp"
    try:
        with open(_ctx_tmp, "w", encoding="utf-8") as fh:
            fh.write(ctx_text)
        os.replace(_ctx_tmp, _ctx_path)
    except OSError as exc:
        try:
            os.unlink(_ctx_tmp)
        except OSError:
            pass
        raise RuntimeError(f"few_shot_injector: cannot write few_shot_context.txt: {exc}") from exc

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "run_dir": run_dir,
        "examples_found": len(golden_candidates),
        "examples_injected": len(top_examples),
        "min_score": min_score,
        "max_examples": max_examples,
    }


@register("few_shot_injector")
class FewShotInjectorFeature(BaseFeature):
    """Post-processing feature: few_shot_injector.

    Finds high-scoring historical runs, extracts golden examples, and
    writes few_shot_examples.json + few_shot_context.txt to run_dir.
    """

    name = "few_shot_injector"
    label = "Few-Shot Example Injector"
    requires: list[str] = []

    def run(self, run_dir: str, config: FeatureConfig) -> FeatureResult:
        """Execute few-shot injection for run_dir."""
        if os.environ.get("FEW_SHOT_ENABLED", "1").strip().lower() in ("0", "false", "no", "off"):
            return FeatureResult(
                feature=self.name, success=True, skipped=True,
                skip_reason="FEW_SHOT_ENABLED is not 1.",
            )
        t0 = time.monotonic()
        try:
            result = run_few_shot_injector(run_dir)
        except Exception as exc:
            return FeatureResult(
                feature=self.name, success=False,
                summary=f"few_shot_injector failed: {exc}",
                duration_seconds=time.monotonic() - t0, error=str(exc),
            )
        duration = time.monotonic() - t0
        injected = result.get("examples_injected", 0)
        found = result.get("examples_found", 0)
        return FeatureResult(
            feature=self.name, success=True,
            summary=(
                f"{injected} example(s) injected from {found} qualifying runs."
            ),
            duration_seconds=duration,
            details={
                "examples_found": found,
                "examples_injected": injected,
            },
        )
