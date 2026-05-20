"""
crucible.features.direction_debate.critic
==========================================

v1.1.8 — Stage 0 External Critic.

The External Critic is an opt-in sixth agent (gated by
``CRUCIBLE_DEBATE_EXTERNAL_CRITIC=1``) that re-judges the Judge's verdict
using *only* the raw research evidence + the Judge's terminal decision
token + reason.  The Critic does NOT see any other agent's chain-of-thought,
so it is isolated from sequential-anchoring bias.

Why a separate module (not ``features/independent_validator.py``)
-----------------------------------------------------------------
The existing ``independent_validator`` runs **post-codegen** subprocess
checks (py_compile, pytest, smoke test) plus an adversarial LLM review.  Its
input is a directory of generated code; its mechanism is subprocess
isolation.  Stage 0 critic input is research evidence + a decision token;
its mechanism is a single isolated LLM call.  Mixing the two would conflate
two different audit phases.  This module instead reuses the project's
generic LLM call/JSON-extract patterns (``crewai.Agent`` + ``Task`` + ``Crew``
minimal one-shot) and writes its own Stage-0-specific prompt.

Model family selection (v1.1.8 vs v1.3.0)
-----------------------------------------
v1.1.8 uses the **same model family as the Judge** — typically OpenRouter
GLM / Anthropic Claude / Alibaba qwen depending on operator config.  Same
family means same blind-spot risk; this is acknowledged in
``AuditTrail.critic_model_family = None`` (v1.3.0 will populate it once
cross-family critic infrastructure ships).  Even with same-family Critic,
the value is non-zero: the Critic does NOT see prior agents' free-form
reasoning, so anchoring bias is reduced even when family-level blind spots
remain.

Override semantics
------------------
The orchestrator (``section_02:_run_single_direction_debate``) calls this
function only when ``CRUCIBLE_DEBATE_EXTERNAL_CRITIC=1``.  The Critic
returns its own :class:`GateVerdict`.  Combination logic in the
orchestrator:

* ``CRUCIBLE_DEBATE_CRITIC_OVERRIDE_PROCEED=0`` (default):  Critic's verdict
  is recorded as ``audit_trail.critic_dissent_recorded`` if it differs from
  Judge but Judge's decision stands.
* ``=1``: If Judge says PROCEED but Critic says KILL, Critic wins.  Other
  conflict combinations still defer to Judge.

This module is deliberately conservative — if the Critic's LLM call fails
to produce parseable JSON after retry, we return ``NEEDS_MORE_DATA`` (not
KILL), so a flaky LLM does not silently kill viable directions.
"""
from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional

# Tri-modal import — see ``features/run_insights/recorder.py`` for the
# rationale.  Note we do NOT import crewai at module level; that import
# may fail in test fixtures that exercise pure-pydantic schema flow.
try:
    from ..._env import env_bool, env_int
    from ...runtime_logging import get_logger
    from ...modules.section_03_models_and_context import (
        GateVerdict,
        SpecialistFinding,
    )
except ImportError:  # pragma: no cover - flat-launcher fallback
    from _env import env_bool, env_int  # type: ignore[no-redef]
    from runtime_logging import get_logger  # type: ignore[no-redef]
    from modules.section_03_models_and_context import (  # type: ignore[no-redef]
        GateVerdict,
        SpecialistFinding,
    )


LOGGER = get_logger(__name__)


# ── Public errors ────────────────────────────────────────────────────────────

class CriticUnavailableError(RuntimeError):
    """Raised when the External Critic cannot run (LLM not configured,
    crewAI import failed, etc.).

    The orchestrator should catch this and treat it as "critic skipped";
    it must NOT propagate to break Stage 0.  Same defensive posture as
    every other audit-mode emit point.
    """


# ── Prompt builder (separated for testability) ────────────────────────────────

