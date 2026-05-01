"""Tests for the rewritten crucible.features.project_memory (JSONL v2)"""
from __future__ import annotations

import json
import os
import sys
import threading

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
from crucible.features.project_memory import (
    MAX_ENTRIES_PER_PROJECT,
    MemoryEntry,
    ProjectMemoryStore,
    _entry_to_jsonl_line,
    _normalise_name,
    _parse_jsonl_line,
    create_memory_entry_from_output,
)


# ── Fixtures ──────────────────────────────────────────────────────────────────

def _make_entry(
    project: str = "alpha",
    ts: str = "2025-01-01T00:00:00+00:00",
    **kw,
) -> MemoryEntry:
    return MemoryEntry(
        timestamp=ts,
        project_name=project,
        direction_selected=kw.get("direction_selected", "dir_A"),
        direction_summary=kw.get("direction_summary", "summary"),
        score=kw.get("score", 80.0),
        risk_level=kw.get("risk_level", "medium"),
        confirmed_tech_choices=kw.get("confirmed_tech_choices", ["python"]),
        failed_experiments=kw.get("failed_experiments", []),
        blocking_risks=kw.get("blocking_risks", []),
    )


# ── MemoryEntry ────────────────────────────────────────────────────────────────

class TestMemoryEntry:
    def test_to_dict_round_trip(self):
        e = _make_entry()
        d = e.to_dict()
        e2 = MemoryEntry.from_dict(d)
        assert e2.project_name == e.project_name
        assert e2.timestamp == e.timestamp
        assert e2.score == e.score

    def test_from_dict_missing_project_name_raises(self):
        with pytest.raises(ValueError, match="project_name"):
            MemoryEntry.from_dict({"timestamp": "2025-01-01T00:00:00+00:00"})

    def test_from_dict_missing_timestamp_raises(self):
        with pytest.raises(ValueError, match="timestamp"):
            MemoryEntry.from_dict({"project_name": "proj"})

    def test_from_dict_empty_project_name_raises(self):
        with pytest.raises(ValueError, match="project_name"):
            MemoryEntry.from_dict({"project_name": "  ", "timestamp": "2025-01-01"})

    def test_from_dict_empty_timestamp_raises(self):
        with pytest.raises(ValueError, match="timestamp"):
            MemoryEntry.from_dict({"project_name": "p", "timestamp": "  "})

    def test_optional_fields_default_gracefully(self):
        e = MemoryEntry.from_dict({"project_name": "p", "timestamp": "2025-01-01"})
        assert e.direction_selected is None
        assert e.confirmed_tech_choices == []
        assert e.notes == ""


# ── JSONL helpers ─────────────────────────────────────────────────────────────

class TestJsonlHelpers:
    def test_entry_to_jsonl_line_is_single_line(self):
        e = _make_entry()
        line = _entry_to_jsonl_line(e)
        assert "\n" not in line

    def test_parse_jsonl_line_roundtrip(self):
        e = _make_entry()
        line = _entry_to_jsonl_line(e)
        obj = _parse_jsonl_line(line)
        assert obj is not None
        assert obj["project_name"] == "alpha"

    def test_parse_jsonl_line_empty_returns_none(self):
        assert _parse_jsonl_line("   ") is None

    def test_parse_jsonl_line_invalid_json_returns_none(self):
        assert _parse_jsonl_line("{bad json}") is None


# ── ProjectMemoryStore ────────────────────────────────────────────────────────

