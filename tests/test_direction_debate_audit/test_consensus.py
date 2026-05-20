"""
v1.1.8 — Tests for ``crucible.features.direction_debate.consensus``.

The consensus-risk computation is deterministic and embedding-free — these
tests pin every heuristic the v1.1.8 design depends on:

* ``zero_disagreement_recorded`` flag fires iff total disagreements == 0
* ``low_diversity_high_confidence`` flag fires below threshold + mean > 0.85
* ``all_high_confidence_low_variance`` flag fires below variance + mean > 0.85
* ``high_assumption_overlap`` flag fires above 0.7 pairwise Jaccard
* ``<role>_too_confident_no_concerns`` flag fires per-agent
* ``groupthink_score`` is monotonically responsive to disagreement count
* threshold env var changes flag behaviour
* empty findings list returns a vacuous report (no_findings flag)

Each test uses the minimal possible SpecialistFinding payload so behaviour
is exactly traceable to the metric being asserted.
"""
from __future__ import annotations

import pytest

from crucible.features.direction_debate.consensus import compute_consensus_risk
from crucible.modules.section_03_models_and_context import (
    Concern,
    Disagreement,
    SpecialistFinding,
)


def _f(
    role: str = "explorer",
    confidence: float = 0.6,
    *,
    concerns=None,
    disagreements=None,
    assumptions=None,
    failed_invariants=None,
) -> SpecialistFinding:
    """Build a minimal SpecialistFinding with the requested fields."""
    return SpecialistFinding(
        role=role,
        conclusion=f"{role} conclusion",
        confidence=confidence,
        assumptions=assumptions or [],
        concerns=concerns or [],
        disagreement_with=disagreements or [],
        failed_invariants=failed_invariants or [],
    )


# ── No-findings edge case ────────────────────────────────────────────────────


class TestEmptyFindings:
    def test_empty_list_returns_no_findings_flag(self) -> None:
        r = compute_consensus_risk([])
        assert r.groupthink_score == 0.0
        assert "no_findings" in r.flags
        assert r.concern_diversity == 1.0  # vacuously diverse


# ── zero_disagreement_recorded ───────────────────────────────────────────────


class TestZeroDisagreement:
    def test_zero_disagreement_flag_fires_when_no_agents_disagree(self) -> None:
        findings = [
            _f("explorer"),
            _f("comparator"),
            _f("skeptic"),
            _f("evidence_auditor"),
            _f("judge"),
        ]
        r = compute_consensus_risk(findings)
        assert "zero_disagreement_recorded" in r.flags
        # Zero-disagreement alone must push groupthink_score above the 0.4
        # "concerning" line — that's Mira Chen's "unanimous = suspicious"
        # signal made structural.
        assert r.groupthink_score >= 0.4

    def test_zero_disagreement_flag_clears_when_any_agent_disagrees(self) -> None:
        findings = [
            _f("explorer"),
            _f(
                "skeptic",
                disagreements=[
                    Disagreement(with_role="explorer", point="too optimistic"),
                ],
            ),
            _f("judge"),
        ]
        r = compute_consensus_risk(findings)
        assert "zero_disagreement_recorded" not in r.flags


# ── concern_diversity ────────────────────────────────────────────────────────


class TestConcernDiversity:
    def test_identical_concerns_across_agents_low_diversity(self) -> None:
        shared_concern = Concern(
            severity="material", description="market liquidity is thin"
        )
        findings = [
            _f("explorer", confidence=0.9, concerns=[shared_concern]),
            _f("skeptic", confidence=0.9, concerns=[shared_concern]),
            _f(
                "judge",
                confidence=0.9,
                concerns=[shared_concern],
                disagreements=[
                    Disagreement(with_role="explorer", point="x"),
                ],
            ),
        ]
        r = compute_consensus_risk(findings)
        # Pairwise Jaccard between identical sets is 1.0; distance 0.0.
        assert r.concern_diversity == 0.0
        # With mean confidence > 0.85, low_diversity_high_confidence fires.
        assert "low_diversity_high_confidence" in r.flags

    def test_distinct_concerns_high_diversity(self) -> None:
        findings = [
            _f(
                "explorer",
                concerns=[Concern(severity="minor", description="liquidity")],
            ),
            _f(
                "skeptic",
                concerns=[Concern(severity="minor", description="regulatory risk")],
                disagreements=[Disagreement(with_role="explorer", point="x")],
            ),
            _f(
                "judge",
                concerns=[Concern(severity="minor", description="venue stability")],
            ),
        ]
        r = compute_consensus_risk(findings)
        # Disjoint token sets → Jaccard 0 → distance 1.0.
        assert r.concern_diversity == 1.0
        assert "low_diversity_high_confidence" not in r.flags

    def test_threshold_env_var_changes_flag_behaviour(self, monkeypatch) -> None:
        """When the operator raises the threshold, even moderately diverse
        concerns trigger the flag."""
        concerns_a = [
            Concern(severity="minor", description="liquidity stale market"),
        ]
        concerns_b = [
            Concern(severity="minor", description="liquidity new market"),
        ]
        findings = [
            _f("explorer", confidence=0.95, concerns=concerns_a),
            _f("skeptic", confidence=0.95, concerns=concerns_b),
        ]
        # Default threshold (0.3) — these share "liquidity" so diversity is
        # ~0.5–0.66.  Set threshold above that to force the flag on.
        monkeypatch.setenv("CRUCIBLE_DEBATE_CONSENSUS_RISK_THRESHOLD", "0.9")
        r = compute_consensus_risk(findings)
        assert "low_diversity_high_confidence" in r.flags


