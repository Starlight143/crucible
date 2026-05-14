"""
features/run_insights/schema.py
================================
Event schema, content-addressable ID computation, and signals extraction for
the Run Insights ledger.

Design alignment with evomap.ai's GEP MemoryGraphEvent / EvolutionEvent
----------------------------------------------------------------------
This v1.1.0 release ships **recording only** (no retrieval, no injection).
Every field below is shaped so that v1.2.0 retrieval/skills can index and
query without backfill, and so the same record can be ingested by a future
Cloudflare D1 row + R2 blob without transformation.

Key alignments:

* ``content_id`` — ``"sha256:" + sha256(canonical_json(event - content_id))``.
  Identical algorithm in Python and (future) JavaScript Worker → identical
  IDs locally and remotely → natural ``INSERT OR IGNORE`` deduplication.

* ``signals[]`` — Tag-style strings (``"mode:quant"``, ``"asset:gold"``,
  ``"venue:ftmo"``).  v1.2.0 uses Jaccard similarity over this set to find
  similar past failures/successes.  Without it, v1.2.0 has no index key.

* ``outcome`` — Always ``{status, score, note}``.  ``status`` ∈
  ``{"success","failure","partial","skipped"}``; ``score`` ∈ ``[0,1]`` or
  ``None``.  v1.2.0 uses Laplace-smoothed ``p = (succ+1)/(total+2)`` over
  this field to compute confidence per signal pattern.

* ``env_fingerprint`` — ``{python_version, platform, arch, model_id,
  llm_provider}``.  evomap stores ``{node_version, platform, arch}``; we
  add ``model_id`` + ``llm_provider`` because LLM choice is the single
  largest source of run-to-run variance in this codebase.

* ``reusability`` (success events only) — ``{trigger_signals,
  applicable_modes, confidence, skill_kind}``.  v1.2.0's LLM-based skill
  distiller reads ``trigger_signals`` to group successful capsules.

* ``schema_version`` is per-event (not per-file).  Future field additions
  bump this; readers always use ``.get(field, default)`` for tolerance.

Canonical JSON algorithm
------------------------
1. Remove the ``content_id`` field if present.
2. Recursively normalise floats: ``NaN`` / ``±Inf`` → ``None``.
3. Serialise with ``sort_keys=True``, ``separators=(",", ":")``,
   ``ensure_ascii=False``, ``allow_nan=False``.

This must match the JS/Worker side byte-for-byte.  See
``backends.py`` module docstring for the JavaScript equivalent.

Asset-category classifier (Quant mode only, v1.1.0)
---------------------------------------------------
Rule-based dictionary lookup against the user problem text and run meta.
Output is one tag in the form ``"asset:<category>"`` appended to
``signals[]``.  v1.2.0's retriever uses this for the
"same-asset-class only" scope predicate on Quant runs.

Recognised categories: ``gold``, ``silver``, ``oil``, ``forex``,
``crypto``, ``equity``, ``futures``, ``options``, ``bonds``,
``uncategorized`` (fallback).

The classifier is deliberately rule-based (no LLM call) so it is
deterministic and zero-cost.  Operators wanting LLM-augmented
classification can override the function after import.
"""
from __future__ import annotations

import hashlib
import json
import math
import platform
import re
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Mapping, Optional

# ── Constants ────────────────────────────────────────────────────────────────

SCHEMA_VERSION: int = 1


class EventKind(str, Enum):
    """Allowed values for the ``kind`` field on every ledger event."""

    OUTPUT_METHOD = "output_method"
    ERROR_RECORD = "error_record"
    DIRECTION_DEBATE_REJECTION = "direction_debate_rejection"
    RUNTIME_PARAMS = "runtime_params"


class OutcomeStatus(str, Enum):
    """Allowed values for ``outcome.status``."""

    SUCCESS = "success"
    FAILURE = "failure"
    PARTIAL = "partial"
    SKIPPED = "skipped"


# ── Asset category classifier (Quant mode) ────────────────────────────────────

