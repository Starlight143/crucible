"""
tests/test_v1_2_0_cloud_backend.py
==================================
v1.2.0 Phase 1 — Python DualWriteBackend / CloudflareBackend client.

Covers the cloud-sync HTTP client, the background sync worker (flush / cursor /
failure handling), the dual backend's never-block write path and prune-respects-
unsynced data-safety invariant, make_backend + recorder-factory wiring, the
3-layer Settings sync, and structural producer→consumer wiring pins (CLAUDE.md
§9.6).  All network is faked — no test makes a real outbound request.
"""
from __future__ import annotations

import gzip
import inspect
import io
import json
import re
import urllib.error
from pathlib import Path

import pytest

from crucible.features.run_insights.backends import (
    CloudflareBackend,
    DualWriteBackend,
    LocalJSONLBackend,
    make_backend,
)
from crucible.features.run_insights.cloud_sync import CloudSyncClient, CloudSyncWorker

_REPO = Path(__file__).resolve().parents[1]

_CLOUD_KEYS = [
    "CRUCIBLE_RUN_INSIGHTS_API_URL",
    "CRUCIBLE_RUN_INSIGHTS_API_TOKEN",
    "CRUCIBLE_RUN_INSIGHTS_API_TIMEOUT_SECONDS",
    "CRUCIBLE_RUN_INSIGHTS_API_MAX_RETRIES",
    "CRUCIBLE_RUN_INSIGHTS_API_BATCH_FLUSH_SECONDS",
    "CRUCIBLE_RUN_INSIGHTS_API_BATCH_SIZE",
]


# ── helpers ───────────────────────────────────────────────────────────────────

def _event(cid: str, ts: str, stream: str = "output") -> dict:
    return {
        "content_id": cid,
        "ts": ts,
        "stream": stream,
        "kind": "output_method",
        "run_id": "r",
        "project_name": "p",
        "mode": "Quant",
        "schema_version": 1,
        "signals": [],
        "env_fingerprint": {},
        "outcome": {"status": "success"},
        "payload": {},
    }


def _write(local: LocalJSONLBackend, cid: str, ts: str, stream: str = "output") -> str:
    return local.write_event(stream, _event(cid, ts, stream))


class _FakeResp:
    def __init__(self, status: int, body: bytes = b""):
        self.status = status
        self._body = body

    def __enter__(self):
        return self

    def __exit__(self, *_a):
        return False

    def read(self):
        return self._body

    def getcode(self):
        return self.status


class _FakeOpener:
    """Stand-in for urllib's OpenerDirector — records the last Request and
    returns whatever ``responder`` produces (or raises)."""

    def __init__(self):
        self.last_request = None
        self.responder = lambda req: _FakeResp(200, b"{}")

    def open(self, req, timeout=None):
        self.last_request = req
        return self.responder(req)


class _FakeClient:
    """Records posted batches; ``ok`` controls the post_batch return."""

    def __init__(self, ok: bool = True):
        self.batches: list[list[dict]] = []
        self._ok = ok

    def post_batch(self, events):
        self.batches.append(list(events))
        return self._ok

    def get_events(self, *a, **k):
        return None, None


class _FlakyClient:
    """Succeeds for the first ``ok_calls`` posts, then fails."""

    def __init__(self, ok_calls: int):
        self.ok_calls = ok_calls
        self.calls = 0
        self.batches: list[list[dict]] = []

    def post_batch(self, events):
        self.calls += 1
        if self.calls <= self.ok_calls:
            self.batches.append(list(events))
            return True
        return False


# ── CloudSyncClient ─────────────────────────────────────────────────────────

