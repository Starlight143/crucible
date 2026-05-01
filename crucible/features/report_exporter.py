"""
features/report_exporter.py
============================
Self-contained HTML and PDF report exporter for completed pipeline runs.

Combines ``analysis_result.json``, ``security_report.json``,
``independent_validation_report.json``, ``dependency_audit_report.json``,
and ``auto_remediation_report.json`` into a single, self-contained HTML
file with inline CSS — no external dependencies required.

A PDF variant is also available via ``export_pdf_report``.  It uses
``fpdf2`` when installed, and falls back to a pure-stdlib PDF writer.

Usage::

    from crucible.features.report_exporter import export_html_report
    path = export_html_report("/path/to/run_dir")
    print(f"Report saved to {path}")

    from crucible.features.report_exporter import export_pdf_report
    pdf_path = export_pdf_report("/path/to/run_dir")
    print(f"PDF report saved to {pdf_path}")
"""
from __future__ import annotations

import html
import json
import os
import re as _re
from typing import Any, Dict, List, Optional

# ── Helpers ──────────────────────────────────────────────────────────────────

def _load_json(path: str) -> Dict[str, Any]:
    if not os.path.isfile(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError):
        return {}


def _esc(value: Any) -> str:
    """HTML-escape a value."""
    return html.escape(str(value)) if value is not None else ""


def _score_color(score: Any) -> str:
    try:
        s = float(score)
        if s >= 70:
            return "#22c55e"
        if s >= 50:
            return "#eab308"
        return "#ef4444"
    except (TypeError, ValueError):
        return "#9ca3af"


def _severity_color(severity: str) -> str:
    s = severity.upper()
    if s in ("CRITICAL", "HIGH"):
        return "#ef4444"
    if s == "MEDIUM":
        return "#eab308"
    return "#9ca3af"


# ── HTML builder ─────────────────────────────────────────────────────────────

_CSS = """\
* { margin: 0; padding: 0; box-sizing: border-box; }
body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
       background: #0f172a; color: #e2e8f0; line-height: 1.6; padding: 2rem; }
.container { max-width: 960px; margin: 0 auto; }
h1 { font-size: 1.75rem; margin-bottom: 0.5rem; color: #f8fafc; }
h2 { font-size: 1.25rem; margin: 1.5rem 0 0.75rem; color: #94a3b8;
     border-bottom: 1px solid #334155; padding-bottom: 0.25rem;
     cursor: pointer; user-select: none; display: flex; align-items: center; gap: 0.5rem; }
h2::before { content: '▼'; font-size: 0.7rem; color: #475569; transition: transform 0.2s; }
h2.collapsed::before { transform: rotate(-90deg); }
.section-body { overflow: hidden; transition: max-height 0.25s ease; }
.section-body.collapsed { max-height: 0 !important; }
h3 { font-size: 1rem; margin: 1rem 0 0.5rem; color: #cbd5e1; }
.meta { color: #64748b; font-size: 0.875rem; margin-bottom: 1.5rem; }
.score-badge { display: inline-block; padding: 0.25rem 0.75rem;
               border-radius: 0.375rem; font-weight: 700; font-size: 1.25rem; }
.card { background: #1e293b; border-radius: 0.5rem; padding: 1rem 1.25rem;
        margin-bottom: 1rem; border: 1px solid #334155; }
.card-title { font-weight: 600; color: #f1f5f9; margin-bottom: 0.5rem; }
table { width: 100%; border-collapse: collapse; margin: 0.5rem 0; }
th { padding: 0.5rem 0.75rem; text-align: left; border-bottom: 1px solid #334155;
     color: #94a3b8; font-size: 0.8125rem; text-transform: uppercase;
     cursor: pointer; user-select: none; white-space: nowrap; }
th:hover { color: #e2e8f0; }
th.sort-asc::after { content: ' ▲'; font-size: 0.6rem; }
th.sort-desc::after { content: ' ▼'; font-size: 0.6rem; }
td { padding: 0.5rem 0.75rem; text-align: left; border-bottom: 1px solid #334155;
     color: #cbd5e1; font-size: 0.875rem; }
tr.hidden { display: none; }
.tag { display: inline-block; padding: 0.125rem 0.5rem; border-radius: 0.25rem;
       font-size: 0.75rem; font-weight: 600; }
.pass { background: #166534; color: #bbf7d0; }
.fail { background: #991b1b; color: #fecaca; }
.warn { background: #854d0e; color: #fef08a; }
.text-block { white-space: pre-wrap; font-size: 0.875rem; color: #cbd5e1;
              background: #0f172a; border-radius: 0.375rem; padding: 0.75rem;
              border: 1px solid #1e293b; margin: 0.5rem 0; }
.footer { margin-top: 2rem; text-align: center; color: #475569; font-size: 0.75rem; }
/* Search / filter toolbar */
.toolbar { display: flex; gap: 0.75rem; align-items: center; margin: 1rem 0 1.5rem; flex-wrap: wrap; }
.search-input { flex: 1 1 240px; background: #1e293b; border: 1px solid #334155;
                color: #e2e8f0; border-radius: 0.375rem; padding: 0.5rem 0.75rem;
                font-size: 0.875rem; outline: none; }
.search-input:focus { border-color: #3b82f6; }
.filter-select { background: #1e293b; border: 1px solid #334155; color: #94a3b8;
                 border-radius: 0.375rem; padding: 0.5rem 0.75rem; font-size: 0.875rem;
                 outline: none; cursor: pointer; }
.search-count { color: #475569; font-size: 0.8125rem; white-space: nowrap; }
/* TOC nav */
.toc { background: #1e293b; border: 1px solid #334155; border-radius: 0.5rem;
       padding: 0.75rem 1rem; margin-bottom: 1.5rem; display: flex; gap: 1rem;
       flex-wrap: wrap; }
.toc a { color: #64748b; font-size: 0.8125rem; text-decoration: none; }
.toc a:hover { color: #94a3b8; }
"""


