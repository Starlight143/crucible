"""Frontend→backend integration regression tests for the v1.0.3 asset split.

These tests guard against the failure mode that broke ``app.js`` immediately
after the inline-script extraction in v1.0.3: a Jinja expression
(``{{ webui_url | tojson }}``) survived the cut and became a JavaScript
syntax error in the static asset, silently disabling the entire SPA —
including the agent-flow visualization, SSE streaming, and every inline
``onclick`` handler.

What this file pins for the future
----------------------------------
1. ``index.html`` exports the ``webui_url`` Jinja variable as
   ``window.WEBUI_URL`` *before* the static ``app.js`` loads.  Without this
   bridge, ``app.js`` (which is no longer template-rendered) cannot read
   the operator-configured URL and the domain badge in the header falls
   back silently to ``window.location.host`` even when an operator has
   configured a public URL via ``WEBUI_URL``.
2. The served ``app.js`` contains zero Jinja artifacts (``{{`` /  ``{%``).
   Any leftover would be a JavaScript syntax error that breaks the entire
   bundle.
3. Every canonical agent-flow SSE event string is matched by at least one
   regex in the in-bundle ``evMap`` table (a missing match means the
   matching node never lights up in the agent-flow panel).
4. Every state handler key referenced from the SSE event mappings has a
   corresponding ``state === '...'`` branch in the live JS.
5. Every inline ``onclick="<symbol>(...)"`` attribute in ``index.html``
   resolves to a top-level function definition in ``app.js`` (otherwise
   the click silently no-ops with a ``ReferenceError`` in the console).
"""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


# ─────────────────────────────────────────────────────────────────────────────
# Fixtures
# ─────────────────────────────────────────────────────────────────────────────


@pytest.fixture(scope="module")
def flask_client():
    """Spin up the real WebUI app in test-client mode."""
    os.environ["WEBUI_URL"] = "https://example.test"
    from webui import app as webui_module

    return webui_module.app.test_client()


@pytest.fixture(scope="module")
def index_html(flask_client) -> str:
    response = flask_client.get("/")
    assert response.status_code == 200, f"GET / returned {response.status_code}"
    return response.data.decode("utf-8")


@pytest.fixture(scope="module")
def app_js(flask_client) -> str:
    response = flask_client.get("/static/js/app.js")
    assert response.status_code == 200, f"GET /static/js/app.js returned {response.status_code}"
    return response.data.decode("utf-8")


@pytest.fixture(scope="module")
def app_css(flask_client) -> str:
    response = flask_client.get("/static/css/app.css")
    assert response.status_code == 200
    return response.data.decode("utf-8")


# ─────────────────────────────────────────────────────────────────────────────
# 1. WEBUI_URL bridge
# ─────────────────────────────────────────────────────────────────────────────


def test_index_html_exports_webui_url_bridge(index_html: str) -> None:
    """The inline ``window.WEBUI_URL = ...;`` script must precede ``app.js``.

    Without this bridge, ``app.js`` reads ``window.WEBUI_URL`` as
    ``undefined`` and silently falls back to ``window.location.host``.
    Operators who set ``WEBUI_URL=https://my-public-host`` would not see
    the value reflected in the domain badge — a regression that would be
    invisible in CI but immediately visible in production.
    """
    bridge_re = re.compile(
        r"<script>\s*window\.WEBUI_URL\s*=\s*[^<]+;\s*</script>",
        re.IGNORECASE,
    )
    assert bridge_re.search(index_html), (
        "Inline `<script>window.WEBUI_URL = ...;</script>` bridge missing "
        "from index.html — webui_url Jinja variable will not reach app.js."
    )

    # The bridge must come before the app.js script tag.
    bridge_idx = bridge_re.search(index_html).start()
    appjs_idx = index_html.find("/static/js/app.js")
    assert appjs_idx > 0, "app.js script tag missing"
    assert bridge_idx < appjs_idx, (
        "WEBUI_URL bridge must appear BEFORE app.js — otherwise the global "
        "is undefined when the bundle reads it."
    )


def test_app_js_consumes_webui_url_global(app_js: str) -> None:
    """``app.js`` must read ``window.WEBUI_URL`` (not the literal Jinja)."""
    assert "window.WEBUI_URL" in app_js, (
        "app.js does not reference window.WEBUI_URL — the bridge value is "
        "ignored and the SPA cannot pick up the operator-configured URL."
    )


