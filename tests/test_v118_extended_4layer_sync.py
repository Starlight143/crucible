"""Structural sync tests for the v1.1.8 extended changes
(Web Research Hardening + Direction Gate Tuning).

These tests verify the 4-layer sync rule from CLAUDE.md § 1:

1. ``.env.example`` lists every new env key.
2. ``webui/static/js/app.js:SETTINGS_SCHEMA`` references every new key
   in one of its group's ``keys`` arrays.
3. ``webui/static/js/app.js:KEY_META`` has a bilingual ``desc:{en, zh}``
   entry per key.
4. For per-run flags ONLY: ``FLAG_META`` + ``FLAG_GROUPS`` +
   ``ENV_BACKED_FLAGS`` + ``webui/app.py:_RUN_INSIGHTS_FLAG_TO_ENV`` all
   stay in lockstep, RHS env-var names matching.

The producer→consumer wiring (whether the env var is actually read by
the consumer code) is verified in per-phase tests; this file only
ensures the 4-layer UI sync surfaces are consistent.  It is the
foundation pin: if any phase reverts these surfaces this file goes red
immediately.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest


_REPO_ROOT = Path(__file__).resolve().parents[1]
_ENV_EXAMPLE = _REPO_ROOT / ".env.example"
_APP_JS = _REPO_ROOT / "webui" / "static" / "js" / "app.js"
_APP_PY = _REPO_ROOT / "webui" / "app.py"
_RUN_CLI = _REPO_ROOT / "run_crucible_enhanced.py"
_DOMAIN_PINS = _REPO_ROOT / "crucible" / "config" / "domain_pins.json"


# All 23 env keys added in v1.1.8 extended (Phase 1 foundation).  Each MUST
# appear in .env.example + KEY_META + SETTINGS_SCHEMA.  If a later phase
# adds another env key, append it here so the structural pin keeps the
# 4-layer sync honest.
_V118_EXTENDED_ENV_KEYS: tuple[str, ...] = (
    # Q1 search cache (Phase 2)
    "LIBRARIAN_SEARCH_DISK_CACHE_ENABLED",
    "LIBRARIAN_SEARCH_CACHE_PATH",
    "LIBRARIAN_SEARCH_CACHE_TTL_DDG_HOURS",
    "LIBRARIAN_SEARCH_CACHE_TTL_GITHUB_HOURS",
    "LIBRARIAN_SEARCH_CACHE_TTL_ARXIV_HOURS",
    "LIBRARIAN_SEARCH_CACHE_TTL_CONTEXT7_HOURS",
    # Q2 / Q3 / Q5 / Q6 / Q7 / Q9 provider resilience (Phase 2 / 4 / 5)
    "LIBRARIAN_PROVIDER_COOLDOWN_INITIAL_SECONDS",
    "LIBRARIAN_PROVIDER_COOLDOWN_MAX_SECONDS",
    "LIBRARIAN_PROVIDER_FALLBACK_ENABLED",
    "LIBRARIAN_ASYNC_FANOUT_ENABLED",
    "LIBRARIAN_CROSS_PROVIDER_DEDUP_ENABLED",
    "LIBRARIAN_PROVIDER_HEALTH_SUMMARY",
    "LIBRARIAN_HTTP2_ENABLED",
    "LIBRARIAN_HTTP_KEEPALIVE_ENABLED",
    # Q4 extra providers (Phase 3)
    "LIBRARIAN_EXTRA_PROVIDERS",
    # Q8 / Q10 / P2 query quality (Phase 3 / 6 / 7)
    "LIBRARIAN_DOMAIN_PINS_ENABLED",
    "LIBRARIAN_DOMAIN_PINS_PATH",
    "LIBRARIAN_BILINGUAL_QUERY_EXPANSION",
    "LIBRARIAN_BILINGUAL_QUERY_THRESHOLD",
    "LIBRARIAN_QUERY_TRANSLATE_MODEL",
    "LIBRARIAN_CLAIM_ATTRIBUTION_DIRECTION_KEY",
    # P5 direction gate tuning (Phase 7)
    "CRUCIBLE_DEBATE_TOLERATE_UNVERIFIABLE_EVIDENCE",
    "CRUCIBLE_DEBATE_DEGRADE_AFTER_N_ITERATIONS",
)


# Per-run flag = the subset of env keys that ALSO have a per-run UI toggle.
# Only P5's master switch qualifies — all others are persistent ops
# settings.  Mapping is ``frontend_flag_key → backend_env_var``.
_V118_EXTENDED_PER_RUN_FLAGS: dict[str, str] = {
    "debate_tolerate_unverifiable_evidence": (
        "CRUCIBLE_DEBATE_TOLERATE_UNVERIFIABLE_EVIDENCE"
    ),
}


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


class TestEnvExampleContainsEveryKey:
    """Layer 1: .env.example MUST list every new env key."""

    def test_all_env_keys_present_in_env_example(self) -> None:
        text = _read(_ENV_EXAMPLE)
        missing = [k for k in _V118_EXTENDED_ENV_KEYS if k not in text]
        assert not missing, (
            "v1.1.8 extended .env.example regression: missing keys "
            f"{missing}.  Add them to .env.example with bilingual "
            "section header comments."
        )


class TestSettingsSchemaReferencesEveryKey:
    """Layer 2: webui/static/js/app.js:SETTINGS_SCHEMA MUST reference
    every new key in one of its group's ``keys`` arrays."""

    def test_all_env_keys_referenced_in_settings_schema(self) -> None:
        text = _read(_APP_JS)
        match = re.search(
            r"const SETTINGS_SCHEMA\s*=\s*\[(.+?)\n\];",
            text,
            re.DOTALL,
        )
        assert match is not None, (
            "Could not locate SETTINGS_SCHEMA block in app.js"
        )
        block = match.group(1)
        missing = [k for k in _V118_EXTENDED_ENV_KEYS if f"'{k}'" not in block]
        assert not missing, (
            "v1.1.8 extended SETTINGS_SCHEMA regression: env keys not "
            f"referenced in any group's keys array: {missing}"
        )


