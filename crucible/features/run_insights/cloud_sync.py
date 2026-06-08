"""
features/run_insights/cloud_sync.py
===================================
v1.2.0 — outbound HTTP client + background sync worker for the Cloudflare
insights backend (:class:`backends.DualWriteBackend` /
:class:`backends.CloudflareBackend`).

Design contract (see ``backends.py`` module docstring + ``cloudflare/insights-worker/``):

* The **local JSONL ledger is the source of truth**; the cloud copy is
  eventually-consistent.  Nothing on the pipeline's hot path ever blocks on the
  network — ``write_event`` returns as soon as the local fsync completes, and a
  background daemon thread batches un-synced events to the Worker.
* Delivery is **at-least-once**; the Worker dedups on ``content_id``
  (``INSERT OR IGNORE``), so storage is **effectively-once**.  Re-sending the
  same event is always safe.
* A persisted ``(ts, content_id)`` cursor per stream lets sync resume after a
  crash / restart without re-reading the whole ledger.
* Cloud failure only delays sync; it never raises into the pipeline and never
  drops local data (the dual backend's ``prune_stream`` refuses to trim below
  the un-synced high-water mark — see :meth:`CloudSyncWorker.unsynced_count`).

Threat-model note — this module deliberately does **not** reuse
``web_research.http_clients`` (which rejects private / loopback IPs for SSRF
defence on *user-controlled* URLs).  The insights API URL is *operator*-
configured (like a database DSN), so an operator self-hosting the Worker on a
private network, or testing against a local ``wrangler dev`` on ``127.0.0.1``,
must be allowed.  We still apply prudent defences: ``http(s)`` scheme only,
redirects are **not** followed (a 3xx is treated as a failure rather than
chased to another host), a hard per-request timeout, and the bearer token is
never written to logs.
"""
from __future__ import annotations

import gzip
import json
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote, urlencode, urlparse

# Tri-modal import (see recorder.py for the launcher matrix).
try:
    from ..._atomic_io import atomic_write_text
    from ...runtime_logging import get_logger
except ImportError:  # pragma: no cover — flat-launcher fallback
    from _atomic_io import atomic_write_text  # type: ignore[no-redef]
    from runtime_logging import get_logger  # type: ignore[no-redef]

LOGGER = get_logger(__name__)

# The four canonical streams (mirrors backends._VALID_STREAMS).
_STREAMS: Tuple[str, ...] = ("output", "error", "debate", "params")

# Sync-cursor sidecar inside the ledger root.
_CURSOR_FILENAME = ".cloud_sync_cursor.json"

