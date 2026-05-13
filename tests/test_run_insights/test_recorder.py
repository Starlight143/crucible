"""
Tests for the InsightsRecorder orchestrator: mode-aware params, total-disable
short-circuit, individual flag gating, redaction, and content_id propagation.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from crucible.features.run_insights import (
    get_recorder,
    reset_recorder,
)
from crucible.features.run_insights.backends import LocalJSONLBackend
from crucible.features.run_insights.recorder import (
    InsightsRecorder,
    _resolve_record_params,
)
from crucible.features.run_insights.schema import OutcomeStatus


def _read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    out = []
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            out.append(json.loads(line))
    return out


@pytest.fixture
def isolated_recorder(tmp_path, monkeypatch):
    """Each test gets a fresh recorder pointed at a temp dir.

    Each per-stream toggle is forced on so the fixture works regardless of
    the operator's local ``.env`` (which may have
    ``CRUCIBLE_RUN_INSIGHTS_RECORD_*=0`` set).  Without this, ``record_*``
    calls would silently return ``None`` and the assertions would fail in a
    way that looked like a recorder bug rather than a test-fixture leak.
    """
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


def test_total_disable_returns_null_recorder(tmp_path, monkeypatch):
    monkeypatch.setenv("CRUCIBLE_RUN_INSIGHTS_ENABLED", "0")
    monkeypatch.setenv("CRUCIBLE_RUN_INSIGHTS_DIR", str(tmp_path / "ledger"))
    reset_recorder()
    r = get_recorder()
    # Null recorder has no backend.
    assert getattr(r, "backend", "MISSING") in (None, "MISSING")
    # All emit calls return None and don't crash.
    assert r.record_output_method(
        run_id="x", project_name="p", mode="Quant",
    ) is None
    assert r.record_error(
        run_id="x", project_name="p", mode="Quant",
        stage="s", exception_class="E",
    ) is None
    # No ledger directory should have been created.
    assert not (tmp_path / "ledger").exists()
    reset_recorder()


def test_record_output_method_writes_to_output_stream(isolated_recorder):
    r = get_recorder()
    cid = r.record_output_method(
        run_id="run_x",
        project_name="my_proj",
        mode="Quant",
        user_problem="FTMO 黃金 strategy",
        run_meta={"llm_provider": "openrouter", "model_id": "test-model"},
        validation_verdict="passed",
        outcome_score=0.82,
        outcome_status=OutcomeStatus.SUCCESS,
    )
    assert cid and cid.startswith("sha256:")

    events = _read_jsonl(isolated_recorder / "output.jsonl")
    assert len(events) == 1
    e = events[0]
    assert e["kind"] == "output_method"
    assert e["mode"] == "Quant"
    assert "asset:gold" in e["signals"]
    assert "venue:ftmo" in e["signals"]
    assert e["outcome"]["status"] == "success"
    # success + score≥0.7 → reusability block present
    assert "reusability" in e
    assert e["reusability"]["skill_kind"] == "direction_template"


def test_record_output_low_score_omits_reusability(isolated_recorder):
    r = get_recorder()
    r.record_output_method(
        run_id="run_x", project_name="p", mode="Quant",
        user_problem="gold strategy",
        run_meta={"llm_provider": "openrouter"},
        outcome_score=0.5,
    )
    events = _read_jsonl(isolated_recorder / "output.jsonl")
    assert "reusability" not in events[0]


def test_record_error_writes_to_error_stream(isolated_recorder):
    r = get_recorder()
    cid = r.record_error(
        run_id="run_x",
        project_name="p",
        mode="Quant",
        stage="codegen",
        exception_class="TimeoutError",
        message="LLM call exceeded timeout after 900s",
        retry_count=3,
    )
    assert cid is not None
    events = _read_jsonl(isolated_recorder / "error.jsonl")
    assert len(events) == 1
    assert events[0]["kind"] == "error_record"
    assert events[0]["payload"]["exception_class"] == "TimeoutError"
    assert events[0]["payload"]["retry_count"] == 3


def test_record_direction_debate_rejection_truncates_verdict(isolated_recorder):
    r = get_recorder()
    long_verdict = "x" * 1000
    r.record_direction_debate_rejection(
        run_id="run_x", project_name="p", mode="Quant",
        direction_id="DIR_A",
        rejection_reason="force_none",
        judge_verdict=long_verdict,
    )
    events = _read_jsonl(isolated_recorder / "debate.jsonl")
    assert len(events) == 1
    excerpt = events[0]["payload"]["judge_verdict_excerpt"]
    # 500 char cap (+ optional ellipsis).
    assert len(excerpt) <= 501


def test_runtime_params_quant_records(isolated_recorder, monkeypatch):
    monkeypatch.setenv("CRUCIBLE_RUN_INSIGHTS_RECORD_PARAMS", "auto")
    reset_recorder()
    r = get_recorder()
    r.record_runtime_params(
        run_id="run_x", project_name="p", mode="Quant",
        cli_flags={"input_mode": "idea"},
        run_meta={"llm_provider": "openrouter"},
    )
    events = _read_jsonl(isolated_recorder / "params.jsonl")
    assert len(events) == 1


def test_runtime_params_non_quant_skipped_in_auto(isolated_recorder, monkeypatch):
    monkeypatch.setenv("CRUCIBLE_RUN_INSIGHTS_RECORD_PARAMS", "auto")
    reset_recorder()
    r = get_recorder()
    r.record_runtime_params(
        run_id="run_x", project_name="p", mode="SaaS",
        cli_flags={"input_mode": "idea"},
        run_meta={"llm_provider": "openrouter"},
    )
    events = _read_jsonl(isolated_recorder / "params.jsonl")
    assert len(events) == 0


def test_runtime_params_force_on_non_quant(isolated_recorder, monkeypatch):
    monkeypatch.setenv("CRUCIBLE_RUN_INSIGHTS_RECORD_PARAMS", "1")
    reset_recorder()
    r = get_recorder()
    r.record_runtime_params(
        run_id="run_x", project_name="p", mode="SaaS",
        cli_flags={"input_mode": "idea"},
        run_meta={"llm_provider": "openrouter"},
    )
    events = _read_jsonl(isolated_recorder / "params.jsonl")
    assert len(events) == 1


def test_runtime_params_force_off_quant(isolated_recorder, monkeypatch):
    monkeypatch.setenv("CRUCIBLE_RUN_INSIGHTS_RECORD_PARAMS", "0")
    reset_recorder()
    r = get_recorder()
    r.record_runtime_params(
        run_id="run_x", project_name="p", mode="Quant",
        cli_flags={"input_mode": "idea"},
        run_meta={"llm_provider": "openrouter"},
    )
    events = _read_jsonl(isolated_recorder / "params.jsonl")
    assert len(events) == 0


def test_typo_in_record_params_falls_back_to_auto(monkeypatch):
    """'atuo' (typo) must NOT silently truthy-coerce — must behave as 'auto'."""
    monkeypatch.setenv("CRUCIBLE_RUN_INSIGHTS_RECORD_PARAMS", "atuo")
    assert _resolve_record_params("Quant") is True   # auto → quant=record
    assert _resolve_record_params("SaaS") is False   # auto → non-quant=skip


def test_individual_disable_flags(tmp_path, monkeypatch):
    monkeypatch.setenv("CRUCIBLE_RUN_INSIGHTS_ENABLED", "1")
    monkeypatch.setenv("CRUCIBLE_RUN_INSIGHTS_DIR", str(tmp_path / "ledger"))
    monkeypatch.setenv("CRUCIBLE_RUN_INSIGHTS_RECORD_OUTPUT", "0")
    monkeypatch.setenv("CRUCIBLE_RUN_INSIGHTS_RECORD_ERRORS", "0")
    monkeypatch.setenv("CRUCIBLE_RUN_INSIGHTS_RECORD_DEBATE", "0")
    reset_recorder()
    r = get_recorder()
    assert r.record_output_method(run_id="x", project_name="p", mode="Quant") is None
    assert r.record_error(
        run_id="x", project_name="p", mode="Quant",
        stage="s", exception_class="E",
    ) is None
    assert r.record_direction_debate_rejection(
        run_id="x", project_name="p", mode="Quant",
        direction_id="A", rejection_reason="force_none",
    ) is None
    # Files should not have been created (or be empty).
    for fname in ("output.jsonl", "error.jsonl", "debate.jsonl"):
        events = _read_jsonl(tmp_path / "ledger" / fname)
        assert events == []
    reset_recorder()


def test_content_id_appears_on_persisted_event(isolated_recorder):
    r = get_recorder()
    r.record_error(
        run_id="x", project_name="p", mode="Quant",
        stage="s", exception_class="E",
    )
    events = _read_jsonl(isolated_recorder / "error.jsonl")
    assert events[0]["content_id"].startswith("sha256:")
    assert len(events[0]["content_id"]) == len("sha256:") + 64


def test_env_fingerprint_present(isolated_recorder):
    r = get_recorder()
    r.record_error(
        run_id="x", project_name="p", mode="Quant",
        stage="s", exception_class="E",
        run_meta={"llm_provider": "openrouter", "model_id": "test"},
    )
    events = _read_jsonl(isolated_recorder / "error.jsonl")
    fp = events[0]["env_fingerprint"]
    assert "python_version" in fp
    assert "platform" in fp
    assert fp["model_id"] == "test"
    assert fp["llm_provider"] == "openrouter"