class TestKeyMetaHasBilingualEntryPerKey:
    """Layer 3: webui/static/js/app.js:KEY_META MUST have a bilingual
    entry (containing both ``en:`` and ``zh:``) for every new key."""

    def test_all_env_keys_have_bilingual_key_meta_entry(self) -> None:
        text = _read(_APP_JS)
        match = re.search(
            r"const KEY_META\s*=\s*\{(.+?)\n\};",
            text,
            re.DOTALL,
        )
        assert match is not None, "Could not locate KEY_META block in app.js"
        block = match.group(1)
        missing_entry: list[str] = []
        missing_bilingual: list[str] = []
        for key in _V118_EXTENDED_ENV_KEYS:
            # Match ``KEY_NAME: { ... },``
            entry_re = re.compile(
                r"^\s*" + re.escape(key) + r"\s*:\s*\{(.+?)\}\s*,",
                re.MULTILINE | re.DOTALL,
            )
            m = entry_re.search(block)
            if m is None:
                missing_entry.append(key)
                continue
            entry_body = m.group(1)
            # Bilingual contract per CLAUDE.md § 10.
            if "en:" not in entry_body or "zh:" not in entry_body:
                missing_bilingual.append(key)
        assert not missing_entry, (
            "v1.1.8 extended KEY_META regression: missing entries for "
            f"{missing_entry}. Per CLAUDE.md § 10, every KEY_META entry "
            "must be bilingual {en, zh}."
        )
        assert not missing_bilingual, (
            "v1.1.8 extended KEY_META bilingual contract violated: "
            f"entries {missing_bilingual} have only one language.  Both "
            "en: and zh: keys are required inside the desc:{} object."
        )


