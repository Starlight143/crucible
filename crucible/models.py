from __future__ import annotations

if __package__ == "crucible":
    from .runtime_api import get_runtime
else:
    from runtime_api import get_runtime


_rt = get_runtime()

Experiment = _rt.Experiment
AnalysisReport = _rt.AnalysisReport
GeneratedFile = _rt.GeneratedFile
CodeBundle = _rt.CodeBundle
ReviewIssue = _rt.ReviewIssue
ReviewReport = _rt.ReviewReport
ApiVersionIssue = _rt.ApiVersionIssue
ApiVersionReport = _rt.ApiVersionReport
DirectionOption = _rt.DirectionOption
DirectionDecision = _rt.DirectionDecision
DirectionDebateArtifacts = _rt.DirectionDebateArtifacts
ResearchCitation = _rt.ResearchCitation
EvidenceAuditItem = _rt.EvidenceAuditItem
EvidenceAuditReport = _rt.EvidenceAuditReport
DirectionComparatorItem = _rt.DirectionComparatorItem
DirectionComparatorReport = _rt.DirectionComparatorReport
ResearchLaneReport = _rt.ResearchLaneReport
ClaimAttribution = _rt.ClaimAttribution
DataFieldCapability = _rt.DataFieldCapability
ResearchContext = _rt.ResearchContext
FailureType = _rt.FailureType
ScoreVector = _rt.ScoreVector
BudgetPolicy = _rt.BudgetPolicy
RunSnapshot = _rt.RunSnapshot
RetryPolicy = _rt.RetryPolicy
AgentSpec = _rt.AgentSpec
TaskSpec = _rt.TaskSpec
GateDecision = _rt.GateDecision
ModeConfig = _rt.ModeConfig
AgentCostRecord = _rt.AgentCostRecord
EntryPointSpec = _rt.EntryPointSpec
RuntimeProfileConfig = _rt.RuntimeProfileConfig
LLMProblemBreakdown = _rt.LLMProblemBreakdown
SmartSearchQueries = _rt.SmartSearchQueries

__all__ = [
    "Experiment",
    "AnalysisReport",
    "GeneratedFile",
    "CodeBundle",
    "ReviewIssue",
    "ReviewReport",
    "ApiVersionIssue",
    "ApiVersionReport",
    "DirectionOption",
    "DirectionDecision",
    "DirectionDebateArtifacts",
    "ResearchCitation",
    "EvidenceAuditItem",
    "EvidenceAuditReport",
    "DirectionComparatorItem",
    "DirectionComparatorReport",
    "ResearchLaneReport",
    "ClaimAttribution",
    "DataFieldCapability",
    "ResearchContext",
    "FailureType",
    "ScoreVector",
    "BudgetPolicy",
    "RunSnapshot",
    "RetryPolicy",
    "AgentSpec",
    "TaskSpec",
    "GateDecision",
    "ModeConfig",
    "AgentCostRecord",
    "EntryPointSpec",
    "RuntimeProfileConfig",
    "LLMProblemBreakdown",
    "SmartSearchQueries",
]
