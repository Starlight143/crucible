# Auto-generated section module — do not edit manually.
# Regenerate via ``python -m crucible.generate``.
from __future__ import annotations

import threading

from . import section_00_bootstrap_and_utils as _prev_00

globals().update({k: v for k, v in _prev_00.__dict__.items() if not k.startswith("__")})
from . import section_01_extraction_and_reformat as _prev_01

globals().update({k: v for k, v in _prev_01.__dict__.items() if not k.startswith("__")})
if __package__ == "crucible.modules":
    from ..resilience import kickoff_crew_with_retry
    from ..runtime_logging import get_logger, log_event, log_exception
    from ..cancellation import OperationCancelledError as _OperationCancelledError
    from ..run_correlation import get_run_id as _get_run_id, set_run_id as _set_run_id
    from ..features.run_insights import get_recorder as _get_insights_recorder
else:  # pragma: no cover - direct script fallback
    from resilience import kickoff_crew_with_retry  # type: ignore[no-redef]
    from runtime_logging import get_logger, log_event, log_exception  # type: ignore[no-redef]
    from cancellation import OperationCancelledError as _OperationCancelledError  # type: ignore[no-redef]
    from run_correlation import (  # type: ignore[no-redef]
        get_run_id as _get_run_id,
        set_run_id as _set_run_id,
    )
    from features.run_insights import get_recorder as _get_insights_recorder  # type: ignore[no-redef]


LOGGER = get_logger(__name__)


def _resolve_run_id_for_ledger_emit() -> str:
    """Resolve a non-empty ``run_id`` for direction-debate ledger emits.

    v1.1.2 (sixth-pass H-3): mirrors the three-tier fallback used by
    ``section_07_selfcheck_output_main:1371-1384``.  Without the
    fresh-uuid third tier an early section_02 emit (force-none /
    parse-fail) that fires before any upstream code has bridged
    ``CRUCIBLE_RUN_ID`` to the ContextVar can write ``run_id=""``,
    violating the CLAUDE.md § 2 invariant and orphaning the row from
    every downstream artefact (run_meta, telemetry, structured logs).

    The minted run_id is also pinned back into the ContextVar so any
    later emit point in the same process sees a consistent value.
    """
    import os as _os_local
    import uuid as _uuid_local

    candidate = (
        (_get_run_id() or "").strip()
        or _os_local.environ.get("CRUCIBLE_RUN_ID", "").strip()
    )
    if candidate:
        return candidate
    fresh = _uuid_local.uuid4().hex[:8]
    try:
        # ``_set_run_id`` is imported at the module level via the existing
        # tri-modal package guard; no per-call import trampoline is needed.
        _set_run_id(fresh)
    except Exception:
        # ContextVar binding is best-effort; the pipeline must not crash
        # if the runtime_logging module is in a degraded state.
        pass
    try:
        LOGGER.warning(
            "section_02 direction-debate emit minted fallback run_id=%s "
            "(both ContextVar and CRUCIBLE_RUN_ID resolved to empty)",
            fresh,
        )
    except Exception:
        pass
    return fresh


# ---------------------------------------------------------------------------
# v1.1.8 — Direction Debate Audit Mode parsers + emit pipeline
# ---------------------------------------------------------------------------
# These helpers parse the AUDIT_FINDING and GATE_VERDICT blocks that the
# audit-mode crew (see ``section_04:build_direction_debate_crew``) appends
# to each task's output, and emit corresponding ledger events.  All of this
# is best-effort: a failure to parse or emit MUST NOT break the main
# direction-debate pipeline, matching the existing swallow-on-failure
# pattern around ``record_direction_debate_rejection``.

import json as _json_audit  # local alias to avoid clashing with any later import
import re as _re_audit


_AUDIT_FINDING_BLOCK_RE = _re_audit.compile(
    r'<<<\s*AUDIT_FINDING_BEGIN\s+role\s*=\s*"([^"]+)"\s*>>>'
    r'(.*?)'
    r'<<<\s*AUDIT_FINDING_END\s*>>>',
    _re_audit.DOTALL,
)

_GATE_VERDICT_BLOCK_RE = _re_audit.compile(
    r'<<<\s*GATE_VERDICT_BEGIN\s*>>>'
    r'(.*?)'
    r'<<<\s*GATE_VERDICT_END\s*>>>',
    _re_audit.DOTALL,
)


def _extract_json_from_block(block_text: str) -> Optional[Dict[str, Any]]:
    """Pull a single JSON object from a noisy LLM block text.

    Strips markdown fences if present, tries a direct parse, then falls back
    to the first ``{...}`` substring.  Returns ``None`` on any failure.
    """
    if not block_text:
        return None
    raw = str(block_text).strip()
    raw = _re_audit.sub(r"^```(?:json)?\s*", "", raw, flags=_re_audit.IGNORECASE)
    raw = _re_audit.sub(r"\s*```$", "", raw)
    try:
        obj = _json_audit.loads(raw)
        if isinstance(obj, dict):
            return obj
    except (ValueError, TypeError):
        pass
    m = _re_audit.search(r"\{.*\}", raw, _re_audit.DOTALL)
    if not m:
        return None
    try:
        obj = _json_audit.loads(m.group(0))
        if isinstance(obj, dict):
            return obj
    except (ValueError, TypeError):
        return None
    return None


def _parse_audit_findings_from_text(text: str) -> List[Dict[str, Any]]:
    """Extract every ``AUDIT_FINDING`` block from an arbitrary text blob.

    Returns a list of dicts, one per successfully parsed block.  Each dict
    has the SpecialistFinding-shape fields plus an injected ``role`` from
    the block header — the role in the header takes precedence over any
    ``role`` field inside the JSON body, which guards against an LLM that
    sets the wrong role in its own JSON.
    """
    out: List[Dict[str, Any]] = []
    if not text:
        return out
    for match in _AUDIT_FINDING_BLOCK_RE.finditer(str(text)):
        header_role = (match.group(1) or "").strip()
        body = match.group(2) or ""
        parsed = _extract_json_from_block(body)
        if not parsed:
            continue
        if header_role:
            parsed["role"] = header_role  # header wins
        out.append(parsed)
    return out


def _parse_gate_verdict_from_text(text: str) -> Optional[Dict[str, Any]]:
    """Extract the single ``GATE_VERDICT`` block from a text blob, if any.

    Returns the parsed dict on success; ``None`` if the block is missing
    or the JSON inside is malformed.  Only the first block is returned —
    a Judge that emits multiple GATE_VERDICT blocks is malformed and we
    take the first as authoritative.
    """
    if not text:
        return None
    match = _GATE_VERDICT_BLOCK_RE.search(str(text))
    if not match:
        return None
    return _extract_json_from_block(match.group(1) or "")


def _coerce_audit_finding_to_payload(raw: Mapping[str, Any]) -> Dict[str, Any]:
    """Normalise a parsed AUDIT_FINDING dict into recorder-payload shape.

    Defensive against missing/null keys — the recorder method's signature
    accepts ``None`` / empty list defaults so a partially-malformed LLM
    output still produces a valid (if sparse) ledger row rather than
    raising and getting swallowed silently.
    """
    role = str(raw.get("role") or "unknown").strip()
    conclusion = str(raw.get("conclusion") or "").strip()

    confidence_raw = raw.get("confidence")
    confidence_clean: Optional[float] = None
    if confidence_raw is not None:
        try:
            cv = float(confidence_raw)
            import math as _math
            if _math.isfinite(cv) and 0.0 <= cv <= 1.0:
                confidence_clean = cv
        except (TypeError, ValueError):
            confidence_clean = None

    def _str_list(value: Any) -> List[str]:
        if not isinstance(value, list):
            return []
        return [str(item).strip() for item in value if str(item or "").strip()]

    def _dict_list(value: Any) -> List[Dict[str, Any]]:
        if not isinstance(value, list):
            return []
        out: List[Dict[str, Any]] = []
        for item in value:
            if isinstance(item, dict):
                out.append(dict(item))
        return out

    return {
        "role": role,
        "conclusion": conclusion,
        "confidence": confidence_clean,
        "assumptions": _str_list(raw.get("assumptions")),
        "supporting_evidence": _dict_list(raw.get("supporting_evidence")),
        "concerns": _dict_list(raw.get("concerns")),
        "disagreement_with": _dict_list(raw.get("disagreement_with")),
        "missing_information": _str_list(raw.get("missing_information")),
        "failed_invariants": _str_list(raw.get("failed_invariants")),
    }


def _emit_audit_mode_ledger_events(
    *,
    direction_result: Any,
    judge_decision: Any,
    judge_summary: str,
    mode: str,
    user_problem: str,
    attempt: int,
    audit_mode_enabled: bool,
    isolation_mode: str,
    external_critic_enabled: bool,
    critic_can_override: bool,
    direction_judge_llm: Any,
    research_context: Optional["ResearchContext"],
    language_hint: str,
) -> Tuple[Optional[Dict[str, Any]], Optional[Dict[str, Any]]]:
    """Parse AUDIT_FINDING + GATE_VERDICT blocks from the crew result,
    optionally invoke the External Critic, run consensus-risk computation,
    and emit ``debate_finding`` + ``gate_verdict`` ledger events.

    Returns ``(gate_verdict_dict, consensus_risk_dict)`` — both ``None`` if
    audit_mode is disabled or if all parsing failed.  Returning structured
    data lets the caller include the verdict in error dumps or status
    output if it wants to.

    100 % swallow on any failure — never raises.
    """
    if not audit_mode_enabled:
        return None, None

    try:
        # Collect every text candidate from the crew result and concatenate.
        # AUDIT_FINDING blocks may live in any task's output; concatenating
        # gives us a single string to scan that covers all five tasks.
        raw_candidates: List[str] = []
        try:
            raw_candidates = _collect_text_candidates_from_result(direction_result) or []
        except Exception:
            raw_candidates = []
        combined_text = "\n\n".join(str(c or "") for c in raw_candidates)

        # Parse AUDIT_FINDING blocks.
        finding_dicts = _parse_audit_findings_from_text(combined_text)

        # Try to build typed SpecialistFinding objects for consensus_risk.
        # Any individual coercion failure is logged and skipped — consensus
        # works fine with a partial set.
        try:
            from .section_03_models_and_context import SpecialistFinding as _SF
        except ImportError:  # pragma: no cover - flat-launcher fallback
            try:
                from modules.section_03_models_and_context import SpecialistFinding as _SF  # type: ignore[no-redef]
            except ImportError:
                _SF = None  # type: ignore[assignment]

        typed_findings: List[Any] = []
        if _SF is not None:
            for fd in finding_dicts:
                try:
                    payload = _coerce_audit_finding_to_payload(fd)
                    # Strip None confidence so the required field falls
                    # back to a sensible default (we set ge=0.0,le=1.0;
                    # a missing value is treated as 0.5 neutral).
                    if payload.get("confidence") is None:
                        payload["confidence"] = 0.5
                    # SpecialistFinding requires non-empty ``conclusion``;
                    # supply a placeholder when missing so the pydantic
                    # build still succeeds and the audit row isn't dropped.
                    if not payload.get("conclusion"):
                        payload["conclusion"] = "(no conclusion text emitted)"
                    typed_findings.append(_SF(**payload))
                except Exception:
                    continue

        # Compute ConsensusRiskReport (deterministic, no LLM).
        consensus_risk_dict: Optional[Dict[str, Any]] = None
        if typed_findings:
            try:
                if __package__ == "crucible.modules":
                    from ..features.direction_debate.consensus import (
                        compute_consensus_risk as _ccr,
                    )
                else:
                    from features.direction_debate.consensus import (  # type: ignore[no-redef]
                        compute_consensus_risk as _ccr,
                    )
                consensus_report = _ccr(typed_findings)
                consensus_risk_dict = consensus_report.model_dump(exclude_none=False)
                # Strip raw_metrics from the ledger payload — it's purely
                # for debugging and inflates row size.
                consensus_risk_dict.pop("raw_metrics", None)
            except Exception:
                consensus_risk_dict = None

        # Parse GATE_VERDICT block from Judge's output.  Judge's task output
        # is typically the last task in the crew, so prefer the LAST
        # candidate that contains a GATE_VERDICT marker.
        gate_verdict_dict: Optional[Dict[str, Any]] = None
        for raw in reversed(raw_candidates):
            verdict_parsed = _parse_gate_verdict_from_text(str(raw or ""))
            if verdict_parsed:
                gate_verdict_dict = verdict_parsed
                break

        # Normalise gate_verdict_dict; if missing entirely, synthesise a
        # PROCEED/none-style placeholder from the legacy DirectionDecision
        # so the ledger always has at least ONE gate_verdict event per run.
        if not gate_verdict_dict:
            legacy_dir = (
                str(getattr(judge_decision, "selected_direction", "") or "").strip().upper()
            )
            if legacy_dir == "NONE" or not legacy_dir:
                gate_verdict_dict = {
                    "decision": "NEEDS_MORE_DATA",
                    "selected_direction": None,
                    "reason": (
                        "Audit-mode GATE_VERDICT block missing from Judge output; "
                        "inferring NEEDS_MORE_DATA from legacy force-none signal."
                    ),
                    "blocking_evidence_queries": [
                        "rerun direction debate with audit mode and verify Judge emits GATE_VERDICT block",
                    ],
                }
            elif legacy_dir in {"A", "B", "C", "D", "E", "F", "G"}:
                gate_verdict_dict = {
                    "decision": "PROCEED",
                    "selected_direction": legacy_dir,
                    "reason": (
                        f"Audit-mode GATE_VERDICT block missing from Judge output; "
                        f"inferring PROCEED with direction {legacy_dir} from legacy DirectionDecision."
                    ),
                }
            else:
                gate_verdict_dict = None  # truly unparseable; skip emit

        # Optionally invoke the External Critic.
        critic_overrode = False
        critic_dissent_recorded = False
        judge_initial_decision: Optional[str] = None
        if (
            external_critic_enabled
            and gate_verdict_dict
            and direction_judge_llm is not None
        ):
            try:
                if __package__ == "crucible.modules":
                    from ..features.direction_debate.critic import (
                        CriticUnavailableError as _CritErr,
                        validate_direction_verdict as _critic_validate,
                    )
                else:
                    from features.direction_debate.critic import (  # type: ignore[no-redef]
                        CriticUnavailableError as _CritErr,
                        validate_direction_verdict as _critic_validate,
                    )
                evidence_block = ""
                if research_context is not None:
                    try:
                        # Lazy import to avoid the section_02 ↔ section_04
                        # circular at module-load time.  By the time this
                        # function executes, section_04 has been fully
                        # loaded via the section_03 → section_02 globals
                        # propagation chain in production runs.
                        try:
                            if __package__ == "crucible.modules":
                                from .section_04_web_research_and_direction import (
                                    _render_research_context_for_prompt as _rrc,
                                )
                            else:
                                from section_04_web_research_and_direction import (  # type: ignore[no-redef]
                                    _render_research_context_for_prompt as _rrc,
                                )
                            evidence_block = _rrc(research_context) or ""
                        except ImportError:
                            # Fall back to a minimal stringification — the
                            # Critic still gets *something* even if the
                            # canonical renderer is unavailable.
                            evidence_block = str(research_context)[:8000]
                    except Exception:
                        evidence_block = ""
                judge_initial_decision = str(gate_verdict_dict.get("decision") or "")
                critic_verdict = _critic_validate(
                    raw_research_evidence=evidence_block,
                    judge_decision=str(gate_verdict_dict.get("decision") or ""),
                    judge_reason=str(gate_verdict_dict.get("reason") or ""),
                    judge_selected_direction=gate_verdict_dict.get("selected_direction"),
                    llm=direction_judge_llm,
                    language_hint=language_hint,
                )
                critic_decision = str(critic_verdict.decision)
                if critic_decision != judge_initial_decision:
                    # Critic disagrees.
                    if (
                        critic_can_override
                        and judge_initial_decision == "PROCEED"
                        and critic_decision == "KILL"
                    ):
                        # Critic wins (the only allowed override case in v1.1.8).
                        gate_verdict_dict = {
                            "decision": critic_decision,
                            "selected_direction": None,
                            "reason": (
                                f"External Critic overrode Judge PROCEED with KILL: "
                                f"{critic_verdict.reason}"
                            )[:2000],
                            "failed_invariants": list(
                                critic_verdict.failed_invariants or []
                            ),
                        }
                        critic_overrode = True
                    else:
                        # Critic dissents but Judge stands (default in v1.1.8).
                        critic_dissent_recorded = True
            except _CritErr:
                pass
            except Exception:
                pass

        # Attach audit_trail metadata to the verdict dict (always present
        # in audit_mode, even when the LLM did not emit one).
        if gate_verdict_dict is not None:
            gate_verdict_dict.setdefault("audit_trail", {})
            at = dict(gate_verdict_dict.get("audit_trail") or {})
            at["audit_mode_enabled"] = True
            at["isolation_mode"] = isolation_mode
            at["external_critic_used"] = bool(external_critic_enabled)
            at["critic_overrode_judge"] = bool(critic_overrode)
            at["critic_dissent_recorded"] = bool(critic_dissent_recorded)
            at["critic_model_family"] = None  # v1.1.8: same family as Judge
            if judge_initial_decision and critic_overrode:
                at["judge_initial_decision"] = judge_initial_decision
            gate_verdict_dict["audit_trail"] = at

        # Emit ledger events — one debate_finding per parsed finding +
        # one gate_verdict for the final verdict.  Best-effort; recorder
        # already swallows backend failures.
        recorder = _get_insights_recorder()
        run_id = _resolve_run_id_for_ledger_emit()
        project_name = "stage0_pending"
        normalised_mode = str(mode or "mode_unknown")

        for finding_dict in finding_dicts:
            try:
                payload = _coerce_audit_finding_to_payload(finding_dict)
                recorder.record_debate_finding(
                    run_id=run_id,
                    project_name=project_name,
                    mode=normalised_mode,
                    role=payload.get("role", "unknown"),
                    conclusion=payload.get("conclusion", ""),
                    confidence=payload.get("confidence"),
                    assumptions=payload.get("assumptions"),
                    supporting_evidence=payload.get("supporting_evidence"),
                    concerns=payload.get("concerns"),
                    disagreement_with=payload.get("disagreement_with"),
                    missing_information=payload.get("missing_information"),
                    failed_invariants=payload.get("failed_invariants"),
                    attempt=attempt,
                    user_problem=user_problem,
                )
            except Exception:
                continue

        if gate_verdict_dict is not None:
            try:
                recorder.record_gate_verdict(
                    run_id=run_id,
                    project_name=project_name,
                    mode=normalised_mode,
                    decision=str(gate_verdict_dict.get("decision") or ""),
                    reason=str(gate_verdict_dict.get("reason") or ""),
                    selected_direction=gate_verdict_dict.get("selected_direction"),
                    branched_paths=gate_verdict_dict.get("branched_paths"),
                    failed_invariants=gate_verdict_dict.get("failed_invariants"),
                    blocking_evidence_queries=gate_verdict_dict.get(
                        "blocking_evidence_queries"
                    ),
                    consensus_risk=consensus_risk_dict,
                    audit_trail=gate_verdict_dict.get("audit_trail"),
                    attempt=attempt,
                    user_problem=user_problem,
                )
            except Exception:
                pass

        return gate_verdict_dict, consensus_risk_dict

    except Exception:
        # Total swallow: audit mode failures NEVER break the main pipeline.
        return None, None


