"""Regression pins for the v1.1.11 audit-fix pass.

Covers the seven fix clusters (F-A frontend behaviour, F-B a11y/RWD, F-C
settings-sync, F-D backend security/stability, F-E core pipeline, F-F run
insights ledger, F-G web-research + infra).  Behavioural tests where feasible,
structural ``inspect``/source pins for wiring that is impractical to drive
end-to-end (CLAUDE.md §9.6 producer→consumer convention).
"""
from __future__ import annotations

import inspect
import re
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _read(*parts: str) -> str:
    return (PROJECT_ROOT.joinpath(*parts)).read_text(encoding="utf-8")


_APP_JS = _read("webui", "static", "js", "app.js")
_INDEX_HTML = _read("webui", "templates", "index.html")
_APP_CSS = _read("webui", "static", "css", "app.css")
_ENV_EXAMPLE = _read(".env.example")


# ───────────────────────────── F-E1: GateVerdict mutual-exclusion ────────────

_REASON = "a" * 30  # ≥ 20 chars (reason min_length=20)


def _branch(direction_id: str = "A"):
    from crucible.modules.section_03_models_and_context import BranchSpec
    return BranchSpec(direction_id=direction_id, rationale="because")


class TestFE1GateVerdictForbidChecks:
    def test_valid_decisions_still_construct(self):
        from crucible.modules.section_03_models_and_context import GateVerdict
        assert GateVerdict(decision="PROCEED", selected_direction="A", reason=_REASON)
        assert GateVerdict(decision="KILL", reason=_REASON, failed_invariants=["x"])
        assert GateVerdict(
            decision="NEEDS_MORE_DATA", reason=_REASON, blocking_evidence_queries=["q"]
        )
        assert GateVerdict(
            decision="BRANCH", reason=_REASON,
            branched_paths=[_branch("A"), _branch("B")],
        )

    def test_proceed_forbids_branched_paths(self):
        from pydantic import ValidationError
        from crucible.modules.section_03_models_and_context import GateVerdict
        with pytest.raises(ValidationError):
            GateVerdict(
                decision="PROCEED", selected_direction="A", reason=_REASON,
                branched_paths=[_branch("A"), _branch("B")],
            )

    def test_kill_forbids_branched_paths(self):
        from pydantic import ValidationError
        from crucible.modules.section_03_models_and_context import GateVerdict
        with pytest.raises(ValidationError):
            GateVerdict(
                decision="KILL", reason=_REASON, failed_invariants=["x"],
                branched_paths=[_branch("A"), _branch("B")],
            )

    def test_needs_more_data_forbids_selected_direction(self):
        from pydantic import ValidationError
        from crucible.modules.section_03_models_and_context import GateVerdict
        with pytest.raises(ValidationError):
            GateVerdict(
                decision="NEEDS_MORE_DATA", selected_direction="A", reason=_REASON,
                blocking_evidence_queries=["q"],
            )

    def test_needs_more_data_forbids_branched_paths(self):
        from pydantic import ValidationError
        from crucible.modules.section_03_models_and_context import GateVerdict
        with pytest.raises(ValidationError):
            GateVerdict(
                decision="NEEDS_MORE_DATA", reason=_REASON,
                blocking_evidence_queries=["q"],
                branched_paths=[_branch("A"), _branch("B")],
            )

    def test_branch_forbids_selected_direction(self):
        from pydantic import ValidationError
        from crucible.modules.section_03_models_and_context import GateVerdict
        with pytest.raises(ValidationError):
            GateVerdict(
                decision="BRANCH", selected_direction="A", reason=_REASON,
                branched_paths=[_branch("A"), _branch("B")],
            )

    def test_branch_forbids_blocking_evidence_queries(self):
        from pydantic import ValidationError
        from crucible.modules.section_03_models_and_context import GateVerdict
        with pytest.raises(ValidationError):
            GateVerdict(
                decision="BRANCH", reason=_REASON,
                branched_paths=[_branch("A"), _branch("B")],
                blocking_evidence_queries=["should not be here"],
            )

    def test_critic_coercer_gates_secondary_fields_by_decision(self):
        # Source pin: the coercer must gate every shape-specific field by
        # decision so a chatty model emitting a stray secondary field does not
        # trip the tightened validator and burn a retry.
        from crucible.features.direction_debate import critic
        src = inspect.getsource(critic._coerce_verdict_dict_to_gateverdict)
        assert 'branched_paths if decision == "BRANCH" else []' in src
        assert 'failed_invariants if decision == "KILL" else []' in src
        assert 'blocking_queries if decision == "NEEDS_MORE_DATA"' in src