class TestCloudSyncClient:
    def _client(self, opener):
        c = CloudSyncClient(api_url="https://x.workers.dev", api_token="tok123")
        c._opener = opener
        return c

    def test_post_batch_gzips_and_authenticates(self):
        opener = _FakeOpener()
        opener.responder = lambda req: _FakeResp(200, b'{"ingested":2}')
        client = self._client(opener)
        events = [
            {"content_id": "sha256:a", "ts": "t1"},
            {"content_id": "sha256:b", "ts": "t2"},
        ]
        assert client.post_batch(events) is True
        req = opener.last_request
        assert req.get_method() == "POST"
        assert req.full_url.endswith("/v1/insights/batch")
        assert req.get_header("Authorization") == "Bearer tok123"
        assert req.get_header("Content-encoding") == "gzip"
        payload = json.loads(gzip.decompress(req.data).decode("utf-8"))
        assert payload["events"] == events

    def test_post_batch_non_2xx_returns_false(self):
        opener = _FakeOpener()
        opener.responder = lambda req: _FakeResp(500, b"boom")
        assert self._client(opener).post_batch([{"content_id": "x"}]) is False

    def test_post_batch_httperror_returns_false(self):
        opener = _FakeOpener()

        def boom(req):
            raise urllib.error.HTTPError(
                req.full_url, 429, "rate", {}, io.BytesIO(b"slow down")
            )

        opener.responder = boom
        assert self._client(opener).post_batch([{"content_id": "x"}]) is False

    def test_empty_batch_is_noop_true(self):
        opener = _FakeOpener()
        # Should not even open a request for an empty batch.
        assert self._client(opener).post_batch([]) is True
        assert opener.last_request is None

    def test_non_http_scheme_raises(self):
        client = CloudSyncClient(api_url="ftp://evil/", api_token="t")
        with pytest.raises(ValueError):
            client.post_batch([{"content_id": "x"}])

    def test_get_events_none_on_non_2xx(self):
        opener = _FakeOpener()
        opener.responder = lambda req: _FakeResp(503, b"")
        events, nxt = self._client(opener).get_events("output")
        assert events is None and nxt is None

    def test_get_events_parses_and_builds_query(self):
        opener = _FakeOpener()
        opener.responder = lambda req: _FakeResp(
            200, json.dumps({"events": [{"a": 1}], "next_cursor": "c1"}).encode("utf-8")
        )
        events, nxt = self._client(opener).get_events("output", limit=10)
        assert events == [{"a": 1}] and nxt == "c1"
        assert "stream=output" in opener.last_request.full_url
        assert "limit=10" in opener.last_request.full_url


# ── CloudSyncWorker flush / cursor ───────────────────────────────────────────

class TestCloudSyncWorker:
    def _worker(self, tmp_path, client, **kw):
        local = LocalJSONLBackend(tmp_path / "led")
        worker = CloudSyncWorker(
            local_backend=local,
            client=client,
            cursor_path=tmp_path / "led" / ".cloud_sync_cursor.json",
            flush_seconds=999,
            **kw,
        )
        return local, worker

    def test_flush_sends_pending_and_advances_cursor(self, tmp_path):
        client = _FakeClient()
        local, worker = self._worker(tmp_path, client, batch_size=2)
        for i in range(3):
            _write(local, f"sha256:{i}", f"2026-01-01T00:00:00.00{i}Z")
        worker._flush_all()
        posted = [e["content_id"] for batch in client.batches for e in batch]
        assert posted == ["sha256:0", "sha256:1", "sha256:2"]
        assert worker.unsynced_count("output") == 0
        assert worker._cursor["output"]["content_id"] == "sha256:2"

    def test_flush_failure_keeps_all_unsynced(self, tmp_path):
        client = _FakeClient(ok=False)
        local, worker = self._worker(tmp_path, client, batch_size=2, max_retries=0)
        for i in range(3):
            _write(local, f"sha256:{i}", f"2026-01-01T00:00:00.00{i}Z")
        worker._flush_all()
        assert worker.unsynced_count("output") == 3
        assert "output" not in worker._cursor

    def test_partial_flush_advances_to_last_success(self, tmp_path):
        client = _FlakyClient(ok_calls=1)  # first batch ok, second fails
        local, worker = self._worker(tmp_path, client, batch_size=2, max_retries=0)
        for i in range(4):
            _write(local, f"sha256:{i}", f"2026-01-01T00:00:00.00{i}Z")
        worker._flush_all()
        assert worker._cursor["output"]["content_id"] == "sha256:1"
        assert worker.unsynced_count("output") == 2

    def test_cursor_persists_and_reloads(self, tmp_path):
        cpath = tmp_path / "led" / ".cloud_sync_cursor.json"
        local = LocalJSONLBackend(tmp_path / "led")
        for i in range(2):
            _write(local, f"sha256:{i}", f"2026-01-01T00:00:00.00{i}Z")
        w1 = CloudSyncWorker(local_backend=local, client=_FakeClient(),
                             cursor_path=cpath, flush_seconds=999)
        w1._flush_all()
        assert cpath.exists()
        # A fresh worker loads the cursor → nothing pending, nothing re-sent.
        w2_client = _FakeClient()
        w2 = CloudSyncWorker(local_backend=local, client=w2_client,
                             cursor_path=cpath, flush_seconds=999)
        assert w2.unsynced_count("output") == 0
        w2._flush_all()
        assert w2_client.batches == []

    def test_after_cursor_logic(self):
        events = [{"content_id": "a"}, {"content_id": "b"}, {"content_id": "c"}]
        assert CloudSyncWorker._after_cursor(events, "b") == [{"content_id": "c"}]
        assert CloudSyncWorker._after_cursor(events, None) == events
        # Cursor row pruned away → re-send the whole window (Worker dedups).
        assert CloudSyncWorker._after_cursor(events, "missing") == events


