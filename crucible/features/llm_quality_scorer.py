from __future__ import annotations
"""features/llm_quality_scorer.py
==================================
Automated multi-dimension quality scoring of LLM agent outputs.

Evaluates the analysis_result.json from a completed pipeline run on five
heuristic dimensions (each 0-20 points, total 0-100):

  1. Completeness  (20): required fields present and non-empty
                         (consensus, experiments, risk_level,
                          gate_decision, blocking_risks)
  2. Specificity   (20): specific numbers / dates / metrics in the text
  3. Risk Coverage (20): multiple risk categories mentioned
                         (market, technical, regulatory, operational)
  4. Actionability (20): concrete action verbs present
                         (implement, test, deploy, measure)
  5. Coherence     (20): gate_decision consistent with score / risk_level

No LLM calls are made; scoring is deterministic and fast.

Outputs
-------
  {run_dir}/llm_quality_score.json
    per-dimension scores, total, discrepancy flag

Env vars (all optional)
-----------------------
  LLM_QUALITY_SCORER_ENABLED   default 1
  LLM_QUALITY_SCORER_MIN_PASS  default 60 (below this adds WARNING to summary)
"""  # noqa: E501

import json
import os
import re
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
# Constants
# ---------------------------------------------------------------------------

_REQUIRED_FIELDS: List[str] = [
    "consensus", "experiments", "risk_level", "gate_decision", "blocking_risks",
]

_RISK_CATEGORIES: List[str] = ["market", "technical", "regulatory", "operational"]

_ACTION_VERBS: List[str] = [
    "implement", "test", "deploy", "measure", "validate", "build", "evaluate",
    "run", "execute", "monitor", "optimise", "optimize", "refactor",
]

# gate_decision -> acceptable risk levels
_GATE_RISK_COMPAT: Dict[str, List[str]] = {
    "GO": ["low", "medium"],
    "CONDITIONAL": ["medium", "high"],
    "NO-GO": ["high", "critical", "very high"],
}

# Numeric pattern: integer, float, percentage, or range
_NUMERIC_RE = re.compile(r"(?:\d+\.?\d*%|\d{4}-\d{2}-\d{2}|\d+\.\d+|\d{2,}%)")


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
    _tmp = path + ".tmp"
    try:
        with open(_tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, ensure_ascii=False)
        os.replace(_tmp, path)
    except OSError as exc:
        try:
            os.unlink(_tmp)
        except OSError:
            pass
        raise RuntimeError(f"llm_quality_scorer: cannot write {path!r}: {exc}") from exc


def _to_text(value: Any) -> str:
    """Convert a field value (str, list, or other) to a single flat text string."""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return " ".join(str(v) for v in value)
    return str(value) if value is not None else ""


# ---------------------------------------------------------------------------
# Scoring functions
# ---------------------------------------------------------------------------


def _score_completeness(analysis: Dict[str, Any]) -> Tuple[int, str]:
    """Score 0-20: required fields present and non-empty."""
    missing = []
    for field in _REQUIRED_FIELDS:
        val = analysis.get(field)
        if val is None:
            missing.append(field)
        elif isinstance(val, (str, list)) and not val:
            missing.append(field)
    present = len(_REQUIRED_FIELDS) - len(missing)
    score = round(present / len(_REQUIRED_FIELDS) * 20)
    detail = (
        f"{present}/{len(_REQUIRED_FIELDS)} required fields present."
        + (f" Missing: {missing}" if missing else "")
    )
    return score, detail


def _score_specificity(analysis: Dict[str, Any]) -> Tuple[int, str]:
    """Score 0-20: analysis contains specific numbers/dates/metrics."""
    full_text = " ".join(
        _to_text(v) for v in analysis.values() if isinstance(v, (str, list))
    )
    matches = _NUMERIC_RE.findall(full_text)
    count = len(matches)
    # 0 numbers -> 0, 1-2 -> 8, 3-5 -> 14, 6-9 -> 18, 10+ -> 20
    if count == 0:
        score, label = 0, "none"
    elif count <= 2:
        score, label = 8, "few"
    elif count <= 5:
        score, label = 14, "moderate"
    elif count <= 9:
        score, label = 18, "good"
    else:
        score, label = 20, "high"
    return score, f"{count} numeric pattern(s) found ({label} specificity)."


