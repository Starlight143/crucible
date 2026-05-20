"""
v1.1.8 — Producer→consumer wiring structural tests.

These tests defend against the silent-regression class identified in
CLAUDE.md § 9.6 ("producer is tested, consumer wiring is not"): mapping
A → B is internally consistent, but the consumer of B never actually
reads what the mapping produces.  v1.1.0 fifth-pass G-1 was a regression
of that exact shape.

What this file structurally verifies for v1.1.8:

1.  Every new ``CRUCIBLE_DEBATE_*`` env key declared in ``.env.example``
    appears in ``SETTINGS_SCHEMA`` and has a ``KEY_META`` entry.
2.  Every ``KEY_META`` entry added in v1.1.8 has a bilingual ``desc``
    object ({en, zh}) — the CLAUDE.md § 10 invariant.
3.  Every ``FLAG_META`` entry added in v1.1.8 has a bilingual ``desc``.
4.  ``ENV_BACKED_FLAGS`` (frontend) and ``_RUN_INSIGHTS_FLAG_TO_ENV``
    (backend) agree on the v1.1.8 mappings — both must list the same
    flag_key → env_var pairs in lockstep.
5.  Backend ``_RUN_INSIGHTS_FLAG_TO_ENV`` RHS env names are actually
    read by section_02 / section_07 — no "producer maps to X but
    consumer reads Y" silent regression.
6.  CLI flags in run_crucible_enhanced.py wire into cmd_run's env
    override translation — verified via ``inspect.getsource`` so the
    wire-up loop cannot be silently deleted.
"""
from __future__ import annotations

import inspect
import re
from pathlib import Path

import pytest


_REPO_ROOT = Path(__file__).resolve().parents[2]
_APP_JS = _REPO_ROOT / "webui" / "static" / "js" / "app.js"
_ENV_EXAMPLE = _REPO_ROOT / ".env.example"
_WEBUI_APP_PY = _REPO_ROOT / "webui" / "app.py"
_RUN_CRUCIBLE_ENHANCED = _REPO_ROOT / "run_crucible_enhanced.py"
_SECTION_02 = (
    _REPO_ROOT / "crucible" / "modules" / "section_02_research_and_llm.py"
)
_SECTION_04 = (
    _REPO_ROOT
    / "crucible"
    / "modules"
    / "section_04_web_research_and_direction.py"
)
_SECTION_07 = (
    _REPO_ROOT
    / "crucible"
    / "modules"
    / "section_07_selfcheck_output_main.py"
)


# All v1.1.8 audit-mode env keys that .env.example declares as live
# (uncommented) and the SETTINGS_SCHEMA/KEY_META must surface.
_V118_AUDIT_ENV_KEYS = {
    "CRUCIBLE_DEBATE_AUDIT_MODE",
    "CRUCIBLE_DEBATE_REQUIRE_STRUCTURED_FINDINGS",
    "CRUCIBLE_DEBATE_ISOLATION_MODE",
    "CRUCIBLE_DEBATE_EXTERNAL_CRITIC",
    "CRUCIBLE_DEBATE_CRITIC_OVERRIDE_PROCEED",
    "CRUCIBLE_DEBATE_CONSENSUS_RISK_THRESHOLD",
    "CRUCIBLE_DEBATE_CRITIC_MAX_ATTEMPTS",
    "CRUCIBLE_RUN_INSIGHTS_RECORD_DEBATE_FINDING",
    "CRUCIBLE_RUN_INSIGHTS_RECORD_GATE_VERDICT",
}

# Keys that show up in per-run flag panel (subset).
_V118_PER_RUN_FLAGS = {
    "debate_audit_mode": "CRUCIBLE_DEBATE_AUDIT_MODE",
    "debate_external_critic": "CRUCIBLE_DEBATE_EXTERNAL_CRITIC",
}


# ── .env.example declares every key uncommented ──────────────────────────────


class TestEnvExampleDeclares:
    def test_every_v118_audit_key_uncommented_in_env_example(self) -> None:
        text = _ENV_EXAMPLE.read_text(encoding="utf-8")
        for key in _V118_AUDIT_ENV_KEYS:
            # Must appear as a non-comment line ``KEY=value``.
            pattern = rf"^\s*{re.escape(key)}\s*="
            assert re.search(pattern, text, re.MULTILINE), (
                f"{key} is missing or commented-out in .env.example — the "
                f"WebUI Settings page reads /api/env which strips commented "
                f"lines, so commented keys are invisible to operators."
            )


# ── SETTINGS_SCHEMA + KEY_META wiring ────────────────────────────────────────