# ── DualWriteBackend ─────────────────────────────────────────────────────────

class TestDualWriteBackend:
    def test_write_event_never_calls_cloud_synchronously(self, tmp_path):
        backend = DualWriteBackend(
            root=tmp_path / "led", api_url="https://x.workers.dev",
            api_token="t", auto_start=False,
        )
        fake = _FakeClient()
        backend._sync.client = fake
        try:
            cid = backend.write_event("output", _event("sha256:1", "2026-01-01T00:00:00.001Z"))
            assert cid == "sha256:1"
            local_events, _ = backend._local.read_events("output", limit=10)
            assert len(local_events) == 1
            # The hot path persists locally + nudges the daemon — it must not
            # post to the cloud itself.
            assert fake.batches == []
        finally:
            backend.close()

    def test_prune_respects_unsynced_high_water(self, tmp_path):
        backend = DualWriteBackend(
            root=tmp_path / "led", api_url="https://x.workers.dev",
            api_token="t", auto_start=False,
        )
        try:
            for i in range(5):
                backend.write_event("output", _event(f"sha256:{i}", f"2026-01-01T00:00:00.00{i}Z"))
            # 5 unsynced, cap 2 → effective keep = 5 → nothing dropped.
            assert backend.prune_stream("output", 2) == 0
            kept, _ = backend._local.read_events("output", limit=100)
            assert len(kept) == 5
            # Simulate the cloud catching up to the last event.
            backend._sync._advance(
                "output", {"ts": "2026-01-01T00:00:00.004Z", "content_id": "sha256:4"}
            )
            assert backend.prune_stream("output", 2) == 3
            kept2, _ = backend._local.read_events("output", limit=100)
            assert len(kept2) == 2
        finally:
            backend.close()

    def test_delegates_read_and_blob_to_local(self, tmp_path):
        backend = DualWriteBackend(
            root=tmp_path / "led", api_url="https://x.workers.dev",
            api_token="t", auto_start=False,
        )
        try:
            backend.write_blob("sha256:blob", b"hello-bytes")
            assert backend.read_blob("sha256:blob") == b"hello-bytes"
        finally:
            backend.close()

    def test_missing_api_config_raises(self, tmp_path):
        with pytest.raises(ValueError):
            DualWriteBackend(root=tmp_path / "led", api_url="", api_token="t")
        with pytest.raises(ValueError):
            DualWriteBackend(root=tmp_path / "led", api_url="https://x", api_token="")

    def test_cloudflare_read_prefers_cloud_falls_back_local(self, tmp_path):
        backend = CloudflareBackend(
            root=tmp_path / "led", api_url="https://x.workers.dev",
            api_token="t", auto_start=False,
        )
        try:
            _write(backend._local, "sha256:local", "2026-01-01T00:00:00.000Z")

            class _CloudHit:
                def get_events(self, *a, **k):
                    return [{"content_id": "sha256:cloud"}], "cur1"

            class _CloudMiss:
                def get_events(self, *a, **k):
                    return None, None  # unreachable → fall back to local

            backend._sync.client = _CloudHit()
            events, nxt = backend.read_events("output", limit=10)
            assert events == [{"content_id": "sha256:cloud"}] and nxt == "cur1"

            backend._sync.client = _CloudMiss()
            events2, _ = backend.read_events("output", limit=10)
            assert [e["content_id"] for e in events2] == ["sha256:local"]
        finally:
            backend.close()


# ── make_backend wiring ──────────────────────────────────────────────────────

class TestMakeBackendWiring:
    def test_dual_and_cloudflare_construct(self, tmp_path):
        d = make_backend("dual", root=tmp_path / "a", api_url="https://h", api_token="t")
        try:
            assert isinstance(d, DualWriteBackend) and not isinstance(d, CloudflareBackend)
        finally:
            d.close()
        c = make_backend("cloudflare", root=tmp_path / "b", api_url="https://h", api_token="t")
        try:
            assert isinstance(c, CloudflareBackend)
        finally:
            c.close()

    def test_missing_config_raises_valueerror(self, tmp_path):
        with pytest.raises(ValueError):
            make_backend("dual", root=tmp_path / "x")
        with pytest.raises(ValueError):
            make_backend("cloudflare", root=tmp_path / "x", api_url="https://h")

    def test_unknown_backend_raises_valueerror(self, tmp_path):
        with pytest.raises(ValueError):
            make_backend("bogus", root=tmp_path / "x")


# ── recorder factory degrade / wiring ────────────────────────────────────────