# Keys ordered by specificity: more specific patterns come first so that
# ``btcusdt`` matches ``crypto`` before ``usdt`` (a forex token).
_ASSET_PATTERNS: List[tuple[str, re.Pattern[str]]] = [
    ("gold",     re.compile(r"\b(gold|xau(usd)?|黃金|黄金)\b", re.IGNORECASE)),
    ("silver",   re.compile(r"\b(silver|xag(usd)?|白銀|白银)\b", re.IGNORECASE)),
    ("oil",      re.compile(r"\b(crude.?oil|brent|wti|原油)\b", re.IGNORECASE)),
    ("crypto",   re.compile(
        r"(?<![A-Za-z0-9])"
        r"(?:btc|eth|sol|xrp|ada|doge|crypto|perp(?:etual)?|binance|"
        r"bybit|okx|加密|币|幣|usdt|busd)"
        r"(?![A-Za-z0-9])",
        re.IGNORECASE,
    )),
    ("forex",    re.compile(
        r"\b(forex|fx|外匯|外汇|"
        r"eur(usd|jpy|gbp)?|usd(jpy|chf|cad)?|gbp(usd|jpy)?|aud(usd)?|"
        r"nzd(usd)?)\b",
        re.IGNORECASE,
    )),
    ("futures",  re.compile(
        r"\b(es|nq|ym|rty|cl|gc|si|期貨|期货|futures|cme)\b",
        re.IGNORECASE,
    )),
    ("options",  re.compile(
        r"\b(option(s)?|call|put|選擇權|选择权|iv|implied.?vol)\b",
        re.IGNORECASE,
    )),
    ("equity",   re.compile(
        r"(?:\bs[&\s]?p[\s-]?500\b|\bspx\b|\bnasdaq\b|\bndx\b|\bdow\b|\bdjia\b|"
        r"\b股票\b|\b股市\b|\bequit(?:y|ies)\b|"
        r"\b\d{4}\.tw\b|\btsla\b|\baapl\b|\bnvda\b|\bmsft\b|\bamzn\b|\bgoogl?\b)",
        re.IGNORECASE,
    )),
    ("bonds",    re.compile(r"\b(bond|treasury|yield|tlt|us10y)\b", re.IGNORECASE)),
]


def classify_asset_category(
    user_problem: Optional[str], run_meta: Optional[Mapping[str, Any]] = None
) -> str:
    """
    Return one of ``gold|silver|oil|crypto|forex|futures|options|equity|
    bonds|uncategorized`` based on rule-based pattern matching against the
    user problem text and (optionally) run_meta fields.

    Deterministic: same input → same output.  No LLM calls.

    The first matching category wins (patterns ordered by specificity).
    """
    haystacks: List[str] = []
    if user_problem:
        haystacks.append(str(user_problem))
    if isinstance(run_meta, Mapping):
        for key in ("project_name", "entrypoint_override", "input_mode"):
            val = run_meta.get(key)
            if val:
                haystacks.append(str(val))
    combined = " ".join(haystacks)
    if not combined.strip():
        return "uncategorized"
    for category, pattern in _ASSET_PATTERNS:
        if pattern.search(combined):
            return category
    return "uncategorized"


# ── Signal extraction ────────────────────────────────────────────────────────

# Per-mode signal vocabulary.  Keep these strictly lowercase, colon-separated
# (``prefix:value``) so Jaccard / set operations remain trivial in v1.2.0.

def _venue_signals(text: str) -> List[str]:
    out: List[str] = []
    t = text.lower()
    if "ftmo" in t:                 out.append("venue:ftmo")
    if "the5ers" in t or "the 5%" in t: out.append("venue:the5ers")
    if "binance" in t:              out.append("venue:binance")
    if "bybit" in t:                out.append("venue:bybit")
    if "okx" in t or "okex" in t:   out.append("venue:okx")
    if "coinbase" in t:             out.append("venue:coinbase")
    if "ib " in t or "interactive brokers" in t: out.append("venue:ib")
    return out


def _framework_signals(text: str) -> List[str]:
    out: List[str] = []
    t = text.lower()
    if "ctrader" in t or "c-trader" in t: out.append("framework:ctrader")
    if "metatrader" in t or "mt4" in t or "mt5" in t: out.append("framework:metatrader")
    if "tradingview" in t or "pine script" in t: out.append("framework:tradingview")
    if "backtrader" in t:           out.append("framework:backtrader")
    if "vectorbt" in t:             out.append("framework:vectorbt")
    if "ccxt" in t:                 out.append("framework:ccxt")
    if "fastapi" in t:              out.append("framework:fastapi")
    if "flask" in t:                out.append("framework:flask")
    if "django" in t:               out.append("framework:django")
    if "next.js" in t or "nextjs" in t: out.append("framework:nextjs")
    return out


