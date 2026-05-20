"""
crucible.features.direction_debate.consensus
=============================================

Deterministic consensus-risk computation for the Stage 0 direction debate.

Why this module exists
----------------------
Mira Chen's feedback on the v1.1.7 design highlighted a single most-valuable
observation: **the gate's most useful output is not PROCEED/KILL, it is the
disagreement trace**.  Same-model-family crews share blind spots; unanimous
high-confidence verdicts are precisely the cases where a human reviewer
should be most suspicious.

This module produces a :class:`ConsensusRiskReport` from a list of
:class:`SpecialistFinding` objects, surfacing the structural signals that a
human reviewer needs to spot premature consensus:

* ``zero_disagreement_recorded`` — sum of all ``disagreement_with`` lists
  is zero across every agent.  This is the strongest groupthink signal.
* ``low_diversity_high_confidence`` — concern Jaccard distance is below the
  configurable threshold AND mean confidence is high (>0.85).
* ``all_high_confidence_low_variance`` — confidence stddev < 0.05 AND mean
  is high.  Indicates copy-paste verdicts.
* ``<role>_too_confident_no_concerns`` — per-agent flag: agent has fewer
  than 2 concerns AND confidence > 0.85.  Especially suspicious for the
  Skeptic role (Skeptic's job is to find concerns; having none is a tell).
* ``high_assumption_overlap`` — pairwise Jaccard similarity of assumption
  sets averages above 0.7.  Indicates the agents are reasoning from the
  same priors and may share the same blind spot.

Algorithm choices
-----------------

* **Token-based, embedding-free.**  All similarity metrics use Jaccard over
  content-word tokens.  v1.2.0 retrieval will re-process this from stored
  ledger events without re-running any LLM, so determinism is mandatory.

* **Empty-empty pair handling.**  Two agents with empty concern sets are
  treated as Jaccard similarity 1.0 (maximally similar) — this correctly
  identifies "everyone said nothing" as a groupthink signal rather than as
  "vacuous diversity".  The per-agent ``agent_too_confident_no_concerns``
  flag provides a parallel signal.

* **Thresholds.**  ``concern_diversity`` < ``threshold`` (env-configurable,
  default 0.3) triggers diversity warnings.  Confidence-mean cutoff fixed
  at 0.85 — this is the "agents-are-being-overconfident" line empirically
  drawn from v1.1.0 debate ledger samples; lower than that, an agent is
  acknowledging some uncertainty and the groupthink risk is reduced.

* **Score weighting.**  ``groupthink_score`` ∈ [0, 1] is a weighted sum of
  the underlying flags, intentionally biased so a single ``zero_disagreement``
  signal can drive the score above 0.4 (the "concerning" line) by itself —
  Mira's point that unanimity *is* the audit signal.

Output:  :class:`ConsensusRiskReport` (defined in
``crucible.modules.section_03_models_and_context``).
"""
from __future__ import annotations

import math
import re
import statistics
from typing import Any, Dict, FrozenSet, List, Optional, Sequence, Set

# Tri-modal import: this module is loaded under three distinct package
# layouts depending on how the entry point was launched (see the analogous
# block in ``features/run_insights/recorder.py``).
try:
    from ..._env import env_float
    from ...modules.section_03_models_and_context import (
        ConsensusRiskReport,
        SpecialistFinding,
    )
except ImportError:  # pragma: no cover - flat-launcher fallback
    from _env import env_float  # type: ignore[no-redef]
    from modules.section_03_models_and_context import (  # type: ignore[no-redef]
        ConsensusRiskReport,
        SpecialistFinding,
    )


# ── Constants ────────────────────────────────────────────────────────────────

