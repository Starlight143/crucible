"""
v1.1.8 — Pydantic invariant tests for Direction Debate Audit Mode schemas.

Pins every structural invariant on :class:`SpecialistFinding`,
:class:`GateVerdict`, :class:`ConsensusRiskReport`, :class:`Disagreement`,
:class:`Concern`, :class:`EvidenceRef`, :class:`BranchSpec`, and
:class:`AuditTrail` so a future contributor cannot silently weaken the
"KILL must cite invariants, NEEDS_MORE_DATA must cite queries" contract.

Three categories of tests below:

* **Constructor success cases** — every valid decision shape constructs
  cleanly with minimal payload.
* **Invariant failure cases** — each illegal combination raises
  pydantic ``ValidationError``.  These are the regression guards.
* **Cross-field interactions** — combinations that touch multiple
  invariants at once (e.g. PROCEED + failed_invariants must reject).
"""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from crucible.modules.section_03_models_and_context import (
    AuditTrail,
    BranchSpec,
    Concern,
    ConsensusRiskReport,
    Disagreement,
    EvidenceRef,
    GateVerdict,
    SpecialistFinding,
)


# ── EvidenceRef ──────────────────────────────────────────────────────────────


class TestEvidenceRef:
    def test_minimum_payload_constructs(self) -> None:
        e = EvidenceRef(claim="BTC daily volume exceeded 30B on 2024-12-01")
        assert e.claim
        assert e.source_url == ""
        assert e.confidence == 0.5  # default

    def test_confidence_bounds_enforced(self) -> None:
        with pytest.raises(ValidationError):
            EvidenceRef(claim="ok", confidence=1.5)
        with pytest.raises(ValidationError):
            EvidenceRef(claim="ok", confidence=-0.1)

    def test_empty_claim_rejected(self) -> None:
        with pytest.raises(ValidationError):
            EvidenceRef(claim="")


# ── Concern ──────────────────────────────────────────────────────────────────


class TestConcern:
    def test_minor_severity_constructs(self) -> None:
        c = Concern(severity="minor", description="data is stale")
        assert c.severity == "minor"
        assert c.blocks_directions == []

    def test_unknown_severity_rejected(self) -> None:
        with pytest.raises(ValidationError):
            Concern(severity="catastrophic", description="x")  # type: ignore[arg-type]

    def test_empty_description_rejected(self) -> None:
        with pytest.raises(ValidationError):
            Concern(severity="material", description="")


# ── Disagreement ─────────────────────────────────────────────────────────────


class TestDisagreement:
    def test_minimum_payload_constructs(self) -> None:
        d = Disagreement(with_role="skeptic", point="evidence too thin")
        assert d.with_role == "skeptic"
        assert d.severity == "material"  # default

    def test_blocking_severity_allowed(self) -> None:
        d = Disagreement(
            with_role="judge", point="invariant violated", severity="blocking"
        )
        assert d.severity == "blocking"


# ── BranchSpec ───────────────────────────────────────────────────────────────


class TestBranchSpec:
    def test_minimum_payload_constructs(self) -> None:
        b = BranchSpec(direction_id="A", rationale="independent venue test")
        assert b.direction_id == "A"
        assert b.blocking_questions == []

    def test_empty_rationale_rejected(self) -> None:
        with pytest.raises(ValidationError):
            BranchSpec(direction_id="B", rationale="")


# ── AuditTrail ───────────────────────────────────────────────────────────────


class TestAuditTrail:
    def test_default_constructs(self) -> None:
        a = AuditTrail()
        assert a.audit_mode_enabled is False
        assert a.isolation_mode == "sequential"
        assert a.critic_model_family is None  # v1.1.8 invariant
        assert a.consensus_risk_threshold == 0.3

    def test_critic_model_family_can_be_set_for_v1_3_0(self) -> None:
        """v1.1.8 leaves the field at None but the schema must already
        accept a string so v1.3.0 doesn't need a migration."""
        a = AuditTrail(critic_model_family="claude")
        assert a.critic_model_family == "claude"


# ── SpecialistFinding ────────────────────────────────────────────────────────


def _minimal_finding(role: str = "explorer", **overrides):
    payload = {"role": role, "conclusion": "test conclusion", "confidence": 0.6}
    payload.update(overrides)
    return SpecialistFinding(**payload)