def _instrument_signals(text: str) -> List[str]:
    out: List[str] = []
    t = text.lower()
    if "perpetual" in t or "perp" in t: out.append("instrument:perpetual")
    elif "spot" in t:                    out.append("instrument:spot")
    elif "futures" in t or "期貨" in text or "期货" in text:
        out.append("instrument:futures")
    return out


def extract_signals(
    *,
    mode: str,
    user_problem: Optional[str],
    run_meta: Optional[Mapping[str, Any]] = None,
    extra: Optional[List[str]] = None,
) -> List[str]:
    """
    Extract a deterministic list of tag-style signals for indexing.

    The returned list is:

    * lowercased (signals are case-insensitive identifiers)
    * deduplicated (order preserved from first occurrence)
    * always contains ``mode:<lowercased mode name>`` and the LLM provider
    * for Quant mode additionally contains ``asset:<category>`` and any
      venue / framework / instrument tags detected in the problem text

    The signal vocabulary is documented in this module's docstring.  v1.2.0
    indexes events on these tags; do not change the prefixes without bumping
    ``SCHEMA_VERSION`` and providing a reader-side migration.
    """
    mode_str = str(mode or "").strip().lower() or "unknown"
    signals: List[str] = [f"mode:{mode_str}"]

    provider = ""
    if isinstance(run_meta, Mapping):
        provider = str(run_meta.get("llm_provider") or "").strip().lower()
    if provider:
        signals.append(f"provider:{provider}")

    text = str(user_problem or "")

    # Asset category is Quant-only; SaaS/Agent/Scientist don't trade assets.
    if mode_str == "quant":
        signals.append(f"asset:{classify_asset_category(text, run_meta)}")

    # Venue / framework / instrument are mode-agnostic — useful as context
    # for any mode that mentions them in the problem statement.
    signals.extend(_venue_signals(text))
    signals.extend(_framework_signals(text))
    if mode_str == "quant":
        signals.extend(_instrument_signals(text))

    if extra:
        for tag in extra:
            tag_s = str(tag or "").strip().lower()
            if tag_s and ":" in tag_s:
                signals.append(tag_s)

    # Dedupe preserving first-seen order.
    seen: set[str] = set()
    out: List[str] = []
    for s in signals:
        if s not in seen:
            seen.add(s)
            out.append(s)
    return out


# ── Canonical JSON + content_id ───────────────────────────────────────────────

def _normalise_for_canonical(value: Any, _visited: Optional[set] = None) -> Any:
    """Recursively normalise an object tree for canonical serialisation.

    * ``NaN`` / ``±Inf`` floats → ``None`` (so ``allow_nan=False`` doesn't
      raise downstream; matches evomap's "non-finite numbers to null" rule).
    * Tuples → lists (JSON has no tuple type; preserves order, evomap-style).
    * Dicts are returned with original keys (sorting handled by
      ``json.dumps(sort_keys=True)`` later — recursive normalisation here
      just touches values).

    v1.1.0 fifth-pass (G-8): detect reference cycles via an ``id()``
    visited set and substitute ``"<cycle>"`` for the back-edge.
    Previously a self-referential dict triggered ``RecursionError``;
    the outer ``_emit`` swallowed it under ``except Exception``, so
    the event silently disappeared with no diagnostic.
    """
    if isinstance(value, float):
        return None if not math.isfinite(value) else value
    # Only container types can form cycles — bool/int/str/None never do.
    if isinstance(value, (dict, list, tuple)):
        if _visited is None:
            _visited = set()
        marker = id(value)
        if marker in _visited:
            return "<cycle>"
        _visited = _visited | {marker}
        if isinstance(value, dict):
            return {k: _normalise_for_canonical(v, _visited) for k, v in value.items()}
        return [_normalise_for_canonical(v, _visited) for v in value]
    return value


# v1.1.0 third-pass: cache the private ``_make_iterencode`` reference
# at module import so the V8FloatJSONEncoder can detect (and gracefully
# fall back) when CPython renames the internal helper.  Without this
# the encoder would crash on import in a future Python release whose
# ``json.encoder`` no longer exposes the underscore-prefixed name.
try:
    from json.encoder import (
        _make_iterencode as _JSON_MAKE_ITERENCODE,  # type: ignore[attr-defined]
        encode_basestring_ascii as _JSON_ENCODE_BS_ASCII,
        encode_basestring as _JSON_ENCODE_BS,
    )
    _V8_ENCODER_AVAILABLE = True