# Content-word stopwords for token sets.  Kept small — we want to keep the
# tokens meaningful but not over-fit any one language.  CJK characters are
# handled by ``_TOKEN_SPLIT_RE`` which keeps individual CJK chars as tokens
# (single-character CJK words are common and meaningful).
_STOPWORDS: FrozenSet[str] = frozenset({
    # English
    "a", "an", "the", "is", "are", "was", "were", "be", "been", "being",
    "of", "in", "on", "to", "for", "with", "and", "or", "but", "by",
    "this", "that", "these", "those", "it", "its", "their", "his", "her",
    "if", "then", "than", "as", "at", "from", "into", "out", "up", "down",
    "we", "you", "they", "i", "he", "she", "them", "us", "our",
    "not", "no", "yes", "so", "do", "does", "did", "doing", "done",
    "can", "could", "should", "would", "may", "might", "must", "will",
    "have", "has", "had", "having", "more", "most", "less", "least",
    "very", "too", "also", "such", "any", "some", "all", "none",
    # Chinese function words (frequent in zh-TW / zh-CN debate output)
    "的", "了", "是", "在", "和", "與", "或", "但", "如", "也",
    "都", "就", "再", "還", "並", "等", "及", "於", "對", "為",
    "不", "沒", "有", "個", "上", "下", "中", "之", "其", "此",
})

# Token splitter that keeps ASCII alphanum runs AND single CJK characters.
# The regex is split-style: ``re.split`` on the inverse character class.
_TOKEN_SPLIT_RE = re.compile(r"[^a-zA-Z0-9一-鿿]+")

# Confidence threshold above which a high-confidence flag fires.  Drawn
# empirically from v1.1.0 debate ledger samples — see module docstring.
_HIGH_CONFIDENCE_CUTOFF: float = 0.85

# Confidence variance below which "all agents copy-pasting" is suspected.
_LOW_VARIANCE_CUTOFF: float = 0.05

# Minimum concerns count below which "this agent thought too little" is
# suspected when paired with high confidence.
_MIN_CONCERNS_FOR_TRUST: int = 2

# Assumption-overlap threshold above which "shared priors" is flagged.
_HIGH_ASSUMPTION_OVERLAP_CUTOFF: float = 0.7

# Numerical floor for Jaccard denominators — per CLAUDE.md (global) the
# correct floor for general division checks is ``> 1e-14`` (not ``> 0`` and
# not ``<= 0``).  Set sizes are integers so this is precautionary, but the
# pattern stays consistent.
_DIV_FLOOR: float = 1e-14


# ── Tokenisation helpers ──────────────────────────────────────────────────────

def _tokenize_strings(strings: Sequence[str]) -> Set[str]:
    """Return content-word token set from a list of strings.

    Lowercases ASCII, splits on non-alphanum (treating individual CJK chars
    as tokens), removes stopwords, and drops single-character ASCII tokens
    (those are usually noise).  CJK single chars are KEPT because they
    are often meaningful in zh-TW / zh-CN debate text.
    """
    tokens: Set[str] = set()
    for raw in strings or []:
        if not raw:
            continue
        s = str(raw).lower()
        for tok in _TOKEN_SPLIT_RE.split(s):
            if not tok or tok in _STOPWORDS:
                continue
            # Single ASCII char is noise; single CJK char is meaningful.
            if len(tok) == 1 and tok.isascii():
                continue
            tokens.add(tok)
    return tokens


def _concern_token_set(finding: "SpecialistFinding") -> Set[str]:
    """Tokenize the descriptions of every Concern this specialist raised."""
    return _tokenize_strings([c.description for c in (finding.concerns or [])])


def _assumption_token_set(finding: "SpecialistFinding") -> Set[str]:
    return _tokenize_strings(list(finding.assumptions or []))


# ── Pairwise Jaccard helpers ──────────────────────────────────────────────────

def _jaccard_similarity(a: Set[str], b: Set[str]) -> float:
    """Standard Jaccard with deterministic empty-set handling.

    * Both empty → 1.0 (maximally similar — both agents recorded the same
      nothing; this is the "everyone agreed there is no concern" case
      that v1.1.8 audit mode wants to flag).
    * One empty, one non-empty → 0.0 (no overlap).
    * Both non-empty → standard ``|A ∩ B| / |A ∪ B|``.
    """
    if not a and not b:
        return 1.0
    if not a or not b:
        return 0.0
    union = len(a | b)
    if union < _DIV_FLOOR:  # vacuous safety; integer ≥ 1 here
        return 0.0
    return len(a & b) / union


