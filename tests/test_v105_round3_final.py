# ruff: noqa: E402
"""v1.0.5 round 3 finalisation tests:

- Q001 escape paths: function-wrapped, positional-index, column-iter-literal
- W003 dict-style subscript escape (`config['LITERAL']`)
- Mode validation matrix data integrity
- Mode-specific lint helpers (H001 SaaS, A001/A002 Agent, S001/S002 Scientist)
"""
import os
import sys
import unittest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from crucible.features.cross_reference_check import analyse_cross_references_from_files
from crucible.features.mode_validation_matrix import (
    MODE_VALIDATION_MATRIX,
    analyse_agent_lint_from_files,
    analyse_saas_lint_from_files,
    analyse_scientist_lint_from_files,
    get_mode_defences,
    mode_validation_summary_markdown,
)
from crucible.features.quant_lint import analyse_quant_lint_from_files


# ─── Q001 escape paths ──────────────────────────────────────────────────────


class TestQ001FunctionWrappedEscape(unittest.TestCase):
    """`compute_entry(row)` where compute_entry returns row['open']."""

    def test_function_wrapped_open_read_caught(self) -> None:
        files = [
            (
                "strategy.py",
                "def compute_entry(row):\n"
                "    return row['open']\n"
                "\n"
                "def signal(df):\n"
                "    for idx, row in df.iterrows():\n"
                "        if row['close'] > 100:\n"
                "            entry_price = compute_entry(row)\n"
                "            return entry_price\n",
            ),
        ]
        report = analyse_quant_lint_from_files(files)
        rules = [i.rule for i in report.issues]
        self.assertIn("Q001-lookahead-entry", rules)
        # Description must call out the function-wrapped form so the LLM fix
        # step doesn't try to rename the variable.
        descs = [i.description for i in report.issues if i.rule == "Q001-lookahead-entry"]
        self.assertTrue(any("function-wrapped" in d for d in descs), msg=f"got {descs}")

    def test_function_wrapped_with_alias_caught(self) -> None:
        # `e = row; return e['open']` — the helper aliases its parameter.
        files = [
            (
                "strategy.py",
                "def fetch_open(bar):\n"
                "    e = bar\n"
                "    return e['open']\n"
                "\n"
                "def signal(df):\n"
                "    for idx, row in df.iterrows():\n"
                "        if row['close'] > 100:\n"
                "            entry_price = fetch_open(row)\n"
                "            return entry_price\n",
            ),
        ]
        report = analyse_quant_lint_from_files(files)
        rules = [i.rule for i in report.issues]
        self.assertIn("Q001-lookahead-entry", rules)

    def test_function_returning_close_does_not_trigger_q001(self) -> None:
        # Helper returns close, not open — no lookahead.
        files = [
            (
                "strategy.py",
                "def compute_close(row):\n"
                "    return row['close']\n"
                "\n"
                "def signal(df):\n"
                "    for idx, row in df.iterrows():\n"
                "        if row['close'] > 100:\n"
                "            entry_price = compute_close(row)\n"
                "            return entry_price\n",
            ),
        ]
        report = analyse_quant_lint_from_files(files)
        # Either no Q001 fires, or only the direct shape — not function-wrapped.
        wrapped = [i for i in report.issues if i.rule == "Q001-lookahead-entry"
                   and "function-wrapped" in i.description]
        self.assertEqual(wrapped, [])


