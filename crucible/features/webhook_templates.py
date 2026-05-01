from __future__ import annotations
"""features/webhook_templates.py
=================================
Pre-built webhook payload templates for n8n, Zapier, PagerDuty, OpsGenie,
and Microsoft Teams.

Usage::

    from crucible.feature_registry import run_features, FeatureConfig
    import crucible.features.webhook_templates  # auto-registers

    config = FeatureConfig()
    results = run_features(
        "/path/to/run_dir",
        enabled_features=["webhook_templates"],
        config=config,
    )

Environment variables
---------------------
WEBHOOK_PLATFORMS     Comma-separated platforms to generate (default: n8n,zapier,teams).
N8N_WEBHOOK_URL       n8n endpoint; payloads are POSTed here if set.
ZAPIER_WEBHOOK_URL    Zapier endpoint.
PAGERDUTY_WEBHOOK_URL PagerDuty Events v2 endpoint.
OPSGENIE_WEBHOOK_URL  OpsGenie Create Alert endpoint.
TEAMS_WEBHOOK_URL     Microsoft Teams incoming webhook.
PAGERDUTY_ROUTING_KEY Routing key embedded in PagerDuty payloads.
"""

import json
import os
import sys
import time
import urllib.request
import urllib.error
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from crucible.feature_registry import (
    BaseFeature,
    FeatureConfig,
    FeatureResult,
    register,
)


def _extract_run_data(run_dir: Path) -> Dict[str, Any]:
    """Read analysis_result.json and run_meta.json to build run data dict."""
    result: Dict[str, Any] = {
        "project_name": run_dir.name,
        "score": 0,
        "risk_level": "unknown",
        "gate_decision": "unknown",
        "run_id": run_dir.name,
        "timestamp": "",
        "experiments_count": 0,
        "cost": 0.0,
        "consensus_summary": "",
    }

    ar_path = run_dir / "analysis_result.json"
    try:
        ar = json.loads(ar_path.read_text(encoding="utf-8"))
        result["project_name"] = ar.get("project_name", run_dir.name)
        result["score"] = ar.get("score", 0)
        result["risk_level"] = str(ar.get("risk_level", "unknown")).lower()
        result["gate_decision"] = str(
            ar.get("gate_decision", ar.get("mode_used", "unknown"))
        ).lower()
        result["experiments_count"] = len(ar.get("experiments") or [])
        result["cost"] = float(ar.get("cost", 0.0) or 0.0)
        result["consensus_summary"] = str(ar.get("consensus", ""))[:500]
    except FileNotFoundError:
        # File simply not generated yet — expected for early-stage runs.
        pass
    except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        # Corrupt or unreadable analysis result is meaningful: downstream
        # alerting decisions (e.g. PagerDuty severity) will fall back to
        # "unknown" and a critical run could silently lose its severity tag.
        # Surface a single warning so the failure is observable.
        try:
            print(
                f"[WARN] webhook_templates: failed to parse analysis_result.json "
                f"at {ar_path}: {type(exc).__name__}: {exc}",
                file=sys.stderr,
            )
        except Exception:
            pass

    rm_path = run_dir / "run_meta.json"
    try:
        rm = json.loads(rm_path.read_text(encoding="utf-8"))
        result["run_id"] = str(rm.get("timestamp", run_dir.name))
        result["timestamp"] = str(rm.get("timestamp", ""))
    except FileNotFoundError:
        pass
    except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        try:
            print(
                f"[WARN] webhook_templates: failed to parse run_meta.json "
                f"at {rm_path}: {type(exc).__name__}: {exc}",
                file=sys.stderr,
            )
        except Exception:
            pass

    if not result["timestamp"]:
        result["timestamp"] = datetime.now(timezone.utc).isoformat()

    return result


def _build_n8n_payload(d: Dict[str, Any]) -> Dict[str, Any]:
    """Build n8n webhook payload."""
    return {
        "event": "pipeline_complete",
        "project": d["project_name"],
        "score": d["score"],
        "risk": d["risk_level"],
        "gate": d["gate_decision"],
        "timestamp": d["timestamp"],
        "run_id": d["run_id"],
        "experiments": d["experiments_count"],
        "cost": d["cost"],
        "summary": d["consensus_summary"],
    }


def _build_zapier_payload(d: Dict[str, Any]) -> Dict[str, str]:
    """Build Zapier flat key-value webhook payload."""
    return {
        "project_name": str(d["project_name"]),
        "score": str(d["score"]),
        "risk_level": str(d["risk_level"]),
        "gate_decision": str(d["gate_decision"]),
        "run_id": str(d["run_id"]),
        "timestamp": str(d["timestamp"]),
        "status": "success",
    }