# ───────────────────────────── F-E2/E4/E5: section pipeline pins ─────────────

class TestFEPipelineSourcePins:
    def test_try_build_lenient_retry_emits_debug(self):
        from crucible.modules import section_01_extraction_and_reformat as s01
        src = inspect.getsource(s01)
        assert "except Exception as _lenient_exc:" in src
        assert "lenient retry" in src

    def test_degraded_proceed_threads_preclamp_score(self):
        from crucible.modules import section_02_research_and_llm as s02
        src = inspect.getsource(s02)
        assert "final_score=0," not in src, "degraded-proceed must not hardcode 0"
        assert "_preclamp_score" in src
        assert "final_score=int(_preclamp_score)" in src

    def test_cooldown_skip_caught_before_generic_except(self):
        src = _read("crucible", "modules", "section_04_web_research_and_direction.py")
        cool = src.find("except _CooldownSkipError:\n                # v1.1.11")
        generic = src.find("except Exception as exc:\n                query_label")
        assert cool != -1, "dispatcher must catch _CooldownSkipError"
        assert generic != -1
        assert cool < generic, "cooldown skip must be caught before the generic except"

    def test_specialist_finding_rejects_non_finite_confidence(self):
        from pydantic import ValidationError
        from crucible.modules.section_03_models_and_context import SpecialistFinding
        for bad in (float("nan"), float("inf"), float("-inf")):
            with pytest.raises(ValidationError):
                SpecialistFinding(role="judge", conclusion="c", confidence=bad)
        # finite still accepted
        assert SpecialistFinding(role="judge", conclusion="c", confidence=0.5)


# ───────────────────────────── F-F: run insights ledger ─────────────────────

class TestFFNoOpBackendParity:
    def test_signature_and_return_shape(self):
        from crucible.features.run_insights.recorder import _NoOpBackend
        from crucible.features.run_insights.backends import LocalJSONLBackend
        noop = _NoOpBackend()
        events, cursor = noop.read_events("output")
        assert events == [] and cursor is None
        e2, c2 = noop.read_events("output", since=None, cursor=None, limit=50)
        assert e2 == [] and c2 is None
        assert noop.write_blob("sha256:" + "00" * 32, b"data") == ""
        assert noop.read_blob("sha256:" + "00" * 32) is None
        for meth in ("write_blob", "read_blob", "read_events"):
            noop_params = list(inspect.signature(getattr(_NoOpBackend, meth)).parameters)[1:]
            live_params = list(inspect.signature(getattr(LocalJSONLBackend, meth)).parameters)[1:]
            assert [p.lstrip("_") for p in noop_params] == [p.lstrip("_") for p in live_params], meth


class TestFFRedactHexKeys:
    @pytest.mark.parametrize("hexlen", [32, 33, 36, 39, 40, 48, 64])
    def test_pure_hex_sk_key_fully_redacted(self, hexlen):
        from crucible.features.run_insights.redact import _redact_string_value, _REDACTED
        secret = "sk-" + ("a1b2c3d4e5f6" * 8)[:hexlen]
        out = _redact_string_value(f"401 invalid api key: {secret} rotate")
        assert secret not in out, f"hex sk- key len {hexlen} leaked: {out!r}"
        assert secret[3:] not in out
        assert _REDACTED in out

    @pytest.mark.parametrize("legit", [
        "see ticket sk-bug-12345",
        "function sk_helper() { return 1; }",
        "ref sk-deadbeef done",
    ])
    def test_negatives_not_over_matched(self, legit):
        from crucible.features.run_insights.redact import _redact_string_value
        assert _redact_string_value(legit) == legit


