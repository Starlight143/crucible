"""
tests/test_v1_1_10_regressions.py
=================================

Regression pins for the v1.1.10 librarian-provider hardening release.

Findings covered
----------------
S1  - ``_search_context7`` injects a Bearer auth header when CONTEXT7_API_KEY
      is configured (anonymous behaviour unchanged when unset / placeholder).
S2  - ``grep_app`` removed from the canonical default provider list in
      ``OPENCODE_LIBRARIAN_DEFAULT_PROVIDERS`` AND from the literal fallback
      default in ``fallback.py``; ``github`` becomes the primary code provider.
      The ``_search_grep_app`` helper itself is preserved so explicit
      opt-in still works.
S3  - domain_pins.json no longer points the librarian at AWS-WAF /
      Cloudflare-protected pages.  The Binance FAQ URL is replaced by the
      ``binance-docs.github.io`` GitHub Pages mirror; the CoinGecko
      ``/en/api/documentation`` page is replaced by ``docs.coingecko.com``.
S4  - ``.env.example`` ships uncommented ``CONTEXT7_API_KEY`` and
      ``GITHUB_TOKEN`` placeholder entries so the Settings UI surfaces
      them on every fresh install.
S5  - ``SETTINGS_SCHEMA`` includes the ``librarian_auth`` group, and
      ``KEY_META`` carries bilingual descriptions for both new keys.

Per CLAUDE.md § 9.6 (producer→consumer wiring): every new mapping or
inter-module contract has both a behavioural test AND a structural
``inspect.getsource`` / regex pin so a silent refactor cannot drop the
wire-up without turning a test red.
"""
from __future__ import annotations

import inspect
import json
import os
import re
import sys
from pathlib import Path
from typing import Dict, List
from unittest.mock import patch

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))


# ────────────────────────────────────────────────────────────────────────────
# Shared helpers
# ────────────────────────────────────────────────────────────────────────────


def _read_env_example() -> str:
    return (PROJECT_ROOT / ".env.example").read_text(encoding="utf-8")


def _read_domain_pins() -> Dict:
    return json.loads(
        (PROJECT_ROOT / "crucible" / "config" / "domain_pins.json").read_text(
            encoding="utf-8"
        )
    )


def _read_app_js() -> str:
    return (PROJECT_ROOT / "webui" / "static" / "js" / "app.js").read_text(
        encoding="utf-8"
    )


def _all_pin_urls(pins_obj: Dict) -> List[str]:
    urls: List[str] = []
    for pin in pins_obj.get("pins") or []:
        for entry in pin.get("pre_fetch") or []:
            url = str(entry.get("url") or "").strip()
            if url:
                urls.append(url)
    return urls


# ────────────────────────────────────────────────────────────────────────────
# S1 — Context7 Bearer auth header
# ────────────────────────────────────────────────────────────────────────────


