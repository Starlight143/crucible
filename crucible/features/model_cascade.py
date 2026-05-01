from __future__ import annotations
"""features/model_cascade.py
==========================
Intelligent LLM model routing (model_cascade feature).

Reads analysis_result.json and run_meta.json from a completed pipeline run,
recommends the cheapest model tier per stage that still meets quality gates,
and estimates cost savings if the cascade were applied.

Model tiers:
  cheap     openai/gpt-4o-mini   simple summarisation / extraction
  mid       openai/gpt-4o        analysis / code review
  expensive openai/o1-preview    direction debate / final synthesis

Outputs
-------
  {run_dir}/model_cascade_report.json  per-stage recommendations
  {run_dir}/model_cascade_config.json  env-var suggestions

Env vars (all optional)
-----------------------
  MODEL_CASCADE_ENABLED              default 1
  MODEL_CASCADE_CHEAP_MODEL          default openai/gpt-4o-mini
  MODEL_CASCADE_MID_MODEL            default openai/gpt-4o
  MODEL_CASCADE_EXPENSIVE_MODEL      default openai/o1-preview
  MODEL_CASCADE_COST_THRESHOLD_USD   default 0.50
"""  # noqa: E501

import json
import os
import time
from dataclasses import dataclass
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

_CHEAP: str = "openai/gpt-4o-mini"
_MID: str = "openai/gpt-4o"
_EXPENSIVE: str = "openai/o1-preview"

_ALWAYS_EXPENSIVE: frozenset = frozenset(
    {"direction_debate", "final_synthesis", "consensus", "gate_decision"}
)
_ALWAYS_MID_OR_HIGHER: frozenset = frozenset(
    {"analysis", "code_review", "validation", "risk_attribution", "quant_analytics"}
)
# Blended cost per 1K tokens (3:1 input:output ratio)
_COST_PER_1K: Dict[str, float] = {
    _CHEAP: 0.000165,
    _MID: 0.00375,
    _EXPENSIVE: 0.045,
}


# ---------------------------------------------------------------------------
# I/O helpers
# ---------------------------------------------------------------------------


def _load_json_safe(path: str) -> Dict[str, Any]:
    """Load *path* as JSON dict; return {} on missing file or parse error."""
    if not os.path.isfile(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _write_json_safe(path: str, data: Any) -> None:
    """Serialise *data* as indented JSON; raise RuntimeError on failure."""
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
        raise RuntimeError(f"model_cascade: cannot write {path!r}: {exc}") from exc


# ---------------------------------------------------------------------------
# Tier selection heuristic
# ---------------------------------------------------------------------------


def _recommend_tier(
    stage_name: str,
    tokens: int,
    cost_usd: float,
    quality_score: Optional[float],
    cost_threshold: float,
) -> Tuple[str, str]:
    """Return (tier_name, reason) for one pipeline stage.

    Decision priority (first match wins):
    1. Stage matches _ALWAYS_EXPENSIVE keyword  -> expensive
    2. Stage matches _ALWAYS_MID_OR_HIGHER keyword -> mid
    3. cost_usd > cost_threshold                -> cheap
    4. tokens < 2000 and quality_score >= 80    -> cheap
    5. Default                                  -> mid
    """
    lower = stage_name.lower()
    for kw in _ALWAYS_EXPENSIVE:
        if kw in lower:
            return (
                "expensive",
                f"Stage {stage_name!r} requires deep reasoning; expensive tier required.",
            )
    for kw in _ALWAYS_MID_OR_HIGHER:
        if kw in lower:
            return (
                "mid",
                f"Stage {stage_name!r} performs structured analysis; mid tier recommended.",
            )
    if cost_usd > cost_threshold:
        return (
            "cheap",
            f"Stage {stage_name!r} cost exceeded threshold; recommend cheap tier.",
        )
    if tokens > 0 and tokens < 2_000 and (quality_score if quality_score is not None else 0.0) >= 80.0:
        return (
            "cheap",
            (
                f"Stage {stage_name!r} used only {tokens} tokens with quality "
                f"score {quality_score if quality_score is not None else 0.0:.1f}; cheap tier sufficient."
            ),
        )
    return ("mid", f"Stage {stage_name!r} no special routing rule; defaulting to mid.")


def _estimated_cost(tokens: int, model: str) -> float:
    """Return estimated USD cost for *tokens* tokens on *model*."""
    return tokens * _COST_PER_1K.get(model, _COST_PER_1K[_MID]) / 1_000.0


# ---------------------------------------------------------------------------
# Data class
# ---------------------------------------------------------------------------


@dataclass
class StageCascadeRec:
    """Cascade recommendation for one pipeline stage."""

    stage: str
    current_model: str
    recommended_tier: str
    recommended_model: str
    tokens_used: int
    current_cost_usd: float
    estimated_cost_usd: float
    estimated_savings_usd: float
    flagged_for_downgrade: bool
    reason: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "stage": self.stage,
            "current_model": self.current_model,
            "recommended_tier": self.recommended_tier,
            "recommended_model": self.recommended_model,
            "tokens_used": self.tokens_used,
            "current_cost_usd": round(self.current_cost_usd, 6),
            "estimated_cost_usd": round(self.estimated_cost_usd, 6),
            "estimated_savings_usd": round(self.estimated_savings_usd, 6),
            "flagged_for_downgrade": self.flagged_for_downgrade,
            "reason": self.reason,
        }


