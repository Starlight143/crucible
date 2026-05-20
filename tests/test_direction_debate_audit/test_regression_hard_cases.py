"""
v1.1.8 — Regression hard cases for Direction Debate Audit Mode.

These tests pin **failure modes** the v1.1.8 design was created to defend
against.  Each test reconstructs a scenario where a vanilla LLM gate would
silently produce the wrong terminal verdict; the audit-mode pipeline must
either prevent the wrong verdict (via pydantic invariants) or surface the
underlying disagreement so a reviewer can spot the issue.

Hard cases covered:

1.  **Hard KILL not silently downgraded to NEEDS_MORE_DATA** — a strategy
    that violates a hard invariant (e.g. mean-reversion on a known
    trending asset) must result in a GateVerdict with ``decision=KILL``
    and at least one entry in ``failed_invariants``.  The pydantic
    invariant validator MUST reject the easier-to-emit
    ``NEEDS_MORE_DATA`` with the same evidence.
2.  **Evidence gap is NEEDS_MORE_DATA, not KILL** — when evidence is
    merely thin (not invariant-violating), the verdict must be
    ``NEEDS_MORE_DATA`` with specific ``blocking_evidence_queries``.
    Returning ``KILL`` on thin evidence is the v1.1.8 failure mode
    we explicitly want to block.
3.  **Groupthink (zero disagreement, all high confidence, empty
    concerns)** — consensus risk computation must flag this even when
    every individual agent's output looks "fine" in isolation.
4.  **Critic dissent recorded but Judge stands by default** — when
    Critic disagrees with Judge but ``CRITIC_OVERRIDE_PROCEED=0``, the
    audit_trail must record ``critic_dissent_recorded=True`` while the
    final ``decision`` remains the Judge's.

Each test constructs the relevant pydantic models directly — no LLM
calls — so the regression guards are deterministic and fast.
"""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from crucible.features.direction_debate.consensus import compute_consensus_risk
from crucible.modules.section_03_models_and_context import (
    AuditTrail,
    BranchSpec,
    Concern,
    Disagreement,
    GateVerdict,
    SpecialistFinding,
)


_LONG_REASON = "a" * 30


# ── Hard case 1: KILL not silently downgraded ────────────────────────────────


class TestKillNotDowngraded:
    def test_kill_with_invariants_constructs(self) -> None:
        """The "correct" outcome for a hard violation: KILL + invariants."""
        v = GateVerdict(
            decision="KILL",
            reason=(
                "Strategy assumes mean-reversion on BTC/USD but the asset "
                "has been in a strong uptrend for 18 months — cointegration "
                "assumption violated."
            ),
            failed_invariants=[
                "mean-reversion strategy applied to non-stationary trending asset",
                "no cointegration test performed on the price series",
            ],
        )
        assert v.decision == "KILL"
        assert len(v.failed_invariants) == 2

    def test_kill_without_invariants_cannot_be_emitted(self) -> None:
        """v1.1.8 contract: an LLM CANNOT emit KILL without citing a
        specific failed invariant.  This is the structural protection
        against "I want to KILL but cannot articulate why".
        """
        with pytest.raises(ValidationError):
            GateVerdict(
                decision="KILL",
                reason="this strategy seems wrong",
                # failed_invariants intentionally empty
            )

    def test_kill_pretending_to_be_proceed_blocked(self) -> None:
        """A KILL verdict pretending to be PROCEED (with a selected
        direction) is structurally impossible."""
        with pytest.raises(ValidationError):
            GateVerdict(
                decision="KILL",
                selected_direction="A",  # contradicts KILL
                reason=_LONG_REASON,
                failed_invariants=["x"],
            )


# ── Hard case 2: NEEDS_MORE_DATA, not KILL, for evidence gaps ────────────────


