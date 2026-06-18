# Auto-generated section module — do not edit manually.
# Regenerate via ``python -m crucible.generate``.
from __future__ import annotations

from . import section_00_bootstrap_and_utils as _prev_00

globals().update({k: v for k, v in _prev_00.__dict__.items() if not k.startswith("__")})
from . import section_01_extraction_and_reformat as _prev_01

globals().update({k: v for k, v in _prev_01.__dict__.items() if not k.startswith("__")})
from . import section_02_research_and_llm as _prev_02

globals().update({k: v for k, v in _prev_02.__dict__.items() if not k.startswith("__")})
from . import section_03_models_and_context as _prev_03

globals().update({k: v for k, v in _prev_03.__dict__.items() if not k.startswith("__")})
from . import section_04_web_research_and_direction as _prev_04

globals().update({k: v for k, v in _prev_04.__dict__.items() if not k.startswith("__")})
from . import section_05_analysis_and_codegen as _prev_05

globals().update({k: v for k, v in _prev_05.__dict__.items() if not k.startswith("__")})
from . import section_06_runtime_quality_api as _prev_06

globals().update({k: v for k, v in _prev_06.__dict__.items() if not k.startswith("__")})
if __package__ == "crucible.modules":
    from ..resilience import kickoff_crew_with_retry, reset_circuit_breakers
    from ..runtime_logging import (
        clear_log_context,
        configure_logging,
        get_logger,
        log_event,
        log_exception,
        update_log_context,
    )
    from ..run_correlation import get_run_id as _get_run_id, set_run_id as _set_run_id
    from ..features.run_insights import get_recorder as _get_insights_recorder
    from ..features.run_insights.schema import OutcomeStatus as _InsightOutcome
else:  # pragma: no cover - direct script fallback
    from resilience import kickoff_crew_with_retry, reset_circuit_breakers
    from runtime_logging import (
        clear_log_context,
        configure_logging,
        get_logger,
        log_event,
        log_exception,
        update_log_context,
    )
    from run_correlation import (  # type: ignore[no-redef]
        get_run_id as _get_run_id,
        set_run_id as _set_run_id,
    )
    from features.run_insights import get_recorder as _get_insights_recorder  # type: ignore[no-redef]
    from features.run_insights.schema import OutcomeStatus as _InsightOutcome  # type: ignore[no-redef]


LOGGER = get_logger(__name__)

import io as _io  # noqa: E402 — stdlib, placed after package imports intentionally
import math  # noqa: E402 — stdlib, used by the v1.1.2 sixth-pass M-7 outcome_score finite gate


def _atomic_write_text(path: str, content: str, encoding: str = "utf-8") -> None:
    """Write *content* to *path* atomically via a sibling .tmp file.

    Delegates to :func:`crucible._atomic_io.atomic_write_text` (v1.1.9 H1)
    so the POSIX parent-directory fsync that durably commits the rename
    is applied uniformly to every final-stage artefact written by
    section 07 (final_output.json, quality_report.json, gate_decision.json,
    README.md, etc.).  Without that fsync, a power loss after ``os.replace``
    can leave the new file content on disk while the directory entry still
    points at the old inode.
    """
    try:
        from .._atomic_io import atomic_write_text as _aw
    except ImportError:  # flat-launcher mode (``python crucible/__main__.py``)
        from _atomic_io import atomic_write_text as _aw  # type: ignore[no-redef]
    _aw(path, content, encoding=encoding)


def sanitize_name(name: str) -> str:
    """Return a filesystem-safe name, collapsing runs of separators to '_'.

    Replaces the earlier one-liner that simply stripped non-alnum chars —
    "my project" → "myproject" — with a version that produces more readable
    output: "my project" → "my_project".  The second (identical-signature)
    definition that used to appear near the bottom of this module has been
    removed to avoid silent shadowing.
    """
    safe_chars: List[str] = []
    pending_sep = False
    for char in str(name or ""):
        if char.isalnum():
            if pending_sep and safe_chars and safe_chars[-1] != "_":
                safe_chars.append("_")
            safe_chars.append(char)
            pending_sep = False
        elif char in (" ", "-", "_"):
            pending_sep = True
    safe = "".join(safe_chars).strip("_")
    return safe or "project"


# =========================
# Localization Labels
# =========================

_README_LABELS_EN = {
    "summary": "Summary",
    "consensus": "Consensus",
    "disagreement": "Disagreement",
    "experiments": "Experiments",
    "goal": "Goal",
    "criteria": "Criteria",
    "quality_review": "Quality Review",
    "issues": "Issues",
    "run_metadata": "Run Metadata",
    "dependency_versions": "Dependency Versions",
    "no_analysis_report": "(No analysis report was produced; code output only.)",
    "date": "Date",
    "score": "Score",
    "mode": "Mode",
    "risk": "Risk",
    "quality_passed": "Quality Passed",
    # Gate decision section
    "yes": "Yes",
    "no": "No",
    "ready_for_codegen": "Ready for Code Generation",
    "codegen_scope": "Code Generation Scope",
    "confidence": "Confidence",
    "gate_fallback_notice": "No analysis report available; gate decision shown only.",
    "gate_decision": "Gate Decision",
    "blocking_risks": "Blocking Risks",
    "required_before_codegen": "Required Before Code Generation",
    "advisory_after_codegen": "Advisory After Code Generation",
    "validation_scope_reason": "Validation Scope Reason",
    "validation_objectives": "Validation Objectives",
    "kill_reason": "Kill Reason",
    # v1.0.5: failure-prominent banner shown above the body when quality_pass=False
    "failure_banner_title": "Quality review did NOT pass",
    "failure_banner_body": (
        "This bundle still has unresolved high/medium issues from the quality "
        "review loop. Running it as-is is likely to crash or produce incorrect "
        "results. Read the issues below (or `review_report.json`) and fix them "
        "before treating this output as deliverable."
    ),
    "failure_banner_giveup_extra": (
        "The quality loop hit early-stop stagnation — issue counts stopped "
        "improving across consecutive rounds, so the loop bailed without "
        "converging. The bundle is in a NOT-production-ready state."
    ),
}

_README_LABELS_ZH = {
    "summary": "摘要",
    "consensus": "共識",
    "disagreement": "分歧",
    "experiments": "實驗計畫",
    "goal": "目標",
    "criteria": "驗證標準",
    "quality_review": "品質審查",
    "issues": "問題",
    "run_metadata": "執行元數據",
    "dependency_versions": "依賴版本",
    "no_analysis_report": "（未產生分析報告；僅輸出程式碼。）",
    "date": "日期",
    "score": "評分",
    "mode": "模式",
    "risk": "風險",
    "quality_passed": "品質通過",
    # 閘門決策區塊
    "yes": "是",
    "no": "否",
    "ready_for_codegen": "可產生程式碼",
    "codegen_scope": "程式碼生成範圍",
    "confidence": "信心度",
    "gate_fallback_notice": "無分析報告；僅顯示閘門決策。",
    "gate_decision": "閘門決策",
    "blocking_risks": "阻止性風險",
    "required_before_codegen": "程式碼生成前必需項目",
    "advisory_after_codegen": "程式碼生成後建議事項",
    "validation_scope_reason": "驗證範圍原因",
    "validation_objectives": "驗證目標",
    "kill_reason": "終止原因",
    # v1.0.5: 品質審查未通過時於正文上方顯示的橫幅
    "failure_banner_title": "品質審查未通過",
    "failure_banner_body": (
        "本批產出仍有未解決的 high/medium 問題。直接執行可能會 crash 或產出"
        "錯誤結果。請先讀完下方的問題清單（或 `review_report.json`），修好"
        "之後才能視為可交付。"
    ),
    "failure_banner_giveup_extra": (
        "品質迴圈觸發 stagnation 早停 — 連續多輪問題數沒有下降，迴圈未"
        "收斂就退出。此份產出屬於「未達生產就緒」狀態。"
    ),
}

_PRINT_LABELS_EN = {
    "direction_not_approved": "Direction not approved; stopping downstream pipeline",
    "direction_decision_not_parsed": "[Error] DirectionDecision not parsed.",
    "project_saved_to": "[System] Project saved to:",
}

_PRINT_LABELS_ZH = {
    "direction_not_approved": "方向未獲批准；停止後續流程",
    "direction_decision_not_parsed": "[錯誤] DirectionDecision 無法解析。",
    "project_saved_to": "[系統] 專案已儲存至：",
}


def _get_readme_labels(use_cjk: bool) -> Dict[str, str]:
    return _README_LABELS_ZH if use_cjk else _README_LABELS_EN


def _get_print_labels(use_cjk: bool) -> Dict[str, str]:
    return _PRINT_LABELS_ZH if use_cjk else _PRINT_LABELS_EN


def _reset_pipeline_runtime_state() -> None:
    # Main can be called repeatedly inside one process via module_runtime; clear prior run state.
    clear_openrouter_usage()
    reset_openrouter_billed_ledger()  # v1.1.12 — authoritative billed-cost ledger (per-run)
    reset_llm_usage_dedup()  # v1.2.3 — per-run response-dedup set (shared by both capture paths)
    ensure_crewai_usage_listener_registered()  # v1.2.3 — PRIMARY capture (native OpenAICompletion)
    ensure_litellm_usage_logger_registered()  # v1.2.3 — fallback capture (direct LiteLLM path)
    reset_cost_accountant()
    reset_circuit_breakers()
    clear_last_librarian_debug()
    reset_research_llm_cache()
    reset_local_llm_cache()
    reset_api_version_cache()
    clear_log_context()


def _reconcile_cost_summary_with_billing(summary: Any) -> Any:
    """Override the headline ``total_cost_usd`` with the authoritative
    OpenRouter billed-cost ledger (v1.1.12).

    The accountant total is reconstructed from the lossy per-stage
    ``_record_cost`` dance (several ``crew.kickoff()`` sites have no matching
    ``_record_cost``) and blends actual ``openrouter_api`` rows with
    locally-estimated ones while labelling the whole thing "actual billing".
    When the billed-cost ledger captured real OpenRouter ``usage.cost`` values
    this run, its sum is the exact amount OpenRouter billed, so we promote it to
    the headline ``total_cost_usd`` and scale the input/output/cache breakdown to
    match.  The pre-existing per-stage figure is preserved as
    ``total_cost_usd_attributed`` for diagnostics / reconciliation.

    Returns ``summary`` unchanged when no actual OpenRouter billing was captured
    (Alibaba coding-plan runs, fully-estimated runs, or unit tests that never
    feed the interceptor), so prior behaviour is preserved bit-for-bit.
    """
    if not isinstance(summary, dict):
        return summary
    try:
        billed_total = float(get_openrouter_billed_total())
        billed_count = int(get_openrouter_billed_count())
    except Exception:
        # Names unavailable (e.g. helper invoked before namespace wiring) or any
        # other failure: never let cost reconciliation break the run.
        return summary
    if billed_count <= 0 or not (billed_total > 0.0):
        return summary
    reconciled = dict(summary)
    try:
        attributed = float(reconciled.get("total_cost_usd", 0.0) or 0.0)
    except (TypeError, ValueError):
        attributed = 0.0
    # Scale the per-direction breakdown to the authoritative total so the parts
    # still sum to the headline; skip when there is nothing to scale against.
    if attributed > 0.0:
        scale = billed_total / attributed
        for _key in ("input_cost_usd", "output_cost_usd", "cache_cost_usd"):
            try:
                reconciled[_key] = float(reconciled.get(_key, 0.0) or 0.0) * scale
            except (TypeError, ValueError):
                pass
    reconciled["total_cost_usd"] = billed_total
    reconciled["total_cost_usd_billed"] = billed_total
    reconciled["total_cost_usd_attributed"] = attributed
    reconciled["billed_request_count"] = billed_count
    reconciled["cost_source"] = "openrouter_api"

    # v1.2.3 — also promote the authoritative TOKEN totals from the billed-cost
    # ledger to the headline, mirroring the USD promotion above.  Every billed
    # OpenRouter response contributes exactly one ledger row, so these equal the
    # dashboard's token counts.  Without this, ``total_tokens`` kept coming from
    # the per-stage accountant — which is why tokens could still read multiples of
    # the real usage even after the USD headline was corrected in v1.1.12.  The
    # pre-promotion figure is preserved as ``total_tokens_attributed``.
    try:
        billed_tokens = get_openrouter_billed_tokens()
    except Exception:
        billed_tokens = {}
    billed_total_tokens = int(billed_tokens.get("total_tokens", 0) or 0)
    if billed_total_tokens > 0:
        try:
            attributed_tokens = int(reconciled.get("total_tokens", 0) or 0)
        except (TypeError, ValueError):
            attributed_tokens = 0
        reconciled["total_tokens"] = billed_total_tokens
        reconciled["total_tokens_billed"] = billed_total_tokens
        reconciled["total_tokens_attributed"] = attributed_tokens
        reconciled["total_input_tokens"] = int(billed_tokens.get("input_tokens", 0) or 0)
        reconciled["total_output_tokens"] = int(billed_tokens.get("output_tokens", 0) or 0)
        reconciled["cached_tokens"] = int(billed_tokens.get("cached_tokens", 0) or 0)
        reconciled["reasoning_tokens"] = int(billed_tokens.get("reasoning_tokens", 0) or 0)
    return reconciled


def _sync_librarian_debug_snapshot(run_snapshot: "RunSnapshot") -> None:
    debug_payload = get_last_librarian_debug()
    if debug_payload:
        run_snapshot.outputs["librarian_research"] = debug_payload
        if debug_payload.get("search_strategy"):
            run_snapshot.inputs["librarian_search_strategy"] = debug_payload.get("search_strategy")
        if debug_payload.get("cache_hit") is not None:
            run_snapshot.inputs["librarian_cache_hit"] = bool(debug_payload.get("cache_hit"))
        return

    run_snapshot.outputs.pop("librarian_research", None)
    run_snapshot.inputs.pop("librarian_search_strategy", None)
    run_snapshot.inputs.pop("librarian_cache_hit", None)


