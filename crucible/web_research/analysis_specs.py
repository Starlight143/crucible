from __future__ import annotations

import re
from typing import Any, Dict, List, Set, Tuple

DEFAULT_ANALYST_TASK_MAX_INPUT_CHARS = 9000
DEFAULT_GATE_CONTEXT_COMPACTOR_MAX_INPUT_CHARS = 14000
DEFAULT_GATE_CONTROLLER_MAX_INPUT_CHARS = 12000
DEFAULT_FORMAT_CHECKER_MAX_INPUT_CHARS = 12000


def _budget_from_deps(deps: Dict[str, Any], key: str, default: int) -> int:
    try:
        value = int(deps.get(key, default) or default)
    except Exception:
        value = default
    return max(1000, value)


def normalize_rerun_agent_keys(agent_names: List[str]) -> List[str]:
    alias_map = {
        "research": "research",
        "researcher": "research",
        "researchagent": "research",
        "risk": "risk",
        "riskagent": "risk",
        "ops": "ops",
        "operations": "ops",
        "opsagent": "ops",
        "biz": "biz",
        "business": "biz",
        "bizagent": "biz",
        "critic": "critic",
        "criticagent": "critic",
    }
    normalized: List[str] = []
    for raw in agent_names or []:
        key = re.sub(r"[^a-z]", "", (raw or "").lower())
        role = alias_map.get(key)
        if role and role not in normalized:
            normalized.append(role)
    return normalized


