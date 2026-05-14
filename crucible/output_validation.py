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
import math
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

# Reasoning-model "thinking" wrappers.  Reasoning models (DeepSeek-V3/V4,
# GLM-5.1, Qwen-3.5, o1-class …) emit chain-of-thought inside these tags
# ahead of the answer.  When the reasoning text contains brace-shape tokens
# (example dicts, pretty-printed sub-results, regex literals …) the forward
# JSON scanner picks them up first and the actual answer is discarded.
_REASONING_TAG_RE = re.compile(
    r"<\s*(?:think|thinking|reasoning|reflection|scratchpad)\b[^>]*>"
    r"[\s\S]*?"
    r"<\s*/\s*(?:think|thinking|reasoning|reflection|scratchpad)\s*>",
    re.IGNORECASE,
)


def strip_reasoning_blocks(text: str) -> str:
    """Strip ``<think>…</think>`` (and aliases) from LLM output.

    Idempotent; returns input unchanged when no such tag is present.

    Reasoning models (DeepSeek-V3/V4, GLM-5.1, Qwen-3.5, o1-class …) emit
    chain-of-thought inside ``<think|thinking|reasoning|reflection|scratchpad>``
    tags ahead of the answer.  Any module that scans LLM output for JSON,
    fenced code blocks, or other structured artefacts must call this first;
    otherwise brace-/fence-shape tokens inside the reasoning block can be
    captured as the "real" output and the actual answer is discarded.
    """
    if not text or "<" not in text:
        return text
    return _REASONING_TAG_RE.sub("", text)


# Backwards-compatible alias (private name used internally before v1.0.4).
_strip_reasoning_blocks = strip_reasoning_blocks


def _coerce_to_str(raw: Any) -> Optional[str]:
    """Best-effort conversion of *raw* to a string for JSON extraction.

    Returns ``None`` when the source has no string-like content to scan
    (which the caller treats as "Empty output").
    """
    if hasattr(raw, "raw"):
        return str(raw.raw)
    if isinstance(raw, str):
        return raw
    return str(raw)


def _try_direct_json(
    stripped: str,
) -> Tuple[Optional[Dict[str, Any]], Optional[str], bool]:
    """Strategy 1 — entire string is a JSON document.

    Returns ``(dict, None, True)`` on a structurally valid dict, ``(None,
    type-mismatch-message, True)`` when the root is the wrong type
    (``list``/``str``/``int``…), or ``(None, None, False)`` to indicate the
    caller should fall through to the next strategy.
    """
    if not (stripped.startswith("{") or stripped.startswith("[")):
        return None, None, False
    try:
        parsed = json.loads(stripped)
    except json.JSONDecodeError:
        return None, None, False  # fall through to next strategy
    if isinstance(parsed, dict):
        return parsed, None, True
    return None, f"JSON root is {type(parsed).__name__}, expected dict", True


def _try_markdown_block(raw_str: str) -> Optional[Dict[str, Any]]:
    """Strategy 2 — extract a fenced ``json`` (or unlabelled ```` ``` ```` ) block."""
    match = _JSON_BLOCK_RE.search(raw_str)
    if match is None:
        return None
    try:
        parsed = json.loads(match.group(1))
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _scan_outermost_json(raw_str: str) -> Optional[Dict[str, Any]]:
    """Strategy 3 — single forward pass tracking brace depth and string context.

    Only a ``{`` encountered at depth 0 begins a new OUTERMOST candidate
    block — nested ``{`` characters (depth > 0) cannot start an independent
    JSON object.  Backward scanning is unreliable when string values contain
    ``{`` or ``}`` characters: those characters corrupt a naive depth counter
    (e.g. ``{"key": "a}b"}`` trips the backward scanner at the inner ``}``,
    causing a boundary mis-detection and a spurious ``json.JSONDecodeError``).
    A single forward pass with string-context tracking avoids this class of
    false-parse-failure entirely while still returning the **last** valid
    outermost JSON dict — preserving the original intent of the backward
    scan that this function replaced.
    """
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
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            if scan_depth == 0:
                outer_start = i  # start of a new outermost candidate
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

    return best_parsed