# ── Inline JavaScript for interactive features ────────────────────────────────

_JS = """\
(function() {
  // ── Collapsible sections ─────────────────────────────────────────────────
  document.querySelectorAll('h2').forEach(function(h2) {
    var body = h2.nextElementSibling;
    if (!body || !body.classList.contains('section-body')) return;
    body.style.maxHeight = body.scrollHeight + 'px';
    h2.addEventListener('click', function() {
      var collapsed = body.classList.toggle('collapsed');
      h2.classList.toggle('collapsed', collapsed);
      if (!collapsed) body.style.maxHeight = body.scrollHeight + 'px';
    });
  });

  // ── Global search / filter ────────────────────────────────────────────────
  var searchInput = document.getElementById('qs-search');
  var countEl = document.getElementById('qs-count');
  if (searchInput) {
    searchInput.addEventListener('input', function() {
      var q = this.value.trim().toLowerCase();
      var cards = document.querySelectorAll('.card, tr[data-searchable]');
      var shown = 0, total = cards.length;
      cards.forEach(function(el) {
        var text = (el.textContent || el.innerText || '').toLowerCase();
        var match = !q || text.includes(q);
        if (el.tagName === 'TR') {
          el.classList.toggle('hidden', !match);
        } else {
          el.style.display = match ? '' : 'none';
        }
        if (match) shown++;
      });
      if (countEl) countEl.textContent = q ? shown + ' / ' + total + ' items' : '';
    });
  }

  // ── Sortable tables ───────────────────────────────────────────────────────
  document.querySelectorAll('table').forEach(function(table) {
    var headers = table.querySelectorAll('th');
    headers.forEach(function(th, col) {
      th.addEventListener('click', function() {
        var asc = !th.classList.contains('sort-asc');
        headers.forEach(function(h) { h.classList.remove('sort-asc', 'sort-desc'); });
        th.classList.add(asc ? 'sort-asc' : 'sort-desc');
        var tbody = table.querySelector('tbody') || table;
        var rows = Array.from(tbody.querySelectorAll('tr[data-searchable], tr:not([data-searchable])'))
                        .filter(function(r) { return r.parentNode === tbody || r.parentNode === table; });
        rows.sort(function(a, b) {
          var at = (a.cells[col] || {}).textContent || '';
          var bt = (b.cells[col] || {}).textContent || '';
          var an = parseFloat(at), bn = parseFloat(bt);
          if (!isNaN(an) && !isNaN(bn)) return asc ? an - bn : bn - an;
          return asc ? at.localeCompare(bt) : bt.localeCompare(at);
        });
        rows.forEach(function(r) { (tbody || table).appendChild(r); });
      });
    });
  });
})();
"""


def _build_overview_section(analysis: Dict[str, Any], meta: Dict[str, Any]) -> str:
    project = _esc(analysis.get("project_name") or meta.get("project_name") or "Unknown")
    score = analysis.get("score")
    risk = _esc(analysis.get("risk_level") or "unknown")
    mode = _esc(meta.get("mode") or analysis.get("mode_used") or "N/A")
    provider = _esc(meta.get("llm_provider") or "N/A")
    timestamp = _esc(meta.get("timestamp") or "N/A")
    score_str = f"{score}/100" if score is not None else "N/A"
    color = _score_color(score)

    consensus = _esc(analysis.get("consensus") or "")
    disagreement = _esc(analysis.get("disagreement") or "")

    sections = f"""
<h1>{project}</h1>
<p class="meta">Mode: {mode} | Provider: {provider} | {timestamp}</p>
<div class="card">
  <span class="score-badge" style="background:{color};color:#000">{score_str}</span>
  <span style="margin-left:1rem;color:#94a3b8">Risk: <strong>{risk}</strong></span>
</div>
"""
    if consensus:
        sections += f"""
<h2>Consensus</h2>
<div class="text-block">{consensus}</div>
"""
    if disagreement:
        sections += f"""
<h2>Disagreement</h2>
<div class="text-block">{disagreement}</div>
"""
    # Experiments
    experiments = analysis.get("experiments") or []
    if experiments:
        sections += "<h2>Proposed Experiments</h2>\n"
        for exp in experiments[:5]:
            if isinstance(exp, dict):
                goal = _esc(exp.get("goal") or "")
                criteria = _esc(exp.get("criteria") or "")
                sections += f'<div class="card"><div class="card-title">{goal}</div>'
                if criteria:
                    sections += f"<div style='color:#94a3b8;font-size:0.875rem'>Criteria: {criteria}</div>"
                sections += "</div>\n"
    return sections


def _build_security_section(security: Dict[str, Any]) -> str:
    if not security:
        return ""
    passed = bool(security.get("passed", True))
    scanner = _esc(security.get("scanner_used") or "")
    try:
        high_count = int(float(security.get("high_severity_count") or 0))
    except (ValueError, TypeError):
        high_count = 0
    tag_class = "pass" if passed else "fail"
    tag_text = "PASS" if passed else "FAIL"

    html_str = f"""
<h2>Security Scan</h2>
<div class="card">
  <span class="tag {tag_class}">{tag_text}</span>
  <span style="margin-left:0.5rem;color:#94a3b8">Scanner: {scanner} | HIGH issues: {high_count}</span>
</div>
"""
    issues = security.get("issues") or []
    if issues:
        html_str += "<table><tr><th>Severity</th><th>Rule</th><th>File</th><th>Description</th></tr>\n"
        for iss in issues[:15]:
            raw_sev = iss.get("severity", "")
            sev = _esc(raw_sev)
            rule = _esc(iss.get("rule_id", ""))
            fname = _esc(iss.get("file", ""))
            # Truncate raw string before escaping to avoid splitting HTML entities mid-sequence
            desc = _esc(str(iss.get("description", ""))[:120])
            color = _severity_color(raw_sev)
            html_str += (
                f'<tr><td><span class="tag" style="background:{color};color:#fff">'
                f"{sev}</span></td><td>{rule}</td><td>{fname}</td><td>{desc}</td></tr>\n"
            )
        html_str += "</table>\n"
    return html_str


