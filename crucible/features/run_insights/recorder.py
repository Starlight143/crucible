"""
features/run_insights/recorder.py
==================================
Public API of the Run Insights ledger.

The :class:`InsightsRecorder` is a thin orchestrator that:

1. Reads operator env flags via :mod:`crucible._env` to decide what to record.
2. Builds :class:`schema.InsightEvent` instances with content_id + signals.
3. Runs payloads through :mod:`redact` before serialisation.
4. Delegates persistence to a :class:`backends.StorageBackend`.

Public entry points:

* :func:`get_recorder` — process-global lazy singleton.
* :func:`reset_recorder` — start a fresh recorder (used by tests / a new
  pipeline run that must not see the previous run's in-memory state).
* :func:`InsightsRecorder.record_output_method`,
  :func:`record_error`, :func:`record_direction_debate_rejection`,
  :func:`record_runtime_params` — the four call-site emitters.

Mode-aware ``runtime_params`` recording
---------------------------------------
``CRUCIBLE_RUN_INSIGHTS_RECORD_PARAMS=auto`` (the default) records parameters
only for Quant runs; SaaS / Agent / Scientist runs skip ``runtime_params``
events.  ``=1`` / ``=0`` force-overrides.  Typos return ``auto`` (no silent
truthy coercion), matching the project's env-bool whitelist rule.
"""
from __future__ import annotations

import os
import threading
from typing import Any, Dict, List, Mapping, Optional, Tuple

# Tri-modal import: the section modules are loaded under three distinct
# package layouts depending on how the entry point was launched:
#   1. ``python -m crucible`` / WebUI flask  → __package__ = "crucible.features.run_insights"
#   2. ``python crucible/__main__.py``       → flat layout; __package__ = "features.run_insights"
#   3. Bare-module pytest runs               → matches layout 1.
# Cover both by trying the package-relative import first, then a flat fallback.
try:
    from ..._env import env_bool, env_int, env_str
    from ...runtime_logging import get_logger
except ImportError:  # pragma: no cover — flat-launcher fallback
    from _env import env_bool, env_int, env_str  # type: ignore[no-redef]
    from runtime_logging import get_logger  # type: ignore[no-redef]

from .backends import StorageBackend, make_backend
from .redact import redact_event_payload, redact_signals
from .schema import (
    EventKind,
    InsightEvent,
    OutcomeStatus,
    build_env_fingerprint,
    compute_content_id,
    extract_signals,
    truncate_text,
)

LOGGER = get_logger(__name__)

# ── Defaults ─────────────────────────────────────────────────────────────────

_DEFAULT_DIR = ".crucible_insights"
_DEFAULT_INLINE_MAX_BYTES = 4096
_DEFAULT_MAX_ENTRIES_PER_STREAM = 2000

# Quant canonical name (lowercased) — used by the auto-params rule.
_QUANT_MODE = "quant"


# ── Helpers ──────────────────────────────────────────────────────────────────

def _resolve_record_params(mode: str) -> bool:
    """Decide whether to record ``runtime_params`` for *mode*.

    Honours ``CRUCIBLE_RUN_INSIGHTS_RECORD_PARAMS``:

    * ``auto`` (default, also fallback for any unrecognised token): record
      only when *mode* canonicalises to ``Quant``.
    * ``1`` / ``true`` / ``yes`` / ``on``: always record.
    * ``0`` / ``false`` / ``no`` / ``off``: never record.

    Typos (e.g. ``"atuo"``) silently return ``auto`` rather than coercing
    to truthy — matches the project env-bool whitelist rule.
    """
    raw = env_str("CRUCIBLE_RUN_INSIGHTS_RECORD_PARAMS", "auto").lower()
    if raw in {"1", "true", "yes", "on"}:
        return True
    if raw in {"0", "false", "no", "off"}:
        return False
    # auto / typo
    return str(mode or "").strip().lower() == _QUANT_MODE


def _resolve_record_debate_finding() -> bool:
    """Decide whether to record per-specialist ``debate_finding`` events.

    Honours ``CRUCIBLE_RUN_INSIGHTS_RECORD_DEBATE_FINDING``:

    * ``auto`` (default, also fallback for typos): record only when
      ``CRUCIBLE_DEBATE_AUDIT_MODE=1`` is set.  Findings are useless
      without audit_mode populating the structured-finding fields, so
      ``auto`` follows audit_mode by design.
    * ``1`` / ``true`` / ``yes`` / ``on``: always record (even outside
      audit_mode; the orchestrator may emit minimal-payload findings).
    * ``0`` / ``false`` / ``no`` / ``off``: never record.

    Typos return ``auto`` per the project env-bool whitelist rule.  The
    "auto follows audit_mode" semantic matches ``runtime_params``'s
    "auto means Quant only" — keep the precedent consistent.
    """
    raw = env_str("CRUCIBLE_RUN_INSIGHTS_RECORD_DEBATE_FINDING", "auto").lower()
    if raw in {"1", "true", "yes", "on"}:
        return True
    if raw in {"0", "false", "no", "off"}:
        return False
    # auto / typo → follow CRUCIBLE_DEBATE_AUDIT_MODE
    return env_bool("CRUCIBLE_DEBATE_AUDIT_MODE", False)


def _outcome_dict(
    status: OutcomeStatus,
    score: Optional[float] = None,
    note: Optional[str] = None,
) -> Dict[str, Any]:
    out: Dict[str, Any] = {"status": status.value}
    if score is not None:
        try:
            score_f = float(score)
            if -1e-9 <= score_f <= 1.0 + 1e-9:
                out["score"] = max(0.0, min(1.0, score_f))
        except (TypeError, ValueError):
            pass
    if note:
        out["note"] = truncate_text(str(note), 200)
    return out