# ---------------------------------------------------------------------------
# Provider URL helpers
# ---------------------------------------------------------------------------


def _get_openrouter_enforced_base_url() -> str:
    """Return the OpenRouter base URL from the environment variable."""
    url = (os.environ.get("OPENROUTER_BASE_URL") or "").strip()
    return url or OPENROUTER_API_BASE_URL


def _get_alibaba_enforced_base_url() -> str:
    """Return the Alibaba Coding Plan base URL from the environment variable."""
    url = (os.environ.get("ALIBABA_CODING_PLAN_BASE_URL") or "").strip()
    return url or ALIBABA_CODING_PLAN_API_BASE_URL


def _get_ollama_enforced_base_url() -> str:
    """Return the Ollama base URL from the environment variable."""
    url = (os.environ.get("OLLAMA_BASE_URL") or "").strip()
    return url or OLLAMA_BASE_URL


def _is_alibaba_base_url(base_url: str) -> bool:
    """Return True if *base_url* resolves to an Alibaba Coding Plan API endpoint.

    Detection is two-pronged:
      1. Domain-based: Alibaba DashScope / coding-intl hostnames.
      2. ENV-match: the URL matches ``ALIBABA_CODING_PLAN_BASE_URL`` (supports
         proxy / custom endpoints).
    """
    if not base_url:
        return False
    low = base_url.lower().rstrip("/")
    if "dashscope.aliyuncs.com" in low or "dashscope.aliyun.com" in low:
        return True
    ali_env = (
        os.environ.get("ALIBABA_CODING_PLAN_BASE_URL") or ""
    ).strip().lower().rstrip("/")
    if ali_env and ali_env == low:
        return True
    return False


def _inject_opencode_header_to_httpx_client(client: Any) -> None:
    """Set ``x-source: opencode`` on the httpx client embedded inside an openai SDK client.

    Used for Alibaba Coding Plan clients to signal opencode origin.
    """
    try:
        _httpx_cli = getattr(client, "_client", None)
        if _httpx_cli is None:
            return
        _headers = getattr(_httpx_cli, "headers", None)
        if _headers is not None and _headers.get("x-source") != "opencode":
            _headers["x-source"] = "opencode"
            LOGGER.debug("[Alibaba] 'X-Source: opencode' header injected")
    except Exception as _exc:
        LOGGER.debug("[Alibaba] Could not inject x-source header: %s", _exc)


# ---------------------------------------------------------------------------
# Alibaba Coding Plan LiteLLM guard — injects x-source: opencode header on
# all litellm.completion / acompletion calls routed to Alibaba endpoints.
# ---------------------------------------------------------------------------

_ALIBABA_LITELLM_GUARD_INSTALLED: bool = False
_ALIBABA_LITELLM_GUARD_LOCK = __import__("threading").Lock()


def _install_litellm_alibaba_guard() -> None:
    """Patch litellm.completion/acompletion to inject x-source: opencode for Alibaba calls.

    Idempotent — subsequent calls after the first are no-ops.
    Double-checked locking prevents duplicate patching under concurrent imports.
    """
    global _ALIBABA_LITELLM_GUARD_INSTALLED
    if _ALIBABA_LITELLM_GUARD_INSTALLED:
        return
    with _ALIBABA_LITELLM_GUARD_LOCK:
        if _ALIBABA_LITELLM_GUARD_INSTALLED:
            return
        # Patching is performed INSIDE the lock so that:
        # (a) Only one thread ever patches (mutex prevents concurrent re-entry).
        # (b) The flag is set only AFTER successful patching — no race window
        #     exists where a concurrent caller reads True and escapes before the
        #     patch is actually applied.
        # The previous design set the flag inside the lock then patched outside
        # it, creating a window: Thread A sets True, releases lock; Thread B
        # reads True and returns (believing guard is installed); Thread A fails
        # and resets to False — Thread B permanently escaped with a false belief.
        try:
            import litellm as _litellm  # noqa: PLC0415

            _orig_completion = _litellm.completion
            _orig_acompletion = getattr(_litellm, "acompletion", None)

            def _guarded_completion(model: str = "", *args: Any, **kwargs: Any) -> Any:  # type: ignore[misc]
                base_url = (
                    str(kwargs.get("base_url") or kwargs.get("api_base") or "").strip()
                    or os.environ.get("OPENAI_BASE_URL", "")
                )
                if _is_alibaba_base_url(base_url):
                    _eh = dict(kwargs.get("extra_headers") or {})
                    _eh["x-source"] = "opencode"
                    kwargs["extra_headers"] = _eh
                return _orig_completion(model, *args, **kwargs)

            try:
                _litellm.completion = _guarded_completion  # type: ignore[attr-defined]
            except Exception as _exc:  # pragma: no cover
                LOGGER.warning("[Alibaba] Could not install sync LiteLLM guard: %s", _exc)

            if _orig_acompletion is not None:
                async def _guarded_acompletion(  # type: ignore[misc]
                    model: str = "", *args: Any, **kwargs: Any
                ) -> Any:
                    base_url = (
                        str(kwargs.get("base_url") or kwargs.get("api_base") or "").strip()
                        or os.environ.get("OPENAI_BASE_URL", "")
                    )
                    if _is_alibaba_base_url(base_url):
                        _eh = dict(kwargs.get("extra_headers") or {})
                        _eh["x-source"] = "opencode"
                        kwargs["extra_headers"] = _eh
                    return await _orig_acompletion(model, *args, **kwargs)

                try:
                    _litellm.acompletion = _guarded_acompletion  # type: ignore[attr-defined]
                except Exception as _exc:  # pragma: no cover
                    LOGGER.warning("[Alibaba] Could not install async LiteLLM guard: %s", _exc)

            LOGGER.info("[Alibaba] LiteLLM guard installed.")
            _ALIBABA_LITELLM_GUARD_INSTALLED = True
        except Exception as _exc:
            LOGGER.warning("[Alibaba] Could not install LiteLLM guard: %s", _exc)
            # Flag remains False — next caller will retry.


# ---------------------------------------------------------------------------
# Alibaba Coding Plan openai.OpenAI constructor patch — injects x-source header
# on all openai.OpenAI clients targeting Alibaba endpoints.
# ---------------------------------------------------------------------------

_ALIBABA_OPENAI_CTOR_PATCHED: bool = False
_ALIBABA_OPENAI_CTOR_LOCK = __import__("threading").Lock()


def _install_alibaba_openai_client_header_patch() -> None:
    """Monkey-patch openai.OpenAI / AsyncOpenAI so Alibaba clients get x-source: opencode.

    Idempotent — subsequent calls after the first are no-ops.
    Non-Alibaba clients are completely unaffected.
    Double-checked locking prevents duplicate patching under concurrent imports.
    """
    global _ALIBABA_OPENAI_CTOR_PATCHED
    if _ALIBABA_OPENAI_CTOR_PATCHED:
        return
    with _ALIBABA_OPENAI_CTOR_LOCK:
        if _ALIBABA_OPENAI_CTOR_PATCHED:
            return
        # Patching is performed INSIDE the lock — flag set only after success.
        # Same rationale as _install_litellm_alibaba_guard: prevents the race
        # where a concurrent caller reads True before the patch has been applied.
        try:
            import openai as _openai  # noqa: PLC0415

            _orig_sync_init = _openai.OpenAI.__init__

            def _patched_sync_init(
                self: Any,
                *,
                api_key: Any = None,
                base_url: Any = None,
                **kwargs: Any,
            ) -> None:
                _orig_sync_init(self, api_key=api_key, base_url=base_url, **kwargs)
                _resolved = str(getattr(self, "base_url", None) or base_url or "")
                if _is_alibaba_base_url(_resolved) or _is_alibaba_base_url(str(base_url or "")):
                    _inject_opencode_header_to_httpx_client(self)
                    LOGGER.debug("[Alibaba] X-Source: opencode injected on openai.OpenAI (base_url=%s)", _resolved)

            _openai.OpenAI.__init__ = _patched_sync_init  # type: ignore[method-assign]

            _AsyncOpenAI = getattr(_openai, "AsyncOpenAI", None)
            if _AsyncOpenAI is not None:
                _orig_async_init = _AsyncOpenAI.__init__

                def _patched_async_init(
                    self: Any,
                    *,
                    api_key: Any = None,
                    base_url: Any = None,
                    **kwargs: Any,
                ) -> None:
                    _orig_async_init(self, api_key=api_key, base_url=base_url, **kwargs)
                    _resolved = str(getattr(self, "base_url", None) or base_url or "")
                    if _is_alibaba_base_url(_resolved) or _is_alibaba_base_url(str(base_url or "")):
                        _inject_opencode_header_to_httpx_client(self)
                        LOGGER.debug("[Alibaba] X-Source: opencode injected on openai.AsyncOpenAI (base_url=%s)", _resolved)

                _AsyncOpenAI.__init__ = _patched_async_init  # type: ignore[method-assign]

            LOGGER.info("[Alibaba] openai.OpenAI constructor patched — all Alibaba clients get x-source: opencode.")
            _ALIBABA_OPENAI_CTOR_PATCHED = True
        except Exception as _exc:
            LOGGER.warning("[Alibaba] Could not patch openai.OpenAI constructor: %s", _exc)
            # Flag remains False — next caller will retry.


def _prompt_chars_for_runtime_crew(crew: Any, fallback_text: str = "") -> int:
    try:
        prompt_chars = int(getattr(crew, "_prompt_total_chars", 0) or 0)
    except Exception:
        prompt_chars = 0
    if prompt_chars > 0:
        return prompt_chars
    task_total = 0
    for task in list(getattr(crew, "tasks", []) or []):
        description = str(getattr(task, "description", "") or "")
        task_total += len(description)
    if task_total > 0:
        return task_total
    return len(str(fallback_text or ""))


