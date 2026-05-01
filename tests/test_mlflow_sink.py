# ruff: noqa: E402
"""Tests for crucible.features.mlflow_sink.

All tests run without a real MLflow server by patching _try_import_mlflow to
return a MagicMock that mimics the mlflow module API surface.

The MlflowSink implementation uses mlflow.tracking.MlflowClient for all
run-lifecycle operations (create_run, log_param, log_metric, log_artifact,
set_terminated).  Tests mock MlflowClient accordingly.
"""
import os
import sys
import threading
import unittest
from typing import Any
from unittest.mock import MagicMock, call, patch

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from crucible.telemetry import TelemetryEvent


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_event(
    name: str = "pipeline.complete",
    payload: dict | None = None,
    source: str = "test",
) -> TelemetryEvent:
    return TelemetryEvent(
        name=name,
        payload=payload or {},
        source=source,
        run_id="test-run-001",
    )


def _mock_mlflow() -> MagicMock:
    """Return a MagicMock mimicking the mlflow module used by MlflowSink.

    The new implementation calls mlflow.tracking.MlflowClient() for all
    run-lifecycle operations, so we set up the nested mock accordingly.
    """
    m = MagicMock()

    exp = MagicMock()
    exp.experiment_id = "exp-1"
    m.set_experiment.return_value = exp

    # Mock MlflowClient instance returned by mlflow.tracking.MlflowClient()
    mock_run = MagicMock()
    mock_run.info.run_id = "mlflow-run-abc"

    client = MagicMock()
    client.create_run.return_value = mock_run
    m.tracking.MlflowClient.return_value = client

    return m


def _get_client(mock_mlflow: MagicMock) -> MagicMock:
    """Return the MlflowClient mock instance from the mock mlflow module."""
    return mock_mlflow.tracking.MlflowClient.return_value


# ── Tests for MlflowSink ──────────────────────────────────────────────────────

class TestMlflowSinkIgnoresUnknownEvents(unittest.TestCase):
    """Events outside _LOGGABLE_EVENTS must be silently dropped."""

    def test_unknown_event_not_logged(self) -> None:
        from crucible.features.mlflow_sink import MlflowSink
        mock_mlflow = _mock_mlflow()
        sink = MlflowSink(tracking_uri="http://localhost:5000")
        event = _make_event(name="some.unknown.event")
        with patch(
            "crucible.features.mlflow_sink._try_import_mlflow",
            return_value=mock_mlflow,
        ):
            sink(event)
        _get_client(mock_mlflow).create_run.assert_not_called()


class TestMlflowSinkMlflowUnavailable(unittest.TestCase):
    """When mlflow is not installed the sink must be a no-op."""

    def test_no_op_when_unavailable(self) -> None:
        from crucible.features.mlflow_sink import MlflowSink, _MLFLOW_UNAVAILABLE
        sink = MlflowSink(tracking_uri="http://localhost:5000")
        event = _make_event("pipeline.complete")
        with patch(
            "crucible.features.mlflow_sink._try_import_mlflow",
            return_value=_MLFLOW_UNAVAILABLE,
        ):
            # Must not raise
            sink(event)