_DEFAULT_USER_AGENT = "Crucible/1.2.0 run-insights-sync"


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Disable redirect following.

    Returning ``None`` from ``redirect_request`` makes urllib raise
    ``HTTPError`` for the 3xx instead of chasing the ``Location`` header to a
    possibly-internal host.  The client catches that and reports the redirect
    status as a failure.
    """

    def redirect_request(self, *_a: Any, **_k: Any) -> None:  # type: ignore[override]
        return None


# ── HTTP client ───────────────────────────────────────────────────────────────

class CloudSyncClient:
    """Minimal, stdlib-only HTTP client for the insights Worker.

    Methods return booleans / parsed data and **never raise** on HTTP status
    codes (a non-2xx becomes ``False`` / ``None``); only genuine network
    errors (DNS, connection refused, timeout) propagate, and the worker treats
    those as a deferred-retry signal.
    """

    def __init__(
        self,
        *,
        api_url: str,
        api_token: str,
        timeout_seconds: float = 10.0,
        user_agent: str = _DEFAULT_USER_AGENT,
    ) -> None:
        self._base = (api_url or "").rstrip("/")
        self._token = api_token or ""
        self._timeout = max(1.0, float(timeout_seconds))
        self._ua = user_agent
        # build_opener replaces the default redirect handler with _NoRedirect.
        self._opener = urllib.request.build_opener(_NoRedirect)

    # -- low-level request --
    def _request(
        self,
        method: str,
        path: str,
        *,
        body: Optional[Any] = None,
        gzip_body: bool = False,
        query: Optional[Dict[str, Any]] = None,
    ) -> Tuple[int, bytes]:
        url = self._base + path
        if query:
            clean = {k: v for k, v in query.items() if v is not None and v != ""}
            if clean:
                url = f"{url}?{urlencode(clean)}"
        scheme = urlparse(url).scheme.lower()
        if scheme not in ("https", "http"):
            raise ValueError(
                f"insights api_url must use http(s); got scheme={scheme!r}"
            )
        headers = {
            "Authorization": f"Bearer {self._token}",
            "User-Agent": self._ua,
            "Accept": "application/json",
        }
        data: Optional[bytes] = None
        if body is not None:
            raw = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            if gzip_body:
                raw = gzip.compress(raw)
                headers["Content-Encoding"] = "gzip"
            headers["Content-Type"] = "application/json; charset=utf-8"
            data = raw
        req = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            with self._opener.open(req, timeout=self._timeout) as resp:
                status = int(getattr(resp, "status", 0) or resp.getcode() or 0)
                return status, resp.read()
        except urllib.error.HTTPError as exc:
            # Non-2xx (incl. a blocked 3xx) carries a status + body; surface it
            # so the caller classifies it instead of treating it as a network
            # error.  NEVER logs headers (token safety).
            try:
                payload = exc.read()
            except Exception:  # noqa: BLE001
                payload = b""
            return int(exc.code), payload

    @staticmethod
    def _ok(status: int) -> bool:
        return 200 <= status < 300

    # -- public API --
    def post_batch(self, events: List[dict]) -> bool:
        """POST a gzip batch.  Returns True on 2xx, False on any non-2xx."""
        if not events:
            return True
        status, _ = self._request(
            "POST", "/v1/insights/batch", body={"events": events}, gzip_body=True
        )
        return self._ok(status)

    def health(self) -> bool:
        try:
            status, _ = self._request("GET", "/health")
            return self._ok(status)
        except Exception:  # noqa: BLE001
            return False

    def get_events(
        self,
        stream: Optional[str] = None,
        *,
        run_id: Optional[str] = None,
        since: Optional[str] = None,
        cursor: Optional[str] = None,
        limit: int = 100,
    ) -> Tuple[Optional[List[dict]], Optional[str]]:
        """Query events.  Returns ``(events, next_cursor)`` on success, or
        ``(None, None)`` on a non-2xx / unparseable response (lets the
        cloud-primary read path fall back to local)."""
        status, payload = self._request(
            "GET",
            "/v1/insights/events",
            query={
                "stream": stream,
                "run_id": run_id,
                "since": since,
                "cursor": cursor,
                "limit": int(limit),
            },
        )
        if not self._ok(status):
            return None, None
        try:
            data = json.loads(payload.decode("utf-8"))
        except Exception:  # noqa: BLE001
            return None, None
        if not isinstance(data, dict):
            return None, None
        events = data.get("events")
        nxt = data.get("next_cursor")
        return (events if isinstance(events, list) else []), (nxt if isinstance(nxt, str) else None)

    def get_event(self, content_id: str) -> Optional[dict]:
        if not content_id:
            return None
        status, payload = self._request(
            "GET", f"/v1/insights/events/{quote(content_id, safe='')}"
        )
        if not self._ok(status):
            return None
        try:
            obj = json.loads(payload.decode("utf-8"))
            return obj if isinstance(obj, dict) else None
        except Exception:  # noqa: BLE001
            return None


# ── Background sync worker ──────────────────────────────────────────────────

class CloudSyncWorker:
    """Background daemon that drains the local ledger to the cloud Worker.

    Lifecycle: construct → :meth:`start` (idempotent; usually lazily on the
    first write) → :meth:`notify` on each write to nudge a flush →
    :meth:`flush_and_stop` on close.  The daemon also flushes every
    ``flush_seconds`` as a safety net, with exponential backoff after
    consecutive failures.
    """

    def __init__(
        self,
        *,
        local_backend: Any,
        client: CloudSyncClient,
        cursor_path: "str | Path",
        flush_seconds: float = 30.0,
        max_retries: int = 3,
        batch_size: int = 100,
    ) -> None:
        self._local = local_backend
        self.client = client
        self._cursor_path = Path(cursor_path)
        self._flush_seconds = max(1.0, float(flush_seconds))
        self._max_retries = max(0, int(max_retries))
        self._batch_size = max(1, int(batch_size))
        self._lock = threading.RLock()
        self._cv = threading.Condition(self._lock)
        self._flush_lock = threading.Lock()
        self._stop = False
        self._thread: Optional[threading.Thread] = None
        self._consecutive_failures = 0
        self._backlog_warned = False
        self._cursor: Dict[str, Dict[str, str]] = self._load_cursor()

    # -- cursor persistence -----------------------------------------------------
    def _load_cursor(self) -> Dict[str, Dict[str, str]]:
        try:
            data = json.loads(self._cursor_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        out: Dict[str, Dict[str, str]] = {}
        if isinstance(data, dict):
            for s in _STREAMS:
                v = data.get(s)
                if isinstance(v, dict) and v.get("content_id"):
                    out[s] = {
                        "ts": str(v.get("ts") or ""),
                        "content_id": str(v["content_id"]),
                    }
        return out

    def _persist_cursor(self) -> None:
        try:
            atomic_write_text(
                self._cursor_path,
                json.dumps(self._cursor, ensure_ascii=False, sort_keys=True),
            )
        except Exception as exc:  # noqa: BLE001
            LOGGER.debug("cloud sync: cursor persist failed: %s", exc)

    # -- lifecycle --------------------------------------------------------------
    def start(self) -> None:
        with self._lock:
            if self._thread is not None or self._stop:
                return
            t = threading.Thread(
                target=self._run, name="insights-cloud-sync", daemon=True
            )
            self._thread = t
            t.start()

    def notify(self) -> None:
        with self._cv:
            self._cv.notify()

    def request_flush_now(self) -> None:
        self.notify()

    def flush_and_stop(self, timeout: float = 5.0) -> None:
        """Best-effort final drain, then stop the daemon and join it.

        Called on clean shutdown (``DualWriteBackend.close`` ← the recorder
        ``atexit`` hook in ``recorder.py``).  A bounded, single-attempt final
        flush runs *first* — while ``_stop`` is still ``False`` so
        :meth:`_flush_stream` / :meth:`_post_with_retry` do not early-out — so
        the last batch of a run reaches the cloud now instead of waiting for
        the next run's cursor-resume.  The drain is time-budgeted and never
        retries (at most one per-request HTTP timeout per stream), so a clean
        exit stays responsive even when the cloud is unreachable.  Anything
        still un-synced remains durable in the local ledger and syncs on the
        next run, so this never needs to fully succeed and never raises.
        """
        try:
            self._final_flush(budget_seconds=max(0.0, float(timeout)))
        except Exception as exc:  # noqa: BLE001 — shutdown must never raise
            LOGGER.debug("cloud sync: final flush errored: %s", exc)
        with self._cv:
            self._stop = True
            self._cv.notify_all()
        t = self._thread
        if t is not None:
            t.join(timeout=timeout)

    def _final_flush(self, *, budget_seconds: float) -> None:
        """Single-attempt, time-budgeted drain of every stream in the caller's
        thread.  Used only by :meth:`flush_and_stop` on shutdown.

        Holds ``_flush_lock`` so it never races a concurrent daemon flush pass
        (the daemon's :meth:`_flush_all` acquires the same lock non-blocking and
        skips when it is held).  Unlike the daemon path it passes
        ``max_retries=0`` and stops once the wall-clock budget is exhausted, so
        it cannot hang the process on an unreachable cloud.  Every error is
        swallowed — the local ledger is the source of truth.
        """
        # Bound the wait for the lock too: if the daemon is mid-flush it is
        # already making progress, so let it, and let any tail resume on the
        # next run rather than blocking exit.
        if budget_seconds <= 0.0:
            acquired = self._flush_lock.acquire(blocking=False)
        else:
            acquired = self._flush_lock.acquire(timeout=min(2.0, budget_seconds))
        if not acquired:
            return
        try:
            deadline = time.monotonic() + budget_seconds
            for stream in _STREAMS:
                if time.monotonic() >= deadline:
                    break
                try:
                    self._flush_stream(stream, max_retries=0)
                except Exception as exc:  # noqa: BLE001
                    LOGGER.debug(
                        "cloud sync: final flush %s failed: %s", stream, exc
                    )
        finally:
            self._flush_lock.release()

    # -- run loop ---------------------------------------------------------------
    def _run(self) -> None:
        while True:
            with self._cv:
                if self._stop:
                    return
                wait = self._flush_seconds
                if self._consecutive_failures:
                    wait = min(
                        self._flush_seconds * (2 ** min(self._consecutive_failures, 5)),
                        1800.0,
                    )
                self._cv.wait(timeout=wait)
                if self._stop:
                    return
            self._flush_all()

    def _flush_all(self) -> None:
        # Serialise flush passes so the daemon and any manual flush never race
        # on the cursor.  Non-blocking: if a pass is already running, skip (the
        # next tick / notify reruns).
        if not self._flush_lock.acquire(blocking=False):
            return
        try:
            for stream in _STREAMS:
                if self._stop:
                    return
                try:
                    self._flush_stream(stream)
                except Exception as exc:  # noqa: BLE001
                    LOGGER.debug("cloud sync: flush %s failed: %s", stream, exc)
        finally:
            self._flush_lock.release()

    # -- pending computation ----------------------------------------------------
    def _pending(self, stream: str) -> List[dict]:
        with self._lock:
            cur = dict(self._cursor.get(stream) or {})
        since = cur.get("ts") or None
        last_cid = cur.get("content_id") or None
        events, _ = self._local.read_events(stream, since=since, limit=1_000_000)
        return self._after_cursor(events, last_cid)

    @staticmethod
    def _after_cursor(events: List[dict], last_cid: Optional[str]) -> List[dict]:
        if not last_cid:
            return list(events)
        seen = False
        out: List[dict] = []
        for ev in events:
            if seen:
                out.append(ev)
            elif ev.get("content_id") == last_cid:
                seen = True
        # If the cursor row was pruned/rotated away we can't locate the
        # boundary; re-send the whole window — the Worker dedups idempotently
        # so the duplicates are harmless.
        return out if seen else list(events)

    def unsynced_count(self, stream: str) -> int:
        """Exact count of events not yet acked by the cloud (used by the dual
        backend's prune to never trim below this high-water mark)."""
        try:
            return len(self._pending(stream))
        except Exception:  # noqa: BLE001
            return 0

    def warn_backlog_once(self, stream: str, n: int) -> None:
        with self._lock:
            if self._backlog_warned:
                return
            self._backlog_warned = True
        LOGGER.warning(
            "run_insights cloud sync: %d unsynced '%s' events exceed the prune "
            "cap; the local ledger is retained beyond the cap until the cloud "
            "catches up (check CRUCIBLE_RUN_INSIGHTS_API_URL reachability).",
            n,
            stream,
        )

    # -- flush one stream -------------------------------------------------------
    def _flush_stream(self, stream: str, max_retries: Optional[int] = None) -> None:
        pending = self._pending(stream)
        if not pending:
            return
        sent = 0
        for i in range(0, len(pending), self._batch_size):
            if self._stop:
                break
            chunk = pending[i : i + self._batch_size]
            if not self._post_with_retry(chunk, max_retries=max_retries):
                with self._lock:
                    self._consecutive_failures += 1
                self.warn_backlog_once(stream, len(pending) - sent)
                if sent:
                    self._advance(stream, pending[sent - 1])
                return
            sent += len(chunk)
            self._advance(stream, pending[sent - 1])
        if sent:
            with self._lock:
                self._consecutive_failures = 0
                self._backlog_warned = False

    def _post_with_retry(
        self, chunk: List[dict], max_retries: Optional[int] = None
    ) -> bool:
        mr = self._max_retries if max_retries is None else max(0, int(max_retries))
        for attempt in range(mr + 1):
            if self._stop:
                return False
            try:
                if self.client.post_batch(chunk):
                    return True
            except Exception as exc:  # noqa: BLE001
                LOGGER.debug("cloud sync: post attempt %d failed: %s", attempt, exc)
            if attempt < mr and not self._stop:
                time.sleep(min(2 ** attempt, 8))
        return False

    def _advance(self, stream: str, ev: dict) -> None:
        with self._lock:
            self._cursor[stream] = {
                "ts": str(ev.get("ts") or ""),
                "content_id": str(ev.get("content_id") or ""),
            }
            self._persist_cursor()


__all__ = ["CloudSyncClient", "CloudSyncWorker"]