def _normalise_mode(mode: Optional[str]) -> str:
    """Canonicalise mode to one of ``Quant|SaaS|Agent|Scientist`` (or
    pass-through if unrecognised).  Used to keep ``event.mode`` consistent
    regardless of how the call site spells it.
    """
    if not mode:
        return ""
    canon = {
        "quant": "Quant", "saas": "SaaS",
        "agent": "Agent", "scientist": "Scientist",
    }
    return canon.get(str(mode).strip().lower(), str(mode))


# ── Recorder ──────────────────────────────────────────────────────────────────

class InsightsRecorder:
    """Thread-safe ledger orchestrator.

    Per-stream FIFO pruning kicks in every ``_prune_check_interval`` events
    so we don't read+rewrite the file on every append.  Threshold is
    ``MAX_ENTRIES_PER_STREAM * 1.25`` (25 % headroom keeps prune cost
    amortised at < 1 %).
    """

    _prune_check_interval = 50

    def __init__(
        self,
        backend: StorageBackend,
        *,
        max_entries_per_stream: int = _DEFAULT_MAX_ENTRIES_PER_STREAM,
        inline_max_bytes: int = _DEFAULT_INLINE_MAX_BYTES,
    ) -> None:
        self._backend = backend
        self._max_entries = max_entries_per_stream
        self._inline_max_bytes = inline_max_bytes
        self._writes_since_prune: Dict[str, int] = {}
        self._lock = threading.Lock()
        # v1.1.2 (audit fix G2-B-HIGH-1): split the single ``_warned_once``
        # flag into two independent channels so a benign debate event with an
        # unrecognised ``rejection_reason`` no longer permanently mutes the
        # critical emit-failure warning (and vice versa).  Prior to this fix
        # the first ``unrecognised reason`` log silently disabled the only
        # signal for every subsequent canonical-json / disk-full / backend
        # exception during the same process lifetime.
        self._warned_unknown_reason = False
        # v1.1.11 (audit fix): separate one-shot channel for unrecognised gate
        # *decisions* (record_gate_verdict).  Previously this reused
        # ``_warned_unknown_reason``, re-coupling the two channels the v1.1.2
        # G2-B-HIGH-1 fix deliberately split — one unknown rejection_reason
        # would permanently mute the unknown-decision log and vice versa.
        self._warned_unknown_decision = False
        self._warned_emit_failed = False

    # -- public emitters --------------------------------------------------------

    def record_output_method(
        self,
        *,
        run_id: str,
        project_name: str,
        mode: str,
        user_problem: Optional[str] = None,
        run_meta: Optional[Mapping[str, Any]] = None,
        validation_verdict: Optional[str] = None,
        entrypoint: Optional[str] = None,
        artefact_names: Optional[List[str]] = None,
        outcome_score: Optional[float] = None,
        outcome_status: OutcomeStatus = OutcomeStatus.SUCCESS,
        extra_signals: Optional[List[str]] = None,
        # v1.1.0 fifth-pass (G-20): propagate backtest data provenance
        # into the ledger so v1.2.0 retrieval can filter out synthetic
        # runs without re-opening ``backtest_report.json`` on disk for
        # every ledger row.  ``data_source`` ∈ {existing, yfinance,
        # binance, ccxt, project_provider, synthetic}; ``data_actual_symbol``
        # tells retrieval which actual asset was traded (the symbol
        # may differ from the requested one — e.g. BTC/USD resolved to
        # BTC-USD via yfinance).  Both are optional — non-Quant modes
        # pass None and the field is omitted from the payload.
        data_source: Optional[str] = None,
        data_actual_symbol: Optional[str] = None,
    ) -> Optional[str]:
        """Emit an ``output_method`` event after ``save_project_output``.

        Returns the persisted ``content_id`` or ``None`` if the event was
        skipped (subsystem disabled, individual flag off, or write failed).
        """
        if not env_bool("CRUCIBLE_RUN_INSIGHTS_RECORD_OUTPUT", True):
            return None
        if not self._enabled():
            return None

        meta = dict(run_meta or {})
        model_id = meta.get("model_id") or meta.get("primary_model_id")
        llm_provider = meta.get("llm_provider")

        payload: Dict[str, Any] = {
            "primary_model_id": model_id,
            "direction_judge_model_id": meta.get("direction_judge_model_id"),
            "librarian_model_id": meta.get("librarian_model_id"),
            "framework": meta.get("preferred_framework") or meta.get("framework"),
            "validation_verdict": validation_verdict,
            "entrypoint": entrypoint,
            "artefact_names": list(artefact_names or []),
        }
        # Optional data-provenance fields (Quant mode only).
        if data_source:
            payload["data_source"] = str(data_source)
        if data_actual_symbol:
            payload["data_actual_symbol"] = str(data_actual_symbol)
        # Mirror data_source into signals so retrieval can filter
        # without parsing the payload dict.
        if data_source:
            extra_signals = list(extra_signals or [])
            extra_signals.append(f"data_source:{data_source}")

        reusability: Optional[Dict[str, Any]] = None
        if outcome_status == OutcomeStatus.SUCCESS and outcome_score is not None:
            try:
                if float(outcome_score) >= 0.7:
                    reusability = {
                        "trigger_signals": list(
                            extract_signals(
                                mode=mode,
                                user_problem=user_problem,
                                run_meta=meta,
                                extra=extra_signals,
                            )
                        ),
                        "applicable_modes": [_normalise_mode(mode)],
                        "confidence": float(outcome_score),
                        "skill_kind": "direction_template",
                    }
            except (TypeError, ValueError):
                reusability = None

        return self._emit(
            kind=EventKind.OUTPUT_METHOD,
            stage="save_output",
            run_id=run_id,
            project_name=project_name,
            mode=mode,
            user_problem=user_problem,
            run_meta=meta,
            payload=payload,
            outcome=_outcome_dict(outcome_status, outcome_score),
            reusability=reusability,
            extra_signals=extra_signals,
        )

    def record_error(
        self,
        *,
        run_id: str,
        project_name: str,
        mode: str,
        stage: str,
        exception_class: str,
        message: Optional[str] = None,
        retry_count: int = 0,
        gate_decision: Optional[Mapping[str, Any]] = None,
        downgraded_provider: Optional[str] = None,
        user_problem: Optional[str] = None,
        run_meta: Optional[Mapping[str, Any]] = None,
        extra_signals: Optional[List[str]] = None,
    ) -> Optional[str]:
        """Emit an ``error_record`` event from an exception / retry-exhausted
        / gate-rejected code path."""
        if not env_bool("CRUCIBLE_RUN_INSIGHTS_RECORD_ERRORS", True):
            return None
        if not self._enabled():
            return None

        payload: Dict[str, Any] = {
            "exception_class": str(exception_class or "Exception"),
            "message_head": truncate_text(message, 300),
            "retry_count": max(0, int(retry_count)),
        }
        if gate_decision:
            payload["gate_decision"] = dict(gate_decision)
        if downgraded_provider:
            payload["downgraded_provider"] = str(downgraded_provider)

        return self._emit(
            kind=EventKind.ERROR_RECORD,
            stage=stage or "unknown",
            run_id=run_id,
            project_name=project_name,
            mode=mode,
            user_problem=user_problem,
            run_meta=run_meta,
            payload=payload,
            outcome=_outcome_dict(OutcomeStatus.FAILURE),
            extra_signals=extra_signals,
        )

    def record_direction_debate_rejection(
        self,
        *,
        run_id: str,
        project_name: str,
        mode: str,
        direction_id: str,
        rejection_reason: str,
        judge_verdict: Optional[str] = None,
        attempt: int = 1,
        user_problem: Optional[str] = None,
        run_meta: Optional[Mapping[str, Any]] = None,
        extra_signals: Optional[List[str]] = None,
    ) -> Optional[str]:
        """Emit a ``direction_debate_rejection`` event when Stage 0 produces
        no winner or force-nones a direction."""
        if not env_bool("CRUCIBLE_RUN_INSIGHTS_RECORD_DEBATE", True):
            return None
        if not self._enabled():
            return None

        # Validate rejection_reason against a known enum-like set; unknown
        # values are kept verbatim but logged once (helps future-proofing).
        # v1.1.8: extended with audit-mode reasons (``judge_explicit_kill``,
        # ``judge_branch``, ``needs_more_data``) so the new orchestration
        # paths in section_02 (which translate GateVerdict.decision back to
        # the legacy rejection event for back-compat) do not trip the
        # one-shot ``unrecognised reason`` log noise.
        known = {
            "force_none", "insufficient_evidence", "skeptic_rejected",
            "auditor_blocked", "judge_no_winner",
            "judge_explicit_kill", "judge_branch", "needs_more_data",
        }
        reason = str(rejection_reason or "").strip().lower() or "judge_no_winner"
        if reason not in known and not self._warned_unknown_reason:
            LOGGER.debug(
                "run_insights: unrecognised rejection_reason=%r (recording verbatim)",
                rejection_reason,
            )
            self._warned_unknown_reason = True

        payload: Dict[str, Any] = {
            "direction_id": str(direction_id or "unknown"),
            "rejection_reason": reason,
            "judge_verdict_excerpt": truncate_text(judge_verdict, 500),
            "attempt": max(1, int(attempt)),
        }

        return self._emit(
            kind=EventKind.DIRECTION_DEBATE_REJECTION,
            stage="stage0_direction",
            run_id=run_id,
            project_name=project_name,
            mode=mode,
            user_problem=user_problem,
            run_meta=run_meta,
            payload=payload,
            outcome=_outcome_dict(OutcomeStatus.FAILURE, note=reason),
            extra_signals=extra_signals,
        )

    # ------------------------------------------------------------------------
    # v1.1.8 — Direction Debate Audit Mode emitters
    # ------------------------------------------------------------------------
    # These two methods together capture the "disagreement log" Mira Chen's
    # feedback identified as the most-valuable gate output.  Both share the
    # existing ``debate`` JSONL stream (see ``schema.EventKind.stream_name``).
    # Both are swallow-only: a failing backend MUST NOT break the main
    # pipeline, since the audit ledger is an observer not a critical path.

    def record_debate_finding(
        self,
        *,
        run_id: str,
        project_name: str,
        mode: str,
        role: str,
        conclusion: str,
        confidence: Optional[float] = None,
        assumptions: Optional[List[str]] = None,
        supporting_evidence: Optional[List[Mapping[str, Any]]] = None,
        concerns: Optional[List[Mapping[str, Any]]] = None,
        disagreement_with: Optional[List[Mapping[str, Any]]] = None,
        missing_information: Optional[List[str]] = None,
        failed_invariants: Optional[List[str]] = None,
        attempt: int = 1,
        user_problem: Optional[str] = None,
        run_meta: Optional[Mapping[str, Any]] = None,
        extra_signals: Optional[List[str]] = None,
    ) -> Optional[str]:
        """Emit a ``direction_debate_finding`` event for one specialist.

        Gated by:

        1. ``CRUCIBLE_RUN_INSIGHTS_ENABLED`` master switch
        2. ``CRUCIBLE_RUN_INSIGHTS_RECORD_DEBATE`` (existing per-stream)
        3. ``_resolve_record_debate_finding()`` which honours
           ``CRUCIBLE_RUN_INSIGHTS_RECORD_DEBATE_FINDING`` and falls back
           to following ``CRUCIBLE_DEBATE_AUDIT_MODE`` on ``auto``.

        Best-effort; never raises.
        """
        if not env_bool("CRUCIBLE_RUN_INSIGHTS_RECORD_DEBATE", True):
            return None
        if not _resolve_record_debate_finding():
            return None
        if not self._enabled():
            return None

        # Coerce confidence into [0, 1] without silent saturation — bound
        # checks happen at SpecialistFinding construction time, but call sites
        # that hand-roll the dict (e.g. test fixtures) shouldn't be able to
        # poison the ledger with NaN / out-of-range.  ``None`` is OK.
        conf_clean: Optional[float] = None
        if confidence is not None:
            try:
                conf_f = float(confidence)
                # Per CLAUDE.md (global) numerical-correctness rule: NaN /
                # Inf MUST be rejected, not silently clamped.
                import math as _math
                if _math.isfinite(conf_f) and 0.0 <= conf_f <= 1.0:
                    conf_clean = conf_f
            except (TypeError, ValueError):
                conf_clean = None

        payload: Dict[str, Any] = {
            "role": str(role or "unknown"),
            "conclusion_excerpt": truncate_text(conclusion, 1000),
            "confidence": conf_clean,
            "assumptions": [str(s) for s in (assumptions or [])][:20],
            "supporting_evidence": [dict(e) for e in (supporting_evidence or [])][:20],
            "concerns": [dict(c) for c in (concerns or [])][:20],
            "disagreement_with": [dict(d) for d in (disagreement_with or [])][:20],
            "missing_information": [str(s) for s in (missing_information or [])][:20],
            "failed_invariants": [str(s) for s in (failed_invariants or [])][:20],
            "attempt": max(1, int(attempt)),
        }

        # Successful finding emit when conclusion is non-empty.  We don't have
        # a binary "this finding succeeded" notion — every finding contributes
        # to the audit trail regardless of the agent's confidence.  Use
        # SKIPPED status so retrieval doesn't confuse it with success/failure
        # outcomes; the conclusion text is what matters.
        return self._emit(
            kind=EventKind.DIRECTION_DEBATE_FINDING,
            stage="stage0_direction",
            run_id=run_id,
            project_name=project_name,
            mode=mode,
            user_problem=user_problem,
            run_meta=run_meta,
            payload=payload,
            outcome=_outcome_dict(OutcomeStatus.SKIPPED, score=conf_clean),
            extra_signals=extra_signals,
        )

    def record_gate_verdict(
        self,
        *,
        run_id: str,
        project_name: str,
        mode: str,
        decision: str,
        reason: str,
        selected_direction: Optional[str] = None,
        branched_paths: Optional[List[Mapping[str, Any]]] = None,
        failed_invariants: Optional[List[str]] = None,
        blocking_evidence_queries: Optional[List[str]] = None,
        consensus_risk: Optional[Mapping[str, Any]] = None,
        audit_trail: Optional[Mapping[str, Any]] = None,
        attempt: int = 1,
        user_problem: Optional[str] = None,
        run_meta: Optional[Mapping[str, Any]] = None,
        extra_signals: Optional[List[str]] = None,
    ) -> Optional[str]:
        """Emit a ``direction_debate_verdict`` event with the gate's terminal
        decision (PROCEED / BRANCH / KILL / NEEDS_MORE_DATA).

        Gated by:

        1. ``CRUCIBLE_RUN_INSIGHTS_ENABLED`` master switch
        2. ``CRUCIBLE_RUN_INSIGHTS_RECORD_DEBATE`` (existing per-stream)
        3. ``CRUCIBLE_RUN_INSIGHTS_RECORD_GATE_VERDICT`` (defaults to ``1``;
           verdicts are cheap to record and the single most-valuable signal
           for future retrieval).

        Best-effort; never raises.
        """
        if not env_bool("CRUCIBLE_RUN_INSIGHTS_RECORD_DEBATE", True):
            return None
        if not env_bool("CRUCIBLE_RUN_INSIGHTS_RECORD_GATE_VERDICT", True):
            return None
        if not self._enabled():
            return None

        decision_clean = str(decision or "").strip().upper()
        known_decisions = {"PROCEED", "BRANCH", "KILL", "NEEDS_MORE_DATA"}
        if decision_clean not in known_decisions:
            # Unknown decision token gets recorded verbatim with a structural
            # warning — same one-shot log discipline as rejection_reason, but
            # on its OWN channel (``_warned_unknown_decision``) so it does not
            # share state with the rejection_reason warning (v1.1.11 audit fix).
            if not self._warned_unknown_decision:
                LOGGER.debug(
                    "run_insights: unrecognised gate decision=%r (recording verbatim)",
                    decision,
                )
                self._warned_unknown_decision = True

        # Status mapping: PROCEED is SUCCESS; KILL is FAILURE; NEEDS_MORE_DATA
        # is PARTIAL (not failure — the gate is awaiting input); BRANCH is
        # SUCCESS (we picked branched_paths[0] as the current PROCEED).
        status_map = {
            "PROCEED": OutcomeStatus.SUCCESS,
            "BRANCH": OutcomeStatus.SUCCESS,
            "KILL": OutcomeStatus.FAILURE,
            "NEEDS_MORE_DATA": OutcomeStatus.PARTIAL,
        }
        outcome_status = status_map.get(decision_clean, OutcomeStatus.PARTIAL)

        payload: Dict[str, Any] = {
            "decision": decision_clean or str(decision or ""),
            "reason_excerpt": truncate_text(reason, 1000),
            "selected_direction": str(selected_direction) if selected_direction else None,
            "branched_paths": [dict(b) for b in (branched_paths or [])][:8],
            "failed_invariants": [str(s) for s in (failed_invariants or [])][:20],
            "blocking_evidence_queries": [
                str(s) for s in (blocking_evidence_queries or [])
            ][:20],
            "consensus_risk": dict(consensus_risk) if consensus_risk else None,
            "audit_trail": dict(audit_trail) if audit_trail else None,
            "attempt": max(1, int(attempt)),
        }

        return self._emit(
            kind=EventKind.DIRECTION_DEBATE_VERDICT,
            stage="stage0_direction",
            run_id=run_id,
            project_name=project_name,
            mode=mode,
            user_problem=user_problem,
            run_meta=run_meta,
            payload=payload,
            outcome=_outcome_dict(outcome_status, note=decision_clean),
            extra_signals=extra_signals,
        )

    def record_runtime_params(
        self,
        *,
        run_id: str,
        project_name: str,
        mode: str,
        cli_flags: Optional[Mapping[str, Any]] = None,
        gate_config: Optional[Mapping[str, Any]] = None,
        budget_policy: Optional[Mapping[str, Any]] = None,
        user_problem: Optional[str] = None,
        run_meta: Optional[Mapping[str, Any]] = None,
        extra_signals: Optional[List[str]] = None,
    ) -> Optional[str]:
        """Emit a ``runtime_params`` event.  Gated by the mode-aware
        ``CRUCIBLE_RUN_INSIGHTS_RECORD_PARAMS`` rule (Quant-only by default).
        """
        if not self._enabled():
            return None
        if not _resolve_record_params(mode):
            return None

        payload: Dict[str, Any] = {
            "mode": _normalise_mode(mode),
            "llm_provider": (run_meta or {}).get("llm_provider"),
            "cli_flags": dict(cli_flags or {}),
            "gate_config": dict(gate_config or {}),
            "budget_policy": dict(budget_policy or {}),
        }

        return self._emit(
            kind=EventKind.RUNTIME_PARAMS,
            stage="save_output",
            run_id=run_id,
            project_name=project_name,
            mode=mode,
            user_problem=user_problem,
            run_meta=run_meta,
            payload=payload,
            outcome=_outcome_dict(OutcomeStatus.SKIPPED),
            extra_signals=extra_signals,
        )

    # -- v1.1.8 extended — librarian observability + gate degrade ---------

    def record_provider_cooldown(
        self,
        *,
        run_id: str,
        project_name: str,
        mode: str,
        provider: str,
        cooldown_seconds: int,
        trigger_reason: str,
        trigger_count: int,
        user_problem: Optional[str] = None,
        run_meta: Optional[Mapping[str, Any]] = None,
        extra_signals: Optional[List[str]] = None,
    ) -> Optional[str]:
        """Emit a ``provider_cooldown_engaged`` event (v1.1.8 Phase 2, Q2).

        Records that a librarian provider hit a rate limit (429) or bot
        detection (202) and entered cooldown.  Routes to the ``error``
        stream because cooldowns are transient librarian failures.

        Gated by ``CRUCIBLE_RUN_INSIGHTS_RECORD_ERRORS`` (per-stream).
        Best-effort; never raises.
        """
        if not env_bool("CRUCIBLE_RUN_INSIGHTS_RECORD_ERRORS", True):
            return None
        if not self._enabled():
            return None
        payload: Dict[str, Any] = {
            "provider": str(provider or "unknown"),
            "cooldown_seconds": max(0, int(cooldown_seconds)),
            "trigger_reason": str(trigger_reason or "")[:200],
            "trigger_count": max(0, int(trigger_count)),
        }
        return self._emit(
            kind=EventKind.PROVIDER_COOLDOWN_ENGAGED,
            stage="librarian_research",
            run_id=run_id,
            project_name=project_name,
            mode=mode,
            user_problem=user_problem,
            run_meta=run_meta,
            payload=payload,
            outcome=_outcome_dict(
                OutcomeStatus.PARTIAL,
                note=f"provider_cooldown:{provider}:{cooldown_seconds}s",
            ),
            extra_signals=extra_signals,
        )

    def record_provider_health_summary(
        self,
        *,
        run_id: str,
        project_name: str,
        mode: str,
        counters: Mapping[str, Mapping[str, int]],
        cooldown_snapshot: Optional[Mapping[str, Mapping[str, Any]]] = None,
        user_problem: Optional[str] = None,
        run_meta: Optional[Mapping[str, Any]] = None,
        extra_signals: Optional[List[str]] = None,
    ) -> Optional[str]:
        """Emit a ``provider_health_summary`` event at end of librarian
        stage (v1.1.8 Phase 2, Q7).

        Aggregates per-provider counter snapshot + cooldown snapshot.
        Routes to ``output`` stream because the summary is an
        end-of-stage observability checkpoint, not a failure.

        Gated by ``CRUCIBLE_RUN_INSIGHTS_RECORD_OUTPUT`` (per-stream).
        Best-effort; never raises.
        """
        if not env_bool("CRUCIBLE_RUN_INSIGHTS_RECORD_OUTPUT", True):
            return None
        if not self._enabled():
            return None
        # Aggregate totals across providers — useful for v1.2.0
        # retrieval to spot under-yielding runs at a glance.
        total_requests = 0
        total_ok = 0
        total_citations = 0
        for c in (counters or {}).values():
            if not c:
                continue
            total_requests += int(c.get("requests", 0))
            total_ok += int(c.get("ok_200", 0))
            total_citations += int(c.get("citations_yielded", 0))
        # _outcome_dict clamps score to [0, 1] (see CLAUDE.md numerical
        # correctness rule).  Use success rate as the score; absolute
        # counts live in payload.totals where they belong.
        success_rate = (
            float(total_ok) / float(total_requests)
            if total_requests > 0
            else 0.0
        )
        # Clamp defensively (success_rate should already be [0, 1] but
        # any counter drift could push it out).
        success_rate = max(0.0, min(1.0, success_rate))

        payload: Dict[str, Any] = {
            "counters": {
                str(p): dict(c) for p, c in (counters or {}).items()
            },
            "cooldown_snapshot": (
                {str(p): dict(c) for p, c in (cooldown_snapshot or {}).items()}
                if cooldown_snapshot
                else None
            ),
            "totals": {
                "providers": len(counters or {}),
                "requests": total_requests,
                "ok_200": total_ok,
                "citations_yielded": total_citations,
            },
        }
        return self._emit(
            kind=EventKind.PROVIDER_HEALTH_SUMMARY,
            stage="librarian_research",
            run_id=run_id,
            project_name=project_name,
            mode=mode,
            user_problem=user_problem,
            run_meta=run_meta,
            payload=payload,
            outcome=_outcome_dict(
                OutcomeStatus.SUCCESS,
                score=success_rate,
                note=(
                    f"providers={len(counters or {})} "
                    f"citations={total_citations}"
                ),
            ),
            extra_signals=extra_signals,
        )

    def record_direction_debate_degraded_proceed(
        self,
        *,
        run_id: str,
        project_name: str,
        mode: str,
        selected_direction: str,
        original_decision: str,
        consecutive_force_none_count: int,
        final_score: int,
        gate_reason: str,
        attempt: int = 1,
        user_problem: Optional[str] = None,
        run_meta: Optional[Mapping[str, Any]] = None,
        extra_signals: Optional[List[str]] = None,
    ) -> Optional[str]:
        """Emit a ``direction_debate_degraded_proceed`` event (v1.1.8
        Phase 7, P5).

        Records that the gate degraded from force-none to low-confidence
        proceed after N consecutive same-reason force-none iterations.
        Routes to ``debate`` stream alongside other gate events.

        Gated by ``CRUCIBLE_RUN_INSIGHTS_RECORD_DEBATE`` (per-stream).
        Best-effort; never raises.

        Note: emit happens REGARDLESS of whether
        ``CRUCIBLE_DEBATE_TOLERATE_UNVERIFIABLE_EVIDENCE`` is on — the
        event captures the OBSERVED degrade-candidate situation so
        v1.2.0 retrieval can identify which runs would have triggered.
        The actual decision change is controlled by the env toggle in
        Phase 7 (``crucible/features/direction_debate/degraded.py``).
        """
        if not env_bool("CRUCIBLE_RUN_INSIGHTS_RECORD_DEBATE", True):
            return None
        if not self._enabled():
            return None
        payload: Dict[str, Any] = {
            "selected_direction": str(selected_direction or "unknown"),
            "original_decision": str(original_decision or "force_none"),
            "consecutive_force_none_count": max(
                0, int(consecutive_force_none_count)
            ),
            "final_score": int(final_score),
            "gate_reason": str(gate_reason or "")[:500],
            "attempt": max(1, int(attempt)),
        }
        return self._emit(
            kind=EventKind.DIRECTION_DEBATE_DEGRADED_PROCEED,
            stage="stage0_direction",
            run_id=run_id,
            project_name=project_name,
            mode=mode,
            user_problem=user_problem,
            run_meta=run_meta,
            payload=payload,
            outcome=_outcome_dict(
                OutcomeStatus.PARTIAL,
                note=f"degraded_proceed:{selected_direction}",
            ),
            extra_signals=extra_signals,
        )

    # -- core emit -------------------------------------------------------------

    def _emit(
        self,
        *,
        kind: EventKind,
        stage: str,
        run_id: str,
        project_name: str,
        mode: str,
        user_problem: Optional[str],
        run_meta: Optional[Mapping[str, Any]],
        payload: Mapping[str, Any],
        outcome: Mapping[str, Any],
        reusability: Optional[Mapping[str, Any]] = None,
        extra_signals: Optional[List[str]] = None,
    ) -> Optional[str]:
        try:
            meta = dict(run_meta or {})
            signals = redact_signals(
                extract_signals(
                    mode=mode,
                    user_problem=user_problem,
                    run_meta=meta,
                    extra=extra_signals,
                )
            )
            env_fp = build_env_fingerprint(
                model_id=meta.get("model_id"),
                llm_provider=meta.get("llm_provider"),
            )
            payload_clean = redact_event_payload(payload)

            ev = InsightEvent(
                kind=kind,
                stage=stage,
                # v1.1.2 (sixth-pass H-3): apply ``.strip()`` BEFORE the
                # 64-char truncation.  v1.1.2 G-1 standardised ``.strip()`` at
                # ``run_correlation.set_run_id`` / ``run_context``, but this
                # emit path bypassed both and could persist whitespace-only
                # run_ids that look truthy to ``or``-fallbacks and break the
                # 8-char-hex assumption every downstream consumer holds.
                run_id=str(run_id or "").strip()[:64],
                project_name=str(project_name or "unknown")[:128],
                mode=_normalise_mode(mode) or "unknown",
                signals=signals,
                payload=payload_clean,
                env_fingerprint=env_fp,
                outcome=dict(outcome),
                reusability=dict(reusability) if reusability else None,
            )
            record = ev.to_dict()
            stream = ev.stream_name()
            content_id = self._backend.write_event(stream, record)
            self._maybe_prune(stream)
            return content_id or None
        except Exception as exc:  # noqa: BLE001 — must never break pipeline
            if not self._warned_emit_failed:
                LOGGER.warning(
                    "run_insights: emit failed (kind=%s): %s", kind.value, exc
                )
                self._warned_emit_failed = True
            return None

    def _maybe_prune(self, stream: str) -> None:
        # v1.1.0 fourth-pass: serialise the read-modify-write of the
        # per-stream counter so two concurrent emits to the same
        # stream don't lose increments (race) or double-prune (also
        # race).  ``self._lock`` is the recorder-instance lock — cheap
        # to take, already used elsewhere for thread-safety guarantees.
        with self._lock:
            counter = self._writes_since_prune.get(stream, 0) + 1
            self._writes_since_prune[stream] = counter
            if counter < self._prune_check_interval:
                return
            self._writes_since_prune[stream] = 0
        try:
            self._backend.prune_stream(stream, self._max_entries)
        except Exception as exc:  # noqa: BLE001
            LOGGER.debug("run_insights: prune deferred for %s: %s", stream, exc)

    # -- introspection ---------------------------------------------------------

    def _enabled(self) -> bool:
        return env_bool("CRUCIBLE_RUN_INSIGHTS_ENABLED", True)

    @property
    def backend(self) -> StorageBackend:
        return self._backend

    def flush(self) -> None:
        try:
            self._backend.flush()
        except Exception:  # noqa: BLE001
            pass

    def close(self) -> None:
        try:
            self._backend.close()
        except Exception:  # noqa: BLE001
            pass