# ─────────────────────────────────────────────────────────────────────────────
# 2. No Jinja artifacts in static assets
# ─────────────────────────────────────────────────────────────────────────────


def test_app_js_has_no_jinja_artifacts(app_js: str) -> None:
    """``app.js`` is served as a static file — Jinja syntax becomes a syntax error."""
    forbidden = ("{{", "{%", "url_for(")
    for token in forbidden:
        assert token not in app_js, (
            f"Found Jinja artifact {token!r} in app.js — the static asset "
            "is not template-rendered, so this would be a JS syntax error "
            "and the entire SPA bundle would fail to load."
        )


def test_app_css_has_no_jinja_artifacts(app_css: str) -> None:
    """``app.css`` symmetry: also a static asset."""
    forbidden = ("{{", "{%")
    for token in forbidden:
        assert token not in app_css, f"Found Jinja artifact {token!r} in app.css"


# ─────────────────────────────────────────────────────────────────────────────
# 3. Agent-flow event mapping coverage
# ─────────────────────────────────────────────────────────────────────────────


# Each entry is the canonical event substring the backend emits.  At least
# one regex in the in-bundle ``evMap`` must match each one — otherwise the
# corresponding agent-flow node never transitions and the visual panel
# appears frozen during real runs.
_CANONICAL_SSE_EVENTS: tuple[str, ...] = (
    "direction_seed_kickoff_start",
    "direction_seed_kickoff_done",
    "librarian_kickoff_start",
    "research_lane_done market_research",
    "research_lane_done technical_research",
    "research_lane_done research_synthesizer",
    "librarian_kickoff_done",
    "event=gate_controller.start",
    "event=self_check.start",
    "direction_debate_kickoff_start",
    "direction_debate_kickoff_done",
    "analysis_kickoff_start",
    "analysis_kickoff_done",
    "codegen_kickoff_start",
    "codegen_kickoff_done",
    "direction_feedback_start",
    "direction_feedback_failed",
    # v1.0.5 audit: ensure project_fix and crash-failure events emitted by
    # backend sections 02/05/07 are wired into the agent-flow visualisation.
    # project_fix_kickoff_* covers the quality-loop re-codegen phase that
    # was previously silent on the frontend for 30-60s × N rounds — the
    # exact area that v1.0.5 round 2/3 added structured failure_type to.
    "project_fix_kickoff_start",
    "project_fix_kickoff_done",
    "project_fix_kickoff_failed",
    "librarian_kickoff_failed",
    "analysis_kickoff_failed",
    "codegen_kickoff_failed",
)


def _extract_evmap_regexes(app_js: str) -> list[tuple[str, str]]:
    """Pull ``[/<pattern>/<flags>, ...]`` rows out of the JS evMap literal."""
    block = re.search(r"const evMap = \[(.+?)\];", app_js, re.DOTALL)
    assert block is not None, "evMap declaration not found in app.js"
    return re.findall(r"\[/(.+?)/(\w*),", block.group(1))


def test_evmap_has_at_least_one_entry_per_phase(app_js: str) -> None:
    """Sanity floor: at least 20 mappings (we ship ~28)."""
    rows = _extract_evmap_regexes(app_js)
    assert len(rows) >= 20, (
        f"evMap shrank to {len(rows)} entries — agent-flow coverage is "
        "almost certainly missing.  A typical bundle has ~28."
    )


@pytest.mark.parametrize("event", _CANONICAL_SSE_EVENTS)
def test_evmap_matches_canonical_sse_event(app_js: str, event: str) -> None:
    """Each canonical SSE event substring matches at least one evMap regex."""
    rows = _extract_evmap_regexes(app_js)
    for pattern, flags in rows:
        try:
            cre = re.compile(pattern, re.IGNORECASE if "i" in flags else 0)
        except re.error:
            continue
        if cre.search(event):
            return
    pytest.fail(
        f"No evMap regex matches SSE event {event!r}.  The matching "
        "agent-flow node will not transition during real runs — the user "
        "will see a stuck/frozen graph for that phase."
    )


