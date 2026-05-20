"""v1.1.8 extended Phase 2 — Tests for 3 new ledger EventKinds + record_*
methods (provider_cooldown_engaged, provider_health_summary,
direction_debate_degraded_proceed).

Coverage:

* Stream mapping: cooldown→error, health→output, degraded→debate.
* ``_VALID_STREAMS`` invariant: still 4 streams (no new file).
* Per-stream toggle respected (cooldown via RECORD_ERRORS,
  health via RECORD_OUTPUT, degraded via RECORD_DEBATE).
* Master switch (RUN_INSIGHTS_ENABLED) respected.
* Swallow contract: backend failure does not raise.
* Payload shape matches recorder docstring.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import List
from unittest.mock import patch

import pytest

from crucible.features.run_insights import get_recorder, reset_recorder
from crucible.features.run_insights.schema import EventKind, InsightEvent


def _read_jsonl(path: Path) -> List[dict]:
    if not path.exists():
        return []
    out: List[dict] = []
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                out.append(json.loads(line))
    return out


@pytest.fixture
def isolated_recorder(tmp_path, monkeypatch):
    """Fresh recorder pointed at a temp dir; per-stream toggles all ON."""
    monkeypatch.setenv("CRUCIBLE_RUN_INSIGHTS_ENABLED", "1")
    monkeypatch.setenv("CRUCIBLE_RUN_INSIGHTS_RECORD_OUTPUT", "1")
    monkeypatch.setenv("CRUCIBLE_RUN_INSIGHTS_RECORD_ERRORS", "1")
    monkeypatch.setenv("CRUCIBLE_RUN_INSIGHTS_RECORD_DEBATE", "1")
    monkeypatch.setenv("CRUCIBLE_RUN_INSIGHTS_DIR", str(tmp_path / "ledger"))
    monkeypatch.setenv("CRUCIBLE_RUN_INSIGHTS_BACKEND", "local")
    monkeypatch.delenv("CRUCIBLE_RUN_INSIGHTS_RECORD_PARAMS", raising=False)
    reset_recorder()
    yield tmp_path / "ledger"
    reset_recorder()


# ─── Stream mapping ─────────────────────────────────────────────────────────


class TestStreamMapping:
    def test_provider_cooldown_routes_to_error(self) -> None:
        ev = InsightEvent(
            kind=EventKind.PROVIDER_COOLDOWN_ENGAGED,
            stage="librarian_research",
            run_id="r",
            project_name="p",
            mode="Quant",
            signals=[],
            payload={},
            env_fingerprint={},
            outcome={"status": "partial"},
        )
        assert ev.stream_name() == "error"

    def test_provider_health_routes_to_output(self) -> None:
        ev = InsightEvent(
            kind=EventKind.PROVIDER_HEALTH_SUMMARY,
            stage="librarian_research",
            run_id="r",
            project_name="p",
            mode="Quant",
            signals=[],
            payload={},
            env_fingerprint={},
            outcome={"status": "success"},
        )
        assert ev.stream_name() == "output"

    def test_degraded_proceed_routes_to_debate(self) -> None:
        ev = InsightEvent(
            kind=EventKind.DIRECTION_DEBATE_DEGRADED_PROCEED,
            stage="stage0_direction",
            run_id="r",
            project_name="p",
            mode="Quant",
            signals=[],
            payload={},
            env_fingerprint={},
            outcome={"status": "partial"},
        )
        assert ev.stream_name() == "debate"

    def test_no_new_stream_files_introduced(self) -> None:
        """v1.1.8 extended deliberately reuses existing 4 streams —
        ``_STREAM_FILENAMES`` and ``_VALID_STREAMS`` invariants must
        not gain new entries (CLAUDE.md § 11.9)."""
        from crucible.features.run_insights.backends import (
            _STREAM_FILENAMES,
            _VALID_STREAMS,
        )
        assert set(_STREAM_FILENAMES.keys()) == {
            "output", "error", "debate", "params",
        }
        assert set(_VALID_STREAMS) == {
            "output", "error", "debate", "params",
        }


# ─── record_provider_cooldown ───────────────────────────────────────────────


class TestRecordProviderCooldown:
    def test_writes_to_error_jsonl(self, isolated_recorder) -> None:
        r = get_recorder()
        cid = r.record_provider_cooldown(
            run_id="r1",
            project_name="p1",
            mode="Quant",
            provider="websearch",
            cooldown_seconds=60,
            trigger_reason="http_429",
            trigger_count=1,
        )
        assert cid is not None
        events = _read_jsonl(isolated_recorder / "error.jsonl")
        assert len(events) == 1
        ev = events[0]
        assert ev["kind"] == "provider_cooldown_engaged"
        assert ev["payload"]["provider"] == "websearch"
        assert ev["payload"]["cooldown_seconds"] == 60
        assert ev["payload"]["trigger_reason"] == "http_429"
        assert ev["payload"]["trigger_count"] == 1

    def test_record_errors_gate_off_skips(self, isolated_recorder, monkeypatch) -> None:
        monkeypatch.setenv("CRUCIBLE_RUN_INSIGHTS_RECORD_ERRORS", "0")
        reset_recorder()
        r = get_recorder()
        cid = r.record_provider_cooldown(
            run_id="r1", project_name="p1", mode="Quant",
            provider="websearch", cooldown_seconds=60,
            trigger_reason="http_429", trigger_count=1,
        )
        assert cid is None
        events = _read_jsonl(isolated_recorder / "error.jsonl")
        assert events == []

    def test_master_switch_off_skips(self, isolated_recorder, monkeypatch) -> None:
        monkeypatch.setenv("CRUCIBLE_RUN_INSIGHTS_ENABLED", "0")
        reset_recorder()
        r = get_recorder()
        cid = r.record_provider_cooldown(
            run_id="r1", project_name="p1", mode="Quant",
            provider="websearch", cooldown_seconds=60,
            trigger_reason="http_429", trigger_count=1,
        )
        assert cid is None

    def test_swallows_backend_failure(self, isolated_recorder) -> None:
        r = get_recorder()
        # Patch backend.write_event to raise; recorder must swallow.
        with patch.object(
            r._backend, "write_event", side_effect=RuntimeError("disk full"),
        ):
            cid = r.record_provider_cooldown(
                run_id="r1", project_name="p1", mode="Quant",
                provider="websearch", cooldown_seconds=60,
                trigger_reason="http_429", trigger_count=1,
            )
            assert cid is None  # swallowed; pipeline continues


# ─── record_provider_health_summary ─────────────────────────────────────────


class TestRecordProviderHealthSummary:
    def test_writes_to_output_jsonl(self, isolated_recorder) -> None:
        r = get_recorder()
        counters = {
            "websearch": {
                "requests": 5, "ok_200": 3, "rate_limited_429": 1,
                "bot_detected_202": 1, "timeouts": 0, "other_errors": 0,
                "citations_yielded": 7, "cache_hits": 2,
            },
            "arxiv": {
                "requests": 2, "ok_200": 2, "rate_limited_429": 0,
                "bot_detected_202": 0, "timeouts": 0, "other_errors": 0,
                "citations_yielded": 4, "cache_hits": 0,
            },
        }
        cid = r.record_provider_health_summary(
            run_id="r1", project_name="p1", mode="Quant",
            counters=counters,
        )
        assert cid is not None
        events = _read_jsonl(isolated_recorder / "output.jsonl")
        # output.jsonl may contain prior events from setup — find ours by kind.
        ours = [e for e in events if e["kind"] == "provider_health_summary"]
        assert len(ours) == 1
        ev = ours[0]
        assert ev["payload"]["counters"]["websearch"]["requests"] == 5
        assert ev["payload"]["counters"]["arxiv"]["citations_yielded"] == 4
        # Absolute counts live in payload.totals.
        assert ev["payload"]["totals"]["providers"] == 2
        assert ev["payload"]["totals"]["requests"] == 7  # 5 + 2
        assert ev["payload"]["totals"]["ok_200"] == 5    # 3 + 2
        assert ev["payload"]["totals"]["citations_yielded"] == 11  # 7 + 4
        # outcome.score is success rate (clamped 0-1).  5 ok / 7 req ≈ 0.71.
        assert 0.71 < ev["outcome"]["score"] < 0.72

    def test_record_output_gate_off_skips(
        self, isolated_recorder, monkeypatch,
    ) -> None:
        monkeypatch.setenv("CRUCIBLE_RUN_INSIGHTS_RECORD_OUTPUT", "0")
        reset_recorder()
        r = get_recorder()
        cid = r.record_provider_health_summary(
            run_id="r1", project_name="p1", mode="Quant",
            counters={"websearch": {"requests": 1, "citations_yielded": 0}},
        )
        assert cid is None

    def test_empty_counters_still_emits(self, isolated_recorder) -> None:
        r = get_recorder()
        cid = r.record_provider_health_summary(
            run_id="r1", project_name="p1", mode="Quant",
            counters={},
        )
        assert cid is not None
        events = _read_jsonl(isolated_recorder / "output.jsonl")
        ours = [e for e in events if e["kind"] == "provider_health_summary"]
        assert len(ours) == 1
        assert ours[0]["payload"]["counters"] == {}
        # No requests, no success → success_rate = 0.0.
        assert ours[0]["outcome"]["score"] == 0.0
        assert ours[0]["payload"]["totals"]["providers"] == 0
        assert ours[0]["payload"]["totals"]["requests"] == 0

    def test_swallows_backend_failure(self, isolated_recorder) -> None:
        r = get_recorder()
        with patch.object(
            r._backend, "write_event", side_effect=OSError("io error"),
        ):
            cid = r.record_provider_health_summary(
                run_id="r1", project_name="p1", mode="Quant",
                counters={"websearch": {"citations_yielded": 5}},
            )
            assert cid is None


# ─── record_direction_debate_degraded_proceed ───────────────────────────────


class TestRecordDegradedProceed:
    def test_writes_to_debate_jsonl(self, isolated_recorder) -> None:
        r = get_recorder()
        cid = r.record_direction_debate_degraded_proceed(
            run_id="r1", project_name="p1", mode="Quant",
            selected_direction="B",
            original_decision="force_none",
            consecutive_force_none_count=3,
            final_score=0,
            gate_reason="short-listed directions have no defendable structured support",
            attempt=3,
        )
        assert cid is not None
        events = _read_jsonl(isolated_recorder / "debate.jsonl")
        ours = [
            e for e in events
            if e["kind"] == "direction_debate_degraded_proceed"
        ]
        assert len(ours) == 1
        ev = ours[0]
        assert ev["payload"]["selected_direction"] == "B"
        assert ev["payload"]["original_decision"] == "force_none"
        assert ev["payload"]["consecutive_force_none_count"] == 3
        assert ev["payload"]["final_score"] == 0
        assert ev["payload"]["attempt"] == 3

    def test_record_debate_gate_off_skips(
        self, isolated_recorder, monkeypatch,
    ) -> None:
        monkeypatch.setenv("CRUCIBLE_RUN_INSIGHTS_RECORD_DEBATE", "0")
        reset_recorder()
        r = get_recorder()
        cid = r.record_direction_debate_degraded_proceed(
            run_id="r1", project_name="p1", mode="Quant",
            selected_direction="A", original_decision="force_none",
            consecutive_force_none_count=3, final_score=0,
            gate_reason="r", attempt=3,
        )
        assert cid is None

    def test_swallows_backend_failure(self, isolated_recorder) -> None:
        r = get_recorder()
        with patch.object(
            r._backend, "write_event", side_effect=ValueError("bad"),
        ):
            cid = r.record_direction_debate_degraded_proceed(
                run_id="r1", project_name="p1", mode="Quant",
                selected_direction="A", original_decision="force_none",
                consecutive_force_none_count=3, final_score=0,
                gate_reason="r", attempt=3,
            )
            assert cid is None

    def test_outcome_is_partial_not_success(self, isolated_recorder) -> None:
        """Degraded proceed is PARTIAL (low-confidence) — must NOT be
        recorded as SUCCESS so v1.2.0 retrieval can spot these runs as
        less-trustworthy."""
        r = get_recorder()
        r.record_direction_debate_degraded_proceed(
            run_id="r1", project_name="p1", mode="Quant",
            selected_direction="B", original_decision="force_none",
            consecutive_force_none_count=3, final_score=0,
            gate_reason="r", attempt=3,
        )
        events = _read_jsonl(isolated_recorder / "debate.jsonl")
        ours = [
            e for e in events
            if e["kind"] == "direction_debate_degraded_proceed"
        ]
        assert ours[0]["outcome"]["status"] == "partial"


# ─── EventKind enum sanity ──────────────────────────────────────────────────


class TestEventKindEnumExtended:
    """Ensure the three new EventKind values exist with the expected
    string serialisation (used by ledger JSONL readers)."""

    def test_provider_cooldown_value(self) -> None:
        assert EventKind.PROVIDER_COOLDOWN_ENGAGED.value == "provider_cooldown_engaged"

    def test_provider_health_value(self) -> None:
        assert EventKind.PROVIDER_HEALTH_SUMMARY.value == "provider_health_summary"

    def test_degraded_proceed_value(self) -> None:
        assert (
            EventKind.DIRECTION_DEBATE_DEGRADED_PROCEED.value
            == "direction_debate_degraded_proceed"
        )