# ── No-op recorder ────────────────────────────────────────────────────────────

class _NoOpBackend:
    """A no-op stand-in for :class:`backends.StorageBackend` used when the
    recorder subsystem is disabled.

    v1.1.2 (audit fix G2-B-MED-5): previously ``_NullRecorder.backend``
    returned ``None``, which broke parity with the live ``InsightsRecorder``
    (whose ``.backend`` is a real :class:`StorageBackend`).  Any code path
    that reached through ``recorder.backend.read_events(...)`` or
    ``.write_blob(...)`` worked in dev (subsystem enabled) but raised
    ``AttributeError`` in prod whenever the operator set
    ``CRUCIBLE_RUN_INSIGHTS_ENABLED=0``.  This stub implements the full
    backend protocol with no-op returns so the parity promise holds at every
    API level.
    """

    _init_failed = False

    def write_event(self, _stream: str, _record: Mapping[str, Any]) -> str:
        return ""

    def write_blob(self, content_id: str, payload: bytes) -> str:  # noqa: ARG002
        # v1.1.11 (audit fix): byte-match StorageBackend.write_blob —
        # (content_id, payload) positional, no ``suffix`` kwarg.  The old
        # signature ``(_payload, *, suffix)`` silently bound content_id to the
        # payload and dropped the real payload arg, so any disabled-subsystem
        # caller passing ``write_blob(cid, data)`` got wrong-arg behaviour.
        return ""

    def read_blob(self, content_id: str) -> Optional[bytes]:  # noqa: ARG002
        # v1.1.11 (audit fix): protocol method was missing entirely →
        # AttributeError for any ``recorder.backend.read_blob(...)`` reach-
        # through when CRUCIBLE_RUN_INSIGHTS_ENABLED=0.
        return None

    def read_events(
        self,
        _stream: str,
        *,
        since: Optional[str] = None,
        cursor: Optional[str] = None,
        limit: int = 100,
    ) -> Tuple[List[Dict[str, Any]], Optional[str]]:  # noqa: ARG002
        # v1.1.11 (audit fix): protocol returns ``(events, next_cursor)``.
        # The old bare ``[]`` broke every caller doing
        # ``events, cursor = backend.read_events(...)`` (ValueError: not
        # enough values to unpack).
        return [], None

    def prune_stream(self, _stream: str, _max_entries: int) -> int:  # noqa: ARG002
        return 0

    def flush(self) -> None:
        return None

    def close(self) -> None:
        return None