_STATE_HANDLER_KEYS: tuple[str, ...] = (
    "codegen_phase_done",
    "research_phase_done",
    "librarian_phase_start",
    "analysis_phase_done",
    # v1.0.5: new state handler for the analysis-crew crash path.
    "analysis_phase_error",
)


@pytest.mark.parametrize("state_key", _STATE_HANDLER_KEYS)
def test_state_handler_branch_present(app_js: str, state_key: str) -> None:
    """``evMap`` references these state keys; the JS must have a handler branch."""
    needle = f"state === '{state_key}'"
    assert needle in app_js, (
        f"Missing state-handler branch {needle!r} — the evMap maps "
        f"events to {state_key!r} but no code path acts on it."
    )


# ─────────────────────────────────────────────────────────────────────────────
# 4. Inline onclick handlers resolve to a function definition
# ─────────────────────────────────────────────────────────────────────────────


def _onclick_symbols(html: str) -> set[str]:
    return set(re.findall(r'onclick="([a-zA-Z_$][\w$]*)\(', html))


def test_every_inline_onclick_resolves_to_a_function(index_html: str, app_js: str) -> None:
    """Every ``onclick="foo()"`` symbol must be defined at the top level of app.js.

    Otherwise the click silently no-ops with a ``ReferenceError`` in the
    browser console — invisible in CI, immediately visible to the user.
    """
    symbols = _onclick_symbols(index_html)
    assert symbols, "expected at least one onclick handler in index.html"
    missing: list[str] = []
    for sym in sorted(symbols):
        # Match any of: ``function foo``, ``foo = function/async``,
        # ``async function foo``, ``window.foo =``.
        defined_re = re.compile(
            rf"(^function {sym}\b)"
            rf"|(^async function {sym}\b)"
            rf"|(^[ \t]*{sym}\s*=\s*(?:function|async))"
            rf"|(^[ \t]*window\.{sym}\s*=)",
            re.MULTILINE,
        )
        if not defined_re.search(app_js):
            missing.append(sym)
    assert not missing, (
        f"Inline onclick handlers reference undefined symbols: {missing}.  "
        "Browser console will show ReferenceError on click."
    )


# ─────────────────────────────────────────────────────────────────────────────
# 5. SSE wiring sanity
# ─────────────────────────────────────────────────────────────────────────────


def test_sse_eventsource_wiring_intact(app_js: str) -> None:
    """The SSE EventSource construction + onmessage handler must remain in app.js."""
    # The exact URL template uses backticks; just look for its meaningful tail.
    assert "new EventSource(" in app_js, "EventSource construction missing"
    assert "/api/run/" in app_js, "Run-status API path missing"
    assert "/stream?from=" in app_js, "SSE stream path missing"
    assert ".onmessage" in app_js, "EventSource.onmessage handler missing"


# ─────────────────────────────────────────────────────────────────────────────
# 6. v1.0.5 — Frontend ↔ backend alignment for the structured quality-loop
#    outcome.  Backend section_07 now writes ``review_report.failure_type``
#    as a strictly-validated enum and promotes ``quality_passed`` /
#    ``quality_loop_failure_type`` to the top level of ``run_meta.json``.
#    The substring fallback (``"QUALITY_LOOP_GAVE_UP" in summary``) was
#    removed in round 3.  These tests pin the frontend to the same contract
#    so a future refactor cannot silently re-introduce the substring path
#    or stop reading the structured field.
# ─────────────────────────────────────────────────────────────────────────────


def test_app_js_reads_structured_quality_passed_field(app_js: str) -> None:
    """``app.js`` must read ``meta.quality_passed`` and ``review.passes``.

    The dashboard runs table and the run-detail modal both render a
    quality-status badge; the only safe source of truth is the structured
    field, not heuristics over free-form summary text.
    """
    assert "quality_passed" in app_js, (
        "app.js does not reference quality_passed — the dashboard cannot "
        "render the quality-loop status badge from the structured field."
    )
    assert "review.passes" in app_js or "review.failure_type" in app_js, (
        "app.js does not read review.passes / review.failure_type — the "
        "run-detail modal cannot fall back to the per-run review report "
        "for older saved_projects/ entries."
    )


def test_app_js_reads_structured_quality_loop_failure_type(app_js: str) -> None:
    """``app.js`` must reference ``quality_loop_failure_type``.

    This is the canonical top-level field promoted by section_07 in
    v1.0.5 round 2.  The frontend-side string match documents the
    contract that a future renaming would need to preserve.
    """
    assert "quality_loop_failure_type" in app_js, (
        "app.js does not reference quality_loop_failure_type — the "
        "frontend will silently lose the structured failure signal."
    )


