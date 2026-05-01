from __future__ import annotations
"""Prometheus metrics exporter for Crucible pipeline runs.

This feature converts optional run artifacts into Prometheus text exposition
metrics, writes them to ``metrics.prom`` in the run directory, and can push the
same payload to a Pushgateway using a PUT request. Missing source files are
treated as absent data rather than failures.
"""

import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, Iterable, List, Mapping, Optional

from crucible.feature_registry import (
    BaseFeature,
    FeatureConfig,
    FeatureResult,
    register,
)


def _load_json(path: str) -> Dict[str, Any]:
    try:
        if not os.path.isfile(path):
            return {}
        with open(path, "r", encoding="utf-8") as fh:
            data = json.loads(fh.read())
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError, TypeError):
        return {}


def _write_text(path: str, content: str) -> None:
    """Write *content* to *path* atomically via a sibling .tmp file."""
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(content)
        os.replace(tmp, path)
    except Exception as exc:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise RuntimeError(f"cannot write {path}: {exc}") from exc


def _to_float(value: Any, default: float = 0.0) -> float:
    if isinstance(value, bool):
        return float(default)
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(default)


def _dig(data: Mapping[str, Any], paths: Iterable[Iterable[str]], default: Any = None) -> Any:
    for path in paths:
        current: Any = data
        ok = True
        for key in path:
            if isinstance(current, Mapping) and key in current:
                current = current[key]
            else:
                ok = False
                break
        if ok:
            return current
    return default


def _label_value(value: Any) -> str:
    text = str(value)
    # Escape all three characters required by the Prometheus text format spec,
    # plus \r (carriage return) which many parsers reject as a line terminator.
    return (
        text
        .replace("\\", "\\\\")
        .replace("\r", "\\r")
        .replace("\n", "\\n")
        .replace('"', '\\"')
    )


def _labels(values: Mapping[str, Any]) -> str:
    return "{" + ",".join(f'{key}="{_label_value(value)}"' for key, value in values.items()) + "}"


def _metric_line(name: str, value: float, labels: Optional[Mapping[str, Any]] = None) -> str:
    import math as _math
    if _math.isnan(value):
        prom_value = "NaN"
    elif _math.isinf(value):
        prom_value = "+Inf" if value > 0 else "-Inf"
    else:
        prom_value = f"{value:g}"
    if labels:
        return f"{name}{_labels(labels)} {prom_value}"
    return f"{name} {prom_value}"


def _extract_tokens(analysis: Mapping[str, Any]) -> Dict[str, float]:
    raw = _dig(analysis, (("stage_tokens",), ("tokens_by_stage",), ("usage", "stage_tokens"), ("llm_usage", "stage_tokens")), {})
    tokens: Dict[str, float] = {}
    if isinstance(raw, Mapping):
        for stage, value in raw.items():
            if isinstance(value, Mapping):
                amount = _dig(value, (("tokens",), ("total_tokens",), ("total",)), 0)
            else:
                amount = value
            tokens[str(stage)] = max(0.0, _to_float(amount))
    return tokens


def _severity_counts(security: Mapping[str, Any]) -> Dict[str, float]:
    counts = {"critical": 0.0, "high": 0.0, "medium": 0.0, "low": 0.0, "info": 0.0}
    # Use None (not {}) as the default so that an absent key is distinguishable
    # from an empty dict — an empty {} is itself a Mapping, which would cause
    # the issues-list fallback below to be unreachable dead code.
    raw_counts = _dig(security, (("severity_counts",), ("issues_by_severity",)), None)
    if isinstance(raw_counts, Mapping):
        for severity, count in raw_counts.items():
            key = str(severity).lower()
            counts[key] = counts.get(key, 0.0) + max(0.0, _to_float(count))
        # Precomputed summary dict is authoritative — do not also count
        # individual issues or counts would be doubled when both keys exist.
        return counts
    issues = security.get("issues")
    if isinstance(issues, list):
        for issue in issues:
            if isinstance(issue, Mapping):
                _raw_sev = issue.get("severity")
                key = str(_raw_sev if _raw_sev is not None else "info").lower()
                counts[key] = counts.get(key, 0.0) + 1.0
    return counts


