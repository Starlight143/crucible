from __future__ import annotations

from typing import Any, Dict, List, Tuple


def build_research_swarm_specs(
    *,
    mode_config: Any,
    language_hint: str,
    deps: Dict[str, Any],
) -> Tuple[Dict[str, Any], List[Any], Dict[str, str]]:
    provider_list = ", ".join(deps["LIBRARIAN_SEARCH_PROVIDERS"])
    common_contract = (
        f"Language hint: {language_hint}\n"
        "- Use only evidence explicitly provided in the task context.\n"
        "- Do not invent citations, URLs, providers, tools, risks, or patterns.\n"
        "- When evidence is weak, move the item to unknowns instead of asserting it.\n"
        "- Output JSON only."
    )
    AgentSpec = deps["AgentSpec"]
    TaskSpec = deps["TaskSpec"]
    agent_specs: Dict[str, Any] = {
        "market_research": AgentSpec(
            name="market_research",
            role="Market Research",
            goal="Gather grounded market precedent, workflow pain, and buyer context for the user problem.",
            backstory=(
                f"[Market Research] Focus: {mode_config.name} mode.\n"
                f"Search providers: {provider_list}\n"
                f"- Prioritize user pain, market precedent, and adoption blockers.\n{common_contract}"
            ),
            output_schema_name="ResearchLaneReport",
            cost_weight=2,
        ),
        "technical_research": AgentSpec(
            name="technical_research",
            role="Technical Research",
            goal="Gather grounded architecture patterns, production constraints, and irreversible technical risks.",
            backstory=(
                f"[Technical Research] Focus: {mode_config.research_focus}.\n"
                f"Search providers: {provider_list}\n"
                f"- Prioritize implementation patterns, reliability constraints, and failure modes.\n{common_contract}"
            ),
            output_schema_name="ResearchLaneReport",
            cost_weight=2,
        ),
        "competitor_research": AgentSpec(
            name="competitor_research",
            role="Competitor Research",
            goal="Map grounded competitors, substitutes, open-source alternatives, and positioning evidence.",
            backstory=(
                f"[Competitor Research] Focus: {mode_config.biz_focus}.\n"
                f"Search providers: {provider_list}\n"
                f"- Prioritize incumbents, alternatives, positioning, and workflow substitutes.\n{common_contract}"
            ),
            output_schema_name="ResearchLaneReport",
            cost_weight=2,
        ),
        "research_synthesizer": AgentSpec(
            name="research_synthesizer",
            role="Research Synthesizer",
            goal="Merge lane outputs into a grounded ResearchContext for downstream debate.",
            backstory=(
                f"[Research Synthesizer] Focus: synthesize evidence for {mode_config.name} mode.\n"
                "- Preserve only grounded claims, map them to citations, and downgrade weak claims.\n"
                f"{common_contract}"
            ),
            output_schema_name="ResearchContext",
            parallel_safe=False,
            cost_weight=3,
            depends_on=["market_research", "technical_research", "competitor_research"],
        ),
    }
    prompt_overrides = {
        "market_research": (
            "[Market Research]\n"
            "User problem:\n{user_problem}\n\n"
            "Language hint: {language_hint}\n"
            "Mode: {mode_name}\n"
            "Search providers: {search_provider_list}\n"
            "Problem decomposition:\n{problem_breakdown_json}\n"
            "Lane brief:\n{market_research_brief}\n"
            "Evidence pack:\n{market_research_material}\n\n"
            "=== EVIDENCE EXTRACTION RULES ===\n"
            "1. CLAIM-TO-EVIDENCE MAPPING:\n"
            "   - Every claim in 'findings' MUST have a corresponding citation in the 'citations' list.\n"
            "   - Format: 'Claim: [your claim]. Source: [title/URL from evidence pack].'\n"
            "   - If you cannot find a source, the claim goes to 'unknowns', not 'findings'.\n\n"
            "2. EVIDENCE QUALITY HIERARCHY (prefer higher):\n"
            "   - Tier 1: Official documentation, verified sources, fetched excerpts\n"
            "   - Tier 2: Reputable news sites, established blogs, academic papers\n"
            "   - Tier 3: Forum discussions, user reviews, anecdotal reports\n"
            "   - Tier 4: Marketing materials, vendor claims (use with skepticism)\n\n"
            "3. PROCESSING SEARCH RESULTS:\n"
            "   - Extract concrete facts: names, numbers, dates, specific features\n"
            "   - Identify patterns: recurring pain points, common solutions, market gaps\n"
            "   - Note contradictions: conflicting information across sources\n"
            "   - Flag uncertainty: vague claims, outdated info, single-source claims\n\n"
            "4. WHAT NOT TO DO:\n"
            "   - Do NOT paraphrase search queries as findings\n"
            "   - Do NOT invent market examples, tools, risks, or citations\n"
            "   - Do NOT generalize from single examples without evidence\n"
            "   - Do NOT use marketing language as factual claims\n\n"
            "5. OUTPUT STRUCTURE:\n"
            "   - findings: Evidence-backed insights with inline citations\n"
            "   - unknowns: Legitimate questions without clear evidence\n"
            "   - citations: Full source list with URLs and relevance notes\n"
            "   - Return ResearchLaneReport JSON with lane='market'.\n"
        ),
        "technical_research": (
            "[Technical Research]\n"
            "User problem:\n{user_problem}\n\n"
            "Language hint: {language_hint}\n"
            "Mode: {mode_name}\n"
            "Search providers: {search_provider_list}\n"
            "Problem decomposition:\n{problem_breakdown_json}\n"
            "Lane brief:\n{technical_research_brief}\n"
            "Evidence pack:\n{technical_research_material}\n\n"
            "=== EVIDENCE EXTRACTION RULES ===\n"
            "1. CLAIM-TO-EVIDENCE MAPPING:\n"
            "   - Every technical claim MUST have a corresponding citation.\n"
            "   - Format: 'Claim: [your claim]. Source: [title/URL from evidence pack].'\n"
            "   - Technical patterns must reference actual implementations or documentation.\n\n"
            "2. EVIDENCE QUALITY HIERARCHY (prefer higher):\n"
            "   - Tier 1: Official documentation, source code, verified tutorials\n"
            "   - Tier 2: Stack Overflow accepted answers, technical blogs by experts\n"
            "   - Tier 3: GitHub repos with significant stars, npm packages with usage\n"
            "   - Tier 4: Unverified tutorials, outdated documentation (use with caution)\n\n"
            "3. PROCESSING SEARCH RESULTS:\n"
            "   - Extract implementation details: specific APIs, configurations, versions\n"
            "   - Identify constraints: performance limits, compatibility issues, dependencies\n"
            "   - Note failure modes: common errors, edge cases, production incidents\n"
            "   - Flag deprecation: outdated patterns, unmaintained libraries\n\n"
            "4. TECHNICAL RISK EXTRACTION:\n"
            "   - Irreversible decisions: architecture choices, data migrations\n"
            "   - Security concerns: vulnerabilities, authentication patterns\n"
            "   - Scalability limits: bottlenecks, scaling patterns\n"
            "   - Operational complexity: deployment, monitoring, debugging\n\n"
            "5. OUTPUT STRUCTURE:\n"
            "   - findings: Evidence-backed technical insights with inline citations\n"
            "   - unknowns: Technical questions without clear answers\n"
            "   - citations: Full source list with technical relevance notes\n"
            "   - Return ResearchLaneReport JSON with lane='technical'.\n"
        ),
        "competitor_research": (
            "[Competitor Research]\n"
            "User problem:\n{user_problem}\n\n"
            "Language hint: {language_hint}\n"
            "Mode: {mode_name}\n"
            "Search providers: {search_provider_list}\n"
            "Problem decomposition:\n{problem_breakdown_json}\n"
            "Lane brief:\n{competitor_research_brief}\n"
            "Evidence pack:\n{competitor_research_material}\n\n"
            "=== EVIDENCE EXTRACTION RULES ===\n"
            "1. CLAIM-TO-EVIDENCE MAPPING:\n"
            "   - Every competitor claim MUST have a corresponding citation.\n"
            "   - Format: 'Claim: [your claim]. Source: [title/URL from evidence pack].'\n"
            "   - Pricing, features, market share must reference actual sources.\n\n"
            "2. EVIDENCE QUALITY HIERARCHY (prefer higher):\n"
            "   - Tier 1: Official company pages, verified product listings\n"
            "   - Tier 2: Reputable review sites, comparison articles, analyst reports\n"
            "   - Tier 3: User reviews, forum discussions, social media mentions\n"
            "   - Tier 4: Unverified claims, marketing materials (use with skepticism)\n\n"
            "3. PROCESSING SEARCH RESULTS:\n"
            "   - Extract concrete details: pricing, features, user counts, funding\n"
            "   - Identify positioning: target market, value proposition, differentiation\n"
            "   - Note gaps: missing features, user complaints, underserved segments\n"
            "   - Flag uncertainty: estimated numbers, unverified claims, speculation\n\n"
            "4. COMPETITOR CATEGORIZATION:\n"
            "   - Direct competitors: Same problem, same market\n"
            "   - Adjacent products: Same problem, different market\n"
            "   - Workflow substitutes: Different solution, same outcome\n"
            "   - Open-source alternatives: Free/community alternatives\n\n"
            "5. OUTPUT STRUCTURE:\n"
            "   - findings: Evidence-backed competitive insights with inline citations\n"
            "   - unknowns: Competitive questions without clear evidence\n"
            "   - citations: Full source list with competitive relevance notes\n"
            "   - Return ResearchLaneReport JSON with lane='competitor'.\n"
        ),
        "research_synthesizer": (
            "[Research Synthesizer]\n"
            "User problem:\n{user_problem}\n\n"
            "Language hint: {language_hint}\n"
            "Mode: {mode_name}\n"
            "Search strategy: {search_strategy}\n"
            "Search providers: {search_provider_list}\n"
            "Problem decomposition:\n{problem_breakdown_json}\n"
            "Suggested queries: {suggested_search_queries_json}\n"
            "Provider errors: {provider_errors_json}\n\n"
            "=== SYNTHESIS RULES ===\n"
            "1. GROUNDED VS UNSUPPORTED CLASSIFICATION:\n"
            "   - GROUNDED: Claim has 2+ independent citations OR 1 Tier-1 citation\n"
            "   - WEAKLY SUPPORTED: Claim has 1 lower-tier citation\n"
            "   - UNSUPPORTED: Claim has no citations or only marketing sources\n"
            "   - Only GROUNDED claims go to market_examples, existing_tools, technical_patterns, key_risks\n"
            "   - WEAKLY SUPPORTED goes to unknowns with citation note\n"
            "   - UNSUPPORTED goes to hallucination_flags\n\n"
            "2. CLAIM ATTRIBUTION FORMAT:\n"
            "   - Map each grounded claim to its supporting citations\n"
            "   - Include citation quality tier in attribution\n"
            "   - Note when multiple sources agree or contradict\n"
            "   - Example: 'Claim: X is growing 40% YoY. Sources: [Source1 (Tier 2), Source2 (Tier 1)]'\n\n"
            "3. CROSS-LANE VALIDATION:\n"
            "   - Check if technical patterns align with market needs\n"
            "   - Verify competitor claims across multiple lanes\n"
            "   - Flag contradictions between lanes\n"
            "   - Prioritize claims with multi-lane support\n\n"
            "4. EVIDENCE COVERAGE ASSESSMENT:\n"
            "   - What percentage of key questions have grounded answers?\n"
            "   - Which critical decisions lack evidence?\n"
            "   - What additional research would be most valuable?\n\n"
            "5. OUTPUT REQUIREMENTS:\n"
            "   - synthesized_summary: Coherent narrative of grounded findings only\n"
            "   - claim_attributions: Every claim mapped to citations with quality tier\n"
            "   - evidence_coverage: Quantitative assessment of research completeness\n"
            "   - hallucination_flags: Unsupported claims that were filtered out\n"
            "   - Return ResearchContext JSON only.\n"
        ),
    }
    task_specs: List[Any] = [
        TaskSpec(
            name="market_research",
            description_template=prompt_overrides["market_research"],
            agent_name="market_research",
            expected_output="ResearchLaneReport JSON only.",
            output_pydantic_model="ResearchLaneReport",
        ),
        TaskSpec(
            name="technical_research",
            description_template=prompt_overrides["technical_research"],
            agent_name="technical_research",
            expected_output="ResearchLaneReport JSON only.",
            output_pydantic_model="ResearchLaneReport",
        ),
        TaskSpec(
            name="competitor_research",
            description_template=prompt_overrides["competitor_research"],
            agent_name="competitor_research",
            expected_output="ResearchLaneReport JSON only.",
            output_pydantic_model="ResearchLaneReport",
        ),
        TaskSpec(
            name="research_synthesizer",
            description_template=prompt_overrides["research_synthesizer"],
            agent_name="research_synthesizer",
            expected_output="ResearchContext JSON only.",
            context_task_names=["market_research", "technical_research", "competitor_research"],
            output_pydantic_model="ResearchContext",
        ),
    ]
    template_vars = {
        "mode_name": mode_config.name,
        "language_hint": language_hint,
        "search_provider_list": provider_list,
    }
    return agent_specs, task_specs, template_vars