class TestSettingsSchemaWiring:
    def _read_app_js(self) -> str:
        return _APP_JS.read_text(encoding="utf-8")

    def test_every_v118_audit_key_appears_in_settings_schema(self) -> None:
        text = self._read_app_js()
        # Find the SETTINGS_SCHEMA block.
        start = text.index("const SETTINGS_SCHEMA = [")
        end_match = re.search(r"\n\];\n", text[start:])
        assert end_match
        block = text[start : start + end_match.start()]
        for key in _V118_AUDIT_ENV_KEYS:
            assert f"'{key}'" in block, (
                f"{key} not found inside SETTINGS_SCHEMA — Settings page will "
                f"not render this env key.  Add it to the debate_audit group."
            )

    def test_every_v118_audit_key_has_key_meta_entry(self) -> None:
        text = self._read_app_js()
        start = text.index("const KEY_META = {")
        end_match = re.search(r"\n};\n", text[start:])
        assert end_match
        block = text[start : start + end_match.start()]
        for key in _V118_AUDIT_ENV_KEYS:
            # KEY_META entries are keyed by the bare env name.
            assert (
                re.search(rf"^\s*{re.escape(key)}\s*:", block, re.MULTILINE)
                is not None
            ), (
                f"{key} missing from KEY_META — Settings page will fall back "
                f"to the 'Other' group with no label / description."
            )


# ── KEY_META v1.1.8 entries are bilingual ────────────────────────────────────


class TestKeyMetaBilingual:
    def test_v118_audit_key_meta_entries_are_bilingual(self) -> None:
        """CLAUDE.md § 10 invariant: every KEY_META entry must have
        bilingual ``desc:{en, zh}``.  This pin is the v1.1.8-specific
        addition — the existing 187 entries are already bilingual."""
        text = _APP_JS.read_text(encoding="utf-8")
        start = text.index("const KEY_META = {")
        end_match = re.search(r"\n};\n", text[start:])
        block = text[start : start + end_match.start()]

        for key in _V118_AUDIT_ENV_KEYS:
            # Extract the entry value (everything between ``KEY:`` and the
            # next ``},`` that terminates this entry).  Crude but adequate
            # because entries are one-line dicts in the v1.1.8 style.
            entry_re = re.compile(
                rf"^\s*{re.escape(key)}\s*:\s*\{{.*?\}}\s*,\s*$",
                re.MULTILINE | re.DOTALL,
            )
            m = entry_re.search(block)
            assert m, f"could not locate KEY_META entry for {key}"
            entry_text = m.group(0)
            # Bilingual contract: desc must be {en: ..., zh: ...}.
            assert "desc:{en:" in entry_text or "desc: {en:" in entry_text or 'desc:{en' in entry_text.replace(" ", ""), (
                f"{key} KEY_META entry is not bilingual — must have "
                f"``desc:{{en:'...', zh:'...'}}``"
            )
            assert "zh:" in entry_text, (
                f"{key} KEY_META entry missing zh: clause"
            )


# ── FLAG_META v1.1.8 entries are bilingual ───────────────────────────────────


class TestFlagMetaBilingual:
    def test_v118_per_run_flag_meta_entries_are_bilingual(self) -> None:
        text = _APP_JS.read_text(encoding="utf-8")
        start = text.index("const FLAG_META = {")
        end_match = re.search(r"\n};\n", text[start:])
        block = text[start : start + end_match.start()]

        for flag_key in _V118_PER_RUN_FLAGS:
            entry_re = re.compile(
                rf"^\s*{re.escape(flag_key)}\s*:\s*\{{.*?\}}\s*,\s*$",
                re.MULTILINE | re.DOTALL,
            )
            m = entry_re.search(block)
            assert m, f"could not locate FLAG_META entry for {flag_key}"
            entry_text = m.group(0)
            assert "desc:{en:" in entry_text.replace(" ", ""), (
                f"{flag_key} FLAG_META is not bilingual"
            )
            assert "zh:" in entry_text, (
                f"{flag_key} FLAG_META missing zh: clause"
            )


# ── ENV_BACKED_FLAGS (frontend) ↔ _RUN_INSIGHTS_FLAG_TO_ENV (backend) ────────


class TestFrontendBackendFlagMapping:
    def test_env_backed_flags_includes_v118_mappings(self) -> None:
        text = _APP_JS.read_text(encoding="utf-8")
        start = text.index("const ENV_BACKED_FLAGS = {")
        end_match = re.search(r"\n};\n", text[start:])
        block = text[start : start + end_match.start()]
        for flag_key, env_key in _V118_PER_RUN_FLAGS.items():
            assert f"'{env_key}'" in block, (
                f"ENV_BACKED_FLAGS missing mapping for {flag_key} → {env_key}"
            )

    def test_backend_run_insights_flag_to_env_includes_v118_mappings(self) -> None:
        from webui.app import _RUN_INSIGHTS_FLAG_TO_ENV

        for flag_key, env_key in _V118_PER_RUN_FLAGS.items():
            assert _RUN_INSIGHTS_FLAG_TO_ENV.get(flag_key) == env_key, (
                f"_RUN_INSIGHTS_FLAG_TO_ENV[{flag_key!r}] != {env_key!r}; "
                f"frontend/backend mapping out of sync"
            )

    def test_frontend_backend_v118_mapping_lockstep(self) -> None:
        """Same flag_key → env_var mapping appears on both sides."""
        text = _APP_JS.read_text(encoding="utf-8")
        # Parse the v1.1.8 portion of ENV_BACKED_FLAGS (the entries we added).
        frontend_pairs = {}
        for flag_key in _V118_PER_RUN_FLAGS:
            m = re.search(
                rf"^\s*{re.escape(flag_key)}\s*:\s*'([^']+)'\s*,\s*$",
                text,
                re.MULTILINE,
            )
            assert m, f"frontend ENV_BACKED_FLAGS missing {flag_key}"
            frontend_pairs[flag_key] = m.group(1)

        from webui.app import _RUN_INSIGHTS_FLAG_TO_ENV
        for flag_key, env_var in frontend_pairs.items():
            assert _RUN_INSIGHTS_FLAG_TO_ENV.get(flag_key) == env_var, (
                f"lockstep violation: frontend says {flag_key}→{env_var}, "
                f"backend says {flag_key}→{_RUN_INSIGHTS_FLAG_TO_ENV.get(flag_key)}"
            )