class TestProjectMemoryStore:
    def test_add_and_get_entries(self, tmp_path):
        store = ProjectMemoryStore(str(tmp_path))
        e = _make_entry("proj1")
        store.add_entry(e)
        entries = store.get_entries("proj1")
        assert len(entries) == 1
        assert entries[0].project_name == "proj1"

    def test_get_entries_empty(self, tmp_path):
        store = ProjectMemoryStore(str(tmp_path))
        assert store.get_entries("nonexistent") == []

    def test_multiple_entries_oldest_first(self, tmp_path):
        store = ProjectMemoryStore(str(tmp_path))
        for i in range(3):
            store.add_entry(_make_entry("proj", ts=f"2025-01-0{i+1}T00:00:00+00:00"))
        entries = store.get_entries("proj")
        assert len(entries) == 3
        assert entries[0].timestamp < entries[1].timestamp < entries[2].timestamp

    def test_project_name_case_insensitive(self, tmp_path):
        store = ProjectMemoryStore(str(tmp_path))
        store.add_entry(_make_entry("MyProject"))
        assert len(store.get_entries("myproject")) == 1
        assert len(store.get_entries("MYPROJECT")) == 1

    def test_multiple_projects_isolated(self, tmp_path):
        store = ProjectMemoryStore(str(tmp_path))
        store.add_entry(_make_entry("alpha"))
        store.add_entry(_make_entry("beta"))
        assert len(store.get_entries("alpha")) == 1
        assert len(store.get_entries("beta")) == 1

    def test_prune_keeps_max_entries(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "crucible.features.project_memory.MAX_ENTRIES_PER_PROJECT", 3
        )
        store = ProjectMemoryStore(str(tmp_path))
        for i in range(5):
            store.add_entry(_make_entry("p", ts=f"2025-01-0{i+1}T00:00:00+00:00"))
        entries = store.get_entries("p")
        assert len(entries) == 3

    def test_jsonl_file_created(self, tmp_path):
        store = ProjectMemoryStore(str(tmp_path))
        store.add_entry(_make_entry("x"))
        assert os.path.isfile(os.path.join(str(tmp_path), "project_memory.jsonl"))

    def test_each_line_is_valid_json(self, tmp_path):
        store = ProjectMemoryStore(str(tmp_path))
        store.add_entry(_make_entry("x"))
        store.add_entry(_make_entry("x", ts="2025-02-01T00:00:00+00:00"))
        with open(os.path.join(str(tmp_path), "project_memory.jsonl"), encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    obj = json.loads(line)
                    assert isinstance(obj, dict)


# ── Legacy migration ──────────────────────────────────────────────────────────

class TestLegacyMigration:
    def test_migrates_legacy_json(self, tmp_path):
        legacy_data = {
            "testproject": [
                {
                    "timestamp": "2024-01-01T00:00:00+00:00",
                    "project_name": "testproject",
                    "direction_selected": "dir_A",
                    "direction_summary": "summary",
                    "score": 70.0,
                    "risk_level": "low",
                    "confirmed_tech_choices": [],
                    "failed_experiments": [],
                    "blocking_risks": [],
                    "notes": "",
                }
            ]
        }
        legacy_path = tmp_path / "project_memory.json"
        legacy_path.write_text(json.dumps(legacy_data), encoding="utf-8")

        store = ProjectMemoryStore(str(tmp_path))
        entries = store.get_entries("testproject")

        assert len(entries) == 1
        assert entries[0].project_name == "testproject"
        # Legacy file should be renamed to .bak
        assert os.path.isfile(str(legacy_path) + ".bak")
        assert not os.path.isfile(str(legacy_path))

    def test_skips_migration_if_jsonl_exists(self, tmp_path):
        jsonl_path = tmp_path / "project_memory.jsonl"
        jsonl_path.write_text("", encoding="utf-8")
        legacy_path = tmp_path / "project_memory.json"
        legacy_data = {"p": [{"timestamp": "2024-01-01", "project_name": "p"}]}
        legacy_path.write_text(json.dumps(legacy_data), encoding="utf-8")

        store = ProjectMemoryStore(str(tmp_path))
        store.get_entries("p")

        # JSONL was already present, legacy should be renamed to .bak, no content merged
        assert os.path.isfile(str(legacy_path) + ".bak")

    def test_corrupted_legacy_json_renamed_to_bak(self, tmp_path):
        legacy_path = tmp_path / "project_memory.json"
        legacy_path.write_text("{bad json}", encoding="utf-8")

        store = ProjectMemoryStore(str(tmp_path))
        store.get_entries("anything")

        assert os.path.isfile(str(legacy_path) + ".bak")


# ── build_context_text / build_memory_prompt_prefix ──────────────────────────

class TestContextText:
    def test_empty_when_no_entries(self, tmp_path):
        store = ProjectMemoryStore(str(tmp_path))
        assert store.build_context_text("proj") == ""
        assert store.build_memory_prompt_prefix("proj") == ""

    def test_returns_non_empty_for_existing(self, tmp_path):
        store = ProjectMemoryStore(str(tmp_path))
        store.add_entry(_make_entry("proj"))
        ctx = store.build_context_text("proj")
        assert "proj" in ctx.lower() or "Project Memory" in ctx

    def test_prompt_prefix_wraps_context(self, tmp_path):
        store = ProjectMemoryStore(str(tmp_path))
        store.add_entry(_make_entry("proj"))
        prefix = store.build_memory_prompt_prefix("proj")
        assert "Historical Project Memory" in prefix

    def test_respects_token_budget(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "crucible.features.project_memory._PROMPT_BUDGET_CHARS", 100
        )
        store = ProjectMemoryStore(str(tmp_path))
        for i in range(10):
            store.add_entry(
                _make_entry("p", ts=f"2025-01-{i+1:02d}T00:00:00+00:00")
            )
        ctx = store.build_context_text("p")
        # The context should be non-empty but bounded
        assert len(ctx) <= 200  # some headroom since header alone may exceed budget


# ── Thread safety ─────────────────────────────────────────────────────────────

class TestThreadSafety:
    def test_concurrent_add_entry(self, tmp_path):
        store = ProjectMemoryStore(str(tmp_path))
        errors: list = []

        def add():
            try:
                store.add_entry(_make_entry("concurrent"))
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=add) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert errors == []
        entries = store.get_entries("concurrent")
        assert len(entries) > 0