class TestMlflowSinkPipelineComplete(unittest.TestCase):
    """pipeline.complete event should create (or reuse) a top-level MLflow run."""

    def setUp(self) -> None:
        from crucible.features.mlflow_sink import MlflowSink
        self.mock_mlflow = _mock_mlflow()
        self.sink = MlflowSink(
            tracking_uri="http://localhost:5000",
            experiment_name="TestExp",
        )

    def _call(self, event: TelemetryEvent) -> None:
        with patch(
            "crucible.features.mlflow_sink._try_import_mlflow",
            return_value=self.mock_mlflow,
        ):
            self.sink(event)

    def test_create_run_called(self) -> None:
        self._call(_make_event("pipeline.complete"))
        _get_client(self.mock_mlflow).create_run.assert_called_once()

    def test_set_experiment_called_with_name(self) -> None:
        self._call(_make_event("pipeline.complete"))
        self.mock_mlflow.set_experiment.assert_called_once_with("TestExp")

    def test_tracking_uri_set(self) -> None:
        self._call(_make_event("pipeline.complete"))
        self.mock_mlflow.set_tracking_uri.assert_called_once_with("http://localhost:5000")

    def test_metrics_logged(self) -> None:
        event = _make_event(
            "pipeline.complete",
            payload={"score": 75, "sharpe_ratio": 1.5, "cost_usd": 0.02},
        )
        self._call(event)
        client = _get_client(self.mock_mlflow)
        logged_metrics = {
            c.args[1]: c.args[2]
            for c in client.log_metric.call_args_list
        }
        self.assertAlmostEqual(logged_metrics.get("score"), 75.0)
        self.assertAlmostEqual(logged_metrics.get("sharpe_ratio"), 1.5)

    def test_params_logged(self) -> None:
        event = _make_event(
            "pipeline.complete",
            payload={"mode": "quant", "provider": "openrouter"},
        )
        self._call(event)
        client = _get_client(self.mock_mlflow)
        # log_param is called per-key (not log_params in bulk)
        logged = {c.args[1]: c.args[2] for c in client.log_param.call_args_list}
        self.assertEqual(logged.get("mode"), "quant")
        self.assertEqual(logged.get("provider"), "openrouter")

    def test_tags_logged(self) -> None:
        event = _make_event("pipeline.complete")
        self._call(event)
        client = _get_client(self.mock_mlflow)
        # Tags are passed as a dict to create_run()
        client.create_run.assert_called_once()
        call_kwargs = client.create_run.call_args.kwargs
        tags = call_kwargs.get("tags") or {}
        self.assertIn("event_name", tags)

    def test_non_numeric_metric_not_logged(self) -> None:
        event = _make_event(
            "pipeline.complete",
            payload={"score": "N/A"},
        )
        self._call(event)
        client = _get_client(self.mock_mlflow)
        # "N/A" → TypeError/ValueError → should be skipped silently
        for c in client.log_metric.call_args_list:
            self.assertNotEqual(c.args[1], "score")

    def test_active_run_id_cleared_after_complete(self) -> None:
        """_active_run_id must be None after pipeline.complete finishes."""
        self._call(_make_event("pipeline.complete"))
        self.assertIsNone(self.sink._active_run_id)


class TestMlflowSinkPipelineStart(unittest.TestCase):
    """pipeline.start event must set _active_run_id for subsequent stage nesting."""

    def _call(self, sink: Any, event: TelemetryEvent, mock_mlflow: MagicMock) -> None:
        with patch(
            "crucible.features.mlflow_sink._try_import_mlflow",
            return_value=mock_mlflow,
        ):
            sink(event)

    def test_pipeline_start_sets_active_run_id(self) -> None:
        from crucible.features.mlflow_sink import MlflowSink
        mock_mlflow = _mock_mlflow()
        sink = MlflowSink(tracking_uri="http://localhost:5000")
        self._call(sink, _make_event("pipeline.start"), mock_mlflow)
        # After pipeline.start, _active_run_id is set to the created run's ID
        self.assertEqual(sink._active_run_id, "mlflow-run-abc")

    def test_pipeline_start_creates_run(self) -> None:
        from crucible.features.mlflow_sink import MlflowSink
        mock_mlflow = _mock_mlflow()
        sink = MlflowSink(tracking_uri="http://localhost:5000")
        self._call(sink, _make_event("pipeline.start"), mock_mlflow)
        _get_client(mock_mlflow).create_run.assert_called_once()

    def test_pipeline_start_does_not_terminate_run(self) -> None:
        """The parent run must stay RUNNING after pipeline.start."""
        from crucible.features.mlflow_sink import MlflowSink
        mock_mlflow = _mock_mlflow()
        sink = MlflowSink(tracking_uri="http://localhost:5000")
        self._call(sink, _make_event("pipeline.start"), mock_mlflow)
        _get_client(mock_mlflow).set_terminated.assert_not_called()