def _score_risk_coverage(analysis: Dict[str, Any]) -> Tuple[int, str]:
    """Score 0-20: multiple risk categories mentioned."""
    full_text = " ".join(
        _to_text(v).lower() for v in analysis.values() if isinstance(v, (str, list))
    )
    covered = [cat for cat in _RISK_CATEGORIES if cat in full_text]
    score = round(len(covered) / len(_RISK_CATEGORIES) * 20)
    return (
        score,
        f"{len(covered)}/{len(_RISK_CATEGORIES)} risk categories covered: {covered}.",
    )


def _score_actionability(analysis: Dict[str, Any]) -> Tuple[int, str]:
    """Score 0-20: concrete action verbs present in experiments / next steps."""
    experiments_text = _to_text(analysis.get("experiments") or []).lower()
    consensus_text = _to_text(analysis.get("consensus") or "").lower()
    combined = experiments_text + " " + consensus_text
    found_verbs = [v for v in _ACTION_VERBS if v in combined]
    # 0 verbs -> 0, 1-2 -> 8, 3-5 -> 14, 6+ -> 20
    n = len(found_verbs)
    if n == 0:
        score = 0
    elif n <= 2:
        score = 8
    elif n <= 5:
        score = 14
    else:
        score = 20
    return score, f"{n} action verb(s) found: {found_verbs[:6]}."


def _score_coherence(analysis: Dict[str, Any]) -> Tuple[int, str]:
    """Score 0-20: gate_decision consistent with score and risk_level."""
    gate = str(analysis.get("gate_decision") or "").upper().strip()
    risk_level = str(analysis.get("risk_level") or "").lower().strip()
    run_score: Optional[float] = None
    try:
        raw = analysis.get("score")
        if raw is not None:
            run_score = float(raw)
    except (TypeError, ValueError):
        pass

    if not gate:
        return 0, "gate_decision is missing; coherence cannot be evaluated."

    # Check risk_level compatibility
    compatible_risks = _GATE_RISK_COMPAT.get(gate, [])
    risk_ok = (not risk_level) or any(r in risk_level for r in compatible_risks)

    # Check score vs gate heuristic
    score_ok = True
    if run_score is not None:
        if gate == "GO" and run_score < 60:
            score_ok = False
        elif gate == "NO-GO" and run_score > 80:
            score_ok = False

    if risk_ok and score_ok:
        score, detail = 20, f"gate={gate!r} is coherent with risk={risk_level!r} and score={run_score}."
    elif risk_ok or score_ok:
        score, detail = 10, (
            f"gate={gate!r} partially coherent: risk_ok={risk_ok}, score_ok={score_ok}."
        )
    else:
        score, detail = 0, (
            f"gate={gate!r} is incoherent: risk_level={risk_level!r}, score={run_score}."
        )
    return score, detail


# ---------------------------------------------------------------------------
# Main scoring function
# ---------------------------------------------------------------------------


