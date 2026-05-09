"""
features/quant_lint.py
=======================
Domain-aware AST lint for Quant-mode generated code.

Reviewers (LLM-based) consistently flag certain quantitative-trading bugs but
fail to *fix* them through the quality loop — most notoriously look-ahead
bias. The fix-loop sees the reviewer's prose complaint, generates a "fix" that
shuffles things around without addressing the root cause, and the issue
re-surfaces in the next round. The loop then either spins until budget runs
out or stagnation-stops with the bug still present.

This module replaces the LLM verdict on a small set of mechanical bugs with a
deterministic AST check. When a rule fires, the issue carries the exact line
number and a concrete code-level suggestion, which gives the fix-step a much
better signal than a paraphrased reviewer note.

Rules
-----

- ``Q001-lookahead-entry`` (HIGH) — entry price is taken from the *current*
  bar's ``open`` instead of the *next* bar's ``open``. Look-ahead bias: the
  signal is computed from bar ``t`` but the order is filled at the same bar's
  open price, which the strategy could not have observed at signal time.
  Pattern: ``entry_price = row["open"]`` (or ``.open``) inside a function
  whose body also computes a signal from ``close`` of the same row.

- ``Q002-range-off-by-one`` (MEDIUM) — ``range(1, hold_minutes)`` in a
  stop-loss / take-profit check loop. The inclusive range should be
  ``range(1, hold_minutes + 1)`` so the final bar in the holding window is
  inspected; otherwise positions exit one bar early.

- ``Q003-trade-spread-zero`` (MEDIUM) — ``Trade(...)`` instantiated with
  ``spread=0`` (or ``spread=0.0``) when ``estimate_spread`` / ``ORDERBOOK_SLIPPAGE_PCT``
  is reachable in the same module. Indicates the realistic-cost model was
  defined but never plumbed through to the trade record.

- ``Q004-fixed-slippage`` (MEDIUM) — module declares
  ``DYNAMIC_SLIPPAGE_ENABLED = True`` (or reads a similarly-named env var)
  but the only assignment to ``slippage`` is a constant — the dynamic path
  was advertised but never implemented.

Each rule emits one issue per offending site, never per file. The check is
purely AST-based — no imports, no exec, safe on any compileable bundle.

Public API mirrors :mod:`cross_reference_check` so callers can merge the two
result lists without adapter code.
"""
from __future__ import annotations

import ast
import os
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set, Tuple


__all__ = [
    "QuantLintIssue",
    "QuantLintReport",
    "analyse_quant_lint",
    "analyse_quant_lint_from_files",
]


# ─── Data classes ─────────────────────────────────────────────────────────────


@dataclass
class QuantLintIssue:
    severity: str
    category: str
    description: str
    file: Optional[str]
    line: Optional[int] = None
    suggestion: Optional[str] = None
    rule: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "severity": self.severity,
            "category": self.category,
            "description": self.description,
            "file": self.file,
            "line": self.line,
            "suggestion": self.suggestion,
            "rule": self.rule,
        }


@dataclass
class QuantLintReport:
    passes: bool = True
    issues: List[QuantLintIssue] = field(default_factory=list)
    files_scanned: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "passes": self.passes,
            "files_scanned": self.files_scanned,
            "issues": [i.to_dict() for i in self.issues],
        }


# ─── Helpers ──────────────────────────────────────────────────────────────────


