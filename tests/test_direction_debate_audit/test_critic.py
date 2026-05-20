"""
v1.1.8 — Tests for the External Critic (Stage 0 sixth agent).

Coverage focuses on the parts of the critic module that DON'T require a
live LLM call:

* Prompt builder structure (delimiters, isolation note, language hint)
* JSON extraction from noisy LLM-style text
* Dict → GateVerdict coercion (decision normalisation, padding, synonyms)
* NEEDS_MORE_DATA fallback for parse failures
* CriticUnavailableError when LLM is None or crewAI is unavailable
* Critic input isolation: source code MUST NOT include other agents' CoT

The live-LLM end-to-end path (validate_direction_verdict with a real LLM)
is exercised indirectly via test_regression_hard_cases.py with synthetic
mock LLM objects — that test file owns the override-logic verification.
"""
from __future__ import annotations

import inspect

import pytest
from pydantic import ValidationError

from crucible.features.direction_debate import critic as critic_mod
from crucible.features.direction_debate.critic import (
    CriticUnavailableError,
    _build_needs_more_data_fallback,
    _coerce_verdict_dict_to_gateverdict,
    _extract_json_object,
    build_critic_prompt,
    validate_direction_verdict,
)
from crucible.modules.section_03_models_and_context import GateVerdict


# ── Prompt builder ───────────────────────────────────────────────────────────


class TestBuildCriticPrompt:
    def test_includes_judge_decision_and_reason(self) -> None:
        p = build_critic_prompt(
            raw_research_evidence="evidence body",
            judge_decision="PROCEED",
            judge_reason="evidence is sufficient",
            judge_selected_direction="A",
        )
        assert "PROCEED" in p
        assert "evidence is sufficient" in p
        assert "A" in p
        assert "evidence body" in p

    def test_uses_explicit_delimiters_for_evidence(self) -> None:
        p = build_critic_prompt(
            raw_research_evidence="X",
            judge_decision="KILL",
            judge_reason="bad strategy",
        )
        # The evidence block is wrapped in clear BEGIN/END markers so an
        # LLM injecting hostile content cannot easily impersonate the
        # system role.
        assert "RAW RESEARCH EVIDENCE (BEGIN)" in p
        assert "RAW RESEARCH EVIDENCE (END)" in p
        assert "JUDGE VERDICT (BEGIN)" in p
        assert "JUDGE VERDICT (END)" in p

    def test_includes_zh_block_when_language_zh(self) -> None:
        p = build_critic_prompt(
            raw_research_evidence="",
            judge_decision="PROCEED",
            judge_reason="x",
            language_hint="zh",
        )
        assert "繁體中文" in p

    def test_no_zh_block_when_language_en(self) -> None:
        p = build_critic_prompt(
            raw_research_evidence="",
            judge_decision="PROCEED",
            judge_reason="x",
            language_hint="en",
        )
        assert "繁體中文" not in p


# ── Critic input isolation (structural source-code check) ────────────────────


class TestCriticInputIsolation:
    def test_critic_does_not_pull_other_agents_findings(self) -> None:
        """v1.1.8 contract: Critic must be isolated from prior agents'
        chain-of-thought.  Structural source-code check: build_critic_prompt
        must NOT accept (and therefore not be able to leak) explorer /
        comparator / skeptic / auditor outputs.
        """
        sig = inspect.signature(build_critic_prompt)
        forbidden_params = {
            "explorer_finding",
            "comparator_finding",
            "skeptic_finding",
            "auditor_finding",
            "specialist_findings",
            "prior_findings",
        }
        # The function signature must not accept any "prior agent" inputs.
        for name in sig.parameters:
            assert name not in forbidden_params, (
                f"build_critic_prompt accepts {name!r} — Critic isolation "
                f"contract violated.  Critic must see ONLY raw evidence + "
                f"Judge decision token, never prior agents' reasoning."
            )

    def test_critic_validate_function_signature_isolated(self) -> None:
        sig = inspect.signature(validate_direction_verdict)
        forbidden_params = {
            "explorer_finding",
            "comparator_finding",
            "skeptic_finding",
            "auditor_finding",
            "specialist_findings",
            "prior_findings",
        }
        for name in sig.parameters:
            assert name not in forbidden_params


# ── JSON extraction ──────────────────────────────────────────────────────────