def score_analysis(
    analysis: Dict[str, Any],
    min_pass: int = 60,
) -> Dict[str, Any]:
    """Compute quality scores for *analysis* on all 5 dimensions.

    Returns a dict suitable for writing to llm_quality_score.json.
    Discrepancies > 20 points between heuristic total and the analysis's own
    score field are flagged.
    """
    c_score, c_detail = _score_completeness(analysis)
    s_score, s_detail = _score_specificity(analysis)
    r_score, r_detail = _score_risk_coverage(analysis)
    a_score, a_detail = _score_actionability(analysis)
    h_score, h_detail = _score_coherence(analysis)

    total = c_score + s_score + r_score + a_score + h_score

    # Compare against existing analysis score
    existing_score: Optional[float] = None
    discrepancy_flag = False
    try:
        raw = analysis.get("score")
        if raw is not None:
            existing_score = float(raw)
            if abs(total - existing_score) > 20:
                discrepancy_flag = True
    except (TypeError, ValueError):
        pass

    warnings: List[str] = []
    if total < min_pass:
        warnings.append(
            f"WARNING: quality total {total} is below min_pass threshold {min_pass}."
        )
    if discrepancy_flag:
        warnings.append(
            f"WARNING: heuristic score {total} differs from analysis score "
            f"{existing_score:.0f} by more than 20 points."
        )

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "total": total,
        "pass": total >= min_pass,
        "min_pass_threshold": min_pass,
        "existing_analysis_score": existing_score,
        "discrepancy_flagged": discrepancy_flag,
        "warnings": warnings,
        "dimensions": {
            "completeness": {"score": c_score, "max": 20, "detail": c_detail},
            "specificity": {"score": s_score, "max": 20, "detail": s_detail},
            "risk_coverage": {"score": r_score, "max": 20, "detail": r_detail},
            "actionability": {"score": a_score, "max": 20, "detail": a_detail},
            "coherence": {"score": h_score, "max": 20, "detail": h_detail},
        },
    }


def run_llm_quality_scorer(run_dir: str) -> Dict[str, Any]:
    """Score the LLM output in run_dir and write llm_quality_score.json.

    Returns the score dict.  Raises RuntimeError on write failure.
    """
    try:
        min_pass = int(os.environ.get("LLM_QUALITY_SCORER_MIN_PASS", "60"))
    except ValueError:
        min_pass = 60

    analysis = _load_json_safe(os.path.join(run_dir, "analysis_result.json"))
    result = score_analysis(analysis, min_pass=min_pass)
    result["run_dir"] = run_dir
    _write_json_safe(os.path.join(run_dir, "llm_quality_score.json"), result)
    return result


@register("llm_quality_scorer")
class LlmQualityScorerFeature(BaseFeature):
    """Post-processing feature: llm_quality_scorer.

    Evaluates analysis_result.json on 5 heuristic dimensions and writes
    llm_quality_score.json to run_dir.  No LLM calls are made.
    """

    name = "llm_quality_scorer"
    label = "LLM Quality Scorer"
    requires: list[str] = []

    def run(self, run_dir: str, config: FeatureConfig) -> FeatureResult:
        """Execute quality scoring for run_dir."""
        _enabled_raw = os.environ.get("LLM_QUALITY_SCORER_ENABLED", "1").strip().lower()
        if _enabled_raw in ("0", "false", "no", "off"):
            return FeatureResult(
                feature=self.name, success=True, skipped=True,
                skip_reason="LLM_QUALITY_SCORER_ENABLED is disabled.",
            )
        t0 = time.monotonic()
        try:
            result = run_llm_quality_scorer(run_dir)
        except Exception as exc:
            return FeatureResult(
                feature=self.name, success=False,
                summary=f"llm_quality_scorer failed: {exc}",
                duration_seconds=time.monotonic() - t0, error=str(exc),
            )
        duration = time.monotonic() - t0
        total = result.get("total", 0)
        passed = result.get("pass", False)
        warnings = result.get("warnings", [])
        discrepancy = result.get("discrepancy_flagged", False)
        pass_label = "PASS" if passed else "FAIL"
        summary_parts = [f"Quality score: {total}/100 ({pass_label})."]
        if discrepancy:
            summary_parts.append("Score discrepancy >20 pts flagged.")
        if warnings:
            summary_parts.append(warnings[0])
        return FeatureResult(
            feature=self.name, success=True,
            summary=" ".join(summary_parts),
            duration_seconds=duration,
            details={
                "total": total,
                "pass": passed,
                "discrepancy_flagged": discrepancy,
                "warnings": warnings,
                "dimensions": result.get("dimensions", {}),
            },
        )