class TestFFWarnFlagDecoupled:
    def test_record_gate_verdict_uses_own_flag(self):
        from crucible.features.run_insights.recorder import InsightsRecorder
        init_src = inspect.getsource(InsightsRecorder.__init__)
        assert "_warned_unknown_decision" in init_src
        gv_src = inspect.getsource(InsightsRecorder.record_gate_verdict)
        assert "_warned_unknown_decision" in gv_src
        assert "_warned_unknown_reason" not in gv_src

    def test_dual_write_backend_keyword_only_signature(self):
        from crucible.features.run_insights.backends import DualWriteBackend, make_backend
        params = inspect.signature(DualWriteBackend.__init__).parameters
        for kw in ("root", "api_url", "api_token", "inline_max_bytes"):
            assert kw in params
        with pytest.raises(NotImplementedError):
            make_backend("dual", root="x", api_url="http://h", api_token="t")


# ───────────────────────────── F-G: web research + infra ────────────────────

class TestFGWebResearch:
    def test_grep_app_dropped_from_default_code_chain(self):
        from crucible.web_research import fallback
        assert "grep_app" not in fallback._DEFAULT_CHAIN_BY_CLASS["code"]
        assert "grep_app" in fallback._CORE_PROVIDERS  # still resolvable on opt-in

    def test_searxng_default_instances_empty(self):
        from crucible.web_research.providers import searxng
        assert searxng._DEFAULT_INSTANCES == []

    def test_searxng_resolve_drops_non_https(self, tmp_path, monkeypatch):
        import json as _json
        from crucible.web_research.providers import searxng
        pins = tmp_path / "pins.json"
        pins.write_text(_json.dumps({"searxng_instances": [
            "https://ok.example.com",
            "http://insecure.example.com",
            "file:///etc/passwd",
            "ftp://x",
        ]}), encoding="utf-8")
        monkeypatch.setenv("LIBRARIAN_DOMAIN_PINS_PATH", str(pins))
        assert searxng._resolve_instances() == ["https://ok.example.com"]

    def test_http_retry_ssrf_predicate_wired(self):
        from crucible import http_retry
        assert http_retry._is_public_http_url("https://example.com") is True
        assert http_retry._is_public_http_url("http://169.254.169.254/latest/") is False
        assert http_retry._is_public_http_url("http://127.0.0.1/x") is False
        src = inspect.getsource(http_retry)
        assert src.count("follow_redirects=False") >= 2
        assert "follow_redirects=True" not in src


class TestFGAtomicIo:
    def test_atomic_write_text_has_newline_kwarg(self):
        from crucible import _atomic_io
        assert "newline" in inspect.signature(_atomic_io.atomic_write_text).parameters

    @pytest.mark.parametrize("relpath", [
        ("crucible", "features", "checkpoint.py"),
        ("crucible", "features", "agent_metrics.py"),
        ("crucible", "features", "citation_verifier.py"),
        ("crucible", "features", "api_version_autopatch.py"),
        ("crucible", "features", "auth_manager.py"),
        ("crucible", "features", "celery_worker.py"),
        ("crucible", "features", "alt_data_connectors.py"),
        ("crucible", "modules", "section_00_bootstrap_and_utils.py"),
    ])
    def test_writer_migrated_to_atomic_write_text(self, relpath):
        src = _read(*relpath)
        assert "atomic_write_text(" in src, f"{relpath[-1]} not migrated"

    def test_transaction_cost_annotates_synthesised_signals(self):
        src = _read("crucible", "features", "transaction_cost_model.py")
        assert "SYNTHESISED" in src
        assert "result.warnings.append" in src


# ───────────────────────────── F-D: backend webui ───────────────────────────