class TestRecorderFactory:
    def _reset(self):
        from crucible.features.run_insights.recorder import reset_recorder
        reset_recorder()

    def test_dual_without_api_degrades_to_local(self, tmp_path, monkeypatch):
        from crucible.features.run_insights.recorder import get_recorder
        monkeypatch.setenv("CRUCIBLE_RUN_INSIGHTS_ENABLED", "1")
        monkeypatch.setenv("CRUCIBLE_RUN_INSIGHTS_BACKEND", "dual")
        monkeypatch.setenv("CRUCIBLE_RUN_INSIGHTS_DIR", str(tmp_path / "led"))
        monkeypatch.delenv("CRUCIBLE_RUN_INSIGHTS_API_URL", raising=False)
        monkeypatch.delenv("CRUCIBLE_RUN_INSIGHTS_API_TOKEN", raising=False)
        self._reset()
        try:
            recorder = get_recorder()
            assert isinstance(recorder.backend, LocalJSONLBackend)
        finally:
            recorder.close()
            self._reset()

    def test_dual_with_api_constructs_dual(self, tmp_path, monkeypatch):
        from crucible.features.run_insights.recorder import get_recorder
        monkeypatch.setenv("CRUCIBLE_RUN_INSIGHTS_ENABLED", "1")
        monkeypatch.setenv("CRUCIBLE_RUN_INSIGHTS_BACKEND", "dual")
        monkeypatch.setenv("CRUCIBLE_RUN_INSIGHTS_DIR", str(tmp_path / "led"))
        monkeypatch.setenv("CRUCIBLE_RUN_INSIGHTS_API_URL", "https://h.workers.dev")
        monkeypatch.setenv("CRUCIBLE_RUN_INSIGHTS_API_TOKEN", "tok")
        self._reset()
        try:
            recorder = get_recorder()
            assert isinstance(recorder.backend, DualWriteBackend)
        finally:
            recorder.close()
            self._reset()


# ── 3-layer Settings sync ────────────────────────────────────────────────────

class TestSettingsSync:
    def test_env_example_uncomments_cloud_keys(self):
        env = (_REPO / ".env.example").read_text(encoding="utf-8")
        for key in _CLOUD_KEYS:
            assert re.search(rf"(?m)^{re.escape(key)}=", env), f"{key} not uncommented in .env.example"

    def test_app_js_settings_group_lists_cloud_keys(self):
        js = (_REPO / "webui" / "static" / "js" / "app.js").read_text(encoding="utf-8")
        # Each key must appear at least twice: once in the SETTINGS_SCHEMA
        # run_insights group keys array, once as a KEY_META entry.
        for key in _CLOUD_KEYS:
            assert js.count(key) >= 2, f"{key} missing from group and/or KEY_META"

    def test_app_js_key_meta_entries_are_bilingual(self):
        js = (_REPO / "webui" / "static" / "js" / "app.js").read_text(encoding="utf-8")
        for key in _CLOUD_KEYS:
            idx = js.find(key + ":")  # the KEY_META entry (group uses 'KEY' quoted)
            assert idx != -1, f"{key} has no KEY_META entry"
            snippet = js[idx : idx + 800]
            assert "en:" in snippet and "zh:" in snippet, f"{key} KEY_META not bilingual"


# ── structural producer→consumer wiring (CLAUDE.md §9.6) ─────────────────────

class TestStructuralWiring:
    def test_recorder_factory_reads_cloud_env_and_degrades(self):
        from crucible.features.run_insights import recorder as rec
        src = inspect.getsource(rec._build_default_recorder)
        for key in (
            "CRUCIBLE_RUN_INSIGHTS_API_TIMEOUT_SECONDS",
            "CRUCIBLE_RUN_INSIGHTS_API_MAX_RETRIES",
            "CRUCIBLE_RUN_INSIGHTS_API_BATCH_FLUSH_SECONDS",
            "CRUCIBLE_RUN_INSIGHTS_API_BATCH_SIZE",
        ):
            assert key in src, f"{key} not read by recorder factory"
        assert "timeout_seconds=" in src and "max_retries=" in src
        assert "falling back to a local-only" in src  # degrade path present

    def test_make_backend_forwards_cloud_config(self):
        from crucible.features.run_insights import backends
        src = inspect.getsource(backends.make_backend)
        for param in ("timeout_seconds", "max_retries", "flush_seconds", "batch_size"):
            assert param in src

    def test_write_event_has_no_synchronous_cloud_post(self):
        src = inspect.getsource(DualWriteBackend.write_event)
        assert "post_batch" not in src, "write_event must not call the cloud synchronously"
        assert "self._local.write_event" in src

    def test_prune_stream_respects_unsynced(self):
        src = inspect.getsource(DualWriteBackend.prune_stream)
        assert "unsynced_count" in src and "max(" in src
