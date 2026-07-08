"""
v1.2.9: two new deterministic Quant-lint rules.

- ``Q005-lookahead-shift`` — a negative ``.shift(...)`` assigned into a
  *trading-signal* column (look-ahead bias).
- ``Q006-stub-implementation`` — hollow function bodies
  (``raise NotImplementedError`` / ``...``) and shortcut-admission comments.

Because ``analyse_quant_lint_from_files`` sets ``passes = not issues`` (ANY
issue gates the quality loop), the emphasis here is on NEGATIVE cases: these
rules must not fire on legitimate code, or they would spuriously trip
``QUALITY_LOOP_GAVE_UP`` on otherwise-fine generated strategies.
"""
from __future__ import annotations

import textwrap

from crucible.features.quant_lint import analyse_quant_lint_from_files


def _rules(source: str) -> list:
    src = textwrap.dedent(source)
    report = analyse_quant_lint_from_files([("strategy.py", src)])
    return [i.rule for i in report.issues]


def _has(source: str, rule: str) -> bool:
    return any(r == rule for r in _rules(source))


# ─── Q005: negative shift into a signal column ───────────────────────────────

def test_q005_flags_negative_shift_into_signal_name():
    assert _has("signal = df['close'].shift(-1)\n", "Q005-lookahead-shift")


def test_q005_flags_subscript_target():
    assert _has("df['signal'] = df['close'].shift(-1)\n", "Q005-lookahead-shift")


def test_q005_flags_periods_keyword():
    assert _has("position = df['close'].shift(periods=-1)\n", "Q005-lookahead-shift")


def test_q005_ignores_supervised_label_names():
    # Shifting a training LABEL backwards is legitimate, not look-ahead.
    for name in ("label", "target", "y", "y_train"):
        assert not _has(f"{name} = df['close'].shift(-1)\n", "Q005-lookahead-shift"), name


def test_q005_ignores_next_open_fix():
    # This is exactly the fix Q001 recommends — must never be flagged by Q005.
    assert not _has("next_open = df['open'].shift(-1)\n", "Q005-lookahead-shift")


def test_q005_ignores_positive_shift():
    assert not _has("signal = df['close'].shift(1)\n", "Q005-lookahead-shift")


def test_q005_respects_noqa():
    assert not _has("signal = df['close'].shift(-1)  # noqa: Q005\n", "Q005-lookahead-shift")


# ─── Q006: stub implementations ──────────────────────────────────────────────

def test_q006_flags_not_implemented_error_body():
    assert _has("def generate_signals(df):\n    raise NotImplementedError\n", "Q006-stub-implementation")


def test_q006_flags_not_implemented_error_with_message():
    assert _has(
        "def generate_signals(df):\n    raise NotImplementedError('todo')\n",
        "Q006-stub-implementation",
    )


def test_q006_flags_ellipsis_body():
    assert _has("def generate_signals(df):\n    ...\n", "Q006-stub-implementation")


def test_q006_flags_ellipsis_after_docstring():
    assert _has(
        'def generate_signals(df):\n    """Generate signals."""\n    ...\n',
        "Q006-stub-implementation",
    )


def test_q006_flags_shortcut_admission_comment():
    src = (
        "def fetch_prices(sym):\n"
        "    # In a real implementation, hit the exchange API here.\n"
        "    return []\n"
    )
    assert _has(src, "Q006-stub-implementation")


# ─── Q006 precision: must NOT fire on legitimate code ────────────────────────

def test_q006_ignores_abstractmethod():
    src = (
        "class Base:\n"
        "    @abstractmethod\n"
        "    def run(self):\n"
        "        ...\n"
    )
    assert not _has(src, "Q006-stub-implementation")


def test_q006_ignores_overload():
    src = (
        "@overload\n"
        "def f(x: int) -> int: ...\n"
    )
    assert not _has(src, "Q006-stub-implementation")


def test_q006_ignores_pass_body():
    assert not _has("def handler(self):\n    pass\n", "Q006-stub-implementation")


