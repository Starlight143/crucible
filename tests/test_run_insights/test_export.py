"""
Tests for the export CLI: archive structure, manifest correctness,
checksum integrity.
"""
from __future__ import annotations

import io
import json
import tarfile
from pathlib import Path

import pytest

from crucible.features.run_insights import get_recorder, reset_recorder
from crucible.features.run_insights.export import export_archive


@pytest.fixture
def populated_ledger(tmp_path, monkeypatch):
    # Force every stream toggle on so the fixture works regardless of the
    # operator's local ``.env`` (which may have disabled some streams via
    # ``CRUCIBLE_RUN_INSIGHTS_RECORD_*=0``).  Without these, this fixture
    # would silently emit fewer than 4 events and downstream tests would
    # assert against an incomplete manifest.
    monkeypatch.setenv("CRUCIBLE_RUN_INSIGHTS_ENABLED", "1")
    monkeypatch.setenv("CRUCIBLE_RUN_INSIGHTS_RECORD_OUTPUT", "1")
    monkeypatch.setenv("CRUCIBLE_RUN_INSIGHTS_RECORD_ERRORS", "1")
    monkeypatch.setenv("CRUCIBLE_RUN_INSIGHTS_RECORD_DEBATE", "1")
    monkeypatch.setenv("CRUCIBLE_RUN_INSIGHTS_DIR", str(tmp_path / "ledger"))
    monkeypatch.setenv("CRUCIBLE_RUN_INSIGHTS_RECORD_PARAMS", "auto")
    reset_recorder()
    r = get_recorder()
    r.record_output_method(
        run_id="r1", project_name="p", mode="Quant",
        user_problem="gold",
        run_meta={"llm_provider": "openrouter"},
    )
    r.record_error(
        run_id="r1", project_name="p", mode="Quant",
        stage="codegen", exception_class="TimeoutError",
    )
    r.record_direction_debate_rejection(
        run_id="r1", project_name="p", mode="Quant",
        direction_id="DIR_A", rejection_reason="force_none",
    )
    r.record_runtime_params(
        run_id="r1", project_name="p", mode="Quant",
        run_meta={"llm_provider": "openrouter"},
    )
    yield tmp_path / "ledger"
    reset_recorder()


def test_export_creates_archive(populated_ledger, tmp_path):
    dest = tmp_path / "out.tar.gz"
    manifest = export_archive(populated_ledger, dest)
    assert dest.exists()
    assert manifest["schema_version"] == 1
    assert "exported_at" in manifest
    # Each populated stream should appear in the manifest with line count 1.
    for stream in ("output", "error", "debate", "params"):
        assert stream in manifest["streams"]
        assert manifest["streams"][stream]["lines"] == 1


def test_archive_contains_expected_members(populated_ledger, tmp_path):
    dest = tmp_path / "out.tar.gz"
    export_archive(populated_ledger, dest)
    with tarfile.open(dest, "r:gz") as tar:
        names = set(tar.getnames())
    assert "output.jsonl" in names
    assert "error.jsonl" in names
    assert "debate.jsonl" in names
    assert "params.jsonl" in names
    assert "manifest.json" in names


def test_archive_manifest_inside_matches_returned(populated_ledger, tmp_path):
    dest = tmp_path / "out.tar.gz"
    returned = export_archive(populated_ledger, dest)
    with tarfile.open(dest, "r:gz") as tar:
        f = tar.extractfile("manifest.json")
        assert f is not None
        inside = json.loads(f.read().decode("utf-8"))
    # The bytes inside must match the returned manifest (modulo dict ordering).
    assert inside["streams"] == returned["streams"]
    assert inside["schema_version"] == returned["schema_version"]


def test_export_missing_root_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        export_archive(tmp_path / "no_such_dir", tmp_path / "out.tar.gz")