class TestQ001PositionalIndexEscape(unittest.TestCase):
    def test_row_dot_values_zero_caught(self) -> None:
        files = [
            (
                "strategy.py",
                "def signal(df):\n"
                "    for idx, row in df.iterrows():\n"
                "        if row['close'] > 100:\n"
                "            entry_price = row.values[0]\n"
                "            return entry_price\n",
            ),
        ]
        report = analyse_quant_lint_from_files(files)
        hits = [i for i in report.issues if i.rule == "Q001-lookahead-entry"]
        self.assertTrue(hits, msg="positional escape (row.values[0]) not caught")
        # Severity is medium (convention-dependent), not high.
        self.assertEqual(hits[0].severity, "medium")
        self.assertIn("positional", hits[0].description.lower())

    def test_list_row_zero_caught(self) -> None:
        files = [
            (
                "strategy.py",
                "def signal(df):\n"
                "    for idx, row in df.iterrows():\n"
                "        if row['close'] > 100:\n"
                "            entry_price = list(row)[0]\n"
                "            return entry_price\n",
            ),
        ]
        report = analyse_quant_lint_from_files(files)
        hits = [i for i in report.issues if i.rule == "Q001-lookahead-entry"]
        self.assertTrue(hits)
        self.assertEqual(hits[0].severity, "medium")

    def test_positional_index_one_does_not_trigger(self) -> None:
        # Index 1 = high, not open, in canonical OHLCV layout.
        files = [
            (
                "strategy.py",
                "def signal(df):\n"
                "    for idx, row in df.iterrows():\n"
                "        if row['close'] > 100:\n"
                "            entry_price = row.values[1]\n"
                "            return entry_price\n",
            ),
        ]
        report = analyse_quant_lint_from_files(files)
        hits = [i for i in report.issues if i.rule == "Q001-lookahead-entry"]
        self.assertEqual(hits, [])


class TestQ001ColumnIterLiteralEscape(unittest.TestCase):
    def test_for_col_in_open_list_caught(self) -> None:
        files = [
            (
                "strategy.py",
                "def signal(df):\n"
                "    for idx, row in df.iterrows():\n"
                "        if row['close'] > 100:\n"
                "            for col in ['open']:\n"
                "                entry_price = row[col]\n",
            ),
        ]
        report = analyse_quant_lint_from_files(files)
        rules = [i.rule for i in report.issues]
        self.assertIn("Q001-lookahead-entry", rules)
        descs = [i.description for i in report.issues if i.rule == "Q001-lookahead-entry"]
        self.assertTrue(any("column-iter" in d for d in descs))

    def test_for_col_in_close_list_does_not_trigger(self) -> None:
        files = [
            (
                "strategy.py",
                "def signal(df):\n"
                "    for idx, row in df.iterrows():\n"
                "        if row['open'] > 100:\n"
                "            for col in ['close']:\n"
                "                entry_price = row[col]\n",
            ),
        ]
        report = analyse_quant_lint_from_files(files)
        # No 'open' in the literal list — no lookahead.
        wrapped = [i for i in report.issues if i.rule == "Q001-lookahead-entry"
                   and "column-iter" in i.description]
        self.assertEqual(wrapped, [])


# ─── W003 subscript escape ──────────────────────────────────────────────────


class TestW003SubscriptEscape(unittest.TestCase):
    def test_subscript_unknown_key_emits_w003(self) -> None:
        files = [
            (
                "config.py",
                "class Config:\n"
                "    POSITION_SIZE = 100\n",
            ),
            (
                "strategy.py",
                "import config\n"
                "def go():\n"
                "    return config['NONEXISTENT_KEY']\n",
            ),
        ]
        report = analyse_cross_references_from_files(files)
        rules = [i.rule for i in report.issues]
        self.assertIn("W003-subscript-dynamic-key-unverifiable", rules)

    def test_subscript_known_key_silent(self) -> None:
        files = [
            (
                "config.py",
                "class Config:\n"
                "    POSITION_SIZE = 100\n",
            ),
            (
                "strategy.py",
                "import config\n"
                "def go():\n"
                "    return config['POSITION_SIZE']\n",
            ),
        ]
        report = analyse_cross_references_from_files(files)
        w003 = [i for i in report.issues if i.rule == "W003-subscript-dynamic-key-unverifiable"]
        self.assertEqual(w003, [])

    def test_subscript_int_index_silent(self) -> None:
        # Numeric Subscript is out of scope for W003.
        files = [
            (
                "config.py",
                "DATA = [1, 2, 3]\n",
            ),
            (
                "strategy.py",
                "import config\n"
                "def go():\n"
                "    return config.DATA[0]\n",
            ),
        ]
        report = analyse_cross_references_from_files(files)
        w003 = [i for i in report.issues if i.rule == "W003-subscript-dynamic-key-unverifiable"]
        self.assertEqual(w003, [])


# ─── Mode validation matrix data integrity ──────────────────────────────────