class TestMlflowSinkActiveRunIdClearedOnException(unittest.TestCase):
    """
    Regression: _active_run_id must be reset to None after pipeline.complete,
    even when an MLflow client method raises mid-execution.
    """

    def test_active_run_id_cleared_after_exception(self) -> None:
        from crucible.features.mlflow_sink import MlflowSink

        mock_mlflow = _mock_mlflow()
        client = _get_client(mock_mlflow)
        # Make log_metric raise an exception during pipeline.complete processing
        client.log_metric.side_effect = RuntimeError("mlflow server down")

        sink = MlflowSink(tracking_uri="http://localhost:5000")
        # Simulate a leftover active run (e.g. pipeline.start happened earlier)
        sink._active_run_id = "stale-id"

        event = _make_event("pipeline.complete")
        with patch(
            "crucible.features.mlflow_sink._try_import_mlflow",
            return_value=mock_mlflow,
        ):
            # Exception is caught by __call__; must not propagate
            sink(event)

        # The finally block must have cleared _active_run_id
        self.assertIsNone(sink._active_run_id)


class TestMlflowSinkStageEvent(unittest.TestCase):
    """stage.complete events: nested under pipeline when active run exists."""

    def _call(self, sink: Any, event: TelemetryEvent, mock_mlflow: MagicMock) -> None:
        with patch(
            "crucible.features.mlflow_sink._try_import_mlflow",
            return_value=mock_mlflow,
        ):
            sink(event)

    def test_stage_not_nested_without_active_run(self) -> None:
        from crucible.features.mlflow_sink import MlflowSink
        mock_mlflow = _mock_mlflow()
        sink = MlflowSink(tracking_uri="http://localhost:5000")
        # No active pipeline run
        self.assertIsNone(sink._active_run_id)
        self._call(sink, _make_event("stage.complete"), mock_mlflow)
        client = _get_client(mock_mlflow)
        client.create_run.assert_called_once()
        call_kwargs = client.create_run.call_args.kwargs
        tags = call_kwargs.get("tags") or {}
        # No parent run ID tag when no active pipeline
        self.assertNotIn("mlflow.parentRunId", tags)

    def test_stage_nested_when_active_run_exists(self) -> None:
        from crucible.features.mlflow_sink import MlflowSink
        mock_mlflow = _mock_mlflow()
        sink = MlflowSink(tracking_uri="http://localhost:5000")
        sink._active_run_id = "pipeline-parent-run"
        self._call(sink, _make_event("stage.complete"), mock_mlflow)
        client = _get_client(mock_mlflow)
        client.create_run.assert_called_once()
        call_kwargs = client.create_run.call_args.kwargs
        tags = call_kwargs.get("tags") or {}
        # Nested stage must carry the parent run ID tag
        self.assertEqual(tags.get("mlflow.parentRunId"), "pipeline-parent-run")

    def test_stage_does_not_clear_active_run_id(self) -> None:
        from crucible.features.mlflow_sink import MlflowSink
        mock_mlflow = _mock_mlflow()
        sink = MlflowSink(tracking_uri="http://localhost:5000")
        sink._active_run_id = "pipeline-run-xyz"
        self._call(sink, _make_event("stage.complete"), mock_mlflow)
        # Stage complete must NOT clear the active pipeline run
        self.assertEqual(sink._active_run_id, "pipeline-run-xyz")


class TestMlflowSinkArtifactLogging(unittest.TestCase):
    """Artifact upload only happens when log_artifacts=True and report_path exists."""

    def _call(self, sink: Any, event: TelemetryEvent, mock_mlflow: MagicMock) -> None:
        with patch(
            "crucible.features.mlflow_sink._try_import_mlflow",
            return_value=mock_mlflow,
        ):
            sink(event)

    def test_artifact_not_uploaded_without_flag(self) -> None:
        from crucible.features.mlflow_sink import MlflowSink
        mock_mlflow = _mock_mlflow()
        sink = MlflowSink(tracking_uri="http://localhost:5000", log_artifacts=False)
        event = _make_event("pipeline.complete", payload={"report_path": "/fake/report.html"})
        self._call(sink, event, mock_mlflow)
        _get_client(mock_mlflow).log_artifact.assert_not_called()

    def test_artifact_not_uploaded_when_file_missing(self) -> None:
        from crucible.features.mlflow_sink import MlflowSink
        mock_mlflow = _mock_mlflow()
        sink = MlflowSink(tracking_uri="http://localhost:5000", log_artifacts=True)
        event = _make_event(
            "pipeline.complete",
            payload={"report_path": "/nonexistent/path/report.html"},
        )
        self._call(sink, event, mock_mlflow)
        _get_client(mock_mlflow).log_artifact.assert_not_called()

    def test_artifact_uploaded_when_file_exists(self) -> None:
        import tempfile
        from crucible.features.mlflow_sink import MlflowSink
        mock_mlflow = _mock_mlflow()
        sink = MlflowSink(tracking_uri="http://localhost:5000", log_artifacts=True)
        with tempfile.NamedTemporaryFile(suffix=".html", delete=False) as f:
            tmp_path = f.name
        try:
            event = _make_event("pipeline.complete", payload={"report_path": tmp_path})
            self._call(sink, event, mock_mlflow)
            client = _get_client(mock_mlflow)
            client.log_artifact.assert_called_once_with(
                "mlflow-run-abc", tmp_path, artifact_path="reports"
            )
        finally:
            os.unlink(tmp_path)