def _is_open_of(node: ast.AST, *, allow_indices: bool = True) -> bool:
    """Return True iff *node* reads an OHLCV ``open`` value.

    v1.0.5 round 2: also recognises ``df.loc[idx, "open"]`` /
    ``df.loc[idx, 'open']`` (pandas .loc tuple access) and unwraps single-
    argument numeric coerces ``float(...)`` / ``Decimal(...)``.
    """
    # Unwrap ``float(x)`` / ``int(x)`` / ``Decimal(x)`` etc.
    if isinstance(node, ast.Call) and len(node.args) == 1 and not node.keywords:
        callee = node.func
        callee_name: Optional[str] = None
        if isinstance(callee, ast.Name):
            callee_name = callee.id
        elif isinstance(callee, ast.Attribute):
            callee_name = callee.attr
        if callee_name in {"float", "int", "Decimal", "round"}:
            return _is_open_of(node.args[0], allow_indices=allow_indices)

    if isinstance(node, ast.Attribute) and node.attr == "open":
        return True
    if not allow_indices:
        return False
    if isinstance(node, ast.Subscript):
        # Constant string "open" / "Open" / "OPEN" — be lenient on case.
        s = node.slice
        if isinstance(s, ast.Index):  # py < 3.9 fallback
            s = s.value  # type: ignore[attr-defined]
        if isinstance(s, ast.Constant) and isinstance(s.value, str):
            return s.value.lower() == "open"
        # df.loc[idx, "open"] — slice is a Tuple where one element is "open".
        if isinstance(s, ast.Tuple):
            for elt in s.elts:
                if isinstance(elt, ast.Constant) and isinstance(elt.value, str):
                    if elt.value.lower() == "open":
                        return True
    return False


def _is_close_of(node: ast.AST) -> bool:
    """v1.0.5 round 2: recognise ``.close`` attribute, ``[<str>]`` and
    ``df.loc[idx, "close"]`` tuple-style indexing.
    """
    if isinstance(node, ast.Call) and len(node.args) == 1 and not node.keywords:
        callee = node.func
        callee_name: Optional[str] = None
        if isinstance(callee, ast.Name):
            callee_name = callee.id
        elif isinstance(callee, ast.Attribute):
            callee_name = callee.attr
        if callee_name in {"float", "int", "Decimal", "round"}:
            return _is_close_of(node.args[0])
    if isinstance(node, ast.Attribute) and node.attr == "close":
        return True
    if isinstance(node, ast.Subscript):
        s = node.slice
        if isinstance(s, ast.Index):
            s = s.value  # type: ignore[attr-defined]
        if isinstance(s, ast.Constant) and isinstance(s.value, str):
            return s.value.lower() == "close"
        if isinstance(s, ast.Tuple):
            for elt in s.elts:
                if isinstance(elt, ast.Constant) and isinstance(elt.value, str):
                    if elt.value.lower() == "close":
                        return True
    return False


def _walk_for_close_signal(fn_node: ast.AST) -> bool:
    """Heuristic — does this function read ``close`` from any source?"""
    for sub in ast.walk(fn_node):
        if _is_close_of(sub):
            return True
        # Reading a precomputed indicator like ``row["signal"]`` also counts.
        if isinstance(sub, ast.Subscript):
            s = sub.slice
            if isinstance(s, ast.Index):
                s = s.value  # type: ignore[attr-defined]
            if isinstance(s, ast.Constant) and isinstance(s.value, str):
                if s.value.lower() in {"signal", "indicator", "rsi", "macd", "score"}:
                    return True
    return False


def _is_zero_constant(node: ast.AST) -> bool:
    return isinstance(node, ast.Constant) and isinstance(node.value, (int, float)) and float(node.value) == 0.0


def _module_defines_dynamic_slippage_flag(tree: ast.AST) -> bool:
    """
    True if the module sets ``DYNAMIC_SLIPPAGE_ENABLED = True`` (or reads it from env
    with a default of ``True``). Other naming conventions
    (``USE_DYNAMIC_SLIPPAGE``, ``ENABLE_DYNAMIC_SLIPPAGE``) also count.
    """
    candidate_names = {
        "DYNAMIC_SLIPPAGE_ENABLED",
        "DYNAMIC_SLIPPAGE",
        "USE_DYNAMIC_SLIPPAGE",
        "ENABLE_DYNAMIC_SLIPPAGE",
    }
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id in candidate_names:
                    val = node.value
                    if isinstance(val, ast.Constant) and val.value is True:
                        return True
                    # `os.environ.get("DYNAMIC_SLIPPAGE_ENABLED", "true").lower() in {"1","true",…}`
                    if isinstance(val, ast.Compare):
                        return True
                    if isinstance(val, ast.Call):
                        return True
    return False


