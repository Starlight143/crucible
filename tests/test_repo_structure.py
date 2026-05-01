# ruff: noqa: I001
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


class TestRepoStructure(unittest.TestCase):
    def test_runtime_entrypoint_uses_modules_tree(self) -> None:
        module_runtime = (ROOT / "crucible" / "module_runtime.py").read_text(
            encoding="utf-8"
        )
        self.assertIn("from .modules import (", module_runtime)
        self.assertNotIn("from .sections import", module_runtime)

    def test_runtime_validation_keeps_critical_snapshot_guards(self) -> None:
        modules_runtime = (
            ROOT / "crucible" / "modules" / "section_06_runtime_quality_api.py"
        ).read_text(encoding="utf-8")

        critical_markers = (
            "def _mode_supports_web_runtime_validation(",
            "def _has_conflicting_validation_mode_metadata(",
            "return len(set(canonical)) > 1",
            "not _mode_supports_web_runtime_validation(",
            "return _mode_supports_web_runtime_validation(mode_cfg)",
            "web runtime validation must not be inferred from prompt",
            "Invalid CodeBundle rejected before validation.",
            "Mode/project_type mismatch rejected before validation.",
            "conflicted with the explicitly requested mode",
            "snapshot route missing.",
            "/health endpoint alone is insufficient.",
            "=== FORMAT CHECKER ORGANIZED HANDOFF ===",
            "Required implementation details:",
            "Preserved analyst findings:",
        )
        for marker in critical_markers:
            self.assertIn(marker, modules_runtime)

    def test_quant_mode_codegen_rules_guards(self) -> None:
        modules_rules = (
            ROOT / "crucible" / "modules" / "section_04_web_research_and_direction.py"
        ).read_text(encoding="utf-8")

        critical_markers = (
            'raise ValueError(',
            "Unsupported mode",
            "Mode is required.",
            '_VALID_PROJECT_TYPES: frozenset = frozenset({"quant", "saas", "agent", "scientist"})',
            "Resolved mode config produced invalid project type",
            "def _validated_mode_name(",
            "def _validated_mode_project_type(",
            "lowered = _project_type_for_mode(mode_name)",
            "mode_name = _validated_mode_name(mode)",
            "def _align_direction_decision_summary_with_selection(",
            "decision = _align_direction_decision_summary_with_selection(decision)",
            "def _resolve_query_helper_mode_name(",
            "Breakdown mode_name conflicted with the explicit mode.",
            "- Quant mode must include strategy logic, a backtest runner, a trading/execution module, and a signals/results export module",
            "- Prefer concrete filenames such as strategy.py, backtest.py, trade.py, export.py, and config.py unless the prompt requires equivalent names",
            "- Do not introduce a web framework unless explicitly required by the prompt",
        )
        for marker in critical_markers:
            self.assertIn(marker, modules_rules)

        module_only_markers = (
            "mode_project_type = _validated_mode_project_type(mode_config)",
            'if mode_project_type == "quant":',
            'if mode_project_type == "quant"\n                else (\n                    "paper algorithm reproduce implementation benchmark"\n                    if mode_project_type == "scientist"\n                    else "implementation architecture risk"\n                )',
        )
        for marker in module_only_markers:
            self.assertIn(marker, modules_rules)

        removed_broken_markers = (
            'description="Fallback Quant mode"',
            'fallback = ModeRegistry.get("Quant")',
            "# Default to SaaS templates",
        )
        for marker in removed_broken_markers:
            self.assertNotIn(
                marker,
                modules_rules,
                msg=f"Mode fallback drift resurfaced in modules rules: {marker}",
            )

    def test_direction_seed_fallback_module_keeps_strict_mode_guards(self) -> None:
        modules_research = (
            ROOT / "crucible" / "modules" / "section_02_research_and_llm.py"
        ).read_text(encoding="utf-8")

        critical_markers = (
            'if mode_name not in {"quant", "saas", "agent", "scientist"}:',
            "Resolved mode config produced invalid project type",
            'elif mode_name == "saas":',
            'label="Workflow painkiller"',
            'label="Single-agent baseline"',
        )
        for marker in critical_markers:
            self.assertIn(marker, modules_research)

    def test_provider_resolution_helpers_keep_active_runtime_priority(self) -> None:
        modules_bootstrap = (
            ROOT / "crucible" / "modules" / "section_00_bootstrap_and_utils.py"
        ).read_text(encoding="utf-8")
        modules_research = (
            ROOT / "crucible" / "modules" / "section_02_research_and_llm.py"
        ).read_text(encoding="utf-8")

        ordered_markers = (
            'active_provider = globals().get("ACTIVE_LLM_PROVIDER")',
            'env_provider = str(os.environ.get("LLM_PROVIDER") or "").strip()',
        )
        for content in (modules_bootstrap, modules_research):
            first_index = content.index(ordered_markers[0])
            second_index = content.index(ordered_markers[1])
            self.assertLess(first_index, second_index)

    def test_reformat_module_keeps_local_llm_model_helper(self) -> None:
        modules_reformat = (
            ROOT / "crucible" / "modules" / "section_01_extraction_and_reformat.py"
        ).read_text(encoding="utf-8")

        critical_markers = (
            "def _reformat_llm_model_id(llm: Any) -> str:",
            "def _reformat_llm_provider_name(llm: Any) -> str:",
            'for attr in ("model", "model_name", "model_id"):',
            '"model": _reformat_llm_model_id(llm),',
            '"llm_provider": _reformat_llm_provider_name(llm),',
        )
        for marker in critical_markers:
            self.assertIn(marker, modules_reformat)

    def test_agent_retry_wrappers_present_in_modules(self) -> None:
        modules_reformat = (
            ROOT / "crucible" / "modules" / "section_01_extraction_and_reformat.py"
        ).read_text(encoding="utf-8")
        modules_direction = (
            ROOT / "crucible" / "modules" / "section_04_web_research_and_direction.py"
        ).read_text(encoding="utf-8")
        modules_runtime = (
            ROOT / "crucible" / "modules" / "section_06_runtime_quality_api.py"
        ).read_text(encoding="utf-8")

        reformat_markers = (
            "def _kickoff_reformat_crew(",
            "return kickoff_crew_with_retry(",
            '"reformat_stage": cost_trace_stage or crew_name,',
        )
        for marker in reformat_markers:
            self.assertIn(marker, modules_reformat)

        shared_markers = (
            'crew_name="llm_problem_breakdown"',
            'crew_name="smart_search_queries"',
            'crew_name="quality_review"',
            'crew_name="quality_fix"',
            'crew_name="api_version_analysis"',
        )
        for marker in shared_markers:
            self.assertIn(marker, modules_direction + modules_runtime)

    def test_codegen_and_quality_recovery_guards(self) -> None:
        modules_codegen = (
            ROOT / "crucible" / "modules" / "section_05_analysis_and_codegen.py"
        ).read_text(encoding="utf-8")
        modules_runtime = (
            ROOT / "crucible" / "modules" / "section_06_runtime_quality_api.py"
        ).read_text(encoding="utf-8")

        codegen_markers = (
            "def _build_codegen_timeout_recovery_crew(",
            "def _kickoff_codegen_with_timeout_recovery(",
            'crew_name="codegen_crew_fallback"',
        )
        for marker in codegen_markers:
            self.assertIn(marker, modules_codegen)

        self.assertNotIn('output_pydantic_model="CodeBundle"', modules_codegen)

        runtime_markers = (
            "def _recover_quality_fix_patch_from_last_raw_output(",
            "Repair the previous answer into one valid CodeBundle JSON object.",
            "Quality fix task timed out; recovered patch from the last raw output.",
            "recovered_from_last_raw_output",
        )
        for marker in runtime_markers:
            self.assertIn(marker, modules_runtime)

    def test_codegen_context_uses_budgeted_handoff(self) -> None:
        modules_runtime = (
            ROOT / "crucible" / "modules" / "section_06_runtime_quality_api.py"
        ).read_text(encoding="utf-8")

        critical_markers = (
            "def build_conditional_codegen_context(",
            "return build_budgeted_codegen_context(",
            "max_chars=CODEGEN_CONTEXT_MAX_CHARS",
            "include_analyst_findings=True",
        )
        for marker in critical_markers:
            self.assertIn(marker, modules_runtime)

    def test_failure_taxonomy_guards(self) -> None:
        modules_models = (
            ROOT / "crucible" / "modules" / "section_03_models_and_context.py"
        ).read_text(encoding="utf-8")
        modules_codegen = (
            ROOT / "crucible" / "modules" / "section_05_analysis_and_codegen.py"
        ).read_text(encoding="utf-8")
        modules_extract = (
            ROOT / "crucible" / "modules" / "section_01_extraction_and_reformat.py"
        ).read_text(encoding="utf-8")
        modules_main = (
            ROOT / "crucible" / "modules" / "section_07_selfcheck_output_main.py"
        ).read_text(encoding="utf-8")

        model_markers = (
            'EXECUTION_ERROR = "EXECUTION_ERROR"',
            "def _classify_runtime_exception_failure(",
        )
        for marker in model_markers:
            self.assertIn(marker, modules_models)

        runtime_markers = (
            "failure_type=_classify_runtime_exception_failure(e)",
            "failure_type=FailureType.CONFLICTING_OUTPUT",
            "run_failure_type = FailureType.CONFLICTING_OUTPUT",
        )
        for marker in runtime_markers:
            self.assertIn(marker, modules_codegen + modules_main)

        schema_markers = (
            '"failure_type": "NONE|JSON_INVALID|EXECUTION_ERROR|LOW_CONFIDENCE|COST_OVER_BUDGET|CONFLICTING_OUTPUT|POLICY_VIOLATION|NON_DETERMINISTIC"',
            '- failure_type: "NONE"|"JSON_INVALID"|"EXECUTION_ERROR"|"LOW_CONFIDENCE"|"COST_OVER_BUDGET"|"CONFLICTING_OUTPUT"|"POLICY_VIOLATION"|"NON_DETERMINISTIC"',
        )
        for marker in schema_markers:
            self.assertIn(marker, modules_models + modules_extract)

    def test_code_bundle_project_type_guards(self) -> None:
        modules_extract = (
            ROOT / "crucible" / "modules" / "section_01_extraction_and_reformat.py"
        ).read_text(encoding="utf-8")
        modules_codegen = (
            ROOT / "crucible" / "modules" / "section_05_analysis_and_codegen.py"
        ).read_text(encoding="utf-8")
        modules_models = (
            ROOT / "crucible" / "modules" / "section_03_models_and_context.py"
        ).read_text(encoding="utf-8")

        extract_markers = (
            "def extract_analysis_report(",
            "return _normalize_analysis_report(_extract_analysis_report_raw(result), mode=mode)",
            'normalized_project_type not in {"quant", "saas", "agent", "scientist"}',
            "return CodeBundle(project_type=normalized_project_type, files=normalized_files)",
        )
        for marker in extract_markers:
            self.assertIn(marker, modules_extract)

        codegen_markers = (
            "Project type is required. Expected one of: quant, saas, agent, scientist",
            "Unsupported project_type",
            'if normalized == "quant":',
            '    if normalized == "quant":\n        return "Quant"\n    if normalized == "scientist":\n        return "Scientist"\n    raise ValueError(',
            "def _code_bundle_mode_mismatch_reason(",
            "CodeBundle mode isolation violation:",
        )
        for marker in codegen_markers:
            self.assertIn(marker, modules_codegen)

        analysis_markers = (
            "canonical_mode_map = {",
            '"quant": "Quant"',
            "if canonical_mode is None:",
            "if expected_mode is not None and canonical_mode != expected_mode:",
            "def _canonical_mode_name_from_project_type(",
            "def _validated_mode_config(",
            "Mode registry returned config name",
            "mode_cfg = _validated_mode_config(mode)",
            "mode_config = _validated_mode_config(mode)",
            "mode_key = _project_type_for_mode(mode)",
        )
        for marker in analysis_markers:
            self.assertIn(marker, modules_models)

        code_fix_markers = ("project_type = _project_type_for_mode(mode)",)
        for marker in code_fix_markers:
            self.assertIn(marker, modules_models)

    def test_cost_reporting_keeps_billable_cost_sources(self) -> None:
        modules_models = (
            ROOT / "crucible" / "modules" / "section_03_models_and_context.py"
        ).read_text(encoding="utf-8")
        modules_output = (
            ROOT / "crucible" / "modules" / "section_07_selfcheck_output_main.py"
        ).read_text(encoding="utf-8")

        accounting_markers = (
            "alibaba_coding_plan_tokens_only",
            "openrouter_tokens_with_pricing",
            "crewai_metrics_with_pricing",
            "def _summarize_cost_source(records: List[AgentCostRecord]) -> str:",
            "float(x.get(\"total_cost_usd\", 0.0) or 0.0)",
            "int(x.get(\"total_tokens\", 0) or 0)",
            "\"models_used\": list(set(r.model_id for r in self._records if r.model_id))",
            "def reset_cost_accountant() -> None:",
        )
        for marker in accounting_markers:
            self.assertIn(marker, modules_models)

        output_markers = (
            "def _reset_pipeline_runtime_state() -> None:",
            "def _sync_librarian_debug_snapshot(run_snapshot: \"RunSnapshot\") -> None:",
            "def _resolve_runtime_model_versions(llm: Any) -> Dict[str, str]:",
            "def _apply_llm_provider_runtime(provider: Optional[str] = None) -> str:",
            "def _resolve_entry_llm_provider(",
            "def _apply_runtime_option_overrides(",
            "def _reset_runtime_option_defaults_from_env() -> None:",
            "def _resolve_runtime_entry_defaults() -> Dict[str, Any]:",
            "def _apply_local_cache_runtime_defaults_from_env() -> None:",
            "def _apply_librarian_runtime_defaults_from_env() -> None:",
            "def _apply_research_runtime_defaults_from_env() -> None:",
            "def _apply_api_version_check_runtime_defaults_from_env() -> None:",
            "def _apply_quality_runtime_defaults_from_env() -> None:",
            "def _apply_project_context_scan_defaults_from_env() -> None:",
            "def _resolve_quality_round_limit(runtime_profile: \"RuntimeProfileConfig\") -> int:",
            "def _apply_output_validation_mode_overrides() -> None:",
            "clear_openrouter_usage()",
            "clear_last_librarian_debug()",
            "reset_research_llm_cache()",
            "reset_local_llm_cache()",
            "reset_cost_accountant()",
            "reset_api_version_cache()",
            "_reset_pipeline_runtime_state()",
            "_sync_librarian_debug_snapshot(run_snapshot)",
            "module.__dict__[\"STRICT_JSON_ENABLED\"] = bool(strict_json)",
            "module.__dict__[\"LOCAL_CACHE_ENABLED\"] = bool(local_cache)",
            "module.__dict__[\"COST_TRACE_ENABLED\"] = bool(cost_trace)",
            "_apply_runtime_option_overrides(",
            "_reset_runtime_option_defaults_from_env()",
            "_apply_local_cache_runtime_defaults_from_env()",
            "_apply_librarian_runtime_defaults_from_env()",
            "_apply_research_runtime_defaults_from_env()",
            "_apply_api_version_check_runtime_defaults_from_env()",
            "_apply_quality_runtime_defaults_from_env()",
            "_apply_project_context_scan_defaults_from_env()",
            "\"api_version_check_enabled\": _resolve_api_version_check_enabled_default(),",
            "\"LOCAL_CACHE_TTL_HOURS\": defaults[\"ttl_hours\"]",
            "\"LOCAL_CACHE_PATH\": defaults[\"path\"]",
            "\"DIRECTION_REFINEMENT_MAX_ITERATIONS\": int(direction_defaults[\"max_iterations\"])",
            "\"OPENROUTER_LLM_TIMEOUT_SECONDS\": int(_resolve_openrouter_llm_timeout_seconds())",
            "\"ALIBABA_CODING_PLAN_LLM_TIMEOUT_SECONDS\": int(",
            "\"ALIBABA_CODING_PLAN_INITIAL_RESPONSE_TIMEOUT_SECONDS\": int(",
            "\"QUICK_MAX_TREE_ENTRIES\": defaults[\"quick_max_tree_entries\"]",
            "\"FULL_MAX_TOTAL_CHARS\": defaults[\"full_max_total_chars\"]",
            "\"LIBRARIAN_ENABLED\": bool(defaults[\"enabled\"]),",
            "\"API_VERSION_CHECK_MAX_LIBRARIES\": int(defaults[\"max_libraries\"]),",
            "\"API_VERSION_CHECK_CACHE_TTL_HOURS\": int(defaults[\"cache_ttl_hours\"]),",
            "\"API_VERSION_CHECK_SEVERITY_THRESHOLD\": str(defaults[\"severity_threshold\"]),",
            "\"QUALITY_JSON_RETRY_ATTEMPTS\": int(defaults[\"json_retry_attempts\"]),",
            "\"QUALITY_FIX_FUSE_CONSECUTIVE_FAILURES\": int(",
            "quality_round_limit = _resolve_quality_round_limit(runtime_profile)",
            "module.__dict__[\"CREWAI_OUTPUT_PYDANTIC\"] = crewai_output_pydantic",
            "_apply_output_validation_mode_overrides()",
            "\"primary\": _llm_model_id(llm) or _resolve_primary_model_id()",
            "\"llm_provider\": resolved_provider",
            "\"direction_judge\": _resolve_direction_judge_model_id()",
            "\"librarian\": _resolve_librarian_model_id() if LIBRARIAN_ENABLED else \"\"",
            "--provider",
            "\"llm_provider\": selected_llm_provider",
            "Cost Source: OpenRouter API (actual billing)",
            "Cost Source: OpenRouter token pricing estimate ",
            "Cost Source: CrewAI usage metrics with model pricing estimate",
            "Cost Source: Alibaba Coding Plan (tokens only, USD not tracked)",
            "Total Cost (USD):",
            "Models Used:",
        )
        for marker in output_markers:
            self.assertIn(marker, modules_output)

        modules_research = (
            ROOT / "crucible" / "modules" / "section_02_research_and_llm.py"
        ).read_text(encoding="utf-8")
        modules_extract = (
            ROOT / "crucible" / "modules" / "section_05_analysis_and_codegen.py"
        ).read_text(encoding="utf-8")
        modules_quality = (
            ROOT / "crucible" / "modules" / "section_06_runtime_quality_api.py"
        ).read_text(encoding="utf-8")
        research_markers = ("def clear_last_librarian_debug() -> None:",)
        for marker in research_markers:
            self.assertIn(marker, modules_research)
        self.assertIn("def get_last_librarian_debug() -> Dict[str, Any]:", modules_research)
        self.assertIn("def reset_research_llm_cache() -> None:", modules_research)
        self.assertIn("def _resolve_local_cache_runtime_defaults() -> Dict[str, Any]:", modules_research)
        self.assertIn("def reset_local_llm_cache() -> None:", modules_research)
        self.assertIn("def _resolve_crewai_output_pydantic_enabled() -> bool:", modules_research)
        self.assertIn("def _resolve_librarian_runtime_defaults() -> Dict[str, Any]:", modules_research)
        self.assertIn("def _resolve_direction_refinement_runtime_defaults() -> Dict[str, Any]:", modules_research)
        self.assertIn("def _resolve_openrouter_llm_timeout_seconds() -> int:", modules_research)
        self.assertIn(
            "def _resolve_alibaba_coding_plan_llm_timeout_seconds() -> int:",
            modules_research,
        )
        self.assertIn(
            "def _resolve_alibaba_coding_plan_initial_response_timeout_seconds() -> int:",
            modules_research,
        )
        self.assertIn("def _resolve_llm_timeout_seconds(provider: Optional[str] = None) -> int:", modules_research)
        self.assertIn("def _build_llm_timeout_value(", modules_research)
        self.assertIn("def _resolve_llm_provider(provider: Optional[str] = None) -> str:", modules_research)
        self.assertIn('active_provider = globals().get("ACTIVE_LLM_PROVIDER")', modules_research)
        self.assertIn("ACTIVE_OPENAI_COMPAT_PROVIDER: Optional[str] = None", modules_research)
        self.assertIn("ACTIVE_OPENAI_COMPAT_API_KEY: str = \"\"", modules_research)
        self.assertIn('resolved_from == "OPENAI_API_KEY"', modules_research)
        self.assertIn("stale_openai_compat_provider != LLM_PROVIDER_OPENROUTER", modules_research)
        self.assertIn("def _resolve_provider_model_setting_keys(", modules_research)
        self.assertIn("def _resolve_llm_base_url(provider: Optional[str] = None) -> str:", modules_research)
        self.assertIn("ALIBABA_CODING_PLAN_API_KEY", modules_research)
        self.assertIn('"llm_provider": _resolve_llm_provider(),', modules_research)
        self.assertIn('v = (_resolve_primary_model_id() or "").strip()', modules_research)
        self.assertIn("def _llm_timeout_seconds(llm: Any) -> Optional[float]:", modules_research)
        self.assertIn("def _llm_timeout_signature(llm: Any) -> Any:", modules_research)
        self.assertIn("def _llm_provider_name(llm: Any) -> str:", modules_research)
        self.assertIn(
            "and _llm_timeout_signature(_DIRECTION_JUDGE_LLM) == resolved_timeout_signature",
            modules_research,
        )
        self.assertIn(
            "and _llm_timeout_signature(_LIBRARIAN_LLM) == resolved_timeout_signature",
            modules_research,
        )
        self.assertIn("and _llm_provider_name(_DIRECTION_JUDGE_LLM) == resolved_provider", modules_research)
        self.assertIn("and _llm_provider_name(_LIBRARIAN_LLM) == resolved_provider", modules_research)
        self.assertIn("def _resolve_project_context_scan_defaults() -> Dict[str, Any]:", modules_models)
        self.assertIn("def _resolve_quality_max_rounds_default() -> int:", modules_extract)
        self.assertIn(
            "def _resolve_selective_rerun_max_attempts_default() -> int:",
            modules_extract,
        )
        self.assertIn("def _resolve_quality_runtime_defaults() -> Dict[str, Any]:", modules_extract)
        self.assertIn("def _resolve_api_version_check_runtime_defaults() -> Dict[str, Any]:", modules_extract)
        self.assertIn("def reset_api_version_cache() -> None:", modules_quality)
        self.assertIn("def _resolve_gate_control_enabled_default() -> bool:", modules_quality)
        self.assertIn("def _resolve_selective_rerun_enabled_default() -> bool:", modules_quality)
        self.assertIn("def _resolve_runtime_profile_default_name() -> str:", modules_quality)
        self.assertIn("def _build_runtime_profiles() -> Dict[str, RuntimeProfileConfig]:", modules_quality)
        self.assertIn("def _resolve_api_version_check_enabled_default() -> bool:", modules_extract)

    def test_validation_first_gate_and_codegen_markers_stay_in_sync(self) -> None:
        modules_extract = (
            ROOT / "crucible" / "modules" / "section_01_extraction_and_reformat.py"
        ).read_text(encoding="utf-8")
        modules_models = (
            ROOT / "crucible" / "modules" / "section_03_models_and_context.py"
        ).read_text(encoding="utf-8")
        modules_research = (
            ROOT / "crucible" / "modules" / "section_04_web_research_and_direction.py"
        ).read_text(encoding="utf-8")
        modules_codegen = (
            ROOT / "crucible" / "modules" / "section_05_analysis_and_codegen.py"
        ).read_text(encoding="utf-8")
        modules_runtime = (
            ROOT / "crucible" / "modules" / "section_06_runtime_quality_api.py"
        ).read_text(encoding="utf-8")

        extract_markers = (
            "- codegen_scope: 'production'|'validation'",
            "- validation_scope_reason: string|null",
            "- validation_objectives: list of strings",
        )
        for marker in extract_markers:
            self.assertIn(marker, modules_extract)

        model_markers = (
            'codegen_scope: str = Field(',
            'validation_scope_reason: Optional[str] = Field(',
            'validation_objectives: List[str] = Field(',
            'def _promote_validation_first_gate(',
            'def _align_analysis_report_with_gate_scope(',
            'Validation-first scope approved because the remaining uncertainty is exactly what the generated harness should measure:',
        )
        for marker in model_markers:
            self.assertIn(marker, modules_models)

        research_markers = (
            'def _validation_first_prompt_guidance(user_problem: str) -> str:',
            'VALIDATION-FIRST ROUTING:',
            'def _direction_option_looks_validation_first(',
        )
        for marker in research_markers:
            self.assertIn(marker, modules_research)

        codegen_markers = (
            'def _validation_scope_codegen_rule_lines(mode_config: "ModeConfig") -> List[str]:',
            'def _resolved_codegen_rule_lines(',
            'gate = _promote_validation_first_gate(',
            'report = _align_analysis_report_with_gate_scope(report, gate)',
        )
        for marker in codegen_markers:
            self.assertIn(marker, modules_codegen)

        runtime_markers = (
            "def build_conditional_codegen_context(",
            "return build_budgeted_codegen_context(",
            "max_chars=CODEGEN_CONTEXT_MAX_CHARS",
            "include_analyst_findings=True",
        )
        for marker in runtime_markers:
            self.assertIn(marker, modules_runtime)

    def test_gate_compactor_markers_present_in_modules(self) -> None:
        modules_direction = (
            ROOT / "crucible" / "modules" / "section_04_web_research_and_direction.py"
        ).read_text(encoding="utf-8")

        markers = (
            'name="gate_context_compactor"',
            'output_pydantic_model="GateContextBundle"',
            'context_task_names=["gate_context_compactor"]',
            'context_task_names=["gate_context_compactor", "gate_controller"]',
            "def _legacy_build_analysis_specs(",
            "return _build_analysis_specs_from_module(",
            '"GATE_CONTEXT_COMPACTOR_RULES": GATE_CONTEXT_COMPACTOR_RULES,',
            '"direction_feedback_enabled": "true" if direction_feedback_enabled else "false"',
        )
        for marker in markers:
            self.assertIn(marker, modules_direction)

    def test_saved_project_output_keeps_mode_recovery_guard(self) -> None:
        modules_output = (
            ROOT / "crucible" / "modules" / "section_07_selfcheck_output_main.py"
        ).read_text(encoding="utf-8")

        critical_markers = (
            "def _resolve_saved_mode_name(",
            "_resolve_saved_mode_name(None, code, run_meta_payload",
            "_normalize_analysis_report(",
            'run_meta_payload["mode"] = saved_mode_name',
            '"mode_used": saved_mode_name',
            "project_fix_failure_note = \"Project fix output missing CodeBundle.\"",
            "project_fix_outcome = (",
            'else "mode_mismatch"',
        )
        for marker in critical_markers:
            self.assertIn(marker, modules_output)

        output_markers = (
            'gate_snapshot = dict(getattr(result, "gate_context_snapshot", {}) or {})',
            "effective_gate_payload = dict(gate_snapshot)",
            'effective_gate_payload.get("codegen_scope") or "production"',
            'effective_gate_payload.get("validation_scope_reason")',
            'effective_gate_payload.get("validation_objectives") or []',
            'payload["gate_decision"] = effective_gate_payload',
            '"codegen_scope": "CodeGen Scope"',
            '"validation_scope_reason": "Validation Scope Reason"',
            '"validation_objectives": "Validation Objectives"',
        )
        for marker in output_markers:
            self.assertIn(marker, modules_output)

    def test_cost_tracking_bootstrap_keeps_openrouter_pricing_guards(self) -> None:
        modules_bootstrap = (
            ROOT / "crucible" / "modules" / "section_00_bootstrap_and_utils.py"
        ).read_text(encoding="utf-8")
        modules_research = (
            ROOT / "crucible" / "modules" / "section_02_research_and_llm.py"
        ).read_text(encoding="utf-8")

        critical_markers = (
            "ALIBABA_CODING_PLAN_API_BASE_URL = \"https://coding-intl.dashscope.aliyuncs.com/v1\"",
            "LLM_PROVIDER_ALIBABA_CODING_PLAN = \"alibaba_coding_plan\"",
            "ACTIVE_LLM_PROVIDER = _normalize_llm_provider(os.environ.get(\"LLM_PROVIDER\"))",
            "def _resolve_usage_provider(provider: Optional[str] = None) -> str:",
            'active_provider = globals().get("ACTIVE_LLM_PROVIDER")',
            "def _canonicalize_usage_model_id(model_id: str, provider: Optional[str] = None) -> str:",
            "def _host_to_usage_provider(host: str) -> Optional[str]:",
            "alibaba_coding_plan_tokens_only",
            "OPENROUTER_MODEL_PRICING: Dict[str, Tuple[float, float]] = {",
            "DEFAULT_MODEL_PRICING = (1.00 / 1_000_000, 3.00 / 1_000_000)",
            "OPENROUTER_MODEL_ALIASES: Dict[str, str] = {",
            "def _canonicalize_model_id(model_id: str) -> str:",
            "def _iter_model_id_candidates(model_id: str) -> List[str]:",
            "def _estimate_cache_savings(",
            "def _merge_usage_cost_source(existing_source: str, new_source: str) -> str:",
            "def _merge_usage_model_ids(existing_model_id: str, new_model_id: str) -> str:",
            "def _direction_debug_llm_model_id(llm: Any) -> str:",
            "def _capture_openrouter_usage_from_http_response(response: Any) -> bool:",
            "def set_openrouter_usage(",
            'cost_source = "openrouter_tokens_with_pricing"',
            'cost_source = "alibaba_coding_plan_tokens_only"',
            'model_id = _merge_usage_model_ids(existing.get("model_id", ""), model_id)',
            'cost_source = _merge_usage_cost_source(existing.get("cost_source", "estimated"), cost_source)',
            "def _get_model_pricing(model_id: str) -> Tuple[float, float]:",
            '"llm_model_id": _direction_debug_llm_model_id(llm),',
            '"direction_judge_model_id": _direction_debug_llm_model_id(direction_judge_llm),',
            "records.append(dict(ctx_data))",
            '            "cost_source": "crewai_metrics_with_pricing",',
            '            "cost_source": _token_only_cost_source,',
            "def get_openrouter_http_interceptor() -> Optional[Any]:",
        )
        for marker in critical_markers:
            self.assertIn(marker, modules_bootstrap)

        removed_broken_markers = (
            "cached_tokens * total_cost / total_tokens",
        )
        for marker in removed_broken_markers:
            self.assertNotIn(
                marker,
                modules_bootstrap,
                msg=f"Broken cost-accounting formula resurfaced in modules bootstrap: {marker}",
            )
        self.assertNotIn('os.environ["LLM_PROVIDER"] = resolved_provider', modules_bootstrap)
        self.assertNotIn('os.environ["LLM_PROVIDER"] = resolved_provider', modules_research)

        storage_markers = (
            "def _workspace_local_crewai_fallback_root() -> str:",
            'workspace_root = os.path.dirname(os.path.dirname(os.path.dirname(module_file)))',
            'return os.path.realpath(os.path.join(workspace_root, ".crewai_local_appdata"))',
        )
        for marker in storage_markers:
            self.assertIn(marker, modules_bootstrap)

    def test_gitignore_excludes_local_staging_and_temp_audit_artifacts(self) -> None:
        gitignore = (ROOT / ".gitignore").read_text(encoding="utf-8")
        self.assertIn("skill_staging/", gitignore)
        self.assertIn(".tmp_bandit.json", gitignore)

    def test_ci_workflow_only_runs_for_pull_requests_and_manual_dispatch(self) -> None:
        ci_workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(
            encoding="utf-8"
        )
        expected_markers = (
            "on:",
            "pull_request:",
            "workflow_dispatch:",
        )
        for marker in expected_markers:
            self.assertIn(
                marker,
                ci_workflow,
                msg=f"CI workflow trigger guard missing marker: {marker}",
            )
        self.assertNotIn(
            "push:",
            ci_workflow,
            msg="CI workflow must not auto-run on push events.",
        )

    def test_source_tree_does_not_contain_embedded_local_crewai_state(self) -> None:
        source_root = ROOT / "crucible"
        embedded_state_dirs = [
            path.relative_to(ROOT).as_posix()
            for path in source_root.rglob(".crewai_local_appdata")
        ]
        self.assertEqual(
            embedded_state_dirs,
            [],
            msg=(
                "Local CrewAI state must stay outside the source tree: "
                f"{embedded_state_dirs}"
            ),
        )

        embedded_secret_files = [
            path.relative_to(ROOT).as_posix()
            for path in source_root.rglob("secret.key")
        ]
        self.assertEqual(
            embedded_secret_files,
            [],
            msg=(
                "Credential files must not be stored under crucible: "
                f"{embedded_secret_files}"
            ),
        )

    def test_runtime_import_trampolines_use_explicit_package_guards(self) -> None:
        expected_package_guards = {
            "crucible/analysis.py": 'if __package__ == "crucible":',
            "crucible/bootstrap.py": 'if __package__ == "crucible":',
            "crucible/cli.py": 'if __package__ == "crucible":',
            "crucible/models.py": 'if __package__ == "crucible":',
            "crucible/module_runtime.py": 'if __package__ == "crucible":',
            "crucible/quality.py": 'if __package__ == "crucible":',
            "crucible/research.py": 'if __package__ == "crucible":',
            "crucible/runtime_api.py": 'if __package__ == "crucible":',
            "crucible/_runtime_loader.py": 'if __package__ == "crucible":',
            "crucible/__main__.py": 'if __package__ == "crucible":',
            "crucible/resilience.py": 'if __package__ == "crucible":',
            "crucible/web_research/http_clients.py": 'if __package__ == "crucible.web_research":',
            "crucible/modules/section_02_research_and_llm.py": 'if __package__ == "crucible.modules":',
            "crucible/modules/section_04_web_research_and_direction.py": 'if __package__ == "crucible.modules":',
            "crucible/modules/section_05_analysis_and_codegen.py": 'if __package__ == "crucible.modules":',
            "crucible/modules/section_07_selfcheck_output_main.py": 'if __package__ == "crucible.modules":',
        }

        import re as _re
        for relative_path, guard in expected_package_guards.items():
            text = (ROOT / relative_path).read_text(encoding="utf-8")
            self.assertIn(guard, text, msg=f"Missing explicit package guard in {relative_path}")
            # Check specifically for the anti-pattern: `except ImportError:` used as an
            # import-fallback trampoline (i.e., the handler body is another import
            # statement).  Legitimate `except ImportError: pass` (or similar non-import
            # handler bodies) in function bodies are intentional and should not be flagged.
            self.assertIsNone(
                _re.search(r"except ImportError:\s*\n[ \t]+(?:from |import )", text),
                msg=(
                    f"Broad ImportError import-fallback trampoline should not appear "
                    f"in {relative_path}"
                ),
            )


if __name__ == "__main__":
    unittest.main()
