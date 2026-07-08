"""
v1.2.8: liveness probe endpoint.

The WebUI gained a ``GET /health`` endpoint so uptime monitors / k8s /
load balancers can check the Flask process is alive.  Two contracts are
pinned here:

1. It returns ``200 {"status": "ok"}`` as JSON.
2. It is NOT behind the CSRF state-change gate (``_enforce_xhr_header_on_
   state_changes``) and needs no auth — a bare cross-origin GET still
   succeeds.  This matters because a future WebUI auth layer (the gated
   "permission isolation" work) must keep exempting the probe; if it
   accidentally gates ``/health``, this test goes red.
"""
from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


@pytest.fixture
def client():
    from webui import app as webui_app

    importlib.reload(webui_app)
    webui_app.app.config["TESTING"] = True
    try:
        yield webui_app.app.test_client()
    finally:
        _t = getattr(webui_app, "_eviction_timer", None)
        if _t is not None:
            try:
                _t.cancel()
            except Exception:
                pass


def test_health_returns_ok_json(client):
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.is_json
    assert resp.get_json() == {"status": "ok"}


def test_health_not_behind_csrf_or_auth(client):
    # Simulate a cross-origin browser GET (Origin set).  POST /api/* with
    # this header would be blocked by the CSRF gate; the liveness probe
    # must pass through unconditionally so monitors can reach it.
    resp = client.get("/health", headers={"Origin": "https://evil.example"})
    assert resp.status_code == 200
    assert resp.get_json() == {"status": "ok"}