def _build_validation_section(validation: Dict[str, Any]) -> str:
    if not validation:
        return ""
    verdict = str(validation.get("overall_verdict", "unknown")).upper()
    tag_class = {"PASS": "pass", "FAIL": "fail", "WARNING": "warn"}.get(verdict, "warn")

    html_str = f"""
<h2>Independent Validation</h2>
<div class="card">
  <span class="tag {tag_class}">{verdict}</span>
</div>
"""
    # Execution phases
    phases = validation.get("execution_phases") or []
    if phases:
        html_str += "<table><tr><th>Phase</th><th>Status</th></tr>\n"
        for p in phases:
            phase_name = _esc(p.get("phase", ""))
            if p.get("timed_out"):
                tag = '<span class="tag warn">TIMEOUT</span>'
            elif p.get("passed"):
                tag = '<span class="tag pass">PASS</span>'
            else:
                tag = '<span class="tag fail">FAIL</span>'
            html_str += f"<tr><td>{phase_name}</td><td>{tag}</td></tr>\n"
        html_str += "</table>\n"

    # Adversarial findings
    findings = validation.get("adversarial_findings") or []
    if findings:
        html_str += "<h3>Adversarial Findings</h3>\n"
        html_str += "<table><tr><th>Severity</th><th>Category</th><th>File</th><th>Description</th></tr>\n"
        for f in findings[:10]:
            raw_sev = f.get("severity", "")
            sev = _esc(raw_sev)
            cat = _esc(f.get("category", ""))
            fname = _esc(f.get("file", ""))
            # Truncate raw string before escaping to avoid splitting HTML entities mid-sequence
            desc = _esc(str(f.get("description", ""))[:120])
            color = _severity_color(raw_sev)
            html_str += (
                f'<tr><td><span class="tag" style="background:{color};color:#fff">'
                f"{sev}</span></td><td>{cat}</td><td>{fname}</td><td>{desc}</td></tr>\n"
            )
        html_str += "</table>\n"
    return html_str


def _build_dependency_section(dep_audit: Dict[str, Any]) -> str:
    if not dep_audit:
        return ""
    passed = bool(dep_audit.get("passed", True))
    scanner = _esc(dep_audit.get("scanner_used") or "")
    try:
        vuln_count = int(float(dep_audit.get("vulnerability_count") or 0))
    except (ValueError, TypeError):
        vuln_count = 0
    tag_class = "pass" if passed else "fail"
    tag_text = "PASS" if passed else "FAIL"

    html_str = f"""
<h2>Dependency Audit</h2>
<div class="card">
  <span class="tag {tag_class}">{tag_text}</span>
  <span style="margin-left:0.5rem;color:#94a3b8">Scanner: {scanner} | Vulnerabilities: {vuln_count}</span>
</div>
"""
    vulns = dep_audit.get("vulnerabilities") or []
    if vulns:
        html_str += "<table><tr><th>Package</th><th>Version</th><th>CVE</th><th>Fix</th></tr>\n"
        for v in vulns[:15]:
            pkg = _esc(v.get("package", ""))
            ver = _esc(v.get("installed_version", ""))
            vid = _esc(v.get("vuln_id", ""))
            fix = _esc(v.get("fix_version", "N/A"))
            html_str += f"<tr><td>{pkg}</td><td>{ver}</td><td>{vid}</td><td>{fix}</td></tr>\n"
        html_str += "</table>\n"
    return html_str


def _build_remediation_section(remediation: Dict[str, Any]) -> str:
    if not remediation:
        return ""
    rounds = remediation.get("rounds_executed", 0)
    applied = remediation.get("total_patches_applied", 0)
    attempted = remediation.get("total_patches_attempted", 0)
    initial = remediation.get("initial_issue_count", 0)
    final = remediation.get("final_issue_count", 0)

    html_str = f"""
<h2>Auto-Remediation</h2>
<div class="card">
  <div>Rounds: {rounds} | Patches: {applied}/{attempted} applied</div>
  <div>Issues: {initial} &rarr; {final}</div>
</div>
"""
    return html_str


# ── v16.9 section builders ────────────────────────────────────────────────────

def _build_quality_score_section(quality: Dict[str, Any]) -> str:
    if not quality:
        return ""
    total = quality.get("total")
    dims = quality.get("dimensions") or {}
    if total is None and not dims:
        return ""
    color = _score_color(total)
    score_display = f"{total}/100" if total is not None else "N/A"
    html_str = f"""
<h2>LLM Quality Score</h2>
<div class="card">
  <span class="score-badge" style="background:{color};color:#000">{score_display}</span>
  <span style="margin-left:0.75rem;color:#94a3b8;font-size:0.875rem">
    {_esc(quality.get('summary') or '')}
  </span>
</div>
"""
    if dims:
        html_str += "<table><tr><th>Dimension</th><th>Score</th><th>Max</th></tr>\n"
        for dim_name, dim_val in dims.items():
            score_v = dim_val.get("score") if isinstance(dim_val, dict) else dim_val
            max_v = dim_val.get("max", 20) if isinstance(dim_val, dict) else 20
            html_str += f"<tr data-searchable><td>{_esc(dim_name)}</td><td>{score_v}</td><td>{max_v}</td></tr>\n"
        html_str += "</table>\n"
    return html_str


