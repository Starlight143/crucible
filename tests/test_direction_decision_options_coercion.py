"""Regression tests for :class:`DirectionDecision` ``options`` shape coercion.

Reasoning-class judge models routinely emit the seven direction options in one
of two shapes that strict pydantic validation rejects outright, which caused the
``[Debug] _extract_pydantic_from_result lenient retry for DirectionDecision
failed validation ...`` spam in production logs and burned the reformat/salvage
retry budget on every direction-debate run:

1. ``options`` as a **mapping keyed by direction letter**
   (``{"A": {...}, "B": {...}}``) instead of the declared list, producing
   ``options Input should be a valid list [type=list_type]``.
2. ``options`` as a **list whose items omit the required ``key``** (the letter
   lives in ``name`` or is implied purely by position), producing seven
   ``options.N.key Field required`` errors.

A ``model_validator(mode="before")`` on :class:`DirectionDecision` now repairs
both shapes at construction time so *every* construction path benefits (primary
extraction, salvage, reformat, crewAI ``output_pydantic`` parsing and cache
deserialisation).  These tests pin that contract and — crucially — pin that a
well-formed payload is **never** mutated and an explicit ``key`` is **never**
overwritten.
"""
from __future__ import annotations

import io
import contextlib

import pytest

from crucible.modules.section_01_extraction_and_reformat import (
    extract_direction_decision,
)
from crucible.modules.section_03_models_and_context import (
    DirectionDecision,
    _normalize_direction_decision,
)

_LETTERS = ["A", "B", "C", "D", "E", "F", "G"]


def _base_decision_fields() -> dict:
    return dict(
        selected_direction="A",
        summary="decision summary",
        backup_candidates=[],
        go_conditions=["proceed only after evidence review"],
        kill_criteria=["stop if evidence contradicts"],
        confidence="low",
        verify_plan=["re-run validation"],
    )


def _option(letter: str, name: str | None = None) -> dict:
    return {
        "key": letter,
        "name": name or f"Direction {letter}",
        "thesis": f"thesis for {letter}",
        "primary_metric": "Sharpe",
        "fastest_test": "backtest 30d",
        "major_risk": "overfitting",
    }


def _option_without_key(letter: str, name: str | None = None) -> dict:
    opt = _option(letter, name)
    opt.pop("key")
    return opt


# ──────────────────────────────────────────────────────────────────────────────
# Shape 1 — options as a mapping keyed by direction letter
# ──────────────────────────────────────────────────────────────────────────────
class TestShape1MappingKeyedByLetter:
    def test_mapping_values_carrying_key_become_ordered_list(self) -> None:
        payload = dict(
            _base_decision_fields(),
            options={letter: _option(letter) for letter in _LETTERS},
        )
        model = DirectionDecision(**payload)
        assert isinstance(model.options, list)
        assert [o.key for o in model.options] == _LETTERS

    def test_mapping_values_missing_key_inherit_mapping_key(self) -> None:
        payload = dict(
            _base_decision_fields(),
            options={
                letter: _option_without_key(letter, name=f"Name {letter}")
                for letter in _LETTERS
            },
        )
        model = DirectionDecision(**payload)
        assert [o.key for o in model.options] == _LETTERS
        # The descriptive name must be preserved, not clobbered by the key.
        assert model.options[0].name == "Name A"

    def test_mapping_key_does_not_overwrite_explicit_value_key(self) -> None:
        # A perverse-but-real case: the mapping key disagrees with the value's
        # own ``key``.  The explicit field wins; the mapping key is ignored.
        opts = {"Z": _option("A")}
        opts.update({letter: _option(letter) for letter in _LETTERS[1:]})
        payload = dict(_base_decision_fields(), options=opts)
        model = DirectionDecision(**payload)
        assert sorted(o.key for o in model.options) == _LETTERS


