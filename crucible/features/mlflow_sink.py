"""
features/mlflow_sink.py
=========================
MLflow experiment tracking sink for Crucible pipeline runs.

Implements the ``TelemetrySink`` protocol to automatically log every pipeline
run as an MLflow experiment when ``MLFLOW_TRACKING_URI`` is set in the
environment.  All MLflow imports are guarded inside try/except blocks so the
pipeline never crashes if MLflow is not installed.

Usage::

    # Auto-activates at import time when MLFLOW_TRACKING_URI is set.
    from crucible.features.mlflow_sink import register_mlflow_sink
    register_mlflow_sink()   # idempotent; called once at pipeline startup

    # Or manually construct and register the sink:
    from crucible.features.mlflow_sink import MlflowSink
    from crucible.telemetry import add_sink
    add_sink(MlflowSink())

Environment variables::

    MLFLOW_TRACKING_URI     MLflow server URI (e.g. ``http://localhost:5000``).
                            If not set, the sink is a no-op and will not be
                            registered automatically.
    MLFLOW_EXPERIMENT_NAME  Experiment name (default ``"Crucible"``).
    MLFLOW_LOG_ARTIFACTS    Set to ``"1"`` to upload the HTML report as an
                            MLflow artifact when a ``pipeline.complete`` event
                            carries a ``report_path`` in its payload.
"""
from __future__ import annotations

import math
import os
import threading
from typing import Any, Dict, Optional

if __package__ == "crucible.features":
    # Relative imports when loaded as part of the package
    from ..runtime_logging import get_logger
    from ..telemetry import TelemetryEvent, add_sink
elif __package__ == "crucible":  # pragma: no cover
    from .runtime_logging import get_logger  # type: ignore[no-redef]
    from .telemetry import TelemetryEvent, add_sink  # type: ignore[no-redef]
else:  # pragma: no cover — direct script fallback
    # Allow the file to be imported directly in tests without the full package.
    import os as _os
    import sys as _sys

    _pkg_root = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
    if _pkg_root not in _sys.path:
        _sys.path.insert(0, _pkg_root)
    from crucible.runtime_logging import get_logger
    from crucible.telemetry import TelemetryEvent, add_sink

LOGGER = get_logger(__name__)

# ── Configuration ─────────────────────────────────────────────────────────────

_DEFAULT_EXPERIMENT_NAME = "Crucible"

# Metrics from event payload that we recognise and forward to MLflow.
_NUMERIC_METRIC_KEYS = (
    "score",
    "cost_usd",
    "total_tokens",
    "sharpe_ratio",
    "max_drawdown",       # legacy alias kept for backward compat
    "max_drawdown_pct",   # canonical BacktestMetrics field name
    "total_return_pct",
    "win_rate",
    "trade_count",
    "profit_factor",
    "annualised_volatility",
    "annualized_volatility",  # US-spelling alias
    "calmar_ratio",
    "sortino_ratio",
    "alpha",
    "beta",
    "elapsed_seconds",
    "utilization",
)

# Params from event payload forwarded as MLflow run parameters.
_PARAM_KEYS = (
    "mode",
    "provider",
    "llm_provider",
    "analysis_type",
    "model_id",
    "param_search",
)

# Tags always recorded on every run.
_TAG_KEYS = (
    "run_id",
    "timestamp",
    "source",
)

# Events that trigger a logged run in MLflow.
_LOGGABLE_EVENTS = frozenset({"pipeline.start", "pipeline.complete", "stage.complete"})

# Sentinel returned by _try_import_mlflow when mlflow is unavailable.
_MLFLOW_UNAVAILABLE = object()


def _try_import_mlflow() -> Any:
    """
    Attempt to import the ``mlflow`` package.

    Returns the ``mlflow`` module on success, or ``_MLFLOW_UNAVAILABLE`` if the
    package is not installed.  Never raises.
    """
    try:
        import mlflow
        return mlflow
    except ImportError:
        return _MLFLOW_UNAVAILABLE


# ── Sink implementation ───────────────────────────────────────────────────────