class TestEvidenceGapIsNeedsMoreData:
    def test_evidence_gap_requires_specific_queries(self) -> None:
        """v1.1.8 NEEDS_MORE_DATA contract: must cite specific evidence
        queries.  "Just need more data" without specifics is structurally
        invalid (and would be useless for retrieval anyway)."""
        v = GateVerdict(
            decision="NEEDS_MORE_DATA",
            reason=(
                "Direction A relies on venue depth metrics that haven't "
                "been validated.  Cannot proceed without confirmation."
            ),
            blocking_evidence_queries=[
                "fetch binance order book depth at top-10 levels for last 30d",
                "verify daily volume > 50M USD on chosen venue",
            ],
        )
        assert v.decision == "NEEDS_MORE_DATA"
        assert len(v.blocking_evidence_queries) == 2

    def test_needs_more_data_without_queries_blocked(self) -> None:
        with pytest.raises(ValidationError):
            GateVerdict(
                decision="NEEDS_MORE_DATA",
                reason="not enough evidence for this direction",
                # blocking_evidence_queries intentionally empty
            )

    def test_needs_more_data_with_invariants_cannot_coexist(self) -> None:
        """NEEDS_MORE_DATA and KILL are mutually exclusive — an LLM that
        wants to express both must choose one."""
        with pytest.raises(ValidationError):
            GateVerdict(
                decision="NEEDS_MORE_DATA",
                reason=_LONG_REASON,
                blocking_evidence_queries=["x"],
                failed_invariants=["should not coexist"],
            )


# ── Hard case 3: Groupthink detection ────────────────────────────────────────


class TestGroupthinkDetection:
    def test_zero_disagreement_high_confidence_no_concerns_flagged(self) -> None:
        """Mira Chen's canonical groupthink case: five agents, all
        confidently agreeing, none recording concerns or disagreements.
        Consensus-risk computation must flag this — the audit trail's
        whole purpose is to surface this scenario for human review."""
        findings = [
            SpecialistFinding(
                role=role,
                conclusion="direction A is correct",
                confidence=0.95,
                # No concerns, no disagreement, no missing information.
            )
            for role in (
                "explorer",
                "comparator",
                "skeptic",
                "evidence_auditor",
                "judge",
            )
        ]
        report = compute_consensus_risk(findings)

        # Multiple flags should fire on this canonical case.
        assert "zero_disagreement_recorded" in report.flags
        assert "all_high_confidence_low_variance" in report.flags
        # Per-agent over-confidence: every role flagged.
        for role in (
            "explorer",
            "comparator",
            "skeptic",
            "evidence_auditor",
            "judge",
        ):
            assert (
                f"{role}_too_confident_no_concerns" in report.flags
            ), f"{role} should be flagged for over-confidence with no concerns"

        # Groupthink score should be high (Mira's "unanimity = suspicious"
        # made measurable).  The exact threshold depends on the weighting
        # function but for this maximally-bad case it MUST be above 0.7.
        assert report.groupthink_score >= 0.7, (
            f"groupthink_score={report.groupthink_score} on a canonical "
            f"groupthink case should be high — adjust weighting if not"
        )

    def test_healthy_debate_does_not_trigger_groupthink(self) -> None:
        """Counter-example: agents with diverse concerns + explicit
        disagreements should NOT trigger groupthink flags."""
        findings = [
            SpecialistFinding(
                role="explorer",
                conclusion="explore A",
                confidence=0.6,
                concerns=[
                    Concern(severity="material", description="liquidity stale"),
                    Concern(severity="minor", description="data quality"),
                ],
            ),
            SpecialistFinding(
                role="skeptic",
                conclusion="A is risky",
                confidence=0.4,
                concerns=[
                    Concern(severity="blocking", description="regulatory uncertainty"),
                    Concern(severity="material", description="venue stability"),
                ],
                disagreement_with=[
                    Disagreement(
                        with_role="explorer",
                        point="evidence too thin to justify A",
                        severity="material",
                    ),
                ],
            ),
            SpecialistFinding(
                role="judge",
                conclusion="proceed with caution on A",
                confidence=0.55,
                concerns=[
                    Concern(severity="material", description="balance of risks"),
                    Concern(severity="minor", description="execution complexity"),
                ],
                disagreement_with=[
                    Disagreement(
                        with_role="skeptic",
                        point="risk is real but manageable",
                        severity="minor",
                    ),
                ],
            ),
        ]
        report = compute_consensus_risk(findings)

        assert "zero_disagreement_recorded" not in report.flags
        # Confidence variance is non-trivial (0.4–0.6 spread).
        assert "all_high_confidence_low_variance" not in report.flags
        # Per-agent: nobody is high-confidence-no-concerns.
        for role in ("explorer", "skeptic", "judge"):
            assert (
                f"{role}_too_confident_no_concerns" not in report.flags
            )
        # Score should be moderate, not alarming.
        assert report.groupthink_score < 0.5


