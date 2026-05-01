from __future__ import annotations
"""features/multi_project_compare.py
==================================
Side-by-side comparison dashboard for multiple Crucible analysis runs.

Scans the saved_projects/ directory relative to the workspace root,
loads analysis_result.json from the most-recent N runs, and produces:

* comparison_report.json -- machine-readable comparison data.
* comparison_report.html -- sortable HTML table (pure HTML + inline
  vanilla JS, zero external dependencies).

Usage::

    from crucible.feature_registry import run_features, FeatureConfig
    import crucible.features.multi_project_compare  # auto-registers

    config = FeatureConfig()
    results = run_features(
        "/path/to/run_dir",
        enabled_features=["multi_project_compare"],
        config=config,
    )

Environment variables
---------------------
COMPARE_MAX_RUNS        Max saved-project runs to include (default: 10).
COMPARE_INCLUDE_CURRENT Include current run_dir in comparison (default: 1).
COMPARE_ENABLED         Master switch; 0 = skip entirely (default: 1).
"""

import html as _html
import json
import os
import re
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from crucible.feature_registry import (
    BaseFeature,
    FeatureConfig,
    FeatureResult,
    register,
)


def _find_workspace_root(start: Path) -> Path:
    """Walk up from *start* to find saved_projects/, .git, or pyproject.toml."""
    current = start.resolve()
    for _ in range(12):
        if (current / "saved_projects").is_dir():
            return current
        if (current / ".git").exists() or (current / "pyproject.toml").exists():
            return current
        parent = current.parent
        if parent == current:
            break
        current = parent
    return start.resolve()


def _load_analysis_result(path: Path) -> Optional[Dict[str, Any]]:
    """Return parsed JSON from *path*, or None on any error."""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _parse_ts(name: str) -> Optional[datetime]:
    """Parse YYYYMMDD_HHMMSS timestamp from a directory name prefix."""
    m = re.match(r"^(\d{8})_(\d{6})", name)
    if m:
        try:
            return datetime.strptime(m.group(1) + m.group(2), "%Y%m%d%H%M%S")
        except ValueError:
            pass
    return None


def _coerce_str(val: Any, fallback: str = "") -> str:
    """Safely coerce a JSON field that may be a dict or None to a plain string."""
    if isinstance(val, dict):
        for key in list(val.keys())[:3]:
            if isinstance(val.get(key), str):
                return val[key]
        return fallback
    if val is None:
        return fallback
    return str(val)


def _collect_runs(
    ws: Path, max_runs: int, cur: Optional[Path]
) -> List[Dict[str, Any]]:
    """Gather the most-recent *max_runs* analysis results from saved_projects/."""
    sp = ws / "saved_projects"
    cands: List[tuple] = []
    if sp.is_dir():
        for entry in sp.iterdir():
            if not entry.is_dir():
                continue
            ar = entry / "analysis_result.json"
            if not ar.exists():
                continue
            try:
                cands.append((ar.stat().st_mtime, entry))
            except OSError:
                pass
    cands.sort(key=lambda x: x[0], reverse=True)
    cands = cands[:max_runs]
    runs: List[Dict[str, Any]] = []
    for mtime, entry in cands:
        data = _load_analysis_result(entry / "analysis_result.json")
        if data is None:
            continue
        ts = _parse_ts(entry.name)
        rd = ts.replace(tzinfo=timezone.utc).isoformat() if ts else datetime.fromtimestamp(mtime, tz=timezone.utc).isoformat()
        runs.append({
            "project_name": data.get("project_name", entry.name),
            "run_dir": str(entry), "date": rd,
            "score": data.get("score"),
            "risk_level": _coerce_str(data.get("risk_level")),
            "gate_decision": _coerce_str(
                data.get("gate_decision") if data.get("gate_decision") is not None
                else data.get("mode_used", "")
            ),
            "experiments_count": len(data.get("experiments") or []),
            "cost": data.get("cost"),
            "consensus": data.get("consensus", ""),
            "summary": data.get("summary", ""),
            "is_current": False,
        })
    if cur is not None:
        ar = cur / "analysis_result.json"
        if ar.exists():
            data = _load_analysis_result(ar)
            if data is not None:
                try:
                    mtime = ar.stat().st_mtime
                except OSError:
                    mtime = 0.0
                rd = datetime.fromtimestamp(mtime, tz=timezone.utc).isoformat()
                if str(cur.resolve()) not in {str(Path(r["run_dir"]).resolve()) for r in runs}:
                    runs.insert(0, {
                        "project_name": data.get("project_name", cur.name),
                        "run_dir": str(cur), "date": rd,
                        "score": data.get("score"),
                        "risk_level": _coerce_str(data.get("risk_level")),
                        "gate_decision": _coerce_str(
                data.get("gate_decision") if data.get("gate_decision") is not None
                else data.get("mode_used", "")
            ),
                        "experiments_count": len(data.get("experiments") or []),
                        "cost": data.get("cost"),
                        "consensus": data.get("consensus", ""),
                        "summary": data.get("summary", ""),
                        "is_current": True,
                    })
    return runs