def _build_options_section(options: Dict[str, Any]) -> str:
    if not options or not options.get("options_relevant"):
        return ""
    params = options.get("parameters") or {}
    grid = options.get("pricing_grid") or []
    if not grid:
        return ""
    spot = params.get("spot", "N/A")
    sigma = params.get("sigma", "N/A")
    html_str = f"""
<h2>Options Pricing Analysis</h2>
<div class="card">
  <div>Spot: <strong>{spot}</strong> &nbsp;|&nbsp;
       Volatility: <strong>{sigma}</strong> &nbsp;|&nbsp;
       Grid points: <strong>{len(grid)}</strong></div>
</div>
<table>
<tr><th>Strike%</th><th>Maturity(d)</th><th>Call</th><th>Put</th>
    <th>Δ Call</th><th>Γ</th><th>θ Call/d</th><th>Vega/1%</th></tr>
"""
    for row in grid[:20]:
        html_str += (
            f"<tr data-searchable>"
            f"<td>{row.get('strike_pct','')}</td>"
            f"<td>{row.get('maturity_days','')}</td>"
            f"<td>{row.get('call_price','')}</td>"
            f"<td>{row.get('put_price','')}</td>"
            f"<td>{row.get('delta_call','')}</td>"
            f"<td>{row.get('gamma','')}</td>"
            f"<td>{row.get('theta_call','')}</td>"
            f"<td>{row.get('vega','')}</td>"
            f"</tr>\n"
        )
    html_str += "</table>\n"
    return html_str


def _build_alt_data_section(alt: Dict[str, Any]) -> str:
    if not alt:
        return ""
    symbol = _esc(alt.get("symbol") or "")
    sentiment = _esc(alt.get("overall_sentiment") or "neutral")
    news = alt.get("news_sentiment") or {}
    reddit = alt.get("reddit_signals") or {}
    events = alt.get("economic_events") or []
    color_map = {"bullish": "#166534", "bearish": "#991b1b", "neutral": "#334155"}
    sent_color = color_map.get(alt.get("overall_sentiment", "neutral"), "#334155")
    html_str = f"""
<h2>Alternative Data</h2>
<div class="card">
  <strong>{symbol}</strong> &nbsp;
  <span class="tag" style="background:{sent_color};color:#e2e8f0">{sentiment}</span>
  &nbsp;|&nbsp; News articles: {news.get('articles_analyzed', 0)}
  &nbsp;|&nbsp; Reddit mentions: {reddit.get('mentions_24h', 0)}
</div>
"""
    if events:
        html_str += "<table><tr><th>Date</th><th>Event</th><th>Impact</th></tr>\n"
        for ev in events[:8]:
            html_str += (
                f"<tr data-searchable><td>{_esc(ev.get('date',''))}</td>"
                f"<td>{_esc(ev.get('event',''))}</td>"
                f"<td>{_esc(ev.get('impact',''))}</td></tr>\n"
            )
        html_str += "</table>\n"
    return html_str


def _build_type_coverage_section(tc: Dict[str, Any]) -> str:
    if not tc:
        return ""
    agg = tc.get("aggregate_coverage_pct")
    files = tc.get("files") or []
    if agg is None and not files:
        return ""
    color = _score_color(agg)
    agg_display = f"{agg:.1f}" if agg is not None else "N/A"
    html_str = f"""
<h2>Type Coverage</h2>
<div class="card">
  <span class="score-badge" style="background:{color};color:#000">{agg_display}%</span>
  <span style="margin-left:0.75rem;color:#94a3b8">
    {tc.get('total_files', 0)} file(s) &nbsp;|&nbsp;
    mypy errors: {tc.get('mypy_errors', 'N/A')}
  </span>
</div>
"""
    if files:
        html_str += "<table><tr><th>File</th><th>Coverage%</th><th>Params</th><th>Returns</th></tr>\n"
        for f in sorted(files, key=lambda x: x.get("coverage_pct") or 0.0):
            cov_pct = f.get('coverage_pct')
            cov_display = f"{cov_pct:.1f}" if cov_pct is not None else "N/A"
            html_str += (
                f"<tr data-searchable>"
                f"<td>{_esc(f.get('file',''))}</td>"
                f"<td>{cov_display}%</td>"
                f"<td>{f.get('annotated_params',0)}/{f.get('total_params',0)}</td>"
                f"<td>{f.get('annotated_returns',0)}/{f.get('total_returns',0)}</td>"
                f"</tr>\n"
            )
        html_str += "</table>\n"
    return html_str


def _build_citations_section(cit: Dict[str, Any]) -> str:
    if not cit:
        return ""
    total = cit.get("total_citations")
    if total is None:
        return ""
    reachable = cit.get("reachable", 0)
    unreachable = cit.get("unreachable", 0)
    citations_list = cit.get("citations") or []
    html_str = f"""
<h2>Citation Verification</h2>
<div class="card">
  Total: <strong>{total}</strong> &nbsp;|&nbsp;
  <span class="tag pass">✓ {reachable}</span> &nbsp;
  <span class="tag fail">✗ {unreachable}</span>
</div>
"""
    if citations_list:
        html_str += "<table><tr><th>URL</th><th>Status</th><th>Code</th><th>ms</th></tr>\n"
        for c in citations_list[:20]:
            url = _esc(str(c.get("url", ""))[:60])
            ok = c.get("reachable", False)
            tag = f'<span class="tag {"pass" if ok else "fail"}">{"✓" if ok else "✗"}</span>'
            code = c.get("status_code", "")
            ms = c.get("check_ms", "")
            html_str += f"<tr data-searchable><td>{url}</td><td>{tag}</td><td>{code}</td><td>{ms}</td></tr>\n"
        html_str += "</table>\n"
    return html_str


# ── Main entry point ─────────────────────────────────────────────────────────