except ImportError:  # pragma: no cover — future Python rename
    _JSON_MAKE_ITERENCODE = None  # type: ignore[assignment]
    _JSON_ENCODE_BS_ASCII = None  # type: ignore[assignment]
    _JSON_ENCODE_BS = None  # type: ignore[assignment]
    _V8_ENCODER_AVAILABLE = False


def _v8_float_repr(value: float) -> str:
    """Format *value* using V8 ``Number.prototype.toString`` rules.

    v1.1.0 (third-pass): re-implemented from scratch following ECMA-262
    §6.1.6.1.13 ``Number::toString`` so the local backend and a future
    Cloudflare Worker produce byte-identical JSON for the same payload.
    The previous heuristic (``repr`` + exponent-stripping) diverged at
    several boundaries that matter for the ledger:

    * V8 emits ``"100"`` for ``Number(100.0)``; Python ``repr`` gave
      ``"100.0"`` — content_ids for any record containing an
      integer-valued float disagreed.
    * V8 emits ``"0.000001"`` for ``Number(1e-6)``; Python ``repr``
      gave ``"1e-06"`` (a different sequence even after exponent-zero
      stripping) — every record using ≤ 1e-6 floats diverged.
    * V8 emits decimal notation for |x| < 1e21; Python switches to
      exponential at 1e16 (``"1e+16"``).  Records in the 1e16–1e21 band
      diverged.

    All three cases now match exactly.  Algorithm: extract the shortest
    significant-digit string ``s`` (length ``k``) and the V8 ``n`` such
    that ``value = s × 10^(n-k)``.  Apply ECMA-262's branching:

    * ``k ≤ n ≤ 21`` → fixed integer ``digits + "0"*(n-k)``
    * ``0 < n ≤ 21`` → split mantissa with decimal point
    * ``-6 < n ≤ 0`` → ``"0." + "0"*(-n) + digits``
    * else → scientific ``digits[0] + "." + digits[1:] + "e" + sign + |n-1|``
    """
    # Non-finite should have been substituted by _normalise_for_canonical
    # upstream; reaching here means a caller bypassed normalisation.
    # v1.1.0 fourth-pass: raise instead of returning "null" so the
    # downstream encoder fails loudly rather than silently producing
    # JSON whose content_id happens to collide with a payload that
    # legitimately stored ``None``.  ``_normalise_for_canonical`` is
    # the contract entry point; if you skip it, you broke the contract.
    if value != value or value in (float("inf"), float("-inf")):
        raise ValueError(
            "_v8_float_repr received non-finite float; call "
            "_normalise_for_canonical first"
        )
    if value == 0.0:
        # +0.0 and -0.0 both serialise to bare "0" (V8 strips the sign).
        return "0"

    is_neg = value < 0.0
    abs_val = -value if is_neg else value
    sign = "-" if is_neg else ""

    # Python ``repr`` produces the shortest decimal that round-trips back
    # to the same float — same property V8 uses for its internal digit
    # extraction.  We just need to re-format the chosen digits to match
    # V8's branching rules.
    py = repr(abs_val)
    if "e" in py:
        mantissa_str, _, exp_str = py.partition("e")
        binary_exp = int(exp_str)
    else:
        mantissa_str = py
        binary_exp = 0
    if "." in mantissa_str:
        int_part, _, frac_part = mantissa_str.partition(".")
    else:
        int_part = mantissa_str
        frac_part = ""

    raw = int_part + frac_part
    # Locate the span of significant digits (no leading/trailing zeros).
    first_nz = 0
    while first_nz < len(raw) and raw[first_nz] == "0":
        first_nz += 1
    if first_nz == len(raw):  # All zeros — caught above by value==0.0 but
        return "0"            # keep the path defensive.
    last_nz = len(raw) - 1
    while last_nz >= 0 and raw[last_nz] == "0":
        last_nz -= 1
    digits = raw[first_nz:last_nz + 1]

    # ``n`` is the V8 exponent of the leading significant digit:
    # value ≈ 0.digits × 10^n  ⟺  the decimal point sits ``n`` digits
    # right of the first significant digit.  In our concatenated ``raw``
    # the implicit decimal lives at position ``len(int_part)``, so n is
    # that offset relative to first_nz plus any explicit ``e``-exponent.
    n = len(int_part) - first_nz + binary_exp
    k = len(digits)

    if k <= n <= 21:
        return sign + digits + ("0" * (n - k))
    if 0 < n <= 21:
        return sign + digits[:n] + "." + digits[n:]
    if -6 < n <= 0:
        return sign + "0." + ("0" * -n) + digits
    # Scientific notation
    exp_part = n - 1
    mantissa_v8 = digits[0] + "." + digits[1:] if k > 1 else digits
    if exp_part >= 0:
        return sign + mantissa_v8 + "e+" + str(exp_part)
    return sign + mantissa_v8 + "e" + str(exp_part)  # exp_part already has '-'