class TestModeValidationMatrix(unittest.TestCase):
    def test_all_four_modes_present(self) -> None:
        self.assertEqual(
            sorted(MODE_VALIDATION_MATRIX),
            ["agent", "quant", "saas", "scientist"],
        )

    def test_each_mode_has_at_least_two_active_defences(self) -> None:
        for mode, matrix in MODE_VALIDATION_MATRIX.items():
            actives = [d for d in matrix.defences if d.status == "active"]
            self.assertGreaterEqual(
                len(actives),
                2,
                msg=f"Mode {mode!r} has fewer than 2 active defences: {actives}",
            )

    def test_status_values_are_canonical(self) -> None:
        allowed = {"active", "opt-in", "n/a", "deferred"}
        for mode, matrix in MODE_VALIDATION_MATRIX.items():
            for d in matrix.defences:
                self.assertIn(d.status, allowed, msg=f"{mode}.{d.name} has unknown status {d.status!r}")

    def test_get_mode_defences_unknown_mode_returns_empty(self) -> None:
        self.assertEqual(get_mode_defences("nonexistent"), ())
        self.assertEqual(get_mode_defences(""), ())
        self.assertEqual(get_mode_defences(None), ())

    def test_summary_markdown_renders(self) -> None:
        md = mode_validation_summary_markdown()
        self.assertIn("| Mode | Defence | Status |", md)
        self.assertIn("`quant`", md)
        self.assertIn("`saas`", md)
        self.assertIn("`agent`", md)
        self.assertIn("`scientist`", md)

    def test_no_mode_missing_cross_reference(self) -> None:
        # Round 3 final: every mode must declare cross-reference as active.
        for mode, matrix in MODE_VALIDATION_MATRIX.items():
            cr = next((d for d in matrix.defences if d.name == "cross_reference"), None)
            self.assertIsNotNone(cr, msg=f"Mode {mode!r} missing cross_reference layer")
            assert cr is not None
            self.assertEqual(cr.status, "active", msg=f"Mode {mode!r} cross_reference status != active")


# ─── SaaS H001 ──────────────────────────────────────────────────────────────


class TestSaasLint(unittest.TestCase):
    def test_h001_fires_when_fastapi_undeclared(self) -> None:
        files = [
            ("app.py", "from fastapi import FastAPI\napp = FastAPI()\n"),
            ("requirements.txt", "uvicorn==0.30\n"),
        ]
        issues = analyse_saas_lint_from_files(files)
        rules = [i.rule for i in issues]
        self.assertIn("H001-web-framework-undeclared", rules)

    def test_h001_silent_when_declared(self) -> None:
        files = [
            ("app.py", "from fastapi import FastAPI\napp = FastAPI()\n"),
            ("requirements.txt", "fastapi==0.110\nuvicorn==0.30\n"),
        ]
        issues = analyse_saas_lint_from_files(files)
        self.assertEqual(issues, [])

    def test_h001_silent_when_pyproject_declares(self) -> None:
        files = [
            ("app.py", "import fastapi\napp = fastapi.FastAPI()\n"),
            (
                "pyproject.toml",
                "[project]\n"
                "name = 'x'\n"
                "dependencies = ['fastapi==0.110']\n",
            ),
        ]
        # Naive substring search would find 'fastapi' in pyproject.
        issues = analyse_saas_lint_from_files(files)
        h001 = [i for i in issues if i.rule == "H001-web-framework-undeclared"]
        self.assertEqual(h001, [])

    def test_h001_silent_when_no_web_framework_imported(self) -> None:
        files = [
            ("app.py", "print('hello')\n"),
            ("requirements.txt", ""),
        ]
        issues = analyse_saas_lint_from_files(files)
        self.assertEqual(issues, [])


# ─── Agent A001 / A002 ──────────────────────────────────────────────────────


