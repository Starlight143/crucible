"""
crucible/output_validation.py
======================================
Structured validation for LLM / crew output.

Inspired by Claude Code's typed response parsing (parse.ts + addRequestID):
instead of accepting crew.kickoff() results as opaque strings, this module
provides a lightweight schema registry and typed ValidationResult so that
downstream stages receive verified, well-shaped data.

Design
------
* No mandatory Pydantic dependency.  Pydantic is used when available
  (for richer schema support), otherwise falls back to dict-based validation.
* ``OutputSchema`` — declarative field definitions with type coercion.
* ``validate_output()`` — parse + validate raw crew output; returns
  ``ValidationResult`` (never raises on validation failure — errors are
  reported in the result so the caller decides whether to abort or recover).
* ``register_schema()`` / ``get_schema()`` — global schema registry.
* JSON extraction: if raw output is a string that contains embedded JSON,
  extraction is attempted automatically.

Usage::

    from crucible.output_validation import (
        OutputSchema, FieldSpec, register_schema, validate_output
    )

    register_schema("direction_debate", OutputSchema(fields=[
        FieldSpec("direction",  str,  required=True),
        FieldSpec("confidence", float, required=False, default=0.0),
        FieldSpec("reasoning",  str,  required=False, default=""),
    ]))

    result = validate_output(crew_output, schema_name="direction_debate")
    if not result.valid:
        logger.warning("Output validation failed: %s", result.errors)
    else:
        direction = result.data["direction"]
"""
from __future__ import annotations

import json
import re
import threading
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple, Type

if __package__ == "crucible":
    from .runtime_logging import get_logger, log_event
else:  # pragma: no cover
    from runtime_logging import get_logger, log_event  # type: ignore[no-redef]

LOGGER = get_logger(__name__)

# ── Field specification ────────────────────────────────────────────────────────

@dataclass
class FieldSpec:
    """
    Specification for a single field in an output schema.

    Parameters
    ----------
    name:        Field name (key in output dict).
    type_:       Expected Python type.  Value is coerced if possible.
    required:    If True and field is missing, validation fails.
    default:     Value used when field is absent and not required.
    validator:   Optional extra predicate; raises ValueError with a message
                 if the field value is invalid.
    """
    name: str
    type_: Type[Any]
    required: bool = True
    default: Any = None
    validator: Optional[Callable[[Any], None]] = None


# ── Schema ────────────────────────────────────────────────────────────────────

@dataclass
class OutputSchema:
    """
    Declarative schema for validating LLM / crew output dicts.

    Parameters
    ----------
    fields:         List of ``FieldSpec`` objects.
    allow_extra:    If False (default), unknown keys are dropped.
                    If True, extra keys are passed through unchanged.
    description:    Human-readable schema description (for error messages).
    """
    fields: List[FieldSpec] = field(default_factory=list)
    allow_extra: bool = False
    description: str = ""


# ── Validation result ─────────────────────────────────────────────────────────

@dataclass
class ValidationResult:
    """
    Result of a ``validate_output()`` call.

    Attributes
    ----------
    valid:       True if all required fields are present and correctly typed.
    data:        Validated (and possibly coerced) output dict.
    errors:      List of human-readable validation error messages.
    raw:         The raw input before validation (string or dict).
    schema_name: Name of the schema used ('' if schema-less).
    """
    valid: bool
    data: Dict[str, Any] = field(default_factory=dict)
    errors: List[str] = field(default_factory=list)
    raw: Any = None
    schema_name: str = ""

    def raise_on_failure(self) -> "ValidationResult":
        """Raise ``OutputValidationError`` if validation failed, else return self."""
        if not self.valid:
            raise OutputValidationError(self)
        return self


class OutputValidationError(ValueError):
    """Raised by ``ValidationResult.raise_on_failure()`` when validation fails."""

    def __init__(self, result: ValidationResult) -> None:
        self.result = result
        errors_text = "; ".join(result.errors) if result.errors else "validation failed"
        super().__init__(
            f"Output validation failed for schema '{result.schema_name}': {errors_text}"
        )


# ── JSON extraction ───────────────────────────────────────────────────────────

# Patterns for extracting JSON from markdown code blocks or raw text
_JSON_BLOCK_RE = re.compile(
    r"```(?:json)?\s*(\{[\s\S]*?\}|\[[\s\S]*?\])\s*```", re.IGNORECASE
)