# ---------------------------------------------------------------------------
# Core logic
# ---------------------------------------------------------------------------


def build_cascade_recommendations(
    analysis: Dict[str, Any],
    meta: Dict[str, Any],
    cheap_model: str,
    mid_model: str,
    expensive_model: str,
    cost_threshold: float,
    quality_score: Optional[float],
) -> List[StageCascadeRec]:
    """Derive per-stage cascade recommendations from run artefacts.

    Stage-data schemas tried in order from analysis and meta:
      llm_usage / stage_usage / token_usage
    Each maps stage_name -> {tokens, cost_usd, model}.

    Falls back to a single overall entry when no per-stage data is found.
    """
    tier_to_model: Dict[str, str] = {
        "cheap": cheap_model, "mid": mid_model, "expensive": expensive_model,
    }
    stage_usage: Dict[str, Dict[str, Any]] = {}
    for source in (analysis, meta):
        for key in ("llm_usage", "stage_usage", "token_usage"):
            raw = source.get(key)
            if isinstance(raw, dict):
                for sname, sdata in raw.items():
                    if isinstance(sdata, dict):
                        stage_usage.setdefault(sname, {}).update(sdata)
    if not stage_usage:
        total_tokens, total_cost = 0, 0.0
        for source in (analysis, meta):
            for k in ("total_tokens", "tokens", "token_count"):
                v = source.get(k)
                if isinstance(v, (int, float)):
                    total_tokens = max(total_tokens, int(v))
            for k in ("total_cost_usd", "cost_usd", "cost"):
                v = source.get(k)
                if isinstance(v, (int, float)):
                    total_cost = max(total_cost, float(v))
        current_model = (
            meta.get("llm_model") or meta.get("llm_provider")
            or analysis.get("model_used") or mid_model
        )
        stage_usage["overall"] = {
            "tokens": total_tokens, "cost_usd": total_cost, "model": current_model,
        }
    recs: List[StageCascadeRec] = []
    for sname, data in stage_usage.items():
        tokens = int(data.get("tokens", 0) or 0)
        cost_usd = float(data.get("cost_usd", 0.0) or 0.0)
        current_model = str(data.get("model") or meta.get("llm_model") or mid_model)
        tier, reason = _recommend_tier(sname, tokens, cost_usd, quality_score, cost_threshold)
        recommended_model = tier_to_model.get(tier, mid_model)
        est_cost = _estimated_cost(tokens, recommended_model) if tokens > 0 else 0.0
        recs.append(StageCascadeRec(
            stage=sname, current_model=current_model, recommended_tier=tier,
            recommended_model=recommended_model, tokens_used=tokens,
            current_cost_usd=cost_usd, estimated_cost_usd=est_cost,
            estimated_savings_usd=max(0.0, cost_usd - est_cost),
            flagged_for_downgrade=cost_usd > cost_threshold, reason=reason,
        ))
    return sorted(recs, key=lambda r: r.estimated_savings_usd, reverse=True)