_CRITIC_OUTPUT_SCHEMA_JSON = """
{
  "decision": "PROCEED | BRANCH | KILL | NEEDS_MORE_DATA",
  "selected_direction": "A | B | C | D | E | F | G | null",
  "reason": "≥20 chars explaining the decision",
  "failed_invariants": ["list of strings (REQUIRED if decision=KILL)"],
  "blocking_evidence_queries": ["list of strings (REQUIRED if decision=NEEDS_MORE_DATA)"],
  "branched_paths": [
    {"direction_id": "A", "rationale": "...", "blocking_questions": []}
  ],
  "disagreement_with_judge": "≥0 chars describing how Critic disagrees with Judge"
}
""".strip()


_CRITIC_SYSTEM_INSTRUCTIONS = """\
You are the External Critic for a multi-agent direction-debate gate.

Your job is to independently re-judge a strategic decision based ONLY on:
  - The raw research evidence (provided below)
  - The Judge's terminal decision token (PROCEED / KILL / etc.)
  - The Judge's brief reason for the decision

You do NOT see the Explorer, Comparator, Skeptic, or Evidence Auditor's
chain-of-thought.  This isolation is intentional — you must form an
independent view, not anchor on prior agents' reasoning.

Your output is a JSON object matching the GateVerdict schema below.  You
MUST output one of four decisions:

  PROCEED          — evidence is sufficient AND no hard invariants violated
  BRANCH           — the hypothesis should split into ≥2 distinct sub-paths
                     (each with its own evidence requirements)
  KILL             — at least one HARD invariant is violated (e.g. strategy
                     assumes cointegration on a known non-cointegrated pair).
                     KILL requires ≥1 entries in ``failed_invariants``.
  NEEDS_MORE_DATA  — Judge may be right, but evidence is too thin to confirm.
                     Requires ≥1 entries in ``blocking_evidence_queries``.

Rules:

1. DO NOT default to NEEDS_MORE_DATA when KILL is structurally correct.
   If you find a hard invariant violated, output KILL with the specific
   invariant in ``failed_invariants``.

2. DO NOT KILL on thin evidence.  KILL is for hard violations; thin
   evidence belongs in NEEDS_MORE_DATA with specific ``blocking_evidence_queries``.

3. If Judge says PROCEED but you find a hard violation, output KILL.  If
   you find weak evidence, output NEEDS_MORE_DATA.  Otherwise PROCEED is
   correct.

4. If Judge says KILL, you may agree (PROCEED-style "no, this is fine" is
   acceptable too if Judge over-killed).  Cite specific evidence either way.

5. Be specific.  "Insufficient evidence" alone is not enough — name what
   evidence is missing.

Output ONLY a JSON object matching this schema (no markdown fences, no
prose before/after):

""" + _CRITIC_OUTPUT_SCHEMA_JSON


def build_critic_prompt(
    *,
    raw_research_evidence: str,
    judge_decision: str,
    judge_reason: str,
    judge_selected_direction: Optional[str] = None,
    language_hint: str = "en",
) -> str:
    """Build the External Critic prompt.

    Separated from the LLM-call path so tests can verify prompt structure
    without invoking any model.

    The prompt format is intentionally strict: input variables are wrapped
    in clear delimiters so an LLM injecting hostile content in
    ``raw_research_evidence`` cannot easily impersonate the system role.
    """
    lang_block = ""
    if (language_hint or "").lower() in {"zh", "zh-tw", "zh-cn", "zh_tw", "zh_cn"}:
        lang_block = (
            "\n(請以繁體中文輸出 ``reason`` 與 ``disagreement_with_judge`` 兩個欄位；"
            "JSON 結構與其他 enum 值維持英文。)\n"
        )

    sd_line = ""
    if judge_selected_direction:
        sd_line = f"\nJudge selected direction: {judge_selected_direction}"

    return (
        _CRITIC_SYSTEM_INSTRUCTIONS
        + "\n\n"
        + "=== RAW RESEARCH EVIDENCE (BEGIN) ===\n"
        + str(raw_research_evidence or "").strip()
        + "\n=== RAW RESEARCH EVIDENCE (END) ===\n"
        + "\n"
        + "=== JUDGE VERDICT (BEGIN) ===\n"
        + f"Decision: {judge_decision}\n"
        + f"Reason: {judge_reason}"
        + sd_line
        + "\n=== JUDGE VERDICT (END) ===\n"
        + lang_block
    )