def export_html_report(
    run_dir: str,
    *,
    output_filename: str = "report.html",
) -> str:
    """
    Generate a self-contained HTML report from all available run artifacts.

    Args:
        run_dir:          Path to a completed run output directory.
        output_filename:  Output file name (default: ``report.html``).

    Returns:
        Absolute path to the generated HTML file.

    See also:
        ``export_pdf_report`` — generates a PDF version of the same data.
    """
    analysis = _load_json(os.path.join(run_dir, "analysis_result.json"))
    meta = _load_json(os.path.join(run_dir, "run_meta.json"))
    security = _load_json(os.path.join(run_dir, "security_report.json"))
    validation = _load_json(os.path.join(run_dir, "independent_validation_report.json"))
    dep_audit = _load_json(os.path.join(run_dir, "dependency_audit_report.json"))
    remediation = _load_json(os.path.join(run_dir, "auto_remediation_report.json"))

    # v16.9 additional artifacts (loaded when present)
    quality_score = _load_json(os.path.join(run_dir, "llm_quality_score.json"))
    options_data  = _load_json(os.path.join(run_dir, "options_analysis.json"))
    alt_data      = _load_json(os.path.join(run_dir, "alt_data_report.json"))
    type_cov      = _load_json(os.path.join(run_dir, "type_coverage_report.json"))
    citations     = _load_json(os.path.join(run_dir, "citation_verification_report.json"))

    body_parts = [
        _build_overview_section(analysis, meta),
        _build_security_section(security),
        _build_validation_section(validation),
        _build_dependency_section(dep_audit),
        _build_remediation_section(remediation),
        _build_quality_score_section(quality_score),
        _build_options_section(options_data),
        _build_alt_data_section(alt_data),
        _build_type_coverage_section(type_cov),
        _build_citations_section(citations),
    ]

    # Wrap each h2 section body in its own collapsible div.
    # The old code used count=1, so only the FIRST <h2> per part got a
    # section-body div; subsequent headings (Disagreement, Experiments) were
    # left unwrapped and the JS toggle had no element to show/hide.
    # The fix: split each part on every <h2> boundary and emit an independent
    # section-body div for each heading so the JS can collapse them separately.
    _H2_SPLIT_RE = _re.compile(r'(<h2[^>]*>.*?</h2>)\s*', _re.DOTALL)
    wrapped_parts: List[str] = []
    for part in body_parts:
        if not part:
            continue
        segments = _H2_SPLIT_RE.split(part)
        # split() on a pattern with a capturing group returns:
        #   [text_before_h2_1, h2_1, text_after_h2_1, h2_2, text_after_h2_2, ...]
        # Even indices (0, 2, 4, ...): content between/after h2 tags.
        # Odd  indices (1, 3, 5, ...): h2 tags themselves.
        if len(segments) == 1:
            # No h2 found — keep as-is (e.g. pure prose sections)
            wrapped_parts.append(part)
            continue
        pieces: List[str] = [segments[0]]  # content before first h2 (usually "")
        has_open_div = False
        for idx in range(1, len(segments)):
            if idx % 2 == 1:  # h2 tag
                if has_open_div:
                    pieces.append('\n</div>\n')
                pieces.append(segments[idx])
                pieces.append('\n<div class="section-body">')
                has_open_div = True
            else:  # content following an h2
                pieces.append(segments[idx])
        if has_open_div:
            pieces.append('\n</div>\n')
        wrapped_parts.append(''.join(pieces))

    # Build Table of Contents from h2 headings
    toc_links: List[str] = []
    _h2_re = _re.compile(r'<h2[^>]*>(.*?)</h2>', _re.DOTALL)
    for part in body_parts:
        if not part:
            continue
        for m in _h2_re.finditer(part):
            label = _re.sub(r'<[^>]+>', '', m.group(1)).strip()
            if label:
                anchor = label.lower().replace(' ', '-').replace('/', '-')
                toc_links.append(f'<a href="#{anchor}">{_esc(label)}</a>')

    toc_html = '<nav class="toc">' + ' &nbsp;·&nbsp; '.join(toc_links) + '</nav>' if toc_links else ''

    toolbar_html = """
<div class="toolbar">
  <input id="qs-search" class="search-input" type="search"
         placeholder="Search across all sections…" autocomplete="off">
  <span id="qs-count" class="search-count"></span>
</div>
"""

    body_html = "\n".join(wrapped_parts)

    full_html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Crucible Analysis Report</title>
