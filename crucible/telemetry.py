"""
crucible/telemetry.py
==============================
Non-blocking telemetry event queue with pluggable sinks.

Inspired by Claude Code's structured event emission pattern: instead of
scattered ``print`` / ``log`` calls, the pipeline emits typed
``TelemetryEvent`` objects that flow through a background queue to one or
more registered sinks (console, JSONL file, external services).

Key design
----------
* **Non-blocking**: ``emit()`` enqueues the event and returns immediately.
  A daemon background thread drains the queue.
* **Pluggable sinks**: register any ``Callable[[TelemetryEvent], None]``
  as a sink.  Sinks run synchronously in the background thread.
* **Exception-isolated**: one broken sink never silences other sinks.
* **Graceful shutdown**: ``flush(timeout)`` drains the queue before process
  exit; ``shutdown()`` stops the background thread cleanly.
* **Thread-safe**: sink registration and the queue are both thread-safe.

Usage::

    from crucible.telemetry import emit, add_sink, TelemetryEvent

    def my_sink(event: TelemetryEvent) -> None:
        print(f"[{event.name}] {event.payload}")

    add_sink(my_sink)
    emit("stage.complete", payload={"stage": "analysis", "elapsed": 42.0})
"""
from __future__ import annotations

import atexit
import json
import os
import queue
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional

if __package__ == "crucible":
    from .runtime_logging import get_logger
    from .cancellation import OperationCancelledError
else:  # pragma: no cover
    from runtime_logging import get_logger  # type: ignore[no-redef]
    from cancellation import OperationCancelledError  # type: ignore[no-redef]

try:
    if __package__ == "crucible":
        from .run_correlation import get_run_id as _get_run_id
    else:
        from run_correlation import get_run_id as _get_run_id  # type: ignore
except ImportError:
    def _get_run_id() -> str:
        return ""

LOGGER = get_logger(__name__)

_SENTINEL = object()  # poison pill for graceful shutdown


# ── Data model ────────────────────────────────────────────────────────────────

@dataclass
class TelemetryEvent:
    """
    A single telemetry event.

    Attributes
    ----------
    name:
        Short dot-separated event name (e.g. ``"stage.complete"``).
    payload:
        Arbitrary key-value data associated with the event.
    timestamp:
        ISO-8601 UTC timestamp (auto-set at construction time).
    source:
        Optional source identifier (module or stage name).
    """

    name: str
    payload: Dict[str, Any] = field(default_factory=dict)
    timestamp: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    source: str = ""
    run_id: str = field(default_factory=lambda: _get_run_id())

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "timestamp": self.timestamp,
            "source": self.source,
            "run_id": self.run_id,
            "payload": self.payload,
        }


# ── Sink type alias ────────────────────────────────────────────────────────────

TelemetrySink = Callable[[TelemetryEvent], None]


# ── Built-in sinks ────────────────────────────────────────────────────────────

class JsonlFileSink:
    """
    Built-in sink that appends each event as a JSON line to a JSONL file.

    Parameters
    ----------
    log_path:
        Absolute or relative path to the output ``.jsonl`` file.
        Parent directories are created automatically.
    """

    def __init__(self, log_path: str) -> None:
        self._path = str(log_path)
        self._lock = threading.Lock()
        parent = os.path.dirname(os.path.abspath(self._path))
        if parent:
            os.makedirs(parent, exist_ok=True)

    def __call__(self, event: TelemetryEvent) -> None:
        line = json.dumps(event.to_dict(), ensure_ascii=False, separators=(",", ":"))
        with self._lock:
            try:
                with open(self._path, "a", encoding="utf-8") as fh:
                    fh.write(line + "\n")
            except OSError as exc:
                LOGGER.warning("JsonlFileSink: write failed: %s", exc)


# ── Queue / dispatcher ────────────────────────────────────────────────────────