def _resolve_runtime_model_versions(llm: Any) -> Dict[str, str]:
    resolved_provider = _resolve_llm_provider()
    return {
        "llm_provider": resolved_provider,
        "primary": _llm_model_id(llm) or _resolve_primary_model_id(),
        "direction_judge": _resolve_direction_judge_model_id(),
        "librarian": _resolve_librarian_model_id() if LIBRARIAN_ENABLED else "",
    }


def _apply_llm_provider_runtime(provider: Optional[str] = None) -> str:
    resolved_provider = _resolve_llm_provider(provider)
    target_modules = (_prev_00, _prev_01, _prev_02, _prev_03, _prev_04, _prev_05, _prev_06)
    for module in target_modules:
        module.__dict__["LLM_PROVIDER"] = resolved_provider
        module.__dict__["ACTIVE_LLM_PROVIDER"] = resolved_provider
    globals()["LLM_PROVIDER"] = resolved_provider
    globals()["ACTIVE_LLM_PROVIDER"] = resolved_provider
    clear_openrouter_usage()
    return resolved_provider


def _prompt_for_llm_provider() -> str:
    while True:
        provider_input = input(
            "Select LLM Provider (1: OpenRouter, 2: Alibaba Coding Plan) [Default: 1]: "
        ).strip()
        if provider_input in ("", "1"):
            return _apply_llm_provider_runtime(LLM_PROVIDER_OPENROUTER)
        if provider_input == "2":
            return _apply_llm_provider_runtime(LLM_PROVIDER_ALIBABA_CODING_PLAN)
        print("Invalid selection. Please try again.")


def _resolve_entry_llm_provider(
    cli_provider: Optional[str],
    *,
    allow_interactive_prompt: bool,
) -> str:
    provider_from_cli = str(cli_provider or "").strip() or None
    provider_from_env = str(os.environ.get("LLM_PROVIDER") or "").strip() or None
    if provider_from_cli:
        return _apply_llm_provider_runtime(provider_from_cli)
    if provider_from_env:
        return _apply_llm_provider_runtime(provider_from_env)
    if allow_interactive_prompt:
        return _prompt_for_llm_provider()
    return _apply_llm_provider_runtime(DEFAULT_LLM_PROVIDER)


def _apply_runtime_option_overrides(
    *,
    strict_json: Optional[bool] = None,
    local_cache: Optional[bool] = None,
    cost_trace: Optional[bool] = None,
) -> None:
    target_modules = (_prev_00, _prev_01, _prev_02, _prev_03, _prev_04, _prev_05, _prev_06)

    for module in target_modules:
        if strict_json is not None:
            module.__dict__["STRICT_JSON_ENABLED"] = bool(strict_json)
        if local_cache is not None:
            module.__dict__["LOCAL_CACHE_ENABLED"] = bool(local_cache)
        if cost_trace is not None:
            module.__dict__["COST_TRACE_ENABLED"] = bool(cost_trace)

    if strict_json is not None:
        globals()["STRICT_JSON_ENABLED"] = bool(strict_json)
    if local_cache is not None:
        globals()["LOCAL_CACHE_ENABLED"] = bool(local_cache)
    if cost_trace is not None:
        globals()["COST_TRACE_ENABLED"] = bool(cost_trace)


def _reset_runtime_option_defaults_from_env() -> None:
    _apply_runtime_option_overrides(
        strict_json=_env_bool("STRICT_JSON", False),
        local_cache=_env_bool("LOCAL_CACHE", False),
        cost_trace=_env_bool("COST_TRACE", False),
    )


def _resolve_runtime_entry_defaults() -> Dict[str, Any]:
    return {
        "runtime_profile": _resolve_runtime_profile_default_name(),
        "budget_soft_cost": _env_float("BUDGET_SOFT_COST_LIMIT", None),
        "budget_hard_cost": _env_float("BUDGET_HARD_COST_LIMIT", None),
        "budget_max_tokens": _env_int("BUDGET_MAX_TOTAL_TOKENS", None),
        "api_version_check_enabled": _resolve_api_version_check_enabled_default(),
    }


def _apply_librarian_runtime_defaults_from_env() -> None:
    target_modules = (_prev_00, _prev_01, _prev_02, _prev_03, _prev_04, _prev_05, _prev_06)
    defaults = _resolve_librarian_runtime_defaults()
    mapping = {
        "LIBRARIAN_ENABLED": bool(defaults["enabled"]),
        "LIBRARIAN_SEARCH_PROVIDERS": list(defaults["search_providers"]),
        "LIBRARIAN_MAX_RESULTS_PER_QUERY": int(defaults["max_results_per_query"]),
        "LIBRARIAN_MAX_CITATIONS": int(defaults["max_citations"]),
        "LIBRARIAN_MAX_QUERIES_PER_LANE": int(defaults["max_queries_per_lane"]),
        "LIBRARIAN_CACHE_WINDOW_HOURS": defaults["cache_window_hours"],
        "LIBRARIAN_HTTP_TIMEOUT_SECONDS": float(defaults["http_timeout_seconds"]),
        "LIBRARIAN_HTTP_MAX_BYTES": int(defaults["http_max_bytes"]),
        "LIBRARIAN_VERIFY_CITATIONS": bool(defaults["verify_citations"]),
        "LIBRARIAN_MAX_VERIFIED_CITATIONS": int(defaults["max_verified_citations"]),
    }
    for module in target_modules:
        for key, value in mapping.items():
            if key in module.__dict__:
                module.__dict__[key] = list(value) if key == "LIBRARIAN_SEARCH_PROVIDERS" else value
    for key, value in mapping.items():
        globals()[key] = list(value) if key == "LIBRARIAN_SEARCH_PROVIDERS" else value


def _apply_research_runtime_defaults_from_env() -> None:
    target_modules = (_prev_02,)
    direction_defaults = _resolve_direction_refinement_runtime_defaults()
    mapping = {
        "DIRECTION_REFINEMENT_MAX_ITERATIONS": int(direction_defaults["max_iterations"]),
        "DIRECTION_REFINEMENT_ENABLED": bool(direction_defaults["enabled"]),
        "OPENROUTER_LLM_TIMEOUT_SECONDS": int(_resolve_openrouter_llm_timeout_seconds()),
        "ALIBABA_CODING_PLAN_LLM_TIMEOUT_SECONDS": int(
            _resolve_alibaba_coding_plan_llm_timeout_seconds()
        ),
        "ALIBABA_CODING_PLAN_INITIAL_RESPONSE_TIMEOUT_SECONDS": int(
            _resolve_alibaba_coding_plan_initial_response_timeout_seconds()
        ),
    }
    for module in target_modules:
        for key, value in mapping.items():
            if key in module.__dict__:
                module.__dict__[key] = value
    for key, value in mapping.items():
        globals()[key] = value


def _apply_local_cache_runtime_defaults_from_env() -> None:
    target_modules = (_prev_02,)
    defaults = _resolve_local_cache_runtime_defaults()
    mapping = {
        "LOCAL_CACHE_TTL_HOURS": defaults["ttl_hours"],
        "LOCAL_CACHE_PATH": defaults["path"],
    }
    for module in target_modules:
        for key, value in mapping.items():
            if key in module.__dict__:
                module.__dict__[key] = value
    for key, value in mapping.items():
        globals()[key] = value
    reset_local_llm_cache()


def _apply_api_version_check_runtime_defaults_from_env() -> None:
    target_modules = (_prev_05, _prev_06)
    defaults = _resolve_api_version_check_runtime_defaults()
    mapping = {
        "API_VERSION_CHECK_ENABLED": bool(defaults["enabled"]),
        "API_VERSION_CHECK_MAX_LIBRARIES": int(defaults["max_libraries"]),
        "API_VERSION_CHECK_TIMEOUT_SECONDS": int(defaults["timeout_seconds"]),
        "API_VERSION_CHECK_CACHE_TTL_HOURS": int(defaults["cache_ttl_hours"]),
        "API_VERSION_CHECK_SEVERITY_THRESHOLD": str(defaults["severity_threshold"]),
    }
    for module in target_modules:
        for key, value in mapping.items():
            if key in module.__dict__:
                module.__dict__[key] = value
    for key, value in mapping.items():
        globals()[key] = value


def _apply_quality_runtime_defaults_from_env() -> None:
    target_modules = (_prev_05, _prev_06)
    defaults = _resolve_quality_runtime_defaults()
    mapping = {
        "QUALITY_MAX_ROUNDS": int(defaults["max_rounds"]),
        "QUALITY_CONTEXT_MAX_CHARS": int(defaults["context_max_chars"]),
        "QUALITY_CODE_BUNDLE_MAX_CHARS": defaults["code_bundle_max_chars"],
        "QUALITY_RUNTIME_LOG_MAX_CHARS": defaults["runtime_log_max_chars"],
        "QUALITY_JSON_RETRY_ATTEMPTS": int(defaults["json_retry_attempts"]),
        "QUALITY_CODE_FILE_MAX_CHARS": defaults["code_file_max_chars"],
        "QUALITY_CODE_FILE_MAX_CHARS_ENTRYPOINT": defaults["code_file_max_chars_entrypoint"],
        "QUALITY_CODE_FILE_MAX_CHARS_SCOPED": defaults["code_file_max_chars_scoped"],
        "QUALITY_CODE_FILE_MAX_CHARS_PRIORITY": defaults["code_file_max_chars_priority"],
        "QUALITY_CODE_SNIPPET_HEAD_CHARS": defaults["code_snippet_head_chars"],
        "QUALITY_CODE_SNIPPET_TAIL_CHARS": defaults["code_snippet_tail_chars"],
        "QUALITY_CONTEXT_TREE_MAX_CHARS": defaults["context_tree_max_chars"],
        "QUALITY_RUNTIME_LOG_TAIL_CHARS": defaults["runtime_log_tail_chars"],
        "QUALITY_MAX_FILES_WITH_CONTENT_ROUND0": defaults["max_files_with_content_round0"],
        "QUALITY_MAX_FILES_WITH_CONTENT_ROUNDN": defaults["max_files_with_content_roundn"],
        "QUALITY_EARLY_STOP_STAGNATION_ROUNDS": defaults["early_stop_stagnation_rounds"],
        "QUALITY_FIX_FUSE_CONSECUTIVE_FAILURES": int(
            defaults["fix_fuse_consecutive_failures"]
        ),
    }
    for module in target_modules:
        for key, value in mapping.items():
            if key in module.__dict__:
                module.__dict__[key] = value
    for key, value in mapping.items():
        globals()[key] = value


def _apply_project_context_scan_defaults_from_env() -> None:
    target_modules = (_prev_03,)
    defaults = _resolve_project_context_scan_defaults()
    mapping = {
        "QUICK_MAX_TREE_ENTRIES": defaults["quick_max_tree_entries"],
        "QUICK_MAX_DEPTH": defaults["quick_max_depth"],
        "QUICK_MAX_FILE_BYTES": defaults["quick_max_file_bytes"],
        "QUICK_MAX_SNIPPET_CHARS": defaults["quick_max_snippet_chars"],
        "FULL_MAX_TREE_ENTRIES": defaults["full_max_tree_entries"],
        "FULL_MAX_DEPTH": defaults["full_max_depth"],
        "FULL_MAX_FILE_BYTES": defaults["full_max_file_bytes"],
        "FULL_MAX_SNIPPET_CHARS": defaults["full_max_snippet_chars"],
        "FULL_MAX_TOTAL_CHARS": defaults["full_max_total_chars"],
    }
    for module in target_modules:
        for key, value in mapping.items():
            if key in module.__dict__:
                module.__dict__[key] = value
    for key, value in mapping.items():
        globals()[key] = value


def _resolve_quality_round_limit(runtime_profile: "RuntimeProfileConfig") -> int:
    # Use `is not None` guard (not falsy check) so that quality_max_rounds=0
    # ("disable quality loop") is honoured instead of being silently overridden
    # to the default value via falsy-zero short-circuit.
    if runtime_profile.quality_max_rounds is not None:
        return int(runtime_profile.quality_max_rounds)
    return _resolve_quality_max_rounds_default()


def _apply_output_validation_mode_overrides() -> None:
    target_modules = (_prev_00, _prev_01, _prev_02, _prev_03, _prev_04, _prev_05, _prev_06)
    base_crewai_output_pydantic = bool(_resolve_crewai_output_pydantic_enabled())

    for module in target_modules:
        if "CREWAI_OUTPUT_PYDANTIC" in module.__dict__:
            module.__dict__["CREWAI_OUTPUT_PYDANTIC"] = base_crewai_output_pydantic
        if "_STRICT_JSON_PYDANTIC_WARNED" in module.__dict__:
            module.__dict__["_STRICT_JSON_PYDANTIC_WARNED"] = False

    globals()["CREWAI_OUTPUT_PYDANTIC"] = base_crewai_output_pydantic
    globals()["_STRICT_JSON_PYDANTIC_WARNED"] = False

    _sync_output_validation_mode()

    crewai_output_pydantic = bool(
        _prev_02.__dict__.get(
            "CREWAI_OUTPUT_PYDANTIC",
            globals().get("CREWAI_OUTPUT_PYDANTIC", False),
        )
    )
    strict_json_pydantic_warned = bool(
        _prev_02.__dict__.get(
            "_STRICT_JSON_PYDANTIC_WARNED",
            globals().get("_STRICT_JSON_PYDANTIC_WARNED", False),
        )
    )

    for module in target_modules:
        if "CREWAI_OUTPUT_PYDANTIC" in module.__dict__:
            module.__dict__["CREWAI_OUTPUT_PYDANTIC"] = crewai_output_pydantic
        if "_STRICT_JSON_PYDANTIC_WARNED" in module.__dict__:
            module.__dict__["_STRICT_JSON_PYDANTIC_WARNED"] = strict_json_pydantic_warned

    globals()["CREWAI_OUTPUT_PYDANTIC"] = crewai_output_pydantic
    globals()["_STRICT_JSON_PYDANTIC_WARNED"] = strict_json_pydantic_warned


