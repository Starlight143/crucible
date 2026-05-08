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
