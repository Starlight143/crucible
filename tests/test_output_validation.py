"""Tests for crucible/output_validation.py"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from crucible.output_validation import (
    FieldSpec,
    OutputSchema,
    OutputValidationError,
    ValidationResult,
    clear_schemas,
    extract_json,
    get_schema,
    list_schemas,
    register_schema,
    validate_output,
)


# ── extract_json ──────────────────────────────────────────────────────────────

class TestExtractJson:
    def test_plain_json_string(self) -> None:
        raw = '{"key": "value", "num": 42}'
        result, err = extract_json(raw)
        assert err is None
        assert result == {"key": "value", "num": 42}

    def test_dict_input_passthrough(self) -> None:
        d = {"a": 1, "b": 2}
        result, err = extract_json(d)
        assert err is None
        assert result == d

    def test_markdown_json_code_block(self) -> None:
        raw = 'Some text\n```json\n{"direction": "long", "confidence": 0.8}\n```\nMore text'
        result, err = extract_json(raw)
        assert err is None
        assert result["direction"] == "long"
        assert result["confidence"] == 0.8

    def test_markdown_code_block_no_lang(self) -> None:
        raw = '```\n{"key": "val"}\n```'
        result, err = extract_json(raw)
        assert err is None
        assert result == {"key": "val"}

    def test_embedded_json_in_text(self) -> None:
        raw = 'The analysis result is: {"status": "ok", "score": 95} end of result.'
        result, err = extract_json(raw)
        assert err is None
        assert result["status"] == "ok"

    def test_embedded_nested_json_returns_outer_object(self) -> None:
        # Regression: rfind("{") used to find the innermost "{", returning only
        # {"nested": "value"} instead of the full outer object.
        raw = 'Here is the result: {"data": {"nested": "value"}, "status": "ok"} done.'
        result, err = extract_json(raw)
        assert err is None
        assert result == {"data": {"nested": "value"}, "status": "ok"}, \
            f"Expected outer object, got: {result}"

    def test_multiple_json_objects_returns_last(self) -> None:
        # When text contains multiple JSON objects, stage-3 returns the last one.
        raw = 'First: {"a": 1} then Second: {"b": 2}'
        result, err = extract_json(raw)
        assert err is None
        assert result == {"b": 2}

    def test_empty_string_returns_error(self) -> None:
        result, err = extract_json("")
        assert result is None
        assert err is not None

    def test_no_json_returns_error(self) -> None:
        result, err = extract_json("plain text without JSON")
        assert result is None
        assert err is not None

    def test_object_with_raw_attr(self) -> None:
        class FakeCrewOutput:
            raw = '{"direction": "short"}'

        result, err = extract_json(FakeCrewOutput())
        assert err is None
        assert result == {"direction": "short"}

    def test_list_json_not_accepted_as_dict(self) -> None:
        raw = '[1, 2, 3]'
        result, err = extract_json(raw)
        assert result is None
        assert err is not None

    def test_whitespace_only_returns_error(self) -> None:
        result, err = extract_json("   ")
        assert result is None
        assert err is not None

    def test_brace_in_string_value_parsed_correctly(self) -> None:
        """
        Regression: the backward brace-scan in stage-3 did not track
        string context.  A '}' or '{' inside a JSON string value corrupted the
        depth counter, causing the scanner to mis-detect the JSON boundary and
        return None instead of the correct dict.

        Input: a response where the 'summary' field value contains a '}'.
        The correct result is the full outer object.
        """
        raw = 'Review complete: {"verdict": "pass", "summary": "score={95}"} end.'
        result, err = extract_json(raw)
        assert err is None, f"Unexpected error: {err}"
        assert result is not None
        assert result["verdict"] == "pass"
        assert result["summary"] == "score={95}"

    def test_opening_brace_in_string_value_parsed_correctly(self) -> None:
        """String values containing '{' must not inflate the depth counter."""
        raw = 'Here: {"key": "open={value", "score": 1} done.'
        result, err = extract_json(raw)
        assert err is None, f"Unexpected error: {err}"
        assert result is not None
        assert result["key"] == "open={value"
        assert result["score"] == 1

    def test_think_tag_with_decoy_json_in_reasoning(self) -> None:
        """Reasoning models (DeepSeek-V3/V4, GLM-5.1, Qwen-3.5, o1-class)
        emit chain-of-thought inside ``<think>...</think>`` ahead of the real
        answer.  When the reasoning text contains an example dict, the forward
        JSON scan would otherwise capture it as the "first" JSON object and
        the actual answer would be lost.  Strip reasoning blocks first."""
        raw = (
            "<think>Let me consider the options. A tentative shape might be "
            '{"option": "A", "draft": true} but I need to verify.</think>\n'
            '{"selected_direction": "B", "confidence": "high"}'
        )
        result, err = extract_json(raw)
        assert err is None, f"Unexpected error: {err}"
        assert result == {"selected_direction": "B", "confidence": "high"}

    def test_thinking_tag_alias_stripped(self) -> None:
        """The <thinking> alias used by some Anthropic-style prompts."""
        raw = (
            "<thinking>{\"hypothesis\": \"X\"}</thinking>\n"
            '{"answer": 42}'
        )
        result, err = extract_json(raw)
        assert err is None
        assert result == {"answer": 42}

    def test_reasoning_tag_alias_stripped(self) -> None:
        raw = (
            "<reasoning>step 1: {\"foo\": 1}\nstep 2: combine</reasoning>"
            '{"final": "ok"}'
        )
        result, err = extract_json(raw)
        assert err is None
        assert result == {"final": "ok"}

    def test_think_tag_with_attributes_stripped(self) -> None:
        """Some providers emit ``<think type="cot">…</think>``."""
        raw = (
            '<think type="cot">{"draft": "ignore me"}</think>'
            '{"selected_direction": "C"}'
        )
        result, err = extract_json(raw)
        assert err is None
        assert result == {"selected_direction": "C"}

    def test_only_think_block_no_json_returns_error(self) -> None:
        raw = "<think>I am still pondering.</think>"
        result, err = extract_json(raw)
        assert result is None
        assert err is not None

    def test_no_think_tag_unchanged(self) -> None:
        """Idempotent on inputs that do not contain reasoning tags."""
        raw = '{"selected_direction": "A"}'
        result, err = extract_json(raw)
        assert err is None
        assert result == {"selected_direction": "A"}


# ── validate_output — schema-less ────────────────────────────────────────────

class TestValidateOutputSchemaless:
    def test_valid_json_string_no_schema(self) -> None:
        result = validate_output('{"a": 1}')
        assert result.valid is True
        assert result.data == {"a": 1}

    def test_dict_input_no_schema(self) -> None:
        result = validate_output({"x": 42})
        assert result.valid is True
        assert result.data == {"x": 42}

    def test_non_json_string_no_schema_returns_raw(self) -> None:
        result = validate_output("plain string output")
        assert result.valid is True
        assert "raw" in result.data
        assert result.data["raw"] == "plain string output"

    def test_raw_stored_in_result(self) -> None:
        raw = '{"z": 99}'
        result = validate_output(raw)
        assert result.raw == raw


# ── validate_output — with schema ─────────────────────────────────────────────

class TestValidateOutputWithSchema:
    def setup_method(self) -> None:
        clear_schemas()

    def _make_schema(self) -> OutputSchema:
        return OutputSchema(fields=[
            FieldSpec("direction", str, required=True),
            FieldSpec("confidence", float, required=False, default=0.0),
            FieldSpec("reasoning", str, required=False, default=""),
        ])

    def test_all_required_fields_present(self) -> None:
        schema = self._make_schema()
        result = validate_output('{"direction": "long", "confidence": 0.9, "reasoning": "trend"}',
                                  schema=schema)
        assert result.valid is True
        assert result.data["direction"] == "long"
        assert result.data["confidence"] == 0.9

    def test_missing_required_field(self) -> None:
        schema = self._make_schema()
        result = validate_output('{"confidence": 0.5}', schema=schema)
        assert result.valid is False
        assert any("direction" in e for e in result.errors)

    def test_optional_field_uses_default(self) -> None:
        schema = self._make_schema()
        result = validate_output('{"direction": "short"}', schema=schema)
        assert result.valid is True
        assert result.data["confidence"] == 0.0
        assert result.data["reasoning"] == ""

    def test_type_coercion_str_to_float(self) -> None:
        schema = self._make_schema()
        result = validate_output('{"direction": "long", "confidence": "0.75"}', schema=schema)
        assert result.valid is True
        assert result.data["confidence"] == 0.75

    def test_type_coercion_str_to_int(self) -> None:
        schema = OutputSchema(fields=[
            FieldSpec("count", int, required=True),
        ])
        result = validate_output('{"count": "42"}', schema=schema)
        assert result.valid is True
        assert result.data["count"] == 42

    def test_type_coercion_str_to_bool_true(self) -> None:
        schema = OutputSchema(fields=[
            FieldSpec("flag", bool, required=True),
        ])
        for truthy in ("true", "yes", "1"):
            result = validate_output(f'{{"flag": "{truthy}"}}', schema=schema)
            assert result.valid is True, f"Expected True for {truthy!r}"
            assert result.data["flag"] is True

    def test_type_coercion_str_to_bool_false(self) -> None:
        schema = OutputSchema(fields=[
            FieldSpec("flag", bool, required=True),
        ])
        for falsy in ("false", "no", "0"):
            result = validate_output(f'{{"flag": "{falsy}"}}', schema=schema)
            assert result.valid is True, f"Expected False for {falsy!r}"
            assert result.data["flag"] is False

    def test_custom_validator_called(self) -> None:
        def positive(v: float) -> None:
            if v <= 0:
                raise ValueError("Must be positive")

        schema = OutputSchema(fields=[
            FieldSpec("score", float, required=True, validator=positive),
        ])
        result_ok = validate_output('{"score": 1.0}', schema=schema)
        assert result_ok.valid is True

        result_fail = validate_output('{"score": -1.0}', schema=schema)
        assert result_fail.valid is False
        assert any("score" in e for e in result_fail.errors)

    def test_extra_fields_dropped_by_default(self) -> None:
        schema = OutputSchema(fields=[
            FieldSpec("direction", str, required=True),
        ])
        result = validate_output('{"direction": "long", "extra": "ignored"}', schema=schema)
        assert result.valid is True
        assert "extra" not in result.data

    def test_extra_fields_kept_when_allow_extra(self) -> None:
        schema = OutputSchema(
            fields=[FieldSpec("direction", str, required=True)],
            allow_extra=True,
        )
        result = validate_output('{"direction": "long", "extra": "kept"}', schema=schema)
        assert result.valid is True
        assert result.data.get("extra") == "kept"

    def test_non_json_output_with_schema_fails(self) -> None:
        schema = self._make_schema()
        result = validate_output("plain text without JSON", schema=schema)
        assert result.valid is False
        assert len(result.errors) > 0


# ── Registry ──────────────────────────────────────────────────────────────────

class TestSchemaRegistry:
    def setup_method(self) -> None:
        clear_schemas()

    def test_register_and_get_schema(self) -> None:
        schema = OutputSchema(fields=[FieldSpec("x", str)])
        register_schema("my_schema", schema)
        retrieved = get_schema("my_schema")
        assert retrieved is schema

    def test_get_schema_unknown_returns_none(self) -> None:
        assert get_schema("nonexistent") is None

    def test_list_schemas(self) -> None:
        register_schema("beta", OutputSchema())
        register_schema("alpha", OutputSchema())
        names = list_schemas()
        assert names == ["alpha", "beta"]

    def test_clear_schemas(self) -> None:
        register_schema("temp", OutputSchema())
        clear_schemas()
        assert list_schemas() == []

    def test_validate_output_uses_registered_schema(self) -> None:
        schema = OutputSchema(fields=[FieldSpec("direction", str, required=True)])
        register_schema("test_direction", schema)
        result = validate_output('{"direction": "long"}', schema_name="test_direction")
        assert result.valid is True
        assert result.schema_name == "test_direction"

    def test_validate_output_unknown_schema_name(self) -> None:
        # schema_name given but not registered → no schema applied → valid JSON returned as-is
        result = validate_output('{"a": 1}', schema_name="unknown_schema")
        assert result.valid is True
        assert result.data == {"a": 1}


# ── ValidationResult.raise_on_failure ────────────────────────────────────────

class TestRaiseOnFailure:
    def test_raises_output_validation_error_on_failure(self) -> None:
        result = ValidationResult(valid=False, errors=["missing field x"], schema_name="s")
        with pytest.raises(OutputValidationError) as exc_info:
            result.raise_on_failure()
        assert "missing field x" in str(exc_info.value)
        assert exc_info.value.result is result

    def test_returns_self_on_success(self) -> None:
        result = ValidationResult(valid=True, data={"x": 1})
        returned = result.raise_on_failure()
        assert returned is result

    def test_output_validation_error_is_value_error(self) -> None:
        result = ValidationResult(valid=False, errors=["e"])
        with pytest.raises(ValueError):
            result.raise_on_failure()
