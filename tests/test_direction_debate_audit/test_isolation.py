"""
v1.1.8 — Tests for isolation mode behaviour in
``build_direction_debate_crew`` and the audit-mode appendix builder.

What this file pins:

* ``_append_audit_mode_instructions(audit_mode=False)`` returns input
  unchanged — pre-v1.1.8 back-compat for the legacy debate flow.
* ``audit_mode=True`` + ``sequential`` adds AUDIT_FINDING appendix but
  NOT the hybrid isolation note.
* ``audit_mode=True`` + ``hybrid`` adds BOTH the AUDIT_FINDING appendix
  AND the hybrid isolation note (instructing agents to treat prior CoT
  as untrusted and rely on structured findings only).
* AUDIT_FINDING + GATE_VERDICT block markers match the parser regex in
  section_02 — structural producer→consumer wiring.

The audit-mode logic is exercised via the helper directly (no crewAI
instantiation needed); this keeps tests fast and deterministic.
"""
from __future__ import annotations

import re

import pytest


# Lazy import of section_04 helpers so test collection does not fail
# when running tests in environments where crewAI is unavailable; the
# section_04 module imports crewai at the top.
def _import_section_04_helpers():
    from crucible.modules.section_04_web_research_and_direction import (
        _append_audit_mode_instructions,
        _AUDIT_FINDING_APPENDIX_TEMPLATE,
        _AUDIT_HYBRID_ISOLATION_NOTE,
        _JUDGE_GATE_VERDICT_APPENDIX,
    )
    return (
        _append_audit_mode_instructions,
        _AUDIT_FINDING_APPENDIX_TEMPLATE,
        _AUDIT_HYBRID_ISOLATION_NOTE,
        _JUDGE_GATE_VERDICT_APPENDIX,
    )


class TestAuditModeOff:
    def test_audit_off_returns_input_unchanged(self) -> None:
        """v1.1.8 back-compat: when audit_mode is False, the appendix
        function MUST return the input string bit-for-bit unchanged.
        This is what guarantees pre-v1.1.8 callers see identical behaviour."""
        append, _, _, _ = _import_section_04_helpers()
        original = "original task description text"
        result = append(
            original, role="explorer", audit_mode=False, isolation_mode="hybrid"
        )
        # Even with isolation_mode=hybrid, audit_mode=False short-circuits.
        assert result == original

    def test_audit_off_with_all_roles(self) -> None:
        append, _, _, _ = _import_section_04_helpers()
        for role in (
            "explorer",
            "comparator",
            "skeptic",
            "evidence_auditor",
            "judge",
        ):
            assert append("X", role=role, audit_mode=False, isolation_mode="sequential") == "X"


class TestAuditModeSequential:
    def test_sequential_adds_audit_finding_appendix(self) -> None:
        append, finding_tpl, hybrid_note, _ = _import_section_04_helpers()
        result = append(
            "original",
            role="skeptic",
            audit_mode=True,
            isolation_mode="sequential",
        )
        # Original preserved at the start.
        assert result.startswith("original")
        # AUDIT_FINDING block markers present.
        assert "AUDIT_FINDING_BEGIN" in result
        assert "AUDIT_FINDING_END" in result
        # Role correctly templated.
        assert 'role="skeptic"' in result

    def test_sequential_does_not_add_hybrid_note(self) -> None:
        """Sequential isolation mode must NOT add the hybrid note —
        agents in sequential mode still receive full prior CoT."""
        append, _, hybrid_note, _ = _import_section_04_helpers()
        result = append(
            "original",
            role="comparator",
            audit_mode=True,
            isolation_mode="sequential",
        )
        # The hybrid note is uniquely identified by its "HYBRID ISOLATION"
        # marker which must not appear in sequential mode.
        assert "HYBRID ISOLATION" not in result


class TestAuditModeHybrid:
    def test_hybrid_adds_both_appendix_and_hybrid_note(self) -> None:
        append, _, _, _ = _import_section_04_helpers()
        result = append(
            "original",
            role="evidence_auditor",
            audit_mode=True,
            isolation_mode="hybrid",
        )
        assert "AUDIT_FINDING_BEGIN" in result
        # Hybrid note must be present.
        assert "HYBRID ISOLATION" in result
        assert "untrusted" in result.lower()

    def test_hybrid_case_insensitive(self) -> None:
        """User may pass 'Hybrid' or 'HYBRID' — both should activate hybrid mode."""
        append, _, _, _ = _import_section_04_helpers()
        for value in ("hybrid", "HYBRID", "Hybrid", " hybrid "):
            result = append(
                "x", role="judge", audit_mode=True, isolation_mode=value
            )
            assert "HYBRID ISOLATION" in result, (
                f"isolation_mode={value!r} did not activate hybrid note"
            )

    def test_unknown_isolation_mode_falls_back_to_sequential_behaviour(
        self,
    ) -> None:
        """Unknown isolation_mode values must NOT activate hybrid (defensive)."""
        append, _, _, _ = _import_section_04_helpers()
        result = append(
            "x", role="judge", audit_mode=True, isolation_mode="quantum"
        )
        assert "AUDIT_FINDING_BEGIN" in result
        assert "HYBRID ISOLATION" not in result


