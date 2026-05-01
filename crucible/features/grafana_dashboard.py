from __future__ import annotations
"""Grafana dashboard generator for Crucible Prometheus metrics.

This feature writes a complete Grafana 9+ dashboard JSON file and a setup guide
for importing the dashboard against a Prometheus data source.
"""

import json
import os
import time
from typing import Any, Dict, List

from crucible.feature_registry import (
    BaseFeature,
    FeatureConfig,
    FeatureResult,
    register,
)


def _write_text(path: str, content: str) -> None:
    _tmp = path + ".tmp"
    try:
        with open(_tmp, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(content)
        os.replace(_tmp, path)
    except OSError as exc:
        try:
            os.unlink(_tmp)
        except OSError:
            pass
        raise RuntimeError(f"cannot write {path}: {exc}") from exc


def _target(expr: str, datasource_uid: str, ref_id: str, legend: str = "") -> Dict[str, Any]:
    return {
        "datasource": {"type": "prometheus", "uid": datasource_uid},
        "editorMode": "code",
        "expr": expr,
        "format": "time_series",
        "instant": False,
        "legendFormat": legend,
        "range": True,
        "refId": ref_id,
    }


def _base_panel(panel_id: int, title: str, panel_type: str, x: int, y: int, datasource_uid: str) -> Dict[str, Any]:
    return {
        "id": panel_id,
        "title": title,
        "type": panel_type,
        "datasource": {"type": "prometheus", "uid": datasource_uid},
        "gridPos": {"h": 8, "w": 12, "x": x, "y": y},
        "fieldConfig": {
            "defaults": {
                "color": {"mode": "palette-classic"},
                "custom": {
                    "axisBorderShow": False,
                    "axisCenteredZero": False,
                    "axisColorMode": "text",
                    "axisLabel": "",
                    "axisPlacement": "auto",
                    "barAlignment": 0,
                    "drawStyle": "line",
                    "fillOpacity": 20,
                    "gradientMode": "none",
                    "hideFrom": {"legend": False, "tooltip": False, "viz": False},
                    "lineInterpolation": "linear",
                    "lineWidth": 2,
                    "pointSize": 5,
                    "scaleDistribution": {"type": "linear"},
                    "showPoints": "auto",
                    "spanNulls": False,
                    "stacking": {"group": "A", "mode": "none"},
                    "thresholdsStyle": {"mode": "off"},
                },
                "mappings": [],
                "thresholds": {"mode": "absolute", "steps": [{"color": "green", "value": None}, {"color": "red", "value": 80}]},
            },
            "overrides": [],
        },
        "options": {"legend": {"calcs": ["lastNotNull"], "displayMode": "list", "placement": "bottom", "showLegend": True}, "tooltip": {"mode": "single", "sort": "none"}},
        "targets": [],
    }


def _build_dashboard(datasource_uid: str) -> Dict[str, Any]:
    panels: List[Dict[str, Any]] = []
    panel = _base_panel(1, "Run Score Over Time", "timeseries", 0, 0, datasource_uid)
    panel["targets"] = [_target('quantsaas_run_score{project=~"$project",run_id=~"$run_id"}', datasource_uid, "A", "{{project}} {{run_id}}")]
    panels.append(panel)

    panel = _base_panel(2, "Pipeline Duration", "bargauge", 12, 0, datasource_uid)
    panel["fieldConfig"]["defaults"]["unit"] = "s"
    panel["options"] = {"displayMode": "gradient", "maxVizHeight": 300, "minVizHeight": 16, "minVizWidth": 8, "namePlacement": "auto", "orientation": "horizontal", "reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False}, "showUnfilled": True, "sizing": "auto", "valueMode": "color"}
    panel["targets"] = [_target('quantsaas_run_duration_seconds{project=~"$project",run_id=~"$run_id"}', datasource_uid, "A", "{{run_id}}")]
    panels.append(panel)

    panel = _base_panel(3, "Token Usage by Stage", "bargauge", 0, 8, datasource_uid)
    panel["fieldConfig"]["defaults"]["unit"] = "short"
    panel["fieldConfig"]["defaults"]["custom"]["stacking"] = {"group": "A", "mode": "normal"}
    panel["options"] = {"displayMode": "lcd", "maxVizHeight": 300, "minVizHeight": 16, "minVizWidth": 8, "namePlacement": "auto", "orientation": "horizontal", "reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False}, "showUnfilled": True, "sizing": "auto", "valueMode": "color"}
    panel["targets"] = [_target('sum by (stage) (quantsaas_stage_tokens_total{project=~"$project",run_id=~"$run_id"})', datasource_uid, "A", "{{stage}}")]
    panels.append(panel)

    panel = _base_panel(4, "LLM Cost Trend", "timeseries", 12, 8, datasource_uid)
    panel["fieldConfig"]["defaults"]["unit"] = "currencyUSD"
    panel["targets"] = [_target('quantsaas_run_cost_usd{project=~"$project",run_id=~"$run_id"}', datasource_uid, "A", "{{project}} {{run_id}}")]
    panels.append(panel)

    panel = _base_panel(5, "Security Issues Table", "table", 0, 16, datasource_uid)
    panel["fieldConfig"]["defaults"]["custom"] = {"align": "auto", "cellOptions": {"type": "auto"}, "inspect": False}
    panel["options"] = {"cellHeight": "sm", "footer": {"countRows": False, "fields": "", "reducer": ["sum"], "show": False}, "showHeader": True}
    panel["targets"] = [{"datasource": {"type": "prometheus", "uid": datasource_uid}, "editorMode": "code", "expr": 'quantsaas_security_issues_total{project=~"$project",run_id=~"$run_id"}', "format": "table", "instant": True, "legendFormat": "{{severity}}", "range": False, "refId": "A"}]
    panels.append(panel)

    panel = _base_panel(6, "Backtest Performance", "stat", 12, 16, datasource_uid)
    panel["fieldConfig"]["defaults"]["unit"] = "short"
    panel["options"] = {"colorMode": "value", "graphMode": "area", "justifyMode": "auto", "orientation": "auto", "reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False}, "showPercentChange": False, "textMode": "auto", "wideLayout": True}
    panel["targets"] = [_target('quantsaas_backtest_sharpe{project=~"$project",run_id=~"$run_id"}', datasource_uid, "A", "Sharpe")]
    panels.append(panel)

    return {
        "annotations": {"list": []},
        "editable": True,
        "fiscalYearStartMonth": 0,
        "graphTooltip": 0,
        "links": [],
        "liveNow": False,
        "panels": panels,
        "refresh": "30s",
        "schemaVersion": 38,
        "style": "dark",
        "tags": ["quantsaas", "pipeline", "prometheus"],
        "templating": {
            "list": [
                {"current": {"selected": False, "text": "All", "value": "$__all"}, "datasource": {"type": "prometheus", "uid": datasource_uid}, "definition": "label_values(quantsaas_run_score, project)", "hide": 0, "includeAll": True, "multi": True, "name": "project", "options": [], "query": {"query": "label_values(quantsaas_run_score, project)", "refId": "PrometheusVariableQueryEditor-VariableQuery"}, "refresh": 1, "regex": "", "skipUrlSync": False, "sort": 1, "type": "query"},
                {"current": {"selected": False, "text": ".*", "value": ".*"}, "datasource": {"type": "prometheus", "uid": datasource_uid}, "definition": "label_values(quantsaas_run_score{project=~\"$project\"}, run_id)", "hide": 0, "includeAll": False, "multi": False, "name": "run_id", "options": [], "query": {"query": "label_values(quantsaas_run_score{project=~\"$project\"}, run_id)", "refId": "PrometheusVariableQueryEditor-VariableQuery"}, "refresh": 1, "regex": "", "skipUrlSync": False, "sort": 1, "type": "query"},
            ]
        },
        "time": {"from": "now-24h", "to": "now"},
        "timepicker": {},
        "timezone": "",
        "title": "Crucible Pipeline Monitor",
        "uid": "quantsaas-pipeline-monitor",
        "version": 1,
        "weekStart": "",
    }


def _setup_markdown(datasource_uid: str) -> str:
    return "\n".join([
        "# Crucible Grafana Dashboard Setup",
        "",
        "1. Ensure Prometheus is scraping `metrics.prom` or receiving metrics from Pushgateway.",
        "2. In Grafana, import `grafana_dashboard.json`.",
        f"3. Select the Prometheus data source with UID `{datasource_uid}`.",
        "4. Confirm the `project` and `run_id` variables resolve after the first metrics scrape.",
        "",
        "Required metrics are generated by the `prometheus_exporter` feature.",
        "",
    ])


@register("grafana_dashboard")
class GrafanaDashboardFeature(BaseFeature):
    name = "grafana_dashboard"
    label = "Grafana Dashboard Generator"
    requires: list[str] = []

    def run(self, run_dir: str, config: FeatureConfig) -> FeatureResult:
        start = time.monotonic()
        if os.environ.get("GRAFANA_ENABLED", "1").strip().lower() in ("0", "false", "no", "off"):
            return FeatureResult(feature=self.name, success=True, summary="disabled", skipped=True, skip_reason="disabled")
        try:
            datasource_uid = os.environ.get("GRAFANA_DATASOURCE_UID", "prometheus").strip() or "prometheus"
            dashboard_path = os.path.join(run_dir, "grafana_dashboard.json")
            setup_path = os.path.join(run_dir, "grafana_setup.md")
            _write_text(dashboard_path, json.dumps(_build_dashboard(datasource_uid), indent=2, ensure_ascii=False) + "\n")
            _write_text(setup_path, _setup_markdown(datasource_uid))
            return FeatureResult(feature=self.name, success=True, summary="Grafana dashboard generated", details={"dashboard_path": dashboard_path, "setup_path": setup_path, "datasource_uid": datasource_uid}, duration_seconds=time.monotonic() - start)
        except Exception as exc:
            return FeatureResult(feature=self.name, success=False, summary="Grafana dashboard generation failed", error=str(exc), duration_seconds=time.monotonic() - start)