class MlflowSink:
    """
    Telemetry sink that logs Crucible pipeline events to MLflow.

    Implements the ``TelemetrySink`` protocol: callable as
    ``sink(event: TelemetryEvent) -> None``.

    One MLflow run is created per ``pipeline.complete`` event.
    ``stage.complete`` events are logged as nested runs under the active
    top-level run if one exists; otherwise as top-level runs.

    Thread-safe: a lock guards all MLflow API calls to prevent concurrent
    run-lifecycle issues when the telemetry queue dispatches events in
    parallel.

    Parameters
    ----------
    tracking_uri:
        MLflow tracking server URI.  Defaults to ``MLFLOW_TRACKING_URI``.
    experiment_name:
        MLflow experiment name.  Defaults to ``MLFLOW_EXPERIMENT_NAME`` env
        var or ``"Crucible"``.
    log_artifacts:
        If True, upload the HTML report file as an artifact on
        ``pipeline.complete`` events that carry a ``report_path`` payload key.
        Defaults to ``MLFLOW_LOG_ARTIFACTS == "1"``.
    """

    def __init__(
        self,
        tracking_uri: Optional[str] = None,
        *,
        experiment_name: Optional[str] = None,
        log_artifacts: Optional[bool] = None,
    ) -> None:
        self._tracking_uri = (
            tracking_uri or os.environ.get("MLFLOW_TRACKING_URI", "").strip()
        )
        self._experiment_name = (
            experiment_name
            or os.environ.get("MLFLOW_EXPERIMENT_NAME", "").strip()
            or _DEFAULT_EXPERIMENT_NAME
        )
        self._log_artifacts: bool = (
            log_artifacts
            if log_artifacts is not None
            else os.environ.get("MLFLOW_LOG_ARTIFACTS", "").strip() == "1"
        )
        self._lock = threading.Lock()
        # Active top-level run ID (set when a pipeline.start event is received)
        self._active_run_id: Optional[str] = None

    # ── Protocol entry point ──────────────────────────────────────────────────

    def __call__(self, event: TelemetryEvent) -> None:
        """
        Process a single telemetry event.

        Ignored event names are dropped silently.  Any exception raised by
        MLflow is caught and logged as a warning so that one broken log call
        never propagates to the telemetry worker thread.
        """
        if event.name not in _LOGGABLE_EVENTS:
            return
        try:
            self._log_event(event)
        except Exception as exc:
            LOGGER.warning(
                "MlflowSink: failed to log event '%s': %s", event.name, exc
            )

    # ── Internal logging ──────────────────────────────────────────────────────

    def _log_event(self, event: TelemetryEvent) -> None:
        """Log *event* to MLflow.  Raises on MLflow errors (caller catches)."""
        mlflow = _try_import_mlflow()
        if mlflow is _MLFLOW_UNAVAILABLE:
            return

        with self._lock:
            if self._tracking_uri:
                mlflow.set_tracking_uri(self._tracking_uri)

            experiment = mlflow.set_experiment(self._experiment_name)
            payload: Dict[str, Any] = event.payload or {}
            is_pipeline_complete = event.name == "pipeline.complete"
            is_pipeline_start = event.name == "pipeline.start"

            # Build common event tags
            tags: Dict[str, str] = {"event_name": event.name}
            for key in _TAG_KEYS:
                val = payload.get(key)
                if val is None:
                    val = getattr(event, key, None)
                if val is not None:
                    tags[key] = str(val)

            client = mlflow.tracking.MlflowClient()

            if is_pipeline_start:
                # Terminate any previously-orphaned parent run before starting a new one.
                # Without this guard, a pipeline restart (or two pipelines sharing the same
                # MlflowSink instance) would overwrite _active_run_id and leave the old run
                # permanently in RUNNING state with no way to clean it up.
                if self._active_run_id is not None:
                    try:
                        client.set_terminated(self._active_run_id)
                    except Exception:
                        pass
                    self._active_run_id = None
                # Create a long-lived parent pipeline run.
                # It remains in RUNNING state until pipeline.complete ends it.
                run_name = f"pipeline-{event.run_id or event.timestamp[:10]}"
                run = client.create_run(
                    experiment_id=experiment.experiment_id,
                    run_name=run_name,
                    tags=tags,
                )
                self._active_run_id = run.info.run_id
                # No params/metrics to log on start — they come with pipeline.complete.
                return

            if is_pipeline_complete and self._active_run_id is not None:
                # Log final metrics into the existing parent run, then end it.
                run_id = self._active_run_id
            else:
                # Create a new run. For stage events, nest under the parent pipeline run
                # by setting the mlflow.parentRunId tag (thread-safe, no thread-local
                # active-run dependency).
                run_name = (
                    f"pipeline-{event.run_id or event.timestamp[:10]}"
                    if is_pipeline_complete
                    else f"stage-{payload.get('stage', event.source or 'unknown')}"
                )
                run_tags = dict(tags)
                if not is_pipeline_complete and self._active_run_id is not None:
                    run_tags["mlflow.parentRunId"] = self._active_run_id
                run = client.create_run(
                    experiment_id=experiment.experiment_id,
                    run_name=run_name,
                    tags=run_tags,
                )
                run_id = run.info.run_id

            try:
                # ── Params ────────────────────────────────────────────────────────
                for key in _PARAM_KEYS:
                    val = payload.get(key)
                    if val is not None:
                        client.log_param(run_id, key, str(val))

                # ── Metrics ───────────────────────────────────────────────────────
                for key in _NUMERIC_METRIC_KEYS:
                    val = payload.get(key)
                    if val is None:
                        continue
                    try:
                        fv = float(val)
                        if math.isfinite(fv):
                            client.log_metric(run_id, key, fv)
                    except (TypeError, ValueError):
                        pass

                # ── Artifacts ─────────────────────────────────────────────────────
                if self._log_artifacts and is_pipeline_complete:
                    report_path = str(payload.get("report_path") or "").strip()
                    if report_path and os.path.isfile(report_path):
                        try:
                            client.log_artifact(run_id, report_path, artifact_path="reports")
                        except Exception as art_exc:
                            LOGGER.warning(
                                "MlflowSink: artifact upload failed for '%s': %s",
                                report_path, art_exc,
                            )
            finally:
                # End every run we process.  For pipeline.complete, this ends the
                # parent run that was started on pipeline.start.
                try:
                    client.set_terminated(run_id)
                except Exception:
                    pass
                if is_pipeline_complete:
                    self._active_run_id = None

    # ── String representation ─────────────────────────────────────────────────

    def __repr__(self) -> str:
        return (
            f"MlflowSink(tracking_uri={self._tracking_uri!r}, "
            f"experiment={self._experiment_name!r}, "
            f"log_artifacts={self._log_artifacts})"
        )


