from __future__ import annotations
"""Notion and Obsidian export feature for Crucible analysis runs.

The feature always writes Obsidian-ready Markdown files and optionally creates a
Notion database page when Notion credentials are present in the environment.
"""

import json
import os
import re
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Any, Dict, List

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


def _write_json(path: str, payload: Dict[str, Any]) -> None:
    _write_text(path, json.dumps(payload, indent=2, ensure_ascii=False) + "\n")


def _mkdir(path: str) -> None:
    try:
        os.makedirs(path, exist_ok=True)
    except OSError as exc:
        raise RuntimeError(f"cannot create directory {path}: {exc}") from exc


def _scalar(data: Dict[str, Any], keys: List[str], default: Any = "") -> Any:
    for key in keys:
        value = data.get(key)
        # Only skip truly absent/empty values; preserve 0, 0.0, False
        if value is not None and value != "" and value != [] and value != {}:
            return value
    return default


def _safe_name(name: str) -> str:
    cleaned = "".join(ch if ch.isalnum() or ch in ("-", "_") else "_" for ch in name.strip())
    return cleaned or "quantsaas_run"


def _yaml_str(v: str) -> str:
    """Quote a string value for safe YAML frontmatter embedding.

    Wraps in double-quotes if the value contains YAML-unsafe characters,
    escaping any existing backslashes and double-quotes.
    Defined at module level (not nested) so it can be reused and tested
    independently.
    """
    if any(c in v for c in (':', '#', '[', ']', '{', '}', ',', '&', '*', '?', '|', '>', '!', "'", '"', '\n')):
        return '"' + v.replace('\\', '\\\\').replace('"', '\\"') + '"'
    return v


def _markdown_files(run_dir: str, analysis: Dict[str, Any]) -> List[str]:
    export_dir = os.path.join(run_dir, "obsidian_export")
    _mkdir(export_dir)
    project_name = _safe_name(os.path.basename(os.path.normpath(run_dir)))
    score = _scalar(analysis, ["score", "final_score", "consensus_score"], 0)
    risk_level = str(_scalar(analysis, ["risk_level", "risk"], "unknown"))
    gate = str(_scalar(analysis, ["gate", "gate_decision", "decision"], "review"))
    date = datetime.now(timezone.utc).date().isoformat()
    consensus = str(_scalar(analysis, ["consensus", "summary", "analysis_summary"], "No consensus text was recorded."))
    risk_text = str(_scalar(analysis, ["risk_summary", "risk_assessment", "risks"], "No risk details were recorded."))
    experiments = analysis.get("experiments") if isinstance(analysis.get("experiments"), list) else []
    main_path = os.path.join(export_dir, f"{project_name}.md")
    risks_path = os.path.join(export_dir, f"{project_name}_risks.md")
    experiments_path = os.path.join(export_dir, f"{project_name}_experiments.md")
    safe_risk = _yaml_str(risk_level)
    safe_gate = _yaml_str(gate)
    # score is numeric, date is ISO format — no quoting needed
    # risk_level tag: strip unsafe chars for YAML list item
    safe_risk_tag = re.sub(r'[^a-zA-Z0-9_\-]', '_', risk_level)
    frontmatter = "\n".join(["---", f"score: {score}", f"risk_level: {safe_risk}", f"gate: {safe_gate}", f"date: {date}", "tags:", "  - quantsaas", "  - analysis", f"  - {safe_risk_tag}", "---", ""])
    _write_text(main_path, frontmatter + f"# {project_name}\n\n## Consensus\n\n{consensus}\n\n## Links\n\n- [[{project_name}_risks]]\n- [[{project_name}_experiments]]\n")
    _write_text(risks_path, f"# {project_name} Risks\n\n{risk_text}\n")
    experiment_lines = [f"# {project_name} Experiments", ""]
    if experiments:
        for item in experiments:
            experiment_lines.append(f"- {item}")
    else:
        experiment_lines.append("- No experiment records were found in analysis_result.json.")
    experiment_lines.append("")
    _write_text(experiments_path, "\n".join(experiment_lines))
    return [main_path, risks_path, experiments_path]


def _post_notion_page(analysis: Dict[str, Any], run_dir: str) -> Dict[str, Any]:
    api_key = os.environ.get("NOTION_API_KEY", "").strip()
    if not api_key:
        return {"attempted": False, "notion_page_url": None}
    database_id = os.environ.get("NOTION_DATABASE_ID", "").strip()
    if not database_id:
        return {"attempted": False, "notion_page_url": None, "error": "NOTION_DATABASE_ID is required when NOTION_API_KEY is set"}
    project_name = os.path.basename(os.path.normpath(run_dir)) or "Crucible Run"
    try:
        score_number = float(_scalar(analysis, ["score", "final_score", "consensus_score"], 0))
    except (TypeError, ValueError):
        score_number = 0.0
    risk_level = str(_scalar(analysis, ["risk_level", "risk"], "unknown"))
    gate = str(_scalar(analysis, ["gate", "gate_decision", "decision"], "review"))
    consensus = str(_scalar(analysis, ["consensus", "summary", "analysis_summary"], "No consensus text was recorded."))
    payload = {
        "parent": {"database_id": database_id},
        "properties": {
            "Name": {"title": [{"text": {"content": project_name}}]},
            "Score": {"number": score_number},
            "Risk Level": {"select": {"name": risk_level}},
            "Gate Decision": {"select": {"name": gate}},
            "Created Date": {"date": {"start": datetime.now(timezone.utc).isoformat()}},
            "Tags": {"multi_select": [{"name": "quantsaas"}, {"name": "analysis"}, {"name": risk_level}]},
        },
        "children": [
            {"object": "block", "type": "heading_1", "heading_1": {"rich_text": [{"type": "text", "text": {"content": "Crucible Consensus"}}]}},
            {"object": "block", "type": "paragraph", "paragraph": {"rich_text": [{"type": "text", "text": {"content": consensus[:1900]}}]}},
        ],
    }
    request = urllib.request.Request(
        "https://api.notion.com/v1/pages",
        data=json.dumps(payload, indent=2, ensure_ascii=False).encode("utf-8"),
        method="POST",
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json", "Notion-Version": os.environ.get("NOTION_API_VERSION", "2022-06-28")},
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            body = response.read().decode("utf-8")
        data = json.loads(body)
        return {"attempted": True, "notion_page_url": data.get("url")}
    except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
        return {"attempted": True, "notion_page_url": None, "error": str(exc)}


@register("notion_export")
class NotionExportFeature(BaseFeature):
    name = "notion_export"
    label = "Notion + Obsidian Export"
    requires: list[str] = []

    def run(self, run_dir: str, config: FeatureConfig) -> FeatureResult:
        start = time.monotonic()
        try:
            analysis = _load_json(os.path.join(run_dir, "analysis_result.json"))
            obsidian_files = _markdown_files(run_dir, analysis)
            notion_result = _post_notion_page(analysis, run_dir)
            report = {"obsidian_files_written": obsidian_files, "notion_page_url": notion_result.get("notion_page_url"), "notion": notion_result}
            report_path = os.path.join(run_dir, "notion_export_report.json")
            _write_json(report_path, report)
            return FeatureResult(feature=self.name, success=True, summary="Notion and Obsidian export completed", details={**report, "report_path": report_path}, duration_seconds=time.monotonic() - start)
        except Exception as exc:
            return FeatureResult(feature=self.name, success=False, summary="Notion and Obsidian export failed", error=str(exc), duration_seconds=time.monotonic() - start)