class _NullRecorder:
    """No-op recorder used when ``CRUCIBLE_RUN_INSIGHTS_ENABLED=0``.

    Every emit method silently returns ``None``.  Lazy: never opens a file,
    never registers a sink.  ``.backend`` returns a :class:`_NoOpBackend`
    so call sites that reach through ``recorder.backend.X`` work
    identically whether the subsystem is enabled or disabled.
    """

    def __init__(self) -> None:
        self._backend = _NoOpBackend()

    def record_output_method(self, **_kw: Any) -> None: return None
    def record_error(self, **_kw: Any) -> None: return None
    def record_direction_debate_rejection(self, **_kw: Any) -> None: return None
    def record_runtime_params(self, **_kw: Any) -> None: return None
    # v1.1.8 audit mode methods (pre-existing — these were added to
    # InsightsRecorder but the NullRecorder interface parity was missed).
    def record_debate_finding(self, **_kw: Any) -> None: return None
    def record_gate_verdict(self, **_kw: Any) -> None: return None
    # v1.1.8 extended (Phase 2 + Phase 7) — librarian observability and
    # degrade-not-die events.  Interface parity prevents AttributeError
    # when callers run with CRUCIBLE_RUN_INSIGHTS_ENABLED=0.
    def record_provider_cooldown(self, **_kw: Any) -> None: return None
    def record_provider_health_summary(self, **_kw: Any) -> None: return None
    def record_direction_debate_degraded_proceed(self, **_kw: Any) -> None: return None
    def flush(self) -> None: return None
    def close(self) -> None: return None

    @property
    def backend(self) -> "_NoOpBackend":
        return self._backend