class TestAgentLint(unittest.TestCase):
    def test_a001_fires_when_role_missing(self) -> None:
        files = [
            (
                "agents.py",
                "from crewai import Agent\n"
                "agent = Agent(goal='do x')\n",
            ),
        ]
        issues = analyse_agent_lint_from_files(files)
        rules = [i.rule for i in issues]
        self.assertIn("A001-agent-missing-required-kwargs", rules)

    def test_a001_silent_with_all_required(self) -> None:
        files = [
            (
                "agents.py",
                "from crewai import Agent\n"
                "agent = Agent(role='r', goal='g', backstory='b')\n",
            ),
        ]
        issues = analyse_agent_lint_from_files(files)
        a001 = [i for i in issues if i.rule == "A001-agent-missing-required-kwargs"]
        self.assertEqual(a001, [])

    def test_a001_silent_with_kwargs_unpack(self) -> None:
        # **kwargs unpack — can't statically check.
        files = [
            (
                "agents.py",
                "from crewai import Agent\n"
                "def make(d):\n"
                "    return Agent(**d)\n",
            ),
        ]
        issues = analyse_agent_lint_from_files(files)
        a001 = [i for i in issues if i.rule == "A001-agent-missing-required-kwargs"]
        self.assertEqual(a001, [])

    def test_a002_fires_for_tool_subclass_no_description(self) -> None:
        files = [
            (
                "tools.py",
                "from crewai_tools import BaseTool\n"
                "class MyTool(BaseTool):\n"
                "    name: str = 'mytool'\n"
                "    def _run(self, q): return q\n",
            ),
        ]
        issues = analyse_agent_lint_from_files(files)
        rules = [i.rule for i in issues]
        self.assertIn("A002-tool-missing-description", rules)

    def test_a002_silent_when_description_present(self) -> None:
        files = [
            (
                "tools.py",
                "from crewai_tools import BaseTool\n"
                "class MyTool(BaseTool):\n"
                "    name: str = 'mytool'\n"
                "    description: str = 'does the thing'\n"
                "    def _run(self, q): return q\n",
            ),
        ]
        issues = analyse_agent_lint_from_files(files)
        a002 = [i for i in issues if i.rule == "A002-tool-missing-description"]
        self.assertEqual(a002, [])


# ─── Scientist S001 / S002 ──────────────────────────────────────────────────


class TestScientistLint(unittest.TestCase):
    def test_s001_fires_when_random_forest_no_seed(self) -> None:
        files = [
            ("requirements.txt", "scikit-learn==1.5\n"),
            (
                "train.py",
                "from sklearn.ensemble import RandomForestClassifier\n"
                "clf = RandomForestClassifier(n_estimators=100)\n",
            ),
        ]
        issues = analyse_scientist_lint_from_files(files)
        rules = [i.rule for i in issues]
        self.assertIn("S001-numeric-without-seed", rules)

    def test_s001_silent_with_random_state(self) -> None:
        files = [
            ("requirements.txt", "scikit-learn==1.5\n"),
            (
                "train.py",
                "from sklearn.ensemble import RandomForestClassifier\n"
                "clf = RandomForestClassifier(n_estimators=100, random_state=42)\n",
            ),
        ]
        issues = analyse_scientist_lint_from_files(files)
        s001 = [i for i in issues if i.rule == "S001-numeric-without-seed"]
        self.assertEqual(s001, [])

    def test_s002_fires_when_no_requirements_manifest(self) -> None:
        files = [
            (
                "train.py",
                "import numpy as np\n"
                "x = np.array([1, 2, 3])\n",
            ),
        ]
        issues = analyse_scientist_lint_from_files(files)
        rules = [i.rule for i in issues]
        self.assertIn("S002-missing-requirements", rules)

    def test_s002_silent_with_pyproject_present(self) -> None:
        files = [
            ("pyproject.toml", "[project]\nname = 'x'\n"),
            ("train.py", "x = 1\n"),
        ]
        issues = analyse_scientist_lint_from_files(files)
        s002 = [i for i in issues if i.rule == "S002-missing-requirements"]
        self.assertEqual(s002, [])

    def test_s002_silent_with_environment_yml(self) -> None:
        files = [
            ("environment.yml", "name: env\n"),
            ("train.py", "x = 1\n"),
        ]
        issues = analyse_scientist_lint_from_files(files)
        s002 = [i for i in issues if i.rule == "S002-missing-requirements"]
        self.assertEqual(s002, [])


if __name__ == "__main__":
    unittest.main()
