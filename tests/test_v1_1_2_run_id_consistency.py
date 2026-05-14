"""v1.1.2 — ``run_meta.json`` ↔ Run Insights ledger run_id consistency pins.

Background
==========
Before v1.1.2 the pipeline carried two independent run_ids:

* ``crucible/__main__.py`` / ``run_crucible_enhanced.py:main()`` bound a
  run-correlation ContextVar to either the WebUI-bridged
  ``CRUCIBLE_RUN_ID`` env var (8-char hex) or a freshly generated UUID4.
  Every Run Insights ledger row picked up this value via ``_get_run_id()``
  (with ``CRUCIBLE_RUN_ID`` as the explicit fallback) so the JSONL streams
  recorded the correct bridged id.
* ``section_07_selfcheck_output_main.py`` then **ignored** that ContextVar
  and called ``uuid.uuid4().hex`` to mint a brand-new 32-char id, which it
  stored under ``run_meta.json["run_id"]``.

The two ids never matched.  v1.2.0 retrieval — which joins ``run_meta.json``
to ``.crucible_insights/*.jsonl`` on run_id — could not associate a stored
project with its own Stage 0 debate rejections, defeating the whole
evomap-style "avoid past failure directions" feature for any run that went
through the WebUI bridge.

The fix at ``section_07_selfcheck_output_main.py:1360`` reuses the bridged
id (ContextVar → env var → freshly generated 8-char) instead of always
generating a fresh 32-char one.  The tests below pin both the resolution
behaviour and the structural invariants that downstream consumers depend
on (CLAUDE.md § 9.6 producer→consumer wiring pattern).

A note on test isolation
------------------------
The run-correlation ContextVar is process-global.  ``set_run_id`` does
**not** return a reset token, so every test that mutates it MUST restore
the prior value via ``monkeypatch.setattr`` on the underlying ContextVar
or capture / re-apply the original via ``set_run_id(original)``.  Forgetting
to restore leaks state into sibling tests and produces order-dependent
failures that are hard to reproduce.
"""
from __future__ import annotations

import inspect
import os
import re
import uuid

import pytest

from crucible import run_correlation
from crucible.modules import section_07_selfcheck_output_main as section_07


# ── helpers ────────────────────────────────────────────────────────────────


def _clear_contextvar() -> None:
    """Force the run-correlation ContextVar to the unbound state.

    ``set_run_id("")`` is a trap: ``rid = run_id or str(uuid.uuid4())`` in
    ``set_run_id`` substitutes a fresh UUID when the argument is falsy, so
    passing ``""`` actually *populates* the ContextVar.  The only reliable
    way to clear it is to ``.set("")`` directly on the private ContextVar.
    """
    run_correlation._RUN_ID.set("")


@pytest.fixture(autouse=True)
def _reset_run_correlation(monkeypatch):
    """Snapshot/restore the run-correlation ContextVar around every test.

    Tests in this module mutate the ContextVar and ``CRUCIBLE_RUN_ID``; the
    fixture guarantees no cross-test leakage even when an assertion fails.
    """
    original_id = run_correlation.get_run_id()
    monkeypatch.delenv("CRUCIBLE_RUN_ID", raising=False)
    yield
    if original_id:
        run_correlation.set_run_id(original_id)
    else:
        _clear_contextvar()


def _resolve_run_id_via_section_07_logic() -> str:
    """Re-implement the v1.1.2 resolution logic in isolation.

    Mirrors the exact branch at ``section_07_selfcheck_output_main.py:1360``
    (the producer side of the run_id contract).  The behavioural tests below
    exercise this helper; the structural ``inspect.getsource`` tests assert
    the production code still matches.
    """
    _bridged_run_id = (
        (run_correlation.get_run_id() or "").strip()
        or os.environ.get("CRUCIBLE_RUN_ID", "").strip()
    )
    if _bridged_run_id:
        return _bridged_run_id
    fresh = uuid.uuid4().hex[:8]
    run_correlation.set_run_id(fresh)
    return fresh


# ── behavioural tests ─────────────────────────────────────────────────────