<style>{_CSS}</style>
</head>
<body>
<div class="container">
{toc_html}
{toolbar_html}
{body_html}
<div class="footer">Generated by Crucible v16.9</div>
</div>
<script>{_JS}</script>
</body>
</html>
"""
    output_path = os.path.join(run_dir, output_filename)
    _tmp_path = output_path + ".tmp"
    try:
        with open(_tmp_path, "w", encoding="utf-8") as fh:
            fh.write(full_html)
        os.replace(_tmp_path, output_path)
    except OSError as exc:
        try:
            os.unlink(_tmp_path)
        except OSError:
            pass
        raise OSError(f"export_html_report: could not write '{output_path}': {exc}") from exc

    return output_path


# ── PDF export ────────────────────────────────────────────────────────────────


class _SimplePDFWriter:
    """
    Minimal valid PDF 1.4 writer using Python stdlib only.

    Supports text pages with a fixed Helvetica font, multi-line wrapping, and
    a proper cross-reference table.  ASCII-safe only (non-ASCII chars are
    replaced with '?').  Not a general-purpose PDF library; sufficient for
    plain-text report pages.

    Usage::

        w = _SimplePDFWriter()
        w.add_page()
        w.draw_text(50, 750, "Hello World", size=14, bold=True)
        w.draw_text(50, 720, "Normal text", size=11)
        pdf_bytes = w.render()
    """

    # A4 dimensions in points (1 pt = 1/72 inch)
    PAGE_WIDTH: int = 595
    PAGE_HEIGHT: int = 842

    # Line height multiplier
    _LINE_HEIGHT_RATIO: float = 1.4

    def __init__(self) -> None:
        self._objects: List[bytes] = []   # raw PDF object bodies (1-indexed)
        self._pages: List[int] = []       # object indices for page objects
        self._page_content_ids: List[int] = []  # content stream obj ids per page

        # Reserve object 1 for catalog, object 2 for pages parent
        self._objects.append(b"")   # placeholder obj 1 (catalog) — filled at render
        self._objects.append(b"")   # placeholder obj 2 (pages)   — filled at render

        # Current page content buffer
        self._current_content: Optional[List[str]] = None

    # ── Page management ───────────────────────────────────────────────────────

    def add_page(self) -> None:
        """Start a new page.  Must be called before draw_text."""
        if self._current_content is not None:
            self._flush_page()
        self._current_content = []

    def _flush_page(self) -> None:
        if self._current_content is None:
            return
        # Build content stream
        stream_body = "\n".join(self._current_content).encode("latin-1", errors="replace")

        # Content stream object
        content_obj = (
            f"<< /Length {len(stream_body)} >>\nstream\n".encode("latin-1")
            + stream_body
            + b"\nendstream"
        )
        content_id = len(self._objects) + 1
        self._objects.append(content_obj)

        # Page object
        page_obj_body = (
            f"<< /Type /Page\n"
            f"   /Parent 2 0 R\n"
            f"   /MediaBox [0 0 {self.PAGE_WIDTH} {self.PAGE_HEIGHT}]\n"
            f"   /Contents {content_id} 0 R\n"
            f"   /Resources << /Font << /F1 << /Type /Font /Subtype /Type1"
            f" /BaseFont /Helvetica >> "
            f"/F2 << /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold >> >> >>\n"
            f">>"
        ).encode("latin-1")
        page_id = len(self._objects) + 1
        self._objects.append(page_obj_body)
        self._pages.append(page_id)
        self._current_content = None

    # ── Drawing primitives ────────────────────────────────────────────────────

    @staticmethod
    def _ascii_safe(text: str) -> str:
        """Replace non-latin-1 chars and PDF-unsafe chars with safe equivalents."""
        # Escape parentheses and backslash (required in PDF string literals)
        out = []
        for ch in text:
            if ord(ch) > 255:
                out.append("?")
            elif ch == "(":
                out.append("\\(")
            elif ch == ")":
                out.append("\\)")
            elif ch == "\\":
                out.append("\\\\")
            else:
                out.append(ch)
        return "".join(out)

    def draw_text(
        self,
        x: float,
        y: float,
        text: str,
        *,
        size: int = 11,
        bold: bool = False,
    ) -> None:
        """
        Draw a single line of text at (x, y) in PDF user-space coordinates.

        (0, 0) is the bottom-left corner.  PAGE_HEIGHT - margin gives the top.
        """
        if self._current_content is None:
            raise RuntimeError("Call add_page() before draw_text().")
        font = "/F2" if bold else "/F1"
        safe_text = self._ascii_safe(text)
        self._current_content.append(
            f"BT {font} {size} Tf {x:.2f} {y:.2f} Td ({safe_text}) Tj ET"
        )

    def draw_line(self, x1: float, y1: float, x2: float, y2: float) -> None:
        """Draw a horizontal/vertical rule."""
        if self._current_content is None:
            raise RuntimeError("Call add_page() before draw_line().")
        self._current_content.append(
            f"{x1:.2f} {y1:.2f} m {x2:.2f} {y2:.2f} l S"
        )

    # ── Render ────────────────────────────────────────────────────────────────

    def render(self) -> bytes:
        """
        Flush the last page and return the complete PDF as bytes.

        The returned bytes can be written directly to a ``.pdf`` file.
        """
        if self._current_content is not None:
            self._flush_page()
        if not self._pages:
            # Ensure at least one (empty) page exists
            self.add_page()
            self._flush_page()

        # Patch placeholder object 2: Pages dictionary
        kids = " ".join(f"{pid} 0 R" for pid in self._pages)
        pages_obj = (
            f"<< /Type /Pages\n   /Kids [{kids}]\n   /Count {len(self._pages)}\n>>"
        ).encode("latin-1")
        self._objects[1] = pages_obj  # index 1 → object 2

        # Patch placeholder object 1: Catalog
        catalog_obj = b"<< /Type /Catalog\n   /Pages 2 0 R\n>>"
        self._objects[0] = catalog_obj  # index 0 → object 1

        # Serialise
        parts: List[bytes] = [b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n"]
        offsets: List[int] = []

        for i, body in enumerate(self._objects):
            obj_num = i + 1
            offsets.append(len(b"".join(parts)))
            obj_bytes = (
                f"{obj_num} 0 obj\n".encode("latin-1")
                + body
                + b"\nendobj\n"
            )
            parts.append(obj_bytes)

        # Cross-reference table
        xref_offset = len(b"".join(parts))
        n = len(self._objects)
        xref_lines = [f"xref\n0 {n + 1}\n".encode("latin-1")]
        xref_lines.append(b"0000000000 65535 f \n")
        for off in offsets:
            xref_lines.append(f"{off:010d} 00000 n \n".encode("latin-1"))

        parts.extend(xref_lines)
        parts.append(
            (
                f"trailer\n<< /Size {n + 1}\n   /Root 1 0 R\n>>\n"
                f"startxref\n{xref_offset}\n%%EOF\n"
            ).encode("latin-1")
        )

        return b"".join(parts)


def _wrap_text(text: str, max_chars: int = 90) -> List[str]:
    """Word-wrap *text* to lines of at most *max_chars* characters."""
    words = text.split()
    lines: List[str] = []
    current = ""
    for word in words:
        if len(current) + len(word) + (1 if current else 0) <= max_chars:
            current = current + (" " if current else "") + word
        else:
            if current:
                lines.append(current)
            # Handle words longer than max_chars
            while len(word) > max_chars:
                lines.append(word[:max_chars])
                word = word[max_chars:]
            current = word
    if current:
        lines.append(current)
    return lines or [""]


def export_pdf_report(
    run_dir: str,
    *,
    output_filename: str = "report.pdf",
) -> str:
    """
    Generate a PDF report from all available run artifacts.

    Attempts to use ``fpdf2`` (``pip install fpdf2``) for richer output.
    Falls back to a pure-Python stdlib PDF writer (``_SimplePDFWriter``)
    when ``fpdf2`` is not installed.

    Key data included:
        - Project name, score, risk level
        - Consensus text
        - Security scan summary (pass/fail, high-severity count)
        - Validation verdict
        - Dependency audit summary
        - Auto-remediation summary

    Args:
        run_dir:          Path to a completed run output directory.
        output_filename:  Output file name (default: ``report.pdf``).

    Returns:
        Absolute path to the generated PDF file.

    See also:
        ``export_html_report`` — generates a richer HTML version of the same data.
    """
    analysis = _load_json(os.path.join(run_dir, "analysis_result.json"))
    meta = _load_json(os.path.join(run_dir, "run_meta.json"))
    security = _load_json(os.path.join(run_dir, "security_report.json"))
    validation = _load_json(os.path.join(run_dir, "independent_validation_report.json"))
    dep_audit = _load_json(os.path.join(run_dir, "dependency_audit_report.json"))
    remediation = _load_json(os.path.join(run_dir, "auto_remediation_report.json"))

    output_path = os.path.join(run_dir, output_filename)

    # ── Try fpdf2 first ───────────────────────────────────────────────────────
    try:
        from fpdf import FPDF

        class _ReportPDF(FPDF):
            def header(self) -> None:
                self.set_font("Helvetica", "B", 14)
                self.cell(0, 10, "Crucible Analysis Report", align="C", new_x="LMARGIN", new_y="NEXT")
                self.ln(2)

            def footer(self) -> None:
                self.set_y(-15)
                self.set_font("Helvetica", "", 8)
                self.cell(0, 10, f"Page {self.page_no()}", align="C")

        pdf = _ReportPDF()
        pdf.set_auto_page_break(auto=True, margin=15)
        pdf.add_page()

        def _section(title: str) -> None:
            pdf.set_font("Helvetica", "B", 12)
            pdf.ln(3)
            pdf.cell(0, 8, title, new_x="LMARGIN", new_y="NEXT")
            pdf.set_draw_color(100, 100, 100)
            pdf.line(10, pdf.get_y(), 200, pdf.get_y())
            pdf.ln(1)
            pdf.set_font("Helvetica", "", 10)

        def _field(label: str, value: Any) -> None:
            pdf.set_x(pdf.l_margin)  # reset after any previous multi_cell (new_x="RIGHT" default)
            pdf.set_font("Helvetica", "B", 10)
            pdf.cell(45, 6, f"{label}:", new_x="RIGHT", new_y="TOP")
            pdf.set_font("Helvetica", "", 10)
            pdf.multi_cell(0, 6, str(value or "N/A"), new_x="LMARGIN", new_y="NEXT")

        def _body(text: str, max_chars: int = 90) -> None:
            pdf.set_font("Helvetica", "", 10)
            for line in _wrap_text(str(text or ""), max_chars):
                pdf.cell(0, 5, line, new_x="LMARGIN", new_y="NEXT")

        # Overview
        project = str(analysis.get("project_name") or meta.get("project_name") or "Unknown")
        score = analysis.get("score")
        risk = str(analysis.get("risk_level") or "unknown")
        mode = str(meta.get("mode") or analysis.get("mode_used") or "N/A")
        provider = str(meta.get("llm_provider") or "N/A")
        timestamp = str(meta.get("timestamp") or "N/A")

        _section("Overview")
        _field("Project", project)
        _field("Score", f"{score}/100" if score is not None else "N/A")
        _field("Risk Level", risk.upper())
        _field("Mode", mode)
        _field("Provider", provider)
        _field("Timestamp", timestamp)

        # Consensus
        consensus = str(analysis.get("consensus") or "")
        if consensus:
            _section("Consensus")
            _body(consensus[:1200])

        # Security
        if security:
            passed = bool(security.get("passed", True))
            try:
                high_count = int(float(security.get("high_severity_count") or 0))
            except (ValueError, TypeError):
                high_count = 0
            scanner = str(security.get("scanner_used") or "unknown")
            _section("Security Scan")
            _field("Result", "PASS" if passed else "FAIL")
            _field("Scanner", scanner)
            _field("HIGH Severity Issues", str(high_count))

        # Validation
        if validation:
            verdict = str(validation.get("overall_verdict", "unknown")).upper()
            _section("Independent Validation")
            _field("Verdict", verdict)

        # Dependency audit
        if dep_audit:
            dep_passed = bool(dep_audit.get("passed", True))
            try:
                vuln_count = int(float(dep_audit.get("vulnerability_count") or 0))
            except (ValueError, TypeError):
                vuln_count = 0
            _section("Dependency Audit")
            _field("Result", "PASS" if dep_passed else "FAIL")
            _field("Vulnerabilities", str(vuln_count))

        # Remediation
        if remediation:
            rounds = remediation.get("rounds_executed", 0)
            applied = remediation.get("total_patches_applied", 0)
            attempted = remediation.get("total_patches_attempted", 0)
            _section("Auto-Remediation")
            _field("Rounds", str(rounds))
            _field("Patches Applied", f"{applied}/{attempted}")

        _tmp_fpdf = output_path + ".tmp"
        try:
            pdf.output(_tmp_fpdf)
            os.replace(_tmp_fpdf, output_path)
        except OSError as exc:
            try:
                os.unlink(_tmp_fpdf)
            except OSError:
                pass
            raise OSError(
                f"export_pdf_report: could not write '{output_path}': {exc}"
            ) from exc
        return output_path

    except ImportError:
        pass  # fall through to stdlib implementation

    # ── Stdlib fallback: _SimplePDFWriter ─────────────────────────────────────
    writer = _SimplePDFWriter()

    MARGIN_X: float = 50.0
    TOP_Y: float = 800.0
    LINE_H: float = 16.0
    SECTION_EXTRA: float = 8.0

    class _Cursor:
        def __init__(self) -> None:
            self.y: float = TOP_Y

        def newline(self, n: float = LINE_H) -> None:
            self.y -= n

        def section_gap(self) -> None:
            self.y -= SECTION_EXTRA

        def ensure_space(self, lines_needed: int = 1) -> None:
            if self.y < 60 + lines_needed * LINE_H:
                writer.add_page()
                self.y = TOP_Y

    def _emit_section(cur: _Cursor, title: str) -> None:
        cur.section_gap()
        cur.ensure_space(2)
        writer.draw_text(MARGIN_X, cur.y, title, size=13, bold=True)
        cur.newline(14)
        writer.draw_line(MARGIN_X, cur.y + 2, _SimplePDFWriter.PAGE_WIDTH - MARGIN_X, cur.y + 2)
        cur.newline(6)

    def _emit_field(cur: _Cursor, label: str, value: Any) -> None:
        cur.ensure_space(1)
        line = f"{label}: {value or 'N/A'}"
        for wrapped_line in _wrap_text(line, max_chars=85):
            cur.ensure_space(1)
            writer.draw_text(MARGIN_X, cur.y, wrapped_line, size=10)
            cur.newline()

    def _emit_body(cur: _Cursor, text: str, max_chars: int = 85) -> None:
        for wrapped_line in _wrap_text(str(text or ""), max_chars):
            cur.ensure_space(1)
            writer.draw_text(MARGIN_X, cur.y, wrapped_line, size=10)
            cur.newline()

    writer.add_page()
    cur = _Cursor()

    # Title
    project = str(analysis.get("project_name") or meta.get("project_name") or "Unknown")
    writer.draw_text(MARGIN_X, cur.y, f"Crucible Analysis Report — {project}", size=16, bold=True)
    cur.newline(20)
    writer.draw_line(MARGIN_X, cur.y, _SimplePDFWriter.PAGE_WIDTH - MARGIN_X, cur.y)
    cur.newline(10)

    # Overview
    _emit_section(cur, "Overview")
    score = analysis.get("score")
    risk = str(analysis.get("risk_level") or "unknown")
    mode = str(meta.get("mode") or analysis.get("mode_used") or "N/A")
    provider_label = str(meta.get("llm_provider") or "N/A")
    timestamp = str(meta.get("timestamp") or "N/A")
    _emit_field(cur, "Project", project)
    _emit_field(cur, "Score", f"{score}/100" if score is not None else "N/A")
    _emit_field(cur, "Risk Level", risk.upper())
    _emit_field(cur, "Mode", mode)
    _emit_field(cur, "Provider", provider_label)
    _emit_field(cur, "Timestamp", timestamp)

    # Consensus
    consensus = str(analysis.get("consensus") or "")
    if consensus:
        _emit_section(cur, "Consensus")
        _emit_body(cur, consensus[:1200])

    # Security
    if security:
        passed = bool(security.get("passed", True))
        try:
            high_count = int(float(security.get("high_severity_count") or 0))
        except (ValueError, TypeError):
            high_count = 0
        scanner = str(security.get("scanner_used") or "unknown")
        _emit_section(cur, "Security Scan")
        _emit_field(cur, "Result", "PASS" if passed else "FAIL")
        _emit_field(cur, "Scanner", scanner)
        _emit_field(cur, "HIGH Severity Issues", str(high_count))

    # Validation
    if validation:
        verdict = str(validation.get("overall_verdict", "unknown")).upper()
        _emit_section(cur, "Independent Validation")
        _emit_field(cur, "Verdict", verdict)

    # Dependency audit
    if dep_audit:
        dep_passed = bool(dep_audit.get("passed", True))
        try:
            vuln_count = int(float(dep_audit.get("vulnerability_count") or 0))
        except (ValueError, TypeError):
            vuln_count = 0
        _emit_section(cur, "Dependency Audit")
        _emit_field(cur, "Result", "PASS" if dep_passed else "FAIL")
        _emit_field(cur, "Vulnerabilities", str(vuln_count))

    # Remediation
    if remediation:
        rounds = remediation.get("rounds_executed", 0)
        applied = remediation.get("total_patches_applied", 0)
        attempted = remediation.get("total_patches_attempted", 0)
        _emit_section(cur, "Auto-Remediation")
        _emit_field(cur, "Rounds", str(rounds))
        _emit_field(cur, "Patches Applied", f"{applied}/{attempted}")

    # Footer
    cur.section_gap()
    cur.ensure_space(1)
    writer.draw_line(MARGIN_X, cur.y, _SimplePDFWriter.PAGE_WIDTH - MARGIN_X, cur.y)
    cur.newline(6)
    writer.draw_text(MARGIN_X, cur.y, "Generated by Crucible", size=8)

    pdf_bytes = writer.render()
    _tmp_pdf = output_path + ".tmp"
    try:
        with open(_tmp_pdf, "wb") as fh:
            fh.write(pdf_bytes)
        os.replace(_tmp_pdf, output_path)
    except OSError as exc:
        try:
            os.unlink(_tmp_pdf)
        except OSError:
            pass
        raise OSError(f"export_pdf_report: could not write '{output_path}': {exc}") from exc

    return output_path