def _direction_debate_enabled_from_inputs(
    cli_direction_debate: bool,
    cli_direction_debate_only: bool,
    input_mode: str,
) -> bool:
    del input_mode
    return bool(cli_direction_debate or cli_direction_debate_only)


def _gate_feedback_enabled_from_env() -> bool:
    return _env_bool("GATE_DIRECTION_FEEDBACK_ENABLED", True)


def _effective_gate_feedback_enabled(
    gate_feedback_enabled: bool,
    gate_control_enabled: bool,
    selective_rerun_enabled: bool,
    input_mode: str,
) -> bool:
    return bool(
        gate_feedback_enabled
        and gate_control_enabled
        and selective_rerun_enabled
        and input_mode != "project_path"
    )


def build_direction_preamble(decision: DirectionDecision, use_cjk: bool) -> str:
    if use_cjk:
        header = "=== 方向決策摘要 ==="
        label_selected = "選定方向"
        label_backups = "備選方向"
        label_summary = "摘要"
        label_go = "進入條件"
        label_kill = "停止條件"
        empty_label = "無"
    else:
        header = "=== Direction Debate Summary ==="
        label_selected = "selected_direction"
        label_backups = "backup_candidates"
        label_summary = "summary"
        label_go = "go_conditions"
        label_kill = "kill_criteria"
        empty_label = "none"

    def _join_items(label: str, items: List[str]) -> str:
        if items:
            return f"{label}: " + "; ".join(items)
        return f"{label}: {empty_label}"

    lines = [
        header,
        f"{label_selected}: {decision.selected_direction}",
        _join_items(label_backups, list(getattr(decision, "backup_candidates", []) or [])),
        f"{label_summary}: {decision.summary}",
        _join_items(label_go, decision.go_conditions),
        _join_items(label_kill, decision.kill_criteria),
        "",
    ]
    return "\n".join(lines)


def run_self_check() -> bool:
    try:
        agent_mode = _get_mode_config("agent")
        if agent_mode.name != "Agent":
            return False
        if _project_type_for_mode("Agent") != "agent":
            return False
        if requires_web_validation(None, mode="Agent"):
            return False
        if not requires_web_validation(None, mode="SaaS"):
            return False
        gate_text = "\n".join(_mode_gate_controller_guidance(agent_mode)).lower()
        if "do not block code generation only because protocol demand" not in gate_text:
            return False
        if "headless" not in " ".join(_mode_codegen_rule_lines(agent_mode)).lower():
            return False
        sample_bundle = CodeBundle(
            project_type="agent",
            files=[GeneratedFile(path="main.py", content="print('ok')\n")],
        )
        sample_review = ReviewReport(
            passes=False,
            summary="missing entrypoint",
            issues=[
                ReviewIssue(
                    severity="high",
                    category="bug",
                    description="Web validation required but no entrypoints were detected.",
                    file=None,
                    suggestion="Expose a FastAPI/Flask entrypoint or set --entrypoint / CODEX_ENTRYPOINT.",
                )
            ],
        )
        if not _review_allows_new_files(sample_review, sample_bundle):
            return False
        affected = _collect_affected_files(
            ReviewReport(
                passes=False,
                summary="scope test",
                issues=[
                    ReviewIssue(
                        severity="high",
                        category="requirements",
                        description="Missing unit file",
                        file="N/A (missing file)",
                        suggestion="Add systemd unit file.",
                    ),
                    ReviewIssue(
                        severity="high",
                        category="bug",
                        description="Fix agent module",
                        file="main.py",
                        suggestion="Patch main.py.",
                    ),
                ],
            )
        )
        if affected != {"main.py"}:
            return False
        if (
            resolve_quality_runtime_validation_scope(input_mode="project_path", mode="Agent")
            != "static"
        ):
            return False
        if (
            resolve_quality_runtime_validation_scope(input_mode="project_path", mode="SaaS")
            != "static"
        ):
            return False
        if (
            resolve_quality_runtime_validation_scope(input_mode="project_path", mode="Quant")
            != "static"
        ):
            return False
        if (
            resolve_quality_runtime_validation_scope(
                input_mode="project_path", mode="Scientist"
            )
            != "static"
        ):
            return False
        runtime_ok, runtime_issues, runtime_log = run_runtime_validation(
            sample_bundle,
            user_problem="agent self-check",
            mode="Agent",
            validation_scope="static",
        )
        if not runtime_ok or runtime_issues:
            return False
        if "static validation scope active" not in runtime_log.lower():
            return False
        sample_gate = GateDecision(
            consensus="ok",
            disagreement="ok",
            experiments=[Experiment(goal="Validate later", criteria="Document the result")],
            ready_for_codegen=True,
            blocking_risks=[],
            required_experiments_before_codegen=[
                "Pick a concrete protocol adapter before production rollout"
            ],
            overall_score=70,
            confidence="medium",
        )
        sample_gate = _normalize_gate_decision(sample_gate, mode="Agent")
        if sample_gate is None:
            return False
        if sample_gate.required_experiments_before_codegen:
            return False
        if not sample_gate.advisory_experiments_after_codegen:
            return False
        if sample_gate.advisory_experiments_after_codegen[0] != (
            "Pick a concrete protocol adapter before production rollout"
        ):
            return False
        saas_gate = GateDecision(
            consensus="ok",
            disagreement="ok",
            experiments=[Experiment(goal="Interview users", criteria="3 calls done")],
            ready_for_codegen=True,
            blocking_risks=[],
            required_experiments_before_codegen=["Validate PMF assumptions"],
            overall_score=60,
            confidence="medium",
        )
        saas_gate = _normalize_gate_decision(saas_gate, mode="SaaS")
        if saas_gate is None:
            return False
        if saas_gate.required_experiments_before_codegen:
            return False
        if saas_gate.advisory_experiments_after_codegen != ["Validate PMF assumptions"]:
            return False
        blocking_gate = GateDecision(
            consensus="ok",
            disagreement="ok",
            experiments=[],
            ready_for_codegen=True,
            blocking_risks=["Critical security issue"],
            overall_score=20,
            confidence="medium",
        )
        blocking_gate = _normalize_gate_decision(blocking_gate, mode="Quant")
        if blocking_gate is None or blocking_gate.ready_for_codegen:
            return False
        blocking_skip, blocking_reason = should_skip_codegen(blocking_gate)
        if not blocking_skip or "Blocking risks:" not in blocking_reason:
            return False
        scientist_gate = GateDecision(
            consensus="ok",
            disagreement="ok",
            experiments=[
                Experiment(goal="Reproduce baseline accuracy", criteria="Within 1% of paper")
            ],
            ready_for_codegen=True,
            blocking_risks=[],
            required_experiments_before_codegen=["Verify deterministic seeding"],
            overall_score=75,
            confidence="medium",
        )
        scientist_gate = _normalize_gate_decision(scientist_gate, mode="Scientist")
        if scientist_gate is None:
            return False
        if scientist_gate.required_experiments_before_codegen:
            return False
        if scientist_gate.advisory_experiments_after_codegen != ["Verify deterministic seeding"]:
            return False
        merged = _merge_code_bundle_patch(
            sample_bundle,
            CodeBundle(
                project_type="agent",
                files=[
                    GeneratedFile(path="main.py", content="print('updated')\n"),
                    GeneratedFile(
                        path="risk-agent.service",
                        content="[Unit]\nDescription=Risk Agent\n",
                    ),
                ],
            ),
            allowed_files={"main.py"},
            allow_new_files=True,
        )
        merged_paths = {_normalize_bundle_relpath(f.path) for f in merged.files}
        if "risk-agent.service" not in merged_paths:
            return False
        if _code_bundle_effective_change_count(sample_bundle, merged) <= 0:
            return False
        if _code_bundle_effective_change_count(sample_bundle, sample_bundle) != 0:
            return False
        if not _bundle_has_files(sample_bundle):
            return False
        if _bundle_has_files(CodeBundle(project_type="agent", files=[])):
            return False
        sanitized_bundle = _sanitize_code_bundle(
            CodeBundle(
                project_type="agent",
                files=[
                    GeneratedFile(path="  ", content="ignored"),
                    GeneratedFile(path="code/main.py", content="v1"),
                    GeneratedFile(path="main.py", content="v2"),
                    GeneratedFile(path="./main.py", content="v3"),
                ],
            )
        )
        if not sanitized_bundle or len(sanitized_bundle.files) != 1:
            return False
        if sanitized_bundle.files[0].path != "main.py":
            return False
        if sanitized_bundle.files[0].content != "v3":
            return False
        if _is_safe_bundle_path_input("/etc/passwd"):
            return False
        if _is_safe_bundle_path_input("\\\\server\\share\\x.py"):
            return False
        if _is_safe_bundle_relpath("../evil.py"):
            return False
        if _is_safe_bundle_relpath("C:/abs.py"):
            return False
        unsafe_filtered = _sanitize_code_bundle(
            CodeBundle(
                project_type="agent",
                files=[
                    GeneratedFile(path="/etc/passwd", content="root"),
                    GeneratedFile(path="../evil.py", content="x"),
                    GeneratedFile(path="C:/abs.py", content="y"),
                    GeneratedFile(path="safe/main.py", content="z"),
                ],
            )
        )
        if not unsafe_filtered or len(unsafe_filtered.files) != 1:
            return False
        if unsafe_filtered.files[0].path != "safe/main.py":
            return False
        resolved_path, resolved_reason = _resolve_bundle_output_path(
            os.path.join(os.getcwd(), ".self_check_bundle_out"),
            '  "code\\\\nested\\\\main.py"  ',
        )
        if resolved_reason is not None or not resolved_path:
            return False
        if not resolved_path.replace("\\", "/").endswith("/nested/main.py"):
            return False
        if QUALITY_FIX_FUSE_CONSECUTIVE_FAILURES < 0:
            return False
        review_payload = {
            "passes": False,
            "summary": "Self-check review",
            "issues": [
                {
                    "severity": "high",
                    "category": "bug",
                    "description": "Missing import",
                    "file": "main.py",
                    "suggestion": "Add the required import.",
                }
            ],
        }
        extracted_review = extract_review_report(review_payload)
        if not extracted_review:
            return False
        if extracted_review.issues[0].file != "main.py":
            return False

        class _SelfCheckTaskOutput:
            text = "child raw"

        class _SelfCheckResult:
            raw = "parent raw"
            tasks_output = [_SelfCheckTaskOutput()]

        review_candidates = _collect_text_candidates_from_result(_SelfCheckResult())
        if review_candidates != ["parent raw", "child raw"]:
            return False
        options = [
            DirectionOption(
                key=key,
                name=f"Option {key}",
                thesis=f"Value thesis {key}",
                primary_metric=f"Metric {key}",
                fastest_test=f"Test {key}",
                major_risk=f"Risk {key}",
            )
            for key in _DIRECTION_OPTION_KEYS
        ]
        decision = DirectionDecision(
            selected_direction="A",
            summary="Self-check decision",
            options=options,
            go_conditions=["Condition A"],
            kill_criteria=["Kill A"],
            confidence="medium",
            verify_plan=["Verify A"],
        )
        _ = to_json_str(decision)
        payload = {
            "selected_direction": "B",
            "summary": "Extract test",
            "options": [
                {
                    "key": key,
                    "name": f"Option {key}",
                    "thesis": f"Value thesis {key}",
                    "primary_metric": f"Metric {key}",
                    "fastest_test": f"Test {key}",
                    "major_risk": f"Risk {key}",
                }
                for key in _DIRECTION_OPTION_KEYS
            ],
            "go_conditions": ["Condition B"],
            "kill_criteria": ["Kill B"],
            "confidence": "low",
            "verify_plan": ["Verify B"],
        }
        extracted = extract_direction_decision(payload)
        if not extracted:
            return False
        if extracted.selected_direction != payload["selected_direction"]:
            return False
        if not extracted.options or extracted.options[0].key != payload["options"][0]["key"]:
            return False
        return True
    except Exception:
        return False


