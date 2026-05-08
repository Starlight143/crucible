import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


class TestCrucibleCrewRuntime(unittest.TestCase):
    def test_root_launcher_exists(self) -> None:
        launcher = ROOT / "run_crucible.py"
        self.assertTrue(launcher.is_file())

    def test_package_root_all_exports_resolve_to_runtime_or_public_shims(self) -> None:
        import crucible as package_root
        from crucible.module_runtime import get_runtime

        runtime = get_runtime()
        exported = getattr(package_root, "__all__", None)

        self.assertIsInstance(exported, list)
        self.assertTrue(exported, msg="crucible package should expose public names")

        for name in exported:
            self.assertTrue(
                hasattr(package_root, name),
                msg=f"crucible package is missing exported name {name}",
            )
            exported_value = getattr(package_root, name)
            runtime_has_name = hasattr(runtime, name)
            if runtime_has_name:
                self.assertEqual(
                    exported_value,
                    getattr(runtime, name),
                    msg=f"crucible.{name} drifted from module_runtime",
                )
            elif name == "get_runtime":
                self.assertIs(exported_value, package_root.get_runtime)
            else:
                self.fail(f"Package export {name} is not backed by module_runtime")

    def test_public_shim_modules_all_exports_resolve_to_runtime_members(self) -> None:
        from crucible import analysis, bootstrap, cli, models, quality, research
        from crucible.module_runtime import get_runtime

        runtime = get_runtime()
        shim_modules = (
            analysis,
            bootstrap,
            cli,
            models,
            quality,
            research,
        )

        for shim_module in shim_modules:
            exported = getattr(shim_module, "__all__", None)
            self.assertIsInstance(exported, list)
            self.assertTrue(exported, msg=f"{shim_module.__name__} should expose public names")
            for name in exported:
                self.assertTrue(
                    hasattr(shim_module, name),
                    msg=f"{shim_module.__name__} is missing exported name {name}",
                )
                self.assertTrue(
                    hasattr(runtime, name),
                    msg=f"Runtime is missing shim-exported name {name} from {shim_module.__name__}",
                )
                self.assertEqual(
                    getattr(shim_module, name),
                    getattr(runtime, name),
                    msg=f"{shim_module.__name__}.{name} drifted from module_runtime",
                )

    def test_public_shim_modules_export_runtime_symbols(self) -> None:
        from crucible import analysis, bootstrap, cli, models, quality, research
        from crucible.module_runtime import get_runtime

        runtime = get_runtime()

        self.assertIs(analysis.build_crew, runtime.build_crew)
        self.assertIs(analysis.run_codegen_stage, runtime.run_codegen_stage)

        self.assertEqual(bootstrap.PROJECT_ROOT, runtime.PROJECT_ROOT)
        self.assertEqual(bootstrap.LOADED_ENV_FILE, runtime.LOADED_ENV_FILE)
        self.assertIs(bootstrap.load_api_key, runtime.load_api_key)
        self.assertIs(bootstrap.save_project_output, runtime.save_project_output)

        self.assertIs(cli.main, runtime.main)
        self.assertIs(cli.run_self_check, runtime.run_self_check)

        self.assertIs(models.AnalysisReport, runtime.AnalysisReport)
        self.assertIs(models.GateDecision, runtime.GateDecision)
        self.assertIs(models.AgentCostRecord, runtime.AgentCostRecord)

        self.assertIs(quality.run_quality_loop, runtime.run_quality_loop)
        self.assertIs(quality.run_runtime_validation, runtime.run_runtime_validation)

        self.assertIs(research.run_direction_debate, runtime.run_direction_debate)
        self.assertIs(research.extract_research_context, runtime.extract_research_context)

    def test_runtime_trampolines_share_module_runtime_singleton(self) -> None:
        from crucible._runtime_loader import load_runtime
        from crucible.module_runtime import get_runtime as get_module_runtime
        from crucible.runtime_api import get_runtime as get_runtime_api

        module_runtime = get_module_runtime()
        api_runtime = get_runtime_api()
        loader_runtime = load_runtime()

        self.assertIs(api_runtime, module_runtime)
        self.assertIs(loader_runtime, module_runtime)
        self.assertEqual(api_runtime.PROJECT_ROOT, str(ROOT))
        self.assertEqual(loader_runtime.PROJECT_ROOT, str(ROOT))
        self.assertTrue(hasattr(api_runtime, "OpenRouterUsageData"))
        self.assertTrue(hasattr(loader_runtime, "OpenRouterUsageData"))

    def test_runtime_uses_workspace_root(self) -> None:
        from crucible.module_runtime import get_runtime

        runtime = get_runtime()
        self.assertEqual(runtime.PROJECT_ROOT, str(ROOT))

    def test_runtime_env_defaults_to_root_env(self) -> None:
        from crucible.module_runtime import get_runtime

        runtime = get_runtime()
        env_path = ROOT / ".env"
        if env_path.is_file():
            self.assertEqual(runtime.LOADED_ENV_FILE, str(env_path))
        else:
            configured = os.environ.get("CRUCIBLE_ENV_FILE", "").strip()
            if configured:
                candidate = Path(configured)
                if not candidate.is_absolute():
                    candidate = (ROOT / candidate).resolve()
                self.assertEqual(runtime.LOADED_ENV_FILE, str(candidate))

    def test_quant_codegen_rules_require_backtest_stack(self) -> None:
        from crucible.module_runtime import get_runtime

        runtime = get_runtime()
        quant_mode = runtime.ModeRegistry.get("Quant")
        self.assertIsNotNone(quant_mode)
        rule_text = "\n".join(runtime._mode_codegen_rule_lines(quant_mode))
        lowered = rule_text.lower()
        self.assertIn("must include strategy logic", lowered)
        self.assertIn("backtest runner", lowered)
        self.assertIn("trading/execution module", lowered)
        self.assertIn("signals/results export module", lowered)
        self.assertIn("backtest.py", lowered)

    def test_task_builder_applies_max_input_chars_budget(self) -> None:
        from crucible.web_research import crew_factory

        class _DummyTask:
            def __init__(self, **kwargs) -> None:
                self.__dict__.update(kwargs)

        task_spec = SimpleNamespace(
            name="budgeted_task",
            description_template="HEADER\n{body}",
            agent_name="worker",
            expected_output="json",
            context_task_names=[],
            output_pydantic_model=None,
            max_input_chars=40,
        )
        task = crew_factory.build_task_from_spec(
            task_spec,
            agents={"worker": object()},
            task_lookup={},
            template_vars={"body": "x" * 200},
            render_prompt_template=lambda template, vars_: template.format(**vars_),
            strict_json_enabled=False,
            crewai_output_pydantic=False,
            output_model_by_name=lambda _name: None,
            task_cls=_DummyTask,
        )
        self.assertLessEqual(len(task.description), 40)
        self.assertIn("truncated", task.description)
        self.assertEqual(task._prompt_chars, len(task.description))
        self.assertEqual(task._prompt_budget_chars, 40)
        self.assertTrue(task._prompt_truncated)

    def test_limit_reformat_input_keeps_head_and_tail(self) -> None:
        from crucible.module_runtime import get_runtime

        runtime = get_runtime()
        raw_text = ("HEAD-" * 2500) + ("MID-" * 2500) + ("TAIL-" * 2500)

        limited = runtime._limit_reformat_input(raw_text)

        self.assertLessEqual(len(limited), runtime.REFORMAT_INPUT_MAX_CHARS + 64)
        self.assertIn("HEAD-", limited)
        self.assertIn("TAIL-", limited)
        self.assertIn("truncated middle", limited)

    def test_quality_context_uses_focused_code_scope_and_full_path_inventory(self) -> None:
        from crucible.module_runtime import get_runtime

        runtime = get_runtime()
        code_bundle = runtime.CodeBundle(
            project_type="saas",
            files=[
                runtime.GeneratedFile(path="app.py", content="print('app')\n" * 1200),
                runtime.GeneratedFile(path="router.py", content="print('router')\n" * 1200),
                runtime.GeneratedFile(path="models.py", content="print('models')\n" * 1200),
                runtime.GeneratedFile(path="services.py", content="print('services')\n" * 1200),
                runtime.GeneratedFile(path="db.py", content="print('db')\n" * 1200),
                runtime.GeneratedFile(path="cli.py", content="print('cli')\n" * 1200),
                runtime.GeneratedFile(path="utils.py", content="print('utils')\n" * 1200),
                runtime.GeneratedFile(path="jobs.py", content="print('jobs')\n" * 1200),
                runtime.GeneratedFile(path="tests/test_app.py", content="assert True\n" * 1200),
                runtime.GeneratedFile(path="README.md", content="# readme\n" * 1200),
            ],
        )

        prompt = runtime.build_quality_context(
            "review this project",
            None,
            code_bundle,
            runtime_log="Traceback in app.py\nrouter.py\n",
            round_idx=0,
        )

        self.assertIn("=== FOCUSED CODE FILES ===", prompt)
        self.assertIn("=== ALL FILE PATHS (NO CONTENT) ===", prompt)
        self.assertIn("app.py", prompt)
        self.assertIn("router.py", prompt)
        self.assertIn("README.md", prompt)
        self.assertLess(prompt.count("\n--- "), len(code_bundle.files) + 3)

    def test_quality_fixer_context_uses_compact_review_summary(self) -> None:
        from crucible.module_runtime import get_runtime

        runtime = get_runtime()
        code_bundle = runtime.CodeBundle(
            project_type="agent",
            files=[
                runtime.GeneratedFile(path="main.py", content="print('main')\n"),
                runtime.GeneratedFile(path="worker.py", content="print('worker')\n"),
            ],
        )
        review_report = runtime.ReviewReport(
            passes=False,
            summary="Need targeted fixes.",
            issues=[
                runtime.ReviewIssue(
                    severity="high",
                    category="logic",
                    description="Fix the main execution path.",
                    file="main.py",
                    suggestion="Guard the invalid branch.",
                ),
                runtime.ReviewIssue(
                    severity="medium",
                    category="bug",
                    description="Worker retries leak state.",
                    file="worker.py",
                    suggestion="Reset mutable state before rerun.",
                ),
            ],
        )

        prompt = runtime.build_quality_fixer_context(
            "repair the agent",
            None,
            code_bundle,
            review_report,
            runtime_log="main.py failed",
            affected_files={"main.py"},
            round_idx=1,
        )

        review_summary = runtime._format_review_report_for_prompt(review_report)
        self.assertIn("=== THIS ROUND REVIEW SUMMARY ===", prompt)
        self.assertIn("Issues count: 2", review_summary)
        self.assertIn("main.py", review_summary)
        self.assertIn("worker.py", review_summary)

    def test_extract_relevant_paths_from_runtime_log_includes_importerror_module_paths(self) -> None:
        from crucible.module_runtime import get_runtime

        runtime = get_runtime()
        code_bundle = runtime.CodeBundle(
            project_type="quant",
            files=[
                runtime.GeneratedFile(
                    path="main.py", content="from models import ValidationResult\n"
                ),
                runtime.GeneratedFile(
                    path="models.py", content="class CalibrationResult:\n    pass\n"
                ),
                runtime.GeneratedFile(
                    path="symbol_mapper.py", content="print('ok')\n"
                ),
            ],
        )
        runtime_log = (
            "[import main.py] exit_code=1\n"
            "Traceback (most recent call last):\n"
            '  File "C:\\tmp\\qa_validate\\main.py", line 30, in <module>\n'
            "    from models import ValidationResult\n"
            "ImportError: cannot import name 'ValidationResult' from 'models' "
            "(C:\\tmp\\qa_validate\\models.py)\n"
        )

        paths = runtime._extract_relevant_paths_from_runtime_log(runtime_log, code_bundle)

        self.assertEqual(paths, {"main.py", "models.py"})

    def test_build_budgeted_codegen_context_respects_exact_budget(self) -> None:
        from crucible.module_runtime import get_runtime

        runtime = get_runtime()
        analysis_report = runtime.AnalysisReport(
            project_name="budgeted_codegen",
            summary="s" * 900,
            consensus="c" * 900,
            disagreement="d" * 900,
            experiments=[runtime.Experiment(goal="g" * 200, criteria="k" * 200)],
            score=87,
            mode_used="Quant",
            risk_level="Medium",
            analyst_findings={
                "research": "r" * 400,
                "ops": "o" * 400,
                "critic": "z" * 400,
            },
            codegen_handoff_summary="h" * 400,
            codegen_requirements=["req " + ("x" * 120)] * 2,
            codegen_constraints=["constraint " + ("y" * 120)] * 2,
            codegen_validation_focus=["focus " + ("v" * 120)] * 2,
            gate_context_snapshot={"ready_for_codegen": True, "codegen_scope": "production"},
        )
        gate_decision = runtime.GateDecision(
            consensus="gate consensus",
            disagreement="gate disagreement",
            experiments=[runtime.Experiment(goal="verify", criteria="pass")],
            ready_for_codegen=True,
            blocking_risks=["risk " + ("b" * 500)],
        )
        context = runtime.build_budgeted_codegen_context(
            gate_decision,
            analysis_report,
            max_chars=5200,
            include_analyst_findings=False,
        )
        self.assertLessEqual(len(context), 5200)
        self.assertIn("APPROVED ANALYSIS HANDOFF", context)
        self.assertIn("LIVE GATE CONTROLLER APPROVAL", context)

    def test_run_codegen_stage_uses_single_staged_pipeline(self) -> None:
        from crucible.module_runtime import get_runtime

        runtime = get_runtime()
        sentinel_bundle = runtime.CodeBundle(
            project_type="quant",
            files=[runtime.GeneratedFile(path="main.py", content="print('ok')\n")],
        )
        sentinel_result = {"pipeline": "staged_codegen", "batch_count": 2}
        pipeline_mock = mock.Mock(return_value=(sentinel_result, sentinel_bundle))
        legacy_mock = mock.Mock(side_effect=AssertionError("legacy codegen path should not run"))
        globals_dict = runtime.run_codegen_stage.__globals__
        original_enabled = globals_dict.get("CODEGEN_STAGED_ENABLED")
        original_pipeline = globals_dict.get("_run_staged_codegen_pipeline")
        original_legacy = globals_dict.get("_LEGACY_RUN_CODEGEN_STAGE")
        try:
            globals_dict["CODEGEN_STAGED_ENABLED"] = True
            globals_dict["_run_staged_codegen_pipeline"] = pipeline_mock
            globals_dict["_LEGACY_RUN_CODEGEN_STAGE"] = legacy_mock
            snapshot = runtime.RunSnapshot(run_id="staged-codegen-test")
            result, bundle = runtime.run_codegen_stage(
                "build a quant strategy",
                mode="Quant",
                language_hint="Python",
                llm=object(),
                analysis_report=None,
                gate_decision=None,
                run_snapshot=snapshot,
            )
        finally:
            globals_dict["CODEGEN_STAGED_ENABLED"] = original_enabled
            globals_dict["_run_staged_codegen_pipeline"] = original_pipeline
            globals_dict["_LEGACY_RUN_CODEGEN_STAGE"] = original_legacy
        self.assertEqual(result, sentinel_result)
        self.assertEqual(bundle, sentinel_bundle)
        pipeline_mock.assert_called_once()
        legacy_mock.assert_not_called()
        self.assertTrue(
            any(record.get("stage") == "codegen_crew.kickoff" for record in snapshot.stage_records)
        )

    def test_staged_codegen_cost_aggregates_all_substage_usage_records(self) -> None:
        from crucible.module_runtime import get_runtime

        runtime = get_runtime()
        runtime.clear_openrouter_usage()
        runtime.reset_cost_accountant()

        sentinel_bundle = runtime.CodeBundle(
            project_type="quant",
            files=[runtime.GeneratedFile(path="main.py", content="print('ok')\n")],
        )

        def _fake_pipeline(*args, **kwargs):
            runtime.set_openrouter_usage(
                {
                    "prompt_tokens": 100,
                    "completion_tokens": 40,
                    "total_tokens": 140,
                    "cost": 0.0014,
                },
                model_id="openai/gpt-5.4",
                accumulate=False,
                provider="openrouter",
            )
            runtime.set_openrouter_usage(
                {
                    "prompt_tokens": 60,
                    "completion_tokens": 20,
                    "total_tokens": 80,
                    "cost": 0.0008,
                },
                model_id="openai/gpt-5.4-mini",
                accumulate=False,
                provider="openrouter",
            )
            return {"pipeline": "staged_codegen", "batch_count": 2}, sentinel_bundle

        globals_dict = runtime.run_codegen_stage.__globals__
        original_enabled = globals_dict.get("CODEGEN_STAGED_ENABLED")
        original_pipeline = globals_dict.get("_run_staged_codegen_pipeline")
        try:
            globals_dict["CODEGEN_STAGED_ENABLED"] = True
            globals_dict["_run_staged_codegen_pipeline"] = _fake_pipeline
            result, bundle = runtime.run_codegen_stage(
                "build a quant strategy",
                mode="Quant",
                language_hint="Python",
                llm=object(),
                analysis_report=None,
                gate_decision=None,
                run_snapshot=runtime.RunSnapshot(run_id="staged-cost-test"),
            )
        finally:
            globals_dict["CODEGEN_STAGED_ENABLED"] = original_enabled
            globals_dict["_run_staged_codegen_pipeline"] = original_pipeline

        self.assertEqual(result["batch_count"], 2)
        self.assertEqual(bundle, sentinel_bundle)
        summary = runtime.get_cost_accountant().get_summary()
        self.assertEqual(summary["total_tokens"], 220)
        self.assertAlmostEqual(summary["total_cost_usd"], 0.0022, places=12)
        self.assertEqual(summary["total_executions"], 1)
        self.assertTrue(summary["models_used"])
        self.assertIn("openai/gpt-5.4", summary["models_used"])
        self.assertEqual(runtime.get_last_openrouter_usage().total_tokens, 0)
        self.assertEqual(runtime.get_usage_records(), [])

    def test_staged_codegen_success_cost_uses_pipeline_prompt_tokens(self) -> None:
        from crucible.module_runtime import get_runtime

        runtime = get_runtime()
        sentinel_bundle = runtime.CodeBundle(
            project_type="quant",
            files=[runtime.GeneratedFile(path="main.py", content="print('ok')\n")],
        )
        helper_globals = runtime.run_codegen_stage.__globals__
        original_values = {
            "CODEGEN_STAGED_ENABLED": helper_globals.get("CODEGEN_STAGED_ENABLED"),
            "_run_staged_codegen_pipeline": helper_globals.get("_run_staged_codegen_pipeline"),
            "_record_codegen_usage_slice": helper_globals.get("_record_codegen_usage_slice"),
            "_cost_trace": helper_globals.get("_cost_trace"),
            "log_event": helper_globals.get("log_event"),
            "log_exception": helper_globals.get("log_exception"),
        }
        recorded: list[dict[str, object]] = []

        try:
            helper_globals["CODEGEN_STAGED_ENABLED"] = True
            helper_globals["_run_staged_codegen_pipeline"] = lambda *args, **kwargs: (
                {
                    "pipeline": "staged_codegen",
                    "batch_count": 2,
                    "prompt_total_chars": 2400,
                },
                sentinel_bundle,
            )
            helper_globals["_record_codegen_usage_slice"] = (
                lambda **kwargs: recorded.append(dict(kwargs))
            )
            helper_globals["_cost_trace"] = lambda *args, **kwargs: None
            helper_globals["log_event"] = lambda *args, **kwargs: None
            helper_globals["log_exception"] = lambda *args, **kwargs: None

            result, bundle = runtime.run_codegen_stage(
                "build a quant strategy",
                mode="Quant",
                language_hint="Python",
                llm=object(),
                analysis_report=None,
                gate_decision=None,
                run_snapshot=runtime.RunSnapshot(run_id="staged-prompt-success"),
            )
        finally:
            for key, value in original_values.items():
                helper_globals[key] = value

        self.assertEqual(bundle, sentinel_bundle)
        self.assertEqual(result["prompt_total_chars"], 2400)
        self.assertTrue(recorded)
        self.assertEqual(recorded[-1]["stage"], "codegen_crew.kickoff")
        self.assertEqual(recorded[-1]["fallback_input_tokens"], 800)
        self.assertEqual(recorded[-1]["outcome"], "success")

    def test_staged_codegen_failure_cost_uses_pipeline_prompt_tokens(self) -> None:
        from crucible.module_runtime import get_runtime

        runtime = get_runtime()
        helper_globals = runtime.run_codegen_stage.__globals__
        original_values = {
            "CODEGEN_STAGED_ENABLED": helper_globals.get("CODEGEN_STAGED_ENABLED"),
            "_run_staged_codegen_pipeline": helper_globals.get("_run_staged_codegen_pipeline"),
            "_record_codegen_usage_slice": helper_globals.get("_record_codegen_usage_slice"),
            "_cost_trace": helper_globals.get("_cost_trace"),
            "log_event": helper_globals.get("log_event"),
            "log_exception": helper_globals.get("log_exception"),
        }
        recorded: list[dict[str, object]] = []
        failure = ConnectionError("boom")
        setattr(failure, "_staged_codegen_prompt_chars", 2100)

        try:
            helper_globals["CODEGEN_STAGED_ENABLED"] = True
            helper_globals["_run_staged_codegen_pipeline"] = (
                lambda *args, **kwargs: (_ for _ in ()).throw(failure)
            )
            helper_globals["_record_codegen_usage_slice"] = (
                lambda **kwargs: recorded.append(dict(kwargs))
            )
            helper_globals["_cost_trace"] = lambda *args, **kwargs: None
            helper_globals["log_event"] = lambda *args, **kwargs: None
            helper_globals["log_exception"] = lambda *args, **kwargs: None

            result, bundle = runtime.run_codegen_stage(
                "build a quant strategy",
                mode="Quant",
                language_hint="Python",
                llm=object(),
                analysis_report=None,
                gate_decision=None,
                run_snapshot=runtime.RunSnapshot(run_id="staged-prompt-failure"),
            )
        finally:
            for key, value in original_values.items():
                helper_globals[key] = value

        self.assertIsNone(result)
        self.assertIsNone(bundle)
        self.assertTrue(recorded)
        self.assertEqual(recorded[-1]["stage"], "codegen_crew.kickoff")
        self.assertEqual(recorded[-1]["fallback_input_tokens"], 700)
        self.assertEqual(recorded[-1]["outcome"], "execution_error")

    def test_pipeline_runtime_reset_clears_log_context_and_circuit_breakers(self) -> None:
        from crucible.module_runtime import get_runtime
        from crucible.resilience import get_circuit_breaker
        from crucible.runtime_logging import current_log_context, update_log_context

        runtime = get_runtime()
        update_log_context(run_id="old-run", stage="old-stage", extra_marker="stale")
        breaker = get_circuit_breaker(
            "stale_breaker",
            failure_threshold=1,
            recovery_timeout_seconds=60.0,
        )
        breaker.record_failure()

        runtime._reset_pipeline_runtime_state()

        self.assertEqual(current_log_context(), {})
        fresh_breaker = get_circuit_breaker(
            "stale_breaker",
            failure_threshold=1,
            recovery_timeout_seconds=60.0,
        )
        self.assertEqual(fresh_breaker.failure_count, 0)
        self.assertIsNone(fresh_breaker.opened_at)

    def test_mode_rule_helpers_reject_invalid_mode_config_name(self) -> None:
        from crucible.module_runtime import get_runtime

        runtime = get_runtime()
        bad_mode = SimpleNamespace(name="   ")
        with self.assertRaisesRegex(ValueError, "invalid project type"):
            runtime._mode_code_fix_rule_lines(bad_mode)
        with self.assertRaisesRegex(ValueError, "invalid project type"):
            runtime._mode_codegen_rule_lines(bad_mode)
        with self.assertRaisesRegex(ValueError, "invalid project type"):
            runtime._mode_gate_controller_guidance(bad_mode)

    def test_mode_resolution_is_case_insensitive_but_rejects_unknown_modes(self) -> None:
        from crucible.module_runtime import get_runtime

        runtime = get_runtime()
        self.assertEqual(runtime._get_mode_config("quant").name, "Quant")
        self.assertEqual(runtime._get_mode_config("SAAS").name, "SaaS")
        self.assertEqual(runtime._get_mode_config("agent").name, "Agent")
        with self.assertRaisesRegex(ValueError, "Unsupported mode"):
            runtime._get_mode_config("unknown-mode")
        with self.assertRaisesRegex(ValueError, "Mode is required"):
            runtime._get_mode_config("")

    def test_project_type_resolution_is_case_insensitive_but_rejects_unknown_values(self) -> None:
        from crucible.module_runtime import get_runtime

        runtime = get_runtime()
        self.assertEqual(runtime._mode_name_from_project_type("quant"), "Quant")
        self.assertEqual(runtime._mode_name_from_project_type("SAAS"), "SaaS")
        self.assertEqual(runtime._mode_name_from_project_type("Agent"), "Agent")
        with self.assertRaisesRegex(ValueError, "Unsupported project_type"):
            runtime._mode_name_from_project_type("unknown-mode")
        with self.assertRaisesRegex(ValueError, "Project type is required"):
            runtime._mode_name_from_project_type("")

    def test_code_bundle_mode_mismatch_reason_is_reported_for_cross_mode_outputs(self) -> None:
        from crucible.module_runtime import get_runtime

        runtime = get_runtime()
        bundle = runtime.CodeBundle(
            project_type="saas",
            files=[runtime.GeneratedFile(path="main.py", content="print('ok')\n")],
        )
        self.assertIn(
            "requested mode 'Quant' expects project_type 'quant'",
            runtime._code_bundle_mode_mismatch_reason(bundle, "Quant"),
        )
        self.assertIsNone(runtime._code_bundle_mode_mismatch_reason(bundle, "SaaS"))

    def test_extract_analysis_report_canonicalizes_mode_and_rejects_cross_mode_outputs(
        self,
    ) -> None:
        from crucible.module_runtime import get_runtime

        runtime = get_runtime()
        payload = {
            "project_name": "mode_guard",
            "summary": "summary",
            "consensus": "consensus",
            "disagreement": "disagreement",
            "experiments": [],
            "score": 80,
            "mode_used": "quant",
            "risk_level": "Medium",
        }
        report = runtime.extract_analysis_report(payload, mode="Quant")
        self.assertIsNotNone(report)
        self.assertEqual(report.mode_used, "Quant")
        self.assertIsNone(
            runtime.extract_analysis_report({**payload, "mode_used": "SaaS"}, mode="Quant")
        )

    def test_project_type_for_mode_rejects_invalid_registry_output(self) -> None:
        from crucible.module_runtime import get_runtime

        runtime = get_runtime()
        globals_dict = runtime._project_type_for_mode.__globals__
        original = globals_dict["_get_mode_config"]

        class _BadModeConfig:
            name = "   "

        globals_dict["_get_mode_config"] = lambda _mode: _BadModeConfig()
        try:
            with self.assertRaisesRegex(ValueError, "invalid project type"):
                runtime._project_type_for_mode("Quant")
        finally:
            globals_dict["_get_mode_config"] = original

    def test_build_code_fix_crew_rejects_invalid_registry_output(self) -> None:
        from crucible.module_runtime import get_runtime

        runtime = get_runtime()
        globals_dict = runtime.build_code_fix_crew.__globals__
        original = globals_dict["_project_type_for_mode"]

        def _raise_invalid_project_type(_mode: str) -> str:
            raise ValueError(
                "Resolved mode config produced invalid project type '   '. Expected one of: quant, saas, agent"
            )

        globals_dict["_project_type_for_mode"] = _raise_invalid_project_type
        try:
            with self.assertRaisesRegex(ValueError, "invalid project type"):
                runtime.build_code_fix_crew(
                    "fix the failing worker",
                    mode="Agent",
                    language_hint="English",
                    llm=None,
                )
        finally:
            globals_dict["_project_type_for_mode"] = original

    def test_build_code_fix_crew_sets_prompt_metadata(self) -> None:
        from crucible.module_runtime import get_runtime

        runtime = get_runtime()
        globals_dict = runtime.build_code_fix_crew.__globals__
        original_values = {
            "Agent": globals_dict.get("Agent"),
            "Task": globals_dict.get("Task"),
            "Crew": globals_dict.get("Crew"),
        }

        class _StubAgent:
            def __init__(self, **kwargs):
                self.kwargs = kwargs

        class _StubTask:
            def __init__(self, description, agent=None, expected_output=None):
                self.description = description
                self.agent = agent
                self.expected_output = expected_output

        class _StubCrew:
            def __init__(self, agents, tasks, process=None, verbose=None):
                self.agents = agents
                self.tasks = tasks
                self.process = process
                self.verbose = verbose

        try:
            globals_dict["Agent"] = _StubAgent
            globals_dict["Task"] = _StubTask
            globals_dict["Crew"] = _StubCrew
            crew = runtime.build_code_fix_crew(
                "fix the failing worker",
                mode="Agent",
                language_hint="English",
                llm=None,
            )
        finally:
            for key, value in original_values.items():
                globals_dict[key] = value

        self.assertGreater(getattr(crew, "_prompt_total_chars", 0), 0)
        self.assertEqual(
            getattr(crew, "_prompt_total_chars", 0),
            len(str(getattr(crew.tasks[0], "description", "") or "")),
        )
        prompt_hashes = getattr(crew, "_prompt_hashes", {})
        self.assertIsInstance(prompt_hashes, dict)
        self.assertTrue(prompt_hashes.get("project_fix"))

    def test_build_codegen_manifest_crew_accepts_literal_schema_key_guidance(self) -> None:
        from crucible.module_runtime import get_runtime

        runtime = get_runtime()
        globals_dict = runtime._build_codegen_manifest_crew.__globals__
        original_values = {
            "build_budgeted_codegen_context": globals_dict.get(
                "build_budgeted_codegen_context"
            ),
            "_build_codegen_single_task_crew": globals_dict.get(
                "_build_codegen_single_task_crew"
            ),
        }

        captured: dict[str, object] = {}

        def _stub_build_codegen_single_task_crew(**kwargs):
            task_spec = kwargs["task_spec"]
            template_vars = kwargs["template_vars"]
            rendered = runtime._render_task_description_with_budget(task_spec, template_vars)
            captured["description"] = rendered
            return SimpleNamespace(
                tasks=[SimpleNamespace(description=rendered)],
                _prompt_total_chars=len(rendered),
                _prompt_hashes={"codegen_manifest": "stub"},
            )

        try:
            globals_dict["build_budgeted_codegen_context"] = (
                lambda *_args, **_kwargs: "Approved implementation context."
            )
            globals_dict["_build_codegen_single_task_crew"] = (
                _stub_build_codegen_single_task_crew
            )
            crew = runtime._build_codegen_manifest_crew(
                "Build a validation-first quant harness.",
                mode="Quant",
                language_hint="English",
                llm=None,
                analysis_report=SimpleNamespace(),
                gate_decision=SimpleNamespace(),
                context_max_chars=4000,
                max_input_chars=12000,
            )
        finally:
            for key, value in original_values.items():
                globals_dict[key] = value

        description = str(captured.get("description", "") or "")
        self.assertIn(
            "files: list of objects with keys path, purpose, depends_on, must_contain",
            description,
        )
        self.assertIn(
            "generation_batches: list of objects with keys name, objective, files",
            description,
        )
        self.assertIsNotNone(crew)

    def test_validate_batch_bundle_rejects_duplicate_normalized_paths(self) -> None:
        from crucible.module_runtime import get_runtime

        runtime = get_runtime()
        batch_plan = runtime.CodegenBatchPlan(
            name="batch_1",
            objective="generate the entrypoint",
            files=["src/main.py"],
        )
        bundle = runtime.CodeBundle(
            project_type="quant",
            files=[
                runtime.GeneratedFile(path="src/main.py", content="print('a')\n"),
                runtime.GeneratedFile(path="./src/main.py", content="print('b')\n"),
            ],
        )

        validated, failure_note = runtime._validate_batch_bundle(
            bundle,
            batch_plan=batch_plan,
            mode="Quant",
        )

        self.assertIsNone(validated)
        self.assertEqual(
            failure_note,
            "Codegen batch returned duplicate file paths: src/main.py",
        )

    def test_validate_batch_bundle_prunes_redundant_previously_completed_files(self) -> None:
        from crucible.module_runtime import get_runtime

        runtime = get_runtime()
        batch_plan = runtime.CodegenBatchPlan(
            name="batch_4",
            objective="generate exchange base adapter",
            files=["exchanges/base.py"],
        )
        current_bundle = runtime.CodeBundle(
            project_type="quant",
            files=[
                runtime.GeneratedFile(path="schema.py", content="SCHEMA = True\n"),
                runtime.GeneratedFile(path="models.py", content="MODELS = True\n"),
            ],
        )
        bundle = runtime.CodeBundle(
            project_type="quant",
            files=[
                runtime.GeneratedFile(path="schema.py", content="SCHEMA = True\n"),
                runtime.GeneratedFile(path="models.py", content="MODELS = True\n"),
                runtime.GeneratedFile(
                    path="exchanges/base.py",
                    content="class BaseExchange:\n    pass\n",
                ),
            ],
        )

        validated, failure_note = runtime._validate_batch_bundle(
            bundle,
            batch_plan=batch_plan,
            mode="Quant",
            current_bundle=current_bundle,
        )

        self.assertIsNotNone(validated)
        self.assertIsNone(failure_note)
        self.assertEqual([file.path for file in validated.files], ["exchanges/base.py"])

    def test_salvage_codegen_batch_bundle_keeps_in_scope_files_and_drops_extras(
        self,
    ) -> None:
        """Lenient-output mode salvages partial batch output instead of
        raising and losing 6+ minutes of LLM work.  The salvage helper must:
            (1) keep every safely-typed planned file, even if it has Python
                syntax errors (user can fix manually)
            (2) drop hallucinated files outside the batch plan's scope
            (3) report missing planned files and syntax-error files
        """
        from crucible.module_runtime import get_runtime

        runtime = get_runtime()
        batch_plan = runtime.CodegenBatchPlan(
            name="batch_1",
            objective="generate scaffolding",
            files=["a.py", "b.py", "c.py"],
        )
        raw = runtime.CodeBundle(
            project_type="quant",
            files=[
                runtime.GeneratedFile(path="a.py", content="x = 1\n"),
                # Syntax error — must still be salvaged
                runtime.GeneratedFile(
                    path="b.py", content="def f(:\n  return 2\n"
                ),
                # Hallucinated extra — must be dropped
                runtime.GeneratedFile(
                    path="hallucinated.py", content="print(1)\n"
                ),
                # c.py is missing entirely
            ],
        )
        salvaged, missing, syn_err = runtime._salvage_codegen_batch_bundle(
            raw, batch_plan=batch_plan, project_type="quant"
        )
        self.assertIsNotNone(salvaged)
        self.assertEqual(
            {f.path for f in (salvaged.files or [])}, {"a.py", "b.py"}
        )
        self.assertEqual(missing, ["c.py"])
        self.assertEqual(syn_err, ["b.py"])

        # Pathological inputs return None so the caller still raises rather
        # than silently writing empty code to disk.
        none_b, _, _ = runtime._salvage_codegen_batch_bundle(
            None, batch_plan=batch_plan, project_type="quant"
        )
        self.assertIsNone(none_b)

        empty_plan = runtime.CodegenBatchPlan(
            name="empty", objective="x", files=[]
        )
        none_b2, _, _ = runtime._salvage_codegen_batch_bundle(
            raw, batch_plan=empty_plan, project_type="quant"
        )
        self.assertIsNone(none_b2)

    def test_salvage_staged_codegen_bundle_keeps_partial_pipeline_output(
        self,
    ) -> None:
        """Pipeline-level salvage must keep planned files (even if some are
        broken or some are missing) so the user gets debuggable output rather
        than a full pipeline abort after a multi-minute LLM run.
        """
        from crucible.module_runtime import get_runtime

        runtime = get_runtime()
        manifest = runtime.CodegenManifest(
            project_type="quant",
            architecture_summary="test scaffold",
            entrypoints=["main.py"],
            shared_constraints=[],
            files=[
                runtime.CodegenFilePlan(
                    path="main.py",
                    purpose="entry",
                    depends_on=[],
                    must_contain=[],
                ),
                runtime.CodegenFilePlan(
                    path="util.py",
                    purpose="helpers",
                    depends_on=[],
                    must_contain=[],
                ),
                runtime.CodegenFilePlan(
                    path="config.py",
                    purpose="config",
                    depends_on=[],
                    must_contain=[],
                ),
            ],
            generation_batches=[],
        )
        current = runtime.CodeBundle(
            project_type="quant",
            files=[
                runtime.GeneratedFile(path="main.py", content="print(1)\n"),
                # Syntax error — kept, user fixes manually
                runtime.GeneratedFile(path="util.py", content="def(\n"),
                # config.py never generated
            ],
        )
        salvaged, missing, syn_err = runtime._salvage_staged_codegen_bundle(
            current, manifest=manifest, mode="Quant"
        )
        self.assertIsNotNone(salvaged)
        self.assertEqual(
            {f.path for f in (salvaged.files or [])}, {"main.py", "util.py"}
        )
        self.assertEqual(missing, ["config.py"])
        self.assertEqual(syn_err, ["util.py"])

        # When current_bundle is None or empty, salvage returns None.
        none_p, _, _ = runtime._salvage_staged_codegen_bundle(
            None, manifest=manifest, mode="Quant"
        )
        self.assertIsNone(none_p)

    @pytest.mark.slow
    def test_codegen_lenient_output_env_var_defaults_on_and_can_be_disabled(
        self,
    ) -> None:
        """``CODEGEN_LENIENT_OUTPUT`` defaults to True (favour producing
        partial output over raising on any validation failure).  Setting it
        to ``0`` reverts to the historical strict behaviour for CI gates
        that require complete bundles.
        """
        import os
        import subprocess
        import sys

        # Default (env var unset) → lenient mode ON
        env = {k: v for k, v in os.environ.items() if k != "CODEGEN_LENIENT_OUTPUT"}
        proc = subprocess.run(
            [
                sys.executable,
                "-c",
                (
                    "import crucible.modules.section_05_analysis_and_codegen as s5;"
                    "assert s5.CODEGEN_LENIENT_OUTPUT is True;"
                    "print('default-lenient OK')"
                ),
            ],
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("default-lenient OK", proc.stdout)

        # Explicit disable
        env["CODEGEN_LENIENT_OUTPUT"] = "0"
        proc = subprocess.run(
            [
                sys.executable,
                "-c",
                (
                    "import crucible.modules.section_05_analysis_and_codegen as s5;"
                    "assert s5.CODEGEN_LENIENT_OUTPUT is False;"
                    "print('strict-disable OK')"
                ),
            ],
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("strict-disable OK", proc.stdout)

        # Common truthy/falsy aliases should also work
        for raw, expected in (
            ("true", True),
            ("yes", True),
            ("on", True),
            ("false", False),
            ("no", False),
            ("off", False),
        ):
            env["CODEGEN_LENIENT_OUTPUT"] = raw
            proc = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    (
                        f"import crucible.modules.section_05_analysis_and_codegen as s5;"
                        f"assert s5.CODEGEN_LENIENT_OUTPUT is {expected!r}, "
                        f"f'value={{s5.CODEGEN_LENIENT_OUTPUT}} expected={expected!r}'"
                    ),
                ],
                env=env,
                capture_output=True,
                text=True,
                timeout=60,
            )
            self.assertEqual(
                proc.returncode,
                0,
                f"raw={raw!r} expected={expected!r}\nstderr={proc.stderr}",
            )

    def test_synthesize_fallback_manifest_produces_valid_single_batch_manifest(
        self,
    ) -> None:
        """When LLM manifest synthesis fails, the never-terminate fallback
        helper must return a valid :class:`CodegenManifest` with at least one
        batch and one file so the downstream batch loop has something to
        execute.  The architecture summary should be drawn from the analysis
        report when available.
        """
        from crucible.module_runtime import get_runtime

        runtime = get_runtime()

        analysis = runtime.AnalysisReport(
            project_name="x",
            summary="A short summary",
            consensus="c",
            disagreement="d",
            experiments=[],
            score=80,
            mode_used="Quant",
            risk_level="Low",
            codegen_handoff_summary="Build a hello-world script.",
        )
        manifest = runtime._synthesize_fallback_manifest(
            mode="Quant", analysis_report=analysis, user_problem="hello"
        )
        self.assertEqual(manifest.project_type, "quant")
        self.assertGreaterEqual(len(list(manifest.generation_batches or [])), 1)
        self.assertGreaterEqual(len(list(manifest.files or [])), 1)
        # Batch files must equal the planned-files list (single-batch design)
        batch = manifest.generation_batches[0]
        plan_paths = {p.path for p in (manifest.files or [])}
        self.assertEqual(set(batch.files), plan_paths)
        # Architecture summary should reference the handoff brief
        self.assertIn("hello-world", manifest.architecture_summary)
        # Quant default entrypoints
        self.assertIn("main.py", batch.files)
        self.assertIn("requirements.txt", batch.files)

        # No analysis report → still works, with generic architecture summary
        manifest2 = runtime._synthesize_fallback_manifest(
            mode="SaaS", analysis_report=None, user_problem=""
        )
        self.assertEqual(manifest2.project_type, "saas")
        self.assertIn("Dockerfile", [f.path for f in (manifest2.files or [])])
        self.assertTrue(manifest2.architecture_summary)

    def test_synthesize_skeleton_bundle_includes_readme_and_entrypoints(
        self,
    ) -> None:
        """Skeleton fallback must always emit a CodeBundle with at least
        README.md plus the project type's default entrypoints, so the user
        always has a non-empty saved_projects/.../code/ directory.
        """
        from crucible.module_runtime import get_runtime

        runtime = get_runtime()
        manifest = runtime.CodegenManifest(
            project_type="saas",
            architecture_summary="x",
            entrypoints=["main.py", "Dockerfile"],
            shared_constraints=[],
            files=[
                runtime.CodegenFilePlan(
                    path="main.py",
                    purpose="entry",
                    depends_on=[],
                    must_contain=[],
                ),
            ],
            generation_batches=[],
        )
        skeleton = runtime._synthesize_skeleton_bundle(
            manifest=manifest,
            mode="SaaS",
            failure_reasons=[
                "Batch 1 (init): RuntimeError: provider down",
                "Finalize: missing planned files",
            ],
            user_problem="A SaaS task",
        )
        self.assertEqual(skeleton.project_type, "saas")
        paths = {f.path for f in skeleton.files}
        # README must always be present
        self.assertIn("README.md", paths)
        # Entrypoints from manifest
        self.assertIn("main.py", paths)
        self.assertIn("Dockerfile", paths)
        # Python project types always get a requirements.txt
        self.assertIn("requirements.txt", paths)
        # README should embed both failure reasons and the user problem
        readme = next(f.content for f in skeleton.files if f.path == "README.md")
        self.assertIn("provider down", readme)
        self.assertIn("missing planned files", readme)
        self.assertIn("A SaaS task", readme)

        # Python stub must exit non-zero — never confused with working code
        main_py = next(f.content for f in skeleton.files if f.path == "main.py")
        self.assertIn("sys.exit", main_py)
        self.assertIn("[skeleton]", main_py)

        # No manifest at all → falls back to default entrypoints
        skeleton2 = runtime._synthesize_skeleton_bundle(
            manifest=None,
            mode="Quant",
            failure_reasons=[],
            user_problem="",
        )
        paths2 = {f.path for f in skeleton2.files}
        self.assertIn("README.md", paths2)
        self.assertIn("main.py", paths2)
        self.assertIn("requirements.txt", paths2)

    def test_skeleton_stub_content_per_extension(self) -> None:
        """Each file extension produces a syntactically valid stub that
        clearly identifies itself as a skeleton fallback so it cannot be
        mistaken for working code.
        """
        from crucible.module_runtime import get_runtime

        runtime = get_runtime()

        py_stub = runtime._skeleton_stub_content_for("main.py")
        # Python stub must be parseable
        compile(py_stub, "main.py", "exec")
        self.assertIn("[skeleton]", py_stub)

        dockerfile_stub = runtime._skeleton_stub_content_for("Dockerfile")
        self.assertIn("FROM python", dockerfile_stub)

        req_stub = runtime._skeleton_stub_content_for("requirements.txt")
        self.assertIn("Skeleton fallback", req_stub)

        json_stub = runtime._skeleton_stub_content_for("data.json")
        import json as _json
        parsed = _json.loads(json_stub)
        self.assertTrue(parsed.get("skeleton_fallback"))

        yaml_stub = runtime._skeleton_stub_content_for("config.yaml")
        self.assertIn("skeleton_fallback", yaml_stub)

        toml_stub = runtime._skeleton_stub_content_for("pyproject.toml")
        self.assertIn("skeleton_fallback", toml_stub)

        sh_stub = runtime._skeleton_stub_content_for("run.sh")
        self.assertIn("#!/usr/bin/env bash", sh_stub)
        self.assertIn("exit 1", sh_stub)

        unknown_stub = runtime._skeleton_stub_content_for("data.bin")
        self.assertIn("Skeleton fallback", unknown_stub)

    def test_default_entrypoints_for_project_type(self) -> None:
        """Per-project-type entrypoint defaults must be deterministic so the
        skeleton fallback never produces empty file lists for valid modes.
        """
        from crucible.module_runtime import get_runtime

        runtime = get_runtime()

        self.assertEqual(
            runtime._default_entrypoints_for_project_type("saas"),
            ["main.py", "requirements.txt", "Dockerfile"],
        )
        for pt in ("quant", "agent", "scientist"):
            self.assertEqual(
                runtime._default_entrypoints_for_project_type(pt),
                ["main.py", "requirements.txt"],
            )
        # Unknown / empty project type → safe Python default
        self.assertEqual(
            runtime._default_entrypoints_for_project_type("unknown"),
            ["main.py", "requirements.txt"],
        )
        self.assertEqual(
            runtime._default_entrypoints_for_project_type(""),
            ["main.py", "requirements.txt"],
        )

    @pytest.mark.slow
    def test_codegen_never_terminate_env_vars_default_on_and_can_be_disabled(
        self,
    ) -> None:
        """``CODEGEN_FALLBACK_MANIFEST``, ``CODEGEN_BATCH_SKIP_ON_ERROR`` and
        ``CODEGEN_SKELETON_FALLBACK`` all default to True so the pipeline
        never terminates on any validation failure.  Each one can be flipped
        to ``0`` independently for CI gates that demand strict behaviour.
        """
        import os
        import subprocess
        import sys

        flag_names = (
            "CODEGEN_FALLBACK_MANIFEST",
            "CODEGEN_BATCH_SKIP_ON_ERROR",
            "CODEGEN_SKELETON_FALLBACK",
        )

        # Default (all unset) → all True
        env = {k: v for k, v in os.environ.items() if k not in flag_names}
        check_default = (
            "import crucible.modules.section_05_analysis_and_codegen as s5;"
            "assert s5.CODEGEN_FALLBACK_MANIFEST is True;"
            "assert s5.CODEGEN_BATCH_SKIP_ON_ERROR is True;"
            "assert s5.CODEGEN_SKELETON_FALLBACK is True;"
            "print('defaults OK')"
        )
        proc = subprocess.run(
            [sys.executable, "-c", check_default],
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("defaults OK", proc.stdout)

        # Independent disable
        for disabled in flag_names:
            env_d = dict(env)
            env_d[disabled] = "0"
            check_disabled = (
                "import crucible.modules.section_05_analysis_and_codegen as s5;"
                f"v = getattr(s5, {disabled!r});"
                f"assert v is False, f'{disabled} expected False, got ' + repr(v);"
                "print('disabled OK')"
            )
            proc = subprocess.run(
                [sys.executable, "-c", check_disabled],
                env=env_d,
                capture_output=True,
                text=True,
                timeout=60,
            )
            self.assertEqual(
                proc.returncode,
                0,
                f"flag={disabled}\nstderr={proc.stderr}",
            )

    def test_run_staged_codegen_pipeline_skips_failed_batch_and_continues(
        self,
    ) -> None:
        """Pipeline orchestrator must skip a batch that raises and continue
        with the next batch when ``CODEGEN_BATCH_SKIP_ON_ERROR`` is on.  The
        result_payload must record the failure under ``batch_failures`` so
        observability is preserved.
        """
        from unittest import mock

        from crucible.module_runtime import get_runtime
        import crucible.modules.section_05_analysis_and_codegen as s5

        runtime = get_runtime()

        manifest = runtime.CodegenManifest(
            project_type="quant",
            architecture_summary="x",
            entrypoints=["a.py"],
            shared_constraints=[],
            files=[
                runtime.CodegenFilePlan(
                    path="a.py",
                    purpose="entry",
                    depends_on=[],
                    must_contain=[],
                ),
                runtime.CodegenFilePlan(
                    path="b.py",
                    purpose="helpers",
                    depends_on=[],
                    must_contain=[],
                ),
            ],
            generation_batches=[
                runtime.CodegenBatchPlan(
                    name="b1", objective="x", files=["a.py"]
                ),
                runtime.CodegenBatchPlan(
                    name="b2", objective="y", files=["b.py"]
                ),
            ],
        )

        good_bundle = runtime.CodeBundle(
            project_type="quant",
            files=[runtime.GeneratedFile(path="a.py", content="x = 1\n")],
        )

        call_log = []

        def _fake_manifest_stage(*_a, **_kw):
            return manifest, 1000

        def _fake_batch_stage(
            *,
            batch_plan,
            batch_index,
            **_kw,
        ):
            call_log.append((batch_index, batch_plan.name))
            if batch_index == 1:
                return good_bundle, 500
            raise RuntimeError("simulated provider 500")

        with mock.patch.object(s5, "CODEGEN_BATCH_RETRY_MAX_ATTEMPTS", 1), \
             mock.patch.object(s5, "CODEGEN_REPAIR_LOOP_MAX_ATTEMPTS", 0), \
             mock.patch.object(
                s5, "_run_codegen_manifest_stage", side_effect=_fake_manifest_stage
            ), mock.patch.object(
                s5,
                "_run_codegen_batch_stage",
                side_effect=lambda *a, **kw: _fake_batch_stage(**kw),
            ):
            payload, bundle = s5._run_staged_codegen_pipeline(
                "test problem",
                mode="Quant",
                language_hint="python",
                llm=mock.MagicMock(),
                analysis_report=None,
                gate_decision=None,
                run_snapshot=None,
                scope="mvp",
            )

        # Both batches were attempted (skip-on-error did not break the loop).
        # Retry and repair-loop are disabled in this test via env-var patching,
        # so each batch is attempted exactly once.
        self.assertEqual([c[0] for c in call_log], [1, 2])
        # Output bundle is non-None and contains the batch-1 file
        self.assertIsNotNone(bundle)
        self.assertEqual({f.path for f in bundle.files}, {"a.py"})
        # batch_failures records the second batch's RuntimeError
        self.assertIn("batch_failures", payload)
        self.assertEqual(len(payload["batch_failures"]), 1)
        self.assertEqual(payload["batch_failures"][0]["batch_index"], 2)
        self.assertEqual(payload["batch_failures"][0]["error_type"], "RuntimeError")
        # Pipeline ran in degraded mode (b.py was missing → lenient salvage)
        self.assertTrue(payload.get("degraded"))

    def test_run_staged_codegen_pipeline_emits_skeleton_when_all_batches_fail(
        self,
    ) -> None:
        """When every batch fails and lenient salvage finds nothing, the
        pipeline must emit the skeleton bundle (README + entrypoint stubs)
        rather than raising.  ``saved_projects/.../code/`` must never be empty.
        """
        from unittest import mock

        from crucible.module_runtime import get_runtime

        runtime = get_runtime()

        manifest = runtime.CodegenManifest(
            project_type="saas",
            architecture_summary="all-fail scenario",
            entrypoints=["main.py", "Dockerfile"],
            shared_constraints=[],
            files=[
                runtime.CodegenFilePlan(
                    path="main.py",
                    purpose="entry",
                    depends_on=[],
                    must_contain=[],
                ),
            ],
            generation_batches=[
                runtime.CodegenBatchPlan(
                    name="b1", objective="x", files=["main.py"]
                ),
            ],
        )

        def _fake_manifest_stage(*_a, **_kw):
            return manifest, 0

        def _fake_batch_stage_fails(**_kw):
            raise RuntimeError("LLM provider unreachable")

        import crucible.modules.section_05_analysis_and_codegen as s5

        with mock.patch.object(s5, "CODEGEN_BATCH_RETRY_MAX_ATTEMPTS", 1), \
             mock.patch.object(s5, "CODEGEN_REPAIR_LOOP_MAX_ATTEMPTS", 0), \
             mock.patch.object(
                s5, "_run_codegen_manifest_stage", side_effect=_fake_manifest_stage
            ), mock.patch.object(
                s5,
                "_run_codegen_batch_stage",
                side_effect=lambda *a, **kw: _fake_batch_stage_fails(**kw),
            ):
            payload, bundle = s5._run_staged_codegen_pipeline(
                "all-fail test",
                mode="SaaS",
                language_hint="python",
                llm=mock.MagicMock(),
                analysis_report=None,
                gate_decision=None,
                run_snapshot=None,
                scope="mvp",
            )

        # Skeleton fallback was triggered — bundle is non-None, has README +
        # entrypoint stubs, and is flagged as a skeleton fallback in payload.
        self.assertIsNotNone(bundle)
        paths = {f.path for f in bundle.files}
        self.assertIn("README.md", paths)
        self.assertIn("main.py", paths)
        self.assertTrue(payload.get("skeleton_fallback"))
        self.assertTrue(payload.get("degraded"))
        self.assertEqual(payload["degraded_reason"], "skeleton_fallback")
        # The README must reference the original LLM error
        readme = next(f.content for f in bundle.files if f.path == "README.md")
        self.assertIn("LLM provider unreachable", readme)

    def test_run_staged_codegen_pipeline_falls_back_when_manifest_raises(
        self,
    ) -> None:
        """When the manifest stage raises an unexpected exception, the
        pipeline must synthesise a fallback manifest and continue with the
        batch loop instead of aborting.  Final output should be at least the
        skeleton bundle.
        """
        from unittest import mock

        from crucible.module_runtime import get_runtime

        runtime = get_runtime()

        def _fake_manifest_stage_raises(*_a, **_kw):
            raise RuntimeError("manifest LLM 401")

        def _fake_batch_stage_fails(**_kw):
            # Force batch failures too so pipeline ends in skeleton fallback
            raise RuntimeError("batch LLM 401")

        import crucible.modules.section_05_analysis_and_codegen as s5

        with mock.patch.object(s5, "CODEGEN_MANIFEST_RETRY_MAX_ATTEMPTS", 1), \
             mock.patch.object(s5, "CODEGEN_BATCH_RETRY_MAX_ATTEMPTS", 1), \
             mock.patch.object(s5, "CODEGEN_REPAIR_LOOP_MAX_ATTEMPTS", 0), \
             mock.patch.object(
                s5,
                "_run_codegen_manifest_stage",
                side_effect=_fake_manifest_stage_raises,
            ), mock.patch.object(
                s5,
                "_run_codegen_batch_stage",
                side_effect=lambda *a, **kw: _fake_batch_stage_fails(**kw),
            ):
            payload, bundle = s5._run_staged_codegen_pipeline(
                "manifest-raises test",
                mode="Quant",
                language_hint="python",
                llm=mock.MagicMock(),
                analysis_report=None,
                gate_decision=None,
                run_snapshot=None,
                scope="mvp",
            )

        self.assertIsNotNone(bundle)
        # At minimum, skeleton README + entrypoints should be present
        paths = {f.path for f in bundle.files}
        self.assertIn("README.md", paths)
        self.assertTrue(payload.get("skeleton_fallback"))

    def test_identify_codegen_repair_targets_returns_missing_and_syntax_errors(
        self,
    ) -> None:
        """``_identify_codegen_repair_targets`` must surface every manifest
        path that is absent from the bundle and every existing file with
        Python syntax errors.  These are the inputs the pipeline-level
        repair loop targets for active regeneration.
        """
        from crucible.module_runtime import get_runtime

        runtime = get_runtime()

        manifest = runtime.CodegenManifest(
            project_type="quant",
            architecture_summary="x",
            entrypoints=["main.py"],
            shared_constraints=[],
            files=[
                runtime.CodegenFilePlan(
                    path="main.py", purpose="entry", depends_on=[], must_contain=[]
                ),
                runtime.CodegenFilePlan(
                    path="util.py", purpose="util", depends_on=[], must_contain=[]
                ),
                runtime.CodegenFilePlan(
                    path="config.py",
                    purpose="config",
                    depends_on=[],
                    must_contain=[],
                ),
            ],
            generation_batches=[],
        )
        bundle = runtime.CodeBundle(
            project_type="quant",
            files=[
                runtime.GeneratedFile(path="main.py", content="x = 1\n"),
                # util.py has a Python syntax error
                runtime.GeneratedFile(path="util.py", content="def f(:\n  pass\n"),
                # config.py is missing
            ],
        )
        missing, syntax = runtime._identify_codegen_repair_targets(
            bundle, manifest=manifest
        )
        self.assertEqual(missing, ["config.py"])
        self.assertEqual(syntax, ["util.py"])

        # None bundle → every planned file is missing, no syntax errors
        missing_n, syntax_n = runtime._identify_codegen_repair_targets(
            None, manifest=manifest
        )
        self.assertEqual(missing_n, ["config.py", "main.py", "util.py"])
        self.assertEqual(syntax_n, [])

    def test_run_codegen_repair_loop_fixes_missing_files_when_llm_succeeds(
        self,
    ) -> None:
        """Pipeline repair loop must regenerate missing planned files via
        a focused supplement batch and merge them into the cumulative bundle.
        After successful repair, ``_identify_codegen_repair_targets`` returns
        empty for both missing and syntax errors.
        """
        from unittest import mock

        from crucible.module_runtime import get_runtime
        import crucible.modules.section_05_analysis_and_codegen as s5

        runtime = get_runtime()

        manifest = runtime.CodegenManifest(
            project_type="quant",
            architecture_summary="x",
            entrypoints=["main.py"],
            shared_constraints=[],
            files=[
                runtime.CodegenFilePlan(
                    path="main.py", purpose="entry", depends_on=[], must_contain=[]
                ),
                runtime.CodegenFilePlan(
                    path="util.py", purpose="util", depends_on=[], must_contain=[]
                ),
            ],
            generation_batches=[],
        )
        partial = runtime.CodeBundle(
            project_type="quant",
            files=[
                runtime.GeneratedFile(path="main.py", content="x = 1\n"),
                # util.py is missing
            ],
        )

        # Simulated LLM success on first repair attempt: generates util.py
        def _fake_repair_batch_stage(*, batch_plan, **_kw):
            self.assertIn("util.py", batch_plan.files)
            return (
                runtime.CodeBundle(
                    project_type="quant",
                    files=[
                        runtime.GeneratedFile(path="util.py", content="y = 2\n")
                    ],
                ),
                250,
            )

        with mock.patch.object(
            s5,
            "_run_codegen_batch_stage",
            side_effect=lambda *a, **kw: _fake_repair_batch_stage(**kw),
        ):
            repaired, chars, history = s5._run_codegen_repair_loop(
                user_problem="x",
                mode="Quant",
                language_hint="python",
                llm=mock.MagicMock(),
                analysis_report=None,
                gate_decision=None,
                manifest=manifest,
                current_bundle=partial,
                run_snapshot=None,
                scope="mvp",
                max_attempts=3,
            )

        self.assertIsNotNone(repaired)
        self.assertEqual(
            {f.path for f in repaired.files}, {"main.py", "util.py"}
        )
        # First attempt fixed util.py; second attempt should record
        # ``no_targets_remaining`` and exit.
        outcomes = [h.get("outcome") for h in history]
        self.assertEqual(outcomes[0], "merged")
        self.assertIn("util.py", history[0].get("fixed", []))
        self.assertEqual(history[0].get("still_broken"), [])
        self.assertGreater(chars, 0)

    def test_run_codegen_repair_loop_anti_thrash_breaks_on_no_progress(
        self,
    ) -> None:
        """When the LLM keeps returning the same broken set across attempts,
        the repair loop must stop early instead of burning the full budget.
        """
        from unittest import mock

        from crucible.module_runtime import get_runtime
        import crucible.modules.section_05_analysis_and_codegen as s5

        runtime = get_runtime()

        manifest = runtime.CodegenManifest(
            project_type="quant",
            architecture_summary="x",
            entrypoints=["main.py"],
            shared_constraints=[],
            files=[
                runtime.CodegenFilePlan(
                    path="missing.py",
                    purpose="m",
                    depends_on=[],
                    must_contain=[],
                ),
            ],
            generation_batches=[],
        )
        partial = runtime.CodeBundle(
            project_type="quant",
            files=[runtime.GeneratedFile(path="main.py", content="x = 1\n")],
        )

        # Repair always returns nothing — missing.py stays missing forever.
        def _no_progress(**_kw):
            return (
                runtime.CodeBundle(project_type="quant", files=[]),
                100,
            )

        call_count = [0]

        def _wrapped(*a, **kw):
            call_count[0] += 1
            return _no_progress(**kw)

        with mock.patch.object(
            s5, "_run_codegen_batch_stage", side_effect=_wrapped
        ):
            repaired, _chars, history = s5._run_codegen_repair_loop(
                user_problem="x",
                mode="Quant",
                language_hint="python",
                llm=mock.MagicMock(),
                analysis_report=None,
                gate_decision=None,
                manifest=manifest,
                current_bundle=partial,
                run_snapshot=None,
                scope="mvp",
                max_attempts=5,
            )

        # missing.py should still be missing
        self.assertNotIn(
            "missing.py", {f.path for f in (repaired.files or [])}
        )
        # Loop should have exited early due to anti-thrash, not consumed all 5
        self.assertLess(
            call_count[0],
            5,
            f"Repair loop should anti-thrash break early, got {call_count[0]} calls",
        )
        # Exactly one of the history entries should record the thrash break
        outcomes = [h.get("outcome") for h in history]
        self.assertIn("no_progress_thrash_break", outcomes)

    def test_run_codegen_repair_loop_continues_through_llm_exceptions(
        self,
    ) -> None:
        """If the repair LLM call raises, the loop must record the exception
        and continue to the next attempt rather than aborting.
        """
        from unittest import mock

        from crucible.module_runtime import get_runtime
        import crucible.modules.section_05_analysis_and_codegen as s5

        runtime = get_runtime()

        manifest = runtime.CodegenManifest(
            project_type="quant",
            architecture_summary="x",
            entrypoints=["main.py"],
            shared_constraints=[],
            files=[
                runtime.CodegenFilePlan(
                    path="main.py", purpose="entry", depends_on=[], must_contain=[]
                ),
                runtime.CodegenFilePlan(
                    path="util.py", purpose="util", depends_on=[], must_contain=[]
                ),
            ],
            generation_batches=[],
        )
        partial = runtime.CodeBundle(
            project_type="quant",
            files=[runtime.GeneratedFile(path="main.py", content="x = 1\n")],
        )

        attempt_log = []

        def _flaky(*, batch_plan, **_kw):
            attempt_log.append(batch_plan.name)
            if len(attempt_log) == 1:
                raise RuntimeError("LLM 500")
            # Second attempt succeeds
            return (
                runtime.CodeBundle(
                    project_type="quant",
                    files=[
                        runtime.GeneratedFile(path="util.py", content="y = 1\n")
                    ],
                ),
                300,
            )

        with mock.patch.object(
            s5,
            "_run_codegen_batch_stage",
            side_effect=lambda *a, **kw: _flaky(**kw),
        ):
            repaired, _chars, history = s5._run_codegen_repair_loop(
                user_problem="x",
                mode="Quant",
                language_hint="python",
                llm=mock.MagicMock(),
                analysis_report=None,
                gate_decision=None,
                manifest=manifest,
                current_bundle=partial,
                run_snapshot=None,
                scope="mvp",
                max_attempts=3,
            )

        # Both attempts ran (loop did not abort on exception)
        self.assertEqual(len(attempt_log), 2)
        # util.py was repaired on attempt 2
        self.assertIn("util.py", {f.path for f in (repaired.files or [])})
        outcomes = [h.get("outcome") for h in history]
        self.assertIn("exception", outcomes)
        self.assertIn("merged", outcomes)

    def test_run_codegen_repair_loop_disabled_when_max_attempts_zero(
        self,
    ) -> None:
        """``max_attempts=0`` must short-circuit the repair loop and
        return the bundle untouched with empty history.
        """
        from unittest import mock

        from crucible.module_runtime import get_runtime
        import crucible.modules.section_05_analysis_and_codegen as s5

        runtime = get_runtime()
        manifest = runtime.CodegenManifest(
            project_type="quant",
            architecture_summary="x",
            entrypoints=[],
            shared_constraints=[],
            files=[
                runtime.CodegenFilePlan(
                    path="main.py", purpose="x", depends_on=[], must_contain=[]
                ),
            ],
            generation_batches=[],
        )

        with mock.patch.object(
            s5, "_run_codegen_batch_stage"
        ) as patched_batch:
            repaired, chars, history = s5._run_codegen_repair_loop(
                user_problem="x",
                mode="Quant",
                language_hint="python",
                llm=mock.MagicMock(),
                analysis_report=None,
                gate_decision=None,
                manifest=manifest,
                current_bundle=None,
                run_snapshot=None,
                scope="mvp",
                max_attempts=0,
            )

        patched_batch.assert_not_called()
        self.assertIsNone(repaired)
        self.assertEqual(chars, 0)
        self.assertEqual(history, [])

    def test_pipeline_retries_manifest_before_falling_back(self) -> None:
        """When ``CODEGEN_MANIFEST_RETRY_MAX_ATTEMPTS`` is >1, the manifest
        stage is retried after a transient failure.  Successful retry must
        return the real manifest rather than the fallback synthesis.
        """
        from unittest import mock

        from crucible.module_runtime import get_runtime
        import crucible.modules.section_05_analysis_and_codegen as s5

        runtime = get_runtime()

        good_manifest = runtime.CodegenManifest(
            project_type="quant",
            architecture_summary="real",
            entrypoints=["main.py"],
            shared_constraints=[],
            files=[
                runtime.CodegenFilePlan(
                    path="main.py", purpose="entry", depends_on=[], must_contain=[]
                ),
            ],
            generation_batches=[
                runtime.CodegenBatchPlan(
                    name="b1", objective="x", files=["main.py"]
                ),
            ],
        )

        attempts = [0]

        def _flaky_manifest(*_a, **_kw):
            attempts[0] += 1
            if attempts[0] == 1:
                raise RuntimeError("transient 503")
            return good_manifest, 1000

        good_bundle = runtime.CodeBundle(
            project_type="quant",
            files=[runtime.GeneratedFile(path="main.py", content="x = 1\n")],
        )

        with mock.patch.object(s5, "CODEGEN_MANIFEST_RETRY_MAX_ATTEMPTS", 2), \
             mock.patch.object(s5, "CODEGEN_BATCH_RETRY_MAX_ATTEMPTS", 1), \
             mock.patch.object(s5, "CODEGEN_REPAIR_LOOP_MAX_ATTEMPTS", 0), \
             mock.patch.object(
                s5, "_run_codegen_manifest_stage", side_effect=_flaky_manifest
            ), mock.patch.object(
                s5,
                "_run_codegen_batch_stage",
                return_value=(good_bundle, 500),
            ):
            payload, bundle = s5._run_staged_codegen_pipeline(
                "x",
                mode="Quant",
                language_hint="python",
                llm=mock.MagicMock(),
                analysis_report=None,
                gate_decision=None,
                run_snapshot=None,
                scope="mvp",
            )

        self.assertEqual(attempts[0], 2)
        # No skeleton fallback — the retry succeeded
        self.assertFalse(payload.get("skeleton_fallback"))
        self.assertIsNotNone(bundle)
        self.assertEqual({f.path for f in bundle.files}, {"main.py"})
        # Manifest retry history records both attempts
        self.assertEqual(len(payload.get("manifest_attempts", [])), 2)
        self.assertEqual(payload["manifest_attempts"][0]["outcome"], "exception")
        self.assertEqual(payload["manifest_attempts"][1]["outcome"], "success")

    def test_pipeline_retries_batch_before_skipping(self) -> None:
        """When ``CODEGEN_BATCH_RETRY_MAX_ATTEMPTS`` is >1, a batch that
        raises is retried before the loop records it as a failure.  A
        retry that succeeds means the batch contributes to the final
        bundle and no batch_failure is recorded.
        """
        from unittest import mock

        from crucible.module_runtime import get_runtime
        import crucible.modules.section_05_analysis_and_codegen as s5

        runtime = get_runtime()

        manifest = runtime.CodegenManifest(
            project_type="quant",
            architecture_summary="x",
            entrypoints=["main.py"],
            shared_constraints=[],
            files=[
                runtime.CodegenFilePlan(
                    path="main.py", purpose="entry", depends_on=[], must_contain=[]
                ),
            ],
            generation_batches=[
                runtime.CodegenBatchPlan(
                    name="b1", objective="x", files=["main.py"]
                ),
            ],
        )
        good_bundle = runtime.CodeBundle(
            project_type="quant",
            files=[runtime.GeneratedFile(path="main.py", content="x = 1\n")],
        )

        attempts = [0]

        def _flaky_batch(**_kw):
            attempts[0] += 1
            if attempts[0] == 1:
                raise RuntimeError("transient 429")
            return good_bundle, 500

        with mock.patch.object(s5, "CODEGEN_MANIFEST_RETRY_MAX_ATTEMPTS", 1), \
             mock.patch.object(s5, "CODEGEN_BATCH_RETRY_MAX_ATTEMPTS", 2), \
             mock.patch.object(s5, "CODEGEN_REPAIR_LOOP_MAX_ATTEMPTS", 0), \
             mock.patch.object(
                s5,
                "_run_codegen_manifest_stage",
                return_value=(manifest, 1000),
            ), mock.patch.object(
                s5,
                "_run_codegen_batch_stage",
                side_effect=lambda *a, **kw: _flaky_batch(**kw),
            ):
            payload, bundle = s5._run_staged_codegen_pipeline(
                "x",
                mode="Quant",
                language_hint="python",
                llm=mock.MagicMock(),
                analysis_report=None,
                gate_decision=None,
                run_snapshot=None,
                scope="mvp",
            )

        self.assertEqual(attempts[0], 2)
        self.assertIsNotNone(bundle)
        self.assertEqual({f.path for f in bundle.files}, {"main.py"})
        # No batch_failures because the retry succeeded
        self.assertNotIn("batch_failures", payload)
        # Batch retry history records both attempts
        self.assertEqual(len(payload.get("batch_attempts", [])), 2)
        self.assertEqual(payload["batch_attempts"][0]["outcome"], "exception")
        self.assertEqual(payload["batch_attempts"][1]["outcome"], "success")

    def test_pipeline_repair_loop_regenerates_skipped_batch_files(
        self,
    ) -> None:
        """End-to-end: a batch that failed entirely (with retry exhausted)
        leaves its files missing from the cumulative bundle.  The pipeline
        repair loop must then regenerate those files via a focused repair
        pass so the user gets complete output.
        """
        from unittest import mock

        from crucible.module_runtime import get_runtime
        import crucible.modules.section_05_analysis_and_codegen as s5

        runtime = get_runtime()

        manifest = runtime.CodegenManifest(
            project_type="quant",
            architecture_summary="x",
            entrypoints=["main.py"],
            shared_constraints=[],
            files=[
                runtime.CodegenFilePlan(
                    path="main.py", purpose="entry", depends_on=[], must_contain=[]
                ),
                runtime.CodegenFilePlan(
                    path="util.py", purpose="util", depends_on=[], must_contain=[]
                ),
            ],
            generation_batches=[
                runtime.CodegenBatchPlan(
                    name="b1", objective="m", files=["main.py"]
                ),
                runtime.CodegenBatchPlan(
                    name="b2", objective="u", files=["util.py"]
                ),
            ],
        )

        def _stage_dispatch(**kw):
            batch_plan = kw["batch_plan"]
            batch_index = kw["batch_index"]
            # Main loop calls — batch 1 succeeds, batch 2 always fails
            if batch_index == 1:
                return (
                    runtime.CodeBundle(
                        project_type="quant",
                        files=[
                            runtime.GeneratedFile(
                                path="main.py", content="x = 1\n"
                            )
                        ],
                    ),
                    400,
                )
            if batch_index == 2:
                raise RuntimeError("batch 2 LLM down")
            # Repair loop call — synthetic batch_index 9001+ —
            # should be asked to regenerate util.py
            self.assertGreaterEqual(batch_index, 9000)
            self.assertIn("util.py", batch_plan.files)
            return (
                runtime.CodeBundle(
                    project_type="quant",
                    files=[
                        runtime.GeneratedFile(path="util.py", content="y = 2\n")
                    ],
                ),
                350,
            )

        with mock.patch.object(s5, "CODEGEN_MANIFEST_RETRY_MAX_ATTEMPTS", 1), \
             mock.patch.object(s5, "CODEGEN_BATCH_RETRY_MAX_ATTEMPTS", 1), \
             mock.patch.object(s5, "CODEGEN_REPAIR_LOOP_MAX_ATTEMPTS", 3), \
             mock.patch.object(
                s5,
                "_run_codegen_manifest_stage",
                return_value=(manifest, 1000),
            ), mock.patch.object(
                s5,
                "_run_codegen_batch_stage",
                side_effect=lambda *a, **kw: _stage_dispatch(**kw),
            ):
            payload, bundle = s5._run_staged_codegen_pipeline(
                "x",
                mode="Quant",
                language_hint="python",
                llm=mock.MagicMock(),
                analysis_report=None,
                gate_decision=None,
                run_snapshot=None,
                scope="mvp",
            )

        # Repair loop regenerated util.py — bundle has both files
        self.assertIsNotNone(bundle)
        self.assertEqual(
            {f.path for f in bundle.files}, {"main.py", "util.py"}
        )
        # batch_failures still records that batch 2's main attempt failed
        self.assertEqual(len(payload.get("batch_failures", [])), 1)
        self.assertEqual(payload["batch_failures"][0]["batch_index"], 2)
        # Repair history has at least one merge that fixed util.py
        repair_hist = payload.get("repair_history", [])
        self.assertTrue(repair_hist)
        merged = [h for h in repair_hist if h.get("outcome") == "merged"]
        self.assertTrue(merged)
        self.assertIn("util.py", merged[0].get("fixed", []))

    @pytest.mark.slow
    def test_codegen_retry_env_vars_default_and_can_be_disabled(self) -> None:
        """``CODEGEN_MANIFEST_RETRY_MAX_ATTEMPTS``,
        ``CODEGEN_BATCH_RETRY_MAX_ATTEMPTS``, and
        ``CODEGEN_REPAIR_LOOP_MAX_ATTEMPTS`` must respect their env vars
        and fall back to safe defaults on invalid input.
        """
        import os
        import subprocess
        import sys

        env_keys = (
            "CODEGEN_MANIFEST_RETRY_MAX_ATTEMPTS",
            "CODEGEN_BATCH_RETRY_MAX_ATTEMPTS",
            "CODEGEN_REPAIR_LOOP_MAX_ATTEMPTS",
        )

        # Default (all unset) → 2/2/3
        env = {k: v for k, v in os.environ.items() if k not in env_keys}
        check_default = (
            "import crucible.modules.section_05_analysis_and_codegen as s5;"
            "assert s5.CODEGEN_MANIFEST_RETRY_MAX_ATTEMPTS == 2;"
            "assert s5.CODEGEN_BATCH_RETRY_MAX_ATTEMPTS == 2;"
            "assert s5.CODEGEN_REPAIR_LOOP_MAX_ATTEMPTS == 3;"
            "print('defaults OK')"
        )
        proc = subprocess.run(
            [sys.executable, "-c", check_default],
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("defaults OK", proc.stdout)

        # Custom values
        env_c = dict(env)
        env_c["CODEGEN_MANIFEST_RETRY_MAX_ATTEMPTS"] = "5"
        env_c["CODEGEN_BATCH_RETRY_MAX_ATTEMPTS"] = "1"
        env_c["CODEGEN_REPAIR_LOOP_MAX_ATTEMPTS"] = "0"
        check_custom = (
            "import crucible.modules.section_05_analysis_and_codegen as s5;"
            "assert s5.CODEGEN_MANIFEST_RETRY_MAX_ATTEMPTS == 5;"
            "assert s5.CODEGEN_BATCH_RETRY_MAX_ATTEMPTS == 1;"
            "assert s5.CODEGEN_REPAIR_LOOP_MAX_ATTEMPTS == 0;"
            "print('custom OK')"
        )
        proc = subprocess.run(
            [sys.executable, "-c", check_custom],
            env=env_c,
            capture_output=True,
            text=True,
            timeout=60,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("custom OK", proc.stdout)

        # Invalid value (not an int) → falls back to default, no crash
        env_bad = dict(env)
        env_bad["CODEGEN_MANIFEST_RETRY_MAX_ATTEMPTS"] = "abc"
        env_bad["CODEGEN_BATCH_RETRY_MAX_ATTEMPTS"] = "xyz"
        env_bad["CODEGEN_REPAIR_LOOP_MAX_ATTEMPTS"] = "???"
        proc = subprocess.run(
            [sys.executable, "-c", check_default],
            env=env_bad,
            capture_output=True,
            text=True,
            timeout=60,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("defaults OK", proc.stdout)

    def test_run_staged_codegen_pipeline_propagates_user_cancellation(
        self,
    ) -> None:
        """``_OperationCancelledError`` must always propagate even with all
        never-terminate flags on — user-driven cancellation is not a
        validation failure and must reach the harness immediately.
        """
        from unittest import mock

        from crucible.module_runtime import get_runtime

        runtime = get_runtime()

        import crucible.modules.section_05_analysis_and_codegen as s5

        def _fake_manifest_stage_cancelled(*_a, **_kw):
            raise s5._OperationCancelledError("user pressed stop")

        with mock.patch.object(
            s5,
            "_run_codegen_manifest_stage",
            side_effect=_fake_manifest_stage_cancelled,
        ):
            with self.assertRaises(s5._OperationCancelledError):
                s5._run_staged_codegen_pipeline(
                    "cancel test",
                    mode="Quant",
                    language_hint="python",
                    llm=mock.MagicMock(),
                    analysis_report=None,
                    gate_decision=None,
                    run_snapshot=None,
                    scope="mvp",
                )

    @pytest.mark.slow
    def test_codegen_token_env_vars_fall_back_to_default_on_invalid_input(
        self,
    ) -> None:
        """Regression: a typo'd env-var value (e.g. ``CODEGEN_MAX_TOKENS=abc``)
        must NOT crash module import.  The original ``int(os.environ.get(...))``
        pattern would raise ``ValueError`` at import time, rendering the entire
        pipeline unusable until the operator's shell config is fixed.
        """
        import os
        import subprocess
        import sys

        env = os.environ.copy()
        env["CODEGEN_MAX_TOKENS"] = "definitely_not_a_number"
        env["CODEGEN_SUPPLEMENT_MAX_MISSING"] = "@#$%"
        env["FORMATTER_MAX_TOKENS"] = "<garbage>"
        # Subprocess so we get a clean module import not affected by the
        # already-loaded section modules in this test process.
        proc = subprocess.run(
            [
                sys.executable,
                "-c",
                (
                    "import crucible.modules.section_05_analysis_and_codegen as s5;"
                    "import crucible.modules.section_01_extraction_and_reformat as s1;"
                    "assert s5.CODEGEN_MAX_TOKENS == 65536, s5.CODEGEN_MAX_TOKENS;"
                    "assert s5._SUPPLEMENT_MAX_MISSING_FILES == 4,"
                    " s5._SUPPLEMENT_MAX_MISSING_FILES;"
                    "assert s1.FORMATTER_MAX_TOKENS == 8192, s1.FORMATTER_MAX_TOKENS;"
                    "print('OK')"
                ),
            ],
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
        )
        self.assertEqual(
            proc.returncode,
            0,
            f"Module import crashed on garbage env-var values:\n"
            f"stdout={proc.stdout!r}\nstderr={proc.stderr!r}",
        )
        self.assertIn("OK", proc.stdout)

        # Also verify zero/negative falls back to default (avoids zero-token
        # configurations that would silently produce empty LLM responses).
        env["CODEGEN_MAX_TOKENS"] = "0"
        env["CODEGEN_SUPPLEMENT_MAX_MISSING"] = "-5"
        env["FORMATTER_MAX_TOKENS"] = "-1"
        proc2 = subprocess.run(
            [
                sys.executable,
                "-c",
                (
                    "import crucible.modules.section_05_analysis_and_codegen as s5;"
                    "import crucible.modules.section_01_extraction_and_reformat as s1;"
                    "assert s5.CODEGEN_MAX_TOKENS == 65536;"
                    "assert s5._SUPPLEMENT_MAX_MISSING_FILES == 4;"
                    "assert s1.FORMATTER_MAX_TOKENS == 8192;"
                    "print('OK')"
                ),
            ],
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
        )
        self.assertEqual(proc2.returncode, 0, proc2.stderr)
        self.assertIn("OK", proc2.stdout)

    def test_py_syntax_error_paths_in_bundle_returns_all_broken_files(self) -> None:
        """Regression: the syntax-repair supplement requires *all* broken files
        in one pass.  ``_py_syntax_error_in_bundle`` short-circuits on the first
        SyntaxError; the new ``_py_syntax_error_paths_in_bundle`` must report
        every offending .py file and skip non-Python files entirely.
        """
        from crucible.module_runtime import get_runtime

        runtime = get_runtime()
        bundle = runtime.CodeBundle(
            project_type="quant",
            files=[
                runtime.GeneratedFile(path="ok.py", content="x = 1\n"),
                runtime.GeneratedFile(
                    path="broken_a.py", content="def f(:\n    return 1\n"
                ),
                runtime.GeneratedFile(
                    path="broken_b.py", content='print("unterminated\n'
                ),
                runtime.GeneratedFile(
                    path="Dockerfile", content="FROM python:3.11\n"
                ),
                runtime.GeneratedFile(path="README.md", content="# title\n"),
            ],
        )

        # Single-error helper still short-circuits on first failure.
        first_only = runtime._py_syntax_error_in_bundle(bundle)
        self.assertIsNotNone(first_only)
        self.assertIn("broken_a.py", first_only)

        # Multi-error helper must surface every broken .py file (and only .py).
        all_pairs = runtime._py_syntax_error_paths_in_bundle(bundle)
        self.assertEqual(len(all_pairs), 2)
        broken_paths = {path for path, _msg in all_pairs}
        self.assertEqual(broken_paths, {"broken_a.py", "broken_b.py"})
        for path, msg in all_pairs:
            self.assertTrue(msg.startswith("Python SyntaxError in "))
            self.assertIn(path, msg)

        # Empty bundle / None bundle must never raise.
        self.assertEqual(runtime._py_syntax_error_paths_in_bundle(None), [])
        empty_bundle = runtime.CodeBundle(project_type="quant", files=[])
        self.assertEqual(
            runtime._py_syntax_error_paths_in_bundle(empty_bundle), []
        )

    def test_validate_batch_bundle_prunes_unknown_extra_files(self) -> None:
        # When the LLM generates all planned files plus unsolicited extras, the batch
        # validator must prune the extras and accept the result rather than discarding
        # the entire batch output. The extra file is silently dropped — it never reaches
        # the saved project — which is safer than failing the whole batch and producing
        # no code at all.
        from crucible.module_runtime import get_runtime

        runtime = get_runtime()
        batch_plan = runtime.CodegenBatchPlan(
            name="batch_4",
            objective="generate exchange base adapter",
            files=["exchanges/base.py"],
        )
        current_bundle = runtime.CodeBundle(
            project_type="quant",
            files=[runtime.GeneratedFile(path="schema.py", content="SCHEMA = True\n")],
        )
        bundle = runtime.CodeBundle(
            project_type="quant",
            files=[
                runtime.GeneratedFile(
                    path="exchanges/base.py",
                    content="class BaseExchange:\n    pass\n",
                ),
                runtime.GeneratedFile(path="rogue.py", content="raise SystemExit\n"),
            ],
        )

        validated, failure_note = runtime._validate_batch_bundle(
            bundle,
            batch_plan=batch_plan,
            mode="Quant",
            current_bundle=current_bundle,
        )

        # The extra file (rogue.py) is pruned; the planned file is preserved.
        self.assertIsNotNone(validated)
        self.assertIsNone(failure_note)
        validated_paths = {f.path for f in (validated.files or [])}
        self.assertIn("exchanges/base.py", validated_paths)
        self.assertNotIn("rogue.py", validated_paths)

    def test_finalize_staged_codegen_bundle_rejects_duplicate_normalized_paths(self) -> None:
        from crucible.module_runtime import get_runtime

        runtime = get_runtime()
        manifest = runtime.CodegenManifest(
            project_type="quant",
            architecture_summary="summary",
            entrypoints=["src/main.py"],
            shared_constraints=[],
            files=[
                runtime.CodegenFilePlan(
                    path="src/main.py",
                    purpose="entrypoint",
                    depends_on=[],
                    must_contain=[],
                )
            ],
            generation_batches=[
                runtime.CodegenBatchPlan(
                    name="batch_1",
                    objective="generate the entrypoint",
                    files=["src/main.py"],
                )
            ],
        )
        bundle = runtime.CodeBundle(
            project_type="quant",
            files=[
                runtime.GeneratedFile(path="src/main.py", content="print('a')\n"),
                runtime.GeneratedFile(path="src\\main.py", content="print('b')\n"),
            ],
        )

        final_bundle, failure_note = runtime._finalize_staged_codegen_bundle(
            bundle,
            manifest=manifest,
            mode="Quant",
        )

        self.assertIsNone(final_bundle)
        self.assertEqual(
            failure_note,
            "Staged codegen returned duplicate file paths: src/main.py",
        )

    def test_normalize_codegen_manifest_rebuilds_unsafe_raw_batch_order(self) -> None:
        from crucible.module_runtime import get_runtime

        runtime = get_runtime()
        manifest = runtime.CodegenManifest(
            project_type="quant",
            architecture_summary="summary",
            entrypoints=["src/main.py"],
            shared_constraints=[],
            files=[
                runtime.CodegenFilePlan(
                    path="src/main.py",
                    purpose="entrypoint",
                    depends_on=["src/types.py"],
                    must_contain=[],
                ),
                runtime.CodegenFilePlan(
                    path="src/types.py",
                    purpose="shared types",
                    depends_on=[],
                    must_contain=[],
                ),
            ],
            generation_batches=[
                runtime.CodegenBatchPlan(
                    name="batch_1",
                    objective="generate entrypoint first",
                    files=["src/main.py"],
                ),
                runtime.CodegenBatchPlan(
                    name="batch_2",
                    objective="generate shared types later",
                    files=["src/types.py"],
                ),
            ],
        )

        normalized = runtime._normalize_codegen_manifest(manifest, mode="Quant")

        self.assertIsNotNone(normalized)
        self.assertEqual(
            [batch.files for batch in normalized.generation_batches],
            [["src/types.py"], ["src/main.py"]],
        )

    def test_build_codegen_batch_crew_preserves_manifest_and_dependency_sections_under_budget(
        self,
    ) -> None:
        from crucible.module_runtime import get_runtime

        runtime = get_runtime()
        globals_dict = runtime._build_codegen_batch_crew.__globals__
        original_values = {
            "build_budgeted_codegen_context": globals_dict.get(
                "build_budgeted_codegen_context"
            ),
            "_build_codegen_single_task_crew": globals_dict.get(
                "_build_codegen_single_task_crew"
            ),
        }
        manifest = runtime.CodegenManifest(
            project_type="quant",
            architecture_summary="architecture " + ("x" * 800),
            entrypoints=["src/main.py"],
            shared_constraints=["constraint " + ("y" * 120)] * 4,
            files=[
                runtime.CodegenFilePlan(
                    path="src/main.py",
                    purpose="entrypoint",
                    depends_on=["src/types.py"],
                    must_contain=["main function", "cli entrypoint"],
                ),
                runtime.CodegenFilePlan(
                    path="src/types.py",
                    purpose="shared types",
                    depends_on=[],
                    must_contain=["TypedDict", "validation helpers"],
                ),
            ],
            generation_batches=[
                runtime.CodegenBatchPlan(
                    name="batch_1",
                    objective="generate runtime entrypoint",
                    files=["src/main.py"],
                )
            ],
        )
        current_bundle = runtime.CodeBundle(
            project_type="quant",
            files=[
                runtime.GeneratedFile(
                    path="src/types.py",
                    content="class TypeA:\n    pass\n" + ("# helper\n" * 600),
                )
            ],
        )
        captured: dict[str, object] = {}

        def _stub_build_codegen_single_task_crew(**kwargs):
            task_spec = kwargs["task_spec"]
            template_vars = kwargs["template_vars"]
            rendered = runtime._render_task_description_with_budget(task_spec, template_vars)
            captured["description"] = rendered
            return SimpleNamespace(
                tasks=[SimpleNamespace(description=rendered)],
                _prompt_total_chars=len(rendered),
                _prompt_hashes={"codegen_batch": "stub"},
            )

        try:
            globals_dict["build_budgeted_codegen_context"] = (
                lambda *_args, **_kwargs: "approved-context " + ("a" * 5000)
            )
            globals_dict["_build_codegen_single_task_crew"] = (
                _stub_build_codegen_single_task_crew
            )
            crew = runtime._build_codegen_batch_crew(
                "solve the quant validation problem " + ("u" * 5000),
                mode="Quant",
                language_hint="English",
                llm=None,
                analysis_report=SimpleNamespace(),
                gate_decision=SimpleNamespace(),
                manifest=manifest,
                batch_plan=manifest.generation_batches[0],
                current_bundle=current_bundle,
                context_max_chars=9000,
                dependency_file_max_chars=5000,
                max_input_chars=4800,
            )
        finally:
            for key, value in original_values.items():
                globals_dict[key] = value

        description = str(captured.get("description", "") or "")
        self.assertLessEqual(len(description), 4800)
        self.assertIn("Current manifest slice:", description)
        self.assertIn("- path: src/main.py", description)
        self.assertIn("depends_on: src/types.py", description)
        self.assertIn("Previously completed dependency files:", description)
        self.assertIn("[dependency_file] src/types.py", description)
        self.assertIsNotNone(crew)

    def test_normalize_codegen_manifest_uses_single_file_batches_for_alibaba_profile(
        self,
    ) -> None:
        from crucible.module_runtime import get_runtime

        runtime = get_runtime()
        manifest = runtime.CodegenManifest(
            project_type="quant",
            architecture_summary="architecture",
            entrypoints=["src/main.py"],
            shared_constraints=["constraint"],
            files=[
                runtime.CodegenFilePlan(
                    path="src/types.py",
                    purpose="types",
                    depends_on=[],
                    must_contain=["TypedDict"],
                ),
                runtime.CodegenFilePlan(
                    path="src/main.py",
                    purpose="entrypoint",
                    depends_on=["src/types.py"],
                    must_contain=["main"],
                ),
            ],
            generation_batches=[
                runtime.CodegenBatchPlan(
                    name="batch_1",
                    objective="oversized raw batch",
                    files=["src/types.py", "src/main.py"],
                )
            ],
        )

        normalized = runtime._normalize_codegen_manifest(
            manifest,
            mode="Quant",
            llm=SimpleNamespace(_quant_llm_provider="alibaba_coding_plan"),
        )

        self.assertIsNotNone(normalized)
        self.assertEqual(
            [batch.files for batch in normalized.generation_batches],
            [["src/types.py"], ["src/main.py"]],
        )


    def test_build_codegen_batch_crew_uses_provider_specific_budget_profile(self) -> None:
        from crucible.module_runtime import get_runtime

        runtime = get_runtime()
        globals_dict = runtime._build_codegen_batch_crew.__globals__
        original_values = {
            "build_budgeted_codegen_context": globals_dict.get(
                "build_budgeted_codegen_context"
            ),
            "_build_codegen_single_task_crew": globals_dict.get(
                "_build_codegen_single_task_crew"
            ),
        }
        manifest = runtime.CodegenManifest(
            project_type="quant",
            architecture_summary="architecture",
            entrypoints=["src/main.py"],
            shared_constraints=["constraint"],
            files=[
                runtime.CodegenFilePlan(
                    path="src/main.py",
                    purpose="entrypoint",
                    depends_on=["src/types.py"],
                    must_contain=["main function"],
                ),
                runtime.CodegenFilePlan(
                    path="src/types.py",
                    purpose="shared types",
                    depends_on=[],
                    must_contain=["TypedDict"],
                ),
            ],
            generation_batches=[
                runtime.CodegenBatchPlan(
                    name="batch_1",
                    objective="generate runtime entrypoint",
                    files=["src/main.py"],
                )
            ],
        )
        current_bundle = runtime.CodeBundle(
            project_type="quant",
            files=[
                runtime.GeneratedFile(
                    path="src/types.py",
                    content="class TypeA:\n    pass\n" + ("# helper\n" * 700),
                )
            ],
        )
        captured: dict[str, object] = {}

        def _stub_build_codegen_single_task_crew(**kwargs):
            task_spec = kwargs["task_spec"]
            template_vars = kwargs["template_vars"]
            rendered = runtime._render_task_description_with_budget(task_spec, template_vars)
            captured["description"] = rendered
            captured["task_max_input_chars"] = task_spec.max_input_chars
            return SimpleNamespace(
                tasks=[SimpleNamespace(description=rendered)],
                _prompt_total_chars=len(rendered),
                _prompt_hashes={"codegen_batch": "stub"},
            )

        try:
            globals_dict["build_budgeted_codegen_context"] = (
                lambda *_args, **_kwargs: "approved-context " + ("a" * 9000)
            )
            globals_dict["_build_codegen_single_task_crew"] = (
                _stub_build_codegen_single_task_crew
            )
            runtime._build_codegen_batch_crew(
                "solve the quant validation problem " + ("u" * 9000),
                mode="Quant",
                language_hint="English",
                llm=SimpleNamespace(_quant_llm_provider="alibaba_coding_plan"),
                analysis_report=SimpleNamespace(),
                gate_decision=SimpleNamespace(),
                manifest=manifest,
                batch_plan=manifest.generation_batches[0],
                current_bundle=current_bundle,
                context_max_chars=9000,
                dependency_file_max_chars=5000,
                max_input_chars=18000,
            )
        finally:
            for key, value in original_values.items():
                globals_dict[key] = value

        description = str(captured.get("description", "") or "")
        self.assertLessEqual(
            int(captured.get("task_max_input_chars", 0) or 0),
            runtime.ALIBABA_CODEGEN_BATCH_MAX_INPUT_CHARS,
        )
        self.assertLessEqual(len(description), runtime.ALIBABA_CODEGEN_BATCH_MAX_INPUT_CHARS)
        self.assertIn("Current manifest slice:", description)
        self.assertIn("Previously completed dependency files:", description)

    def test_run_codegen_manifest_stage_retries_with_fallback_on_parse_failure(self) -> None:
        from crucible.module_runtime import get_runtime

        runtime = get_runtime()
        helper_globals = runtime._run_codegen_manifest_stage.__globals__
        original_values = {
            "_build_codegen_manifest_crew": helper_globals.get("_build_codegen_manifest_crew"),
            "_kickoff_codegen_substage_with_recovery": helper_globals.get(
                "_kickoff_codegen_substage_with_recovery"
            ),
            "_run_codegen_substage_fallback_attempt": helper_globals.get(
                "_run_codegen_substage_fallback_attempt"
            ),
            "_extract_codegen_manifest": helper_globals.get("_extract_codegen_manifest"),
            "_sync_codegen_snapshot_metadata": helper_globals.get(
                "_sync_codegen_snapshot_metadata"
            ),
            "_snapshot_record_stage": helper_globals.get("_snapshot_record_stage"),
            "_cost_trace": helper_globals.get("_cost_trace"),
        }
        primary_crew = SimpleNamespace(_prompt_total_chars=1200, _prompt_hashes={})
        fallback_crew = SimpleNamespace(_prompt_total_chars=500, _prompt_hashes={})
        expected_manifest = runtime.CodegenManifest(
            project_type="quant",
            architecture_summary="summary",
            entrypoints=["src/main.py"],
            shared_constraints=[],
            files=[
                runtime.CodegenFilePlan(
                    path="src/main.py",
                    purpose="entrypoint",
                    depends_on=[],
                    must_contain=[],
                )
            ],
            generation_batches=[
                runtime.CodegenBatchPlan(
                    name="batch_1",
                    objective="generate entrypoint",
                    files=["src/main.py"],
                )
            ],
        )
        extract_results = [None, expected_manifest]
        fallback_calls: list[dict[str, object]] = []

        def _extract_stub(*_args, **_kwargs):
            return extract_results.pop(0)

        def _fallback_stub(**kwargs):
            fallback_calls.append(dict(kwargs))
            return "fallback-result", fallback_crew, 500

        try:
            helper_globals["_build_codegen_manifest_crew"] = (
                lambda *_args, **_kwargs: primary_crew
            )
            helper_globals["_kickoff_codegen_substage_with_recovery"] = (
                lambda *_args, **_kwargs: ("primary-result", primary_crew, 1200)
            )
            helper_globals["_run_codegen_substage_fallback_attempt"] = _fallback_stub
            helper_globals["_extract_codegen_manifest"] = _extract_stub
            helper_globals["_sync_codegen_snapshot_metadata"] = lambda *args, **kwargs: None
            helper_globals["_snapshot_record_stage"] = lambda *args, **kwargs: None
            helper_globals["_cost_trace"] = lambda *args, **kwargs: None

            manifest, prompt_chars = runtime._run_codegen_manifest_stage(
                "build a quant validation harness",
                mode="Quant",
                language_hint="English",
                llm=None,
                analysis_report=None,
                gate_decision=None,
                run_snapshot=None,
            )
        finally:
            for key, value in original_values.items():
                helper_globals[key] = value

        self.assertEqual(manifest, expected_manifest)
        self.assertEqual(prompt_chars, 1700)
        self.assertEqual(len(fallback_calls), 1)
        self.assertEqual(fallback_calls[0]["reason"], "manifest_parse_failed")

    def test_run_codegen_batch_stage_retries_with_fallback_on_validation_failure(self) -> None:
        from crucible.module_runtime import get_runtime

        runtime = get_runtime()
        helper_globals = runtime._run_codegen_batch_stage.__globals__
        original_values = {
            "_build_codegen_batch_crew": helper_globals.get("_build_codegen_batch_crew"),
            "_kickoff_codegen_substage_with_recovery": helper_globals.get(
                "_kickoff_codegen_substage_with_recovery"
            ),
            "_run_codegen_substage_fallback_attempt": helper_globals.get(
                "_run_codegen_substage_fallback_attempt"
            ),
            "_extract_codegen_bundle_from_result": helper_globals.get(
                "_extract_codegen_bundle_from_result"
            ),
            "_sync_codegen_snapshot_metadata": helper_globals.get(
                "_sync_codegen_snapshot_metadata"
            ),
            "_snapshot_record_stage": helper_globals.get("_snapshot_record_stage"),
            "_cost_trace": helper_globals.get("_cost_trace"),
        }
        manifest = runtime.CodegenManifest(
            project_type="quant",
            architecture_summary="summary",
            entrypoints=["src/main.py"],
            shared_constraints=[],
            files=[
                runtime.CodegenFilePlan(
                    path="src/main.py",
                    purpose="entrypoint",
                    depends_on=[],
                    must_contain=[],
                )
            ],
            generation_batches=[
                runtime.CodegenBatchPlan(
                    name="batch_1",
                    objective="generate entrypoint",
                    files=["src/main.py"],
                )
            ],
        )
        bad_bundle = runtime.CodeBundle(
            project_type="quant",
            files=[runtime.GeneratedFile(path="src/other.py", content="print('bad')\n")],
        )
        good_bundle = runtime.CodeBundle(
            project_type="quant",
            files=[runtime.GeneratedFile(path="src/main.py", content="print('ok')\n")],
        )
        primary_crew = SimpleNamespace(_prompt_total_chars=1400, _prompt_hashes={})
        fallback_crew = SimpleNamespace(_prompt_total_chars=600, _prompt_hashes={})
        extract_results = [bad_bundle, good_bundle]
        fallback_calls: list[dict[str, object]] = []

        def _extract_stub(*_args, **_kwargs):
            return extract_results.pop(0)

        def _fallback_stub(**kwargs):
            fallback_calls.append(dict(kwargs))
            return "fallback-result", fallback_crew, 600

        try:
            helper_globals["_build_codegen_batch_crew"] = (
                lambda *_args, **_kwargs: primary_crew
            )
            helper_globals["_kickoff_codegen_substage_with_recovery"] = (
                lambda *_args, **_kwargs: ("primary-result", primary_crew, 1400)
            )
            helper_globals["_run_codegen_substage_fallback_attempt"] = _fallback_stub
            helper_globals["_extract_codegen_bundle_from_result"] = _extract_stub
            helper_globals["_sync_codegen_snapshot_metadata"] = lambda *args, **kwargs: None
            helper_globals["_snapshot_record_stage"] = lambda *args, **kwargs: None
            helper_globals["_cost_trace"] = lambda *args, **kwargs: None

            bundle, prompt_chars = runtime._run_codegen_batch_stage(
                "build a quant validation harness",
                mode="Quant",
                language_hint="English",
                llm=None,
                analysis_report=None,
                gate_decision=None,
                manifest=manifest,
                batch_plan=manifest.generation_batches[0],
                current_bundle=None,
                run_snapshot=None,
                batch_index=1,
            )
        finally:
            for key, value in original_values.items():
                helper_globals[key] = value

        self.assertEqual(bundle, good_bundle)
        self.assertEqual(prompt_chars, 2000)
        self.assertEqual(len(fallback_calls), 1)
        self.assertEqual(fallback_calls[0]["reason"], "batch_output_validation_failed")

    def test_kickoff_codegen_substage_preemptively_falls_back_for_large_alibaba_prompt(
        self,
    ) -> None:
        from crucible.module_runtime import get_runtime

        runtime = get_runtime()
        helper_globals = runtime._kickoff_codegen_substage_with_recovery.__globals__
        original_values = {
            "kickoff_crew_with_retry": helper_globals.get("kickoff_crew_with_retry"),
            "_run_codegen_substage_fallback_attempt": helper_globals.get(
                "_run_codegen_substage_fallback_attempt"
            ),
        }
        primary_crew = SimpleNamespace(
            _prompt_total_chars=9000,
            _quant_llm_provider="alibaba_coding_plan",
        )
        fallback_crew = SimpleNamespace(_prompt_total_chars=4200)
        kickoff_calls: list[object] = []
        fallback_calls: list[dict[str, object]] = []

        def _kickoff_stub(*args, **kwargs):
            kickoff_calls.append((args, kwargs))
            raise AssertionError("primary kickoff should be skipped")

        def _fallback_stub(**kwargs):
            fallback_calls.append(dict(kwargs))
            return "fallback-result", fallback_crew, 4200

        try:
            helper_globals["kickoff_crew_with_retry"] = _kickoff_stub
            helper_globals["_run_codegen_substage_fallback_attempt"] = _fallback_stub
            result, effective_crew, prompt_chars = runtime._kickoff_codegen_substage_with_recovery(
                primary_crew,
                fallback_crew_factory=lambda: fallback_crew,
                mode="Quant",
                stage_name="codegen_batch_crew_1.kickoff",
            )
        finally:
            for key, value in original_values.items():
                helper_globals[key] = value

        self.assertEqual(result, "fallback-result")
        self.assertIs(effective_crew, fallback_crew)
        self.assertEqual(prompt_chars, 13200)
        self.assertEqual(kickoff_calls, [])
        self.assertEqual(len(fallback_calls), 1)
        self.assertEqual(fallback_calls[0]["reason"], "provider_prompt_budget_guard")

    def test_build_crew_rejects_registry_mode_name_mismatch(self) -> None:
        from crucible.module_runtime import get_runtime

        runtime = get_runtime()
        globals_dict = runtime.build_crew.__globals__
        original = globals_dict["_get_mode_config"]

        bad_mode = runtime.ModeConfig(
            name="SaaS",
            description="polluted registry result",
            metrics="MRR",
            research_focus="saas-only",
            biz_focus="saas-only",
        )
        globals_dict["_get_mode_config"] = lambda _mode: bad_mode
        try:
            with self.assertRaisesRegex(ValueError, "Mode registry returned config name"):
                runtime.build_crew(
                    "design a deterministic cross-exchange strategy runner",
                    mode="Quant",
                    language_hint="English",
                    llm=None,
                )
        finally:
            globals_dict["_get_mode_config"] = original

    def test_cost_accountant_can_reset_between_runs(self) -> None:
        from crucible.module_runtime import get_runtime

        runtime = get_runtime()
        runtime.reset_cost_accountant()
        runtime.get_cost_accountant().record(
            agent_name="analysis_crew",
            stage="analysis_crew.kickoff",
            input_tokens=120,
            output_tokens=80,
            total_cost_usd=1.25,
            cost_source="openrouter_api",
        )
        self.assertEqual(runtime.get_cost_accountant().get_summary()["total_executions"], 1)

        runtime.reset_cost_accountant()

        summary = runtime.get_cost_accountant().get_summary()
        self.assertEqual(summary["total_executions"], 0)
        self.assertEqual(summary["total_tokens"], 0)
        self.assertEqual(summary["total_cost_usd"], 0.0)

    def test_pipeline_runtime_state_reset_clears_cross_run_debug_and_usage(self) -> None:
        from crucible.module_runtime import get_runtime

        runtime = get_runtime()
        runtime.reset_cost_accountant()
        runtime.clear_last_librarian_debug()
        runtime.clear_openrouter_usage()
        runtime.reset_api_version_cache()
        api_cache_globals = runtime.reset_api_version_cache.__globals__
        research_globals = runtime.reset_research_llm_cache.__globals__
        runtime.reset_research_llm_cache()

        runtime.get_cost_accountant().record(
            agent_name="analysis_crew",
            stage="analysis_crew.kickoff",
            input_tokens=30,
            output_tokens=20,
            total_cost_usd=0.42,
            cost_source="openrouter_api",
        )
        runtime._update_last_librarian_debug(
            status="success",
            search_strategy="semantic",
            cache_hit=True,
        )
        runtime.set_openrouter_usage(
            {
                "prompt_tokens": 40,
                "completion_tokens": 10,
                "total_tokens": 50,
                "cost": 0.5,
            },
            model_id="openai/gpt-5.4",
        )
        api_cache_globals["_API_VERSION_CACHE"] = {
            "fastapi": {
                "timestamp": "2026-03-23T10:00:00",
                "data": {"latest_version": "0.116.0"},
            }
        }
        research_globals["_DIRECTION_JUDGE_LLM"] = object()
        research_globals["_LIBRARIAN_LLM"] = object()
        research_globals["_LOCAL_LLM_CACHE"] = object()

        self.assertEqual(runtime.get_cost_accountant().get_summary()["total_executions"], 1)
        self.assertTrue(runtime.get_last_librarian_debug())
        self.assertEqual(len(runtime.get_usage_records()), 1)
        self.assertIsNotNone(runtime.get_last_openrouter_usage())
        self.assertTrue(api_cache_globals["_API_VERSION_CACHE"])
        self.assertIsNotNone(research_globals["_DIRECTION_JUDGE_LLM"])
        self.assertIsNotNone(research_globals["_LIBRARIAN_LLM"])
        self.assertIsNotNone(research_globals["_LOCAL_LLM_CACHE"])

        runtime._reset_pipeline_runtime_state()

        self.assertEqual(runtime.get_cost_accountant().get_summary()["total_executions"], 0)
        self.assertEqual(runtime.get_last_librarian_debug(), {})
        self.assertEqual(runtime.get_usage_records(), [])
        self.assertEqual(runtime.get_last_openrouter_usage().total_tokens, 0)
        self.assertEqual(runtime.get_last_openrouter_usage().total_cost_usd, 0.0)
        self.assertEqual(api_cache_globals["_API_VERSION_CACHE"], {})
        self.assertIsNone(research_globals["_DIRECTION_JUDGE_LLM"])
        self.assertIsNone(research_globals["_LIBRARIAN_LLM"])
        self.assertIsNone(research_globals["_LOCAL_LLM_CACHE"])

    def test_reset_api_version_cache_clears_in_process_state(self) -> None:
        from crucible.module_runtime import get_runtime

        runtime = get_runtime()
        globals_dict = runtime.reset_api_version_cache.__globals__
        original_cache = globals_dict.get("_API_VERSION_CACHE")
        try:
            globals_dict["_API_VERSION_CACHE"] = {
                "ccxt": {
                    "timestamp": "2026-03-23T10:00:00",
                    "data": {"latest_version": "4.5.1"},
                }
            }
            runtime.reset_api_version_cache()
            self.assertEqual(globals_dict["_API_VERSION_CACHE"], {})
        finally:
            globals_dict["_API_VERSION_CACHE"] = (
                dict(original_cache) if isinstance(original_cache, dict) else {}
            )

    def test_reset_research_llm_cache_clears_cached_clients(self) -> None:
        from crucible.module_runtime import get_runtime

        runtime = get_runtime()
        globals_dict = runtime.reset_research_llm_cache.__globals__
        original_direction = globals_dict.get("_DIRECTION_JUDGE_LLM")
        original_librarian = globals_dict.get("_LIBRARIAN_LLM")
        try:
            globals_dict["_DIRECTION_JUDGE_LLM"] = object()
            globals_dict["_LIBRARIAN_LLM"] = object()
            runtime.reset_research_llm_cache()
            self.assertIsNone(globals_dict["_DIRECTION_JUDGE_LLM"])
            self.assertIsNone(globals_dict["_LIBRARIAN_LLM"])
        finally:
            globals_dict["_DIRECTION_JUDGE_LLM"] = original_direction
            globals_dict["_LIBRARIAN_LLM"] = original_librarian

    def test_librarian_debug_snapshot_sync_tracks_current_run_state(self) -> None:
        from crucible.module_runtime import get_runtime

        runtime = get_runtime()
        runtime.clear_last_librarian_debug()
        run_snapshot = runtime.RunSnapshot(
            run_id="test-run",
            runtime_profile="pro",
            mode="Quant",
            model_versions={},
            inputs={},
            outputs={},
        )

        runtime._sync_librarian_debug_snapshot(run_snapshot)
        self.assertNotIn("librarian_research", run_snapshot.outputs)

        runtime._update_last_librarian_debug(
            status="success",
            search_strategy="semantic",
            cache_hit=False,
        )
        runtime._sync_librarian_debug_snapshot(run_snapshot)
        self.assertEqual(run_snapshot.outputs["librarian_research"]["status"], "success")
        self.assertEqual(run_snapshot.inputs["librarian_search_strategy"], "semantic")
        self.assertFalse(run_snapshot.inputs["librarian_cache_hit"])

        runtime.clear_last_librarian_debug()
        runtime._sync_librarian_debug_snapshot(run_snapshot)
        self.assertNotIn("librarian_research", run_snapshot.outputs)
        self.assertNotIn("librarian_search_strategy", run_snapshot.inputs)
        self.assertNotIn("librarian_cache_hit", run_snapshot.inputs)

    def test_direction_debug_dump_uses_local_model_id_helper(self) -> None:
        from crucible.module_runtime import get_runtime

        runtime = get_runtime()
        helper_globals = runtime._write_direction_debate_debug_dump.__globals__
        original_path_resolver = helper_globals.get("_direction_debug_dump_path")
        tmpdir = tempfile.mkdtemp(dir=str(ROOT))
        try:
            helper_globals["_direction_debug_dump_path"] = lambda: tmpdir
            with mock.patch.dict(os.environ, {"DIRECTION_DEBATE_DEBUG_DUMP": "1"}, clear=False):
                dump_path = runtime._write_direction_debate_debug_dump(
                    user_problem="debug dump regression",
                    attempt=1,
                    llm=object(),
                    direction_judge_llm=object(),
                    elapsed_seconds=0.1,
                    stage_index_map={"judge": 0},
                    result=None,
                    raw_candidates=[],
                    decision=None,
                    comparator_report=None,
                    audit_report=None,
                    note="unit-test",
                )
            self.assertIsNotNone(dump_path)
            payload = json.loads(Path(dump_path).read_text(encoding="utf-8"))
            self.assertEqual(payload["llm_model_id"], "")
            self.assertEqual(payload["direction_judge_model_id"], "")
        finally:
            helper_globals["_direction_debug_dump_path"] = original_path_resolver
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_reformat_section_owns_llm_model_id_helper(self) -> None:
        from crucible.modules import section_01_extraction_and_reformat as m01

        helper = m01._legacy_reformat_gate_decision.__globals__["_reformat_llm_model_id"]
        self.assertTrue(callable(helper))
        self.assertEqual(helper(object()), "")
        llm = SimpleNamespace(model="formatter-model")
        self.assertEqual(helper(llm), "formatter-model")

    def test_reformat_cache_payload_isolated_by_provider(self) -> None:
        from crucible.module_runtime import get_runtime

        runtime = get_runtime()
        globals_dict = runtime._reformat_direction_decision.__globals__
        original_runner = globals_dict["_run_schema_reformatter"]
        original_active_provider = globals_dict.get("ACTIVE_LLM_PROVIDER")
        captured: dict[str, object] = {}

        def _fake_runner(**kwargs):
            captured.update(kwargs)
            return None

        try:
            globals_dict["_run_schema_reformatter"] = _fake_runner
            globals_dict["ACTIVE_LLM_PROVIDER"] = "openrouter"
            runtime._reformat_direction_decision(
                "direction payload",
                llm=SimpleNamespace(
                    model="shared-model",
                    _quant_llm_provider="alibaba_coding_plan",
                ),
                language_hint="English",
            )
        finally:
            globals_dict["_run_schema_reformatter"] = original_runner
            globals_dict["ACTIVE_LLM_PROVIDER"] = original_active_provider

        self.assertEqual(
            captured["cache_payload"]["llm_provider"],
            "alibaba_coding_plan",
        )
        self.assertEqual(captured["cache_payload"]["model"], "shared-model")

    def test_runtime_model_versions_use_live_resolvers_not_stale_section_globals(self) -> None:
        from crucible.module_runtime import get_runtime

        runtime = get_runtime()
        globals_dict = runtime._resolve_runtime_model_versions.__globals__
        original_llm_model_id = globals_dict["_llm_model_id"]
        original_primary = globals_dict["_resolve_primary_model_id"]
        original_direction = globals_dict["_resolve_direction_judge_model_id"]
        original_librarian = globals_dict["_resolve_librarian_model_id"]
        original_provider = globals_dict["_resolve_llm_provider"]
        original_enabled = globals_dict["LIBRARIAN_ENABLED"]
        original_model_id = globals_dict.get("MODEL_ID")
        original_direction_model_id = globals_dict.get("DIRECTION_JUDGE_MODEL_ID")
        original_librarian_model_id = globals_dict.get("LIBRARIAN_MODEL_ID")

        globals_dict["_llm_model_id"] = lambda _llm: ""
        globals_dict["_resolve_primary_model_id"] = lambda: "fresh-primary"
        globals_dict["_resolve_direction_judge_model_id"] = lambda: "fresh-direction"
        globals_dict["_resolve_librarian_model_id"] = lambda: "fresh-librarian"
        globals_dict["_resolve_llm_provider"] = lambda _provider=None: "alibaba_coding_plan"
        globals_dict["LIBRARIAN_ENABLED"] = True
        globals_dict["MODEL_ID"] = "stale-primary"
        globals_dict["DIRECTION_JUDGE_MODEL_ID"] = "stale-direction"
        globals_dict["LIBRARIAN_MODEL_ID"] = "stale-librarian"
        try:
            versions = runtime._resolve_runtime_model_versions(None)
        finally:
            globals_dict["_llm_model_id"] = original_llm_model_id
            globals_dict["_resolve_primary_model_id"] = original_primary
            globals_dict["_resolve_direction_judge_model_id"] = original_direction
            globals_dict["_resolve_librarian_model_id"] = original_librarian
            globals_dict["_resolve_llm_provider"] = original_provider
            globals_dict["LIBRARIAN_ENABLED"] = original_enabled
            globals_dict["MODEL_ID"] = original_model_id
            globals_dict["DIRECTION_JUDGE_MODEL_ID"] = original_direction_model_id
            globals_dict["LIBRARIAN_MODEL_ID"] = original_librarian_model_id

        self.assertEqual(versions["llm_provider"], "alibaba_coding_plan")
        self.assertEqual(versions["primary"], "fresh-primary")
        self.assertEqual(versions["direction_judge"], "fresh-direction")
        self.assertEqual(versions["librarian"], "fresh-librarian")

    def test_entry_llm_provider_prefers_cli_over_env(self) -> None:
        from crucible.module_runtime import get_runtime

        runtime = get_runtime()
        with mock.patch.dict(os.environ, {"LLM_PROVIDER": "openrouter"}, clear=False):
            resolved = runtime._resolve_entry_llm_provider(
                "alibaba_coding_plan",
                allow_interactive_prompt=False,
            )
        self.assertEqual(resolved, "alibaba_coding_plan")


    def test_entry_llm_provider_uses_env_without_prompt(self) -> None:
        from crucible.module_runtime import get_runtime

        runtime = get_runtime()
        # ``runtime`` is a SimpleNamespace; the prompt helper is resolved from
        # ``_resolve_entry_llm_provider``'s own module globals, so patching it
        # via ``mock.patch.object(runtime, ...)`` is a silent no-op.  Patch
        # the live module global instead.
        globals_dict = runtime._resolve_entry_llm_provider.__globals__
        original_prompt = globals_dict["_prompt_for_llm_provider"]
        with mock.patch.dict(os.environ, {"LLM_PROVIDER": "alibaba_coding_plan"}, clear=False):
            def _raise() -> str:
                raise AssertionError("should not prompt")
            globals_dict["_prompt_for_llm_provider"] = _raise
            try:
                resolved = runtime._resolve_entry_llm_provider(
                    None,
                    allow_interactive_prompt=True,
                )
            finally:
                globals_dict["_prompt_for_llm_provider"] = original_prompt
        self.assertEqual(resolved, "alibaba_coding_plan")

    def test_entry_llm_provider_prompts_interactively_when_needed(self) -> None:
        from crucible.module_runtime import get_runtime

        runtime = get_runtime()
        globals_dict = runtime._resolve_entry_llm_provider.__globals__
        original_prompt = globals_dict["_prompt_for_llm_provider"]
        with mock.patch.dict(os.environ, {"LLM_PROVIDER": ""}, clear=False):
            globals_dict["_prompt_for_llm_provider"] = lambda: "alibaba_coding_plan"
            try:
                resolved = runtime._resolve_entry_llm_provider(
                    None,
                    allow_interactive_prompt=True,
                )
            finally:
                globals_dict["_prompt_for_llm_provider"] = original_prompt
        self.assertEqual(resolved, "alibaba_coding_plan")

    def test_entry_llm_provider_defaults_to_openrouter_without_prompt(self) -> None:
        from crucible.module_runtime import get_runtime

        runtime = get_runtime()
        # See ``test_entry_llm_provider_uses_env_without_prompt`` — patch via
        # the live module global, not the SimpleNamespace.
        globals_dict = runtime._resolve_entry_llm_provider.__globals__
        original_prompt = globals_dict["_prompt_for_llm_provider"]
        with mock.patch.dict(os.environ, {"LLM_PROVIDER": ""}, clear=False):
            def _raise() -> str:
                raise AssertionError("should not prompt")
            globals_dict["_prompt_for_llm_provider"] = _raise
            try:
                resolved = runtime._resolve_entry_llm_provider(
                    None,
                    allow_interactive_prompt=False,
                )
            finally:
                globals_dict["_prompt_for_llm_provider"] = original_prompt
        self.assertEqual(resolved, "openrouter")

    def test_runtime_provider_selection_does_not_pollute_next_entry_resolution(self) -> None:
        from crucible.module_runtime import get_runtime

        runtime = get_runtime()
        helper_globals = runtime._apply_llm_provider_runtime.__globals__
        target_modules = [
            helper_globals["_prev_00"],
            helper_globals["_prev_01"],
            helper_globals["_prev_02"],
            helper_globals["_prev_03"],
            helper_globals["_prev_04"],
            helper_globals["_prev_05"],
            helper_globals["_prev_06"],
        ]
        original_module_values = [
            {
                "LLM_PROVIDER": module.__dict__.get("LLM_PROVIDER"),
                "ACTIVE_LLM_PROVIDER": module.__dict__.get("ACTIVE_LLM_PROVIDER"),
            }
            for module in target_modules
        ]
        original_helper_values = {
            "LLM_PROVIDER": helper_globals.get("LLM_PROVIDER"),
            "ACTIVE_LLM_PROVIDER": helper_globals.get("ACTIVE_LLM_PROVIDER"),
        }
        try:
            with mock.patch.dict(os.environ, {"LLM_PROVIDER": ""}, clear=False):
                first = runtime._apply_llm_provider_runtime("alibaba_coding_plan")
                second = runtime._resolve_entry_llm_provider(
                    None,
                    allow_interactive_prompt=False,
                )
                self.assertEqual(first, "alibaba_coding_plan")
                self.assertEqual(os.environ.get("LLM_PROVIDER", ""), "")
                self.assertEqual(second, "openrouter")
                self.assertEqual(os.environ.get("LLM_PROVIDER", ""), "")
        finally:
            for module, values in zip(target_modules, original_module_values):
                for key, value in values.items():
                    module.__dict__[key] = value
            for key, value in original_helper_values.items():
                helper_globals[key] = value

    def test_apply_llm_provider_runtime_clears_stale_usage_context(self) -> None:
        from crucible.module_runtime import get_runtime

        runtime = get_runtime()
        helper_globals = runtime._apply_llm_provider_runtime.__globals__
        runtime.clear_openrouter_usage()
        runtime.set_openrouter_usage(
            {
                "prompt_tokens": 100,
                "completion_tokens": 50,
                "total_tokens": 150,
                "cost": 0.001,
            },
            model_id="openai/gpt-5.4",
            provider="openrouter",
        )

        original_helper_values = {
            "LLM_PROVIDER": helper_globals.get("LLM_PROVIDER"),
            "ACTIVE_LLM_PROVIDER": helper_globals.get("ACTIVE_LLM_PROVIDER"),
        }
        try:
            runtime._apply_llm_provider_runtime("alibaba_coding_plan")
            usage = runtime.get_last_openrouter_usage()
            self.assertEqual(usage.total_tokens, 0)
            self.assertEqual(usage.total_cost_usd, 0.0)
        finally:
            for key, value in original_helper_values.items():
                helper_globals[key] = value
            runtime.clear_openrouter_usage()

    def test_evaluate_budget_state_prefers_usd_for_billable_cost_sources(self) -> None:
        from crucible.module_runtime import get_runtime

        runtime = get_runtime()
        runtime.reset_cost_accountant()
        runtime.get_cost_accountant().record(
            agent_name="analysis_crew",
            stage="analysis_crew.kickoff",
            input_tokens=5000,
            output_tokens=0,
            total_cost_usd=0.50,
            cost_source="openrouter_api",
        )

        state = runtime._evaluate_budget_state(
            runtime.BudgetPolicy(
                soft_cost_limit=0.4,
                hard_cost_limit=0.6,
                max_total_tokens=10_000,
            )
        )

        self.assertEqual(state["total_cost"], 0.5)
        self.assertEqual(state["total_cost_usd"], 0.5)
        self.assertEqual(state["cost_basis"], "usd")
        self.assertEqual(state["cost_source"], "openrouter_api")
        self.assertTrue(state["over_soft_limit"])
        self.assertFalse(state["over_hard_limit"])
        self.assertFalse(state["over_token_limit"])

    def test_evaluate_budget_state_does_not_treat_alibaba_token_only_usage_as_usd_cost(self) -> None:
        from crucible.module_runtime import get_runtime

        runtime = get_runtime()
        runtime.reset_cost_accountant()
        runtime.get_cost_accountant().record(
            agent_name="analysis_crew",
            stage="analysis_crew.kickoff",
            input_tokens=2500,
            output_tokens=500,
            cost_source="alibaba_coding_plan_tokens_only",
        )

        state = runtime._evaluate_budget_state(
            runtime.BudgetPolicy(
                soft_cost_limit=0.1,
                hard_cost_limit=0.2,
                max_total_tokens=2000,
            )
        )

        self.assertEqual(state["total_cost"], 0.0)
        self.assertEqual(state["total_cost_usd"], 0.0)
        self.assertEqual(state["cost_basis"], "token_only")
        self.assertEqual(state["cost_source"], "alibaba_coding_plan_tokens_only")
        self.assertFalse(state["over_soft_limit"])
        self.assertFalse(state["over_hard_limit"])
        self.assertTrue(state["over_token_limit"])

    def test_runtime_resolve_llm_provider_prefers_active_selection_over_env(self) -> None:
        from crucible.module_runtime import get_runtime

        runtime = get_runtime()
        globals_dict = runtime._resolve_llm_provider.__globals__
        original_active_provider = globals_dict.get("ACTIVE_LLM_PROVIDER")
        with mock.patch.dict(os.environ, {"LLM_PROVIDER": "openrouter"}, clear=False):
            try:
                globals_dict["ACTIVE_LLM_PROVIDER"] = "alibaba_coding_plan"
                self.assertEqual(runtime._resolve_llm_provider(), "alibaba_coding_plan")
                self.assertEqual(runtime._resolve_llm_provider("openrouter"), "openrouter")
            finally:
                globals_dict["ACTIVE_LLM_PROVIDER"] = original_active_provider

    def test_research_llm_cache_rebuilds_when_provider_changes(self) -> None:
        from crucible.module_runtime import get_runtime

        runtime = get_runtime()
        globals_dict = runtime._get_direction_judge_llm.__globals__
        original_builder = globals_dict["_create_openrouter_llm"]
        original_resolve_model = globals_dict["_resolve_direction_judge_model_id"]
        original_resolve_timeout = globals_dict["_build_llm_timeout_value"]
        original_cached = globals_dict.get("_DIRECTION_JUDGE_LLM")
        original_active_provider = globals_dict.get("ACTIVE_LLM_PROVIDER")
        call_providers = []

        def _fake_builder(model_id: str, **kwargs):
            provider = kwargs.get("provider")
            timeout = kwargs.get("timeout_seconds")
            call_providers.append(provider)
            return SimpleNamespace(
                model=model_id,
                timeout=timeout,
                _quant_llm_provider=provider,
            )

        globals_dict["_create_openrouter_llm"] = _fake_builder
        globals_dict["_resolve_direction_judge_model_id"] = lambda: "shared-model"
        globals_dict["_build_llm_timeout_value"] = (
            lambda _provider=None, timeout_seconds=None: 180.0
        )
        globals_dict["_DIRECTION_JUDGE_LLM"] = None
        try:
            globals_dict["ACTIVE_LLM_PROVIDER"] = "openrouter"
            first = runtime._get_direction_judge_llm()
            globals_dict["ACTIVE_LLM_PROVIDER"] = "alibaba_coding_plan"
            second = runtime._get_direction_judge_llm()
        finally:
            globals_dict["_create_openrouter_llm"] = original_builder
            globals_dict["_resolve_direction_judge_model_id"] = original_resolve_model
            globals_dict["_build_llm_timeout_value"] = original_resolve_timeout
            globals_dict["_DIRECTION_JUDGE_LLM"] = original_cached
            globals_dict["ACTIVE_LLM_PROVIDER"] = original_active_provider

        self.assertEqual(call_providers, ["openrouter", "alibaba_coding_plan"])
        self.assertIsNot(first, second)

    def test_resolve_llm_timeout_seconds_uses_provider_specific_defaults(self) -> None:
        from crucible.module_runtime import get_runtime

        runtime = get_runtime()
        globals_dict = runtime._resolve_llm_timeout_seconds.__globals__
        original_active_provider = globals_dict.get("ACTIVE_LLM_PROVIDER")
        try:
            globals_dict["ACTIVE_LLM_PROVIDER"] = "openrouter"
            with mock.patch.dict(
                os.environ,
                {
                    "OPENROUTER_LLM_TIMEOUT_SECONDS": "180",
                    "ALIBABA_CODING_PLAN_LLM_TIMEOUT_SECONDS": "",
                },
                clear=False,
            ):
                self.assertEqual(runtime._resolve_llm_timeout_seconds("openrouter"), 180)
                self.assertEqual(
                    runtime._resolve_llm_timeout_seconds("alibaba_coding_plan"),
                    900,
                )
            with mock.patch.dict(
                os.environ,
                {"ALIBABA_CODING_PLAN_LLM_TIMEOUT_SECONDS": "510"},
                clear=False,
            ):
                self.assertEqual(
                    runtime._resolve_llm_timeout_seconds("alibaba_coding_plan"),
                    510,
                )
        finally:
            globals_dict["ACTIVE_LLM_PROVIDER"] = original_active_provider

    def test_build_llm_timeout_value_uses_alibaba_dual_timeout_profile(self) -> None:
        from crucible.module_runtime import get_runtime

        runtime = get_runtime()
        with mock.patch.dict(
            os.environ,
            {
                "ALIBABA_CODING_PLAN_LLM_TIMEOUT_SECONDS": "900",
                "ALIBABA_CODING_PLAN_INITIAL_RESPONSE_TIMEOUT_SECONDS": "120",
            },
            clear=False,
        ):
            timeout_value = runtime._build_llm_timeout_value("alibaba_coding_plan")

        self.assertEqual(float(timeout_value.read), 900.0)
        self.assertEqual(float(timeout_value.connect), 120.0)
        self.assertEqual(float(timeout_value.write), 120.0)
        self.assertEqual(float(timeout_value.pool), 120.0)


    def test_openrouter_load_api_key_rejects_stale_alibaba_openai_compat_key(self) -> None:
        from crucible.module_runtime import get_runtime

        runtime = get_runtime()
        globals_dict = runtime.load_api_key.__globals__
        original_active_provider = globals_dict.get("ACTIVE_LLM_PROVIDER")
        original_compat_provider = globals_dict.get("ACTIVE_OPENAI_COMPAT_PROVIDER")
        original_compat_key = globals_dict.get("ACTIVE_OPENAI_COMPAT_API_KEY")
        with mock.patch.dict(
            os.environ,
            {
                "OPENROUTER_API_KEY": "",
                "OPENAI_API_KEY": "sk-sp-stale-alibaba-key",
                "ALIBABA_CODING_PLAN_API_KEY": "sk-sp-real-alibaba-key",
                "LLM_PROVIDER": "",
            },
            clear=False,
        ):
            globals_dict["ACTIVE_LLM_PROVIDER"] = "openrouter"
            globals_dict["ACTIVE_OPENAI_COMPAT_PROVIDER"] = "alibaba_coding_plan"
            globals_dict["ACTIVE_OPENAI_COMPAT_API_KEY"] = "sk-sp-stale-alibaba-key"
            try:
                # load_api_key raises RuntimeError (not sys.exit) so pipeline
                # checkpoints/telemetry can flush and callers can fall back
                # to a different provider rather than being killed.
                with self.assertRaises(RuntimeError):
                    runtime.load_api_key("openrouter")
            finally:
                globals_dict["ACTIVE_LLM_PROVIDER"] = original_active_provider
                globals_dict["ACTIVE_OPENAI_COMPAT_PROVIDER"] = original_compat_provider
                globals_dict["ACTIVE_OPENAI_COMPAT_API_KEY"] = original_compat_key

    def test_runtime_option_overrides_propagate_to_all_execution_sections(self) -> None:
        from crucible.module_runtime import get_runtime

        runtime = get_runtime()
        helper_globals = runtime._apply_runtime_option_overrides.__globals__
        target_modules = [
            helper_globals["_prev_00"],
            helper_globals["_prev_01"],
            helper_globals["_prev_02"],
            helper_globals["_prev_03"],
            helper_globals["_prev_04"],
            helper_globals["_prev_05"],
            helper_globals["_prev_06"],
        ]
        original_values = [
            (
                module.__dict__.get("STRICT_JSON_ENABLED"),
                module.__dict__.get("LOCAL_CACHE_ENABLED"),
                module.__dict__.get("COST_TRACE_ENABLED"),
            )
            for module in target_modules
        ]
        original_helper_values = (
            helper_globals.get("STRICT_JSON_ENABLED"),
            helper_globals.get("LOCAL_CACHE_ENABLED"),
            helper_globals.get("COST_TRACE_ENABLED"),
        )
        try:
            runtime._apply_runtime_option_overrides(
                strict_json=True,
                local_cache=True,
                cost_trace=True,
            )
            for module in target_modules:
                self.assertTrue(module.__dict__["STRICT_JSON_ENABLED"])
                self.assertTrue(module.__dict__["LOCAL_CACHE_ENABLED"])
                self.assertTrue(module.__dict__["COST_TRACE_ENABLED"])
            self.assertTrue(helper_globals["STRICT_JSON_ENABLED"])
            self.assertTrue(helper_globals["LOCAL_CACHE_ENABLED"])
            self.assertTrue(helper_globals["COST_TRACE_ENABLED"])
        finally:
            for module, values in zip(target_modules, original_values):
                strict_json, local_cache, cost_trace = values
                module.__dict__["STRICT_JSON_ENABLED"] = strict_json
                module.__dict__["LOCAL_CACHE_ENABLED"] = local_cache
                module.__dict__["COST_TRACE_ENABLED"] = cost_trace
            helper_globals["STRICT_JSON_ENABLED"] = original_helper_values[0]
            helper_globals["LOCAL_CACHE_ENABLED"] = original_helper_values[1]
            helper_globals["COST_TRACE_ENABLED"] = original_helper_values[2]

    def test_runtime_option_defaults_reset_from_env_between_runs(self) -> None:
        from crucible.module_runtime import get_runtime

        runtime = get_runtime()
        helper_globals = runtime._reset_runtime_option_defaults_from_env.__globals__
        target_modules = [
            helper_globals["_prev_00"],
            helper_globals["_prev_01"],
            helper_globals["_prev_02"],
            helper_globals["_prev_03"],
            helper_globals["_prev_04"],
            helper_globals["_prev_05"],
            helper_globals["_prev_06"],
        ]
        original_module_values = [
            (
                module.__dict__.get("STRICT_JSON_ENABLED"),
                module.__dict__.get("LOCAL_CACHE_ENABLED"),
                module.__dict__.get("COST_TRACE_ENABLED"),
            )
            for module in target_modules
        ]
        original_helper_values = (
            helper_globals.get("STRICT_JSON_ENABLED"),
            helper_globals.get("LOCAL_CACHE_ENABLED"),
            helper_globals.get("COST_TRACE_ENABLED"),
            helper_globals.get("_env_bool"),
        )
        try:
            helper_globals["_env_bool"] = lambda key, default=False: {
                "STRICT_JSON": False,
                "LOCAL_CACHE": False,
                "COST_TRACE": False,
            }.get(key, default)
            for module in target_modules:
                module.__dict__["STRICT_JSON_ENABLED"] = True
                module.__dict__["LOCAL_CACHE_ENABLED"] = True
                module.__dict__["COST_TRACE_ENABLED"] = True
            helper_globals["STRICT_JSON_ENABLED"] = True
            helper_globals["LOCAL_CACHE_ENABLED"] = True
            helper_globals["COST_TRACE_ENABLED"] = True

            runtime._reset_runtime_option_defaults_from_env()

            for module in target_modules:
                self.assertFalse(module.__dict__["STRICT_JSON_ENABLED"])
                self.assertFalse(module.__dict__["LOCAL_CACHE_ENABLED"])
                self.assertFalse(module.__dict__["COST_TRACE_ENABLED"])
            self.assertFalse(helper_globals["STRICT_JSON_ENABLED"])
            self.assertFalse(helper_globals["LOCAL_CACHE_ENABLED"])
            self.assertFalse(helper_globals["COST_TRACE_ENABLED"])
        finally:
            for module, values in zip(target_modules, original_module_values):
                strict_json, local_cache, cost_trace = values
                module.__dict__["STRICT_JSON_ENABLED"] = strict_json
                module.__dict__["LOCAL_CACHE_ENABLED"] = local_cache
                module.__dict__["COST_TRACE_ENABLED"] = cost_trace
            helper_globals["STRICT_JSON_ENABLED"] = original_helper_values[0]
            helper_globals["LOCAL_CACHE_ENABLED"] = original_helper_values[1]
            helper_globals["COST_TRACE_ENABLED"] = original_helper_values[2]
            helper_globals["_env_bool"] = original_helper_values[3]

    def test_resolve_runtime_profile_reads_fresh_env_defaults_each_call(self) -> None:
        from crucible.module_runtime import get_runtime

        runtime = get_runtime()
        helper_globals = runtime.resolve_runtime_profile.__globals__
        original_values = (
            helper_globals.get("_resolve_gate_control_enabled_default"),
            helper_globals.get("_resolve_selective_rerun_enabled_default"),
            helper_globals.get("_resolve_runtime_profile_strict_json_default"),
            helper_globals.get("_resolve_runtime_profile_cache_default"),
            helper_globals.get("_resolve_runtime_profile_default_name"),
        )
        try:
            helper_globals["_resolve_gate_control_enabled_default"] = lambda: False
            helper_globals["_resolve_selective_rerun_enabled_default"] = lambda: True
            helper_globals["_resolve_runtime_profile_strict_json_default"] = lambda: True
            helper_globals["_resolve_runtime_profile_cache_default"] = lambda: False
            helper_globals["_resolve_runtime_profile_default_name"] = lambda: "pro"
            profile_a = runtime.resolve_runtime_profile(None)

            helper_globals["_resolve_gate_control_enabled_default"] = lambda: True
            helper_globals["_resolve_selective_rerun_enabled_default"] = lambda: False
            helper_globals["_resolve_runtime_profile_strict_json_default"] = lambda: False
            helper_globals["_resolve_runtime_profile_cache_default"] = lambda: True
            helper_globals["_resolve_runtime_profile_default_name"] = lambda: "pro"
            profile_b = runtime.resolve_runtime_profile(None)
        finally:
            helper_globals["_resolve_gate_control_enabled_default"] = original_values[0]
            helper_globals["_resolve_selective_rerun_enabled_default"] = original_values[1]
            helper_globals["_resolve_runtime_profile_strict_json_default"] = original_values[2]
            helper_globals["_resolve_runtime_profile_cache_default"] = original_values[3]
            helper_globals["_resolve_runtime_profile_default_name"] = original_values[4]

        self.assertFalse(profile_a.gate_control_default)
        self.assertTrue(profile_a.selective_rerun_default)
        self.assertTrue(profile_a.strict_json_default)
        self.assertFalse(profile_a.cache_default)
        self.assertTrue(profile_b.gate_control_default)
        self.assertFalse(profile_b.selective_rerun_default)
        self.assertFalse(profile_b.strict_json_default)
        self.assertTrue(profile_b.cache_default)

    def test_runtime_entry_defaults_read_fresh_values_each_call(self) -> None:
        from crucible.module_runtime import get_runtime

        runtime = get_runtime()
        helper_globals = runtime._resolve_runtime_entry_defaults.__globals__
        original_values = (
            helper_globals.get("_resolve_runtime_profile_default_name"),
            helper_globals.get("_env_float"),
            helper_globals.get("_env_int"),
            helper_globals.get("_resolve_api_version_check_enabled_default"),
        )
        try:
            helper_globals["_resolve_runtime_profile_default_name"] = lambda: "lite"
            helper_globals["_env_float"] = lambda key, default=None: {
                "BUDGET_SOFT_COST_LIMIT": 1.5,
                "BUDGET_HARD_COST_LIMIT": 2.5,
            }.get(key, default)
            helper_globals["_env_int"] = lambda key, default=None: {
                "BUDGET_MAX_TOTAL_TOKENS": 1234,
            }.get(key, default)
            helper_globals["_resolve_api_version_check_enabled_default"] = lambda: False
            defaults_a = runtime._resolve_runtime_entry_defaults()

            helper_globals["_resolve_runtime_profile_default_name"] = lambda: "enterprise"
            helper_globals["_env_float"] = lambda key, default=None: {
                "BUDGET_SOFT_COST_LIMIT": 9.5,
                "BUDGET_HARD_COST_LIMIT": 19.5,
            }.get(key, default)
            helper_globals["_env_int"] = lambda key, default=None: {
                "BUDGET_MAX_TOTAL_TOKENS": 9999,
            }.get(key, default)
            helper_globals["_resolve_api_version_check_enabled_default"] = lambda: True
            defaults_b = runtime._resolve_runtime_entry_defaults()
        finally:
            helper_globals["_resolve_runtime_profile_default_name"] = original_values[0]
            helper_globals["_env_float"] = original_values[1]
            helper_globals["_env_int"] = original_values[2]
            helper_globals["_resolve_api_version_check_enabled_default"] = original_values[3]

        self.assertEqual(defaults_a["runtime_profile"], "lite")
        self.assertEqual(defaults_a["budget_soft_cost"], 1.5)
        self.assertEqual(defaults_a["budget_hard_cost"], 2.5)
        self.assertEqual(defaults_a["budget_max_tokens"], 1234)
        self.assertFalse(defaults_a["api_version_check_enabled"])
        self.assertEqual(defaults_b["runtime_profile"], "enterprise")
        self.assertEqual(defaults_b["budget_soft_cost"], 9.5)
        self.assertEqual(defaults_b["budget_hard_cost"], 19.5)
        self.assertEqual(defaults_b["budget_max_tokens"], 9999)
        self.assertTrue(defaults_b["api_version_check_enabled"])

    def test_run_analysis_with_selective_rerun_reads_fresh_max_attempts_each_call(self) -> None:
        from crucible.module_runtime import get_runtime

        runtime = get_runtime()
        helper_globals = runtime.run_analysis_with_selective_rerun.__globals__
        original_values = {
            "_resolve_selective_rerun_max_attempts_default": helper_globals.get(
                "_resolve_selective_rerun_max_attempts_default"
            ),
            "build_analysis_crew": helper_globals.get("build_analysis_crew"),
            "kickoff_crew_with_retry": helper_globals.get("kickoff_crew_with_retry"),
            "_parse_analysis_outputs": helper_globals.get("_parse_analysis_outputs"),
            "_promote_validation_first_gate": helper_globals.get(
                "_promote_validation_first_gate"
            ),
            "_align_analysis_report_with_gate_scope": helper_globals.get(
                "_align_analysis_report_with_gate_scope"
            ),
            "_extract_text_from_result": helper_globals.get("_extract_text_from_result"),
            "_record_cost": helper_globals.get("_record_cost"),
            "_cost_trace": helper_globals.get("_cost_trace"),
            "log_event": helper_globals.get("log_event"),
            "log_exception": helper_globals.get("log_exception"),
            "_classify_gate_failure": helper_globals.get("_classify_gate_failure"),
            "_gate_requests_direction_feedback": helper_globals.get(
                "_gate_requests_direction_feedback"
            ),
            "_normalize_rerun_agent_keys": helper_globals.get("_normalize_rerun_agent_keys"),
        }

        def _make_gate() -> SimpleNamespace:
            return SimpleNamespace(
                should_kill=False,
                agents_needing_rerun=["analyst"],
                overall_score=7,
                confidence="low",
                rerun_reasons={"analyst": "need rerun"},
                blocking_risks=[],
            )

        kickoff_counter = {"count": 0}

        def _build_analysis_crew_stub(*args, **kwargs) -> SimpleNamespace:
            kickoff_counter["count"] += 1
            return SimpleNamespace()

        try:
            helper_globals["build_analysis_crew"] = _build_analysis_crew_stub
            helper_globals["kickoff_crew_with_retry"] = (
                lambda crew, logger=None, log_fields=None: SimpleNamespace()
            )
            helper_globals["_parse_analysis_outputs"] = (
                lambda result, llm=None, language_hint=None, mode=None: (None, _make_gate())
            )
            helper_globals["_promote_validation_first_gate"] = (
                lambda gate, user_problem=None, mode=None: gate
            )
            helper_globals["_align_analysis_report_with_gate_scope"] = (
                lambda report, gate: report
            )
            helper_globals["_extract_text_from_result"] = lambda result: ""
            helper_globals["_record_cost"] = lambda **kwargs: None
            helper_globals["_cost_trace"] = lambda *args, **kwargs: None
            helper_globals["log_event"] = lambda *args, **kwargs: None
            helper_globals["log_exception"] = lambda *args, **kwargs: None
            helper_globals["_classify_gate_failure"] = lambda gate: (None, None)
            helper_globals["_gate_requests_direction_feedback"] = lambda gate: False
            helper_globals["_normalize_rerun_agent_keys"] = (
                lambda roles: {"analyst"} if roles else set()
            )

            helper_globals["_resolve_selective_rerun_max_attempts_default"] = lambda: 0
            runtime.run_analysis_with_selective_rerun(
                user_problem="test",
                mode="Quant",
                language_hint="English",
                llm=None,
                enable_selective_rerun=True,
                gate_feedback_enabled=False,
                direction_debate_enabled=False,
            )
            first_call_attempts = kickoff_counter["count"]

            kickoff_counter["count"] = 0
            helper_globals["_resolve_selective_rerun_max_attempts_default"] = lambda: 1
            runtime.run_analysis_with_selective_rerun(
                user_problem="test",
                mode="Quant",
                language_hint="English",
                llm=None,
                enable_selective_rerun=True,
                gate_feedback_enabled=False,
                direction_debate_enabled=False,
            )
            second_call_attempts = kickoff_counter["count"]
        finally:
            for key, value in original_values.items():
                helper_globals[key] = value

        self.assertEqual(first_call_attempts, 1)
        self.assertEqual(second_call_attempts, 2)

    def test_run_analysis_with_selective_rerun_preserves_kickoff_exception_snapshot(self) -> None:
        from crucible.module_runtime import get_runtime

        runtime = get_runtime()
        helper = runtime.run_analysis_with_selective_rerun
        helper_globals = helper.__globals__
        original_values = {
            "build_analysis_crew": helper_globals.get("build_analysis_crew"),
            "kickoff_crew_with_retry": helper_globals.get("kickoff_crew_with_retry"),
            "_record_cost": helper_globals.get("_record_cost"),
            "_cost_trace": helper_globals.get("_cost_trace"),
            "log_event": helper_globals.get("log_event"),
            "log_exception": helper_globals.get("log_exception"),
        }

        snapshot = runtime.RunSnapshot(run_id="analysis-kickoff-failure")

        try:
            helper_globals["build_analysis_crew"] = (
                lambda *args, **kwargs: SimpleNamespace(_prompt_total_chars=321)
            )
            helper_globals["kickoff_crew_with_retry"] = (
                lambda *args, **kwargs: (_ for _ in ()).throw(ConnectionError("boom"))
            )
            helper_globals["_record_cost"] = lambda **kwargs: None
            helper_globals["_cost_trace"] = lambda *args, **kwargs: None
            helper_globals["log_event"] = lambda *args, **kwargs: None
            helper_globals["log_exception"] = lambda *args, **kwargs: None

            with self.assertRaises(ConnectionError):
                helper(
                    user_problem="test",
                    mode="Quant",
                    language_hint="English",
                    llm=None,
                    enable_selective_rerun=False,
                    run_snapshot=snapshot,
                )
        finally:
            for key, value in original_values.items():
                helper_globals[key] = value

        self.assertGreaterEqual(len(snapshot.stage_records), 2)
        started = snapshot.stage_records[0]
        failed = snapshot.stage_records[-1]
        self.assertEqual(started["status"], "started")
        self.assertEqual(started.get("prompt_chars"), 321)
        self.assertEqual(failed["status"], "failed")
        expected_failure_type = helper_globals["_classify_runtime_exception_failure"](
            ConnectionError("boom")
        )
        expected_value = (
            expected_failure_type.value
            if hasattr(expected_failure_type, "value")
            else str(expected_failure_type)
        )
        self.assertEqual(failed["failure_type"], expected_value)
        self.assertIn("boom", failed.get("notes", ""))

    def test_run_analysis_with_selective_rerun_failure_cost_uses_prompt_tokens(self) -> None:
        from crucible.module_runtime import get_runtime

        runtime = get_runtime()
        helper = runtime.run_analysis_with_selective_rerun
        helper_globals = helper.__globals__
        original_values = {
            "build_analysis_crew": helper_globals.get("build_analysis_crew"),
            "kickoff_crew_with_retry": helper_globals.get("kickoff_crew_with_retry"),
            "_record_cost": helper_globals.get("_record_cost"),
            "_cost_trace": helper_globals.get("_cost_trace"),
            "log_event": helper_globals.get("log_event"),
            "log_exception": helper_globals.get("log_exception"),
        }
        recorded: list[dict[str, object]] = []

        try:
            helper_globals["build_analysis_crew"] = (
                lambda *args, **kwargs: SimpleNamespace(_prompt_total_chars=900)
            )
            helper_globals["kickoff_crew_with_retry"] = (
                lambda *args, **kwargs: (_ for _ in ()).throw(ConnectionError("boom"))
            )
            helper_globals["_record_cost"] = lambda **kwargs: recorded.append(dict(kwargs))
            helper_globals["_cost_trace"] = lambda *args, **kwargs: None
            helper_globals["log_event"] = lambda *args, **kwargs: None
            helper_globals["log_exception"] = lambda *args, **kwargs: None

            with self.assertRaises(ConnectionError):
                helper(
                    user_problem="test",
                    mode="Quant",
                    language_hint="English",
                    llm=None,
                    enable_selective_rerun=False,
                )
        finally:
            for key, value in original_values.items():
                helper_globals[key] = value

        self.assertTrue(recorded)
        self.assertEqual(recorded[-1]["stage"], "analysis_crew.kickoff")
        self.assertEqual(recorded[-1]["input_tokens"], 300)
        self.assertEqual(recorded[-1]["outcome"], "execution_error")

    def test_legacy_run_codegen_stage_failure_cost_uses_prompt_tokens(self) -> None:
        from crucible.module_runtime import get_runtime

        runtime = get_runtime()
        helper = runtime.run_codegen_stage.__globals__["_LEGACY_RUN_CODEGEN_STAGE"]
        helper_globals = helper.__globals__
        original_values = {
            "build_codegen_crew": helper_globals.get("build_codegen_crew"),
            "_kickoff_codegen_with_timeout_recovery": helper_globals.get(
                "_kickoff_codegen_with_timeout_recovery"
            ),
            "_record_cost": helper_globals.get("_record_cost"),
            "_cost_trace": helper_globals.get("_cost_trace"),
            "log_event": helper_globals.get("log_event"),
            "log_exception": helper_globals.get("log_exception"),
        }
        recorded: list[dict[str, object]] = []

        try:
            helper_globals["build_codegen_crew"] = (
                lambda *args, **kwargs: SimpleNamespace(
                    _prompt_total_chars=1200,
                    _prompt_hashes={},
                )
            )
            helper_globals["_kickoff_codegen_with_timeout_recovery"] = (
                lambda *args, **kwargs: (_ for _ in ()).throw(ConnectionError("boom"))
            )
            helper_globals["_record_cost"] = lambda **kwargs: recorded.append(dict(kwargs))
            helper_globals["_cost_trace"] = lambda *args, **kwargs: None
            helper_globals["log_event"] = lambda *args, **kwargs: None
            helper_globals["log_exception"] = lambda *args, **kwargs: None

            result, bundle = helper(
                user_problem="build a quant app",
                mode="Quant",
                language_hint="English",
                llm=None,
                analysis_report=None,
                gate_decision=None,
            )
        finally:
            for key, value in original_values.items():
                helper_globals[key] = value

        self.assertIsNone(result)
        self.assertIsNone(bundle)
        self.assertTrue(recorded)
        self.assertEqual(recorded[-1]["stage"], "codegen_crew.kickoff")
        self.assertEqual(recorded[-1]["input_tokens"], 400)
        self.assertEqual(recorded[-1]["outcome"], "execution_error")

    def test_librarian_research_failure_cost_uses_prompt_tokens(self) -> None:
        from crucible.module_runtime import get_runtime

        runtime = get_runtime()
        helper = runtime.run_librarian_research
        helper_globals = helper.__globals__
        fallback_context = SimpleNamespace(
            citations=[],
            claim_attributions=[],
            provider_errors={},
            evidence_coverage={},
            providers_used=[],
            search_strategy="fallback",
            hallucination_flags=[],
        )
        original_values = {
            "LIBRARIAN_ENABLED": helper_globals.get("LIBRARIAN_ENABLED"),
            "QUALITY_JSON_RETRY_ATTEMPTS": helper_globals.get("QUALITY_JSON_RETRY_ATTEMPTS"),
            "LIBRARIAN_SEARCH_PROVIDERS": helper_globals.get("LIBRARIAN_SEARCH_PROVIDERS"),
            "_cache_get_pydantic": helper_globals.get("_cache_get_pydantic"),
            "_collect_librarian_search_materials": helper_globals.get(
                "_collect_librarian_search_materials"
            ),
            "_build_fallback_research_context": helper_globals.get(
                "_build_fallback_research_context"
            ),
            "_stabilize_research_context": helper_globals.get("_stabilize_research_context"),
            "_update_last_librarian_debug": helper_globals.get("_update_last_librarian_debug"),
            "_cache_set_pydantic": helper_globals.get("_cache_set_pydantic"),
            "_get_librarian_llm": helper_globals.get("_get_librarian_llm"),
            "_resolve_librarian_model_id": helper_globals.get("_resolve_librarian_model_id"),
            "_librarian_provider_fingerprint": helper_globals.get(
                "_librarian_provider_fingerprint"
            ),
            "_cache_window_bucket": helper_globals.get("_cache_window_bucket"),
            "_resolve_llm_provider": helper_globals.get("_resolve_llm_provider"),
            "build_research_swarm_crew": helper_globals.get("build_research_swarm_crew"),
            "kickoff_crew_with_retry": helper_globals.get("kickoff_crew_with_retry"),
            "_record_cost": helper_globals.get("_record_cost"),
            "_cost_trace": helper_globals.get("_cost_trace"),
            "log_event": helper_globals.get("log_event"),
            "log_exception": helper_globals.get("log_exception"),
        }
        recorded: list[dict[str, object]] = []

        try:
            helper_globals["LIBRARIAN_ENABLED"] = True
            helper_globals["QUALITY_JSON_RETRY_ATTEMPTS"] = 1
            helper_globals["LIBRARIAN_SEARCH_PROVIDERS"] = ["stub"]
            helper_globals["_cache_get_pydantic"] = lambda *args, **kwargs: None
            helper_globals["_collect_librarian_search_materials"] = (
                lambda *args, **kwargs: {
                    "search_strategy": "stub",
                    "providers_used": [],
                    "provider_errors": {},
                    "suggested_search_queries": [],
                    "citations": [],
                }
            )
            helper_globals["_build_fallback_research_context"] = (
                lambda *args, **kwargs: fallback_context
            )
            helper_globals["_stabilize_research_context"] = lambda context: context
            helper_globals["_update_last_librarian_debug"] = lambda **kwargs: None
            helper_globals["_cache_set_pydantic"] = lambda *args, **kwargs: None
            helper_globals["_get_librarian_llm"] = lambda: object()
            helper_globals["_resolve_librarian_model_id"] = lambda: "stub-model"
            helper_globals["_librarian_provider_fingerprint"] = lambda: "stub-provider"
            helper_globals["_cache_window_bucket"] = lambda *args, **kwargs: "bucket"
            helper_globals["_resolve_llm_provider"] = lambda *args, **kwargs: "openrouter"
            helper_globals["build_research_swarm_crew"] = (
                lambda *args, **kwargs: SimpleNamespace(_prompt_total_chars=1500, tasks=[])
            )
            helper_globals["kickoff_crew_with_retry"] = (
                lambda *args, **kwargs: (_ for _ in ()).throw(ConnectionError("boom"))
            )
            helper_globals["_record_cost"] = lambda **kwargs: recorded.append(dict(kwargs))
            helper_globals["_cost_trace"] = lambda *args, **kwargs: None
            helper_globals["log_event"] = lambda *args, **kwargs: None
            helper_globals["log_exception"] = lambda *args, **kwargs: None

            result = helper(
                user_problem="research this market",
                mode="Quant",
                language_hint="English",
            )
        finally:
            for key, value in original_values.items():
                helper_globals[key] = value

        self.assertIs(result, fallback_context)
        self.assertTrue(recorded)
        self.assertEqual(recorded[-1]["stage"], "librarian_research.kickoff")
        self.assertEqual(recorded[-1]["input_tokens"], 500)
        self.assertEqual(recorded[-1]["outcome"], "execution_error")

    def test_direction_debate_failure_cost_uses_prompt_tokens(self) -> None:
        from crucible.module_runtime import get_runtime

        runtime = get_runtime()
        helper = runtime._run_single_direction_debate
        helper_globals = helper.__globals__
        original_values = {
            "QUALITY_JSON_RETRY_ATTEMPTS": helper_globals.get("QUALITY_JSON_RETRY_ATTEMPTS"),
            "build_direction_debate_crew": helper_globals.get("build_direction_debate_crew"),
            "_build_direction_stage_index_map": helper_globals.get(
                "_build_direction_stage_index_map"
            ),
            "kickoff_crew_with_retry": helper_globals.get("kickoff_crew_with_retry"),
            "_write_direction_debate_debug_dump": helper_globals.get(
                "_write_direction_debate_debug_dump"
            ),
            "_record_cost": helper_globals.get("_record_cost"),
            "_cost_trace": helper_globals.get("_cost_trace"),
            "log_event": helper_globals.get("log_event"),
            "log_exception": helper_globals.get("log_exception"),
            "_llm_model_id": helper_globals.get("_llm_model_id"),
        }
        recorded: list[dict[str, object]] = []

        try:
            helper_globals["QUALITY_JSON_RETRY_ATTEMPTS"] = 1
            helper_globals["build_direction_debate_crew"] = (
                lambda *args, **kwargs: SimpleNamespace(_prompt_total_chars=1800, tasks=[])
            )
            helper_globals["_build_direction_stage_index_map"] = lambda tasks: {}
            helper_globals["kickoff_crew_with_retry"] = (
                lambda *args, **kwargs: (_ for _ in ()).throw(ConnectionError("boom"))
            )
            helper_globals["_write_direction_debate_debug_dump"] = (
                lambda **kwargs: None
            )
            helper_globals["_record_cost"] = lambda **kwargs: recorded.append(dict(kwargs))
            helper_globals["_cost_trace"] = lambda *args, **kwargs: None
            helper_globals["log_event"] = lambda *args, **kwargs: None
            helper_globals["log_exception"] = lambda *args, **kwargs: None
            helper_globals["_llm_model_id"] = lambda llm: "judge-model"

            decision, comparator_report, audit_report, gap_info = helper(
                user_problem="choose a direction",
                mode="Quant",
                language_hint="English",
                llm=None,
                research_context=None,
                direction_judge_llm=object(),
                cache_payload={},
            )
        finally:
            for key, value in original_values.items():
                helper_globals[key] = value

        self.assertIsNone(decision)
        self.assertIsNone(comparator_report)
        self.assertIsNone(audit_report)
        self.assertIsNone(gap_info)
        self.assertTrue(recorded)
        self.assertEqual(recorded[-1]["stage"], "direction_debate.kickoff")
        self.assertEqual(recorded[-1]["input_tokens"], 600)
        self.assertEqual(recorded[-1]["outcome"], "execution_error")

    def test_codegen_timeout_recovery_uses_fallback_crew_on_transient_failure(self) -> None:
        from crucible.module_runtime import get_runtime

        runtime = get_runtime()
        helper = runtime._kickoff_codegen_with_timeout_recovery
        helper_globals = helper.__globals__
        original_values = {
            "kickoff_crew_with_retry": helper_globals.get("kickoff_crew_with_retry"),
            "is_transient_retryable_error": helper_globals.get(
                "is_transient_retryable_error"
            ),
            "log_event": helper_globals.get("log_event"),
        }
        primary_crew = SimpleNamespace(name="primary")
        fallback_crew = SimpleNamespace(name="fallback")
        calls: list[tuple[object, object, dict[str, object]]] = []

        def _kickoff_stub(crew, crew_name=None, logger=None, log_fields=None):
            calls.append((crew, crew_name, dict(log_fields or {})))
            if crew is primary_crew:
                raise ConnectionError("Request timed out.")
            return "fallback-ok"

        try:
            helper_globals["kickoff_crew_with_retry"] = _kickoff_stub
            helper_globals["is_transient_retryable_error"] = lambda exc: True
            helper_globals["log_event"] = lambda *args, **kwargs: None

            result = helper(
                primary_crew,
                fallback_crew_factory=lambda: fallback_crew,
                mode="Quant",
            )
        finally:
            for key, value in original_values.items():
                helper_globals[key] = value

        self.assertEqual(result, "fallback-ok")
        self.assertEqual(len(calls), 2)
        self.assertIs(calls[0][0], primary_crew)
        self.assertIs(calls[1][0], fallback_crew)
        self.assertEqual(calls[1][1], "codegen_crew_fallback")
        self.assertEqual(calls[1][2]["stage"], "codegen_fallback")

    def test_quality_fix_recovery_helper_uses_last_raw_output(self) -> None:
        from crucible.module_runtime import get_runtime

        runtime = get_runtime()
        helper = runtime._recover_quality_fix_patch_from_last_raw_output
        helper_globals = helper.__globals__
        original_values = {
            "_reformat_code_bundle": helper_globals.get("_reformat_code_bundle"),
            "STRICT_JSON_ENABLED": helper_globals.get("STRICT_JSON_ENABLED"),
        }
        expected_bundle = runtime.CodeBundle(
            project_type="saas",
            files=[runtime.GeneratedFile(path="app.py", content="print('ok')\n")],
        )
        recorded: dict[str, object] = {}

        def _reformat_stub(raw, llm=None, language_hint=None, mode=None):
            recorded["raw"] = raw
            recorded["language_hint"] = language_hint
            recorded["mode"] = mode
            return expected_bundle

        try:
            helper_globals["_reformat_code_bundle"] = _reformat_stub
            helper_globals["STRICT_JSON_ENABLED"] = True

            recovered = helper(
                last_raw_output="not valid json",
                user_problem="build a saas app",
                llm=None,
                mode_name="SaaS",
            )
        finally:
            for key, value in original_values.items():
                helper_globals[key] = value

        self.assertIs(recovered, expected_bundle)
        self.assertEqual(recorded["raw"], "not valid json")
        self.assertEqual(recorded["language_hint"], "English")
        self.assertEqual(recorded["mode"], "SaaS")

    def test_run_quality_fix_recovery_does_not_require_result_or_double_count_cost(self) -> None:
        from crucible.module_runtime import get_runtime

        runtime = get_runtime()
        helper = runtime.run_quality_fix
        helper_globals = helper.__globals__
        tracked_keys = (
            "Agent",
            "Task",
            "Crew",
            "kickoff_crew_with_retry",
            "is_transient_retryable_error",
            "_recover_quality_fix_patch_from_last_raw_output",
            "_merge_code_bundle_patch",
            "_code_bundle_effective_change_count",
            "_record_cost",
            "_cost_trace",
            "_review_allows_new_files",
            "_extract_relevant_paths_from_runtime_log",
            "requires_web_validation",
            "build_quality_fixer_context",
            "_extract_text_from_result",
        )
        original_values = {key: helper_globals.get(key) for key in tracked_keys}
        code_bundle = runtime.CodeBundle(
            project_type="saas",
            files=[runtime.GeneratedFile(path="app.py", content="print('old')\n")],
        )
        patch_bundle = runtime.CodeBundle(
            project_type="saas",
            files=[runtime.GeneratedFile(path="app.py", content="print('new')\n")],
        )
        review_report = runtime.ReviewReport(
            passes=False,
            summary="fix app",
            issues=[
                runtime.ReviewIssue(
                    severity="high",
                    category="bug",
                    description="app needs fix",
                    file="app.py",
                    suggestion="update app.py",
                )
            ],
        )
        cost_records: list[dict[str, object]] = []

        try:
            helper_globals["Agent"] = lambda **kwargs: SimpleNamespace(**kwargs)
            helper_globals["Task"] = lambda **kwargs: SimpleNamespace(**kwargs)
            helper_globals["Crew"] = lambda **kwargs: SimpleNamespace(**kwargs)
            helper_globals["kickoff_crew_with_retry"] = (
                lambda *args, **kwargs: (_ for _ in ()).throw(
                    ConnectionError("Request timed out.")
                )
            )
            helper_globals["is_transient_retryable_error"] = lambda exc: True
            helper_globals["_recover_quality_fix_patch_from_last_raw_output"] = (
                lambda **kwargs: patch_bundle
            )
            helper_globals["_merge_code_bundle_patch"] = (
                lambda original, patch, **kwargs: patch
            )
            helper_globals["_code_bundle_effective_change_count"] = (
                lambda original, merged: 1
            )
            helper_globals["_record_cost"] = lambda **kwargs: cost_records.append(kwargs)
            helper_globals["_cost_trace"] = lambda *args, **kwargs: None
            helper_globals["_review_allows_new_files"] = lambda *args, **kwargs: False
            helper_globals["_extract_relevant_paths_from_runtime_log"] = (
                lambda *args, **kwargs: set()
            )
            helper_globals["requires_web_validation"] = lambda *args, **kwargs: False
            helper_globals["build_quality_fixer_context"] = (
                lambda *args, **kwargs: "quality fixer context"
            )
            helper_globals["_extract_text_from_result"] = (
                lambda result: (_ for _ in ()).throw(
                    AssertionError(
                        "_extract_text_from_result should not run for recovered timeout path"
                    )
                )
            )

            merged, failure_reason = helper(
                user_problem="build a saas app",
                analysis_report=None,
                code_bundle=code_bundle,
                review_report=review_report,
                llm=None,
                affected_files={"app.py"},
                round_idx=0,
            )
        finally:
            for key, value in original_values.items():
                helper_globals[key] = value

        self.assertEqual(failure_reason, "")
        self.assertIsNotNone(merged)
        self.assertEqual(merged.files[0].content, "print('new')\n")
        self.assertEqual(len(cost_records), 1)
        self.assertTrue(cost_records[0]["success"])
        self.assertEqual(cost_records[0]["output_tokens"], 0)
        self.assertEqual(
            cost_records[0]["outcome"],
            "recovered_from_last_raw_output",
        )

    def test_runtime_exception_failure_classifier_distinguishes_execution_classes(self) -> None:
        from crucible.module_runtime import get_runtime

        runtime = get_runtime()

        self.assertEqual(
            runtime._classify_runtime_exception_failure(
                RuntimeError("Request timed out while contacting upstream api")
            ),
            runtime.FailureType.NON_DETERMINISTIC,
        )
        self.assertEqual(
            runtime._classify_runtime_exception_failure(
                ValueError("schema mismatch in CodeBundle JSON output")
            ),
            runtime.FailureType.JSON_INVALID,
        )
        self.assertEqual(
            runtime._classify_runtime_exception_failure(
                RuntimeError("unexpected subprocess exit")
            ),
            runtime.FailureType.EXECUTION_ERROR,
        )
        self.assertEqual(
            runtime._classify_runtime_exception_failure(
                RuntimeError("model not found on upstream provider")
            ),
            runtime.FailureType.POLICY_VIOLATION,
        )

    def test_run_api_version_check_honors_explicit_enabled_even_if_global_flag_is_stale(self) -> None:
        from crucible.module_runtime import get_runtime

        runtime = get_runtime()
        helper_globals = runtime.run_api_version_check.__globals__
        original_values = (
            helper_globals.get("API_VERSION_CHECK_ENABLED"),
            helper_globals.get("_extract_imports_from_code"),
        )
        bundle = runtime.CodeBundle(
            project_type="agent",
            files=[runtime.GeneratedFile(path="worker.py", content="print('ok')\n")],
        )
        try:
            helper_globals["API_VERSION_CHECK_ENABLED"] = False
            helper_globals["_extract_imports_from_code"] = lambda _bundle: {}
            report = runtime.run_api_version_check(bundle, None, enabled=True)
        finally:
            helper_globals["API_VERSION_CHECK_ENABLED"] = original_values[0]
            helper_globals["_extract_imports_from_code"] = original_values[1]

        self.assertEqual(report.summary, "No imports found in generated code.")
        self.assertFalse(report.needs_update)

    def test_resolve_quality_round_limit_reads_fresh_default(self) -> None:
        from crucible.module_runtime import get_runtime

        runtime = get_runtime()
        helper_globals = runtime._resolve_quality_round_limit.__globals__
        original_resolver = helper_globals.get("_resolve_quality_max_rounds_default")
        try:
            helper_globals["_resolve_quality_max_rounds_default"] = lambda: 17
            pro_profile = runtime.RuntimeProfileConfig(
                name="pro",
                gate_control_default=True,
                selective_rerun_default=True,
                quality_max_rounds=None,
                strict_json_default=False,
                cache_default=False,
                snapshot_level="standard",
            )
            lite_profile = runtime.RuntimeProfileConfig(
                name="lite",
                gate_control_default=False,
                selective_rerun_default=False,
                quality_max_rounds=3,
                strict_json_default=False,
                cache_default=False,
                snapshot_level="minimal",
            )
            resolved_default = runtime._resolve_quality_round_limit(pro_profile)
            resolved_profile_cap = runtime._resolve_quality_round_limit(lite_profile)
        finally:
            helper_globals["_resolve_quality_max_rounds_default"] = original_resolver

        self.assertEqual(resolved_default, 17)
        self.assertEqual(resolved_profile_cap, 3)

    def test_librarian_runtime_defaults_reset_from_env_between_runs(self) -> None:
        from crucible.module_runtime import get_runtime

        runtime = get_runtime()
        helper_globals = runtime._apply_librarian_runtime_defaults_from_env.__globals__
        target_modules = [
            helper_globals["_prev_02"],
            helper_globals["_prev_04"],
        ]
        tracked_keys = (
            "LIBRARIAN_ENABLED",
            "LIBRARIAN_SEARCH_PROVIDERS",
            "LIBRARIAN_MAX_RESULTS_PER_QUERY",
            "LIBRARIAN_MAX_CITATIONS",
            "LIBRARIAN_MAX_QUERIES_PER_LANE",
            "LIBRARIAN_MAX_VERIFIED_CITATIONS",
        )
        original_module_values = [
            {key: module.__dict__.get(key) for key in tracked_keys} for module in target_modules
        ]
        original_helper_values = {key: helper_globals.get(key) for key in tracked_keys}
        original_resolver = helper_globals.get("_resolve_librarian_runtime_defaults")
        try:
            helper_globals["_resolve_librarian_runtime_defaults"] = lambda: {
                "enabled": False,
                "search_providers": ["exa", "tavily"],
                "max_results_per_query": 7,
                "max_citations": 19,
                "max_queries_per_lane": 5,
                "cache_window_hours": 12,
                "http_timeout_seconds": 9.5,
                "http_max_bytes": 222222,
                "verify_citations": False,
                "max_verified_citations": 2,
            }
            runtime._apply_librarian_runtime_defaults_from_env()
            for module in target_modules:
                self.assertFalse(module.__dict__["LIBRARIAN_ENABLED"])
                self.assertEqual(module.__dict__["LIBRARIAN_SEARCH_PROVIDERS"], ["exa", "tavily"])
                self.assertEqual(module.__dict__["LIBRARIAN_MAX_RESULTS_PER_QUERY"], 7)
                self.assertEqual(module.__dict__["LIBRARIAN_MAX_CITATIONS"], 19)
                self.assertEqual(module.__dict__["LIBRARIAN_MAX_QUERIES_PER_LANE"], 5)
                self.assertEqual(module.__dict__["LIBRARIAN_MAX_VERIFIED_CITATIONS"], 2)
            self.assertEqual(helper_globals["LIBRARIAN_SEARCH_PROVIDERS"], ["exa", "tavily"])
        finally:
            helper_globals["_resolve_librarian_runtime_defaults"] = original_resolver
            for module, values in zip(target_modules, original_module_values):
                for key, value in values.items():
                    module.__dict__[key] = value
            for key, value in original_helper_values.items():
                helper_globals[key] = value

    def test_local_cache_runtime_defaults_reset_from_env_between_runs(self) -> None:
        from crucible.module_runtime import get_runtime

        runtime = get_runtime()
        helper_globals = runtime._apply_local_cache_runtime_defaults_from_env.__globals__
        target_modules = [helper_globals["_prev_02"]]
        tracked_keys = (
            "LOCAL_CACHE_TTL_HOURS",
            "LOCAL_CACHE_PATH",
        )
        original_module_values = [
            {key: module.__dict__.get(key) for key in tracked_keys} for module in target_modules
        ]
        original_helper_values = {key: helper_globals.get(key) for key in tracked_keys}
        original_resolver = helper_globals.get("_resolve_local_cache_runtime_defaults")
        original_reset = helper_globals.get("reset_local_llm_cache")
        reset_calls = {"count": 0}
        try:
            helper_globals["_resolve_local_cache_runtime_defaults"] = lambda: {
                "enabled": True,
                "ttl_hours": 48,
                "path": "E:/tmp/runtime-cache.sqlite3",
            }
            helper_globals["reset_local_llm_cache"] = (
                lambda: reset_calls.__setitem__("count", reset_calls["count"] + 1)
            )
            runtime._apply_local_cache_runtime_defaults_from_env()
            for module in target_modules:
                self.assertEqual(module.__dict__["LOCAL_CACHE_TTL_HOURS"], 48)
                self.assertEqual(module.__dict__["LOCAL_CACHE_PATH"], "E:/tmp/runtime-cache.sqlite3")
            self.assertEqual(helper_globals["LOCAL_CACHE_TTL_HOURS"], 48)
            self.assertEqual(helper_globals["LOCAL_CACHE_PATH"], "E:/tmp/runtime-cache.sqlite3")
            self.assertEqual(reset_calls["count"], 1)
        finally:
            helper_globals["_resolve_local_cache_runtime_defaults"] = original_resolver
            helper_globals["reset_local_llm_cache"] = original_reset
            for module, values in zip(target_modules, original_module_values):
                for key, value in values.items():
                    module.__dict__[key] = value
            for key, value in original_helper_values.items():
                helper_globals[key] = value

    def test_api_version_check_runtime_defaults_reset_from_env_between_runs(self) -> None:
        from crucible.module_runtime import get_runtime

        runtime = get_runtime()
        helper_globals = runtime._apply_api_version_check_runtime_defaults_from_env.__globals__
        target_modules = [
            helper_globals["_prev_05"],
            helper_globals["_prev_06"],
        ]
        tracked_keys = (
            "API_VERSION_CHECK_ENABLED",
            "API_VERSION_CHECK_MAX_LIBRARIES",
            "API_VERSION_CHECK_TIMEOUT_SECONDS",
            "API_VERSION_CHECK_CACHE_TTL_HOURS",
            "API_VERSION_CHECK_SEVERITY_THRESHOLD",
        )
        original_module_values = [
            {key: module.__dict__.get(key) for key in tracked_keys} for module in target_modules
        ]
        original_helper_values = {key: helper_globals.get(key) for key in tracked_keys}
        original_resolver = helper_globals.get("_resolve_api_version_check_runtime_defaults")
        try:
            helper_globals["_resolve_api_version_check_runtime_defaults"] = lambda: {
                "enabled": False,
                "max_libraries": 11,
                "timeout_seconds": 95,
                "cache_ttl_hours": 36,
                "severity_threshold": "high",
            }
            runtime._apply_api_version_check_runtime_defaults_from_env()
            for module in target_modules:
                self.assertFalse(module.__dict__["API_VERSION_CHECK_ENABLED"])
                self.assertEqual(module.__dict__["API_VERSION_CHECK_MAX_LIBRARIES"], 11)
                self.assertEqual(module.__dict__["API_VERSION_CHECK_TIMEOUT_SECONDS"], 95)
                self.assertEqual(module.__dict__["API_VERSION_CHECK_CACHE_TTL_HOURS"], 36)
                self.assertEqual(module.__dict__["API_VERSION_CHECK_SEVERITY_THRESHOLD"], "high")
            self.assertFalse(helper_globals["API_VERSION_CHECK_ENABLED"])
            self.assertEqual(helper_globals["API_VERSION_CHECK_MAX_LIBRARIES"], 11)
            self.assertEqual(helper_globals["API_VERSION_CHECK_TIMEOUT_SECONDS"], 95)
            self.assertEqual(helper_globals["API_VERSION_CHECK_CACHE_TTL_HOURS"], 36)
            self.assertEqual(helper_globals["API_VERSION_CHECK_SEVERITY_THRESHOLD"], "high")
        finally:
            helper_globals["_resolve_api_version_check_runtime_defaults"] = original_resolver
            for module, values in zip(target_modules, original_module_values):
                for key, value in values.items():
                    module.__dict__[key] = value
            for key, value in original_helper_values.items():
                helper_globals[key] = value

    def test_quality_runtime_defaults_reset_from_env_between_runs(self) -> None:
        from crucible.module_runtime import get_runtime

        runtime = get_runtime()
        helper_globals = runtime._apply_quality_runtime_defaults_from_env.__globals__
        target_modules = [
            helper_globals["_prev_05"],
            helper_globals["_prev_06"],
        ]
        tracked_keys = (
            "QUALITY_MAX_ROUNDS",
            "QUALITY_CONTEXT_MAX_CHARS",
            "QUALITY_CODE_BUNDLE_MAX_CHARS",
            "QUALITY_RUNTIME_LOG_MAX_CHARS",
            "QUALITY_JSON_RETRY_ATTEMPTS",
            "QUALITY_CODE_FILE_MAX_CHARS",
            "QUALITY_CODE_FILE_MAX_CHARS_ENTRYPOINT",
            "QUALITY_CODE_FILE_MAX_CHARS_SCOPED",
            "QUALITY_CODE_FILE_MAX_CHARS_PRIORITY",
            "QUALITY_CODE_SNIPPET_HEAD_CHARS",
            "QUALITY_CODE_SNIPPET_TAIL_CHARS",
            "QUALITY_CONTEXT_TREE_MAX_CHARS",
            "QUALITY_RUNTIME_LOG_TAIL_CHARS",
            "QUALITY_MAX_FILES_WITH_CONTENT_ROUND0",
            "QUALITY_MAX_FILES_WITH_CONTENT_ROUNDN",
            "QUALITY_EARLY_STOP_STAGNATION_ROUNDS",
            "QUALITY_FIX_FUSE_CONSECUTIVE_FAILURES",
        )
        original_module_values = [
            {key: module.__dict__.get(key) for key in tracked_keys} for module in target_modules
        ]
        original_helper_values = {key: helper_globals.get(key) for key in tracked_keys}
        original_resolver = helper_globals.get("_resolve_quality_runtime_defaults")
        try:
            helper_globals["_resolve_quality_runtime_defaults"] = lambda: {
                "max_rounds": 13,
                "context_max_chars": 9001,
                "code_bundle_max_chars": 70001,
                "runtime_log_max_chars": 3456,
                "json_retry_attempts": 4,
                "code_file_max_chars": 16000,
                "code_file_max_chars_entrypoint": 26000,
                "code_file_max_chars_scoped": 52000,
                "code_file_max_chars_priority": 31000,
                "code_snippet_head_chars": 6100,
                "code_snippet_tail_chars": 2100,
                "context_tree_max_chars": 6200,
                "runtime_log_tail_chars": 2200,
                "max_files_with_content_round0": 14,
                "max_files_with_content_roundn": 8,
                "early_stop_stagnation_rounds": 5,
                "fix_fuse_consecutive_failures": 3,
            }
            runtime._apply_quality_runtime_defaults_from_env()
            for module in target_modules:
                self.assertEqual(module.__dict__["QUALITY_MAX_ROUNDS"], 13)
                self.assertEqual(module.__dict__["QUALITY_JSON_RETRY_ATTEMPTS"], 4)
                self.assertEqual(module.__dict__["QUALITY_EARLY_STOP_STAGNATION_ROUNDS"], 5)
                self.assertEqual(module.__dict__["QUALITY_FIX_FUSE_CONSECUTIVE_FAILURES"], 3)
                self.assertEqual(module.__dict__["QUALITY_RUNTIME_LOG_MAX_CHARS"], 3456)
                self.assertEqual(module.__dict__["QUALITY_MAX_FILES_WITH_CONTENT_ROUND0"], 14)
            self.assertEqual(helper_globals["QUALITY_MAX_ROUNDS"], 13)
            self.assertEqual(helper_globals["QUALITY_JSON_RETRY_ATTEMPTS"], 4)
            self.assertEqual(helper_globals["QUALITY_EARLY_STOP_STAGNATION_ROUNDS"], 5)
            self.assertEqual(helper_globals["QUALITY_FIX_FUSE_CONSECUTIVE_FAILURES"], 3)
            self.assertEqual(helper_globals["QUALITY_RUNTIME_LOG_MAX_CHARS"], 3456)
            self.assertEqual(helper_globals["QUALITY_MAX_FILES_WITH_CONTENT_ROUND0"], 14)
        finally:
            helper_globals["_resolve_quality_runtime_defaults"] = original_resolver
            for module, values in zip(target_modules, original_module_values):
                for key, value in values.items():
                    module.__dict__[key] = value
            for key, value in original_helper_values.items():
                helper_globals[key] = value

    def test_project_context_scan_defaults_reset_from_env_between_runs(self) -> None:
        from crucible.module_runtime import get_runtime

        runtime = get_runtime()
        helper_globals = runtime._apply_project_context_scan_defaults_from_env.__globals__
        target_modules = [helper_globals["_prev_03"]]
        tracked_keys = (
            "QUICK_MAX_TREE_ENTRIES",
            "QUICK_MAX_DEPTH",
            "QUICK_MAX_FILE_BYTES",
            "QUICK_MAX_SNIPPET_CHARS",
            "FULL_MAX_TREE_ENTRIES",
            "FULL_MAX_DEPTH",
            "FULL_MAX_FILE_BYTES",
            "FULL_MAX_SNIPPET_CHARS",
            "FULL_MAX_TOTAL_CHARS",
        )
        original_module_values = [
            {key: module.__dict__.get(key) for key in tracked_keys} for module in target_modules
        ]
        original_helper_values = {key: helper_globals.get(key) for key in tracked_keys}
        original_resolver = helper_globals.get("_resolve_project_context_scan_defaults")
        try:
            helper_globals["_resolve_project_context_scan_defaults"] = lambda: {
                "quick_max_tree_entries": 11,
                "quick_max_depth": 2,
                "quick_max_file_bytes": 12345,
                "quick_max_snippet_chars": 2345,
                "full_max_tree_entries": 999,
                "full_max_depth": 7,
                "full_max_file_bytes": 54321,
                "full_max_snippet_chars": 6789,
                "full_max_total_chars": 13579,
            }
            runtime._apply_project_context_scan_defaults_from_env()
            for module in target_modules:
                self.assertEqual(module.__dict__["QUICK_MAX_TREE_ENTRIES"], 11)
                self.assertEqual(module.__dict__["QUICK_MAX_DEPTH"], 2)
                self.assertEqual(module.__dict__["FULL_MAX_TOTAL_CHARS"], 13579)
            self.assertEqual(helper_globals["QUICK_MAX_TREE_ENTRIES"], 11)
            self.assertEqual(helper_globals["QUICK_MAX_DEPTH"], 2)
            self.assertEqual(helper_globals["FULL_MAX_TOTAL_CHARS"], 13579)
        finally:
            helper_globals["_resolve_project_context_scan_defaults"] = original_resolver
            for module, values in zip(target_modules, original_module_values):
                for key, value in values.items():
                    module.__dict__[key] = value
            for key, value in original_helper_values.items():
                helper_globals[key] = value

    def test_research_runtime_defaults_reset_from_env_between_runs(self) -> None:
        from crucible.module_runtime import get_runtime

        runtime = get_runtime()
        helper_globals = runtime._apply_research_runtime_defaults_from_env.__globals__
        target_modules = [helper_globals["_prev_02"]]
        tracked_keys = (
            "DIRECTION_REFINEMENT_MAX_ITERATIONS",
            "DIRECTION_REFINEMENT_ENABLED",
            "OPENROUTER_LLM_TIMEOUT_SECONDS",
            "ALIBABA_CODING_PLAN_LLM_TIMEOUT_SECONDS",
            "ALIBABA_CODING_PLAN_INITIAL_RESPONSE_TIMEOUT_SECONDS",
        )
        original_module_values = [
            {key: module.__dict__.get(key) for key in tracked_keys} for module in target_modules
        ]
        original_helper_values = {key: helper_globals.get(key) for key in tracked_keys}
        original_direction_resolver = helper_globals.get("_resolve_direction_refinement_runtime_defaults")
        original_timeout_resolver = helper_globals.get("_resolve_openrouter_llm_timeout_seconds")
        original_alibaba_timeout_resolver = helper_globals.get(
            "_resolve_alibaba_coding_plan_llm_timeout_seconds"
        )
        original_alibaba_initial_response_timeout_resolver = helper_globals.get(
            "_resolve_alibaba_coding_plan_initial_response_timeout_seconds"
        )
        try:
            helper_globals["_resolve_direction_refinement_runtime_defaults"] = lambda: {
                "max_iterations": 6,
                "enabled": False,
            }
            helper_globals["_resolve_openrouter_llm_timeout_seconds"] = lambda: 321
            helper_globals["_resolve_alibaba_coding_plan_llm_timeout_seconds"] = lambda: 654
            helper_globals["_resolve_alibaba_coding_plan_initial_response_timeout_seconds"] = (
                lambda: 123
            )
            runtime._apply_research_runtime_defaults_from_env()
            for module in target_modules:
                self.assertEqual(module.__dict__["DIRECTION_REFINEMENT_MAX_ITERATIONS"], 6)
                self.assertFalse(module.__dict__["DIRECTION_REFINEMENT_ENABLED"])
                self.assertEqual(module.__dict__["OPENROUTER_LLM_TIMEOUT_SECONDS"], 321)
                self.assertEqual(
                    module.__dict__["ALIBABA_CODING_PLAN_LLM_TIMEOUT_SECONDS"], 654
                )
                self.assertEqual(
                    module.__dict__["ALIBABA_CODING_PLAN_INITIAL_RESPONSE_TIMEOUT_SECONDS"],
                    123,
                )
            self.assertEqual(helper_globals["DIRECTION_REFINEMENT_MAX_ITERATIONS"], 6)
            self.assertFalse(helper_globals["DIRECTION_REFINEMENT_ENABLED"])
            self.assertEqual(helper_globals["OPENROUTER_LLM_TIMEOUT_SECONDS"], 321)
            self.assertEqual(
                helper_globals["ALIBABA_CODING_PLAN_LLM_TIMEOUT_SECONDS"], 654
            )
            self.assertEqual(
                helper_globals["ALIBABA_CODING_PLAN_INITIAL_RESPONSE_TIMEOUT_SECONDS"],
                123,
            )
        finally:
            helper_globals["_resolve_direction_refinement_runtime_defaults"] = original_direction_resolver
            helper_globals["_resolve_openrouter_llm_timeout_seconds"] = original_timeout_resolver
            helper_globals["_resolve_alibaba_coding_plan_llm_timeout_seconds"] = (
                original_alibaba_timeout_resolver
            )
            helper_globals["_resolve_alibaba_coding_plan_initial_response_timeout_seconds"] = (
                original_alibaba_initial_response_timeout_resolver
            )
            for module, values in zip(target_modules, original_module_values):
                for key, value in values.items():
                    module.__dict__[key] = value
            for key, value in original_helper_values.items():
                helper_globals[key] = value

    def test_local_llm_cache_key_isolated_by_provider(self) -> None:
        from crucible.module_runtime import get_runtime

        runtime = get_runtime()
        payload = {"probe": "provider-isolation"}
        globals_dict = runtime._cache_key.__globals__
        original_active_provider = globals_dict.get("ACTIVE_LLM_PROVIDER")
        try:
            globals_dict["ACTIVE_LLM_PROVIDER"] = "openrouter"
            openrouter_key_a = runtime._cache_key("direction_seed_plan", payload)
            openrouter_key_b = runtime._cache_key("direction_seed_plan", payload)

            globals_dict["ACTIVE_LLM_PROVIDER"] = "alibaba_coding_plan"
            alibaba_key = runtime._cache_key("direction_seed_plan", payload)
        finally:
            globals_dict["ACTIVE_LLM_PROVIDER"] = original_active_provider

        self.assertEqual(openrouter_key_a, openrouter_key_b)
        self.assertNotEqual(openrouter_key_a, alibaba_key)

    def test_direction_and_librarian_llm_cache_refresh_when_timeout_changes(self) -> None:
        from crucible.module_runtime import get_runtime

        runtime = get_runtime()
        globals_dict = runtime._get_direction_judge_llm.__globals__
        original_direction_llm = globals_dict.get("_DIRECTION_JUDGE_LLM")
        original_librarian_llm = globals_dict.get("_LIBRARIAN_LLM")
        original_direction_model_resolver = globals_dict.get("_resolve_direction_judge_model_id")
        original_librarian_model_resolver = globals_dict.get("_resolve_librarian_model_id")
        original_timeout_resolver = globals_dict.get("_build_llm_timeout_value")
        original_create_llm = globals_dict.get("_create_openrouter_llm")
        original_llm_model_id = globals_dict.get("_llm_model_id")
        original_llm_timeout_signature = globals_dict.get("_llm_timeout_signature")
        created_calls = []
        try:
            globals_dict["_DIRECTION_JUDGE_LLM"] = SimpleNamespace(model="judge-model", timeout=180.0)
            globals_dict["_LIBRARIAN_LLM"] = SimpleNamespace(model="research-model", timeout=180.0)
            globals_dict["_resolve_direction_judge_model_id"] = lambda: "judge-model"
            globals_dict["_resolve_librarian_model_id"] = lambda: "research-model"
            globals_dict["_build_llm_timeout_value"] = (
                lambda _provider=None, timeout_seconds=None: 321.0
            )
            globals_dict["_llm_model_id"] = lambda llm: getattr(llm, "model", "")
            globals_dict["_llm_timeout_signature"] = (
                lambda llm: float(getattr(llm, "timeout", 0.0))
            )

            def _fake_create_openrouter_llm(
                *,
                model_id: str,
                temperature: float = 0.7,
                timeout_seconds=None,
                enable_cost_tracking: bool = True,
                provider: str | None = None,
            ):
                llm = SimpleNamespace(
                    model=model_id,
                    timeout=float(timeout_seconds),
                    temperature=temperature,
                    enable_cost_tracking=enable_cost_tracking,
                    _quant_llm_provider=provider,
                )
                created_calls.append(llm)
                return llm

            globals_dict["_create_openrouter_llm"] = _fake_create_openrouter_llm

            direction_llm = runtime._get_direction_judge_llm()
            librarian_llm = runtime._get_librarian_llm()

            self.assertEqual(direction_llm.timeout, 321.0)
            self.assertEqual(librarian_llm.timeout, 321.0)
            self.assertIs(globals_dict["_DIRECTION_JUDGE_LLM"], direction_llm)
            self.assertIs(globals_dict["_LIBRARIAN_LLM"], librarian_llm)
            self.assertEqual(len(created_calls), 2)
        finally:
            globals_dict["_DIRECTION_JUDGE_LLM"] = original_direction_llm
            globals_dict["_LIBRARIAN_LLM"] = original_librarian_llm
            globals_dict["_resolve_direction_judge_model_id"] = original_direction_model_resolver
            globals_dict["_resolve_librarian_model_id"] = original_librarian_model_resolver
            globals_dict["_build_llm_timeout_value"] = original_timeout_resolver
            globals_dict["_create_openrouter_llm"] = original_create_llm
            globals_dict["_llm_model_id"] = original_llm_model_id
            globals_dict["_llm_timeout_signature"] = original_llm_timeout_signature

    def test_llm_model_id_falls_back_to_fresh_primary_model_resolver(self) -> None:
        from crucible.module_runtime import get_runtime

        runtime = get_runtime()
        globals_dict = runtime._llm_model_id.__globals__
        original_model_id = globals_dict.get("MODEL_ID")
        original_resolver = globals_dict.get("_resolve_primary_model_id")
        try:
            globals_dict["MODEL_ID"] = "stale-primary-model"
            globals_dict["_resolve_primary_model_id"] = lambda: "fresh-primary-model"
            self.assertEqual(runtime._llm_model_id(object()), "fresh-primary-model")
        finally:
            globals_dict["MODEL_ID"] = original_model_id
            globals_dict["_resolve_primary_model_id"] = original_resolver

    def test_output_validation_mode_overrides_propagate_to_all_execution_sections(self) -> None:
        from crucible.module_runtime import get_runtime

        runtime = get_runtime()
        helper_globals = runtime._apply_output_validation_mode_overrides.__globals__
        target_modules = [
            helper_globals["_prev_00"],
            helper_globals["_prev_01"],
            helper_globals["_prev_02"],
            helper_globals["_prev_03"],
            helper_globals["_prev_04"],
            helper_globals["_prev_05"],
            helper_globals["_prev_06"],
        ]
        original_module_values = [
            (
                module.__dict__.get("STRICT_JSON_ENABLED"),
                module.__dict__.get("CREWAI_OUTPUT_PYDANTIC"),
                module.__dict__.get("_STRICT_JSON_PYDANTIC_WARNED"),
            )
            for module in target_modules
        ]
        original_helper_values = (
            helper_globals.get("CREWAI_OUTPUT_PYDANTIC"),
            helper_globals.get("_STRICT_JSON_PYDANTIC_WARNED"),
            helper_globals.get("_resolve_crewai_output_pydantic_enabled"),
        )
        try:
            helper_globals["_resolve_crewai_output_pydantic_enabled"] = lambda: True
            for module in target_modules:
                if "STRICT_JSON_ENABLED" in module.__dict__:
                    module.__dict__["STRICT_JSON_ENABLED"] = True
                if "CREWAI_OUTPUT_PYDANTIC" in module.__dict__:
                    module.__dict__["CREWAI_OUTPUT_PYDANTIC"] = True
                if "_STRICT_JSON_PYDANTIC_WARNED" in module.__dict__:
                    module.__dict__["_STRICT_JSON_PYDANTIC_WARNED"] = False
            helper_globals["CREWAI_OUTPUT_PYDANTIC"] = True
            helper_globals["_STRICT_JSON_PYDANTIC_WARNED"] = False

            runtime._apply_output_validation_mode_overrides()

            for module in target_modules:
                if "CREWAI_OUTPUT_PYDANTIC" in module.__dict__:
                    self.assertFalse(module.__dict__["CREWAI_OUTPUT_PYDANTIC"])
            self.assertFalse(helper_globals["CREWAI_OUTPUT_PYDANTIC"])
            self.assertTrue(helper_globals["_STRICT_JSON_PYDANTIC_WARNED"])
        finally:
            for module, values in zip(target_modules, original_module_values):
                strict_json, crewai_output_pydantic, warned = values
                if "STRICT_JSON_ENABLED" in module.__dict__ or strict_json is not None:
                    module.__dict__["STRICT_JSON_ENABLED"] = strict_json
                if "CREWAI_OUTPUT_PYDANTIC" in module.__dict__ or crewai_output_pydantic is not None:
                    module.__dict__["CREWAI_OUTPUT_PYDANTIC"] = crewai_output_pydantic
                if "_STRICT_JSON_PYDANTIC_WARNED" in module.__dict__ or warned is not None:
                    module.__dict__["_STRICT_JSON_PYDANTIC_WARNED"] = warned
            helper_globals["CREWAI_OUTPUT_PYDANTIC"] = original_helper_values[0]
            helper_globals["_STRICT_JSON_PYDANTIC_WARNED"] = original_helper_values[1]
            helper_globals["_resolve_crewai_output_pydantic_enabled"] = original_helper_values[2]

    def test_output_validation_mode_overrides_reset_from_env_between_runs(self) -> None:
        from crucible.module_runtime import get_runtime

        runtime = get_runtime()
        helper_globals = runtime._apply_output_validation_mode_overrides.__globals__
        target_modules = [
            helper_globals["_prev_00"],
            helper_globals["_prev_01"],
            helper_globals["_prev_02"],
            helper_globals["_prev_03"],
            helper_globals["_prev_04"],
            helper_globals["_prev_05"],
            helper_globals["_prev_06"],
        ]
        original_module_values = [
            (
                module.__dict__.get("STRICT_JSON_ENABLED"),
                module.__dict__.get("CREWAI_OUTPUT_PYDANTIC"),
                module.__dict__.get("_STRICT_JSON_PYDANTIC_WARNED"),
            )
            for module in target_modules
        ]
        original_helper_values = (
            helper_globals.get("CREWAI_OUTPUT_PYDANTIC"),
            helper_globals.get("_STRICT_JSON_PYDANTIC_WARNED"),
            helper_globals.get("_resolve_crewai_output_pydantic_enabled"),
        )
        try:
            helper_globals["_resolve_crewai_output_pydantic_enabled"] = lambda: True

            for module in target_modules:
                if "STRICT_JSON_ENABLED" in module.__dict__:
                    module.__dict__["STRICT_JSON_ENABLED"] = True
                if "CREWAI_OUTPUT_PYDANTIC" in module.__dict__:
                    module.__dict__["CREWAI_OUTPUT_PYDANTIC"] = True
                if "_STRICT_JSON_PYDANTIC_WARNED" in module.__dict__:
                    module.__dict__["_STRICT_JSON_PYDANTIC_WARNED"] = False
            helper_globals["CREWAI_OUTPUT_PYDANTIC"] = True
            helper_globals["_STRICT_JSON_PYDANTIC_WARNED"] = False

            runtime._apply_output_validation_mode_overrides()

            for module in target_modules:
                if "STRICT_JSON_ENABLED" in module.__dict__:
                    module.__dict__["STRICT_JSON_ENABLED"] = False
                if "CREWAI_OUTPUT_PYDANTIC" in module.__dict__:
                    module.__dict__["CREWAI_OUTPUT_PYDANTIC"] = False
                if "_STRICT_JSON_PYDANTIC_WARNED" in module.__dict__:
                    module.__dict__["_STRICT_JSON_PYDANTIC_WARNED"] = True
            helper_globals["CREWAI_OUTPUT_PYDANTIC"] = False
            helper_globals["_STRICT_JSON_PYDANTIC_WARNED"] = True

            runtime._apply_output_validation_mode_overrides()

            for module in target_modules:
                if "CREWAI_OUTPUT_PYDANTIC" in module.__dict__:
                    self.assertTrue(module.__dict__["CREWAI_OUTPUT_PYDANTIC"])
                if "_STRICT_JSON_PYDANTIC_WARNED" in module.__dict__:
                    self.assertFalse(module.__dict__["_STRICT_JSON_PYDANTIC_WARNED"])
            self.assertTrue(helper_globals["CREWAI_OUTPUT_PYDANTIC"])
            self.assertFalse(helper_globals["_STRICT_JSON_PYDANTIC_WARNED"])
        finally:
            for module, values in zip(target_modules, original_module_values):
                strict_json, crewai_output_pydantic, warned = values
                if "STRICT_JSON_ENABLED" in module.__dict__ or strict_json is not None:
                    module.__dict__["STRICT_JSON_ENABLED"] = strict_json
                if "CREWAI_OUTPUT_PYDANTIC" in module.__dict__ or crewai_output_pydantic is not None:
                    module.__dict__["CREWAI_OUTPUT_PYDANTIC"] = crewai_output_pydantic
                if "_STRICT_JSON_PYDANTIC_WARNED" in module.__dict__ or warned is not None:
                    module.__dict__["_STRICT_JSON_PYDANTIC_WARNED"] = warned
            helper_globals["CREWAI_OUTPUT_PYDANTIC"] = original_helper_values[0]
            helper_globals["_STRICT_JSON_PYDANTIC_WARNED"] = original_helper_values[1]
            helper_globals["_resolve_crewai_output_pydantic_enabled"] = original_helper_values[2]

    def test_fallback_direction_seed_plan_rejects_invalid_registry_output(self) -> None:
        from crucible.module_runtime import get_runtime

        runtime = get_runtime()
        globals_dict = runtime._fallback_direction_seed_plan.__globals__
        original = globals_dict["_get_mode_config"]

        class _BadModeConfig:
            name = "   "

        globals_dict["_get_mode_config"] = lambda _mode: _BadModeConfig()
        try:
            with self.assertRaisesRegex(ValueError, "invalid project type"):
                runtime._fallback_direction_seed_plan(
                    "build an autonomous trading assistant",
                    mode="Agent",
                )
        finally:
            globals_dict["_get_mode_config"] = original

    def test_search_template_mode_helpers_reject_unknown_or_conflicting_modes(self) -> None:
        from crucible.module_runtime import get_runtime

        runtime = get_runtime()
        quant_templates = runtime._get_search_templates_for_mode("Quant")
        self.assertIn("market", quant_templates)
        with self.assertRaisesRegex(ValueError, "Unsupported mode"):
            runtime._get_search_templates_for_mode("unknown-mode")

        queries = runtime._build_lane_queries_from_breakdown(
            {
                "mode_name": "Agent",
                "core_objective": "build a daemonized review worker",
                "entities": ["worker"],
                "constraints": ["deterministic"],
                "domain_keywords": ["automation"],
                "lane_focus": {"technical": ["retry", "idempotent"]},
            },
            "technical",
            mode_name="Agent",
            language="en",
        )
        self.assertTrue(any("daemon" in query.lower() or "idempotent" in query.lower() for query in queries))

        with self.assertRaisesRegex(ValueError, "conflicted with the explicit mode"):
            runtime._build_lane_queries_from_breakdown(
                {
                    "mode_name": "SaaS",
                    "core_objective": "build a daemonized review worker",
                    "entities": ["worker"],
                    "constraints": ["deterministic"],
                    "domain_keywords": ["automation"],
                    "lane_focus": {"technical": ["retry", "idempotent"]},
                },
                "technical",
                mode_name="Agent",
                language="en",
            )

    def test_librarian_breakdown_rejects_invalid_registry_output(self) -> None:
        from crucible.module_runtime import get_runtime

        runtime = get_runtime()
        globals_dict = runtime._build_librarian_problem_breakdown.__globals__
        original = globals_dict["_project_type_for_mode"]

        def _raise_invalid_project_type(_mode: str) -> str:
            raise ValueError(
                "Resolved mode config produced invalid project type '   '. Expected one of: quant, saas, agent"
            )

        globals_dict["_project_type_for_mode"] = _raise_invalid_project_type
        try:
            with self.assertRaisesRegex(ValueError, "invalid project type"):
                runtime._build_librarian_problem_breakdown(
                    "build a daemonized review worker",
                    mode="Agent",
                )
        finally:
            globals_dict["_project_type_for_mode"] = original

    def test_librarian_query_helpers_reject_invalid_registry_output(self) -> None:
        from crucible.module_runtime import get_runtime

        runtime = get_runtime()

        def _raise_invalid_project_type(_mode_cfg: object) -> str:
            raise ValueError(
                "Resolved mode config produced invalid project type '   '. Expected one of: quant, saas, agent"
            )

        # Sentinel LLM — never actually called; prevents API-key sys.exit before
        # _validated_mode_project_type is reached.
        class _SentinelLLM:
            pass

        def _stub_get_librarian_llm() -> "_SentinelLLM":
            return _SentinelLLM()

        smart_globals = runtime._build_smart_search_queries.__globals__
        smart_original = smart_globals["_validated_mode_project_type"]
        llm_key = "_get_librarian_llm"
        llm_original = smart_globals.get(llm_key)
        smart_globals["_validated_mode_project_type"] = _raise_invalid_project_type
        smart_globals[llm_key] = _stub_get_librarian_llm
        try:
            with self.assertRaisesRegex(ValueError, "invalid project type"):
                runtime._build_smart_search_queries(
                    "build a daemonized review worker",
                    "Agent",
                    {"entities": [], "constraints": [], "domain_keywords": []},
                    language_hint="English",
                )
        finally:
            smart_globals["_validated_mode_project_type"] = smart_original
            if llm_original is None:
                smart_globals.pop(llm_key, None)
            else:
                smart_globals[llm_key] = llm_original

        plan_globals = runtime._build_librarian_query_plan.__globals__
        plan_original = plan_globals["_validated_mode_project_type"]
        plan_globals["_validated_mode_project_type"] = _raise_invalid_project_type
        try:
            with self.assertRaisesRegex(ValueError, "invalid project type"):
                runtime._build_librarian_query_plan(
                    "build a daemonized review worker",
                    "Agent",
                )
        finally:
            plan_globals["_validated_mode_project_type"] = plan_original

    def test_direction_summary_alignment_rewrites_stale_selected_direction_intro(
        self,
    ) -> None:
        from crucible.module_runtime import get_runtime

        runtime = get_runtime()
        decision = runtime.DirectionDecision(
            selected_direction="C",
            summary=(
                "選擇 B（Spread 建模與 Beta 估計方法論驗證）作為首選方向。 "
                "Deterministic evidence rerank favored C because the original choice had no grounded support."
            ),
            options=[
                runtime.DirectionOption(
                    key="A",
                    name="配對篩選管線與共整合檢定模組",
                    thesis="A thesis",
                    primary_metric="metric",
                    fastest_test="test",
                    major_risk="risk",
                ),
                runtime.DirectionOption(
                    key="B",
                    name="Spread 建模與 Beta 估計方法論驗證",
                    thesis="B thesis",
                    primary_metric="metric",
                    fastest_test="test",
                    major_risk="risk",
                ),
                runtime.DirectionOption(
                    key="C",
                    name="跨交易所執行層風險建模",
                    thesis="C thesis",
                    primary_metric="metric",
                    fastest_test="test",
                    major_risk="risk",
                ),
            ],
            backup_candidates=["A", "D"],
            go_conditions=["go"],
            kill_criteria=["kill"],
            confidence="low",
            verify_plan=["verify"],
        )

        aligned = runtime._align_direction_decision_summary_with_selection(decision)
        self.assertIsNotNone(aligned)
        self.assertTrue(
            aligned.summary.startswith("選擇 C（跨交易所執行層風險建模）作為首選方向。")
        )
        self.assertNotIn(
            "選擇 B（Spread 建模與 Beta 估計方法論驗證）作為首選方向。",
            aligned.summary,
        )
        self.assertIn(
            "Deterministic evidence rerank favored C because the original choice had no grounded support.",
            aligned.summary,
        )

    def test_direction_summary_alignment_discards_stale_old_primary_rationale_after_rerank(
        self,
    ) -> None:
        from crucible.module_runtime import get_runtime

        runtime = get_runtime()
        decision = runtime.DirectionDecision(
            selected_direction="D",
            summary=(
                "選擇 D（邊際成本與期望收益精確建模）作為首選方向。 "
                "方向 A（動態 Hedge Ratio 估計方法論驗證）為首選，因其在 comparator 評估中 composite_score 最高。 "
                "Deterministic evidence rerank favored D because the original choice had no grounded support."
            ),
            options=[
                runtime.DirectionOption(
                    key="A",
                    name="動態 Hedge Ratio 估計方法論驗證",
                    thesis="A thesis",
                    primary_metric="metric",
                    fastest_test="test",
                    major_risk="risk",
                ),
                runtime.DirectionOption(
                    key="D",
                    name="邊際成本與期望收益精確建模",
                    thesis="D thesis",
                    primary_metric="metric",
                    fastest_test="test",
                    major_risk="risk",
                ),
            ],
            backup_candidates=["C", "A"],
            go_conditions=["go"],
            kill_criteria=["kill"],
            confidence="low",
            verify_plan=["verify"],
        )

        aligned = runtime._align_direction_decision_summary_with_selection(decision)
        self.assertIsNotNone(aligned)
        self.assertTrue(
            aligned.summary.startswith("選擇 D（邊際成本與期望收益精確建模）作為首選方向。")
        )
        self.assertNotIn("方向 A（動態 Hedge Ratio 估計方法論驗證）為首選", aligned.summary)
        self.assertIn(
            "Deterministic evidence rerank favored D because the original choice had no grounded support.",
            aligned.summary,
        )
        self.assertIn("備選方向：C；A。", aligned.summary)

    def test_code_bundle_sanitizer_rejects_invalid_project_type_and_canonicalizes_valid_values(
        self,
    ) -> None:
        from crucible.module_runtime import get_runtime

        runtime = get_runtime()
        sanitized = runtime._sanitize_code_bundle(
            runtime.CodeBundle(
                project_type="SAAS",
                files=[runtime.GeneratedFile(path="src/main.py", content="print('ok')\n")],
            )
        )
        self.assertIsNotNone(sanitized)
        self.assertEqual(sanitized.project_type, "saas")

        rejected = runtime._sanitize_code_bundle(
            runtime.CodeBundle(
                project_type="unknown-mode",
                files=[runtime.GeneratedFile(path="src/main.py", content="print('ok')\n")],
            )
        )
        self.assertIsNone(rejected)

    def test_repair_cjk_punctuation_fixes_compile_blocking_chars(self) -> None:
        """The deterministic CJK-punctuation repairer must replace fullwidth /
        ideographic punctuation that breaks Python ``compile()`` with their
        ASCII equivalents — covering the four failure shapes the codegen
        pipeline saw in production:

        - U+3002 ``。``  trailing/inline ideographic full stop
        - U+FF08 ``（``  fullwidth left parenthesis
        - U+FF0C ``，``  fullwidth comma
        - ``invalid decimal literal`` from CJK full stop inside a numeric
          literal (e.g. ``1。5`` instead of ``1.5``)
        """
        from crucible.module_runtime import get_runtime

        runtime = get_runtime()
        repair = runtime._repair_cjk_punctuation_in_python_source

        cases = [
            # (original, expected, description)
            (
                "def f(x):\n    return print（x）\n",
                "def f(x):\n    return print(x)\n",
                "fullwidth parens",
            ),
            (
                "def g(x):\n    return foo(1，2，3)\n",
                "def g(x):\n    return foo(1,2,3)\n",
                "fullwidth commas in argument list",
            ),
            (
                "def h():\n    return 1。5\n",
                "def h():\n    return 1.5\n",
                "ideographic full stop inside decimal literal",
            ),
            (
                "def k(x):\n    return foo（x，2）\n",
                "def k(x):\n    return foo(x,2)\n",
                "mixed fullwidth parens + comma on same line",
            ),
        ]
        for original, expected, desc in cases:
            with self.subTest(desc=desc):
                repaired = repair(original, "test.py")
                self.assertEqual(repaired, expected, desc)
                # And the repaired source must compile.
                compile(repaired, "test.py", "exec")

    def test_repair_cjk_punctuation_preserves_legitimate_unicode(self) -> None:
        """CJK punctuation that lives inside string literals, comments, and
        docstrings must NEVER be mutated — those are valid Python and the
        characters carry semantic meaning the user expects to survive.
        """
        from crucible.module_runtime import get_runtime

        runtime = get_runtime()
        repair = runtime._repair_cjk_punctuation_in_python_source

        sources = [
            (
                "msg = \"這是一段中文。也包含（括號）。\"\n",
                "regular string literal with CJK punctuation",
            ),
            (
                "# 這是註釋。包含（括號）和，逗號。\n"
                "x = 1\n",
                "comment with CJK punctuation",
            ),
            (
                "def fn():\n"
                "    \"\"\"做某件事。包含（括號）和，逗號。\"\"\"\n"
                "    return 1\n",
                "docstring with CJK punctuation",
            ),
            (
                # Already-valid Python: must compile, must round-trip unchanged.
                "x = 1\ny = 2\nprint(x + y)\n",
                "already-valid ASCII Python",
            ),
        ]
        for src, desc in sources:
            with self.subTest(desc=desc):
                # Sanity: input must compile (otherwise the test is buggy).
                compile(src, "test.py", "exec")
                repaired = repair(src, "test.py")
                self.assertEqual(repaired, src, desc)

    def test_repair_cjk_punctuation_safe_bail_on_unrelated_syntax_error(self) -> None:
        """When the SyntaxError is not at a known CJK character (e.g. an
        unterminated string literal, missing ``:`` after ``def``), the
        repairer must return the **original** content unchanged — surfacing
        the problem to the LLM-driven repair pass with full fidelity rather
        than mutating into something the model can't recognise.
        """
        from crucible.module_runtime import get_runtime

        runtime = get_runtime()
        repair = runtime._repair_cjk_punctuation_in_python_source

        # Unterminated string — error position points at the quote, not a CJK char.
        broken_unterminated = "def f():\n    return \"hello\n"
        self.assertEqual(repair(broken_unterminated, "test.py"), broken_unterminated)

        # Non-Python file extension is a no-op.
        unrelated = "print（'hi'）"
        self.assertEqual(repair(unrelated, "test.txt"), unrelated)

        # Empty input is a no-op.
        self.assertEqual(repair("", "test.py"), "")

    def test_unescape_llm_code_content_handles_double_and_triple_escape(self) -> None:
        """When a reasoning-class LLM applies the JSON escape pass twice (or
        even three times) under STRICT_JSON, the resulting content has
        ``\\\\n`` / ``\\\\\\"`` sequences that need *multiple* reduction
        passes — one pass leaves ``\\n`` / ``\\"`` artifacts that still
        break ``compile()``.  The iterative unescaper must reduce all
        levels until either the source compiles cleanly or a fixed point
        is reached.
        """
        from crucible.module_runtime import get_runtime

        runtime = get_runtime()
        unescape = runtime._unescape_llm_code_content

        # Single-escaped: \n is 2 chars (backslash + n).  This was already
        # handled by the legacy single-pass implementation; included as a
        # regression guard.
        single = 'def f():\\n    return print(\\"hi\\")\\n'
        out_single = unescape(single)
        self.assertNotIn("\\n", out_single)
        self.assertNotIn('\\"', out_single)
        compile(out_single, "t.py", "exec")

        # Double-escaped: \n is 3 chars (\\\\n in source = two backslashes
        # then n).  Pre-fix, one pass left ``\n`` / ``\"`` artifacts and
        # compile() failed with "unexpected character after line
        # continuation character".
        double = 'def f():\\\\n    return print(\\\\"hi\\\\")\\\\n'
        out_double = unescape(double)
        self.assertNotIn("\\n", out_double)
        self.assertNotIn('\\"', out_double)
        self.assertNotIn("\\\\", out_double)
        compile(out_double, "t.py", "exec")

        # Triple-escaped: defensive — escape pass run three times.
        triple = "def f():\\\\\\\\n    return 1\\\\\\\\n"
        out_triple = unescape(triple)
        self.assertNotIn("\\n", out_triple)
        compile(out_triple, "t.py", "exec")

        # Real log-extracted pattern: double-escaped triple-quoted
        # docstring with embedded CJK.  This is exactly what the
        # production log showed at lines 25767+ in the user's run.
        log_pattern = (
            'def fn():\\\\n    \\\\\\"\\\\\\"\\\\\\"格式化指標值。'
            '\\\\\\"\\\\\\"\\\\\\"\\\\n    return 1\\\\n'
        )
        out_log = unescape(log_pattern)
        compile(out_log, "t.py", "exec")
        # CJK content inside the docstring is preserved.
        self.assertIn("格式化指標值。", out_log)
        # And the triple-quote delimiters are now real triple-quotes.
        self.assertIn('"""', out_log)

    def test_unescape_llm_code_content_preserves_already_clean_source(self) -> None:
        """Already-valid Python source must round-trip unchanged — the
        unescaper only applies reductions when ``compile()`` already fails.
        Falsely "unescaping" a clean source would corrupt regex patterns,
        Windows paths, and other backslash-bearing literals.
        """
        from crucible.module_runtime import get_runtime

        runtime = get_runtime()
        unescape = runtime._unescape_llm_code_content

        clean_sources = [
            "def f():\n    return 1\n",
            "import re\npat = re.compile(r'\\d+')\n",
            'msg = "他說：\\"hello\\""\n',  # Already-valid Python
            "# Just a comment, no code\n",
            "",  # Empty
        ]
        for src in clean_sources:
            with self.subTest(src=src[:40]):
                self.assertEqual(unescape(src), src)

    def test_unescape_llm_code_content_iteration_cap_does_not_hang(self) -> None:
        """Defensive: pathological inputs (no progress on each pass) must
        terminate via the fixed-point check rather than burning iterations
        up to the cap.  Verified by passing input where no escape sequence
        is present and confirming the function returns immediately.
        """
        from crucible.module_runtime import get_runtime

        runtime = get_runtime()
        unescape = runtime._unescape_llm_code_content

        # No backslash → fast path, returns immediately.
        no_escape = "def f():\n    return 1\n" * 100
        out = unescape(no_escape)
        self.assertEqual(out, no_escape)

        # Backslash present but no recognised escape pattern (e.g. Windows
        # path-like content already failing on \U) — must converge to a
        # fixed point and return without exhausting all passes.
        path_like = 'p = "C:\\Users\\foo"'  # broken Python (unicode escape)
        out2 = unescape(path_like)
        # Either returns unchanged (no reduction possible) or some
        # reduction; in both cases must not loop forever.
        self.assertIsInstance(out2, str)

    def test_sanitize_code_bundle_applies_cjk_repair_end_to_end(self) -> None:
        """The full ``_sanitize_code_bundle`` pipeline must apply CJK repair
        so downstream syntax-validation gates (``_validate_batch_bundle``,
        ``_py_syntax_error_paths_in_bundle``, ``_identify_codegen_repair_targets``)
        all see compile-able Python.  This is the integration that prevents
        the in-batch syntax-repair supplement / cross-batch repair loop /
        AutoOptimize round from looping forever on CJK-punctuation errors.
        """
        from crucible.module_runtime import get_runtime

        runtime = get_runtime()
        bundle = runtime.CodeBundle(
            project_type="quant",
            files=[
                runtime.GeneratedFile(
                    path="strategy.py",
                    content=(
                        "def signal(x):\n"
                        "    \"\"\"判斷訊號。\"\"\"\n"
                        "    return foo（x，2）\n"
                    ),
                ),
            ],
        )
        sanitized = runtime._sanitize_code_bundle(bundle)
        self.assertIsNotNone(sanitized)
        self.assertEqual(len(sanitized.files), 1)
        repaired_content = sanitized.files[0].content
        # CJK punct in the docstring is preserved.
        self.assertIn("判斷訊號。", repaired_content)
        # CJK punct in code context has been converted to ASCII.
        self.assertIn("foo(x,2)", repaired_content)
        self.assertNotIn("foo（", repaired_content)
        # And the repaired file compiles.
        compile(repaired_content, "strategy.py", "exec")

    def test_save_project_output_preserves_gate_fallback_details(self) -> None:
        from crucible.module_runtime import get_runtime

        runtime = get_runtime()
        gate = runtime.GateDecision(
            consensus="黃金短線策略需先完成資料遷移驗證後再進入實盤開發。",
            disagreement="冷啟動代理資料與實盤資料的一致性仍不足。",
            experiments=[
                runtime.Experiment(
                    goal="驗證代理資料遷移有效性",
                    criteria="代理資料與實盤資料相關係數 > 0.5",
                )
            ],
            ready_for_codegen=False,
            blocking_risks=["代理資料遷移有效性未驗證"],
            required_experiments_before_codegen=["完成 72 小時測試網壓測"],
            advisory_experiments_after_codegen=["補充手續費敏感度分析"],
            overall_score=28,
            confidence="low",
        )
        project_dir = runtime.save_project_output(
            None,
            None,
            None,
            run_meta={"mode": "Quant", "input_mode": "idea"},
            gate_decision=gate,
            language_hint="Traditional Chinese",
        )
        project_path = Path(project_dir)
        try:
            self.assertNotEqual(project_path.name.split("_", 2)[-1], "project")

            analysis_payload = json.loads(
                (project_path / "analysis_result.json").read_text(encoding="utf-8")
            )
            self.assertFalse(analysis_payload["analysis_report_available"])
            self.assertTrue(analysis_payload["derived_from_gate_decision"])
            self.assertEqual(
                analysis_payload["blocking_risks"], ["代理資料遷移有效性未驗證"]
            )
            self.assertIn("黃金短線策略", analysis_payload["project_name"])

            readme_text = (project_path / "README.md").read_text(encoding="utf-8")
            self.assertIn("Gate 決策", readme_text)
            self.assertIn("代理資料遷移有效性未驗證", readme_text)
            self.assertNotIn("未產生分析報告；僅輸出程式碼", readme_text)
        finally:
            shutil.rmtree(project_path, ignore_errors=True)

    def test_validation_first_gate_promotion_allows_low_confidence_codegen(self) -> None:
        from crucible.module_runtime import get_runtime

        runtime = get_runtime()
        gate = runtime.GateDecision(
            consensus="需要先驗證 mid-price 語義與門檻校準。",
            disagreement="目前缺少 mid-price 語義驗證、門檻校準與資料對齊證據。",
            experiments=[
                runtime.Experiment(
                    goal="驗證 Binance/Bybit mid-price 語義一致性",
                    criteria="輸出語義對照與差異報告",
                )
            ],
            ready_for_codegen=False,
            blocking_risks=["缺少 mid-price 語義驗證"],
            required_experiments_before_codegen=["完成門檻校準報告"],
            advisory_experiments_after_codegen=[],
            overall_score=42,
            confidence="low",
            direction_feedback_needed=True,
            direction_feedback_type="evidence",
            direction_feedback_reason="這不是 production blocker，而是 Phase0 驗證框架要量測的缺口",
            direction_feedback_evidence_gaps=["缺少 timestamp alignment 驗證"],
            direction_feedback_questions=["如何量測 mid-price 語義漂移？"],
        )

        promoted = runtime._promote_validation_first_gate(
            gate,
            user_problem="請先做 phase0 validation framework，驗證 mid-price semantic 與 threshold calibration，再決定後續 production implementation。",
            mode="Quant",
        )

        self.assertIsNotNone(promoted)
        assert promoted is not None
        self.assertTrue(promoted.ready_for_codegen)
        self.assertEqual(promoted.codegen_scope, "validation")
        self.assertFalse(promoted.blocking_risks)
        self.assertFalse(promoted.required_experiments_before_codegen)
        self.assertFalse(promoted.direction_feedback_needed)
        self.assertEqual(promoted.failure_type, runtime.FailureType.NONE.value)
        self.assertIn("mid-price", " | ".join(promoted.validation_objectives).lower())
        failure_type, failure_details = runtime._classify_gate_failure(promoted)
        self.assertEqual(failure_type, runtime.FailureType.NONE)
        self.assertEqual(failure_details, "")

    def test_save_project_output_persists_validation_first_gate_scope_details(self) -> None:
        from crucible.module_runtime import get_runtime

        runtime = get_runtime()
        gate = runtime.GateDecision(
            consensus="先交付 validation harness",
            disagreement="production strategy 仍需後續驗證",
            experiments=[
                runtime.Experiment(
                    goal="驗證 mid-price semantic consistency",
                    criteria="輸出差異報告",
                )
            ],
            ready_for_codegen=True,
            blocking_risks=[],
            required_experiments_before_codegen=[],
            advisory_experiments_after_codegen=["補做 paper trading"],
            codegen_scope="validation",
            validation_scope_reason="缺口正是 validation harness 要量測的內容。",
            validation_objectives=[
                "驗證 mid-price semantic consistency",
                "量測 timestamp alignment drift",
            ],
            overall_score=58,
            confidence="low",
        )
        project_dir = runtime.save_project_output(
            None,
            None,
            None,
            run_meta={"mode": "Quant", "input_mode": "idea"},
            gate_decision=gate,
            language_hint="Traditional Chinese",
        )
        project_path = Path(project_dir)
        try:
            analysis_payload = json.loads(
                (project_path / "analysis_result.json").read_text(encoding="utf-8")
            )
            self.assertEqual(analysis_payload["codegen_scope"], "validation")
            self.assertEqual(
                analysis_payload["validation_scope_reason"],
                "缺口正是 validation harness 要量測的內容。",
            )
            self.assertEqual(
                analysis_payload["validation_objectives"],
                ["驗證 mid-price semantic consistency", "量測 timestamp alignment drift"],
            )

            readme_text = (project_path / "README.md").read_text(encoding="utf-8")
            self.assertIn("CodeGen 範圍: validation", readme_text)
            self.assertIn("Validation 範圍原因", readme_text)
            self.assertIn("缺口正是 validation harness 要量測的內容。", readme_text)
            self.assertIn("Validation 目標", readme_text)
            self.assertIn("量測 timestamp alignment drift", readme_text)
        finally:
            shutil.rmtree(project_path, ignore_errors=True)

    def test_save_project_output_with_analysis_report_keeps_validation_first_top_level_fields(
        self,
    ) -> None:
        from crucible.module_runtime import get_runtime

        runtime = get_runtime()
        result = runtime.AnalysisReport(
            project_name="phase0_validation_framework",
            summary="先交付 validation harness",
            consensus="建立 measurement pipeline",
            disagreement="production implementation 稍後再決定",
            experiments=[runtime.Experiment(goal="量測 drift", criteria="輸出報告")],
            score=62,
            mode_used="Quant",
            risk_level="Medium",
            gate_context_snapshot={
                "ready_for_codegen": True,
                "codegen_scope": "validation",
                "validation_scope_reason": "正常保存路徑也必須保留 validation-first gate 欄位。",
                "validation_objectives": [
                    "量測 semantic drift",
                    "驗證 timestamp alignment",
                ],
                "blocking_risks": [],
                "required_experiments_before_codegen": [],
                "advisory_experiments_after_codegen": ["補做實盤驗證"],
                "confidence": "low",
            },
            codegen_handoff_summary="Validation-first scope only.",
        )
        gate = runtime.GateDecision(
            consensus="允許 validation-first codegen",
            disagreement="production 還未放行",
            experiments=[],
            ready_for_codegen=True,
            blocking_risks=[],
            required_experiments_before_codegen=[],
            advisory_experiments_after_codegen=["補做實盤驗證"],
            codegen_scope="validation",
            validation_scope_reason="正常保存路徑也必須保留 validation-first gate 欄位。",
            validation_objectives=["量測 semantic drift", "驗證 timestamp alignment"],
            overall_score=62,
            confidence="low",
        )
        project_dir = runtime.save_project_output(
            result,
            None,
            None,
            run_meta={"mode": "Quant", "input_mode": "idea"},
            gate_decision=gate,
            language_hint="Traditional Chinese",
        )
        project_path = Path(project_dir)
        try:
            analysis_payload = json.loads(
                (project_path / "analysis_result.json").read_text(encoding="utf-8")
            )
            self.assertTrue(analysis_payload["analysis_report_available"])
            self.assertEqual(analysis_payload["codegen_scope"], "validation")
            self.assertEqual(
                analysis_payload["validation_scope_reason"],
                "正常保存路徑也必須保留 validation-first gate 欄位。",
            )
            self.assertEqual(
                analysis_payload["validation_objectives"],
                ["量測 semantic drift", "驗證 timestamp alignment"],
            )

            readme_text = (project_path / "README.md").read_text(encoding="utf-8")
            self.assertIn("CodeGen 範圍: validation", readme_text)
            self.assertIn("正常保存路徑也必須保留 validation-first gate 欄位。", readme_text)
            self.assertIn("驗證 timestamp alignment", readme_text)
        finally:
            shutil.rmtree(project_path, ignore_errors=True)

    def test_save_project_output_recovers_mode_from_code_bundle_when_run_meta_mode_missing(
        self,
    ) -> None:
        from crucible.module_runtime import get_runtime

        runtime = get_runtime()
        bundle = runtime.CodeBundle(
            project_type="agent",
            files=[runtime.GeneratedFile(path="worker.py", content="print('ok')\n")],
        )
        project_dir = runtime.save_project_output(
            None,
            code=bundle,
            run_meta={"input_mode": "idea"},
            language_hint="English",
        )
        project_path = Path(project_dir)
        try:
            analysis_payload = json.loads(
                (project_path / "analysis_result.json").read_text(encoding="utf-8")
            )
            self.assertEqual(analysis_payload["mode_used"], "Agent")
            self.assertFalse(analysis_payload["analysis_report_available"])

            run_meta_payload = json.loads(
                (project_path / "run_meta.json").read_text(encoding="utf-8")
            )
            self.assertEqual(run_meta_payload["mode"], "Agent")

            readme_text = (project_path / "README.md").read_text(encoding="utf-8")
            self.assertIn("- Mode: Agent", readme_text)
        finally:
            shutil.rmtree(project_path, ignore_errors=True)

    def test_save_project_output_overrides_conflicting_run_meta_mode_from_code_bundle(self) -> None:
        from crucible.module_runtime import get_runtime

        runtime = get_runtime()
        bundle = runtime.CodeBundle(
            project_type="agent",
            files=[runtime.GeneratedFile(path="worker.py", content="print('ok')\n")],
        )
        project_dir = runtime.save_project_output(
            None,
            code=bundle,
            run_meta={"mode": "SaaS", "input_mode": "idea"},
            language_hint="English",
        )
        project_path = Path(project_dir)
        try:
            analysis_payload = json.loads(
                (project_path / "analysis_result.json").read_text(encoding="utf-8")
            )
            self.assertEqual(analysis_payload["mode_used"], "Agent")
            self.assertFalse(analysis_payload["analysis_report_available"])

            run_meta_payload = json.loads(
                (project_path / "run_meta.json").read_text(encoding="utf-8")
            )
            self.assertEqual(run_meta_payload["mode"], "Agent")

            readme_text = (project_path / "README.md").read_text(encoding="utf-8")
            self.assertIn("- Mode: Agent", readme_text)
        finally:
            shutil.rmtree(project_path, ignore_errors=True)

    def test_save_project_output_rejects_conflicting_analysis_report_mode(self) -> None:
        from crucible.module_runtime import get_runtime

        runtime = get_runtime()
        result = runtime.AnalysisReport(
            project_name="wrong_mode_report",
            summary="summary",
            consensus="consensus",
            disagreement="disagreement",
            experiments=[],
            score=75,
            mode_used="SaaS",
            risk_level="Medium",
        )
        bundle = runtime.CodeBundle(
            project_type="agent",
            files=[runtime.GeneratedFile(path="worker.py", content="print('ok')\n")],
        )
        project_dir = runtime.save_project_output(
            result,
            code=bundle,
            run_meta={"mode": "Agent", "input_mode": "idea"},
            language_hint="English",
        )
        project_path = Path(project_dir)
        try:
            analysis_payload = json.loads(
                (project_path / "analysis_result.json").read_text(encoding="utf-8")
            )
            self.assertFalse(analysis_payload["analysis_report_available"])
            self.assertEqual(analysis_payload["mode_used"], "Agent")

            run_meta_payload = json.loads(
                (project_path / "run_meta.json").read_text(encoding="utf-8")
            )
            self.assertEqual(run_meta_payload["mode"], "Agent")
        finally:
            shutil.rmtree(project_path, ignore_errors=True)

    def test_save_project_output_rejects_conflicting_analysis_report_mode_even_without_run_meta_mode(
        self,
    ) -> None:
        from crucible.module_runtime import get_runtime

        runtime = get_runtime()
        result = runtime.AnalysisReport(
            project_name="wrong_mode_report_missing_meta",
            summary="summary",
            consensus="consensus",
            disagreement="disagreement",
            experiments=[],
            score=75,
            mode_used="SaaS",
            risk_level="Medium",
        )
        bundle = runtime.CodeBundle(
            project_type="agent",
            files=[runtime.GeneratedFile(path="worker.py", content="print('ok')\n")],
        )
        project_dir = runtime.save_project_output(
            result,
            code=bundle,
            run_meta={"input_mode": "idea"},
            language_hint="English",
        )
        project_path = Path(project_dir)
        try:
            analysis_payload = json.loads(
                (project_path / "analysis_result.json").read_text(encoding="utf-8")
            )
            self.assertFalse(analysis_payload["analysis_report_available"])
            self.assertEqual(analysis_payload["mode_used"], "Agent")

            run_meta_payload = json.loads(
                (project_path / "run_meta.json").read_text(encoding="utf-8")
            )
            self.assertEqual(run_meta_payload["mode"], "Agent")
        finally:
            shutil.rmtree(project_path, ignore_errors=True)


class TestStagedCodegenTokenAccounting(unittest.TestCase):
    """
    Regression tests for _staged_codegen_prompt_chars accounting in
    _run_staged_codegen_pipeline.  The attribute is used as a fallback
    token count when usage records are unavailable; incorrect values lead
    to over-reported API costs in cost_trace logs.
    """

    SOURCE = open(
        os.path.join(str(ROOT), "crucible", "modules",
                     "section_05_analysis_and_codegen.py"),
        encoding="utf-8",
    ).read()

    def _extract_function_source(self, func_name: str) -> str:
        import ast
        tree = ast.parse(self.SOURCE)
        lines = self.SOURCE.splitlines(keepends=True)
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if node.name == func_name:
                    end = getattr(node, "end_lineno", node.lineno + 200)
                    return "".join(lines[node.lineno - 1: end])
        return ""

    def test_finalize_failure_sets_zero_additional_prompt_chars(self) -> None:
        """
        _finalize_staged_codegen_bundle makes no LLM calls and consumes 0
        additional prompt chars beyond what pipeline_prompt_chars already
        tracks.  When it fails, the exception must carry
        _staged_codegen_prompt_chars=0 so the outer except handler computes
        the correct total:

            pipeline_prompt_chars + 0 = pipeline_prompt_chars   ← correct

        Previously the code set _staged_codegen_prompt_chars=pipeline_prompt_chars,
        causing the outer handler to double-count:

            pipeline_prompt_chars + pipeline_prompt_chars        ← BUG

        This test pins the fix in place by verifying the source code of
        _run_staged_codegen_pipeline uses 0 (not pipeline_prompt_chars) as
        the _staged_codegen_prompt_chars value in the finalize-failure block.
        """
        body = self._extract_function_source("_run_staged_codegen_pipeline")
        self.assertTrue(body, "Could not find _run_staged_codegen_pipeline in source")

        lines = body.splitlines()

        # Find the line that calls _finalize_staged_codegen_bundle and assigns
        # the result (there is exactly one such line in the function).
        finalize_idx = next(
            (i for i, ln in enumerate(lines)
             if "_finalize_staged_codegen_bundle" in ln and "final_bundle" in ln),
            None,
        )
        self.assertIsNotNone(
            finalize_idx,
            "_run_staged_codegen_pipeline must contain a "
            "'final_bundle, ... = _finalize_staged_codegen_bundle(...)' call.",
        )

        # The setattr carrying _staged_codegen_prompt_chars must appear within
        # the finalize-failure block.  We allow a generous window so that
        # additional recovery / salvage logic between the finalize call and
        # the raise (e.g. lenient-output salvage paths) does not break the
        # test as long as the underlying invariant — the setattr value must
        # be 0, not pipeline_prompt_chars — is preserved.  The block ends at
        # the next top-level `except` clause of the outer try, which is
        # marked by lines starting with "except".
        end_idx = next(
            (
                i
                for i, ln in enumerate(lines[finalize_idx + 1:], start=finalize_idx + 1)
                if ln.strip().startswith("except ")
            ),
            len(lines),
        )
        finalize_block = "\n".join(lines[finalize_idx:end_idx])

        self.assertIn(
            '_staged_codegen_prompt_chars", 0)',
            finalize_block,
            "_run_staged_codegen_pipeline finalize-failure block must set "
            "_staged_codegen_prompt_chars=0 (not pipeline_prompt_chars). "
            "Setting it to pipeline_prompt_chars causes the outer except "
            "handler to double-count the accumulated batch chars.",
        )

        # Confirm the old (buggy) form is absent in that block.
        self.assertNotIn(
            "_staged_codegen_prompt_chars\", pipeline_prompt_chars)",
            finalize_block,
            "_run_staged_codegen_pipeline finalize-failure block must NOT set "
            "_staged_codegen_prompt_chars=pipeline_prompt_chars — that causes "
            "the outer except handler to double-count prompt chars.",
        )


# ─── load_analysis_report_safe / schema_version ──────────────────────────────


class TestLoadAnalysisReportSafe(unittest.TestCase):
    """Tests for load_analysis_report_safe() and schema_version in AnalysisReport."""

    # Minimal valid payload that satisfies all required AnalysisReport fields.
    _VALID_PAYLOAD: dict = {
        "project_name": "TestProject",
        "summary": "A summary",
        "consensus": "Strong consensus",
        "disagreement": "Minor disagreement",
        "experiments": [{"goal": "Test goal", "criteria": "Pass/fail"}],
        "score": 75,
        "mode_used": "Quant",
        "risk_level": "Medium",
    }

    def _write_json(self, data: dict, path: str) -> None:
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(data, fh)

    def setUp(self) -> None:
        from crucible.module_runtime import get_runtime
        self.runtime = get_runtime()

    def test_missing_file_returns_none(self) -> None:
        result = self.runtime.load_analysis_report_safe("/nonexistent/path/analysis.json")
        self.assertIsNone(result)

    def test_valid_file_returns_report(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "analysis.json")
            self._write_json(self._VALID_PAYLOAD, path)
            result = self.runtime.load_analysis_report_safe(path)
            self.assertIsNotNone(result)
            self.assertEqual(result.project_name, "TestProject")
            self.assertEqual(result.score, 75)

    def test_malformed_json_returns_none(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "analysis.json")
            with open(path, "w") as fh:
                fh.write("{not valid json}")
            import warnings
            with warnings.catch_warnings(record=True):
                warnings.simplefilter("always")
                result = self.runtime.load_analysis_report_safe(path)
            self.assertIsNone(result)

    def test_non_dict_json_returns_none(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "analysis.json")
            self._write_json([1, 2, 3], path)
            result = self.runtime.load_analysis_report_safe(path)
            self.assertIsNone(result)

    def test_schema_version_defaults_to_1_when_absent(self) -> None:
        """Old files without schema_version should silently default to 1."""
        payload = dict(self._VALID_PAYLOAD)
        payload.pop("schema_version", None)  # ensure it's absent
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "analysis.json")
            self._write_json(payload, path)
            result = self.runtime.load_analysis_report_safe(path)
            self.assertIsNotNone(result)
            self.assertEqual(result.schema_version, 1)

    def test_schema_version_present_preserved(self) -> None:
        payload = dict(self._VALID_PAYLOAD)
        payload["schema_version"] = 3
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "analysis.json")
            self._write_json(payload, path)
            result = self.runtime.load_analysis_report_safe(path)
            self.assertIsNotNone(result)
            self.assertEqual(result.schema_version, 3)

    def test_missing_optional_fields_filled_with_defaults(self) -> None:
        """Files written before optional fields existed should load without error.

        All six compat-default fields must be restored to their correct default
        values when absent from the JSON, matching the _COMPAT_DEFAULTS table in
        load_analysis_report_safe.
        """
        payload = dict(self._VALID_PAYLOAD)
        # Remove all six optional fields that have compat defaults
        for key in (
            "analyst_findings", "gate_context_snapshot",
            "codegen_handoff_summary", "codegen_requirements",
            "codegen_constraints", "codegen_validation_focus",
        ):
            payload.pop(key, None)
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "analysis.json")
            self._write_json(payload, path)
            result = self.runtime.load_analysis_report_safe(path)
            self.assertIsNotNone(result)
            # Verify ALL six compat-default fields are restored correctly.
            # Previously only 2 were checked, leaving 4 fields without assertion.
            self.assertEqual(result.analyst_findings, {})
            self.assertEqual(result.gate_context_snapshot, {})
            self.assertEqual(result.codegen_handoff_summary, "")
            self.assertEqual(result.codegen_requirements, [])
            self.assertEqual(result.codegen_constraints, [])
            self.assertEqual(result.codegen_validation_focus, [])

    def test_unknown_future_fields_stripped_gracefully(self) -> None:
        """Extra fields from future schema versions must not cause a crash or return None.

        AnalysisReport has no model_config / class Config, so both Pydantic v1
        and v2 default to ignoring (stripping) unknown fields.  The loader must
        therefore succeed and return a fully populated report — not None.
        """
        payload = dict(self._VALID_PAYLOAD)
        payload["future_field_xyz"] = "some value"
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "analysis.json")
            self._write_json(payload, path)
            import warnings
            with warnings.catch_warnings(record=True):
                warnings.simplefilter("always")
                result = self.runtime.load_analysis_report_safe(path)
            # Pydantic v1 and v2 both ignore extra fields by default.
            # The load must succeed and return a valid AnalysisReport.
            self.assertIsNotNone(result)
            self.assertEqual(result.project_name, "TestProject")

    def test_missing_required_field_returns_none(self) -> None:
        """A file missing a required field (no default) must return None, not raise."""
        payload = dict(self._VALID_PAYLOAD)
        del payload["project_name"]
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "analysis.json")
            self._write_json(payload, path)
            import warnings
            with warnings.catch_warnings(record=True):
                warnings.simplefilter("always")
                result = self.runtime.load_analysis_report_safe(path)
            self.assertIsNone(result)


if __name__ == "__main__":
    unittest.main()
