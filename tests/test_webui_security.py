"""
WebUI security regression tests for the v1.1.0 hardening.

v1.1.0 third-pass: the original v1.1.0 audit added four HIGH-severity
WebUI hardening fixes (MAX_CONTENT_LENGTH cap, X-Requested-With
enforcement, SSRF guard, SRI on the Chart.js CDN link), but NONE of
them were covered by tests.  A future "convenience" refactor that
drops one of these would break security silently.  These tests pin
each behaviour.
"""
from __future__ import annotations

import importlib
import os
import sys
from pathlib import Path

import pytest

# Resolve project root so we can import webui.app even when pytest is
# launched from an arbitrary cwd.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


@pytest.fixture
def client():
    """Flask test client.  Imports lazily so tests in other modules
    that need a different env do not collide."""
    from webui import app as webui_app
    importlib.reload(webui_app)
    webui_app.app.config["TESTING"] = True
    return webui_app.app.test_client()


# ─── 1. MAX_CONTENT_LENGTH (1 MB cap, 413 JSON response) ─────────────────────

def test_oversize_body_returns_413_json(client):
    """A POST with body > 1 MB must hit the MAX_CONTENT_LENGTH guard
    and return a JSON 413 (not Flask's default HTML 413).
    """
    payload = b"x" * (2 * 1024 * 1024)  # 2 MB
    resp = client.post(
        "/api/run",
        data=payload,
        content_type="application/json",
        headers={"X-Requested-With": "XMLHttpRequest"},
    )
    assert resp.status_code == 413
    # Must be JSON, not HTML
    assert resp.is_json, (
        f"413 response must be JSON, got Content-Type "
        f"{resp.headers.get('Content-Type')!r}"
    )
    body = resp.get_json()
    assert "error" in body
    assert "limit_bytes" in body


# ─── 2. X-Requested-With (CSRF guard) ────────────────────────────────────────

def test_cross_origin_post_without_xhr_header_returns_403(client):
    """Cross-origin POST (carries Origin header) without
    X-Requested-With must be rejected with 403.

    v1.1.0 fourth-pass: also assert the response body shape so a
    future WAF middleware that 403s for unrelated reasons does NOT
    silently pass this test.
    """
    resp = client.post(
        "/api/run",
        json={"mode": "Quant", "user_problem": "x"},
        headers={"Origin": "https://attacker.example"},
    )
    assert resp.status_code == 403, (
        f"expected 403 (CSRF guard), got {resp.status_code}"
    )
    assert resp.is_json, "CSRF gate must return JSON body"
    body = resp.get_json()
    assert "X-Requested-With" in body.get("error", ""), (
        f"403 body does not mention X-Requested-With: {body!r}"
    )


def test_same_origin_post_without_origin_passes_csrf(client):
    """Server-to-server POST (no Origin header, no Referer) must NOT
    trigger the CSRF guard — that would block legitimate scheduler /
    curl / Flask test client callers.
    """
    resp = client.post(
        "/api/run",
        json={"mode": "Quant", "user_problem": "x"},
    )
    # Even if the actual handler 400s (missing fields), the request
    # passes the CSRF gate (the response is not 403 from the gate).
    # We accept 400/422 from the handler but never 403 from the gate.
    assert resp.status_code != 403, (
        f"server-to-server POST blocked by CSRF gate (status {resp.status_code})"
    )


def test_origin_null_post_without_xhr_returns_403(client):
    """``Origin: null`` (sandboxed iframe / opaque origin) is
    untrusted: must require X-Requested-With."""
    resp = client.post(
        "/api/run",
        json={"mode": "Quant", "user_problem": "x"},
        headers={"Origin": "null"},
    )
    assert resp.status_code == 403, (
        f"expected 403 for Origin: null, got {resp.status_code}"
    )


def test_referer_matches_forwarded_host_passes_csrf(client):
    """v1.1.0 fourth-pass (F-3): when ``X-Forwarded-Host`` matches
    the Referer host, the same-origin check must accept it as
    same-origin so reverse-proxy deployments don't 403 every
    legitimate non-XHR POST.

    Note: this exercises the X-Forwarded-Host fallback inside the
    gate even when ProxyFix is NOT wired up (the env var path).
    """
    resp = client.post(
        "/api/run",
        json={"mode": "Quant", "user_problem": "x"},
        headers={
            "Referer": "https://crucible.example.com/path",
            "X-Forwarded-Host": "crucible.example.com",
        },
    )
    # NOT 403 from the CSRF gate (route may 400/422 for missing fields).
    assert resp.status_code != 403, (
        f"forwarded-host same-origin was 403'd: {resp.status_code}"
    )


def test_malformed_referer_fails_closed(client):
    """v1.1.0 fourth-pass (F-3): malformed Referer must fail CLOSED
    (require X-Requested-With), not silently pass.
    """
    resp = client.post(
        "/api/run",
        json={"mode": "Quant", "user_problem": "x"},
        headers={"Referer": "javascript:alert(1)"},
    )
    # Without X-Requested-With and with non-null referer parse path,
    # the malformed parse must hit the fail-closed branch.
    assert resp.status_code == 403, (
        f"malformed Referer didn't fail closed: status={resp.status_code}"
    )


# ─── 3. SSRF (_is_safe_url) ─────────────────────────────────────────────────