class TestSpecialistFinding:
    def test_minimum_payload_constructs(self) -> None:
        f = _minimal_finding()
        assert f.role == "explorer"
        assert f.concerns == []
        assert f.disagreement_with == []

    def test_unknown_role_rejected(self) -> None:
        with pytest.raises(ValidationError):
            SpecialistFinding(role="oracle", conclusion="x", confidence=0.5)  # type: ignore[arg-type]

    def test_confidence_bounds_enforced(self) -> None:
        with pytest.raises(ValidationError):
            _minimal_finding(confidence=1.5)
        with pytest.raises(ValidationError):
            _minimal_finding(confidence=-0.5)

    def test_failed_invariants_only_for_judge_or_critic(self) -> None:
        # Non-judge/critic role with failed_invariants must reject.
        for bad_role in ("explorer", "comparator", "skeptic", "evidence_auditor"):
            with pytest.raises(ValidationError):
                _minimal_finding(
                    role=bad_role,
                    failed_invariants=["strategy assumes cointegration"],
                )

    def test_failed_invariants_allowed_for_judge(self) -> None:
        f = _minimal_finding(
            role="judge",
            failed_invariants=["mean-reversion on trending asset is invalid"],
        )
        assert f.failed_invariants == [
            "mean-reversion on trending asset is invalid"
        ]

    def test_failed_invariants_allowed_for_critic(self) -> None:
        f = _minimal_finding(
            role="critic",
            failed_invariants=["data leakage in feature engineering"],
        )
        assert f.role == "critic"

    def test_empty_conclusion_rejected(self) -> None:
        with pytest.raises(ValidationError):
            SpecialistFinding(role="explorer", conclusion="", confidence=0.5)


# ── ConsensusRiskReport ──────────────────────────────────────────────────────


class TestConsensusRiskReport:
    def test_minimum_payload_constructs(self) -> None:
        r = ConsensusRiskReport(
            groupthink_score=0.2,
            concern_diversity=0.8,
            assumption_overlap=0.1,
            confidence_variance=0.05,
        )
        assert r.flags == []

    def test_score_bounds_enforced(self) -> None:
        with pytest.raises(ValidationError):
            ConsensusRiskReport(
                groupthink_score=1.5,
                concern_diversity=0.5,
                assumption_overlap=0.5,
                confidence_variance=0.1,
            )

    def test_confidence_variance_non_negative(self) -> None:
        with pytest.raises(ValidationError):
            ConsensusRiskReport(
                groupthink_score=0.5,
                concern_diversity=0.5,
                assumption_overlap=0.5,
                confidence_variance=-0.1,
            )


# ── GateVerdict — the v1.1.8 contract ────────────────────────────────────────


_LONG_REASON = "a" * 30  # ≥20 chars


class TestGateVerdictProceed:
    def test_proceed_with_direction_constructs(self) -> None:
        v = GateVerdict(
            decision="PROCEED",
            selected_direction="A",
            reason=_LONG_REASON,
        )
        assert v.decision == "PROCEED"

    def test_proceed_without_direction_rejected(self) -> None:
        with pytest.raises(ValidationError):
            GateVerdict(decision="PROCEED", reason=_LONG_REASON)

    def test_proceed_with_none_direction_rejected(self) -> None:
        with pytest.raises(ValidationError):
            GateVerdict(
                decision="PROCEED",
                selected_direction="none",
                reason=_LONG_REASON,
            )

    def test_proceed_with_invalid_direction_rejected(self) -> None:
        with pytest.raises(ValidationError):
            GateVerdict(
                decision="PROCEED",
                selected_direction="Z",
                reason=_LONG_REASON,
            )

    def test_proceed_with_failed_invariants_rejected(self) -> None:
        """v1.1.8 contract: PROCEED cannot carry KILL fields."""
        with pytest.raises(ValidationError):
            GateVerdict(
                decision="PROCEED",
                selected_direction="A",
                reason=_LONG_REASON,
                failed_invariants=["should not be here"],
            )

    def test_proceed_with_blocking_queries_rejected(self) -> None:
        """v1.1.8 contract: PROCEED cannot carry NEEDS_MORE_DATA fields."""
        with pytest.raises(ValidationError):
            GateVerdict(
                decision="PROCEED",
                selected_direction="A",
                reason=_LONG_REASON,
                blocking_evidence_queries=["should not be here"],
            )