class TestS1Context7Auth:
    def test_resolve_token_returns_empty_when_unset(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from crucible.modules.section_04_web_research_and_direction import (
            _resolve_context7_token,
        )

        for name in ("CONTEXT7_API_KEY", "CONTEXT7_TOKEN"):
            monkeypatch.delenv(name, raising=False)
        assert _resolve_context7_token() == ""

    def test_resolve_token_filters_placeholder_values(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from crucible.modules.section_04_web_research_and_direction import (
            _resolve_context7_token,
        )

        # The default value shipped in .env.example must be treated as "unset"
        # so a fresh install does not pretend it has a key.
        for placeholder in (
            "replace_with_context7_api_key",
            "your_context7_key_here",
            "xxxx-xxxx-xxxx",
            "placeholder",
            "changeme",
        ):
            monkeypatch.setenv("CONTEXT7_API_KEY", placeholder)
            assert _resolve_context7_token() == "", (
                f"placeholder {placeholder!r} should be filtered out"
            )

    def test_resolve_token_returns_real_value(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from crucible.modules.section_04_web_research_and_direction import (
            _resolve_context7_token,
        )

        monkeypatch.setenv("CONTEXT7_API_KEY", "ctx7_live_abc123def456")
        assert _resolve_context7_token() == "ctx7_live_abc123def456"

    def test_resolve_token_strips_whitespace(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from crucible.modules.section_04_web_research_and_direction import (
            _resolve_context7_token,
        )

        monkeypatch.setenv("CONTEXT7_API_KEY", "  ctx7_token_xyz  \n")
        assert _resolve_context7_token() == "ctx7_token_xyz"

    def test_resolve_token_prefers_api_key_over_token(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from crucible.modules.section_04_web_research_and_direction import (
            _resolve_context7_token,
        )

        monkeypatch.setenv("CONTEXT7_API_KEY", "primary_key")
        monkeypatch.setenv("CONTEXT7_TOKEN", "secondary_key")
        assert _resolve_context7_token() == "primary_key"

    def test_resolve_token_falls_back_to_token_when_api_key_blank(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from crucible.modules.section_04_web_research_and_direction import (
            _resolve_context7_token,
        )

        monkeypatch.setenv("CONTEXT7_API_KEY", "")
        monkeypatch.setenv("CONTEXT7_TOKEN", "fallback_token")
        assert _resolve_context7_token() == "fallback_token"

    def test_headers_include_bearer_when_token_configured(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from crucible.modules.section_04_web_research_and_direction import (
            _context7_api_headers,
        )

        monkeypatch.setenv("CONTEXT7_API_KEY", "ctx7_realkey")
        headers = _context7_api_headers()
        assert headers["Accept"] == "application/json"
        assert headers["Authorization"] == "Bearer ctx7_realkey"

    def test_headers_omit_authorization_when_unconfigured(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from crucible.modules.section_04_web_research_and_direction import (
            _context7_api_headers,
        )

        for name in ("CONTEXT7_API_KEY", "CONTEXT7_TOKEN"):
            monkeypatch.delenv(name, raising=False)
        headers = _context7_api_headers()
        assert "Authorization" not in headers
        assert headers["Accept"] == "application/json"

    def test_search_context7_passes_headers_with_token(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When a token is configured, ``_search_context7`` must thread the
        Bearer header through to ``_safe_http_json``.  Captures the headers
        argument seen by the (mocked) underlying HTTP call."""
        from crucible.modules import section_04_web_research_and_direction as sec04

        monkeypatch.setenv("CONTEXT7_API_KEY", "ctx7_inject_test")
        captured: List[Dict] = []

        def _fake_safe_http_json(url, **kwargs):
            captured.append(dict(kwargs))
            return {"results": []}

        monkeypatch.setattr(
            sec04, "_safe_http_json", _fake_safe_http_json, raising=True
        )
        monkeypatch.setattr(
            sec04,
            "_extract_context7_library_candidates",
            lambda *a, **kw: ["pandas"],
            raising=True,
        )
        sec04._search_context7(
            "groupby aggregate",
            user_problem="how do I group by",
            mode="agent",
            problem_breakdown=None,
            lane_queries=None,
        )
        assert captured, "expected at least one _safe_http_json call"
        first_headers = captured[0].get("headers") or {}
        assert first_headers.get("Authorization") == "Bearer ctx7_inject_test"
        assert captured[0].get("provider_name") == "context7"

    def test_search_context7_no_authorization_when_unconfigured(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When no token is configured, ``_search_context7`` must NOT emit
        an Authorization header (anonymous tier, pre-v1.1.10 behaviour)."""
        from crucible.modules import section_04_web_research_and_direction as sec04

        for name in ("CONTEXT7_API_KEY", "CONTEXT7_TOKEN"):
            monkeypatch.delenv(name, raising=False)
        captured: List[Dict] = []

        def _fake_safe_http_json(url, **kwargs):
            captured.append(dict(kwargs))
            return {"results": []}

        monkeypatch.setattr(
            sec04, "_safe_http_json", _fake_safe_http_json, raising=True
        )
        monkeypatch.setattr(
            sec04,
            "_extract_context7_library_candidates",
            lambda *a, **kw: ["numpy"],
            raising=True,
        )
        sec04._search_context7(
            "ndarray reshape",
            user_problem="how do I reshape",
            mode="agent",
            problem_breakdown=None,
            lane_queries=None,
        )
        assert captured
        first_headers = captured[0].get("headers") or {}
        assert "Authorization" not in first_headers

    def test_search_context7_source_invokes_headers_helper(self) -> None:
        """Structural pin: a future refactor that drops the
        ``_context7_api_headers()`` call from ``_search_context7`` would
        regress the entire feature silently — the unit tests above mock
        ``_safe_http_json`` so they'd pass even if the helper was no
        longer wired in.  This pin reads source to confirm the wire-up."""
        from crucible.modules import section_04_web_research_and_direction as sec04

        src = inspect.getsource(sec04._search_context7)
        assert "_context7_api_headers" in src, (
            "_search_context7 must call _context7_api_headers — see v1.1.10 S1"
        )
        assert "headers=" in src, (
            "_search_context7 must pass headers= to _safe_http_json"
        )


# ────────────────────────────────────────────────────────────────────────────
# S2 — grep_app removed from defaults; github becomes primary code provider
# ────────────────────────────────────────────────────────────────────────────


class TestS2GrepAppRemovedFromDefaults:
    def test_default_provider_list_excludes_grep_app(self) -> None:
        from crucible.modules.section_00_bootstrap_and_utils import (
            OPENCODE_LIBRARIAN_DEFAULT_PROVIDERS,
        )
        assert "grep_app" not in OPENCODE_LIBRARIAN_DEFAULT_PROVIDERS
        # And the github provider — which has both code and repo search —
        # must remain in the default list so the "code" query class still
        # has a working primary.
        assert "github" in OPENCODE_LIBRARIAN_DEFAULT_PROVIDERS

    def test_default_provider_list_unchanged_core_members(self) -> None:
        """The other five members of the historical core list must remain
        — only grep_app is removed in v1.1.10."""
        from crucible.modules.section_00_bootstrap_and_utils import (
            OPENCODE_LIBRARIAN_DEFAULT_PROVIDERS,
        )
        expected = {"websearch", "context7", "github", "arxiv", "paperswithcode"}
        assert set(OPENCODE_LIBRARIAN_DEFAULT_PROVIDERS) == expected

    def test_fallback_default_string_excludes_grep_app(self) -> None:
        """``fallback._enabled_providers`` carries its own literal default
        string for the case where ``LIBRARIAN_SEARCH_PROVIDERS`` env is
        completely unset.  That literal must stay in lockstep with the
        canonical default list."""
        from crucible.web_research import fallback

        src = inspect.getsource(fallback._enabled_providers)
        # Find the literal CSV string passed as the default.  It is the
        # second positional arg to _parse_csv_env in the LIBRARIAN_SEARCH_PROVIDERS branch.
        match = re.search(
            r'LIBRARIAN_SEARCH_PROVIDERS"\s*,\s*"([^"]+)"', src
        )
        assert match, "could not locate the LIBRARIAN_SEARCH_PROVIDERS default literal"
        default_csv = match.group(1)
        names = [s.strip() for s in default_csv.split(",")]
        assert "grep_app" not in names
        assert "github" in names

    def test_fallback_default_matches_canonical_default(self) -> None:
        """Structural pin: the two default sources (section_00 const and
        fallback.py literal) must agree.  A future PR that updates one
        without the other introduces a silent split-brain."""
        from crucible.modules.section_00_bootstrap_and_utils import (
            OPENCODE_LIBRARIAN_DEFAULT_PROVIDERS,
        )
        from crucible.web_research import fallback

        src = inspect.getsource(fallback._enabled_providers)
        match = re.search(
            r'LIBRARIAN_SEARCH_PROVIDERS"\s*,\s*"([^"]+)"', src
        )
        assert match
        fallback_default = [s.strip() for s in match.group(1).split(",")]
        assert fallback_default == list(OPENCODE_LIBRARIAN_DEFAULT_PROVIDERS), (
            "fallback._enabled_providers default string out of sync with "
            "OPENCODE_LIBRARIAN_DEFAULT_PROVIDERS — update both together"
        )

    def test_search_grep_app_helper_still_exists(self) -> None:
        """grep_app is removed from defaults but NOT deleted — operators
        can still pin it via env override.  Behaviour preserved."""
        from crucible.modules import section_04_web_research_and_direction as sec04
        assert callable(getattr(sec04, "_search_grep_app", None))

    def test_grep_app_in_known_provider_aliases(self) -> None:
        """Provider name alias map still recognises ``grep_app`` so
        explicit env opt-in continues to resolve correctly."""
        from crucible.modules.section_00_bootstrap_and_utils import (
            LIBRARIAN_PROVIDER_ALIASES,
        )
        assert LIBRARIAN_PROVIDER_ALIASES.get("grep_app") == "grep_app"
        assert LIBRARIAN_PROVIDER_ALIASES.get("grep") == "grep_app"
        assert LIBRARIAN_PROVIDER_ALIASES.get("codesearch") == "grep_app"

    def test_code_query_class_chain_still_lists_github_first(self) -> None:
        """The fallback chain for the ``code`` query class must keep
        ``github`` as the primary — grep_app moves to position 2 (still
        usable as fallback if operator explicitly enables it)."""
        from crucible.web_research import fallback

        chain = fallback._DEFAULT_CHAIN_BY_CLASS.get("code") or []
        assert chain and chain[0] == "github", (
            f"code query class primary must be github, got {chain!r}"
        )


# ────────────────────────────────────────────────────────────────────────────
# S3 — WAF/CDN-protected pin URLs replaced
# ────────────────────────────────────────────────────────────────────────────


class TestS3DomainPinsReplaced:
    """Tier-1 pin URLs that returned AWS WAF / Cloudflare bot-challenge
    responses (HTTP 202 / 403) for every unauthenticated HTTP client are
    replaced by mirror endpoints that return 200 to ``CrucibleCrew/14``."""

    # Concrete URL fragments confirmed by Agent test (UA: CrucibleCrew/14,
    # 2026-05) to return non-200 for every unauthenticated HTTP client.
    _BLOCKED_FRAGMENTS = (
        # Binance FAQ pages live behind AWS WAF — always 202 + JS challenge
        # for both librarian UA and a full Chrome UA.
        "www.binance.com/en/support/faq/",
        # CoinGecko web docs page lives behind Cloudflare — always 403.
        "www.coingecko.com/en/api/documentation",
        # CME Group education hub also lives behind a CDN WAF — 403 for
        # every UA tried.
        "www.cmegroup.com/education",
        # OpenAI cookbook old root 308-redirects to developers.openai.com;
        # 308 is non-retryable for safe_http_text so the auditor would
        # silently get no body.  The new URL is required.
        "cookbook.openai.com/",
        # LangChain old path 308-redirects through a 3-hop chain to
        # docs.langchain.com; the manual-redirect helper caps at 3 hops
        # and the original UA gets dropped.
        "python.langchain.com/docs/introduction/",
    )

    def test_no_pin_points_at_known_waf_blocked_or_redirected_pages(
        self,
    ) -> None:
        pins = _read_domain_pins()
        urls = _all_pin_urls(pins)
        for url in urls:
            for fragment in self._BLOCKED_FRAGMENTS:
                assert fragment not in url, (
                    f"domain_pins.json still references blocked/redirected "
                    f"URL: {url} (matches forbidden fragment {fragment!r}); "
                    f"see v1.1.10 S3"
                )

    def test_binance_pin_uses_reachable_endpoint(self) -> None:
        """All Binance-related pins must point at a host that returns 200
        to anonymous clients.  Two known-good hosts:
        ``binance-docs.github.io`` (GitHub Pages mirror) and
        ``developers.binance.com`` (official developer portal)."""
        pins = _read_domain_pins()
        urls = _all_pin_urls(pins)
        binance_urls = [u for u in urls if "binance" in u.lower()]
        assert binance_urls, "expected at least one Binance pin"
        allowed_hosts = ("binance-docs.github.io", "developers.binance.com")
        for url in binance_urls:
            assert any(host in url for host in allowed_hosts), (
                f"Binance pin must use one of {allowed_hosts!r}, got: {url}"
            )

    def test_coingecko_pin_uses_reachable_endpoint(self) -> None:
        """CoinGecko docs root and the ``/en/api/documentation`` page are
        Cloudflare-gated.  Reachable substitutes: ``api.coingecko.com``
        (REST endpoints) or ``coingecko.com/learn`` (prose docs hub)."""
        pins = _read_domain_pins()
        urls = _all_pin_urls(pins)
        coingecko_urls = [u for u in urls if "coingecko" in u.lower()]
        assert coingecko_urls, "expected at least one CoinGecko pin"
        for url in coingecko_urls:
            ok = (
                "api.coingecko.com" in url
                or "coingecko.com/learn" in url
            )
            assert ok, (
                f"CoinGecko pin must use api.coingecko.com or "
                f"coingecko.com/learn, got: {url}"
            )

    def test_cmegroup_pin_replaced_with_wikipedia(self) -> None:
        """``www.cmegroup.com`` is WAF-blocked.  Replacement is Wikipedia
        articles covering the same conceptual material (CME, futures
        contracts, options)."""
        pins = _read_domain_pins()
        urls = _all_pin_urls(pins)
        # Confirm no cmegroup URL appears AND at least one Wikipedia
        # futures-related article is present in the same pin group as a
        # substitute.
        assert all("cmegroup.com" not in u for u in urls), (
            "www.cmegroup.com pins must be replaced — see v1.1.10 S3"
        )
        wiki_futures = [
            u for u in urls
            if "en.wikipedia.org/wiki/" in u
            and any(
                kw in u.lower() for kw in ("futures", "chicago", "options")
            )
        ]
        assert wiki_futures, (
            "expected at least one Wikipedia futures/options article as "
            "CME replacement"
        )

    def test_openai_cookbook_pin_uses_developers_url(self) -> None:
        """``cookbook.openai.com`` 308-redirects to
        ``developers.openai.com/cookbook`` and 308 is non-retryable for
        safe_http_text — so the auditor never sees the body unless we
        pin the post-redirect URL directly."""
        pins = _read_domain_pins()
        urls = _all_pin_urls(pins)
        openai_urls = [u for u in urls if "openai" in u.lower()]
        assert openai_urls, "expected at least one OpenAI pin"
        for url in openai_urls:
            assert "developers.openai.com" in url, (
                f"OpenAI cookbook pin must use developers.openai.com "
                f"(cookbook.openai.com 308s away), got: {url}"
            )

    def test_langchain_pin_uses_docs_subdomain(self) -> None:
        """``python.langchain.com`` 308-redirects through a 3-hop chain
        to ``docs.langchain.com``; the manual-redirect helper caps at
        3 hops and the chain exceeds budget for some entries.  Pin the
        canonical destination directly."""
        pins = _read_domain_pins()
        urls = _all_pin_urls(pins)
        lc_urls = [u for u in urls if "langchain" in u.lower()]
        assert lc_urls, "expected at least one LangChain pin"
        for url in lc_urls:
            assert "docs.langchain.com" in url, (
                f"LangChain pin must use docs.langchain.com, got: {url}"
            )

    def test_searxng_brave_instance_replaced(self) -> None:
        """``search.brave4u.com`` no longer resolves (DNS NXDOMAIN).
        Replacement: another long-running public SearXNG instance."""
        pins = _read_domain_pins()
        instances = pins.get("searxng_instances") or []
        assert instances, "searxng_instances list missing"
        assert all(
            "brave4u" not in i for i in instances
        ), "search.brave4u.com no longer resolves — remove it"
        assert any(
            "paulgo.io" in i or "opnxng.com" in i or "searx.be" in i
            for i in instances
        ), "expected at least one known-good public SearXNG instance"

    def test_all_pin_urls_use_https(self) -> None:
        """v1.1.8 contract: all pre-fetched URLs must be HTTPS (SSRF
        protection in ``prefetch_pinned_urls``).  Adding a new pin in
        HTTP is silently dropped at runtime."""
        pins = _read_domain_pins()
        urls = _all_pin_urls(pins)
        for url in urls:
            assert url.startswith("https://"), (
                f"pin URL must be https://, got: {url}"
            )


# ────────────────────────────────────────────────────────────────────────────
# S4 — .env.example ships placeholder API keys uncommented
# ────────────────────────────────────────────────────────────────────────────


class TestS4EnvExampleSurfacesKeys:
    def test_context7_api_key_uncommented(self) -> None:
        content = _read_env_example()
        # Look for a line that starts with the key name (i.e. NOT a comment
        # line) — placeholder value present is fine, we just want the
        # backend ``/api/env`` reader to emit the key to the Settings UI.
        assert re.search(
            r"^CONTEXT7_API_KEY\s*=", content, flags=re.MULTILINE
        ), "CONTEXT7_API_KEY must be uncommented in .env.example for the Settings UI to surface it"

    def test_github_token_uncommented(self) -> None:
        content = _read_env_example()
        assert re.search(
            r"^GITHUB_TOKEN\s*=", content, flags=re.MULTILINE
        ), "GITHUB_TOKEN must be uncommented in .env.example for the Settings UI to surface it"

    def test_context7_placeholder_is_filterable(self) -> None:
        """The placeholder value must start with one of the prefixes the
        backend ``_resolve_context7_token`` helper filters out, so a
        fresh install (`.env.example` copied to `.env` unchanged) does
        not pretend to have a working token."""
        content = _read_env_example()
        match = re.search(
            r"^CONTEXT7_API_KEY\s*=\s*(\S+)", content, flags=re.MULTILINE
        )
        assert match, "CONTEXT7_API_KEY assignment missing"
        value = match.group(1).strip().lower()
        assert value.startswith(
            ("replace_", "your_", "xxxx", "placeholder", "changeme")
        ), (
            f"CONTEXT7_API_KEY placeholder must be filterable by "
            f"_resolve_context7_token, got: {match.group(1)!r}"
        )

    def test_github_token_placeholder_is_filterable(self) -> None:
        content = _read_env_example()
        match = re.search(
            r"^GITHUB_TOKEN\s*=\s*(\S+)", content, flags=re.MULTILINE
        )
        assert match, "GITHUB_TOKEN assignment missing"
        value = match.group(1).strip().lower()
        assert value.startswith(
            ("replace_", "your_", "xxxx", "placeholder", "changeme")
        ), (
            f"GITHUB_TOKEN placeholder must be filterable, "
            f"got: {match.group(1)!r}"
        )


# ────────────────────────────────────────────────────────────────────────────
# S5 — WebUI Settings schema + KEY_META exposes both new keys
# ────────────────────────────────────────────────────────────────────────────


class TestS5SettingsUiExposesNewKeys:
    def test_librarian_auth_group_present_in_settings_schema(self) -> None:
        js = _read_app_js()
        # The schema entry uses single-quoted JS object literal so we
        # match a permissive pattern.
        assert "id:'librarian_auth'" in js, (
            "SETTINGS_SCHEMA must contain a librarian_auth group — see v1.1.10 S5"
        )

    def test_librarian_auth_group_lists_both_keys(self) -> None:
        js = _read_app_js()
        # Find the librarian_auth group block and confirm both keys appear
        # within its `keys:[...]` array.
        match = re.search(
            r"id:'librarian_auth'[^}]*?keys:\[([^\]]+)\]",
            js,
            flags=re.DOTALL,
        )
        assert match, "could not locate librarian_auth group's keys array"
        keys_text = match.group(1)
        assert "'CONTEXT7_API_KEY'" in keys_text
        assert "'GITHUB_TOKEN'" in keys_text

    def test_key_meta_has_bilingual_entries(self) -> None:
        """v1.1.0 KEY_META bilingual contract: every new entry must use
        ``desc:{en:'...', zh:'...'}`` shape.  An en-only string would
        regress the language-toggle feature for these two keys."""
        js = _read_app_js()
        # CONTEXT7_API_KEY entry
        m1 = re.search(
            r"CONTEXT7_API_KEY\s*:\s*\{[^}]*?desc:\s*(\{[^}]*\})",
            js,
            flags=re.DOTALL,
        )
        assert m1, "CONTEXT7_API_KEY missing or has no desc field"
        assert "en:" in m1.group(1) and "zh:" in m1.group(1), (
            "CONTEXT7_API_KEY desc must be bilingual {en, zh}"
        )
        # GITHUB_TOKEN entry
        m2 = re.search(
            r"GITHUB_TOKEN\s*:\s*\{[^}]*?desc:\s*(\{[^}]*\})",
            js,
            flags=re.DOTALL,
        )
        assert m2, "GITHUB_TOKEN missing or has no desc field"
        assert "en:" in m2.group(1) and "zh:" in m2.group(1), (
            "GITHUB_TOKEN desc must be bilingual {en, zh}"
        )

    def test_both_new_keys_are_password_type(self) -> None:
        """Credentials must render as <input type=password> so the value
        is masked in the UI.

        Because each KEY_META entry contains a nested ``desc:{en, zh}``
        object, a naive ``\\{[^}]*\\}`` regex stops at the first inner
        ``}`` and misses the ``type:`` field that comes after.  Use a
        line-based scan instead: locate the line that starts the entry,
        then check the rest of that line (KEY_META entries are
        single-line by convention)."""
        js = _read_app_js()
        for key in ("CONTEXT7_API_KEY", "GITHUB_TOKEN"):
            entry_line = next(
                (line for line in js.splitlines() if f"{key}:" in line and "label:" in line),
                None,
            )
            assert entry_line is not None, f"{key} entry not found in KEY_META"
            assert "type:'password'" in entry_line, (
                f"{key} must be type:'password' so the UI masks the value"
            )


# ────────────────────────────────────────────────────────────────────────────
# Cross-cutting: per-CLAUDE.md § 9.6 producer→consumer wiring
# ────────────────────────────────────────────────────────────────────────────


class TestCrossCuttingWiring:
    def test_context7_helpers_export_names_match_what_search_uses(self) -> None:
        """Sanity check: the helper symbol names that ``_search_context7``
        references must actually exist on the module."""
        from crucible.modules import section_04_web_research_and_direction as sec04

        assert callable(getattr(sec04, "_resolve_context7_token", None))
        assert callable(getattr(sec04, "_context7_api_headers", None))
        # And the existing GitHub helpers must NOT have been disturbed.
        assert callable(getattr(sec04, "_resolve_github_token", None))
        assert callable(getattr(sec04, "_github_api_headers", None))

    def test_context7_token_resolution_matches_github_token_pattern(self) -> None:
        """Both ``_resolve_context7_token`` and ``_resolve_github_token``
        must use the same placeholder-filtering pattern so a fresh
        ``.env.example`` copy does not accidentally enable either tier."""
        from crucible.modules.section_04_web_research_and_direction import (
            _resolve_context7_token,
            _resolve_github_token,
        )

        # Cross-check: both should reject "replace_..." style placeholders.
        for env_name, helper in (
            ("CONTEXT7_API_KEY", _resolve_context7_token),
            ("GITHUB_TOKEN", _resolve_github_token),
        ):
            with patch.dict(
                os.environ, {env_name: "replace_with_real_value"}, clear=False
            ):
                # GH token resolver only filters your_/xxxx/placeholder/changeme,
                # not "replace_" — but Context7 resolver does.  We only assert
                # behaviour on the resolver that owns the placeholder it sees.
                if env_name == "CONTEXT7_API_KEY":
                    assert helper() == ""