# ── JSON extraction ───────────────────────────────────────────────────────────

_JSON_OBJECT_RE = re.compile(r"\{.*\}", re.DOTALL)


def _extract_json_object(text: str) -> Optional[Dict[str, Any]]:
    """Extract a single JSON object from a possibly-noisy LLM response.

    Strategy:
      1. Try ``json.loads`` on the raw stripped text.
      2. If that fails, find the first ``{...}`` substring (greedy DOTALL)
         and try parsing that.  Handles "Sure, here's the JSON: {...}"
         preludes and trailing commentary.
      3. Return ``None`` on any failure — caller decides how to fallback.
    """
    if not text:
        return None
    raw = str(text).strip()
    # Drop markdown code fences if present.
    raw = re.sub(r"^```(?:json)?\s*", "", raw, flags=re.IGNORECASE)
    raw = re.sub(r"\s*```$", "", raw)
    try:
        obj = json.loads(raw)
        if isinstance(obj, dict):
            return obj
    except (ValueError, TypeError):
        pass
    m = _JSON_OBJECT_RE.search(raw)
    if not m:
        return None
    try:
        obj = json.loads(m.group(0))
        if isinstance(obj, dict):
            return obj
    except (ValueError, TypeError):
        return None
    return None


def _coerce_verdict_dict_to_gateverdict(
    data: Dict[str, Any],
    *,
    fallback_reason: str,
) -> "GateVerdict":
    """Coerce a raw LLM JSON dict into a validated ``GateVerdict``.

    Applies normalisation:
      * ``decision`` upper-cased and stripped
      * ``selected_direction`` upper-cased; "null"/""/None → None
      * Lists default to ``[]``; strings stripped
      * ``reason`` padded to ≥20 chars by appending fallback excerpt

    Raises ``ValueError`` if the pydantic validation still fails after
    normalisation (caller decides whether to retry or fallback to
    ``NEEDS_MORE_DATA``).
    """
    decision = str(data.get("decision") or "").strip().upper()
    if decision not in {"PROCEED", "BRANCH", "KILL", "NEEDS_MORE_DATA"}:
        # Most common malformed cases: "Proceed", "proceed", "GO", "STOP".
        # Map a small set of synonyms; otherwise raise so caller retries.
        synonym_map = {
            "GO": "PROCEED", "OK": "PROCEED", "APPROVE": "PROCEED",
            "STOP": "KILL", "REJECT": "KILL", "VETO": "KILL",
            "SPLIT": "BRANCH", "FORK": "BRANCH",
            "WAIT": "NEEDS_MORE_DATA", "PAUSE": "NEEDS_MORE_DATA",
        }
        decision = synonym_map.get(decision, "")
        if not decision:
            raise ValueError(f"unrecognised decision token: {data.get('decision')!r}")

    sd_raw = data.get("selected_direction")
    if sd_raw is None or str(sd_raw).strip().lower() in {"", "null", "none"}:
        selected_direction: Optional[str] = None
    else:
        selected_direction = str(sd_raw).strip().upper()

    reason = str(data.get("reason") or "").strip()
    if len(reason) < 20:
        # Pad with fallback so pydantic validator passes; keep the original
        # at the start so retrieval can still index on it.
        pad = fallback_reason or "no detailed reason provided by external critic"
        reason = (reason + " | " + pad).strip()
        if len(reason) < 20:
            reason = reason + (" " * (20 - len(reason)))

    failed_invariants = [
        str(s).strip() for s in (data.get("failed_invariants") or []) if str(s).strip()
    ]
    blocking_queries = [
        str(s).strip()
        for s in (data.get("blocking_evidence_queries") or [])
        if str(s).strip()
    ]

    raw_branches = data.get("branched_paths") or []
    branched_paths: List[Dict[str, Any]] = []
    for raw in raw_branches:
        if not isinstance(raw, dict):
            continue
        direction_id = str(raw.get("direction_id") or "").strip().upper()
        if not direction_id:
            continue
        branched_paths.append(
            {
                "direction_id": direction_id,
                "rationale": str(raw.get("rationale") or "no rationale provided").strip()
                or "no rationale provided",
                "blocking_questions": [
                    str(q).strip()
                    for q in (raw.get("blocking_questions") or [])
                    if str(q).strip()
                ],
            }
        )

    # Build payload for pydantic.  Pydantic invariant validators will
    # raise ValueError if any structural rule is violated.
    payload: Dict[str, Any] = {
        "decision": decision,
        "selected_direction": selected_direction if decision == "PROCEED" else None,
        "branched_paths": branched_paths,
        "failed_invariants": failed_invariants,
        "blocking_evidence_queries": blocking_queries,
        "reason": reason,
    }
    return GateVerdict(**payload)