class _V8FloatJSONEncoder(json.JSONEncoder):
    """JSON encoder that emits floats using V8 ``Number.toString`` rules.

    Used by :func:`canonical_json` so the Python local backend and the
    Cloudflare Worker compute byte-identical canonical bytes for the
    same event, preserving content-id parity for cloud-side dedup.

    Fallback: if the CPython internal ``json.encoder._make_iterencode``
    is unavailable (renamed in a future Python release), the encoder
    falls back to the default formatter — content_ids will then differ
    from V8 for integer-valued floats but the writer does not crash.
    The fallback is logged via the warning in :func:`canonical_json`.
    """

    def iterencode(self, o, _one_shot=False):  # type: ignore[override]
        # v1.1.0 third-pass: cached private-API reference at import
        # time.  Falls back to the default JSONEncoder iterencode if
        # the CPython internals were renamed.
        if not _V8_ENCODER_AVAILABLE:
            return super().iterencode(o, _one_shot)
        markers = {} if self.check_circular else None
        _encoder = (
            _JSON_ENCODE_BS_ASCII if self.ensure_ascii else _JSON_ENCODE_BS
        )

        def floatstr(o, _inf=float("inf"), _neginf=float("-inf")):
            return _v8_float_repr(o)

        _iterencode = _JSON_MAKE_ITERENCODE(
            markers, self.default, _encoder, self.indent, floatstr,
            self.key_separator, self.item_separator, self.sort_keys,
            self.skipkeys, _one_shot,
        )
        return _iterencode(o, 0)