def _module_has_estimate_spread(tree: ast.AST) -> bool:
    """True if the module exposes a callable named like a spread estimator."""
    targets = {"estimate_spread", "compute_spread", "calc_spread", "get_spread"}
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name in targets:
            return True
        if isinstance(node, ast.Name) and node.id in targets:
            return True
    return False


def _slippage_is_only_constant(tree: ast.AST) -> bool:
    """
    True if every assignment whose target is named ``slippage`` (case-insensitive)
    is a numeric constant or a direct attribute read of a config constant
    (``config.ORDERBOOK_SLIPPAGE_PCT`` etc.) — no function call, no arithmetic
    over an order's notional.
    """
    saw_slippage_assign = False
    has_dynamic = False
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        for target in node.targets:
            name = None
            if isinstance(target, ast.Name):
                name = target.id
            elif isinstance(target, ast.Attribute):
                name = target.attr
            if not name or name.lower() != "slippage":
                continue
            saw_slippage_assign = True
            val = node.value
            if isinstance(val, ast.Constant):
                continue
            if isinstance(val, ast.Attribute):
                continue
            if isinstance(val, ast.Name):
                # Could still be a precomputed scalar — still effectively constant.
                continue
            # Anything else (Call, BinOp involving non-constants, IfExp, etc.)
            # is treated as dynamic.
            has_dynamic = True
    if not saw_slippage_assign:
        return False
    return not has_dynamic


# ─── Visitors ─────────────────────────────────────────────────────────────────


_ENTRY_PRICE_NAMES: Set[str] = {
    "entry_price",
    "entry",
    "fill_price",
    "execution_price",
    "exec_price",
    "price_at_entry",
    "trade_price",
    "buy_price",
    "open_price",
    "fill",
}

# v1.0.5 round 3 (final): the canonical OHLCV layout in pandas / pyarrow
# bar-row tuples is positional-index 0=open, 1=high, 2=low, 3=close, 4=volume.
# Any positional indexing into a row alias inside a loop body that hits index
# 0 or 1 is therefore very likely an "open" read in disguise.
_OHLCV_POSITIONAL_OPEN_INDICES: Set[int] = {0}
_ROW_VAR_NAME_HINTS: Tuple[str, ...] = (
    "row", "bar", "candle", "tick", "ohlcv", "ohlc", "rec", "r", "b",
)


def _row_alias_name(node: ast.AST) -> Optional[str]:
    """Best-effort: does *node* read from something that looks like a row?

    Returns the variable name if so. Used for positional-index escape detection.
    """
    if isinstance(node, ast.Name):
        if node.id in _ROW_VAR_NAME_HINTS:
            return node.id
    # `row.values` / `bar.values` access count too — `.values` returns the
    # underlying array.
    if isinstance(node, ast.Attribute) and node.attr in {"values", "to_list", "tolist"}:
        return _row_alias_name(node.value)
    # `list(row)` cast — Call to `list` with a single positional arg.
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id in {"list", "tuple"}
        and len(node.args) == 1
    ):
        return _row_alias_name(node.args[0])
    return None


def _module_open_returning_functions(tree: ast.AST) -> Set[str]:
    """v1.0.5 round 3 (final): find module-level functions whose body returns
    an "open" read of one of their parameters — these are bug-multipliers
    when wrapped around a row variable.

    Example::

        def compute_entry(row):
            return row['open']

    Anywhere in the module ``entry_price = compute_entry(row)`` becomes a
    look-ahead site. We only consider top-level FunctionDef / AsyncFunctionDef
    so the result remains tractable on large bundles.
    """
    out: Set[str] = set()
    for node in tree.body if hasattr(tree, "body") else []:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        # Collect the parameter names — these are "row-like" within the body.
        params = {
            arg.arg for arg in (node.args.posonlyargs + node.args.args + node.args.kwonlyargs)
        }
        if not params:
            continue
        if _function_body_returns_open(node, param_names=params):
            out.add(node.name)
    return out