class TestRunIdResolution:
    """The three branches of the v1.1.2 resolution: ContextVar / env / fresh."""

    def test_uses_contextvar_when_set(self):
        run_correlation.set_run_id("ctx12345")
        assert _resolve_run_id_via_section_07_logic() == "ctx12345"

    def test_uses_env_var_when_contextvar_empty(self, monkeypatch):
        # Force ContextVar to "" (unbound state) — must use the direct
        # ContextVar API; set_run_id("") would substitute a fresh UUID.
        _clear_contextvar()
        monkeypatch.setenv("CRUCIBLE_RUN_ID", "envabc12")
        assert _resolve_run_id_via_section_07_logic() == "envabc12"

    def test_contextvar_wins_over_env_when_both_present(self, monkeypatch):
        run_correlation.set_run_id("ctxwins1")
        monkeypatch.setenv("CRUCIBLE_RUN_ID", "envloses")
        # Per CLAUDE.md § 2, the ContextVar is the canonical source; the env
        # var only exists as a bridge for subprocess boundaries.  If both are
        # set, the ContextVar takes precedence — otherwise a stale env var
        # would silently override the freshly bound ContextVar.
        assert _resolve_run_id_via_section_07_logic() == "ctxwins1"

    def test_falls_back_to_eight_char_when_neither_present(self, monkeypatch):
        _clear_contextvar()
        monkeypatch.delenv("CRUCIBLE_RUN_ID", raising=False)
        resolved = _resolve_run_id_via_section_07_logic()
        assert isinstance(resolved, str)
        assert len(resolved) == 8
        # Must be a valid hex slice (matches WebUI's ``uuid.uuid4().hex[:8]``).
        assert re.fullmatch(r"[0-9a-f]{8}", resolved) is not None

    def test_fallback_pins_id_into_contextvar(self, monkeypatch):
        _clear_contextvar()
        monkeypatch.delenv("CRUCIBLE_RUN_ID", raising=False)
        resolved = _resolve_run_id_via_section_07_logic()
        # After the fallback path runs, the ContextVar must carry the same id
        # so downstream emit points within the run see a consistent value
        # (mirrors the production behaviour of the ``_set_run_id(run_id)`` line).
        assert run_correlation.get_run_id() == resolved

    def test_whitespace_only_contextvar_treated_as_empty(self, monkeypatch):
        # v1.1.2 audit fix G1-2: set_run_id("   ") now ``.strip()``s the
        # input and substitutes a fresh UUID for whitespace-only input
        # (the original v1.1.2 implementation only checked truthiness, so
        # "   " landed verbatim in the ContextVar).  We test the
        # section-07 resolver's BEHAVIOUR rather than its specific input:
        # whitespace ContextVar input must NOT block the env-var fallback
        # from taking over.  Directly write the whitespace string via the
        # private ContextVar API to simulate the legacy / cross-process
        # mismatch scenario.
        run_correlation._RUN_ID.set("   ")
        monkeypatch.setenv("CRUCIBLE_RUN_ID", "envfall1")
        # A whitespace-only ContextVar should not block the env-var fallback
        # — otherwise misconfigured tooling could silently pin a blank id.
        assert _resolve_run_id_via_section_07_logic() == "envfall1"

    def test_whitespace_only_env_var_falls_through_to_fresh(self, monkeypatch):
        _clear_contextvar()
        monkeypatch.setenv("CRUCIBLE_RUN_ID", "   ")
        resolved = _resolve_run_id_via_section_07_logic()
        assert len(resolved) == 8


# ── structural pins (CLAUDE.md § 9.6 producer→consumer wiring) ─────────────