# ── Registration helper ───────────────────────────────────────────────────────

# Module-level guard: only register once per process.
_SINK_REGISTERED = False
_REGISTRATION_LOCK = threading.Lock()


def register_mlflow_sink(
    *,
    tracking_uri: Optional[str] = None,
    experiment_name: Optional[str] = None,
    log_artifacts: Optional[bool] = None,
) -> bool:
    """
    Create and register an ``MlflowSink`` on the global telemetry queue.

    This function is idempotent: calling it multiple times registers the sink
    only once per process.

    Prerequisites:
    - ``MLFLOW_TRACKING_URI`` must be set (or *tracking_uri* must be provided).
    - ``mlflow`` must be installed (``pip install mlflow``).

    Parameters
    ----------
    tracking_uri:
        MLflow tracking server URI.  Falls back to ``MLFLOW_TRACKING_URI`` env var.
    experiment_name:
        MLflow experiment name.  Falls back to ``MLFLOW_EXPERIMENT_NAME`` env
        var, then ``"Crucible"``.
    log_artifacts:
        If True, upload the HTML report as an MLflow artifact on
        ``pipeline.complete`` events.  Falls back to ``MLFLOW_LOG_ARTIFACTS`` env var.

    Returns
    -------
    bool
        ``True`` if the sink was (or was already) registered successfully.
        ``False`` if MLflow is unavailable, the tracking URI is unset, or any
        setup error occurred.
    """
    global _SINK_REGISTERED

    with _REGISTRATION_LOCK:
        if _SINK_REGISTERED:
            return True

        resolved_uri = (
            tracking_uri or os.environ.get("MLFLOW_TRACKING_URI", "").strip()
        )
        if not resolved_uri:
            LOGGER.debug(
                "register_mlflow_sink: MLFLOW_TRACKING_URI not set — sink not registered."
            )
            return False

        mlflow = _try_import_mlflow()
        if mlflow is _MLFLOW_UNAVAILABLE:
            LOGGER.warning(
                "register_mlflow_sink: mlflow is not installed — sink not registered. "
                "Install with: pip install mlflow"
            )
            return False

        try:
            sink = MlflowSink(
                tracking_uri=resolved_uri,
                experiment_name=experiment_name,
                log_artifacts=log_artifacts,
            )
            add_sink(sink)
            _SINK_REGISTERED = True
            LOGGER.info(
                "register_mlflow_sink: MLflow sink registered → %s (experiment=%s)",
                resolved_uri,
                sink._experiment_name,
            )
            return True
        except Exception as exc:
            LOGGER.warning("register_mlflow_sink: failed to register sink: %s", exc)
            return False


# Auto-register when the module is imported and the env var is set.
def _auto_register() -> None:
    if os.environ.get("MLFLOW_TRACKING_URI", "").strip():
        register_mlflow_sink()


_auto_register()