def _function_body_returns_open(fn: ast.AST, *, param_names: Set[str]) -> bool:
    """True iff some Return inside *fn* yields an "open" read of one of the
    function's parameters (or an alias of them).
    """
    aliases: Set[str] = set(param_names)
    # First pass: collect simple aliases `e = row` where `row` is in param_names.
    for sub in ast.walk(fn):
        if isinstance(sub, ast.Assign) and len(sub.targets) == 1:
            tgt = sub.targets[0]
            if isinstance(tgt, ast.Name) and isinstance(sub.value, ast.Name):
                if sub.value.id in aliases:
                    aliases.add(tgt.id)
    # Second pass: any Return whose value is `<alias>.open` / `alias['open']`
    # / wrapper-of-those.
    for sub in ast.walk(fn):
        if not isinstance(sub, ast.Return) or sub.value is None:
            continue
        val = sub.value
        # Strip simple wrapper Calls (float/int/Decimal/round).
        if isinstance(val, ast.Call) and len(val.args) == 1 and not val.keywords:
            f = val.func
            f_name = f.id if isinstance(f, ast.Name) else (
                f.attr if isinstance(f, ast.Attribute) else None
            )
            if f_name in {"float", "int", "Decimal", "round"}:
                val = val.args[0]
        if not _is_open_of(val):
            continue
        # Confirm the open-read is on an alias of a parameter.
        # _is_open_of accepts shapes like `x.open` or `x['open']`.
        target_obj: Optional[ast.AST] = None
        if isinstance(val, ast.Attribute):
            target_obj = val.value
        elif isinstance(val, ast.Subscript):
            target_obj = val.value
        if isinstance(target_obj, ast.Name) and target_obj.id in aliases:
            return True
    return False


