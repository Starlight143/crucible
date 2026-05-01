"""Tests for credential masking in crucible/runtime_logging.py"""
from __future__ import annotations

import json
import logging
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from crucible.runtime_logging import (
    _REDACTED,
    _redact_fields,
    StructuredFormatter,
    get_logger,
    set_log_context,
    clear_log_context,
)


# ── _redact_fields ────────────────────────────────────────────────────────────

class TestRedactFields:
    def test_redacts_api_key(self) -> None:
        result = _redact_fields({"api_key": "sk-secret123"})
        assert result["api_key"] == _REDACTED

    def test_redacts_apikey_no_underscore(self) -> None:
        result = _redact_fields({"apikey": "mykey"})
        assert result["apikey"] == _REDACTED

    def test_redacts_token(self) -> None:
        result = _redact_fields({"token": "bearer_xyz"})
        assert result["token"] == _REDACTED

    def test_redacts_password(self) -> None:
        result = _redact_fields({"password": "hunter2"})
        assert result["password"] == _REDACTED

    def test_redacts_secret(self) -> None:
        result = _redact_fields({"secret": "topsecret"})
        assert result["secret"] == _REDACTED

    def test_redacts_authorization(self) -> None:
        result = _redact_fields({"authorization": "Bearer token123"})
        assert result["authorization"] == _REDACTED

    def test_redacts_bearer(self) -> None:
        result = _redact_fields({"bearer": "mytoken"})
        assert result["bearer"] == _REDACTED

    def test_redacts_credential(self) -> None:
        result = _redact_fields({"credential": "cred_value"})
        assert result["credential"] == _REDACTED

    def test_does_not_redact_unrelated_keys(self) -> None:
        result = _redact_fields({"stage": "analysis", "elapsed": 42.0, "model": "gpt-4"})
        assert result["stage"] == "analysis"
        assert result["elapsed"] == 42.0
        assert result["model"] == "gpt-4"

    def test_case_insensitive_matching(self) -> None:
        result = _redact_fields({"OPENAI_API_KEY": "sk-123"})
        assert result["OPENAI_API_KEY"] == _REDACTED

    def test_case_insensitive_token(self) -> None:
        result = _redact_fields({"ACCESS_TOKEN": "mytoken"})
        assert result["ACCESS_TOKEN"] == _REDACTED

    def test_partial_match_in_key(self) -> None:
        # "api_key" substring in "OPENAI_API_KEY"
        result = _redact_fields({"OPENAI_API_KEY": "sk-xxx"})
        assert result["OPENAI_API_KEY"] == _REDACTED

    def test_empty_dict(self) -> None:
        result = _redact_fields({})
        assert result == {}

    def test_returns_copy_not_in_place(self) -> None:
        original = {"api_key": "secret", "stage": "run"}
        result = _redact_fields(original)
        assert original["api_key"] == "secret"  # original unchanged
        assert result["api_key"] == _REDACTED

    def test_does_not_recurse_into_nested_dicts(self) -> None:
        """Top-level only: nested dicts are not recursed."""
        inner = {"api_key": "should_not_be_redacted_by_outer"}
        result = _redact_fields({"nested": inner, "name": "test"})
        # The "nested" key itself does not match any sensitive fragment
        assert result["nested"] is inner
        assert result["name"] == "test"

    def test_passwd_variant(self) -> None:
        result = _redact_fields({"passwd": "mypass"})
        assert result["passwd"] == _REDACTED

    def test_access_key(self) -> None:
        result = _redact_fields({"access_key": "AKIA1234"})
        assert result["access_key"] == _REDACTED

    def test_secret_key(self) -> None:
        result = _redact_fields({"secret_key": "wJal..."})
        assert result["secret_key"] == _REDACTED

    def test_client_secret(self) -> None:
        result = _redact_fields({"client_secret": "abc123"})
        assert result["client_secret"] == _REDACTED

    def test_private_key(self) -> None:
        result = _redact_fields({"private_key": "-----BEGIN PRIVATE KEY-----"})
        assert result["private_key"] == _REDACTED

    def test_auth_substring(self) -> None:
        result = _redact_fields({"auth": "basic xyz"})
        assert result["auth"] == _REDACTED


# ── StructuredFormatter applies redaction ────────────────────────────────────

class TestStructuredFormatterRedaction:
    def _make_record(self, message: str, **structured_fields: object) -> logging.LogRecord:
        record = logging.LogRecord(
            name="test",
            level=logging.INFO,
            pathname="test.py",
            lineno=1,
            msg=message,
            args=(),
            exc_info=None,
        )
        record.structured_fields = structured_fields  # type: ignore[attr-defined]
        return record

    def test_redacts_api_key_in_structured_fields_text_mode(self) -> None:
        formatter = StructuredFormatter(json_mode=False)
        record = self._make_record("test msg", api_key="sk-secret", stage="run")
        output = formatter.format(record)
        assert _REDACTED in output
        assert "sk-secret" not in output

    def test_redacts_api_key_in_structured_fields_json_mode(self) -> None:
        formatter = StructuredFormatter(json_mode=True)
        record = self._make_record("test msg", api_key="sk-secret", stage="run")
        output = formatter.format(record)
        parsed = json.loads(output)
        assert parsed["api_key"] == _REDACTED
        assert "sk-secret" not in output

    def test_redacts_token_in_context_json_mode(self) -> None:
        # Use the context var by setting it then formatting
        clear_log_context()
        set_log_context(token="mytoken123", stage="pipeline")
        try:
            formatter = StructuredFormatter(json_mode=True)
            record = self._make_record("ctx test")
            output = formatter.format(record)
            parsed = json.loads(output)
            assert parsed.get("token") == _REDACTED
            assert "mytoken123" not in output
        finally:
            clear_log_context()

    def test_does_not_redact_unrelated_in_json_mode(self) -> None:
        formatter = StructuredFormatter(json_mode=True)
        record = self._make_record("test", stage="analysis", model="gpt-4")
        output = formatter.format(record)
        parsed = json.loads(output)
        assert parsed.get("stage") == "analysis"
        assert parsed.get("model") == "gpt-4"
