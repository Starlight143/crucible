"""
tests/regression/test_regression_runner.py
===========================================
Cross-run regression test suite for the Crucible pipeline.

Purpose
-------
Detects quality regressions across pipeline versions by asserting that
completed run outputs satisfy a set of golden constraints defined in
``golden_constraints.json``.

Two complementary test approaches are provided:

1. **Golden-file tests** (``TestGoldenConstraints``) — load previously
   completed run outputs and verify they still satisfy the defined
   constraint ranges.  These tests are intended to run in CI after every
   code change to detect regressions in output quality.

2. **Schema / structural tests** (``TestRunOutputSchema``) — verify that
   key output files (``analysis_result.json``, ``run_meta.json``, etc.)
   conform to the expected structure.  These run against ANY run directory
   found under ``saved_projects/`` and catch regressions in output shape
   (e.g. a field being renamed or removed).

Constraint schema
-----------------
Each entry in ``golden_constraints.json`` may include:

``score_min``               (number)   — analysis score ≥ this value
``score_max``               (number)   — analysis score ≤ this value
``risk_level_allowed``      (list[str])— risk_level must be in this list
``gate_decision_must_be``   (list[str])— gate_decision must match one of these
``gate_decision_must_not_be``(list[str])— gate_decision must NOT be any of these
``blocking_risks_max``      (int)      — number of blocking risks ≤ this value
``experiments_min``         (int)      — number of experiments ≥ this value
``has_consensus``           (bool)     — analysis must have non-empty consensus
``has_direction``           (bool)     — run_snapshot must have a direction

Running the regression suite
-----------------------------
pytest tests/regression/ -v

To target a specific run directory::

    REGRESSION_RUN_DIR=/path/to/run pytest tests/regression/ -v
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

# ── Project root detection ────────────────────────────────────────────────────

_HERE = Path(__file__).resolve().parent                  # tests/regression/
_TESTS_DIR = _HERE.parent                                # tests/
_REPO_ROOT = _TESTS_DIR.parent                           # repo root

if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

_SAVED_PROJECTS_DIR = _REPO_ROOT / "saved_projects"
_GOLDEN_CONSTRAINTS_PATH = _HERE / "golden_constraints.json"

# ── Helpers ───────────────────────────────────────────────────────────────────

def _load_json(path: "Path | str") -> Dict[str, Any]:
    p = Path(path)
    if not p.is_file():
        return {}
    try:
        with p.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError):
        return {}


def _load_golden_constraints() -> List[Dict[str, Any]]:
    data = _load_json(_GOLDEN_CONSTRAINTS_PATH)
    examples = data.get("examples") or []
    # Filter out the placeholder example
    return [
        e for e in examples
        if isinstance(e, dict)
        and e.get("run_id") != "EXAMPLE_RUN_ID"
    ]


def _find_run_dirs() -> List[Path]:
    """Return all valid run directories under saved_projects/."""
    if not _SAVED_PROJECTS_DIR.is_dir():
        return []
    dirs = []
    for entry in _SAVED_PROJECTS_DIR.iterdir():
        if entry.is_dir() and (entry / "analysis_result.json").is_file():
            dirs.append(entry)
    return sorted(dirs)


def _find_run_dir_for_id(run_id: str) -> Optional[Path]:
    """Find a run directory matching *run_id* (exact or suffix match)."""
    for d in _find_run_dirs():
        if d.name == run_id or d.name.endswith(run_id):
            return d
    return None


# ── Constraint verifier ───────────────────────────────────────────────────────

class ConstraintViolation(Exception):
    """Raised when a golden constraint is violated."""


def _verify_constraints(
    run_dir: Path,
    constraints: Dict[str, Any],
    run_id: str,
) -> None:
    """
    Assert all *constraints* against the run output in *run_dir*.

    Raises ConstraintViolation with a descriptive message on the first
    violation.
    """
    analysis = _load_json(run_dir / "analysis_result.json")
    snapshot = _load_json(run_dir / "run_snapshot.json")

    errors: List[str] = []

    # Score bounds
    score = analysis.get("score")
    if "score_min" in constraints:
        min_val = constraints["score_min"]
        if score is None:
            errors.append(f"score is None; expected ≥ {min_val}")
        elif float(score) < float(min_val):
            errors.append(f"score {score} < min {min_val}")

    if "score_max" in constraints:
        max_val = constraints["score_max"]
        if score is not None and float(score) > float(max_val):
            errors.append(f"score {score} > max {max_val}")

    # Risk level
    risk = str(analysis.get("risk_level") or "").lower()
    if "risk_level_allowed" in constraints:
        allowed = [r.lower() for r in constraints["risk_level_allowed"]]
        if risk not in allowed:
            errors.append(f"risk_level '{risk}' not in allowed {allowed}")

    # Gate decision
    gate = str(analysis.get("gate_decision") or "").lower()
    if "gate_decision_must_be" in constraints:
        must_be = [g.lower() for g in constraints["gate_decision_must_be"]]
        if gate not in must_be:
            errors.append(f"gate_decision '{gate}' not in required {must_be}")

    if "gate_decision_must_not_be" in constraints:
        forbidden = [g.lower() for g in constraints["gate_decision_must_not_be"]]
        if gate in forbidden:
            errors.append(f"gate_decision '{gate}' is in forbidden list {forbidden}")

    # Blocking risks
    risks_raw = list(analysis.get("blocking_risks") or [])
    if not risks_raw:
        gate_snap = analysis.get("gate_context_snapshot") or {}
        if isinstance(gate_snap, dict):
            risks_raw = list(gate_snap.get("blocking_risks") or [])
    if "blocking_risks_max" in constraints:
        max_risks = int(constraints["blocking_risks_max"])
        if len(risks_raw) > max_risks:
            errors.append(
                f"blocking_risks count {len(risks_raw)} > max {max_risks}"
            )

    # Experiments
    experiments = list(analysis.get("experiments") or [])
    if "experiments_min" in constraints:
        min_exp = int(constraints["experiments_min"])
        if len(experiments) < min_exp:
            errors.append(
                f"experiments count {len(experiments)} < min {min_exp}"
            )

    # Consensus presence
    if constraints.get("has_consensus"):
        consensus = str(analysis.get("consensus") or "").strip()
        if not consensus:
            errors.append("consensus is empty; expected non-empty")

    # Direction presence
    if constraints.get("has_direction"):
        dd = snapshot.get("direction_decision") or {}
        direction = None
        if isinstance(dd, dict):
            direction = dd.get("selected_direction") or dd.get("direction")
        if not direction:
            errors.append("direction_decision.selected_direction is missing")

    if errors:
        raise ConstraintViolation(
            f"Run '{run_id}' violated {len(errors)} constraint(s):\n"
            + "\n".join(f"  - {e}" for e in errors)
        )


# ── Golden-file tests ─────────────────────────────────────────────────────────

_GOLDEN = _load_golden_constraints()
_GOLDEN_IDS = [entry.get("run_id", f"entry_{i}") for i, entry in enumerate(_GOLDEN)]


@pytest.mark.skipif(
    not _GOLDEN,
    reason="No golden constraints defined in golden_constraints.json",
)
@pytest.mark.parametrize("entry,run_id", zip(_GOLDEN, _GOLDEN_IDS), ids=_GOLDEN_IDS)
def test_golden_constraint(entry: Dict[str, Any], run_id: str) -> None:
    """Assert that a previously completed run still satisfies its golden constraints."""
    run_dir = _find_run_dir_for_id(run_id)
    if run_dir is None:
        pytest.skip(
            f"Run directory for '{run_id}' not found under saved_projects/. "
            "Golden constraint tests require the run to have been completed locally."
        )

    constraints = entry.get("constraints") or {}
    if not constraints:
        pytest.skip(f"No constraints defined for run '{run_id}'")

    try:
        _verify_constraints(run_dir, constraints, run_id)
    except ConstraintViolation as exc:
        pytest.fail(str(exc))


# ── Schema / structural tests ─────────────────────────────────────────────────

class TestRunOutputSchema:
    """
    Structural tests that verify every run output in saved_projects/ conforms
    to the expected JSON schema.  These catch regressions caused by renaming
    or removing fields from pipeline outputs.
    """

    @pytest.fixture(
        params=[
            pytest.param(d, id=d.name)
            for d in _find_run_dirs()
        ]
        if _find_run_dirs()
        else [pytest.param(None, id="no_runs")],
    )
    def run_dir(self, request: pytest.FixtureRequest) -> Optional[Path]:
        return request.param

    @pytest.mark.skipif(
        not _find_run_dirs(),
        reason="No completed runs found in saved_projects/",
    )
    def test_analysis_result_has_required_fields(self, run_dir: Path) -> None:
        """analysis_result.json must contain at minimum: project_name when non-empty."""
        if run_dir is None:
            pytest.skip("No runs available")
        data = _load_json(run_dir / "analysis_result.json")
        # _load_json returns {} for null, non-dict, or parse errors.
        # Runs with null/empty analysis_result.json are code-only runs — skip them.
        if not data:
            pytest.skip(
                f"{run_dir.name}: analysis_result.json is empty or null "
                "(code-only run — no analysis to validate)"
            )
        assert isinstance(data, dict), f"{run_dir.name}: analysis_result.json not a dict"
        for field in ("project_name",):
            assert field in data, (
                f"{run_dir.name}: analysis_result.json missing required field '{field}'"
            )

    @pytest.mark.skipif(
        not _find_run_dirs(),
        reason="No completed runs found in saved_projects/",
    )
    def test_run_meta_has_timestamp(self, run_dir: Path) -> None:
        """run_meta.json must have a timestamp field when present."""
        if run_dir is None:
            pytest.skip("No runs available")
        meta_path = run_dir / "run_meta.json"
        if not meta_path.is_file():
            pytest.skip(f"{run_dir.name}: run_meta.json not present (optional)")
        data = _load_json(meta_path)
        assert isinstance(data, dict), f"{run_dir.name}: run_meta.json not a dict"
        assert data.get("timestamp"), (
            f"{run_dir.name}: run_meta.json missing or empty 'timestamp'"
        )

    @pytest.mark.skipif(
        not _find_run_dirs(),
        reason="No completed runs found in saved_projects/",
    )
    def test_score_is_numeric_when_present(self, run_dir: Path) -> None:
        """analysis score, if present, must be numeric and in [0, 100]."""
        if run_dir is None:
            pytest.skip("No runs available")
        data = _load_json(run_dir / "analysis_result.json")
        score = data.get("score")
        if score is None:
            return  # score is optional; other tests check for presence

        try:
            s = float(score)
        except (TypeError, ValueError):
            pytest.fail(
                f"{run_dir.name}: score={score!r} is not numeric"
            )
        assert 0.0 <= s <= 100.0, (
            f"{run_dir.name}: score={s} is outside valid range [0, 100]"
        )

    @pytest.mark.skipif(
        not _find_run_dirs(),
        reason="No completed runs found in saved_projects/",
    )
    def test_blocking_risks_is_list(self, run_dir: Path) -> None:
        """blocking_risks, if present in analysis_result.json, must be a list."""
        if run_dir is None:
            pytest.skip("No runs available")
        data = _load_json(run_dir / "analysis_result.json")
        risks = data.get("blocking_risks")
        if risks is not None:
            assert isinstance(risks, list), (
                f"{run_dir.name}: blocking_risks must be a list, got {type(risks).__name__}"
            )


# ── Standalone helper: register golden constraints for a run ──────────────────

def add_golden_constraint(
    run_id: str,
    *,
    score_min: Optional[float] = None,
    score_max: Optional[float] = None,
    risk_level_allowed: Optional[List[str]] = None,
    gate_decision_must_not_be: Optional[List[str]] = None,
    blocking_risks_max: Optional[int] = None,
    experiments_min: Optional[int] = None,
    has_consensus: Optional[bool] = None,
    has_direction: Optional[bool] = None,
    project_name: str = "",
) -> None:
    """
    Add or update a golden constraint entry for *run_id*.

    Reads and rewrites ``golden_constraints.json`` atomically.

    Usage::

        from tests.regression.test_regression_runner import add_golden_constraint
        add_golden_constraint(
            "20240401_120000_my_project",
            score_min=60,
            risk_level_allowed=["low", "medium"],
            gate_decision_must_not_be=["kill"],
        )
    """
    import tempfile

    path = _GOLDEN_CONSTRAINTS_PATH
    existing: Dict[str, Any] = {}
    if path.is_file():
        try:
            with path.open("r", encoding="utf-8") as fh:
                existing = json.load(fh)
        except (OSError, json.JSONDecodeError):
            pass

    examples: List[Dict[str, Any]] = existing.get("examples") or []

    # Find and remove existing entry for this run_id
    examples = [e for e in examples if e.get("run_id") != run_id]

    constraints: Dict[str, Any] = {}
    if score_min is not None:
        constraints["score_min"] = score_min
    if score_max is not None:
        constraints["score_max"] = score_max
    if risk_level_allowed is not None:
        constraints["risk_level_allowed"] = risk_level_allowed
    if gate_decision_must_not_be is not None:
        constraints["gate_decision_must_not_be"] = gate_decision_must_not_be
    if blocking_risks_max is not None:
        constraints["blocking_risks_max"] = blocking_risks_max
    if experiments_min is not None:
        constraints["experiments_min"] = experiments_min
    if has_consensus is not None:
        constraints["has_consensus"] = has_consensus
    if has_direction is not None:
        constraints["has_direction"] = has_direction

    examples.append({
        "run_id": run_id,
        "project_name": project_name,
        "constraints": constraints,
    })

    out = {
        "_comment": existing.get("_comment", "Golden constraints for cross-run regression testing."),
        "_description": existing.get("_description", ""),
        "examples": examples,
    }

    # Atomic write via temp file + rename
    fd, tmp_path = tempfile.mkstemp(
        dir=str(path.parent), prefix=".golden_", suffix=".json"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(out, fh, indent=2, ensure_ascii=False)
        os.replace(tmp_path, str(path))
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