class TestGateVerdictKill:
    def test_kill_with_invariants_constructs(self) -> None:
        v = GateVerdict(
            decision="KILL",
            reason=_LONG_REASON,
            failed_invariants=["mean-reversion on a trending asset"],
        )
        assert v.failed_invariants == ["mean-reversion on a trending asset"]
        assert v.selected_direction is None

    def test_kill_without_invariants_rejected(self) -> None:
        """v1.1.8 contract: KILL must cite at least one failed invariant.
        This is the structural enforcement that prevents an LLM from
        silently downgrading a hard KILL into a vague NEEDS_MORE_DATA."""
        with pytest.raises(ValidationError):
            GateVerdict(decision="KILL", reason=_LONG_REASON)

    def test_kill_with_empty_invariants_list_rejected(self) -> None:
        with pytest.raises(ValidationError):
            GateVerdict(
                decision="KILL", reason=_LONG_REASON, failed_invariants=[]
            )

    def test_kill_with_selected_direction_rejected(self) -> None:
        """v1.1.8 contract: KILL must NOT also select a direction."""
        with pytest.raises(ValidationError):
            GateVerdict(
                decision="KILL",
                selected_direction="B",
                reason=_LONG_REASON,
                failed_invariants=["x"],
            )

    def test_kill_with_blocking_queries_rejected(self) -> None:
        """KILL and NEEDS_MORE_DATA fields must be mutually exclusive."""
        with pytest.raises(ValidationError):
            GateVerdict(
                decision="KILL",
                reason=_LONG_REASON,
                failed_invariants=["x"],
                blocking_evidence_queries=["should not coexist"],
            )


class TestGateVerdictNeedsMoreData:
    def test_needs_more_data_with_queries_constructs(self) -> None:
        v = GateVerdict(
            decision="NEEDS_MORE_DATA",
            reason=_LONG_REASON,
            blocking_evidence_queries=["fetch venue depth from binance"],
        )
        assert v.blocking_evidence_queries[0] == "fetch venue depth from binance"

    def test_needs_more_data_without_queries_rejected(self) -> None:
        with pytest.raises(ValidationError):
            GateVerdict(decision="NEEDS_MORE_DATA", reason=_LONG_REASON)

    def test_needs_more_data_with_empty_queries_rejected(self) -> None:
        with pytest.raises(ValidationError):
            GateVerdict(
                decision="NEEDS_MORE_DATA",
                reason=_LONG_REASON,
                blocking_evidence_queries=[],
            )

    def test_needs_more_data_with_failed_invariants_rejected(self) -> None:
        """v1.1.8 contract: NEEDS_MORE_DATA is for evidence gaps; KILL is
        for hard violations.  Mixing them defeats the structural separation."""
        with pytest.raises(ValidationError):
            GateVerdict(
                decision="NEEDS_MORE_DATA",
                reason=_LONG_REASON,
                blocking_evidence_queries=["x"],
                failed_invariants=["should not coexist"],
            )


class TestGateVerdictBranch:
    def _branch(self, did: str) -> BranchSpec:
        return BranchSpec(direction_id=did, rationale="independent test")

    def test_branch_with_two_paths_constructs(self) -> None:
        v = GateVerdict(
            decision="BRANCH",
            reason=_LONG_REASON,
            branched_paths=[self._branch("A"), self._branch("B")],
        )
        assert len(v.branched_paths) == 2

    def test_branch_with_zero_paths_rejected(self) -> None:
        with pytest.raises(ValidationError):
            GateVerdict(decision="BRANCH", reason=_LONG_REASON)

    def test_branch_with_one_path_rejected(self) -> None:
        """v1.1.8 contract: BRANCH means ≥2 distinct sub-paths; a single
        branch is just PROCEED."""
        with pytest.raises(ValidationError):
            GateVerdict(
                decision="BRANCH",
                reason=_LONG_REASON,
                branched_paths=[self._branch("A")],
            )

    def test_branch_with_failed_invariants_rejected(self) -> None:
        with pytest.raises(ValidationError):
            GateVerdict(
                decision="BRANCH",
                reason=_LONG_REASON,
                branched_paths=[self._branch("A"), self._branch("B")],
                failed_invariants=["should not be here"],
            )


class TestGateVerdictCommon:
    def test_unknown_decision_rejected(self) -> None:
        with pytest.raises(ValidationError):
            GateVerdict(decision="MAYBE", reason=_LONG_REASON)  # type: ignore[arg-type]

    def test_short_reason_rejected(self) -> None:
        """Reason must be ≥20 chars so the audit log is not a wasteland of
        empty placeholders.  Pinned here so this minimum-length cannot be
        relaxed without an explicit migration."""
        with pytest.raises(ValidationError):
            GateVerdict(
                decision="PROCEED", selected_direction="A", reason="too short"
            )

    def test_findings_default_empty_list(self) -> None:
        v = GateVerdict(
            decision="PROCEED",
            selected_direction="A",
            reason=_LONG_REASON,
        )
        assert v.findings == []

    def test_audit_trail_default_present(self) -> None:
        v = GateVerdict(
            decision="PROCEED",
            selected_direction="A",
            reason=_LONG_REASON,
        )
        # AuditTrail is always present (default factory) even when audit
        # mode was off — readers should not have to None-check.
        assert v.audit_trail is not None
        assert v.audit_trail.audit_mode_enabled is False