def _build_metrics(run_dir: str) -> str:
    analysis = _load_json(os.path.join(run_dir, "analysis_result.json"))
    meta = _load_json(os.path.join(run_dir, "run_meta.json"))
    security = _load_json(os.path.join(run_dir, "security_report.json"))
    backtest = _load_json(os.path.join(run_dir, "backtest_report.json"))
    project = str(meta.get("project") or meta.get("project_name") or analysis.get("project") or os.path.basename(os.path.normpath(run_dir)) or "unknown")
    run_id = str(meta.get("run_id") or analysis.get("run_id") or os.path.basename(os.path.normpath(run_dir)) or "unknown")
    base_labels = {"project": project, "run_id": run_id}
    # Note: ("consensus", "score") was removed — "consensus" is a string field,
    # not a nested dict, so that path always falls through to the default anyway.
    score = _to_float(_dig(analysis, (("score",), ("final_score",)), 0.0))
    duration = _to_float(_dig(meta, (("duration_seconds",), ("elapsed_seconds",)), 0.0))
    if duration <= 0.0:
        duration = _to_float(_dig(analysis, (("duration_seconds",), ("elapsed_seconds",)), 0.0))
    cost = _to_float(_dig(analysis, (("cost_usd",), ("run_cost_usd",), ("llm_usage", "cost_usd")), 0.0))
    sharpe = _to_float(_dig(backtest, (("sharpe",), ("sharpe_ratio",), ("metrics", "sharpe"), ("metrics", "sharpe_ratio")), 0.0))
    lines: List[str] = [
        "# HELP quantsaas_run_score Final Crucible run score.",
        "# TYPE quantsaas_run_score gauge",
        _metric_line("quantsaas_run_score", score, base_labels),
        "# HELP quantsaas_run_duration_seconds Crucible pipeline duration in seconds.",
        "# TYPE quantsaas_run_duration_seconds gauge",
        _metric_line("quantsaas_run_duration_seconds", duration, base_labels),
        "# HELP quantsaas_stage_tokens_total LLM token usage by pipeline stage.",
        "# TYPE quantsaas_stage_tokens_total counter",
    ]
    # _extract_tokens always returns Dict[str, float] (never None).
    # The dead `is None` guard was removed: it could never trigger, its
    # "unknown" fallback contradicted the anti-phantom-label comment, and it
    # would introduce a phantom label if ever "fixed" to `not tokens`.
    tokens = _extract_tokens(analysis)
    for stage, amount in sorted(tokens.items()):
        lines.append(_metric_line("quantsaas_stage_tokens_total", amount, {"stage": stage, **base_labels}))
    lines.extend([
        "# HELP quantsaas_run_cost_usd Estimated LLM cost in USD.",
        "# TYPE quantsaas_run_cost_usd gauge",
        _metric_line("quantsaas_run_cost_usd", cost, base_labels),
        "# HELP quantsaas_security_issues_total Security issue count by severity.",
        "# TYPE quantsaas_security_issues_total gauge",
    ])
    for severity, count in sorted(_severity_counts(security).items()):
        lines.append(_metric_line("quantsaas_security_issues_total", count, {"severity": severity, **base_labels}))
    lines.extend([
        "# HELP quantsaas_backtest_sharpe Backtest Sharpe ratio.",
        "# TYPE quantsaas_backtest_sharpe gauge",
        _metric_line("quantsaas_backtest_sharpe", sharpe, base_labels),
        "",
    ])
    return "\n".join(lines)


# SSRF blocklist for Pushgateway URL validation — matches against the parsed
# hostname so that userinfo tricks (http://x@169.254.169.254/) are caught.
_GATEWAY_PRIVATE_HOST_RE = re.compile(
    r"^("
    r"localhost$|127\.|0\.0\.0\.0$|10\.|192\.168\.|"
    r"172\.(1[6-9]|2[0-9]|3[01])\.|169\.254\.|::1$|::ffff:127\.|fe80:"
    r")"
)