class TestPerRunFlagWiringLockstep:
    """Layers 4-7: per-run flag must appear in FLAG_META + FLAG_GROUPS +
    ENV_BACKED_FLAGS + webui/app.py _RUN_INSIGHTS_FLAG_TO_ENV, all
    referring to the same env-var name on the RHS."""

    def test_per_run_flag_in_flag_meta(self) -> None:
        text = _read(_APP_JS)
        match = re.search(
            r"const FLAG_META\s*=\s*\{(.+?)\n\};",
            text,
            re.DOTALL,
        )
        assert match is not None, "Could not locate FLAG_META in app.js"
        block = match.group(1)
        for flag_key in _V118_EXTENDED_PER_RUN_FLAGS:
            pattern = re.compile(
                r"\b" + re.escape(flag_key) + r"\s*:\s*\{",
            )
            assert pattern.search(block), (
                f"FLAG_META regression: {flag_key} missing from "
                "webui/static/js/app.js FLAG_META block"
            )

    def test_per_run_flag_in_flag_groups(self) -> None:
        text = _read(_APP_JS)
        match = re.search(
            r"const FLAG_GROUPS\s*=\s*\[(.+?)\n\];",
            text,
            re.DOTALL,
        )
        assert match is not None, "Could not locate FLAG_GROUPS in app.js"
        block = match.group(1)
        for flag_key in _V118_EXTENDED_PER_RUN_FLAGS:
            assert f"'{flag_key}'" in block, (
                f"FLAG_GROUPS regression: {flag_key} not referenced in "
                "any group's flags array"
            )

    def test_per_run_flag_in_env_backed_flags(self) -> None:
        text = _read(_APP_JS)
        match = re.search(
            r"const ENV_BACKED_FLAGS\s*=\s*\{(.+?)\n\};",
            text,
            re.DOTALL,
        )
        assert match is not None, (
            "Could not locate ENV_BACKED_FLAGS in app.js"
        )
        block = match.group(1)
        for flag_key, env_key in _V118_EXTENDED_PER_RUN_FLAGS.items():
            pattern = re.compile(
                r"\b"
                + re.escape(flag_key)
                + r"\s*:\s*['\"]"
                + re.escape(env_key)
                + r"['\"]"
            )
            assert pattern.search(block), (
                f"ENV_BACKED_FLAGS regression: {flag_key} → {env_key} "
                "mapping missing or wrong RHS"
            )

    def test_per_run_flag_in_app_py_mapping(self) -> None:
        text = _read(_APP_PY)
        match = re.search(
            r"_RUN_INSIGHTS_FLAG_TO_ENV\s*:\s*dict\[[^\]]+\]\s*=\s*\{(.+?)\n\}",
            text,
            re.DOTALL,
        )
        assert match is not None, (
            "Could not locate _RUN_INSIGHTS_FLAG_TO_ENV in webui/app.py"
        )
        block = match.group(1)
        for flag_key, env_key in _V118_EXTENDED_PER_RUN_FLAGS.items():
            pattern = re.compile(
                r"['\"]"
                + re.escape(flag_key)
                + r"['\"]"
                + r"\s*:\s*['\"]"
                + re.escape(env_key)
                + r"['\"]"
            )
            assert pattern.search(block), (
                f"_RUN_INSIGHTS_FLAG_TO_ENV regression: {flag_key} → "
                f"{env_key} mapping missing or wrong RHS"
            )


class TestCliFlagWiring:
    """The --tolerate-unverifiable-evidence CLI flag must exist in
    run_crucible_enhanced.py AND have an env-translation block that
    writes CRUCIBLE_DEBATE_TOLERATE_UNVERIFIABLE_EVIDENCE.

    Mirrors test_wiring.py:test_cmd_run_translates_audit_mode_arg_to_env
    pattern from v1.1.8 audit mode."""

    def test_cli_flag_declared(self) -> None:
        text = _read(_RUN_CLI)
        assert '"--tolerate-unverifiable-evidence"' in text, (
            "CLI flag --tolerate-unverifiable-evidence missing from "
            "run_crucible_enhanced.py argparse setup"
        )

    def test_cli_flag_translates_to_env(self) -> None:
        text = _read(_RUN_CLI)
        # Same structural pin pattern used by the v1.1.8 audit-mode
        # wiring test (test_wiring.py).
        env_name = "CRUCIBLE_DEBATE_TOLERATE_UNVERIFIABLE_EVIDENCE"
        assert f'os.environ["{env_name}"]' in text, (
            "CLI flag wiring regression: --tolerate-unverifiable-evidence "
            f"does not translate to {env_name} env var.  The flag "
            "silently has no effect.  Add the assignment to cmd_run's "
            "env-translation block (around line 1145-1160)."
        )