def test_app_js_does_not_substring_match_quality_loop_giveup(app_js: str) -> None:
    """The frontend must mirror backend section_07's removal of the
    substring fallback for ``QUALITY_LOOP_GAVE_UP``.  We forbid any code
    path that does ``summary.includes("QUALITY_LOOP_GAVE_UP")`` or
    ``indexOf("QUALITY_LOOP_GAVE_UP")`` on free-form text — the only
    legitimate consumers are the strict-equality checks against the
    structured ``failure_type`` enum value.
    """
    forbidden_patterns = [
        re.compile(r"summary[^.]*\.\s*(?:includes|indexOf)\s*\(\s*['\"]QUALITY_LOOP_GAVE_UP", re.IGNORECASE),
        re.compile(r"['\"]QUALITY_LOOP_GAVE_UP['\"]\s*\.\s*test\s*\(", re.IGNORECASE),
        re.compile(r"\.match\s*\(\s*/QUALITY_LOOP_GAVE_UP/", re.IGNORECASE),
    ]
    for pat in forbidden_patterns:
        m = pat.search(app_js)
        assert not m, (
            f"app.js contains a substring/regex match for "
            f"QUALITY_LOOP_GAVE_UP against free-form text "
            f"({m.group(0) if m else ''!r}).  Backend section_07 removed "
            "this fallback in v1.0.5 round 3 — the frontend must use the "
            "structured review_report.failure_type field instead."
        )


def test_app_js_quality_badge_helper_defined(app_js: str) -> None:
    """The ``_qualityBadgeHtml`` helper must exist and recognise the
    canonical enum value ``QUALITY_LOOP_GAVE_UP`` (uppercase, exact)."""
    assert "_qualityBadgeHtml" in app_js, (
        "_qualityBadgeHtml helper missing — runs table / run-detail modal "
        "have no way to render the quality-loop status badge."
    )
    assert "'QUALITY_LOOP_GAVE_UP'" in app_js or '"QUALITY_LOOP_GAVE_UP"' in app_js, (
        "QUALITY_LOOP_GAVE_UP literal missing from app.js — the badge "
        "helper cannot match the structured failure_type value."
    )


def test_app_js_renders_review_issues_section(app_js: str) -> None:
    """The run-detail modal must render the ``review.issues`` array with
    severity grouping (high/medium/low).  Without this, the operator can
    see ``Quality Status: ⚠ Gave up`` but has no in-UI way to inspect
    *why* it gave up — the JSON file is only accessible from the disk.
    """
    assert "review.issues" in app_js, (
        "app.js does not read review.issues — the run-detail modal will "
        "not render the actionable issue list emitted by the quality loop."
    )
    assert "review-issue-list" in app_js, (
        "app.js does not render the .review-issue-list container — the "
        "issues section is missing from the modal body."
    )


def test_app_css_quality_badge_classes_present(app_css: str) -> None:
    """The CSS must define the badge variants; missing classes silently
    render an unstyled span and look like a regression."""
    for cls in (
        ".quality-badge.passed",
        ".quality-badge.gaveup",
        ".quality-badge.failed",
        ".review-issue-severity-high",
        ".review-issue-severity-medium",
        ".review-issue-severity-low",
    ):
        assert cls in app_css, (
            f"app.css does not define {cls} — the badge / issue row "
            "renders without colour and the operator cannot tell the "
            "severity at a glance."
        )


def test_project_fix_start_maps_to_code_gen_active(app_js: str) -> None:
    """Backend section_07 emits ``project_fix_kickoff_start`` whenever the
    quality loop re-runs codegen with feedback after self_check finds
    issues.  The frontend evMap must map this event to the ``code_gen``
    node in ``active`` state — otherwise the visual graph goes silent for
    the entire quality loop (often 30-60s × N rounds), which is exactly
    the v1.0.5 round 2/3 work area.
    """
    block = re.search(r"const evMap = \[(.+?)\];", app_js, re.DOTALL)
    assert block is not None, "evMap declaration not found in app.js"
    evmap_text = block.group(1)
    # The mapping line must include all three pieces in the same row.
    pat = re.compile(
        r"\[/project_fix_kickoff_start[^/]*/[^,]*,\s*'code_gen'\s*,\s*'active'",
    )
    assert pat.search(evmap_text), (
        "project_fix_kickoff_start does not map to code_gen / active in "
        "evMap.  The quality-loop re-codegen phase will be invisible on "
        "the agent-flow panel."
    )