def _build_needs_more_data_fallback(reason: str) -> "GateVerdict":
    """Construct a safe NEEDS_MORE_DATA fallback when the Critic LLM call
    produced no parseable verdict.

    This is the v1.1.8 "fail-safe" — we explicitly do NOT KILL when the
    Critic is itself broken, since a flaky LLM should not silently destroy
    work the Judge approved.  ``blocking_evidence_queries`` is populated
    with a generic "rerun critic" query so the verdict satisfies pydantic.
    """
    safe_reason = (reason or "").strip()
    if len(safe_reason) < 20:
        safe_reason = (
            "External critic produced no parseable verdict after retries; "
            "deferring with NEEDS_MORE_DATA fallback. " + safe_reason
        ).strip()
    return GateVerdict(
        decision="NEEDS_MORE_DATA",
        reason=safe_reason,
        blocking_evidence_queries=[
            "rerun external critic with a fresh LLM context",
            "verify research evidence reaches the critic LLM",
        ],
    )


# ── Public entry point ───────────────────────────────────────────────────────

def validate_direction_verdict(
    *,
    raw_research_evidence: str,
    judge_decision: str,
    judge_reason: str,
    judge_selected_direction: Optional[str] = None,
    llm: Optional[Any] = None,
    language_hint: str = "en",
    max_attempts: Optional[int] = None,
) -> "GateVerdict":
    """Re-judge the Judge's verdict using an independent LLM call.

    Parameters
    ----------
    raw_research_evidence
        Plain-text or markdown bundle of the raw research evidence as
        provided to the Judge.  The Critic sees this verbatim.
    judge_decision
        The Judge's decision token: ``"PROCEED"`` / ``"KILL"`` / etc., OR
        a legacy ``"none"`` / ``"A"`` / ``"B"`` token from the pre-audit-mode
        ``DirectionDecision`` flow.  Both are normalised internally.
    judge_reason
        The Judge's brief reason for the decision (typically the ``summary``
        field of ``DirectionDecision``).
    judge_selected_direction
        Direction key A-G if Judge selected one; ``None`` for force-none.
    llm
        A crewAI ``LLM`` object (or any object with the crewAI LLM contract).
        If ``None``, raises :class:`CriticUnavailableError` — the orchestrator
        is responsible for constructing or providing the LLM.
    language_hint
        ``"en"`` | ``"zh"`` — only affects the language of free-form fields
        (``reason``, ``disagreement_with_judge``).  JSON schema and enum
        values stay English.
    max_attempts
        Optional override for retry count.  Defaults to
        ``CRUCIBLE_DEBATE_CRITIC_MAX_ATTEMPTS`` (env var; default 2).

    Returns
    -------
    GateVerdict
        The Critic's independent verdict.  Always a valid :class:`GateVerdict`
        — if the LLM call fails or returns unparseable output after all
        retries, returns a safe ``NEEDS_MORE_DATA`` fallback (never KILL on
        critic failure, never silently PROCEED).

    Raises
    ------
    CriticUnavailableError
        If ``llm`` is ``None`` or the crewAI import fails.  Orchestrator
        should treat as "critic skipped"; never propagate.
    """
    if llm is None:
        raise CriticUnavailableError("External Critic requires an LLM instance")

    # Lazy import: crewAI may not be available in pure-pydantic test paths.
    try:
        from crewai import Agent, Crew, Process, Task
    except ImportError as exc:  # pragma: no cover - environment-specific
        raise CriticUnavailableError(
            f"crewAI is not available for External Critic: {exc}"
        ) from exc

    if max_attempts is None:
        max_attempts = env_int(
            "CRUCIBLE_DEBATE_CRITIC_MAX_ATTEMPTS",
            2,
            clamp_min=1,
            clamp_max=5,
        )

    prompt = build_critic_prompt(
        raw_research_evidence=raw_research_evidence,
        judge_decision=judge_decision,
        judge_reason=judge_reason,
        judge_selected_direction=judge_selected_direction,
        language_hint=language_hint,
    )

    fallback_reason = (
        f"External Critic: Judge said {judge_decision!r} with reason "
        f"{(judge_reason or '')[:200]!r}; Critic could not produce a "
        f"structured verdict."
    )

    last_error: Optional[Exception] = None
    last_raw_output: str = ""

    for attempt in range(max(1, int(max_attempts))):
        try:
            critic_agent = Agent(
                role="External Direction Critic",
                goal=(
                    "Re-judge a Stage 0 direction decision using ONLY raw "
                    "research evidence and the Judge's terminal decision; "
                    "output a valid GateVerdict JSON."
                ),
                backstory=(
                    "You are deliberately isolated from prior agents' "
                    "reasoning to provide an independent second opinion."
                ),
                llm=llm,
                allow_delegation=False,
                verbose=False,
                max_iter=2,
            )
            critic_task = Task(
                description=prompt,
                expected_output=(
                    "A single JSON object matching the GateVerdict schema "
                    "with the four-decision enum and shape-specific required "
                    "fields populated."
                ),
                agent=critic_agent,
            )
            crew = Crew(
                agents=[critic_agent],
                tasks=[critic_task],
                process=Process.sequential,
                verbose=False,
            )
            result = crew.kickoff()
            # crewAI ``CrewOutput`` exposes ``.raw`` (preferred) or stringifies
            # to the raw response.  Try ``.raw`` first; fall back to str().
            raw_output = ""
            for attr in ("raw", "output", "result"):
                val = getattr(result, attr, None)
                if isinstance(val, str) and val.strip():
                    raw_output = val
                    break
            if not raw_output:
                raw_output = str(result or "")
            last_raw_output = raw_output

            data = _extract_json_object(raw_output)
            if data is None:
                last_error = ValueError("no JSON object found in critic output")
                continue

            verdict = _coerce_verdict_dict_to_gateverdict(
                data, fallback_reason=fallback_reason
            )
            # Success — annotate audit_trail.critic_model_family (v1.1.8: None).
            verdict.audit_trail.external_critic_used = True
            verdict.audit_trail.critic_model_family = None
            return verdict

        except Exception as exc:  # noqa: BLE001 — critic must never break pipeline
            last_error = exc
            LOGGER.debug(
                "external_critic: attempt %d/%d failed: %s",
                attempt + 1,
                max_attempts,
                exc,
            )
            continue

    # All retries exhausted — return safe NEEDS_MORE_DATA fallback.
    LOGGER.warning(
        "external_critic: all %d attempts failed; returning NEEDS_MORE_DATA "
        "fallback (last_error=%r raw_output_len=%d)",
        max_attempts,
        last_error,
        len(last_raw_output or ""),
    )
    fb = _build_needs_more_data_fallback(fallback_reason)
    fb.audit_trail.external_critic_used = True
    fb.audit_trail.critic_model_family = None
    return fb


__all__ = [
    "CriticUnavailableError",
    "build_critic_prompt",
    "validate_direction_verdict",
]