class TestSection07ProductionSourceMatchesResolutionContract:
    """Assert the production code at section_07_selfcheck_output_main.py:1360
    still implements the resolution chain.

    These pins catch silent regressions where a future refactor reintroduces
    ``uuid.uuid4().hex`` without the bridge — exactly the v1.1.1→v1.1.2 bug.
    """

    def test_run_id_resolution_block_present_in_main(self):
        src = inspect.getsource(section_07)
        # The ContextVar source must appear before the env-var fallback.
        assert "_bridged_run_id" in src, (
            "section_07 must declare a _bridged_run_id alias so future "
            "auditors can find the v1.1.2 resolution block."
        )
        # Must reference _get_run_id() (ContextVar) — the canonical source.
        ctx_call_pattern = r"_get_run_id\(\)\s*or\s*[\"']{2,}\s*\)\s*\.strip\(\)"
        assert re.search(ctx_call_pattern, src), (
            "section_07 must read the run-correlation ContextVar via "
            "_get_run_id() with a string fallback + .strip() chain."
        )
        # Must reference the CRUCIBLE_RUN_ID env var as the explicit fallback.
        env_call_pattern = (
            r"os\.environ\.get\(\s*[\"']CRUCIBLE_RUN_ID[\"']\s*,\s*[\"']{2,}\s*\)\s*\.strip\(\)"
        )
        assert re.search(env_call_pattern, src), (
            "section_07 must fall back to os.environ.get('CRUCIBLE_RUN_ID', "
            "'').strip() when the ContextVar is empty."
        )

    def test_run_id_fallback_uses_eight_char_slice(self):
        src = inspect.getsource(section_07)
        # Match the exact fallback signature; refuse 32-char ``uuid.uuid4().hex``.
        eight_char_pattern = r"uuid\.uuid4\(\)\.hex\[:8\]"
        assert re.search(eight_char_pattern, src), (
            "section_07 fallback must use uuid.uuid4().hex[:8] to match the "
            "WebUI run_id convention (CLAUDE.md § 2 invariant)."
        )

    def test_run_id_fallback_pins_into_contextvar(self):
        src = inspect.getsource(section_07)
        # The defensive fallback must call _set_run_id so downstream emit
        # points see the freshly generated id — otherwise the ledger would
        # record run_id="" while run_meta.json carries the 8-char id.
        assert "_set_run_id(run_id)" in src, (
            "section_07 fallback path must pin the freshly generated id "
            "into the ContextVar via _set_run_id(run_id)."
        )

    def test_run_snapshot_consumes_bridged_run_id_not_fresh_uuid(self):
        src = inspect.getsource(section_07)
        # Locate the RunSnapshot construction.  The run_id= kwarg MUST come
        # from the local ``run_id`` variable assigned by the resolution
        # block — not from a fresh uuid.uuid4().hex() call inline.
        match = re.search(
            r"run_snapshot\s*=\s*RunSnapshot\(\s*\n\s*run_id\s*=\s*([A-Za-z_][A-Za-z0-9_]*)",
            src,
        )
        assert match is not None, (
            "Failed to locate ``run_snapshot = RunSnapshot(run_id=...)`` in "
            "section_07; the regression test cannot verify the consumer wiring."
        )
        assert match.group(1) == "run_id", (
            "RunSnapshot must consume the locally-resolved run_id variable, "
            "not an inline uuid.uuid4() call.  Found run_id=%r." % match.group(1)
        )

    def test_run_meta_consumes_run_snapshot_run_id(self):
        src = inspect.getsource(section_07)
        # run_meta["run_id"] = run_snapshot.run_id is the wire that
        # propagates the resolved id into run_meta.json.  If this line
        # ever changes to mint a fresh id, the bug returns.
        assert 'run_meta["run_id"] = run_snapshot.run_id' in src, (
            "run_meta['run_id'] must be sourced from run_snapshot.run_id "
            "so the resolved bridged id flows into run_meta.json."
        )


class TestLedgerEmitPointsShareSameRunIdChain:
    """Cross-emit-point consistency: ``record_output_method`` and
    ``record_runtime_params`` must both use the same three-tier fallback
    so they cannot diverge.

    This is the consumer side of the v1.1.2 contract — if section_07 ever
    resolves run_id one way but the ledger emit points resolve it another,
    we get split-brain artefacts again.
    """

    @pytest.mark.parametrize(
        "anchor_kwarg",
        [
            "record_output_method(\n            run_id=(",
            "record_runtime_params(\n            run_id=(",
        ],
    )
    def test_emit_point_uses_three_tier_fallback(self, anchor_kwarg):
        src = inspect.getsource(section_07)
        idx = src.find(anchor_kwarg)
        assert idx >= 0, (
            f"Failed to locate emit-point anchor {anchor_kwarg!r} in section_07."
        )
        # Slice the next ~300 chars and confirm the chain.
        snippet = src[idx : idx + 400]
        assert "_get_run_id()" in snippet, (
            f"{anchor_kwarg!r} emit point missing _get_run_id() as the "
            "primary run_id source."
        )
        assert 'os.environ.get("CRUCIBLE_RUN_ID"' in snippet, (
            f"{anchor_kwarg!r} emit point missing CRUCIBLE_RUN_ID env-var "
            "fallback."
        )
        assert 'run_meta_payload.get("run_id")' in snippet, (
            f"{anchor_kwarg!r} emit point missing run_meta_payload['run_id'] "
            "tertiary fallback (kept as a final defensive layer)."
        )