class _LookaheadVisitor(ast.NodeVisitor):
    """Detect ``entry_price = <row>.open`` inside a function that reads ``close``.

    v1.0.5 round 3 (final): the visitor now also fires on three escape
    patterns previously missed:

    - ``compute_entry(row)`` where ``compute_entry`` is a module-level
      function that returns ``row['open']`` (or an alias). Caller passes
      ``open_returning_funcs`` collected once per module.
    - Positional indexing ``bar.values[0]`` / ``list(row)[0]`` / ``row[0]``
      where the indexed object is a row alias and the index is 0 (open in
      canonical OHLCV layout). Emits MEDIUM severity since the layout
      convention isn't universal.
    - ``for col in ['open', ...]: ... entry_price = row[col]`` — column-name
      indirection through a string-literal iter.
    """

    def __init__(
        self,
        file: str,
        noqa_lines: Set[int],
        *,
        open_returning_funcs: Optional[Set[str]] = None,
    ) -> None:
        self.file = file
        self.noqa_lines = noqa_lines
        self.issues: List[QuantLintIssue] = []
        self._fn_stack: List[ast.FunctionDef] = []
        self._open_returning_funcs: Set[str] = set(open_returning_funcs or [])
        # column-iter binding stack: each entry is dict[var_name -> set of column-string-literals]
        self._col_iter_stack: List[Dict[str, Set[str]]] = []

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:  # noqa: D401
        self._fn_stack.append(node)
        self.generic_visit(node)
        self._fn_stack.pop()

    visit_AsyncFunctionDef = visit_FunctionDef  # type: ignore[assignment]

    def visit_For(self, node: ast.For) -> None:
        # Track `for col in ['open', ...]:` so a downstream `row[col]` can be
        # classified.
        binding: Dict[str, Set[str]] = {}
        if isinstance(node.target, ast.Name) and isinstance(node.iter, (ast.List, ast.Tuple)):
            literals: Set[str] = set()
            for elt in node.iter.elts:
                if isinstance(elt, ast.Constant) and isinstance(elt.value, str):
                    literals.add(elt.value.lower())
            if literals:
                binding[node.target.id] = literals
        self._col_iter_stack.append(binding)
        try:
            self.generic_visit(node)
        finally:
            self._col_iter_stack.pop()

    def _value_reads_open_via_iter_var(self, value: ast.AST) -> bool:
        """v1.0.5 round 3 (final): detect ``row[col]`` where ``col`` is the
        iter-var of a surrounding ``for col in ['open', ...]:``.
        """
        if not isinstance(value, ast.Subscript):
            return False
        s = value.slice
        if isinstance(s, ast.Index):  # py < 3.9 fallback
            s = s.value  # type: ignore[attr-defined]
        if not isinstance(s, ast.Name):
            return False
        for binding in reversed(self._col_iter_stack):
            literals = binding.get(s.id)
            if literals and "open" in literals:
                return True
        return False

    def _value_reads_open_positional(self, value: ast.AST) -> bool:
        """v1.0.5 round 3 (final): detect ``row[0]`` / ``bar.values[0]`` /
        ``list(row)[0]`` — positional indexing on a row-alias yielding the
        canonical OHLCV "open" slot.
        """
        if not isinstance(value, ast.Subscript):
            return False
        s = value.slice
        if isinstance(s, ast.Index):
            s = s.value  # type: ignore[attr-defined]
        if not isinstance(s, ast.Constant) or not isinstance(s.value, int):
            return False
        if s.value not in _OHLCV_POSITIONAL_OPEN_INDICES:
            return False
        return _row_alias_name(value.value) is not None

    def _value_calls_open_returning_fn(self, value: ast.AST) -> Optional[str]:
        """v1.0.5 round 3 (final): detect ``compute_entry(row)`` where the
        callee is a module-level function known to return ``row['open']``.
        Returns the called function's name, or None.
        """
        # Strip wrapper calls (float/int/Decimal/round).
        if isinstance(value, ast.Call) and len(value.args) == 1 and not value.keywords:
            f = value.func
            f_name = f.id if isinstance(f, ast.Name) else (
                f.attr if isinstance(f, ast.Attribute) else None
            )
            if f_name in {"float", "int", "Decimal", "round"}:
                return self._value_calls_open_returning_fn(value.args[0])
        if not isinstance(value, ast.Call):
            return None
        callee = value.func
        callee_name: Optional[str] = None
        if isinstance(callee, ast.Name):
            callee_name = callee.id
        elif isinstance(callee, ast.Attribute):
            callee_name = callee.attr
        if callee_name and callee_name in self._open_returning_funcs:
            return callee_name
        return None

    def visit_Assign(self, node: ast.Assign) -> None:
        # Only flag when we're inside a function that also reads `close`.
        # A standalone `entry_price = bar.open` at module scope is too noisy
        # to flag (could be metadata, fixture, etc.).
        if not self._fn_stack:
            self.generic_visit(node)
            return
        if node.lineno in self.noqa_lines:
            self.generic_visit(node)
            return

        for target in node.targets:
            if not isinstance(target, ast.Name):
                continue
            if target.id.lower() not in _ENTRY_PRICE_NAMES:
                continue
            value = node.value
            current_fn = self._fn_stack[-1]
            if not _walk_for_close_signal(current_fn):
                continue

            # Tier 1: direct `_is_open_of` shapes (existing behaviour).
            if _is_open_of(value):
                self.issues.append(self._make_issue(target.id, node, variant="direct"))
                continue
            # Tier 2: column-string-literal iter binding.
            if self._value_reads_open_via_iter_var(value):
                self.issues.append(
                    self._make_issue(target.id, node, variant="iter-col-literal")
                )
                continue
            # Tier 3: function-wrapped open read.
            wrapped_fn = self._value_calls_open_returning_fn(value)
            if wrapped_fn is not None:
                self.issues.append(
                    self._make_issue(
                        target.id, node, variant="fn-wrapped", fn_name=wrapped_fn
                    )
                )
                continue
            # Tier 4 (medium): positional index — convention-dependent, not
            # certain. Emit as warning rather than high.
            if self._value_reads_open_positional(value):
                self.issues.append(
                    self._make_issue(target.id, node, variant="positional")
                )
                continue
        self.generic_visit(node)

    def _make_issue(
        self,
        target_name: str,
        node: ast.Assign,
        *,
        variant: str,
        fn_name: Optional[str] = None,
    ) -> QuantLintIssue:
        if variant == "direct":
            severity = "high"
            description = (
                f"Look-ahead bias: '{target_name}' is set to the *current* bar's "
                "`open`, but the signal is derived from the same bar's `close`. "
                "The strategy could not have observed `close` at the time of "
                "entry on that bar."
            )
            suggestion = (
                "Fill at the *next* bar's open price. For DataFrame iteration, "
                "pre-shift the entry-price column with `df['next_open'] = "
                "df['open'].shift(-1)` and read `row['next_open']`, or step the "
                "loop index by one before reading `open`."
                " (Use `# noqa: Q001` to suppress for legitimate same-bar fills.)"
            )
        elif variant == "iter-col-literal":
            severity = "high"
            description = (
                f"Look-ahead bias (column-iter form): '{target_name}' is set to "
                "`row[col]` where `col` ranges over a list literal containing "
                "'open'. Same root cause as the direct form — entry uses the "
                "current bar's open while the signal sees the current bar's close."
            )
            suggestion = (
                "Either pre-shift the open column to `next_open` and switch the "
                "iter list to `['next_open', ...]`, or step the bar index by one "
                "before reading. (Use `# noqa: Q001` to suppress.)"
            )
        elif variant == "fn-wrapped":
            severity = "high"
            description = (
                f"Look-ahead bias (function-wrapped form): '{target_name}' is "
                f"set to `{fn_name}(row)`, and `{fn_name}` is a module-level "
                "function that returns the row's `open`. Wrapping the open-read "
                "in a helper does not change the bug — the strategy still fills "
                "at the same bar whose close drove the signal."
            )
            suggestion = (
                f"Modify `{fn_name}` to read the *next* bar's open, or stop "
                "calling it from the same iteration that computes the close-based "
                "signal."
            )
        elif variant == "positional":
            # Convention-dependent — emit as MEDIUM warning rather than HIGH.
            severity = "medium"
            description = (
                f"Possible look-ahead bias (positional form): '{target_name}' is "
                "assigned from a positional index on a row alias (e.g. "
                "`bar.values[0]`, `list(row)[0]`, `row[0]`). In the canonical "
                "pandas/pyarrow OHLCV layout, index 0 is `open`. If your data "
                "uses that convention, this is a same-bar look-ahead."
            )
            suggestion = (
                "Switch to named indexing (`row['open']` after a `next_open` "
                "shift) so the fix path is explicit and Q001 stays in HIGH "
                "regime. (Use `# noqa: Q001` if the convention is reversed.)"
            )
        else:  # pragma: no cover - exhaustive switch
            severity = "high"
            description = "Look-ahead bias (unspecified variant)."
            suggestion = "Use the next bar's open."

        return QuantLintIssue(
            severity=severity,
            category="bug",
            description=description,
            file=os.path.basename(self.file),
            line=node.lineno,
            suggestion=suggestion,
            rule="Q001-lookahead-entry",
        )