def extract_json(raw: Any) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """
    Attempt to extract a JSON dict from *raw* (str, dict, or CrewOutput-like).

    Returns
    -------
    Tuple[Optional[dict], Optional[str]]
        (parsed_dict, error_message)
        On success: (dict, None).  On failure: (None, error_message).
    """
    # Already a dict
    if isinstance(raw, dict):
        return raw, None

    # Try .raw attribute (CrewAI CrewOutput)
    raw_str: Optional[str] = None
    if hasattr(raw, "raw"):
        raw_str = str(raw.raw)
    elif isinstance(raw, str):
        raw_str = raw
    else:
        raw_str = str(raw)

    if not raw_str or not raw_str.strip():
        return None, "Empty output"

    # 1. Try direct JSON parse
    stripped = raw_str.strip()
    if stripped.startswith("{") or stripped.startswith("["):
        try:
            parsed = json.loads(stripped)
            if isinstance(parsed, dict):
                return parsed, None
            return None, f"JSON root is {type(parsed).__name__}, expected dict"
        except json.JSONDecodeError:
            pass  # fall through to block extraction

    # 2. Try markdown JSON code block
    match = _JSON_BLOCK_RE.search(raw_str)
    if match:
        try:
            parsed = json.loads(match.group(1))
            if isinstance(parsed, dict):
                return parsed, None
        except json.JSONDecodeError:
            pass

    # 3. Single-pass forward scan that tracks both brace depth and string
    #    context.  Only a '{' encountered at depth 0 begins a new OUTERMOST
    #    candidate block — nested '{' characters (depth > 0) cannot start an
    #    independent JSON object.  Backward scanning is unreliable when string
    #    values contain '{' or '}' characters: those characters corrupt a naive
    #    depth counter (e.g. '{"key": "a}b"}' trips the backward scanner at the
    #    inner '}', causing a boundary mis-detection and a spurious
    #    json.JSONDecodeError).  A single forward pass with string-context
    #    tracking avoids this class of false-parse-failure entirely while still
    #    returning the LAST valid outermost JSON dict (matching the original
    #    intent of the backward scan).
    best_parsed: Optional[Dict[str, Any]] = None
    scan_depth = 0
    in_string = False
    escape_next = False
    outer_start = -1

    for i, ch in enumerate(raw_str):
        if escape_next:
            escape_next = False
            continue
        if in_string:
            if ch == "\\":
                escape_next = True
            elif ch == '"':
                in_string = False
        else:
            if ch == '"':
                in_string = True
            elif ch == "{":
                if scan_depth == 0:
                    outer_start = i   # start of a new outermost candidate
                scan_depth += 1
            elif ch == "}":
                # Clamp to zero: an extra '}' in malformed JSON must not push
                # scan_depth negative, which would cause the next '{' to set
                # outer_start at depth 0 (triggering a false capture start)
                # instead of depth 1, and then never find its matching '}'.
                if scan_depth > 0:
                    scan_depth -= 1
                if scan_depth == 0 and outer_start != -1:
                    try:
                        parsed = json.loads(raw_str[outer_start : i + 1])
                        if isinstance(parsed, dict):
                            best_parsed = parsed
                    except json.JSONDecodeError:
                        pass
                    outer_start = -1

    if best_parsed is not None:
        return best_parsed, None

    return None, f"No JSON dict found in output (len={len(raw_str)})"


# ── Core validator ────────────────────────────────────────────────────────────

def _coerce(value: Any, type_: Type[Any]) -> Tuple[Any, Optional[str]]:
    """
    Attempt to coerce *value* to *type_*.

    Returns (coerced_value, error_message_or_None).
    """
    if isinstance(value, type_):
        # Guard: bool is a subclass of int in Python, so isinstance(True, int) is True.
        # A bool value must not silently pass as a valid int without explicit coercion.
        if type_ is int and isinstance(value, bool):
            return int(value), None
        return value, None
    # Special cases
    if type_ is bool:
        if isinstance(value, str):
            low = value.strip().lower()
            if low in ("true", "yes", "1", "on"):
                return True, None
            if low in ("false", "no", "0", "off"):
                return False, None
            # Reject ambiguous strings explicitly: ``bool("random")`` is True
            # in Python (any non-empty string is truthy), so a fall-through to
            # ``bool(value)`` would silently coerce malformed input to True.
            return None, f"Cannot coerce {value!r} to bool"
        # Numerics and None are unambiguous; pass them through ``bool()``.
        try:
            return bool(value), None
        except Exception:
            return None, f"Cannot coerce {value!r} to bool"
    if type_ is float:
        # Guard: bool is NOT a subclass of float in Python, so isinstance(True, float) is
        # False — execution falls through to here and float(True) = 1.0 silently.
        # Apply the same explicit coercion as the int guard above for consistency.
        if isinstance(value, bool):
            return float(value), None
        try:
            return float(value), None
        except (ValueError, TypeError):
            return None, f"Cannot coerce {value!r} to float"
    if type_ is int:
        try:
            return int(value), None
        except (ValueError, TypeError):
            return None, f"Cannot coerce {value!r} to int"
    if type_ is str:
        return str(value), None
    if type_ is list:
        if isinstance(value, (list, tuple)):
            return list(value), None
        return None, f"Cannot coerce {type(value).__name__} to list"
    if type_ is dict:
        if isinstance(value, dict):
            return value, None
        return None, f"Cannot coerce {type(value).__name__} to dict"
    # Fallback: attempt construction
    try:
        return type_(value), None
    except Exception as exc:
        return None, f"Cannot coerce {value!r} to {type_.__name__}: {exc}"


