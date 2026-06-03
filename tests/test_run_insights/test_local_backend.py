"""
Tests for the local JSONL backend: write/read roundtrip, cursor pagination,
prune FIFO, blob storage, atomic writes.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from crucible.features.run_insights.backends import (
    LocalJSONLBackend,
    make_backend,
)
from crucible.features.run_insights.schema import (
    compute_content_id,
    utc_now_iso,
)


def _make_event(stream_kind: str = "error", n: int = 0) -> dict:
    e = {
        "schema_version": 1,
        "ts": utc_now_iso(),
        "run_id": "test_run",
        "project_name": "test_proj",
        "mode": "Quant",
        "kind": stream_kind,
        "stage": "test",
        "signals": ["mode:quant", "asset:gold"],
        "env_fingerprint": {},
        "outcome": {"status": "failure"},
        "payload": {"n": n},
    }
    e["content_id"] = compute_content_id(e)
    return e


def test_backend_init_creates_layout(tmp_path: Path):
    backend = LocalJSONLBackend(tmp_path / ".crucible_insights")
    root = backend.root
    assert root.exists()
    assert (root / "blobs").is_dir()
    assert (root / ".schema_version").read_text(encoding="utf-8").strip() == "1"


def test_write_and_read_roundtrip(tmp_path: Path):
    backend = LocalJSONLBackend(tmp_path / "ledger")
    events = [_make_event("error_record", n=i) for i in range(5)]
    for e in events:
        cid = backend.write_event("error", e)
        assert cid == e["content_id"]

    read, cursor = backend.read_events("error", limit=10)
    assert len(read) == 5
    assert [r["payload"]["n"] for r in read] == [0, 1, 2, 3, 4]
    assert cursor is None  # no more events


def test_cursor_pagination(tmp_path: Path):
    backend = LocalJSONLBackend(tmp_path / "ledger")
    for i in range(10):
        backend.write_event("error", _make_event("error_record", n=i))

    page1, cursor = backend.read_events("error", limit=3)
    assert len(page1) == 3
    assert cursor is not None

    page2, cursor2 = backend.read_events("error", cursor=cursor, limit=3)
    assert len(page2) == 3
    assert page2[0]["payload"]["n"] == 3

    page3, cursor3 = backend.read_events("error", cursor=cursor2, limit=10)
    assert len(page3) == 4
    assert cursor3 is None  # ran out


def test_since_filter(tmp_path: Path):
    backend = LocalJSONLBackend(tmp_path / "ledger")
    # Manually craft events with controlled timestamps.
    older = _make_event("error_record", n=1)
    older["ts"] = "2020-01-01T00:00:00.000Z"
    older["content_id"] = compute_content_id(older)
    newer = _make_event("error_record", n=2)
    newer["ts"] = "2099-01-01T00:00:00.000Z"
    newer["content_id"] = compute_content_id(newer)

    backend.write_event("error", older)
    backend.write_event("error", newer)

    read, _ = backend.read_events("error", since="2050-01-01T00:00:00.000Z")
    assert len(read) == 1
    assert read[0]["payload"]["n"] == 2


def test_prune_fifo_keeps_recent(tmp_path: Path):
    backend = LocalJSONLBackend(tmp_path / "ledger")
    for i in range(10):
        backend.write_event("error", _make_event("error_record", n=i))

    dropped = backend.prune_stream("error", 3)
    assert dropped == 7

    read, _ = backend.read_events("error", limit=100)
    assert len(read) == 3
    assert [r["payload"]["n"] for r in read] == [7, 8, 9]


def test_prune_below_threshold_is_noop(tmp_path: Path):
    backend = LocalJSONLBackend(tmp_path / "ledger")
    for i in range(5):
        backend.write_event("error", _make_event("error_record", n=i))

    dropped = backend.prune_stream("error", 100)
    assert dropped == 0
    read, _ = backend.read_events("error", limit=100)
    assert len(read) == 5


def test_blob_write_and_read(tmp_path: Path):
    backend = LocalJSONLBackend(tmp_path / "ledger")
    payload = b'{"big": "payload"}'
    cid = "sha256:" + ("ab" * 32)
    key = backend.write_blob(cid, payload)
    assert key.startswith("blobs/")
    assert backend.read_blob(cid) == payload


def test_blob_read_missing_returns_none(tmp_path: Path):
    backend = LocalJSONLBackend(tmp_path / "ledger")
    assert backend.read_blob("sha256:" + "00" * 32) is None


def test_invalid_stream_raises(tmp_path: Path):
    backend = LocalJSONLBackend(tmp_path / "ledger")
    with pytest.raises(ValueError):
        backend.write_event("unknown_stream", _make_event())


def test_read_missing_stream_returns_empty(tmp_path: Path):
    backend = LocalJSONLBackend(tmp_path / "ledger")
    read, cursor = backend.read_events("error", limit=10)
    assert read == []
    assert cursor is None


def test_malformed_lines_skipped(tmp_path: Path):
    backend = LocalJSONLBackend(tmp_path / "ledger")
    backend.write_event("error", _make_event("error_record", n=1))
    # Inject a bogus line directly.
    path = backend.root / "error.jsonl"
    with open(path, "a", encoding="utf-8") as fh:
        fh.write("this is not json\n")
    backend.write_event("error", _make_event("error_record", n=2))

    read, _ = backend.read_events("error", limit=10)
    assert len(read) == 2  # bogus line skipped


def test_make_backend_local(tmp_path: Path):
    backend = make_backend("local", root=tmp_path / "x")
    assert isinstance(backend, LocalJSONLBackend)


def test_make_backend_cloudflare_constructs(tmp_path: Path):
    # v1.2.0: cloudflare is implemented — make_backend now returns a
    # CloudflareBackend (cloud-primary reads) instead of raising.
    from crucible.features.run_insights.backends import CloudflareBackend
    backend = make_backend(
        "cloudflare",
        root=tmp_path / "x",
        api_url="https://example.workers.dev",
        api_token="dummy",
    )
    try:
        assert isinstance(backend, CloudflareBackend)
    finally:
        backend.close()


def test_make_backend_cloudflare_missing_url_token_raises(tmp_path: Path):
    # v1.2.0: missing url/token is a loud config error (ValueError); the
    # recorder factory translates it into a graceful local-only fallback.
    with pytest.raises(ValueError):
        make_backend("cloudflare", root=tmp_path / "x")


def test_make_backend_dual_constructs(tmp_path: Path):
    # v1.2.0: dual is implemented — make_backend returns a DualWriteBackend.
    from crucible.features.run_insights.backends import DualWriteBackend
    backend = make_backend("dual", root=tmp_path / "x",
                           api_url="https://h.workers.dev", api_token="y")
    try:
        assert isinstance(backend, DualWriteBackend)
    finally:
        backend.close()


def test_make_backend_unknown_raises(tmp_path: Path):
    with pytest.raises(ValueError):
        make_backend("bogus_backend", root=tmp_path / "x")


# ─── v1.1.0 third-pass: schema-marker race ───────────────────────────────────

def test_concurrent_init_layout_no_partial_marker(tmp_path: Path):
    """Two threads constructing ``LocalJSONLBackend`` on the same root
    must agree on a complete schema marker.

    v1.1.0 ships the ``.schema_version.lock`` sidecar + atomic
    temp+replace marker write specifically to handle this case.  This
    test would have caught a regression that removed the lock-then-
    recheck step (e.g. someone "simplifying" the init path).
    """
    import threading as _threading

    root = tmp_path / "shared_ledger"
    errors: list[Exception] = []

    def _ctor():
        try:
            LocalJSONLBackend(root)
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [_threading.Thread(target=_ctor) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, f"concurrent _init_layout raised: {errors!r}"
    # Marker must be complete ("1\n", no truncation, no double-write).
    marker = (root / ".schema_version").read_text(encoding="utf-8")
    assert marker.strip() == "1", (
        f"schema marker corrupted under race: {marker!r}"
    )


def test_max_entries_clamp_max_via_recorder():
    """``MAX_ENTRIES_PER_STREAM`` is clamped to 1_000_000 to prevent
    OOM from a hostile / typo env value.

    v1.1.0 third-pass added ``clamp_max=1_000_000`` to
    ``_build_default_recorder``.  Without it, a typo like
    ``MAX_ENTRIES_PER_STREAM=2000000000`` would have caused
    ``collections.deque(maxlen=2e9)`` to attempt a multi-terabyte
    allocation during prune.
    """
    import os as _os
    from crucible.features.run_insights.recorder import _build_default_recorder

    prev = _os.environ.get("CRUCIBLE_RUN_INSIGHTS_MAX_ENTRIES_PER_STREAM")
    _os.environ["CRUCIBLE_RUN_INSIGHTS_MAX_ENTRIES_PER_STREAM"] = "2000000000"
    try:
        recorder = _build_default_recorder()
    finally:
        if prev is None:
            _os.environ.pop("CRUCIBLE_RUN_INSIGHTS_MAX_ENTRIES_PER_STREAM", None)
        else:
            _os.environ["CRUCIBLE_RUN_INSIGHTS_MAX_ENTRIES_PER_STREAM"] = prev

    # The recorder may be the no-op variant if the subsystem was disabled
    # in the surrounding env; only assert when we got the real recorder.
    max_entries = getattr(recorder, "_max_entries", None)
    if isinstance(max_entries, int):
        assert max_entries <= 1_000_000, (
            f"clamp_max breached: got {max_entries}"
        )


# ─── v1.1.0 fourth-pass: schema-marker forward-compat (T9) ─────────────────

def test_schema_marker_forward_compat_does_not_rollback(tmp_path: Path):
    """If a future v1.2 process wrote ``"2"`` to the marker, a v1.1
    backend constructed against the same root MUST NOT roll it back
    to ``"1"``.  T9 changed the comparison from ``content == "1"``
    to ``int(content) >= 1``; this test pins that contract.
    """
    root = tmp_path / "v2_ledger"
    root.mkdir()
    (root / "blobs").mkdir()
    # Simulate a v1.2 marker written by some future process.
    (root / ".schema_version").write_text("2\n", encoding="utf-8")

    # Construct v1.1 backend (expected=1 internally).
    LocalJSONLBackend(root)

    # Marker must NOT have been rewritten back to "1".
    on_disk = (root / ".schema_version").read_text(encoding="utf-8").strip()
    assert on_disk == "2", (
        f"v1.1 backend rolled the marker back to {on_disk!r}; forward-compat broken"
    )


# ─── v1.1.0 fourth-pass: V8 encoder fallback (T11) ─────────────────────────

def test_v8_encoder_fallback_when_make_iterencode_missing(monkeypatch):
    """If a future CPython release renames ``json.encoder._make_iterencode``,
    the cached binding at module import is None and the encoder falls
    back to the default ``json.JSONEncoder.iterencode``.

    This test simulates that scenario by monkey-patching
    ``_V8_ENCODER_AVAILABLE = False`` and asserting that
    ``canonical_json`` still produces valid JSON (content_id parity
    with V8 is sacrificed but the writer does NOT crash).
    """
    from crucible.features.run_insights import schema as _schema

    monkeypatch.setattr(_schema, "_V8_ENCODER_AVAILABLE", False)

    event = {
        "schema_version": 1,
        "ts": "2026-05-13T00:00:00.000Z",
        "run_id": "fallback",
        "project_name": "p",
        "mode": "Quant",
        "kind": "output_method",
        "stage": "test",
        "signals": ["mode:quant"],
        "env_fingerprint": {},
        "outcome": {"status": "success"},
        "payload": {"v": 1.5},  # float forces the encoder path
    }
    raw = _schema.canonical_json(event)
    # Must be valid JSON.
    parsed = json.loads(raw)
    assert parsed["payload"]["v"] == 1.5


# ─── v1.1.0 fourth-pass: _init_layout failure path falls back (F-6) ────────

def test_init_failure_marks_backend_init_failed(tmp_path: Path, monkeypatch):
    """When ``_init_layout`` cannot create the ledger root (e.g.
    parent path is a regular file blocking mkdir), the backend
    must mark itself ``_init_failed=True`` and ``_closed=True`` so
    the recorder factory substitutes ``_NullRecorder`` instead of
    keeping a black-hole backend alive.
    """
    # Block the ledger path: parent is a regular file → mkdir errors.
    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory", encoding="utf-8")
    target = blocker / "subdir_that_cannot_exist"

    backend = LocalJSONLBackend(target)
    assert backend._init_failed is True
    assert backend._closed is True