class TelemetryQueue:
    """
    Background-threaded telemetry dispatcher.

    Parameters
    ----------
    maxsize:
        Maximum queue depth.  When full, ``emit()`` drops events silently
        (non-blocking).  Defaults to ``TELEMETRY_QUEUE_SIZE`` env var → 1 000.
    """

    def __init__(self, *, maxsize: Optional[int] = None) -> None:
        try:
            _size = max(
                10,
                int(os.environ.get("TELEMETRY_QUEUE_SIZE", "") or "1000"),
            )
        except (ValueError, TypeError):
            _size = 1000

        resolved_size = maxsize if maxsize is not None else _size
        self._queue: "queue.Queue[Any]" = queue.Queue(maxsize=resolved_size)
        self._sinks: List[TelemetrySink] = []
        self._sinks_lock = threading.Lock()
        self._dropped: int = 0
        self._dropped_lock = threading.Lock()
        # Serialises concurrent flush() callers so that at most one joiner
        # thread is alive at a time, preventing unbounded thread accumulation
        # when flush() is called rapidly while the sink is slow.
        self._flush_lock = threading.Lock()

        self._thread = threading.Thread(
            target=self._worker, name="telemetry-worker", daemon=True
        )
        self._thread.start()

    # ── Sink management ───────────────────────────────────────────────────────

    def add_sink(self, sink: TelemetrySink) -> None:
        """Register *sink* to receive all future events."""
        with self._sinks_lock:
            if sink not in self._sinks:
                self._sinks.append(sink)

    def remove_sink(self, sink: TelemetrySink) -> None:
        """Unregister *sink* (no-op if not registered)."""
        with self._sinks_lock:
            try:
                self._sinks.remove(sink)
            except ValueError:
                pass

    def clear_sinks(self) -> None:
        """Remove all registered sinks."""
        with self._sinks_lock:
            self._sinks.clear()

    # ── Emit ──────────────────────────────────────────────────────────────────

    def emit(self, event: TelemetryEvent) -> bool:
        """
        Enqueue *event* for delivery to all registered sinks.

        Non-blocking: if the queue is full the event is dropped and the
        ``dropped`` counter is incremented.

        Returns
        -------
        bool
            ``True`` if the event was successfully enqueued, ``False`` if it
            was dropped because the queue was full.
        """
        try:
            self._queue.put_nowait(event)
            return True
        except queue.Full:
            with self._dropped_lock:
                self._dropped += 1
            return False

    @property
    def dropped(self) -> int:
        """Number of events dropped due to a full queue."""
        with self._dropped_lock:
            return self._dropped

    # ── Control ───────────────────────────────────────────────────────────────

    def flush(self, timeout: float = 5.0) -> None:
        """Block until all enqueued events have been fully processed by sinks,
        or until *timeout* seconds have elapsed.

        Uses ``queue.join()`` rather than ``queue.empty()`` so that the method
        does not return prematurely while the worker thread is still inside a
        sink for the last dequeued item.  ``queue.join()`` only unblocks after
        ``task_done()`` is called, which happens *after* all sinks have been
        invoked for that event.
        """
        if timeout <= 0.0:
            return
        with self._flush_lock:
            done = threading.Event()

            def _join() -> None:
                try:
                    self._queue.join()
                finally:
                    done.set()

            joiner = threading.Thread(target=_join, name="telemetry-flush-joiner", daemon=True)
            joiner.start()
            done.wait(timeout=max(0.0, timeout))

    def shutdown(self, timeout: float = 5.0) -> None:
        """
        Signal the background worker to stop after draining remaining events,
        then wait up to *timeout* seconds for the thread to exit.

        Uses a blocking ``put`` (half the timeout budget) instead of
        ``put_nowait`` so the sentinel is deliverable even when the queue is
        temporarily full — e.g. if a slow sink is holding the worker while
        many events are queued.  The total elapsed time stays within *timeout*.
        """
        _start = time.monotonic()
        try:
            self._queue.put(
                _SENTINEL, block=True, timeout=max(0.1, timeout) / 2
            )
        except queue.Full:
            pass
        elapsed = time.monotonic() - _start
        self._thread.join(timeout=max(0.0, timeout - elapsed))

    # ── Worker ────────────────────────────────────────────────────────────────

    def _worker(self) -> None:
        while True:
            try:
                item = self._queue.get(timeout=0.1)
            except queue.Empty:
                continue
            if item is _SENTINEL:
                self._queue.task_done()
                break
            event: TelemetryEvent = item
            try:
                with self._sinks_lock:
                    sinks = list(self._sinks)
                for sink in sinks:
                    try:
                        sink(event)
                    except Exception as exc:
                        LOGGER.warning(
                            "TelemetryQueue: sink '%s' raised: %s",
                            getattr(sink, "__name__", repr(sink)),
                            exc,
                        )
            finally:
                self._queue.task_done()


# ── Module-level singleton ────────────────────────────────────────────────────

_GLOBAL_QUEUE: Optional[TelemetryQueue] = None
_QUEUE_LOCK = threading.Lock()


