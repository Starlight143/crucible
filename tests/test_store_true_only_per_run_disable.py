"""
v1.1.0 fourth-pass (F-9) / fifth-pass (G-1): pin the per-run disable
contract for the three ``_STORE_TRUE_ONLY`` flags (``cache`` /
``strict_json`` / ``cost_trace``).

Before the fourth-pass fix, unchecking any of these in the idea/path
panel had NO effect — the flag has no ``--no-`` CLI form so
``_build_command`` silently dropped it, and the child subprocess
inherited the ``.env`` default (typically ``1``).  UI said "off";
run was still "on".

The fourth-pass routed these flags through env-var override
(``_STORE_TRUE_FLAG_TO_ENV``) but shipped the WRONG env names —
``CRUCIBLE_CACHE`` / ``CRUCIBLE_STRICT_JSON`` / ``CRUCIBLE_COST_TRACE``
do NOT match what the core pipeline actually reads.
``section_07_selfcheck_output_main.py:323-325`` (and mirrors in
sections 02 / 05 / 06) read the un-prefixed names ``LOCAL_CACHE`` /
``STRICT_JSON`` / ``COST_TRACE`` via ``_env.env_bool()``.  So the
fourth-pass test passed because it only checked the mapping was
internally self-consistent — it never verified the RHS keys are
what the pipeline reads.  A textbook "producer is tested, consumer
wiring is not" trap.

v1.1.0 fifth-pass fixes the env names AND adds:

* Per-flag positive / negative tests on the corrected names.
* A producer→consumer integration test that imports the actual
  pipeline read-site identifiers and asserts every mapping RHS
  matches one of them.  This pins the contract structurally — a
  future regression that renames the mapping or the pipeline read
  drifts apart and the test fails immediately.
"""
from __future__ import annotations

import pytest

from webui.app import (
    _STORE_TRUE_FLAG_TO_ENV,
    _resolve_run_insights_env_overrides,
)


# ── G-1 fifth-pass: env names match what the core pipeline actually reads ──
#
# The mapping below is the CONTRACT.  If a future contributor changes the
# mapping RHS, they must also update the pipeline read-site (or the
# integration test below will fail).
_EXPECTED_MAPPING = {
    "cache":       "LOCAL_CACHE",
    "strict_json": "STRICT_JSON",
    "cost_trace":  "COST_TRACE",
}


def test_mapping_matches_expected_env_names():
    """The fourth-pass shipped ``CRUCIBLE_*`` prefixes that don't match
    pipeline reads.  Fifth-pass corrected to the bare legacy names."""
    assert _STORE_TRUE_FLAG_TO_ENV == _EXPECTED_MAPPING, (
        f"mapping drifted from expected pipeline read names: "
        f"got {_STORE_TRUE_FLAG_TO_ENV}, expected {_EXPECTED_MAPPING}"
    )


@pytest.mark.parametrize(
    "flag_key,env_key",
    [
        ("cache",       "LOCAL_CACHE"),
        ("strict_json", "STRICT_JSON"),
        ("cost_trace",  "COST_TRACE"),
    ],
)
def test_store_true_flag_false_emits_env_zero(flag_key: str, env_key: str):
    """Unchecking the box sets the corresponding env var to ``"0"``,
    overriding any ``.env`` default."""
    out = _resolve_run_insights_env_overrides({flag_key: False})
    assert out.get(env_key) == "0", (
        f"flag {flag_key}=False didn't produce {env_key}=0: {out}"
    )


@pytest.mark.parametrize(
    "flag_key,env_key",
    [
        ("cache",       "LOCAL_CACHE"),
        ("strict_json", "STRICT_JSON"),
        ("cost_trace",  "COST_TRACE"),
    ],
)
def test_store_true_flag_true_emits_env_one(flag_key: str, env_key: str):
    """Checking the box sets the env var to ``"1"`` for symmetry."""
    out = _resolve_run_insights_env_overrides({flag_key: True})
    assert out.get(env_key) == "1", (
        f"flag {flag_key}=True didn't produce {env_key}=1: {out}"
    )


def test_store_true_flag_missing_does_not_override():
    """Missing / None flag inherits parent env — the helper returns
    no entry, so ``_child_env`` keeps the ``.env`` default.
    """
    out = _resolve_run_insights_env_overrides({})
    for env_key in ("LOCAL_CACHE", "STRICT_JSON", "COST_TRACE"):
        assert env_key not in out, (
            f"missing flag produced {env_key} entry: {out}"
        )


