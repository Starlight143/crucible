from __future__ import annotations

if __package__ == "crucible":
    from .runtime_api import get_runtime
else:
    from runtime_api import get_runtime


_rt = get_runtime()

run_librarian_research = _rt.run_librarian_research
run_direction_debate = _rt.run_direction_debate
build_research_swarm_crew = _rt.build_research_swarm_crew
build_direction_debate_crew = _rt.build_direction_debate_crew
build_direction_preamble = _rt.build_direction_preamble
extract_direction_decision = _rt.extract_direction_decision
extract_direction_comparator_report = _rt.extract_direction_comparator_report
extract_evidence_audit_report = _rt.extract_evidence_audit_report
extract_research_context = _rt.extract_research_context

__all__ = [
    "run_librarian_research",
    "run_direction_debate",
    "build_research_swarm_crew",
    "build_direction_debate_crew",
    "build_direction_preamble",
    "extract_direction_decision",
    "extract_direction_comparator_report",
    "extract_evidence_audit_report",
    "extract_research_context",
]