def main() -> None:
    # v1.1.0: bind the run-correlation contextvar before any pipeline work
    # happens so every emit (telemetry, structured logs, run_insights ledger)
    # carries a consistent run_id.  When the WebUI spawned us, CRUCIBLE_RUN_ID
    # was already set; otherwise set_run_id() falls back to a fresh UUID4.
    # Idempotent: re-binding to the same env value is a no-op.  Without this
    # fallback, direct invocations of section_07's main() (`crucible.cli`
    # binds it as `main`) would leave _RUN_ID="" and ledger rows ungroupable.
    try:
        _set_run_id(os.environ.get("CRUCIBLE_RUN_ID") or None)
    except Exception:
        # Correlation-id binding must never break the pipeline boot.
        pass

    # v1.1.8 — Direction Debate Audit Mode contradiction detection.
    # If AUDIT_MODE=1 but the Run Insights ledger is disabled, there is
    # nowhere for the disagreement log to land — print a warning and
    # silently treat audit mode as disabled.  Other audit-mode env
    # combinations (e.g. AUDIT_MODE=1 + EXTERNAL_CRITIC=0) are valid
    # configurations and intentionally NOT checked here.
    try:
        if _env_bool("CRUCIBLE_DEBATE_AUDIT_MODE", False) and not _env_bool(
            "CRUCIBLE_RUN_INSIGHTS_ENABLED", True
        ):
            print(
                "[Warn] CRUCIBLE_DEBATE_AUDIT_MODE=1 but "
                "CRUCIBLE_RUN_INSIGHTS_ENABLED=0 — audit mode has nothing "
                "to write to.  Effectively disabled.  Set "
                "CRUCIBLE_RUN_INSIGHTS_ENABLED=1 to record the audit trail."
            )
            # Force-disable audit_mode for this process so downstream emit
            # pipelines do not waste tokens generating structured findings
            # the ledger will never persist.  Operator can still see the
            # warning above and decide what to fix.
            os.environ["CRUCIBLE_DEBATE_AUDIT_MODE"] = "0"
    except Exception:
        pass

    entry_defaults = _resolve_runtime_entry_defaults()
    parser = argparse.ArgumentParser(
        prog="run_crucible.py",
        description="Quant / SaaS / Agent / Scientist Analysis Crew + Project Scan",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Scan project and print context without calling the LLM.",
    )
    parser.add_argument(
        "--entrypoint",
        help="Override runtime validation entrypoint, e.g. api/main.py:app (comma-separated).",
    )
    parser.add_argument(
        "--direction-debate",
        action="store_true",
        help="Run Stage 0 direction debate before the main flow.",
    )
    parser.add_argument(
        "--direction-debate-only",
        action="store_true",
        help="Run Stage 0 direction debate only and exit.",
    )
    parser.add_argument(
        "--strict-json",
        action="store_true",
        help="Enable strict JSON schema enforcement (more stable, may cost more tokens).",
    )
    parser.add_argument(
        "--cost-trace",
        action="store_true",
        help="Print cost-trace markers (stage + prompt chars) to stderr for correlation with provider logs.",
    )
    parser.add_argument(
        "--cache",
        action="store_true",
        help="Enable local SQLite cache for some structured LLM steps (saves cost on reruns).",
    )
    parser.add_argument(
        "--self-check",
        action="store_true",
        help="Run offline self-check and exit.",
    )
    parser.add_argument(
        "--provider",
        choices=sorted(SUPPORTED_LLM_PROVIDERS),
        help="LLM provider: openrouter|alibaba_coding_plan|ollama.",
    )
    parser.add_argument(
        "--cost-report",
        action="store_true",
        help="Print detailed cost report at the end of execution.",
    )
    parser.add_argument(
        "--runtime-profile",
        choices=sorted(_build_runtime_profiles().keys()),
        default=entry_defaults["runtime_profile"],
        help="Runtime profile: lite|pro|enterprise.",
    )
    parser.add_argument(
        "--budget-soft-cost",
        type=float,
        default=entry_defaults["budget_soft_cost"],
        help="Soft cost limit. If reached, selective rerun is disabled.",
    )
    parser.add_argument(
        "--budget-hard-cost",
        type=float,
        default=entry_defaults["budget_hard_cost"],
        help="Hard cost limit. If reached, expensive stages can be skipped.",
    )
    parser.add_argument(
        "--budget-max-tokens",
        type=int,
        default=entry_defaults["budget_max_tokens"],
        help="Hard total token limit across all stages.",
    )
    parser.add_argument(
        "--gate-control",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Enable Gate Controller for flow control.",
    )
    parser.add_argument(
        "--selective-rerun",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Enable selective re-run for failed agents.",
    )
    parser.add_argument(
        "--api-version-check",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Enable API version check after CodeGen. Checks for outdated library usage.",
    )
    parser.add_argument(
        "--scope",
        choices=["mvp", "full", "production"],
        default="mvp",
        dest="codegen_scope",
        help=(
            "Code generation scope. "
            "'mvp' (default): minimal runnable implementation. "
            "'full': complete modular system — "
            "quant: full backtest engine + risk manager + portfolio + performance analytics + CLI; "
            "saas: complete FastAPI service with DB (SQLAlchemy/Alembic), JWT auth, full CRUD, structured logging; "
            "agent: complete headless service with job queue, tool registry, retry/circuit-breaker, graceful shutdown. "
            "'production': full scope + tests (pytest), Dockerfile, docker-compose, and CI (GitHub Actions)."
        ),
    )
    parser.add_argument(
        "--codegen-auto-optimize",
        action="store_true",
        help=(
            "Enable auto-optimize loop for CodeGen: after each generation the codegen_critic "
            "agent scores the output and injects actionable feedback into the next round until "
            "the score threshold is met or max rounds are exhausted."
        ),
    )
    parser.add_argument(
        "--codegen-optimize-rounds",
        type=int,
        default=3,
        metavar="N",
        help=(
            "Maximum number of generate+critique rounds when --codegen-auto-optimize is active. "
            "Must be >= 1. Default: 3."
        ),
    )
    parser.add_argument(
        "--codegen-optimize-threshold",
        type=float,
        default=0.80,
        metavar="SCORE",
        help=(
            "Critic score threshold [0.0, 1.0] at which auto-optimize stops early. "
            "Default: 0.80."
        ),
    )
    args = parser.parse_args()
    configure_logging()

    _reset_runtime_option_defaults_from_env()
    _apply_local_cache_runtime_defaults_from_env()
    _apply_librarian_runtime_defaults_from_env()
    _apply_research_runtime_defaults_from_env()
    _apply_api_version_check_runtime_defaults_from_env()
    _apply_quality_runtime_defaults_from_env()
    _apply_project_context_scan_defaults_from_env()
    selected_llm_provider = _resolve_entry_llm_provider(
        args.provider,
        allow_interactive_prompt=not (args.self_check or args.dry_run),
    )
    runtime_profile = resolve_runtime_profile(args.runtime_profile)
    gate_control_enabled = (
        runtime_profile.gate_control_default
        if args.gate_control is None
        else bool(args.gate_control)
    )
    selective_rerun_enabled = (
        runtime_profile.selective_rerun_default
        if args.selective_rerun is None
        else bool(args.selective_rerun)
    )
    api_version_check_enabled = (
        entry_defaults["api_version_check_enabled"]
        if args.api_version_check is None
        else bool(args.api_version_check)
    )
    quality_round_limit = _resolve_quality_round_limit(runtime_profile)
    budget_policy = BudgetPolicy(
        soft_cost_limit=args.budget_soft_cost,
        hard_cost_limit=args.budget_hard_cost,
        max_total_tokens=args.budget_max_tokens,
    )

    effective_strict_json = bool(STRICT_JSON_ENABLED)
    effective_local_cache = bool(LOCAL_CACHE_ENABLED)
    effective_cost_trace = bool(COST_TRACE_ENABLED)
    if runtime_profile.strict_json_default:
        effective_strict_json = True
    if runtime_profile.cache_default:
        effective_local_cache = True
    if args.strict_json:
        effective_strict_json = True
    if args.cost_trace:
        effective_cost_trace = True
    if args.cache:
        effective_local_cache = True
    _apply_runtime_option_overrides(
        strict_json=effective_strict_json,
        local_cache=effective_local_cache,
        cost_trace=effective_cost_trace,
    )
    _apply_output_validation_mode_overrides()

    if args.self_check:
        ok = run_self_check()
        if ok:
            print("Self-check OK")
            sys.exit(0)
        print("Self-check failed")
        sys.exit(1)

    _reset_pipeline_runtime_state()

    original_stdout = None
    if args.direction_debate_only:
        original_stdout = sys.stdout
        sys.stdout = sys.stderr

    print("==============================================")
    print("   Quant / SaaS / Agent / Scientist Analysis Crew")
    print("==============================================")
    print(f"[Info] LLM Provider: {_llm_provider_label(selected_llm_provider)}")

    # Mode Selection
    while True:
        mode_input = input("Select Mode (1: Quant, 2: SaaS, 3: Agent, 4: Scientist) [Default: 1]: ").strip()
        if mode_input in ("1", ""):
            selected_mode = "Quant"
            break
        if mode_input == "2":
            selected_mode = "SaaS"
            break
        if mode_input == "3":
            selected_mode = "Agent"
            break
        if mode_input == "4":
            selected_mode = "Scientist"
            break
        print("Invalid selection. Please try again.")

    # Input selection
    while True:
        print("\nSelect Input Mode:")
        print("1) Idea / strategy prompt")
        print("2) Project path bugfix")
        input_mode = input("Enter 1 or 2 [Default: 1]: ").strip()
        if input_mode in ("", "1"):
            input_mode = "idea"
            break
        if input_mode == "2":
            input_mode = "project_path"
            break
        print("Invalid selection. Try again.")

    user_problem = ""
    context: Optional[Dict[str, Any]] = None
    scan_mode: Optional[str] = None
    if input_mode == "project_path":
        project_path = input("\nEnter project folder path:\n> ").strip()
        if not project_path or not os.path.isdir(project_path):
            print("Error: Invalid project path.")
            sys.exit(1)

        while True:
            print("\nSelect scan depth:")
            print("1) Quick scan (depth 3, key files only)")
            print("2) Full scan (all files, full content)")
            scan_input = input("Enter 1 or 2 [Default: 1]: ").strip()
            if scan_input in ("", "1"):
                scan_mode = "quick"
                break
            if scan_input == "2":
                scan_mode = "full"
                break
            print("Invalid selection. Try again.")

        try:
            extra_notes = read_multiline_input(
                "Optional: describe the bug to fix or constraints",
                required=False,
            )
        except ValueError as exc:
            print(f"Error: {exc}")
            sys.exit(1)
        if scan_mode == "full":
            context = build_project_context(
                project_path,
                FULL_MAX_DEPTH,
                FULL_MAX_TREE_ENTRIES,
                True,
                FULL_MAX_FILE_BYTES,
                FULL_MAX_SNIPPET_CHARS,
                FULL_MAX_TOTAL_CHARS,
            )
            settings_line = (
                "Operator settings: "
                f"scan_mode=full, max_depth={fmt_limit(FULL_MAX_DEPTH)}, "
                f"max_tree_entries={fmt_limit(FULL_MAX_TREE_ENTRIES)}, "
                f"max_file_bytes={fmt_limit(FULL_MAX_FILE_BYTES)}, "
                f"max_snippet_chars={fmt_limit(FULL_MAX_SNIPPET_CHARS)}, "
                f"max_total_chars={fmt_limit(FULL_MAX_TOTAL_CHARS)}"
            )
        else:
            context = build_project_context(
                project_path,
                QUICK_MAX_DEPTH,
                QUICK_MAX_TREE_ENTRIES,
                False,
                QUICK_MAX_FILE_BYTES,
                QUICK_MAX_SNIPPET_CHARS,
                None,
            )
            settings_line = (
                "Operator settings: "
                f"scan_mode=quick, max_depth={fmt_limit(QUICK_MAX_DEPTH)}, "
                f"max_tree_entries={fmt_limit(QUICK_MAX_TREE_ENTRIES)}, "
                f"max_file_bytes={fmt_limit(QUICK_MAX_FILE_BYTES)}, "
                f"max_snippet_chars={fmt_limit(QUICK_MAX_SNIPPET_CHARS)}, "
                "max_total_chars=unlimited"
            )

        project_context_text = format_project_context(context)
        if args.dry_run:
            print("\n\n################################################")
            print("## DRY RUN: PROJECT CONTEXT (NO LLM CALL) ##")
            print("################################################\n")
            print(project_context_text)
            return

        user_problem = (
            f"Goal: Fix bugs in this {selected_mode} project using the provided context.\n"
            "Scope: minimal changes only; do not add new features or redesign.\n\n"
            f"{settings_line}\n\n"
            f"{project_context_text}\n\n"
        )
        if extra_notes:
            user_problem += f"Bug report: {extra_notes}\n"
    else:
        if args.dry_run:
            print("Error: --dry-run requires project path mode.")
            sys.exit(1)
        try:
            prompt = read_multiline_input(
                f"<< Enter your {selected_mode} problem / strategy / idea >>",
                required=True,
            )
        except ValueError as exc:
            print(f"Error: {exc}")
            sys.exit(1)
        try:
            extra_notes = read_multiline_input(
                "Optional: add target market or constraints",
                required=False,
            )
        except ValueError as exc:
            print(f"Error: {exc}")
            sys.exit(1)
        user_problem = f"Idea: {prompt}\n"
        if extra_notes:
            user_problem += f"Notes: {extra_notes}\n"

    entrypoint_override = args.entrypoint or os.environ.get(ENTRYPOINT_OVERRIDE_ENV)
    quality_runtime_scope = resolve_quality_runtime_validation_scope(
        input_mode=input_mode, mode=selected_mode
    )

    run_meta: Dict[str, Any] = {
        "mode": selected_mode,
        "llm_provider": selected_llm_provider,
        "input_mode": input_mode,
        "scan_mode": scan_mode,
        "entrypoint_override": entrypoint_override,
        "quality_runtime_validation_scope": quality_runtime_scope,
        "runtime_profile": runtime_profile.name,
        "gate_control_enabled": gate_control_enabled,
        "selective_rerun_enabled": selective_rerun_enabled,
        "quality_round_limit": quality_round_limit,
        "budget_policy": _model_to_dict(budget_policy),
    }
    if context:
        run_meta.update(
            {
                "limits": context.get("limits"),
                "context_truncated": context.get("context_truncated"),
                "context_truncate_reasons": context.get("context_truncate_reasons"),
                "scan_summary": context.get("scan_summary"),
            }
        )

    language_hint = "Traditional Chinese" if contains_cjk(user_problem) else "English"
    use_cjk = language_hint == "Traditional Chinese"
    print_labels = _get_print_labels(use_cjk)
    llm = init_llm()
    if STRICT_JSON_ENABLED:
        print(
            "[Info] STRICT_JSON enabled: schema enforcement may increase token cost but improves stability."
        )
    direction_debate_enabled = _direction_debate_enabled_from_inputs(
        bool(args.direction_debate), bool(args.direction_debate_only), input_mode
    )
    gate_feedback_enabled = _gate_feedback_enabled_from_env()
    effective_gate_feedback_enabled = _effective_gate_feedback_enabled(
        gate_feedback_enabled,
        gate_control_enabled,
        selective_rerun_enabled,
        input_mode,
    )
    direction_decision: Optional[DirectionDecision] = None
    if direction_debate_enabled:
        direction_decision = run_direction_debate(
            user_problem, mode=selected_mode, language_hint=language_hint, llm=llm
        )
        if direction_decision is None:
            # In --direction-debate-only mode the caller expects a valid decision on stdout;
            # nothing useful to continue with → hard exit.
            if args.direction_debate_only:
                print(print_labels["direction_decision_not_parsed"])
                # Restore stdout before exiting: in direction_debate_only mode sys.stdout
                # was redirected to sys.stderr; failing to restore it means atexit handlers
                # and any subsequent cleanup code write to stderr instead of stdout.
                if original_stdout is not None:
                    sys.stdout = original_stdout
                sys.exit(1)
            # In normal pipeline mode the direction debate is an optional enhancement.
            # Rather than crashing an expensive pipeline (librarian + debate already ran),
            # fall back gracefully: skip the direction preamble and continue as if
            # direction debate were disabled.
            print(
                "[Warn] Direction debate could not produce a valid decision. "
                "See the preceding [Warn] line(s) and any debug dump under "
                "saved_projects/direction_debug/ for the exact gate that fired. "
                "Continuing without direction preamble."
            )
        else:
            if original_stdout is not None:
                sys.stdout = original_stdout
            print(to_json_str(direction_decision))
            if args.direction_debate_only:
                sys.exit(0)
            if direction_decision.selected_direction == "none":
                print(print_labels["direction_not_approved"])
                sys.exit(2)
            preamble = build_direction_preamble(direction_decision, contains_cjk(user_problem))
            user_problem = preamble + user_problem
    dependency_versions = collect_dependency_versions()
    version_line = ", ".join(f"{k}={v}" for k, v in dependency_versions.items())
    if version_line:
        print(f"[Info] Dependency versions: {version_line}")

    # v1.1.2: Reuse the run_id already bound to the run-correlation
    # ContextVar by ``crucible/__main__.py`` or ``run_crucible_enhanced.py``
    # (which read ``CRUCIBLE_RUN_ID`` injected by the WebUI / set their own
    # fresh UUID4 for direct CLI invocations).  Generating a new id here
    # used to desynchronise ``run_meta.json`` from every other artefact
    # (Run Insights ledger, telemetry, structured logs) that already
    # consumed the bridged id — violating the CLAUDE.md § 2 invariant and
    # breaking v1.2.0 retrieval joins on run_id.  The defensive fallback
    # below pins a fresh 8-char id into the ContextVar so any later emit
    # point in this run sees the same value even if section_07 is invoked
    # through an untested entry path.
    _bridged_run_id = (
        (_get_run_id() or "").strip()
        or os.environ.get("CRUCIBLE_RUN_ID", "").strip()
    )
    if _bridged_run_id:
        run_id = _bridged_run_id
    else:
        run_id = uuid.uuid4().hex[:8]
        try:
            _set_run_id(run_id)
        except Exception:
            # ContextVar binding is best-effort; pipeline must not crash
            # if the runtime_logging module is in a degraded state.
            pass
    run_snapshot = RunSnapshot(
        run_id=run_id,
        runtime_profile=runtime_profile.name,
        mode=selected_mode,
        model_versions=_resolve_runtime_model_versions(llm),
        inputs={
            "mode": selected_mode,
            "llm_provider": selected_llm_provider,
            "input_mode": input_mode,
            "user_problem_sha256": _text_sha256(user_problem),
            "user_problem_chars": len(user_problem or ""),
            "scan_mode": scan_mode,
            "entrypoint_override": entrypoint_override,
            "quality_runtime_validation_scope": quality_runtime_scope,
            "librarian_enabled": LIBRARIAN_ENABLED,
            "librarian_search_providers": list(LIBRARIAN_SEARCH_PROVIDERS),
            "direction_debate_enabled": direction_debate_enabled,
            "gate_direction_feedback_configured": gate_feedback_enabled,
            "gate_direction_feedback_enabled": effective_gate_feedback_enabled,
        },
        budget_policy=_model_to_dict(budget_policy),
    )
    update_log_context(
        run_id=run_id,
        runtime_profile=runtime_profile.name,
        mode=selected_mode,
        input_mode=input_mode,
        llm_provider=selected_llm_provider,
    )
    log_event(
        LOGGER,
        20,
        "pipeline_start",
        "Crucible pipeline started.",
        direction_debate_enabled=direction_debate_enabled,
        gate_feedback_enabled=effective_gate_feedback_enabled,
    )
    run_snapshot.prompt_hashes["input.user_problem"] = _text_sha256(user_problem)
    _sync_librarian_debug_snapshot(run_snapshot)
    if direction_decision is not None:
        run_snapshot.inputs["direction_debate_selected"] = direction_decision.selected_direction
        run_snapshot.inputs["direction_debate_confidence"] = direction_decision.confidence
        run_snapshot.prompt_hashes["direction.summary"] = _text_sha256(
            direction_decision.summary or ""
        )
    run_meta["run_id"] = run_snapshot.run_id
    run_meta["run_snapshot_schema_version"] = run_snapshot.schema_version

    try:
        result = None
        final_report: Optional[AnalysisReport] = None
        code_bundle: Optional[CodeBundle] = None
        gate_decision: Optional[GateDecision] = None
        review_report: Optional[ReviewReport] = None
        runtime_log: Optional[str] = None
        api_version_report: Optional[ApiVersionReport] = None
        skip_codegen = False
        quality_loop_executed = False
        _snapshot_record_stage(run_snapshot, stage="run", status="started")

        if input_mode == "project_path":
            run_snapshot.prompt_hashes["project_fix.user_problem"] = _text_sha256(user_problem)
            crew = build_code_fix_crew(
                user_problem, mode=selected_mode, language_hint=language_hint, llm=llm
            )
            project_fix_prompt_chars = _prompt_chars_for_crew(crew, user_problem)
            run_snapshot.prompt_hashes.update(getattr(crew, "_prompt_hashes", {}) or {})
            _snapshot_record_stage(
                run_snapshot,
                stage="project_fix_crew.kickoff",
                status="started",
                extra={"prompt_chars": project_fix_prompt_chars},
            )
            try:
                log_event(
                    LOGGER,
                    20,
                    "project_fix_kickoff_start",
                    "Starting project fix crew kickoff.",
                    mode=selected_mode,
                )
                result = kickoff_crew_with_retry(
                    crew,
                    logger=LOGGER,
                    log_fields={"stage": "project_fix", "mode": selected_mode},
                )
            except _OperationCancelledError:
                # Cooperative cancellation must propagate immediately — do not
                # log as a project_fix failure or record a failure cost entry.
                raise
            except Exception as e:
                log_exception(
                    LOGGER,
                    "project_fix_kickoff_failed",
                    "Project fix crew failed.",
                    mode=selected_mode,
                )
                try:
                    _record_cost(
                        stage="project_fix_crew.kickoff",
                        agent_name="code_fixer",
                        input_tokens=max(0, project_fix_prompt_chars // 3),
                        output_tokens=0,
                        success=False,
                        outcome="execution_error",
                    )
                except Exception:
                    pass
                _snapshot_record_stage(
                    run_snapshot,
                    stage="project_fix_crew.kickoff",
                    status="failed",
                    failure_type=_classify_runtime_exception_failure(e),
                    notes=f"Project fix crew exception: {e}",
                )
                raise
            log_event(
                LOGGER,
                20,
                "project_fix_kickoff_done",
                "Project fix crew kickoff completed.",
                mode=selected_mode,
            )
            final_report = extract_analysis_report(result, mode=selected_mode)
            code_bundle = extract_code_bundle(result)
            if final_report is None or code_bundle is None:
                text_candidates = _collect_text_candidates_from_result(result)
                for raw in reversed(text_candidates):
                    if final_report is None:
                        final_report = extract_analysis_report(raw, mode=selected_mode)
                        if final_report is None and STRICT_JSON_ENABLED:
                            final_report = _reformat_analysis_report(
                                raw,
                                llm=llm,
                                language_hint=language_hint,
                                mode=selected_mode,
                            )
                    if code_bundle is None:
                        code_bundle = extract_code_bundle(raw)
                        if code_bundle is None and STRICT_JSON_ENABLED:
                            code_bundle = _reformat_code_bundle(
                                raw,
                                llm=llm,
                                language_hint=language_hint,
                                mode=selected_mode,
                            )
                    if final_report is not None and code_bundle is not None:
                        break
            code_bundle = _sanitize_code_bundle(code_bundle)
            if code_bundle is not None and not _bundle_has_files(code_bundle):
                code_bundle = None
            project_fix_failure_note = "Project fix output missing CodeBundle."
            mismatch_reason = _code_bundle_mode_mismatch_reason(code_bundle, selected_mode)
            if mismatch_reason:
                print(f"[Warn] {mismatch_reason}")
                code_bundle = None
                project_fix_failure_note = mismatch_reason
            try:
                result_text = _extract_text_from_result(result) or ""
                project_fix_success = code_bundle is not None
                project_fix_outcome = (
                    "success"
                    if code_bundle is not None and final_report is not None
                    else "partial_success"
                    if code_bundle is not None
                    else "mode_mismatch"
                    if mismatch_reason
                    else "parse_failed"
                )
                _record_cost(
                    stage="project_fix_crew.kickoff",
                    agent_name="code_fixer",
                    input_tokens=max(0, project_fix_prompt_chars // 3),
                    output_tokens=len(result_text) // 3,
                    success=project_fix_success,
                    outcome=project_fix_outcome,
                )
            except Exception:
                pass
            if code_bundle is None:
                _snapshot_record_stage(
                    run_snapshot,
                    stage="project_fix_crew.kickoff",
                    status="failed",
                    failure_type=FailureType.JSON_INVALID,
                    notes=project_fix_failure_note,
                )
            else:
                _snapshot_record_stage(
                    run_snapshot,
                    stage="project_fix_crew.kickoff",
                    status="completed",
                    notes="AnalysisReport not parsed; proceeding with CodeBundle only."
                    if final_report is None
                    else None,
                )
        else:
            result, final_report, gate_decision = run_analysis_with_selective_rerun(
                user_problem,
                mode=selected_mode,
                language_hint=language_hint,
                llm=llm,
                enable_selective_rerun=(gate_control_enabled and selective_rerun_enabled),
                gate_feedback_enabled=effective_gate_feedback_enabled,
                direction_debate_enabled=direction_debate_enabled,
                incumbent_direction=direction_decision,
                budget_policy=budget_policy,
                run_snapshot=run_snapshot,
            )

        print("\n\n################################################")
        print("## FINAL STRUCTURED OUTPUT (JSON Format) ##")
        print("################################################\n")

        if final_report:
            print(to_json_str(final_report))
        else:
            print("[Warn] AnalysisReport not parsed (will still save code if available).")

        if final_report is not None:
            run_snapshot.outputs["analysis_report"] = {
                "project_name": final_report.project_name,
                "score": final_report.score,
                "risk_level": final_report.risk_level,
            }
        budget_state = _evaluate_budget_state(budget_policy)
        run_snapshot.budget_state = budget_state

        if input_mode != "project_path":
            skip_codegen = False
            skip_reason = ""
            if budget_policy.skip_codegen_on_hard_limit and (
                budget_state.get("over_hard_limit") or budget_state.get("over_token_limit")
            ):
                skip_codegen = True
                skip_reason = "Cost/token budget exceeded hard limit."
                print(f"\n[System] Skipping CodeGen: {skip_reason}")
                _snapshot_record_stage(
                    run_snapshot,
                    stage="gate_controller.decision",
                    status="skipped",
                    failure_type=FailureType.COST_OVER_BUDGET,
                    notes=skip_reason,
                )
                try:
                    _record_cost(
                        stage="gate_controller.decision",
                        agent_name="gate_controller",
                        input_tokens=0,
                        output_tokens=0,
                        success=True,
                        outcome="skipped",
                    )
                except Exception:
                    pass
            if gate_control_enabled and not skip_codegen:
                if gate_decision is None:
                    skip_codegen = True
                    skip_reason = "GateDecision missing; failing closed before CodeGen."
                    print(f"\n[Gate Controller] Skipping CodeGen: {skip_reason}")
                    _snapshot_record_stage(
                        run_snapshot,
                        stage="gate_controller.decision",
                        status="failed",
                        failure_type=FailureType.JSON_INVALID,
                        notes=skip_reason,
                    )
                    try:
                        _record_cost(
                            stage="gate_controller.decision",
                            agent_name="gate_controller",
                            input_tokens=0,
                            output_tokens=0,
                            success=False,
                            outcome="parse_failed",
                        )
                    except Exception:
                        pass
                else:
                    skip_codegen, skip_reason = should_skip_codegen(
                        gate_decision,
                        budget_state=budget_state,
                        budget_policy=budget_policy,
                    )
                    if skip_codegen:
                        print(f"\n[Gate Controller] Skipping CodeGen: {skip_reason}")
                        _snapshot_record_stage(
                            run_snapshot,
                            stage="gate_controller.decision",
                            status="skipped",
                            failure_type=FailureType.COST_OVER_BUDGET
                            if "budget" in skip_reason.lower()
                            else FailureType(_normalize_failure_type(gate_decision.failure_type)),
                            notes=skip_reason,
                        )
                        try:
                            _record_cost(
                                stage="gate_controller.decision",
                                agent_name="gate_controller",
                                input_tokens=0,
                                output_tokens=0,
                                success=True,
                                outcome="killed" if gate_decision.should_kill else "skipped",
                            )
                        except Exception:
                            pass
                        if gate_decision.should_kill:
                            print(f"[Gate Controller] Flow killed: {gate_decision.kill_reason}")
                            run_snapshot.outputs["gate_decision"] = _model_to_dict(gate_decision)
                            _sync_librarian_debug_snapshot(run_snapshot)
                            run_snapshot.finished_at = datetime.now().isoformat()
                            run_snapshot.cost_summary = _reconcile_cost_summary_with_billing(get_cost_accountant().get_summary())
                            _update_budget_state(budget_policy, run_snapshot)
                            _snapshot_record_stage(
                                run_snapshot,
                                stage="run",
                                status="killed",
                                failure_type=FailureType.POLICY_VIOLATION,
                                notes=gate_decision.kill_reason or "Kill signal raised.",
                            )
                            save_project_output(
                                final_report,
                                None,
                                None,
                                runtime_log=None,
                                run_meta=run_meta,
                                dependency_versions=dependency_versions,
                                run_snapshot=run_snapshot,
                                language_hint=language_hint,
                            )
                            if args.cost_report:
                                cost_summary = _reconcile_cost_summary_with_billing(get_cost_accountant().get_summary())
                                print("\n=== COST REPORT ===")
                                print(to_json_str(cost_summary))
                            sys.exit(2)
                    else:
                        try:
                            _record_cost(
                                stage="gate_controller.decision",
                                agent_name="gate_controller",
                                input_tokens=0,
                                output_tokens=0,
                                success=True,
                                outcome="approved",
                            )
                        except Exception:
                            pass

            if not skip_codegen:
                codegen_scope = str(getattr(args, "codegen_scope", "mvp") or "mvp").strip().lower()
                if codegen_scope not in ("mvp", "full", "production"):
                    codegen_scope = "mvp"
                if codegen_scope != "mvp":
                    print(f"[CodeGen] Scope: {codegen_scope}")
                if args.codegen_auto_optimize:
                    optimize_rounds = max(1, int(args.codegen_optimize_rounds))
                    optimize_threshold = max(0.0, min(1.0, float(args.codegen_optimize_threshold)))
                    print(
                        f"[AutoOptimize] Enabled — rounds={optimize_rounds}, "
                        f"threshold={optimize_threshold:.2f}"
                    )
                    _, code_bundle = run_codegen_auto_optimize(
                        user_problem,
                        mode=selected_mode,
                        language_hint=language_hint,
                        llm=llm,
                        analysis_report=final_report,
                        gate_decision=gate_decision,
                        run_snapshot=run_snapshot,
                        max_rounds=optimize_rounds,
                        threshold=optimize_threshold,
                        budget_policy=budget_policy,
                        scope=codegen_scope,
                    )
                else:
                    _, code_bundle = run_codegen_stage(
                        user_problem,
                        mode=selected_mode,
                        language_hint=language_hint,
                        llm=llm,
                        analysis_report=final_report,
                        gate_decision=gate_decision,
                        run_snapshot=run_snapshot,
                        scope=codegen_scope,
                    )
                if code_bundle is None:
                    print("[Warn] CodeBundle not parsed from CodeGen output.")

        # ========== v14: API Version Check ==========
        # Run API version check BEFORE quality loop.
        # This is a ONE-TIME upfront validation, NOT part of the quality loop.
        api_version_report = _maybe_run_api_version_check(
            code_bundle,
            llm,
            enabled=api_version_check_enabled,
            run_snapshot=run_snapshot,
        )
        # ========== End v14: API Version Check ==========

        if code_bundle:
            print(f"[System] Code files generated: {len(code_bundle.files)}")
            run_snapshot.outputs["code_bundle"] = {
                "project_type": code_bundle.project_type,
                "file_count": len(code_bundle.files),
            }
            budget_state_after_codegen = _update_budget_state(budget_policy, run_snapshot)
            skip_quality_due_to_budget = budget_policy.skip_quality_on_hard_limit and (
                budget_state_after_codegen.get("over_hard_limit")
                or budget_state_after_codegen.get("over_token_limit")
            )
            if skip_quality_due_to_budget:
                runtime_log = "Quality loop skipped due to budget hard limit."
                _snapshot_record_stage(
                    run_snapshot,
                    stage="quality_loop",
                    status="skipped",
                    failure_type=FailureType.COST_OVER_BUDGET,
                    notes=runtime_log,
                )
                print(f"[Warn] {runtime_log}")
            else:
                _snapshot_record_stage(run_snapshot, stage="quality_loop", status="started")
                quality_loop_executed = True
                final_code, review_report, runtime_log = run_quality_loop(
                    user_problem,
                    final_report,
                    code_bundle,
                    llm,
                    max_rounds=quality_round_limit,
                    mode=selected_mode,
                    runtime_validation_scope=quality_runtime_scope,
                    entrypoint_override=entrypoint_override,
                    api_version_report=api_version_report,
                )
                if review_report:
                    status = "PASS" if review_report.passes else "FAIL"
                    print(f"[System] Quality review: {status}")
                    run_snapshot.outputs["quality_review"] = {
                        "passes": review_report.passes,
                        "issue_count": len(review_report.issues or []),
                    }
                code_bundle = final_code
                if review_report is None:
                    _snapshot_record_stage(
                        run_snapshot,
                        stage="quality_loop",
                        status="failed",
                        failure_type=FailureType.JSON_INVALID,
                        notes="Quality loop ended without a parsed ReviewReport.",
                    )
                elif review_report.passes:
                    _snapshot_record_stage(run_snapshot, stage="quality_loop", status="completed")
                else:
                    _snapshot_record_stage(
                        run_snapshot,
                        stage="quality_loop",
                        status="failed",
                        failure_type=FailureType.CONFLICTING_OUTPUT,
                        notes=f"Quality review failed with {len(review_report.issues or [])} remaining issue(s).",
                    )

        run_snapshot.outputs["gate_decision"] = (
            _model_to_dict(gate_decision) if gate_decision else None
        )
        _sync_librarian_debug_snapshot(run_snapshot)
        run_snapshot.finished_at = datetime.now().isoformat()
        run_snapshot.cost_summary = _reconcile_cost_summary_with_billing(get_cost_accountant().get_summary())
        _update_budget_state(budget_policy, run_snapshot)
        run_status = "completed"
        run_failure_type = FailureType.NONE
        run_notes: Optional[str] = None
        if input_mode == "project_path" and code_bundle is None:
            run_status = "failed"
            run_failure_type = FailureType.JSON_INVALID
            run_notes = "Project path flow ended without a CodeBundle artifact."
        elif input_mode != "project_path" and not skip_codegen and code_bundle is None:
            run_status = "failed"
            run_failure_type = FailureType.JSON_INVALID
            run_notes = "CodeGen flow ended without a CodeBundle artifact."
        elif quality_loop_executed and review_report is None:
            run_status = "failed"
            run_failure_type = FailureType.JSON_INVALID
            run_notes = "Quality loop ended without a parsed ReviewReport."
        elif quality_loop_executed and not review_report.passes:
            run_status = "failed"
            run_failure_type = FailureType.CONFLICTING_OUTPUT
            run_notes = (
                f"Quality review failed with {len(review_report.issues or [])} remaining issue(s)."
            )
        _snapshot_record_stage(
            run_snapshot,
            stage="run",
            status=run_status,
            failure_type=run_failure_type,
            notes=run_notes,
        )

        save_project_output(
            final_report,
            code_bundle,
            review_report,
            runtime_log=runtime_log,
            run_meta=run_meta,
            dependency_versions=dependency_versions,
            run_snapshot=run_snapshot,
            language_hint=language_hint,
        )

        # Print cost report if requested
        if args.cost_report:
            cost_summary = _reconcile_cost_summary_with_billing(get_cost_accountant().get_summary())
            cost_source = cost_summary.get("cost_source", "estimated")
            print("\n=== COST REPORT ===")

            if cost_source == "alibaba_coding_plan_tokens_only":
                print("Cost Source: Alibaba Coding Plan (tokens only, USD not tracked)")
            elif cost_summary.get("total_cost_usd", 0) > 0:
                print(f"Total Cost (USD): ${cost_summary['total_cost_usd']:.6f}")
                print(f"  - Input Cost: ${cost_summary.get('input_cost_usd', 0):.6f}")
                print(f"  - Output Cost: ${cost_summary.get('output_cost_usd', 0):.6f}")
                print(f"  - Cache Savings: ${cost_summary.get('cache_cost_usd', 0):.6f}")
            else:
                print(f"Total Cost Units: {cost_summary['total_cost']:.2f}")

            print(f"Total Tokens: {cost_summary['total_tokens']}")

            if cost_summary.get("cached_tokens", 0) > 0:
                print(f"Cached Tokens: {cost_summary['cached_tokens']}")

            if cost_summary.get("reasoning_tokens", 0) > 0:
                print(f"Reasoning Tokens: {cost_summary['reasoning_tokens']}")

            print(f"Total Executions: {cost_summary['total_executions']}")
            print(f"Cache Hit Rate: {cost_summary['cache_hit_rate'] * 100:.1f}%")
            print(f"Success Rate: {cost_summary['success_rate'] * 100:.1f}%")

            if cost_summary.get("models_used"):
                print(f"Models Used: {', '.join(cost_summary['models_used'])}")

            if cost_source == "openrouter_api":
                print("Cost Source: OpenRouter API (actual billing)")
            elif cost_source == "openrouter_tokens_with_pricing":
                print(
                    "Cost Source: OpenRouter token pricing estimate "
                    "(OpenRouter returned tokens without billed cost)"
                )
            elif cost_source == "litellm_computed":
                print(
                    "Cost Source: LiteLLM-computed cost "
                    "(provider returned tokens without a billed cost field)"
                )
            elif cost_source == "crewai_metrics_with_pricing":
                print("Cost Source: CrewAI usage metrics with model pricing estimate")
            elif cost_source == "alibaba_coding_plan_tokens_only":
                pass
            else:
                print("Cost Source: Estimated (OpenRouter API data not captured)")

            _token_only_sources = ("alibaba_coding_plan_tokens_only",)
            top_agents = get_cost_accountant().get_top_cost_agents(limit=5)
            if top_agents:
                print("\nTop Cost Agents:")
                for agent_cost in top_agents:
                    if agent_cost.get("total_cost_usd", 0) > 0:
                        print(
                            f"  - {agent_cost['agent']}: ${agent_cost['total_cost_usd']:.6f} ({agent_cost['executions']} executions)"
                        )
                    elif cost_source in _token_only_sources:
                        print(
                            f"  - {agent_cost['agent']}: {agent_cost['total_tokens']} tokens ({agent_cost['executions']} executions)"
                        )
                    else:
                        print(
                            f"  - {agent_cost['agent']}: {agent_cost['total_cost']:.2f} units ({agent_cost['executions']} executions)"
                        )

            print("\nDetailed Summary:")
            print(to_json_str(cost_summary))
    except _OperationCancelledError:
        # Cooperative cancellation — propagate cleanly without recording a run
        # failure or printing an error.  The caller or top-level entry point is
        # responsible for the cancellation exit path.
        if original_stdout is not None:
            sys.stdout = original_stdout
        raise
    except Exception as e:
        # Restore stdout before error handling so that error messages and
        # tracebacks are written to the real stdout, not the redirected stderr.
        if original_stdout is not None:
            sys.stdout = original_stdout
        try:
            _sync_librarian_debug_snapshot(run_snapshot)
            run_snapshot.finished_at = datetime.now().isoformat()
            run_snapshot.cost_summary = _reconcile_cost_summary_with_billing(get_cost_accountant().get_summary())
            _update_budget_state(budget_policy, run_snapshot)
            _snapshot_record_stage(
                run_snapshot,
                stage="run",
                status="failed",
                failure_type=_classify_runtime_exception_failure(e),
                notes=str(e),
            )
        except Exception:
            pass
        print(f"\n[Error] An error occurred during execution: {e}")
        msg = str(e).lower()
        if "model" in msg and ("not found" in msg or "no such" in msg or "invalid" in msg):
            print("[Hint] Set OPENROUTER_MODEL (or OPENAI_MODEL) to a valid model ID.")
        import traceback

        traceback.print_exc()
        sys.exit(1)


# BEGIN MANUAL OUTPUT SAVE OVERRIDES
_README_LABELS_EN.update(
    {
        "gate_fallback_notice": "(Primary analysis report was unavailable; persisted Gate Controller fallback instead.)",
        "gate_decision": "Gate Decision",
        "ready_for_codegen": "Ready For CodeGen",
        "codegen_scope": "CodeGen Scope",
        "confidence": "Confidence",
        "blocking_risks": "Blocking Risks",
        "required_before_codegen": "Required Before CodeGen",
        "advisory_after_codegen": "Advisory After CodeGen",
        "validation_scope_reason": "Validation Scope Reason",
        "validation_objectives": "Validation Objectives",
        "kill_reason": "Kill Reason",
        "yes": "yes",
        "no": "no",
    }
)

_README_LABELS_ZH.update(
    {
        "gate_fallback_notice": "（主要 AnalysisReport 未成功產生；已改為保存 Gate Controller 的回退結果。）",
        "gate_decision": "Gate 決策",
        "ready_for_codegen": "可否進入 CodeGen",
        "codegen_scope": "CodeGen 範圍",
        "confidence": "信心等級",
        "blocking_risks": "阻斷風險",
        "required_before_codegen": "CodeGen 前必做項目",
        "advisory_after_codegen": "CodeGen 後建議追蹤",
        "validation_scope_reason": "Validation 範圍原因",
        "validation_objectives": "Validation 目標",
        "kill_reason": "終止原因",
        "yes": "是",
        "no": "否",
    }
)


def _trim_project_name_candidate(value: Any, max_chars: int = 48) -> str:
    text = " ".join(str(value or "").strip().split())
    if not text:
        return ""
    lower = text.lower()
    for prefix in ("idea:", "notes:", "summary:", "consensus:", "project:", "strategy:"):
        if lower.startswith(prefix):
            text = text[len(prefix) :].strip()
            lower = text.lower()
            break
    for sep in (
        "\n",
        "。",
        "！",
        "？",
        "!",
        "?",
        ";",
        "；",
        ":",
        "：",
        ",",
        "，",
        "、",
        "(",
        ")",
        "（",
        "）",
        "[",
        "]",
        "【",
        "】",
    ):
        if sep in text:
            text = text.split(sep, 1)[0].strip()
    return text[:max_chars].strip(" _-")


def _extract_saved_gate_payload(
    gate_decision: Optional["GateDecision"],
    run_snapshot: Optional["RunSnapshot"],
) -> Dict[str, Any]:
    payload = _model_to_dict(gate_decision)
    if payload:
        return payload
    snapshot_payload = _model_to_dict(run_snapshot)
    outputs = snapshot_payload.get("outputs") or {}
    gate_output = outputs.get("gate_decision")
    if isinstance(gate_output, dict) and gate_output:
        return dict(gate_output)
    gate_decisions = snapshot_payload.get("gate_decisions") or []
    if gate_decisions and isinstance(gate_decisions[-1], dict):
        return dict(gate_decisions[-1])
    return {}


def _derive_saved_project_name(
    result: Optional["AnalysisReport"],
    code: Optional[CodeBundle],
    run_meta_payload: Dict[str, Any],
    gate_payload: Dict[str, Any],
) -> str:
    if result and getattr(result, "project_name", None):
        return str(result.project_name).strip()

    candidates: List[Any] = [
        run_meta_payload.get("project_name"),
        run_meta_payload.get("project_root_name"),
        gate_payload.get("project_name"),
        gate_payload.get("consensus"),
        gate_payload.get("disagreement"),
    ]
    if code and getattr(code, "project_type", None):
        candidates.append(f"{code.project_type}_analysis")
    if run_meta_payload.get("mode"):
        candidates.append(f"{run_meta_payload['mode']}_analysis")

    for candidate in candidates:
        trimmed = _trim_project_name_candidate(candidate)
        if trimmed:
            return trimmed
    return "project"


def _derive_saved_risk_level(gate_payload: Dict[str, Any]) -> str:
    if gate_payload.get("blocking_risks") or gate_payload.get("should_kill"):
        return "High"
    score = gate_payload.get("overall_score")
    if isinstance(score, (int, float)):
        if score >= 70:
            return "Low"
        if score >= 40:
            return "Medium"
        return "High"
    confidence = str(gate_payload.get("confidence") or "").strip().lower()
    if confidence == "high":
        return "Low"
    if confidence == "low":
        return "High"
    return "Medium"


def _resolve_saved_mode_name(
    result: Optional["AnalysisReport"],
    code: Optional[CodeBundle],
    run_meta_payload: Dict[str, Any],
    gate_payload: Dict[str, Any],
) -> str:
    candidates: List[Any] = []
    if result and getattr(result, "mode_used", None):
        candidates.append(result.mode_used)
    if code and getattr(code, "project_type", None):
        candidates.append(code.project_type)
    candidates.append(gate_payload.get("mode_used"))
    candidates.append(gate_payload.get("project_type"))
    candidates.append(run_meta_payload.get("mode"))

    canonical_names = {
        "quant": "Quant",
        "saas": "SaaS",
        "agent": "Agent",
        "scientist": "Scientist",
    }
    for candidate in candidates:
        normalized = str(candidate or "").strip()
        if not normalized:
            continue
        canonical = canonical_names.get(normalized.lower())
        if canonical:
            return canonical
    return ""


def _build_saved_analysis_payload(
    result: Optional["AnalysisReport"],
    code: Optional[CodeBundle],
    proj_name: str,
    gate_payload: Dict[str, Any],
    run_meta_payload: Dict[str, Any],
) -> Dict[str, Any]:
    gate_snapshot = {}
    if result is not None:
        gate_snapshot = dict(getattr(result, "gate_context_snapshot", {}) or {})
    effective_gate_payload = dict(gate_snapshot)
    effective_gate_payload.update(dict(gate_payload or {}))
    saved_mode_name = _resolve_saved_mode_name(result, code, run_meta_payload, gate_payload)
    if result:
        payload = _model_to_dict(result)
        payload.setdefault("project_name", proj_name)
        payload["analysis_report_available"] = True
        if saved_mode_name and not payload.get("mode_used"):
            payload["mode_used"] = saved_mode_name
        if effective_gate_payload:
            payload.setdefault(
                "ready_for_codegen", effective_gate_payload.get("ready_for_codegen")
            )
            payload.setdefault(
                "blocking_risks",
                list(effective_gate_payload.get("blocking_risks") or []),
            )
            payload.setdefault(
                "required_experiments_before_codegen",
                list(effective_gate_payload.get("required_experiments_before_codegen") or []),
            )
            payload.setdefault(
                "advisory_experiments_after_codegen",
                list(effective_gate_payload.get("advisory_experiments_after_codegen") or []),
            )
            payload.setdefault(
                "codegen_scope",
                effective_gate_payload.get("codegen_scope") or "production",
            )
            payload.setdefault(
                "validation_scope_reason",
                effective_gate_payload.get("validation_scope_reason"),
            )
            payload.setdefault(
                "validation_objectives",
                list(effective_gate_payload.get("validation_objectives") or []),
            )
            payload.setdefault("confidence", effective_gate_payload.get("confidence"))
            payload.setdefault("failure_type", effective_gate_payload.get("failure_type"))
            payload.setdefault("failure_details", effective_gate_payload.get("failure_details"))
            payload.setdefault("should_kill", effective_gate_payload.get("should_kill"))
            payload.setdefault("kill_reason", effective_gate_payload.get("kill_reason"))
            payload["gate_decision"] = effective_gate_payload
        return payload

    summary = (
        effective_gate_payload.get("consensus")
        or effective_gate_payload.get("kill_reason")
        or ""
    )
    payload = {
        "project_name": proj_name,
        "summary": summary,
        "consensus": effective_gate_payload.get("consensus", ""),
        "disagreement": effective_gate_payload.get("disagreement", ""),
        "experiments": list(effective_gate_payload.get("experiments") or []),
        "score": effective_gate_payload.get("overall_score"),
        "mode_used": saved_mode_name,
        "risk_level": _derive_saved_risk_level(effective_gate_payload),
        "analysis_report_available": False,
        "derived_from_gate_decision": bool(effective_gate_payload),
        "ready_for_codegen": effective_gate_payload.get("ready_for_codegen"),
        "blocking_risks": list(effective_gate_payload.get("blocking_risks") or []),
        "required_experiments_before_codegen": list(
            effective_gate_payload.get("required_experiments_before_codegen") or []
        ),
        "advisory_experiments_after_codegen": list(
            effective_gate_payload.get("advisory_experiments_after_codegen") or []
        ),
        "codegen_scope": effective_gate_payload.get("codegen_scope") or "production",
        "validation_scope_reason": effective_gate_payload.get("validation_scope_reason"),
        "validation_objectives": list(effective_gate_payload.get("validation_objectives") or []),
        "confidence": effective_gate_payload.get("confidence"),
        "failure_type": effective_gate_payload.get("failure_type"),
        "failure_details": effective_gate_payload.get("failure_details"),
        "should_kill": effective_gate_payload.get("should_kill"),
        "kill_reason": effective_gate_payload.get("kill_reason"),
    }
    if effective_gate_payload:
        payload["gate_decision"] = effective_gate_payload
    return payload


def save_project_output(
    result: Optional["AnalysisReport"],
    code: Optional[CodeBundle] = None,
    review: Optional[ReviewReport] = None,
    runtime_log: Optional[str] = None,
    run_meta: Optional[Dict[str, Any]] = None,
    dependency_versions: Optional[Dict[str, str]] = None,
    run_snapshot: Optional[RunSnapshot] = None,
    language_hint: str = "English",
    gate_decision: Optional["GateDecision"] = None,
) -> str:
    """Saves the analysis result to a timestamped folder."""
    base_dir = os.path.join(_REPO_ROOT, "saved_projects")
    os.makedirs(base_dir, exist_ok=True)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    # v1.1.2 (sixth-pass M-10): also emit a TZ-aware UTC timestamp so the
    # v1.2.0 Cloudflare retrieval layer (D1 + R2) can join across machines
    # without DST / local-tz ambiguity.  ``timestamp`` stays local-time
    # because that is what the ``saved_projects/<ts>_<name>`` directory
    # naming uses and operators read it visually; ``timestamp_utc`` is the
    # join key for cross-machine analytics.
    try:
        from datetime import timezone as _utc_tz
        timestamp_utc = datetime.now(_utc_tz.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    except Exception:
        timestamp_utc = ""
    run_meta_payload: Dict[str, Any] = dict(run_meta or {})
    gate_payload = _extract_saved_gate_payload(gate_decision, run_snapshot)
    authoritative_mode = _resolve_saved_mode_name(None, code, run_meta_payload, gate_payload)
    result = _normalize_analysis_report(
        result,
        mode=authoritative_mode or run_meta_payload.get("mode"),
    )
    proj_name = _derive_saved_project_name(result, code, run_meta_payload, gate_payload)
    safe_name = sanitize_name(proj_name)
    project_dir = os.path.join(base_dir, f"{timestamp}_{safe_name}")
    os.makedirs(project_dir, exist_ok=True)

    if dependency_versions is None:
        dependency_versions = collect_dependency_versions()

    run_meta_payload.setdefault("project_name", proj_name)
    run_meta_payload.setdefault("timestamp", timestamp)
    if timestamp_utc:
        run_meta_payload.setdefault("timestamp_utc", timestamp_utc)
    if gate_payload:
        run_meta_payload.setdefault("gate_ready_for_codegen", gate_payload.get("ready_for_codegen"))
        run_meta_payload.setdefault("gate_confidence", gate_payload.get("confidence"))
    # v1.0.5 round 2 (P2-11 structural): expose review.passes and review.failure_type
    # at the top level of run_meta.json so observability tools (agent_metrics,
    # multi_project_compare, run_diff, batch dashboards) can read the
    # quality-loop outcome without parsing review_report.json.
    if review is not None:
        run_meta_payload.setdefault("quality_passed", bool(review.passes))
        review_failure_type = (
            str(getattr(review, "failure_type", "") or "").strip().upper()
        )
        if review_failure_type:
            run_meta_payload.setdefault(
                "quality_loop_failure_type", review_failure_type
            )

    # v1.0.5 round 4 (cost surfacing): promote total_cost / total_cost_usd /
    # total_tokens / cost_source from the in-memory cost summary to the top
    # level of run_meta.json so the WebUI dashboard renders the real $ amount
    # instead of $0.00.  Without this, the dashboard reads ``meta.total_cost``
    # (None) → SQLite stores NULL → "Total Cost" widget collapses to $0.
    #
    # Authoritative source priority:
    #   1. ``run_snapshot.cost_summary`` — frozen end-of-run state, written
    #      by section_07 itself before save_project_output is called.
    #   2. ``get_cost_accountant().get_summary()`` — live accountant for
    #      legacy callers that pass run_meta but no run_snapshot.
    #
    # Persistence rule: NEVER round before persisting.  OpenRouter per-call
    # costs reach the 6th decimal (e.g. $0.000003 for cached tokens on a
    # cheap model) — rounding here would silently truncate real money to $0.
    # The display layer (WebUI / __format__ specifiers) is responsible for
    # any human-readable rounding; the persisted JSON keeps full float
    # precision.
    cost_summary_payload: Optional[Dict[str, Any]] = None
    if run_snapshot is not None:
        cs_attr = getattr(run_snapshot, "cost_summary", None)
        if isinstance(cs_attr, dict) and cs_attr:
            cost_summary_payload = cs_attr
    if cost_summary_payload is None:
        try:
            live_summary = _reconcile_cost_summary_with_billing(get_cost_accountant().get_summary())
            if (
                isinstance(live_summary, dict)
                and int(live_summary.get("total_executions") or 0) > 0
            ):
                cost_summary_payload = live_summary
        except Exception:
            cost_summary_payload = None
    if cost_summary_payload:
        if "total_cost_usd" not in run_meta_payload:
            try:
                run_meta_payload["total_cost_usd"] = float(
                    cost_summary_payload.get("total_cost_usd") or 0.0
                )
            except (TypeError, ValueError):
                pass
        if "total_cost" not in run_meta_payload:
            try:
                run_meta_payload["total_cost"] = float(
                    cost_summary_payload.get("total_cost") or 0.0
                )
            except (TypeError, ValueError):
                pass
        if "total_tokens" not in run_meta_payload:
            try:
                run_meta_payload["total_tokens"] = int(
                    cost_summary_payload.get("total_tokens") or 0
                )
            except (TypeError, ValueError):
                pass
        cs_label = str(cost_summary_payload.get("cost_source") or "").strip()
        if cs_label and "cost_source" not in run_meta_payload:
            run_meta_payload["cost_source"] = cs_label
    resolved_primary_model_id = _resolve_primary_model_id()
    if resolved_primary_model_id and "model_id" not in run_meta_payload:
        run_meta_payload["model_id"] = resolved_primary_model_id
    run_meta_payload.setdefault("llm_provider", _resolve_llm_provider(run_meta_payload.get("llm_provider")))
    if dependency_versions:
        run_meta_payload.setdefault("dependency_versions", dependency_versions)
    saved_mode_name = _resolve_saved_mode_name(result, code, run_meta_payload, gate_payload)
    if saved_mode_name:
        run_meta_payload["mode"] = saved_mode_name

    analysis_payload = _build_saved_analysis_payload(
        result, code, proj_name, gate_payload, run_meta_payload
    )

    _atomic_write_text(
        os.path.join(project_dir, "analysis_result.json"), to_json_str(analysis_payload)
    )

    if review:
        _atomic_write_text(
            os.path.join(project_dir, "review_report.json"), to_json_str(review)
        )

    if runtime_log:
        _atomic_write_text(
            os.path.join(project_dir, "runtime_validation.log"),
            runtime_log.strip() + "\n",
        )

    if run_meta_payload:
        _atomic_write_text(
            os.path.join(project_dir, "run_meta.json"), to_json_str(run_meta_payload)
        )

    if run_snapshot is not None:
        _atomic_write_text(
            os.path.join(project_dir, "run_snapshot.json"), to_json_str(run_snapshot)
        )

    if dependency_versions:
        _req_buf = _io.StringIO()
        for key, version in dependency_versions.items():
            name = DEPENDENCY_PIP_NAMES.get(key, key)
            if version and version != "not_installed":
                _req_buf.write(f"{name}=={version}\n")
        _atomic_write_text(
            os.path.join(project_dir, "requirements.txt"), _req_buf.getvalue()
        )

    md_path = os.path.join(project_dir, "README.md")
    use_cjk = language_hint == "Traditional Chinese"
    labels = _get_readme_labels(use_cjk)
    summary_text = str(analysis_payload.get("summary") or "").strip()
    consensus_text = str(analysis_payload.get("consensus") or "").strip()
    disagreement_text = str(analysis_payload.get("disagreement") or "").strip()
    experiments = list(analysis_payload.get("experiments") or [])
    blocking_risks = list(analysis_payload.get("blocking_risks") or [])
    required_before = list(analysis_payload.get("required_experiments_before_codegen") or [])
    advisory_after = list(analysis_payload.get("advisory_experiments_after_codegen") or [])
    codegen_scope = str(analysis_payload.get("codegen_scope") or "production").strip()
    validation_scope_reason = str(analysis_payload.get("validation_scope_reason") or "").strip()
    validation_objectives = list(analysis_payload.get("validation_objectives") or [])
    _md_buf = _io.StringIO()
    _md_buf.write(f"# {proj_name}\n\n")
    _md_buf.write(f"- {labels['date']}: {timestamp}\n")
    if analysis_payload.get("score") is not None:
        _md_buf.write(f"- {labels['score']}: {analysis_payload['score']}/100\n")
    if analysis_payload.get("mode_used"):
        _md_buf.write(f"- {labels['mode']}: {analysis_payload['mode_used']}\n")
    if analysis_payload.get("risk_level"):
        _md_buf.write(f"- {labels['risk']}: {analysis_payload['risk_level']}\n")
    if gate_payload:
        ready_value = labels["yes"] if gate_payload.get("ready_for_codegen") else labels["no"]
        _md_buf.write(f"- {labels['ready_for_codegen']}: {ready_value}\n")
        _md_buf.write(f"- {labels['codegen_scope']}: {codegen_scope}\n")
        if gate_payload.get("confidence"):
            _md_buf.write(f"- {labels['confidence']}: {gate_payload.get('confidence')}\n")
    if review:
        _md_buf.write(f"- {labels['quality_passed']}: {review.passes}\n")
    _md_buf.write("\n")

    # v1.0.5 (P2-12): when the quality review explicitly did not pass, render
    # a prominent warning banner above the body so consumers of the
    # saved_project README cannot mistake the bundle for deliverable work.
    # Detect the QUALITY_LOOP_GAVE_UP marker injected by run_quality_loop's
    # stagnation early-stop path so the banner can call out the failure mode
    # specifically.
    if review is not None and review.passes is False:
        # v1.0.5 round 3 (P2-11 strict): the substring fallback on
        # review.summary has been removed. The Pydantic model now validates
        # failure_type at write time against _REVIEW_REPORT_ALLOWED_FAILURE_TYPES
        # so any typo raises ValueError at the write site instead of silently
        # missing the marker here. Consumers of older saved_projects that
        # predate the structured field can run scripts/migrate_review_failure_type.py
        # to backfill it from the summary string.
        review_failure_type = (
            str(getattr(review, "failure_type", "") or "").strip().upper()
        )
        gave_up = review_failure_type == "QUALITY_LOOP_GAVE_UP"
        _md_buf.write(f"> **⚠️ {labels['failure_banner_title']}**\n")
        _md_buf.write(f"> \n")
        _md_buf.write(f"> {labels['failure_banner_body']}\n")
        if gave_up:
            _md_buf.write(f"> \n")
            _md_buf.write(f"> {labels['failure_banner_giveup_extra']}\n")
        _md_buf.write("\n")

    if not result and gate_payload:
        _md_buf.write(f"{labels['gate_fallback_notice']}\n\n")

    if summary_text:
        _md_buf.write(f"## {labels['summary']}\n{summary_text}\n\n")
    if consensus_text:
        _md_buf.write(f"## {labels['consensus']}\n{consensus_text}\n\n")
    if disagreement_text:
        _md_buf.write(f"## {labels['disagreement']}\n{disagreement_text}\n\n")
    if experiments:
        _md_buf.write(f"## {labels['experiments']}\n")
        for exp in experiments:
            if isinstance(exp, dict):
                goal = exp.get("goal", "")
                criteria = exp.get("criteria", "")
            else:
                goal = getattr(exp, "goal", "")
                criteria = getattr(exp, "criteria", "")
            _md_buf.write(f"- {labels['goal']}: {goal}\n")
            _md_buf.write(f"  - {labels['criteria']}: {criteria}\n")
        _md_buf.write("\n")
    elif not gate_payload:
        _md_buf.write(f"{labels['no_analysis_report']}\n\n")

    if gate_payload:
        _md_buf.write(f"## {labels['gate_decision']}\n")
        if blocking_risks:
            _md_buf.write(f"- {labels['blocking_risks']}:\n")
            for risk in blocking_risks:
                _md_buf.write(f"  - {risk}\n")
        if required_before:
            _md_buf.write(f"- {labels['required_before_codegen']}:\n")
            for item in required_before:
                _md_buf.write(f"  - {item}\n")
        if advisory_after:
            _md_buf.write(f"- {labels['advisory_after_codegen']}:\n")
            for item in advisory_after:
                _md_buf.write(f"  - {item}\n")
        if validation_scope_reason:
            _md_buf.write(f"- {labels['validation_scope_reason']}: {validation_scope_reason}\n")
        if validation_objectives:
            _md_buf.write(f"- {labels['validation_objectives']}:\n")
            for item in validation_objectives:
                _md_buf.write(f"  - {item}\n")
        if gate_payload.get("kill_reason"):
            _md_buf.write(f"- {labels['kill_reason']}: {gate_payload['kill_reason']}\n")
        _md_buf.write("\n")

    if review:
        _md_buf.write(f"## {labels['quality_review']}\n")
        _md_buf.write(f"{labels['summary']}: {review.summary}\n")
        if review.issues:
            _md_buf.write(f"{labels['issues']}:\n")
            for issue in review.issues:
                _md_buf.write(
                    f"- [{issue.severity}] {issue.category}: {issue.description}"
                    f" ({issue.file or 'n/a'})\n"
                )

    if run_meta_payload:
        _md_buf.write(f"\n## {labels['run_metadata']}\n")
        if use_cjk:
            _md_buf.write("詳見 run_meta.json。\n")
        else:
            _md_buf.write("See run_meta.json for full details.\n")
    if dependency_versions:
        _md_buf.write(f"\n## {labels['dependency_versions']}\n")
        for key, version in dependency_versions.items():
            _md_buf.write(f"- {key}: {version}\n")

    _atomic_write_text(md_path, _md_buf.getvalue())

    if code:
        code_dir = os.path.join(project_dir, "code")
        os.makedirs(code_dir, exist_ok=True)
        clean_code = _sanitize_code_bundle(code)
        if clean_code is None:
            warn_msg = (
                "[警告] 程式碼包安全驗證失敗，略過寫入。"
                if use_cjk
                else "[Warn] Code bundle sanitization failed; skipping code write."
            )
            print(warn_msg)
        else:
            expected_files = len(clean_code.files)
            written = _write_code_bundle_to_dir(clean_code, code_dir)
            if expected_files and len(written) < expected_files:
                warn_msg = (
                    f"[警告] 因路徑無效或不安全，已略過 {expected_files - len(written)} 個程式碼檔案。"
                    if use_cjk
                    else f"[Warn] Skipped {expected_files - len(written)} code file(s) due to invalid or unsafe output paths."
                )
                print(warn_msg)

    print_labels = _get_print_labels(use_cjk)
    print(f"\n{print_labels['project_saved_to']} {project_dir}")

    # v1.1.0 run_insights: record output_method + (Quant only) runtime_params.
    # This is the single source-of-truth point — every saved run flows
    # through here, so the insights ledger always sees what the WebUI sees.
    # Best-effort, never raises.
    try:
        _insights_recorder = _get_insights_recorder()
        _saved_mode = run_meta_payload.get("mode") or authoritative_mode or ""
        _user_problem_text = ""
        try:
            if result is not None:
                _user_problem_text = str(getattr(result, "user_problem", "") or "")
        except Exception:
            _user_problem_text = ""
        if not _user_problem_text:
            _user_problem_text = str(run_meta_payload.get("user_problem") or "")
        _validation_verdict = None
        try:
            if review is not None:
                _validation_verdict = "passed" if bool(review.passes) else "failed"
        except Exception:
            _validation_verdict = None
        _entrypoint = run_meta_payload.get("entrypoint_override") or None
        _artefact_names: List[str] = []
        try:
            for _entry in os.listdir(project_dir):
                if not _entry.startswith("."):
                    _artefact_names.append(_entry)
            _artefact_names.sort()
        except OSError:
            _artefact_names = []
        _outcome_status = _InsightOutcome.SUCCESS
        _outcome_score: Optional[float] = None
        try:
            if review is not None and not bool(review.passes):
                _outcome_status = _InsightOutcome.PARTIAL
            _score_raw = run_meta_payload.get("score")
            if _score_raw is None and result is not None:
                _score_raw = getattr(result, "overall_score", None)
            if _score_raw is not None:
                _outcome_score_candidate = float(_score_raw) / 100.0
                # v1.1.2 (sixth-pass M-7): reject non-finite ``_outcome_score``
                # symmetrically with ``output_validation._coerce``'s NaN/Inf
                # gate.  ``_score_raw="nan"`` / ``"infinity"`` survives
                # ``float()`` and would propagate into the ledger; downstream
                # consumers (``v1.2.0`` retrieval ranker) would then sort
                # against an ``inf`` and either crash or produce undefined
                # ordering.
                if math.isfinite(_outcome_score_candidate):
                    _outcome_score = _outcome_score_candidate
                else:
                    _outcome_score = None
        except (TypeError, ValueError):
            _outcome_score = None

        # v1.1.0 fifth-pass (G-20): best-effort read of backtest_report.json
        # to enrich the ledger emit with data provenance.  Failure to
        # read is silent — non-Quant modes don't write this file.
        _bt_data_source: Optional[str] = (
            run_meta_payload.get("backtest_data_source") or None
        )
        _bt_actual_symbol: Optional[str] = (
            run_meta_payload.get("backtest_actual_symbol") or None
        )
        if _bt_data_source is None:
            try:
                _bt_report_path = os.path.join(project_dir, "backtest_report.json")
                if os.path.isfile(_bt_report_path):
                    with open(_bt_report_path, "r", encoding="utf-8") as _bt_fh:
                        _bt_report = json.load(_bt_fh)
                    if isinstance(_bt_report, dict):
                        _ds_raw = _bt_report.get("data_source")
                        if isinstance(_ds_raw, str) and _ds_raw.strip():
                            _bt_data_source = _ds_raw.strip()
                        _sym_raw = (
                            _bt_report.get("data_actual_symbol")
                            or _bt_report.get("actual_symbol")
                            or _bt_report.get("symbol_used")
                        )
                        if isinstance(_sym_raw, str) and _sym_raw.strip():
                            _bt_actual_symbol = _sym_raw.strip()
            except (OSError, json.JSONDecodeError, ValueError):
                _bt_data_source = _bt_data_source  # keep prior value
                _bt_actual_symbol = _bt_actual_symbol

        _insights_recorder.record_output_method(
            run_id=(
                _get_run_id()
                or os.environ.get("CRUCIBLE_RUN_ID", "").strip()
                or str(run_meta_payload.get("run_id") or "")
            ),
            project_name=proj_name,
            mode=_saved_mode,
            user_problem=_user_problem_text,
            run_meta=run_meta_payload,
            validation_verdict=_validation_verdict,
            entrypoint=_entrypoint,
            artefact_names=_artefact_names,
            outcome_score=_outcome_score,
            outcome_status=_outcome_status,
            # v1.1.0 fifth-pass (G-20): forward backtest data provenance
            # to the ledger so v1.2.0 retrieval can filter synthetic
            # runs without re-opening backtest_report.json per row.
            # Quant-mode payload carries these; non-Quant modes pass
            # None which is dropped from the persisted event.
            data_source=_bt_data_source,
            data_actual_symbol=_bt_actual_symbol,
        )

        # runtime_params is Quant-only by default (CRUCIBLE_RUN_INSIGHTS_RECORD_PARAMS=auto).
        # Recorder's mode-aware gate suppresses the call for non-Quant.
        _gate_cfg: Dict[str, Any] = {}
        try:
            _gate_cfg = {
                "gate_control_enabled": run_meta_payload.get("gate_control_enabled"),
                "selective_rerun_enabled": run_meta_payload.get("selective_rerun_enabled"),
                "quality_round_limit": run_meta_payload.get("quality_round_limit"),
                "quality_runtime_validation_scope": run_meta_payload.get(
                    "quality_runtime_validation_scope"
                ),
            }
        except Exception:
            _gate_cfg = {}
        _budget_cfg: Dict[str, Any] = {}
        try:
            _bp = run_meta_payload.get("budget_policy")
            if isinstance(_bp, dict):
                _budget_cfg = dict(_bp)
        except Exception:
            _budget_cfg = {}
        _cli_flags: Dict[str, Any] = {}
        try:
            for _k in (
                "input_mode", "scan_mode", "runtime_profile",
                "entrypoint_override",
            ):
                _v = run_meta_payload.get(_k)
                if _v is not None:
                    _cli_flags[_k] = _v
        except Exception:
            _cli_flags = {}

        _insights_recorder.record_runtime_params(
            run_id=(
                _get_run_id()
                or os.environ.get("CRUCIBLE_RUN_ID", "").strip()
                or str(run_meta_payload.get("run_id") or "")
            ),
            project_name=proj_name,
            mode=_saved_mode,
            cli_flags=_cli_flags,
            gate_config=_gate_cfg,
            budget_policy=_budget_cfg,
            user_problem=_user_problem_text,
            run_meta=run_meta_payload,
        )
    except Exception:
        # Insights must never break the main save path.  Swallow.
        pass

    return project_dir


# END MANUAL OUTPUT SAVE OVERRIDES

if __name__ == "__main__":
    main()
