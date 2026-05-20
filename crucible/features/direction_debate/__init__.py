"""
crucible.features.direction_debate
===================================

v1.1.8 — Direction Debate Audit Mode utilities.

This package adds two capabilities on top of the existing Stage 0 direction
debate orchestration in ``section_02_research_and_llm`` /
``section_04_web_research_and_direction``:

1.  **Deterministic consensus-risk computation** (:mod:`.consensus`)
    — analyses a list of :class:`SpecialistFinding` objects emitted by the
    five-agent crew (Explorer, Comparator, Skeptic, Evidence Auditor, Judge)
    and produces a :class:`ConsensusRiskReport` that captures groupthink
    signals (zero recorded disagreement, low concern-diversity, near-uniform
    high confidence, etc.).  All metrics are deterministic, token-based, and
    embedding-free so v1.2.0 retrieval can recompute them from stored ledger
    events without re-running any LLM.

2.  **External Critic** (:mod:`.critic`)
    — an opt-in sixth agent (gated by ``CRUCIBLE_DEBATE_EXTERNAL_CRITIC=1``)
    that re-judges the Judge's verdict using *only* the raw research evidence
    plus the Judge's terminal decision token + reason.  The Critic does NOT
    see any other agent's chain-of-thought, so it is isolated from the
    sequential-anchoring risk that affects the Explorer→Judge pipeline.  Its
    output is a fresh :class:`GateVerdict`; the orchestrator decides whether
    to let the Critic override the Judge based on
    ``CRUCIBLE_DEBATE_CRITIC_OVERRIDE_PROCEED`` (default ``0`` — Critic only
    records dissent; Judge verdict stands).

Both capabilities are additive — sequential default behaviour is preserved
when ``CRUCIBLE_DEBATE_AUDIT_MODE=0`` (default).  See ``CLAUDE.md § 11`` for
the v1.1.8 invariants every contributor must respect.
"""
from __future__ import annotations

from .consensus import compute_consensus_risk
from .critic import (
    CriticUnavailableError,
    build_critic_prompt,
    validate_direction_verdict,
)

__all__ = [
    "compute_consensus_risk",
    "validate_direction_verdict",
    "build_critic_prompt",
    "CriticUnavailableError",
]