def _get_queue() -> TelemetryQueue:
    global _GLOBAL_QUEUE
    with _QUEUE_LOCK:
        if _GLOBAL_QUEUE is None:
            _GLOBAL_QUEUE = TelemetryQueue()
            atexit.register(_GLOBAL_QUEUE.shutdown, 3.0)
        return _GLOBAL_QUEUE


# ── Public API ────────────────────────────────────────────────────────────────

def emit(
    name: str,
    *,
    payload: Optional[Dict[str, Any]] = None,
    source: str = "",
) -> bool:
    """
    Emit a telemetry event to all registered sinks (non-blocking).

    Parameters
    ----------
    name:
        Event name (e.g. ``"stage.complete"``).
    payload:
        Arbitrary key-value data.
    source:
        Source identifier (module or stage name).

    Returns
    -------
    bool
        ``True`` if the event was enqueued, ``False`` if dropped (queue full).
    """
    event = TelemetryEvent(name=name, payload=payload or {}, source=source)
    return _get_queue().emit(event)


def get_dropped_count() -> int:
    """Return the number of events dropped due to a full queue."""
    return _get_queue().dropped


def add_sink(sink: TelemetrySink) -> None:
    """Register *sink* on the global telemetry queue."""
    _get_queue().add_sink(sink)


def remove_sink(sink: TelemetrySink) -> None:
    """Unregister *sink* from the global telemetry queue."""
    _get_queue().remove_sink(sink)


def clear_sinks() -> None:
    """Remove all sinks from the global telemetry queue."""
    _get_queue().clear_sinks()


def flush(timeout: float = 5.0) -> None:
    """Flush the global telemetry queue (block until empty or timeout)."""
    _get_queue().flush(timeout=timeout)


def shutdown(timeout: float = 5.0) -> None:
    """Shut down the global telemetry worker thread and reset the singleton."""
    global _GLOBAL_QUEUE
    with _QUEUE_LOCK:
        q = _GLOBAL_QUEUE
        _GLOBAL_QUEUE = None
    if q is not None:
        q.shutdown(timeout=timeout)


def reset_for_testing() -> None:
    """Tear down and recreate the global queue (for use in tests only)."""
    shutdown(timeout=2.0)


# ── OpenTelemetry OTLP export sink ────────────────────────────────────────────
#
# Exports pipeline telemetry events as OpenTelemetry spans to an OTLP endpoint
# (e.g. Jaeger, Tempo, Datadog) when the opentelemetry packages are installed.
#
# Activation
# ----------
# Set OTEL_EXPORTER_OTLP_ENDPOINT in the environment (e.g.
# ``http://localhost:4317`` for a local Jaeger / OTel Collector).  The sink
# is created and registered automatically when this module is imported and the
# env var is set, OR can be activated programmatically via
# ``activate_otel_sink()``.
#
# Optional dependencies
# ---------------------
#   pip install opentelemetry-sdk opentelemetry-exporter-otlp-proto-grpc
#
# If the packages are not installed, calls to ``activate_otel_sink()`` are
# silently no-ops and no error is raised.

try:
    from opentelemetry import trace as _otel_trace
    from opentelemetry.sdk.resources import Resource as _Resource
    from opentelemetry.sdk.trace import TracerProvider as _TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor as _BatchSpanProcessor
    _HAS_OTEL_SDK = True
except ImportError:
    _HAS_OTEL_SDK = False

try:
    from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import (
        OTLPSpanExporter as _OTLPSpanExporter,
    )
    _HAS_OTEL_GRPC = True
except ImportError:
    _HAS_OTEL_GRPC = False

try:
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
        OTLPSpanExporter as _OTLPHttpSpanExporter,
    )
    _HAS_OTEL_HTTP = True
except ImportError:
    _HAS_OTEL_HTTP = False