# ── Block markers ↔ parser regex (producer→consumer wiring) ──────────────────


_AUDIT_FINDING_BLOCK_RE = re.compile(
    r'<<<\s*AUDIT_FINDING_BEGIN\s+role\s*=\s*"([^"]+)"\s*>>>'
    r"(.*?)"
    r"<<<\s*AUDIT_FINDING_END\s*>>>",
    re.DOTALL,
)
_GATE_VERDICT_BLOCK_RE = re.compile(
    r"<<<\s*GATE_VERDICT_BEGIN\s*>>>(.*?)<<<\s*GATE_VERDICT_END\s*>>>",
    re.DOTALL,
)


class TestBlockMarkerWiring:
    def test_audit_finding_template_matches_section_02_parser(self) -> None:
        """v1.1.8 producer→consumer wiring: the marker format emitted by
        section_04's appendix template MUST match the regex used by
        section_02's parser.  If either side changes its format without
        the other, every audit event silently disappears.
        """
        append, finding_tpl, _, _ = _import_section_04_helpers()
        # The template includes ``{role}`` placeholder; instantiate as the
        # production code does.
        sample = finding_tpl.format(role="explorer")
        # The parser regex must find exactly one match in the sample.
        matches = _AUDIT_FINDING_BLOCK_RE.findall(sample)
        assert len(matches) == 1, (
            "AUDIT_FINDING template marker format does not match the "
            "parser regex in section_02 — producer/consumer drift."
        )
        role_match, body = matches[0]
        assert role_match == "explorer"

    def test_gate_verdict_template_matches_section_02_parser(self) -> None:
        """Same wiring check for the GATE_VERDICT block (Judge-only)."""
        _, _, _, verdict_tpl = _import_section_04_helpers()
        matches = _GATE_VERDICT_BLOCK_RE.findall(verdict_tpl)
        assert len(matches) == 1


# ── Section_02 parser sanity checks ──────────────────────────────────────────


class TestSection02Parsers:
    def test_parse_audit_findings_recovers_role_and_payload(self) -> None:
        from crucible.modules.section_02_research_and_llm import (
            _parse_audit_findings_from_text,
        )
        raw = (
            'noise before <<<AUDIT_FINDING_BEGIN role="explorer">>>\n'
            '{"role": "explorer", "conclusion": "test", "confidence": 0.5}\n'
            "<<<AUDIT_FINDING_END>>> noise after"
        )
        findings = _parse_audit_findings_from_text(raw)
        assert len(findings) == 1
        assert findings[0]["role"] == "explorer"
        assert findings[0]["conclusion"] == "test"

    def test_parse_gate_verdict_recovers_decision(self) -> None:
        from crucible.modules.section_02_research_and_llm import (
            _parse_gate_verdict_from_text,
        )
        raw = (
            'noise <<<GATE_VERDICT_BEGIN>>>{"decision": "PROCEED", '
            '"selected_direction": "A", "reason": "ok"}<<<GATE_VERDICT_END>>>'
        )
        verdict = _parse_gate_verdict_from_text(raw)
        assert verdict is not None
        assert verdict["decision"] == "PROCEED"

    def test_parse_audit_findings_header_role_overrides_body_role(self) -> None:
        """If LLM emits ``role="skeptic"`` in the header but ``"role":
        "judge"`` inside the JSON, the header takes precedence.  This
        defends against an LLM that confuses itself about which role it
        is playing."""
        from crucible.modules.section_02_research_and_llm import (
            _parse_audit_findings_from_text,
        )
        raw = (
            '<<<AUDIT_FINDING_BEGIN role="skeptic">>>\n'
            '{"role": "judge", "conclusion": "x", "confidence": 0.5}\n'
            "<<<AUDIT_FINDING_END>>>"
        )
        findings = _parse_audit_findings_from_text(raw)
        assert findings[0]["role"] == "skeptic"

    def test_parse_audit_findings_handles_no_blocks(self) -> None:
        from crucible.modules.section_02_research_and_llm import (
            _parse_audit_findings_from_text,
        )
        assert _parse_audit_findings_from_text("no markers here") == []
        assert _parse_audit_findings_from_text("") == []

    def test_parse_gate_verdict_handles_missing_block(self) -> None:
        from crucible.modules.section_02_research_and_llm import (
            _parse_gate_verdict_from_text,
        )
        assert _parse_gate_verdict_from_text("no markers") is None
        assert _parse_gate_verdict_from_text("") is None
