"""
features/notification_hooks.py
===============================
Post-run notification hooks for completed pipeline runs.

Sends a notification (HTTP webhook, Slack, or Discord) when a pipeline run
completes or fails.  Useful for batch mode, watch mode, and CI environments
where the user is not actively watching the terminal.

Configuration is via environment variables:

- ``NOTIFY_WEBHOOK_URL``: Generic HTTP POST endpoint.
- ``NOTIFY_SLACK_WEBHOOK_URL``: Slack Incoming Webhook URL.
- ``NOTIFY_DISCORD_WEBHOOK_URL``: Discord Webhook URL.
- ``NOTIFY_ON_FAIL_ONLY``: Set to ``1`` to only notify on failure.

Usage::

    from crucible.features.notification_hooks import notify_run_complete
    notify_run_complete("/path/to/run_dir")
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Dict, List

# ── Data models ──────────────────────────────────────────────────────────────

@dataclass
class NotificationResult:
    channel: str          # "webhook" | "slack" | "discord"
    sent: bool
    status_code: int = 0
    error: str = ""


@dataclass
class NotificationReport:
    notifications_sent: int = 0
    notifications_failed: int = 0
    results: List[NotificationResult] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "notifications_sent": self.notifications_sent,
            "notifications_failed": self.notifications_failed,
            "results": [
                {
                    "channel": r.channel,
                    "sent": r.sent,
                    "status_code": r.status_code,
                    "error": r.error,
                }
                for r in self.results
            ],
            "errors": self.errors,
        }


# ── Helpers ──────────────────────────────────────────────────────────────────

def _load_run_summary(run_dir: str) -> Dict[str, Any]:
    """Extract key fields from analysis_result.json for the notification."""
    analysis_path = os.path.join(run_dir, "analysis_result.json")
    if not os.path.isfile(analysis_path):
        return {}
    try:
        with open(analysis_path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError):
        return {}


def _build_message(run_dir: str, success: bool) -> Dict[str, Any]:
    """Build a structured message payload from run results."""
    analysis = _load_run_summary(run_dir)
    project_name = str(analysis.get("project_name") or os.path.basename(run_dir))
    score = analysis.get("score")
    risk = str(analysis.get("risk_level") or "unknown")
    status = "SUCCESS" if success else "FAILURE"

    # Check for security report
    sec_passed = True
    sec_path = os.path.join(run_dir, "security_report.json")
    if os.path.isfile(sec_path):
        try:
            with open(sec_path, "r", encoding="utf-8") as fh:
                sec_data = json.load(fh)
            raw_passed = sec_data.get("passed", True)
            # bool("false") is True in Python because non-empty strings are truthy.
            # Handle str explicitly so a JSON "false" string is read correctly.
            if isinstance(raw_passed, str):
                sec_passed = raw_passed.strip().lower() not in ("false", "0", "no", "fail", "failed")
            else:
                sec_passed = bool(raw_passed)
        except (json.JSONDecodeError, OSError):
            pass

    score_str = f"{score}/100" if score is not None else "N/A"
    text = (
        f"[Crucible] {status}: {project_name}\n"
        f"Score: {score_str} | Risk: {risk} | "
        f"Security: {'PASS' if sec_passed else 'FAIL'}\n"
        f"Output: {os.path.basename(run_dir)}"
    )

    return {
        "text": text,
        "project_name": project_name,
        "status": status,
        "score": score,
        "risk_level": risk,
        "security_passed": sec_passed,
        "run_dir": os.path.basename(run_dir),
    }


def _post_json(
    url: str,
    payload: Dict[str, Any],
    timeout: int = 15,
) -> tuple:
    """POST JSON to *url*.  Returns ``(status_code, error_str)``."""
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, ""
    except urllib.error.HTTPError as exc:
        return exc.code, str(exc)
    except (urllib.error.URLError, OSError) as exc:
        return 0, str(exc)


# ── Channel senders ──────────────────────────────────────────────────────────

def _send_generic_webhook(
    url: str,
    message: Dict[str, Any],
) -> NotificationResult:
    """Send to a generic HTTP webhook."""
    status, error = _post_json(url, message)
    sent = 200 <= status < 300
    return NotificationResult(
        channel="webhook",
        sent=sent,
        status_code=status,
        error=error,
    )


def _send_slack(
    url: str,
    message: Dict[str, Any],
) -> NotificationResult:
    """Send to Slack Incoming Webhook."""
    # Slack expects { "text": "..." } at minimum
    payload = {"text": message.get("text", "")}
    status, error = _post_json(url, payload)
    sent = 200 <= status < 300
    return NotificationResult(
        channel="slack",
        sent=sent,
        status_code=status,
        error=error,
    )


def _send_discord(
    url: str,
    message: Dict[str, Any],
) -> NotificationResult:
    """Send to Discord Webhook."""
    # Discord expects { "content": "..." }
    payload = {"content": message.get("text", "")}
    status, error = _post_json(url, payload)
    sent = 200 <= status < 300 or status == 204
    return NotificationResult(
        channel="discord",
        sent=sent,
        status_code=status,
        error=error,
    )


# ── Main entry point ─────────────────────────────────────────────────────────

def notify_run_complete(
    run_dir: str,
    *,
    success: bool = True,
) -> NotificationReport:
    """
    Send notifications for a completed pipeline run.

    Reads webhook URLs from environment variables.  If no URLs are configured,
    returns an empty report (no-op).

    Args:
        run_dir:  Path to the completed run output directory.
        success:  Whether the run succeeded (affects message content).

    Returns:
        NotificationReport summarising what was sent.
    """
    if os.environ.get("NOTIFICATION_HOOKS_ENABLED", "1").strip().lower() in ("0", "false", "no", "off"):
        return NotificationReport()

    report = NotificationReport()

    # Check fail-only filter (whitelist mode — match the convention used
    # everywhere else in the project, including "on" as a truthy value).
    fail_only = os.environ.get("NOTIFY_ON_FAIL_ONLY", "").strip().lower()
    if fail_only in ("1", "true", "yes", "on") and success:
        return report

    message = _build_message(run_dir, success)

    # Collect configured channels
    channels: List[tuple] = []
    generic_url = os.environ.get("NOTIFY_WEBHOOK_URL", "").strip()
    if generic_url:
        channels.append(("webhook", generic_url, _send_generic_webhook))

    slack_url = os.environ.get("NOTIFY_SLACK_WEBHOOK_URL", "").strip()
    if slack_url:
        channels.append(("slack", slack_url, _send_slack))

    discord_url = os.environ.get("NOTIFY_DISCORD_WEBHOOK_URL", "").strip()
    if discord_url:
        channels.append(("discord", discord_url, _send_discord))

    if not channels:
        return report

    for _channel_name, url, sender_fn in channels:
        try:
            result = sender_fn(url, message)
            report.results.append(result)
            if result.sent:
                report.notifications_sent += 1
            else:
                report.notifications_failed += 1
        except Exception as exc:
            report.notifications_failed += 1
            report.results.append(NotificationResult(
                channel=_channel_name,
                sent=False,
                error=str(exc)[:200],
            ))

    return report
