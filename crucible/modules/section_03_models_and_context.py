# Auto-generated section module — do not edit manually.
# Regenerate via ``python -m crucible.generate``.
from __future__ import annotations

from . import section_00_bootstrap_and_utils as _prev_00

globals().update({k: v for k, v in _prev_00.__dict__.items() if not k.startswith("__")})
from . import section_01_extraction_and_reformat as _prev_01

globals().update({k: v for k, v in _prev_01.__dict__.items() if not k.startswith("__")})
from . import section_02_research_and_llm as _prev_02

globals().update({k: v for k, v in _prev_02.__dict__.items() if not k.startswith("__")})


class Experiment(BaseModel):
    goal: str = Field(..., description="驗證目標")
    criteria: str = Field(..., description="成功判準")


class AnalysisReport(BaseModel):
    project_name: str = Field(..., description="Project name, ideally concise and deployment-safe")
    summary: str = Field(..., description="Short analysis summary")
    consensus: str = Field(..., description="Main cross-agent consensus")
    disagreement: str = Field(
        ..., description="Main disagreement, uncertainty, or unresolved tradeoff"
    )
    experiments: List[Experiment] = Field(..., description="Experiment list")
    score: int = Field(..., description="Overall score from 0 to 100")
    mode_used: str = Field(..., description="Mode used: Quant, SaaS, Agent, or Scientist")
    risk_level: str = Field(..., description="Risk level: Low, Medium, or High")
    analyst_findings: Dict[str, str] = Field(
        default_factory=dict,
        description="Preserved analyst outputs keyed by role so downstream stages do not lose implementation detail",
    )
    gate_context_snapshot: Dict[str, Any] = Field(
        default_factory=dict,
        description="Structured snapshot of GateDecision fields preserved for downstream stages",
    )
    codegen_handoff_summary: str = Field(
        default="",
        description="Format Checker organized implementation brief for CodeGen",
    )
    codegen_requirements: List[str] = Field(
        default_factory=list,
        description="Concrete implementation requirements preserved from analyst and gate context",
    )
    codegen_constraints: List[str] = Field(
        default_factory=list,
        description="Non-negotiable constraints or prohibitions preserved from analyst and gate context",
    )
    codegen_validation_focus: List[str] = Field(
        default_factory=list,
        description="Checks and validation points CodeGen and quality review should preserve",
    )
    schema_version: int = Field(
        default=1,
        description="Schema version for forward-compatible loading; bump when fields are added or removed",
    )