# ──────────────────────────────────────────────────────────────────────────────
# Shape 2 — options as a list whose items omit ``key``
# ──────────────────────────────────────────────────────────────────────────────
class TestShape2ListMissingKey:
    def test_list_missing_key_with_letter_in_name(self) -> None:
        # Mirrors the production log where ``name`` held the bare letter.
        payload = dict(
            _base_decision_fields(),
            options=[_option_without_key(letter, name=letter) for letter in _LETTERS],
        )
        model = DirectionDecision(**payload)
        assert [o.key for o in model.options] == _LETTERS

    def test_list_missing_key_with_real_names_keys_by_position(self) -> None:
        names = ["DynZ", "Slip", "Spread", "Funding", "VolClust", "MM", "Backtest"]
        payload = dict(
            _base_decision_fields(),
            options=[
                _option_without_key(letter, name=names[i])
                for i, letter in enumerate(_LETTERS)
            ],
        )
        model = DirectionDecision(**payload)
        assert [o.key for o in model.options] == _LETTERS
        assert [o.name for o in model.options] == names

    def test_list_partial_missing_key_fills_only_blanks_by_position(self) -> None:
        options = [_option(letter) for letter in _LETTERS]
        # Blank out the key on a middle item only; position injection must
        # re-derive the correct letter for it without touching the others.
        options[3].pop("key")
        payload = dict(_base_decision_fields(), options=options)
        model = DirectionDecision(**payload)
        assert [o.key for o in model.options] == _LETTERS


# ──────────────────────────────────────────────────────────────────────────────
# Well-formed inputs and non-regression guarantees
# ──────────────────────────────────────────────────────────────────────────────
class TestWellFormedUntouched:
    def test_well_formed_list_passes_through_unchanged(self) -> None:
        options = [_option(letter) for letter in _LETTERS]
        payload = dict(_base_decision_fields(), options=options)
        model = DirectionDecision(**payload)
        assert [o.key for o in model.options] == _LETTERS
        assert [o.name for o in model.options] == [f"Direction {x}" for x in _LETTERS]

    def test_explicit_key_in_list_never_overwritten_by_position(self) -> None:
        # Items already carry the *correct* keys but in shuffled order; because
        # none is missing, the position-injection branch must not fire and must
        # not renumber them.
        shuffled = ["C", "A", "B", "D", "E", "F", "G"]
        payload = dict(
            _base_decision_fields(),
            options=[_option(letter) for letter in shuffled],
        )
        model = DirectionDecision(**payload)
        assert [o.key for o in model.options] == shuffled

    def test_missing_options_key_is_left_for_field_validation(self) -> None:
        payload = _base_decision_fields()  # no ``options`` at all
        with pytest.raises(Exception):
            DirectionDecision(**payload)

    def test_non_dict_options_items_still_rejected(self) -> None:
        payload = dict(
            _base_decision_fields(),
            options=["not-a-dict", 123, None],
        )
        with pytest.raises(Exception):
            DirectionDecision(**payload)


# ──────────────────────────────────────────────────────────────────────────────
# Full extraction path — the symptom the user reported
# ──────────────────────────────────────────────────────────────────────────────
class _FakeCrewResult:
    """Minimal stand-in for a crewAI result object carrying ``json_dict``."""

    def __init__(self, payload: dict) -> None:
        self.json_dict = payload
        self.raw = ""
        self.output = ""

    @property
    def tasks_output(self):  # noqa: D401 - simple stub
        return []


class TestExtractionPathNoLongerSpamsDebug:
    @pytest.mark.parametrize(
        "options",
        [
            {letter: _option(letter) for letter in _LETTERS},  # shape 1
            [_option_without_key(letter) for letter in _LETTERS],  # shape 2
        ],
    )
    def test_extract_succeeds_without_lenient_retry_debug_line(self, options) -> None:
        payload = dict(_base_decision_fields(), options=options)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            decision = extract_direction_decision(_FakeCrewResult(payload))
        stdout = buf.getvalue()
        assert decision is not None
        assert decision.selected_direction == "A"
        assert [o.key for o in decision.options] == _LETTERS
        assert "lenient retry" not in stdout, stdout


class TestNormalizeAcceptsCoercedShapes:
    @pytest.mark.parametrize(
        "options",
        [
            {letter: _option(letter) for letter in _LETTERS},
            [_option_without_key(letter) for letter in _LETTERS],
            [_option(letter) for letter in _LETTERS],
        ],
    )
    def test_normalize_returns_non_none_for_recoverable_shapes(self, options) -> None:
        payload = dict(_base_decision_fields(), options=options)
        normalized = _normalize_direction_decision(DirectionDecision(**payload))
        assert normalized is not None
        assert normalized.selected_direction == "A"
        assert [o.key for o in normalized.options] == _LETTERS