# ── Process-global singleton ──────────────────────────────────────────────────

_RECORDER: Any = None
_RECORDER_LOCK = threading.Lock()


def _build_default_recorder() -> Any:
    """Construct the recorder from env vars.

    Honours total disable (``CRUCIBLE_RUN_INSIGHTS_ENABLED=0`` → null
    recorder), backend selection, ledger directory, inline limit, and the
    per-stream FIFO cap.
    """
    if not env_bool("CRUCIBLE_RUN_INSIGHTS_ENABLED", True):
        return _NullRecorder()

    backend_name = env_str("CRUCIBLE_RUN_INSIGHTS_BACKEND", "local").lower()
    root = env_str("CRUCIBLE_RUN_INSIGHTS_DIR", _DEFAULT_DIR)
    inline_max = env_int(
        "CRUCIBLE_RUN_INSIGHTS_INLINE_MAX_BYTES",
        _DEFAULT_INLINE_MAX_BYTES,
        clamp_min=0,
    )
    max_entries = env_int(
        "CRUCIBLE_RUN_INSIGHTS_MAX_ENTRIES_PER_STREAM",
        _DEFAULT_MAX_ENTRIES_PER_STREAM,
        clamp_min=10,
        # v1.1.0 third-pass: clamp_max guards against an operator typo
        # (e.g. ``MAX_ENTRIES_PER_STREAM=2000000000``) that would have
        # caused ``collections.deque(maxlen=2e9)`` to attempt a 1 TB
        # allocation during prune.  1_000_000 events × ~1 KB = 1 GB
        # ceiling — far beyond any practical ledger workload.
        clamp_max=1_000_000,
    )
    api_url = env_str("CRUCIBLE_RUN_INSIGHTS_API_URL", "")
    api_token = env_str("CRUCIBLE_RUN_INSIGHTS_API_TOKEN", "")

    # Anchor the local backend's root: if it's a bare directory name, place
    # it under the repo root (parent of the ``crucible`` package), not the
    # current working directory of whatever spawned the process.
    # Anchor a relative ledger root under the repo root (not the spawning
    # process CWD) for EVERY backend — dual/cloudflare keep a local durable
    # copy and must anchor identically to the local backend.
    root_path = root
    if not os.path.isabs(root):
        # crucible/features/run_insights/recorder.py → repo root is 3 up
        here = os.path.dirname(os.path.abspath(__file__))
        repo_root = os.path.dirname(os.path.dirname(os.path.dirname(here)))
        root_path = os.path.join(repo_root, root)

    if backend_name == "local":
        backend = make_backend(
            "local",
            root=root_path,
            inline_max_bytes=inline_max,
        )
    elif backend_name in ("dual", "cloudflare"):
        if not api_url or not api_token:
            # Misconfigured cloud backend — degrade to a durable local-only
            # ledger rather than dropping recording entirely.  Loud, not
            # silent: the operator gets a clear one-line warning.
            LOGGER.warning(
                "run_insights: BACKEND=%s but CRUCIBLE_RUN_INSIGHTS_API_URL / "
                "_API_TOKEN are not both set; falling back to a local-only "
                "ledger (no cloud sync).",
                backend_name,
            )
            backend = make_backend("local", root=root_path, inline_max_bytes=inline_max)
        else:
            api_timeout = env_int(
                "CRUCIBLE_RUN_INSIGHTS_API_TIMEOUT_SECONDS", 10,
                clamp_min=1, clamp_max=300,
            )
            api_retries = env_int(
                "CRUCIBLE_RUN_INSIGHTS_API_MAX_RETRIES", 3,
                clamp_min=0, clamp_max=10,
            )
            api_flush = env_int(
                "CRUCIBLE_RUN_INSIGHTS_API_BATCH_FLUSH_SECONDS", 30,
                clamp_min=1, clamp_max=3600,
            )
            api_batch = env_int(
                "CRUCIBLE_RUN_INSIGHTS_API_BATCH_SIZE", 100,
                clamp_min=1, clamp_max=500,
            )
            backend = make_backend(
                backend_name,
                root=root_path,
                inline_max_bytes=inline_max,
                api_url=api_url,
                api_token=api_token,
                timeout_seconds=api_timeout,
                max_retries=api_retries,
                flush_seconds=api_flush,
                batch_size=api_batch,
            )
    else:
        # Unknown backend name → make_backend raises ValueError (loud); the
        # outer get_recorder() try/except substitutes _NullRecorder.
        backend = make_backend(backend_name, root=root_path, inline_max_bytes=inline_max)

    # v1.1.0 fourth-pass (F-6): if the backend signalled init failure
    # (e.g. read-only filesystem, permission denied, root is a
    # regular file), substitute the no-op recorder so operators
    # don't end up with a black-hole backend that silently drops
    # every event.  ``_init_failed`` is set on LocalJSONLBackend when
    # ``_init_layout`` swallows an OSError — see backends.py.
    if getattr(backend, "_init_failed", False):
        LOGGER.warning(
            "run_insights: backend init failed at %s; falling back to "
            "_NullRecorder.  No events will be recorded for this process.",
            root,
        )
        return _NullRecorder()

    return InsightsRecorder(
        backend,
        max_entries_per_stream=max_entries,
        inline_max_bytes=inline_max,
    )