def _build_pagerduty_payload(
    d: Dict[str, Any], routing_key: str
) -> Dict[str, Any]:
    """Build PagerDuty Events v2 payload."""
    risk = d["risk_level"]
    if risk in ("critical",):
        severity = "critical"
    elif risk in ("high",):
        severity = "warning"
    else:
        severity = "info"
    # "unknown" risk means parse failed — safer to trigger than to silently resolve.
    event_action = "trigger" if risk in ("high", "critical", "unknown") else "resolve"
    run_id_val = d["run_id"]
    proj_val = d["project_name"]
    gate_val = d["gate_decision"]
    return {
        "routing_key": routing_key,
        "event_action": event_action,
        "dedup_key": f"quantsaas-{run_id_val}",
        "payload": {
            "summary": f"Crucible: {proj_val} - {gate_val}",
            "severity": severity,
            "source": "quantsaas",
            "component": d["project_name"],
            "custom_details": {
                "score": d["score"],
                "risk": d["risk_level"],
                "gate": d["gate_decision"],
                "experiments": d["experiments_count"],
            },
        },
    }



def _build_opsgenie_payload(d: Dict[str, Any]) -> Dict[str, Any]:
    """Build OpsGenie Create Alert payload."""
    risk = d["risk_level"]
    if risk == "critical":
        priority = "P1"
    elif risk == "high":
        priority = "P2"
    else:
        priority = "P3"
    proj = d["project_name"]
    run_id = d["run_id"]
    gate = d["gate_decision"]
    score_s = str(d["score"])
    return {
        "message": f"Crucible Alert: {proj}",
        "alias": f"run-{run_id}",
        "description": d["consensus_summary"][:500],
        "priority": priority,
        "tags": ["quantsaas", risk],
        "details": {"score": score_s, "gate": gate},
    }


def _build_teams_payload(d: Dict[str, Any]) -> Dict[str, Any]:
    """Build Microsoft Teams MessageCard payload."""
    risk = d["risk_level"]
    if risk in ("high", "critical"):
        color = "FF0000"
    elif risk == "medium":
        color = "FFA500"
    else:
        color = "00AA00"
    proj = d["project_name"]
    score = d["score"]
    risk_s = d["risk_level"]
    gate = d["gate_decision"]
    run_id = d["run_id"]
    ts = d["timestamp"]
    exp = str(d["experiments_count"])
    cost = str(d["cost"])
    return {
        "@type": "MessageCard",
        "@context": "http://schema.org/extensions",
        "themeColor": color,
        "summary": "Crucible Run Complete",
        "sections": [
            {
                "activityTitle": f"Crucible: {proj}",
                "activitySubtitle": (
                    f"Score: {score} | Risk: {risk_s} | Gate: {gate}"
                ),
                "facts": [
                    {"name": "Run ID", "value": run_id},
                    {"name": "Timestamp", "value": ts},
                    {"name": "Experiments", "value": exp},
                    {"name": "Cost", "value": cost},
                ],
                "markdown": True,
            }
        ],
    }