def test_run_insights_and_store_true_flags_both_resolved():
    """The renamed-but-still-named helper resolves BOTH the
    run_insights toggles AND the new store-true-only toggles in
    one call.  Verifies the merged behaviour landed in fourth-pass.
    """
    out = _resolve_run_insights_env_overrides({
        "run_insights_enabled": False,
        "cache": False,
        "strict_json": True,
    })
    assert out.get("CRUCIBLE_RUN_INSIGHTS_ENABLED") == "0"
    assert out.get("LOCAL_CACHE") == "0"
    assert out.get("STRICT_JSON") == "1"


# ── C-H3 fifth-pass: producer → consumer wiring contract ────────────────────
#
# These tests pin the structural property "the env names this UI helper
# writes are the SAME env names the core pipeline reads."  Without them,
# the F-9 fix could silently regress again (as it did between v1.1.0
# fourth-pass and fifth-pass) and the existing self-consistency tests
# above would not catch it.
def test_mapping_rhs_keys_match_actual_pipeline_reads():
    """The RHS of every ``_STORE_TRUE_FLAG_TO_ENV`` entry MUST appear
    verbatim as an ``env_bool()`` argument somewhere in the core
    pipeline.  If a contributor renames the mapping but forgets to
    update the pipeline (or vice versa), this test fails with a
    diff between the expected and observed name sets.
    """
    import re
    from pathlib import Path

    project_root = Path(__file__).resolve().parents[1]
    pipeline_files = [
        project_root / "crucible" / "modules" / "section_02_research_and_llm.py",
        project_root / "crucible" / "modules" / "section_05_analysis_and_codegen.py",
        project_root / "crucible" / "modules" / "section_06_runtime_quality_api.py",
        project_root / "crucible" / "modules" / "section_07_selfcheck_output_main.py",
    ]

    # Collect every (env_bool|env_int|env_str)("NAME", ...) keyword name.
    env_arg_re = re.compile(
        r"_env_bool\(\s*[\"']([A-Z][A-Z0-9_]+)[\"']"
        r"|env_bool\(\s*[\"']([A-Z][A-Z0-9_]+)[\"']"
    )
    found_names: set[str] = set()
    for path in pipeline_files:
        if not path.exists():
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        for match in env_arg_re.finditer(text):
            name = match.group(1) or match.group(2) or ""
            if name:
                found_names.add(name)

    missing = []
    for flag_key, env_key in _STORE_TRUE_FLAG_TO_ENV.items():
        if env_key not in found_names:
            missing.append(
                f"{flag_key!r} → {env_key!r} (not read by any pipeline section)"
            )
    assert not missing, (
        "F-9 producer→consumer drift: mapping writes env names that the "
        "core pipeline never reads.  This is exactly the bug the v1.1.0 "
        "fifth-pass G-1 fix repaired — DO NOT regress.  Missing reads:\n  "
        + "\n  ".join(missing)
        + f"\n\nPipeline reads observed: {sorted(found_names)}"
    )


def test_run_worker_source_merges_env_overrides_into_child_env():
    """Structural check: ``_run_worker``'s source code merges
    ``env_overrides`` into ``_child_env`` BEFORE the ``subprocess.Popen``
    call.  Failing this means a refactor dropped the merge loop and
    every per-run flag toggle silently no-ops — exactly the F-9
    regression the v1.1.0 fifth-pass G-1 fix addressed.

    We deliberately check the SOURCE rather than fake out Popen
    because ``_run_worker`` does a lot more than spawn — pipes,
    stage timing, AWAIT_INPUT detection — and a partial mock either
    leaks (raises elsewhere) or silently no-ops the merge before
    Popen runs.  Source-level check is simpler and pins the exact
    invariant.
    """
    import inspect
    from webui.app import _run_worker

    source = inspect.getsource(_run_worker)
    # Must reference ``env_overrides`` (parameter name).
    assert "env_overrides" in source, (
        "_run_worker no longer takes env_overrides — F-9 regressed"
    )
    # Must contain a loop that iterates env_overrides items.
    assert (
        "env_overrides.items()" in source
        or "env_overrides.keys()" in source
        or "for k, v in env_overrides" in source
    ), (
        "_run_worker no longer merges env_overrides into _child_env — F-9 regressed"
    )
    # Must NEVER let env_overrides overwrite CRUCIBLE_RUN_ID (correlation id).
    assert "CRUCIBLE_RUN_ID" in source and "continue" in source, (
        "_run_worker no longer protects CRUCIBLE_RUN_ID from env_overrides "
        "— correlation id can now be smashed by per-run flags"
    )