def get_recorder() -> Any:
    """Return the process-global recorder, constructing it on first use.

    The recorder is one of :class:`InsightsRecorder` (subsystem enabled) or
    :class:`_NullRecorder` (subsystem disabled).  Both implement the same
    emit methods so call sites never branch on enablement.

    Failures during construction (e.g. unimplemented backend, unwritable
    directory) fall back to :class:`_NullRecorder` and log once at WARNING
    level.  Pipeline behaviour must never break because the ledger is
    misconfigured.
    """
    global _RECORDER
    if _RECORDER is not None:
        return _RECORDER
    with _RECORDER_LOCK:
        if _RECORDER is not None:
            return _RECORDER
        try:
            _RECORDER = _build_default_recorder()
        except Exception as exc:  # noqa: BLE001
            LOGGER.warning(
                "run_insights: recorder init failed (%s); using no-op recorder",
                exc,
            )
            _RECORDER = _NullRecorder()
    return _RECORDER


def reset_recorder() -> None:
    """Tear down the process-global recorder.

    Used by tests (each test gets a fresh ledger) and by the runner at the
    start of a brand-new pipeline run that wants to ignore any in-memory
    counters / prune deferrals from the previous run.
    """
    global _RECORDER
    with _RECORDER_LOCK:
        if _RECORDER is not None:
            try:
                _RECORDER.close()
            except Exception:  # noqa: BLE001
                pass
        _RECORDER = None