def _post_payload(
    url: str,
    payload: Dict[str, Any],
    timeout: float = 10.0,
) -> Dict[str, Any]:
    """POST *payload* as JSON to *url*. Returns a result dict."""
    result: Dict[str, Any] = {
        "url_preview": url[:50] + "..." if len(url) > 50 else url,
        "status_code": None,
        "success": False,
        "error": None,
    }
    try:
        body = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=body,
            headers={"Content-Type": "application/json", "User-Agent": "Crucible-Webhook/1.0"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            result["status_code"] = resp.status
            result["success"] = 200 <= resp.status < 300
    except urllib.error.HTTPError as exc:
        result["status_code"] = exc.code
        result["error"] = str(exc)
    except Exception as exc:
        result["error"] = str(exc)
    return result


_PLATFORM_URL_ENV: Dict[str, str] = {
    "n8n": "N8N_WEBHOOK_URL",
    "zapier": "ZAPIER_WEBHOOK_URL",
    "pagerduty": "PAGERDUTY_WEBHOOK_URL",
    "opsgenie": "OPSGENIE_WEBHOOK_URL",
    "teams": "TEAMS_WEBHOOK_URL",
}


@register("webhook_templates")
class WebhookTemplatesFeature(BaseFeature):
    """Generate and optionally send webhook payloads for multiple platforms.

    Writes payload JSON files to run_dir/webhook_payloads/ and sends
    them to configured URLs.

    Usage example::

        from crucible.feature_registry import run_features, FeatureConfig
        import crucible.features.webhook_templates

        results = run_features(
            "/path/to/run_dir",
            enabled_features=["webhook_templates"],
            config=FeatureConfig(),
        )
    """

    name = "webhook_templates"
    label = "Webhook Templates"
    requires: list[str] = []

    def run(self, run_dir: str, config: FeatureConfig) -> FeatureResult:
        """Generate webhook payloads and optionally send them."""
        env: Dict[str, str] = config.env if config.env is not None else dict(os.environ)
        t0 = time.monotonic()
        warnings: List[str] = []
        artifacts: List[str] = []

        rdp = Path(run_dir).resolve()
        run_data = _extract_run_data(rdp)

        platforms_raw = env.get("WEBHOOK_PLATFORMS", "n8n,zapier,teams")
        platforms = [p.strip().lower() for p in platforms_raw.split(",") if p.strip()]

        routing_key = env.get("PAGERDUTY_ROUTING_KEY", "")

        # Build payloads
        payloads: Dict[str, Dict[str, Any]] = {}
        if "n8n" in platforms:
            payloads["n8n"] = _build_n8n_payload(run_data)
        if "zapier" in platforms:
            payloads["zapier"] = _build_zapier_payload(run_data)
        if "pagerduty" in platforms:
            payloads["pagerduty"] = _build_pagerduty_payload(run_data, routing_key)
        if "opsgenie" in platforms:
            payloads["opsgenie"] = _build_opsgenie_payload(run_data)
        if "teams" in platforms:
            payloads["teams"] = _build_teams_payload(run_data)

        # Write payload files
        out_dir = rdp / "webhook_payloads"
        try:
            out_dir.mkdir(parents=True, exist_ok=True)
        except Exception as exc:
            warnings.append(f"Could not create webhook_payloads/: {exc}")

        for platform, payload in payloads.items():
            out_path = out_dir / f"{platform}_payload.json"
            _tmp_payload = out_path.parent / (out_path.name + ".tmp")
            try:
                _tmp_payload.write_text(
                    json.dumps(payload, indent=2, default=str), encoding="utf-8"
                )
                _tmp_payload.replace(out_path)
                artifacts.append(str(out_path))
            except OSError as _exc:
                try:
                    _tmp_payload.unlink(missing_ok=True)
                except OSError:
                    pass
                warnings.append(f"Could not write {out_path.name}: {_exc}")
                continue

        # Send payloads
        send_results: List[Dict[str, Any]] = []
        platforms_sent: List[str] = []
        for platform, payload in payloads.items():
            url_env_key = _PLATFORM_URL_ENV.get(platform, "")
            url = env.get(url_env_key, "") if url_env_key else ""
            if not url:
                continue
            try:
                sr = _post_payload(url, payload)
                sr["platform"] = platform
                send_results.append(sr)
                if sr.get("success"):
                    platforms_sent.append(platform)
                else:
                    _sc = sr.get("status_code")
                    _err = sr.get("error")
                    warnings.append(
                        f"Send to {platform} failed: status={_sc} error={_err}"
                    )
            except Exception as exc:
                warnings.append(f"Error sending to {platform}: {exc}")

        report: Dict[str, Any] = {
            "platforms_generated": list(payloads.keys()),
            "platforms_sent": platforms_sent,
            "send_results": send_results,
            "errors": [w for w in warnings if "error" in w.lower() or "fail" in w.lower() or "could not" in w.lower()],
        }

        report_path = rdp / "webhook_templates_report.json"
        _tmp_rpt = report_path.parent / (report_path.name + ".tmp")
        try:
            _tmp_rpt.write_text(
                json.dumps(report, indent=2, default=str), encoding="utf-8"
            )
            _tmp_rpt.replace(report_path)
            artifacts.append(str(report_path))
        except OSError as exc:
            try:
                _tmp_rpt.unlink(missing_ok=True)
            except OSError:
                pass
            warnings.append(f"Failed to write webhook report: {exc}")

        return FeatureResult(
            feature=self.name,
            success=True,
            summary=(
                f"Generated {len(payloads)} payloads; "
                f"sent to {len(platforms_sent)} platforms."
            ),
            details={
                "webhook_templates": report,
                "artifacts": artifacts,
                "warnings": warnings,
            },
            duration_seconds=time.monotonic() - t0,
        )