def _validate_against_schema(
    data: Dict[str, Any],
    schema: OutputSchema,
    schema_name: str,
) -> ValidationResult:
    """Apply *schema* to *data* and return a ``ValidationResult``."""
    errors: List[str] = []
    validated: Dict[str, Any] = {}

    for spec in schema.fields:
        if spec.name not in data:
            if spec.required:
                errors.append(f"Missing required field: '{spec.name}'")
            else:
                validated[spec.name] = spec.default
            continue

        raw_val = data[spec.name]
        coerced, coerce_err = _coerce(raw_val, spec.type_)
        if coerce_err is not None:
            errors.append(f"Field '{spec.name}': {coerce_err}")
            continue

        if spec.validator is not None:
            try:
                spec.validator(coerced)
            except (ValueError, AssertionError) as exc:
                errors.append(f"Field '{spec.name}' failed validation: {exc}")
                continue

        validated[spec.name] = coerced

    if schema.allow_extra:
        known_names = {f.name for f in schema.fields}
        for k, v in data.items():
            if k not in known_names:
                validated[k] = v

    return ValidationResult(
        valid=len(errors) == 0,
        data=validated,
        errors=errors,
        schema_name=schema_name,
    )


def validate_output(
    raw: Any,
    *,
    schema_name: str = "",
    schema: Optional[OutputSchema] = None,
) -> ValidationResult:
    """
    Parse and validate *raw* crew output.

    Parameters
    ----------
    raw:
        Raw crew result: str, CrewOutput (with .raw attr), or dict.
    schema_name:
        Name of a registered schema to validate against.  Ignored if
        *schema* is provided directly.
    schema:
        Explicit schema.  If both *schema_name* and *schema* are provided,
        *schema* takes precedence.

    Returns
    -------
    ValidationResult
        Always returns (never raises on validation failure).
    """
    resolved_schema = schema or (_get_schema(schema_name) if schema_name else None)

    # Extract JSON dict from raw output
    parsed_dict, extract_err = extract_json(raw)

    if parsed_dict is None:
        # No JSON dict found
        if resolved_schema is None:
            # Schema-less: return the raw string as-is wrapped in dict
            raw_str = str(raw.raw if hasattr(raw, "raw") else raw)
            return ValidationResult(
                valid=True,
                data={"raw": raw_str},
                errors=[],
                raw=raw,
                schema_name=schema_name,
            )
        else:
            return ValidationResult(
                valid=False,
                data={},
                errors=[extract_err or "Could not extract JSON dict"],
                raw=raw,
                schema_name=schema_name,
            )

    if resolved_schema is None:
        # No schema: JSON extracted, return as-is
        return ValidationResult(
            valid=True,
            data=parsed_dict,
            errors=[],
            raw=raw,
            schema_name=schema_name,
        )

    result = _validate_against_schema(parsed_dict, resolved_schema, schema_name)
    result.raw = raw

    log_event(
        LOGGER,
        20 if result.valid else 30,
        "output_validated",
        f"Output validation for '{schema_name}': {'OK' if result.valid else 'FAILED'}",
        schema=schema_name,
        valid=result.valid,
        errors=result.errors or None,
    )
    return result


# ── Schema registry ───────────────────────────────────────────────────────────

_SCHEMA_REGISTRY: Dict[str, OutputSchema] = {}
_REGISTRY_LOCK = threading.Lock()


def register_schema(name: str, schema: OutputSchema) -> None:
    """Register *schema* under *name* in the global schema registry."""
    with _REGISTRY_LOCK:
        _SCHEMA_REGISTRY[name] = schema


def _get_schema(name: str) -> Optional[OutputSchema]:
    with _REGISTRY_LOCK:
        return _SCHEMA_REGISTRY.get(name)


def get_schema(name: str) -> Optional[OutputSchema]:
    """Return the schema registered under *name*, or None."""
    return _get_schema(name)


def list_schemas() -> List[str]:
    """Return sorted list of all registered schema names."""
    with _REGISTRY_LOCK:
        return sorted(_SCHEMA_REGISTRY.keys())


def clear_schemas() -> None:
    """Remove all registered schemas (mainly for tests)."""
    with _REGISTRY_LOCK:
        _SCHEMA_REGISTRY.clear()
