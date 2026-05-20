"""
v1.1.8 — Tests for the two new ledger event kinds and recorder methods:

* ``EventKind.DIRECTION_DEBATE_FINDING`` + ``record_debate_finding()``
* ``EventKind.DIRECTION_DEBATE_VERDICT`` + ``record_gate_verdict()``

Coverage:

1. **Stream mapping** — both new EventKinds route to the existing ``debate``
   stream (no new stream file created).
2. **Swallow contract** — a backend that raises must NOT propagate.
3. **Mode-aware ``RECORD_DEBATE_FINDING=auto``** — follows
   ``CRUCIBLE_DEBATE_AUDIT_MODE``, not Quant mode.  This is the new
   semantic introduced in v1.1.8 (distinct from ``RECORD_PARAMS=auto``
   which follows Quant mode).
4. **Per-stream toggle ``RECORD_GATE_VERDICT``** — when 0, no verdict
   event is written even when audit_mode is on.
5. **Known rejection_reason set extended** — judge_explicit_kill /
   judge_branch / needs_more_data must NOT trip the one-shot unknown-
   reason log (this is the v1.1.8 extension to the existing
   record_direction_debate_rejection set).
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import List
from unittest.mock import patch

import pytest

from crucible.features.run_insights import get_recorder, reset_recorder
from crucible.features.run_insights.recorder import (
    InsightsRecorder,
    _resolve_record_debate_finding,
)
from crucible.features.run_insights.schema import EventKind, InsightEvent


def _read_jsonl(path: Path) -> List[dict]:
    if not path.exists():
        return []
    out: List[dict] = []
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            out.append(json.loads(line))
    return out


@pytest.fixture
def isolated_recorder(tmp_path, monkeypatch):
    """Fresh recorder pointed at a temp dir; all per-stream toggles ON."""
    monkeypatch.setenv("CRUCIBLE_RUN_INSIGHTS_ENABLED", "1")
    monkeypatch.setenv("CRUCIBLE_RUN_INSIGHTS_RECORD_OUTPUT", "1")
    monkeypatch.setenv("CRUCIBLE_RUN_INSIGHTS_RECORD_ERRORS", "1")
    monkeypatch.setenv("CRUCIBLE_RUN_INSIGHTS_RECORD_DEBATE", "1")
    monkeypatch.setenv("CRUCIBLE_RUN_INSIGHTS_RECORD_DEBATE_FINDING", "1")
    monkeypatch.setenv("CRUCIBLE_RUN_INSIGHTS_RECORD_GATE_VERDICT", "1")
    monkeypatch.setenv("CRUCIBLE_RUN_INSIGHTS_DIR", str(tmp_path / "ledger"))
    monkeypatch.setenv("CRUCIBLE_RUN_INSIGHTS_BACKEND", "local")
    monkeypatch.setenv("CRUCIBLE_DEBATE_AUDIT_MODE", "1")  # ensure auto-mode default writes
    monkeypatch.delenv("CRUCIBLE_RUN_INSIGHTS_RECORD_PARAMS", raising=False)
    reset_recorder()
    yield tmp_path / "ledger"
    reset_recorder()


# ── Stream mapping ───────────────────────────────────────────────────────────


class TestStreamMapping:
    def test_debate_finding_eventkind_routes_to_debate_stream(self) -> None:
        ev = InsightEvent(
            kind=EventKind.DIRECTION_DEBATE_FINDING,
            stage="stage0_direction",
            run_id="r",
            project_name="p",
            mode="Quant",
            signals=[],
            payload={},
            env_fingerprint={},
            outcome={"status": "skipped"},
        )
        assert ev.stream_name() == "debate"

    def test_gate_verdict_eventkind_routes_to_debate_stream(self) -> None:
        ev = InsightEvent(
            kind=EventKind.DIRECTION_DEBATE_VERDICT,
            stage="stage0_direction",
            run_id="r",
            project_name="p",
            mode="Quant",
            signals=[],
            payload={},
            env_fingerprint={},
            outcome={"status": "success"},
        )
        assert ev.stream_name() == "debate"

    def test_no_new_stream_files_introduced(self) -> None:
        """v1.1.8 deliberately reuses ``debate.jsonl`` — _STREAM_FILENAMES
        invariant should not gain new entries."""
        from crucible.features.run_insights.backends import _STREAM_FILENAMES, _VALID_STREAMS
        assert set(_STREAM_FILENAMES.keys()) == {"output", "error", "debate", "params"}
        assert set(_VALID_STREAMS) == {"output", "error", "debate", "params"}


# ── record_debate_finding ────────────────────────────────────────────────────


class TestRecordDebateFinding:
    def test_writes_to_debate_jsonl(self, isolated_recorder) -> None:
        r = get_recorder()
        cid = r.record_debate_finding(
            run_id="r1",
            project_name="p1",
            mode="Quant",
            role="explorer",
            conclusion="Direction A looks promising",
            confidence=0.7,
            assumptions=["assumption A", "assumption B"],
            concerns=[{"severity": "minor", "description": "data is stale"}],
            disagreement_with=[],
        )
        assert cid and cid.startswith("sha256:")

        events = _read_jsonl(isolated_recorder / "debate.jsonl")
        assert len(events) == 1
        e = events[0]
        assert e["kind"] == "direction_debate_finding"
        assert e["payload"]["role"] == "explorer"
        assert e["payload"]["confidence"] == 0.7
        assert e["payload"]["assumptions"] == ["assumption A", "assumption B"]
        assert e["payload"]["concerns"][0]["severity"] == "minor"

    def test_swallows_backend_failures(self, monkeypatch, tmp_path) -> None:
        """v1.1.8 contract: backend exception must NOT break the main pipeline.
        Matches the existing record_direction_debate_rejection swallow pattern."""
        monkeypatch.setenv("CRUCIBLE_RUN_INSIGHTS_ENABLED", "1")
        monkeypatch.setenv("CRUCIBLE_RUN_INSIGHTS_RECORD_DEBATE", "1")
        monkeypatch.setenv("CRUCIBLE_RUN_INSIGHTS_RECORD_DEBATE_FINDING", "1")
        monkeypatch.setenv("CRUCIBLE_RUN_INSIGHTS_DIR", str(tmp_path / "ledger"))
        monkeypatch.setenv("CRUCIBLE_DEBATE_AUDIT_MODE", "1")
        reset_recorder()

        r = get_recorder()
        # Inject a backend that always raises.
        from crucible.features.run_insights.backends import StorageBackend

        class _ExplodingBackend(StorageBackend):
            def write_event(self, stream, record):  # noqa: D401
                raise IOError("simulated disk full")

            def prune_stream(self, stream, max_entries):
                pass

            def read_recent(self, stream, limit=100):
                return []

            def flush(self) -> None:
                pass

            def close(self) -> None:
                pass

        # Swap the backend in place.
        r._backend = _ExplodingBackend()  # type: ignore[attr-defined]
        # Should return None, NOT raise.
        result = r.record_debate_finding(
            run_id="r",
            project_name="p",
            mode="Quant",
            role="judge",
            conclusion="failure simulation",
            confidence=0.5,
        )
        assert result is None
        reset_recorder()

    def test_invalid_confidence_coerced_to_none(self, isolated_recorder) -> None:
        """NaN / out-of-range confidence MUST be rejected (CLAUDE.md §
        numerical-correctness rule), not silently clamped."""
        import math
        r = get_recorder()
        r.record_debate_finding(
            run_id="r",
            project_name="p",
            mode="Quant",
            role="skeptic",
            conclusion="x",
            confidence=float("nan"),
        )
        events = _read_jsonl(isolated_recorder / "debate.jsonl")
        # Confidence stored as None when input is non-finite.
        assert len(events) == 1
        assert events[0]["payload"]["confidence"] is None


# ── _resolve_record_debate_finding semantic ──────────────────────────────────


class TestResolveDebateFinding:
    def test_auto_follows_audit_mode_on(self, monkeypatch) -> None:
        """``auto`` (default) must record when CRUCIBLE_DEBATE_AUDIT_MODE=1."""
        monkeypatch.setenv("CRUCIBLE_RUN_INSIGHTS_RECORD_DEBATE_FINDING", "auto")
        monkeypatch.setenv("CRUCIBLE_DEBATE_AUDIT_MODE", "1")
        assert _resolve_record_debate_finding() is True

    def test_auto_follows_audit_mode_off(self, monkeypatch) -> None:
        """``auto`` must skip when audit_mode is off — findings are useless
        without audit_mode populating the structured fields."""
        monkeypatch.setenv("CRUCIBLE_RUN_INSIGHTS_RECORD_DEBATE_FINDING", "auto")
        monkeypatch.setenv("CRUCIBLE_DEBATE_AUDIT_MODE", "0")
        assert _resolve_record_debate_finding() is False

    def test_explicit_1_always_records(self, monkeypatch) -> None:
        """=1 must override audit_mode and always record."""
        monkeypatch.setenv("CRUCIBLE_RUN_INSIGHTS_RECORD_DEBATE_FINDING", "1")
        monkeypatch.setenv("CRUCIBLE_DEBATE_AUDIT_MODE", "0")
        assert _resolve_record_debate_finding() is True

    def test_explicit_0_never_records(self, monkeypatch) -> None:
        monkeypatch.setenv("CRUCIBLE_RUN_INSIGHTS_RECORD_DEBATE_FINDING", "0")
        monkeypatch.setenv("CRUCIBLE_DEBATE_AUDIT_MODE", "1")
        assert _resolve_record_debate_finding() is False

    def test_typo_falls_back_to_auto(self, monkeypatch) -> None:
        """Typos must fall back to ``auto``, NEVER truthy-coerce.  Matches
        the project env-bool whitelist rule and the runtime_params precedent."""
        monkeypatch.setenv("CRUCIBLE_RUN_INSIGHTS_RECORD_DEBATE_FINDING", "atuo")
        monkeypatch.setenv("CRUCIBLE_DEBATE_AUDIT_MODE", "1")
        assert _resolve_record_debate_finding() is True  # follows audit_mode
        monkeypatch.setenv("CRUCIBLE_DEBATE_AUDIT_MODE", "0")
        assert _resolve_record_debate_finding() is False


# ── record_gate_verdict ──────────────────────────────────────────────────────


class TestRecordGateVerdict:
    def test_writes_proceed_verdict(self, isolated_recorder) -> None:
        r = get_recorder()
        cid = r.record_gate_verdict(
            run_id="r",
            project_name="p",
            mode="Quant",
            decision="PROCEED",
            reason="evidence is sufficient to proceed with direction A",
            selected_direction="A",
        )
        assert cid and cid.startswith("sha256:")
        events = _read_jsonl(isolated_recorder / "debate.jsonl")
        assert len(events) == 1
        e = events[0]
        assert e["kind"] == "direction_debate_verdict"
        assert e["payload"]["decision"] == "PROCEED"
        assert e["payload"]["selected_direction"] == "A"
        assert e["outcome"]["status"] == "success"

    def test_writes_kill_verdict(self, isolated_recorder) -> None:
        r = get_recorder()
        r.record_gate_verdict(
            run_id="r",
            project_name="p",
            mode="Quant",
            decision="KILL",
            reason="hard invariant violated: cointegration on trending asset",
            failed_invariants=["mean-reversion on trending asset is invalid"],
        )
        events = _read_jsonl(isolated_recorder / "debate.jsonl")
        assert len(events) == 1
        e = events[0]
        assert e["payload"]["decision"] == "KILL"
        assert e["payload"]["failed_invariants"] == [
            "mean-reversion on trending asset is invalid"
        ]
        assert e["outcome"]["status"] == "failure"

    def test_writes_needs_more_data_verdict(self, isolated_recorder) -> None:
        r = get_recorder()
        r.record_gate_verdict(
            run_id="r",
            project_name="p",
            mode="Quant",
            decision="NEEDS_MORE_DATA",
            reason="evidence is too thin to commit to direction A",
            blocking_evidence_queries=["fetch binance depth", "verify volume profile"],
        )
        events = _read_jsonl(isolated_recorder / "debate.jsonl")
        e = events[0]
        assert e["payload"]["decision"] == "NEEDS_MORE_DATA"
        assert len(e["payload"]["blocking_evidence_queries"]) == 2
        assert e["outcome"]["status"] == "partial"

    def test_record_gate_verdict_toggle_off_skips_write(
        self, isolated_recorder, monkeypatch
    ) -> None:
        monkeypatch.setenv("CRUCIBLE_RUN_INSIGHTS_RECORD_GATE_VERDICT", "0")
        r = get_recorder()
        cid = r.record_gate_verdict(
            run_id="r",
            project_name="p",
            mode="Quant",
            decision="PROCEED",
            reason="evidence is sufficient to proceed",
            selected_direction="A",
        )
        assert cid is None
        events = _read_jsonl(isolated_recorder / "debate.jsonl")
        assert len(events) == 0

    def test_swallows_backend_failures(self, monkeypatch, tmp_path) -> None:
        monkeypatch.setenv("CRUCIBLE_RUN_INSIGHTS_ENABLED", "1")
        monkeypatch.setenv("CRUCIBLE_RUN_INSIGHTS_RECORD_DEBATE", "1")
        monkeypatch.setenv("CRUCIBLE_RUN_INSIGHTS_RECORD_GATE_VERDICT", "1")
        monkeypatch.setenv("CRUCIBLE_RUN_INSIGHTS_DIR", str(tmp_path / "ledger"))
        reset_recorder()

        from crucible.features.run_insights.backends import StorageBackend

        class _ExplodingBackend(StorageBackend):
            def write_event(self, stream, record):
                raise IOError("simulated")

            def prune_stream(self, stream, max_entries):
                pass

            def read_recent(self, stream, limit=100):
                return []

            def flush(self) -> None:
                pass

            def close(self) -> None:
                pass

        r = get_recorder()
        r._backend = _ExplodingBackend()  # type: ignore[attr-defined]
        result = r.record_gate_verdict(
            run_id="r",
            project_name="p",
            mode="Quant",
            decision="PROCEED",
            reason="ok",
            selected_direction="A",
        )
        assert result is None
        reset_recorder()


# ── Extended rejection_reason set ────────────────────────────────────────────


class TestExtendedKnownRejectionReasons:
    def test_judge_explicit_kill_is_known(self, isolated_recorder, caplog) -> None:
        """v1.1.8 extends recorder.py:329 known set.  Recording with
        ``judge_explicit_kill`` MUST NOT trigger the one-shot unknown-reason
        debug log (which would imply the reason is unrecognised).
        """
        r = get_recorder()
        r.record_direction_debate_rejection(
            run_id="r",
            project_name="p",
            mode="Quant",
            direction_id="A",
            rejection_reason="judge_explicit_kill",
            judge_verdict="explicit invariant violation",
        )
        # _warned_unknown_reason flag should still be False after a recognised
        # reason (since v1.1.8 extends the known set).
        assert getattr(r, "_warned_unknown_reason", False) is False

    def test_needs_more_data_is_known(self, isolated_recorder) -> None:
        r = get_recorder()
        r.record_direction_debate_rejection(
            run_id="r",
            project_name="p",
            mode="Quant",
            direction_id="B",
            rejection_reason="needs_more_data",
            judge_verdict="evidence too thin",
        )
        assert getattr(r, "_warned_unknown_reason", False) is False

    def test_judge_branch_is_known(self, isolated_recorder) -> None:
        r = get_recorder()
        r.record_direction_debate_rejection(
            run_id="r",
            project_name="p",
            mode="Quant",
            direction_id="C",
            rejection_reason="judge_branch",
            judge_verdict="must split",
        )
        assert getattr(r, "_warned_unknown_reason", False) is False