def _risk_dist(runs: List[Dict[str, Any]]) -> Dict[str, int]:
    """Count runs by risk level."""
    d: Dict[str, int] = {}
    for r in runs:
        raw = r.get("risk_level")
        if isinstance(raw, dict):
            raw = raw.get("risk_level", "unknown")
        k = (str(raw) if raw else "unknown").strip().lower()
        d[k] = d.get(k, 0) + 1
    return dict(sorted(d.items()))


def _gate_bkdn(runs: List[Dict[str, Any]]) -> Dict[str, int]:
    """Count runs by gate decision."""
    d: Dict[str, int] = {}
    for r in runs:
        raw = r.get("gate_decision")
        if isinstance(raw, dict):
            raw = raw.get("gate_decision", "unknown")
        k = (str(raw) if raw else "unknown").strip().lower()
        d[k] = d.get(k, 0) + 1
    return dict(sorted(d.items()))


def _score_trend(runs: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Return runs sorted by date ascending with scores."""
    dated = sorted(
        [r for r in runs if r.get("score") is not None and r.get("date")],
        key=lambda x: x["date"],
    )
    return [
        {"date": r["date"], "project_name": r["project_name"], "score": r["score"]}
        for r in dated
    ]


def _top_n(runs: List[Dict[str, Any]], n: int = 3) -> List[Dict[str, Any]]:
    """Return the top *n* runs by score."""
    sc = sorted(
        [r for r in runs if r.get("score") is not None],
        key=lambda x: float(x["score"]) if x["score"] is not None else 0.0,
        reverse=True,
    )
    return [
        {"project_name": r["project_name"], "date": r["date"], "score": r["score"]}
        for r in sc[:n]
    ]


def _themes(runs: List[Dict[str, Any]], min_cnt: int = 2) -> List[str]:
    """Extract 3-word phrases appearing in >= *min_cnt* consensus/summary fields."""
    SW = frozenset({
        "the", "a", "an", "and", "or", "of", "to", "in", "for", "on",
        "with", "at", "by", "from", "is", "are", "that", "this", "it",
        "be", "as", "was", "will", "not", "but", "can", "if", "we",
        "our", "all", "each", "use", "using", "used", "run", "runs",
    })
    cnt: Counter = Counter()
    for r in runs:
        text = (r.get("consensus") or "") + " " + (r.get("summary") or "")
        words = re.findall(r"[a-zA-Z]+", text.lower())
        for i in range(len(words) - 2):
            cnt[" ".join(words[i: i + 3])] += 1
    out: List[str] = []
    for phrase, c in cnt.most_common(60):
        if c < min_cnt:
            break
        parts = phrase.split()
        if sum(1 for w in parts if w in SW) >= 2:
            continue
        out.append(phrase)
        if len(out) >= 10:
            break
    return out


def _avg(runs: List[Dict[str, Any]]) -> Optional[float]:
    """Return mean score across all scored runs."""
    scores = [float(r["score"]) for r in runs if r.get("score") is not None]
    return round(sum(scores) / len(scores), 2) if scores else None


_SORT_JS = (
    "(function(){"
    "var st={};"
    "window.sortTable=function(col){"
    "var t=document.getElementById('compareTable');"
    "var tb=t.querySelector('tbody');"
    "var rows=Array.from(tb.querySelectorAll('tr'));"
    "var ths=t.querySelectorAll('thead th');"
    "var asc=st[col]!==true;"
    "st={};st[col]=asc;"
    "ths.forEach(function(th,i){"
    "th.classList.remove('sorted-asc','sorted-desc');"
    "if(i===col)th.classList.add(asc?'sorted-asc':'sorted-desc');"
    "});"
    "rows.sort(function(a,b){"
    "var va=a.cells[col].textContent.trim();"
    "var vb=b.cells[col].textContent.trim();"
    "var na=parseFloat(va),nb=parseFloat(vb);"
    "if(!isNaN(na)&&!isNaN(nb))return asc?na-nb:nb-na;"
    "return asc?va.localeCompare(vb):vb.localeCompare(va);"
    "});"
    "rows.forEach(function(r){tb.appendChild(r);});"
    "};"
    "})();"
)


def _build_html(runs: List[Dict[str, Any]], report: Dict[str, Any]) -> str:
    """Render the sortable comparison HTML page."""
    rows: List[str] = []
    for r in runs:
        cls = " class='current'" if r.get("is_current") else ""
        cost = r.get("cost")
        try:
            cs = f"{float(cost):.4f}" if cost is not None else "&#8212;"
        except (TypeError, ValueError):
            cs = "&#8212;"
        sc = r.get("score")
        ss = str(sc) if sc is not None else "&#8212;"
        ex = r.get("experiments_count")
        es = str(ex) if ex is not None else "&#8212;"
        # HTML-escape user-controlled strings to prevent XSS / malformed HTML
        # when a project_name or gate_decision contains special characters.
        rows.append(
            f"<tr{cls}><td>{_html.escape(str(r.get('project_name', '')))}</td>"
            f"<td>{_html.escape(str(r.get('date', '')))}</td><td>{ss}</td>"
            f"<td>{_html.escape(str(r.get('risk_level', '')))}</td>"
            f"<td>{_html.escape(str(r.get('gate_decision', '')))}</td>"
            f"<td>{es}</td><td>{cs}</td></tr>"
        )
    rows_html = chr(10).join(rows)
    top3 = "".join(
        f"<li>#{i+1} {_html.escape(str(t.get('project_name', '')))} &mdash; "
        f"{_html.escape(str(t.get('score', '&mdash;')))}"
        f" ({_html.escape(str(t.get('date', ''))[:10])})</li>"
        for i, t in enumerate(report.get("top3_by_score", []))
    ) or "<li>No scored runs.</li>"
    themes_html = "".join(
        f"<li>{x}</li>" for x in report.get("common_themes", [])
    ) or "<li>Insufficient data.</li>"
    rd = ", ".join(
        f"{k}: {v}" for k, v in report.get("risk_distribution", {}).items()
    ) or "&mdash;"
    gd = ", ".join(
        f"{k}: {v}" for k, v in report.get("gate_breakdown", {}).items()
    ) or "&mdash;"
    _avg_val = report.get("average_score")
    avg_s = str(_avg_val) if _avg_val is not None else "&mdash;"
    tot_s = str(report.get("total_runs", len(runs)))
    style = (
        "body{font-family:system-ui,sans-serif;margin:2rem;background:#f5f5f5;color:#222}"
        "h1{color:#1a3a5c}"
        ".summary{display:flex;gap:1.5rem;flex-wrap:wrap;margin-bottom:1.5rem}"
        ".card{background:white;border-radius:8px;padding:1rem 1.5rem;"
        "box-shadow:0 1px 4px rgba(0,0,0,.1);min-width:140px}"
        ".card h3{margin:0 0 .3rem;font-size:.85rem;color:#666;text-transform:uppercase}"
        ".card p{margin:0;font-size:1.6rem;font-weight:700;color:#1a3a5c}"
        ".card p.sm{font-size:.95rem}"
        "table{border-collapse:collapse;width:100%;background:white;"
        "border-radius:8px;overflow:hidden;box-shadow:0 1px 4px rgba(0,0,0,.1)}"
        "th{background:#1a3a5c;color:white;padding:.6rem 1rem;text-align:left;"
        "cursor:pointer;user-select:none;white-space:nowrap}"
        "th:hover{background:#2c5f8a}"
        "th.sorted-asc::after{content:' ↑'}"
        "th.sorted-desc::after{content:' ↓'}"
        "td{padding:.5rem 1rem;border-bottom:1px solid #eee}"
        "tr:nth-child(even){background:#f9f9f9}"
        "tr:hover{background:#eef4ff}"
        ".current td{background:#fffbe6!important}"
        ".section{background:white;border-radius:8px;padding:1rem 1.5rem;"
        "margin-top:1.5rem;box-shadow:0 1px 4px rgba(0,0,0,.1)}"
        "ul,ol{margin:.5rem 0;padding-left:1.5rem}"
        ".footer{margin-top:1.5rem;font-size:.8rem;color:#999}"
    )
    return (
        f"<!DOCTYPE html><html lang='en'><head><meta charset='UTF-8'/>"
        f"<title>Crucible Multi-Project Comparison</title>"
        f"<style>{style}</style></head><body>"
        "<h1>Crucible Multi-Project Comparison</h1>"
        "<div class='summary'>"
        f"<div class='card'><h3>Total Runs</h3><p>{tot_s}</p></div>"
        f"<div class='card'><h3>Avg Score</h3><p>{avg_s}</p></div>"
        f"<div class='card'><h3>Risk Distribution</h3><p class='sm'>{rd}</p></div>"
        f"<div class='card'><h3>Gate Breakdown</h3><p class='sm'>{gd}</p></div>"
        "</div><h2>All Runs</h2>"
        "<table id='compareTable'><thead><tr>"
        "<th onclick='sortTable(0)'>Project Name</th>"
        "<th onclick='sortTable(1)'>Date</th>"
        "<th onclick='sortTable(2)'>Score</th>"
        "<th onclick='sortTable(3)'>Risk Level</th>"
        "<th onclick='sortTable(4)'>Gate Decision</th>"
        "<th onclick='sortTable(5)'>Experiments</th>"
        "<th onclick='sortTable(6)'>Cost</th>"
        f"</tr></thead><tbody>{rows_html}</tbody></table>"
        f"<div class='section'><h2>Top 3 Runs by Score</h2><ol>{top3}</ol></div>"
        f"<div class='section'><h2>Common Themes</h2><ul>{themes_html}</ul></div>"
        "<div class='footer'>Generated by Crucible multi_project_compare.</div>"
        f"<script>{_SORT_JS}</script></body></html>"
    )


@register("multi_project_compare")
class MultiProjectCompareFeature(BaseFeature):
    """Compare multiple Crucible analysis runs side-by-side.

    Scans saved_projects/ for the most-recent N runs (default 10) and
    writes comparison_report.json and comparison_report.html to
    the current run_dir.

    Usage example::

        from crucible.feature_registry import run_features, FeatureConfig
        import crucible.features.multi_project_compare

        results = run_features(
            "/path/to/run_dir",
            enabled_features=["multi_project_compare"],
            config=FeatureConfig(),
        )
    """

    name = "multi_project_compare"
    label = "Multi-Project Comparison"
    requires: list[str] = []

    def run(self, run_dir: str, config: FeatureConfig) -> FeatureResult:
        """Execute the comparison and write report files.

        Parameters
        ----------
        run_dir:
            Path to the current pipeline run directory.
        config:
            Shared feature configuration (env vars, LLM, args).

        Returns
        -------
        FeatureResult
            Success status with summary, report data, and artifact paths.
        """
        env: Dict[str, str] = config.env if config.env is not None else dict(os.environ)
        if env.get("COMPARE_ENABLED", "1").strip().lower() in ("0", "false", "no", "off"):
            return FeatureResult(
                feature=self.name, success=True, skipped=True,
                skip_reason="COMPARE_ENABLED is not 1.",
            )

        t0 = time.monotonic()
        warnings: List[str] = []
        artifacts: List[str] = []

        try:
            max_runs = int(env.get("COMPARE_MAX_RUNS", "10"))
        except ValueError:
            max_runs = 10
            warnings.append("COMPARE_MAX_RUNS invalid integer; defaulting to 10.")

        include_current = env.get("COMPARE_INCLUDE_CURRENT", "1").strip().lower() not in ("0", "false", "no", "off")
        rdp = Path(run_dir).resolve()
        ws = _find_workspace_root(rdp)
        cur: Optional[Path] = rdp if include_current else None

        runs: List[Dict[str, Any]] = []
        try:
            runs = _collect_runs(ws, max_runs, cur)
        except Exception as exc:
            warnings.append(f"Error collecting runs: {exc}")

        if not runs:
            return FeatureResult(
                feature=self.name, success=True,
                summary="No analysis runs found for comparison.",
                details={"warnings": warnings},
                duration_seconds=time.monotonic() - t0,
            )

        report: Dict[str, Any] = {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "workspace_root": str(ws),
            "total_runs": len(runs),
            "average_score": _avg(runs),
            "risk_distribution": _risk_dist(runs),
            "gate_breakdown": _gate_bkdn(runs),
            "score_trend": _score_trend(runs),
            "top3_by_score": _top_n(runs, 3),
            "common_themes": _themes(runs),
            "runs": [
                {
                    "project_name": r["project_name"],
                    "date": r["date"],
                    "score": r.get("score"),
                    "risk_level": r.get("risk_level"),
                    "gate_decision": r.get("gate_decision"),
                    "experiments_count": r.get("experiments_count"),
                    "cost": r.get("cost"),
                    "is_current": r.get("is_current", False),
                }
                for r in runs
            ],
        }

        jpath = rdp / "comparison_report.json"
        hpath = rdp / "comparison_report.html"
        _jtmp = jpath.parent / (jpath.name + ".tmp")
        try:
            _jtmp.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
            _jtmp.replace(jpath)
            artifacts.append(str(jpath))
        except OSError as _jexc:
            try:
                _jtmp.unlink(missing_ok=True)
            except OSError:
                pass
            warnings.append(f"Failed to write comparison_report.json: {_jexc}")
        _htmp = hpath.parent / (hpath.name + ".tmp")
        try:
            _htmp.write_text(_build_html(runs, report), encoding="utf-8")
            _htmp.replace(hpath)
            artifacts.append(str(hpath))
        except OSError as _hexc:
            try:
                _htmp.unlink(missing_ok=True)
            except OSError:
                pass
            warnings.append(f"Failed to write comparison_report.html: {_hexc}")

        return FeatureResult(
            feature=self.name,
            success=True,
            summary=(
                f"Compared {len(runs)} runs; "
                f"avg score={report['average_score']}; "
                "wrote comparison_report.json and comparison_report.html."
            ),
            details={
                "multi_project_compare": report,
                "artifacts": artifacts,
                "warnings": warnings,
            },
            duration_seconds=time.monotonic() - t0,
        )
