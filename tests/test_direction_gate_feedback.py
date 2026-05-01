# ruff: noqa: E402
import json
import os
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import patch

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from crucible.module_runtime import get_runtime
from crucible.modules import (
    section_02_research_and_llm as m02,
    section_04_web_research_and_direction as m04,
    section_05_analysis_and_codegen as m05,
    section_07_selfcheck_output_main as m07,
)

qsc = get_runtime()


class _FakeCrew:
    def __init__(self, task_names, result):
        self._task_names = list(task_names)
        self._result = result
        self._prompt_hashes = {}
        self._dag_snapshot = {}
        self.tasks = [SimpleNamespace(name=name) for name in task_names]

    def kickoff(self):
        return self._result


class _FakeResult:
    def __init__(self, task_texts):
        self.tasks_output = [
            SimpleNamespace(raw=text, text=text) for text in list(task_texts or [])
        ]
        self.raw = "\n".join(task_texts or [])


class TestDirectionGateFeedback(unittest.TestCase):
    def test_direction_salvage_recovers_options_from_explorer_array_root(self) -> None:
        options_payload = [
            {
                "key": key,
                "name": f"Option {key}",
                "thesis": f"Thesis {key}",
                "primary_metric": f"Metric {key}",
                "fastest_test": f"Test {key}",
                "major_risk": f"Risk {key}",
            }
            for key in ("A", "B", "C", "D", "E", "F", "G")
        ]
        judge_partial = {
            "selected_direction": "B",
            "summary": "Direction B remains strongest after review.",
            "backup_candidates": ["A", "C"],
            "go_conditions": ["Validate the selected venue first."],
            "kill_criteria": ["Stop if live slippage exceeds the budget."],
            "confidence": "medium",
            "verify_plan": ["Replay the strategy on fresh data."],
        }
        result = _FakeResult(
            [
                json.dumps(options_payload, ensure_ascii=False),
                json.dumps(judge_partial, ensure_ascii=False),
            ]
        )

        decision = qsc._salvage_direction_decision_from_result(result)

        self.assertIsNotNone(decision)
        self.assertEqual(decision.selected_direction, "B")
        self.assertEqual([option.key for option in decision.options], ["A", "B", "C", "D", "E", "F", "G"])
        self.assertEqual(decision.backup_candidates, ["A", "C"])

    def test_provisional_direction_fallback_recovers_options_from_explorer_array_root(self) -> None:
        options_payload = [
            {
                "key": key,
                "name": f"Option {key}",
                "thesis": f"Thesis {key}",
                "primary_metric": f"Metric {key}",
                "fastest_test": f"Test {key}",
                "major_risk": f"Risk {key}",
            }
            for key in ("A", "B", "C", "D", "E", "F", "G")
        ]
        result = _FakeResult([json.dumps(options_payload, ensure_ascii=False)])
        research_context = qsc.ResearchContext(
            user_problem="Need a quant direction with enough evidence to proceed.",
            search_strategy="websearch+context7",
            providers_used=["websearch"],
            suggested_search_queries=["quant direction evidence"],
            market_examples=[],
            existing_tools=[],
            technical_patterns=[],
            key_risks=["Execution quality can break the thesis."],
            unknowns=["Need to validate live slippage."],
            synthesized_summary="Direction B has the best grounded evidence.",
            citations=[
                qsc.ResearchCitation(
                    provider="websearch",
                    title="Evidence 1",
                    url="https://example.com/evidence-1",
                    snippet="Grounded evidence for direction B.",
                )
            ],
            provider_errors={},
            evidence_coverage={"grounded_claims": 7, "citations": 12},
            claim_attributions=[],
        )
        comparator_report = qsc.DirectionComparatorReport(
            items=[
                qsc.DirectionComparatorItem(key="A", composite_score=11, evidence_strength_score=3),
                qsc.DirectionComparatorItem(key="B", composite_score=16, evidence_strength_score=5),
                qsc.DirectionComparatorItem(key="C", composite_score=10, evidence_strength_score=3),
            ],
            top_keys=["B", "A", "C"],
            comparison_notes=["Direction B is easiest to defend."],
        )
        audit_report = qsc.EvidenceAuditReport(
            items=[
                qsc.EvidenceAuditItem(
                    key="A",
                    evidence_score=9,
                    supported_fields=["thesis"],
                ),
                qsc.EvidenceAuditItem(
                    key="B",
                    evidence_score=15,
                    supported_fields=["thesis", "primary_metric", "fastest_test"],
                ),
                qsc.EvidenceAuditItem(
                    key="C",
                    evidence_score=8,
                    supported_fields=["thesis"],
                ),
            ],
            top_keys=["B", "A", "C"],
            global_warnings=[],
        )

        decision = qsc._build_provisional_direction_decision_from_stage_reports(
            result,
            research_context=research_context,
            comparator_report=comparator_report,
            audit_report=audit_report,
        )

        self.assertIsNotNone(decision)
        self.assertEqual(decision.selected_direction, "B")
        self.assertEqual(decision.confidence, "low")
        self.assertEqual([option.key for option in decision.options], ["A", "B", "C", "D", "E", "F", "G"])

    def test_direction_debate_runs_once_more_after_final_research_refinement(self) -> None:
        initial_context = qsc.ResearchContext(
            user_problem="Need a defensible cross-exchange direction.",
            search_strategy="initial",
            providers_used=["websearch"],
            suggested_search_queries=["query-1", "query-2"],
            market_examples=[],
            existing_tools=[],
            technical_patterns=[],
            key_risks=[],
            unknowns=["Need evidence for execution asymmetry."],
            synthesized_summary="Initial evidence is thin.",
            citations=[],
            provider_errors={},
            evidence_coverage={"grounded_claims": 0},
            claim_attributions=[],
        )
        refined_context_one = initial_context.model_copy(
            update={
                "search_strategy": "refined-1",
                "citations": [
                    qsc.ResearchCitation(
                        provider="websearch",
                        title="Refined Evidence 1",
                        url="https://example.com/refined-1",
                        snippet="Some grounded support.",
                    )
                ],
                "evidence_coverage": {"grounded_claims": 2},
            }
        )
        refined_context_two = refined_context_one.model_copy(
            update={
                "search_strategy": "refined-2",
                "citations": [
                    qsc.ResearchCitation(
                        provider="websearch",
                        title="Refined Evidence 2",
                        url="https://example.com/refined-2",
                        snippet="Enough grounded support to choose a direction.",
                    )
                ],
                "evidence_coverage": {"grounded_claims": 7},
            }
        )
        final_decision = qsc.DirectionDecision(
            selected_direction="B",
            summary="Direction B is strongest after the second refinement.",
            options=[
                qsc.DirectionOption(
                    key=key,
                    name=f"Direction {key}",
                    thesis=f"Thesis {key}",
                    primary_metric=f"Metric {key}",
                    fastest_test=f"Test {key}",
                    major_risk=f"Risk {key}",
                )
                for key in ("A", "B", "C", "D", "E", "F", "G")
            ],
            backup_candidates=["A", "C"],
            go_conditions=["Validate with fresh data."],
            kill_criteria=["Stop if spread is unstable."],
            confidence="medium",
            verify_plan=["Run out-of-sample replay."],
        )

        contexts_seen = []
        refinement_inputs = []
        stage_results = [
            (None, None, None, {"research_queries": ["query-1"]}),
            (None, None, None, {"research_queries": ["query-2"]}),
            (final_decision, None, None, None),
        ]

        def fake_run_single_direction_debate(
            user_problem,
            *,
            mode,
            language_hint,
            llm,
            research_context,
            direction_judge_llm,
            cache_payload,
        ):
            contexts_seen.append(research_context)
            return stage_results[len(contexts_seen) - 1]

        def fake_run_direction_research_refinement(
            user_problem,
            *,
            mode,
            language_hint,
            gap_info,
            existing_context,
            direction_seed_plan,
        ):
            refinement_inputs.append(existing_context)
            return [refined_context_one, refined_context_two][len(refinement_inputs) - 1]

        with patch.object(m02, "DIRECTION_REFINEMENT_MAX_ITERATIONS", 2):
            with patch.object(
                m02, "_build_direction_seed_plan", return_value=SimpleNamespace(directions=[])
            ):
                with patch.object(m02, "_render_direction_seed_block", return_value=""):
                    with patch.object(
                        m02, "run_librarian_research", return_value=initial_context
                    ):
                        with patch.object(
                            m02,
                            "_build_direction_debate_cache_payload",
                            return_value={"research_context_sha256": "seed"},
                        ):
                            with patch.object(m02, "_cache_get_pydantic", return_value=None):
                                with patch.object(m02, "_get_direction_judge_llm", return_value=object()):
                                    with patch.object(m02, "_llm_model_id", return_value="test-model"):
                                        with patch.object(
                                            m02,
                                            "_run_single_direction_debate",
                                            side_effect=fake_run_single_direction_debate,
                                        ):
                                            with patch.object(
                                                m02,
                                                "_run_direction_research_refinement",
                                                side_effect=fake_run_direction_research_refinement,
                                            ):
                                                with patch.object(m02, "_text_sha256", return_value="sha"):
                                                    with patch.object(
                                                        m02, "_model_to_stable_json", return_value="{}"
                                                    ):
                                                        decision = m02.run_direction_debate(
                                                            "cross-exchange spread idea",
                                                            mode="Quant",
                                                            language_hint="Traditional Chinese",
                                                            llm=object(),
                                                            force_refresh=True,
                                                        )

        self.assertIs(decision, final_decision)
        self.assertEqual(
            contexts_seen, [initial_context, refined_context_one, refined_context_two]
        )
        self.assertEqual(refinement_inputs, [initial_context, refined_context_one])

    def test_direction_debate_mode_requires_cli_flag(self) -> None:
        with patch.dict(os.environ, {"DIRECTION_DEBATE_ENABLED": "1"}, clear=False):
            self.assertFalse(
                m07._direction_debate_enabled_from_inputs(False, False, "idea")
            )
        self.assertTrue(m07._direction_debate_enabled_from_inputs(True, False, "idea"))
        self.assertTrue(m07._direction_debate_enabled_from_inputs(False, True, "idea"))

    def test_gate_feedback_loop_can_be_toggled_from_env(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            self.assertTrue(m07._gate_feedback_enabled_from_env())
        with patch.dict(os.environ, {"GATE_DIRECTION_FEEDBACK_ENABLED": "1"}, clear=True):
            self.assertTrue(m07._gate_feedback_enabled_from_env())
        with patch.dict(os.environ, {"GATE_DIRECTION_FEEDBACK_ENABLED": "0"}, clear=True):
            self.assertFalse(m07._gate_feedback_enabled_from_env())

    def test_effective_gate_feedback_requires_runtime_path(self) -> None:
        self.assertTrue(
            m07._effective_gate_feedback_enabled(
                True, True, True, "idea"
            )
        )
        self.assertFalse(
            m07._effective_gate_feedback_enabled(
                True, False, True, "idea"
            )
        )
        self.assertFalse(
            m07._effective_gate_feedback_enabled(
                True, True, False, "idea"
            )
        )
        self.assertFalse(
            m07._effective_gate_feedback_enabled(
                True, True, True, "project_path"
            )
        )

    def test_gate_feedback_prompt_is_disabled_when_selective_rerun_is_off(self) -> None:
        report = qsc.AnalysisReport(
            project_name="epsilon",
            summary="direction is viable",
            consensus="keep current approach",
            disagreement="detail is still missing",
            experiments=[],
            score=60,
            mode_used="Quant",
            risk_level="Medium",
        )
        gate = qsc.GateDecision(
            consensus="viable",
            disagreement="missing execution detail",
            experiments=[],
            ready_for_codegen=False,
            blocking_risks=[],
            required_experiments_before_codegen=[],
            agents_needing_rerun=["ops"],
            rerun_reasons={"ops": "missing execution steps"},
            overall_score=60,
            confidence="medium",
            direction_feedback_needed=True,
            direction_feedback_type="detail",
        )
        build_flags = []

        def fake_build_analysis_crew(
            user_problem,
            mode,
            language_hint,
            llm,
            *,
            active_roles=None,
            rerun_note=None,
            direction_feedback_enabled=False,
        ):
            build_flags.append(direction_feedback_enabled)
            return _FakeCrew(
                list(m05.ANALYST_AGENT_ORDER) + ["gate_controller", "format_checker"],
                _FakeResult(["gate_controller: keep current decision"]),
            )

        with patch.object(m05, "build_analysis_crew", side_effect=fake_build_analysis_crew):
            with patch.object(m05, "_parse_analysis_outputs", return_value=(report, gate)):
                result, final_report, final_gate = m05.run_analysis_with_selective_rerun(
                    "gate disabled prompt alignment",
                    mode="Quant",
                    language_hint="Traditional Chinese",
                    llm=object(),
                    enable_selective_rerun=False,
                    gate_feedback_enabled=True,
                    direction_debate_enabled=True,
                    budget_policy=None,
                    run_snapshot=None,
                )

        self.assertIsNotNone(result)
        self.assertIs(final_report, report)
        self.assertIs(final_gate, gate)
        self.assertEqual(build_flags, [False])

    def test_gate_direction_feedback_heuristic_accepts_chinese_evidence_gap_language(self) -> None:
        gate = qsc.GateDecision(
            consensus="方向可行但需要補充",
            disagreement="目前證據不足，且交易成本細節不足",
            experiments=[],
            ready_for_codegen=False,
            blocking_risks=[],
            required_experiments_before_codegen=[],
            agents_needing_rerun=["research"],
            rerun_reasons={"research": "缺少交易成本與資料依據"},
            overall_score=40,
            confidence="low",
            direction_feedback_type="evidence",
        )
        self.assertTrue(m05._gate_requests_direction_feedback(gate))

    def test_librarian_query_plan_includes_all_seed_directions(self) -> None:
        seed_plan = qsc.DirectionSeedPlan(
            summary="Initial rough directions before librarian research.",
            directions=[
                qsc.DirectionSeedIdea(
                    label="Mean reversion basket",
                    thesis="Use short-horizon mean reversion across liquid futures.",
                    why_now="Simple baseline for a fast edge check.",
                    search_terms=["mean reversion futures", "liquid futures"],
                ),
                qsc.DirectionSeedIdea(
                    label="Adaptive trend filter",
                    thesis="Use adaptive trend regimes with volatility-aware entries.",
                    why_now="Potentially more robust under regime shifts.",
                    search_terms=["adaptive trend", "volatility regime"],
                ),
            ],
        )
        breakdown = {
            "core_objective": "Validate a trading strategy idea",
            "entities": ["BTC perpetual", "ETH perpetual"],
            "constraints": ["Low latency is not required"],
            "domain_keywords": ["crypto", "perpetual futures"],
            "technical_stack": ["python", "backtest"],
        }
        smart_queries = {
            "market": ["crypto perpetual market structure"],
            "technical": ["python backtest slippage handling"],
            "competitor": ["open source crypto strategy frameworks"],
        }

        with patch.object(m04, "_build_llm_problem_breakdown", return_value=breakdown):
            with patch.object(m04, "_build_smart_search_queries", return_value=smart_queries):
                plan = m04._build_librarian_query_plan(
                    "Build a viable quant strategy around liquid crypto perpetuals.",
                    "Quant",
                    language_hint="English",
                    direction_seed_plan=seed_plan,
                )

        self.assertEqual(plan["direction_seed_count"], 2)
        self.assertGreaterEqual(plan["query_budget_per_lane"], 4)
        self.assertIn("mean reversion futures", " ".join(plan["query_map"]["market"]))
        self.assertIn("adaptive trend", " ".join(plan["query_map"]["technical"]))
        self.assertIn("alternatives competitors", " ".join(plan["query_map"]["competitor"]))
        self.assertEqual(
            [item["label"] for item in plan["problem_breakdown"]["seed_directions"]],
            ["Mean reversion basket", "Adaptive trend filter"],
        )

    def test_direction_debate_runs_seed_then_librarian_then_final_llm(self) -> None:
        seed_plan = qsc.DirectionSeedPlan(
            summary="Two rough strategy branches to validate before final direction selection.",
            directions=[
                qsc.DirectionSeedIdea(
                    label="Direction Alpha",
                    thesis="Explore a rule-based alpha idea first.",
                    why_now="Cheap to verify.",
                    search_terms=["rule-based alpha", "slippage"],
                ),
                qsc.DirectionSeedIdea(
                    label="Direction Beta",
                    thesis="Explore an adaptive regime idea second.",
                    why_now="Could be more robust.",
                    search_terms=["adaptive regime", "regime shift"],
                ),
            ],
        )
        final_decision = qsc.DirectionDecision(
            selected_direction="B",
            summary="Direction Beta remains strongest after librarian research.",
            options=[
                qsc.DirectionOption(
                    key=key,
                    name=f"Option {key}",
                    thesis=f"Thesis {key}",
                    primary_metric=f"Metric {key}",
                    fastest_test=f"Test {key}",
                    major_risk=f"Risk {key}",
                )
                for key in ("A", "B", "C", "D", "E", "F", "G")
            ],
            backup_candidates=["A", "C"],
            go_conditions=["Evidence confirms execution feasibility"],
            kill_criteria=["No reliable execution data exists"],
            confidence="medium",
            verify_plan=["Validate slippage and data-source assumptions"],
        )
        librarian_context = qsc.ResearchContext(
            user_problem="Need a strategy for liquid crypto perpetuals.",
            key_risks=["Regime instability remains a risk."],
            suggested_search_queries=["rule-based alpha slippage", "adaptive regime trading"],
            search_strategy="websearch+context7",
            providers_used=["websearch", "context7"],
            synthesized_summary="Librarian found enough evidence to narrow the direction.",
            claim_attributions=[],
            citations=[],
        )
        captured = {"seed_problem": None, "final_problem": None}

        def fake_run_librarian_research(
            user_problem,
            *,
            mode,
            language_hint,
            direction_seed_plan=None,
        ):
            captured["seed_problem"] = user_problem
            self.assertIs(direction_seed_plan, seed_plan)
            self.assertEqual(mode, "Quant")
            self.assertEqual(language_hint, "English")
            self.assertIn("=== INITIAL STRATEGY DIRECTIONS ===", user_problem)
            self.assertIn("Direction Alpha", user_problem)
            self.assertIn("Direction Beta", user_problem)
            return librarian_context

        def fake_run_single_direction_debate(
            user_problem,
            *,
            mode,
            language_hint,
            llm,
            research_context,
            direction_judge_llm,
            cache_payload,
        ):
            captured["final_problem"] = user_problem
            self.assertEqual(mode, "Quant")
            self.assertEqual(language_hint, "English")
            self.assertIs(research_context, librarian_context)
            self.assertIn("=== INITIAL STRATEGY DIRECTIONS ===", user_problem)
            return final_decision, None, None, None

        with patch.object(m02, "_build_direction_seed_plan", return_value=seed_plan):
            with patch.object(
                m02,
                "run_librarian_research",
                side_effect=fake_run_librarian_research,
            ):
                with patch.object(m02, "_cache_get_pydantic", return_value=None):
                    with patch.object(m02, "_get_direction_judge_llm", return_value=object()):
                        with patch.object(
                            m02,
                            "_run_single_direction_debate",
                            side_effect=fake_run_single_direction_debate,
                        ):
                            decision = m02.run_direction_debate(
                                "Need a strategy for liquid crypto perpetuals.",
                                mode="Quant",
                                language_hint="English",
                                llm=object(),
                            )

        self.assertIs(decision, final_decision)
        self.assertEqual(captured["seed_problem"], captured["final_problem"])
        self.assertIn("=== INITIAL STRATEGY DIRECTIONS ===", captured["final_problem"] or "")

    def test_direction_feedback_refines_incumbent_direction_instead_of_generating_new_seeds(self) -> None:
        incumbent_decision = qsc.DirectionDecision(
            selected_direction="B",
            summary="原始方向 B：聚焦流動性較好的永續合約，先驗證成本可控。",
            options=[
                qsc.DirectionOption(
                    key=key,
                    name=f"Option {key}",
                    thesis=f"Original thesis {key}",
                    primary_metric=f"Original metric {key}",
                    fastest_test=f"Original test {key}",
                    major_risk=f"Original risk {key}",
                )
                for key in ("A", "B", "C", "D", "E", "F", "G")
            ],
            backup_candidates=["A"],
            go_conditions=["先補齊交易成本與滑價證據"],
            kill_criteria=["流動性不足"],
            confidence="medium",
            verify_plan=["針對高滑價場景補證據"],
        )
        refined_decision = qsc.DirectionDecision(
            selected_direction="B",
            summary="補入交易成本、滑價與資料源可靠性後，原始方向 B 仍成立。",
            options=[
                qsc.DirectionOption(
                    key=key,
                    name=f"Option {key}",
                    thesis=f"Refined thesis {key}",
                    primary_metric=f"Refined metric {key}",
                    fastest_test=f"Refined test {key}",
                    major_risk=f"Refined risk {key}",
                )
                for key in ("A", "B", "C", "D", "E", "F", "G")
            ],
            backup_candidates=["A"],
            go_conditions=["成本模型完成"],
            kill_criteria=["滑價超標"],
            confidence="medium",
            verify_plan=["檢查高滑價與資料延遲"],
        )
        librarian_context = qsc.ResearchContext(
            user_problem="Need a strategy for liquid crypto perpetuals.",
            search_strategy="incumbent refinement",
            market_examples=["補原方向證據"],
            existing_tools=["需要更完整成本證據"],
            technical_patterns=["slippage modelling"],
            key_risks=["liquidity stress"],
            synthesized_summary="補原方向，不重開新方向。",
            citations=[],
            claim_attributions=[],
            providers_used=["unit-test"],
            provider_errors={},
            suggested_search_queries=["交易成本 建模", "滑價 上限"],
            hallucination_flags=[],
            evidence_coverage={"grounded_claims": 2, "citations": 2},
            unknowns=["資料源延遲上限"],
        )
        feedback_note = (
            "Feedback path: evidence\n"
            "Evidence gaps:\n"
            "- 缺少交易成本假設\n"
            "- 缺少滑價上限\n"
            "Questions:\n"
            "- 高滑價場景是否仍成立？"
        )
        captured = {}

        def fake_run_librarian_research(
            user_problem,
            *,
            mode,
            language_hint,
            direction_seed_plan=None,
        ):
            captured["librarian_problem"] = user_problem
            captured["direction_seed_plan"] = direction_seed_plan
            self.assertIsNotNone(direction_seed_plan)
            self.assertEqual(len(direction_seed_plan.directions), 1)
            self.assertIn("Incumbent direction B", direction_seed_plan.directions[0].label)
            self.assertIn("Original metric B", " | ".join(direction_seed_plan.directions[0].search_terms))
            self.assertIn("缺少交易成本假設", user_problem)
            self.assertIn("=== INCUMBENT DIRECTION REFINEMENT MODE ===", user_problem)
            return librarian_context

        def fake_run_single_direction_debate(
            user_problem,
            *,
            mode,
            language_hint,
            llm,
            research_context,
            direction_judge_llm,
            cache_payload,
        ):
            captured["judge_problem"] = user_problem
            self.assertIs(research_context, librarian_context)
            self.assertIn("=== INCUMBENT DIRECTION REFINEMENT MODE ===", user_problem)
            self.assertIn("Incumbent selected direction: B", user_problem)
            self.assertIn("Supplement evidence and implementation detail for the incumbent direction only.", user_problem)
            self.assertIn("=== INITIAL STRATEGY DIRECTIONS ===", user_problem)
            self.assertIn("Original thesis B", user_problem)
            return refined_decision, None, None, None

        with patch.object(
            m02,
            "_build_direction_seed_plan",
            side_effect=AssertionError("fresh seed planning should not run in incumbent refinement mode"),
        ):
            with patch.object(
                m02,
                "run_librarian_research",
                side_effect=fake_run_librarian_research,
            ):
                with patch.object(m02, "_cache_get_pydantic", return_value=None):
                    with patch.object(m02, "_get_direction_judge_llm", return_value=object()):
                        with patch.object(
                            m02,
                            "_run_single_direction_debate",
                            side_effect=fake_run_single_direction_debate,
                        ):
                            decision = m02.run_direction_debate(
                                "Need a strategy for liquid crypto perpetuals.",
                                mode="Quant",
                                language_hint="English",
                                llm=object(),
                                feedback_note=feedback_note,
                                incumbent_direction=incumbent_decision,
                                force_refresh=True,
                            )

        self.assertIs(decision, refined_decision)
        self.assertIn("Original risk B", " | ".join(captured["direction_seed_plan"].directions[0].search_terms))

    def test_evidence_feedback_reruns_only_requested_analysts_and_direction_debate(self) -> None:
        first_report = qsc.AnalysisReport(
            project_name="alpha",
            summary="初版方向摘要",
            consensus="先用 alpha 版本進場",
            disagreement="交易成本估算與資料來源不足",
            experiments=[qsc.Experiment(goal="補研究", criteria="完成成本假設")],
            score=52,
            mode_used="Quant",
            risk_level="Medium",
        )
        first_gate = qsc.GateDecision(
            consensus="方向有潛力，但證據還不夠",
            disagreement="需要更細的成本、滑價與資料可得性細節",
            experiments=[qsc.Experiment(goal="補強成本證據", criteria="列出滑價假設")],
            ready_for_codegen=False,
            blocking_risks=[],
            required_experiments_before_codegen=[],
            agents_needing_rerun=["research"],
            rerun_reasons={"research": "缺少交易成本、滑價、資料來源細節"},
            overall_score=46,
            confidence="low",
            direction_feedback_needed=True,
            direction_feedback_type="evidence",
            direction_feedback_reason="方向未被推翻，但證據不足，需回到 direction debate 補細節",
            direction_feedback_evidence_gaps=["缺少交易成本假設", "缺少滑價上限", "缺少資料來源可靠性"],
            direction_feedback_questions=["這個方向在高滑價場景是否仍成立？"],
        )
        final_gate = qsc.GateDecision(
            consensus="方向與風險邊界已補齊，可進入 CodeGen",
            disagreement="仍需後續實盤驗證",
            experiments=[qsc.Experiment(goal="實盤驗證", criteria="一週監控")],
            ready_for_codegen=True,
            blocking_risks=[],
            required_experiments_before_codegen=[],
            agents_needing_rerun=[],
            rerun_reasons={},
            overall_score=75,
            confidence="medium",
        )

        first_result = _FakeResult(
            [
                "research: 初版研究只講方向，沒有交易成本與滑價細節",
                "risk: 缺少極端滑價風險邊界",
                "ops: 缺少資料供應與刷新頻率說明",
                "biz: 報酬來源合理，但假設不夠細",
                "critic: 缺少可驗證的證據鏈",
                "gate_controller: 需要更多細節後再決策",
            ]
        )
        second_result = _FakeResult(
            [
                "research: 已補齊交易成本、滑價與資料源",
                "risk: 已補齊風險邊界",
                "ops: 已補齊監控與刷新策略",
                "biz: 已補齊可持續性說明",
                "critic: 已有足夠證據鏈",
                "gate_controller: 可以放行",
            ]
        )

        build_calls = []
        parse_pairs = [
            (first_report, first_gate),
            (first_report, final_gate),
            (first_report, final_gate),
        ]
        fake_crews = [
            _FakeCrew(
                list(m05.ANALYST_AGENT_ORDER) + ["gate_controller", "format_checker"],
                first_result,
            ),
            _FakeCrew(
                list(m05.ANALYST_AGENT_ORDER) + ["gate_controller", "format_checker"],
                second_result,
            ),
            _FakeCrew(
                list(m05.ANALYST_AGENT_ORDER) + ["gate_controller", "format_checker"],
                second_result,
            ),
        ]
        debate_calls = []
        incumbent_decision = qsc.DirectionDecision(
            selected_direction="B",
            summary="原始方向 B：先鎖定具流動性的永續合約，重點在交易成本可控。",
            options=[
                qsc.DirectionOption(
                    key=key,
                    name=f"Option {key}",
                    thesis=f"Original thesis {key}",
                    primary_metric=f"Original metric {key}",
                    fastest_test=f"Original test {key}",
                    major_risk=f"Original risk {key}",
                )
                for key in ("A", "B", "C", "D", "E", "F", "G")
            ],
            backup_candidates=["A", "C"],
            go_conditions=["原方向先完成交易成本量化"],
            kill_criteria=["永續合約流動性不符預期"],
            confidence="medium",
            verify_plan=["先補成本與滑價證據"],
        )

        refined_decision = qsc.DirectionDecision(
            selected_direction="B",
            summary="方向 B 在補入交易成本、滑價與資料源可靠性後仍可行，且比方向 A 更穩定。",
            options=[
                qsc.DirectionOption(
                    key=key,
                    name=f"Option {key}",
                    thesis=f"Thesis {key}",
                    primary_metric=f"Metric {key}",
                    fastest_test=f"Test {key}",
                    major_risk=f"Risk {key}",
                )
                for key in ("A", "B", "C", "D", "E", "F", "G")
            ],
            backup_candidates=["A", "C"],
            go_conditions=["成本假設被明確量化"],
            kill_criteria=["資料源延遲超過策略容忍"],
            confidence="medium",
            verify_plan=["驗證高滑價場景"],
        )

        def fake_build_analysis_crew(
            user_problem,
            mode,
            language_hint,
            llm,
            *,
            active_roles=None,
            rerun_note=None,
            direction_feedback_enabled=False,
        ):
            build_calls.append(
                {
                    "active_roles": None if active_roles is None else set(active_roles),
                    "rerun_note": rerun_note,
                    "direction_feedback_enabled": direction_feedback_enabled,
                }
            )
            return fake_crews[len(build_calls) - 1]

        def fake_parse_analysis_outputs(result, *, llm, language_hint, mode):
            return parse_pairs.pop(0)

        def fake_run_direction_debate(
            user_problem,
            *,
            mode,
            language_hint,
            llm,
            feedback_note=None,
            incumbent_direction=None,
            force_refresh=False,
        ):
            debate_calls.append(
                {
                    "user_problem": user_problem,
                    "feedback_note": feedback_note,
                    "incumbent_direction": incumbent_direction,
                    "force_refresh": force_refresh,
                }
            )
            return refined_decision

        with patch.object(m05, "build_analysis_crew", side_effect=fake_build_analysis_crew):
            with patch.object(m05, "_parse_analysis_outputs", side_effect=fake_parse_analysis_outputs):
                with patch.object(m05, "run_direction_debate", side_effect=fake_run_direction_debate):
                    result, report, gate = m05.run_analysis_with_selective_rerun(
                        "策略方向測試",
                        mode="Quant",
                        language_hint="Traditional Chinese",
                        llm=object(),
                        enable_selective_rerun=True,
                        gate_feedback_enabled=True,
                        direction_debate_enabled=True,
                        incumbent_direction=incumbent_decision,
                        budget_policy=None,
                        run_snapshot=None,
                    )

        self.assertIs(result, second_result)
        self.assertIs(report, first_report)
        self.assertTrue(gate.ready_for_codegen)
        self.assertEqual(len(debate_calls), 1)
        self.assertTrue(debate_calls[0]["force_refresh"])
        self.assertIn("缺少交易成本假設", debate_calls[0]["feedback_note"])
        self.assertIn("research: 初版研究只講方向", debate_calls[0]["feedback_note"])
        self.assertIs(debate_calls[0]["incumbent_direction"], incumbent_decision)
        self.assertEqual(build_calls[1]["active_roles"], {"research"})
        self.assertTrue(build_calls[1]["direction_feedback_enabled"])
        self.assertIn("Direction debate feedback", build_calls[1]["rerun_note"])
        self.assertIn("Feedback path: evidence", build_calls[1]["rerun_note"])
        self.assertIn("缺少交易成本假設", build_calls[1]["rerun_note"])
        self.assertIn("方向 B", build_calls[1]["rerun_note"])
        self.assertIn("Refined go conditions:", build_calls[1]["rerun_note"])
        self.assertIn("Refined primary metric: Metric B", build_calls[1]["rerun_note"])
        self.assertIn("Refined fastest test: Test B", build_calls[1]["rerun_note"])
        self.assertIn("成本假設被明確量化", build_calls[1]["rerun_note"])
        self.assertIn("Refined verify plan:", build_calls[1]["rerun_note"])
        self.assertIn("驗證高滑價場景", build_calls[1]["rerun_note"])

    def test_validation_first_gate_promotion_skips_direction_feedback_rerun(self) -> None:
        report = qsc.AnalysisReport(
            project_name="phase0_validation_framework",
            summary="需要先做 phase0 validation framework",
            consensus="先交付 measurement harness",
            disagreement="production 細節先不要鎖死",
            experiments=[qsc.Experiment(goal="語義驗證", criteria="輸出報告")],
            score=48,
            mode_used="Quant",
            risk_level="Medium",
        )
        gate = qsc.GateDecision(
            consensus="production 方向尚不能直接放行",
            disagreement="mid-price 語義、門檻校準與 timestamp alignment 都還沒驗證",
            experiments=[qsc.Experiment(goal="校準門檻", criteria="產出 calibration report")],
            ready_for_codegen=False,
            blocking_risks=["缺少 mid-price semantic validation"],
            required_experiments_before_codegen=["完成 timestamp alignment measurement"],
            advisory_experiments_after_codegen=[],
            agents_needing_rerun=["research"],
            rerun_reasons={"research": "缺少 phase0 measurement detail"},
            overall_score=45,
            confidence="low",
            direction_feedback_needed=True,
            direction_feedback_type="evidence",
            direction_feedback_reason="這些缺口正是 validation harness 應該量測的內容",
            direction_feedback_evidence_gaps=["缺少 threshold calibration"],
            direction_feedback_questions=["如何量測 semantic drift？"],
        )

        fake_result = _FakeResult(
            [
                "research: 需要先做 measurement harness",
                "gate_controller: validation-first scope should be allowed",
            ]
        )

        with patch.object(
            m05,
            "build_analysis_crew",
            return_value=_FakeCrew(
                list(m05.ANALYST_AGENT_ORDER) + ["gate_controller", "format_checker"],
                fake_result,
            ),
        ):
            with patch.object(
                m05,
                "_parse_analysis_outputs",
                return_value=(report, gate),
            ):
                with patch.object(m05, "run_direction_debate") as mocked_direction_debate:
                    result, final_report, final_gate = m05.run_analysis_with_selective_rerun(
                        "請先做 phase0 validation framework，驗證 mid-price semantic 與 threshold calibration。",
                        mode="Quant",
                        language_hint="Traditional Chinese",
                        llm=object(),
                        enable_selective_rerun=True,
                        gate_feedback_enabled=True,
                        direction_debate_enabled=True,
                        budget_policy=None,
                        run_snapshot=None,
                    )

        self.assertIs(result, fake_result)
        self.assertIs(final_report, report)
        self.assertTrue(final_gate.ready_for_codegen)
        self.assertEqual(final_gate.codegen_scope, "validation")
        self.assertFalse(final_gate.direction_feedback_needed)
        self.assertFalse(final_gate.blocking_risks)
        self.assertIn("threshold calibration", " | ".join(final_gate.validation_objectives).lower())
        mocked_direction_debate.assert_not_called()

    def test_direction_debate_validation_first_guidance_and_scoring_bias(self) -> None:
        prompts = m04._build_direction_debate_prompt_bundle(
            user_problem="請先做 phase0 validation framework，驗證語義與 threshold calibration。",
            language_hint="Traditional Chinese",
            research_block="research",
        )
        self.assertIn("VALIDATION-FIRST ROUTING", prompts["explorer"])
        self.assertIn("VALIDATION-FIRST ROUTING", prompts["comparator"])
        self.assertIn("VALIDATION-FIRST ROUTING", prompts["judge"])

        research_context = qsc.ResearchContext(
            user_problem="請先做 phase0 validation framework，驗證語義與 threshold calibration。",
            search_strategy="test",
            providers_used=[],
            suggested_search_queries=[],
            market_examples=[],
            existing_tools=[],
            technical_patterns=[],
            key_risks=[],
            unknowns=[],
            synthesized_summary="",
            citations=[],
            provider_errors={},
            evidence_coverage={},
            hallucination_flags=[],
            claim_attributions=[],
            field_capability_matrix=[],
        )
        validation_option = qsc.DirectionOption(
            key="A",
            name="Mid-price semantic validation framework",
            thesis="Build a measurement harness to validate cross-exchange semantics and calibrate thresholds.",
            primary_metric="Semantic drift report coverage",
            fastest_test="Run a phase0 comparison report",
            major_risk="Semantic assumptions may fail",
        )
        production_option = qsc.DirectionOption(
            key="B",
            name="Production alpha strategy",
            thesis="Ship the final spread trading strategy immediately.",
            primary_metric="PnL",
            fastest_test="Deploy trading engine",
            major_risk="Unknown semantics break live trading",
        )

        self.assertGreater(
            m04._deterministic_direction_option_score(validation_option, research_context),
            m04._deterministic_direction_option_score(production_option, research_context),
        )

    def test_detail_feedback_reruns_only_requested_analysts_without_direction_debate(self) -> None:
        first_report = qsc.AnalysisReport(
            project_name="gamma",
            summary="方向成立但細節不足",
            consensus="核心方向可行",
            disagreement="實作與風險細節不足",
            experiments=[qsc.Experiment(goal="補細節", criteria="完成執行規格")],
            score=58,
            mode_used="Quant",
            risk_level="Medium",
        )
        first_gate = qsc.GateDecision(
            consensus="方向成立，但需要更多流程與參數細節",
            disagreement="缺少刷新節奏、風險門檻與執行步驟",
            experiments=[qsc.Experiment(goal="補流程細節", criteria="完成規格")],
            ready_for_codegen=False,
            blocking_risks=[],
            required_experiments_before_codegen=[],
            agents_needing_rerun=["ops", "risk"],
            rerun_reasons={"ops": "缺少執行流程", "risk": "缺少風險門檻"},
            overall_score=55,
            confidence="medium",
            direction_feedback_needed=True,
            direction_feedback_type="detail",
            direction_feedback_reason="方向不需要重選，但細節不足",
            direction_feedback_evidence_gaps=["缺少刷新頻率", "缺少停損門檻"],
            direction_feedback_questions=["批次頻率與停損閾值應該如何設定？"],
        )
        final_gate = qsc.GateDecision(
            consensus="細節補齊，可進入 CodeGen",
            disagreement="仍需後續驗證",
            experiments=[],
            ready_for_codegen=True,
            blocking_risks=[],
            required_experiments_before_codegen=[],
            agents_needing_rerun=[],
            rerun_reasons={},
            overall_score=72,
            confidence="medium",
        )
        first_result = _FakeResult(
            [
                "research: 核心方向合理",
                "risk: 風險門檻未量化",
                "ops: 缺少刷新節奏與執行步驟",
                "biz: 商業邏輯可接受",
                "critic: 需要更具體規格",
                "gate_controller: 請補細節",
            ]
        )
        second_result = _FakeResult(
            [
                "risk: 已補停損與滑價門檻",
                "ops: 已補刷新節奏與執行步驟",
                "gate_controller: 可以放行",
            ]
        )

        build_calls = []
        parse_pairs = [
            (first_report, first_gate),
            (first_report, final_gate),
            (first_report, final_gate),
        ]
        fake_crews = [
            _FakeCrew(
                list(m05.ANALYST_AGENT_ORDER) + ["gate_controller", "format_checker"],
                first_result,
            ),
            _FakeCrew(
                ["risk", "ops", "gate_controller", "format_checker"],
                second_result,
            ),
            _FakeCrew(
                ["gate_controller", "format_checker"],
                second_result,
            ),
        ]

        def fake_build_analysis_crew(
            user_problem,
            mode,
            language_hint,
            llm,
            *,
            active_roles=None,
            rerun_note=None,
            direction_feedback_enabled=False,
        ):
            build_calls.append(
                {
                    "active_roles": None if active_roles is None else set(active_roles),
                    "rerun_note": rerun_note,
                    "direction_feedback_enabled": direction_feedback_enabled,
                }
            )
            return fake_crews[len(build_calls) - 1]

        with patch.object(m05, "build_analysis_crew", side_effect=fake_build_analysis_crew):
            with patch.object(m05, "_parse_analysis_outputs", side_effect=lambda result, **_: parse_pairs.pop(0)):
                with patch.object(m05, "run_direction_debate") as mocked_direction_debate:
                    result, report, gate = m05.run_analysis_with_selective_rerun(
                        "策略細節測試",
                        mode="Quant",
                        language_hint="Traditional Chinese",
                        llm=object(),
                        enable_selective_rerun=True,
                        gate_feedback_enabled=True,
                        direction_debate_enabled=True,
                        budget_policy=None,
                        run_snapshot=None,
                    )

        self.assertIs(result, second_result)
        self.assertIs(report, first_report)
        self.assertTrue(gate.ready_for_codegen)
        mocked_direction_debate.assert_not_called()
        self.assertEqual(build_calls[1]["active_roles"], {"ops", "risk"})
        self.assertIn("Feedback path: detail", build_calls[1]["rerun_note"])

    def test_direction_feedback_stops_after_two_bounces(self) -> None:
        report = qsc.AnalysisReport(
            project_name="delta",
            summary="需要多次補證據",
            consensus="方向可能成立",
            disagreement="證據仍不足",
            experiments=[qsc.Experiment(goal="補證據", criteria="完成驗證")],
            score=41,
            mode_used="Quant",
            risk_level="High",
        )
        loop_gate = qsc.GateDecision(
            consensus="需補證據",
            disagreement="資料來源與驗證鏈不足",
            experiments=[],
            ready_for_codegen=False,
            blocking_risks=[],
            required_experiments_before_codegen=[],
            agents_needing_rerun=["research"],
            rerun_reasons={"research": "補資料來源"},
            overall_score=41,
            confidence="low",
            direction_feedback_needed=True,
            direction_feedback_type="evidence",
            direction_feedback_reason="需要回送補證據",
            direction_feedback_evidence_gaps=["缺少可驗證資料源"],
            direction_feedback_questions=["資料來源是否可靠？"],
        )
        parse_pairs = [
            (report, loop_gate.model_copy(deep=True)),
            (report, loop_gate.model_copy(deep=True)),
            (report, loop_gate.model_copy(deep=True)),
        ]
        fake_crews = [
            _FakeCrew(
                list(m05.ANALYST_AGENT_ORDER) + ["gate_controller", "format_checker"],
                _FakeResult(["gate_controller: first loop"]),
            ),
            _FakeCrew(
                ["research", "gate_controller", "format_checker"],
                _FakeResult(["gate_controller: second loop"]),
            ),
            _FakeCrew(
                ["research", "gate_controller", "format_checker"],
                _FakeResult(["gate_controller: third loop"]),
            ),
        ]
        build_calls = []
        debate_calls = []

        refined_decision = qsc.DirectionDecision(
            selected_direction="B",
            summary="仍可繼續，但需更多證據。",
            options=[
                qsc.DirectionOption(
                    key=key,
                    name=f"Option {key}",
                    thesis=f"Thesis {key}",
                    primary_metric=f"Metric {key}",
                    fastest_test=f"Test {key}",
                    major_risk=f"Risk {key}",
                )
                for key in ("A", "B", "C", "D", "E", "F", "G")
            ],
            backup_candidates=["A"],
            go_conditions=["補到足夠來源"],
            kill_criteria=["來源不存在"],
            confidence="low",
            verify_plan=["再找兩輪證據"],
        )

        def fake_build_analysis_crew(
            user_problem,
            mode,
            language_hint,
            llm,
            *,
            active_roles=None,
            rerun_note=None,
            direction_feedback_enabled=False,
        ):
            build_calls.append(active_roles)
            return fake_crews[len(build_calls) - 1]

        def fake_run_direction_debate(*args, **kwargs):
            debate_calls.append(kwargs)
            return refined_decision

        with patch.object(m05, "build_analysis_crew", side_effect=fake_build_analysis_crew):
            with patch.object(m05, "_parse_analysis_outputs", side_effect=lambda result, **_: parse_pairs.pop(0)):
                with patch.object(m05, "run_direction_debate", side_effect=fake_run_direction_debate):
                    result, _, gate = m05.run_analysis_with_selective_rerun(
                        "策略回送上限測試",
                        mode="Quant",
                        language_hint="Traditional Chinese",
                        llm=object(),
                        enable_selective_rerun=True,
                        gate_feedback_enabled=True,
                        direction_debate_enabled=True,
                        budget_policy=None,
                        run_snapshot=None,
                    )

        self.assertEqual(len(build_calls), 3)
        self.assertEqual(len(debate_calls), 2)
        self.assertFalse(gate.ready_for_codegen)
        self.assertFalse(gate.should_kill)
        self.assertIsNotNone(result)

    def test_direction_feedback_can_kill_flow_when_refined_direction_returns_none(self) -> None:
        first_report = qsc.AnalysisReport(
            project_name="beta",
            summary="需要再次確認方向",
            consensus="目前方向尚未被證明成立",
            disagreement="可能存在根本性矛盾",
            experiments=[qsc.Experiment(goal="補方向驗證", criteria="完成重審")],
            score=35,
            mode_used="Quant",
            risk_level="High",
        )
        first_gate = qsc.GateDecision(
            consensus="方向需要重審",
            disagreement="資料補齊後仍可能不成立",
            experiments=[qsc.Experiment(goal="重審方向", criteria="direction debate 回覆")],
            ready_for_codegen=False,
            blocking_risks=[],
            required_experiments_before_codegen=[],
            agents_needing_rerun=["critic"],
            rerun_reasons={"critic": "根本方向可能錯誤"},
            overall_score=25,
            confidence="low",
            direction_feedback_needed=True,
            direction_feedback_type="evidence",
            direction_feedback_reason="需要重新判斷方向是否根本不可行",
            direction_feedback_evidence_gaps=["核心假設互相衝突"],
            direction_feedback_questions=["若核心資料源不存在，是否應直接放棄？"],
        )
        first_result = _FakeResult(
            [
                "research: 核心數據來源不明",
                "risk: 核心假設互斥",
                "ops: 執行路徑不完整",
                "biz: 收益來源站不住腳",
                "critic: 建議回到方向層重審",
                "gate_controller: 不應直接 codegen",
            ]
        )
        rejected_decision = qsc.DirectionDecision(
            selected_direction="none",
            summary="所有候選方向都缺乏可執行基礎。",
            options=[
                qsc.DirectionOption(
                    key=key,
                    name=f"Option {key}",
                    thesis=f"Thesis {key}",
                    primary_metric=f"Metric {key}",
                    fastest_test=f"Test {key}",
                    major_risk=f"Risk {key}",
                )
                for key in ("A", "B", "C", "D", "E", "F", "G")
            ],
            backup_candidates=[],
            go_conditions=[],
            kill_criteria=["核心資料源不存在"],
            confidence="low",
            verify_plan=["停止此方向"],
        )

        build_calls = []

        def fake_build_analysis_crew(
            user_problem,
            mode,
            language_hint,
            llm,
            *,
            active_roles=None,
            rerun_note=None,
            direction_feedback_enabled=False,
        ):
            build_calls.append({"active_roles": active_roles, "rerun_note": rerun_note})
            return _FakeCrew(
                list(m05.ANALYST_AGENT_ORDER) + ["gate_controller", "format_checker"],
                first_result,
            )

        with patch.object(m05, "build_analysis_crew", side_effect=fake_build_analysis_crew):
            with patch.object(m05, "_parse_analysis_outputs", return_value=(first_report, first_gate)):
                with patch.object(m05, "run_direction_debate", return_value=rejected_decision):
                    result, report, gate = m05.run_analysis_with_selective_rerun(
                        "策略方向 kill 測試",
                        mode="Quant",
                        language_hint="Traditional Chinese",
                        llm=object(),
                        enable_selective_rerun=True,
                        gate_feedback_enabled=True,
                        direction_debate_enabled=True,
                        budget_policy=None,
                        run_snapshot=None,
                    )

        self.assertIs(result, first_result)
        self.assertIs(report, first_report)
        self.assertTrue(gate.should_kill)
        self.assertIn("Direction debate", gate.kill_reason or "")
        self.assertEqual(len(build_calls), 1)


if __name__ == "__main__":
    unittest.main()
