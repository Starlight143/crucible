"""
Tests for redaction: API keys, tokens, webhook URLs, nested dicts.

These checks defend against accidental credential leakage into the ledger.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from crucible.features.run_insights import get_recorder, reset_recorder
from crucible.features.run_insights.redact import (
    redact_event_payload,
    redact_signals,
)


# ── Direct redaction unit tests ──────────────────────────────────────────────

def test_redact_api_token_at_top_level(monkeypatch):
    monkeypatch.setenv("CRUCIBLE_RUN_INSIGHTS_REDACT", "1")
    out = redact_event_payload({"api_token": "supersecret", "other": "fine"})
    assert out["api_token"] == "***REDACTED***"
    assert out["other"] == "fine"


def test_redact_webhook_url(monkeypatch):
    monkeypatch.setenv("CRUCIBLE_RUN_INSIGHTS_REDACT", "1")
    out = redact_event_payload({
        "webhook_url": "https://hooks.slack.com/services/T0/B0/abc",
        "name": "alert",
    })
    assert out["webhook_url"] == "***REDACTED***"
    assert out["name"] == "alert"


def test_redact_recursive_nested(monkeypatch):
    monkeypatch.setenv("CRUCIBLE_RUN_INSIGHTS_REDACT", "1")
    out = redact_event_payload({
        "config": {
            "api_token": "secret123",
            "timeout_s": 30,
            "endpoint": {"webhook_url": "https://hooks.example.com"},
        }
    })
    assert out["config"]["api_token"] == "***REDACTED***"
    assert out["config"]["timeout_s"] == 30
    assert out["config"]["endpoint"]["webhook_url"] == "***REDACTED***"


def test_redact_preserves_innocent_keys(monkeypatch):
    monkeypatch.setenv("CRUCIBLE_RUN_INSIGHTS_REDACT", "1")
    out = redact_event_payload({
        "author": "alice",        # "auth" prefix but NOT redacted
        "authority": "system",
        "authored_by": "bob",
    })
    assert out["author"] == "alice"
    assert out["authority"] == "system"
    assert out["authored_by"] == "bob"


def test_redact_disabled_via_env(monkeypatch):
    monkeypatch.setenv("CRUCIBLE_RUN_INSIGHTS_REDACT", "0")
    out = redact_event_payload({"api_token": "supersecret"})
    assert out["api_token"] == "supersecret"  # passthrough


def test_redact_signals_drops_suspicious():
    raw = [
        "mode:quant",
        "asset:gold",
        "x:eyJhbGciOiJIUzI1NiJ9",  # JWT-ish → drop
        "y:" + "a" * 100,           # too long → drop
        "z:foo=bar",                # contains '=' → drop
        "v:ok",                     # OK
        "no_colon",                 # OK form check
    ]
    out = redact_signals(raw)
    assert "mode:quant" in out
    assert "asset:gold" in out
    assert "v:ok" in out
    assert not any(s.startswith("x:") for s in out)
    assert not any(s.startswith("y:") for s in out)
    assert not any(s.startswith("z:") for s in out)
    assert "no_colon" not in out


# ── End-to-end via recorder ──────────────────────────────────────────────────

def test_runtime_params_with_secret_token_redacted(tmp_path, monkeypatch):
    """The runtime_params payload commonly embeds operator config.  A
    webhook URL leaking into the ledger is a real failure mode.

    Per-stream toggles forced on so this test passes regardless of the
    operator's local ``.env`` state (which may have any of the
    ``CRUCIBLE_RUN_INSIGHTS_RECORD_*=0`` set for normal dev use).
    """
    monkeypatch.setenv("CRUCIBLE_RUN_INSIGHTS_ENABLED", "1")
    monkeypatch.setenv("CRUCIBLE_RUN_INSIGHTS_RECORD_OUTPUT", "1")
    monkeypatch.setenv("CRUCIBLE_RUN_INSIGHTS_RECORD_ERRORS", "1")
    monkeypatch.setenv("CRUCIBLE_RUN_INSIGHTS_RECORD_DEBATE", "1")
    monkeypatch.setenv("CRUCIBLE_RUN_INSIGHTS_DIR", str(tmp_path / "x"))
    monkeypatch.setenv("CRUCIBLE_RUN_INSIGHTS_RECORD_PARAMS", "auto")
    monkeypatch.setenv("CRUCIBLE_RUN_INSIGHTS_REDACT", "1")
    reset_recorder()
    r = get_recorder()
    r.record_runtime_params(
        run_id="run_x",
        project_name="p",
        mode="Quant",
        cli_flags={
            "input_mode": "idea",
            "api_token": "abc123secret",
            "openrouter_api_key": "sk-or-v1-secret",
        },
        gate_config={
            "webhook_url": "https://hooks.slack.com/services/T0/B0/secret",
        },
        run_meta={"llm_provider": "openrouter"},
    )
    import json
    text = (tmp_path / "x" / "params.jsonl").read_text(encoding="utf-8")
    event = json.loads(text.strip())
    assert "abc123secret" not in text
    assert "sk-or-v1-secret" not in text
    assert "hooks.slack.com/services/T0/B0/secret" not in text
    # Field names preserved (so the existence of a webhook is recorded;
    # only the value is gone).
    assert event["payload"]["cli_flags"]["api_token"] == "***REDACTED***"
    assert event["payload"]["gate_config"]["webhook_url"] == "***REDACTED***"
    reset_recorder()