# ── create_memory_entry_from_output ──────────────────────────────────────────

class TestCreateMemoryEntryFromOutput:
    def test_returns_none_for_empty_dir(self, tmp_path):
        result = create_memory_entry_from_output(str(tmp_path))
        assert result is None

    def test_reads_analysis_result(self, tmp_path):
        data = {
            "project_name": "myproj",
            "score": 85,
            "risk_level": "low",
            "codegen_requirements": ["req1"],
        }
        (tmp_path / "analysis_result.json").write_text(
            json.dumps(data), encoding="utf-8"
        )
        entry = create_memory_entry_from_output(str(tmp_path))
        assert entry is not None
        assert entry.project_name == "myproj"
        assert entry.score == pytest.approx(85.0)

    def test_reads_run_meta_for_timestamp(self, tmp_path):
        (tmp_path / "analysis_result.json").write_text(
            json.dumps({"project_name": "p"}), encoding="utf-8"
        )
        (tmp_path / "run_meta.json").write_text(
            json.dumps({"timestamp": "2025-06-01T00:00:00Z"}), encoding="utf-8"
        )
        entry = create_memory_entry_from_output(str(tmp_path))
        assert entry is not None
        assert "2025-06-01" in entry.timestamp

    def test_collects_failed_experiments(self, tmp_path):
        data = {
            "project_name": "p",
            "experiments": [
                {"name": "exp1", "status": "failed"},
                {"name": "exp2", "status": "passed"},
            ],
        }
        (tmp_path / "analysis_result.json").write_text(
            json.dumps(data), encoding="utf-8"
        )
        entry = create_memory_entry_from_output(str(tmp_path))
        assert entry is not None
        assert "exp1" in entry.failed_experiments
        assert "exp2" not in entry.failed_experiments


# ── Regression tests ──────────────────────────────────────────────────────────

class TestBuildMemoryPromptPrefixRegression:
    """Regression: build_memory_prompt_prefix must not raise when ctx
    contains curly-brace patterns (previously used .format(ctx=ctx) which
    would fail with KeyError on user-supplied data like '{custom}')."""

    def test_curly_braces_in_direction_do_not_raise(self, tmp_path):
        store = ProjectMemoryStore(str(tmp_path))
        entry = MemoryEntry(
            timestamp="2025-01-01T00:00:00+00:00",
            project_name="proj",
            direction_selected="use {custom_format} strings",
            direction_summary="summary with {placeholders} inside",
            score=80.0,
            risk_level="low",
        )
        store.add_entry(entry)
        # Should NOT raise KeyError from .format() interpolation
        prefix = store.build_memory_prompt_prefix("proj")
        assert "Historical Project Memory" in prefix
        assert "{custom_format}" in prefix or "custom_format" in prefix

    def test_curly_braces_in_failed_experiments_do_not_raise(self, tmp_path):
        store = ProjectMemoryStore(str(tmp_path))
        entry = MemoryEntry(
            timestamp="2025-01-01T00:00:00+00:00",
            project_name="proj",
            direction_selected=None,
            direction_summary=None,
            score=None,
            risk_level=None,
            failed_experiments=["{class_method} pattern", "other {x}"],
        )
        store.add_entry(entry)
        prefix = store.build_memory_prompt_prefix("proj")
        assert "Historical Project Memory" in prefix