class _RangeOffByOneVisitor(ast.NodeVisitor):
    """Detect ``range(1, hold_minutes)`` inside a stop-check loop."""

    _HOLD_NAMES = {
        "hold_minutes",
        "holding_minutes",
        "holding_period",
        "hold_bars",
        "holding_bars",
        "max_hold",
        "max_hold_minutes",
    }

    def __init__(self, file: str, noqa_lines: Set[int]) -> None:
        self.file = file
        self.noqa_lines = noqa_lines
        self.issues: List[QuantLintIssue] = []

    def visit_For(self, node: ast.For) -> None:
        if node.lineno in self.noqa_lines:
            self.generic_visit(node)
            return
        iter_node = node.iter
        if (
            isinstance(iter_node, ast.Call)
            and isinstance(iter_node.func, ast.Name)
            and iter_node.func.id == "range"
            and len(iter_node.args) == 2
        ):
            start, stop = iter_node.args
            if (
                isinstance(start, ast.Constant)
                and start.value == 1
                and isinstance(stop, ast.Name)
                and stop.id in self._HOLD_NAMES
            ):
                # Only flag if the loop body looks like a stop-loss / take-profit
                # check — heuristic via attribute/keyword presence.
                body_text = ast.dump(node)
                signals = (
                    "stop_loss",
                    "stop_price",
                    "take_profit",
                    "tp_price",
                    "exit_price",
                    "high",
                    "low",
                )
                if any(s in body_text for s in signals):
                    self.issues.append(
                        QuantLintIssue(
                            severity="medium",
                            category="bug",
                            description=(
                                f"Off-by-one stop check: range(1, {stop.id}) excludes "
                                f"the last bar of the holding window. With "
                                f"{stop.id}=N, the loop only examines bars t+1..t+N-1, "
                                "so the position exits one bar early when the stop "
                                "fires on the final bar."
                            ),
                            file=os.path.basename(self.file),
                            line=node.lineno,
                            suggestion=f"Use range(1, {stop.id} + 1).",
                            rule="Q002-range-off-by-one",
                        )
                    )
        self.generic_visit(node)