def run_model_cascade(run_dir: str) -> Dict[str, Any]:
    """Analyse run_dir and write model_cascade_report.json + model_cascade_config.json.

    Returns the report dict.  Raises RuntimeError on write failure.
    """
    cheap_model = os.environ.get("MODEL_CASCADE_CHEAP_MODEL", _CHEAP)
    mid_model = os.environ.get("MODEL_CASCADE_MID_MODEL", _MID)
    expensive_model = os.environ.get("MODEL_CASCADE_EXPENSIVE_MODEL", _EXPENSIVE)
    try:
        cost_threshold = float(os.environ.get("MODEL_CASCADE_COST_THRESHOLD_USD", "0.50"))
    except ValueError:
        cost_threshold = 0.50
    analysis = _load_json_safe(os.path.join(run_dir, "analysis_result.json"))
    meta = _load_json_safe(os.path.join(run_dir, "run_meta.json"))
    quality_score: Optional[float] = None
    try:
        raw = analysis.get("score")
        if raw is not None:
            quality_score = float(raw)
    except (TypeError, ValueError):
        pass
    recs = build_cascade_recommendations(
        analysis=analysis, meta=meta, cheap_model=cheap_model,
        mid_model=mid_model, expensive_model=expensive_model,
        cost_threshold=cost_threshold, quality_score=quality_score,
    )
    total_current = sum(r.current_cost_usd for r in recs)
    total_estimated = sum(r.estimated_cost_usd for r in recs)
    total_savings = sum(r.estimated_savings_usd for r in recs)
    flagged_stages = [r.stage for r in recs if r.flagged_for_downgrade]
    report: Dict[str, Any] = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "run_dir": run_dir,
        "quality_score": quality_score,
        "cost_threshold_usd": cost_threshold,
        "total_current_cost_usd": round(total_current, 6),
        "total_estimated_cost_usd": round(total_estimated, 6),
        "total_estimated_savings_usd": round(total_savings, 6),
        "flagged_stages": flagged_stages,
        "recommendations": [r.to_dict() for r in recs],
        "models": {"cheap": cheap_model, "mid": mid_model, "expensive": expensive_model},
    }
    cascade_config: Dict[str, Any] = {
        "generated_at": report["generated_at"],
        "run_dir": run_dir,
        "env_var_recommendations": {
            "MODEL_CASCADE_ENABLED": "1",
            "MODEL_CASCADE_CHEAP_MODEL": cheap_model,
            "MODEL_CASCADE_MID_MODEL": mid_model,
            "MODEL_CASCADE_EXPENSIVE_MODEL": expensive_model,
            "MODEL_CASCADE_COST_THRESHOLD_USD": str(cost_threshold),
        },
        "per_stage_model_override": {r.stage: r.recommended_model for r in recs},
        "summary": (
            f"Cascade reduces cost from ${total_current:.4f} "
            f"to ${total_estimated:.4f} (saving ${total_savings:.4f})."
        ),
    }
    _write_json_safe(os.path.join(run_dir, "model_cascade_report.json"), report)
    _write_json_safe(os.path.join(run_dir, "model_cascade_config.json"), cascade_config)
    return report


# ---------------------------------------------------------------------------
# Feature class
# ---------------------------------------------------------------------------


@register("model_cascade")
class ModelCascadeFeature(BaseFeature):
    """Post-processing feature: model_cascade.

    Recommends the cheapest safe model tier for each pipeline stage and
    writes model_cascade_report.json and model_cascade_config.json.
    """

    name = "model_cascade"
    label = "Model Cascade Routing"
    requires: list[str] = []

    def run(self, run_dir: str, config: FeatureConfig) -> FeatureResult:
        """Execute model cascade analysis for run_dir."""
        if os.environ.get("MODEL_CASCADE_ENABLED", "1").strip().lower() in ("0", "false", "no", "off"):
            return FeatureResult(
                feature=self.name, success=True, skipped=True,
                skip_reason="MODEL_CASCADE_ENABLED is not 1.",
            )
        t0 = time.monotonic()
        try:
            report = run_model_cascade(run_dir)
        except Exception as exc:
            return FeatureResult(
                feature=self.name, success=False,
                summary=f"model_cascade failed: {exc}",
                duration_seconds=time.monotonic() - t0, error=str(exc),
            )
        duration = time.monotonic() - t0
        savings = report.get("total_estimated_savings_usd", 0.0)
        flagged = report.get("flagged_stages", [])
        rec_count = len(report.get("recommendations", []))
        return FeatureResult(
            feature=self.name, success=True,
            summary=(
                f"{rec_count} stage(s) analysed; "
                f"estimated savings ${savings:.4f}; "
                f"{len(flagged)} stage(s) flagged for downgrade."
            ),
            duration_seconds=duration,
            details={
                "total_estimated_savings_usd": savings,
                "flagged_stages": flagged,
                "recommendations_count": rec_count,
            },
        )
