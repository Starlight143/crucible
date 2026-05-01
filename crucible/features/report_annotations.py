from __future__ import annotations
"""Report annotation generator for Crucible run artifacts.

This feature maintains ``annotations.json`` and writes ``annotated_report.json``
with a newly derived annotation for consensus, risk, or general analysis
sections.
"""

import hashlib
import json
import os
import time
from datetime import datetime, timezone
from typing import Any, Dict, List

from crucible.feature_registry import (
    BaseFeature,
    FeatureConfig,
    FeatureResult,
    register,
)


def _load_json(path: str, fallback: Any) -> Any:
    try:
        if not os.path.isfile(path):
            return fallback
        with open(path, "r", encoding="utf-8") as fh:
            return json.loads(fh.read())
    except (OSError, json.JSONDecodeError, TypeError):
        return fallback


def _write_text(path: str, content: str) -> None:
    # Atomic write via sibling .tmp + os.replace: if the process is killed
    # between open() and close(), the existing file is left intact.
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


def _write_json(path: str, payload: Any) -> None:
    _write_text(path, json.dumps(payload, indent=2, ensure_ascii=False) + "\n")


def _as_annotations(data: Any) -> List[Dict[str, Any]]:
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]
    if isinstance(data, dict) and isinstance(data.get("annotations"), list):
        return [item for item in data["annotations"] if isinstance(item, dict)]
    return []


def _has_risk_data(analysis: Dict[str, Any]) -> bool:
    for key in ("risk", "risks", "risk_level", "risk_assessment", "risk_summary"):
        value = analysis.get(key)
        if value not in (None, "", [], {}):
            return True
    return False


def _annotation_text(section: str, analysis: Dict[str, Any]) -> str:
    if section == "consensus":
        # Use explicit None check so score=0 is preserved (not treated as falsy)
        score = next(
            (analysis[k] for k in ("score", "final_score", "consensus_score") if analysis.get(k) is not None),
            None,
        )
        consensus = analysis.get("consensus") or analysis.get("summary") or analysis.get("analysis_summary") or ""
        return f"Consensus score: {score}. {str(consensus).strip()}".strip()
    if section == "risk":
        risk_level = analysis.get("risk_level") or analysis.get("risk") or "review required"
        risk_summary = analysis.get("risk_summary") or analysis.get("risk_assessment") or analysis.get("risks") or ""
        return f"Risk level: {risk_level}. {str(risk_summary).strip()}".strip()
    general = analysis.get("summary") or analysis.get("analysis_summary") or analysis.get("user_problem") or "Run analysis completed."
    return str(general).strip()


@register("report_annotations")
class ReportAnnotationsFeature(BaseFeature):
    name = "report_annotations"
    label = "Report Annotations"
    requires: list[str] = []

    def run(self, run_dir: str, config: FeatureConfig) -> FeatureResult:
        start = time.monotonic()
        _env = config.env if config.env is not None else dict(os.environ)
        if _env.get("ANNOTATIONS_ENABLED", "1").strip().lower() in ("0", "false", "no", "off"):
            return FeatureResult(feature=self.name, success=True, summary="disabled", skipped=True, skip_reason="disabled")
        try:
            annotations_path = os.path.join(run_dir, "annotations.json")
            analysis_path = os.path.join(run_dir, "analysis_result.json")
            annotated_path = os.path.join(run_dir, "annotated_report.json")
            if not os.path.isfile(annotations_path):
                _write_json(annotations_path, {"annotations": []})
            annotations = _as_annotations(_load_json(annotations_path, {"annotations": []}))
            analysis = _load_json(analysis_path, {})
            if not isinstance(analysis, dict):
                analysis = {}
            section = "consensus" if any(analysis.get(key) is not None for key in ("score", "final_score", "consensus_score")) else "risk" if _has_risk_data(analysis) else "general"
            created_at = datetime.now(timezone.utc).isoformat()
            text = _annotation_text(section, analysis)
            digest = hashlib.sha256(f"{text}{run_dir}{created_at}".encode("utf-8")).hexdigest()
            annotation = {"id": digest, "author": os.environ.get("ANNOTATIONS_AUTHOR", "system"), "created_at": created_at, "section": section, "text": text, "tags": []}
            seen = {str(item.get("id")) for item in annotations}
            if annotation["id"] not in seen:
                annotations.append(annotation)
            _write_json(annotations_path, {"annotations": annotations})
            annotated = {"analysis_result": analysis, "annotations": annotations, "latest_annotation": annotation}
            _write_json(annotated_path, annotated)
            return FeatureResult(feature=self.name, success=True, summary="Report annotation written", details={"annotations_path": annotations_path, "annotated_report_path": annotated_path, "section": section, "annotation_id": digest}, duration_seconds=time.monotonic() - start)
        except Exception as exc:
            return FeatureResult(feature=self.name, success=False, summary="Report annotation failed", error=str(exc), duration_seconds=time.monotonic() - start)