class TestFDBackend:
    def test_secret_env_masking(self):
        from webui import app as wa
        masked = wa._mask_secret_env({
            "OPENROUTER_API_KEY": "sk-secret-123",
            "WEBHOOK_SECRET": "hmac",
            "CRUCIBLE_LOG_LEVEL": "INFO",
            "EMPTY_KEY_API_KEY": "",
        })
        assert masked["OPENROUTER_API_KEY"] == wa._SECRET_VALUE_MASK
        assert masked["WEBHOOK_SECRET"] == wa._SECRET_VALUE_MASK
        assert masked["CRUCIBLE_LOG_LEVEL"] == "INFO"   # non-secret untouched
        assert masked["EMPTY_KEY_API_KEY"] == ""        # empty stays empty

    def test_is_secret_env_key(self):
        from webui import app as wa
        assert wa._is_secret_env_key("OPENROUTER_API_KEY")
        assert wa._is_secret_env_key("WEBHOOK_SECRET")
        assert not wa._is_secret_env_key("CRUCIBLE_LOG_LEVEL")

    def test_env_key_allowlist_and_denylist(self):
        from webui import app as wa
        assert wa._ENV_KEY_NAME_RE.match("OPENROUTER_API_KEY")
        assert not wa._ENV_KEY_NAME_RE.match("lowercase")
        assert not wa._ENV_KEY_NAME_RE.match("1LEADINGDIGIT")
        for hijack in ("PATH", "PYTHONPATH", "LD_PRELOAD", "LD_LIBRARY_PATH"):
            assert hijack in wa._ENV_KEY_DENYLIST

    def test_post_env_handler_filters_sentinel_and_validates_keys(self):
        from webui import app as wa
        src = inspect.getsource(wa.api_set_env)
        assert "_ENV_KEY_NAME_RE.match(k)" in src
        assert "_ENV_KEY_DENYLIST" in src
        assert "_SECRET_VALUE_MASK" in src

    def test_get_env_handler_masks(self):
        from webui import app as wa
        assert "_mask_secret_env(_load_env())" in inspect.getsource(wa.api_get_env)

    def test_process_tree_termination_wired(self):
        from webui import app as wa
        assert hasattr(wa, "_terminate_process_tree")
        worker_src = inspect.getsource(wa._run_worker)
        assert "start_new_session" in worker_src
        assert "CREATE_NEW_PROCESS_GROUP" in worker_src
        kill_src = inspect.getsource(wa.api_kill_run)
        assert "_terminate_process_tree(proc_to_kill)" in kill_src

    def test_ab_tests_and_streamer_eviction_wired(self):
        from webui import app as wa
        evict_src = inspect.getsource(wa._evict_stale_runs)
        assert "_ab_tests" in evict_src        # F-D4: AB records swept
        assert "_active_streamers" in evict_src  # F-D7: streamer-aware
        worker_src = inspect.getsource(wa._run_worker)
        assert "[WEBUI ERROR]" in worker_src
        # the WEBUI ERROR / stdin-warn lines must now route through the redactor
        assert worker_src.count("_redact_for_client") >= 2

    def test_run_id_width_widened(self):
        from webui import app as wa
        src = inspect.getsource(wa)
        assert "run_id = uuid.uuid4().hex[:12]" in src
        assert "run_id = uuid.uuid4().hex[:8]" not in src
        assert "run_id_a = uuid.uuid4().hex[:12]" in src


# ───────────────────────────── F-C: settings sync ───────────────────────────

class TestFCSettingsSync:
    def test_backtest_keys_in_settings_schema_and_keymeta(self):
        assert "'BACKTEST_PARAM_SEED'" in _APP_JS
        assert "'BACKTEST_FETCH_HARD_TIMEOUT_SEC'" in _APP_JS
        assert "BACKTEST_PARAM_SEED:" in _APP_JS
        assert "BACKTEST_FETCH_HARD_TIMEOUT_SEC:" in _APP_JS

    def test_librarian_keys_uncommented_in_env_example(self):
        for key in (
            "LIBRARIAN_MAX_RESULTS_PER_QUERY",
            "LIBRARIAN_MAX_CITATIONS",
            "LIBRARIAN_MAX_QUERIES_PER_LANE",
            "LIBRARIAN_HTTP_TIMEOUT_SECONDS",
            "LIBRARIAN_HTTP_MAX_BYTES",
            "LIBRARIAN_MAX_VERIFIED_CITATIONS",
        ):
            assert re.search(rf"^{key}=", _ENV_EXAMPLE, re.MULTILINE), key
        assert re.search(r"^BACKTEST_PARAM_SEARCH=grid", _ENV_EXAMPLE, re.MULTILINE)
        assert re.search(r"^BACKTEST_BAYESIAN_N_TRIALS=30", _ENV_EXAMPLE, re.MULTILINE)

    def test_keymeta_all_bilingual(self):
        s = _APP_JS.index("const KEY_META = {")
        e = s + re.search(r"\n};", _APP_JS[s:]).start()
        block = _APP_JS[s:e]
        assert len(re.findall(r"desc:\s*'", block)) == 0, "all KEY_META must be bilingual"