class OtelSpanSink:
    """
    Telemetry sink that emits each ``TelemetryEvent`` as an OpenTelemetry
    span to a configured OTLP endpoint.

    Each event becomes a root span named ``event.name`` with the event's
    payload mapped to span attributes.  Attribute values are coerced to
    strings, ints, floats, or bools — the only types OTel attributes accept.

    Parameters
    ----------
    endpoint:
        OTLP endpoint URL (e.g. ``"http://localhost:4317"`` for gRPC,
        ``"http://localhost:4318/v1/traces"`` for HTTP).
    service_name:
        ``service.name`` resource attribute (default ``"crucible"``).
    use_http:
        If True, use OTLP/HTTP instead of OTLP/gRPC.  Auto-detected when
        the endpoint contains ``/v1/traces``.

    Raises
    ------
    ImportError
        If neither ``opentelemetry-sdk`` nor the appropriate OTLP exporter
        package is installed.
    """

    def __init__(
        self,
        endpoint: str,
        *,
        service_name: str = "crucible",
        use_http: bool = False,
    ) -> None:
        if not _HAS_OTEL_SDK:
            raise ImportError(
                "OtelSpanSink requires opentelemetry-sdk. "
                "Install with: pip install opentelemetry-sdk "
                "opentelemetry-exporter-otlp-proto-grpc"
            )

        # Auto-detect HTTP vs gRPC from endpoint path
        _use_http = use_http or "/v1/traces" in endpoint

        if _use_http:
            if not _HAS_OTEL_HTTP:
                raise ImportError(
                    "OTLP/HTTP export requires opentelemetry-exporter-otlp-proto-http. "
                    "Install with: pip install opentelemetry-exporter-otlp-proto-http"
                )
            exporter = _OTLPHttpSpanExporter(endpoint=endpoint)
        else:
            if not _HAS_OTEL_GRPC:
                raise ImportError(
                    "OTLP/gRPC export requires opentelemetry-exporter-otlp-proto-grpc. "
                    "Install with: pip install opentelemetry-exporter-otlp-proto-grpc"
                )
            exporter = _OTLPSpanExporter(endpoint=endpoint)

        resource = _Resource.create({"service.name": service_name})
        provider = _TracerProvider(resource=resource)
        provider.add_span_processor(_BatchSpanProcessor(exporter))
        _otel_trace.set_tracer_provider(provider)

        self._tracer = _otel_trace.get_tracer(service_name)
        self._provider = provider

    def __call__(self, event: "TelemetryEvent") -> None:
        """Emit *event* as an OTel root span."""
        with self._tracer.start_as_current_span(event.name) as span:
            # Map event fields to span attributes
            span.set_attribute("event.source", event.source or "")
            span.set_attribute("event.run_id", event.run_id or "")
            span.set_attribute("event.timestamp", event.timestamp)
            # Map payload values — OTel only accepts str, int, float, bool
            for k, v in (event.payload or {}).items():
                attr_key = f"payload.{k}"
                if isinstance(v, bool):
                    span.set_attribute(attr_key, v)
                elif isinstance(v, int):
                    span.set_attribute(attr_key, v)
                elif isinstance(v, float):
                    span.set_attribute(attr_key, v)
                else:
                    span.set_attribute(attr_key, str(v))

    def shutdown(self, timeout: float = 5.0) -> None:
        """Flush and shut down the underlying tracer provider."""
        try:
            self._provider.force_flush(int(timeout * 1000))
            self._provider.shutdown()
        except OperationCancelledError:
            # Subclass of ``RuntimeError``/``Exception`` — explicit re-raise
            # so user cancellation isn't swallowed during shutdown.
            raise
        except Exception:
            pass


def activate_otel_sink(
    *,
    endpoint: Optional[str] = None,
    service_name: str = "crucible",
    use_http: bool = False,
) -> bool:
    """
    Create and register an ``OtelSpanSink`` on the global telemetry queue.

    Parameters
    ----------
    endpoint:
        OTLP endpoint URL.  Defaults to the ``OTEL_EXPORTER_OTLP_ENDPOINT``
        environment variable.  Returns False (no-op) if neither is provided.
    service_name:
        ``service.name`` resource attribute.
    use_http:
        Force OTLP/HTTP transport (auto-detected when omitted).

    Returns
    -------
    bool
        True if the sink was successfully registered, False otherwise.
    """
    resolved_endpoint = endpoint or os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", "").strip()
    if not resolved_endpoint:
        return False

    if not _HAS_OTEL_SDK:
        LOGGER.warning(
            "activate_otel_sink: opentelemetry-sdk not installed — "
            "OTLP export disabled.  Install with: "
            "pip install opentelemetry-sdk opentelemetry-exporter-otlp-proto-grpc"
        )
        return False

    try:
        sink = OtelSpanSink(
            resolved_endpoint,
            service_name=service_name,
            use_http=use_http,
        )
        add_sink(sink)
        LOGGER.info(
            "activate_otel_sink: OTLP export active → %s (service=%s)",
            resolved_endpoint,
            service_name,
        )
        return True
    except Exception as exc:
        LOGGER.warning("activate_otel_sink: failed to create OTLP sink: %s", exc)
        return False


# Auto-activate OTLP sink when OTEL_EXPORTER_OTLP_ENDPOINT is set at import time.
def _auto_activate_otel() -> None:
    if os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", "").strip():
        activate_otel_sink()


_auto_activate_otel()
