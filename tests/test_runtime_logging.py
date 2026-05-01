import io
import json
import logging
import os
import sys
import unittest
from unittest.mock import patch

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import crucible.runtime_logging as runtime_logging  # noqa: E402
from crucible.runtime_logging import (  # noqa: E402
    clear_log_context,
    configure_logging,
    get_logger,
    log_context,
    log_event,
    update_log_context,
)


class TestRuntimeLogging(unittest.TestCase):
    def setUp(self) -> None:
        self.root_logger = logging.getLogger()
        self.original_handlers = list(self.root_logger.handlers)
        self.original_level = self.root_logger.level
        self.original_configured = runtime_logging._CONFIGURED

    def tearDown(self) -> None:
        clear_log_context()
        self.root_logger.handlers = self.original_handlers
        self.root_logger.setLevel(self.original_level)
        runtime_logging._CONFIGURED = self.original_configured

    def test_configure_logging_preserves_existing_root_handlers_without_force(self) -> None:
        existing_handler = logging.StreamHandler(io.StringIO())
        self.root_logger.handlers = [existing_handler]
        self.root_logger.setLevel(logging.ERROR)
        runtime_logging._CONFIGURED = False

        configure_logging()

        self.assertEqual(self.root_logger.handlers, [existing_handler])
        self.assertEqual(self.root_logger.level, logging.ERROR)
        self.assertTrue(runtime_logging._CONFIGURED)

    def test_force_configure_logging_replaces_existing_root_handlers(self) -> None:
        existing_handler = logging.StreamHandler(io.StringIO())
        self.root_logger.handlers = [existing_handler]
        self.root_logger.setLevel(logging.ERROR)
        runtime_logging._CONFIGURED = False

        # Pin CRUCIBLE_LOG_LEVEL to INFO so the assertion is deterministic
        # regardless of what the developer's .env file contains.
        with patch.dict(os.environ, {"CRUCIBLE_LOG_LEVEL": "INFO"}, clear=False):
            with patch("sys.stderr", io.StringIO()):
                configure_logging(force=True)

        self.assertEqual(len(self.root_logger.handlers), 1)
        self.assertIsNot(self.root_logger.handlers[0], existing_handler)
        self.assertEqual(self.root_logger.level, logging.INFO)

    def test_plain_logging_includes_context_fields(self) -> None:
        stream = io.StringIO()
        # Explicitly suppress JSON mode so the formatter uses plain-text output
        # regardless of what CRUCIBLE_JSON_LOGS is set to in the outer env.
        with patch.dict(os.environ, {"CRUCIBLE_JSON_LOGS": "0"}, clear=False):
            with patch("sys.stderr", stream):
                configure_logging(force=True)
                logger = get_logger("crucible.test.plain")
                update_log_context(run_id="run-123", stage="analysis")
                log_event(
                    logger,
                    logging.INFO,
                    "plain_event",
                    "hello world",
                    agent="gate_controller",
                )

        output = stream.getvalue()
        self.assertIn("hello world", output)
        self.assertIn("run_id=run-123", output)
        self.assertIn("stage=analysis", output)
        self.assertIn("agent=gate_controller", output)

    def test_json_logging_renders_structured_payload(self) -> None:
        stream = io.StringIO()
        with patch.dict(os.environ, {"CRUCIBLE_JSON_LOGS": "1"}, clear=False):
            with patch("sys.stderr", stream):
                configure_logging(force=True)
                logger = get_logger("crucible.test.json")
                with log_context(run_id="run-json", stage="codegen"):
                    log_event(
                        logger,
                        logging.WARNING,
                        "json_event",
                        "structured message",
                        attempt=2,
                    )

        payload = json.loads(stream.getvalue().strip())
        self.assertEqual(payload["message"], "structured message")
        self.assertEqual(payload["run_id"], "run-json")
        self.assertEqual(payload["stage"], "codegen")
        self.assertEqual(payload["attempt"], 2)
        self.assertEqual(payload["event"], "json_event")


if __name__ == "__main__":
    unittest.main()