# ───────────────────────────── F-A / F-B: frontend ──────────────────────────

class TestFAFrontendBehaviour:
    def test_run_button_gate(self):
        assert "_modeHasLiveSession" in _APP_JS
        assert "_runBtn.disabled = _modeHasLiveSession" in _APP_JS
        # the old immediate re-enable after run_id is gone
        assert "sess.run_id = data.run_id;\n    runBtn.disabled = false;" not in _APP_JS

    def test_no_blocking_alert_validation(self):
        assert "alert('Please enter a project path.')" not in _APP_JS
        assert "_markFieldInvalid" in _APP_JS

    def test_status_labels_centralised(self):
        assert "_STATUS_LABELS" in _APP_JS and "function _statusLabel" in _APP_JS
        assert "`${sess.status}  ·  ${elapsed}`" not in _APP_JS

    def test_compare_stale_guard(self):
        assert "_compareToken" in _APP_JS
        assert "_token !== State._compareToken" in _APP_JS

    def test_dashboard_and_settings_error_states(self):
        assert "Failed to load dashboard" in _APP_JS
        assert "getElementById('settings-container')" in _APP_JS

    def test_destructive_confirms(self):
        assert "Clear the terminal output for this session?" in _APP_JS
        assert "Stop this run?" in _APP_JS
        assert "Close this session?" in _APP_JS

    def test_domain_badge_null_guard(self):
        assert "if (_domainBadge) _domainBadge.textContent" in _APP_JS


class TestFBAccessibilityRwd:
    def test_responsive_breakpoints_present(self):
        assert "@media (max-width: 900px)" in _APP_CSS
        assert "@media (max-width: 600px)" in _APP_CSS
        assert ".mobile-nav-toggle" in _APP_CSS
        assert ".run-pill" in _APP_CSS

    def test_sidebar_toggle_wired(self):
        assert "function toggleSidebar" in _APP_JS
        assert 'onclick="toggleSidebar()"' in _INDEX_HTML
        assert 'id="primary-sidebar"' in _INDEX_HTML
        assert 'id="sidebar-scrim"' in _INDEX_HTML

    def test_modal_dialog_semantics(self):
        assert 'role="dialog"' in _INDEX_HTML
        assert 'aria-modal="true"' in _INDEX_HTML
        assert "function _trapModalTab" in _APP_JS

    def test_toast_live_region(self):
        assert 'id="toast-region"' in _INDEX_HTML
        assert 'aria-live="assertive"' in _INDEX_HTML

    def test_global_run_pill(self):
        assert 'id="global-run-pill"' in _INDEX_HTML
        assert "function _updateGlobalRunPill" in _APP_JS

    def test_terminal_log_role(self):
        assert _INDEX_HTML.count('role="log"') >= 2

    def test_lang_buttons_have_data_lang(self):
        assert 'id="lang-btn-en" data-lang="en"' in _INDEX_HTML
        assert 'id="lang-btn-zh" data-lang="zh"' in _INDEX_HTML

    def test_label_for_associations(self):
        assert 'for="project-path"' in _INDEX_HTML
        assert 'for="idea-text"' in _INDEX_HTML
        assert 'for="ab-mode-a"' in _INDEX_HTML

    def test_tooltip_keyboard_parity(self):
        assert "focusin" in _APP_JS and "focusout" in _APP_JS
        assert 'class="tip-icon" tabindex="0"' in _APP_JS
