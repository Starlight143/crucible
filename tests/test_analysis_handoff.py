# ruff: noqa: E402
import os
import sys
import unittest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from crucible.module_runtime import get_runtime
from crucible.modules import (
    section_04_web_research_and_direction as m04,
    section_06_runtime_quality_api as m06,
)

qsc = get_runtime()


class TestAnalysisHandoff(unittest.TestCase):
    def test_canonical_analysis_specs_use_compactor_and_prompt_budgets(self) -> None:
        from crucible.web_research.analysis_specs import build_analysis_specs

        mode_config = m04._get_mode_config("Quant")
        active_roles = set(m04.ANALYST_AGENT_ORDER)
        _, task_specs, _ = build_analysis_specs(
            mode_config=mode_config,
            active_roles=active_roles,
            direction_feedback_enabled=True,
            deps={
                "mode_gate_controller_guidance": m04._mode_gate_controller_guidance,
                "AgentSpec": m04.AgentSpec,
                "TaskSpec": m04.TaskSpec,
                "RetryPolicy": m04.RetryPolicy,
                "ANALYST_AGENT_ORDER": m04.ANALYST_AGENT_ORDER,
                "NO_CROSS_ROLE_RULE": m04.NO_CROSS_ROLE_RULE,
                "COMMON_OUTPUT_RULES": m04.COMMON_OUTPUT_RULES,
                "GATE_CONTROLLER_RULES": m04.GATE_CONTROLLER_RULES,
                "GATE_CONTEXT_COMPACTOR_RULES": m04.GATE_CONTEXT_COMPACTOR_RULES,
            },
        )

        by_name = {spec.name: spec for spec in task_specs}
        self.assertIn("gate_context_compactor", by_name)
        self.assertEqual(by_name["gate_controller"].context_task_names, ["gate_context_compactor"])
        self.assertEqual(
            by_name["format_checker"].context_task_names,
            ["gate_context_compactor", "gate_controller"],
        )
        self.assertIsNotNone(by_name["research"].max_input_chars)
        self.assertIsNotNone(by_name["gate_context_compactor"].max_input_chars)
        self.assertIsNotNone(by_name["gate_controller"].max_input_chars)
        self.assertIsNotNone(by_name["format_checker"].max_input_chars)

    def test_gate_handoff_compacts_full_analyst_context_before_gate_controller(self) -> None:
        mode_config = m04._get_mode_config("Quant")
        active_roles = set(m04.ANALYST_AGENT_ORDER)
        _, task_specs, _ = m04._build_analysis_specs(
            mode_config=mode_config,
            active_roles=active_roles,
            direction_feedback_enabled=True,
        )

        compact_task = next(spec for spec in task_specs if spec.name == "gate_context_compactor")
        format_task = next(spec for spec in task_specs if spec.name == "format_checker")
        gate_task = next(spec for spec in task_specs if spec.name == "gate_controller")

        self.assertEqual(compact_task.context_task_names, list(m04.ANALYST_AGENT_ORDER))
        self.assertEqual(gate_task.context_task_names, ["gate_context_compactor"])
        self.assertEqual(
            format_task.context_task_names,
            ["gate_context_compactor", "gate_controller"],
        )
        self.assertEqual(compact_task.output_pydantic_model, "GateContextBundle")
        self.assertIn("implementation_requirements", compact_task.description_template)
        self.assertIn("implementation_constraints", compact_task.description_template)
        self.assertIn("validation_focus", compact_task.description_template)
        self.assertIn("GateContextBundle", gate_task.description_template)
        self.assertIn("analyst_findings", format_task.description_template)
        self.assertIn("gate_context_snapshot", format_task.description_template)
        self.assertIn("codegen_handoff_summary", format_task.description_template)

    def test_codegen_context_prefers_format_checker_handoff_but_keeps_gate_detail(self) -> None:
        analysis_report = qsc.AnalysisReport(
            project_name="cross_exchange_stat_arb",
            summary="方向已整理完成",
            consensus="先交付純 Python 策略與回測流程",
            disagreement="實盤參數仍需後續驗證",
            experiments=[
                qsc.Experiment(goal="回放驗證", criteria="回測與輸出一致"),
            ],
            score=78,
            mode_used="Quant",
            risk_level="Medium",
            analyst_findings={
                "research": "需要保留交易成本、滑價與資料來源細節。",
                "ops": "執行流程必須先抓資料、算訊號、再輸出結果。",
            },
            gate_context_snapshot={
                "ready_for_codegen": True,
                "blocking_risks": ["不可引入 web framework"],
                "required_experiments_before_codegen": [],
            },
            codegen_handoff_summary="依照整理後規格實作純 Python 模組，不要偏離 Quant mode。",
            codegen_requirements=[
                "包含 strategy.py、backtest.py、trade.py、export.py 等等價模組責任",
                "保留交易成本與滑價假設",
            ],
            codegen_constraints=[
                "不可引入 FastAPI 或其他 web framework",
                "不可省略輸出模組",
            ],
            codegen_validation_focus=[
                "策略訊號與回測結果必須可重現",
                "輸出檔案需包含可檢查欄位",
            ],
        )
        gate = qsc.GateDecision(
            consensus="可以進入 CodeGen",
            disagreement="實盤驗證留到下一階段",
            experiments=[qsc.Experiment(goal="實盤模擬", criteria="一週觀察")],
            ready_for_codegen=True,
            blocking_risks=["不可改成 web app"],
            required_experiments_before_codegen=[],
            advisory_experiments_after_codegen=["補做實盤模擬"],
            agents_needing_rerun=[],
            rerun_reasons={},
            overall_score=78,
            confidence="medium",
        )

        context = m06.build_conditional_codegen_context(gate, analysis_report)

        self.assertIn("=== APPROVED ANALYSIS HANDOFF ===", context)
        self.assertIn("Required implementation details:", context)
        self.assertIn("Implementation constraints:", context)
        self.assertIn("Validation focus:", context)
        self.assertIn("Analyst implementation notes:", context)
        self.assertIn("=== LIVE GATE CONTROLLER APPROVAL ===", context)
        self.assertIn("不可引入 FastAPI 或其他 web framework", context)
        self.assertIn("不可改成 web app", context)
        self.assertIn("依照整理後規格實作純 Python 模組", context)

    def test_codegen_context_caps_large_handoff_sections(self) -> None:
        oversized = "A" * 12000
        analysis_report = qsc.AnalysisReport(
            project_name="oversized_handoff",
            summary=oversized,
            consensus=oversized,
            disagreement=oversized,
            experiments=[qsc.Experiment(goal=oversized, criteria=oversized)],
            score=70,
            mode_used="Quant",
            risk_level="Medium",
            analyst_findings={
                "research": oversized,
                "ops": oversized,
            },
            gate_context_snapshot={"large": oversized},
            codegen_handoff_summary=oversized,
            codegen_requirements=[oversized, oversized],
            codegen_constraints=[oversized],
            codegen_validation_focus=[oversized],
        )

        context = m06.build_conditional_codegen_context(None, analysis_report)

        self.assertIn("...[truncated]...", context)
        self.assertLess(len(context), 22000)

    def test_codegen_context_includes_validation_first_approval_section(self) -> None:
        analysis_report = qsc.AnalysisReport(
            project_name="mid_price_semantic_validation",
            summary="先做 validation harness",
            consensus="先交付 measurement pipeline",
            disagreement="production 門檻仍未驗證",
            experiments=[qsc.Experiment(goal="語義驗證", criteria="輸出差異報告")],
            score=61,
            mode_used="Quant",
            risk_level="Medium",
            gate_context_snapshot={},
            codegen_handoff_summary="Validation-first scope only.",
            codegen_requirements=["建立語義對照表"],
            codegen_constraints=["不得直接輸出 production strategy"],
            codegen_validation_focus=["量測 timestamp alignment"],
        )
        gate = qsc.GateDecision(
            consensus="允許進入 validation-first codegen",
            disagreement="production 邏輯仍待驗證",
            experiments=[qsc.Experiment(goal="門檻校準", criteria="輸出 calibration report")],
            ready_for_codegen=True,
            blocking_risks=[],
            required_experiments_before_codegen=[],
            advisory_experiments_after_codegen=["補做實盤驗證"],
            codegen_scope="validation",
            validation_scope_reason="缺口正是 validation harness 要直接量測的內容。",
            validation_objectives=[
                "驗證 mid-price semantic consistency",
                "量測 timestamp alignment drift",
            ],
            agents_needing_rerun=[],
            rerun_reasons={},
            overall_score=61,
            confidence="low",
        )

        context = m06.build_conditional_codegen_context(gate, analysis_report)

        self.assertIn("=== VALIDATION-FIRST CODEGEN APPROVAL ===", context)
        self.assertIn("Approved scope: validation", context)
        self.assertIn("Validation objectives:", context)
        self.assertIn("驗證 mid-price semantic consistency", context)
        self.assertIn("Guardrails:", context)
        self.assertIn("Only generate validation/calibration/measurement scaffolding", context)


if __name__ == "__main__":
    unittest.main()