def _pairwise_min_jaccard_distance(sets: List[Set[str]]) -> float:
    """Return the minimum Jaccard *distance* (= 1 - similarity) across pairs.

    "Diversity" semantics: a low value means at least one pair is highly
    similar (potential groupthink seed).  Returns 1.0 (max diverse) when
    there are fewer than 2 sets.
    """
    if len(sets) < 2:
        return 1.0
    min_dist = 1.0
    for i in range(len(sets)):
        for j in range(i + 1, len(sets)):
            dist = 1.0 - _jaccard_similarity(sets[i], sets[j])
            if dist < min_dist:
                min_dist = dist
    return max(0.0, min(1.0, min_dist))


def _pairwise_avg_jaccard_similarity(sets: List[Set[str]]) -> float:
    """Return the mean Jaccard similarity across all pairs.

    Returns 0.0 when there are fewer than 2 sets.  Pearson-r-style clamp
    applied at the end so floating-point drift cannot push the result
    above 1.0 or below 0.0 (CLAUDE.md global rule).
    """
    if len(sets) < 2:
        return 0.0
    sims = []
    for i in range(len(sets)):
        for j in range(i + 1, len(sets)):
            sims.append(_jaccard_similarity(sets[i], sets[j]))
    if not sims:
        return 0.0
    avg = sum(sims) / len(sims)
    return max(0.0, min(1.0, avg))


# ── Confidence statistics ─────────────────────────────────────────────────────

def _confidence_stats(findings: Sequence["SpecialistFinding"]) -> Dict[str, float]:
    """Return mean + population stddev of confidence across findings.

    Uses ``statistics.pstdev`` (population stddev) instead of ``stdev``
    (sample stddev) because we have the full set of agents — no inference
    to a larger population is involved.  Returns 0.0 for both when fewer
    than 1 finding present.
    """
    confs: List[float] = []
    for f in findings:
        try:
            c = float(f.confidence)
            if math.isfinite(c):
                confs.append(c)
        except (TypeError, ValueError):
            continue
    if not confs:
        return {"mean": 0.0, "stddev": 0.0, "n": 0}
    mean = sum(confs) / len(confs)
    stddev = statistics.pstdev(confs) if len(confs) > 1 else 0.0
    return {"mean": mean, "stddev": stddev, "n": len(confs)}


# ── Public API ────────────────────────────────────────────────────────────────