class TestExtractJsonObject:
    def test_clean_json_extracted(self) -> None:
        obj = _extract_json_object('{"decision": "PROCEED"}')
        assert obj == {"decision": "PROCEED"}

    def test_markdown_fence_stripped(self) -> None:
        obj = _extract_json_object('```json\n{"decision": "KILL"}\n```')
        assert obj == {"decision": "KILL"}

    def test_prelude_text_tolerated(self) -> None:
        obj = _extract_json_object(
            'Sure, here is my verdict: {"decision": "PROCEED", "selected_direction": "A"}.'
        )
        assert obj is not None
        assert obj.get("decision") == "PROCEED"

    def test_empty_input_returns_none(self) -> None:
        assert _extract_json_object("") is None
        assert _extract_json_object(None) is None  # type: ignore[arg-type]

    def test_invalid_json_returns_none(self) -> None:
        assert _extract_json_object("not even close to JSON") is None


# ── Dict → GateVerdict coercion ──────────────────────────────────────────────


class TestCoerceVerdictDict:
    def test_proceed_dict_coerces(self) -> None:
        v = _coerce_verdict_dict_to_gateverdict(
            {
                "decision": "PROCEED",
                "selected_direction": "A",
                "reason": "evidence sufficient for direction A",
            },
            fallback_reason="(unused)",
        )
        assert v.decision == "PROCEED"
        assert v.selected_direction == "A"

    def test_lowercase_decision_normalised(self) -> None:
        v = _coerce_verdict_dict_to_gateverdict(
            {
                "decision": "proceed",
                "selected_direction": "a",
                "reason": "evidence sufficient for direction A",
            },
            fallback_reason="(unused)",
        )
        assert v.decision == "PROCEED"
        assert v.selected_direction == "A"  # upper-cased

    def test_kill_synonym_normalised(self) -> None:
        v = _coerce_verdict_dict_to_gateverdict(
            {
                "decision": "stop",  # synonym for KILL
                "reason": "hard invariant violated by this strategy",
                "failed_invariants": ["data leakage in lookback window"],
            },
            fallback_reason="(unused)",
        )
        assert v.decision == "KILL"
        assert v.failed_invariants == ["data leakage in lookback window"]

    def test_unknown_decision_raises(self) -> None:
        with pytest.raises(ValueError):
            _coerce_verdict_dict_to_gateverdict(
                {"decision": "ENGAGE_WARP", "reason": "x"},
                fallback_reason="(unused)",
            )

    def test_short_reason_padded(self) -> None:
        v = _coerce_verdict_dict_to_gateverdict(
            {
                "decision": "PROCEED",
                "selected_direction": "A",
                "reason": "OK",  # too short — should be padded
            },
            fallback_reason="this is the fallback explanation",
        )
        assert len(v.reason) >= 20

    def test_kill_without_invariants_propagates_pydantic_error(self) -> None:
        """The coercer normalises but does not invent invariants — pydantic
        invariant validation will reject and the caller's retry layer
        decides whether to try again or fallback."""
        with pytest.raises(ValidationError):
            _coerce_verdict_dict_to_gateverdict(
                {
                    "decision": "KILL",
                    "reason": "this is twenty chars long",
                    "failed_invariants": [],
                },
                fallback_reason="(unused)",
            )

    def test_branch_with_two_paths_coerces(self) -> None:
        v = _coerce_verdict_dict_to_gateverdict(
            {
                "decision": "BRANCH",
                "reason": "the hypothesis splits into two sub-paths",
                "branched_paths": [
                    {"direction_id": "a", "rationale": "venue1 test"},
                    {"direction_id": "B", "rationale": "venue2 test"},
                ],
            },
            fallback_reason="(unused)",
        )
        assert v.decision == "BRANCH"
        assert len(v.branched_paths) == 2
        # direction_id was upper-cased.
        assert v.branched_paths[0].direction_id == "A"


# ── NEEDS_MORE_DATA fallback ─────────────────────────────────────────────────


class TestNeedsMoreDataFallback:
    def test_fallback_is_needs_more_data_not_kill(self) -> None:
        """v1.1.8 contract: when Critic LLM fails, we MUST fall back to
        NEEDS_MORE_DATA (not KILL) so a flaky LLM cannot silently destroy
        viable directions."""
        v = _build_needs_more_data_fallback("LLM timed out after 30s")
        assert v.decision == "NEEDS_MORE_DATA"
        assert len(v.blocking_evidence_queries) >= 1

    def test_fallback_pads_short_reason(self) -> None:
        v = _build_needs_more_data_fallback("x")
        assert len(v.reason) >= 20