def canonical_json(event: Mapping[str, Any]) -> bytes:
    """Return the canonical JSON serialisation of *event* (excluding
    ``content_id``) as UTF-8 bytes.

    Algorithm (must match the JavaScript/Worker side byte-for-byte):

    1. Drop the ``content_id`` key if present.
    2. Recursively replace non-finite floats with ``None``.
    3. ``json.dumps(obj, sort_keys=True, ensure_ascii=False,
       separators=(",", ":"))`` with a custom float formatter that
       matches V8's ``Number.prototype.toString`` (no trailing ``.0``
       on integer-valued floats, no leading zero in exponents).
    4. ``.encode("utf-8")``.

    v1.1.0: the float formatter was added explicitly because Python's
    default ``json.dumps`` produces ``1.0`` / ``1e-07`` while V8 emits
    ``1`` / ``1e-7``.  Without this alignment, content_ids computed
    locally and by the future Cloudflare Worker would diverge for any
    payload containing floats.
    """
    cleaned = {k: v for k, v in event.items() if k != "content_id"}
    normalised = _normalise_for_canonical(cleaned)
    return json.dumps(
        normalised,
        cls=_V8FloatJSONEncoder,
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def canonical_record_line(event: Mapping[str, Any]) -> bytes:
    """Return the canonical-form JSONL line for *event*, **including**
    ``content_id`` (UTF-8 bytes; trailing newline NOT included).

    v1.1.2 (audit fix G2-B-MED-2): companion helper to :func:`canonical_json`
    used by :meth:`backends.LocalJSONLBackend.write_event` so the on-disk
    JSONL form IS the canonical form.  The Cloudflare Worker (v1.2.0
    DualWriteBackend target) can byte-copy disk lines to R2 and verify
    content_id directly — strip the ``content_id`` key from the parsed line
    and re-canonicalise the rest; the result matches :func:`canonical_json`
    output for the same record.

    Differs from :func:`canonical_json` only in that ``content_id`` is
    preserved, so the on-disk row stays self-describing (reader code can
    read content_id without recomputing).  Every other rule (sorted keys,
    V8 float repr, NaN→null) is identical, so the disk row sorted-and-
    stripped equals the canonical_json bytes for the same event.
    """
    normalised = _normalise_for_canonical(dict(event))
    return json.dumps(
        normalised,
        cls=_V8FloatJSONEncoder,
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def compute_content_id(event: Mapping[str, Any]) -> str:
    """Return ``"sha256:<hex>"`` content-addressable ID for *event*.

    The ID is the SHA-256 of :func:`canonical_json` output.  Tamper-evident:
    any field change produces a different ID.  Idempotent: re-emitting the
    same event computes the same ID, enabling natural dedup.
    """
    digest = hashlib.sha256(canonical_json(event)).hexdigest()
    return f"sha256:{digest}"


# ── Environment fingerprint ───────────────────────────────────────────────────

def build_env_fingerprint(
    *, model_id: Optional[str] = None, llm_provider: Optional[str] = None
) -> Dict[str, Any]:
    """Return a small, side-effect-free environment fingerprint dict.

    Mirrors evomap's ``{node_version, platform, arch}`` plus the LLM-side
    identifiers, which are the single biggest source of run-to-run variance
    in this codebase.
    """
    return {
        "python_version": platform.python_version(),
        "platform": platform.platform(terse=True),
        "arch": platform.machine() or "unknown",
        "model_id": str(model_id or "").strip() or None,
        "llm_provider": str(llm_provider or "").strip() or None,
    }


# ── Timestamps ────────────────────────────────────────────────────────────────

def utc_now_iso() -> str:
    """Return current UTC time as ISO-8601 with millisecond precision and
    a ``Z`` suffix (matches the ``ts`` format produced by ``telemetry.py``).
    """
    now = datetime.now(timezone.utc)
    return now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z"


# ── Event builder ─────────────────────────────────────────────────────────────

@dataclass
class InsightEvent:
    """A single insight ledger event.

    The order of fields determines neither serialisation order nor canonical
    JSON ordering (which uses ``sort_keys=True``).  This dataclass exists
    purely for builder ergonomics in the call sites; the persisted form is
    the ``to_dict()`` output.
    """

    kind: EventKind
    stage: str
    run_id: str
    project_name: str
    mode: str
    signals: List[str]
    payload: Dict[str, Any]
    env_fingerprint: Dict[str, Any]
    outcome: Dict[str, Any]
    reusability: Optional[Dict[str, Any]] = None
    ts: str = field(default_factory=utc_now_iso)
    schema_version: int = SCHEMA_VERSION
    content_id: str = ""

    def stream_name(self) -> str:
        """Return the JSONL stream this event belongs to.

        Mapping (one stream per kind, matches the layout documented in
        ``backends.py``):

        * ``output_method`` → ``output``
        * ``error_record`` → ``error``
        * ``direction_debate_rejection`` → ``debate``
        * ``runtime_params`` → ``params``
        """
        return {
            EventKind.OUTPUT_METHOD: "output",
            EventKind.ERROR_RECORD: "error",
            EventKind.DIRECTION_DEBATE_REJECTION: "debate",
            EventKind.RUNTIME_PARAMS: "params",
        }[self.kind]

    def to_dict(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {
            "schema_version": self.schema_version,
            "ts": self.ts,
            "run_id": self.run_id,
            "project_name": self.project_name,
            "mode": self.mode,
            "kind": self.kind.value,
            "stage": self.stage,
            "signals": list(self.signals),
            "env_fingerprint": dict(self.env_fingerprint),
            "outcome": dict(self.outcome),
            "payload": dict(self.payload),
        }
        if self.reusability is not None:
            out["reusability"] = dict(self.reusability)
        # content_id is computed *over* the dict-without-content_id and then
        # injected so it survives a JSONL round-trip.
        if not self.content_id:
            self.content_id = compute_content_id(out)
        out["content_id"] = self.content_id
        return out


# ── Truncation helpers (used by call-site payload builders) ───────────────────

def truncate_text(s: Optional[str], max_chars: int) -> str:
    """Return *s* truncated to *max_chars* with an ellipsis if cut.

    Returns ``""`` for ``None``/empty inputs.  ``max_chars <= 0`` returns
    ``""``.  No structural parsing — pure character truncation, matching
    evomap's "first N chars" approach for verdict excerpts.
    """
    if not s:
        return ""
    if max_chars <= 0:
        return ""
    text = str(s)
    if len(text) <= max_chars:
        return text
    return text[: max_chars] + "…"


__all__ = [
    "SCHEMA_VERSION",
    "EventKind",
    "OutcomeStatus",
    "InsightEvent",
    "classify_asset_category",
    "extract_signals",
    "canonical_json",
    "compute_content_id",
    "build_env_fingerprint",
    "utc_now_iso",
    "truncate_text",
]