# ── confidence_variance ──────────────────────────────────────────────────────


class TestConfidenceVariance:
    def test_high_mean_low_variance_flag_fires(self) -> None:
        findings = [
            _f("explorer", confidence=0.92),
            _f("skeptic", confidence=0.93, disagreements=[
                Disagreement(with_role="explorer", point="x")
            ]),
            _f("judge", confidence=0.91),
        ]
        r = compute_consensus_risk(findings)
        assert "all_high_confidence_low_variance" in r.flags

    def test_low_mean_does_not_trigger(self) -> None:
        findings = [
            _f("explorer", confidence=0.45),
            _f("skeptic", confidence=0.42, disagreements=[
                Disagreement(with_role="explorer", point="x")
            ]),
            _f("judge", confidence=0.43),
        ]
        r = compute_consensus_risk(findings)
        assert "all_high_confidence_low_variance" not in r.flags


# ── per-agent over-confidence ────────────────────────────────────────────────


class TestPerAgentOverconfident:
    def test_skeptic_with_no_concerns_high_confidence_flagged(self) -> None:
        """Skeptic's job is to find concerns — having none with high
        confidence is the canonical "agent too confident" tell."""
        findings = [
            _f("skeptic", confidence=0.95, concerns=[]),
            _f("judge", confidence=0.50, disagreements=[
                Disagreement(with_role="skeptic", point="x")
            ]),
        ]
        r = compute_consensus_risk(findings)
        assert "skeptic_too_confident_no_concerns" in r.flags

    def test_judge_with_one_concern_high_confidence_flagged(self) -> None:
        """One concern still counts as "too few" — threshold is < 2."""
        findings = [
            _f(
                "judge",
                confidence=0.90,
                concerns=[
                    Concern(severity="minor", description="single concern")
                ],
            ),
        ]
        r = compute_consensus_risk(findings)
        assert "judge_too_confident_no_concerns" in r.flags

    def test_agent_with_two_concerns_not_flagged(self) -> None:
        findings = [
            _f(
                "skeptic",
                confidence=0.95,
                concerns=[
                    Concern(severity="minor", description="a"),
                    Concern(severity="minor", description="b"),
                ],
            ),
        ]
        r = compute_consensus_risk(findings)
        assert "skeptic_too_confident_no_concerns" not in r.flags


# ── assumption_overlap ───────────────────────────────────────────────────────


class TestAssumptionOverlap:
    def test_high_overlap_flag_fires(self) -> None:
        shared_assumptions = [
            "market depth is at least 10M USD daily",
            "fee schedule is standard taker rate",
            "no exchange downtime in lookback window",
        ]
        findings = [
            _f("explorer", assumptions=shared_assumptions),
            _f("skeptic", assumptions=shared_assumptions),
            _f("judge", assumptions=shared_assumptions),
        ]
        r = compute_consensus_risk(findings)
        # Identical assumption sets → Jaccard 1.0 → overlap 1.0.
        assert r.assumption_overlap == 1.0
        assert "high_assumption_overlap" in r.flags

    def test_low_overlap_no_flag(self) -> None:
        findings = [
            _f("explorer", assumptions=["liquidity is high"]),
            _f("skeptic", assumptions=["regulator stable"]),
            _f("judge", assumptions=["venue uptime acceptable"]),
        ]
        r = compute_consensus_risk(findings)
        assert "high_assumption_overlap" not in r.flags


# ── groupthink_score monotonicity ────────────────────────────────────────────


class TestGroupthinkScore:
    def test_score_in_unit_interval(self) -> None:
        """Score MUST be clamped to [0, 1] regardless of how many flags fire."""
        shared_concern = Concern(severity="material", description="risk")
        findings = [
            _f("explorer", confidence=0.95, concerns=[shared_concern],
               assumptions=["x"]),
            _f("comparator", confidence=0.95, concerns=[shared_concern],
               assumptions=["x"]),
            _f("skeptic", confidence=0.95, concerns=[shared_concern],
               assumptions=["x"]),
            _f("evidence_auditor", confidence=0.95, concerns=[shared_concern],
               assumptions=["x"]),
            _f("judge", confidence=0.95, concerns=[shared_concern],
               assumptions=["x"]),
        ]
        r = compute_consensus_risk(findings)
        assert 0.0 <= r.groupthink_score <= 1.0

    def test_score_responds_to_disagreement_count(self) -> None:
        """Adding disagreements should monotonically decrease the
        groupthink score (zero_disagreement contribution is removed)."""
        base = [_f("explorer"), _f("skeptic"), _f("judge")]
        r_no_disagree = compute_consensus_risk(base)

        with_disagree = [
            _f("explorer"),
            _f(
                "skeptic",
                disagreements=[
                    Disagreement(with_role="explorer", point="too optimistic"),
                ],
            ),
            _f("judge"),
        ]
        r_with_disagree = compute_consensus_risk(with_disagree)
        # Adding disagreement removes the 0.40 zero_disagreement contribution.
        assert r_with_disagree.groupthink_score < r_no_disagree.groupthink_score


# ── raw_metrics ──────────────────────────────────────────────────────────────


class TestRawMetrics:
    def test_raw_metrics_populated(self) -> None:
        findings = [
            _f("explorer"),
            _f("skeptic", disagreements=[
                Disagreement(with_role="explorer", point="x")
            ]),
        ]
        r = compute_consensus_risk(findings)
        assert r.raw_metrics["n_findings"] == 2
        assert r.raw_metrics["total_disagreements"] == 1
        assert "mean_confidence" in r.raw_metrics
        assert "threshold" in r.raw_metrics