class _TradeSpreadVisitor(ast.NodeVisitor):
    """Detect ``Trade(spread=0, ...)`` when the module has a real spread estimator."""

    def __init__(
        self,
        file: str,
        has_estimate_spread: bool,
        noqa_lines: Set[int],
    ) -> None:
        self.file = file
        self.has_estimate_spread = has_estimate_spread
        self.noqa_lines = noqa_lines
        self.issues: List[QuantLintIssue] = []

    def visit_Call(self, node: ast.Call) -> None:
        callee = node.func
        callee_name: Optional[str] = None
        if isinstance(callee, ast.Name):
            callee_name = callee.id
        elif isinstance(callee, ast.Attribute):
            callee_name = callee.attr
        if callee_name != "Trade":
            self.generic_visit(node)
            return
        for kw in node.keywords:
            if kw.arg != "spread":
                continue
            if not _is_zero_constant(kw.value):
                continue
            if not self.has_estimate_spread:
                continue
            kw_line = getattr(kw, "lineno", node.lineno)
            if kw_line in self.noqa_lines or node.lineno in self.noqa_lines:
                continue
            self.issues.append(
                QuantLintIssue(
                    severity="medium",
                    category="bug",
                    description=(
                        "Trade(spread=0) but the module exposes a spread-estimator "
                        "function — costs are silently being zeroed out, so backtest "
                        "P&L will be optimistic by the entire spread."
                    ),
                    file=os.path.basename(self.file),
                    line=kw_line,
                    suggestion=(
                        "Replace spread=0 with spread=estimate_spread(...) (or whichever "
                        "estimator the module provides), passing the same bar/price the "
                        "trade executes against."
                    ),
                    rule="Q003-trade-spread-zero",
                )
            )
        self.generic_visit(node)


# ─── Module-level entry points ───────────────────────────────────────────────


_NOQA_PATTERN = re.compile(
    r"#\s*noqa(?::\s*(?P<rules>[\w\d\-,\s]+))?", re.IGNORECASE
)


def _collect_noqa_lines(source: str, *, rule_id: str) -> Set[int]:
    """v1.0.5 round 2: parse ``# noqa`` / ``# noqa: Q001`` line suppressions.

    Returns the set of 1-indexed line numbers carrying a noqa marker that
    matches ``rule_id`` either by a literal occurrence or by an empty
    ``# noqa`` (which suppresses everything on that line).
    """
    out: Set[int] = set()
    if not source:
        return out
    rule_token = rule_id.split("-", 1)[0].lower()
    for lineno, line in enumerate(source.splitlines(), start=1):
        m = _NOQA_PATTERN.search(line)
        if not m:
            continue
        rules = (m.group("rules") or "").strip()
        if not rules:
            out.add(lineno)
            continue
        tokens = {t.strip().lower() for t in rules.split(",") if t.strip()}
        if rule_token in tokens or rule_id.lower() in tokens:
            out.add(lineno)
    return out