def test_q006_ignores_return_none_body():
    assert not _has("def handler(self):\n    return None\n", "Q006-stub-implementation")


def test_q006_ignores_branch_level_not_implemented():
    # NotImplementedError as one branch of a real function is not a stub.
    src = (
        "def convert(mode, x):\n"
        "    if mode == 'weird':\n"
        "        raise NotImplementedError\n"
        "    return x * 2\n"
    )
    assert not _has(src, "Q006-stub-implementation")


def test_q006_ignores_real_body():
    src = (
        "def score(prices):\n"
        "    total = sum(prices)\n"
        "    return total / len(prices)\n"
    )
    assert not _has(src, "Q006-stub-implementation")


def test_q006_ignores_plain_docstring_without_admission():
    src = 'def score(x):\n    """Compute the score for x."""\n    return x + 1\n'
    assert not _has(src, "Q006-stub-implementation")


def test_q006_ignores_admission_phrase_inside_string_without_comment():
    # A phrase inside a string literal (no '#' on the line) must not match.
    assert not _has('msg = "not implemented yet"\n', "Q006-stub-implementation")


def test_q006_respects_noqa():
    src = "def generate_signals(df):\n    raise NotImplementedError  # noqa: Q006\n"
    assert not _has(src, "Q006-stub-implementation")


# ─── whole-report sanity ─────────────────────────────────────────────────────

def test_clean_strategy_passes():
    src = textwrap.dedent(
        """
        def generate_signals(df):
            df['fast'] = df['close'].rolling(10).mean()
            df['slow'] = df['close'].rolling(30).mean()
            df['signal'] = (df['fast'] > df['slow']).astype(int)
            return df
        """
    )
    report = analyse_quant_lint_from_files([("s.py", src)])
    assert report.passes
    assert report.issues == []


# ─── robustness: pathologically deep files must degrade, never crash ────────
#
# Every Q00N rule's visitor is a recursive ast.NodeVisitor (Q005's
# _LookaheadShiftVisitor and Q006's _StubImplementationVisitor included). A
# many-hundred-branch elif-chain -- plausible if an LLM emits a per-symbol /
# per-regime dispatch as elif instead of a lookup table -- nests ~1 `If` AST
# node per branch (`If(orelse=[If(...)])`), which exceeds Python's default
# recursion limit mid-traversal and used to raise an uncaught RecursionError
# out of analyse_quant_lint_from_files, crashing the whole quality-loop
# invocation instead of yielding a lint report.


def _long_elif_chain_source(n: int) -> str:
    lines = ["def dispatch(regime):", "    if regime == 0:", "        return 0"]
    for i in range(1, n):
        lines.append(f"    elif regime == {i}:")
        lines.append(f"        return {i}")
    lines.append("    else:")
    lines.append("        return -1")
    return "\n".join(lines) + "\n"


def test_deep_elif_chain_does_not_crash_the_batch():
    src = _long_elif_chain_source(500)
    report = analyse_quant_lint_from_files([("dispatch.py", src)])
    # Degrades gracefully: no crash, and (since the pathological file could
    # not be fully analysed) it contributes no issues rather than a partial
    # or corrupted result.
    assert report.issues == []


def test_deep_elif_chain_does_not_lose_other_files_in_the_same_batch():
    pathological = _long_elif_chain_source(500)
    clean_with_real_issue = "signal = df['close'].shift(-1)\n"
    report = analyse_quant_lint_from_files(
        [("dispatch.py", pathological), ("strategy.py", clean_with_real_issue)]
    )
    assert report.files_scanned == 2
    rules = [i.rule for i in report.issues]
    assert rules == ["Q005-lookahead-shift"]
    assert report.issues[0].file == "strategy.py"


def test_deep_binop_chain_does_not_crash():
    src = "x = " + "1 + " * 800 + "1\n"
    report = analyse_quant_lint_from_files([("s.py", src)])
    assert report.issues == []