class TestDomainPinsJsonValid:
    """crucible/config/domain_pins.json MUST exist and parse as valid
    JSON with the expected v1.1.8 schema (version + pins array)."""

    def test_file_exists(self) -> None:
        assert _DOMAIN_PINS.exists(), (
            f"Expected domain pins JSON at {_DOMAIN_PINS} — created in "
            "Phase 1 of v1.1.8 extended"
        )

    def test_valid_json(self) -> None:
        try:
            data = json.loads(_DOMAIN_PINS.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            pytest.fail(f"domain_pins.json is not valid JSON: {exc}")
        assert isinstance(data, dict), (
            "domain_pins.json root must be an object"
        )
        assert data.get("version") == 1, (
            "domain_pins.json version must be 1"
        )
        assert isinstance(data.get("pins"), list), (
            "domain_pins.json must have a pins array"
        )
        # At least 5 domain coverage areas: crypto, tradfi, scientific,
        # saas, agent (CLAUDE.md plan for v1.1.8 extended).
        assert len(data["pins"]) >= 5, (
            "domain_pins.json should cover at least 5 modes (crypto, "
            "tradfi, scientific, saas, agent) — got "
            f"{len(data['pins'])}"
        )

    def test_pin_schema(self) -> None:
        data = json.loads(_DOMAIN_PINS.read_text(encoding="utf-8"))
        for i, pin in enumerate(data["pins"]):
            assert isinstance(pin, dict), f"Pin {i} not an object"
            assert "id" in pin, f"Pin {i} missing 'id'"
            assert "match" in pin, f"Pin {i} missing 'match'"
            assert isinstance(pin["match"], dict), (
                f"Pin {i} 'match' not an object"
            )
            assert "mode" in pin["match"], (
                f"Pin {i} 'match' missing 'mode'"
            )
            assert "pre_fetch" in pin, f"Pin {i} missing 'pre_fetch'"
            assert isinstance(pin["pre_fetch"], list), (
                f"Pin {i} 'pre_fetch' not a list"
            )
            for j, fetch in enumerate(pin["pre_fetch"]):
                assert "url" in fetch, (
                    f"Pin {i} fetch {j} missing 'url'"
                )
                # SSRF safety: pinned URLs must use HTTPS.  The runtime
                # ``_is_public_http_url`` check (Phase 3 dispatcher) will
                # reject anything else, so this is a structural pin.
                assert fetch["url"].startswith("https://"), (
                    f"Pin {i} fetch {j} url must use https:// — got "
                    f"{fetch['url']!r}"
                )
                assert "tier" in fetch, (
                    f"Pin {i} fetch {j} missing 'tier'"
                )


class TestNoDuplicateEnvKeysIntroduced:
    """Defensive structural test: ensure none of the v1.1.8 extended env
    keys collide with existing env keys in .env.example (which would
    confuse the operator and the parser)."""

    def test_no_duplicate_lines_in_env_example(self) -> None:
        text = _read(_ENV_EXAMPLE)
        for key in _V118_EXTENDED_ENV_KEYS:
            # Count uncommented occurrences as ``KEY=...`` at start of
            # line; comment lines have ``# KEY=...`` and don't count.
            pattern = re.compile(
                r"^\s*" + re.escape(key) + r"\s*=",
                re.MULTILINE,
            )
            matches = pattern.findall(text)
            assert len(matches) == 1, (
                f".env.example duplicate / missing: {key} found "
                f"{len(matches)} times.  Each env key must appear "
                "exactly once as an active definition."
            )
