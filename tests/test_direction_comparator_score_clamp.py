"""Regression tests for :class:`DirectionComparatorItem` score clamping.

Reasoning-class judge models occasionally emit a comparator score outside its
declared bound — the production symptom was
``DirectionComparatorReport.items[6].composite_score == -3`` against a
``composite_score: int = Field(ge=0)`` field, producing repeated
``[Debug] _extract_pydantic_from_result lenient retry for
DirectionComparatorReport failed validation ... Input should be greater than or
equal to 0`` lines and burning the reformat/salvage budget on every affected run.

The strict ``ge``/``le`` field constraints reject the WHOLE item at construction,
and the report's ``after`` normaliser (``_normalize_direction_comparator_items``)
— which already clamps every score with ``max(0, min(5, ...))`` — never runs
because the item never constructs.  A ``model_validator(mode="before")`` on
:class:`DirectionComparatorItem` now clamps the score fields into range *before*
field validation, so every construction path benefits (primary extraction,
lenient retry, salvage, reformat, cache deserialisation).  These tests pin that
contract — including that a well-formed item is untouched and a non-numeric score
is still rejected with a clear error.
"""
from __future__ import annotations

import contextlib
import io

import pytest

from crucible.modules.section_01_extraction_and_reformat import (
    extract_direction_comparator_report,
)
from crucible.modules.section_03_models_and_context import (
    DirectionComparatorItem,
    DirectionComparatorReport,
)

_LETTERS = ["A", "B", "C", "D", "E", "F", "G"]


class TestItemScoreClamp:
    def test_negative_composite_score_clamps_to_zero(self) -> None:
        # The exact production failure: composite_score=-3 (ge=0).
        item = DirectionComparatorItem(key="A", composite_score=-3)
        assert item.composite_score == 0

    def test_subscore_above_ceiling_clamps_to_five(self) -> None:
        item = DirectionComparatorItem(key="B", feasibility_score=7)
        assert item.feasibility_score == 5

    def test_negative_subscore_clamps_to_zero(self) -> None:
        item = DirectionComparatorItem(
            key="C", downside_severity_score=-2, unresolved_unknown_dependency_score=-9
        )
        assert item.downside_severity_score == 0
        assert item.unresolved_unknown_dependency_score == 0

    def test_composite_score_has_no_ceiling(self) -> None:
        # composite_score is ge=0 only — a large value must pass through unclamped.
        item = DirectionComparatorItem(key="D", composite_score=42)
        assert item.composite_score == 42

    def test_well_formed_item_untouched(self) -> None:
        item = DirectionComparatorItem(
            key="E",
            feasibility_score=4,
            reversibility_score=3,
            speed_to_test_score=5,
            evidence_strength_score=2,
            downside_severity_score=1,
            unresolved_unknown_dependency_score=0,
            composite_score=9,
        )
        assert (item.feasibility_score, item.reversibility_score, item.speed_to_test_score) == (4, 3, 5)
        assert (item.evidence_strength_score, item.downside_severity_score) == (2, 1)
        assert item.unresolved_unknown_dependency_score == 0
        assert item.composite_score == 9

    def test_numeric_string_score_is_coerced_and_clamped(self) -> None:
        item = DirectionComparatorItem(key="F", composite_score="-3", feasibility_score="8")
        assert item.composite_score == 0
        assert item.feasibility_score == 5

    def test_non_numeric_score_still_rejected(self) -> None:
        # A non-numeric score is left in place for the field validator to reject
        # with a clear error rather than silently coerced.
        with pytest.raises(Exception):
            DirectionComparatorItem(key="G", composite_score="not-a-number")


class _FakeCrewResult:
    """Minimal stand-in for a crewAI result object carrying ``json_dict``."""

    def __init__(self, payload: dict) -> None:
        self.json_dict = payload
        self.raw = ""
        self.output = ""

    @property
    def tasks_output(self):  # noqa: D401 - simple stub
        return []


def _report_payload(bad_index: int = 6, bad_value: int = -3) -> dict:
    return {
        "items": [
            {
                "key": letter,
                "feasibility_score": 3,
                "reversibility_score": 3,
                "speed_to_test_score": 3,
                "evidence_strength_score": 3,
                "downside_severity_score": 1,
                "unresolved_unknown_dependency_score": 1,
                "composite_score": (bad_value if i == bad_index else i + 1),
                "rationale": f"rationale {letter}",
            }
            for i, letter in enumerate(_LETTERS)
        ],
        "top_keys": ["A", "B", "C"],
        "comparison_notes": ["note one"],
    }


class TestReportConstructsWithBadItem:
    def test_full_report_with_negative_composite_constructs(self) -> None:
        report = DirectionComparatorReport(**_report_payload())
        assert len(report.items) == 7
        g_item = [it for it in report.items if it.key == "G"]
        assert g_item and g_item[0].composite_score == 0


class TestExtractionPathNoLongerSpamsDebug:
    def test_extract_succeeds_without_lenient_retry_debug_line(self) -> None:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            report = extract_direction_comparator_report(_FakeCrewResult(_report_payload()))
        stdout = buf.getvalue()
        assert report is not None
        assert len(report.items) == 7
        assert "lenient retry" not in stdout, stdout


if __name__ == "__main__":
    import sys

    sys.exit(pytest.main([__file__, "-q"]))