def _gateway_url_is_safe(url: str) -> bool:
    """Return True when *url* does not resolve to a private/loopback address.

    Also rejects non-http(s) schemes and empty/unparseable hostnames.
    """
    try:
        parsed = urllib.parse.urlparse(url)
        hostname = (parsed.hostname or "").lower()
    except Exception:
        return False
    if parsed.scheme not in ("http", "https"):
        return False
    return bool(hostname) and not bool(_GATEWAY_PRIVATE_HOST_RE.match(hostname))


class _PushGatewaySSRFRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Block 3xx redirects to private/loopback addresses.

    Without this, a malicious Pushgateway can respond with a redirect to
    http://169.254.169.254/ and bypass ``_gateway_url_is_safe`` which only
    checks the *initial* URL.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[override]
        if not _gateway_url_is_safe(newurl):
            raise urllib.error.URLError(
                f"SSRF blocked: redirect to private/non-http address {newurl!r}"
            )
        return super().redirect_request(req, fp, code, msg, headers, newurl)


# Built once at module level; reused across all pushgateway calls.
_PUSH_SAFE_OPENER = urllib.request.build_opener(_PushGatewaySSRFRedirectHandler)


def _push_to_gateway(payload: str, gateway_url: str, job_name: str) -> Dict[str, Any]:
    safe_job = re.sub(r"[^A-Za-z0-9_.-]+", "_", job_name.strip() or "quantsaas")
    # Strip leading dots to prevent path-traversal via ".." in the job segment
    # (e.g. job_name="..%2Fadmin" after substitution → ".._admin"; lstrip removes
    # the leading dots so the final URL path component is never "..").
    safe_job = safe_job.lstrip(".")
    if not safe_job:
        safe_job = "quantsaas"
    url = gateway_url.rstrip("/") + f"/metrics/job/{safe_job}"
    # SSRF guard: reject private/loopback addresses before opening the connection.
    if not _gateway_url_is_safe(url):
        return {"pushed": False, "url": url, "error": "SSRF blocked: private address"}
    request = urllib.request.Request(
        url,
        data=payload.encode("utf-8"),
        method="PUT",
        headers={"Content-Type": "text/plain; version=0.0.4; charset=utf-8"},
    )
    try:
        # Use _PUSH_SAFE_OPENER (not urlopen) so that 3xx redirects to private
        # addresses are also blocked — a malicious server could otherwise bypass
        # the initial URL check above by redirecting to 169.254.169.254.
        with _PUSH_SAFE_OPENER.open(request, timeout=10) as response:
            return {"pushed": True, "url": url, "status": int(response.status)}
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return {"pushed": False, "url": url, "error": str(exc)}


@register("prometheus_exporter")
class PrometheusExporterFeature(BaseFeature):
    name = "prometheus_exporter"
    label = "Prometheus Metrics Exporter"
    requires: list[str] = []

    def run(self, run_dir: str, config: FeatureConfig) -> FeatureResult:
        start = time.monotonic()
        # Prefer config.env so tests can inject a clean environment dict without
        # polluting os.environ; fall back to os.environ for standalone use.
        _env: Dict[str, str] = config.env if config.env is not None else dict(os.environ)
        _prom_enabled = (_env.get("PROMETHEUS_ENABLED", "1") or "1").strip().lower()
        if _prom_enabled in ("0", "false", "no", "off"):
            return FeatureResult(feature=self.name, success=True, summary="disabled", skipped=True, skip_reason="disabled")
        try:
            payload = _build_metrics(run_dir)
            output_path = os.path.join(run_dir, "metrics.prom")
            _write_text(output_path, payload)
            gateway_url = _env.get("PROMETHEUS_PUSHGATEWAY_URL", "").strip()
            push_result: Dict[str, Any] = {"pushed": False}
            if gateway_url:
                push_result = _push_to_gateway(payload, gateway_url, _env.get("PROMETHEUS_JOB_NAME", "quantsaas"))
            return FeatureResult(
                feature=self.name,
                success=True,
                summary="metrics.prom generated",
                details={"metrics_path": output_path, "pushgateway": push_result},
                duration_seconds=time.monotonic() - start,
            )
        except Exception as exc:
            return FeatureResult(feature=self.name, success=False, summary="Prometheus export failed", error=str(exc), duration_seconds=time.monotonic() - start)