# ── Hard case 4: Critic dissent recorded without override ────────────────────


class TestCriticDissentRecording:
    def test_critic_dissent_recorded_default(self) -> None:
        """When CRITIC_OVERRIDE_PROCEED=0 (default), Critic dissent must be
        recorded but Judge verdict stands.  Construct the GateVerdict +
        AuditTrail directly to verify the schema supports this case."""
        v = GateVerdict(
            decision="PROCEED",  # Judge's verdict; Critic disagreed
            selected_direction="A",
            reason=(
                "Judge selected A; Critic argued for NEEDS_MORE_DATA but "
                "override_proceed=False so Judge verdict stands."
            ),
            audit_trail=AuditTrail(
                audit_mode_enabled=True,
                external_critic_used=True,
                critic_overrode_judge=False,
                critic_dissent_recorded=True,
            ),
        )
        assert v.decision == "PROCEED"
        assert v.audit_trail.critic_dissent_recorded is True
        assert v.audit_trail.critic_overrode_judge is False

    def test_critic_override_kill_recorded(self) -> None:
        """When CRITIC_OVERRIDE_PROCEED=1 and Critic returns KILL, the
        final verdict is KILL and audit_trail records the override."""
        v = GateVerdict(
            decision="KILL",
            reason="External Critic overrode Judge PROCEED with KILL on invariant violation.",
            failed_invariants=["cointegration not validated"],
            audit_trail=AuditTrail(
                audit_mode_enabled=True,
                external_critic_used=True,
                critic_overrode_judge=True,
                judge_initial_decision="PROCEED",
            ),
        )
        assert v.decision == "KILL"
        assert v.audit_trail.critic_overrode_judge is True
        assert v.audit_trail.judge_initial_decision == "PROCEED"

    def test_v118_critic_model_family_always_none(self) -> None:
        """v1.1.8 invariant: critic_model_family is reserved for v1.3.0.
        In v1.1.8 it MUST be None — the same-family Critic does not
        identify itself as belonging to a different family.  Tests that
        the schema accepts None (and that we don't accidentally populate
        it) protect against silent v1.3.0 promotion."""
        at = AuditTrail(
            audit_mode_enabled=True,
            external_critic_used=True,
        )
        # Default factory sets it to None; the value must not be coerced.
        assert at.critic_model_family is None


# ── Cross-case: BRANCH preserved as audit-only ───────────────────────────────


class TestBranchPreservation:
    def test_branch_with_two_paths_constructs(self) -> None:
        """v1.1.8 BRANCH is audit-only — does NOT spawn sub-runs.  The
        ledger event preserves the branch info for v1.2.0 retrieval, but
        the current run still picks branched_paths[0] as the PROCEED
        direction (handled at the orchestrator level)."""
        v = GateVerdict(
            decision="BRANCH",
            reason=(
                "Strategy splits into two independent venue-specific paths "
                "that should be tested separately."
            ),
            branched_paths=[
                BranchSpec(
                    direction_id="A",
                    rationale="binance-specific implementation",
                    blocking_questions=["binance fee structure verified?"],
                ),
                BranchSpec(
                    direction_id="C",
                    rationale="coinbase-specific implementation",
                    blocking_questions=["coinbase liquidity verified?"],
                ),
            ],
        )
        assert v.decision == "BRANCH"
        assert len(v.branched_paths) == 2