def _reset_recorder_after_fork() -> None:
    """POSIX fork hook: discard the inherited recorder in the child.

    Without this, ``os.fork()`` (used by pytest-xdist and any operator who
    forks the process) leaves the child with:

    * a ``_RECORDER`` global pointing at the parent's backend object,
    * a ``threading.Lock`` whose state was captured at fork time (if the
      parent held it, the child deadlocks on first emit),
    * a file handle (in some backends) inherited via fd duplication.

    Resetting the globals forces the child to lazily build its own
    recorder on first ``get_recorder()`` call — fresh lock, fresh backend,
    fresh prune counters.  No-op on Windows (no fork) and on exotic
    Pythons that lack ``os.register_at_fork``.
    """
    global _RECORDER, _RECORDER_LOCK
    _RECORDER = None
    # The lock object may have been held by the parent at fork time; replace
    # it with a fresh one so the child cannot inherit a wedged state.
    _RECORDER_LOCK = threading.Lock()


if hasattr(os, "register_at_fork"):
    try:
        os.register_at_fork(after_in_child=_reset_recorder_after_fork)
    except Exception:  # noqa: BLE001 — register_at_fork can fail on rare interp builds
        pass


__all__ = [
    "InsightsRecorder",
    "get_recorder",
    "reset_recorder",
]