def _prompt_tokens_for_runtime_crew(crew: Any, fallback_text: str = "") -> int:
    return max(0, _prompt_chars_for_runtime_crew(crew, fallback_text) // 3)


def run_librarian_research(
    user_problem: str,
    *,
    mode: str,
    language_hint: str,
    direction_seed_plan: Optional["DirectionSeedPlan"] = None,
) -> Optional["ResearchContext"]:
    global LIBRARIAN_MODEL_ID
    resolved_research_model_id = ""
    if LIBRARIAN_ENABLED:
        resolved_research_model_id = _resolve_librarian_model_id()
        LIBRARIAN_MODEL_ID = resolved_research_model_id
    _update_last_librarian_debug(
        status="started",
        mode=mode,
        language_hint=language_hint,
        providers=list(LIBRARIAN_SEARCH_PROVIDERS),
        librarian_enabled=bool(LIBRARIAN_ENABLED),
        research_model=resolved_research_model_id,
        user_problem_chars=len(user_problem or ""),
        direction_seed_count=len(list(getattr(direction_seed_plan, "directions", []) or [])),
    )
    provider_fingerprint = _librarian_provider_fingerprint()
    cache_payload = {
        "mode": mode,
        "language_hint": language_hint,
        "user_problem_sha256": _text_sha256(user_problem or ""),
        "user_problem_len": len(user_problem or ""),
        "llm_provider": _resolve_llm_provider(),
        "research_model": resolved_research_model_id,
        "provider_fingerprint": provider_fingerprint,
        "cache_window": _cache_window_bucket(LIBRARIAN_CACHE_WINDOW_HOURS),
        "direction_seed_sha256": _text_sha256(
            _model_to_stable_json(direction_seed_plan) if direction_seed_plan else ""
        ),
    }
    cached = _cache_get_pydantic("librarian_research", cache_payload, ResearchContext)
    if cached is not None:
        stabilized_cached = _stabilize_research_context(cached)
        _update_last_librarian_debug(
            status="cache_hit",
            cache_hit=True,
            search_strategy=stabilized_cached.search_strategy,
            providers_used=list(stabilized_cached.providers_used or []),
            citations_count=len(list(stabilized_cached.citations or [])),
            claim_attributions_count=len(list(stabilized_cached.claim_attributions or [])),
            provider_errors=dict(stabilized_cached.provider_errors or {}),
            evidence_coverage=dict(stabilized_cached.evidence_coverage or {}),
        )
        _cost_trace(
            "librarian_research.cache_hit",
            providers="+".join(list(stabilized_cached.providers_used or [])),
            citations=len(list(stabilized_cached.citations or [])),
        )
        print(
            "[Info] Librarian research cache hit: "
            f"providers={','.join(stabilized_cached.providers_used or []) or 'none'} "
            f"citations={len(list(stabilized_cached.citations or []))}"
        )
        try:
            _record_cost(
                stage="librarian_research.cache_hit",
                agent_name="librarian_research",
                input_tokens=len(user_problem) // 3,
                output_tokens=0,
                success=True,
                cache_hit=True,
                outcome="cache_hit",
            )
        except Exception:
            pass
        return stabilized_cached

    # Belt-and-braces: the librarian's provider loop already catches per-provider
    # exceptions (section_04 `_collect_librarian_search_materials`), but
    # post-processing steps (dedupe, citation verification, lane_materials
    # formatting) or unexpected BaseException subclasses (e.g. MemoryError,
    # SystemExit from third-party code) can still escape.  We never want a
    # research-phase failure to abort the whole pipeline — fall back to an
    # empty-materials shell and let the debate phase continue with the
    # fallback research context.
    try:
        materials = _collect_librarian_search_materials(
            user_problem,
            mode=mode,
            language_hint=language_hint,
            direction_seed_plan=direction_seed_plan,
        )
    except _OperationCancelledError:
        raise
    except (KeyboardInterrupt, SystemExit):
        # Never swallow user-initiated interrupts or interpreter shutdown.
        raise
    except Exception as _librarian_exc:
        # Log at WARNING so the user can see WHY research was empty, but do not
        # propagate — downstream phases must run on the fallback context.
        try:
            LOGGER.warning(
                "librarian_search_materials_failed: %s: %s",
                type(_librarian_exc).__name__,
                _librarian_exc,
            )
        except Exception:
            pass
        materials = {
            "problem_breakdown": {},
            "query_map": {},
            "search_language": "en",
            "suggested_search_queries": [],
            "search_strategy": "+".join(LIBRARIAN_SEARCH_PROVIDERS),
            "direction_seed_count": 0,
            "providers_used": [],
            "provider_errors": {
                "librarian": f"{type(_librarian_exc).__name__}: {_librarian_exc}"[:600]
            },
            "citations": [],
            "lane_materials": {},
        }
    fallback_context = _stabilize_research_context(
        _build_fallback_research_context(user_problem, materials)
    )
    _update_last_librarian_debug(
        search_strategy=str(materials.get("search_strategy") or ""),
        providers_used=list(materials.get("providers_used") or []),
        provider_errors=dict(materials.get("provider_errors") or {}),
        suggested_search_queries=list(materials.get("suggested_search_queries") or []),
        collected_citations=len(list(materials.get("citations") or [])),
    )
    if not LIBRARIAN_ENABLED:
        _update_last_librarian_debug(
            status="disabled_fallback",
            fallback_used=True,
            citations_count=len(list(fallback_context.citations or [])),
        )
        _cache_set_pydantic("librarian_research", cache_payload, fallback_context)
        return fallback_context

    librarian_llm = _get_librarian_llm()
    last_error: Optional[Exception] = None
    librarian_prompt_chars = len(str(user_problem or ""))
    for attempt in range(QUALITY_JSON_RETRY_ATTEMPTS):
        try:
            librarian_crew = build_research_swarm_crew(
                user_problem,
                mode=mode,
                language_hint=language_hint,
                llm=librarian_llm,
                research_materials=materials,
            )
            librarian_prompt_chars = _prompt_chars_for_runtime_crew(
                librarian_crew,
                user_problem,
            )
            _cost_trace(
                "librarian_research.kickoff",
                attempt=attempt + 1,
                user_problem_chars=len(user_problem or ""),
                prompt_chars=librarian_prompt_chars,
                providers="+".join(LIBRARIAN_SEARCH_PROVIDERS),
            )
            print(
                "[Info] Librarian research kickoff: "
                f"attempt={attempt + 1} "
                f"providers={','.join(LIBRARIAN_SEARCH_PROVIDERS)}"
            )
            log_event(
                LOGGER,
                20,
                "librarian_kickoff_start",
                "Starting librarian research crew kickoff.",
                attempt=attempt + 1,
                providers=",".join(LIBRARIAN_SEARCH_PROVIDERS),
            )
            librarian_result = kickoff_crew_with_retry(
                librarian_crew,
                logger=LOGGER,
                log_fields={
                    "stage": "librarian_research",
                    "outer_attempt": attempt + 1,
                    "providers": ",".join(LIBRARIAN_SEARCH_PROVIDERS),
                },
            )
        except _OperationCancelledError:
            # Cooperative cancellation must abort the entire librarian research
            # loop — do not record as a per-attempt failure and continue.
            raise
        except Exception as exc:
            last_error = exc
            log_exception(
                LOGGER,
                "librarian_kickoff_failed",
                "Librarian research execution failed.",
                attempt=attempt + 1,
            )
            _update_last_librarian_debug(
                status="execution_error",
                error=str(exc),
                retry_count=attempt,
            )
            try:
                _record_cost(
                    stage="librarian_research.kickoff",
                    agent_name="librarian_research",
                    input_tokens=max(0, librarian_prompt_chars // 3),
                    output_tokens=0,
                    success=False,
                    retry_count=attempt,
                    outcome="execution_error",
                )
            except Exception:
                pass
            continue

        context = extract_research_context(librarian_result)
        raw_candidates = _collect_text_candidates_from_result(librarian_result)
        result_text = _extract_text_from_result(librarian_result) or (
            raw_candidates[-1] if raw_candidates else ""
        )
        if context is None:
            for raw in reversed(raw_candidates):
                context = extract_research_context(raw)
                if context is None and STRICT_JSON_ENABLED:
                    context = _reformat_research_context(
                        raw, llm=librarian_llm, language_hint=language_hint
                    )
                if context is not None:
                    break
        if context is not None:
            # Restore user_problem from the outer parameter when the formatter
            # left it empty (happens when the synthesizer output lacks the field
            # and _preprocess_research_context_dict filled it with "").
            if not context.user_problem and user_problem:
                context.user_problem = user_problem
            material_provider_errors = dict(materials.get("provider_errors") or {})
            context_provider_errors = dict(context.provider_errors or {})
            merged_provider_errors = dict(context_provider_errors)
            merged_provider_errors.update(material_provider_errors)
            context.provider_errors = merged_provider_errors
            context.providers_used = _dedupe_text_items(
                list(materials.get("providers_used") or []) + list(context.providers_used or []),
                limit=max(1, len(LIBRARIAN_SEARCH_PROVIDERS)),
            )
            if not context.search_strategy:
                context.search_strategy = str(materials.get("search_strategy") or "")
            context.suggested_search_queries = _dedupe_text_items(
                list(materials.get("suggested_search_queries") or [])
                + list(context.suggested_search_queries or []),
                limit=12,
            )
            context.citations = _dedupe_citations(
                list(materials.get("citations") or []) + list(context.citations or []),
                limit=LIBRARIAN_MAX_CITATIONS,
            )
            context = _stabilize_research_context(context)
            _update_last_librarian_debug(
                status="success",
                cache_hit=False,
                fallback_used=False,
                search_strategy=context.search_strategy,
                providers_used=list(context.providers_used or []),
                citations_count=len(list(context.citations or [])),
                claim_attributions_count=len(list(context.claim_attributions or [])),
                provider_errors=dict(context.provider_errors or {}),
                evidence_coverage=dict(context.evidence_coverage or {}),
                hallucination_flags_count=len(list(context.hallucination_flags or [])),
            )
            print(
                "[Info] Librarian research completed: "
                f"providers={','.join(context.providers_used or []) or 'none'} "
                f"citations={len(list(context.citations or []))} "
                f"claims={len(list(context.claim_attributions or []))}"
            )
            log_event(
                LOGGER,
                20,
                "librarian_kickoff_done",
                "Librarian research completed successfully.",
                attempt=attempt + 1,
                citations=len(list(context.citations or [])),
                claims=len(list(context.claim_attributions or [])),
            )
            try:
                _record_cost(
                    stage="librarian_research.kickoff",
                    agent_name="librarian_research",
                    input_tokens=max(0, librarian_prompt_chars // 3),
                    output_tokens=len(result_text) // 3,
                    success=True,
                    retry_count=attempt,
                    outcome="success",
                )
            except Exception:
                pass
            _cache_set_pydantic("librarian_research", cache_payload, context)
            return context

    if last_error is not None:
        print(f"[Warn] Librarian research fell back after error: {last_error}")
    _update_last_librarian_debug(
        status="fallback",
        fallback_used=True,
        error=(str(last_error) if last_error is not None else None),
        citations_count=len(list(fallback_context.citations or [])),
        claim_attributions_count=len(list(fallback_context.claim_attributions or [])),
        provider_errors=dict(fallback_context.provider_errors or {}),
        evidence_coverage=dict(fallback_context.evidence_coverage or {}),
    )
    _cache_set_pydantic("librarian_research", cache_payload, fallback_context)
    return fallback_context


def _resolve_direction_refinement_runtime_defaults() -> Dict[str, Any]:
    _raw_dir_max_iter = _env_int("DIRECTION_REFINEMENT_MAX_ITERATIONS", 2)
    return {
        "max_iterations": max(1, _raw_dir_max_iter if _raw_dir_max_iter is not None else 2),
        "enabled": _env_bool("DIRECTION_REFINEMENT_ENABLED", True),
    }


_DIRECTION_REFINEMENT_RUNTIME_DEFAULTS = _resolve_direction_refinement_runtime_defaults()
DIRECTION_REFINEMENT_MAX_ITERATIONS = _DIRECTION_REFINEMENT_RUNTIME_DEFAULTS["max_iterations"]
DIRECTION_REFINEMENT_ENABLED = _DIRECTION_REFINEMENT_RUNTIME_DEFAULTS["enabled"]


def _build_refinement_research_queries(
    gap_info: Dict[str, Any],
    user_problem: str,
) -> List[str]:
    queries: List[str] = []
    for unknown in list(gap_info.get("critical_unknowns") or []):
        if unknown:
            queries.append(f"evidence for: {unknown}")
    for area in list(gap_info.get("missing_evidence_areas") or []):
        if area:
            queries.append(f"research: {area}")
    for query in list(gap_info.get("research_queries") or []):
        if query and query not in queries:
            queries.append(query)
    weak_directions = list(gap_info.get("weak_directions") or [])
    if weak_directions:
        queries.append(f"comparative evidence for directions: {', '.join(weak_directions[:3])}")
    if not queries:
        queries.append(f"additional evidence for: {user_problem[:100]}")
    return queries[:8]


def _run_direction_research_refinement(
    user_problem: str,
    *,
    mode: str,
    language_hint: str,
    gap_info: Dict[str, Any],
    existing_context: Optional["ResearchContext"],
    direction_seed_plan: Optional["DirectionSeedPlan"] = None,
) -> Optional["ResearchContext"]:
    if not DIRECTION_REFINEMENT_ENABLED:
        return None
    refinement_queries = _build_refinement_research_queries(gap_info, user_problem)
    if not refinement_queries:
        return existing_context
    print(
        f"[Info] Running research refinement for gaps: "
        f"queries={len(refinement_queries)} "
        f"critical_unknowns={len(list(gap_info.get('critical_unknowns') or []))} "
        f"weak_directions={list(gap_info.get('weak_directions') or [])[:3]}"
    )
    combined_problem = f"{user_problem}\n\nAdditional research focus:\n"
    for i, query in enumerate(refinement_queries, 1):
        combined_problem += f"{i}. {query}\n"
    refined_context = run_librarian_research(
        combined_problem,
        mode=mode,
        language_hint=language_hint,
        direction_seed_plan=direction_seed_plan,
    )
    if refined_context is None:
        return existing_context
    if existing_context is not None:
        merged_citations = list(existing_context.citations or []) + list(
            refined_context.citations or []
        )
        merged_claims = list(existing_context.claim_attributions or []) + list(
            refined_context.claim_attributions or []
        )
        merged_unknowns = list(existing_context.unknowns or []) + list(
            refined_context.unknowns or []
        )
        merged_technical = list(existing_context.technical_patterns or []) + list(
            refined_context.technical_patterns or []
        )
        merged_risks = list(existing_context.key_risks or []) + list(
            refined_context.key_risks or []
        )
        refined_context.citations = _dedupe_citations(
            merged_citations, limit=LIBRARIAN_MAX_CITATIONS
        )
        seen_claims: set = set()
        deduped_claims = []
        for claim in merged_claims:
            claim_text = getattr(claim, "claim", "") or ""
            if claim_text and claim_text not in seen_claims:
                seen_claims.add(claim_text)
                deduped_claims.append(claim)
        refined_context.claim_attributions = deduped_claims[:LIBRARIAN_MAX_CITATIONS]
        refined_context.unknowns = _dedupe_text_items(merged_unknowns, limit=12)
        refined_context.technical_patterns = _dedupe_text_items(merged_technical, limit=15)
        refined_context.key_risks = _dedupe_text_items(merged_risks, limit=12)
        old_coverage = dict(existing_context.evidence_coverage or {})
        new_coverage = dict(refined_context.evidence_coverage or {})
        merged_coverage = {
            "grounded_claims": max(
                int(old_coverage.get("grounded_claims") or 0),
                int(new_coverage.get("grounded_claims") or 0),
            ),
            "citations": max(
                int(old_coverage.get("citations") or 0),
                int(new_coverage.get("citations") or 0),
            ),
        }
        refined_context.evidence_coverage = merged_coverage
        refined_context = _stabilize_research_context(refined_context)
    print(
        f"[Info] Research refinement completed: "
        f"citations={len(list(refined_context.citations or []))} "
        f"claims={len(list(refined_context.claim_attributions or []))} "
        f"unknowns={len(list(refined_context.unknowns or []))}"
    )
    return refined_context


def _extract_direction_stage_reports(
    result: Any,
    *,
    llm: Any,
    language_hint: str,
    stage_index_map: Optional[Dict[str, int]] = None,
) -> Tuple[Optional["DirectionComparatorReport"], Optional["EvidenceAuditReport"]]:
    task_outputs = _get_task_outputs(result)
    comparator_candidates: List[DirectionComparatorReport] = []
    audit_candidates: List[EvidenceAuditReport] = []
    for task_output in task_outputs:
        comparator_candidate = extract_direction_comparator_report(task_output)
        if comparator_candidate is not None:
            comparator_candidates.append(comparator_candidate)
        audit_candidate = extract_evidence_audit_report(task_output)
        if audit_candidate is not None:
            audit_candidates.append(audit_candidate)

    stage_index_map = dict(stage_index_map or {})
    comparator_index = int(stage_index_map.get("comparator", 1))
    auditor_index = int(stage_index_map.get("auditor", 3))

    comparator_report: Optional["DirectionComparatorReport"] = None
    if len(comparator_candidates) == 1:
        comparator_report = comparator_candidates[0]
    elif comparator_candidates:
        comparator_report = extract_direction_comparator_report(
            _get_task_output_at_index(result, comparator_index)
        )
        if comparator_report is None:
            comparator_report = comparator_candidates[-1]
    else:
        comparator_report = extract_direction_comparator_report(
            _get_task_output_at_index(result, comparator_index)
        )
    if comparator_report is None:
        comparator_raw = (
            _extract_text_from_result(_get_task_output_at_index(result, comparator_index)) or ""
        )
        if comparator_raw:
            comparator_report = _reformat_direction_comparator_report(
                comparator_raw,
                llm=llm,
                language_hint=language_hint,
            )

    audit_report: Optional["EvidenceAuditReport"] = None
    if len(audit_candidates) == 1:
        audit_report = audit_candidates[0]
    elif audit_candidates:
        audit_report = extract_evidence_audit_report(
            _get_task_output_at_index(result, auditor_index)
        )
        if audit_report is None:
            audit_report = audit_candidates[-1]
    else:
        audit_report = extract_evidence_audit_report(
            _get_task_output_at_index(result, auditor_index)
        )
    if audit_report is None:
        audit_raw = (
            _extract_text_from_result(_get_task_output_at_index(result, auditor_index)) or ""
        )
        if audit_raw:
            audit_report = _reformat_evidence_audit_report(
                audit_raw,
                llm=llm,
                language_hint=language_hint,
            )

    return comparator_report, audit_report


def _load_direction_debate_artifacts(
    cache_payload: Dict[str, Any],
) -> Optional["DirectionDebateArtifacts"]:
    artifacts = _cache_get_pydantic(
        "direction_debate_artifacts",
        cache_payload,
        DirectionDebateArtifacts,
    )
    if artifacts is None:
        return None
    if artifacts.comparator_report is not None:
        artifacts.comparator_report = _normalize_direction_comparator_report_instance(
            artifacts.comparator_report
        )
    if artifacts.audit_report is not None:
        artifacts.audit_report = _normalize_evidence_audit_report_instance(artifacts.audit_report)
    return artifacts


def _store_direction_debate_artifacts(
    cache_payload: Dict[str, Any],
    *,
    comparator_report: Optional["DirectionComparatorReport"],
    audit_report: Optional["EvidenceAuditReport"],
) -> None:
    if comparator_report is None and audit_report is None:
        return
    artifacts = DirectionDebateArtifacts(
        comparator_report=comparator_report,
        audit_report=audit_report,
    )
    _cache_set_pydantic("direction_debate_artifacts", cache_payload, artifacts)


def _run_single_direction_debate(
    user_problem: str,
    *,
    mode: str,
    language_hint: str,
    llm: Any,
    research_context: Optional["ResearchContext"],
    direction_judge_llm: Any,
    cache_payload: Dict[str, Any],
) -> Tuple[
    Optional["DirectionDecision"],
    Optional["DirectionComparatorReport"],
    Optional["EvidenceAuditReport"],
    Optional[Dict[str, Any]],
]:
    # v1.1.8 — Direction Debate Audit Mode env reads.  These flow into the
    # crew builder so each task description gets the structured-finding
    # appendix, and into the post-result emit pipeline so debate_finding /
    # gate_verdict ledger events are written.  Defaults preserve pre-v1.1.8
    # behaviour bit-for-bit (audit_mode off, sequential isolation).
    #
    # Uses ``_env_bool`` (section_00's wrapper that delegates to ``_env.env_bool``
    # with ``extended=True``) — matches the project's env-bool whitelist rule;
    # raw ``os.environ.get`` is used for the string ``ISOLATION_MODE`` read.
    audit_mode_enabled = _env_bool("CRUCIBLE_DEBATE_AUDIT_MODE", False)
    isolation_mode = (
        os.environ.get("CRUCIBLE_DEBATE_ISOLATION_MODE", "sequential") or "sequential"
    ).strip().lower()
    if isolation_mode not in ("sequential", "hybrid"):
        isolation_mode = "sequential"
    external_critic_enabled = _env_bool("CRUCIBLE_DEBATE_EXTERNAL_CRITIC", False)
    critic_can_override = _env_bool("CRUCIBLE_DEBATE_CRITIC_OVERRIDE_PROCEED", False)
    last_error: Optional[Exception] = None
    for attempt in range(QUALITY_JSON_RETRY_ATTEMPTS):
        attempt_started_at = time.perf_counter()
        stage_index_map: Optional[Dict[str, int]] = None
        direction_prompt_chars = len(str(user_problem or ""))
        try:
            direction_crew = build_direction_debate_crew(
                user_problem,
                mode=mode,
                language_hint=language_hint,
                llm=llm,
                direction_judge_llm=direction_judge_llm,
                research_context=research_context,
                audit_mode=audit_mode_enabled,
                isolation_mode=isolation_mode,
            )
            direction_prompt_chars = _prompt_chars_for_runtime_crew(
                direction_crew,
                user_problem,
            )
            stage_index_map = _build_direction_stage_index_map(getattr(direction_crew, "tasks", []))
            if attempt == 0:
                print(
                    "[Info] Direction debate runtime: "
                    f"all_stages_model={_llm_model_id(direction_judge_llm)} "
                    f"llm_timeout={OPENROUTER_LLM_TIMEOUT_SECONDS}s "
                    "stages=explorer,comparator,skeptic,auditor,judge"
                )
            _cost_trace(
                "direction_debate.kickoff",
                attempt=attempt + 1,
                user_problem_chars=len(user_problem),
                prompt_chars=direction_prompt_chars,
            )
            log_event(
                LOGGER,
                20,
                "direction_debate_kickoff_start",
                "Starting direction debate crew kickoff.",
                attempt=attempt + 1,
            )
            direction_result = kickoff_crew_with_retry(
                direction_crew,
                logger=LOGGER,
                log_fields={
                    "stage": "direction_debate",
                    "outer_attempt": attempt + 1,
                },
            )
            elapsed_seconds = time.perf_counter() - attempt_started_at
            _cost_trace(
                "direction_debate.kickoff.done",
                attempt=attempt + 1,
                elapsed_s=f"{elapsed_seconds:.1f}",
            )
            print(f"[Info] Direction debate kickoff completed in {elapsed_seconds:.1f}s.")
        except _OperationCancelledError:
            # Cooperative cancellation must abort the entire direction debate
            # loop — do not record as a per-attempt failure and continue.
            raise
        except Exception as e:
            elapsed_seconds = time.perf_counter() - attempt_started_at
            last_error = e
            log_exception(
                LOGGER,
                "direction_debate_kickoff_failed",
                "Direction debate execution failed.",
                attempt=attempt + 1,
                elapsed_seconds=f"{elapsed_seconds:.1f}",
            )
            try:
                _record_cost(
                    stage="direction_debate.kickoff",
                    agent_name="direction_debate",
                    input_tokens=max(0, direction_prompt_chars // 3),
                    output_tokens=0,
                    success=False,
                    retry_count=attempt,
                    outcome="execution_error",
                )
            except Exception:
                pass
            dump_path = _write_direction_debate_debug_dump(
                user_problem=user_problem,
                attempt=attempt + 1,
                llm=llm,
                direction_judge_llm=direction_judge_llm,
                elapsed_seconds=elapsed_seconds,
                stage_index_map=stage_index_map,
                result=None,
                raw_candidates=[],
                decision=None,
                comparator_report=None,
                audit_report=None,
                exception=e,
                note="kickoff_exception",
            )
            print(f"[Error] Direction debate execution failed after {elapsed_seconds:.1f}s: {e}")
            if dump_path:
                print(f"[Info] Direction debate debug dump: {dump_path}")
            continue
        log_event(
            LOGGER,
            20,
            "direction_debate_kickoff_done",
            "Direction debate kickoff completed.",
            attempt=attempt + 1,
            elapsed_seconds=f"{elapsed_seconds:.1f}",
        )

        decision = extract_direction_decision(direction_result)
        raw_candidates = _collect_text_candidates_from_result(direction_result)
        result_text = _extract_text_from_result(direction_result) or (
            raw_candidates[-1] if raw_candidates else ""
        )
        comparator_report, audit_report = _extract_direction_stage_reports(
            direction_result,
            llm=direction_judge_llm,
            language_hint=language_hint,
            stage_index_map=stage_index_map,
        )
        if decision is None:
            for raw in reversed(raw_candidates):
                decision = extract_direction_decision(raw)
                if decision is None and STRICT_JSON_ENABLED:
                    decision = _reformat_direction_decision(
                        raw, llm=direction_judge_llm, language_hint=language_hint
                    )
                if decision is not None:
                    break
        if decision is None:
            decision = _salvage_direction_decision_from_result(direction_result)
        if decision is None:
            decision = _build_provisional_direction_decision_from_stage_reports(
                direction_result,
                research_context=research_context,
                comparator_report=comparator_report,
                audit_report=audit_report,
            )

        # v1.1.8 — Direction Debate Audit Mode emit pipeline.
        # Runs unconditionally on every attempt when audit_mode is enabled.
        # Parses AUDIT_FINDING + GATE_VERDICT blocks from the crew result,
        # runs deterministic consensus-risk computation, optionally invokes
        # the External Critic, and emits ``debate_finding`` + ``gate_verdict``
        # ledger events.  Completely swallow-safe; never breaks the legacy
        # path below.  Returns the parsed verdict + risk dicts but those
        # are not used to override the legacy ``force_none`` flow — v1.1.8
        # audit mode is observation-only by design (back-compat).  v1.2.0
        # may introduce true override behaviour with a separate env gate.
        _audit_verdict_dict, _audit_consensus_risk = _emit_audit_mode_ledger_events(
            direction_result=direction_result,
            judge_decision=decision,
            judge_summary=str(getattr(decision, "summary", "") or "") if decision is not None else "",
            mode=mode,
            user_problem=user_problem,
            attempt=attempt + 1,
            audit_mode_enabled=audit_mode_enabled,
            isolation_mode=isolation_mode,
            external_critic_enabled=external_critic_enabled,
            critic_can_override=critic_can_override,
            direction_judge_llm=direction_judge_llm,
            research_context=research_context,
            language_hint=language_hint,
        )

        if decision is not None:
            ranking = (
                _build_deterministic_direction_ranking(decision, research_context)
                if research_context
                else []
            )
            force_none, reason, gap_info = _should_force_direction_none(
                decision,
                research_context=research_context,
                comparator_report=comparator_report,
                audit_report=audit_report,
                deterministic_ranking=ranking,
            )
            # v1.1.2 (audit fix G2-A2-HIGH-2): the previous gate predicate
            # required force_none AND a non-empty ``gap_info`` shape (critical
            # unknowns / weak directions / citations needed).  A legitimate
            # force-none verdict with a non-evidence-shaped reason (e.g.
            # ``judge_explicit_none``, ``unanimous_reject``) carried no
            # gap_info entries, so the entire telemetry path was silently
            # skipped: no ``[Warn]`` print, no debug dump, no
            # ``record_direction_debate_rejection`` ledger row.  That left
            # v1.2.0 retrieval blind to one of the most actionable rejection
            # classes — the judge explicitly killing a direction with no
            # evidence-shaped reason.  We now ALWAYS emit telemetry on
            # force_none; ``gap_info`` only controls how detailed the
            # diagnostic dump is.
            if force_none:
                _has_gap_detail = bool(
                    gap_info.get("critical_unknowns")
                    or gap_info.get("missing_evidence_areas")
                    or gap_info.get("research_queries")
                    or gap_info.get("weak_directions")
                    or gap_info.get("grounded_claims_needed", 0) > 0
                    or gap_info.get("citations_needed", 0) > 0
                )
                # Diagnostic surface: the force-none gate used to return silently,
                # so a user looking at the runner output had no idea which condition
                # killed the decision (citations? grounded claims? weak directions?).
                # Print the actual numbers and dump a JSON debug artefact so the
                # next iteration / user can act on it.
                _coverage_now: Dict[str, Any] = {}
                try:
                    _coverage_now = dict(getattr(research_context, "evidence_coverage", {}) or {})
                except Exception:
                    _coverage_now = {}
                _citations_now = 0
                _attributions_now = 0
                try:
                    _citations_now = len(list(getattr(research_context, "citations", []) or []))
                    _attributions_now = len(
                        list(getattr(research_context, "claim_attributions", []) or [])
                    )
                except Exception:
                    pass
                print(
                    "[Warn] Direction debate force-none gate fired "
                    f"(reason={reason!r} attempt={attempt + 1} "
                    f"citations={_citations_now} "
                    f"grounded_claims={int(_coverage_now.get('grounded_claims') or 0)} "
                    f"grounded_summary_claims={int(_coverage_now.get('grounded_summary_claims') or 0)} "
                    f"claim_attributions={_attributions_now} "
                    f"weak_directions={list(gap_info.get('weak_directions') or [])[:3]} "
                    f"critical_unknowns={list(gap_info.get('critical_unknowns') or [])[:3]})."
                )
                if _has_gap_detail:
                    dump_path = _write_direction_debate_debug_dump(
                        user_problem=user_problem,
                        attempt=attempt + 1,
                        llm=llm,
                        direction_judge_llm=direction_judge_llm,
                        elapsed_seconds=elapsed_seconds,
                        stage_index_map=stage_index_map,
                        result=direction_result,
                        raw_candidates=raw_candidates,
                        decision=decision,
                        comparator_report=comparator_report,
                        audit_report=audit_report,
                        note=f"force_none:{reason}",
                    )
                    if dump_path:
                        print(f"[Info] Direction debate debug dump: {dump_path}")
                # v1.1.0 run_insights: record direction-debate rejection.
                # The force-none gate is the most actionable failure surface
                # for the v1.2.0 retrieval layer — it tells us which signals
                # consistently produce un-grounded directions so a future
                # run with the same signals can avoid them.  Best-effort,
                # never raises.  v1.1.2: emitted unconditionally on
                # force_none, regardless of gap_info shape (see above).
                try:
                    _judge_verdict = ""
                    if decision is not None:
                        _judge_verdict = str(getattr(decision, "summary", "") or "")
                    _get_insights_recorder().record_direction_debate_rejection(
                        # v1.1.2 (sixth-pass H-3): three-tier fallback so an
                        # early force-none emit cannot write ``run_id=""``
                        # into the ledger.  ``mode`` also coerced to a
                        # ``mode_unknown`` sentinel for v1.2.0 retrieval-
                        # aggregation parity with resilience.error_record.
                        run_id=_resolve_run_id_for_ledger_emit(),
                        project_name="stage0_pending",
                        mode=str(mode or "mode_unknown"),
                        direction_id=str(getattr(decision, "selected_direction", "") or "unknown"),
                        rejection_reason="force_none",
                        judge_verdict=f"force_none:{reason} | {_judge_verdict}",
                        attempt=attempt + 1,
                        user_problem=user_problem,
                        run_meta=None,
                    )
                except Exception:
                    pass
                return None, comparator_report, audit_report, gap_info
            decision = _apply_deterministic_direction_rerank(
                decision,
                research_context=research_context,
                comparator_report=comparator_report,
                audit_report=audit_report,
            )
            decision = _cap_direction_decision_confidence(
                decision,
                research_context=research_context,
                comparator_report=comparator_report,
                audit_report=audit_report,
            )
            try:
                _record_cost(
                    stage="direction_debate.kickoff",
                    agent_name="direction_debate",
                    input_tokens=max(0, direction_prompt_chars // 3),
                    output_tokens=len(result_text) // 3,
                    success=True,
                    retry_count=attempt,
                    outcome="success",
                )
            except Exception:
                pass
            _store_direction_debate_artifacts(
                cache_payload,
                comparator_report=comparator_report,
                audit_report=audit_report,
            )
            _cache_set_pydantic("direction_debate", cache_payload, decision)
            return decision, comparator_report, audit_report, None

        print(
            "[Warn] DirectionDecision parse failed after all fallbacks. "
            f"raw_candidates={len(raw_candidates)} "
            f"comparator={'yes' if comparator_report is not None else 'no'} "
            f"auditor={'yes' if audit_report is not None else 'no'}"
        )
        dump_path = _write_direction_debate_debug_dump(
            user_problem=user_problem,
            attempt=attempt + 1,
            llm=llm,
            direction_judge_llm=direction_judge_llm,
            elapsed_seconds=elapsed_seconds,
            stage_index_map=stage_index_map,
            result=direction_result,
            raw_candidates=raw_candidates,
            decision=decision,
            comparator_report=comparator_report,
            audit_report=audit_report,
            note="parse_failed_after_fallbacks",
        )
        if dump_path:
            print(f"[Info] Direction debate debug dump: {dump_path}")
        # v1.1.0 run_insights: record parse-failed rejection.
        try:
            _get_insights_recorder().record_direction_debate_rejection(
                # v1.1.2 (sixth-pass H-3): three-tier fallback as above.
                run_id=_resolve_run_id_for_ledger_emit(),
                project_name="stage0_pending",
                mode=str(mode or "mode_unknown"),
                direction_id=str(getattr(decision, "selected_direction", "") or "unknown"),
                rejection_reason="judge_no_winner",
                judge_verdict="parse_failed_after_fallbacks",
                attempt=attempt + 1,
                user_problem=user_problem,
            )
        except Exception:
            pass
        try:
            _record_cost(
                stage="direction_debate.kickoff",
                agent_name="direction_debate",
                input_tokens=max(0, direction_prompt_chars // 3),
                output_tokens=len(result_text) // 3,
                success=False,
                retry_count=attempt,
                outcome="parse_failed",
            )
        except Exception:
            pass

        if attempt < QUALITY_JSON_RETRY_ATTEMPTS - 1:
            print("[Warn] DirectionDecision not parsed; retrying direction debate...")

    return None, None, None, None


def _fallback_direction_seed_plan(
    user_problem: str,
    *,
    mode: str,
) -> "DirectionSeedPlan":
    mode_name = str(_get_mode_config(mode).name or "").strip().lower()
    if mode_name not in {"quant", "saas", "agent", "scientist"}:
        raise ValueError(
            f"Resolved mode config produced invalid project type {mode_name!r}. "
            "Expected one of: quant, saas, agent, scientist"
        )
    normalized_problem = re.sub(r"\s+", " ", user_problem or "").strip()
    problem_excerpt = limit_text(normalized_problem, 180) or "the user problem"
    if mode_name == "quant":
        directions = [
            DirectionSeedIdea(
                label="Rule-based alpha",
                thesis="Test a simple rule-based signal that can be backtested quickly.",
                why_now="Fastest way to validate whether the idea has any tradable edge.",
                search_terms=["rule based strategy", "backtest", "slippage", "drawdown"],
            ),
            DirectionSeedIdea(
                label="Adaptive regime model",
                thesis="Use a regime-aware or rolling-window model to adapt the strategy by market state.",
                why_now="Useful when the idea likely behaves differently across volatility regimes.",
                search_terms=[
                    "regime switching",
                    "rolling window",
                    "volatility regime",
                    "execution risk",
                ],
            ),
            DirectionSeedIdea(
                label="Risk overlay first",
                thesis="Treat the idea as a signal source and focus on execution gating plus risk overlay.",
                why_now="Lets research verify whether implementation risk dominates raw alpha.",
                search_terms=["risk overlay", "execution control", "position sizing", "stop loss"],
            ),
        ]
    elif mode_name == "agent":
        directions = [
            DirectionSeedIdea(
                label="Single-agent baseline",
                thesis="Start from one deterministic agent with explicit tools and retries.",
                why_now="Provides the cleanest baseline before adding orchestration complexity.",
                search_terms=["single agent workflow", "tool calling", "retry", "idempotent"],
            ),
            DirectionSeedIdea(
                label="Multi-agent review loop",
                thesis="Split planning, execution, and critique into separate agent roles.",
                why_now="Useful when the idea depends on structured debate or adversarial review.",
                search_terms=[
                    "multi agent orchestration",
                    "critic agent",
                    "planner executor",
                    "review loop",
                ],
            ),
            DirectionSeedIdea(
                label="Human-in-loop control",
                thesis="Keep key approval gates with humans while automating the low-risk steps.",
                why_now="Good when autonomy risk is high or requirements are still unstable.",
                search_terms=[
                    "human in the loop",
                    "approval workflow",
                    "agent governance",
                    "audit trail",
                ],
            ),
        ]
    elif mode_name == "saas":
        directions = [
            DirectionSeedIdea(
                label="Workflow painkiller",
                thesis="Turn the idea into a narrow workflow product that removes one painful step.",
                why_now="Often the fastest path to real demand validation.",
                search_terms=[
                    "workflow automation",
                    "pain point",
                    "time saving",
                    "adoption blocker",
                ],
            ),
            DirectionSeedIdea(
                label="Ops platform layer",
                thesis="Position the idea as an operational layer that coordinates existing tools.",
                why_now="Useful when buyers already have fragmented tools but lack orchestration.",
                search_terms=[
                    "operations platform",
                    "integration",
                    "orchestration",
                    "existing tools",
                ],
            ),
            DirectionSeedIdea(
                label="Embedded insight wedge",
                thesis="Treat the idea as an insight or analytics wedge embedded into an existing workflow.",
                why_now="Can reduce switching cost and narrow the first launch surface.",
                search_terms=[
                    "embedded analytics",
                    "decision support",
                    "dashboard",
                    "workflow integration",
                ],
            ),
        ]
    elif mode_name == "scientist":
        directions = [
            DirectionSeedIdea(
                label="Faithful paper reproduction",
                thesis="Implement the exact algorithm from the paper with the same hyperparameters and dataset to verify the reported results.",
                why_now="Establishing a faithful baseline is the prerequisite for any extension or ablation.",
                search_terms=[
                    "reproduce paper results",
                    "replication study",
                    "implementation details",
                    "baseline code",
                ],
            ),
            DirectionSeedIdea(
                label="Ablation-driven analysis",
                thesis="Systematically remove or replace key components of the algorithm to quantify each component's contribution to performance.",
                why_now="Ablations expose which design choices are essential and which are incidental, guiding future research directions.",
                search_terms=[
                    "ablation study",
                    "component analysis",
                    "contribution analysis",
                    "model variants",
                ],
            ),
            DirectionSeedIdea(
                label="Benchmark comparison",
                thesis="Compare the paper's method against two or more published baselines on a shared benchmark dataset.",
                why_now="Positions the implementation relative to the state of the art and validates the claimed improvement.",
                search_terms=[
                    "benchmark comparison",
                    "state of the art",
                    "baseline methods",
                    "evaluation protocol",
                ],
            ),
        ]
    return DirectionSeedPlan(
        summary=f"Fallback provisional directions for {problem_excerpt}.",
        directions=directions,
    )


def _normalize_direction_seed_plan(
    plan: Optional["DirectionSeedPlan"],
    *,
    user_problem: str,
    mode: str,
) -> "DirectionSeedPlan":
    if plan is None:
        return _fallback_direction_seed_plan(user_problem, mode=mode)
    normalized_directions: List["DirectionSeedIdea"] = []
    seen_keys: Set[str] = set()
    for index, item in enumerate(list(getattr(plan, "directions", []) or []), start=1):
        label = str(getattr(item, "label", "") or "").strip() or f"Direction {index}"
        thesis = str(getattr(item, "thesis", "") or "").strip()
        if not thesis:
            continue
        dedupe_key = re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "", f"{label} {thesis}".lower())
        if not dedupe_key or dedupe_key in seen_keys:
            continue
        seen_keys.add(dedupe_key)
        why_now = str(getattr(item, "why_now", "") or "").strip()
        search_terms = _dedupe_text_items(
            list(getattr(item, "search_terms", []) or []) + [label, thesis],
            limit=6,
        )
        normalized_directions.append(
            DirectionSeedIdea(
                label=limit_text(label, 80),
                thesis=limit_text(thesis, 220),
                why_now=limit_text(why_now or thesis, 180),
                search_terms=search_terms,
            )
        )
    if not normalized_directions:
        return _fallback_direction_seed_plan(user_problem, mode=mode)
    summary = str(getattr(plan, "summary", "") or "").strip()
    if not summary:
        summary = "Provisional strategy directions generated before librarian research."
    return DirectionSeedPlan(
        summary=limit_text(summary, 240),
        directions=normalized_directions[:5],
    )


def _build_direction_seed_plan(
    user_problem: str,
    *,
    mode: str,
    language_hint: str,
    llm: Any,
) -> "DirectionSeedPlan":
    cache_payload = {
        "user_problem_sha256": _text_sha256(user_problem or ""),
        "mode": mode,
        "language_hint": language_hint,
        "llm_provider": _resolve_llm_provider(),
        "llm_model_id": _llm_model_id(llm),
        "version": "v1_direction_seed_plan",
    }
    cached = _cache_get_pydantic("direction_seed_plan", cache_payload, DirectionSeedPlan)
    if cached is not None:
        return _normalize_direction_seed_plan(cached, user_problem=user_problem, mode=mode)

    mode_config = _get_mode_config(mode)
    planner = Agent(
        role="Strategy Seed Planner",
        goal="Generate a small set of rough strategic directions before external research begins.",
        backstory=(
            f"You are the first-pass strategist for {mode_config.name} mode.\n"
            "- Produce rough directions only; do not pretend they are validated.\n"
            "- The goal is to give Librarian concrete directions to research, not to make the final call.\n"
            "- Directions must be meaningfully distinct."
        ),
        allow_delegation=False,
        verbose=False,
        llm=llm,
    )
    task_kwargs = {
        "description": (
            "Generate 3 to 5 rough strategic directions for the user problem before research begins.\n"
            "These are provisional hypotheses for Librarian to validate.\n"
            "Each direction must include:\n"
            "- label: short direction label\n"
            "- thesis: one concise direction thesis\n"
            "- why_now: why this direction is worth researching now\n"
            "- search_terms: 3 to 6 concrete search terms Librarian should use\n"
            "Rules:\n"
            "- Output JSON only.\n"
            "- Keep directions distinct.\n"
            "- Do not claim evidence you do not have yet.\n"
            f"Language hint: {language_hint}\n"
            f"Mode: {mode_config.name}\n\n"
            f"User problem:\n{user_problem}\n\n"
            "Return JSON:\n"
            "{\n"
            '  "summary": "...",\n'
            '  "directions": [\n'
            "    {\n"
            '      "label": "...",\n'
            '      "thesis": "...",\n'
            '      "why_now": "...",\n'
            '      "search_terms": ["...", "..."]\n'
            "    }\n"
            "  ]\n"
            "}"
        ),
        "agent": planner,
        "expected_output": "DirectionSeedPlan JSON only.",
    }
    if STRICT_JSON_ENABLED and CREWAI_OUTPUT_PYDANTIC:
        task_kwargs["output_pydantic"] = DirectionSeedPlan
    crew = Crew(
        agents=[planner],
        tasks=[Task(**task_kwargs)],
        process=Process.sequential,
        verbose=False,
    )
    setattr(
        crew,
        "_retry_policy",
        RetryPolicy(max_attempts=20, backoff_seconds=2.0, retry_on_json_fail=True),
    )
    setattr(crew, "_crew_name", "direction_seed_planner")
    try:
        log_event(
            LOGGER,
            20,
            "direction_seed_kickoff_start",
            "Starting direction seed planner kickoff.",
            mode=mode,
        )
        result = kickoff_crew_with_retry(
            crew,
            logger=LOGGER,
            log_fields={"stage": "direction_seed_plan", "mode": mode},
        )
        raw_candidates = _collect_text_candidates_from_result(result)
        parsed_plan: Optional["DirectionSeedPlan"] = None
        for raw in reversed(raw_candidates):
            parsed = _extract_first_json_object(raw)
            if not isinstance(parsed, dict):
                continue
            try:
                parsed_plan = DirectionSeedPlan(**parsed)
                break
            except Exception:
                continue
        normalized = _normalize_direction_seed_plan(
            parsed_plan, user_problem=user_problem, mode=mode
        )
        _cache_set_pydantic("direction_seed_plan", cache_payload, normalized)
        log_event(
            LOGGER,
            20,
            "direction_seed_kickoff_done",
            "Direction seed planner completed.",
            mode=mode,
            directions=len(list(normalized.directions or [])),
        )
        return normalized
    except _OperationCancelledError:
        # Cooperative cancellation must propagate — returning a fallback
        # direction plan would allow the pipeline to continue running after the
        # user has explicitly requested cancellation.
        raise
    except Exception as exc:
        log_exception(
            LOGGER,
            "direction_seed_kickoff_failed",
            "Direction seed planning failed; using fallback directions.",
            mode=mode,
        )
        print(f"[Warn] Direction seed planning failed; using fallback directions: {exc}")
        return _fallback_direction_seed_plan(user_problem, mode=mode)


def _render_direction_seed_block(direction_seed_plan: Optional["DirectionSeedPlan"]) -> str:
    if direction_seed_plan is None or not list(direction_seed_plan.directions or []):
        return ""
    lines = [
        "=== INITIAL STRATEGY DIRECTIONS ===",
        f"Summary: {direction_seed_plan.summary}",
    ]
    for index, direction in enumerate(direction_seed_plan.directions, start=1):
        lines.append(f"{index}. {direction.label}")
        lines.append(f"   Thesis: {direction.thesis}")
        lines.append(f"   Why now: {direction.why_now}")
        if direction.search_terms:
            lines.append("   Search terms: " + ", ".join(direction.search_terms[:6]))
    return "\n".join(lines)


def _selected_direction_option(
    decision: Optional["DirectionDecision"],
) -> Optional["DirectionOption"]:
    if decision is None:
        return None
    selected_key = str(getattr(decision, "selected_direction", "") or "").strip().upper()
    if not selected_key or selected_key == "NONE":
        return None
    for option in list(getattr(decision, "options", []) or []):
        if str(getattr(option, "key", "") or "").strip().upper() == selected_key:
            return option
    return None


def _extract_direction_feedback_focus_terms(
    feedback_note: Optional[str],
) -> List[str]:
    if not feedback_note:
        return []
    focus_terms: List[str] = []
    for raw_line in str(feedback_note or "").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("-"):
            line = line[1:].strip()
        if not line:
            continue
        if ":" in line:
            prefix, suffix = line.split(":", 1)
            normalized_prefix = prefix.strip().lower()
            if normalized_prefix in {
                "feedback path",
                "requested analyst reruns",
                "requested analyst rerun",
                "rerun attempt",
                "rerun reasons",
                "rerun reason",
            }:
                line = suffix.strip()
        if not line:
            continue
        focus_terms.append(limit_text(line, 120))
    return _dedupe_text_items(focus_terms, limit=8)


def _build_incumbent_direction_seed_plan(
    incumbent_direction: "DirectionDecision",
    *,
    user_problem: str,
    feedback_note: Optional[str],
) -> "DirectionSeedPlan":
    selected_key = str(getattr(incumbent_direction, "selected_direction", "") or "").strip().upper()
    option = _selected_direction_option(incumbent_direction)
    label = (
        f"Incumbent direction {selected_key}: {option.name}"
        if option is not None
        else f"Incumbent direction {selected_key or 'unknown'}"
    )
    thesis = (
        str(getattr(option, "thesis", "") or "").strip()
        if option is not None
        else str(getattr(incumbent_direction, "summary", "") or "").strip()
    )
    feedback_focus = _extract_direction_feedback_focus_terms(feedback_note)
    why_now_parts = [
        "Gate Controller requested stronger evidence and more concrete detail for the existing direction before code generation.",
    ]
    if feedback_focus:
        why_now_parts.append("Priority gaps: " + "; ".join(feedback_focus[:3]))
    search_terms = _dedupe_text_items(
        [
            label,
            thesis,
            getattr(option, "primary_metric", "") if option is not None else "",
            getattr(option, "fastest_test", "") if option is not None else "",
            getattr(option, "major_risk", "") if option is not None else "",
        ]
        + feedback_focus,
        limit=6,
    )
    summary_target = limit_text(re.sub(r"\s+", " ", user_problem or "").strip(), 160)
    return DirectionSeedPlan(
        summary=(
            "Incumbent direction refinement plan for "
            + (summary_target or "the current strategy problem")
            + ". Do not generate fresh directions; deepen evidence for the current direction."
        ),
        directions=[
            DirectionSeedIdea(
                label=limit_text(label, 100),
                thesis=limit_text(
                    thesis or "Refine the incumbent direction with stronger evidence.", 220
                ),
                why_now=limit_text(" ".join(why_now_parts), 220),
                search_terms=search_terms,
            )
        ],
    )


def _render_incumbent_direction_block(
    incumbent_direction: Optional["DirectionDecision"],
    *,
    feedback_note: Optional[str],
) -> str:
    if incumbent_direction is None:
        return ""
    selected_key = str(getattr(incumbent_direction, "selected_direction", "") or "").strip().upper()
    if not selected_key or selected_key == "NONE":
        return ""
    option = _selected_direction_option(incumbent_direction)
    lines = [
        "=== INCUMBENT DIRECTION REFINEMENT MODE ===",
        "This is not a fresh ideation round.",
        "Supplement evidence and implementation detail for the incumbent direction only.",
        "Keep the incumbent selected_direction unless grounded evidence shows the incumbent direction is fundamentally invalid.",
        f"Incumbent selected direction: {selected_key}",
        f"Incumbent summary: {limit_text(str(getattr(incumbent_direction, 'summary', '') or ''), 320)}",
    ]
    if option is not None:
        lines.extend(
            [
                f"Incumbent option name: {limit_text(option.name, 120)}",
                f"Incumbent thesis: {limit_text(option.thesis, 220)}",
                f"Incumbent primary metric: {limit_text(option.primary_metric, 160)}",
                f"Incumbent fastest test: {limit_text(option.fastest_test, 160)}",
                f"Incumbent major risk: {limit_text(option.major_risk, 180)}",
            ]
        )
    backup_candidates = list(getattr(incumbent_direction, "backup_candidates", []) or [])
    if backup_candidates:
        lines.append("Incumbent backup candidates: " + ", ".join(backup_candidates[:4]))
    go_conditions = list(getattr(incumbent_direction, "go_conditions", []) or [])
    if go_conditions:
        lines.append("Incumbent go conditions:")
        lines.extend(f"- {limit_text(str(item), 220)}" for item in go_conditions[:5])
    kill_criteria = list(getattr(incumbent_direction, "kill_criteria", []) or [])
    if kill_criteria:
        lines.append("Incumbent kill criteria:")
        lines.extend(f"- {limit_text(str(item), 220)}" for item in kill_criteria[:5])
    verify_plan = list(getattr(incumbent_direction, "verify_plan", []) or [])
    if verify_plan:
        lines.append("Incumbent verify plan:")
        lines.extend(f"- {limit_text(str(item), 220)}" for item in verify_plan[:5])
    feedback_focus = _extract_direction_feedback_focus_terms(feedback_note)
    if feedback_focus:
        lines.append("Gate feedback focus areas:")
        lines.extend(f"- {item}" for item in feedback_focus[:8])
    return "\n".join(lines)


def run_direction_debate(
    user_problem: str,
    *,
    mode: str,
    language_hint: str,
    llm: Any,
    feedback_note: Optional[str] = None,
    incumbent_direction: Optional["DirectionDecision"] = None,
    force_refresh: bool = False,
) -> Optional["DirectionDecision"]:
    global DIRECTION_JUDGE_MODEL_ID, LIBRARIAN_MODEL_ID
    combined_problem = user_problem
    if feedback_note:
        combined_problem = (
            user_problem + "\n\n=== GATE CONTROLLER DIRECTION FEEDBACK ===\n" + feedback_note
        )
    force_refresh = bool(force_refresh or feedback_note)
    refinement_mode = bool(
        feedback_note
        and incumbent_direction is not None
        and str(getattr(incumbent_direction, "selected_direction", "") or "").strip().lower()
        not in ("", "none")
    )
    if refinement_mode:
        direction_seed_plan = _build_incumbent_direction_seed_plan(
            incumbent_direction,
            user_problem=user_problem,
            feedback_note=feedback_note,
        )
    else:
        direction_seed_plan = _build_direction_seed_plan(
            combined_problem,
            mode=mode,
            language_hint=language_hint,
            llm=llm,
        )
    seeded_problem = combined_problem
    context_blocks: List[str] = []
    if refinement_mode:
        incumbent_block = _render_incumbent_direction_block(
            incumbent_direction,
            feedback_note=feedback_note,
        )
        if incumbent_block:
            context_blocks.append(incumbent_block)
    seed_block = _render_direction_seed_block(direction_seed_plan)
    if seed_block:
        context_blocks.append(seed_block)
    if context_blocks:
        seeded_problem = combined_problem + "\n\n" + "\n\n".join(context_blocks)
    resolved_direction_judge_model_id = _resolve_direction_judge_model_id()
    DIRECTION_JUDGE_MODEL_ID = resolved_direction_judge_model_id
    resolved_research_model_id = ""
    if LIBRARIAN_ENABLED:
        resolved_research_model_id = _resolve_librarian_model_id()
        LIBRARIAN_MODEL_ID = resolved_research_model_id
    research_context = run_librarian_research(
        seeded_problem,
        mode=mode,
        language_hint=language_hint,
        direction_seed_plan=direction_seed_plan,
    )
    cache_payload = _build_direction_debate_cache_payload(
        user_problem=seeded_problem,
        mode=mode,
        language_hint=language_hint,
        llm_model_id=_llm_model_id(llm),
        direction_judge_model_id=resolved_direction_judge_model_id,
        research_model_id=resolved_research_model_id,
        strict_json=bool(STRICT_JSON_ENABLED),
        research_context=research_context,
    )
    cached = None
    if not force_refresh:
        cached = _cache_get_pydantic("direction_debate", cache_payload, DirectionDecision)
    if cached is not None:
        normalized_cached = _normalize_direction_decision(cached)
        if normalized_cached is not None:
            cached_artifacts = _load_direction_debate_artifacts(cache_payload)
            normalized_cached = _apply_deterministic_direction_rerank(
                normalized_cached,
                research_context=research_context,
                comparator_report=(
                    cached_artifacts.comparator_report if cached_artifacts else None
                ),
                audit_report=(cached_artifacts.audit_report if cached_artifacts else None),
            )
            return _cap_direction_decision_confidence(
                normalized_cached,
                research_context=research_context,
                comparator_report=(
                    cached_artifacts.comparator_report if cached_artifacts else None
                ),
                audit_report=(cached_artifacts.audit_report if cached_artifacts else None),
            )

    direction_judge_llm = _get_direction_judge_llm()
    current_research_context = research_context
    last_gap_info: Optional[Dict[str, Any]] = None
    for refinement_iteration in range(DIRECTION_REFINEMENT_MAX_ITERATIONS + 1):
        decision, comparator_report, audit_report, gap_info = _run_single_direction_debate(
            seeded_problem,
            mode=mode,
            language_hint=language_hint,
            llm=llm,
            research_context=current_research_context,
            direction_judge_llm=direction_judge_llm,
            cache_payload=cache_payload,
        )
        if decision is not None:
            return decision
        if gap_info is not None:
            last_gap_info = gap_info
        if gap_info is None:
            break
        if refinement_iteration >= DIRECTION_REFINEMENT_MAX_ITERATIONS:
            break
        print(
            f"[Info] Direction debate returned none due to evidence gaps. "
            f"Running research refinement (iteration {refinement_iteration + 1}/{DIRECTION_REFINEMENT_MAX_ITERATIONS})..."
        )
        refined_context = _run_direction_research_refinement(
            seeded_problem,
            mode=mode,
            language_hint=language_hint,
            gap_info=gap_info,
            existing_context=current_research_context,
            direction_seed_plan=direction_seed_plan,
        )
        if refined_context is None:
            break
        current_research_context = refined_context
        cache_payload["research_context_sha256"] = _text_sha256(
            _model_to_stable_json(current_research_context)
        )
    # Final summary before giving up — preserves what we know about why the
    # debate failed so the caller (and a human reading the run output) can act
    # on it.  Without this line the failure is invisible when the JSON-parse
    # path was never hit and only the force-none gate fired.
    # v1.1.8 extended (Phase 7, P5): degrade-not-die observability emit.
    # When force-none has fired the full DIRECTION_REFINEMENT_MAX_ITERATIONS+1
    # times AND the operator has opted into the toggle, emit a
    # ``direction_debate_degraded_proceed`` ledger event so v1.2.0
    # retrieval can identify which runs would have benefited.
    # v1.1.8 is OBSERVATION-ONLY for the degrade path — the actual
    # behavioural change (returning a low-confidence direction instead
    # of None) is deferred to v1.1.9 because it requires a non-trivial
    # signature change to ``_run_single_direction_debate``.  The env
    # toggle ``CRUCIBLE_DEBATE_TOLERATE_UNVERIFIABLE_EVIDENCE`` therefore
    # currently controls only whether the event is emitted (and is
    # primed for the future behavioural change).
    if last_gap_info is not None:
        try:
            _tolerate = _env_bool(
                "CRUCIBLE_DEBATE_TOLERATE_UNVERIFIABLE_EVIDENCE", False,
            )
            _n_threshold = _env_int(
                "CRUCIBLE_DEBATE_DEGRADE_AFTER_N_ITERATIONS", 3,
            ) or 3
            _total_iter = DIRECTION_REFINEMENT_MAX_ITERATIONS + 1
            if _tolerate and _total_iter >= _n_threshold:
                _candidate_dir = ""
                _weak = list(last_gap_info.get("weak_directions") or [])
                if _weak:
                    _candidate_dir = str(_weak[0])
                _get_insights_recorder().record_direction_debate_degraded_proceed(
                    run_id=_resolve_run_id_for_ledger_emit(),
                    project_name="stage0_pending",
                    mode=str(mode or "mode_unknown"),
                    selected_direction=_candidate_dir or "unknown",
                    original_decision="force_none",
                    consecutive_force_none_count=int(_total_iter),
                    final_score=0,
                    gate_reason=(
                        "consecutive force-none iterations exhausted "
                        "with structural failure (supported_fields empty)"
                    ),
                    attempt=int(_total_iter),
                    user_problem=user_problem,
                )
        except Exception:
            # Never let degrade telemetry break the warning print below.
            pass

    if last_gap_info is not None:
        # v1.1.8 extended (Phase 7, P4): warning UX cleanup.
        # Only print quantity counters when the firing gate actually
        # populated them.  Previously every warning printed
        # ``grounded_claims_needed=0 citations_needed=0`` even when the
        # gate that fired was ``not scores`` (which doesn't set those
        # fields), misleading the operator into thinking "I have enough
        # citations".  See v1.1.8 diagnostic for the full root cause.
        _gci_needed = int(last_gap_info.get("grounded_claims_needed") or 0)
        _cit_needed = int(last_gap_info.get("citations_needed") or 0)
        _weak_dirs = list(last_gap_info.get("weak_directions") or [])[:3]
        _missing = list(
            last_gap_info.get("missing_evidence_areas") or []
        )[:3]
        _critical = list(last_gap_info.get("critical_unknowns") or [])[:3]
        _msg_parts: List[str] = [
            "[Warn] Direction debate exhausted "
            f"{DIRECTION_REFINEMENT_MAX_ITERATIONS + 1} iteration(s) without "
            "a defendable decision. Likely cause: insufficient grounded "
            "evidence (see preceding force-none diagnostic line).",
            f"weak_directions={_weak_dirs}",
            f"missing_evidence={_missing}",
        ]
        # Quantity counters only printed when non-zero — match the
        # gate-branch shape (``near_zero_evidence`` and
        # ``weakly_supported`` set these; ``not scores`` and
        # ``high_critical_unknowns`` do NOT).
        if _gci_needed > 0:
            _msg_parts.append(f"grounded_claims_needed={_gci_needed}")
        if _cit_needed > 0:
            _msg_parts.append(f"citations_needed={_cit_needed}")
        # When neither counter was set, surface the structural hint
        # instead — tells the operator "every direction's supported_
        # fields ended up empty, so increase per-direction claim
        # anchoring rather than total citation count".
        if _gci_needed == 0 and _cit_needed == 0:
            _msg_parts.append(
                "structural_failure=supported_fields_empty_across_directions"
            )
        if _critical:
            _msg_parts.append(f"critical_unknowns={_critical}")
        print(" ".join(_msg_parts))
    else:
        print(
            "[Warn] Direction debate produced no parseable decision after all "
            "JSON-retry attempts. Inspect the latest dump under "
            "saved_projects/direction_debug/ "
            "(or %TEMP%/CrucibleCrew_direction_debug/ on Windows fallback)."
        )
    return None


def _resolve_llm_provider(provider: Optional[str] = None) -> str:
    if provider is not None and str(provider or "").strip():
        return _normalize_llm_provider(provider)
    active_provider = globals().get("ACTIVE_LLM_PROVIDER")
    if isinstance(active_provider, str) and active_provider.strip():
        return _normalize_llm_provider(active_provider)
    env_provider = str(os.environ.get("LLM_PROVIDER") or "").strip()
    if env_provider:
        return _normalize_llm_provider(env_provider)
    return _normalize_llm_provider(os.environ.get("LLM_PROVIDER"))


def _resolve_provider_model_setting_keys(role: str, provider: Optional[str] = None) -> Tuple[str, ...]:
    resolved_provider = _resolve_llm_provider(provider)
    if resolved_provider == LLM_PROVIDER_ALIBABA_CODING_PLAN:
        mapping = {
            "primary": ("ALIBABA_CODING_PLAN_PRIMARY_MODEL",),
            "direction_judge": ("ALIBABA_CODING_PLAN_DIRECTION_JUDGE_MODEL",),
            "librarian": ("ALIBABA_CODING_PLAN_LIBRARIAN_MODEL",),
        }
        return mapping[role]
    if resolved_provider == LLM_PROVIDER_OLLAMA:
        mapping = {
            "primary": ("OLLAMA_PRIMARY_MODEL",),
            "direction_judge": ("OLLAMA_DIRECTION_JUDGE_MODEL", "OLLAMA_PRIMARY_MODEL"),
            "librarian": ("OLLAMA_LIBRARIAN_MODEL", "OLLAMA_PRIMARY_MODEL"),
        }
        return mapping[role]
    mapping = {
        "primary": PRIMARY_MODEL_ENV_KEYS,
        "direction_judge": DIRECTION_JUDGE_MODEL_ENV_KEYS,
        "librarian": LIBRARIAN_MODEL_ENV_KEYS,
    }
    return mapping[role]


def _resolve_provider_default_model_id(role: str, provider: Optional[str] = None) -> str:
    resolved_provider = _resolve_llm_provider(provider)
    if resolved_provider == LLM_PROVIDER_ALIBABA_CODING_PLAN:
        mapping = {
            "primary": DEFAULT_ALIBABA_CODING_PLAN_PRIMARY_MODEL_ID,
            "direction_judge": DEFAULT_ALIBABA_CODING_PLAN_DIRECTION_JUDGE_MODEL_ID,
            "librarian": DEFAULT_ALIBABA_CODING_PLAN_LIBRARIAN_MODEL_ID,
        }
        return mapping[role]
    if resolved_provider == LLM_PROVIDER_OLLAMA:
        mapping = {
            "primary": DEFAULT_OLLAMA_PRIMARY_MODEL_ID,
            "direction_judge": DEFAULT_OLLAMA_PRIMARY_MODEL_ID,
            "librarian": DEFAULT_OLLAMA_PRIMARY_MODEL_ID,
        }
        return mapping[role]
    mapping = {
        "primary": DEFAULT_PRIMARY_MODEL_ID,
        "direction_judge": DEFAULT_DIRECTION_JUDGE_MODEL_ID,
        "librarian": DEFAULT_LIBRARIAN_MODEL_ID,
    }
    return mapping[role]


def _resolve_llm_base_url(provider: Optional[str] = None) -> str:
    """Return the base URL for the active LLM provider.

    **Single source of truth**: the .env file.  Each provider reads its URL
    from the corresponding environment variable.  Module-level constants are
    used ONLY as last-resort fallbacks when the env var is completely unset
    (e.g. during unit tests).
    """
    resolved_provider = _resolve_llm_provider(provider)
    if resolved_provider == LLM_PROVIDER_ALIBABA_CODING_PLAN:
        return _get_alibaba_enforced_base_url()
    if resolved_provider == LLM_PROVIDER_OLLAMA:
        return _get_ollama_enforced_base_url()
    return _get_openrouter_enforced_base_url()


def load_api_key(provider: Optional[str] = None) -> str:
    """Reads the active provider API key from environment variables or the project .env file."""
    resolved_provider = _resolve_llm_provider(provider)
    if resolved_provider == LLM_PROVIDER_ALIBABA_CODING_PLAN:
        env_keys: Tuple[str, ...] = ("ALIBABA_CODING_PLAN_API_KEY",)
    elif resolved_provider == LLM_PROVIDER_OLLAMA:
        # Ollama does not require a real API key; return a sentinel immediately.
        return "ollama"
    else:
        env_keys = ("OPENROUTER_API_KEY", "OPENAI_API_KEY")
    env_key, resolved_from = _resolve_env_setting(
        env_keys,
        ignore_placeholders=True,
    )
    stale_openai_compat_key = str(globals().get("ACTIVE_OPENAI_COMPAT_API_KEY") or "").strip()
    stale_openai_compat_provider = _normalize_llm_provider(
        globals().get("ACTIVE_OPENAI_COMPAT_PROVIDER")
    )
    if (
        resolved_provider == LLM_PROVIDER_OPENROUTER
        and resolved_from == "OPENAI_API_KEY"
        and env_key
        and stale_openai_compat_key
        and env_key == stale_openai_compat_key
        and stale_openai_compat_provider != LLM_PROVIDER_OPENROUTER
    ):
        env_key = None
    if env_key:
        if (
            resolved_provider == LLM_PROVIDER_ALIBABA_CODING_PLAN
            and not str(env_key).strip().startswith("sk-sp-")
        ):
            print(
                "[Warn] ALIBABA_CODING_PLAN_API_KEY does not start with 'sk-sp-'. "
                "Verify that you are using a valid Alibaba Coding Plan key."
            )
        return env_key
    env_hint = LOADED_ENV_FILE or os.path.join(PROJECT_ROOT, DEFAULT_ENV_FILE_NAME)
    if resolved_provider == LLM_PROVIDER_ALIBABA_CODING_PLAN:
        message = (
            f"ALIBABA_CODING_PLAN_API_KEY is not configured. "
            f"Set it in your shell or add it to {env_hint}."
        )
    else:
        message = (
            f"OPENROUTER_API_KEY is not configured. "
            f"Set OPENROUTER_API_KEY (or OPENAI_API_KEY for compatibility) in your shell "
            f"or add it to {env_hint}."
        )
    print(f"Error: {message}")
    # Raise rather than sys.exit(1) so the pipeline can flush checkpoints /
    # telemetry and so callers that retry with a different provider are not
    # terminated mid-run.  The top-level CLI converts uncaught exceptions
    # into exit code 1 anyway.
    raise RuntimeError(message)


def _resolve_primary_model_id() -> str:
    model_id, _ = _resolve_env_setting(
        _resolve_provider_model_setting_keys("primary"),
        default=_resolve_provider_default_model_id("primary"),
    )
    default_model_id = _resolve_provider_default_model_id("primary")
    return str(model_id or "").strip() or default_model_id


def init_llm() -> Any:
    resolved_provider = _resolve_llm_provider()
    model_id, resolved_from = _resolve_env_setting(
        _resolve_provider_model_setting_keys("primary", resolved_provider),
        default=_resolve_provider_default_model_id("primary", resolved_provider),
    )
    model_id = str(model_id or "").strip() or _resolve_provider_default_model_id(
        "primary", resolved_provider
    )
    if resolved_from is None:
        print(
            f"[Info] Primary {_llm_provider_label(resolved_provider)} model not set; "
            f"using default: {model_id}"
        )
    global MODEL_ID, ACTIVE_LLM_PROVIDER
    MODEL_ID = model_id
    ACTIVE_LLM_PROVIDER = resolved_provider
    return _create_openrouter_llm(
        model_id=model_id,
        temperature=0.7,
        provider=resolved_provider,
    )


def _resolve_openrouter_llm_timeout_seconds() -> int:
    raw = _env_int("OPENROUTER_LLM_TIMEOUT_SECONDS", 180)
    return max(30, raw if raw is not None else 180)


OPENROUTER_LLM_TIMEOUT_SECONDS = _resolve_openrouter_llm_timeout_seconds()


def _resolve_alibaba_coding_plan_llm_timeout_seconds() -> int:
    raw = _env_int("ALIBABA_CODING_PLAN_LLM_TIMEOUT_SECONDS", 900)
    return max(60, raw if raw is not None else 900)


ALIBABA_CODING_PLAN_LLM_TIMEOUT_SECONDS = _resolve_alibaba_coding_plan_llm_timeout_seconds()


def _resolve_alibaba_coding_plan_initial_response_timeout_seconds() -> int:
    raw = _env_int("ALIBABA_CODING_PLAN_INITIAL_RESPONSE_TIMEOUT_SECONDS", 120)
    return max(30, raw if raw is not None else 120)


ALIBABA_CODING_PLAN_INITIAL_RESPONSE_TIMEOUT_SECONDS = (
    _resolve_alibaba_coding_plan_initial_response_timeout_seconds()
)



def _resolve_llm_timeout_seconds(provider: Optional[str] = None) -> int:
    resolved_provider = _resolve_llm_provider(provider)
    if resolved_provider == LLM_PROVIDER_ALIBABA_CODING_PLAN:
        return _resolve_alibaba_coding_plan_llm_timeout_seconds()
    return _resolve_openrouter_llm_timeout_seconds()


def _build_llm_timeout_value(
    provider: Optional[str] = None,
    *,
    timeout_seconds: Optional[float] = None,
) -> Any:
    resolved_provider = _resolve_llm_provider(provider)
    if timeout_seconds is not None and _llm_timeout_signature_value(timeout_seconds) is not None:
        if not isinstance(timeout_seconds, (int, float)):
            return timeout_seconds
    resolved_total = float(
        timeout_seconds
        if timeout_seconds is not None
        else _resolve_llm_timeout_seconds(resolved_provider)
    )
    if resolved_provider == LLM_PROVIDER_ALIBABA_CODING_PLAN:
        stall_timeout = float(_resolve_alibaba_coding_plan_initial_response_timeout_seconds())
        phase_timeout = min(resolved_total, stall_timeout)
        return httpx.Timeout(
            connect=phase_timeout,
            read=resolved_total,
            write=phase_timeout,
            pool=phase_timeout,
        )
    return resolved_total


def _llm_timeout_signature_value(timeout: Any) -> Any:
    if timeout is None:
        return None
    for attr in ("connect", "read", "write", "pool"):
        if hasattr(timeout, attr):
            return tuple(
                (
                    None
                    if getattr(timeout, key, None) is None
                    else float(getattr(timeout, key))
                )
                for key in ("connect", "read", "write", "pool")
            )
    try:
        return float(timeout)
    except Exception:
        return None


def _create_openrouter_llm(
    model_id: str,
    *,
    temperature: float = 0.7,
    timeout_seconds: Optional[float] = None,
    enable_cost_tracking: bool = True,
    provider: Optional[str] = None,
) -> Any:
    global ACTIVE_LLM_PROVIDER, ACTIVE_OPENAI_COMPAT_PROVIDER, ACTIVE_OPENAI_COMPAT_API_KEY
    resolved_provider = _resolve_llm_provider(provider)
    api_key = load_api_key(resolved_provider)
    base_url = _resolve_llm_base_url(resolved_provider)
    if resolved_provider == LLM_PROVIDER_OPENROUTER:
        os.environ["OPENROUTER_API_KEY"] = api_key
    elif resolved_provider == LLM_PROVIDER_ALIBABA_CODING_PLAN:
        os.environ["ALIBABA_CODING_PLAN_API_KEY"] = api_key
        os.environ["ALIBABA_CODING_PLAN_BASE_URL"] = base_url
    elif resolved_provider == LLM_PROVIDER_OLLAMA:
        # Ollama uses a dummy key; do not clobber real API keys in the env.
        api_key = "ollama"
    os.environ["OPENAI_API_KEY"] = api_key
    os.environ["OPENAI_API_BASE"] = base_url
    os.environ["OPENAI_BASE_URL"] = base_url
    ACTIVE_LLM_PROVIDER = resolved_provider
    ACTIVE_OPENAI_COMPAT_PROVIDER = resolved_provider
    ACTIVE_OPENAI_COMPAT_API_KEY = str(api_key or "")
    resolved_timeout = _build_llm_timeout_value(
        resolved_provider,
        timeout_seconds=timeout_seconds,
    )

    # crewai.LLM (backed by LiteLLM / OpenAICompletion pydantic model) requires
    # timeout to be a plain float.  _build_llm_timeout_value returns an
    # httpx.Timeout for Alibaba; extract its read value so we
    # always pass a scalar here.
    _timeout_scalar: Optional[float] = (
        float(resolved_timeout.read)
        if hasattr(resolved_timeout, "read")
        else (float(resolved_timeout) if resolved_timeout is not None else None)
    )

    llm_kwargs: Dict[str, Any] = {
        "model": model_id,
        "provider": "openai",
        "temperature": temperature,
        "api_key": api_key,
        "base_url": base_url,
    }
    if _timeout_scalar is not None:
        llm_kwargs["timeout"] = _timeout_scalar

    if resolved_provider == LLM_PROVIDER_ALIBABA_CODING_PLAN:
        llm_kwargs["extra_headers"] = {"x-source": "opencode"}
        # Patch openai.OpenAI constructor so any clients created internally
        # by CrewAI (e.g. memory module) also get the x-source: opencode header.
        _install_alibaba_openai_client_header_patch()

    if resolved_provider == LLM_PROVIDER_OPENROUTER:
        # v1.1.1 — Opt into OpenRouter's usage-accounting so the actual
        # billed USD amount lands in ``response.usage.cost`` instead of
        # being silently elided.  Without this, cost tracking falls back
        # to the local pricing table and emits zero whenever the model
        # ID is missing or misspelled (e.g. ``deepseek-v4-flash`` before
        # the v1.1.1 table update).  See ``inject_openrouter_usage_extra_body``
        # docstring for the three-layer plumbing rationale.
        inject_openrouter_usage_extra_body(llm_kwargs)

    if enable_cost_tracking:
        callback_handler = get_openrouter_callback_handler()
        if callback_handler is not None:
            llm_kwargs["callbacks"] = [callback_handler]
        http_interceptor = get_openrouter_http_interceptor()
        if http_interceptor is not None:
            llm_kwargs["interceptor"] = http_interceptor

    llm = LLM(**llm_kwargs)
    try:
        setattr(llm, "_quant_llm_provider", resolved_provider)
        setattr(llm, "_quant_llm_timeout_signature", _llm_timeout_signature_value(resolved_timeout))
    except Exception:
        pass

    if resolved_provider == LLM_PROVIDER_ALIBABA_CODING_PLAN:
        # Install the Alibaba LiteLLM guard (idempotent).
        _install_litellm_alibaba_guard()

    return llm


def _resolve_direction_judge_model_id() -> str:
    model_id, _ = _resolve_env_setting(
        _resolve_provider_model_setting_keys("direction_judge"),
        default=_resolve_provider_default_model_id("direction_judge"),
    )
    default_model_id = _resolve_provider_default_model_id("direction_judge")
    return str(model_id or "").strip() or default_model_id


def _resolve_librarian_model_id() -> str:
    model_id, _ = _resolve_env_setting(
        _resolve_provider_model_setting_keys("librarian"),
        default=_resolve_provider_default_model_id("librarian"),
    )
    default_model_id = _resolve_provider_default_model_id("librarian")
    return str(model_id or "").strip() or default_model_id


DIRECTION_JUDGE_MODEL_ID = _resolve_direction_judge_model_id()
_DIRECTION_JUDGE_LLM: Optional[Any] = None
_DIRECTION_JUDGE_LLM_LOCK = threading.Lock()
LIBRARIAN_MODEL_ID = _resolve_librarian_model_id()
LIBRARIAN_HTTP_USER_AGENT = "CrucibleCrew/14 librarian"
LIBRARIAN_WEBSEARCH_USER_AGENT = (
    "Mozilla/5.0 (compatible; CrucibleCrew/14; +https://duckduckgo.com/)"
)
_LIBRARIAN_LLM: Optional[Any] = None
_LIBRARIAN_LLM_LOCK = threading.Lock()
_LAST_LIBRARIAN_DEBUG: Dict[str, Any] = {}
ACTIVE_LLM_PROVIDER = _resolve_llm_provider()
ACTIVE_OPENAI_COMPAT_PROVIDER: Optional[str] = None
ACTIVE_OPENAI_COMPAT_API_KEY: str = ""


def _update_last_librarian_debug(**fields: Any) -> None:
    global _LAST_LIBRARIAN_DEBUG
    if fields.get("status") == "started":
        _LAST_LIBRARIAN_DEBUG = {}
    merged = dict(_LAST_LIBRARIAN_DEBUG or {})
    merged.update({k: v for k, v in fields.items() if v is not None})
    # Truncate oversized string values to prevent memory bloat in batch runs
    _MAX_VALUE_CHARS = 10_000
    _MAX_KEYS = 100
    for _k, _v in list(merged.items()):
        if isinstance(_v, str) and len(_v) > _MAX_VALUE_CHARS:
            merged[_k] = _v[:_MAX_VALUE_CHARS] + "...[truncated]"
    # If dict grows beyond key limit (should not happen normally), prune oldest
    if len(merged) > _MAX_KEYS:
        # Keep the most recently added keys by removing the first (oldest) entries
        keys_to_remove = list(merged.keys())[:len(merged) - _MAX_KEYS]
        for _k in keys_to_remove:
            del merged[_k]
    merged["captured_at"] = datetime.now().isoformat(timespec="seconds")
    _LAST_LIBRARIAN_DEBUG = merged


def clear_last_librarian_debug() -> None:
    global _LAST_LIBRARIAN_DEBUG
    _LAST_LIBRARIAN_DEBUG = {}


def get_last_librarian_debug() -> Dict[str, Any]:
    return dict(_LAST_LIBRARIAN_DEBUG or {})


def reset_research_llm_cache() -> None:
    global _DIRECTION_JUDGE_LLM, _LIBRARIAN_LLM
    _DIRECTION_JUDGE_LLM = None
    _LIBRARIAN_LLM = None


def _llm_timeout_seconds(llm: Any) -> Optional[float]:
    try:
        timeout = getattr(llm, "timeout", None)
    except Exception:
        timeout = None
    if timeout is None:
        return None
    try:
        read_timeout = getattr(timeout, "read", None)
    except Exception:
        read_timeout = None
    if read_timeout is not None:
        try:
            return float(read_timeout)
        except Exception:
            return None
    try:
        return float(timeout)
    except Exception:
        return None


def _llm_timeout_signature(llm: Any) -> Any:
    try:
        cached = getattr(llm, "_quant_llm_timeout_signature", None)
    except Exception:
        cached = None
    if cached is not None:
        return cached
    try:
        timeout = getattr(llm, "timeout", None)
    except Exception:
        timeout = None
    return _llm_timeout_signature_value(timeout)


def _llm_provider_name(llm: Any) -> str:
    try:
        provider = getattr(llm, "_quant_llm_provider", None)
    except Exception:
        provider = None
    if isinstance(provider, str) and provider.strip():
        return _resolve_llm_provider(provider)
    try:
        base_url = getattr(llm, "base_url", None)
    except Exception:
        base_url = None
    if isinstance(base_url, str) and base_url.strip():
        resolved = _host_to_usage_provider(urllib.parse.urlparse(base_url).hostname or "")
        if resolved:
            return resolved
    return _resolve_llm_provider()


def _get_direction_judge_llm() -> Any:
    global _DIRECTION_JUDGE_LLM, DIRECTION_JUDGE_MODEL_ID
    resolved_provider = _resolve_llm_provider()
    resolved_model_id = _resolve_direction_judge_model_id()
    resolved_timeout = _build_llm_timeout_value(resolved_provider)
    resolved_timeout_signature = _llm_timeout_signature_value(resolved_timeout)
    # Fast path (no lock): if the LLM already matches the current config,
    # return it immediately.  CPython's GIL makes attribute reads atomic.
    if (
        _DIRECTION_JUDGE_LLM is not None
        and _llm_model_id(_DIRECTION_JUDGE_LLM) == resolved_model_id
        and _llm_timeout_signature(_DIRECTION_JUDGE_LLM) == resolved_timeout_signature
        and _llm_provider_name(_DIRECTION_JUDGE_LLM) == resolved_provider
    ):
        return _DIRECTION_JUDGE_LLM
    with _DIRECTION_JUDGE_LLM_LOCK:
        # Re-check inside the lock: another thread may have created the LLM
        # between the fast-path check above and acquiring the lock here.
        if (
            _DIRECTION_JUDGE_LLM is not None
            and _llm_model_id(_DIRECTION_JUDGE_LLM) == resolved_model_id
            and _llm_timeout_signature(_DIRECTION_JUDGE_LLM) == resolved_timeout_signature
            and _llm_provider_name(_DIRECTION_JUDGE_LLM) == resolved_provider
        ):
            return _DIRECTION_JUDGE_LLM
        DIRECTION_JUDGE_MODEL_ID = resolved_model_id
        _DIRECTION_JUDGE_LLM = _create_openrouter_llm(
            model_id=resolved_model_id,
            temperature=0.7,
            timeout_seconds=resolved_timeout,
            provider=resolved_provider,
        )
        return _DIRECTION_JUDGE_LLM


def _get_librarian_llm() -> Any:
    global _LIBRARIAN_LLM, LIBRARIAN_MODEL_ID
    resolved_provider = _resolve_llm_provider()
    resolved_model_id = _resolve_librarian_model_id()
    resolved_timeout = _build_llm_timeout_value(resolved_provider)
    resolved_timeout_signature = _llm_timeout_signature_value(resolved_timeout)
    # Fast path (no lock): if the LLM already matches the current config,
    # return it immediately.  CPython's GIL makes attribute reads atomic.
    if (
        _LIBRARIAN_LLM is not None
        and _llm_model_id(_LIBRARIAN_LLM) == resolved_model_id
        and _llm_timeout_signature(_LIBRARIAN_LLM) == resolved_timeout_signature
        and _llm_provider_name(_LIBRARIAN_LLM) == resolved_provider
    ):
        return _LIBRARIAN_LLM
    with _LIBRARIAN_LLM_LOCK:
        # Re-check inside the lock: another thread may have created the LLM
        # between the fast-path check above and acquiring the lock here.
        if (
            _LIBRARIAN_LLM is not None
            and _llm_model_id(_LIBRARIAN_LLM) == resolved_model_id
            and _llm_timeout_signature(_LIBRARIAN_LLM) == resolved_timeout_signature
            and _llm_provider_name(_LIBRARIAN_LLM) == resolved_provider
        ):
            return _LIBRARIAN_LLM
        LIBRARIAN_MODEL_ID = resolved_model_id
        _LIBRARIAN_LLM = _create_openrouter_llm(
            model_id=resolved_model_id,
            temperature=0.3,
            timeout_seconds=resolved_timeout,
            provider=resolved_provider,
        )
        return _LIBRARIAN_LLM


MODEL_ID: Optional[str] = None
DEPENDENCY_VERSION_CANDIDATES = {
    "crewai": ["crewai"],
    "langchain_openai": ["langchain-openai", "langchain_openai"],
    "pydantic": ["pydantic"],
    "fastapi": ["fastapi"],
    "uvicorn": ["uvicorn"],
}
STRICT_JSON_ENABLED = _env_bool("STRICT_JSON", False)
COST_TRACE_ENABLED = _env_bool("COST_TRACE", False)


def _resolve_local_cache_runtime_defaults() -> Dict[str, Any]:
    return {
        "enabled": _env_bool("LOCAL_CACHE", False),
        "ttl_hours": _env_int("LOCAL_CACHE_TTL_HOURS", 168),
        "path": os.environ.get("LOCAL_CACHE_PATH"),
    }


_LOCAL_CACHE_RUNTIME_DEFAULTS = _resolve_local_cache_runtime_defaults()
LOCAL_CACHE_ENABLED = bool(_LOCAL_CACHE_RUNTIME_DEFAULTS["enabled"])
LOCAL_CACHE_TTL_HOURS = _LOCAL_CACHE_RUNTIME_DEFAULTS["ttl_hours"]
LOCAL_CACHE_PATH = _LOCAL_CACHE_RUNTIME_DEFAULTS["path"]


def _resolve_crewai_output_pydantic_enabled() -> bool:
    return _env_bool("CREWAI_OUTPUT_PYDANTIC", False)


def _resolve_librarian_runtime_defaults() -> Dict[str, Any]:
    providers = _normalize_librarian_provider_names(
        os.environ.get(
            "LIBRARIAN_SEARCH_PROVIDERS",
            ",".join(OPENCODE_LIBRARIAN_DEFAULT_PROVIDERS),
        )
    ) or list(OPENCODE_LIBRARIAN_DEFAULT_PROVIDERS)
    # Fix C: compute _iqd before the return dict so 0.0 is preserved correctly
    # (using `or 4.0` would coerce an explicit 0.0 to 4.0, preventing the user
    # from ever setting the delay below 0.5 via the env var).
    _iqd = _env_float("LIBRARIAN_INTER_QUERY_DELAY_SECONDS", 4.0)
    return {
        "enabled": _env_bool("LIBRARIAN_ENABLED", True),
        "search_providers": list(providers),
        "max_results_per_query": max(1, (_env_int("LIBRARIAN_MAX_RESULTS_PER_QUERY", 3) or 3)),
        "max_citations": max(1, (_env_int("LIBRARIAN_MAX_CITATIONS", 12) or 12)),
        "max_queries_per_lane": max(2, (_env_int("LIBRARIAN_MAX_QUERIES_PER_LANE", 4) or 4)),
        "cache_window_hours": _env_int("LIBRARIAN_CACHE_WINDOW_HOURS", 24),
        "http_timeout_seconds": max(3.0, (_env_float("LIBRARIAN_HTTP_TIMEOUT_SECONDS", 15.0) or 15.0)),
        "http_max_bytes": max(4096, (_env_int("LIBRARIAN_HTTP_MAX_BYTES", 1048576) or 1048576)),
        "verify_citations": _env_bool("LIBRARIAN_VERIFY_CITATIONS", True),
        "max_verified_citations": max(0, (_env_int("LIBRARIAN_MAX_VERIFIED_CITATIONS", 6) or 0)),
        # Minimum seconds to wait between consecutive web-search HTTP requests.
        # DuckDuckGo and similar services return 429 / block when requests arrive
        # faster than ~1-2 s.  Default 4 s is conservative; lower to 2 s if the
        # target service handles bursts well.
        "inter_query_delay_seconds": max(0.5, _iqd if _iqd is not None else 4.0),
    }


CREWAI_OUTPUT_PYDANTIC = _resolve_crewai_output_pydantic_enabled()
_LIBRARIAN_RUNTIME_DEFAULTS = _resolve_librarian_runtime_defaults()
LIBRARIAN_ENABLED = bool(_LIBRARIAN_RUNTIME_DEFAULTS["enabled"])
LIBRARIAN_SEARCH_PROVIDERS = list(_LIBRARIAN_RUNTIME_DEFAULTS["search_providers"])
LIBRARIAN_MAX_RESULTS_PER_QUERY = int(_LIBRARIAN_RUNTIME_DEFAULTS["max_results_per_query"])
LIBRARIAN_MAX_CITATIONS = int(_LIBRARIAN_RUNTIME_DEFAULTS["max_citations"])
LIBRARIAN_MAX_QUERIES_PER_LANE = int(_LIBRARIAN_RUNTIME_DEFAULTS["max_queries_per_lane"])
LIBRARIAN_CACHE_WINDOW_HOURS = _LIBRARIAN_RUNTIME_DEFAULTS["cache_window_hours"]
LIBRARIAN_HTTP_TIMEOUT_SECONDS = float(_LIBRARIAN_RUNTIME_DEFAULTS["http_timeout_seconds"])
LIBRARIAN_HTTP_MAX_BYTES = int(_LIBRARIAN_RUNTIME_DEFAULTS["http_max_bytes"])
LIBRARIAN_VERIFY_CITATIONS = bool(_LIBRARIAN_RUNTIME_DEFAULTS["verify_citations"])
LIBRARIAN_MAX_VERIFIED_CITATIONS = int(_LIBRARIAN_RUNTIME_DEFAULTS["max_verified_citations"])
LIBRARIAN_INTER_QUERY_DELAY_SECONDS = float(_LIBRARIAN_RUNTIME_DEFAULTS["inter_query_delay_seconds"])
LIBRARIAN_QUERY_PLAN_VERSION = "v2"

HIGH_VALUE_SOURCES: List[str] = [
    "github.com",
    "pypi.org",
    "stackoverflow.com",
    "docs.python.org",
    "binance.com",
    "readthedocs.io",
    "medium.com",
    "towardsdatascience.com",
]

HIGH_VALUE_EXTENSIONS: Set[str] = {
    ".py",
    ".md",
    ".rst",
    ".txt",
}


def _is_high_value_source(url: str) -> bool:
    if not url:
        return False
    lowered = url.lower()
    for source in HIGH_VALUE_SOURCES:
        if source in lowered:
            return True
    for ext in HIGH_VALUE_EXTENSIONS:
        if lowered.endswith(ext):
            return True
    return False


_STRICT_JSON_PYDANTIC_WARNED = False


def _sync_output_validation_mode() -> None:
    """
    Strict JSON mode relies on raw text repair; disable CrewAI output_pydantic in this mode.
    """
    global CREWAI_OUTPUT_PYDANTIC, _STRICT_JSON_PYDANTIC_WARNED
    if STRICT_JSON_ENABLED and CREWAI_OUTPUT_PYDANTIC:
        CREWAI_OUTPUT_PYDANTIC = False
        if not _STRICT_JSON_PYDANTIC_WARNED:
            print(
                "[Warn] STRICT_JSON and CREWAI_OUTPUT_PYDANTIC were both enabled; "
                "disabling CREWAI_OUTPUT_PYDANTIC to preserve raw-text recovery.",
                file=sys.stderr,
            )
            _STRICT_JSON_PYDANTIC_WARNED = True


_sync_output_validation_mode()


_LOCAL_CACHE_SCHEMA_VERSION = 3


class _LocalLLMCache:
    def __init__(self, path: str, ttl_hours: Optional[int]) -> None:
        self.path = path
        if ttl_hours is None:
            self.ttl_seconds = 0
        else:
            self.ttl_seconds = max(0, int(ttl_hours) * 3600)
        self._conn: Optional[sqlite3.Connection] = None
        self._connect_lock = threading.Lock()

    def _connect(self) -> sqlite3.Connection:
        # Fast path: connection already initialised (no lock needed for
        # a simple attribute read on CPython; the lock below handles the
        # double-create race for concurrent first callers).
        if self._conn is not None:
            return self._conn
        with self._connect_lock:
            if self._conn is not None:   # second guard inside lock
                return self._conn
            cache_dir = os.path.dirname(self.path)
            if cache_dir:
                os.makedirs(cache_dir, exist_ok=True)
            conn = sqlite3.connect(self.path, timeout=5)
            try:
                conn.execute("PRAGMA busy_timeout=5000")
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute("PRAGMA synchronous=NORMAL")
                conn.execute("PRAGMA temp_store=MEMORY")
            except Exception:
                pass
            conn.execute(
                "CREATE TABLE IF NOT EXISTS kv ("
                "  key TEXT PRIMARY KEY,"
                "  created_at INTEGER NOT NULL,"
                "  value TEXT NOT NULL"
                ")"
            )
            conn.commit()
            self._conn = conn
            return conn

    def get(self, key: str) -> Optional[str]:
        try:
            conn = self._connect()
            row = conn.execute("SELECT created_at, value FROM kv WHERE key=?", (key,)).fetchone()
            if not row:
                return None
            created_at, value = int(row[0]), row[1]
            if self.ttl_seconds and (int(time.time()) - created_at) > self.ttl_seconds:
                try:
                    conn.execute("DELETE FROM kv WHERE key=?", (key,))
                    conn.commit()
                except Exception:
                    pass
                return None
            return value
        except Exception:
            return None

    def set(self, key: str, value: str) -> None:
        try:
            conn = self._connect()
            conn.execute(
                "INSERT OR REPLACE INTO kv(key, created_at, value) VALUES(?, ?, ?)",
                (key, int(time.time()), value),
            )
            conn.commit()
        except Exception:
            return


_LOCAL_LLM_CACHE: Optional[_LocalLLMCache] = None
_LOCAL_LLM_CACHE_LOCK = threading.Lock()


def reset_local_llm_cache() -> None:
    global _LOCAL_LLM_CACHE
    if _LOCAL_LLM_CACHE is not None:
        conn = getattr(_LOCAL_LLM_CACHE, "_conn", None)
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
    _LOCAL_LLM_CACHE = None


def _get_local_llm_cache() -> Optional[_LocalLLMCache]:
    global _LOCAL_LLM_CACHE
    if not LOCAL_CACHE_ENABLED:
        return None
    if _LOCAL_LLM_CACHE is not None:
        return _LOCAL_LLM_CACHE
    with _LOCAL_LLM_CACHE_LOCK:
        if _LOCAL_LLM_CACHE is not None:   # second guard inside lock
            return _LOCAL_LLM_CACHE
        default_path = os.path.join(_REPO_ROOT, "saved_projects", ".cache", "llm_cache.sqlite3")
        path = (LOCAL_CACHE_PATH or default_path).strip() or default_path
        _LOCAL_LLM_CACHE = _LocalLLMCache(path=path, ttl_hours=LOCAL_CACHE_TTL_HOURS)
        return _LOCAL_LLM_CACHE


def _cache_key(stage: str, payload: Dict[str, Any]) -> str:
    base = {
        "schema": _LOCAL_CACHE_SCHEMA_VERSION,
        "stage": stage,
        "llm_provider": _resolve_llm_provider(),
        "payload": payload,
    }
    data = json.dumps(base, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(data.encode("utf-8")).hexdigest()


def _text_sha256(text: str) -> str:
    try:
        return hashlib.sha256((text or "").encode("utf-8")).hexdigest()
    except Exception:
        return ""


def _llm_model_id(llm: Any) -> str:
    for attr in ("model", "model_name", "model_id"):
        try:
            v = getattr(llm, attr, None)
        except Exception:
            v = None
        if isinstance(v, str) and v.strip():
            return v.strip()
    try:
        v = (_resolve_primary_model_id() or "").strip()
        if v:
            return v
    except Exception:
        pass
    return ""


def _cache_get_pydantic(stage: str, payload: Dict[str, Any], model_cls: Any) -> Optional[Any]:
    cache = _get_local_llm_cache()
    if cache is None:
        return None
    key = _cache_key(stage, payload)
    cached = cache.get(key)
    if cached is None:
        return None
    return _model_validate_json_compat(model_cls, cached)


def _cache_set_pydantic(stage: str, payload: Dict[str, Any], model_obj: Any) -> None:
    cache = _get_local_llm_cache()
    if cache is None:
        return
    try:
        cache.set(_cache_key(stage, payload), _model_to_stable_json(model_obj))
    except Exception:
        return


DEPENDENCY_PIP_NAMES = {
    "crewai": "crewai",
    "langchain_openai": "langchain-openai",
    "pydantic": "pydantic",
    "fastapi": "fastapi",
    "uvicorn": "uvicorn",
}


def collect_dependency_versions() -> Dict[str, str]:
    try:
        from importlib import metadata as importlib_metadata
    except Exception:
        try:
            import importlib_metadata  # type: ignore[no-redef]
        except Exception:
            return {}
    versions: Dict[str, str] = {}
    for label, candidates in DEPENDENCY_VERSION_CANDIDATES.items():
        version = None
        for name in candidates:
            try:
                version = importlib_metadata.version(name)
                break
            except Exception:
                continue
        versions[label] = version or "not_installed"
    return versions


# =========================
# 1) JSON Schema (Pydantic)
# =========================