def compute_consensus_risk(
    findings: Sequence["SpecialistFinding"],
    *,
    threshold: Optional[float] = None,
) -> "ConsensusRiskReport":
    """Compute a :class:`ConsensusRiskReport` from a list of findings.

    Parameters
    ----------
    findings
        Sequence of :class:`SpecialistFinding` from the Stage 0 crew.  Empty
        list returns a minimal report with ``flags=["no_findings"]`` and
        zero scores — caller should generally not invoke this with no
        findings, but the function tolerates it.
    threshold
        Optional override for the concern-diversity threshold.  If ``None``,
        reads ``CRUCIBLE_DEBATE_CONSENSUS_RISK_THRESHOLD`` (default 0.3).

    Returns
    -------
    ConsensusRiskReport
        Deterministic, embedding-free risk analysis.  ``groupthink_score``
        is in [0, 1] (clamped); ``flags`` lists every triggered heuristic.
    """
    if threshold is None:
        threshold = env_float(
            "CRUCIBLE_DEBATE_CONSENSUS_RISK_THRESHOLD",
            0.3,
            finite_only=True,
            clamp_min=0.0,
            clamp_max=1.0,
        )

    findings_list = list(findings or [])

    if not findings_list:
        return ConsensusRiskReport(
            groupthink_score=0.0,
            concern_diversity=1.0,  # vacuously diverse
            assumption_overlap=0.0,
            confidence_variance=0.0,
            flags=["no_findings"],
            raw_metrics={"n_findings": 0, "threshold": float(threshold)},
        )

    # ── Tokenize concerns / assumptions per agent ──────────────────────────
    concern_sets = [_concern_token_set(f) for f in findings_list]
    assumption_sets = [_assumption_token_set(f) for f in findings_list]

    # ── Compute structural metrics ─────────────────────────────────────────
    concern_diversity = _pairwise_min_jaccard_distance(concern_sets)
    assumption_overlap = _pairwise_avg_jaccard_similarity(assumption_sets)

    conf_stats = _confidence_stats(findings_list)
    mean_conf = conf_stats["mean"]
    confidence_variance = conf_stats["stddev"]

    total_disagreements = sum(
        len(f.disagreement_with or []) for f in findings_list
    )

    # ── Trigger flags ──────────────────────────────────────────────────────
    flags: List[str] = []

    if total_disagreements == 0:
        flags.append("zero_disagreement_recorded")

    if (
        confidence_variance < _LOW_VARIANCE_CUTOFF
        and mean_conf > _HIGH_CONFIDENCE_CUTOFF
    ):
        flags.append("all_high_confidence_low_variance")

    if concern_diversity < float(threshold) and mean_conf > _HIGH_CONFIDENCE_CUTOFF:
        flags.append("low_diversity_high_confidence")

    if assumption_overlap > _HIGH_ASSUMPTION_OVERLAP_CUTOFF:
        flags.append("high_assumption_overlap")

    for f in findings_list:
        concerns_n = len(f.concerns or [])
        if (
            concerns_n < _MIN_CONCERNS_FOR_TRUST
            and float(f.confidence) > _HIGH_CONFIDENCE_CUTOFF
        ):
            flags.append(f"{f.role}_too_confident_no_concerns")

    # ── Weighted groupthink score ──────────────────────────────────────────
    # Weights chosen so zero_disagreement alone reaches 0.40 (concerning
    # threshold).  Other signals stack but each individually maxes out
    # below 0.40 — keeps zero_disagreement as the primary tell, matching
    # the v1.1.8 design intent.
    score = 0.0
    if total_disagreements == 0:
        score += 0.40
    # Low concern diversity contributes up to 0.25 (inverse of diversity).
    score += (1.0 - concern_diversity) * 0.25
    # High assumption overlap contributes up to 0.15.
    score += assumption_overlap * 0.15
    # Low variance + high mean = +0.20.
    if (
        confidence_variance < _LOW_VARIANCE_CUTOFF
        and mean_conf > _HIGH_CONFIDENCE_CUTOFF
    ):
        score += 0.20
    # Per-agent over-confidence flags each add a small 0.05 with cap (so
    # five agents all flagging contributes 0.25, not blowing past 1.0).
    per_agent_overconfident = sum(
        1
        for f in findings_list
        if len(f.concerns or []) < _MIN_CONCERNS_FOR_TRUST
        and float(f.confidence) > _HIGH_CONFIDENCE_CUTOFF
    )
    score += min(0.25, per_agent_overconfident * 0.05)

    groupthink_score = max(0.0, min(1.0, score))

    return ConsensusRiskReport(
        groupthink_score=groupthink_score,
        concern_diversity=concern_diversity,
        assumption_overlap=assumption_overlap,
        confidence_variance=confidence_variance,
        flags=flags,
        raw_metrics={
            "n_findings": len(findings_list),
            "total_disagreements": total_disagreements,
            "mean_confidence": mean_conf,
            "threshold": float(threshold),
            "concern_set_sizes": [len(s) for s in concern_sets],
            "assumption_set_sizes": [len(s) for s in assumption_sets],
            "per_agent_overconfident": per_agent_overconfident,
        },
    )


__all__ = ["compute_consensus_risk"]