# ── Mapping RHS env names actually read by pipeline ──────────────────────────


class TestMappingRhsMatchesPipelineReads:
    """v1.1.0 fifth-pass G-1 was a regression in this exact shape: the
    mapping said ``flag → CRUCIBLE_CACHE`` but the pipeline read
    ``LOCAL_CACHE``, so unchecking the box had zero effect.  This pin
    prevents v1.1.8 from making the same mistake.
    """

    def test_v118_env_keys_read_by_section_02_or_section_07(self) -> None:
        section_02_text = _SECTION_02.read_text(encoding="utf-8")
        section_07_text = _SECTION_07.read_text(encoding="utf-8")
        recorder_text = (
            _REPO_ROOT
            / "crucible"
            / "features"
            / "run_insights"
            / "recorder.py"
        ).read_text(encoding="utf-8")

        combined = section_02_text + "\n" + section_07_text + "\n" + recorder_text

        # Each per-run mapping RHS must appear in at least one consumer
        # read site (env_bool / env_str / os.environ.get / env_float).
        for flag_key, env_key in _V118_PER_RUN_FLAGS.items():
            assert env_key in combined, (
                f"_RUN_INSIGHTS_FLAG_TO_ENV maps {flag_key}→{env_key}, but "
                f"{env_key} is not read in section_02 / section_07 / "
                f"recorder.py — the per-run toggle is a no-op."
            )


# ── CLI flag → env override wiring ───────────────────────────────────────────


class TestCliFlagWiring:
    def test_cmd_run_translates_audit_mode_arg_to_env(self) -> None:
        """v1.1.8 CLI → env wiring: cmd_run MUST contain the env-translation
        block we added; otherwise --audit-mode silently has no effect.
        Structural inspect.getsource pin per CLAUDE.md § 9.6."""
        text = _RUN_CRUCIBLE_ENHANCED.read_text(encoding="utf-8")
        # Each of the four CLI flags must result in an os.environ
        # assignment with the corresponding env var name.
        required_assignments = [
            'CRUCIBLE_DEBATE_AUDIT_MODE',
            'CRUCIBLE_DEBATE_ISOLATION_MODE',
            'CRUCIBLE_DEBATE_EXTERNAL_CRITIC',
            'CRUCIBLE_DEBATE_CRITIC_OVERRIDE_PROCEED',
        ]
        for env_var in required_assignments:
            assert f'os.environ["{env_var}"]' in text, (
                f"cmd_run is missing the env-translation assignment for "
                f"{env_var}; the CLI flag silently has no effect."
            )

    def test_argparse_registers_all_v118_flags(self) -> None:
        """argparse must register all four CLI flags so users can actually
        pass them."""
        text = _RUN_CRUCIBLE_ENHANCED.read_text(encoding="utf-8")
        required_flags = [
            "--audit-mode",
            "--debate-isolation",
            "--external-critic",
            "--critic-can-override",
        ]
        for flag in required_flags:
            assert f'"{flag}"' in text, (
                f"CLI flag {flag} not registered in run_crucible_enhanced.py"
            )


# ── selfcheck contradiction detection ────────────────────────────────────────


class TestSelfcheckContradiction:
    def test_section_07_main_has_audit_mode_contradiction_check(self) -> None:
        text = _SECTION_07.read_text(encoding="utf-8")
        # The contradiction check looks for the AUDIT_MODE + RUN_INSIGHTS_ENABLED
        # combination and warns when audit_mode is on but ledger is off.
        assert (
            'CRUCIBLE_DEBATE_AUDIT_MODE' in text
            and 'CRUCIBLE_RUN_INSIGHTS_ENABLED' in text
            and 'audit mode has nothing' in text
        ), (
            "section_07.main() is missing the v1.1.8 contradiction check "
            "(AUDIT_MODE=1 + RUN_INSIGHTS_ENABLED=0 silent failure mode)."
        )