# ── CriticUnavailableError ───────────────────────────────────────────────────


class TestCriticUnavailable:
    def test_raises_when_llm_is_none(self) -> None:
        with pytest.raises(CriticUnavailableError):
            validate_direction_verdict(
                raw_research_evidence="evidence",
                judge_decision="PROCEED",
                judge_reason="x",
                llm=None,
            )

    def test_error_message_mentions_llm(self) -> None:
        with pytest.raises(CriticUnavailableError) as exc_info:
            validate_direction_verdict(
                raw_research_evidence="evidence",
                judge_decision="PROCEED",
                judge_reason="x",
                llm=None,
            )
        assert "llm" in str(exc_info.value).lower() or "LLM" in str(exc_info.value)


# ── End-to-end with mocked crewAI ────────────────────────────────────────────


class _MockResult:
    """Mimics a crewAI ``CrewOutput`` with a ``.raw`` attribute."""

    def __init__(self, raw: str) -> None:
        self.raw = raw


class _MockCrew:
    def __init__(self, response_text: str) -> None:
        self._response_text = response_text

    def kickoff(self):
        return _MockResult(self._response_text)


def _install_mock_crewai(monkeypatch, response_text: str) -> None:
    """Replace crewai.Agent/Task/Crew with a minimal mock that returns the
    canned response on kickoff().  The mock bypasses any real LLM call.
    """
    import sys
    import types

    mock_crewai = types.SimpleNamespace()

    class _MockAgent:
        def __init__(self, *args, **kwargs):
            pass

    class _MockTask:
        def __init__(self, *args, **kwargs):
            pass

    def _mock_crew_factory(*args, **kwargs):
        return _MockCrew(response_text)

    class _MockProcess:
        sequential = "sequential"

    mock_crewai.Agent = _MockAgent
    mock_crewai.Task = _MockTask
    mock_crewai.Crew = _mock_crew_factory
    mock_crewai.Process = _MockProcess

    # Inject into sys.modules so the lazy import inside
    # validate_direction_verdict picks up our mock.
    monkeypatch.setitem(sys.modules, "crewai", mock_crewai)


class TestValidateDirectionVerdictE2E:
    def test_critic_returns_proceed_when_llm_says_so(self, monkeypatch) -> None:
        canned = (
            '{"decision": "PROCEED", "selected_direction": "A", '
            '"reason": "critic agrees evidence is sufficient for direction A"}'
        )
        _install_mock_crewai(monkeypatch, canned)
        v = validate_direction_verdict(
            raw_research_evidence="evidence",
            judge_decision="PROCEED",
            judge_reason="x",
            judge_selected_direction="A",
            llm=object(),  # truthy placeholder
        )
        assert v.decision == "PROCEED"
        assert v.audit_trail.external_critic_used is True
        # v1.1.8 invariant: critic_model_family is None.
        assert v.audit_trail.critic_model_family is None

    def test_critic_returns_kill_with_invariants(self, monkeypatch) -> None:
        canned = (
            '{"decision": "KILL", "reason": "hard violation detected by critic", '
            '"failed_invariants": ["data leakage detected"]}'
        )
        _install_mock_crewai(monkeypatch, canned)
        v = validate_direction_verdict(
            raw_research_evidence="evidence",
            judge_decision="PROCEED",
            judge_reason="x",
            llm=object(),
        )
        assert v.decision == "KILL"
        assert v.failed_invariants == ["data leakage detected"]

    def test_critic_fallback_on_parse_failure(self, monkeypatch) -> None:
        """v1.1.8 contract: when LLM returns garbage, fallback is
        NEEDS_MORE_DATA (NEVER KILL).  This protects viable directions
        from flaky-LLM destruction."""
        _install_mock_crewai(monkeypatch, "blah blah no JSON here")
        v = validate_direction_verdict(
            raw_research_evidence="evidence",
            judge_decision="PROCEED",
            judge_reason="x",
            llm=object(),
            max_attempts=1,
        )
        assert v.decision == "NEEDS_MORE_DATA"
        assert len(v.blocking_evidence_queries) >= 1
        assert v.audit_trail.external_critic_used is True