def _analyse_one_file(path: str, source: str) -> List[QuantLintIssue]:
    issues: List[QuantLintIssue] = []
    try:
        tree = ast.parse(source, filename=path)
    except SyntaxError:
        return issues

    noqa_q001 = _collect_noqa_lines(source, rule_id="Q001-lookahead-entry")
    noqa_q002 = _collect_noqa_lines(source, rule_id="Q002-range-off-by-one")
    noqa_q003 = _collect_noqa_lines(source, rule_id="Q003-trade-spread-zero")
    noqa_q004 = _collect_noqa_lines(source, rule_id="Q004-fixed-slippage")

    # Q001 / Q002 — round 3 (final): scan once for module-level functions that
    # return an open-shaped read of one of their parameters; the LookaheadVisitor
    # uses this to catch the function-wrapped escape pattern.
    open_returning_funcs = _module_open_returning_functions(tree)
    look = _LookaheadVisitor(
        path, noqa_lines=noqa_q001, open_returning_funcs=open_returning_funcs
    )
    look.visit(tree)
    issues.extend(look.issues)

    rng = _RangeOffByOneVisitor(path, noqa_lines=noqa_q002)
    rng.visit(tree)
    issues.extend(rng.issues)

    # Q003
    has_estimator = _module_has_estimate_spread(tree)
    if has_estimator:
        spread = _TradeSpreadVisitor(path, has_estimator, noqa_lines=noqa_q003)
        spread.visit(tree)
        issues.extend(spread.issues)

    # Q004 — module-level state check, not per-call.
    if _module_defines_dynamic_slippage_flag(tree) and _slippage_is_only_constant(tree):
        # Pin the issue to the first slippage assignment we find.
        first_line: Optional[int] = None
        for node in ast.walk(tree):
            if not isinstance(node, ast.Assign):
                continue
            for target in node.targets:
                name = None
                if isinstance(target, ast.Name):
                    name = target.id
                elif isinstance(target, ast.Attribute):
                    name = target.attr
                if name and name.lower() == "slippage":
                    first_line = node.lineno
                    break
            if first_line is not None:
                break
        # Q004 honours noqa on ANY line of the module's slippage section, OR a
        # module-level `# noqa: Q004` on the DYNAMIC_SLIPPAGE_ENABLED line.
        if not noqa_q004 or (first_line is not None and first_line not in noqa_q004):
            issues.append(
                QuantLintIssue(
                    severity="medium",
                    category="bug",
                    description=(
                        "DYNAMIC_SLIPPAGE_ENABLED is set to True (or read from env), "
                        "but every assignment to `slippage` in this module is a constant. "
                        "The dynamic-slippage path was advertised in config but never "
                        "implemented — backtest costs will not respond to order size."
                    ),
                    file=os.path.basename(path),
                    line=first_line,
                    suggestion=(
                        "Implement a size→slippage function (e.g. sqrt-impact: "
                        "`slippage = config.ORDERBOOK_SLIPPAGE_PCT * sqrt(notional / liquidity)`)"
                        " and call it from the trade-cost path."
                        " (Use `# noqa: Q004` on the slippage assignment to suppress.)"
                    ),
                    rule="Q004-fixed-slippage",
                )
            )

    return issues


_DEFAULT_IGNORE_DIRS = {
    "__pycache__",
    ".git",
    ".mypy_cache",
    ".pytest_cache",
    ".tox",
    "dist",
    "build",
    ".eggs",
    "node_modules",
    ".venv",
    "venv",
    "tests",
}


def analyse_quant_lint_from_files(files: List[Tuple[str, str]]) -> QuantLintReport:
    issues: List[QuantLintIssue] = []
    for path, source in files:
        issues.extend(_analyse_one_file(path, source))
    return QuantLintReport(
        passes=not issues,
        issues=issues,
        files_scanned=len(files),
    )


def analyse_quant_lint(code_dir: str) -> QuantLintReport:
    """
    Walk ``code_dir`` for ``*.py`` files (skipping ``tests/``) and run all four
    Quant lint rules.
    """
    if not os.path.isdir(code_dir):
        return QuantLintReport(passes=True)
    py_files: List[str] = []
    for dirpath, dirnames, filenames in os.walk(code_dir):
        dirnames[:] = [d for d in dirnames if d not in _DEFAULT_IGNORE_DIRS]
        for fname in filenames:
            if fname.endswith(".py"):
                py_files.append(os.path.join(dirpath, fname))
    files: List[Tuple[str, str]] = []
    for path in py_files:
        try:
            with open(path, "r", encoding="utf-8") as fh:
                files.append((os.path.relpath(path, code_dir), fh.read()))
        except OSError:
            continue
    return analyse_quant_lint_from_files(files)