def load_analysis_report_safe(
    path: str,
) -> Optional["AnalysisReport"]:
    """
    Load an :class:`AnalysisReport` from a JSON file with graceful degradation.

    Handles:

    * Missing file — returns ``None``.
    * Malformed JSON — returns ``None`` (warns via ``warnings.warn``).
    * Missing required fields from older schema versions (e.g. files written
      before ``schema_version`` existed) — fills defaults and returns a
      partially populated report rather than raising.
    * Extra unknown fields from future schema versions — ignored safely by
      Pydantic (they are stripped without error).

    Parameters
    ----------
    path:
        Absolute or relative path to the ``analysis_result.json`` file.

    Returns
    -------
    AnalysisReport | None
        The loaded report, or ``None`` if the file cannot be read or parsed.
    """
    import json as _json
    import warnings as _warnings

    if not os.path.isfile(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as _fh:
            raw = _json.load(_fh)
    except (OSError, _json.JSONDecodeError) as exc:
        _warnings.warn(
            f"load_analysis_report_safe: could not read '{path}': {exc}",
            RuntimeWarning,
            stacklevel=2,
        )
        return None
    if not isinstance(raw, dict):
        return None

    # ── Backward-compatibility defaults for older schema files ──────────────
    # Fields added in schema_version >= 2 that may be absent in v1 files.
    _COMPAT_DEFAULTS: Dict[str, Any] = {
        "schema_version": 1,
        "analyst_findings": {},
        "gate_context_snapshot": {},
        "codegen_handoff_summary": "",
        "codegen_requirements": [],
        "codegen_constraints": [],
        "codegen_validation_focus": [],
    }
    # Only inject defaults for keys that are truly absent so that explicit
    # None values (written intentionally) are not overridden.
    for key, default_val in _COMPAT_DEFAULTS.items():
        if key not in raw:
            raw[key] = default_val

    try:
        return AnalysisReport(**raw)
    except Exception as exc:
        _warnings.warn(
            f"load_analysis_report_safe: validation error loading '{path}': {exc}",
            RuntimeWarning,
            stacklevel=2,
        )
        # Last-resort: strip unknown fields and retry with only known keys.
        # model_fields is Pydantic v2; __fields__ is Pydantic v1.
        if hasattr(AnalysisReport, "model_fields"):
            known_keys = set(AnalysisReport.model_fields.keys())
        else:  # Pydantic v1 fallback
            known_keys = set(AnalysisReport.__fields__.keys())  # type: ignore[attr-defined]
        filtered = {k: v for k, v in raw.items() if k in known_keys}
        dropped = set(raw.keys()) - known_keys
        if dropped:
            _warnings.warn(
                f"load_analysis_report_safe: dropped unrecognised fields {sorted(dropped)} "
                f"from '{path}' during fallback load",
                RuntimeWarning,
                stacklevel=2,
            )
        for key, default_val in _COMPAT_DEFAULTS.items():
            if key not in filtered:
                filtered[key] = default_val
        try:
            return AnalysisReport(**filtered)
        except Exception as exc2:
            _warnings.warn(
                f"load_analysis_report_safe: final fallback also failed for '{path}': {exc2}",
                RuntimeWarning,
                stacklevel=2,
            )
            return None


class GateContextBundle(BaseModel):
    executive_summary: str = Field(
        ...,
        description="Compact cross-role handoff summary for Gate Controller",
    )
    analyst_findings: Dict[str, str] = Field(
        default_factory=dict,
        description="Compact but implementation-complete findings keyed by analyst role",
    )
    implementation_requirements: List[str] = Field(
        default_factory=list,
        description="Concrete implementation details that must be preserved downstream",
    )
    implementation_constraints: List[str] = Field(
        default_factory=list,
        description="Non-negotiable prohibitions, boundaries, or risk controls",
    )
    validation_focus: List[str] = Field(
        default_factory=list,
        description="Checks, metrics, and experiments that later stages must preserve",
    )
    blocking_unknowns: List[str] = Field(
        default_factory=list,
        description="Unknowns or evidence gaps that could block production-grade codegen",
    )
    rerun_signals: Dict[str, List[str]] = Field(
        default_factory=dict,
        description="Per-role reasons that may justify rerunning a specific analyst",
    )


def _normalize_analysis_report(
    analysis_report: Optional["AnalysisReport"],
    *,
    mode: Optional[str] = None,
) -> Optional["AnalysisReport"]:
    if analysis_report is None:
        return None

    analysis_report.project_name = str(analysis_report.project_name or "").strip()
    analysis_report.summary = str(analysis_report.summary or "").strip()
    analysis_report.consensus = str(analysis_report.consensus or "").strip()
    analysis_report.disagreement = str(analysis_report.disagreement or "").strip()
    normalized_mode = str(analysis_report.mode_used or "").strip()
    canonical_mode_map = {
        "quant": "Quant",
        "saas": "SaaS",
        "agent": "Agent",
        "scientist": "Scientist",
    }
    canonical_mode = canonical_mode_map.get(normalized_mode.lower())
    if canonical_mode is None:
        return None
    expected_mode = canonical_mode_map.get(str(mode or "").strip().lower())
    if expected_mode is not None and canonical_mode != expected_mode:
        return None
    analysis_report.mode_used = canonical_mode

    risk_value = str(analysis_report.risk_level or "").strip().lower()
    risk_map = {"low": "Low", "medium": "Medium", "high": "High"}
    if risk_value in risk_map:
        analysis_report.risk_level = risk_map[risk_value]

    normalized_findings: Dict[str, str] = {}
    for raw_key, raw_value in dict(analysis_report.analyst_findings or {}).items():
        key = str(raw_key or "").strip()
        value = str(raw_value or "").strip()
        if key and value:
            normalized_findings[key] = value
    analysis_report.analyst_findings = normalized_findings

    gate_snapshot = analysis_report.gate_context_snapshot or {}
    analysis_report.gate_context_snapshot = (
        dict(gate_snapshot) if isinstance(gate_snapshot, dict) else {}
    )

    analysis_report.codegen_handoff_summary = str(
        analysis_report.codegen_handoff_summary or ""
    ).strip()
    analysis_report.codegen_requirements = _normalize_text_list(
        analysis_report.codegen_requirements
    )
    analysis_report.codegen_constraints = _normalize_text_list(
        analysis_report.codegen_constraints
    )
    analysis_report.codegen_validation_focus = _normalize_text_list(
        analysis_report.codegen_validation_focus
    )
    return analysis_report


class GeneratedFile(BaseModel):
    path: str = Field(
        ..., description='Relative path under generated code root (no leading "code/").'
    )
    content: str = Field(..., description="Full file content")


class CodeBundle(BaseModel):
    project_type: str = Field(..., description="saas, quant, agent, or scientist")
    files: List[GeneratedFile]


class ReviewIssue(BaseModel):
    severity: str = Field(..., description="low|medium|high")
    category: str = Field(
        ..., description="requirements|logic|bug|security|performance|usability|other"
    )
    description: str = Field(..., description="Issue description")
    file: Optional[str] = Field(None, description="Optional affected file path")
    suggestion: Optional[str] = Field(None, description="Suggested fix or mitigation")


# v1.0.5 round 3 (P2-11 strict): allowed values for ReviewReport.failure_type.
# Every value MUST be a member of FailureType (defined later in this file) and
# MUST have a banner branch wired up in section_07. Adding a new value
# requires updates in three places:
#   1. add the FailureType enum member,
#   2. add it here,
#   3. add the banner copy + handling in section_07_selfcheck_output_main.
# A unit test (test_failure_type_enum_drift.py) asserts the three lists stay
# in sync, so a typo at any callsite raises ValueError at write time rather
# than silently dropping the structured signal.
_REVIEW_REPORT_ALLOWED_FAILURE_TYPES: "frozenset[str]" = frozenset({
    "QUALITY_LOOP_GAVE_UP",
})


def _coerce_review_failure_type(value: Any) -> Optional[str]:
    """Normalise + validate the value being assigned to ReviewReport.failure_type.

    Accepts ``None``, an empty string (treated as None), an Enum whose
    ``.value`` is a known marker, or a string equal (case-insensitively after
    strip) to a known marker. Anything else raises ``ValueError`` so typos
    (e.g. ``"QUALITY_LOOP_GIVE_UP"``) fail loudly at the write site instead of
    silently dropping the observability signal.
    """
    if value is None:
        return None
    if hasattr(value, "value") and not isinstance(value, str):
        # Enum instance — recurse on its .value so we share the same path.
        return _coerce_review_failure_type(value.value)
    if isinstance(value, str):
        normalized = value.strip().upper()
        if not normalized:
            return None
        if normalized not in _REVIEW_REPORT_ALLOWED_FAILURE_TYPES:
            raise ValueError(
                "ReviewReport.failure_type must be one of "
                f"{sorted(_REVIEW_REPORT_ALLOWED_FAILURE_TYPES)} or None; "
                f"got {value!r}. Add the new value to "
                "_REVIEW_REPORT_ALLOWED_FAILURE_TYPES and to FailureType, "
                "and wire a banner branch in section_07."
            )
        return normalized
    raise TypeError(
        "ReviewReport.failure_type must be str | Enum | None, "
        f"got {type(value).__name__}"
    )


class ReviewReport(BaseModel):
    passes: bool = Field(..., description="Whether the review passed")
    summary: str = Field("", description="Review summary")
    issues: List[ReviewIssue] = Field(..., description="Review findings")
    # v1.0.5 round 2 (P2-11 structural): structured failure_type so observability
    # tools (agent_metrics, multi_project_compare, run_diff) can read the
    # quality-loop outcome without grepping the summary string. Defaults to
    # None for normal converged runs; set to FailureType.QUALITY_LOOP_GAVE_UP
    # by run_quality_loop's stagnation early-stop path.
    # v1.0.5 round 3 (strict): a model_validator below now coerces + validates
    # every assignment against _REVIEW_REPORT_ALLOWED_FAILURE_TYPES, so typos
    # raise ValueError at write time. The substring fallback in section_07's
    # banner detection has been removed — consumers MUST go through the
    # structured field.
    failure_type: Optional[str] = Field(
        default=None,
        description="Structured failure_type marker (e.g. 'QUALITY_LOOP_GAVE_UP'); None for normal review outcomes.",
    )

    if _HAS_PYDANTIC_V2_MODEL_VALIDATOR:

        @model_validator(mode="after")
        def _validate_failure_type(self) -> "ReviewReport":
            self.failure_type = _coerce_review_failure_type(self.failure_type)
            return self
    else:  # pragma: no cover - exercised only on Pydantic v1

        @root_validator(skip_on_failure=True)
        def _validate_failure_type(cls, values: Dict[str, Any]) -> Dict[str, Any]:
            values["failure_type"] = _coerce_review_failure_type(values.get("failure_type"))
            return values


# =========================
# 1.4) API Version Checker Schemas (v14)
# =========================


class ApiVersionIssue(BaseModel):
    """Single API version issue detected in generated code."""

    library: str = Field(..., description="Library name (e.g., 'fastapi', 'pandas')")
    detected_version_hint: Optional[str] = Field(
        None, description="Version hint detected in code comments or imports"
    )
    latest_version: Optional[str] = Field(
        None, description="Latest version from documentation search"
    )
    is_deprecated: bool = Field(False, description="Whether the detected API usage is deprecated")
    deprecated_api: Optional[str] = Field(None, description="The deprecated API/method name")
    recommended_api: Optional[str] = Field(
        None, description="The recommended replacement API/method"
    )
    severity: str = Field("medium", description="Issue severity: low|medium|high")
    file: Optional[str] = Field(None, description="File path where issue was detected")
    line_hint: Optional[str] = Field(None, description="Line or code snippet hint")
    description: str = Field("", description="Human-readable description of the issue")
    suggestion: str = Field("", description="Suggested fix or migration path")
    citation_url: Optional[str] = Field(
        None, description="URL to documentation supporting this finding"
    )


class ApiVersionReport(BaseModel):
    """Report from API version checking."""

    needs_update: bool = Field(False, description="Whether any outdated API usage was detected")
    issues: List[ApiVersionIssue] = Field(
        default_factory=list, description="List of detected API version issues"
    )
    checked_libraries: List[str] = Field(
        default_factory=list, description="Libraries that were checked"
    )
    skipped_libraries: List[str] = Field(
        default_factory=list, description="Libraries skipped (not in high-risk list)"
    )
    search_timestamp: str = Field(
        default_factory=lambda: datetime.now().isoformat(),
        description="When the check was performed",
    )
    cache_hits: int = Field(0, description="Number of cache hits")
    confidence: str = Field(
        "medium", description="Overall confidence in the report: low|medium|high"
    )
    summary: str = Field("", description="Summary of findings")


class DirectionOption(BaseModel):
    key: str = Field(..., description='Direction key "A" | "B" | "C" | "D" | "E" | "F" | "G"')
    name: str = Field(..., description="Direction name")
    thesis: str = Field(..., description="Option thesis")
    primary_metric: str = Field(..., description="Primary metric")
    fastest_test: str = Field(..., description="Fastest validation test")
    major_risk: str = Field(..., description="Major risk")


class DirectionSeedIdea(BaseModel):
    label: str = Field(..., description="Short label for the rough direction")
    thesis: str = Field(..., description="Provisional direction thesis before research")
    why_now: str = Field(default="", description="Why this direction is worth checking now")
    search_terms: List[str] = Field(
        default_factory=list,
        description="Search terms that Librarian should use to validate this direction",
    )


class DirectionSeedPlan(BaseModel):
    summary: str = Field(..., description="High-level summary of the provisional directions")
    directions: List[DirectionSeedIdea] = Field(
        default_factory=list,
        description="Three to five rough directions to validate before final direction selection",
    )


class DirectionDecision(BaseModel):
    selected_direction: str = Field(
        ...,
        description='Selected direction "A" | "B" | "C" | "D" | "E" | "F" | "G" | "none"',
    )
    summary: str = Field(..., description="Decision summary")
    options: List[DirectionOption] = Field(
        ..., description="Exactly seven mutually exclusive options keyed A through G"
    )
    backup_candidates: List[str] = Field(
        default_factory=list,
        description="Ordered fallback direction keys excluding the selected direction",
    )
    go_conditions: List[str] = Field(
        ..., description="Conditions that must hold before proceeding, up to five"
    )
    kill_criteria: List[str] = Field(
        ..., description="Conditions that should stop the direction, up to five"
    )
    confidence: str = Field(..., description='Confidence level "low" | "medium" | "high"')
    verify_plan: List[str] = Field(..., description="Concrete validation plan items, up to five")


class DirectionDebateArtifacts(BaseModel):
    comparator_report: Optional["DirectionComparatorReport"] = Field(
        default=None,
        description="Structured comparator funnel report for the debate run",
    )
    audit_report: Optional["EvidenceAuditReport"] = Field(
        default=None,
        description="Structured evidence audit report for the debate run",
    )


class ResearchCitation(BaseModel):
    provider: str = Field(..., description="Evidence provider name")
    title: str = Field(..., description="Source title")
    url: str = Field(..., description="Canonical source URL")
    snippet: str = Field(default="", description="Short relevant excerpt")
    query: str = Field(default="", description="Search query that produced this citation")
    source_domain: str = Field(default="", description="Normalized source domain")
    snippet_hash: str = Field(default="", description="Stable hash of the current snippet")
    verification_status: str = Field(
        default="search_snippet",
        description="search_snippet|fetched_excerpt|metadata_only|unverified",
    )
    evidence_type: str = Field(
        default="web_result",
        description="repo_search|code_search|paper|docs|site_search|discovery_only|web_result",
    )


class EvidenceAuditItem(BaseModel):
    key: str = Field(..., description='Direction key "A" | "B" | "C" | "D" | "E" | "F" | "G"')
    evidence_score: int = Field(
        default=0, ge=0, description="Weighted evidence score for the option"
    )
    supported_fields: List[str] = Field(
        default_factory=list,
        description="Option fields backed by claim_attributions/citations",
    )
    summary_only_fields: List[str] = Field(
        default_factory=list,
        description="Fields supported only by summary-level narrative",
    )
    unsupported_fields: List[str] = Field(
        default_factory=list, description="Fields without clear support"
    )
    unsupported_count: int = Field(default=0, ge=0, description="Count of unsupported_fields")
    decision_critical_unknowns: List[str] = Field(
        default_factory=list,
        description="Unknowns that materially affect comparison for this option",
    )


_REPORT_DIRECTION_KEYS: Tuple[str, ...] = ("A", "B", "C", "D", "E", "F", "G")


def _canonical_report_direction_key(value: Any) -> str:
    key = str(value or "").strip().upper()
    return key if key in _REPORT_DIRECTION_KEYS else ""


def _normalize_report_top_keys(
    raw_top_keys: Optional[List[Any]],
    fallback_keys: Optional[List[str]],
    *,
    limit: int,
) -> List[str]:
    normalized: List[str] = []
    seen: Set[str] = set()
    for raw in list(raw_top_keys or []) + list(fallback_keys or []):
        key = _canonical_report_direction_key(raw)
        if not key or key in seen:
            continue
        seen.add(key)
        normalized.append(key)
        if limit is not None and len(normalized) >= max(0, int(limit)):
            break
    return normalized


def _normalize_evidence_audit_items(
    items: Optional[List[EvidenceAuditItem]],
) -> List[EvidenceAuditItem]:
    best_by_key: Dict[str, EvidenceAuditItem] = {}
    for item in list(items or []):
        key = _canonical_report_direction_key(getattr(item, "key", ""))
        if not key:
            continue
        item.key = key
        item.supported_fields = _normalize_text_list(getattr(item, "supported_fields", []) or [])
        item.summary_only_fields = _normalize_text_list(
            getattr(item, "summary_only_fields", []) or []
        )
        item.unsupported_fields = _normalize_text_list(
            getattr(item, "unsupported_fields", []) or []
        )
        item.decision_critical_unknowns = _normalize_text_list(
            getattr(item, "decision_critical_unknowns", []) or []
        )
        item.evidence_score = max(0, int(getattr(item, "evidence_score", 0) or 0))
        item.unsupported_count = max(
            len(item.unsupported_fields),
            int(getattr(item, "unsupported_count", 0) or 0),
        )
        existing = best_by_key.get(key)
        current_rank = (item.evidence_score, -item.unsupported_count)
        if existing is None or current_rank > (
            existing.evidence_score,
            -existing.unsupported_count,
        ):
            best_by_key[key] = item
    return [best_by_key[key] for key in _REPORT_DIRECTION_KEYS if key in best_by_key]


def _normalize_evidence_audit_report_instance(
    report: "EvidenceAuditReport",
) -> "EvidenceAuditReport":
    items = _normalize_evidence_audit_items(report.items or [])
    report.items = items
    fallback_keys = [
        item.key
        for item in sorted(
            items,
            key=lambda item: (-item.evidence_score, item.unsupported_count, item.key),
        )
    ]
    report.top_keys = _normalize_report_top_keys(report.top_keys, fallback_keys, limit=3)
    report.global_warnings = _normalize_text_list(report.global_warnings or [])
    return report


class EvidenceAuditReport(BaseModel):
    items: List[EvidenceAuditItem] = Field(
        default_factory=list, description="Per-option evidence scorecard for A-G"
    )
    top_keys: List[str] = Field(default_factory=list, description="Best-supported direction keys")
    global_warnings: List[str] = Field(
        default_factory=list, description="Cross-option evidence warnings"
    )

    if _HAS_PYDANTIC_V2_MODEL_VALIDATOR:

        @model_validator(mode="after")
        def _normalize_report(self) -> "EvidenceAuditReport":
            return _normalize_evidence_audit_report_instance(self)
    else:  # pragma: no cover - exercised only on Pydantic v1

        @root_validator(skip_on_failure=True)
        def _normalize_report(cls, values: Dict[str, Any]) -> Dict[str, Any]:
            report = cls.construct(**values)
            report = _normalize_evidence_audit_report_instance(report)
            return report.dict()


class DirectionComparatorItem(BaseModel):
    key: str = Field(..., description='Direction key "A" | "B" | "C" | "D" | "E" | "F" | "G"')
    feasibility_score: int = Field(default=0, ge=0, le=5, description="Execution feasibility score")
    reversibility_score: int = Field(default=0, ge=0, le=5, description="How reversible the bet is")
    speed_to_test_score: int = Field(
        default=0, ge=0, le=5, description="How quickly the thesis can be validated"
    )
    evidence_strength_score: int = Field(
        default=0, ge=0, le=5, description="Strength of supporting evidence"
    )
    downside_severity_score: int = Field(
        default=0, ge=0, le=5, description="Lower is better: downside severity"
    )
    unresolved_unknown_dependency_score: int = Field(
        default=0,
        ge=0,
        le=5,
        description="Lower is better: reliance on unresolved unknowns",
    )
    composite_score: int = Field(default=0, ge=0, description="Comparator weighted score")
    hard_feasibility_pass: bool = Field(
        default=True, description="Whether the direction passes hard feasibility gate"
    )
    hard_blockers: List[str] = Field(
        default_factory=list, description="Concrete blockers from hard feasibility gate"
    )
    recommended_lane: str = Field(
        default="production", description="production|exploration|conditional"
    )
    rationale: str = Field(default="", description="Short explanation for the ranking")


def _normalize_direction_comparator_items(
    items: Optional[List[DirectionComparatorItem]],
) -> List[DirectionComparatorItem]:
    best_by_key: Dict[str, DirectionComparatorItem] = {}
    for item in list(items or []):
        key = _canonical_report_direction_key(getattr(item, "key", ""))
        if not key:
            continue
        item.key = key
        item.feasibility_score = max(0, min(5, int(getattr(item, "feasibility_score", 0) or 0)))
        item.reversibility_score = max(0, min(5, int(getattr(item, "reversibility_score", 0) or 0)))
        item.speed_to_test_score = max(0, min(5, int(getattr(item, "speed_to_test_score", 0) or 0)))
        item.evidence_strength_score = max(
            0, min(5, int(getattr(item, "evidence_strength_score", 0) or 0))
        )
        item.downside_severity_score = max(
            0, min(5, int(getattr(item, "downside_severity_score", 0) or 0))
        )
        item.unresolved_unknown_dependency_score = max(
            0,
            min(5, int(getattr(item, "unresolved_unknown_dependency_score", 0) or 0)),
        )
        item.composite_score = max(0, int(getattr(item, "composite_score", 0) or 0))
        item.hard_feasibility_pass = bool(getattr(item, "hard_feasibility_pass", True))
        item.hard_blockers = _normalize_text_list(getattr(item, "hard_blockers", []) or [])[:4]
        lane = str(getattr(item, "recommended_lane", "production") or "").strip().lower()
        if lane not in ("production", "exploration", "conditional"):
            lane = "production"
        item.recommended_lane = lane
        item.rationale = str(getattr(item, "rationale", "") or "").strip()
        existing = best_by_key.get(key)
        current_rank = (
            1 if item.hard_feasibility_pass else 0,
            item.composite_score,
            item.evidence_strength_score,
            item.feasibility_score,
            item.speed_to_test_score,
            -item.downside_severity_score,
            -item.unresolved_unknown_dependency_score,
        )
        if existing is None or current_rank > (
            1 if existing.hard_feasibility_pass else 0,
            existing.composite_score,
            existing.evidence_strength_score,
            existing.feasibility_score,
            existing.speed_to_test_score,
            -existing.downside_severity_score,
            -existing.unresolved_unknown_dependency_score,
        ):
            best_by_key[key] = item
    return [best_by_key[key] for key in _REPORT_DIRECTION_KEYS if key in best_by_key]


def _normalize_direction_comparator_report_instance(
    report: "DirectionComparatorReport",
) -> "DirectionComparatorReport":
    items = _normalize_direction_comparator_items(report.items or [])
    report.items = items
    fallback_keys = [
        item.key
        for item in sorted(
            items,
            key=lambda item: (
                -(1 if item.hard_feasibility_pass else 0),
                -item.composite_score,
                -item.evidence_strength_score,
                -item.feasibility_score,
                -item.speed_to_test_score,
                item.downside_severity_score,
                item.unresolved_unknown_dependency_score,
                item.key,
            ),
        )
    ]
    report.top_keys = _normalize_report_top_keys(report.top_keys, fallback_keys, limit=3)
    report.comparison_notes = _normalize_text_list(report.comparison_notes or [])
    return report


class DirectionComparatorReport(BaseModel):
    items: List[DirectionComparatorItem] = Field(
        default_factory=list,
        description="Structured comparison matrix for directions A-G",
    )
    top_keys: List[str] = Field(
        default_factory=list, description="Top three directions after comparator funnel"
    )
    comparison_notes: List[str] = Field(
        default_factory=list, description="Cross-option decision notes"
    )

    if _HAS_PYDANTIC_V2_MODEL_VALIDATOR:

        @model_validator(mode="after")
        def _normalize_report(self) -> "DirectionComparatorReport":
            return _normalize_direction_comparator_report_instance(self)
    else:  # pragma: no cover - exercised only on Pydantic v1

        @root_validator(skip_on_failure=True)
        def _normalize_report(cls, values: Dict[str, Any]) -> Dict[str, Any]:
            report = cls.construct(**values)
            report = _normalize_direction_comparator_report_instance(report)
            return report.dict()


class ResearchLaneReport(BaseModel):
    lane: str = Field(..., description="market|technical|competitor")
    findings: List[str] = Field(default_factory=list, description="Lane-specific findings")
    market_examples: List[str] = Field(default_factory=list, description="Relevant market examples")
    existing_tools: List[str] = Field(
        default_factory=list, description="Existing tools or incumbents"
    )
    technical_patterns: List[str] = Field(
        default_factory=list, description="Implementation patterns"
    )
    key_risks: List[str] = Field(
        default_factory=list, description="Key risks uncovered by this lane"
    )
    unknowns: List[str] = Field(default_factory=list, description="Unresolved questions")
    citations: List[ResearchCitation] = Field(
        default_factory=list, description="Supporting citations"
    )


class ClaimAttribution(BaseModel):
    category: str = Field(..., description="Claim category in ResearchContext")
    claim: str = Field(..., description="Grounded claim text")
    citation_indices: List[int] = Field(
        default_factory=list, description="Indices into ResearchContext.citations"
    )
    citation_urls: List[str] = Field(
        default_factory=list, description="Direct source URLs supporting the claim"
    )
    support_score: int = Field(default=0, ge=0, description="Deterministic support score")


class DataFieldCapability(BaseModel):
    field_name: str = Field(..., description="Canonical research field name")
    tier: str = Field(..., description="Capability tier such as tier_1_core or tier_2_short_window")
    availability_class: str = Field(
        ..., description="stable_long_history|paged_history|short_window|conditional"
    )
    recommended_lane: str = Field(..., description="production|exploration|conditional")
    recommended_horizons: List[str] = Field(
        default_factory=list, description="Recommended research horizons or cycles"
    )
    hard_gate_rule: str = Field(
        default="",
        description="Hard feasibility gate guidance for this field",
    )
    soft_preference_rule: str = Field(
        default="",
        description="Soft ranking or routing guidance for this field",
    )
    notes: str = Field(default="", description="Short operator-facing note")


class ResearchContext(BaseModel):
    user_problem: str = Field(..., description="Original user problem")
    search_strategy: str = Field(..., description="Normalized provider strategy string")
    providers_used: List[str] = Field(
        default_factory=list, description="Providers successfully used"
    )
    suggested_search_queries: List[str] = Field(
        default_factory=list, description="Queries used or recommended"
    )
    market_examples: List[str] = Field(
        default_factory=list, description="Comparable products or workflows"
    )
    existing_tools: List[str] = Field(
        default_factory=list, description="Existing tools and alternatives"
    )
    technical_patterns: List[str] = Field(
        default_factory=list, description="Recommended architecture patterns"
    )
    key_risks: List[str] = Field(
        default_factory=list, description="Irreversible or high-severity risks"
    )
    unknowns: List[str] = Field(
        default_factory=list, description="Open questions that remain unresolved"
    )
    synthesized_summary: str = Field(
        default="", description="Compressed research summary for downstream debate"
    )
    citations: List[ResearchCitation] = Field(
        default_factory=list, description="Evidence citations"
    )
    provider_errors: Dict[str, str] = Field(
        default_factory=dict, description="Provider failures or skips"
    )
    evidence_coverage: Dict[str, int] = Field(
        default_factory=dict, description="Evidence grounding metrics"
    )
    hallucination_flags: List[str] = Field(
        default_factory=list, description="Claims removed or downgraded as unsupported"
    )
    claim_attributions: List[ClaimAttribution] = Field(
        default_factory=list, description="Per-claim citation mapping"
    )
    field_capability_matrix: List[DataFieldCapability] = Field(
        default_factory=list,
        description="Field-level feasibility matrix for quant research and routing",
    )


class FailureType(str, Enum):
    NONE = "NONE"
    JSON_INVALID = "JSON_INVALID"
    EXECUTION_ERROR = "EXECUTION_ERROR"
    LOW_CONFIDENCE = "LOW_CONFIDENCE"
    COST_OVER_BUDGET = "COST_OVER_BUDGET"
    CONFLICTING_OUTPUT = "CONFLICTING_OUTPUT"
    POLICY_VIOLATION = "POLICY_VIOLATION"
    NON_DETERMINISTIC = "NON_DETERMINISTIC"
    # v1.0.5: quality-loop stagnation early stop — emitted when the per-round
    # issue score has not improved for QUALITY_EARLY_STOP_STAGNATION_ROUNDS
    # rounds and the loop bails. The bundle is *not* ready for production use,
    # and snapshots flagged with this value should be surfaced as failures
    # in the saved-project README rather than treated as deliverable.
    QUALITY_LOOP_GAVE_UP = "QUALITY_LOOP_GAVE_UP"


class ScoreVector(BaseModel):
    feasibility: int = Field(default=0, ge=0, le=100)
    risk: int = Field(default=0, ge=0, le=100)
    roi: int = Field(default=0, ge=0, le=100)
    uncertainty: int = Field(default=0, ge=0, le=100)


class BudgetPolicy(BaseModel):
    soft_cost_limit: Optional[float] = Field(default=None, ge=0)
    hard_cost_limit: Optional[float] = Field(default=None, ge=0)
    max_total_tokens: Optional[int] = Field(default=None, ge=0)
    downgrade_on_soft_limit: bool = Field(default=True)
    skip_codegen_on_hard_limit: bool = Field(default=True)
    skip_quality_on_hard_limit: bool = Field(default=True)


class RunSnapshot(BaseModel):
    schema_version: int = Field(default=1)
    run_id: str = Field(...)
    started_at: str = Field(default_factory=lambda: datetime.now().isoformat())
    finished_at: Optional[str] = Field(default=None)
    runtime_profile: str = Field(default="pro")
    mode: Optional[str] = Field(default=None)
    model_versions: Dict[str, str] = Field(default_factory=dict)
    prompt_hashes: Dict[str, str] = Field(default_factory=dict)
    agent_graph: Dict[str, Any] = Field(default_factory=dict)
    inputs: Dict[str, Any] = Field(default_factory=dict)
    outputs: Dict[str, Any] = Field(default_factory=dict)
    gate_decisions: List[Dict[str, Any]] = Field(default_factory=list)
    stage_records: List[Dict[str, Any]] = Field(default_factory=list)
    budget_policy: Dict[str, Any] = Field(default_factory=dict)
    budget_state: Dict[str, Any] = Field(default_factory=dict)
    cost_summary: Dict[str, Any] = Field(default_factory=dict)


# =========================
# 1.5) Agent Contract System (AgentSpec + TaskSpec)
# =========================
# 優化1：將 agent 定義從「寫死流程」變成「可編排流程」
# 允許 selective rerun、parallel execution、A/B testing


class RetryPolicy(BaseModel):
    """Retry policy for agent execution."""

    max_attempts: int = Field(default=20, description="Maximum retry attempts")
    backoff_seconds: float = Field(default=2.0, description="Backoff between retries")
    retry_on_json_fail: bool = Field(default=True, description="Retry when JSON parsing fails")
    retry_on_low_confidence: bool = Field(default=False, description="Retry when confidence is low")


class AgentSpec(BaseModel):
    """
    Agent specification contract.
    Defines the input/output schema and execution properties of an agent.
    """

    name: str = Field(..., description="Agent identifier (e.g., 'research', 'risk', 'arbiter')")
    role: str = Field(..., description="Agent role description")
    goal: str = Field(..., description="Agent goal")
    backstory: str = Field(..., description="Agent backstory/context")
    version: str = Field(default="v1.0.0", description="Semantic behavior version")
    behavior_contract: str = Field(default="", description="Agent behavior contract summary")
    breaking_changes: bool = Field(
        default=False, description="Whether this version includes breaking changes"
    )

    # Execution properties
    cacheable: bool = Field(default=True, description="Whether results can be cached")
    parallel_safe: bool = Field(default=True, description="Can run in parallel with other agents")
    allow_delegation: bool = Field(default=False, description="Can delegate to other agents")
    verbose: bool = Field(default=True, description="Verbose output")

    # Retry policy
    retry_policy: RetryPolicy = Field(
        default_factory=RetryPolicy, description="Retry configuration"
    )

    # Cost weight for priority scheduling (higher = more expensive)
    cost_weight: int = Field(default=1, ge=1, le=10, description="Cost weight 1-10")

    # Dependencies
    depends_on: List[str] = Field(
        default_factory=list, description="List of agent names this depends on"
    )

    # Output schema hint (for JSON extraction)
    output_schema_name: Optional[str] = Field(
        default=None, description="Expected output Pydantic model name"
    )


class TaskSpec(BaseModel):
    """
    Task specification contract.
    Defines the input/output requirements of a task.
    """

    name: str = Field(..., description="Task identifier")
    description_template: str = Field(
        ..., description="Task description template with {placeholders}"
    )
    agent_name: str = Field(..., description="Agent to execute this task")
    expected_output: str = Field(..., description="Expected output description")

    # Context dependencies
    context_task_names: List[str] = Field(
        default_factory=list, description="Tasks whose output is context"
    )

    # Output schema
    output_pydantic_model: Optional[str] = Field(
        default=None, description="Pydantic model for output"
    )

    # Token budget
    max_input_chars: Optional[int] = Field(default=None, description="Max input characters")
    max_output_chars: Optional[int] = Field(default=None, description="Max output characters")


class GateDecision(BaseModel):
    """
    Arbiter Gate Controller decision.
    Extends the original Arbiter role to include control flow decisions.
    """

    # Original Arbiter outputs
    consensus: str = Field(..., description="各角色明確一致的結論")
    disagreement: str = Field(..., description="角色間衝突的假設、判斷或風險認知")
    experiments: List[Experiment] = Field(..., description="Required experiments")

    # Gate Controller additions (internal control flags)
    ready_for_codegen: bool = Field(default=True, description="是否準備好進入 CodeGen 階段")
    blocking_risks: List[str] = Field(
        default_factory=list,
        description="Blocking risks that must be resolved before code generation",
    )
    required_experiments_before_codegen: List[str] = Field(
        default_factory=list, description="CodeGen 前必須完成的實驗"
    )
    advisory_experiments_after_codegen: List[str] = Field(
        default_factory=list,
        description="Non-blocking follow-up experiments that can continue after code generation starts.",
    )
    codegen_scope: str = Field(
        default="production",
        description="Approved code generation scope: production or validation",
    )
    validation_scope_reason: Optional[str] = Field(
        default=None,
        description="Why validation-first code generation is allowed despite remaining unknowns.",
    )
    validation_objectives: List[str] = Field(
        default_factory=list,
        description="Concrete validation/calibration goals that the generated code must implement.",
    )

    # Selective rerun signals
    agents_needing_rerun: List[str] = Field(
        default_factory=list, description="需要重跑的 agent 清單"
    )
    rerun_reasons: Dict[str, str] = Field(
        default_factory=dict, description="每個 agent 需要重跑的原因"
    )

    direction_feedback_needed: bool = Field(
        default=False,
        description="Whether the current gate result should be bounced back into Direction Debate for refinement.",
    )
    direction_feedback_reason: Optional[str] = Field(
        default=None,
        description="Short explanation for why Direction Debate should be revisited before killing the flow.",
    )
    direction_feedback_type: Optional[str] = Field(
        default=None,
        description="Feedback path type: 'evidence' for missing proof/data or 'detail' for missing implementation specifics.",
    )
    direction_feedback_evidence_gaps: List[str] = Field(
        default_factory=list,
        description="Concrete evidence/data/detail gaps that Direction Debate and analysts must resolve.",
    )
    direction_feedback_questions: List[str] = Field(
        default_factory=list,
        description="Concrete follow-up questions for the Direction Debate rerun.",
    )

    # Overall assessment
    overall_score: int = Field(default=0, ge=0, le=100, description="整體評分")
    confidence: str = Field(default="medium", description="整體信心等級")

    score_breakdown: ScoreVector = Field(
        default_factory=ScoreVector,
        description="feasibility/risk/roi/uncertainty score vector",
    )
    failure_type: str = Field(default=FailureType.NONE.value, description="Failure taxonomy type")
    failure_details: Optional[str] = Field(
        default=None, description="Failure details for audit/recovery"
    )

    # Kill signal
    should_kill: bool = Field(default=False, description="Whether the run should be terminated")
    kill_reason: Optional[str] = Field(default=None, description="終止原因")


def _normalize_failure_type(value: Optional[str]) -> str:
    if not value:
        return FailureType.NONE.value
    upper = str(value).strip().upper()
    if upper in FailureType._value2member_map_:
        return upper
    return FailureType.NONE.value


def _classify_runtime_exception_failure(exc: BaseException) -> FailureType:
    text = str(exc or "").strip().lower()
    name = type(exc).__name__.strip().lower()
    combined = f"{name} {text}".strip()

    if any(
        marker in combined
        for marker in (
            "json",
            "schema",
            "pydantic",
            "parse",
            "codebundle",
            "analysisreport",
            "reviewreport",
            "gatedecision",
        )
    ):
        return FailureType.JSON_INVALID
    if any(
        marker in combined
        for marker in (
            "budget",
            "token limit",
            "hard limit",
            "over budget",
            "cost limit",
        )
    ):
        return FailureType.COST_OVER_BUDGET
    if any(
        marker in combined
        for marker in (
            "policy",
            "forbidden",
            "not allowed",
            "kill signal",
            "permission denied",
            "unauthorized",
            "invalid model",
            "model not found",
        )
    ):
        return FailureType.POLICY_VIOLATION
    if any(
        marker in combined
        for marker in (
            "timeout",
            "timed out",
            "connection",
            "network",
            "rate limit",
            "too many requests",
            "service unavailable",
            "temporarily unavailable",
            "overloaded",
            "circuit breaker",
        )
    ):
        return FailureType.NON_DETERMINISTIC
    return FailureType.EXECUTION_ERROR


def _derive_score_vector(overall_score: int, confidence: str) -> ScoreVector:
    score = max(0, min(100, int(overall_score if overall_score is not None else 0)))
    conf = (confidence or "medium").lower()
    uncertainty = max(0, 100 - score)
    if conf == "low":
        uncertainty = max(uncertainty, 70)
    elif conf == "high":
        uncertainty = min(uncertainty, 40)
    return ScoreVector(
        feasibility=score,
        risk=max(0, 100 - score),
        roi=score,
        uncertainty=uncertainty,
    )


def _apply_gate_failure(
    gate_decision: Optional[GateDecision],
    failure_type: FailureType,
    details: Optional[str] = None,
    *,
    overwrite: bool = False,
) -> Optional[GateDecision]:
    if gate_decision is None:
        return None
    current = _normalize_failure_type(getattr(gate_decision, "failure_type", None))
    if overwrite or current == FailureType.NONE.value:
        gate_decision.failure_type = failure_type.value
    if details and (overwrite or not gate_decision.failure_details):
        gate_decision.failure_details = details
    return gate_decision


def _normalize_text_list(values: Optional[List[Any]]) -> List[str]:
    seen: Set[str] = set()
    normalized: List[str] = []
    for raw in values or []:
        text = str(raw or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        normalized.append(text)
    return normalized


def _normalize_codegen_scope(value: Optional[str]) -> str:
    normalized = str(value or "").strip().lower()
    if normalized == "validation":
        return "validation"
    return "production"


def _build_gate_context_snapshot(gate_decision: Optional[GateDecision]) -> Dict[str, Any]:
    if gate_decision is None:
        return {}
    return {
        "consensus": gate_decision.consensus,
        "disagreement": gate_decision.disagreement,
        "experiments": [
            {"goal": exp.goal, "criteria": exp.criteria}
            for exp in list(gate_decision.experiments or [])
        ],
        "ready_for_codegen": gate_decision.ready_for_codegen,
        "blocking_risks": list(gate_decision.blocking_risks or []),
        "required_experiments_before_codegen": list(
            gate_decision.required_experiments_before_codegen or []
        ),
        "advisory_experiments_after_codegen": list(
            gate_decision.advisory_experiments_after_codegen or []
        ),
        "codegen_scope": gate_decision.codegen_scope,
        "validation_scope_reason": gate_decision.validation_scope_reason,
        "validation_objectives": list(gate_decision.validation_objectives or []),
        "agents_needing_rerun": list(gate_decision.agents_needing_rerun or []),
        "rerun_reasons": dict(gate_decision.rerun_reasons or {}),
        "direction_feedback_needed": gate_decision.direction_feedback_needed,
        "direction_feedback_reason": gate_decision.direction_feedback_reason,
        "direction_feedback_type": gate_decision.direction_feedback_type,
        "direction_feedback_evidence_gaps": list(
            gate_decision.direction_feedback_evidence_gaps or []
        ),
        "direction_feedback_questions": list(gate_decision.direction_feedback_questions or []),
        "overall_score": gate_decision.overall_score,
        "confidence": gate_decision.confidence,
        "score_breakdown": (
            gate_decision.score_breakdown.model_dump()
            if hasattr(gate_decision.score_breakdown, "model_dump")
            else dict(gate_decision.score_breakdown or {})
        ),
        "failure_type": gate_decision.failure_type,
        "failure_details": gate_decision.failure_details,
        "should_kill": gate_decision.should_kill,
        "kill_reason": gate_decision.kill_reason,
    }


_VALIDATION_FIRST_REQUEST_MARKERS: Tuple[str, ...] = (
    "phase0",
    "phase 0",
    "validation-first",
    "validation first",
    "validation framework",
    "validation harness",
    "measurement framework",
    "measurement harness",
    "calibration framework",
    "calibration harness",
    "semantic validation",
    "semantics validation",
    "verification framework",
    "驗證框架",
    "驗證流程",
    "驗證系統",
    "先驗證",
    "先做驗證",
    "量測框架",
    "量測流程",
    "校準框架",
    "校準流程",
    "語義驗證",
    "phase0 驗證",
)
_VALIDATION_FIRST_TOPIC_MARKERS: Tuple[str, ...] = (
    "validate",
    "validation",
    "verify",
    "verification",
    "measure",
    "measurement",
    "calibrate",
    "calibration",
    "benchmark",
    "baseline",
    "semantic",
    "semantics",
    "threshold",
    "thresholds",
    "latency",
    "alignment",
    "phase0",
    "phase 0",
    "驗證",
    "驗真",
    "量測",
    "校準",
    "語義",
    "門檻",
    "對齊",
    "延遲",
)
_VALIDATION_SCOPE_DELIVERABLE_MARKERS: Tuple[str, ...] = (
    "framework",
    "harness",
    "pipeline",
    "report",
    "collector",
    "analyzer",
    "comparator",
    "audit",
    "監測",
    "驗證框架",
    "量測框架",
    "報告",
    "收集器",
    "分析器",
)
_VALIDATION_GAP_MARKERS: Tuple[str, ...] = (
    "evidence",
    "citation",
    "proof",
    "data quality",
    "data source",
    "semantic",
    "semantics",
    "threshold",
    "thresholds",
    "parameter",
    "parameters",
    "latency",
    "alignment",
    "calibration",
    "beta",
    "half-life",
    "timestamp",
    "mid-price",
    "證據",
    "資料來源",
    "資料品質",
    "資料不足",
    "細節不足",
    "語義",
    "門檻",
    "參數",
    "延遲",
    "對齊",
    "校準",
    "半衰期",
    "時間戳",
)
_FUNDAMENTAL_BLOCKER_MARKERS: Tuple[str, ...] = (
    "impossible",
    "fundamentally invalid",
    "fundamental contradiction",
    "internally contradictory",
    "unsafe by default",
    "no data source exists",
    "source does not exist",
    "cannot exist",
    "不可行",
    "根本矛盾",
    "核心資料源不存在",
    "資料源不存在",
    "無法取得任何資料",
    "預設不安全",
)


def _mode_project_type_or_none(mode: Optional[str]) -> Optional[str]:
    normalized = str(mode or "").strip()
    if not normalized:
        return None
    _KNOWN_PROJECT_TYPES = {"quant", "saas", "agent", "scientist"}
    lowered = normalized.lower()
    if lowered in _KNOWN_PROJECT_TYPES:
        return lowered
    registry = globals().get("ModeRegistry")
    if registry is None:
        return None
    try:
        exact = registry.get(normalized)
        if exact is not None:
            project_type = str(getattr(exact, "name", "") or "").strip().lower()
            if project_type in _KNOWN_PROJECT_TYPES:
                return project_type
        for _name, cfg in dict(registry.all_modes() or {}).items():
            project_type = str(getattr(cfg, "name", "") or "").strip().lower()
            if project_type == lowered and project_type in _KNOWN_PROJECT_TYPES:
                return project_type
    except Exception:
        return None
    return None


def _text_contains_any_marker(text: str, markers: Tuple[str, ...]) -> bool:
    lowered = str(text or "").strip().lower()
    return bool(lowered) and any(marker in lowered for marker in markers)


def _count_text_markers(text: str, markers: Tuple[str, ...]) -> int:
    lowered = str(text or "").strip().lower()
    if not lowered:
        return 0
    return sum(1 for marker in markers if marker in lowered)


def _is_validation_first_problem(user_problem: Optional[str], *, mode: Optional[str]) -> bool:
    text = str(user_problem or "").strip()
    if not text:
        return False
    project_type = _mode_project_type_or_none(mode)
    if project_type not in {"quant", "saas", "agent", "scientist"}:
        return False
    if _text_contains_any_marker(text, _VALIDATION_FIRST_REQUEST_MARKERS):
        return True
    return (
        _count_text_markers(text, _VALIDATION_FIRST_TOPIC_MARKERS) >= 2
        and _count_text_markers(text, _VALIDATION_SCOPE_DELIVERABLE_MARKERS) >= 1
    )


def _gate_is_validation_scope(gate_decision: Optional[GateDecision]) -> bool:
    return (
        gate_decision is not None
        and _normalize_codegen_scope(getattr(gate_decision, "codegen_scope", None))
        == "validation"
    )


def _gate_allows_low_confidence_codegen(gate_decision: Optional[GateDecision]) -> bool:
    return bool(
        gate_decision is not None
        and gate_decision.ready_for_codegen
        and not gate_decision.should_kill
        and _gate_is_validation_scope(gate_decision)
    )


def _validation_scope_guardrails() -> List[str]:
    return [
        "Only generate validation/calibration/measurement scaffolding, not final production strategy logic.",
        "Keep thresholds, assumptions, and scoring rules configurable until empirical validation completes.",
        "Produce machine-readable metrics, logs, and reports that make the unresolved assumptions falsifiable.",
    ]


def _collect_validation_scope_objectives(
    gate_decision: Optional[GateDecision],
) -> List[str]:
    if gate_decision is None:
        return []
    raw_items: List[str] = []
    raw_items.extend(list(getattr(gate_decision, "required_experiments_before_codegen", []) or []))
    raw_items.extend(list(getattr(gate_decision, "blocking_risks", []) or []))
    raw_items.extend(list(getattr(gate_decision, "direction_feedback_evidence_gaps", []) or []))
    raw_items.extend(list(getattr(gate_decision, "direction_feedback_questions", []) or []))
    raw_items.extend(list((getattr(gate_decision, "rerun_reasons", {}) or {}).values()))
    raw_items.extend(list(getattr(gate_decision, "advisory_experiments_after_codegen", []) or []))
    for experiment in list(getattr(gate_decision, "experiments", []) or []):
        goal = str(getattr(experiment, "goal", "") or "").strip()
        criteria = str(getattr(experiment, "criteria", "") or "").strip()
        if goal and criteria:
            raw_items.append(f"{goal} ({criteria})")
        elif goal:
            raw_items.append(goal)
    objectives = _normalize_text_list(raw_items)
    if objectives:
        return objectives[:6]
    return [
        "Build a validation harness that measures the unresolved assumptions before production implementation."
    ]


def _gate_has_fundamental_blockers(gate_decision: Optional[GateDecision]) -> bool:
    if gate_decision is None:
        return False
    blocker_text = " ".join(
        [
            str(getattr(gate_decision, "disagreement", "") or ""),
            str(getattr(gate_decision, "failure_details", "") or ""),
            str(getattr(gate_decision, "kill_reason", "") or ""),
            " ".join(list(getattr(gate_decision, "blocking_risks", []) or [])),
        ]
    )
    return _text_contains_any_marker(blocker_text, _FUNDAMENTAL_BLOCKER_MARKERS)


def _gate_has_validation_gaps(gate_decision: Optional[GateDecision]) -> bool:
    if gate_decision is None:
        return False
    gap_text = " ".join(
        [
            str(getattr(gate_decision, "disagreement", "") or ""),
            str(getattr(gate_decision, "failure_details", "") or ""),
            str(getattr(gate_decision, "direction_feedback_reason", "") or ""),
            " ".join(list(getattr(gate_decision, "blocking_risks", []) or [])),
            " ".join(list(getattr(gate_decision, "required_experiments_before_codegen", []) or [])),
            " ".join(list(getattr(gate_decision, "direction_feedback_evidence_gaps", []) or [])),
            " ".join(list(getattr(gate_decision, "direction_feedback_questions", []) or [])),
            " ".join(list((getattr(gate_decision, "rerun_reasons", {}) or {}).values())),
        ]
    )
    return _text_contains_any_marker(gap_text, _VALIDATION_GAP_MARKERS)


def _build_validation_scope_reason(
    gate_decision: Optional[GateDecision],
) -> str:
    if gate_decision is None:
        return (
            "Validation-first scope approved because the request is to measure unresolved assumptions before production implementation."
        )
    candidate = (
        str(getattr(gate_decision, "direction_feedback_reason", "") or "").strip()
        or str(getattr(gate_decision, "failure_details", "") or "").strip()
    )
    if not candidate:
        candidate = next(
            (
                item
                for item in (
                    list(getattr(gate_decision, "blocking_risks", []) or [])
                    + list(getattr(gate_decision, "required_experiments_before_codegen", []) or [])
                )
                if str(item or "").strip()
            ),
            "",
        )
    candidate = str(candidate or "").strip()
    if candidate:
        return (
            "Validation-first scope approved because the remaining uncertainty is exactly what the generated harness should measure: "
            + candidate
        )
    return (
        "Validation-first scope approved because the request is to measure unresolved assumptions before production implementation."
    )


def _promote_validation_first_gate(
    gate_decision: Optional[GateDecision],
    *,
    user_problem: Optional[str],
    mode: Optional[str],
) -> Optional[GateDecision]:
    if gate_decision is None:
        return None
    if not _is_validation_first_problem(user_problem, mode=mode):
        return _normalize_gate_decision(gate_decision, mode=mode)
    promoted = gate_decision
    promoted.codegen_scope = "validation"
    promoted.validation_objectives = _collect_validation_scope_objectives(promoted)
    promoted.validation_scope_reason = _build_validation_scope_reason(promoted)
    if promoted.should_kill or _gate_has_fundamental_blockers(promoted):
        return _normalize_gate_decision(promoted, mode=mode)
    if _gate_allows_low_confidence_codegen(promoted):
        promoted.failure_type = FailureType.NONE.value
        promoted.failure_details = None
    if (
        not promoted.ready_for_codegen
        and (
            promoted.blocking_risks
            or promoted.required_experiments_before_codegen
            or promoted.direction_feedback_needed
            or promoted.confidence == "low"
            or _gate_has_validation_gaps(promoted)
        )
    ):
        promoted.ready_for_codegen = True
        promoted.blocking_risks = []
        promoted.required_experiments_before_codegen = []
        promoted.direction_feedback_needed = False
        promoted.direction_feedback_reason = None
        promoted.direction_feedback_type = None
        promoted.direction_feedback_evidence_gaps = []
        promoted.direction_feedback_questions = []
        promoted.agents_needing_rerun = []
        promoted.rerun_reasons = {}
        promoted.failure_type = FailureType.NONE.value
        promoted.failure_details = None
    promoted.advisory_experiments_after_codegen = _normalize_text_list(
        list(promoted.advisory_experiments_after_codegen or [])
        + list(promoted.validation_objectives or [])
    )
    return _normalize_gate_decision(promoted, mode=mode)


def _align_analysis_report_with_gate_scope(
    analysis_report: Optional["AnalysisReport"],
    gate_decision: Optional[GateDecision],
) -> Optional["AnalysisReport"]:
    if analysis_report is None:
        return None
    if gate_decision is not None:
        analysis_report.gate_context_snapshot = _build_gate_context_snapshot(gate_decision)
    if not _gate_is_validation_scope(gate_decision):
        return analysis_report
    objectives = _normalize_text_list(
        list(getattr(gate_decision, "validation_objectives", []) or [])
        + list(getattr(analysis_report, "codegen_validation_focus", []) or [])
    )
    analysis_report.codegen_handoff_summary = (
        "Validation-first scope only. Build a reversible validation/calibration/measurement harness instead of final production strategy logic. "
        + str(getattr(gate_decision, "validation_scope_reason", "") or "").strip()
    ).strip()
    analysis_report.codegen_requirements = _normalize_text_list(
        list(getattr(analysis_report, "codegen_requirements", []) or [])
        + objectives
    )
    analysis_report.codegen_constraints = _normalize_text_list(
        list(getattr(analysis_report, "codegen_constraints", []) or [])
        + _validation_scope_guardrails()
    )
    analysis_report.codegen_validation_focus = _normalize_text_list(
        objectives
        + [
            "Validation outputs must directly measure the unresolved assumptions that blocked production codegen.",
        ]
    )
    return analysis_report


_DIRECTION_OPTION_KEYS: Tuple[str, ...] = ("A", "B", "C", "D", "E", "F", "G")


def _normalize_direction_key_list(
    raw_keys: Optional[List[Any]],
    *,
    exclude: Optional[Set[str]] = None,
    limit: Optional[int] = None,
) -> List[str]:
    exclude_keys = {str(item or "").strip().upper() for item in list(exclude or set())}
    normalized: List[str] = []
    for raw in list(raw_keys or []):
        key = str(raw or "").strip().upper()
        if key == "NONE" or key not in _DIRECTION_OPTION_KEYS or key in exclude_keys:
            continue
        if key not in normalized:
            normalized.append(key)
        if limit is not None and len(normalized) >= limit:
            break
    return normalized


def _normalize_direction_decision(
    decision: Optional[DirectionDecision],
) -> Optional[DirectionDecision]:
    if decision is None:
        return None
    selected = str(decision.selected_direction or "").strip().upper()
    if selected == "NONE":
        decision.selected_direction = "none"
    elif selected in _DIRECTION_OPTION_KEYS:
        decision.selected_direction = selected
    else:
        return None

    decision.summary = str(decision.summary or "").strip()
    if not decision.summary:
        return None

    normalized_options: Dict[str, DirectionOption] = {}
    for option in decision.options or []:
        key = str(getattr(option, "key", "") or "").strip().upper()
        if key not in _DIRECTION_OPTION_KEYS or key in normalized_options:
            return None
        option.key = key
        option.name = str(option.name or "").strip()
        option.thesis = str(option.thesis or "").strip()
        option.primary_metric = str(option.primary_metric or "").strip()
        option.fastest_test = str(option.fastest_test or "").strip()
        option.major_risk = str(option.major_risk or "").strip()
        if not all(
            [
                option.name,
                option.thesis,
                option.primary_metric,
                option.fastest_test,
                option.major_risk,
            ]
        ):
            return None
        normalized_options[key] = option

    if tuple(sorted(normalized_options.keys())) != _DIRECTION_OPTION_KEYS:
        return None
    decision.options = [normalized_options[key] for key in _DIRECTION_OPTION_KEYS]
    decision.backup_candidates = _normalize_direction_key_list(
        getattr(decision, "backup_candidates", []) or [],
        exclude={decision.selected_direction},
        limit=2,
    )

    decision.go_conditions = _normalize_text_list(decision.go_conditions)[:5]
    decision.kill_criteria = _normalize_text_list(decision.kill_criteria)[:5]
    decision.verify_plan = _normalize_text_list(decision.verify_plan)[:5]
    if not decision.go_conditions or not decision.kill_criteria or not decision.verify_plan:
        return None

    confidence = str(decision.confidence or "").strip().lower()
    if confidence not in ("low", "medium", "high"):
        return None
    decision.confidence = confidence
    if (
        decision.selected_direction != "none"
        and decision.selected_direction not in normalized_options
    ):
        return None
    if decision.selected_direction == "none":
        decision.backup_candidates = []
    return decision


def _normalize_gate_decision(
    gate_decision: Optional[GateDecision], *, mode: Optional[str] = None
) -> Optional[GateDecision]:
    if gate_decision is None:
        return None
    gate_decision.confidence = (gate_decision.confidence or "medium").lower()
    if gate_decision.confidence not in ("low", "medium", "high"):
        gate_decision.confidence = "medium"
    gate_decision.failure_type = _normalize_failure_type(gate_decision.failure_type)
    gate_decision.blocking_risks = _normalize_text_list(gate_decision.blocking_risks)
    gate_decision.required_experiments_before_codegen = _normalize_text_list(
        gate_decision.required_experiments_before_codegen
    )
    gate_decision.advisory_experiments_after_codegen = _normalize_text_list(
        gate_decision.advisory_experiments_after_codegen
    )
    gate_decision.codegen_scope = _normalize_codegen_scope(
        getattr(gate_decision, "codegen_scope", None)
    )
    gate_decision.validation_scope_reason = (
        str(getattr(gate_decision, "validation_scope_reason", "") or "").strip() or None
    )
    gate_decision.validation_objectives = _normalize_text_list(
        getattr(gate_decision, "validation_objectives", []) or []
    )
    gate_decision.agents_needing_rerun = _normalize_text_list(gate_decision.agents_needing_rerun)
    gate_decision.direction_feedback_reason = (
        str(getattr(gate_decision, "direction_feedback_reason", "") or "").strip() or None
    )
    feedback_type = str(getattr(gate_decision, "direction_feedback_type", "") or "").strip().lower()
    gate_decision.direction_feedback_type = (
        feedback_type if feedback_type in {"evidence", "detail"} else None
    )
    gate_decision.direction_feedback_evidence_gaps = _normalize_text_list(
        getattr(gate_decision, "direction_feedback_evidence_gaps", []) or []
    )
    gate_decision.direction_feedback_questions = _normalize_text_list(
        getattr(gate_decision, "direction_feedback_questions", []) or []
    )
    score_vec = gate_decision.score_breakdown
    if not isinstance(score_vec, ScoreVector):
        try:
            score_vec = ScoreVector(**dict(score_vec or {}))
        except Exception:
            score_vec = ScoreVector()
    # Use explicit None-check: overall_score=0 is a valid score that should still
    # trigger sub-vector derivation (e.g. uncertainty=100 for a zero-score result).
    if gate_decision.overall_score is not None and (
        score_vec.feasibility == 0
        and score_vec.roi == 0
        and score_vec.uncertainty == 0
        and score_vec.risk == 0
    ):
        score_vec = _derive_score_vector(gate_decision.overall_score, gate_decision.confidence)
    gate_decision.score_breakdown = score_vec
    if gate_decision.ready_for_codegen and gate_decision.blocking_risks:
        gate_decision.ready_for_codegen = False
        if (
            gate_decision.failure_type == FailureType.CONFLICTING_OUTPUT.value
            and "blocking_risks" in (gate_decision.failure_details or "").lower()
        ):
            gate_decision.failure_type = FailureType.NONE.value
            gate_decision.failure_details = None
    if gate_decision.ready_for_codegen and gate_decision.required_experiments_before_codegen:
        gate_decision.advisory_experiments_after_codegen = _normalize_text_list(
            gate_decision.advisory_experiments_after_codegen
            + gate_decision.required_experiments_before_codegen
        )
        gate_decision.required_experiments_before_codegen = []
        if (
            gate_decision.failure_type == FailureType.CONFLICTING_OUTPUT.value
            and "required experiment" in (gate_decision.failure_details or "").lower()
        ):
            gate_decision.failure_type = FailureType.NONE.value
            gate_decision.failure_details = None
    if _gate_is_validation_scope(gate_decision):
        gate_decision.advisory_experiments_after_codegen = _normalize_text_list(
            list(gate_decision.advisory_experiments_after_codegen or [])
            + list(gate_decision.validation_objectives or [])
        )
    # v1.0.5: hard pre-codegen gate on overall_score.
    # Background: a previous run had ready_for_codegen=true with overall_score=40
    # and 11 high/medium issues — the gate let the bundle through to codegen,
    # the quality loop spun for 80 rounds without resolving the structural
    # mismatches, and the saved_project shipped quality_pass=False as if
    # deliverable. The fix is a deterministic floor: when overall_score is
    # below the configured threshold, ready_for_codegen is forced False unless
    # the gate explicitly opted into a validation-only scope. The threshold
    # defaults to 60 and can be overridden with CRUCIBLE_PRE_CODEGEN_MIN_SCORE.
    if (
        gate_decision.ready_for_codegen
        and not _gate_is_validation_scope(gate_decision)
        and gate_decision.overall_score is not None
    ):
        # v1.1.2 (audit fix G4-A2-MED-6): route through ``_env_int`` so the
        # whitelist sentinel semantics (``none`` / ``unlimited`` → None →
        # default) match the rest of the codebase.  Previously a raw
        # ``int(os.environ.get(...))`` raised ``ValueError`` on non-numeric
        # tokens and silently fell back to 60, surprising operators who
        # tried ``=none`` expecting "disable the floor".  Now ``=none`` /
        # ``=unlimited`` correctly return None and we treat that as "disable
        # the floor" (escape hatch), matching the original intent of the
        # ``min_score == 0`` branch below.
        _raw_min_score = _env_int("CRUCIBLE_PRE_CODEGEN_MIN_SCORE", 60)
        min_score = 0 if _raw_min_score is None else int(_raw_min_score)
        # min_score == 0 disables the floor entirely (escape hatch for tests).
        if min_score > 0 and gate_decision.overall_score < min_score:
            gate_decision.ready_for_codegen = False
            detail = (
                f"Pre-codegen gate floor: overall_score={gate_decision.overall_score} "
                f"< {min_score}; refusing to enter codegen on a low-quality analysis."
            )
            if gate_decision.failure_type == FailureType.NONE.value:
                gate_decision.failure_type = FailureType.LOW_CONFIDENCE.value
                gate_decision.failure_details = detail
            elif not gate_decision.failure_details:
                gate_decision.failure_details = detail
    if gate_decision.should_kill or gate_decision.ready_for_codegen:
        gate_decision.direction_feedback_needed = False
        gate_decision.direction_feedback_type = None
    return gate_decision


def _classify_gate_failure(
    gate_decision: Optional[GateDecision],
) -> Tuple[FailureType, str]:
    if gate_decision is None:
        return FailureType.JSON_INVALID, "GateDecision parsing failed."
    preset = _normalize_failure_type(getattr(gate_decision, "failure_type", None))
    if preset != FailureType.NONE.value:
        try:
            return FailureType(preset), gate_decision.failure_details or ""
        except Exception as _exc:
            # _normalize_failure_type guarantees a valid enum value, so this
            # branch should be unreachable.  Emit a warning if it ever fires
            # so the root cause is visible in logs rather than silently lost.
            import warnings as _warnings
            _warnings.warn(
                f"_classify_gate_failure: unexpected FailureType construction error "
                f"for preset={preset!r}: {_exc}",
                RuntimeWarning,
                stacklevel=2,
            )
    confidence = (gate_decision.confidence or "").lower()
    if confidence == "low" and not _gate_allows_low_confidence_codegen(gate_decision):
        return FailureType.LOW_CONFIDENCE, "GateDecision confidence is low."
    if gate_decision.should_kill:
        return (
            FailureType.POLICY_VIOLATION,
            gate_decision.kill_reason or "Kill signal raised.",
        )
    if gate_decision.blocking_risks and gate_decision.ready_for_codegen:
        return (
            FailureType.CONFLICTING_OUTPUT,
            "ready_for_codegen=true but blocking_risks is not empty.",
        )
    if gate_decision.required_experiments_before_codegen and gate_decision.ready_for_codegen:
        return (
            FailureType.CONFLICTING_OUTPUT,
            "ready_for_codegen=true but required experiments are pending.",
        )
    return FailureType.NONE, ""


def _dag_has_cycle(nodes: List[str], edges: List[Tuple[str, str]]) -> bool:
    in_degree: Dict[str, int] = {n: 0 for n in nodes}
    out_edges: Dict[str, List[str]] = {n: [] for n in nodes}
    for src, dst in edges:
        if src not in in_degree or dst not in in_degree:
            continue
        out_edges[src].append(dst)
        in_degree[dst] += 1
    queue = [n for n in nodes if in_degree[n] == 0]
    visited = 0
    while queue:
        node = queue.pop()
        visited += 1
        for nxt in out_edges.get(node, []):
            in_degree[nxt] -= 1
            if in_degree[nxt] == 0:
                queue.append(nxt)
    return visited != len(nodes)


def _build_agent_dag_snapshot(
    agent_specs: Dict[str, AgentSpec],
    task_specs: List[TaskSpec],
) -> Dict[str, Any]:
    nodes = sorted(agent_specs.keys())
    edges_set: Set[Tuple[str, str]] = set()
    for name, spec in agent_specs.items():
        for dep in spec.depends_on or []:
            if dep in agent_specs and dep != name:
                edges_set.add((dep, name))
    task_agent = {task.name: task.agent_name for task in task_specs}
    for task in task_specs:
        for ctx_name in task.context_task_names or []:
            dep_agent = task_agent.get(ctx_name)
            if dep_agent and dep_agent != task.agent_name:
                edges_set.add((dep_agent, task.agent_name))
    edges = sorted(edges_set)
    retry_policy = {
        name: {
            "max_attempts": spec.retry_policy.max_attempts,
            "backoff_seconds": spec.retry_policy.backoff_seconds,
            "retry_on_json_fail": spec.retry_policy.retry_on_json_fail,
            "retry_on_low_confidence": spec.retry_policy.retry_on_low_confidence,
        }
        for name, spec in agent_specs.items()
    }
    node_meta = {
        name: {
            "version": spec.version,
            "behavior_contract": spec.behavior_contract,
            "breaking_changes": spec.breaking_changes,
            "parallel_safe": spec.parallel_safe,
            "cacheable": spec.cacheable,
        }
        for name, spec in agent_specs.items()
    }
    return {
        "nodes": nodes,
        "edges": [[src, dst] for src, dst in edges],
        "retry_policy": retry_policy,
        "node_meta": node_meta,
        "has_cycle": _dag_has_cycle(nodes, edges),
    }


def _compute_task_prompt_hashes(
    task_specs: List[TaskSpec],
    *,
    base_template_vars: Dict[str, str],
    role_template_vars: Dict[str, str],
    active_roles: Set[str],
) -> Dict[str, str]:
    prompt_hashes: Dict[str, str] = {}
    for spec in task_specs:
        vars_for_task = dict(base_template_vars)
        if spec.name in active_roles:
            vars_for_task["role_name"] = role_template_vars.get(f"{spec.name}_role_name", spec.name)
            vars_for_task["focus"] = role_template_vars.get(f"{spec.name}_focus", "")
        rendered = _render_prompt_template(spec.description_template, vars_for_task)
        prompt_hashes[spec.name] = _text_sha256(rendered)
    return prompt_hashes


def _snapshot_record_stage(
    snapshot: Optional[RunSnapshot],
    *,
    stage: str,
    status: str,
    failure_type: FailureType = FailureType.NONE,
    notes: Optional[str] = None,
    extra: Optional[Dict[str, Any]] = None,
) -> None:
    if snapshot is None:
        return
    record: Dict[str, Any] = {
        "timestamp": datetime.now().isoformat(),
        "stage": stage,
        "status": status,
        "failure_type": failure_type.value,
    }
    if notes:
        record["notes"] = notes
    if extra:
        record.update(extra)
    snapshot.stage_records.append(record)


def _snapshot_record_gate(
    snapshot: Optional[RunSnapshot], gate_decision: Optional[GateDecision]
) -> None:
    if snapshot is None or gate_decision is None:
        return
    try:
        payload = _model_to_dict(gate_decision)
    except Exception:
        payload = {}
    payload["captured_at"] = datetime.now().isoformat()
    snapshot.gate_decisions.append(payload)


def _evaluate_budget_state(policy: BudgetPolicy) -> Dict[str, Any]:
    summary = get_cost_accountant().get_summary()
    total_cost_usd = float(summary.get("total_cost_usd") or 0.0)
    legacy_total_cost = float(summary.get("total_cost") or 0.0)
    cost_source = str(summary.get("cost_source") or "").strip() or "estimated"
    usd_sources = {
        "openrouter_api",
        "openrouter_tokens_with_pricing",
        "crewai_metrics_with_pricing",
    }
    if cost_source in usd_sources:
        total_cost = total_cost_usd
        cost_basis = "usd"
    elif cost_source == "alibaba_coding_plan_tokens_only":
        total_cost = 0.0
        cost_basis = "token_only"
    else:
        total_cost = legacy_total_cost
        cost_basis = "legacy_units"
    total_tokens = int(summary.get("total_tokens") or 0)
    over_soft = policy.soft_cost_limit is not None and total_cost >= float(policy.soft_cost_limit)
    over_hard = policy.hard_cost_limit is not None and total_cost >= float(policy.hard_cost_limit)
    over_tokens = policy.max_total_tokens is not None and total_tokens >= int(
        policy.max_total_tokens
    )
    return {
        "total_cost": total_cost,
        "total_cost_usd": total_cost_usd,
        "total_tokens": total_tokens,
        "cost_basis": cost_basis,
        "cost_source": cost_source,
        "over_soft_limit": bool(over_soft),
        "over_hard_limit": bool(over_hard),
        "over_token_limit": bool(over_tokens),
    }


def _update_budget_state(
    policy: BudgetPolicy,
    run_snapshot: Optional[RunSnapshot] = None,
) -> Dict[str, Any]:
    state = _evaluate_budget_state(policy)
    if run_snapshot is not None:
        run_snapshot.budget_state = state
    return state


# =========================
# 2) Project Scan Utilities
# =========================

EXCLUDE_DIRS = {
    ".git",
    "__pycache__",
    ".venv",
    "venv",
    "node_modules",
    "dist",
    "build",
    ".idea",
    ".vscode",
    "coverage",
    ".pytest_cache",
    "saved_projects",
}
KEY_FILES = {
    "README.md",
    "README.txt",
    "package.json",
    "pyproject.toml",
    "requirements.txt",
    "Pipfile",
    "setup.py",
    "Dockerfile",
    ".env.example",
    "Makefile",
}
ENTRYPOINT_FILES = {
    "main.py",
    "app.py",
    "wsgi.py",
    "asgi.py",
    "server.py",
    "index.py",
    "main.js",
    "server.js",
    "index.js",
}
# v1.0.5: Quant-mode bundles never expose a web entrypoint; their canonical
# top-level scripts are listed below so runtime validation can detect them
# (importable smoke + synthetic-data dry-run) instead of reporting "no
# entrypoints detected" and skipping smoke entirely.
QUANT_ENTRYPOINT_FILES = {
    "backtest.py",
    "run_backtest.py",
    "live_trader.py",
    "data_provider.py",
    "strategy.py",
    "trade.py",
    "cli.py",
}
SENSITIVE_FILENAMES = {
    "openrouter_key.txt",
    ".env",
    ".env.local",
    ".env.production",
    ".env.development",
    ".env.staging",
    ".npmrc",
    ".pypirc",
    "id_rsa",
    "id_dsa",
    "id_ecdsa",
    "id_ed25519",
    "credentials.json",
    "client_secret.json",
}
SENSITIVE_EXTS = {
    ".pem",
    ".key",
    ".p12",
    ".pfx",
    ".crt",
    ".der",
    ".jks",
    ".keystore",
}
SKIP_CONTENT_EXTS = {
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".bmp",
    ".ico",
    ".webp",
    ".pdf",
    ".zip",
    ".tar",
    ".gz",
    ".7z",
    ".rar",
    ".exe",
    ".dll",
    ".so",
    ".dylib",
    ".bin",
    ".db",
    ".sqlite",
    ".sqlite3",
    ".parquet",
    ".feather",
    ".arrow",
    ".xlsx",
    ".xls",
    ".docx",
    ".pptx",
    ".mp4",
    ".mov",
    ".mp3",
    ".wav",
    ".flac",
    ".webm",
    ".avi",
    ".mkv",
    ".otf",
    ".ttf",
    ".woff",
    ".woff2",
    ".pkl",
    ".joblib",
    ".pt",
    ".pth",
    ".onnx",
}
SENSITIVE_PATTERN = re.compile(
    r"(^|[._-])(api_?key|secret|token|password|passwd|credential|private_key)([._-]|$)"
)


def _resolve_project_context_scan_defaults() -> Dict[str, Any]:
    return {
        "quick_max_tree_entries": _env_int("CODEX_QUICK_MAX_TREE_ENTRIES", 200),
        "quick_max_depth": _env_int("CODEX_QUICK_MAX_DEPTH", 3),
        "quick_max_file_bytes": _env_int("CODEX_QUICK_MAX_FILE_BYTES", 20000),
        "quick_max_snippet_chars": _env_int("CODEX_QUICK_MAX_SNIPPET_CHARS", 4000),
        "full_max_tree_entries": _env_int("CODEX_FULL_MAX_TREE_ENTRIES", None),
        "full_max_depth": _env_int("CODEX_FULL_MAX_DEPTH", None),
        "full_max_file_bytes": _env_int("CODEX_FULL_MAX_FILE_BYTES", 1000000),
        "full_max_snippet_chars": _env_int("CODEX_FULL_MAX_SNIPPET_CHARS", None),
        "full_max_total_chars": _env_int("CODEX_FULL_MAX_TOTAL_CHARS", 200000),
    }


_PROJECT_CONTEXT_SCAN_DEFAULTS = _resolve_project_context_scan_defaults()
QUICK_MAX_TREE_ENTRIES = _PROJECT_CONTEXT_SCAN_DEFAULTS["quick_max_tree_entries"]
QUICK_MAX_DEPTH = _PROJECT_CONTEXT_SCAN_DEFAULTS["quick_max_depth"]
QUICK_MAX_FILE_BYTES = _PROJECT_CONTEXT_SCAN_DEFAULTS["quick_max_file_bytes"]
QUICK_MAX_SNIPPET_CHARS = _PROJECT_CONTEXT_SCAN_DEFAULTS["quick_max_snippet_chars"]

FULL_MAX_TREE_ENTRIES = _PROJECT_CONTEXT_SCAN_DEFAULTS["full_max_tree_entries"]
FULL_MAX_DEPTH = _PROJECT_CONTEXT_SCAN_DEFAULTS["full_max_depth"]
FULL_MAX_FILE_BYTES = _PROJECT_CONTEXT_SCAN_DEFAULTS["full_max_file_bytes"]
FULL_MAX_SNIPPET_CHARS = _PROJECT_CONTEXT_SCAN_DEFAULTS["full_max_snippet_chars"]
FULL_MAX_TOTAL_CHARS = _PROJECT_CONTEXT_SCAN_DEFAULTS["full_max_total_chars"]


def is_sensitive_filename(name: str) -> bool:
    base = name.lower()
    if base in SENSITIVE_FILENAMES:
        return True
    if base.startswith(".env"):
        return True
    if any(base.endswith(ext) for ext in SENSITIVE_EXTS):
        return True
    return bool(SENSITIVE_PATTERN.search(base))


def should_skip_content(name: str) -> bool:
    ext = os.path.splitext(name)[1].lower()
    if ext in SKIP_CONTENT_EXTS:
        return True
    return False


# v1.1.2 (sixth-pass H-M3): all quantifiers now have explicit upper bounds.
# The previous ``{20,}`` / ``{8,}`` / ``{6,}`` / ``{10,}`` patterns were
# unbounded, exposing this redactor (called on every uploaded file via
# ``safe_read_text``) to ReDoS-style backtracking on adversarial input.
# The Run Insights ``_VALUE_SECRET_PATTERNS`` set was bounded in v1.1.0
# third-pass for the same reason; this codebase-wide twin must follow.
# Upper bounds chosen to cover realistic real-world tokens plus generous
# headroom:
#   * sk-/gh-: 200 chars (real keys ~64; rotated formats ≤120)
#   * credential values:  500 chars (covers long base64 tokens + headroom)
#   * JWT segments: header 300, payload 2000, signature 300 (matches
#     recorder.py contract).
# A DeepSeek-style ``sk-[32 hex]`` pattern is added BEFORE the generic
# ``sk-[A-Za-z0-9]{20,200}`` so DeepSeek keys produce the expected
# vendor-shaped match.
REDACT_RULES = [
    (re.compile(r"(bearer\s+)[A-Za-z0-9\-._~+/]{20,500}=*", re.IGNORECASE), r"\1[REDACTED]"),
    (re.compile(r"\bsk-[A-Fa-f0-9]{32}\b"), "[REDACTED]"),
    (re.compile(r"\bsk-[A-Za-z0-9]{20,200}\b"), "[REDACTED]"),
    (re.compile(r"\b(ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{20,200}\b"), "[REDACTED]"),
    (re.compile(r"\b(AKIA|ASIA)[0-9A-Z]{16}\b"), "[REDACTED:AWS_ACCESS_KEY_ID]"),
    (
        re.compile(r"(aws_secret_access_key\s*[:=]\s*)([A-Za-z0-9/+=]{40})", re.IGNORECASE),
        r"\1[REDACTED]",
    ),
    (
        re.compile(r"(x-api-key\s*[:=]\s*)(['\"]?)[^'\"\s]{8,500}", re.IGNORECASE),
        r"\1\2[REDACTED]",
    ),
    (
        re.compile(r"(\bapi_?key\b\s*[:=]\s*)(['\"]?)[^'\"\s]{8,500}", re.IGNORECASE),
        r"\1\2[REDACTED]",
    ),
    (
        re.compile(r"(\bsecret\b\s*[:=]\s*)(['\"]?)[^'\"\s]{8,500}", re.IGNORECASE),
        r"\1\2[REDACTED]",
    ),
    (
        re.compile(r"(\btoken\b\s*[:=]\s*)(['\"]?)[^'\"\s]{8,500}", re.IGNORECASE),
        r"\1\2[REDACTED]",
    ),
    (
        re.compile(r"(\b(password|passwd)\b\s*[:=]\s*)(['\"]?)[^'\"\s]{6,500}", re.IGNORECASE),
        r"\1\3[REDACTED]",
    ),
    (
        re.compile(r"\beyJ[a-zA-Z0-9_-]{10,300}\.[a-zA-Z0-9_-]{10,2000}\.[a-zA-Z0-9_-]{10,300}\b"),
        "[REDACTED:JWT]",
    ),
]


def redact_secrets(text: str) -> str:
    redacted = text
    for pattern, repl in REDACT_RULES:
        redacted = pattern.sub(repl, redacted)
    return redacted


def safe_read_text(
    path: str, max_bytes: Optional[int], max_chars: Optional[int]
) -> Tuple[str, bool, Optional[str]]:
    try:
        with open(path, "rb") as f:
            if max_bytes is None:
                data = f.read()
                truncated = False
            else:
                data = f.read(max_bytes + 1)
                truncated = len(data) > max_bytes
                if truncated:
                    data = data[:max_bytes]
        if b"\x00" in data:
            return "", False, "binary"
        text = data.decode("utf-8", errors="replace")
        text = redact_secrets(text)
        if max_chars is not None and len(text) > max_chars:
            text, was_trimmed = _truncate_text_preserve_lines(text, max_chars)
            truncated = truncated or was_trimmed
        return text, truncated, None
    except Exception:
        return "", False, "error"


def parse_requirements(text: str) -> List[str]:
    deps: List[str] = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("-"):
            continue
        if ";" in line:
            line = line.split(";", 1)[0].strip()
            if not line:
                continue
        for sep in ["==", ">=", "<=", "~=", ">", "<"]:
            if sep in line:
                line = line.split(sep, 1)[0].strip()
                break
        if line and line not in deps:
            deps.append(line)
    return deps


def parse_package_json(text: str) -> List[str]:
    try:
        data = json.loads(text)
    except Exception:
        return []
    deps: List[str] = []
    for key in ("dependencies", "devDependencies"):
        block = data.get(key, {})
        if isinstance(block, dict):
            for dep in block.keys():
                if dep not in deps:
                    deps.append(dep)
    return deps


def detect_stack(ext_counts: Dict[str, int], deps: List[str]) -> List[str]:
    stack: List[str] = []
    if ext_counts.get("py"):
        stack.append("Python")
    if ext_counts.get("js") or ext_counts.get("ts"):
        stack.append("JavaScript/TypeScript")
    if ext_counts.get("html") or ext_counts.get("css"):
        stack.append("Web")

    dep_set = {d.lower() for d in deps}
    if "django" in dep_set:
        stack.append("Django")
    if "flask" in dep_set:
        stack.append("Flask")
    if "fastapi" in dep_set:
        stack.append("FastAPI")
    if "streamlit" in dep_set:
        stack.append("Streamlit")
    if "react" in dep_set or "next" in dep_set:
        stack.append("React/Next")
    if "vue" in dep_set:
        stack.append("Vue")
    if "express" in dep_set:
        stack.append("Express")
    if "nestjs" in dep_set:
        stack.append("NestJS")
    return sorted(set(stack))


def build_project_context(
    project_path: str,
    max_depth: Optional[int],
    max_tree_entries: Optional[int],
    read_all_files: bool,
    max_file_bytes: Optional[int],
    max_snippet_chars: Optional[int],
    max_total_chars: Optional[int],
) -> Dict[str, Any]:
    root = os.path.abspath(project_path)
    tree_lines: List[str] = [f"{os.path.basename(root)}/"]
    ext_counts: Dict[str, int] = {}
    key_file_snippets: Dict[str, Tuple[str, bool]] = {}
    entry_snippets: Dict[str, Tuple[str, bool]] = {}
    all_file_snippets: List[Tuple[str, str, bool]] = []
    deps: List[str] = []
    notes: List[str] = []
    skipped_binary = 0
    skipped_error = 0
    skipped_sensitive = 0
    skipped_by_ext = 0
    skipped_budget = 0
    budget_trimmed = 0
    total_chars = 0
    truncated_files = 0
    truncate_reasons: List[str] = []
    limit_settings = {
        "max_depth": max_depth,
        "max_tree_entries": max_tree_entries,
        "max_file_bytes": max_file_bytes,
        "max_snippet_chars": max_snippet_chars,
        "max_total_chars": max_total_chars,
    }

    def _file_priority(name: str) -> Tuple[int, str]:
        if name in KEY_FILES:
            return (0, name)
        if name in ENTRYPOINT_FILES:
            return (1, name)
        return (2, name)

    entry_count = 0
    for current_root, dirs, files in os.walk(root):
        rel_root = os.path.relpath(current_root, root)
        depth = 0 if rel_root == "." else rel_root.count(os.sep) + 1
        if max_depth is not None and depth > max_depth:
            if "max_depth" not in truncate_reasons:
                truncate_reasons.append("max_depth")
            dirs[:] = []
            continue

        dirs[:] = [d for d in dirs if d not in EXCLUDE_DIRS and not d.startswith(".")]
        indent = "  " * depth
        if rel_root != ".":
            tree_lines.append(f"{indent}{os.path.basename(current_root)}/")
            entry_count += 1
            if max_tree_entries is not None and entry_count >= max_tree_entries:
                note = f"Tree truncated after {max_tree_entries} entries."
                if note not in notes:
                    notes.append(note)
                if "max_tree_entries" not in truncate_reasons:
                    truncate_reasons.append("max_tree_entries")
                break

        for name in sorted(files, key=_file_priority):
            if max_tree_entries is not None and entry_count >= max_tree_entries:
                note = f"Tree truncated after {max_tree_entries} entries."
                if note not in notes:
                    notes.append(note)
                if "max_tree_entries" not in truncate_reasons:
                    truncate_reasons.append("max_tree_entries")
                break
            rel_path = os.path.join(rel_root, name) if rel_root != "." else name
            tree_lines.append(f"{indent}  {name}")
            entry_count += 1

            ext = os.path.splitext(name)[1].lstrip(".").lower()
            if ext:
                ext_counts[ext] = ext_counts.get(ext, 0) + 1
            is_sensitive = is_sensitive_filename(name)
            skip_by_ext = should_skip_content(name)
            if is_sensitive:
                skipped_sensitive += 1
            if skip_by_ext:
                skipped_by_ext += 1

            file_text = None
            file_truncated = False
            file_skip_reason = None
            if read_all_files and not is_sensitive and not skip_by_ext:
                if max_total_chars is not None and total_chars >= max_total_chars:
                    skipped_budget += 1
                    if "max_total_chars" not in truncate_reasons:
                        truncate_reasons.append("max_total_chars")
                    continue
                file_text, file_truncated, file_skip_reason = safe_read_text(
                    os.path.join(current_root, name), max_file_bytes, max_snippet_chars
                )
                if file_skip_reason == "binary":
                    skipped_binary += 1
                elif file_skip_reason == "error":
                    skipped_error += 1
                elif file_text:
                    if max_total_chars is not None:
                        remaining = max_total_chars - total_chars
                        if remaining <= 0:
                            skipped_budget += 1
                            if "max_total_chars" not in truncate_reasons:
                                truncate_reasons.append("max_total_chars")
                            continue
                        if len(file_text) > remaining:
                            file_text, _ = _truncate_text_preserve_lines(file_text, remaining)
                            file_truncated = True
                            budget_trimmed += 1
                            if "max_total_chars" not in truncate_reasons:
                                truncate_reasons.append("max_total_chars")
                    all_file_snippets.append((rel_path, file_text, file_truncated))
                    total_chars += len(file_text)
                    if file_truncated:
                        truncated_files += 1

            if name in KEY_FILES:
                if read_all_files:
                    if file_text:
                        if name == "requirements.txt":
                            deps.extend(parse_requirements(file_text))
                        if name == "package.json":
                            deps.extend(parse_package_json(file_text))
                elif len(key_file_snippets) < 5 and not is_sensitive and not skip_by_ext:
                    text, truncated, skip_reason = safe_read_text(
                        os.path.join(current_root, name),
                        max_file_bytes,
                        max_snippet_chars,
                    )
                    if skip_reason == "binary":
                        skipped_binary += 1
                    elif skip_reason == "error":
                        skipped_error += 1
                    elif text:
                        key_file_snippets[rel_path] = (text, truncated)
                        if name == "requirements.txt":
                            deps.extend(parse_requirements(text))
                        if name == "package.json":
                            deps.extend(parse_package_json(text))
                        if truncated:
                            truncated_files += 1

            if (
                name in ENTRYPOINT_FILES
                and not read_all_files
                and len(entry_snippets) < 3
                and not is_sensitive
                and not skip_by_ext
            ):
                text, truncated, skip_reason = safe_read_text(
                    os.path.join(current_root, name), max_file_bytes, max_snippet_chars
                )
                if skip_reason == "binary":
                    skipped_binary += 1
                elif skip_reason == "error":
                    skipped_error += 1
                elif text:
                    entry_snippets[rel_path] = (text, truncated)
                    if truncated:
                        truncated_files += 1

        if max_tree_entries is not None and entry_count >= max_tree_entries:
            break

    if not tree_lines:
        notes.append("No files found in project root.")
    if skipped_by_ext:
        notes.append(f"Skipped files by extension: {skipped_by_ext}.")
    if skipped_sensitive:
        notes.append(f"Skipped sensitive files: {skipped_sensitive}.")
    if skipped_budget:
        notes.append(f"Skipped files due to content budget: {skipped_budget}.")
    if budget_trimmed:
        notes.append(f"Trimmed files due to content budget: {budget_trimmed}.")
    if skipped_binary:
        notes.append(f"Skipped binary files: {skipped_binary}.")
    if skipped_error:
        notes.append(f"Skipped unreadable files: {skipped_error}.")
    if truncated_files:
        notes.append(f"Truncated file contents: {truncated_files}.")

    if read_all_files:
        scan_summary = "full scan, all readable text files included"
    else:
        depth_label = str(max_depth) if max_depth is not None else "unlimited"
        entries_label = str(max_tree_entries) if max_tree_entries is not None else "unlimited"
        scan_summary = f"quick scan (depth <= {depth_label}, max {entries_label} entries)"

    context = {
        "root": root,
        "tree": "\n".join(tree_lines),
        "ext_counts": ext_counts,
        "key_files": key_file_snippets,
        "entrypoints": entry_snippets,
        "all_files": all_file_snippets,
        "deps": sorted(set(deps)),
        "stack_guess": detect_stack(ext_counts, deps),
        "notes": notes,
        "scan_summary": scan_summary,
        "context_truncated": bool(
            truncate_reasons or truncated_files or skipped_budget or budget_trimmed
        ),
        "context_truncate_reasons": truncate_reasons,
        "content_chars": total_chars,
        "limits": limit_settings,
    }
    return context


def format_project_context(context: Dict[str, Any]) -> str:
    parts: List[str] = []
    parts.append("=== PROJECT METADATA ===")
    parts.append(f"Project path: {context['root']}")
    parts.append(f"Scan summary: {context.get('scan_summary', 'scan')}")
    limits = context.get("limits", {})
    if limits:
        limit_parts: List[str] = []
        for key in (
            "max_depth",
            "max_tree_entries",
            "max_file_bytes",
            "max_snippet_chars",
            "max_total_chars",
        ):
            if key in limits:
                limit_parts.append(f"{key}={fmt_limit(limits.get(key))}")
        if limit_parts:
            parts.append("Limits: " + ", ".join(limit_parts))
    if context.get("context_truncated"):
        reasons = context.get("context_truncate_reasons", [])
        if reasons:
            reason_details: List[str] = []
            for reason in reasons:
                if limits and reason in limits:
                    reason_details.append(f"{reason}={fmt_limit(limits.get(reason))}")
                else:
                    reason_details.append(reason)
            parts.append(f"Context truncated: yes ({', '.join(reason_details)})")
        else:
            parts.append("Context truncated: yes")
    else:
        parts.append("Context truncated: no")
    parts.append(f"Content chars included: {context.get('content_chars', 0)}")

    parts.append("\n=== PROJECT TREE ===")
    parts.append(context["tree"])

    ext_counts = context.get("ext_counts", {})
    if ext_counts:
        parts.append("\n=== EXTENSION COUNTS ===")
        for ext, count in sorted(ext_counts.items(), key=lambda x: (-x[1], x[0])):
            parts.append(f"- {ext}: {count}")

    stack_guess = context.get("stack_guess", [])
    if stack_guess:
        parts.append("\n=== STACK GUESS ===")
        parts.append(", ".join(stack_guess))

    deps = context.get("deps", [])
    if deps:
        parts.append("\n=== DEPENDENCIES (SAMPLED) ===")
        parts.append(", ".join(deps[:40]))

    all_files = context.get("all_files", [])
    if all_files:
        parts.append("\n=== FILE CONTENTS ===")
        for path, text, truncated in all_files:
            suffix = " (truncated)" if truncated else ""
            parts.append(f"[{path}]{suffix}\n{text}")
    else:
        key_files = context.get("key_files", {})
        if key_files:
            parts.append("\n=== KEY FILE EXCERPTS ===")
            for path, (text, truncated) in key_files.items():
                suffix = " (truncated)" if truncated else ""
                parts.append(f"[{path}]{suffix}\n{text}")

        entrypoints = context.get("entrypoints", {})
        if entrypoints:
            parts.append("\n=== ENTRYPOINT EXCERPTS ===")
            for path, (text, truncated) in entrypoints.items():
                suffix = " (truncated)" if truncated else ""
                parts.append(f"[{path}]{suffix}\n{text}")

    notes = context.get("notes", [])
    if notes:
        parts.append("\n=== NOTES ===")
        for note in notes:
            parts.append(f"- {note}")

    return "\n".join(parts)


# =========================
# 3) Global Rules & Modes
# =========================

COMMON_OUTPUT_RULES = """
你必須嚴格依照下列格式輸出，任何缺欄、合併欄位、或未標註「信心等級 + 可驗證方式」都視為錯誤。

【要點清單】（最多 10 點）
- 條列式、具體、避免空泛描述

【需要的輸入 / 假設】（最多 5 點）
- 明確列出結論成立所需的前提

【結論標註】
- 信心等級：低 / 中 / 高
- 可驗證方式：回測、模擬、壓測、A/B Test、用戶實驗、指標追蹤等（需具體）
"""

NO_CROSS_ROLE_RULE = """
規則：你必須【完全獨立思考】，
不得互相引用、反駁或覆寫其他角色結論。
不得評論或總結其他角色內容。
"""

ARBITER_OUTPUT_RULES = """
你是 [Arbiter]，僅能整理，不得新增觀點。
你必須嚴格依照下列格式輸出（且只能輸出以下三段）：

【共識】
- 各角色明確一致的結論（不得自行延伸）

【分歧】
- 角色間衝突的假設、判斷或風險認知

【下一步實驗清單】（最多 7 項）
- 每項需包含：驗證目標 + 成功判準
- 需具體可執行、可量測
"""

GATE_CONTROLLER_RULES = """
你是 [Gate Controller]，負責整合決策與流程控制。
你必須嚴格依照下列 JSON 格式輸出：

{
  "consensus": "各角色明確一致的結論",
  "disagreement": "角色間衝突的假設、判斷或風險認知",
  "experiments": [{"goal": "驗證目標", "criteria": "成功判準"}],
  
  "ready_for_codegen": true/false,
  "blocking_risks": ["阻斷性風險1", "風險2"],
  "required_experiments_before_codegen": ["必須先完成的實驗"],
  "advisory_experiments_after_codegen": ["可在 codegen 後持續驗證的項目"],
  "codegen_scope": "production|validation",
  "validation_scope_reason": null,
  "validation_objectives": ["validation-first codegen 必須直接量測的目標"],
  
  "agents_needing_rerun": ["agent_name1", "agent_name2"],
  "rerun_reasons": {"agent_name1": "原因", "agent_name2": "原因"},
  
  "direction_feedback_needed": false,
  "direction_feedback_reason": null,
  "direction_feedback_type": null,
  "direction_feedback_evidence_gaps": ["missing detail 1"],
  "direction_feedback_questions": ["question 1"],

  "overall_score": 0-100,
  "score_breakdown": {"feasibility": 0-100, "risk": 0-100, "roi": 0-100, "uncertainty": 0-100},
  "confidence": "low|medium|high",
  "failure_type": "NONE|JSON_INVALID|EXECUTION_ERROR|LOW_CONFIDENCE|COST_OVER_BUDGET|CONFLICTING_OUTPUT|POLICY_VIOLATION|NON_DETERMINISTIC",
  "failure_details": null,
  
  "should_kill": false,
  "kill_reason": null
}

【控制邏輯】
- ready_for_codegen=false 時，不進入 CodeGen 階段
- blocking_risks 非空時，必須先解決這些風險
- agents_needing_rerun 列出需要重跑的 agent（如 confidence=low、json 失敗）
- should_kill=true 時，終止整個流程
- 如果使用者要求的是驗證框架 / Phase0 / 校準 / 語義驗證 / measurement harness，而缺口正是程式要去量測的內容，允許 ready_for_codegen=true 且 codegen_scope="validation"
- codegen_scope="validation" 時，不可假裝已經證明 production strategy 成立；validation_objectives 必須明確列出要量測、校準、驗證的項目
- validation_scope_reason 必須說明為何可以先生成 validation harness 而不是直接卡死在 ready_for_codegen=false

【禁止事項】
- 不得新增觀點或結論
- 不得延伸推論
- 只能基於其他角色的輸出進行整合
"""

GATE_CONTROLLER_RULES += (
    "\n- If the issue is insufficient evidence or missing detail rather than a fundamentally invalid direction, "
    "set direction_feedback_needed=true and list the gaps/questions explicitly.\n"
    "- Set direction_feedback_type='evidence' when proof, data, citations, or research support is missing.\n"
    "- Set direction_feedback_type='detail' when implementation steps, parameters, thresholds, sequencing, or concrete operational detail is missing.\n"
    "- Use should_kill=true only when the direction is fundamentally contradictory, unsafe, or not viable even after additional clarification.\n"
)

GATE_CONTEXT_COMPACTOR_RULES = """
你是 [Gate Context Compactor]，負責在不遺漏 implementation-critical detail 的前提下，
把多個 analyst 輸出整理成 Gate Controller 更容易消化的單一 JSON。

你必須嚴格依照下列 JSON 格式輸出：

{
  "executive_summary": "跨角色共同結論與主要 tradeoff 的精簡摘要",
  "analyst_findings": {
    "research": "保留可直接影響實作、驗證或 go/no-go 的要點",
    "risk": "保留風險、前提、失敗模式與防護要求"
  },
  "implementation_requirements": ["具體步驟、模組責任、資料流、依賴條件"],
  "implementation_constraints": ["不可做的事、邊界條件、風險控制"],
  "validation_focus": ["後續必須量測、驗證、校準或對齊的重點"],
  "blocking_unknowns": ["目前仍缺失的證據、參數、門檻或設計細節"],
  "rerun_signals": {
    "research": ["何種缺口需要重跑 research"],
    "ops": ["何種缺口需要重跑 ops"]
  }
}

【壓縮規則】
- 去重、合併同義重複點，但不得丟失會影響實作或 gate 決策的細節
- analyst_findings 必須保留各角色獨立觀點，不能混成單一泛化摘要
- implementation_requirements / implementation_constraints / validation_focus 只保留可執行、可驗證內容
- blocking_unknowns 只列真正會影響 production-ready codegen 或需要額外澄清的缺口
- rerun_signals 只在確實存在缺口時列出理由；沒有理由就不要硬填
- 不得新增原始 analyst 輸出沒有的事實
"""


# =========================
# 3.5) Mode Plugin System & Agent Cost Accounting
# =========================
# 優化4：Mode 插件化 - 支援動態擴展新模式
# 優化5：Agent Cost Accounting - 追蹤每個 agent 的成本
class ModeConfig(BaseModel):
    """
    Configuration for a mode (Quant, SaaS, etc.).
    Supports plugin-style extension for new product types.
    """

    name: str = Field(..., description="Mode name (e.g., 'Quant', 'SaaS')")
    description: str = Field(..., description="Mode description")
    metrics: str = Field(..., description="Key metrics for this mode")
    research_focus: str = Field(..., description="Research focus areas")
    biz_focus: str = Field(..., description="Business focus areas")

    # Cost weights for this mode (higher = more expensive operations)
    cost_multiplier: float = Field(default=1.0, description="Cost multiplier for this mode")

    # Runtime validation settings
    requires_runtime_validation: bool = Field(
        default=True, description="Whether runtime validation is required"
    )
    requires_snapshot_validation: bool = Field(
        default=False, description="Whether snapshot validation is required"
    )

    # Code generation preferences
    preferred_framework: str = Field(
        default="fastapi", description="Preferred framework for code generation"
    )
    additional_requirements: List[str] = Field(
        default_factory=list, description="Additional requirements for code generation"
    )


class AgentCostRecord(BaseModel):
    """
    Record of cost for a single agent execution.
    Supports both legacy arbitrary units and OpenRouter native USD costs.
    """

    agent_name: str = Field(..., description="Agent name")
    stage: str = Field(..., description="Execution stage")
    timestamp: str = Field(default_factory=lambda: datetime.now().isoformat())

    # Token counts (from OpenRouter API response when available)
    input_tokens: int = Field(default=0, description="Input tokens from API response")
    output_tokens: int = Field(default=0, description="Output tokens from API response")
    total_tokens: int = Field(default=0, description="Total tokens used")

    # Cache tokens (from OpenRouter prompt_tokens_details.cached_tokens)
    cached_tokens: int = Field(default=0, description="Tokens served from cache")

    # Reasoning tokens (from OpenRouter completion_tokens_details.reasoning_tokens)
    reasoning_tokens: int = Field(default=0, description="Tokens used for reasoning")

    # Cost in arbitrary units (legacy, for backward compatibility)
    cost_units: float = Field(default=0.0, description="Cost in arbitrary units (legacy)")

    # OpenRouter native USD costs (actual billing amounts)
    input_cost_usd: float = Field(default=0.0, description="Input cost in USD from OpenRouter")
    output_cost_usd: float = Field(default=0.0, description="Output cost in USD from OpenRouter")
    cache_cost_usd: float = Field(
        default=0.0, description="Cache cost savings in USD from OpenRouter"
    )
    total_cost_usd: float = Field(default=0.0, description="Total cost in USD from OpenRouter")

    # Model information for cost tracking
    model_id: str = Field(default="", description="Model ID used for this request")

    # Result metadata
    success: bool = Field(default=True, description="Whether execution succeeded")
    cache_hit: bool = Field(default=False, description="Whether result was from cache")
    retry_count: int = Field(default=0, description="Number of retries")

    # Outcome classification
    outcome: str = Field(default="success", description="Outcome: success, failed, cached, skipped")

    # Source of cost data
    cost_source: str = Field(
        default="estimated",
        description=(
            "Source of cost data: 'openrouter_api', "
            "'openrouter_tokens_with_pricing', "
            "'crewai_metrics_with_pricing', "
            "'alibaba_coding_plan_tokens_only', or 'estimated'"
        ),
    )


_BILLING_AWARE_COST_SOURCES: Tuple[str, ...] = (
    "openrouter_api",
    "openrouter_tokens_with_pricing",
    "crewai_metrics_with_pricing",
    "alibaba_coding_plan_tokens_only",
)


def _summarize_cost_source(records: List[AgentCostRecord]) -> str:
    sources = {str(r.cost_source or "").strip() for r in records if getattr(r, "cost_source", None)}
    for source in _BILLING_AWARE_COST_SOURCES:
        if source in sources:
            return source
    return "estimated"


class AgentCostAccountant:
    """
    Tracks and analyzes costs per agent, per stage, and per decision.
    Enables SaaS credits, API billing, and plan gating.
    """

    def __init__(self):
        self._records: List[AgentCostRecord] = []
        self._by_agent: Dict[str, List[AgentCostRecord]] = {}
        self._by_stage: Dict[str, List[AgentCostRecord]] = {}

    def record(
        self,
        agent_name: str,
        stage: str,
        input_tokens: int = 0,
        output_tokens: int = 0,
        success: bool = True,
        cache_hit: bool = False,
        retry_count: int = 0,
        outcome: str = "success",
        # OpenRouter native cost fields
        cached_tokens: int = 0,
        reasoning_tokens: int = 0,
        input_cost_usd: float = 0.0,
        output_cost_usd: float = 0.0,
        cache_cost_usd: float = 0.0,
        total_cost_usd: float = 0.0,
        model_id: str = "",
        cost_source: str = "estimated",
    ) -> None:
        total = input_tokens + output_tokens
        cost_units = (total / 1000.0) * (0.1 if cache_hit else 1.0)

        record = AgentCostRecord(
            agent_name=agent_name,
            stage=stage,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            total_tokens=total,
            cached_tokens=cached_tokens,
            reasoning_tokens=reasoning_tokens,
            cost_units=cost_units,
            input_cost_usd=input_cost_usd,
            output_cost_usd=output_cost_usd,
            cache_cost_usd=cache_cost_usd,
            total_cost_usd=total_cost_usd,
            model_id=model_id,
            success=success,
            cache_hit=cache_hit,
            retry_count=retry_count,
            outcome=outcome,
            cost_source=cost_source,
        )

        self._records.append(record)
        self._by_agent.setdefault(agent_name, []).append(record)
        self._by_stage.setdefault(stage, []).append(record)

    def get_agent_cost(self, agent_name: str) -> Dict[str, Any]:
        records = self._by_agent.get(agent_name, [])
        if not records:
            return {"agent": agent_name, "total_cost": 0, "total_cost_usd": 0.0, "executions": 0}

        return {
            "agent": agent_name,
            "executions": len(records),
            "total_tokens": sum(r.total_tokens for r in records),
            "cached_tokens": sum(r.cached_tokens for r in records),
            "reasoning_tokens": sum(r.reasoning_tokens for r in records),
            "total_cost": sum(r.cost_units for r in records),
            "total_cost_usd": sum(r.total_cost_usd for r in records),
            "input_cost_usd": sum(r.input_cost_usd for r in records),
            "output_cost_usd": sum(r.output_cost_usd for r in records),
            "cache_cost_usd": sum(r.cache_cost_usd for r in records),
            "cache_hits": sum(1 for r in records if r.cache_hit),
            "failures": sum(1 for r in records if not r.success),
            "avg_cost_per_execution": sum(r.cost_units for r in records) / len(records),
            "avg_cost_usd_per_execution": sum(r.total_cost_usd for r in records) / len(records),
            "models_used": list(set(r.model_id for r in records if r.model_id)),
            "cost_source": _summarize_cost_source(records),
        }

    def get_stage_cost(self, stage: str) -> Dict[str, Any]:
        records = self._by_stage.get(stage, [])
        if not records:
            return {"stage": stage, "total_cost": 0, "total_cost_usd": 0.0, "executions": 0}

        return {
            "stage": stage,
            "executions": len(records),
            "total_tokens": sum(r.total_tokens for r in records),
            "cached_tokens": sum(r.cached_tokens for r in records),
            "reasoning_tokens": sum(r.reasoning_tokens for r in records),
            "total_cost": sum(r.cost_units for r in records),
            "total_cost_usd": sum(r.total_cost_usd for r in records),
            "input_cost_usd": sum(r.input_cost_usd for r in records),
            "output_cost_usd": sum(r.output_cost_usd for r in records),
            "cache_cost_usd": sum(r.cache_cost_usd for r in records),
        }

    def get_summary(self) -> Dict[str, Any]:
        if not self._records:
            return {
                "total_cost": 0,
                "total_cost_usd": 0.0,
                "total_tokens": 0,
                "total_executions": 0,
                "cache_hit_rate": 0,
                "success_rate": 1.0,
                "cached_tokens": 0,
                "reasoning_tokens": 0,
            }

        total_cost = sum(r.cost_units for r in self._records)
        total_cost_usd = sum(r.total_cost_usd for r in self._records)
        total_tokens = sum(r.total_tokens for r in self._records)
        cached_tokens = sum(r.cached_tokens for r in self._records)
        reasoning_tokens = sum(r.reasoning_tokens for r in self._records)
        cache_hits = sum(1 for r in self._records if r.cache_hit)
        successes = sum(1 for r in self._records if r.success)
        cost_source = _summarize_cost_source(self._records)

        return {
            "total_cost": total_cost,
            "total_cost_usd": total_cost_usd,
            "total_tokens": total_tokens,
            "cached_tokens": cached_tokens,
            "reasoning_tokens": reasoning_tokens,
            "total_executions": len(self._records),
            "cache_hit_rate": cache_hits / len(self._records) if self._records else 0,
            "success_rate": successes / len(self._records) if self._records else 1.0,
            "input_cost_usd": sum(r.input_cost_usd for r in self._records),
            "output_cost_usd": sum(r.output_cost_usd for r in self._records),
            "cache_cost_usd": sum(r.cache_cost_usd for r in self._records),
            "cost_source": cost_source,
            "models_used": list(set(r.model_id for r in self._records if r.model_id)),
            "by_agent": {name: self.get_agent_cost(name) for name in self._by_agent.keys()},
            "by_stage": {stage: self.get_stage_cost(stage) for stage in self._by_stage.keys()},
        }

    def get_top_cost_agents(self, limit: int = 5) -> List[Dict[str, Any]]:
        """Get the top cost agents."""
        agent_costs = [self.get_agent_cost(name) for name in self._by_agent.keys()]
        return sorted(
            agent_costs,
            key=lambda x: (
                float(x.get("total_cost_usd", 0.0) or 0.0),
                float(x.get("total_cost", 0.0) or 0.0),
                int(x.get("total_tokens", 0) or 0),
            ),
            reverse=True,
        )[:limit]


class ModeRegistry:
    """
    Registry for mode configurations.
    Supports plugin-style registration of new modes.
    """

    _modes: Dict[str, ModeConfig] = {}

    @classmethod
    def register(cls, config: ModeConfig) -> None:
        cls._modes[config.name] = config

    @classmethod
    def get(cls, name: str) -> Optional[ModeConfig]:
        return cls._modes.get(name)

    @classmethod
    def all_modes(cls) -> Dict[str, ModeConfig]:
        return dict(cls._modes)

    @classmethod
    def list_names(cls) -> List[str]:
        return list(cls._modes.keys())


# Register default modes
ModeRegistry.register(
    ModeConfig(
        name="Quant",
        description="量化研究與策略驗證模式",
        metrics="Sharpe Ratio, Alpha, Volatility, Max Drawdown, Slippage",
        research_focus="策略邏輯、資料來源、回測設計、交易成本與風險控制",
        biz_focus="Portfolio construction, packaging, distribution, and monetization",
        cost_multiplier=1.2,
        requires_runtime_validation=True,
        requires_snapshot_validation=False,
        preferred_framework="pure_python",
    )
)

ModeRegistry.register(
    ModeConfig(
        name="SaaS",
        description="SaaS product strategy and validation mode",
        metrics="MRR, Churn Rate, LTV/CAC, NPS, Retention",
        research_focus="市場痛點、使用者工作流、競品定位、定價與 GTM 假設",
        biz_focus="商業模式、分銷策略、成長槓桿與留存機制",
        cost_multiplier=1.0,
        requires_runtime_validation=True,
        requires_snapshot_validation=False,
        preferred_framework="fastapi",
    )
)

ModeRegistry.register(
    ModeConfig(
        name="Agent",
        description="Headless agent / protocol automation / orchestration 研究模式",
        metrics="Determinism, Replayability, Cost-to-Reward, Uptime, Failure Recovery",
        research_focus="工具鏈設計、協定整合、容錯恢復、可重播性與運維邊界",
        biz_focus="Protocol integrations, operating leverage, deployment, and reliability economics",
        cost_multiplier=1.1,
        requires_runtime_validation=True,
        requires_snapshot_validation=False,
        preferred_framework="pure_python",
        additional_requirements=[
            "headless execution",
            "systemd friendly",
            "structured logs",
            "deterministic outputs",
        ],
    )
)

ModeRegistry.register(
    ModeConfig(
        name="Scientist",
        description="學術研究與論文實踐模式：搜索論文並實作為可執行程式碼",
        metrics="Reproducibility Score, Fidelity-to-Paper, Benchmark Delta, Ablation Coverage, Experiment Runtime",
        research_focus="論文搜索、演算法理解、實驗設計、基準比較與可重現性驗證",
        biz_focus="Research contributions, benchmarking, reproducible experiments, publication-ready implementations",
        cost_multiplier=1.1,
        requires_runtime_validation=True,
        requires_snapshot_validation=False,
        preferred_framework="pure_python",
        additional_requirements=[
            "reproducible experiments",
            "paper citations",
            "benchmark comparisons",
            "ablation studies",
            "deterministic seeding",
        ],
    )
)


# Backwards-compatible MODES dict
MODES = {
    name: {
        "description": config.description,
        "metrics": config.metrics,
        "research_focus": config.research_focus,
        "biz_focus": config.biz_focus,
    }
    for name, config in ModeRegistry.all_modes().items()
}


# Global cost accountant instance
_COST_ACCOUNTANT: Optional[AgentCostAccountant] = None


def get_cost_accountant() -> AgentCostAccountant:
    """Get or create the global cost accountant."""
    global _COST_ACCOUNTANT
    if _COST_ACCOUNTANT is None:
        _COST_ACCOUNTANT = AgentCostAccountant()
    return _COST_ACCOUNTANT


def reset_cost_accountant() -> None:
    """Reset process-global cost state before starting a fresh pipeline run."""
    global _COST_ACCOUNTANT
    _COST_ACCOUNTANT = AgentCostAccountant()


def _canonical_mode_name_from_project_type(project_type: str) -> str:
    mapping = {
        "quant": "Quant",
        "saas": "SaaS",
        "agent": "Agent",
        "scientist": "Scientist",
    }
    canonical_mode_name = mapping.get(str(project_type or "").strip().lower())
    if canonical_mode_name is None:
        raise ValueError(
            f"Unsupported project_type '{project_type}'. Expected one of: quant, saas, agent, scientist"
        )
    return canonical_mode_name


def _validated_mode_config(mode: str) -> ModeConfig:
    canonical_mode_name = _canonical_mode_name_from_project_type(
        _project_type_for_mode(mode)
    )
    mode_config = _get_mode_config(mode)
    config_name = str(getattr(mode_config, "name", "") or "").strip()
    if config_name != canonical_mode_name:
        raise ValueError(
            "Mode registry returned config name "
            f"'{config_name}' for requested mode '{canonical_mode_name}'."
        )
    return mode_config


# =========================
# 4) Agents & Tasks Factory
# =========================


def _legacy_build_code_fix_crew(user_problem: str, mode: str, language_hint: str, llm: Any) -> Crew:
    mode_cfg = _validated_mode_config(mode)
    project_type = _project_type_for_mode(mode)
    mode_fix_rules = "\n".join(_mode_code_fix_rule_lines(mode_cfg))
    print(f"\n[System] Initializing Code Fix Crew in {mode} Mode...")

    fixer = Agent(
        role="Code Fixer",
        goal="以最小改動修復既有程式碼中的 bug。",
        backstory=("你是資深工程師。你只能做精準修補，不得順手新增功能或大改架構。"),
        allow_delegation=False,
        verbose=True,
        llm=llm,
    )

    task = Task(
        description=(
            "你現在處於專案修復模式。只能使用提供的專案內容與使用者說明來修 bug。\n\n"
            "規則：\n"
            "- 只修改既有檔案；除非修復必需，否則不要新增檔案\n"
            "- 改動必須最小化，且以正確性為唯一優先\n"
            "- 除非修 bug 必需，否則不得破壞 public API 或既有行為\n"
            "- 不得重設計或重寫整個專案\n"
            "- 輸出 path 必須是專案根目錄下的相對路徑（不得有前綴 code/）\n"
            f"{mode_fix_rules}\n"
            f"- project_type 必須設為 '{project_type}'\n\n"
            f"語言：{language_hint}\n\n"
            "專案內容：\n"
            f"{user_problem}"
        ),
        agent=fixer,
        expected_output="Fixed CodeBundle JSON only (no markdown, no extra text).",
    )

    return Crew(agents=[fixer], tasks=[task], process=Process.sequential, verbose=True)


def build_code_fix_crew(user_problem: str, mode: str, language_hint: str, llm: Any) -> Crew:
    mode_cfg = _validated_mode_config(mode)
    project_type = _project_type_for_mode(mode)
    mode_fix_rules = "\n".join(_mode_code_fix_rule_lines(mode_cfg))
    print(f"\n[System] Initializing Code Fix Crew in {mode} Mode...")

    fixer = Agent(
        role="Code Fixer",
        goal="Repair only the concrete defects described in the approved bug context.",
        backstory=(
            "You are a production-minded software engineer.\n"
            "- Fix the smallest root cause that resolves the reported bug.\n"
            "- Preserve existing public behavior unless the bug report explicitly requires a change.\n"
            "- Output runnable code only; do not include analysis prose."
        ),
        allow_delegation=False,
        verbose=True,
        llm=llm,
    )

    task = Task(
        description=(
            "Fix the reported bug and return a CodeBundle JSON response.\n\n"
            "Rules:\n"
            "- Modify only the files and code paths required to fix the bug.\n"
            "- Do not invent features, refactors, migrations, or API changes beyond the approved fix scope.\n"
            "- Keep the result minimal, runnable, and internally consistent.\n"
            "- Paths in the CodeBundle must be safe relative project paths.\n"
            f"{mode_fix_rules}\n"
            f"- project_type must be '{project_type}'.\n\n"
            f"Language hint: {language_hint}\n\n"
            "Bug context:\n"
            f"{user_problem}"
        ),
        agent=fixer,
        expected_output="Fixed CodeBundle JSON only (no markdown, no extra text).",
    )

    crew = Crew(agents=[fixer], tasks=[task], process=Process.sequential, verbose=True)
    rendered = str(getattr(task, "description", "") or "")
    setattr(crew, "_prompt_total_chars", len(rendered))
    setattr(crew, "_prompt_hashes", {"project_fix": _text_sha256(rendered)})
    setattr(
        crew,
        "_retry_policy",
        RetryPolicy(max_attempts=2, backoff_seconds=1.0, retry_on_json_fail=True),
    )
    setattr(crew, "_crew_name", "project_fix_crew")
    return crew


def _legacy_build_direction_debate_prompt_bundle(
    *,
    user_problem: str,
    language_hint: str,
    research_block: str,
) -> Dict[str, str]:
    return {
        "explorer": f"""
        你是 [Explorer]。請提出 7 個彼此互斥的方向（A 到 G）。

        規則：
        - 只輸出 JSON，不要 markdown，不要額外文字。
        - 不得引用、反駁或總結其他角色，必須獨立思考。
        - 每個 option 必須包含：key、name、thesis、primary_metric、fastest_test、major_risk。
        - key 只能使用 A/B/C/D/E/F/G。
        - 只能使用下方已 grounding 的 research context。
        - unknowns 只能當作未解約束，不能當作證據。
        - 不得重新引用已被 research context 移除的 unsupported claims。
        - 若證據稀薄，優先提出可逆測試與保守假設。

        語言：{language_hint}

        問題：
        {user_problem}

        Research context：
        {research_block}

        輸出 JSON 結構：
        {{
          "options": [
            {{
              "key": "A",
              "name": "...",
              "thesis": "...",
              "primary_metric": "...",
              "fastest_test": "...",
              "major_risk": "..."
            }}
          ]
        }}
        """,
        "skeptic": f"""
        你是 [Skeptic]。請針對每個方向 A 到 G 提供不可逆風險與 veto reason。

        規則：
        - 只輸出 JSON，不要 markdown，不要額外文字。
        - 不得新增方向，只能使用 A/B/C/D/E/F/G。
        - 不得引用、反駁或總結其他角色，必須獨立思考。
        - 只能使用下方已 grounding 的 research context。
        - unknowns 只能當作未解約束，不能當作已證實風險。
        - 不得重新引用已被 research context 移除的 unsupported claims。
        - 若證據稀薄，請給保守的 veto reason，不可編造細節。

        語言：{language_hint}

        問題：
        {user_problem}

        Research context：
        {research_block}

        輸出 JSON 結構：
        {{
          "risks": [
            {{
              "key": "A",
              "irreversible_risk": "...",
              "veto_reason": "..."
            }}
          ]
        }}
        """,
        "judge": f"""
        你是 [Judge]。請把 Explorer + Skeptic 的輸出整合成 DirectionDecision。

        規則：
        - 只輸出 JSON，不要 markdown，不要額外文字。
        - 不得新增方向，也不得新增觀點。
        - 只能使用已 grounding 的 research context；unsupported claims 與 unknowns 都不能升格為事實。
        - selected_direction 必須是 "A"、"B"、"C"、"D"、"E"、"F"、"G" 或 "none"。
        - options 必須只包含 7 個互斥方向（A 到 G）。
        - 每個 options item 都必須包含 key/name/thesis/primary_metric/fastest_test/major_risk。
        - options 的 key 必須剛好覆蓋 A/B/C/D/E/F/G 各一次。
        - go_conditions、kill_criteria、verify_plan 各 1~5 項，且不可缺欄。
        - confidence 必須是 "low" | "medium" | "high"。
        - 若 evidence coverage 稀薄或 unknowns 過多，必須降低 confidence，並優先選擇 "none" 而不是編造確定性。
        - summary 必須存在。
        - 輸出必須是完整且合法的 DirectionDecision JSON。

        語言：{language_hint}

        問題：
        {user_problem}

        Research context：
        {research_block}
        """,
    }


def _legacy_build_direction_debate_crew(
    user_problem: str,
    mode: str,
    language_hint: str,
    llm: Any,
    direction_judge_llm: Any,
    research_context: Optional[ResearchContext] = None,
) -> Crew:
    mode_config = _validated_mode_config(mode)
    research_block = _render_research_context_for_prompt(research_context)
    direction_prompts = _build_direction_debate_prompt_bundle(
        user_problem=user_problem,
        language_hint=language_hint,
        research_block=research_block,
    )

    explorer = Agent(
        role="Explorer",
        goal="提出 7 個互斥方向（A 到 G），並完整填好 option 欄位。",
        backstory=(
            f"[Explorer] Direction Options ({mode} Mode)\n"
            f"- 聚焦：{mode_config.research_focus}\n"
            "- 提出 7 個互斥方向（A 到 G）\n"
            "- 每個 option 都必須包含 key、name、thesis、primary_metric、fastest_test、major_risk\n"
            "- 只能使用 grounded research context；unknowns 只能保留為未解約束\n"
            "- 只輸出 JSON，不要額外文字\n\n" + NO_CROSS_ROLE_RULE
        ),
        allow_delegation=False,
        verbose=False,
        llm=llm,
    )

    skeptic = Agent(
        role="Skeptic",
        goal="為 A 到 G 每個方向提供不可逆風險與 veto reason。",
        backstory=(
            f"[Skeptic] Direction Risks ({mode} Mode)\n"
            "- 針對每個方向（A 到 G）列出不可逆風險與 veto reason\n"
            "- 不得新增方向\n"
            "- 只能使用 grounded research context；unsupported claims 不得復活\n"
            "- 只輸出 JSON，不要額外文字\n\n" + NO_CROSS_ROLE_RULE
        ),
        allow_delegation=False,
        verbose=False,
        llm=llm,
    )

    judge = Agent(
        role="Judge",
        goal="選出 A 到 G 其中一個方向，或選 none，並輸出 DirectionDecision JSON。",
        backstory=(
            "[Judge] Decision and Convergence\n"
            "- 只能整合 Explorer + Skeptic 的輸出\n"
            "- 不得新增方向或觀點\n"
            "- 只能使用 grounded research context；證據稀薄時必須降低 confidence\n"
            "- 輸出必須是合法的 DirectionDecision JSON，且不得夾帶其他文字\n"
        ),
        allow_delegation=False,
        verbose=False,
        llm=direction_judge_llm,
    )

    explorer_task = Task(
        description=direction_prompts["explorer"],
        agent=explorer,
        expected_output="JSON with options list only.",
    )

    skeptic_task = Task(
        description=direction_prompts["skeptic"],
        agent=skeptic,
        expected_output="JSON with risks per direction.",
    )

    judge_task_kwargs = {
        "description": direction_prompts["judge"],
        "agent": judge,
        "context": [explorer_task, skeptic_task],
        "expected_output": "Valid DirectionDecision JSON only (no markdown, no extra text).",
    }
    if STRICT_JSON_ENABLED and CREWAI_OUTPUT_PYDANTIC:
        judge_task_kwargs["output_pydantic"] = DirectionDecision

    judge_task = Task(**judge_task_kwargs)

    return Crew(
        agents=[explorer, skeptic, judge],
        tasks=[explorer_task, skeptic_task, judge_task],
        process=Process.sequential,
        verbose=False,
    )


def build_crew(user_problem: str, mode: str, language_hint: str, llm: Any) -> Crew:
    mode_config = _validated_mode_config(mode)
    mode_key = _project_type_for_mode(mode)
    gate_guidance = "\n".join(_mode_gate_controller_guidance(mode_config))
    print(f"\n[System] Initializing Crew in {mode} Mode...")
    print(f"[System] Focus Metrics: {mode_config.metrics}\n")

    research_goal = f"基於證據，為 {mode_config.name} 模式產出研究與實作指引，評估指標為：{mode_config.metrics}。"
    research_backstory = (
        f"[Research] 聚焦：{mode_config.research_focus}。\n"
        "- 只能根據提供的問題敘述與 context 建立 claims\n"
        "- 必須清楚指出假設、限制與缺失資訊\n"
        "- 優先輸出精簡、可驗證的 findings，而不是抽象評論\n\n"
        + NO_CROSS_ROLE_RULE
        + COMMON_OUTPUT_RULES
    )
    biz_goal = f"評估與 {mode_config.biz_focus} 相關的市場、分銷與商業可行性。"
    biz_backstory = (
        f"[Biz] 聚焦：{mode_config.biz_focus}。\n"
        "- 分析 ICP 痛點、定價邏輯、GTM 風險與採用摩擦\n"
        "- 所有 claims 都必須具體，且能被商業實驗驗證\n\n"
        + NO_CROSS_ROLE_RULE
        + COMMON_OUTPUT_RULES
    )
    if mode_key == "agent":
        research_goal = f"基於證據，為 {mode_config.name} 模式產出 orchestration 與 automation 指引，評估指標為：{mode_config.metrics}。"
        research_backstory = (
            f"[Research] 聚焦：{mode_config.research_focus}。\n"
            "- 強調 deterministic execution、protocol boundaries 與 recovery paths\n"
            "- 明確指出營運假設與 failure isolation points\n\n"
            + NO_CROSS_ROLE_RULE
            + COMMON_OUTPUT_RULES
        )
        biz_goal = f"評估與 {mode_config.biz_focus} 相關的部署槓桿與營運經濟性。"
        biz_backstory = (
            f"[Biz] 聚焦：{mode_config.biz_focus}。\n"
            "- 評估 operating leverage、變現路徑與 reliability cost\n"
            "- 所有 tradeoff 都要明確，且要貼近實作\n\n" + NO_CROSS_ROLE_RULE + COMMON_OUTPUT_RULES
        )
    elif mode_key == "scientist":
        research_goal = f"基於論文證據，為 {mode_config.name} 模式產出 paper reproduction、baseline comparison 與 algorithm validation 指引，評估指標為：{mode_config.metrics}。"
        research_backstory = (
            f"[Research] 聚焦：{mode_config.research_focus}。\n"
            "- 強調論文忠實複現、hyperparameter 校準、ablation study 設計與 baseline comparison\n"
            "- 明確指出實驗設計假設、reproducibility 邊界條件與可驗證指標\n\n"
            + NO_CROSS_ROLE_RULE
            + COMMON_OUTPUT_RULES
        )
        biz_goal = f"評估與 {mode_config.biz_focus} 相關的研究傳播價值、reproducibility toolkit 潛力與學術/業界影響力。"
        biz_backstory = (
            f"[Biz] 聚焦：{mode_config.biz_focus}。\n"
            "- 評估論文影響力、open-source toolkit market fit 與學術社區採用可能性\n"
            "- 所有 claims 都必須基於實驗結果與可引用文獻，不得引入未驗證的商業假設\n\n"
            + NO_CROSS_ROLE_RULE
            + COMMON_OUTPUT_RULES
        )

    research = Agent(
        role="Research",
        goal=research_goal,
        backstory=research_backstory,
        allow_delegation=False,
        verbose=True,
        llm=llm,
    )

    risk = Agent(
        role="Risk",
        goal=f"找出 {mode_config.name} 模式下最嚴重的執行、產品與可靠性風險。",
        backstory=(
            f"[Risk] 聚焦 {mode_config.name} 模式下的不可逆 downside。\n"
            "- 優先處理 failure modes、隱藏假設與風險集中點\n"
            "- 偏好具體的 veto reason 與 mitigation ideas，不接受空泛警告\n\n"
            + NO_CROSS_ROLE_RULE
            + COMMON_OUTPUT_RULES
        ),
        allow_delegation=False,
        verbose=True,
        llm=llm,
    )

    ops = Agent(
        role="Ops",
        goal="為目前模式設計可執行的營運方案、KPI 迴圈與執行順序。",
        backstory=(
            f"[Ops] 聚焦 {mode_config.name} 模式下的 execution system。\n"
            "- 把策略轉成 milestones、instrumentation 與 operating cadence\n"
            "- 優先選擇低摩擦且能快速驗證的方案\n\n" + NO_CROSS_ROLE_RULE + COMMON_OUTPUT_RULES
        ),
        allow_delegation=False,
        verbose=True,
        llm=llm,
    )

    biz = Agent(
        role="Biz",
        goal=biz_goal,
        backstory=biz_backstory,
        allow_delegation=False,
        verbose=True,
        llm=llm,
    )

    critic = Agent(
        role="Critic",
        goal="在實作前壓測假設、找出矛盾，並阻止脆弱的 MVP 邏輯。",
        backstory=(
            f"[Critic] 質疑 {mode_config.name} 模式中的薄弱推理。\n"
            "- 專打 hidden coupling、假確定性與不必要複雜度\n"
            "- 強迫輸出更銳利的 tradeoff 與更簡單的 MVP 邊界\n\n"
            + NO_CROSS_ROLE_RULE
            + COMMON_OUTPUT_RULES
        ),
        allow_delegation=False,
        verbose=True,
        llm=llm,
    )

    arbiter = Agent(
        role="Gate Controller",
        goal=(
            "整合各角色輸出，決定是否允許進入 code generation，並明確標示 blocking risks、rerun 與 kill conditions。"
        ),
        backstory=(
            "[Gate Controller] 負責最終流程控制決策。\n"
            "- 只有在證據與推理足夠強時才可批准 CodeGen\n"
            "- blocking_risks、rerun signals 與 kill decisions 都必須保守且明確\n"
            f"{gate_guidance}\n\n" + GATE_CONTROLLER_RULES
        ),
        allow_delegation=False,
        verbose=True,
        llm=llm,
    )

    format_checker = Agent(
        role="Format Checker",
        goal="確保最終輸出 100% 符合 JSON Schema。",
        backstory=(
            "你是嚴格的 QA automation engineer。"
            "你的唯一工作是把 Arbiter 輸出轉成合法的 AnalysisReport JSON。"
        ),
        allow_delegation=False,
        verbose=True,
        llm=llm,
    )

    codegen = Agent(
        role="CodeGen",
        goal="嚴格依照 Arbiter 共識生成最小可執行程式碼。",
        backstory=(
            "你是資深軟體工程師。\n"
            "- 只能實作 Arbiter 已批准的內容\n"
            "- 產出必須最小但可執行\n"
            "- SaaS 優先用 FastAPI，Quant 與 Agent 優先用 pure Python\n"
            "- 輸出必須是 file-based JSON\n"
        ),
        allow_delegation=False,
        verbose=True,
        llm=llm,
    )

    base_desc = f"""
    請從你的角色視角獨立分析這個問題。

    問題：
    {user_problem}

    語言：{language_hint}

    要求：
    - 明確指出具體假設、tradeoff 與風險
    - 建議必須與後續實作直接相關
    - 優先使用 evidence、metrics 或 experiments，而不是空泛意見
    """

    common_expected_output = (
        "結構化角色分析，需涵蓋 assumptions、tradeoffs、risks 與 next-step experiments。"
    )

    t_research = Task(description=base_desc, agent=research, expected_output=common_expected_output)
    t_risk = Task(description=base_desc, agent=risk, expected_output=common_expected_output)
    t_ops = Task(description=base_desc, agent=ops, expected_output=common_expected_output)
    t_biz = Task(description=base_desc, agent=biz, expected_output=common_expected_output)
    t_critic = Task(description=base_desc, agent=critic, expected_output=common_expected_output)

    t_arbiter = Task(
        description=f"""
        你是 [Gate Controller]。請把 Research、Risk、Ops、Biz、Critic 的輸出整合成 GateDecision JSON。

        必要 JSON 結構：
        {{
          "consensus": "...",
          "disagreement": "...",
          "experiments": [{{"goal": "...", "criteria": "..."}}],
          "ready_for_codegen": true,
          "blocking_risks": ["..."],
          "required_experiments_before_codegen": ["..."],
          "agents_needing_rerun": ["research|risk|ops|biz|critic"],
          "rerun_reasons": {{"research": "..."}},
          "overall_score": 0,
          "confidence": "low|medium|high",
          "should_kill": false,
          "kill_reason": null
        }}

        規則：
        - 若證據不足，ready_for_codegen 必須設為 false。
        - blocking_risks 只能用於真正的 hard blockers。
        - agents_needing_rerun 只能在特定角色確實需要重跑時才填。
        - should_kill=true 只能用於整個方向應被完全終止的情況。
        {gate_guidance}

        語言：{language_hint}

        問題：
        {user_problem}
        """,
        agent=arbiter,
        context=[t_research, t_risk, t_ops, t_biz, t_critic],
        expected_output="GateDecision JSON only (no markdown, no extra text).",
    )

    t_format = Task(
        description=(
            "請把 Gate Controller 的 GateDecision 輸出轉成 AnalysisReport JSON。"
            f"其中 'mode_used' 必須精確設為 '{mode}'。"
            "請依分析主題生成簡短的 snake_case 'project_name'（例如：'trend_following_btc'）。\n"
            "GateDecision 與 AnalysisReport 的欄位映射如下：\n"
            "- consensus -> consensus\n"
            "- disagreement -> disagreement\n"
            "- experiments -> experiments\n"
            "- overall_score -> score\n"
            "- confidence -> used for risk_level estimation\n"
            "'summary' 必須是對 consensus 與關鍵重點的精簡綜合。"
        ),
        agent=format_checker,
        context=[t_arbiter],
        expected_output="A valid JSON object matching the AnalysisReport schema only (no markdown, no extra text).",
    )

    t_codegen = Task(
        description="""
        只能根據 Arbiter 輸出生成可執行的 MVP 程式碼。

        規則：
        - SaaS mode：使用 FastAPI + Pydantic web service
        - Quant mode：產出策略 + 簡單 backtest（不可用 web framework）
        - Agent mode：產出 headless Python service/daemon，用於 automation 或 orchestration（除非明確需要，否則不要引入 web framework）
        - 程式碼必須最小但可執行
        - 檔案數量與程式碼體積都要盡量小，避免冗長註解與 boilerplate
        - 不要輸出分析文字
        - 輸出 path 必須是 code root 下的相對路徑（不得有前綴 "code/"）
        - Quant mode 必須包含：
          1) 策略邏輯檔（例如 strategy.py）
          2) Backtest runner（例如 backtest.py）
          3) Trading/execution module（例如 trade.py）
          4) Signals/results 匯出邏輯（例如 export.py）
        - Agent mode 應優先包含：
          1) Service entrypoint（例如 main.py）
          2) Orchestrator/runtime module
          3) Configuration module
          4) Deterministic task/decision logic modules

        輸出格式：
        {{
          "project_type": "saas|quant|agent|scientist",
          "files": [
            {{ "path": "main.py", "content": "..." }}
          ]
        }}
        """,
        agent=codegen,
        context=[t_arbiter],
        expected_output="CodeBundle JSON only (no markdown, no extra text).",
    )

    return Crew(
        agents=[research, risk, ops, biz, critic, arbiter, format_checker, codegen],
        tasks=[
            t_research,
            t_risk,
            t_ops,
            t_biz,
            t_critic,
            t_arbiter,
            t_format,
            t_codegen,
        ],
        process=Process.sequential,
        verbose=True,
    )


# =========================
# 4.5) Orchestrated Pipeline (Spec-driven)
# =========================

ANALYST_AGENT_ORDER = ("research", "risk", "ops", "biz", "critic")


# BEGIN MANUAL OUTPUT SAVE OVERRIDES
class CodegenFilePlan(BaseModel):
    path: str = Field(..., description="Safe relative path under the generated project root")
    purpose: str = Field(default="", description="Why this file exists")
    depends_on: List[str] = Field(default_factory=list, description="Relative paths this file depends on")
    must_contain: List[str] = Field(
        default_factory=list,
        description="Concrete elements, interfaces, or behaviors this file must include",
    )


class CodegenBatchPlan(BaseModel):
    name: str = Field(default="batch", description="Human-readable batch label")
    objective: str = Field(default="", description="What this batch is responsible for")
    files: List[str] = Field(
        default_factory=list,
        description="Relative paths that should be generated together in this batch",
    )


class CodegenManifest(BaseModel):
    project_type: str = Field(..., description="saas, quant, agent, or scientist")
    architecture_summary: str = Field(
        default="",
        description="Short architecture contract that later codegen batches must preserve",
    )
    entrypoints: List[str] = Field(
        default_factory=list,
        description="Expected entrypoint or bootstrap files for the generated project",
    )
    shared_constraints: List[str] = Field(
        default_factory=list,
        description="Cross-file constraints that every batch must preserve",
    )
    files: List[CodegenFilePlan] = Field(
        default_factory=list,
        description="Planned files with responsibilities and dependency hints",
    )
    generation_batches: List[CodegenBatchPlan] = Field(
        default_factory=list,
        description="Ordered file batches for staged code generation",
    )
# END MANUAL OUTPUT SAVE OVERRIDES
