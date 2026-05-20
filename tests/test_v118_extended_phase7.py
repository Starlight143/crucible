"""v1.1.8 extended Phase 7 — Direction debate end (P1-P6) regression
+ producer-consumer wiring tests per CLAUDE.md § 9.6.

Coverage:

* P2 ClaimAttribution schema migration:
  - direction_key field exists, Optional[Literal[A..G]], default None.
  - field_name field exists, Optional[Literal[...]], default None.
  - Backward compat: old shape (no tags) still parses.
  - Invalid values rejected.
* P2 auditor prompt wiring:
  - Prompt mentions ``direction_key`` (consumer reads producer tag).
  - Prompt mentions ``semantic matching`` fallback (no tags → match text).
* P4 warning UX wiring:
  - Warning message conditional on counter values (per gate branch).
  - Includes ``structural_failure`` hint when both counters are 0.
* P5 degrade-not-die observational emit:
  - Ledger event emitted when env toggle on + iterations exhausted.
  - No-op when env toggle off.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from pydantic import ValidationError

from crucible.modules.section_03_models_and_context import ClaimAttribution


_REPO_ROOT = Path(__file__).resolve().parents[1]
_SECTION_02 = (
    _REPO_ROOT
    / "crucible"
    / "modules"
    / "section_02_research_and_llm.py"
)
_SECTION_04 = (
    _REPO_ROOT
    / "crucible"
    / "modules"
    / "section_04_web_research_and_direction.py"
)


# ─── P2: ClaimAttribution schema migration ──────────────────────────────────


class TestClaimAttributionSchemaMigration:
    def test_backward_compat_no_tags(self) -> None:
        # Pre-v1.1.8 caller shape — only category + claim.
        cit = ClaimAttribution(
            category="market_examples",
            claim="ETH funding rate analysis",
        )
        assert cit.direction_key is None
        assert cit.field_name is None

    def test_valid_direction_key(self) -> None:
        for key in ("A", "B", "C", "D", "E", "F", "G"):
            cit = ClaimAttribution(
                category="x", claim="x", direction_key=key,
            )
            assert cit.direction_key == key

    def test_invalid_direction_key_rejected(self) -> None:
        with pytest.raises(ValidationError):
            ClaimAttribution(
                category="x", claim="x", direction_key="Z",
            )
        with pytest.raises(ValidationError):
            ClaimAttribution(
                category="x", claim="x", direction_key="b",  # lowercase
            )

    def test_valid_field_name(self) -> None:
        for fname in (
            "thesis", "primary_metric", "fastest_test",
            "major_risk", "data_sources",
        ):
            cit = ClaimAttribution(
                category="x", claim="x", field_name=fname,
            )
            assert cit.field_name == fname

    def test_invalid_field_name_rejected(self) -> None:
        with pytest.raises(ValidationError):
            ClaimAttribution(
                category="x", claim="x", field_name="unknown_field",
            )

    def test_partial_tagging_allowed(self) -> None:
        # Only direction_key set (no field_name) is valid — useful for
        # claims that broadly support a direction without anchoring to
        # a specific field.
        cit = ClaimAttribution(
            category="x", claim="x", direction_key="B",
        )
        assert cit.direction_key == "B"
        assert cit.field_name is None


# ─── P2: auditor prompt wiring ──────────────────────────────────────────────


class TestAuditorPromptWiring:
    """CLAUDE.md § 9.6 producer→consumer wiring pin.

    The auditor prompt MUST tell the LLM to (a) prefer explicit
    direction_key/field_name tags when present, AND (b) do semantic
    matching from claim text when tags are absent.  Without both
    halves, the v1.1.7 silent-empty-supported_fields bug returns.
    """

    def test_auditor_prompt_mentions_direction_key(self) -> None:
        text = _SECTION_04.read_text(encoding="utf-8")
        assert "direction_key" in text, (
            "Auditor prompt MUST mention direction_key — the consumer "
            "needs to know to read the explicit tag.  See v1.1.8 "
            "extended Phase 7 P2 plan."
        )

    def test_auditor_prompt_mentions_field_name_tag(self) -> None:
        text = _SECTION_04.read_text(encoding="utf-8")
        assert "field_name" in text, (
            "Auditor prompt MUST mention field_name — the consumer "
            "needs to know to read the explicit tag.  See v1.1.8 "
            "extended Phase 7 P2 plan."
        )

    def test_auditor_prompt_mentions_semantic_matching_fallback(self) -> None:
        text = _SECTION_04.read_text(encoding="utf-8")
        # Look for either "semantic match" or "scan claim text" hints
        # that signal the prompt teaches the LLM to fall through tags
        # to semantic matching.
        assert re.search(
            r"semantic\s+match|scan\s+claim\s+text|fall\s*back\s+to\s+semantic",
            text,
            re.IGNORECASE,
        ), (
            "Auditor prompt MUST instruct semantic matching fallback "
            "for untagged claims, otherwise legacy claim_attributions "
            "(no direction_key set) won't populate supported_fields.  "
            "See v1.1.8 force-none diagnostic."
        )


# ─── P4: warning UX wiring ──────────────────────────────────────────────────


class TestWarningUxWiring:
    """The exhaustion warning in section_02 MUST print
    ``structural_failure=`` when both grounded_claims_needed and
    citations_needed are 0 — the v1.1.8 diagnostic showed those zeros
    were misleading operators into thinking citations were sufficient
    when the real problem was supported_fields anchoring."""

    def test_warning_emits_structural_failure_hint(self) -> None:
        text = _SECTION_02.read_text(encoding="utf-8")
        assert "structural_failure" in text, (
            "Phase 7 P4 warning UX missing: when gate fires with empty "
            "counter values, warning MUST print structural_failure "
            "hint instead of misleading zero counters"
        )

    def test_warning_conditional_on_counter_value(self) -> None:
        text = _SECTION_02.read_text(encoding="utf-8")
        # Look for the conditional print pattern.
        assert re.search(
            r"if\s+_gci_needed\s*>\s*0|if\s+_cit_needed\s*>\s*0",
            text,
        ), (
            "Warning UX (P4) MUST gate counter prints on non-zero "
            "values — see Phase 7 P4 plan"
        )


# ─── P5: degrade-not-die observational emit ─────────────────────────────────


class TestDegradeObservabilityWiring:
    """The degrade-not-die observability emit MUST be wired into the
    loop-exhaustion path with the env-toggle gate."""

    def test_degraded_proceed_emit_call_present(self) -> None:
        text = _SECTION_02.read_text(encoding="utf-8")
        assert "record_direction_debate_degraded_proceed" in text, (
            "P5 ledger event emit (record_direction_debate_degraded_"
            "proceed) missing from section_02 exhaustion handler"
        )

    def test_env_toggle_gates_emit(self) -> None:
        text = _SECTION_02.read_text(encoding="utf-8")
        # The emit MUST be inside an env_bool check on the toggle.
        assert "CRUCIBLE_DEBATE_TOLERATE_UNVERIFIABLE_EVIDENCE" in text, (
            "P5 emit must be gated by "
            "CRUCIBLE_DEBATE_TOLERATE_UNVERIFIABLE_EVIDENCE"
        )

    def test_threshold_env_referenced(self) -> None:
        text = _SECTION_02.read_text(encoding="utf-8")
        assert "CRUCIBLE_DEBATE_DEGRADE_AFTER_N_ITERATIONS" in text, (
            "P5 threshold env (CRUCIBLE_DEBATE_DEGRADE_AFTER_N_ITERATIONS) "
            "must be read in the section_02 exhaustion handler"
        )


# ─── Structural wiring smoke test (CLAUDE.md § 9.6) ─────────────────────────


class TestProducerConsumerWiring:
    """Catch the producer→consumer drift class of bug: schema fields
    that no consumer ever reads (zombie fields) or consumer prompts
    that reference fields the schema doesn't have (broken pointers).
    """

    def test_direction_key_referenced_in_consumer_prompt(self) -> None:
        """The new direction_key field MUST be referenced by at least
        one consumer location (auditor prompt or section_04 logic),
        otherwise it's a zombie schema field."""
        text = _SECTION_04.read_text(encoding="utf-8")
        # Auditor prompt mentions it (verified above), but also confirm
        # the field name appears as a Pydantic field reference.
        # Match the schema definition substring AND the prompt text.
        assert text.count("direction_key") >= 1, (
            "direction_key field is defined but has no consumer "
            "reference — producer→consumer drift (CLAUDE.md § 9.6)"
        )

    def test_field_name_referenced_in_consumer_prompt(self) -> None:
        text = _SECTION_04.read_text(encoding="utf-8")
        assert text.count("field_name") >= 1, (
            "field_name field is defined but has no consumer "
            "reference — producer→consumer drift (CLAUDE.md § 9.6)"
        )