def test_project_fix_done_dispatches_codegen_phase_done(app_js: str) -> None:
    """``project_fix_kickoff_done`` must dispatch the existing
    ``codegen_phase_done`` state handler so code_gen closes cleanly and
    self_check re-activates for the next loop iteration.  Inventing a
    new state handler would silently bypass the deferred-done flash that
    ``codegen_phase_done`` already implements for stage-8 nodes.
    """
    block = re.search(r"const evMap = \[(.+?)\];", app_js, re.DOTALL)
    assert block is not None
    evmap_text = block.group(1)
    pat = re.compile(
        r"\[/project_fix_kickoff_done[^/]*/[^,]*,\s*null\s*,\s*'codegen_phase_done'",
    )
    assert pat.search(evmap_text), (
        "project_fix_kickoff_done does not dispatch codegen_phase_done — "
        "the quality-loop iteration boundary will not visibly hand off "
        "from code_gen back to self_check."
    )


def test_project_fix_failed_maps_to_code_gen_error(app_js: str) -> None:
    """A project_fix failure must mark code_gen as ``error`` so the
    operator sees the red dot exactly where the fix attempt crashed.
    """
    block = re.search(r"const evMap = \[(.+?)\];", app_js, re.DOTALL)
    assert block is not None
    evmap_text = block.group(1)
    pat = re.compile(
        r"\[/project_fix_kickoff_failed[^/]*/[^,]*,\s*'code_gen'\s*,\s*'error'",
    )
    assert pat.search(evmap_text), (
        "project_fix_kickoff_failed does not mark code_gen as error — "
        "a crashed fix attempt would leave code_gen stuck active."
    )


def test_analysis_kickoff_failed_dispatches_phase_error(app_js: str) -> None:
    """The analysis-crew crash path must error every stage-5/6/7 node so
    the failure is visible regardless of which sub-agent crashed."""
    block = re.search(r"const evMap = \[(.+?)\];", app_js, re.DOTALL)
    assert block is not None
    evmap_text = block.group(1)
    pat = re.compile(
        r"\[/analysis_kickoff_failed[^/]*/[^,]*,\s*null\s*,\s*'analysis_phase_error'",
    )
    assert pat.search(evmap_text), (
        "analysis_kickoff_failed does not dispatch analysis_phase_error — "
        "a crashed analysis crew would leave its nodes stuck active."
    )


def test_librarian_kickoff_failed_maps_to_librarian_error(app_js: str) -> None:
    """The librarian-crew crash path must mark the librarian node as
    error.  Without this, ``librarian_kickoff_done`` cannot fire either
    (since the crew never finished) and the node stays green-active."""
    block = re.search(r"const evMap = \[(.+?)\];", app_js, re.DOTALL)
    assert block is not None
    evmap_text = block.group(1)
    pat = re.compile(
        r"\[/librarian_kickoff_failed[^/]*/[^,]*,\s*'librarian'\s*,\s*'error'",
    )
    assert pat.search(evmap_text), (
        "librarian_kickoff_failed does not mark librarian as error."
    )


def test_app_js_does_not_use_int_quality_passed_truthiness(app_js: str) -> None:
    """The backend emits ``quality_passed`` as a JSON boolean (true / false
    / null) — never an int.  The frontend must check against the boolean
    explicitly so SQLite's int(0) result from older codepaths cannot
    silently render as falsy when surfaced by some other backend feeding
    in legacy data.  The badge helper does ``passed === true`` /
    ``passed === false``; we pin that strict-equality usage here.
    """
    assert "passed === true" in app_js, (
        "app.js does not strict-compare quality_passed against true — "
        "ambiguous truthiness on the wire (1 vs true) could silently "
        "render the wrong badge."
    )
    assert "passed === false" in app_js, (
        "app.js does not strict-compare quality_passed against false — "
        "the failed-run badge will not render correctly on legacy data."
    )