class TestMlflowSinkThreadSafety(unittest.TestCase):
    """MlflowSink must be callable from multiple threads without data races."""

    def test_concurrent_events_do_not_raise(self) -> None:
        """Verify that a single shared MlflowSink instance is safe under concurrent access."""
        from crucible.features.mlflow_sink import MlflowSink
        errors: list[Exception] = []
        shared_mock = _mock_mlflow()
        shared_sink = MlflowSink(tracking_uri="http://localhost:5000")

        def thread_task() -> None:
            event = _make_event("pipeline.complete")
            with patch(
                "crucible.features.mlflow_sink._try_import_mlflow",
                return_value=shared_mock,
            ):
                try:
                    shared_sink(event)
                except Exception as exc:
                    errors.append(exc)

        threads = [threading.Thread(target=thread_task) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])


class TestMlflowSinkRepr(unittest.TestCase):
    def test_repr_contains_uri(self) -> None:
        from crucible.features.mlflow_sink import MlflowSink
        sink = MlflowSink(tracking_uri="http://localhost:5000", experiment_name="MyExp")
        r = repr(sink)
        self.assertIn("localhost:5000", r)
        self.assertIn("MyExp", r)


# ── Tests for register_mlflow_sink ────────────────────────────────────────────

class TestRegisterMlflowSink(unittest.TestCase):
    """register_mlflow_sink idempotency and guard conditions."""

    def _reset_registration(self) -> None:
        """Reset the module-level _SINK_REGISTERED sentinel between tests."""
        import crucible.features.mlflow_sink as mod
        mod._SINK_REGISTERED = False

    def test_returns_false_without_tracking_uri(self) -> None:
        from crucible.features.mlflow_sink import register_mlflow_sink
        self._reset_registration()
        env = {k: v for k, v in os.environ.items() if k != "MLFLOW_TRACKING_URI"}
        with patch.dict(os.environ, env, clear=True):
            result = register_mlflow_sink()
        self.assertFalse(result)

    def test_returns_false_when_mlflow_unavailable(self) -> None:
        from crucible.features.mlflow_sink import register_mlflow_sink, _MLFLOW_UNAVAILABLE
        self._reset_registration()
        with patch(
            "crucible.features.mlflow_sink._try_import_mlflow",
            return_value=_MLFLOW_UNAVAILABLE,
        ):
            result = register_mlflow_sink(tracking_uri="http://localhost:5000")
        self.assertFalse(result)

    def test_idempotent_second_call_returns_true(self) -> None:
        from crucible.features.mlflow_sink import register_mlflow_sink
        self._reset_registration()
        mock_mlflow = _mock_mlflow()
        with patch(
            "crucible.features.mlflow_sink._try_import_mlflow",
            return_value=mock_mlflow,
        ), patch("crucible.features.mlflow_sink.add_sink"):
            result1 = register_mlflow_sink(tracking_uri="http://localhost:5000")
            result2 = register_mlflow_sink(tracking_uri="http://localhost:5000")
        self.assertTrue(result1)
        self.assertTrue(result2)

    def tearDown(self) -> None:
        # Clean up module state after each test in this class
        import crucible.features.mlflow_sink as mod
        mod._SINK_REGISTERED = False


if __name__ == "__main__":
    unittest.main()