def test_is_safe_url_rejects_private_addresses():
    """Direct unit test of the SSRF guard.

    A regression that allows ``192.168.x.x`` or ``127.0.0.1`` through
    the guard would re-open the SSRF attack surface.  This pins the
    contract independent of the calling route.
    """
    from webui.app import _is_safe_url

    for url in [
        "http://127.0.0.1/admin",
        "http://localhost/admin",
        "http://192.168.1.1/admin",
        "http://10.0.0.1/admin",
        "http://169.254.169.254/latest/meta-data/",  # AWS metadata
        "http://[::1]/x",
        "ftp://example.com",  # non-HTTP scheme
        "javascript:alert(1)",
    ]:
        assert not _is_safe_url(url), (
            f"SSRF guard accepted {url!r} (should reject)"
        )


def test_is_safe_url_rejects_userinfo_smuggling():
    """v1.1.0 third-pass: ``http://victim@evil.com/`` returns
    hostname="evil.com" from urlparse but the userinfo travels in
    Authorization headers — must be rejected outright.
    """
    from webui.app import _is_safe_url

    for url in [
        "http://attacker.com@192.168.1.1/",  # username smuggling
        "http://user:pass@10.0.0.1/",        # full userinfo
    ]:
        assert not _is_safe_url(url), (
            f"SSRF guard accepted userinfo URL {url!r}"
        )


def test_is_safe_url_rejects_ipv6_scope_id():
    """IPv6 link-local with scope-id syntax must be rejected."""
    from webui.app import _is_safe_url
    assert not _is_safe_url("http://[fe80::1%eth0]/")


def test_is_safe_url_rejects_ipv6_sparse_embedded_v4():
    """v1.1.0 fourth-pass (F-2): IPv6 forms that embed a private IPv4
    address must be rejected even though Python's
    ``ipaddress.ip_address.is_global`` reports True at the IPv6 layer.

    Covers four embedding patterns:
      1. ``::ffff:w.x.y.z`` (IPv4-mapped) — already covered by T16.
      2. ``::w.x.y.z`` (IPv4-compatible, RFC 4291 §2.5.5.1).
      3. ``2002:wxyz:abcd::`` (6to4, RFC 3056).
      4. ``64:ff9b::w.x.y.z`` (NAT64 well-known, RFC 6052).

    All four can be used to bypass naive SSRF guards that only check
    ``addr.is_global`` without unwrapping the embedded IPv4.
    """
    from webui.app import _is_safe_url

    sparse_embed_attacks = [
        "http://[::a00:1]/",                  # ::10.0.0.1 → private
        "http://[::c0a8:101]/",               # ::192.168.1.1 → private
        "http://[::7f00:1]/",                 # ::127.0.0.1 → loopback
        "http://[2002:a00:1::]/",             # 6to4 of 10.0.0.1 → private
        "http://[2002:c0a8:101::]/",          # 6to4 of 192.168.1.1 → private
        "http://[64:ff9b::a00:1]/",           # NAT64 of 10.0.0.1 → private
        "http://[64:ff9b::7f00:1]/",          # NAT64 of 127.0.0.1 → loopback
        "http://[::ffff:10.0.0.1]/",          # IPv4-mapped (redundant T16 coverage)
    ]
    for url in sparse_embed_attacks:
        assert not _is_safe_url(url), (
            f"SSRF guard accepted IPv6-embedded private v4: {url}"
        )


def test_is_safe_url_accepts_public_https():
    """Sanity: real public URLs must still pass the guard."""
    from webui.app import _is_safe_url

    # We don't actually network here — _is_safe_url resolves DNS.
    # ``example.com`` resolves to a public address; skip if offline.
    import socket
    try:
        socket.getaddrinfo("example.com", None)
    except socket.gaierror:
        pytest.skip("DNS unavailable; cannot validate public URL")

    assert _is_safe_url("https://example.com/")


# ─── 4. Chart.js SRI hash ────────────────────────────────────────────────────

def test_chartjs_cdn_tag_has_sri_integrity():
    """Chart.js loaded from a CDN must carry an SRI ``integrity``
    attribute so a compromised CDN cannot inject malicious JS.

    Parses the index.html template directly.  If anyone removes the
    integrity / crossorigin attribute as part of a refactor, this
    test fails.
    """
    import re as _re

    template_path = (
        _REPO_ROOT / "webui" / "templates" / "index.html"
    )
    html = template_path.read_text(encoding="utf-8")
    # The Chart.js tag spans multiple lines in our template, so use a
    # multiline regex that captures the whole ``<script ... src="...chart..."
    # ... ></script>`` block.
    pattern = _re.compile(
        r"<script\b[^>]*src=\"[^\"]*chart[^\"]*\"[^>]*>",
        _re.IGNORECASE | _re.DOTALL,
    )
    matches = pattern.findall(html)
    assert matches, "no chart.js script tag found in index.html"
    for tag in matches:
        if _re.search(r"cdn|jsdelivr|unpkg", tag, _re.IGNORECASE):
            assert 'integrity="sha' in tag, (
                f"Chart.js CDN tag missing SRI integrity: {tag!r}"
            )
            assert "crossorigin" in tag, (
                f"Chart.js CDN tag missing crossorigin: {tag!r}"
            )