def extract_json(raw: Any) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """
    Attempt to extract a JSON dict from *raw* (str, dict, or CrewOutput-like).

    Returns
    -------
    Tuple[Optional[dict], Optional[str]]
        (parsed_dict, error_message)
        On success: (dict, None).  On failure: (None, error_message).

    Strategy order (delegates to private helpers above):

    1. ``raw`` is already a dict — return immediately.
    2. Direct ``json.loads`` of the whole string.
    3. ``json`` markdown code block.
    4. Forward-scan for the last outermost ``{...}`` substring that parses.
    """
    if isinstance(raw, dict):
        return raw, None

    raw_str = _coerce_to_str(raw)
    if not raw_str or not raw_str.strip():
        return None, "Empty output"

    raw_str = _strip_reasoning_blocks(raw_str)
    if not raw_str.strip():
        return None, "Empty output"

    parsed, type_error, hit = _try_direct_json(raw_str.strip())
    if hit:
        if parsed is not None:
            return parsed, None
        # Type-mismatch terminates extraction — emulating the original
        # behaviour where a non-dict root short-circuits before strategies
        # 2 and 3 are tried.
        return None, type_error

    md_parsed = _try_markdown_block(raw_str)
    if md_parsed is not None:
        return md_parsed, None

    scan_parsed = _scan_outermost_json(raw_str)
    if scan_parsed is not None:
        return scan_parsed, None

    return None, f"No JSON dict found in output (len={len(raw_str)})"


# ── Core validator ────────────────────────────────────────────────────────────

def _coerce(value: Any, type_: Type[Any]) -> Tuple[Any, Optional[str]]:
    """
    Attempt to coerce *value* to *type_*.

    Returns (coerced_value, error_message_or_None).
    """
    if isinstance(value, type_):
        # Guard: bool is a subclass of int in Python, so isinstance(True, int)
        # is True — the early-return would otherwise hand back a ``bool`` for
        # an int-typed field, leaking the bool-subtype semantics (``True is 1``
        # would still be False, ``json.dumps(...)`` would write ``true`` instead
        # of ``1``).  ``int(value)`` returns a plain ``int`` (``type(int(True))
        # is int``), so the field gets the explicit numeric form the schema
        # promises.  This is intentionally an accepting conversion, not a
        # rejection: callers wanting bool↦int rejection should declare the
        # field as ``int`` with a ``FieldSpec.validator`` that excludes bool.
        if type_ is int and isinstance(value, bool):
            return int(value), None
        # v1.1.2 (audit fix G3-A1-HIGH-2): when ``value`` is ALREADY a float,
        # the isinstance early-return would bypass the finite check below.
        # Apply the same NaN/Inf reject here so a Python-level ``float("nan")``
        # passed in directly (not as a string) is also rejected at the schema
        # boundary.
        if type_ is float and isinstance(value, float) and not isinstance(value, bool):
            if not math.isfinite(value):
                return None, (
                    f"Cannot coerce {value!r} to float: non-finite floats "
                    f"(NaN, +Inf, -Inf) are rejected by schema validation. "
                    "Override via FieldSpec.validator if the field is "
                    "expected to legitimately carry sentinel non-finite values."
                )
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
            coerced = float(value)
        except (ValueError, TypeError):
            return None, f"Cannot coerce {value!r} to float"
        # v1.1.2 (audit fix G3-A1-HIGH-2): reject NaN / ±Inf at the schema
        # boundary.  ``float("nan")`` / ``float("inf")`` are valid Python
        # values but they silently bypass every downstream gate
        # (``if confidence > 0.5`` is False for NaN, Infinity propagates
        # through cost/risk math producing invalid sort orders).  The
        # project-wide ``finite_only`` discipline in ``_env.env_float``
        # already enforces this on operator-controlled env vars; this
        # extension applies the same rule to LLM-controlled schema input,
        # closing the symmetric prompt-injection path where a model emits
        # ``{"confidence": "nan"}`` or ``{"score": "Infinity"}`` and a
        # downstream consumer reads a numerically-plausible but
        # statistically-meaningless value.
        if not math.isfinite(coerced):
            return None, (
                f"Cannot coerce {value!r} to float: non-finite floats "
                f"(NaN, +Inf, -Inf) are rejected by schema validation. "
                "Override via FieldSpec.validator if the field is "
                "expected to legitimately carry sentinel non-finite values."
            )
        return coerced, None
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