def build_analysis_specs(
    *,
    mode_config: Any,
    active_roles: Set[str],
    direction_feedback_enabled: bool,
    deps: Dict[str, Any],
) -> Tuple[Dict[str, Any], List[Any], Dict[str, str]]:
    analyst_task_max_input_chars = _budget_from_deps(
        deps,
        "ANALYST_TASK_MAX_INPUT_CHARS",
        DEFAULT_ANALYST_TASK_MAX_INPUT_CHARS,
    )
    gate_context_compactor_max_input_chars = _budget_from_deps(
        deps,
        "GATE_CONTEXT_COMPACTOR_MAX_INPUT_CHARS",
        DEFAULT_GATE_CONTEXT_COMPACTOR_MAX_INPUT_CHARS,
    )
    gate_controller_max_input_chars = _budget_from_deps(
        deps,
        "GATE_CONTROLLER_MAX_INPUT_CHARS",
        DEFAULT_GATE_CONTROLLER_MAX_INPUT_CHARS,
    )
    format_checker_max_input_chars = _budget_from_deps(
        deps,
        "FORMAT_CHECKER_MAX_INPUT_CHARS",
        DEFAULT_FORMAT_CHECKER_MAX_INPUT_CHARS,
    )
    gate_guidance = "\n".join(deps["mode_gate_controller_guidance"](mode_config))
    analyst_focus = {
        "research": (
            "Research",
            "Surface market and user opportunities with practical hypotheses.",
            f"Focus on {mode_config.research_focus}. Produce concise, decision-oriented findings.",
        ),
        "risk": (
            "Risk",
            "Identify irreversible risks and explicit failure conditions.",
            "Prioritize downside protection, falsifiable assumptions, and kill criteria.",
        ),
        "ops": (
            "Ops",
            "Define execution sequencing and operational constraints.",
            "Focus on delivery risk, monitoring, reliability, and rollout dependencies.",
        ),
        "biz": (
            "Biz",
            "Validate monetization and distribution assumptions.",
            f"Focus on {mode_config.biz_focus}.",
        ),
        "critic": (
            "Critic",
            "Challenge weak assumptions and expose hidden coupling.",
            "Be strict, concrete, and non-generic.",
        ),
    }
    if mode_config.name.strip().lower() == "agent":
        analyst_focus["research"] = (
            "Research",
            "Define automation scope, state boundaries, and deterministic decision assumptions.",
            f"Focus on {mode_config.research_focus}. Prioritize machine-only execution, state observability, and replayability.",
        )
        analyst_focus["risk"] = (
            "Risk",
            "Identify irreversible execution, protocol, and state-consistency risks.",
            "Prioritize deterministic failure handling, replay safety, and anti-corruption boundaries.",
        )
        analyst_focus["ops"] = (
            "Ops",
            "Define runtime orchestration, deployment, and reliability constraints for a headless service.",
            "Focus on retries, process supervision, structured logs, monitoring, and safe recovery.",
        )
        analyst_focus["biz"] = (
            "Biz",
            "Validate incentive alignment, reward economics, and operator sustainability.",
            f"Focus on {mode_config.biz_focus}. Avoid consumer SaaS assumptions unless explicitly stated.",
        )

    AgentSpec = deps["AgentSpec"]
    TaskSpec = deps["TaskSpec"]
    RetryPolicy = deps["RetryPolicy"]
    analyst_agent_order = deps["ANALYST_AGENT_ORDER"]
    no_cross_role_rule = deps["NO_CROSS_ROLE_RULE"]
    common_output_rules = deps["COMMON_OUTPUT_RULES"]
    gate_controller_rules = deps["GATE_CONTROLLER_RULES"]
    gate_context_compactor_rules = deps.get("GATE_CONTEXT_COMPACTOR_RULES", "")

    agent_specs: Dict[str, Any] = {}
    template_vars: Dict[str, str] = {
        "gate_guidance": gate_guidance,
        "direction_feedback_enabled": "true" if direction_feedback_enabled else "false",
    }
    task_specs: List[Any] = []

    for role_key in analyst_agent_order:
        if role_key not in active_roles:
            continue
        role_name, goal, focus = analyst_focus[role_key]
        agent_specs[role_key] = AgentSpec(
            name=role_key,
            role=role_name,
            goal=goal,
            backstory=(
                f"[{role_name}] {focus}\n"
                "Return structured findings with complete implementation-relevant detail.\n"
                "Preserve concrete assumptions, thresholds, dependencies, and risks instead of collapsing them into vague summaries.\n\n"
                + no_cross_role_rule
                + common_output_rules
            ),
            output_schema_name=None,
            parallel_safe=True,
            retry_policy=RetryPolicy(max_attempts=2, retry_on_json_fail=False),
            version="v1.0.0",
            behavior_contract=f"{role_name} specialist output must be concrete, complete, and decision-useful.",
        )
        task_specs.append(
            TaskSpec(
                name=role_key,
                description_template=(
                    "You are the [{role_name}] analyst for {mode_name} mode.\n"
                    "Problem:\n{user_problem}\n\n"
                    "Language hint: {language_hint}\n\n"
                    "Focus:\n{focus}\n\n"
                    "Return concrete findings that the Gate Controller and Format Checker can aggregate directly.\n"
                    "Keep the output dense and structured, but do not omit implementation-relevant detail.\n"
                    "Do not output markdown wrappers or final recommendations."
                ),
                agent_name=role_key,
                expected_output="Structured role-specific findings only.",
                max_input_chars=analyst_task_max_input_chars,
            )
        )
        template_vars[f"{role_key}_role_name"] = role_name
        template_vars[f"{role_key}_focus"] = focus

    gate_context = [r for r in analyst_agent_order if r in active_roles]
    compacted_gate_context = ["gate_context_compactor"]

    agent_specs["gate_context_compactor"] = AgentSpec(
        name="gate_context_compactor",
        role="Gate Context Compactor",
        goal="Compress analyst outputs into a compact but implementation-complete GateContextBundle.",
        backstory=(
            "You are the pre-gate context compactor.\n"
            "Return strict GateContextBundle JSON only.\n"
            "Aggressively deduplicate repeated points, but preserve implementation-critical detail, blockers, and rerun signals.\n\n"
            + gate_context_compactor_rules
        ),
        output_schema_name="GateContextBundle",
        parallel_safe=False,
        retry_policy=RetryPolicy(
            max_attempts=20,
            backoff_seconds=2.0,
            retry_on_json_fail=True,
        ),
        version="v1.0.0",
        behavior_contract="Must preserve implementation-critical detail while removing prompt-bloating duplication.",
        depends_on=gate_context,
    )

    agent_specs["gate_controller"] = AgentSpec(
        name="gate_controller",
        role="Gate Controller",
        goal="Integrate analyst findings and decide whether the workflow is allowed to proceed to CodeGen.",
        backstory=(
            "You are the flow-control arbiter.\n"
            "Emit strict GateDecision JSON only.\n"
            "Read the GateContextBundle before deciding.\n"
            "Preserve concrete execution/risk detail in the GateDecision fields instead of abstracting it away.\n"
            + gate_guidance
            + "\n\n"
            + (
                "Gate feedback loop is ENABLED. When the issue is missing evidence/detail rather than a fatal contradiction, "
                "request targeted feedback refinement instead of killing the flow, and classify it as evidence or detail.\n\n"
                if direction_feedback_enabled
                else "Gate feedback loop is DISABLED. Decide based only on the current analyst evidence.\n\n"
            )
            + gate_controller_rules
        ),
        output_schema_name="GateDecision",
        parallel_safe=False,
        retry_policy=RetryPolicy(
            max_attempts=2, retry_on_json_fail=True, retry_on_low_confidence=True
        ),
        version="v1.1.0",
        behavior_contract="Must emit strict GateDecision JSON with explicit flow-control fields.",
        depends_on=compacted_gate_context,
    )
    agent_specs["format_checker"] = AgentSpec(
        name="format_checker",
        role="Format Checker",
        goal="Organize the full analyst and gate detail into a valid AnalysisReport JSON payload for downstream implementation.",
        backstory=(
            "You are a strict formatter and handoff organizer.\n"
            "Use the GateContextBundle plus GateDecision context.\n"
            "Do not add new facts, assumptions, or recommendations.\n"
            "Preserve implementation-relevant detail and reorganize it into AnalysisReport fields that CodeGen can follow directly."
        ),
        output_schema_name="AnalysisReport",
        parallel_safe=False,
        retry_policy=RetryPolicy(max_attempts=2, retry_on_json_fail=True),
        version="v1.1.0",
        behavior_contract="No new facts allowed; must preserve and organize full upstream detail for CodeGen.",
        depends_on=compacted_gate_context + ["gate_controller"],
    )

    task_specs.append(
        TaskSpec(
            name="gate_context_compactor",
            description_template=(
                "Compress the analyst outputs into GateContextBundle JSON only.\n"
                "Mode: {mode_name}\n"
                "Language hint: {language_hint}\n"
                "Problem:\n{user_problem}\n\n"
                "Required fields:\n"
                "- executive_summary (string)\n"
                "- analyst_findings (dict[role,string])\n"
                "- implementation_requirements (list[string])\n"
                "- implementation_constraints (list[string])\n"
                "- validation_focus (list[string])\n"
                "- blocking_unknowns (list[string])\n"
                "- rerun_signals (dict[role,list[string]])\n\n"
                "Rules:\n"
                "- Deduplicate repetition, but do not drop implementation-critical detail.\n"
                "- analyst_findings must preserve each role's independent perspective.\n"
                "- Keep only material that affects gate decisions, downstream code generation, runtime validation, or rerun decisions.\n"
                "- Return JSON only."
            ),
            agent_name="gate_context_compactor",
            expected_output="GateContextBundle JSON only.",
            context_task_names=gate_context,
            output_pydantic_model="GateContextBundle",
            max_input_chars=gate_context_compactor_max_input_chars,
        )
    )

    task_specs.append(
        TaskSpec(
            name="gate_controller",
            description_template=(
                "Aggregate GateContextBundle and return GateDecision JSON only.\n"
                "Mode: {mode_name}\n"
                "Language hint: {language_hint}\n"
                "Problem:\n{user_problem}\n\n"
                "Read the GateContextBundle in full before deciding.\n"
                "When risks, requirements, or rerun reasons are concrete, preserve that specificity in the JSON fields.\n"
                "Required fields:\n"
                "- consensus (string)\n"
                "- disagreement (string)\n"
                "- experiments (list of {goal, criteria})\n"
                "- ready_for_codegen (bool)\n"
                "- blocking_risks (list[string])\n"
                "- required_experiments_before_codegen (list[string])\n"
                "- advisory_experiments_after_codegen (list[string])\n"
                "- codegen_scope (production|validation)\n"
                "- validation_scope_reason (string|null)\n"
                "- validation_objectives (list[string])\n"
                "- agents_needing_rerun (list[string])\n"
                "- rerun_reasons (dict)\n"
                "- direction_feedback_needed (bool)\n"
                "- direction_feedback_reason (string|null)\n"
                "- direction_feedback_type ('evidence'|'detail'|null)\n"
                "- direction_feedback_evidence_gaps (list[string])\n"
                "- direction_feedback_questions (list[string])\n"
                "- overall_score (0-100)\n"
                "- score_breakdown (dict: feasibility/risk/roi/uncertainty)\n"
                "- confidence (low|medium|high)\n"
                "- failure_type (enum)\n"
                "- failure_details (string|null)\n"
                "- should_kill (bool)\n"
                "- kill_reason (string|null)\n"
                "Direction debate feedback loop enabled: {direction_feedback_enabled}\n"
                "Mode guidance:\n{gate_guidance}\n"
                "Return JSON only."
            ),
            agent_name="gate_controller",
            expected_output="GateDecision JSON only.",
            context_task_names=compacted_gate_context,
            output_pydantic_model="GateDecision",
            max_input_chars=gate_controller_max_input_chars,
        )
    )
    task_specs.append(
        TaskSpec(
            name="format_checker",
            description_template=(
                "Convert GateContextBundle plus GateDecision into AnalysisReport JSON only.\n"
                "mode_used must be exactly '{mode_name}'.\n"
                "Language hint: {language_hint}\n"
                "Required AnalysisReport fields (ALL must be present):\n"
                "- project_name: short snake_case identifier derived from the strategy/problem description\n"
                "- summary: concise summary string derived from GateContextBundle.executive_summary or GateDecision.consensus\n"
                "- consensus: copy from GateDecision.consensus\n"
                "- disagreement: copy from GateDecision.disagreement\n"
                "- experiments: copy from GateDecision.experiments (list of {goal, criteria})\n"
                "- score: integer 0-100 — copy from GateDecision.overall_score (rename the field)\n"
                "- mode_used: exactly '{mode_name}'\n"
                "- risk_level: 'Low'|'Medium'|'High' — derive from GateDecision.confidence and overall_score "
                "(low confidence or score<50 → 'High'; medium confidence or 50<=score<70 → 'Medium'; otherwise 'Low')\n"
                "- analyst_findings: copy GateContextBundle.analyst_findings keyed by role name\n"
                "- gate_context_snapshot: copy all GateDecision fields as a JSON object\n"
                "- codegen_handoff_summary: organized implementation brief from GateContextBundle.executive_summary "
                "plus GateDecision.validation_scope_reason\n"
                "- codegen_requirements: copy GateContextBundle.implementation_requirements\n"
                "- codegen_constraints: copy GateContextBundle.implementation_constraints\n"
                "- codegen_validation_focus: copy GateContextBundle.validation_focus\n"
                "Rules:\n"
                "- Do not invent facts not supported by the context; derive required fields from the available context.\n"
                "- overall_score in GateDecision maps to score in AnalysisReport — rename it.\n"
                "- Return JSON only."
            ),
            agent_name="format_checker",
            expected_output="AnalysisReport JSON only.",
            context_task_names=compacted_gate_context + ["gate_controller"],
            output_pydantic_model="AnalysisReport",
            max_input_chars=format_checker_max_input_chars,
        )
    )

    return agent_specs, task_specs, template_vars
