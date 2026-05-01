# Auto-generated from OLD_version/crucible_v14.py.
# Import-based section module. Do not edit manually; regenerate from V14.
from __future__ import annotations

from . import section_00_bootstrap_and_utils as _prev_00
globals().update({k: v for k, v in _prev_00.__dict__.items() if not k.startswith('__')})
from . import section_01_extraction_and_reformat as _prev_01
globals().update({k: v for k, v in _prev_01.__dict__.items() if not k.startswith('__')})
from . import section_02_research_and_llm as _prev_02
globals().update({k: v for k, v in _prev_02.__dict__.items() if not k.startswith('__')})
from . import section_03_models_and_context as _prev_03
globals().update({k: v for k, v in _prev_03.__dict__.items() if not k.startswith('__')})
from . import section_04_web_research_and_direction as _prev_04
globals().update({k: v for k, v in _prev_04.__dict__.items() if not k.startswith('__')})
if __package__ == "crucible.modules":
    from ..resilience import kickoff_crew_with_retry, is_transient_retryable_error
    from ..runtime_logging import get_logger, log_event, log_exception
else:  # pragma: no cover - direct script fallback
    from resilience import kickoff_crew_with_retry, is_transient_retryable_error
    from runtime_logging import get_logger, log_event, log_exception


LOGGER = get_logger(__name__)

# Maximum tokens the codegen LLM is allowed to generate.
# Reasoning models (minimax-m2.7, GLM-5.1, etc.) dedicate a large fraction of
# their output budget to chain-of-thought.  Observed pattern: with max_tokens=32768
# the model can consume 20 000-25 000 tokens on reasoning, leaving only ~8 000 for
# actual code — not enough for a multi-file project.  Raising the default to 65536
# gives the model room to think AND produce complete file output on models that
# support it; models hard-capped at 32768 by the provider will simply use their
# native maximum (the API enforces the lower bound transparently).
# Override with the CODEGEN_MAX_TOKENS env var (e.g. CODEGEN_MAX_TOKENS=131072).
# Use ``_env_int`` (inherited from section_00 via the module chain) so that a
# malformed env-var value (``CODEGEN_MAX_TOKENS=abc``) falls back to the default
# instead of crashing module import with ``ValueError: invalid literal for int()``
# — which would render the entire pipeline unusable until the env var is fixed.
_codegen_max_tokens_env = _env_int("CODEGEN_MAX_TOKENS", 65536)
CODEGEN_MAX_TOKENS: int = (
    _codegen_max_tokens_env
    if _codegen_max_tokens_env is not None and _codegen_max_tokens_env > 0
    else 65536
)

# Maximum number of missing files for which the supplement retry is attempted.
# When a batch produces a partial bundle (some files present but a small number
# missing due to token truncation), a targeted follow-up call is made for just
# those files instead of re-running the entire batch.
_supplement_max_missing_env = _env_int("CODEGEN_SUPPLEMENT_MAX_MISSING", 4)
_SUPPLEMENT_MAX_MISSING_FILES: int = (
    _supplement_max_missing_env
    if _supplement_max_missing_env is not None and _supplement_max_missing_env > 0
    else 4
)

# Lenient-output mode: when codegen validation/recovery has exhausted every
# strict path and would otherwise raise (losing 6+ minutes of LLM output),
# salvage whatever partial files exist and let the user inspect / fix manually.
# Default is ON ("favour producing output over strict correctness").  Set
# ``CODEGEN_LENIENT_OUTPUT=0`` to revert to the historical strict behaviour
# where any validation failure aborts the pipeline.
#
# Salvage path keeps:
#   - All sanitisable files (paths are safe, content is a string)
#   - Files even if they have Python syntax errors (user-fixable manually)
#   - Bundles that are missing some planned files (LLM didn't finish)
#
# Salvage path drops:
#   - Files with unsafe paths (absolute, ../, control characters)
#   - Files outside the batch plan's allowed scope (anti-hallucination)
#   - The mode-mismatch case (bundle declared the wrong project_type — that
#     is a fundamental violation of the user's mode selection, not a partial
#     output condition)
CODEGEN_LENIENT_OUTPUT: bool = _env_bool("CODEGEN_LENIENT_OUTPUT", True)

# Never-terminate guarantees: when any of the four codegen stages fails in a
# way that strict mode would raise on, the following three flags shift the
# pipeline into salvage-and-continue mode so the user always gets *some*
# saved_projects/.../code/ output.  Defaults are ON; set the corresponding
# env var to ``0`` for the historical strict behaviour (e.g. CI gates).
#
#   CODEGEN_FALLBACK_MANIFEST   — When the LLM-produced manifest cannot be
#                                 parsed or the manifest stage raises, a
#                                 minimal single-batch manifest is synthesised
#                                 from analysis_report so batches can still
#                                 attempt code generation.
#
#   CODEGEN_BATCH_SKIP_ON_ERROR — When a single batch raises (LLM API error,
#                                 JSON unparseable, every supplement and
#                                 lenient salvage exhausted), the failure is
#                                 logged and recorded in result_payload's
#                                 ``batch_failures``; the pipeline continues
#                                 with the next batch instead of aborting.
#
#   CODEGEN_SKELETON_FALLBACK   — When every batch failed and zero salvageable
#                                 files survived, an explicit skeleton bundle
#                                 (README.md describing the failure +
#                                 entrypoint stubs that exit non-zero) is
#                                 emitted so the user always has a directory
#                                 they can inspect and edit.
CODEGEN_FALLBACK_MANIFEST: bool = _env_bool("CODEGEN_FALLBACK_MANIFEST", True)
CODEGEN_BATCH_SKIP_ON_ERROR: bool = _env_bool("CODEGEN_BATCH_SKIP_ON_ERROR", True)
CODEGEN_SKELETON_FALLBACK: bool = _env_bool("CODEGEN_SKELETON_FALLBACK", True)

# Active-repair retry budgets.  Before any of the passive degrade paths above
# are taken, the pipeline retries the failing stage / runs a cross-batch
# repair pass that regenerates only the missing or broken files.  Defaults
# trade ~30-50% extra LLM tokens for dramatically higher first-run success
# rate; CI gates that demand fast-fail can set the corresponding env to ``0``.
#
#   CODEGEN_MANIFEST_RETRY_MAX_ATTEMPTS — Total attempts (including the
#       initial one) for the manifest stage before falling back to
#       ``_synthesize_fallback_manifest``.  Set to 1 to disable retry.
#
#   CODEGEN_BATCH_RETRY_MAX_ATTEMPTS — Total attempts per batch before the
#       batch is recorded in ``batch_failures`` and the loop skips to the
#       next batch.  Set to 1 to disable retry.
#
#   CODEGEN_REPAIR_LOOP_MAX_ATTEMPTS — Maximum passes of the cross-batch
#       repair loop that runs after the main batch loop completes and
#       before the finalize step.  Each pass identifies missing planned
#       files and files with Python syntax errors across the cumulative
#       bundle, then dispatches a focused regeneration batch.  The loop
#       stops early when nothing remains to fix or no progress is made.
#       Set to 0 to disable the repair loop entirely.
_codegen_manifest_retry_env = _env_int("CODEGEN_MANIFEST_RETRY_MAX_ATTEMPTS", 2)
CODEGEN_MANIFEST_RETRY_MAX_ATTEMPTS: int = (
    _codegen_manifest_retry_env
    if _codegen_manifest_retry_env is not None and _codegen_manifest_retry_env >= 1
    else 2
)
_codegen_batch_retry_env = _env_int("CODEGEN_BATCH_RETRY_MAX_ATTEMPTS", 2)
CODEGEN_BATCH_RETRY_MAX_ATTEMPTS: int = (
    _codegen_batch_retry_env
    if _codegen_batch_retry_env is not None and _codegen_batch_retry_env >= 1
    else 2
)
_codegen_repair_loop_env = _env_int("CODEGEN_REPAIR_LOOP_MAX_ATTEMPTS", 3)
CODEGEN_REPAIR_LOOP_MAX_ATTEMPTS: int = (
    _codegen_repair_loop_env
    if _codegen_repair_loop_env is not None and _codegen_repair_loop_env >= 0
    else 3
)


def _make_codegen_llm(main_llm: Any) -> Any:
    """Return an LLM instance capped at CODEGEN_MAX_TOKENS for code generation.

    Mirrors _make_formatter_llm (section_01) but with a much larger token budget:
    codegen tasks produce tens of KB of source files whereas formatter tasks only
    produce compact JSON structures.  Falls back to ``main_llm`` unchanged if the
    LLM cannot be reconstructed (e.g. non-standard LLM object).
    """
    try:
        from crewai import LLM as _CrewAI_LLM  # local import — avoids circular deps

        model_id: str = str(
            getattr(main_llm, "model", None)
            or getattr(main_llm, "model_name", None)
            or ""
        )
        if not model_id:
            return main_llm  # Cannot determine model; use as-is

        kwargs: Dict[str, Any] = {
            "model": model_id,
            "provider": "openai",
            "temperature": float(getattr(main_llm, "temperature", 0.7) or 0.7),
            "max_tokens": CODEGEN_MAX_TOKENS,
        }
        for attr, key in (("api_key", "api_key"), ("base_url", "base_url")):
            val = getattr(main_llm, attr, None)
            if val:
                kwargs[key] = val
        timeout_raw = getattr(main_llm, "timeout", None)
        if timeout_raw is not None:
            try:
                kwargs["timeout"] = float(timeout_raw)
            except (TypeError, ValueError):
                pass

        codegen_llm = _CrewAI_LLM(**kwargs)
        # Carry over the provider tag so cost tracking / cache keying stays correct.
        try:
            provider_tag = getattr(main_llm, "_quant_llm_provider", None)
            if provider_tag:
                setattr(codegen_llm, "_quant_llm_provider", provider_tag)
        except Exception:
            pass
        return codegen_llm
    except Exception:
        return main_llm  # Fallback: use main_llm unchanged


def _attach_crew_prompt_metrics(
    crew: Crew,
    *,
    task_specs: List[TaskSpec],
    tasks: List[Task],
) -> None:
    prompt_metrics: Dict[str, Dict[str, Any]] = {}
    total_prompt_chars = 0
    for spec, task in zip(task_specs, tasks):
        prompt_chars = int(len(getattr(task, "description", "") or ""))
        total_prompt_chars += prompt_chars
        prompt_metrics[spec.name] = {
            "prompt_chars": prompt_chars,
            "budget_chars": getattr(spec, "max_input_chars", None),
            "context_count": len(getattr(spec, "context_task_names", []) or []),
            "truncated": bool(getattr(task, "_prompt_truncated", False)),
        }
    setattr(crew, "_prompt_metrics", prompt_metrics)
    setattr(crew, "_prompt_total_chars", total_prompt_chars)


def _prompt_chars_for_crew(crew: Any, fallback_text: str = "") -> int:
    try:
        prompt_chars = int(getattr(crew, "_prompt_total_chars", 0) or 0)
    except Exception:
        prompt_chars = 0
    if prompt_chars > 0:
        return prompt_chars
    return len(str(fallback_text or ""))


def _prompt_tokens_for_crew(crew: Any, fallback_text: str = "") -> int:
    return max(0, _prompt_chars_for_crew(crew, fallback_text) // 3)


def _analyst_task_callback(task_output: Any) -> None:
    """Print a per-analyst completion marker after each task finishes.

    Defined at module scope (not as a closure inside
    :func:`build_analysis_crew`) so pydantic can serialise the Crew
    object during checkpointing — the legacy closure form emitted
    ``UserWarning: function callbacks cannot be serialized and will
    prevent checkpointing`` on every analysis kickoff.  Fixed in v16.9.72
    along with the symmetric ``_research_task_callback`` in
    :mod:`crucible.modules.section_04_web_research_and_direction`.
    """
    try:
        agent_role = getattr(task_output, "agent", "") or ""
        task_name = getattr(task_output, "name", "") or ""
        print(f"\n[Analyst] {agent_role} ({task_name}) ✓ done", flush=True)
    except Exception:
        pass


def build_analysis_crew(
    user_problem: str,
    mode: str,
    language_hint: str,
    llm: Any,
    *,
    active_roles: Optional[Set[str]] = None,
    rerun_note: Optional[str] = None,
    direction_feedback_enabled: bool = False,
) -> Crew:
    mode_config = _get_mode_config(mode)
    if active_roles is None:
        active = set(ANALYST_AGENT_ORDER)
    else:
        active = set(active_roles) & set(ANALYST_AGENT_ORDER)

    agent_specs, task_specs, task_vars = _build_analysis_specs(
        mode_config=mode_config,
        active_roles=active,
        direction_feedback_enabled=direction_feedback_enabled,
    )
    agents = {
        name: _create_agent_from_spec(spec, llm) for name, spec in agent_specs.items()
    }

    effective_problem = user_problem
    if rerun_note:
        effective_problem = (
            user_problem + "\n\n=== SELECTIVE RERUN NOTE ===\n" + rerun_note
        )

    base_template_vars: Dict[str, str] = {
        "mode_name": mode_config.name,
        "language_hint": language_hint,
        "user_problem": effective_problem,
    }
    base_template_vars.update(
        {
            k: v
            for k, v in task_vars.items()
            if not k.endswith("_role_name") and not k.endswith("_focus")
        }
    )
    task_lookup: Dict[str, Task] = {}
    ordered_tasks: List[Task] = []
    for spec in task_specs:
        task_template_vars = dict(base_template_vars)
        if spec.name in active:
            task_template_vars["role_name"] = task_vars.get(
                f"{spec.name}_role_name", spec.name
            )
            task_template_vars["focus"] = task_vars.get(f"{spec.name}_focus", "")
        task = _build_task_from_spec(
            spec,
            agents=agents,
            task_lookup=task_lookup,
            template_vars=task_template_vars,
        )
        task_lookup[spec.name] = task
        ordered_tasks.append(task)

    ordered_agents = [
        agents[name]
        for name in list(ANALYST_AGENT_ORDER) + ["gate_controller", "format_checker"]
        if name in agents
    ]

    crew = Crew(
        agents=ordered_agents,
        tasks=ordered_tasks,
        process=Process.sequential,
        verbose=True,
        # NOTE: `_analyst_task_callback` is defined at module scope (NOT a
        # closure here) so pydantic's checkpoint serialiser can pickle the
        # Crew object \u2014 closures emit `UserWarning: function callbacks
        # cannot be serialized and will prevent checkpointing` on every
        # crew kickoff (regression introduced in v16.9.70 by the
        # symmetric research-swarm callback, fixed in v16.9.72).
        task_callback=_analyst_task_callback,
    )
    prompt_hashes = _compute_task_prompt_hashes(
        task_specs,
        base_template_vars=base_template_vars,
        role_template_vars=task_vars,
        active_roles=active,
    )
    setattr(crew, "_prompt_hashes", prompt_hashes)
    _attach_crew_prompt_metrics(crew, task_specs=task_specs, tasks=ordered_tasks)
    setattr(crew, "_dag_snapshot", _build_agent_dag_snapshot(agent_specs, task_specs))
    setattr(crew, "_task_names", [spec.name for spec in task_specs])
    setattr(
        crew,
        "_retry_policy",
        _aggregate_retry_policy(agent_specs, retry_policy_cls=RetryPolicy),
    )
    setattr(crew, "_crew_name", "analysis_crew")
    return crew


MAX_DIRECTION_FEEDBACK_BOUNCES = 2


def _validation_scope_codegen_rule_lines(mode_config: "ModeConfig") -> List[str]:
    rules = [
        "- Output CodeBundle JSON only",
        "- Paths must be relative and must not start with 'code/'",
        "- Keep implementation minimal and executable",
        "- Validation-first scope is active; generate only the measurement/calibration/verification scaffold that the gate approved",
        f"- Stay within {mode_config.name} mode boundaries; do not leak assumptions or deliverables from other modes",
        "- Do not present the output as a production-ready strategy, product, or alpha implementation",
        "- Keep thresholds, assumptions, and scoring rules configurable until empirical validation completes",
        "- Include machine-readable outputs such as reports, metrics, logs, or comparison artifacts that directly measure the unresolved assumptions",
    ]
    # Quant validation scope: even a calibration scaffold must follow the automated backtest
    # runner's contract so --backtest-runner works without manual intervention.
    if _validated_mode_project_type(mode_config) == "quant":
        rules += [
            "- Any backtest.py or measurement script that processes OHLCV data must read its data from "
            "os.environ.get('BACKTEST_DATA_FILE', 'data/sample_data.csv') — "
            "the automated backtest runner injects this variable at runtime",
            "- If backtest.py is generated, its final stdout line must be a single JSON object with at minimum: "
            "sharpe_ratio (float), total_return_pct (float), max_drawdown_pct (float), "
            "win_rate (float 0–1), trade_count (int) — "
            "this lets the backtest runner parse results without code changes",
        ]
    return rules


def _codegen_scope_prompts(scope: str) -> tuple:
    """Return (agent_goal, agent_backstory, task_description_header) for the given codegen scope."""
    scope = str(scope or "mvp").strip().lower()
    if scope == "production":
        return (
            "Generate a complete, production-ready system including all modules, tests, Docker, and CI as described in the rules.",
            (
                "You are a senior software engineer building a production-grade system.\n"
                "- Generate every module completely — no stubs, no TODOs, no placeholders.\n"
                "- Include the full test suite, Dockerfile, and CI configuration.\n"
                "- Apply proper error handling, type annotations, and structured logging throughout.\n"
                "- Stay strictly inside the approved gate context."
            ),
            "Generate a production-ready implementation.",
        )
    if scope == "full":
        return (
            "Generate a complete, modular implementation covering all required subsystems as described in the rules.",
            (
                "You are a senior software engineer building a complete modular system.\n"
                "- Generate every module fully — not a minimal prototype.\n"
                "- Cover all subsystems, edge cases, and configuration specified in the rules.\n"
                "- Do not include analysis text.\n"
                "- Stay strictly inside the approved gate context."
            ),
            "Generate a full-scope implementation.",
        )
    # mvp (default)
    return (
        "Generate the smallest runnable implementation allowed by the approved gate context.",
        (
            "You are a senior software engineer.\n"
            "- Produce the minimal runnable artifact set.\n"
            "- Do not include analysis text.\n"
            "- Stay strictly inside the approved gate context."
        ),
        "Generate runnable MVP code.",
    )


def _resolved_codegen_rule_lines(
    mode_config: "ModeConfig",
    gate_decision: Optional[GateDecision],
    scope: str = "mvp",
) -> List[str]:
    # Validation-scope always wins regardless of user-selected scope.
    if _gate_is_validation_scope(gate_decision):
        return _validation_scope_codegen_rule_lines(mode_config)
    return list(_mode_codegen_rule_lines(mode_config, scope=scope))


def build_codegen_crew(
    user_problem: str,
    mode: str,
    language_hint: str,
    llm: Any,
    analysis_report: Optional[AnalysisReport],
    gate_decision: Optional[GateDecision],
    scope: str = "mvp",
) -> Crew:
    mode_config = _get_mode_config(mode)
    project_type = _project_type_for_mode(mode)
    approved_context = limit_text(
        build_conditional_codegen_context(gate_decision, analysis_report),
        14000,
    )
    mode_rule_text = "\n".join(_resolved_codegen_rule_lines(mode_config, gate_decision, scope=scope))
    # Validation-scope gate overrides user scope for agent prompts to keep goal/rules consistent.
    effective_prompt_scope = "mvp" if _gate_is_validation_scope(gate_decision) else scope
    agent_goal, agent_backstory, task_header = _codegen_scope_prompts(effective_prompt_scope)
    agent_spec = AgentSpec(
        name="codegen",
        role="CodeGen",
        goal=agent_goal,
        backstory=agent_backstory,
        output_schema_name="CodeBundle",
        parallel_safe=False,
        retry_policy=RetryPolicy(max_attempts=20, backoff_seconds=2.0, retry_on_json_fail=True),
        version="v1.0.0",
        behavior_contract=f"Generate {effective_prompt_scope}-scope runnable code from approved gate context only.",
    )
    task_spec = TaskSpec(
        name="codegen",
        description_template=(
            f"{task_header}\n"
            "Mode: {mode_name}\n"
            "project_type must be '{project_type}'.\n"
            "Language hint: {language_hint}\n\n"
            "User problem:\n{user_problem}\n\n"
            "Approved context:\n{approved_context}\n\n"
            "Rules:\n"
            "{mode_rule_text}"
        ),
        agent_name="codegen",
        expected_output="CodeBundle JSON only.",
        max_input_chars=18000,
    )
    agents = {"codegen": _create_agent_from_spec(agent_spec, llm)}
    template_vars = {
        "mode_name": mode_config.name,
        "project_type": project_type,
        "language_hint": language_hint,
        "user_problem": limit_text(user_problem, 4000),
        "approved_context": approved_context,
        "mode_rule_text": mode_rule_text,
    }
    task = _build_task_from_spec(
        task_spec,
        agents=agents,
        task_lookup={},
        template_vars=template_vars,
    )
    crew = Crew(
        agents=[agents["codegen"]],
        tasks=[task],
        process=Process.sequential,
        verbose=True,
    )
    rendered = _render_prompt_template(task_spec.description_template, template_vars)
    setattr(crew, "_prompt_hashes", {"codegen": _text_sha256(rendered)})
    setattr(
        crew,
        "_prompt_metrics",
        {
            "codegen": {
                "prompt_chars": len(str(rendered)),
                "budget_chars": getattr(task_spec, "max_input_chars", None),
                "context_count": 0,
                "truncated": "...[truncated]..." in str(rendered),
            }
        },
    )
    setattr(crew, "_prompt_total_chars", len(str(rendered)))
    setattr(
        crew,
        "_dag_snapshot",
        _build_agent_dag_snapshot({"codegen": agent_spec}, [task_spec]),
    )
    setattr(crew, "_retry_policy", agent_spec.retry_policy)
    setattr(crew, "_crew_name", "codegen_crew")
    return crew


def _build_codegen_timeout_recovery_crew(
    user_problem: str,
    *,
    mode: str,
    language_hint: str,
    llm: Any,
    analysis_report: Optional[AnalysisReport],
    gate_decision: Optional[GateDecision],
    scope: str = "mvp",
) -> Crew:
    mode_config = _get_mode_config(mode)
    project_type = _project_type_for_mode(mode)
    approved_context = limit_text(
        build_conditional_codegen_context(gate_decision, analysis_report),
        8000,
    )
    mode_rule_text = "\n".join(_resolved_codegen_rule_lines(mode_config, gate_decision, scope=scope))
    # Validation-scope gate overrides user scope for recovery agent prompts too.
    effective_prompt_scope = "mvp" if _gate_is_validation_scope(gate_decision) else str(scope or "mvp").strip().lower()
    if effective_prompt_scope in ("full", "production"):
        recovery_goal = (
            f"Recover from a transient code generation failure and emit a valid "
            f"{effective_prompt_scope}-scope CodeBundle JSON response."
        )
        recovery_backstory = (
            "You are handling a retry after an upstream timeout.\n"
            "- Return only one valid CodeBundle JSON object.\n"
            f"- Attempt a {effective_prompt_scope}-scope implementation following the rules; "
            "if context is too limited, produce the most complete runnable artifact set you can.\n"
            "- Do not silently downgrade to a minimal scaffold without attempting the full scope."
        )
        recovery_label = effective_prompt_scope.upper()
    else:
        recovery_goal = (
            "Recover from a transient code generation failure and emit a minimal valid CodeBundle JSON response."
        )
        recovery_backstory = (
            "You are handling a retry after an upstream timeout.\n"
            "- Return only one valid CodeBundle JSON object.\n"
            "- Prefer the smallest runnable artifact set that still satisfies the approved gate context.\n"
            "- If context is incomplete, choose a conservative minimal scaffold over prose."
        )
        recovery_label = "MVP"
    generator = Agent(
        role="CodeGen Recovery",
        goal=recovery_goal,
        backstory=recovery_backstory,
        allow_delegation=False,
        verbose=False,
        llm=llm,
    )
    task = Task(
        description=(
            f"Recover runnable {recovery_label} code generation after a transient timeout.\n"
            f"Mode: {mode_config.name}\n"
            f"project_type must be '{project_type}'.\n"
            f"Language hint: {language_hint}\n\n"
            "Return exactly one valid CodeBundle JSON object.\n"
            "No markdown, no commentary, no code fences.\n\n"
            "User problem:\n"
            f"{limit_text(user_problem, 4000)}\n\n"
            "Approved context (reduced for timeout recovery):\n"
            f"{approved_context}\n\n"
            "Rules:\n"
            f"{mode_rule_text}"
        ),
        agent=generator,
        expected_output="CodeBundle JSON only.",
    )
    crew = Crew(
        agents=[generator],
        tasks=[task],
        process=Process.sequential,
        verbose=False,
    )
    setattr(
        crew,
        "_retry_policy",
        RetryPolicy(max_attempts=6, backoff_seconds=3.0, retry_on_json_fail=True),
    )
    setattr(crew, "_crew_name", "codegen_crew_fallback")
    return crew


def _kickoff_codegen_with_timeout_recovery(
    primary_crew: Crew,
    *,
    fallback_crew_factory: Callable[[], Crew],
    mode: str,
) -> Any:
    try:
        return kickoff_crew_with_retry(
            primary_crew,
            logger=LOGGER,
            log_fields={"stage": "codegen", "mode": mode},
        )
    except _OperationCancelledError:
        # Cooperative cancellation must abort before any fallback is attempted.
        raise
    except Exception as exc:
        if not is_transient_retryable_error(exc):
            raise
        log_event(
            LOGGER,
            30,
            "codegen_timeout_recovery_start",
            "Primary codegen kickoff failed transiently; starting reduced-context fallback.",
            mode=mode,
            error_type=type(exc).__name__,
        )
        fallback_crew = fallback_crew_factory()
        return kickoff_crew_with_retry(
            fallback_crew,
            crew_name="codegen_crew_fallback",
            logger=LOGGER,
            log_fields={"stage": "codegen_fallback", "mode": mode},
        )


def _parse_analysis_outputs(
    result: Any, *, llm: Any, language_hint: str, mode: str
) -> Tuple[Optional[AnalysisReport], Optional[GateDecision]]:
    report = extract_analysis_report(result, mode=mode)
    gate = extract_gate_decision(result)

    raw_output = _extract_text_from_result(result)
    if raw_output:
        if report is None:
            report = extract_analysis_report(raw_output, mode=mode)
        if gate is None:
            gate = extract_gate_decision(raw_output)

    if report is None or gate is None:
        for raw in reversed(_collect_text_candidates_from_result(result)):
            if report is None:
                report = extract_analysis_report(raw, mode=mode)
                if report is None and STRICT_JSON_ENABLED:
                    report = _reformat_analysis_report(
                        raw, llm=llm, language_hint=language_hint, mode=mode
                    )
            if gate is None:
                gate = extract_gate_decision(raw)
                if gate is None and STRICT_JSON_ENABLED:
                    gate = _reformat_gate_decision(
                        raw, llm=llm, language_hint=language_hint
                    )
            if report is not None and gate is not None:
                break

    gate = _normalize_gate_decision(gate, mode=mode)
    failure_type, failure_details = _classify_gate_failure(gate)
    if failure_type != FailureType.NONE:
        _apply_gate_failure(gate, failure_type, failure_details)

    return report, gate


def _gate_requests_direction_feedback(
    gate: Optional[GateDecision],
) -> bool:
    if gate is None or gate.should_kill or gate.ready_for_codegen:
        return False
    if bool(getattr(gate, "direction_feedback_needed", False)):
        return True
    if _classify_direction_feedback_type(gate) in {"evidence", "detail"}:
        return True
    evidence_text = " ".join(
        [
            str(getattr(gate, "disagreement", "") or ""),
            str(getattr(gate, "failure_details", "") or ""),
            " ".join(list(getattr(gate, "blocking_risks", []) or [])),
            " ".join(list((getattr(gate, "rerun_reasons", {}) or {}).values())),
        ]
    ).lower()
    if not evidence_text:
        return False
    evidence_markers = (
        "evidence",
        "insufficient",
        "missing",
        "detail",
        "details",
        "unknown",
        "uncertain",
        "uncertainty",
        "citation",
        "grounded",
        "support",
        "coverage",
        "data",
        "證據",
        "資料不足",
        "證據不足",
        "細節不足",
        "缺少",
        "缺乏",
        "不確定",
        "未知",
        "資料",
        "依據",
        "流程",
        "步驟",
        "參數",
        "門檻",
        "規格",
    )
    return bool(gate.agents_needing_rerun or gate.confidence == "low") and any(
        marker in evidence_text for marker in evidence_markers
    )


def _classify_direction_feedback_type(
    gate: Optional[GateDecision],
) -> Optional[str]:
    if gate is None or gate.should_kill or gate.ready_for_codegen:
        return None
    explicit = str(getattr(gate, "direction_feedback_type", "") or "").strip().lower()
    if explicit in {"evidence", "detail"}:
        return explicit
    feedback_text = " ".join(
        [
            str(getattr(gate, "direction_feedback_reason", "") or ""),
            str(getattr(gate, "disagreement", "") or ""),
            " ".join(list(getattr(gate, "direction_feedback_evidence_gaps", []) or [])),
            " ".join(list(getattr(gate, "direction_feedback_questions", []) or [])),
            " ".join(list((getattr(gate, "rerun_reasons", {}) or {}).values())),
        ]
    ).lower()
    if not feedback_text:
        return None
    evidence_markers = (
        "evidence",
        "citation",
        "source",
        "sources",
        "proof",
        "grounded",
        "research",
        "data",
        "資料",
        "證據",
        "數據",
        "來源",
        "依據",
        "驗證",
    )
    detail_markers = (
        "detail",
        "details",
        "implementation",
        "workflow",
        "step",
        "steps",
        "parameter",
        "parameters",
        "threshold",
        "thresholds",
        "sequence",
        "spec",
        "specification",
        "流程",
        "步驟",
        "參數",
        "門檻",
        "規格",
        "細節",
        "刷新",
        "頻率",
    )
    if any(marker in feedback_text for marker in evidence_markers):
        return "evidence"
    if any(marker in feedback_text for marker in detail_markers):
        return "detail"
    return None


def _collect_analysis_task_details(crew: Any, result: Any) -> Dict[str, str]:
    task_names = list(getattr(crew, "_task_names", []) or [])
    details: Dict[str, str] = {}
    for index, task_output in enumerate(_get_task_outputs(result)):
        task_name = task_names[index] if index < len(task_names) else f"task_{index}"
        if task_name not in set(ANALYST_AGENT_ORDER) | {"gate_controller"}:
            continue
        raw_text = _extract_text_from_result(task_output) or str(
            getattr(task_output, "raw", "") or ""
        )
        normalized = re.sub(r"\s+", " ", raw_text).strip()
        if normalized:
            details[task_name] = limit_text(normalized, 280)
    return details


def _build_direction_feedback_note(
    *,
    gate: GateDecision,
    report: Optional[AnalysisReport],
    task_details: Dict[str, str],
    rerun_attempt: int,
    feedback_type: Optional[str],
) -> str:
    lines = [
        "Direction debate feedback",
        f"Rerun attempt: {rerun_attempt}",
        f"Feedback path: {feedback_type or 'detail'}",
        f"Gate consensus: {limit_text(gate.consensus, 240)}",
        f"Gate disagreement: {limit_text(gate.disagreement, 240)}",
    ]
    if report is not None:
        lines.append(f"Previous analysis summary: {limit_text(report.summary, 280)}")
    feedback_reason = str(getattr(gate, "direction_feedback_reason", "") or "").strip()
    if feedback_reason:
        lines.append(f"Feedback reason: {limit_text(feedback_reason, 280)}")
    evidence_gaps = list(getattr(gate, "direction_feedback_evidence_gaps", []) or [])
    if not evidence_gaps:
        evidence_gaps = _dedupe_text_items(
            list((getattr(gate, "rerun_reasons", {}) or {}).values()), limit=4
        )
    if evidence_gaps:
        lines.append("Evidence gaps:")
        lines.extend(f"- {limit_text(item, 220)}" for item in evidence_gaps[:5])
    feedback_questions = list(
        getattr(gate, "direction_feedback_questions", []) or []
    )
    if feedback_questions:
        lines.append("Direction questions:")
        lines.extend(f"- {limit_text(item, 220)}" for item in feedback_questions[:5])
    if task_details:
        lines.append("Analyst details:")
        for task_name in ANALYST_AGENT_ORDER:
            if task_name in task_details:
                lines.append(f"- {task_name}: {task_details[task_name]}")
        if "gate_controller" in task_details:
            lines.append(f"- gate_controller: {task_details['gate_controller']}")
    if feedback_type == "evidence":
        lines.append(
            "Re-open the direction debate, gather missing evidence, and return selected_direction='none' only if the direction is fundamentally not viable."
        )
    else:
        lines.append(
            "Do not re-open the direction debate unless explicitly required. Focus on the missing implementation detail, parameters, and workflow specifics."
        )
    return "\n".join(lines)


def _build_refined_direction_context(
    refined_direction: Optional[DirectionDecision],
) -> List[str]:
    if refined_direction is None:
        return []
    lines = [
        "Refined direction decision: "
        + limit_text(
            f"Direction {refined_direction.selected_direction} | "
            f"confidence={refined_direction.confidence} | "
            f"summary={refined_direction.summary}",
            420,
        )
    ]
    selected_key = str(getattr(refined_direction, "selected_direction", "") or "").strip()
    selected_option = None
    for option in list(getattr(refined_direction, "options", []) or []):
        if str(getattr(option, "key", "") or "").strip() == selected_key:
            selected_option = option
            break
    if selected_option is not None:
        lines.append(
            "Refined direction thesis: "
            + limit_text(str(getattr(selected_option, "thesis", "") or ""), 320)
        )
        lines.append(
            "Refined primary metric: "
            + limit_text(str(getattr(selected_option, "primary_metric", "") or ""), 220)
        )
        lines.append(
            "Refined fastest test: "
            + limit_text(str(getattr(selected_option, "fastest_test", "") or ""), 220)
        )
        lines.append(
            "Refined major risk: "
            + limit_text(str(getattr(selected_option, "major_risk", "") or ""), 220)
        )
    backup_candidates = [
        str(item).strip()
        for item in list(getattr(refined_direction, "backup_candidates", []) or [])
        if str(item).strip()
    ]
    if backup_candidates:
        lines.append("Refined backup candidates: " + ", ".join(backup_candidates[:4]))
    go_conditions = list(getattr(refined_direction, "go_conditions", []) or [])
    if go_conditions:
        lines.append("Refined go conditions:")
        lines.extend(f"- {limit_text(str(item), 220)}" for item in go_conditions[:5])
    kill_criteria = list(getattr(refined_direction, "kill_criteria", []) or [])
    if kill_criteria:
        lines.append("Refined kill criteria:")
        lines.extend(f"- {limit_text(str(item), 220)}" for item in kill_criteria[:5])
    verify_plan = list(getattr(refined_direction, "verify_plan", []) or [])
    if verify_plan:
        lines.append("Refined verify plan:")
        lines.extend(f"- {limit_text(str(item), 220)}" for item in verify_plan[:5])
    return lines


def _resolve_selective_rerun_max_attempts_default() -> int:
    configured = _env_int("SELECTIVE_RERUN_MAX_ATTEMPTS", 5)
    if configured is None:
        return 5
    return max(0, int(configured))


def run_analysis_with_selective_rerun(
    user_problem: str,
    *,
    mode: str,
    language_hint: str,
    llm: Any,
    enable_selective_rerun: bool,
    gate_feedback_enabled: bool = True,
    direction_debate_enabled: bool = False,
    incumbent_direction: Optional[DirectionDecision] = None,
    budget_policy: Optional[BudgetPolicy] = None,
    run_snapshot: Optional[RunSnapshot] = None,
) -> Tuple[Any, Optional[AnalysisReport], Optional[GateDecision]]:
    rerun_attempt = 0
    direction_feedback_attempts = 0
    active_roles: Optional[Set[str]] = None
    rerun_note: Optional[str] = None
    max_reruns = _resolve_selective_rerun_max_attempts_default() if enable_selective_rerun else 0
    feedback_loop_enabled = bool(gate_feedback_enabled and enable_selective_rerun)
    seen_rerun_signatures: Set[Tuple[Tuple[str, ...], int, str, str, int]] = set()
    current_direction = incumbent_direction

    while True:
        crew = build_analysis_crew(
            user_problem,
            mode=mode,
            language_hint=language_hint,
            llm=llm,
            active_roles=active_roles,
            rerun_note=rerun_note,
            direction_feedback_enabled=feedback_loop_enabled,
        )
        if run_snapshot is not None:
            prompt_hashes = getattr(crew, "_prompt_hashes", {}) or {}
            for task_name, task_hash in prompt_hashes.items():
                key = f"analysis_r{rerun_attempt}.{task_name}"
                run_snapshot.prompt_hashes[key] = task_hash
            prompt_metrics = getattr(crew, "_prompt_metrics", {}) or {}
            if prompt_metrics:
                run_snapshot.agent_graph.setdefault("analysis_prompt_metrics", {})[
                    f"rerun_{rerun_attempt}"
                ] = prompt_metrics
            dag_snapshot = getattr(crew, "_dag_snapshot", None)
            if isinstance(dag_snapshot, dict):
                run_snapshot.agent_graph["analysis"] = dag_snapshot
            _snapshot_record_stage(
                run_snapshot,
                stage="analysis_crew.kickoff",
                status="started",
                extra={
                    "rerun_attempt": rerun_attempt,
                    "prompt_chars": int(getattr(crew, "_prompt_total_chars", 0) or 0),
                },
            )
        _cost_trace(
            "analysis_crew.kickoff",
            rerun_attempt=rerun_attempt,
            active_roles="all"
            if active_roles is None
            else ",".join(sorted(active_roles)),
            prompt_chars=int(getattr(crew, "_prompt_total_chars", 0) or 0),
        )
        try:
            log_event(
                LOGGER,
                20,
                "analysis_kickoff_start",
                "Starting analysis crew kickoff.",
                rerun_attempt=rerun_attempt,
                active_roles="all"
                if active_roles is None
                else ",".join(sorted(active_roles)),
                prompt_chars=int(getattr(crew, "_prompt_total_chars", 0) or 0),
            )
            result = kickoff_crew_with_retry(
                crew,
                logger=LOGGER,
                log_fields={
                    "stage": "analysis",
                    "rerun_attempt": rerun_attempt,
                    "active_roles": "all"
                    if active_roles is None
                    else ",".join(sorted(active_roles)),
                    "prompt_chars": int(getattr(crew, "_prompt_total_chars", 0) or 0),
                },
            )
        except _OperationCancelledError:
            # Cooperative cancellation must propagate immediately — do not log
            # as an analysis kickoff failure or record a failed cost/snapshot entry.
            raise
        except Exception as e:
            log_exception(
                LOGGER,
                "analysis_kickoff_failed",
                "Analysis crew kickoff failed.",
                rerun_attempt=rerun_attempt,
            )
            try:
                _record_cost(
                    stage="analysis_crew.kickoff",
                    agent_name="analysis_crew",
                    input_tokens=_prompt_tokens_for_crew(crew, user_problem),
                    output_tokens=0,
                    success=False,
                    retry_count=rerun_attempt,
                    outcome="execution_error",
                )
            except Exception:
                pass
            _snapshot_record_stage(
                run_snapshot,
                stage="analysis_crew.kickoff",
                status="failed",
                failure_type=_classify_runtime_exception_failure(e),
                notes=f"Crew kickoff exception: {e}",
                extra={"rerun_attempt": rerun_attempt},
            )
            raise
        log_event(
            LOGGER,
            20,
            "analysis_kickoff_done",
            "Analysis crew kickoff completed.",
            rerun_attempt=rerun_attempt,
        )
        report, gate = _parse_analysis_outputs(
            result, llm=llm, language_hint=language_hint, mode=mode
        )
        gate = _promote_validation_first_gate(
            gate,
            user_problem=user_problem,
            mode=mode,
        )
        report = _align_analysis_report_with_gate_scope(report, gate)
        try:
            result_text = _extract_text_from_result(result) or ""
            analysis_parse_success = report is not None and gate is not None
            analysis_outcome = (
                "success"
                if report is not None and gate is not None
                else "partial_success"
                if report is not None or gate is not None
                else "parse_failed"
            )
            _record_cost(
                stage="analysis_crew.kickoff",
                agent_name="analysis_crew",
                input_tokens=_prompt_tokens_for_crew(crew, user_problem),
                output_tokens=len(result_text) // 3,
                success=analysis_parse_success,
                retry_count=rerun_attempt,
                outcome=analysis_outcome,
            )
        except Exception:
            pass
        _snapshot_record_gate(run_snapshot, gate)

        budget_state = _evaluate_budget_state(budget_policy) if budget_policy else {}
        if budget_state and (
            budget_state.get("over_hard_limit") or budget_state.get("over_token_limit")
        ):
            _apply_gate_failure(
                gate,
                FailureType.COST_OVER_BUDGET,
                "Budget hard limit reached during analysis stage.",
                overwrite=True,
            )
            _snapshot_record_stage(
                run_snapshot,
                stage="analysis_crew.kickoff",
                status="skipped",
                failure_type=FailureType.COST_OVER_BUDGET,
                notes="Stopped due to hard budget limit.",
                extra={"rerun_attempt": rerun_attempt},
            )
            return result, report, gate

        if gate is None:
            _snapshot_record_stage(
                run_snapshot,
                stage="analysis_crew.kickoff",
                status="failed",
                failure_type=FailureType.JSON_INVALID,
                notes="GateDecision missing after parse/reformat.",
                extra={"rerun_attempt": rerun_attempt},
            )
        else:
            failure_type, failure_details = _classify_gate_failure(gate)
            _snapshot_record_stage(
                run_snapshot,
                stage="analysis_crew.kickoff",
                status="completed",
                failure_type=failure_type,
                notes=failure_details or None,
                extra={"rerun_attempt": rerun_attempt},
            )

        direction_feedback_requested = bool(
            feedback_loop_enabled and _gate_requests_direction_feedback(gate)
        )
        feedback_type = (
            _classify_direction_feedback_type(gate)
            if direction_feedback_requested
            else None
        )

        if (
            not enable_selective_rerun
            or gate is None
            or gate.should_kill
            or rerun_attempt >= max_reruns
            or (not gate.agents_needing_rerun and not direction_feedback_requested)
        ):
            return result, report, gate

        if budget_state and budget_state.get("over_soft_limit"):
            _snapshot_record_stage(
                run_snapshot,
                stage="analysis_crew.selective_rerun",
                status="skipped",
                failure_type=FailureType.COST_OVER_BUDGET,
                notes="Soft budget limit reached; selective rerun disabled.",
                extra={"rerun_attempt": rerun_attempt},
            )
            return result, report, gate

        if (
            direction_feedback_requested
            and direction_feedback_attempts >= MAX_DIRECTION_FEEDBACK_BOUNCES
        ):
            _snapshot_record_stage(
                run_snapshot,
                stage="analysis_crew.direction_feedback",
                status="skipped",
                notes=f"Direction feedback max attempts reached ({MAX_DIRECTION_FEEDBACK_BOUNCES}).",
                extra={
                    "rerun_attempt": rerun_attempt,
                    "feedback_path": feedback_type,
                },
            )
            return result, report, gate

        target_roles = _normalize_rerun_agent_keys(gate.agents_needing_rerun)
        direction_feedback_note: Optional[str] = None
        refined_direction: Optional[DirectionDecision] = None
        if direction_feedback_requested and gate is not None:
            direction_feedback_attempts += 1
            direction_feedback_note = _build_direction_feedback_note(
                gate=gate,
                report=report,
                task_details=_collect_analysis_task_details(crew, result),
                rerun_attempt=rerun_attempt + 1,
                feedback_type=feedback_type,
            )
            should_run_direction_debate = bool(
                direction_debate_enabled and feedback_type == "evidence"
            )
            if should_run_direction_debate:
                try:
                    log_event(
                        LOGGER,
                        20,
                        "direction_feedback_start",
                        "Starting direction feedback refinement.",
                        rerun_attempt=rerun_attempt,
                        feedback_type=feedback_type,
                    )
                    refined_direction = run_direction_debate(
                        user_problem,
                        mode=mode,
                        language_hint=language_hint,
                        llm=llm,
                        feedback_note=direction_feedback_note,
                        incumbent_direction=current_direction,
                        force_refresh=True,
                    )
                except _OperationCancelledError:
                    raise
                except Exception as exc:
                    log_exception(
                        LOGGER,
                        "direction_feedback_failed",
                        "Direction debate feedback loop failed.",
                        rerun_attempt=rerun_attempt,
                        feedback_type=feedback_type,
                    )
                    print(f"[Warn] Direction debate feedback loop failed: {exc}")
                if (
                    refined_direction is not None
                    and refined_direction.selected_direction == "none"
                ):
                    gate.ready_for_codegen = False
                    gate.should_kill = True
                    gate.kill_reason = (
                        "Direction debate rejected the direction after gate feedback: "
                        + limit_text(refined_direction.summary, 280)
                    )
                    _apply_gate_failure(
                        gate,
                        FailureType.POLICY_VIOLATION,
                        gate.kill_reason,
                        overwrite=True,
                    )
                    _snapshot_record_stage(
                        run_snapshot,
                        stage="analysis_crew.direction_feedback",
                        status="killed",
                        failure_type=FailureType.POLICY_VIOLATION,
                        notes=gate.kill_reason,
                        extra={"rerun_attempt": rerun_attempt, "feedback_path": feedback_type},
                    )
                    return result, report, gate
                if (
                    refined_direction is not None
                    and str(getattr(refined_direction, "selected_direction", "") or "").strip().lower()
                    not in ("", "none")
                ):
                    current_direction = refined_direction
            _snapshot_record_stage(
                run_snapshot,
                stage="analysis_crew.direction_feedback",
                status="completed",
                notes=f"Direction feedback loop executed via {feedback_type or 'detail'} path.",
                extra={
                    "rerun_attempt": rerun_attempt,
                    "feedback_path": feedback_type,
                    "direction_debate_invoked": should_run_direction_debate,
                    "refined_direction": getattr(
                        refined_direction, "selected_direction", None
                    ),
                },
            )
        if not target_roles:
            if gate.agents_needing_rerun:
                print(
                    f"[Warn] GateController requested unknown rerun roles: {gate.agents_needing_rerun}"
                )
            if not direction_feedback_requested:
                return result, report, gate

        signature = (
            tuple(sorted(target_roles)),
            int(gate.overall_score or 0),
            str(gate.confidence or ""),
            str(feedback_type or ""),
            int(direction_feedback_attempts if direction_feedback_requested else 0),
        )
        if signature in seen_rerun_signatures:
            print(
                "[Warn] Repeated selective rerun signature detected; stopping rerun loop."
            )
            _apply_gate_failure(
                gate,
                FailureType.NON_DETERMINISTIC,
                "Repeated rerun signature detected.",
                overwrite=False,
            )
            _snapshot_record_stage(
                run_snapshot,
                stage="analysis_crew.selective_rerun",
                status="skipped",
                failure_type=FailureType.NON_DETERMINISTIC,
                notes="Repeated rerun signature detected.",
                extra={"rerun_attempt": rerun_attempt},
            )
            return result, report, gate
        seen_rerun_signatures.add(signature)

        rerun_attempt += 1
        active_roles = set(target_roles)
        reasons = gate.rerun_reasons or {}
        reason_lines = (
            [
                f"- {name}: {reasons.get(name, 'no reason provided')}"
                for name in target_roles
            ]
            if target_roles
            else ["- (none; refresh Gate Controller using the updated feedback context only)"]
        )
        baseline_lines: List[str] = []
        if report is not None:
            baseline_lines.append(
                f"Previous summary: {limit_text(report.summary, 400)}"
            )
            baseline_lines.append(
                f"Previous consensus: {limit_text(report.consensus, 400)}"
            )
            baseline_lines.append(
                f"Previous disagreement: {limit_text(report.disagreement, 300)}"
            )
        if gate is not None and gate.blocking_risks:
            baseline_lines.append(
                "Previous blocking risks: " + "; ".join(gate.blocking_risks[:5])
            )
        if direction_feedback_note:
            baseline_lines.append(direction_feedback_note)
        if refined_direction is not None:
            _dir_label = "方向" if "zh" in str(language_hint or "").lower() else "Direction"
            baseline_lines.append(
                "Refined direction decision: "
                + limit_text(
                    f"{_dir_label} {refined_direction.selected_direction} | "
                    f"confidence={refined_direction.confidence} | "
                    f"summary={refined_direction.summary}",
                    420,
                )
            )

        if refined_direction is not None:
            baseline_lines.extend(_build_refined_direction_context(refined_direction)[1:])

        rerun_note = (
            f"Rerun attempt: {rerun_attempt}/{max_reruns}\n"
            + (
                (
                    "Evidence feedback triggered. Re-open direction debate if enabled, rerun only the requested analyst roles, and refresh GateDecision.\n"
                    if feedback_type == "evidence"
                    else "Detail feedback triggered. Rerun only the requested analyst roles and refresh GateDecision.\n"
                )
                if direction_feedback_note
                else "Only rerun the listed specialist roles and refresh GateDecision.\n"
            )
            + "Keep prior accepted conclusions unless new evidence clearly contradicts them.\n"
            + "Requested roles:\n"
            + "\n".join(reason_lines)
        )
        if baseline_lines:
            rerun_note += "\n\nBaseline context to preserve:\n" + "\n".join(
                baseline_lines
            )
        print(
            f"[Gate Controller] Selective re-run roles: {target_roles} (attempt {rerun_attempt}/{max_reruns})"
        )
        log_event(
            LOGGER,
            20,
            "analysis_selective_rerun",
            "Gate Controller requested selective rerun.",
            rerun_attempt=rerun_attempt,
            target_roles=",".join(target_roles),
            feedback_type=feedback_type,
        )
        try:
            _record_cost(
                stage="analysis_crew.selective_rerun",
                agent_name="gate_controller",
                input_tokens=0,
                output_tokens=0,
                success=True,
                retry_count=rerun_attempt,
                outcome="rerun_requested",
            )
        except Exception:
            pass


def run_codegen_stage(
    user_problem: str,
    *,
    mode: str,
    language_hint: str,
    llm: Any,
    analysis_report: Optional[AnalysisReport],
    gate_decision: Optional[GateDecision],
    run_snapshot: Optional[RunSnapshot] = None,
    scope: str = "mvp",
) -> Tuple[Any, Optional[CodeBundle]]:
    bundle_failure_reason = "parse_failed"
    bundle_failure_note = "CodeBundle parse failed."
    crew = build_codegen_crew(
        user_problem,
        mode=mode,
        language_hint=language_hint,
        llm=llm,
        analysis_report=analysis_report,
        gate_decision=gate_decision,
        scope=scope,
    )
    if run_snapshot is not None:
        prompt_hashes = getattr(crew, "_prompt_hashes", {}) or {}
        for task_name, task_hash in prompt_hashes.items():
            run_snapshot.prompt_hashes[f"codegen.{task_name}"] = task_hash
        dag_snapshot = getattr(crew, "_dag_snapshot", None)
        if isinstance(dag_snapshot, dict):
            run_snapshot.agent_graph["codegen"] = dag_snapshot
        _snapshot_record_stage(
            run_snapshot,
            stage="codegen_crew.kickoff",
            status="started",
            extra={"prompt_chars": _prompt_chars_for_crew(crew, user_problem)},
        )
    _cost_trace(
        "codegen_crew.kickoff",
        mode=mode,
        prompt_chars=_prompt_chars_for_crew(crew, user_problem),
    )
    try:
        log_event(
            LOGGER,
            20,
            "codegen_kickoff_start",
            "Starting codegen crew kickoff.",
            mode=mode,
        )
        result = _kickoff_codegen_with_timeout_recovery(
            crew,
            fallback_crew_factory=lambda: _build_codegen_timeout_recovery_crew(
                user_problem,
                mode=mode,
                language_hint=language_hint,
                llm=llm,
                analysis_report=analysis_report,
                gate_decision=gate_decision,
                scope=scope,
            ),
            mode=mode,
        )
    except _OperationCancelledError:
        raise
    except Exception as e:
        log_exception(
            LOGGER,
            "codegen_kickoff_failed",
            "CodeGen stage failed.",
            mode=mode,
        )
        print(f"[Error] CodeGen stage failed: {e}")
        try:
            _record_cost(
                stage="codegen_crew.kickoff",
                agent_name="codegen",
                input_tokens=_prompt_tokens_for_crew(crew, user_problem),
                output_tokens=0,
                success=False,
                outcome="execution_error",
            )
        except Exception:
            pass
        _snapshot_record_stage(
            run_snapshot,
            stage="codegen_crew.kickoff",
            status="failed",
            failure_type=_classify_runtime_exception_failure(e),
            notes=str(e),
        )
        return None, None
    log_event(
        LOGGER,
        20,
        "codegen_kickoff_done",
        "Codegen crew kickoff completed.",
        mode=mode,
    )
    bundle = extract_code_bundle(result)
    if bundle is None:
        # Phase 1: try cheap parse on every text candidate first.  The legacy
        # interleaved loop spent an LLM call to reformat raw_i the moment its
        # parse failed, even when raw_{i+1}'s parse would have succeeded for
        # free (CrewAI exposes the same output via several attrs).
        text_candidates = _collect_text_candidates_from_result(result)
        for raw in reversed(text_candidates):
            bundle = extract_code_bundle(raw)
            if bundle is not None:
                break
        # Phase 2: only when every cheap parse fails do we fall back to the
        # LLM-driven schema reformatter.  STRICT_JSON_ENABLED gates this so
        # non-strict runs don't pay the extra LLM round-trip.
        if bundle is None and STRICT_JSON_ENABLED:
            for raw in reversed(text_candidates):
                bundle = _reformat_code_bundle(
                    raw, llm=llm, language_hint=language_hint, mode=mode
                )
                if bundle is not None:
                    break
    bundle = _sanitize_code_bundle(bundle)
    if bundle is not None and not _bundle_has_files(bundle):
        bundle = None
    bundle_failure_reason = "parse_failed"
    bundle_failure_note: Optional[str] = None
    mismatch_reason = _code_bundle_mode_mismatch_reason(bundle, mode)
    if mismatch_reason:
        print(f"[Warn] {mismatch_reason}")
        bundle = None
        bundle_failure_reason = "mode_mismatch"
        bundle_failure_note = mismatch_reason
    try:
        result_text = _extract_text_from_result(result) or ""
        _record_cost(
            stage="codegen_crew.kickoff",
            agent_name="codegen",
            input_tokens=_prompt_tokens_for_crew(crew, user_problem),
            output_tokens=len(result_text) // 3,
            success=bundle is not None,
            outcome="success" if bundle is not None else bundle_failure_reason,
        )
    except Exception:
        pass
    if bundle is None:
        _snapshot_record_stage(
            run_snapshot,
            stage="codegen_crew.kickoff",
            status="failed",
            failure_type=FailureType.JSON_INVALID,
            notes=bundle_failure_note,
        )
    else:
        _snapshot_record_stage(
            run_snapshot,
            stage="codegen_crew.kickoff",
            status="completed",
            failure_type=FailureType.NONE,
            notes=None,
        )
    return result, bundle


# =========================
# 5) Quality Review Loop
# =========================



def _resolve_quality_max_rounds_default() -> int:
    # Use explicit None-sentinel so that QUALITY_MAX_ROUNDS=0 ("disable quality loop")
    # is honoured.  The previous `or 80` falsy-zero short-circuit silently overrode
    # a legitimate 0 to 80, making it impossible to disable the quality loop via env var.
    result = _env_int("QUALITY_MAX_ROUNDS", 80)
    return result if result is not None else 80


def _resolve_quality_runtime_defaults() -> Dict[str, Any]:
    # Compute values that need None-safe handling before building the dict so
    # each env var is read exactly once.
    _raw_fuse = _env_int("QUALITY_FIX_FUSE_CONSECUTIVE_FAILURES", 2)
    # _env_int returns None when the env var is set to "none"/"unlimited"; fall
    # back to the default (2) rather than treating None as falsy-zero (which
    # would set the fuse to 0 and cause it to trigger immediately).
    _fix_fuse = max(0, _raw_fuse if _raw_fuse is not None else 2)
    return {
        "max_rounds": _resolve_quality_max_rounds_default(),
        "context_max_chars": (_env_int("QUALITY_CONTEXT_MAX_CHARS", 10000) or 10000),
        "code_bundle_max_chars": (_env_int("QUALITY_CODE_BUNDLE_MAX_CHARS", 36000) or 36000),
        "runtime_log_max_chars": (_env_int("QUALITY_RUNTIME_LOG_MAX_CHARS", 8000) or 8000),
        "json_retry_attempts": max(1, (_env_int("QUALITY_JSON_RETRY_ATTEMPTS", 2) or 2)),
        "code_file_max_chars": _env_int("QUALITY_CODE_FILE_MAX_CHARS", 6000),
        "code_file_max_chars_entrypoint": _env_int(
            "QUALITY_CODE_FILE_MAX_CHARS_ENTRYPOINT", 12000
        ),
        "code_file_max_chars_scoped": _env_int(
            "QUALITY_CODE_FILE_MAX_CHARS_SCOPED", 14000
        ),
        "code_file_max_chars_priority": _env_int(
            "QUALITY_CODE_FILE_MAX_CHARS_PRIORITY", 9000
        ),
        "code_snippet_head_chars": _env_int("QUALITY_CODE_SNIPPET_HEAD_CHARS", 4200),
        "code_snippet_tail_chars": _env_int("QUALITY_CODE_SNIPPET_TAIL_CHARS", 1800),
        "context_tree_max_chars": _env_int("QUALITY_CONTEXT_TREE_MAX_CHARS", 3500),
        "runtime_log_tail_chars": _env_int("QUALITY_RUNTIME_LOG_TAIL_CHARS", 2000),
        "max_files_with_content_round0": _env_int("QUALITY_MAX_FILES_WITH_CONTENT_ROUND0", 8),
        "max_files_with_content_roundn": _env_int("QUALITY_MAX_FILES_WITH_CONTENT_ROUNDN", 5),
        "review_report_max_chars": _env_int("QUALITY_REVIEW_REPORT_MAX_CHARS", 4500),
        "review_issue_max_chars": _env_int("QUALITY_REVIEW_ISSUE_MAX_CHARS", 600),
        "max_issues_in_prompt": _env_int("QUALITY_MAX_ISSUES_IN_PROMPT", 8),
        "early_stop_stagnation_rounds": _env_int("QUALITY_EARLY_STOP_STAGNATION_ROUNDS", 3),
        "fix_fuse_consecutive_failures": _fix_fuse,
    }


_QUALITY_RUNTIME_DEFAULTS = _resolve_quality_runtime_defaults()
QUALITY_MAX_ROUNDS = int(_QUALITY_RUNTIME_DEFAULTS["max_rounds"])
QUALITY_CONTEXT_MAX_CHARS = int(_QUALITY_RUNTIME_DEFAULTS["context_max_chars"])
QUALITY_CODE_BUNDLE_MAX_CHARS = _QUALITY_RUNTIME_DEFAULTS["code_bundle_max_chars"]
QUALITY_RUNTIME_LOG_MAX_CHARS = _QUALITY_RUNTIME_DEFAULTS["runtime_log_max_chars"]
QUALITY_JSON_RETRY_ATTEMPTS = int(_QUALITY_RUNTIME_DEFAULTS["json_retry_attempts"])
QUALITY_CODE_FILE_MAX_CHARS = _QUALITY_RUNTIME_DEFAULTS["code_file_max_chars"]
QUALITY_CODE_FILE_MAX_CHARS_ENTRYPOINT = _QUALITY_RUNTIME_DEFAULTS["code_file_max_chars_entrypoint"]
QUALITY_CODE_FILE_MAX_CHARS_SCOPED = _QUALITY_RUNTIME_DEFAULTS["code_file_max_chars_scoped"]
QUALITY_CODE_FILE_MAX_CHARS_PRIORITY = _QUALITY_RUNTIME_DEFAULTS["code_file_max_chars_priority"]
QUALITY_CODE_SNIPPET_HEAD_CHARS = _QUALITY_RUNTIME_DEFAULTS["code_snippet_head_chars"]
QUALITY_CODE_SNIPPET_TAIL_CHARS = _QUALITY_RUNTIME_DEFAULTS["code_snippet_tail_chars"]
QUALITY_CONTEXT_TREE_MAX_CHARS = _QUALITY_RUNTIME_DEFAULTS["context_tree_max_chars"]
QUALITY_RUNTIME_LOG_TAIL_CHARS = _QUALITY_RUNTIME_DEFAULTS["runtime_log_tail_chars"]
QUALITY_MAX_FILES_WITH_CONTENT_ROUND0 = _QUALITY_RUNTIME_DEFAULTS["max_files_with_content_round0"]
QUALITY_MAX_FILES_WITH_CONTENT_ROUNDN = _QUALITY_RUNTIME_DEFAULTS["max_files_with_content_roundn"]
QUALITY_REVIEW_REPORT_MAX_CHARS = _QUALITY_RUNTIME_DEFAULTS["review_report_max_chars"]
QUALITY_REVIEW_ISSUE_MAX_CHARS = _QUALITY_RUNTIME_DEFAULTS["review_issue_max_chars"]
QUALITY_MAX_ISSUES_IN_PROMPT = _QUALITY_RUNTIME_DEFAULTS["max_issues_in_prompt"]
QUALITY_EARLY_STOP_STAGNATION_ROUNDS = _QUALITY_RUNTIME_DEFAULTS[
    "early_stop_stagnation_rounds"
]
QUALITY_FIX_FUSE_CONSECUTIVE_FAILURES = int(
    _QUALITY_RUNTIME_DEFAULTS["fix_fuse_consecutive_failures"]
)


# =========================
# 5.5) API Version Checker Configuration (v14)
# =========================



def _resolve_api_version_check_enabled_default() -> bool:
    return _env_bool("API_VERSION_CHECK_ENABLED", True)


def _resolve_api_version_check_runtime_defaults() -> Dict[str, Any]:
    severity_threshold = (
        (os.environ.get("API_VERSION_CHECK_SEVERITY_THRESHOLD") or "medium")
        .strip()
        .lower()
    )
    if severity_threshold not in ("low", "medium", "high"):
        severity_threshold = "medium"
    # _env_int returns None when the env var is "none"/"unlimited"; guard each
    # max() call to avoid TypeError: '>' not supported between NoneType and int.
    _raw_max_lib = _env_int("API_VERSION_CHECK_MAX_LIBRARIES", 5)
    _raw_timeout = _env_int("API_VERSION_CHECK_TIMEOUT_SECONDS", 60)
    _raw_ttl = _env_int("API_VERSION_CHECK_CACHE_TTL_HOURS", 24)
    return {
        "enabled": _resolve_api_version_check_enabled_default(),
        "max_libraries": max(1, _raw_max_lib if _raw_max_lib is not None else 5),
        "timeout_seconds": max(30, _raw_timeout if _raw_timeout is not None else 60),
        "cache_ttl_hours": max(1, _raw_ttl if _raw_ttl is not None else 24),
        "severity_threshold": severity_threshold,
    }


API_VERSION_CHECK_ENABLED = _resolve_api_version_check_enabled_default()
_API_VERSION_CHECK_RUNTIME_DEFAULTS = _resolve_api_version_check_runtime_defaults()
API_VERSION_CHECK_MAX_LIBRARIES = int(_API_VERSION_CHECK_RUNTIME_DEFAULTS["max_libraries"])
API_VERSION_CHECK_TIMEOUT_SECONDS = int(_API_VERSION_CHECK_RUNTIME_DEFAULTS["timeout_seconds"])
API_VERSION_CHECK_CACHE_TTL_HOURS = int(_API_VERSION_CHECK_RUNTIME_DEFAULTS["cache_ttl_hours"])
API_VERSION_CHECK_SEVERITY_THRESHOLD = str(
    _API_VERSION_CHECK_RUNTIME_DEFAULTS["severity_threshold"]
)

# Curated high-risk library list for API version checking
# These libraries have frequent breaking changes and are commonly used in generated code
API_VERSION_HIGH_RISK_LIBRARIES = [
    # Web frameworks
    "fastapi",
    "flask",
    "django",
    "starlette",
    # Data validation & serialization
    "pydantic",
    "marshmallow",
    # Data processing
    "pandas",
    "numpy",
    "polars",
    # ML/AI
    "langchain",
    "openai",
    "anthropic",
    "transformers",
    "torch",
    "tensorflow",
    # Database
    "sqlalchemy",
    "alembic",
    "pymongo",
    # Async
    "httpx",
    "aiohttp",
    "httpcore",
    # Testing
    "pytest",
    "pytest-asyncio",
    # Utils
    "pydantic-settings",
    "python-dotenv",
    # Trading / exchange APIs
    "ccxt",
]

# Map library names to their official documentation domains
API_VERSION_LIBRARY_DOC_DOMAINS = {
    "fastapi": "fastapi.tiangolo.com",
    "flask": "flask.palletsprojects.com",
    "django": "docs.djangoproject.com",
    "starlette": "www.starlette.io",
    "pydantic": "docs.pydantic.dev",
    "marshmallow": "marshmallow.readthedocs.io",
    "pandas": "pandas.pydata.org",
    "numpy": "numpy.org",
    "polars": "pola-rs.github.io",
    "langchain": "python.langchain.com",
    "openai": "platform.openai.com",
    "anthropic": "docs.anthropic.com",
    "transformers": "huggingface.co/docs/transformers",
    "torch": "pytorch.org",
    "tensorflow": "www.tensorflow.org",
    "sqlalchemy": "docs.sqlalchemy.org",
    "alembic": "alembic.sqlalchemy.org",
    "pymongo": "www.mongodb.com/docs/drivers/pymongo",
    "httpx": "www.python-httpx.org",
    "aiohttp": "docs.aiohttp.org",
    "httpcore": "www.encode.io/httpcore",
    "pytest": "docs.pytest.org",
    "pytest-asyncio": "pytest-asyncio.readthedocs.io",
    "pydantic-settings": "docs.pydantic.dev/latest/concepts/pydantic_settings/",
    "python-dotenv": "saurabh-kumar.com/python-dotenv/",
    "ccxt": "github.com/ccxt/ccxt/wiki/Manual",
}

API_VERSION_IMPORT_NAME_ALIASES = {
    "pydantic_settings": "pydantic-settings",
    "dotenv": "python-dotenv",
    "pytest_asyncio": "pytest-asyncio",
}

CCXT_OFFICIAL_MANUAL_URL = "https://github.com/ccxt/ccxt/wiki/Manual"
CCXT_RELEASE_NOTES_URL = "https://github.com/ccxt/ccxt/releases"
CCXT_OFFICIAL_EXCHANGE_DOCS: Dict[str, List[str]] = {
    "binance": ["developers.binance.com", "binance-docs.github.io"],
    "bybit": ["bybit-exchange.github.io"],
    "okx": ["www.okx.com"],
    "kraken": ["docs.kraken.com"],
    "kucoin": ["www.kucoin.com"],
    "bitget": ["www.bitget.com"],
    "coinbase": ["docs.cdp.coinbase.com"],
}
CCXT_CAPABILITY_KEYS = {
    "fetch_ohlcv": "fetchOHLCV",
    "create_order": "createOrder",
    "fetch_open_orders": "fetchOpenOrders",
    "fetch_positions": "fetchPositions",
}
CCXT_SYMBOL_METHODS = {
    "fetch_ohlcv",
    "fetch_ticker",
    "fetch_order_book",
    "fetch_trades",
    "fetch_my_trades",
    "create_order",
    "fetch_open_orders",
    "fetch_closed_orders",
    "fetch_positions",
}
CCXT_RISKY_METHODS = CCXT_SYMBOL_METHODS | {
    "fetch_balance",
    "cancel_order",
    "fetch_order",
}
CCXT_COMMON_TIMEFRAMES = {
    "1m",
    "3m",
    "5m",
    "15m",
    "30m",
    "1h",
    "2h",
    "4h",
    "6h",
    "8h",
    "12h",
    "1d",
    "3d",
    "1w",
    "1M",
}
CCXT_RETRY_ERROR_NAMES = {
    "ccxt.NetworkError",
    "ccxt.ExchangeError",
    "ccxt.RequestTimeout",
    "ccxt.DDoSProtection",
    "ccxt.RateLimitExceeded",
    "NetworkError",
    "ExchangeError",
    "RequestTimeout",
    "DDoSProtection",
    "RateLimitExceeded",
}


def _normalize_bundle_relpath(raw_path: str) -> str:
    normalized = (raw_path or "").strip().strip("`\"'").replace("\\", "/").lstrip("/")
    normalized = re.sub(r"/+", "/", normalized)
    if normalized.startswith("code/"):
        normalized = normalized[len("code/") :]
    normalized = posixpath.normpath(normalized)
    if normalized in ("", "."):
        return ""
    return normalized


def _is_safe_bundle_path_input(raw_path: str) -> bool:
    cleaned = (raw_path or "").strip().strip("`\"'")
    if not cleaned:
        return False
    slash_normalized = cleaned.replace("\\", "/")
    if slash_normalized.startswith("//"):
        return False  # UNC paths — must precede the single-slash check
    if slash_normalized.startswith("/"):
        return False
    if re.match(r"^[A-Za-z]:($|/)", slash_normalized):
        return False
    return _is_safe_bundle_relpath(slash_normalized)


def _is_safe_bundle_relpath(path: str) -> bool:
    normalized = _normalize_bundle_relpath(path)
    if not normalized:
        return False
    if normalized == ".." or normalized.startswith("../"):
        return False
    if "/../" in normalized:
        return False
    if re.match(r"^[A-Za-z]:($|/)", normalized):
        return False
    return True


def _is_nonfile_review_path(raw_path: Optional[str]) -> bool:
    normalized = _normalize_bundle_relpath(raw_path or "").lower()
    if not normalized:
        return True
    return normalized in {
        "n/a",
        "na",
        "none",
        "(missing file)",
        "n/a (missing file)",
    }


def _collect_affected_files(review_report: ReviewReport) -> Set[str]:
    affected: Set[str] = set()
    for issue in review_report.issues or []:
        if not issue.file:
            continue
        if _is_nonfile_review_path(issue.file):
            continue
        path = _normalize_bundle_relpath(issue.file)
        if path:
            affected.add(path)
    return affected


def _mode_name_from_project_type(project_type: Optional[str]) -> str:
    normalized = (project_type or "").strip().lower()
    if not normalized:
        raise ValueError("Project type is required. Expected one of: quant, saas, agent, scientist")
    if normalized == "agent":
        return "Agent"
    if normalized == "saas":
        return "SaaS"
    if normalized == "quant":
        return "Quant"
    if normalized == "scientist":
        return "Scientist"
    raise ValueError(
        f"Unsupported project_type {project_type!r}. Expected one of: quant, saas, agent, scientist"
    )


def _code_bundle_mode_mismatch_reason(
    bundle: Optional[CodeBundle], mode: Optional[str]
) -> Optional[str]:
    if bundle is None:
        return None
    normalized_mode = str(mode or "").strip()
    if not normalized_mode:
        return None
    expected_project_type = _project_type_for_mode(normalized_mode)
    actual_project_type = str(getattr(bundle, "project_type", "") or "").strip().lower()
    if not actual_project_type or actual_project_type == expected_project_type:
        return None
    return (
        "CodeBundle mode isolation violation: "
        f"requested mode {normalized_mode!r} expects project_type "
        f"{expected_project_type!r}, but the bundle declared {actual_project_type!r}."
    )


def _review_allows_new_files(
    review_report: ReviewReport, code_bundle: CodeBundle
) -> bool:
    existing_paths = {
        _normalize_bundle_relpath(f.path)
        for f in (code_bundle.files or [])
        if getattr(f, "path", None)
    }
    for issue in review_report.issues or []:
        if not issue.file:
            return True
        issue_path = _normalize_bundle_relpath(issue.file)
        if issue_path and issue_path not in existing_paths:
            return True
        details = " ".join(
            part for part in [issue.description or "", issue.suggestion or ""] if part
        ).lower()
        if "entrypoint" in details and ("no " in details or "missing" in details):
            return True
    return False


def _resolve_review_paths_to_existing(
    code_bundle: CodeBundle,
    review_paths: Set[str],
    *,
    max_suffix_segments: int = 4,
) -> Set[str]:
    existing = {_normalize_bundle_relpath(f.path) for f in code_bundle.files if f.path}
    if not existing or not review_paths:
        return set()

    by_basename: Dict[str, List[str]] = {}
    for p in existing:
        base = os.path.basename(p)
        by_basename.setdefault(base, []).append(p)

    resolved: Set[str] = set()
    for raw in review_paths:
        p = _normalize_bundle_relpath(raw)
        if not p:
            continue

        # Direct match.
        if p in existing:
            resolved.add(p)
            continue

        # Strip Windows drive letter if present (e.g. C:/...).
        if re.match(r"^[A-Za-z]:/", p):
            p2 = p[3:]
            if p2 in existing:
                resolved.add(p2)
                continue
            p = p2

        # Best-effort suffix match on path segments (handles absolute paths and prefixed dirs).
        parts = [seg for seg in p.split("/") if seg]
        best_matches: Optional[Set[str]] = None
        for k in range(min(max_suffix_segments, len(parts)), 0, -1):
            suffix = "/".join(parts[-k:])
            matches = {e for e in existing if e == suffix or e.endswith("/" + suffix)}
            if matches:
                best_matches = matches
                break
        if best_matches:
            resolved |= best_matches
            continue

        # Basename match as last resort (may resolve to multiple paths).
        base = os.path.basename(p)
        if base and base in by_basename:
            resolved |= set(by_basename[base])

    return resolved


def _format_code_bundle_paths_only(
    code_bundle: CodeBundle, max_entries: int = 500
) -> str:
    paths = sorted(
        {_normalize_bundle_relpath(f.path) for f in code_bundle.files if f.path}
    )
    if not paths:
        return "(no files)"
    if max_entries is not None and len(paths) > max_entries:
        head = paths[:max_entries]
        return "\n".join(head) + f"\n... ({len(paths) - max_entries} more)"
    return "\n".join(paths)


def _format_code_bundle_tree(code_bundle: CodeBundle, max_entries: int = 350) -> str:
    paths = sorted(
        {_normalize_bundle_relpath(f.path) for f in code_bundle.files if f.path}
    )
    if not paths:
        return "(no files)"

    # Build a compact tree from paths (text only).
    tree: Dict[str, Any] = {}
    for p in paths:
        cur = tree
        parts = [part for part in p.split("/") if part]
        for part in parts[:-1]:
            cur = cur.setdefault(part + "/", {})
        if parts:
            cur.setdefault(parts[-1], None)

    lines: List[str] = []
    emitted = 0

    def _walk(node: Dict[str, Any], indent: str) -> None:
        nonlocal emitted
        for name in sorted(node.keys()):
            if max_entries is not None and emitted >= max_entries:
                return
            child = node[name]
            lines.append(f"{indent}{name}")
            emitted += 1
            if isinstance(child, dict):
                _walk(child, indent + "  ")
                if max_entries is not None and emitted >= max_entries:
                    return

    _walk(tree, "")
    if max_entries is not None and emitted >= max_entries and len(paths) > emitted:
        lines.append(f"... (tree truncated after {max_entries} entries)")
    return "\n".join(lines)


def _detect_entrypoint_filenames(code_bundle: CodeBundle) -> List[str]:
    filenames = sorted(
        {
            os.path.basename(_normalize_bundle_relpath(f.path))
            for f in code_bundle.files
            if f.path
            and os.path.basename(_normalize_bundle_relpath(f.path)) in ENTRYPOINT_FILES
        }
    )
    return filenames


def _detect_entrypoint_paths(code_bundle: CodeBundle) -> Set[str]:
    paths: Set[str] = set()
    for f in code_bundle.files:
        try:
            p = _normalize_bundle_relpath(f.path)
            if not p:
                continue
            if os.path.basename(p) in ENTRYPOINT_FILES:
                paths.add(p)
        except Exception:
            continue
    return paths


def _safe_scope_files(
    code_bundle: CodeBundle, affected_files: Optional[Set[str]]
) -> Optional[Set[str]]:
    """
    Returns a normalized file set to scope context to, or None to indicate "use full CodeBundle".

    Conservative fallback: if affected_files is empty/None, or none of them exist in the bundle,
    return None to avoid over-restricting the fixer/reviewer.
    """
    if not affected_files:
        return None

    normalized = {_normalize_bundle_relpath(p) for p in affected_files if p}
    normalized.discard("")
    if not normalized:
        return None

    resolved = _resolve_review_paths_to_existing(code_bundle, normalized)
    if not resolved:
        return None
    return resolved


def format_code_bundle_for_review(
    code_bundle: CodeBundle,
    max_chars: Optional[int],
    affected_files: Optional[Set[str]] = None,
    priority_files: Optional[Set[str]] = None,
    max_files_with_content: Optional[int] = None,
) -> str:
    per_file_max_chars = QUALITY_CODE_FILE_MAX_CHARS
    head_chars = QUALITY_CODE_SNIPPET_HEAD_CHARS
    tail_chars = QUALITY_CODE_SNIPPET_TAIL_CHARS
    parts: List[str] = []
    allowed = _safe_scope_files(code_bundle, affected_files)
    normalized_priority: Set[str] = set()
    if priority_files:
        normalized_priority = {
            _normalize_bundle_relpath(p) for p in priority_files if p
        }
        normalized_priority.discard("")

    selected: List[GeneratedFile] = []
    seen: Set[str] = set()
    for f in code_bundle.files:
        p = _normalize_bundle_relpath(f.path)
        if allowed is not None and p not in allowed:
            continue
        if p in seen:
            continue
        seen.add(p)
        selected.append(f)

    def _priority_score(f: GeneratedFile) -> Tuple[int, str]:
        p = _normalize_bundle_relpath(f.path)
        base = os.path.basename(p)
        if p in normalized_priority:
            return (0, p)
        if base in ENTRYPOINT_FILES:
            return (1, p)
        return (2, p)

    # Put likely-relevant files first so they survive global truncation.
    try:
        selected.sort(key=_priority_score)
    except Exception:
        pass

    omitted_paths: List[str] = []
    if (
        max_files_with_content is not None
        and max_files_with_content > 0
        and len(selected) > max_files_with_content
    ):
        omitted_paths = [
            _normalize_bundle_relpath(f.path) for f in selected[max_files_with_content:]
        ]
        selected = selected[:max_files_with_content]

    if allowed is not None:
        # If the review references files not present in the bundle, keep a placeholder header.
        missing = sorted([p for p in allowed if p not in seen])
        for p in missing:
            parts.append(f"\n--- {p} (missing in current CodeBundle) ---")
            parts.append("")

    def _snippet_for_file(path: str, content: str) -> str:
        p = _normalize_bundle_relpath(path)
        base = os.path.basename(p)
        limit = per_file_max_chars
        if (
            base in ENTRYPOINT_FILES
            and QUALITY_CODE_FILE_MAX_CHARS_ENTRYPOINT is not None
        ):
            limit = max(limit or 0, QUALITY_CODE_FILE_MAX_CHARS_ENTRYPOINT)
        if (
            p in normalized_priority
            and QUALITY_CODE_FILE_MAX_CHARS_PRIORITY is not None
        ):
            limit = max(limit or 0, QUALITY_CODE_FILE_MAX_CHARS_PRIORITY)
        # If we're scoped to a small set of files, include more of each file.
        if (
            allowed is not None
            and len(selected) <= 8
            and QUALITY_CODE_FILE_MAX_CHARS_SCOPED is not None
        ):
            limit = max(limit or 0, QUALITY_CODE_FILE_MAX_CHARS_SCOPED)

        if limit is None or len(content) <= limit:
            return content

        # Allocate head/tail based on the chosen limit to keep more useful context.
        if head_chars is None or tail_chars is None:
            return content[:limit] + "\n...[truncated]..."

        local_tail = min(tail_chars, max(0, int(limit * 0.2)))
        local_head = min(head_chars, max(0, limit - local_tail))
        # If limit is large, let head grow to cover more of the file.
        if limit >= 20000:
            local_tail = min(max(tail_chars, 2000), max(0, int(limit * 0.25)))
            local_head = max(0, limit - local_tail)

        head = content[:local_head]
        tail = content[-local_tail:] if local_tail > 0 else ""
        omitted = max(0, len(content) - len(head) - len(tail))
        return (
            head.rstrip() + f"\n\n...[{omitted} chars omitted]...\n\n" + tail.lstrip()
        )

    for f in selected:
        parts.append(f"\n--- {_normalize_bundle_relpath(f.path)} ---")
        parts.append(_snippet_for_file(f.path, f.content))

    if omitted_paths:
        parts.append("\n--- (other files omitted; paths only) ---")
        parts.append("\n".join(omitted_paths[:500]))
    return limit_text("\n".join(parts), max_chars)


def _format_code_bundle_tree_limited(
    code_bundle: CodeBundle, max_chars: Optional[int]
) -> str:
    tree = _format_code_bundle_tree(code_bundle)
    return limit_text(tree, max_chars)


_RUNTIME_LOG_KEYWORDS = (
    "traceback",
    "exception",
    "error",
    "failed",
    "fail",
    "syntaxerror",
    "modulenotfounderror",
    "importerror",
    "py_compile",
    "stderr:",
    "exit_code=",
    "smoke_fail",
    "smoke_skip",
    "timeout",
)


def format_runtime_log_for_llm(runtime_log: str, max_chars: Optional[int]) -> str:
    if not runtime_log:
        return ""
    if max_chars is None:
        return runtime_log

    text = runtime_log.strip()
    if len(text) <= max_chars:
        return text

    lines = text.splitlines()
    key_lines: List[str] = []
    for line in lines:
        lower = line.lower()
        if any(k in lower for k in _RUNTIME_LOG_KEYWORDS):
            key_lines.append(line)

    # QUALITY_RUNTIME_LOG_TAIL_CHARS is an int (never None after _resolve_); use
    # a None-safe fallback only — do NOT use `or 2000` which would also override
    # a legitimate explicit 0 (disabling the tail entirely).
    tail_n = int(QUALITY_RUNTIME_LOG_TAIL_CHARS if QUALITY_RUNTIME_LOG_TAIL_CHARS is not None else 2000)
    tail = text[-min(len(text), max(0, tail_n)) :]

    compact = "\n".join(key_lines[-200:]).strip()
    combined = "\n\n".join(
        [
            s
            for s in [
                "[runtime_log] key lines:\n" + compact if compact else "",
                "[runtime_log] tail:\n" + tail if tail else "",
            ]
            if s
        ]
    ).strip()
    return limit_text(combined, max_chars)


_TRACEBACK_FILE_RE = re.compile(r"File \"([^\"]+)\"")
_RUNTIME_LOG_FILE_RE = re.compile(
    r"([A-Za-z]:[^\r\n\"'()<>|?*]*?\.py|/(?:[^()\s]+/)*[^()\s]+\.py)"
)


def _extract_relevant_paths_from_runtime_log(
    runtime_log: Optional[str], code_bundle: CodeBundle
) -> Set[str]:
    if not runtime_log:
        return set()
    text = runtime_log
    found_basenames: Set[str] = set()
    for pattern in (_TRACEBACK_FILE_RE, _RUNTIME_LOG_FILE_RE):
        for m in pattern.finditer(text):
            try:
                candidate = m.group(1)
                base = os.path.basename(candidate.replace("\\", "/"))
                if base:
                    found_basenames.add(base)
            except Exception:
                continue
    if not found_basenames:
        return set()
    existing = {_normalize_bundle_relpath(f.path) for f in code_bundle.files if f.path}
    matches: Set[str] = set()
    for p in existing:
        if os.path.basename(p) in found_basenames:
            matches.add(p)
    return matches


def _merge_code_bundle_patch(
    base: CodeBundle,
    patch: CodeBundle,
    allowed_files: Optional[Set[str]],
    *,
    allow_new_files: bool = False,
) -> CodeBundle:
    base_by_norm: Dict[str, GeneratedFile] = {}
    base_order: List[str] = []
    for f in base.files:
        key = _normalize_bundle_relpath(f.path)
        if key in base_by_norm:
            continue
        base_by_norm[key] = f
        base_order.append(key)

    patch_by_norm: Dict[str, GeneratedFile] = {}
    for f in patch.files:
        key = _normalize_bundle_relpath(f.path)
        if not key:
            continue
        is_existing_file = key in base_by_norm
        if (
            allowed_files is not None
            and key not in allowed_files
            and not (allow_new_files and not is_existing_file)
        ):
            continue
        patch_by_norm[key] = f

    merged_files: List[GeneratedFile] = []
    for key in base_order:
        if key in patch_by_norm:
            base_f = base_by_norm[key]
            patch_f = patch_by_norm[key]
            merged_files.append(
                GeneratedFile(path=base_f.path, content=patch_f.content)
            )
        else:
            merged_files.append(base_by_norm[key])

    new_files: List[str] = []
    for key, f in sorted(patch_by_norm.items(), key=lambda x: x[0]):
        if key in base_by_norm:
            continue
        if not allow_new_files:
            new_files.append(key)
            continue
        merged_files.append(GeneratedFile(path=f.path, content=f.content))

    if new_files:
        print(
            "[Warn] Ignored new files from patch (new files disabled): "
            + ", ".join(new_files[:20])
        )

    return CodeBundle(project_type=base.project_type, files=merged_files)


def _code_bundle_effective_change_count(base: CodeBundle, merged: CodeBundle) -> int:
    base_map: Dict[str, str] = {}
    merged_map: Dict[str, str] = {}
    for f in base.files:
        key = _normalize_bundle_relpath(f.path)
        if key and key not in base_map:
            base_map[key] = f.content
    for f in merged.files:
        key = _normalize_bundle_relpath(f.path)
        if key and key not in merged_map:
            merged_map[key] = f.content
    changed = 0
    all_keys = set(base_map.keys()) | set(merged_map.keys())
    for key in all_keys:
        if base_map.get(key) != merged_map.get(key):
            changed += 1
    return changed


def _is_within_root(path: str, root: str) -> bool:
    root_real = os.path.realpath(root)
    path_real = os.path.realpath(path)
    try:
        root_norm = os.path.normcase(root_real)
        path_norm = os.path.normcase(path_real)
        return os.path.commonpath([path_norm, root_norm]) == root_norm
    except ValueError:
        return False


def _resolve_bundle_output_path(
    base_dir: str, raw_path: str
) -> Tuple[Optional[str], Optional[str]]:
    if not _is_safe_bundle_path_input(raw_path):
        return None, "unsafe"
    normalized = _normalize_bundle_relpath(raw_path)
    if normalized in ("", "."):
        return None, "empty"
    root = os.path.realpath(base_dir)
    full_path = os.path.realpath(os.path.join(root, normalized))
    if not _is_within_root(full_path, root):
        return None, "unsafe"
    return full_path, None


def _write_code_bundle_to_dir(code_bundle: CodeBundle, base_dir: str) -> List[str]:
    written: List[str] = []
    clean_bundle = _sanitize_code_bundle(code_bundle)
    if clean_bundle is None:
        return written
    code_root = os.path.realpath(base_dir)
    for f in clean_bundle.files:
        full_path, _reason = _resolve_bundle_output_path(code_root, f.path)
        if not full_path:
            continue
        os.makedirs(os.path.dirname(full_path), exist_ok=True)
        with open(full_path, "w", encoding="utf-8") as fp:
            fp.write(f.content)
        written.append(full_path)
    return written


def _format_proc_result(label: str, result: subprocess.CompletedProcess[str]) -> str:
    parts = [f"[{label}] exit_code={result.returncode}"]
    if result.stdout:
        parts.append("stdout:\n" + result.stdout.strip())
    if result.stderr:
        parts.append("stderr:\n" + result.stderr.strip())
    return "\n".join(parts)


ENTRYPOINT_OVERRIDE_ENV = "CODEX_ENTRYPOINT"


# BEGIN MANUAL OUTPUT SAVE OVERRIDES
# All max(floor, _env_int(...)) calls guard against None below: _env_int
# returns None when the env var is set to "none"/"unlimited"/"inf", and
# max(int, None) raises TypeError in Python 3.  Pre-compute each raw value
# and substitute the default when it is None before passing to max().
CODEGEN_STAGED_ENABLED = _env_bool("CODEGEN_STAGED_ENABLED", True)
_r = _env_int("CODEGEN_PRIMARY_MAX_INPUT_CHARS", 18000)
CODEGEN_PRIMARY_MAX_INPUT_CHARS = max(6000, _r if _r is not None else 18000)
_r = _env_int("CODEGEN_MANIFEST_MAX_INPUT_CHARS", 16000)
CODEGEN_MANIFEST_MAX_INPUT_CHARS = max(5000, _r if _r is not None else 16000)
_r = _env_int("CODEGEN_MANIFEST_CONTEXT_MAX_CHARS", 10000)
CODEGEN_MANIFEST_CONTEXT_MAX_CHARS = max(4000, _r if _r is not None else 10000)
_r = _env_int("CODEGEN_BATCH_MAX_INPUT_CHARS", 18000)
CODEGEN_BATCH_MAX_INPUT_CHARS = max(5000, _r if _r is not None else 18000)
_r = _env_int("CODEGEN_BATCH_CONTEXT_MAX_CHARS", 9000)
CODEGEN_BATCH_CONTEXT_MAX_CHARS = max(3500, _r if _r is not None else 9000)
_r = _env_int("CODEGEN_BATCH_DEP_FILE_MAX_CHARS", 5000)
CODEGEN_BATCH_DEP_FILE_MAX_CHARS = max(1200, _r if _r is not None else 5000)
_r = _env_int("CODEGEN_BATCH_DEP_FILE_MAX_CHARS_FALLBACK", 2500)
CODEGEN_BATCH_DEP_FILE_MAX_CHARS_FALLBACK = max(800, _r if _r is not None else 2500)
# Max files per codegen batch.  Reasoning models (GLM-5.1, minimax-m2.7, …)
# spend 15 000-25 000 of their output-token budget on chain-of-thought, leaving
# only 7 000-17 000 tokens for actual code.  3 medium Python files at ~5 000
# tokens each = 15 000 tokens — right at the edge for hard-capped-at-32768
# models.  Reducing to 2 ensures every batch fits comfortably even when
# provider-enforced output limits are lower than CODEGEN_MAX_TOKENS.
# Override: CODEGEN_BATCH_SIZE=3 in .env to restore the previous behaviour.
_r = _env_int("CODEGEN_BATCH_SIZE", 2)
CODEGEN_BATCH_SIZE = max(1, _r if _r is not None else 2)
_r = _env_int("CODEGEN_BATCH_MAX_DEP_FILES", 3)
CODEGEN_BATCH_MAX_DEP_FILES = max(1, _r if _r is not None else 3)
_r = _env_int("ALIBABA_CODEGEN_MANIFEST_MAX_INPUT_CHARS", 12000)
ALIBABA_CODEGEN_MANIFEST_MAX_INPUT_CHARS = max(4000, _r if _r is not None else 12000)
_r = _env_int("ALIBABA_CODEGEN_MANIFEST_CONTEXT_MAX_CHARS", 7000)
ALIBABA_CODEGEN_MANIFEST_CONTEXT_MAX_CHARS = max(3000, _r if _r is not None else 7000)
_r = _env_int("ALIBABA_CODEGEN_BATCH_MAX_INPUT_CHARS", 10000)
ALIBABA_CODEGEN_BATCH_MAX_INPUT_CHARS = max(4000, _r if _r is not None else 10000)
_r = _env_int("ALIBABA_CODEGEN_BATCH_CONTEXT_MAX_CHARS", 5500)
ALIBABA_CODEGEN_BATCH_CONTEXT_MAX_CHARS = max(2500, _r if _r is not None else 5500)
_r = _env_int("ALIBABA_CODEGEN_BATCH_DEP_FILE_MAX_CHARS", 2200)
ALIBABA_CODEGEN_BATCH_DEP_FILE_MAX_CHARS = max(800, _r if _r is not None else 2200)
_r = _env_int("ALIBABA_CODEGEN_BATCH_DEP_FILE_MAX_CHARS_FALLBACK", 1200)
ALIBABA_CODEGEN_BATCH_DEP_FILE_MAX_CHARS_FALLBACK = max(600, _r if _r is not None else 1200)
_r = _env_int("ALIBABA_CODEGEN_BATCH_SIZE", 1)
ALIBABA_CODEGEN_BATCH_SIZE = max(1, _r if _r is not None else 1)
_r = _env_int("ALIBABA_CODEGEN_BATCH_MAX_DEP_FILES", 2)
ALIBABA_CODEGEN_BATCH_MAX_DEP_FILES = max(1, _r if _r is not None else 2)
_r = _env_int("ALIBABA_CODEGEN_PREEMPTIVE_FALLBACK_PROMPT_CHARS", 8000)
ALIBABA_CODEGEN_PREEMPTIVE_FALLBACK_PROMPT_CHARS = max(4000, _r if _r is not None else 8000)
_r = _env_int("CODEGEN_MANIFEST_SHARED_CONSTRAINT_LIMIT", 8)
CODEGEN_MANIFEST_SHARED_CONSTRAINT_LIMIT = max(3, _r if _r is not None else 8)
del _r  # clean up the temp variable from the module namespace
CODEGEN_MANIFEST_SECTION_RESERVE_CHARS = {
    "approved_context": 3200,
    "user_problem": 900,
}
CODEGEN_BATCH_SECTION_RESERVE_CHARS = {
    "manifest_prompt": 2200,
    "existing_bundle_context": 1800,
    "approved_context": 1500,
    "user_problem": 700,
}
_r = _env_int("CODEGEN_MANIFEST_REFORMAT_MAX_INPUT_CHARS", 10000)
CODEGEN_MANIFEST_REFORMAT_MAX_INPUT_CHARS = max(3000, _r if _r is not None else 10000)
del _r

# These assignments intentionally capture the FIRST definitions of each function
# (the ones above this line), which implement the legacy single-crew codegen path.
# The second definitions below (new staged pipeline) later override the names at
# module level, but these aliases still point to the first definitions so that
# `run_codegen_stage` (staged) can fall back to the legacy path when
# CODEGEN_STAGED_ENABLED=False.  All three captured functions accept the `scope`
# parameter and are safe to call with keyword argument scope=<value>.
_LEGACY_BUILD_CODEGEN_CREW = build_codegen_crew
_LEGACY_BUILD_CODEGEN_TIMEOUT_RECOVERY_CREW = _build_codegen_timeout_recovery_crew
_LEGACY_RUN_CODEGEN_STAGE = run_codegen_stage


def _dedupe_text_list_codegen(
    items: Optional[List[str]],
    *,
    limit: int,
    max_chars: int,
) -> List[str]:
    seen: Set[str] = set()
    normalized: List[str] = []
    for item in list(items or []):
        value = str(item or "").strip()
        if not value:
            continue
        fingerprint = value.lower()
        if fingerprint in seen:
            continue
        seen.add(fingerprint)
        normalized.append(limit_text(value, max_chars))
        if len(normalized) >= limit:
            break
    return normalized


def _validate_codegen_model(model_cls: Any, payload: Any) -> Optional[Any]:
    if payload is None:
        return None
    try:
        if isinstance(payload, str):
            return _model_validate_json_compat(model_cls, payload)
        if hasattr(model_cls, "model_validate"):
            return model_cls.model_validate(payload)
        if hasattr(model_cls, "parse_obj"):
            return model_cls.parse_obj(payload)
    except Exception:
        return None
    return None


def _render_task_description_with_budget(
    task_spec: TaskSpec, template_vars: Dict[str, str]
) -> str:
    description = _render_prompt_template(task_spec.description_template, template_vars)
    max_chars = getattr(task_spec, "max_input_chars", None)
    try:
        budget = None if max_chars is None else int(max_chars)
    except Exception:
        budget = None
    if budget is None or budget <= 0 or len(description) <= budget:
        return description
    suffix = "\n...[truncated]..."
    if budget <= len(suffix):
        return suffix[:budget]
    return description[: budget - len(suffix)] + suffix


def _build_codegen_single_task_crew(
    *,
    crew_name: str,
    llm: Any,
    agent_spec: AgentSpec,
    task_spec: TaskSpec,
    template_vars: Dict[str, str],
    verbose: bool,
) -> Crew:
    agents = {agent_spec.name: _create_agent_from_spec(agent_spec, llm)}
    task = _build_task_from_spec(
        task_spec,
        agents=agents,
        task_lookup={},
        template_vars=template_vars,
    )
    crew = Crew(
        agents=[agents[agent_spec.name]],
        tasks=[task],
        process=Process.sequential,
        verbose=verbose,
    )
    rendered = getattr(task, "description", None) or _render_task_description_with_budget(
        task_spec, template_vars
    )
    setattr(crew, "_prompt_hashes", {task_spec.name: _text_sha256(str(rendered))})
    setattr(
        crew,
        "_prompt_metrics",
        {
            task_spec.name: {
                "prompt_chars": len(str(rendered)),
                "budget_chars": getattr(task_spec, "max_input_chars", None),
                "context_count": len(getattr(task_spec, "context_task_names", []) or []),
                "truncated": "...[truncated]..." in str(rendered),
            }
        },
    )
    setattr(crew, "_prompt_total_chars", len(str(rendered)))
    setattr(
        crew,
        "_dag_snapshot",
        _build_agent_dag_snapshot({agent_spec.name: agent_spec}, [task_spec]),
    )
    setattr(crew, "_retry_policy", agent_spec.retry_policy)
    setattr(crew, "_crew_name", crew_name)
    setattr(crew, "_quant_llm_provider", _codegen_provider_name(llm))
    return crew


def _sync_codegen_snapshot_metadata(
    run_snapshot: Optional[RunSnapshot],
    crew: Optional[Crew],
    *,
    stage_prefix: str,
) -> None:
    if run_snapshot is None or crew is None:
        return
    prompt_hashes = getattr(crew, "_prompt_hashes", {}) or {}
    for task_name, task_hash in prompt_hashes.items():
        run_snapshot.prompt_hashes[f"{stage_prefix}.{task_name}"] = task_hash
    run_snapshot.inputs[f"{stage_prefix}_prompt_chars"] = _prompt_chars_for_crew(crew)
    dag_snapshot = getattr(crew, "_dag_snapshot", None)
    if isinstance(dag_snapshot, dict):
        run_snapshot.agent_graph[stage_prefix] = dag_snapshot


def _kickoff_codegen_substage_with_recovery(
    primary_crew: Crew,
    *,
    fallback_crew_factory: Callable[[], Crew],
    mode: str,
    stage_name: str,
) -> Tuple[Any, Crew, int]:
    primary_prompt_chars = int(getattr(primary_crew, "_prompt_total_chars", 0) or 0)
    provider = str(getattr(primary_crew, "_quant_llm_provider", "") or "").strip()
    if (
        provider == LLM_PROVIDER_ALIBABA_CODING_PLAN
        and primary_prompt_chars >= ALIBABA_CODEGEN_PREEMPTIVE_FALLBACK_PROMPT_CHARS
    ):
        result, fallback_crew, fallback_prompt_chars = _run_codegen_substage_fallback_attempt(
            fallback_crew_factory=fallback_crew_factory,
            mode=mode,
            stage_name=stage_name,
            reason="provider_prompt_budget_guard",
            error_type="PromptBudgetGuard",
        )
        return result, fallback_crew, primary_prompt_chars + fallback_prompt_chars
    try:
        return (
            kickoff_crew_with_retry(
                primary_crew,
                crew_name=stage_name,
                logger=LOGGER,
                log_fields={
                    "stage": stage_name,
                    "mode": mode,
                    "prompt_chars": primary_prompt_chars,
                },
            ),
            primary_crew,
            primary_prompt_chars,
        )
    except _OperationCancelledError:
        # Cooperative cancellation must abort before the fallback substage is attempted.
        raise
    except Exception as exc:
        if not is_transient_retryable_error(exc):
            raise
        result, fallback_crew, fallback_prompt_chars = _run_codegen_substage_fallback_attempt(
            fallback_crew_factory=fallback_crew_factory,
            mode=mode,
            stage_name=stage_name,
            reason="transient_kickoff_failure",
            error_type=type(exc).__name__,
        )
        return result, fallback_crew, primary_prompt_chars + fallback_prompt_chars


def _run_codegen_substage_fallback_attempt(
    *,
    fallback_crew_factory: Callable[[], Crew],
    mode: str,
    stage_name: str,
    reason: str,
    error_type: str,
) -> Tuple[Any, Crew, int]:
    log_event(
        LOGGER,
        30,
        "codegen_substage_fallback_start",
        "Codegen substage starting reduced-context fallback.",
        mode=mode,
        stage=stage_name,
        reason=reason,
        error_type=error_type,
    )
    fallback_crew = fallback_crew_factory()
    fallback_prompt_chars = int(getattr(fallback_crew, "_prompt_total_chars", 0) or 0)
    return (
        kickoff_crew_with_retry(
            fallback_crew,
            crew_name=f"{stage_name}_fallback",
            logger=LOGGER,
            log_fields={
                "stage": f"{stage_name}_fallback",
                "mode": mode,
                "prompt_chars": fallback_prompt_chars,
                "reason": reason,
            },
        ),
        fallback_crew,
        fallback_prompt_chars,
    )


def _budget_prompt_template_sections(
    *,
    description_template: str,
    static_template_vars: Dict[str, str],
    section_values: Dict[str, str],
    section_priority: List[str],
    section_reserve_chars: Dict[str, int],
    max_input_chars: int,
) -> Dict[str, str]:
    merged = dict(static_template_vars)
    normalized_sections = {
        key: str(section_values.get(key, "") or "") for key in list(section_values.keys())
    }
    try:
        budget = int(max_input_chars or 0)
    except Exception:
        budget = 0
    if budget <= 0:
        merged.update(normalized_sections)
        return merged

    fixed_template_vars = dict(static_template_vars)
    fixed_template_vars.update({key: "" for key in normalized_sections})
    fixed_chars = len(_render_prompt_template(description_template, fixed_template_vars))
    available = max(0, budget - fixed_chars)
    section_lengths = {key: len(value) for key, value in normalized_sections.items()}
    if sum(section_lengths.values()) <= available:
        merged.update(normalized_sections)
        return merged

    ordered_keys: List[str] = []
    for key in section_priority:
        if key in normalized_sections and key not in ordered_keys:
            ordered_keys.append(key)
    for key in normalized_sections:
        if key not in ordered_keys:
            ordered_keys.append(key)

    allocated = {key: 0 for key in ordered_keys}
    remaining = available
    for key in ordered_keys:
        reserve = max(0, int(section_reserve_chars.get(key, 0) or 0))
        take = min(section_lengths.get(key, 0), reserve, remaining)
        allocated[key] = take
        remaining -= take
        if remaining <= 0:
            break

    if remaining > 0:
        for key in ordered_keys:
            extra = max(0, section_lengths.get(key, 0) - allocated.get(key, 0))
            if extra <= 0:
                continue
            take = min(extra, remaining)
            allocated[key] = allocated.get(key, 0) + take
            remaining -= take
            if remaining <= 0:
                break

    for key, value in normalized_sections.items():
        limit = max(0, int(allocated.get(key, 0) or 0))
        merged[key] = limit_text(value, limit) if limit > 0 else ""
    return merged


def _codegen_provider_name(llm: Any) -> str:
    try:
        return _llm_provider_name(llm)
    except Exception:
        return _resolve_llm_provider()


def _codegen_budget_profile(llm: Any) -> Dict[str, int]:
    provider = _codegen_provider_name(llm)
    if provider == LLM_PROVIDER_ALIBABA_CODING_PLAN:
        return {
            "manifest_max_input_chars": ALIBABA_CODEGEN_MANIFEST_MAX_INPUT_CHARS,
            "manifest_context_max_chars": ALIBABA_CODEGEN_MANIFEST_CONTEXT_MAX_CHARS,
            "batch_max_input_chars": ALIBABA_CODEGEN_BATCH_MAX_INPUT_CHARS,
            "batch_context_max_chars": ALIBABA_CODEGEN_BATCH_CONTEXT_MAX_CHARS,
            "batch_dep_file_max_chars": ALIBABA_CODEGEN_BATCH_DEP_FILE_MAX_CHARS,
            "batch_dep_file_max_chars_fallback": ALIBABA_CODEGEN_BATCH_DEP_FILE_MAX_CHARS_FALLBACK,
            "batch_size": ALIBABA_CODEGEN_BATCH_SIZE,
            "batch_max_dep_files": ALIBABA_CODEGEN_BATCH_MAX_DEP_FILES,
        }
    return {
        "manifest_max_input_chars": CODEGEN_MANIFEST_MAX_INPUT_CHARS,
        "manifest_context_max_chars": CODEGEN_MANIFEST_CONTEXT_MAX_CHARS,
        "batch_max_input_chars": CODEGEN_BATCH_MAX_INPUT_CHARS,
        "batch_context_max_chars": CODEGEN_BATCH_CONTEXT_MAX_CHARS,
        "batch_dep_file_max_chars": CODEGEN_BATCH_DEP_FILE_MAX_CHARS,
        "batch_dep_file_max_chars_fallback": CODEGEN_BATCH_DEP_FILE_MAX_CHARS_FALLBACK,
        "batch_size": CODEGEN_BATCH_SIZE,
        "batch_max_dep_files": CODEGEN_BATCH_MAX_DEP_FILES,
    }


def _normalize_codegen_manifest(
    manifest: Optional[CodegenManifest], *, mode: str, llm: Any = None
) -> Optional[CodegenManifest]:
    if manifest is None:
        return None
    expected_project_type = _project_type_for_mode(mode)
    project_type = str(getattr(manifest, "project_type", "") or "").strip().lower()
    if project_type != expected_project_type:
        return None
    batch_size = max(1, int(_codegen_budget_profile(llm)["batch_size"]))

    file_map: Dict[str, CodegenFilePlan] = {}
    for raw_plan in list(getattr(manifest, "files", []) or []):
        raw_path = getattr(raw_plan, "path", "")
        if not _is_safe_bundle_path_input(raw_path):
            continue
        path = _normalize_bundle_relpath(raw_path)
        if not path or not _is_safe_bundle_relpath(path):
            continue
        depends_on: List[str] = []
        seen_dep: Set[str] = set()
        for dep in list(getattr(raw_plan, "depends_on", []) or []):
            if not _is_safe_bundle_path_input(dep):
                continue
            normalized_dep = _normalize_bundle_relpath(dep)
            if not normalized_dep or normalized_dep == path:
                continue
            if normalized_dep in seen_dep:
                continue
            seen_dep.add(normalized_dep)
            depends_on.append(normalized_dep)
        file_map[path] = CodegenFilePlan(
            path=path,
            purpose=limit_text(str(getattr(raw_plan, "purpose", "") or "").strip(), 320),
            depends_on=depends_on,
            must_contain=_dedupe_text_list_codegen(
                list(getattr(raw_plan, "must_contain", []) or []),
                limit=6,
                max_chars=220,
            ),
        )

    if not file_map:
        return None

    known_paths = set(file_map.keys())
    normalized_files: List[CodegenFilePlan] = []
    for path in sorted(known_paths):
        plan = file_map[path]
        normalized_files.append(
            CodegenFilePlan(
                path=path,
                purpose=plan.purpose,
                depends_on=[dep for dep in plan.depends_on if dep in known_paths],
                must_contain=plan.must_contain,
            )
        )

    normalized_entrypoints: List[str] = []
    for raw_entry in list(getattr(manifest, "entrypoints", []) or []):
        if not _is_safe_bundle_path_input(raw_entry):
            continue
        entry = _normalize_bundle_relpath(raw_entry)
        if entry in known_paths and entry not in normalized_entrypoints:
            normalized_entrypoints.append(entry)
    if not normalized_entrypoints:
        preferred = ("main.py", "app.py", "agent.py", "strategy.py", "backtest.py")
        for candidate in preferred:
            if candidate in known_paths:
                normalized_entrypoints.append(candidate)
                break
    if not normalized_entrypoints:
        normalized_entrypoints.append(sorted(known_paths)[0])

    shared_constraints = _dedupe_text_list_codegen(
        list(getattr(manifest, "shared_constraints", []) or []),
        limit=CODEGEN_MANIFEST_SHARED_CONSTRAINT_LIMIT,
        max_chars=240,
    )

    normalized_plan_map: Dict[str, CodegenFilePlan] = {
        plan.path: plan for plan in normalized_files
    }
    normalized_batches: List[CodegenBatchPlan] = []
    assigned: Set[str] = set()
    emitted: Set[str] = set()
    raw_batches = list(getattr(manifest, "generation_batches", []) or [])
    for index, raw_batch in enumerate(raw_batches, start=1):
        batch_files: List[str] = []
        for raw_path in list(getattr(raw_batch, "files", []) or []):
            if not _is_safe_bundle_path_input(raw_path):
                continue
            path = _normalize_bundle_relpath(raw_path)
            if path in known_paths and path not in batch_files and path not in assigned:
                batch_files.append(path)
        if not batch_files:
            continue
        batch_file_set = set(batch_files)
        if len(batch_files) > batch_size:
            # The LLM-planned batch exceeds the per-batch token budget cap.
            # Skip it here so the dependency-safe fallback splitter below can
            # rebuild dependency-respecting batches of the configured size.
            # Surface the skip for diagnosability — silent drops were
            # previously hiding why downstream batches looked auto-generated.
            LOGGER.warning(
                "Codegen manifest batch %d (%s) has %d files exceeding the "
                "batch_size cap of %d; deferring to dependency-safe fallback "
                "splitter.",
                index,
                str(getattr(raw_batch, "name", "") or "unnamed"),
                len(batch_files),
                batch_size,
            )
            continue
        if any(
            dep not in emitted and dep not in batch_file_set
            for path in batch_files
            for dep in list(normalized_plan_map[path].depends_on or [])
        ):
            continue
        assigned.update(batch_files)
        emitted.update(batch_files)
        normalized_batches.append(
            CodegenBatchPlan(
                name=limit_text(str(getattr(raw_batch, "name", "") or f"batch_{index}"), 120),
                objective=limit_text(str(getattr(raw_batch, "objective", "") or ""), 240),
                files=batch_files,
            )
        )

    remaining: Dict[str, Set[str]] = {
        plan.path: {dep for dep in plan.depends_on if dep in known_paths}
        for plan in normalized_files
        if plan.path not in emitted
    }
    fallback_counter = len(normalized_batches)
    while remaining:
        ready = sorted(path for path, deps in remaining.items() if deps <= emitted)
        if not ready:
            ready = [sorted(remaining.keys())[0]]
        while ready:
            chunk = ready[:batch_size]
            ready = ready[batch_size:]
            fallback_counter += 1
            normalized_batches.append(
                CodegenBatchPlan(
                    name=f"batch_{fallback_counter}",
                    objective="Generate the next dependency-safe file group.",
                    files=chunk,
                )
            )
            emitted.update(chunk)
            for key in chunk:
                remaining.pop(key, None)

    return CodegenManifest(
        project_type=project_type,
        architecture_summary=limit_text(
            str(getattr(manifest, "architecture_summary", "") or "").strip(),
            800,
        ),
        entrypoints=normalized_entrypoints,
        shared_constraints=shared_constraints,
        files=normalized_files,
        generation_batches=normalized_batches,
    )


def _reformat_codegen_manifest(
    raw_text: str,
    *,
    llm: Any,
    language_hint: str,
    mode: str,
) -> Optional[CodegenManifest]:
    cache_payload = {
        "model": _reformat_llm_model_id(llm),
        "llm_provider": _reformat_llm_provider_name(llm),
        "strict_json": bool(STRICT_JSON_ENABLED),
        "mode": mode,
        "language_hint": language_hint,
        "raw_text_len": len(raw_text or ""),
        "raw_text_sha256": _text_sha256(raw_text or ""),
    }
    return _run_schema_reformatter(
        cache_namespace="reformat_codegen_manifest",
        cache_payload=cache_payload,
        model_cls=CodegenManifest,
        raw_text=raw_text,
        llm=llm,
        role="Codegen Manifest Formatter",
        goal="Convert malformed output into valid CodegenManifest JSON.",
        description=(
            "Reformat the INPUT into a valid CodegenManifest JSON object.\n"
            "Required fields:\n"
            '- project_type: "saas", "quant", "agent", or "scientist"\n'
            "- architecture_summary: string\n"
            "- entrypoints: list of safe relative paths\n"
            "- shared_constraints: list[string]\n"
            "- files: list of objects with keys path, purpose, depends_on, must_contain\n"
            "- generation_batches: list of objects with keys name, objective, files\n\n"
            "Rules:\n"
            "- Do not invent unsupported files.\n"
            "- Paths must be safe relative paths.\n"
            "- Return JSON only.\n\n"
            f"Language hint: {language_hint}\n"
            f"Mode: {mode}\n\n"
            "INPUT:\n" + limit_text(raw_text, CODEGEN_MANIFEST_REFORMAT_MAX_INPUT_CHARS)
        ),
        expected_output="CodegenManifest JSON only.",
        parse_fn=lambda result: _validate_codegen_model(
            CodegenManifest, _coerce_json_dict(result) or result
        ),
        postprocess_fn=lambda manifest: _normalize_codegen_manifest(manifest, mode=mode, llm=llm),
        validate_fn=lambda manifest: bool(
            manifest
            and list(getattr(manifest, "files", []) or [])
            and list(getattr(manifest, "generation_batches", []) or [])
        ),
        error_label="CodegenManifest reformat task",
    )


def _extract_codegen_manifest(
    result: Any,
    *,
    llm: Any,
    language_hint: str,
    mode: str,
) -> Optional[CodegenManifest]:
    # Phase 0: structured candidates (already-parsed Pydantic / dict payloads
    # returned by CrewAI helpers).  Cheap — no parsing or LLM cost.
    for payload in _collect_structured_candidates_from_result(result):
        manifest = _normalize_codegen_manifest(
            _validate_codegen_model(CodegenManifest, payload),
            mode=mode,
            llm=llm,
        )
        if manifest is not None:
            return manifest
    # Phase 1: try cheap JSON parse on every text candidate first.  The
    # legacy interleaved loop spent an LLM round-trip on reformat the moment
    # raw_i's parse failed, even when raw_{i+1} would have parsed for free —
    # CrewAI typically exposes the same output through several attrs and one
    # of them tends to be a clean JSON dump.
    text_candidates = _collect_text_candidates_from_result(result)
    for raw in reversed(text_candidates):
        payload = _extract_first_json_object(raw)
        manifest = _normalize_codegen_manifest(
            _validate_codegen_model(CodegenManifest, payload),
            mode=mode,
            llm=llm,
        )
        if manifest is not None:
            return manifest
    # Phase 2: only when every cheap parse has failed do we fall back to
    # the LLM-driven schema reformatter.
    if STRICT_JSON_ENABLED:
        for raw in reversed(text_candidates):
            manifest = _reformat_codegen_manifest(
                raw, llm=llm, language_hint=language_hint, mode=mode
            )
            manifest = _normalize_codegen_manifest(manifest, mode=mode, llm=llm)
            if manifest is not None:
                return manifest
    return None


def _manifest_file_map(manifest: CodegenManifest) -> Dict[str, CodegenFilePlan]:
    return {plan.path: plan for plan in list(manifest.files or [])}


def _code_bundle_duplicate_paths(bundle: Optional[CodeBundle]) -> List[str]:
    if bundle is None:
        return []
    seen: Set[str] = set()
    duplicates: List[str] = []
    for item in list(getattr(bundle, "files", []) or []):
        raw_path = getattr(item, "path", "")
        if not _is_safe_bundle_path_input(raw_path):
            continue
        path = _normalize_bundle_relpath(raw_path)
        if not path or not _is_safe_bundle_relpath(path):
            continue
        if path in seen and path not in duplicates:
            duplicates.append(path)
            continue
        seen.add(path)
    return duplicates


def _merge_code_bundles(
    base_bundle: Optional[CodeBundle],
    patch_bundle: CodeBundle,
    *,
    project_type: str,
) -> CodeBundle:
    merged: Dict[str, GeneratedFile] = {}
    order: List[str] = []
    for bundle in [base_bundle, patch_bundle]:
        clean = _sanitize_code_bundle(bundle)
        if clean is None:
            continue
        for item in clean.files:
            if item.path not in merged:
                order.append(item.path)
            merged[item.path] = GeneratedFile(path=item.path, content=item.content)
    return CodeBundle(project_type=project_type, files=[merged[path] for path in order])


def _format_codegen_manifest_for_prompt(
    manifest: CodegenManifest,
    *,
    batch_paths: Optional[List[str]] = None,
) -> str:
    file_map = _manifest_file_map(manifest)
    selected_paths = list(batch_paths or [plan.path for plan in manifest.files])
    parts: List[str] = [
        f"project_type: {manifest.project_type}",
        "entrypoints: " + ", ".join(list(manifest.entrypoints or [])),
    ]
    if manifest.architecture_summary:
        parts.append("architecture_summary: " + limit_text(manifest.architecture_summary, 700))
    if manifest.shared_constraints:
        parts.append("shared_constraints:")
        parts.extend(f"- {item}" for item in manifest.shared_constraints)
    parts.append("planned_files:")
    for path in selected_paths:
        plan = file_map.get(path)
        if plan is None:
            continue
        parts.append(f"- path: {plan.path}")
        if plan.purpose:
            parts.append(f"  purpose: {limit_text(plan.purpose, 260)}")
        if plan.depends_on:
            parts.append("  depends_on: " + ", ".join(plan.depends_on))
        if plan.must_contain:
            parts.append("  must_contain:")
            parts.extend(f"    - {item}" for item in plan.must_contain)
    return "\n".join(parts)


def _format_existing_bundle_context_for_batch(
    manifest: CodegenManifest,
    current_bundle: Optional[CodeBundle],
    *,
    batch_paths: List[str],
    dependency_file_max_chars: int,
    max_dependency_files: int,
) -> str:
    if current_bundle is None or not list(getattr(current_bundle, "files", []) or []):
        return "(none yet)"
    file_map = _manifest_file_map(manifest)
    batch_dep_paths: List[str] = []
    for path in batch_paths:
        plan = file_map.get(path)
        if plan is None:
            continue
        for dep in plan.depends_on:
            if dep not in batch_dep_paths:
                batch_dep_paths.append(dep)
    parts: List[str] = []
    emitted_dep_paths = 0
    for file in list(current_bundle.files or []):
        if file.path not in batch_dep_paths:
            continue
        parts.append(f"[dependency_file] {file.path}")
        parts.append(limit_text(file.content, dependency_file_max_chars))
        emitted_dep_paths += 1
        if emitted_dep_paths >= max(1, int(max_dependency_files or 1)):
            break
    remaining_paths = [
        file.path for file in list(current_bundle.files or []) if file.path not in batch_dep_paths
    ]
    if remaining_paths:
        parts.append("[other_completed_files]")
        parts.extend(f"- {path}" for path in remaining_paths[:12])
    return "\n".join(parts) if parts else "(none yet)"


def _build_codegen_manifest_crew(
    user_problem: str,
    *,
    mode: str,
    language_hint: str,
    llm: Any,
    analysis_report: Optional[AnalysisReport],
    gate_decision: Optional[GateDecision],
    context_max_chars: int,
    max_input_chars: int,
    scope: str = "mvp",
) -> Crew:
    mode_config = _get_mode_config(mode)
    project_type = _project_type_for_mode(mode)
    budget_profile = _codegen_budget_profile(llm)
    effective_context_max_chars = min(
        max(1, int(context_max_chars or 1)),
        int(budget_profile["manifest_context_max_chars"]),
    )
    effective_max_input_chars = min(
        max(1, int(max_input_chars or 1)),
        int(budget_profile["manifest_max_input_chars"]),
    )
    approved_context = build_budgeted_codegen_context(
        gate_decision,
        analysis_report,
        max_chars=effective_context_max_chars,
        include_analyst_findings=True,
    )
    mode_rule_text = "\n".join(_resolved_codegen_rule_lines(mode_config, gate_decision, scope=scope))
    # Validation-scope gate overrides user scope for manifest planning to keep goal/rules consistent.
    scope_norm = "mvp" if _gate_is_validation_scope(gate_decision) else str(scope or "mvp").strip().lower()
    if scope_norm in ("full", "production"):
        manifest_planner_goal = (
            f"Plan a dependency-safe file manifest for a {scope_norm}-scope implementation "
            "that covers every module required by the rules."
        )
        manifest_file_rule = (
            f"- Include ALL files required by the {scope_norm}-scope rules; "
            "do not reduce the file set to a minimal subset."
        )
    else:
        manifest_planner_goal = "Plan a dependency-safe file manifest before code generation starts."
        manifest_file_rule = "- Minimize file count while still producing a runnable implementation."
    agent_spec = AgentSpec(
        name="codegen_manifest",
        role="Codegen Planner",
        goal=manifest_planner_goal,
        backstory=(
            "You are planning staged implementation work for a production-minded coding agent.\n"
            f"- Define the {'complete' if scope_norm != 'mvp' else 'minimum'} file set needed to satisfy the approved scope.\n"
            "- Group files into dependency-safe batches.\n"
            "- Keep the manifest concrete enough that later codegen can follow it without improvising."
        ),
        output_schema_name="CodegenManifest",
        parallel_safe=False,
        retry_policy=RetryPolicy(max_attempts=8, backoff_seconds=2.0, retry_on_json_fail=True),
        version="v1.0.0",
        behavior_contract="Return a concrete staged file manifest for the approved codegen scope.",
    )
    task_spec = TaskSpec(
        name="codegen_manifest",
        description_template=(
            f"Plan staged {'full-scope' if scope_norm != 'mvp' else 'runnable MVP'} code generation.\n"
            "Mode: {mode_name}\n"
            "project_type must be '{project_type}'.\n"
            "Language hint: {language_hint}\n\n"
            "Return exactly one CodegenManifest JSON object with these fields:\n"
            "- project_type\n"
            "- architecture_summary\n"
            "- entrypoints\n"
            "- shared_constraints\n"
            "- files: list of objects with keys path, purpose, depends_on, must_contain\n"
            "- generation_batches: list of objects with keys name, objective, files\n\n"
            "Rules:\n"
            f"{manifest_file_rule}\n"
            "- Every file path must be safe and relative.\n"
            "- Batches must respect dependencies and keep earlier shared files ahead of downstream files.\n"
            "- Include only files justified by the approved context.\n"
            "- Prefer explicit filenames over vague placeholders.\n"
            "- Do not return prose outside JSON.\n"
            "{mode_rule_text}\n\n"
            "User problem:\n{user_problem}\n\n"
            "Approved implementation context:\n{approved_context}\n"
        ),
        agent_name="codegen_manifest",
        expected_output="CodegenManifest JSON only.",
        max_input_chars=effective_max_input_chars,
    )
    static_template_vars = {
        "mode_name": mode_config.name,
        "project_type": project_type,
        "language_hint": language_hint,
        "mode_rule_text": mode_rule_text,
    }
    template_vars = _budget_prompt_template_sections(
        description_template=task_spec.description_template,
        static_template_vars=static_template_vars,
        section_values={
            "user_problem": limit_text(user_problem, 4000),
            "approved_context": approved_context,
        },
        section_priority=["approved_context", "user_problem"],
        section_reserve_chars=CODEGEN_MANIFEST_SECTION_RESERVE_CHARS,
        max_input_chars=effective_max_input_chars,
    )
    return _build_codegen_single_task_crew(
        crew_name="codegen_manifest_crew",
        llm=llm,
        agent_spec=agent_spec,
        task_spec=task_spec,
        template_vars=template_vars,
        verbose=False,
    )


def _build_codegen_batch_crew(
    user_problem: str,
    *,
    mode: str,
    language_hint: str,
    llm: Any,
    analysis_report: Optional[AnalysisReport],
    gate_decision: Optional[GateDecision],
    manifest: CodegenManifest,
    batch_plan: CodegenBatchPlan,
    current_bundle: Optional[CodeBundle],
    context_max_chars: int,
    dependency_file_max_chars: int,
    max_input_chars: int,
    scope: str = "mvp",
) -> Crew:
    mode_config = _get_mode_config(mode)
    project_type = _project_type_for_mode(mode)
    budget_profile = _codegen_budget_profile(llm)
    effective_context_max_chars = min(
        max(1, int(context_max_chars or 1)),
        int(budget_profile["batch_context_max_chars"]),
    )
    effective_dependency_file_max_chars = min(
        max(1, int(dependency_file_max_chars or 1)),
        int(budget_profile["batch_dep_file_max_chars"]),
    )
    effective_max_input_chars = min(
        max(1, int(max_input_chars or 1)),
        int(budget_profile["batch_max_input_chars"]),
    )
    approved_context = build_budgeted_codegen_context(
        gate_decision,
        analysis_report,
        max_chars=effective_context_max_chars,
        include_analyst_findings=False,
    )
    manifest_prompt = _format_codegen_manifest_for_prompt(
        manifest, batch_paths=list(batch_plan.files or [])
    )
    existing_bundle_context = _format_existing_bundle_context_for_batch(
        manifest,
        current_bundle,
        batch_paths=list(batch_plan.files or []),
        dependency_file_max_chars=effective_dependency_file_max_chars,
        max_dependency_files=int(budget_profile["batch_max_dep_files"]),
    )
    mode_rule_text = "\n".join(_resolved_codegen_rule_lines(mode_config, gate_decision, scope=scope))
    agent_spec = AgentSpec(
        name="codegen_batch",
        role="Codegen Batch Worker",
        goal="Generate one dependency-safe file batch that fits the approved manifest.",
        backstory=(
            "You are generating one staged batch of code for a larger implementation.\n"
            "- Generate only the files assigned to the current batch.\n"
            "- Preserve imports and contracts required by earlier files.\n"
            "- Output only runnable code in CodeBundle JSON form."
        ),
        output_schema_name="CodeBundle",
        parallel_safe=False,
        retry_policy=RetryPolicy(max_attempts=8, backoff_seconds=2.0, retry_on_json_fail=True),
        version="v1.0.0",
        behavior_contract="Return only the files assigned to the current codegen batch.",
    )
    task_spec = TaskSpec(
        name="codegen_batch",
        description_template=(
            "Generate exactly one codegen batch as a CodeBundle JSON object.\n"
            "Mode: {mode_name}\n"
            "project_type must be '{project_type}'.\n"
            "Language hint: {language_hint}\n\n"
            "Rules:\n"
            "- Return only files from the current batch.\n"
            "- Do not omit any file listed in the current batch.\n"
            "- Keep imports, names, and contracts consistent with dependency files already completed.\n"
            "- Do not emit markdown or commentary.\n"
            "{mode_rule_text}\n\n"
            "User problem:\n{user_problem}\n\n"
            "Approved implementation context:\n{approved_context}\n\n"
            "Current manifest slice:\n{manifest_prompt}\n\n"
            "Previously completed dependency files:\n{existing_bundle_context}\n"
        ),
        agent_name="codegen_batch",
        expected_output="CodeBundle JSON only.",
        max_input_chars=effective_max_input_chars,
    )
    static_template_vars = {
        "mode_name": mode_config.name,
        "project_type": project_type,
        "language_hint": language_hint,
        "mode_rule_text": mode_rule_text,
    }
    template_vars = _budget_prompt_template_sections(
        description_template=task_spec.description_template,
        static_template_vars=static_template_vars,
        section_values={
            "user_problem": limit_text(user_problem, 4000),
            "approved_context": approved_context,
            "manifest_prompt": manifest_prompt,
            "existing_bundle_context": existing_bundle_context,
        },
        section_priority=[
            "manifest_prompt",
            "existing_bundle_context",
            "approved_context",
            "user_problem",
        ],
        section_reserve_chars=CODEGEN_BATCH_SECTION_RESERVE_CHARS,
        max_input_chars=effective_max_input_chars,
    )
    return _build_codegen_single_task_crew(
        crew_name="codegen_batch_crew",
        llm=llm,
        agent_spec=agent_spec,
        task_spec=task_spec,
        template_vars=template_vars,
        verbose=False,
    )


def _extract_codegen_bundle_from_result(
    result: Any,
    *,
    llm: Any,
    language_hint: str,
    mode: str,
) -> Optional[CodeBundle]:
    # Phase 1: try cheap parse on the result and on all text candidates first.
    # Each iteration of the legacy interleaved loop spent an LLM call on
    # reformatting raw_i the moment its parse failed, even when raw_{i+1}'s
    # parse would have succeeded for free.  Doing every parse before any
    # reformat call avoids that wasted LLM call in the common case where one of
    # the later candidates (CrewAI exposes the same output via several attrs:
    # json_dict, raw, output, text, content, tasks_output[*]) is already valid
    # JSON.
    bundle = extract_code_bundle(result)
    if bundle is None:
        text_candidates = _collect_text_candidates_from_result(result)
        for raw in reversed(text_candidates):
            bundle = extract_code_bundle(raw)
            if bundle is not None:
                break
        # Phase 2: only when every cheap parse has failed do we fall back to
        # the LLM-driven schema reformatter.  STRICT_JSON_ENABLED gates this so
        # non-strict runs don't pay the extra LLM round-trip.
        if bundle is None and STRICT_JSON_ENABLED:
            for raw in reversed(text_candidates):
                bundle = _reformat_code_bundle(
                    raw, llm=llm, language_hint=language_hint, mode=mode
                )
                if bundle is not None:
                    break
    bundle = _sanitize_code_bundle(bundle)
    if bundle is not None and not _bundle_has_files(bundle):
        return None
    return bundle


def _prune_code_bundle_to_paths(
    bundle: Optional[CodeBundle],
    *,
    allowed_paths: Set[str],
) -> Optional[CodeBundle]:
    clean = _sanitize_code_bundle(bundle)
    if clean is None:
        return None
    normalized_allowed = {_normalize_bundle_relpath(path) for path in allowed_paths if path}
    normalized_allowed.discard("")
    filtered_files: List[GeneratedFile] = []
    seen: Set[str] = set()
    for file in list(clean.files or []):
        normalized = _normalize_bundle_relpath(file.path)
        if not normalized or normalized not in normalized_allowed or normalized in seen:
            continue
        filtered_files.append(GeneratedFile(path=normalized, content=file.content))
        seen.add(normalized)
    return CodeBundle(project_type=clean.project_type, files=filtered_files)


def _py_syntax_error_in_bundle(bundle: Optional["CodeBundle"]) -> Optional[str]:
    """Return a human-readable error if any .py file in *bundle* has a SyntaxError.

    Uses the built-in ``compile()`` for a syntax-only check (no bytecode written).
    Returns None when all Python files are syntactically valid.
    """
    if bundle is None:
        return None
    for f in list(bundle.files or []):
        if not (f.path or "").endswith(".py"):
            continue
        try:
            compile(f.content or "", f.path, "exec")
        except (SyntaxError, TypeError) as exc:
            lineno = getattr(exc, "lineno", None)
            msg = getattr(exc, "msg", str(exc))
            location = f" near line {lineno}" if lineno else ""
            return f"Python SyntaxError in {f.path}{location}: {msg}"
    return None


def _py_syntax_error_paths_in_bundle(
    bundle: Optional["CodeBundle"],
) -> List[Tuple[str, str]]:
    """Return ``[(path, error_message), ...]`` for every .py file with a SyntaxError.

    Unlike :func:`_py_syntax_error_in_bundle` (which short-circuits on the first
    failure), this scans the entire bundle so the syntax-repair supplement can
    request regeneration of *all* broken files in a single targeted call rather
    than discovering them one-by-one across multiple supplement rounds.
    Returns an empty list when no Python files have SyntaxErrors.
    """
    results: List[Tuple[str, str]] = []
    if bundle is None:
        return results
    for f in list(bundle.files or []):
        if not (f.path or "").endswith(".py"):
            continue
        try:
            compile(f.content or "", f.path, "exec")
        except (SyntaxError, TypeError) as exc:
            lineno = getattr(exc, "lineno", None)
            msg = getattr(exc, "msg", str(exc))
            location = f" near line {lineno}" if lineno else ""
            results.append(
                (f.path, f"Python SyntaxError in {f.path}{location}: {msg}")
            )
    return results


def _validate_batch_bundle(
    bundle: Optional[CodeBundle],
    *,
    batch_plan: CodegenBatchPlan,
    mode: str,
    current_bundle: Optional[CodeBundle] = None,
) -> Tuple[Optional[CodeBundle], Optional[str]]:
    duplicate_paths = _code_bundle_duplicate_paths(bundle)
    if duplicate_paths:
        return None, "Codegen batch returned duplicate file paths: " + ", ".join(
            duplicate_paths
        )
    clean = _sanitize_code_bundle(bundle)
    if clean is None or not _bundle_has_files(clean):
        return None, "Codegen batch did not return a valid CodeBundle."
    mismatch_reason = _code_bundle_mode_mismatch_reason(clean, mode)
    if mismatch_reason:
        return None, mismatch_reason
    expected_paths = {
        _normalize_bundle_relpath(path)
        for path in list(getattr(batch_plan, "files", []) or [])
        if _normalize_bundle_relpath(path)
    }
    actual_paths = {
        _normalize_bundle_relpath(file.path)
        for file in list(clean.files or [])
        if _normalize_bundle_relpath(file.path)
    }
    missing = sorted(path for path in expected_paths if path not in actual_paths)
    extra = sorted(path for path in actual_paths if path not in expected_paths)
    if missing:
        return None, "Codegen batch omitted planned files: " + ", ".join(missing)
    if extra:
        # All planned files are present (missing is empty at this point).
        # The LLM generated unsolicited extra files — prune to the planned set and
        # accept the result rather than discarding the entire batch output.
        # This covers two sub-cases:
        #   (a) extra files already exist in current_bundle (LLM echoed prior output)
        #   (b) extra files are genuinely new (LLM hallucinated bonus files)
        # In both cases the correct recovery is to keep only the planned paths.
        pruned = _prune_code_bundle_to_paths(clean, allowed_paths=expected_paths)
        if pruned is not None and _bundle_has_files(pruned):
            syntax_err = _py_syntax_error_in_bundle(pruned)
            if syntax_err:
                return None, syntax_err
            return pruned, None
        return None, "Codegen batch returned files outside the planned batch: " + ", ".join(extra)
    syntax_err = _py_syntax_error_in_bundle(clean)
    if syntax_err:
        return None, syntax_err
    return clean, None


def _merge_supplement_into_bundle(
    base: "CodeBundle", supplement: "CodeBundle"
) -> Optional["CodeBundle"]:
    """Merge supplement files into base bundle.

    Files in *supplement* take precedence over base files at the same
    normalised path.  Used to graft files produced by a targeted
    supplement run onto a partial bundle that was truncated earlier.
    Returns a new CodeBundle, or None if the merged result has no files.

    NOTE: This helper is *distinct* from :func:`_merge_code_bundles` (which
    merges two completed batch bundles using ``project_type`` and
    sanitisation).  Keep them separate — they have different signatures and
    different invariants, and were previously colliding under the same name
    which silently broke the staged codegen pipeline.
    """
    if base is None:
        return supplement if _bundle_has_files(supplement) else None
    if supplement is None or not _bundle_has_files(supplement):
        return base if _bundle_has_files(base) else None
    supplement_paths = {
        _normalize_bundle_relpath(f.path)
        for f in (supplement.files or [])
        if f.path
    }
    # Keep base files whose paths are NOT being replaced by the supplement.
    merged_files = [
        f
        for f in (base.files or [])
        if _normalize_bundle_relpath(f.path) not in supplement_paths
    ]
    merged_files.extend(supplement.files or [])
    if not merged_files:
        return None
    return _model_copy_compat(base, update={"files": merged_files})


def _run_codegen_manifest_stage(
    user_problem: str,
    *,
    mode: str,
    language_hint: str,
    llm: Any,
    analysis_report: Optional[AnalysisReport],
    gate_decision: Optional[GateDecision],
    run_snapshot: Optional[RunSnapshot],
    scope: str = "mvp",
) -> Tuple[CodegenManifest, int]:
    fallback_crew_factory = lambda: _build_codegen_manifest_crew(
        user_problem,
        mode=mode,
        language_hint=language_hint,
        llm=llm,
        analysis_report=analysis_report,
        gate_decision=gate_decision,
        context_max_chars=max(3000, CODEGEN_MANIFEST_CONTEXT_MAX_CHARS // 2),
        max_input_chars=max(5000, CODEGEN_MANIFEST_MAX_INPUT_CHARS // 2),
        scope=scope,
    )
    crew = _build_codegen_manifest_crew(
        user_problem,
        mode=mode,
        language_hint=language_hint,
        llm=llm,
        analysis_report=analysis_report,
        gate_decision=gate_decision,
        context_max_chars=CODEGEN_MANIFEST_CONTEXT_MAX_CHARS,
        max_input_chars=CODEGEN_MANIFEST_MAX_INPUT_CHARS,
        scope=scope,
    )
    _sync_codegen_snapshot_metadata(run_snapshot, crew, stage_prefix="codegen.manifest")
    _snapshot_record_stage(run_snapshot, stage="codegen.manifest", status="started")
    _cost_trace("codegen.manifest", mode=mode)
    result, effective_crew, stage_prompt_chars = _kickoff_codegen_substage_with_recovery(
        crew,
        fallback_crew_factory=fallback_crew_factory,
        mode=mode,
        stage_name="codegen_manifest_crew.kickoff",
    )
    if effective_crew is not crew:
        _sync_codegen_snapshot_metadata(
            run_snapshot,
            effective_crew,
            stage_prefix="codegen.manifest",
        )
    manifest = _extract_codegen_manifest(
        result, llm=llm, language_hint=language_hint, mode=mode
    )
    if manifest is None and effective_crew is crew:
        result, effective_crew, fallback_prompt_chars = _run_codegen_substage_fallback_attempt(
            fallback_crew_factory=fallback_crew_factory,
            mode=mode,
            stage_name="codegen_manifest_crew.kickoff",
            reason="manifest_parse_failed",
            error_type="ValueError",
        )
        stage_prompt_chars += fallback_prompt_chars
        _sync_codegen_snapshot_metadata(
            run_snapshot,
            effective_crew,
            stage_prefix="codegen.manifest",
        )
        manifest = _extract_codegen_manifest(
            result, llm=llm, language_hint=language_hint, mode=mode
        )
    effective_prompt_chars = max(
        stage_prompt_chars, _prompt_chars_for_crew(effective_crew, user_problem)
    )
    if manifest is None:
        # Never-terminate: synthesise a fallback manifest so the pipeline can
        # still attempt code generation instead of aborting.  See module-level
        # documentation at ``CODEGEN_FALLBACK_MANIFEST`` for the policy.
        if CODEGEN_FALLBACK_MANIFEST:
            manifest = _synthesize_fallback_manifest(
                mode=mode,
                analysis_report=analysis_report,
                user_problem=user_problem,
            )
            LOGGER.warning(
                "Codegen manifest parse failed; synthesised fallback manifest "
                "with %d batch(es) and %d file(s).  Set "
                "CODEGEN_FALLBACK_MANIFEST=0 to abort instead.",
                len(list(manifest.generation_batches or [])),
                len(list(manifest.files or [])),
            )
            _snapshot_record_stage(
                run_snapshot,
                stage="codegen.manifest",
                status="completed",
                failure_type=FailureType.NONE,
                notes=(
                    "synthesised_fallback batches="
                    + str(len(list(manifest.generation_batches or [])))
                    + " files="
                    + str(len(list(manifest.files or [])))
                ),
            )
            return manifest, effective_prompt_chars
        _snapshot_record_stage(
            run_snapshot,
            stage="codegen.manifest",
            status="failed",
            failure_type=FailureType.JSON_INVALID,
            notes="CodegenManifest parse failed.",
        )
        exc = ValueError("CodegenManifest parse failed.")
        setattr(exc, "_staged_codegen_prompt_chars", effective_prompt_chars)
        raise exc
    _snapshot_record_stage(
        run_snapshot,
        stage="codegen.manifest",
        status="completed",
        failure_type=FailureType.NONE,
        notes=f"batches={len(list(manifest.generation_batches or []))}",
    )
    return manifest, effective_prompt_chars


def _run_codegen_batch_stage(
    user_problem: str,
    *,
    mode: str,
    language_hint: str,
    llm: Any,
    analysis_report: Optional[AnalysisReport],
    gate_decision: Optional[GateDecision],
    manifest: CodegenManifest,
    batch_plan: CodegenBatchPlan,
    current_bundle: Optional[CodeBundle],
    run_snapshot: Optional[RunSnapshot],
    batch_index: int,
    scope: str = "mvp",
) -> Tuple[CodeBundle, int]:
    # Derive a high-cap LLM variant for code generation.  Reasoning models
    # (minimax-m2.7, GLM-5.1 …) default to 8 192 max completion tokens; their
    # chain-of-thought reasoning alone consumes 5 000-7 000 of that budget,
    # leaving only ~1 000-3 000 tokens for actual code — not enough for even a
    # single medium-sized Python file.  CODEGEN_MAX_TOKENS (default 32 768) gives
    # the model room to reason AND produce complete file output.
    codegen_llm = _make_codegen_llm(llm)
    fallback_crew_factory = lambda: _build_codegen_batch_crew(
        user_problem,
        mode=mode,
        language_hint=language_hint,
        llm=codegen_llm,
        analysis_report=analysis_report,
        gate_decision=gate_decision,
        manifest=manifest,
        batch_plan=batch_plan,
        current_bundle=current_bundle,
        context_max_chars=max(2500, CODEGEN_BATCH_CONTEXT_MAX_CHARS // 2),
        dependency_file_max_chars=CODEGEN_BATCH_DEP_FILE_MAX_CHARS_FALLBACK,
        max_input_chars=max(5000, CODEGEN_BATCH_MAX_INPUT_CHARS // 2),
        scope=scope,
    )
    crew = _build_codegen_batch_crew(
        user_problem,
        mode=mode,
        language_hint=language_hint,
        llm=codegen_llm,
        analysis_report=analysis_report,
        gate_decision=gate_decision,
        manifest=manifest,
        batch_plan=batch_plan,
        current_bundle=current_bundle,
        context_max_chars=CODEGEN_BATCH_CONTEXT_MAX_CHARS,
        dependency_file_max_chars=CODEGEN_BATCH_DEP_FILE_MAX_CHARS,
        max_input_chars=CODEGEN_BATCH_MAX_INPUT_CHARS,
        scope=scope,
    )
    stage_label = f"codegen.batch_{batch_index}"
    _sync_codegen_snapshot_metadata(run_snapshot, crew, stage_prefix=stage_label)
    _snapshot_record_stage(
        run_snapshot,
        stage=stage_label,
        status="started",
        notes="files=" + ", ".join(list(batch_plan.files or [])),
    )
    _cost_trace(stage_label, mode=mode)
    result, effective_crew, stage_prompt_chars = _kickoff_codegen_substage_with_recovery(
        crew,
        fallback_crew_factory=fallback_crew_factory,
        mode=mode,
        stage_name=f"codegen_batch_crew_{batch_index}.kickoff",
    )
    if effective_crew is not crew:
        _sync_codegen_snapshot_metadata(
            run_snapshot,
            effective_crew,
            stage_prefix=stage_label,
        )
    # Save the raw (pre-validation) bundle so the supplement stage can use
    # whatever files were successfully generated even when validation fails.
    _raw_primary_bundle = _extract_codegen_bundle_from_result(
        result, llm=codegen_llm, language_hint=language_hint, mode=mode
    )
    bundle, failure_note = _validate_batch_bundle(
        _raw_primary_bundle, batch_plan=batch_plan, mode=mode, current_bundle=current_bundle
    )
    _raw_fallback_bundle: Optional["CodeBundle"] = None
    if bundle is None and effective_crew is crew:
        result, effective_crew, fallback_prompt_chars = _run_codegen_substage_fallback_attempt(
            fallback_crew_factory=fallback_crew_factory,
            mode=mode,
            stage_name=f"codegen_batch_crew_{batch_index}.kickoff",
            reason="batch_output_validation_failed",
            error_type="ValueError",
        )
        stage_prompt_chars += fallback_prompt_chars
        _sync_codegen_snapshot_metadata(
            run_snapshot,
            effective_crew,
            stage_prefix=stage_label,
        )
        _raw_fallback_bundle = _extract_codegen_bundle_from_result(
            result, llm=codegen_llm, language_hint=language_hint, mode=mode
        )
        bundle, failure_note = _validate_batch_bundle(
            _raw_fallback_bundle, batch_plan=batch_plan, mode=mode, current_bundle=current_bundle
        )

    # When the missing-files supplement structurally merges but the merged
    # bundle still fails validation (e.g. the supplement itself emitted a file
    # with a Python SyntaxError), persist the more-complete merged bundle so
    # the syntax-repair supplement below can operate on it rather than the
    # raw partial.  Initialised to None so the syntax-repair block falls back
    # to the raw primary/fallback bundle when this code path is not entered.
    _missing_files_merged_partial: Optional["CodeBundle"] = None

    # Supplement attempt: both primary and fallback produced a partial bundle
    # (some files present but specific ones missing — typically because a
    # reasoning model exhausted its token budget mid-output and the JSON was
    # truncated before the last file was written).  Instead of re-running the
    # entire batch, make a targeted call that asks for ONLY the missing files
    # and merges them into the best partial result.
    if (
        bundle is None
        and failure_note is not None
        and "omitted planned files:" in failure_note
    ):
        _best_partial: Optional["CodeBundle"] = (
            _raw_fallback_bundle
            if _raw_fallback_bundle is not None and _bundle_has_files(_raw_fallback_bundle)
            else _raw_primary_bundle
        )
        _marker = "omitted planned files:"
        _missing_suffix = failure_note[failure_note.index(_marker) + len(_marker):]
        _missing_paths = [p.strip() for p in _missing_suffix.split(",") if p.strip()]
        if (
            _best_partial is not None
            and _bundle_has_files(_best_partial)
            and 0 < len(_missing_paths) <= _SUPPLEMENT_MAX_MISSING_FILES
        ):
            LOGGER.info(
                "Codegen batch_%d supplement: generating %d missing file(s): %s",
                batch_index,
                len(_missing_paths),
                ", ".join(_missing_paths),
            )
            try:
                _supplement_plan = CodegenBatchPlan(
                    name=f"{batch_plan.name or 'batch'}_supplement",
                    objective=(
                        f"Generate ONLY the following files that were omitted from the "
                        f"previous generation pass: {', '.join(_missing_paths)}.  "
                        "All other files have already been generated successfully and "
                        "are provided in current_bundle as context."
                    ),
                    files=_missing_paths,
                )
                _supplement_crew = _build_codegen_batch_crew(
                    user_problem,
                    mode=mode,
                    language_hint=language_hint,
                    llm=codegen_llm,
                    analysis_report=analysis_report,
                    gate_decision=gate_decision,
                    manifest=manifest,
                    batch_plan=_supplement_plan,
                    current_bundle=_best_partial,
                    context_max_chars=max(2500, CODEGEN_BATCH_CONTEXT_MAX_CHARS // 2),
                    dependency_file_max_chars=CODEGEN_BATCH_DEP_FILE_MAX_CHARS_FALLBACK,
                    max_input_chars=max(5000, CODEGEN_BATCH_MAX_INPUT_CHARS // 2),
                    scope=scope,
                )
                _supplement_result = kickoff_crew_with_retry(
                    _supplement_crew,
                    crew_name=f"codegen_batch_crew_{batch_index}_supplement.kickoff",
                    logger=LOGGER,
                    log_fields={
                        "stage": f"codegen.batch_{batch_index}_supplement",
                        "mode": mode,
                        "missing_files": ", ".join(_missing_paths),
                    },
                )
                stage_prompt_chars += int(
                    getattr(_supplement_crew, "_prompt_total_chars", 0) or 0
                )
                _supplement_raw = _extract_codegen_bundle_from_result(
                    _supplement_result,
                    llm=codegen_llm,
                    language_hint=language_hint,
                    mode=mode,
                )
                # Defensive: prune the supplement bundle to ONLY the missing paths
                # we asked for.  An LLM that ignores its scope and re-emits the
                # entire batch must NOT be allowed to overwrite the
                # already-validated files in `_best_partial`.  Pruning also
                # discards any hallucinated extras the LLM may have added.
                _missing_path_set = {
                    _normalize_bundle_relpath(p) for p in _missing_paths if p
                }
                _missing_path_set.discard("")
                _supplement_filtered = _prune_code_bundle_to_paths(
                    _supplement_raw, allowed_paths=_missing_path_set
                )
                if (
                    _supplement_filtered is not None
                    and _bundle_has_files(_supplement_filtered)
                ):
                    # Surface any out-of-scope behaviour for diagnosability.
                    if _supplement_raw is not None:
                        _supp_raw_paths = {
                            _normalize_bundle_relpath(f.path)
                            for f in (_supplement_raw.files or [])
                            if f.path
                        }
                        _extras = sorted(_supp_raw_paths - _missing_path_set - {""})
                        if _extras:
                            LOGGER.warning(
                                "Codegen batch_%d supplement returned %d "
                                "out-of-scope file(s) which were discarded: %s",
                                batch_index,
                                len(_extras),
                                ", ".join(_extras),
                            )
                    _merged = _merge_supplement_into_bundle(
                        _best_partial, _supplement_filtered
                    )
                    if _merged is not None:
                        _merged_val, _merged_note = _validate_batch_bundle(
                            _merged,
                            batch_plan=batch_plan,
                            mode=mode,
                            current_bundle=current_bundle,
                        )
                        if _merged_val is not None:
                            bundle = _merged_val
                            failure_note = None
                            LOGGER.info(
                                "Codegen batch_%d supplement succeeded; merged: %s",
                                batch_index,
                                ", ".join(_missing_paths),
                            )
                        else:
                            # Merge produced a structurally-complete bundle but
                            # validation rejected it (typical cause: the
                            # supplement filled the missing paths but emitted
                            # syntactically-broken code in one of them).
                            # Update failure_note so downstream recovery
                            # strategies (the syntax-repair supplement below)
                            # trigger on the actual reason instead of the stale
                            # "omitted planned files" marker, and persist the
                            # merged bundle so the syntax-repair block can
                            # operate on the more-complete file set.
                            failure_note = _merged_note or failure_note
                            if _bundle_has_files(_merged):
                                _missing_files_merged_partial = _merged
                            LOGGER.warning(
                                "Codegen batch_%d supplement merged but failed "
                                "post-merge validation: %s",
                                batch_index,
                                _merged_note,
                            )
            except _OperationCancelledError:
                raise
            except Exception as _supp_exc:
                LOGGER.warning(
                    "Codegen batch_%d supplement failed: %s",
                    batch_index,
                    _supp_exc,
                    exc_info=False,
                )

    # ── Syntax-repair supplement ─────────────────────────────────────────────
    # When the batch validation fails specifically because one or more Python
    # files contain SyntaxErrors (typically because the LLM hit its completion
    # token cap mid-output and produced truncated/garbled code), make a targeted
    # supplement call asking ONLY for the broken files to be regenerated, then
    # merge the repaired files into the otherwise-good partial bundle.  Distinct
    # from the missing-files supplement above: with this failure mode the file
    # paths *are* present, but their *contents* are syntactically invalid.
    if (
        bundle is None
        and failure_note is not None
        and "Python SyntaxError" in failure_note
    ):
        # Prefer the merged-but-broken bundle from a prior missing-files
        # supplement (when the merge structurally completed but introduced its
        # own syntax error).  Falling back to the raw primary/fallback bundle
        # would lose the files the missing-files supplement just added.
        if (
            _missing_files_merged_partial is not None
            and _bundle_has_files(_missing_files_merged_partial)
        ):
            _best_partial_se: Optional["CodeBundle"] = _missing_files_merged_partial
        else:
            _best_partial_se = (
                _raw_fallback_bundle
                if _raw_fallback_bundle is not None and _bundle_has_files(_raw_fallback_bundle)
                else _raw_primary_bundle
            )
        # Sanitise so path normalisation, project_type validation and content
        # un-escaping match the validator's view of the bundle.
        _clean_partial_se = _sanitize_code_bundle(_best_partial_se)
        if _clean_partial_se is not None and _bundle_has_files(_clean_partial_se):
            _planned_paths_se = {
                _normalize_bundle_relpath(p)
                for p in (batch_plan.files or [])
                if p and _normalize_bundle_relpath(p)
            }
            _broken_seen_se: Set[str] = set()
            _broken_paths_se: List[str] = []
            for _bp_path, _bp_msg in _py_syntax_error_paths_in_bundle(_clean_partial_se):
                _norm = _normalize_bundle_relpath(_bp_path)
                if (
                    _norm
                    and _norm in _planned_paths_se
                    and _norm not in _broken_seen_se
                ):
                    _broken_seen_se.add(_norm)
                    _broken_paths_se.append(_norm)
            if 0 < len(_broken_paths_se) <= _SUPPLEMENT_MAX_MISSING_FILES:
                LOGGER.info(
                    "Codegen batch_%d syntax-repair: regenerating %d file(s) "
                    "with Python SyntaxErrors: %s",
                    batch_index,
                    len(_broken_paths_se),
                    ", ".join(_broken_paths_se),
                )
                try:
                    _se_supplement_plan = CodegenBatchPlan(
                        name=f"{batch_plan.name or 'batch'}_syntax_repair",
                        objective=(
                            "REGENERATE ONLY the following files because the "
                            "previous generation pass produced files with Python "
                            f"SyntaxErrors: {', '.join(_broken_paths_se)}.  "
                            "Emit COMPLETE, syntactically valid Python source for "
                            "each listed file (verify every parenthesis, bracket, "
                            "comma and string literal closes correctly).  "
                            "Files NOT in this list have already been generated "
                            "successfully and are provided in current_bundle as "
                            "context — do NOT re-emit them."
                        ),
                        files=list(_broken_paths_se),
                    )
                    _se_supplement_crew = _build_codegen_batch_crew(
                        user_problem,
                        mode=mode,
                        language_hint=language_hint,
                        llm=codegen_llm,
                        analysis_report=analysis_report,
                        gate_decision=gate_decision,
                        manifest=manifest,
                        batch_plan=_se_supplement_plan,
                        current_bundle=_clean_partial_se,
                        context_max_chars=max(
                            2500, CODEGEN_BATCH_CONTEXT_MAX_CHARS // 2
                        ),
                        dependency_file_max_chars=CODEGEN_BATCH_DEP_FILE_MAX_CHARS_FALLBACK,
                        max_input_chars=max(
                            5000, CODEGEN_BATCH_MAX_INPUT_CHARS // 2
                        ),
                        scope=scope,
                    )
                    _se_supplement_result = kickoff_crew_with_retry(
                        _se_supplement_crew,
                        crew_name=(
                            f"codegen_batch_crew_{batch_index}_syntax_repair.kickoff"
                        ),
                        logger=LOGGER,
                        log_fields={
                            "stage": (
                                f"codegen.batch_{batch_index}_syntax_repair"
                            ),
                            "mode": mode,
                            "broken_files": ", ".join(_broken_paths_se),
                        },
                    )
                    stage_prompt_chars += int(
                        getattr(_se_supplement_crew, "_prompt_total_chars", 0)
                        or 0
                    )
                    _se_supplement_raw = _extract_codegen_bundle_from_result(
                        _se_supplement_result,
                        llm=codegen_llm,
                        language_hint=language_hint,
                        mode=mode,
                    )
                    _se_target_set = {
                        _normalize_bundle_relpath(p)
                        for p in _broken_paths_se
                        if p
                    }
                    _se_target_set.discard("")
                    # Defensive prune: drop any out-of-scope files the LLM
                    # may have re-emitted, and silently discard anything
                    # outside the broken-paths set so we never overwrite an
                    # already-good file with a fresh attempt.
                    _se_supplement_filtered = _prune_code_bundle_to_paths(
                        _se_supplement_raw, allowed_paths=_se_target_set
                    )
                    if (
                        _se_supplement_filtered is not None
                        and _bundle_has_files(_se_supplement_filtered)
                    ):
                        if _se_supplement_raw is not None:
                            _se_raw_paths = {
                                _normalize_bundle_relpath(f.path)
                                for f in (_se_supplement_raw.files or [])
                                if f.path
                            }
                            _se_extras = sorted(
                                _se_raw_paths - _se_target_set - {""}
                            )
                            if _se_extras:
                                LOGGER.warning(
                                    "Codegen batch_%d syntax-repair returned "
                                    "%d out-of-scope file(s) which were "
                                    "discarded: %s",
                                    batch_index,
                                    len(_se_extras),
                                    ", ".join(_se_extras),
                                )
                        # Pre-flight syntax check on the supplement's repaired
                        # files.  If the LLM still emits broken syntax we
                        # surface that distinctly rather than producing the
                        # same generic failure_note as the original validator.
                        _se_residual_errors = _py_syntax_error_paths_in_bundle(
                            _se_supplement_filtered
                        )
                        if _se_residual_errors:
                            LOGGER.warning(
                                "Codegen batch_%d syntax-repair supplement "
                                "still produced syntax errors in %d file(s); "
                                "merge will be rejected by validator: %s",
                                batch_index,
                                len(_se_residual_errors),
                                "; ".join(
                                    msg for _, msg in _se_residual_errors
                                ),
                            )
                        _se_merged = _merge_supplement_into_bundle(
                            _clean_partial_se, _se_supplement_filtered
                        )
                        if _se_merged is not None:
                            (
                                _se_merged_val,
                                _se_merged_note,
                            ) = _validate_batch_bundle(
                                _se_merged,
                                batch_plan=batch_plan,
                                mode=mode,
                                current_bundle=current_bundle,
                            )
                            if _se_merged_val is not None:
                                bundle = _se_merged_val
                                failure_note = None
                                LOGGER.info(
                                    "Codegen batch_%d syntax-repair "
                                    "supplement succeeded; repaired: %s",
                                    batch_index,
                                    ", ".join(_broken_paths_se),
                                )
                            else:
                                LOGGER.warning(
                                    "Codegen batch_%d syntax-repair merged "
                                    "but failed validation: %s",
                                    batch_index,
                                    _se_merged_note,
                                )
                except _OperationCancelledError:
                    raise
                except Exception as _se_supp_exc:
                    LOGGER.warning(
                        "Codegen batch_%d syntax-repair supplement failed: %s",
                        batch_index,
                        _se_supp_exc,
                        exc_info=False,
                    )
            elif len(_broken_paths_se) > _SUPPLEMENT_MAX_MISSING_FILES:
                LOGGER.warning(
                    "Codegen batch_%d skipping syntax-repair supplement: "
                    "too many broken files (%d > %d)",
                    batch_index,
                    len(_broken_paths_se),
                    _SUPPLEMENT_MAX_MISSING_FILES,
                )

    effective_prompt_chars = max(
        stage_prompt_chars, _prompt_chars_for_crew(effective_crew, user_problem)
    )

    # ── Lenient-output salvage ────────────────────────────────────────────────
    # Strict validation and every supplement attempt have failed.  When
    # ``CODEGEN_LENIENT_OUTPUT`` is on (default) we prefer producing partial
    # output — even with syntax errors or missing files — over discarding 6+
    # minutes of LLM work.  The user can fix issues manually faster than a
    # full regeneration.  Set ``CODEGEN_LENIENT_OUTPUT=0`` for the historical
    # strict-raise behaviour (e.g. CI gates that require complete bundles).
    if bundle is None and CODEGEN_LENIENT_OUTPUT:
        _salvage_source: Optional["CodeBundle"] = (
            _missing_files_merged_partial
            if _missing_files_merged_partial is not None
            and _bundle_has_files(_missing_files_merged_partial)
            else (
                _raw_fallback_bundle
                if _raw_fallback_bundle is not None
                and _bundle_has_files(_raw_fallback_bundle)
                else _raw_primary_bundle
            )
        )
        salvaged_bundle, salvage_missing, salvage_syntax_err = _salvage_codegen_batch_bundle(
            _salvage_source,
            batch_plan=batch_plan,
            project_type=manifest.project_type,
        )
        if salvaged_bundle is not None and _bundle_has_files(salvaged_bundle):
            bundle = salvaged_bundle
            failure_note = None
            LOGGER.warning(
                "Codegen batch_%d salvaged in lenient-output mode: kept %d "
                "of %d planned file(s); missing=%s; syntax_errors=%s.  "
                "Set CODEGEN_LENIENT_OUTPUT=0 to abort instead of salvaging.",
                batch_index,
                len(salvaged_bundle.files or []),
                len(list(batch_plan.files or [])),
                ", ".join(salvage_missing) if salvage_missing else "(none)",
                ", ".join(salvage_syntax_err) if salvage_syntax_err else "(none)",
            )
            _snapshot_record_stage(
                run_snapshot,
                stage=stage_label,
                status="completed",
                failure_type=FailureType.NONE,
                notes=(
                    "salvaged_lenient files="
                    + ", ".join(f.path for f in (salvaged_bundle.files or []))
                    + (
                        " missing=" + ", ".join(salvage_missing)
                        if salvage_missing
                        else ""
                    )
                    + (
                        " syntax_errors=" + ", ".join(salvage_syntax_err)
                        if salvage_syntax_err
                        else ""
                    )
                ),
            )

    if bundle is None:
        _snapshot_record_stage(
            run_snapshot,
            stage=stage_label,
            status="failed",
            failure_type=FailureType.JSON_INVALID,
            notes=failure_note,
        )
        exc = ValueError(failure_note or "Codegen batch failed validation.")
        setattr(exc, "_staged_codegen_prompt_chars", effective_prompt_chars)
        raise exc
    _snapshot_record_stage(
        run_snapshot,
        stage=stage_label,
        status="completed",
        failure_type=FailureType.NONE,
        notes="files=" + ", ".join(list(batch_plan.files or [])),
    )
    return bundle, effective_prompt_chars


def _finalize_staged_codegen_bundle(
    current_bundle: Optional[CodeBundle],
    *,
    manifest: CodegenManifest,
    mode: str,
) -> Tuple[Optional[CodeBundle], Optional[str]]:
    duplicate_paths = _code_bundle_duplicate_paths(current_bundle)
    if duplicate_paths:
        return None, "Staged codegen returned duplicate file paths: " + ", ".join(
            duplicate_paths
        )
    clean = _sanitize_code_bundle(current_bundle)
    if clean is None or not _bundle_has_files(clean):
        return None, "Staged codegen did not produce a valid CodeBundle."
    mismatch_reason = _code_bundle_mode_mismatch_reason(clean, mode)
    if mismatch_reason:
        return None, mismatch_reason
    expected_paths = {
        _normalize_bundle_relpath(plan.path) for plan in list(manifest.files or [])
    }
    actual_paths = {
        _normalize_bundle_relpath(file.path) for file in list(clean.files or [])
    }
    missing = sorted(path for path in expected_paths if path not in actual_paths)
    if missing:
        return None, "Staged codegen is missing planned files: " + ", ".join(missing)
    missing_entrypoints = [
        path for path in list(manifest.entrypoints or []) if path not in actual_paths
    ]
    if missing_entrypoints:
        return None, "Staged codegen is missing planned entrypoints: " + ", ".join(missing_entrypoints)
    return clean, None


# ── Lenient-output salvage helpers ────────────────────────────────────────────
# Used when ``CODEGEN_LENIENT_OUTPUT`` is on (default) to recover partial output
# instead of raising and losing the entire LLM run.  See module-level docstring
# at the ``CODEGEN_LENIENT_OUTPUT`` definition for the keep/drop policy.

def _salvage_codegen_batch_bundle(
    best_partial: Optional[CodeBundle],
    *,
    batch_plan: CodegenBatchPlan,
    project_type: str,
) -> Tuple[Optional[CodeBundle], List[str], List[str]]:
    """Salvage whatever the LLM produced for a single batch when strict
    validation fails and all supplement attempts are exhausted.

    Returns ``(salvaged_bundle, missing_paths, syntax_error_paths)``.  Files
    outside the batch's planned scope are dropped (anti-hallucination), but
    every other safely-typed file in *best_partial* is preserved verbatim,
    even if it has Python syntax errors — the user can fix them faster than
    re-running a 6-minute LLM call.

    Returns ``(None, ...)`` only when nothing whatsoever can be salvaged
    (no input bundle, no safe paths, or sanitiser rejected the whole bundle
    e.g. due to invalid project_type).  In that pathological case the caller
    should still raise so the pipeline does not silently produce empty code.
    """
    planned_paths = {
        _normalize_bundle_relpath(p)
        for p in (batch_plan.files or [])
        if p and _normalize_bundle_relpath(p)
    }
    planned_paths.discard("")
    if not planned_paths:
        # Defensive: a batch plan with zero planned files is degenerate.
        # Nothing we can scope the salvage to.
        return None, [], []

    # If best_partial has the wrong project_type, force-rewrite it to match
    # the manifest's expected project_type so _sanitize_code_bundle accepts it.
    # The mode-isolation contract is enforced by _validate_batch_bundle for
    # the strict path; in salvage mode we trust the caller's project_type and
    # focus on retaining the file content the LLM produced.
    if best_partial is not None:
        try:
            best_partial = _model_copy_compat(
                best_partial, update={"project_type": project_type}
            )
        except Exception:
            pass

    sanitised = _sanitize_code_bundle(best_partial)
    if sanitised is None or not _bundle_has_files(sanitised):
        return None, sorted(planned_paths), []

    # Keep only files within the planned scope; drop hallucinated extras.
    salvaged_files: List[GeneratedFile] = []
    seen: Set[str] = set()
    for file in list(sanitised.files or []):
        normalised = _normalize_bundle_relpath(file.path)
        if not normalised or normalised in seen:
            continue
        if normalised not in planned_paths:
            continue
        salvaged_files.append(GeneratedFile(path=normalised, content=file.content))
        seen.add(normalised)

    if not salvaged_files:
        # Nothing in scope survived sanitisation — return None so the caller
        # can decide whether to fail or accept an empty placeholder bundle.
        return None, sorted(planned_paths), []

    salvaged_bundle = CodeBundle(project_type=project_type, files=salvaged_files)
    missing_paths = sorted(p for p in planned_paths if p not in seen)
    syntax_error_paths = [
        path for path, _msg in _py_syntax_error_paths_in_bundle(salvaged_bundle)
    ]
    return salvaged_bundle, missing_paths, syntax_error_paths


def _salvage_staged_codegen_bundle(
    current_bundle: Optional[CodeBundle],
    *,
    manifest: CodegenManifest,
    mode: str,
) -> Tuple[Optional[CodeBundle], List[str], List[str]]:
    """Salvage the cumulative staged-codegen bundle when strict finalize fails.

    Mirrors :func:`_salvage_codegen_batch_bundle` at the pipeline level: we
    keep every safe file the LLM produced across all batches, drop files
    outside the manifest's planned set, and report what's missing or broken
    so the caller can surface the degraded status.

    Returns ``(salvaged_bundle, missing_paths, syntax_error_paths)`` or
    ``(None, ..., ...)`` when nothing can be salvaged (caller must still
    raise in that case so we don't write empty code to disk).
    """
    expected_paths = {
        _normalize_bundle_relpath(plan.path)
        for plan in list(manifest.files or [])
        if plan.path and _normalize_bundle_relpath(plan.path)
    }
    expected_paths.discard("")

    expected_project_type = _project_type_for_mode(mode)
    if current_bundle is not None:
        try:
            current_bundle = _model_copy_compat(
                current_bundle, update={"project_type": expected_project_type}
            )
        except Exception:
            pass

    clean = _sanitize_code_bundle(current_bundle)
    if clean is None or not _bundle_has_files(clean):
        return None, sorted(expected_paths), []

    # Drop hallucinated extras but keep everything that was planned.
    salvaged_files: List[GeneratedFile] = []
    seen: Set[str] = set()
    for file in list(clean.files or []):
        normalised = _normalize_bundle_relpath(file.path)
        if not normalised or normalised in seen:
            continue
        if expected_paths and normalised not in expected_paths:
            # If we have a manifest, filter to planned paths.  If the manifest
            # somehow had zero planned paths (degenerate), keep everything.
            continue
        salvaged_files.append(GeneratedFile(path=normalised, content=file.content))
        seen.add(normalised)

    if not salvaged_files and expected_paths:
        # Manifest is non-empty but nothing matched after filtering — this
        # is a near-total scope mismatch.  Fall back to the un-filtered
        # sanitised bundle so the user still gets the LLM output to debug.
        for file in list(clean.files or []):
            normalised = _normalize_bundle_relpath(file.path)
            if not normalised or normalised in seen:
                continue
            salvaged_files.append(GeneratedFile(path=normalised, content=file.content))
            seen.add(normalised)

    if not salvaged_files:
        return None, sorted(expected_paths), []

    salvaged_bundle = CodeBundle(
        project_type=expected_project_type, files=salvaged_files
    )
    missing_paths = sorted(p for p in expected_paths if p not in seen)
    syntax_error_paths = [
        path for path, _msg in _py_syntax_error_paths_in_bundle(salvaged_bundle)
    ]
    return salvaged_bundle, missing_paths, syntax_error_paths


# ── Never-terminate fallback helpers ──────────────────────────────────────────
# Used when ``CODEGEN_FALLBACK_MANIFEST`` / ``CODEGEN_SKELETON_FALLBACK`` are on
# (default) to keep the pipeline producing output even when every prior recovery
# path has been exhausted.  See module-level documentation at the env-flag
# definitions for the policy boundaries.

_DEFAULT_ENTRYPOINTS_BY_PROJECT_TYPE: Dict[str, Tuple[str, ...]] = {
    "saas": ("main.py", "requirements.txt", "Dockerfile"),
    "quant": ("main.py", "requirements.txt"),
    "agent": ("main.py", "requirements.txt"),
    "scientist": ("main.py", "requirements.txt"),
}


def _default_entrypoints_for_project_type(project_type: str) -> List[str]:
    """Return reasonable entrypoint paths per project type for fallback bundles."""
    pt = (project_type or "").strip().lower()
    return list(_DEFAULT_ENTRYPOINTS_BY_PROJECT_TYPE.get(pt, ("main.py", "requirements.txt")))


def _synthesize_fallback_manifest(
    *,
    mode: str,
    analysis_report: Optional[AnalysisReport],
    user_problem: str,
) -> CodegenManifest:
    """Build a minimum-viable :class:`CodegenManifest` when LLM manifest synthesis fails.

    The synthesised manifest contains a single batch that lists the project
    type's default entrypoints plus ``README.md``.  Any additional file hints
    extracted from ``analysis_report.codegen_requirements`` are *not* added —
    requirements text is rarely a clean file list and forcing the LLM to
    target speculative paths in the next stage produces more harm than good.
    The downstream batch stage will produce whatever it can; if that batch
    also fails the skeleton-bundle fallback fills in the remaining files.
    """
    project_type = _project_type_for_mode(mode)
    entrypoints = _default_entrypoints_for_project_type(project_type)
    seen: Set[str] = set()
    file_paths: List[str] = []
    for raw in list(entrypoints) + ["README.md"]:
        normalised = _normalize_bundle_relpath(raw)
        if not normalised or normalised in seen:
            continue
        seen.add(normalised)
        file_paths.append(normalised)
    file_plans = [
        CodegenFilePlan(
            path=p,
            purpose="Auto-fallback manifest entrypoint (LLM manifest synthesis failed)",
            depends_on=[],
            must_contain=[],
        )
        for p in file_paths
    ]
    arch = ""
    if analysis_report is not None:
        arch = (str(getattr(analysis_report, "codegen_handoff_summary", "") or "")).strip()
        if not arch:
            arch = (str(getattr(analysis_report, "summary", "") or "")).strip()
    if not arch:
        arch = (
            "Fallback manifest synthesised because the LLM did not produce a "
            "parseable manifest.  Generate a minimal working entrypoint that "
            "demonstrates the user's intent."
        )
    arch = arch[:1500]
    return CodegenManifest(
        project_type=project_type,
        architecture_summary=arch,
        entrypoints=list(entrypoints),
        shared_constraints=[],
        files=file_plans,
        generation_batches=[
            CodegenBatchPlan(
                name="fallback_batch_1",
                objective=(
                    "Synthesised fallback batch — generate whatever the LLM "
                    "can for the planned entrypoints.  The skeleton fallback "
                    "will fill in any files this batch cannot produce."
                ),
                files=list(file_paths),
            )
        ],
    )


def _skeleton_stub_content_for(path: str) -> str:
    """Return safe stub content for a skeleton entrypoint based on its path."""
    p = (path or "").strip().lower()
    base = os.path.basename(p)
    if p.endswith(".py"):
        return (
            '"""Skeleton stub - codegen pipeline failed to produce real code.\n\n'
            'See README.md for failure details and recovery instructions.\n'
            '"""\n'
            "import sys\n\n\n"
            "def main() -> int:\n"
            '    print(\n'
            '        "[skeleton] Codegen failed to produce real code. "\n'
            '        "See README.md for details.",\n'
            "        file=sys.stderr,\n"
            "    )\n"
            "    return 1\n\n\n"
            'if __name__ == "__main__":\n'
            "    sys.exit(main())\n"
        )
    if base == "dockerfile" or p.endswith("dockerfile"):
        return (
            "# Skeleton fallback - codegen pipeline failed to produce a real Dockerfile.\n"
            "# Replace this stub with a real container definition.\n"
            "FROM python:3.11-slim\n"
            "WORKDIR /app\n"
            "COPY . /app\n"
            "RUN if [ -f requirements.txt ]; then pip install --no-cache-dir -r requirements.txt; fi\n"
            'CMD ["python", "main.py"]\n'
        )
    if base == "requirements.txt" or p.endswith("/requirements.txt"):
        return (
            "# Skeleton fallback - codegen pipeline failed.\n"
            "# Add real Python dependencies (one per line) before running.\n"
        )
    if p.endswith(".txt"):
        return "# Skeleton fallback - codegen pipeline failed.\n"
    if p.endswith(".md"):
        return (
            "# Skeleton Fallback\n\n"
            "Codegen pipeline failed to produce real content for this file.\n"
            "See the project root README.md for full failure details.\n"
        )
    if p.endswith(".json"):
        return (
            "{\n"
            '  "skeleton_fallback": true,\n'
            '  "note": "Codegen pipeline failed - replace with real content."\n'
            "}\n"
        )
    if p.endswith((".yaml", ".yml")):
        return (
            "skeleton_fallback: true\n"
            "note: Codegen pipeline failed - replace with real content.\n"
        )
    if p.endswith(".toml"):
        return (
            "[skeleton_fallback]\n"
            'enabled = true\n'
            'note = "Codegen pipeline failed - replace with real content."\n'
        )
    if p.endswith((".sh", ".bash")):
        return (
            "#!/usr/bin/env bash\n"
            "# Skeleton fallback - codegen pipeline failed.\n"
            'echo "[skeleton] Replace this stub with a real script." >&2\n'
            "exit 1\n"
        )
    if p.endswith(".env") or base == ".env" or base == ".env.example":
        return "# Skeleton fallback - add real env variables here.\n"
    return "# Skeleton fallback - codegen pipeline failed.\n"


def _synthesize_skeleton_bundle(
    *,
    manifest: Optional[CodegenManifest],
    mode: str,
    failure_reasons: List[str],
    user_problem: str,
) -> CodeBundle:
    """Emit a placeholder :class:`CodeBundle` when codegen produced zero salvageable files.

    The skeleton always contains:

    * ``README.md`` describing what was attempted, what failed, and what to
      do next.
    * Project-type entrypoint stubs that exit non-zero so an automated runner
      cannot mistake the skeleton for a real working build.
    * ``requirements.txt`` for Python-based project types.

    Used when ``CODEGEN_SKELETON_FALLBACK`` is on (default) and every other
    recovery path is exhausted.  Guarantees ``saved_projects/.../code/`` is
    never empty so the user always has somewhere to start manually fixing.
    """
    project_type = _project_type_for_mode(mode)
    entrypoints: List[str] = []
    if manifest is not None:
        entrypoints = [
            p for p in list(getattr(manifest, "entrypoints", []) or []) if p
        ]
        # Also include any planned files from the manifest as stubs.
        for plan in list(getattr(manifest, "files", []) or []):
            path = getattr(plan, "path", "")
            if path:
                entrypoints.append(path)
    if not entrypoints:
        entrypoints = _default_entrypoints_for_project_type(project_type)

    seen: Set[str] = set()
    files: List[GeneratedFile] = []

    reasons_block = "\n".join(f"- {r}" for r in failure_reasons if r) or (
        "- (no specific reason recorded — check the run snapshot for stage details)"
    )
    user_excerpt = (user_problem or "").strip()[:4000] or "(empty)"
    readme_text = (
        "# Codegen Skeleton (Auto-Fallback)\n\n"
        "This directory was emitted by the Crucible pipeline's skeleton "
        "fallback after every codegen attempt failed to produce salvageable "
        "files.\n\n"
        "## User problem\n\n"
        f"{user_excerpt}\n\n"
        "## Failure reasons\n\n"
        f"{reasons_block}\n\n"
        "## What to do next\n\n"
        "- Re-run with the same prompt — the LLM provider may have been transient.\n"
        "- Inspect the run snapshot under `runs/.../snapshots/` for per-stage details.\n"
        "- Lower `CODEGEN_BATCH_CONTEXT_MAX_CHARS` if the LLM hit context limits.\n"
        "- Set `CODEGEN_LENIENT_OUTPUT=0` to surface the strict-mode error.\n"
        "- Set `CODEGEN_SKELETON_FALLBACK=0` to abort instead of emitting this skeleton.\n\n"
        "## Skeleton contents\n\n"
        "The other files in this directory are placeholder stubs that exit "
        "non-zero so they cannot be mistaken for working code.  Replace them "
        "with real implementations or rerun the pipeline.\n"
    )
    files.append(GeneratedFile(path="README.md", content=readme_text))
    seen.add("README.md")

    for ep in entrypoints:
        normalised = _normalize_bundle_relpath(ep)
        if not normalised or normalised in seen:
            continue
        files.append(
            GeneratedFile(
                path=normalised,
                content=_skeleton_stub_content_for(normalised),
            )
        )
        seen.add(normalised)

    # Always include requirements.txt for Python-based project types.
    if (
        project_type in ("saas", "quant", "agent", "scientist")
        and "requirements.txt" not in seen
    ):
        files.append(
            GeneratedFile(
                path="requirements.txt",
                content=_skeleton_stub_content_for("requirements.txt"),
            )
        )
        seen.add("requirements.txt")

    return CodeBundle(project_type=project_type, files=files)


def _codegen_partial_checkpoint_path(run_id: str) -> Optional[str]:
    """Return a stable path for the partial-bundle checkpoint for this run."""
    if not run_id:
        return None
    try:
        root = globals().get("PROJECT_ROOT") or os.path.dirname(os.path.abspath(__file__))
        ckpt_dir = os.path.join(root, ".tmp", "codegen_checkpoints")
        os.makedirs(ckpt_dir, exist_ok=True)
        return os.path.join(ckpt_dir, f"{run_id}.json")
    except Exception:
        return None


def _save_codegen_partial_checkpoint(
    run_id: str,
    current_bundle: Optional[CodeBundle],
    completed_batches: List[Dict[str, Any]],
) -> None:
    """Persist the accumulated bundle after each successful batch."""
    if current_bundle is None or not run_id:
        return
    path = _codegen_partial_checkpoint_path(run_id)
    if path is None:
        return
    try:
        payload = {
            "run_id": run_id,
            "completed_batches": completed_batches,
            "partial_bundle": current_bundle.model_dump(),
        }
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False)
    except Exception:
        pass  # checkpoint write failure must never abort the pipeline


def _delete_codegen_partial_checkpoint(run_id: str) -> None:
    """Remove checkpoint after a successful full run."""
    if not run_id:
        return
    path = _codegen_partial_checkpoint_path(run_id)
    if path and os.path.exists(path):
        try:
            os.remove(path)
        except Exception:
            pass


def _identify_codegen_repair_targets(
    bundle: Optional[CodeBundle],
    *,
    manifest: CodegenManifest,
) -> Tuple[List[str], List[str]]:
    """Return ``(missing_planned_paths, syntax_error_paths)`` for *bundle*.

    Both lists are sorted, de-duplicated, and use the canonical relpath
    form.  ``missing_planned_paths`` contains the manifest-planned paths
    that are absent from *bundle* after sanitisation.  ``syntax_error_paths``
    contains existing files whose Python source fails ``compile()``.
    """
    expected: Set[str] = {
        _normalize_bundle_relpath(plan.path)
        for plan in list(manifest.files or [])
        if plan.path
    }
    expected.discard("")
    if bundle is None:
        return sorted(expected), []
    clean = _sanitize_code_bundle(bundle)
    actual: Set[str] = set()
    if clean is not None:
        for f in list(clean.files or []):
            actual.add(_normalize_bundle_relpath(f.path))
        actual.discard("")
    missing = sorted(p for p in expected if p not in actual)
    syntax_errors_seen: Set[str] = set()
    syntax_errors: List[str] = []
    for path, _msg in _py_syntax_error_paths_in_bundle(bundle):
        normalised = _normalize_bundle_relpath(path)
        if normalised and normalised not in syntax_errors_seen:
            syntax_errors_seen.add(normalised)
            syntax_errors.append(normalised)
    return missing, syntax_errors


def _run_codegen_repair_loop(
    *,
    user_problem: str,
    mode: str,
    language_hint: str,
    llm: Any,
    analysis_report: Optional[AnalysisReport],
    gate_decision: Optional[GateDecision],
    manifest: CodegenManifest,
    current_bundle: Optional[CodeBundle],
    run_snapshot: Optional[RunSnapshot],
    scope: str,
    max_attempts: int,
) -> Tuple[Optional[CodeBundle], int, List[Dict[str, Any]]]:
    """Pipeline-level repair pass — actively regenerate missing or broken
    files until everything is fixed, *max_attempts* is reached, or progress
    stalls (anti-thrash).

    Distinct from per-batch supplements (which target a single batch's
    scope): this loop crosses batch boundaries and can regenerate files
    that were completely lost when an entire batch was skipped due to an
    LLM error in the main batch loop.

    Returns ``(repaired_bundle, accumulated_prompt_chars, repair_history)``.
    The history records every attempt's outcome and any LLM exceptions for
    observability without ever propagating them.  Only
    :class:`_OperationCancelledError` is allowed to escape — it is the
    caller's explicit cancel signal, not a validation failure.
    """
    if max_attempts <= 0 or manifest is None:
        return current_bundle, 0, []

    accumulated_chars = 0
    history: List[Dict[str, Any]] = []
    last_problem_set: Optional[Set[str]] = None
    # Per-attempt scope cap.  ``_SUPPLEMENT_MAX_MISSING_FILES`` is tuned for
    # single-batch supplements (default 4); cross-batch repair can safely
    # target a larger chunk because the LLM has the full bundle as context.
    chunk_cap = max(1, _SUPPLEMENT_MAX_MISSING_FILES * 2)

    for attempt in range(1, max_attempts + 1):
        missing, syntax_errors = _identify_codegen_repair_targets(
            current_bundle, manifest=manifest
        )
        targets: List[str] = list(missing)
        for p in syntax_errors:
            if p not in targets:
                targets.append(p)
        if not targets:
            history.append(
                {
                    "attempt": attempt,
                    "outcome": "no_targets_remaining",
                    "fixed": [],
                    "still_broken": [],
                    "prompt_chars": 0,
                }
            )
            break

        current_problem_set: Set[str] = set(targets)
        # Anti-thrash: if the previous attempt produced exactly the same
        # problem set, the LLM is not making progress on these files.  Stop
        # wasting tokens — let the outer pipeline fall through to lenient
        # salvage / skeleton fallback.
        if last_problem_set is not None and current_problem_set == last_problem_set:
            history.append(
                {
                    "attempt": attempt,
                    "outcome": "no_progress_thrash_break",
                    "still_broken": sorted(current_problem_set),
                    "prompt_chars": 0,
                }
            )
            break

        chunk = targets[:chunk_cap]
        objective_lines = [
            f"PIPELINE-LEVEL REPAIR PASS {attempt}/{max_attempts}.",
            (
                "Regenerate ONLY the following files because they are either "
                "missing from the cumulative bundle or contain Python syntax "
                f"errors: {', '.join(chunk)}."
            ),
            (
                "Emit COMPLETE, syntactically valid source for each listed "
                "file.  Verify every parenthesis, bracket, comma, and string "
                "literal closes correctly."
            ),
            (
                "All other files are already in current_bundle as context — "
                "do NOT re-emit them."
            ),
        ]
        repair_plan = CodegenBatchPlan(
            name=f"repair_pass_{attempt}",
            objective=" ".join(objective_lines),
            files=chunk,
        )

        LOGGER.info(
            "Codegen repair-loop attempt %d/%d targeting %d file(s): %s",
            attempt,
            max_attempts,
            len(chunk),
            ", ".join(chunk[:8]) + ("..." if len(chunk) > 8 else ""),
        )

        # Synthetic batch_index avoids snapshot-key collision with the
        # numeric indices used by the main batch loop while still flowing
        # through the same instrumented stage helper.
        synthetic_batch_index = 9000 + attempt

        try:
            repair_bundle, repair_chars = _run_codegen_batch_stage(
                user_problem,
                mode=mode,
                language_hint=language_hint,
                llm=llm,
                analysis_report=analysis_report,
                gate_decision=gate_decision,
                manifest=manifest,
                batch_plan=repair_plan,
                current_bundle=current_bundle,
                run_snapshot=run_snapshot,
                batch_index=synthetic_batch_index,
                scope=scope,
            )
            accumulated_chars += max(0, int(repair_chars or 0))
        except _OperationCancelledError:
            raise
        except Exception as repair_exc:
            accumulated_chars += int(
                getattr(repair_exc, "_staged_codegen_prompt_chars", 0) or 0
            )
            history.append(
                {
                    "attempt": attempt,
                    "outcome": "exception",
                    "targets": list(chunk),
                    "error_type": type(repair_exc).__name__,
                    "error": str(repair_exc)[:300],
                    "prompt_chars": int(
                        getattr(repair_exc, "_staged_codegen_prompt_chars", 0) or 0
                    ),
                }
            )
            LOGGER.warning(
                "Codegen repair-loop attempt %d/%d raised (%s: %s); "
                "continuing to next attempt.",
                attempt,
                max_attempts,
                type(repair_exc).__name__,
                str(repair_exc)[:200],
            )
            # Intentionally do NOT update ``last_problem_set`` on the
            # exception path: the bundle is unchanged, and clobbering the
            # tracker here would falsely trigger anti-thrash on the very
            # next attempt even though the previous attempt produced no
            # output to compare against.
            continue

        merged = _merge_code_bundles(
            current_bundle, repair_bundle, project_type=manifest.project_type
        )
        new_missing, new_syntax = _identify_codegen_repair_targets(
            merged, manifest=manifest
        )
        new_problem_set: Set[str] = set(new_missing) | set(new_syntax)
        fixed = sorted(current_problem_set - new_problem_set)
        still_broken = sorted(current_problem_set & new_problem_set)

        history.append(
            {
                "attempt": attempt,
                "outcome": "merged",
                "targets": list(chunk),
                "fixed": fixed,
                "still_broken": still_broken,
                "prompt_chars": int(repair_chars or 0),
            }
        )
        LOGGER.info(
            "Codegen repair-loop attempt %d/%d: fixed %d file(s), "
            "%d still broken across pipeline.",
            attempt,
            max_attempts,
            len(fixed),
            len(still_broken),
        )

        current_bundle = merged
        last_problem_set = current_problem_set

        if not new_problem_set:
            break  # Everything fixed across the entire bundle.

    return current_bundle, accumulated_chars, history


def _run_staged_codegen_pipeline(
    user_problem: str,
    *,
    mode: str,
    language_hint: str,
    llm: Any,
    analysis_report: Optional[AnalysisReport],
    gate_decision: Optional[GateDecision],
    run_snapshot: Optional[RunSnapshot],
    scope: str = "mvp",
) -> Tuple[Dict[str, Any], Optional[CodeBundle]]:
    """Execute the staged codegen pipeline with the never-terminate guarantee.

    The pipeline is split into three guarded layers:

    1. **Manifest stage** — wrapped so that any exception (LLM API error,
       parse failure, etc.) triggers ``_synthesize_fallback_manifest`` when
       ``CODEGEN_FALLBACK_MANIFEST`` is on.
    2. **Batch loop** — each batch is wrapped so that an exception raised by
       :func:`_run_codegen_batch_stage` (after every supplement and lenient
       salvage attempt has been exhausted) is logged, recorded in
       ``batch_failures``, and the loop continues to the next batch when
       ``CODEGEN_BATCH_SKIP_ON_ERROR`` is on.
    3. **Finalize / skeleton fallback** — if the cumulative bundle still
       cannot pass strict finalize *or* lenient salvage, an explicit
       skeleton bundle (README + entrypoint stubs) is emitted when
       ``CODEGEN_SKELETON_FALLBACK`` is on so the user always receives a
       directory under ``saved_projects/.../code/`` to inspect or edit.

    The only condition that still propagates is :class:`_OperationCancelledError`
    (explicit user-driven cancellation), which is not a validation failure
    and must reach the outer harness immediately.
    """
    pipeline_prompt_chars = 0
    run_id: str = (getattr(run_snapshot, "run_id", None) or "") if run_snapshot else ""
    current_bundle: Optional[CodeBundle] = None
    manifest: Optional[CodegenManifest] = None
    batch_failures: List[Dict[str, Any]] = []
    executed_batches: List[Dict[str, Any]] = []
    manifest_attempt_history: List[Dict[str, Any]] = []
    batch_attempt_history: List[Dict[str, Any]] = []
    repair_history: List[Dict[str, Any]] = []
    try:
        # ── Stage 1: manifest (with retry, then exception fallback) ───────
        manifest_attempts = max(1, int(CODEGEN_MANIFEST_RETRY_MAX_ATTEMPTS or 1))
        last_manifest_exc: Optional[BaseException] = None
        for attempt in range(1, manifest_attempts + 1):
            try:
                manifest, manifest_prompt_chars = _run_codegen_manifest_stage(
                    user_problem,
                    mode=mode,
                    language_hint=language_hint,
                    llm=llm,
                    analysis_report=analysis_report,
                    gate_decision=gate_decision,
                    run_snapshot=run_snapshot,
                    scope=scope,
                )
                pipeline_prompt_chars += max(0, int(manifest_prompt_chars or 0))
                manifest_attempt_history.append(
                    {"attempt": attempt, "outcome": "success"}
                )
                last_manifest_exc = None
                break
            except _OperationCancelledError:
                raise
            except Exception as manifest_exc:
                last_manifest_exc = manifest_exc
                pipeline_prompt_chars += int(
                    getattr(manifest_exc, "_staged_codegen_prompt_chars", 0) or 0
                )
                manifest_attempt_history.append(
                    {
                        "attempt": attempt,
                        "outcome": "exception",
                        "error_type": type(manifest_exc).__name__,
                        "error": str(manifest_exc)[:300],
                    }
                )
                LOGGER.warning(
                    "Codegen manifest attempt %d/%d failed (%s: %s); %s.",
                    attempt,
                    manifest_attempts,
                    type(manifest_exc).__name__,
                    str(manifest_exc)[:200],
                    "retrying" if attempt < manifest_attempts else "exhausted retries",
                )
        if manifest is None:
            # Every retry attempt raised — fall back to synthesis (or
            # propagate when CODEGEN_FALLBACK_MANIFEST is off).
            if not CODEGEN_FALLBACK_MANIFEST:
                if last_manifest_exc is not None:
                    raise last_manifest_exc
                raise ValueError(
                    "Codegen manifest stage exhausted retries with no exception."
                )
            LOGGER.warning(
                "Codegen manifest stage exhausted %d retry attempt(s); "
                "synthesising fallback manifest.  Set "
                "CODEGEN_FALLBACK_MANIFEST=0 to abort instead.",
                manifest_attempts,
            )
            manifest = _synthesize_fallback_manifest(
                mode=mode,
                analysis_report=analysis_report,
                user_problem=user_problem,
            )
            _snapshot_record_stage(
                run_snapshot,
                stage="codegen.manifest",
                status="completed",
                failure_type=FailureType.NONE,
                notes=(
                    "synthesised_fallback_after_retries attempts="
                    + str(manifest_attempts)
                ),
            )

        # ── Stage 2: batch loop (per-batch retry, then skip-on-error) ─────
        batch_attempts_cap = max(1, int(CODEGEN_BATCH_RETRY_MAX_ATTEMPTS or 1))
        for batch_index, batch_plan in enumerate(
            list(manifest.generation_batches or []), start=1
        ):
            batch_bundle: Optional[CodeBundle] = None
            batch_prompt_chars = 0
            last_batch_exc: Optional[BaseException] = None
            for batch_attempt in range(1, batch_attempts_cap + 1):
                try:
                    batch_bundle, batch_prompt_chars = _run_codegen_batch_stage(
                        user_problem,
                        mode=mode,
                        language_hint=language_hint,
                        llm=llm,
                        analysis_report=analysis_report,
                        gate_decision=gate_decision,
                        manifest=manifest,
                        batch_plan=batch_plan,
                        current_bundle=current_bundle,
                        run_snapshot=run_snapshot,
                        batch_index=batch_index,
                        scope=scope,
                    )
                    batch_attempt_history.append(
                        {
                            "batch_index": batch_index,
                            "attempt": batch_attempt,
                            "outcome": "success",
                        }
                    )
                    last_batch_exc = None
                    break
                except _OperationCancelledError:
                    raise
                except Exception as batch_exc:
                    last_batch_exc = batch_exc
                    pipeline_prompt_chars += int(
                        getattr(batch_exc, "_staged_codegen_prompt_chars", 0) or 0
                    )
                    batch_attempt_history.append(
                        {
                            "batch_index": batch_index,
                            "attempt": batch_attempt,
                            "outcome": "exception",
                            "error_type": type(batch_exc).__name__,
                            "error": str(batch_exc)[:300],
                        }
                    )
                    LOGGER.warning(
                        "Codegen batch_%d attempt %d/%d failed (%s: %s); %s.",
                        batch_index,
                        batch_attempt,
                        batch_attempts_cap,
                        type(batch_exc).__name__,
                        str(batch_exc)[:200],
                        "retrying"
                        if batch_attempt < batch_attempts_cap
                        else "exhausted retries",
                    )

            if batch_bundle is None:
                # Every retry attempt raised for this batch.
                if not CODEGEN_BATCH_SKIP_ON_ERROR:
                    if last_batch_exc is not None:
                        raise last_batch_exc
                    raise ValueError(
                        f"Codegen batch_{batch_index} exhausted retries with no exception."
                    )
                LOGGER.warning(
                    "Codegen batch_%d exhausted %d retry attempt(s); "
                    "recording failure and continuing.  Pipeline-level "
                    "repair loop will attempt regeneration after the main "
                    "batch loop finishes.",
                    batch_index,
                    batch_attempts_cap,
                )
                batch_failures.append(
                    {
                        "batch_index": batch_index,
                        "name": batch_plan.name,
                        "error_type": (
                            type(last_batch_exc).__name__
                            if last_batch_exc is not None
                            else "Unknown"
                        ),
                        "error": (
                            str(last_batch_exc)[:500] if last_batch_exc is not None else ""
                        ),
                        "missing_files": list(batch_plan.files or []),
                        "attempts": batch_attempts_cap,
                    }
                )
                _snapshot_record_stage(
                    run_snapshot,
                    stage=f"codegen.batch_{batch_index}",
                    status="failed",
                    failure_type=FailureType.JSON_INVALID,
                    notes=(
                        "skipped_on_error_after_retries attempts="
                        + str(batch_attempts_cap)
                        + " "
                        + (
                            type(last_batch_exc).__name__
                            if last_batch_exc is not None
                            else "Unknown"
                        )
                    ),
                )
                continue
            pipeline_prompt_chars += max(0, int(batch_prompt_chars or 0))
            current_bundle = _merge_code_bundles(
                current_bundle,
                batch_bundle,
                project_type=manifest.project_type,
            )
            executed_batches.append(
                {
                    "batch_index": batch_index,
                    "name": batch_plan.name,
                    "files": list(batch_plan.files or []),
                }
            )
            # Persist partial output so a later batch failure does not lose completed work.
            _save_codegen_partial_checkpoint(run_id, current_bundle, executed_batches)

        # ── Stage 2.5: pipeline-level repair loop ─────────────────────────
        # Now that the main batch loop is finished, identify any missing
        # planned files (e.g. lost when a batch was skipped) and any files
        # with Python syntax errors that the per-batch supplements could
        # not fix.  Run cross-batch repair attempts until everything is
        # fixed, max_attempts is reached, or progress stalls.
        if (
            int(CODEGEN_REPAIR_LOOP_MAX_ATTEMPTS or 0) > 0
            and manifest is not None
        ):
            pre_repair_missing, pre_repair_syntax = _identify_codegen_repair_targets(
                current_bundle, manifest=manifest
            )
            if pre_repair_missing or pre_repair_syntax:
                LOGGER.info(
                    "Codegen pipeline running repair loop: %d missing, "
                    "%d files with syntax errors.",
                    len(pre_repair_missing),
                    len(pre_repair_syntax),
                )
                repaired_bundle, repair_chars, repair_history = _run_codegen_repair_loop(
                    user_problem=user_problem,
                    mode=mode,
                    language_hint=language_hint,
                    llm=llm,
                    analysis_report=analysis_report,
                    gate_decision=gate_decision,
                    manifest=manifest,
                    current_bundle=current_bundle,
                    run_snapshot=run_snapshot,
                    scope=scope,
                    max_attempts=int(CODEGEN_REPAIR_LOOP_MAX_ATTEMPTS),
                )
                pipeline_prompt_chars += max(0, int(repair_chars or 0))
                if repaired_bundle is not None:
                    current_bundle = repaired_bundle
                if repair_history:
                    _snapshot_record_stage(
                        run_snapshot,
                        stage="codegen.repair_loop",
                        status="completed",
                        failure_type=FailureType.NONE,
                        notes=(
                            "attempts="
                            + str(len(repair_history))
                            + " final_outcome="
                            + str(repair_history[-1].get("outcome", "?"))
                        ),
                    )

        # ── Stage 3: finalize → lenient salvage → skeleton fallback ───────
        final_bundle, final_failure_note = _finalize_staged_codegen_bundle(
            current_bundle, manifest=manifest, mode=mode
        )
        degraded_metadata: Dict[str, Any] = {}
        if final_bundle is None:
            # Lenient salvage: keep whatever batches produced.
            if CODEGEN_LENIENT_OUTPUT:
                salvaged, salvage_missing, salvage_syntax_err = (
                    _salvage_staged_codegen_bundle(
                        current_bundle, manifest=manifest, mode=mode
                    )
                )
                if salvaged is not None and _bundle_has_files(salvaged):
                    final_bundle = salvaged
                    degraded_metadata = {
                        "degraded": True,
                        "degraded_reason": (
                            final_failure_note
                            or "Staged codegen finalize failed."
                        ),
                        "missing_planned_files": salvage_missing,
                        "syntax_error_files": salvage_syntax_err,
                    }
                    LOGGER.warning(
                        "Staged codegen pipeline salvaged in lenient-output "
                        "mode: kept %d file(s); missing=%s; syntax_errors=%s; "
                        "original_failure=%s.  Set CODEGEN_LENIENT_OUTPUT=0 "
                        "to abort instead of salvaging.",
                        len(salvaged.files or []),
                        ", ".join(salvage_missing) if salvage_missing else "(none)",
                        ", ".join(salvage_syntax_err)
                        if salvage_syntax_err
                        else "(none)",
                        final_failure_note,
                    )
        # If lenient salvage still produced nothing, emit a skeleton bundle
        # so the user always has *something* to inspect.
        if final_bundle is None or not _bundle_has_files(final_bundle):
            if CODEGEN_SKELETON_FALLBACK:
                failure_reasons: List[str] = []
                if final_failure_note:
                    failure_reasons.append(f"Finalize: {final_failure_note}")
                for bf in batch_failures:
                    failure_reasons.append(
                        f"Batch {bf.get('batch_index')} "
                        f"({bf.get('name')}): "
                        f"{bf.get('error_type')}: {bf.get('error')}"
                    )
                if not executed_batches and not batch_failures:
                    failure_reasons.append(
                        "No batches executed (manifest had zero generation_batches)."
                    )
                final_bundle = _synthesize_skeleton_bundle(
                    manifest=manifest,
                    mode=mode,
                    failure_reasons=failure_reasons,
                    user_problem=user_problem,
                )
                degraded_metadata = {
                    "degraded": True,
                    "degraded_reason": "skeleton_fallback",
                    "skeleton_fallback": True,
                    "missing_planned_files": [
                        _normalize_bundle_relpath(plan.path)
                        for plan in list(manifest.files or [])
                        if plan.path
                    ],
                    "syntax_error_files": [],
                    "finalize_failure_note": final_failure_note or "",
                }
                LOGGER.warning(
                    "Staged codegen pipeline emitted skeleton fallback "
                    "(%d file(s)); set CODEGEN_SKELETON_FALLBACK=0 to abort "
                    "instead.  Reasons: %s",
                    len(list(final_bundle.files or [])),
                    "; ".join(failure_reasons) if failure_reasons else "(none)",
                )
                _snapshot_record_stage(
                    run_snapshot,
                    stage="codegen.skeleton_fallback",
                    status="completed",
                    failure_type=FailureType.NONE,
                    notes=(
                        "skeleton_files="
                        + str(len(list(final_bundle.files or [])))
                    ),
                )
            else:
                exc = ValueError(
                    final_failure_note
                    or "Staged codegen bundle finalization failed."
                )
                setattr(exc, "_staged_codegen_prompt_chars", 0)
                raise exc

        # All paths succeeded (real, salvaged, or skeleton).  Cleanup.
        _delete_codegen_partial_checkpoint(run_id)
        result_payload: Dict[str, Any] = {
            "pipeline": "staged_codegen",
            "manifest_project_type": manifest.project_type,
            "batch_count": len(executed_batches),
            "batches": executed_batches,
            "prompt_total_chars": pipeline_prompt_chars,
        }
        if batch_failures:
            result_payload["batch_failures"] = list(batch_failures)
        if manifest_attempt_history:
            result_payload["manifest_attempts"] = list(manifest_attempt_history)
        if batch_attempt_history:
            result_payload["batch_attempts"] = list(batch_attempt_history)
        if repair_history:
            result_payload["repair_history"] = list(repair_history)
        if degraded_metadata:
            result_payload.update(degraded_metadata)
        return (result_payload, final_bundle)
    except _OperationCancelledError:
        raise
    except Exception as exc:
        stage_prompt_chars = int(getattr(exc, "_staged_codegen_prompt_chars", 0) or 0)
        setattr(exc, "_staged_codegen_prompt_chars", pipeline_prompt_chars + stage_prompt_chars)
        # Attach checkpoint path to exception so the caller can surface it.
        if run_id and current_bundle is not None:
            ckpt = _codegen_partial_checkpoint_path(run_id)
            if ckpt and os.path.exists(ckpt):
                setattr(exc, "_codegen_partial_checkpoint_path", ckpt)
        raise


def _record_codegen_usage_slice(
    *,
    stage: str,
    agent_name: str,
    usage_records: List[OpenRouterUsageData],
    success: bool,
    outcome: str,
    fallback_input_tokens: int,
    fallback_output_tokens: int,
) -> None:
    if usage_records:
        merged_model_id = ""
        merged_cost_source = "estimated"
        total_input_tokens = 0
        total_output_tokens = 0
        total_cached_tokens = 0
        total_reasoning_tokens = 0
        total_input_cost_usd = 0.0
        total_output_cost_usd = 0.0
        total_cache_cost_usd = 0.0
        total_cost_usd = 0.0
        cache_hit = False

        for usage in usage_records:
            total_input_tokens += int(getattr(usage, "input_tokens", 0) or 0)
            total_output_tokens += int(getattr(usage, "output_tokens", 0) or 0)
            total_cached_tokens += int(getattr(usage, "cached_tokens", 0) or 0)
            total_reasoning_tokens += int(getattr(usage, "reasoning_tokens", 0) or 0)
            total_input_cost_usd += float(getattr(usage, "input_cost_usd", 0.0) or 0.0)
            total_output_cost_usd += float(getattr(usage, "output_cost_usd", 0.0) or 0.0)
            total_cache_cost_usd += float(getattr(usage, "cache_cost_usd", 0.0) or 0.0)
            total_cost_usd += float(getattr(usage, "total_cost_usd", 0.0) or 0.0)
            cache_hit = cache_hit or bool(getattr(usage, "cached_tokens", 0) or 0)
            merged_model_id = _merge_usage_model_ids(
                merged_model_id, str(getattr(usage, "model_id", "") or "")
            )
            merged_cost_source = _merge_usage_cost_source(
                merged_cost_source,
                str(getattr(usage, "cost_source", "estimated") or "estimated"),
            )

        get_cost_accountant().record(
            agent_name=agent_name,
            stage=stage,
            input_tokens=total_input_tokens,
            output_tokens=total_output_tokens,
            success=success,
            cache_hit=cache_hit,
            outcome=outcome,
            cached_tokens=total_cached_tokens,
            reasoning_tokens=total_reasoning_tokens,
            input_cost_usd=total_input_cost_usd,
            output_cost_usd=total_output_cost_usd,
            cache_cost_usd=total_cache_cost_usd,
            total_cost_usd=total_cost_usd,
            model_id=merged_model_id,
            cost_source=merged_cost_source,
        )
        clear_openrouter_usage()
        return

    _record_cost(
        stage=stage,
        agent_name=agent_name,
        input_tokens=fallback_input_tokens,
        output_tokens=fallback_output_tokens,
        success=success,
        outcome=outcome,
        use_openrouter_usage=False,
        clear_usage_after_record=False,
    )


def build_codegen_crew(
    user_problem: str,
    mode: str,
    language_hint: str,
    llm: Any,
    analysis_report: Optional[AnalysisReport],
    gate_decision: Optional[GateDecision],
    scope: str = "mvp",
) -> Crew:
    mode_config = _get_mode_config(mode)
    project_type = _project_type_for_mode(mode)
    approved_context = build_budgeted_codegen_context(
        gate_decision,
        analysis_report,
        max_chars=CODEGEN_CONTEXT_MAX_CHARS,
        include_analyst_findings=True,
    )
    mode_rule_text = "\n".join(_resolved_codegen_rule_lines(mode_config, gate_decision, scope=scope))
    # Validation-scope gate overrides user scope for agent prompts to keep goal/rules consistent.
    effective_prompt_scope = "mvp" if _gate_is_validation_scope(gate_decision) else scope
    agent_goal, agent_backstory, task_header = _codegen_scope_prompts(effective_prompt_scope)
    agent_spec = AgentSpec(
        name="codegen",
        role="CodeGen",
        goal=agent_goal,
        backstory=agent_backstory,
        output_schema_name="CodeBundle",
        parallel_safe=False,
        retry_policy=RetryPolicy(max_attempts=20, backoff_seconds=2.0, retry_on_json_fail=True),
        version="v1.0.0",
        behavior_contract=f"Generate {effective_prompt_scope}-scope runnable code from approved gate context only.",
    )
    task_spec = TaskSpec(
        name="codegen",
        description_template=(
            f"{task_header}\n"
            "Mode: {mode_name}\n"
            "project_type must be '{project_type}'.\n"
            "Language hint: {language_hint}\n\n"
            "User problem:\n{user_problem}\n\n"
            "Approved context:\n{approved_context}\n\n"
            "Rules:\n"
            "{mode_rule_text}"
        ),
        agent_name="codegen",
        expected_output="CodeBundle JSON only.",
        max_input_chars=CODEGEN_PRIMARY_MAX_INPUT_CHARS,
    )
    template_vars = {
        "mode_name": mode_config.name,
        "project_type": project_type,
        "language_hint": language_hint,
        "user_problem": limit_text(user_problem, 4000),
        "approved_context": approved_context,
        "mode_rule_text": mode_rule_text,
    }
    return _build_codegen_single_task_crew(
        crew_name="codegen_crew",
        llm=llm,
        agent_spec=agent_spec,
        task_spec=task_spec,
        template_vars=template_vars,
        verbose=True,
    )


def _build_codegen_timeout_recovery_crew(
    user_problem: str,
    *,
    mode: str,
    language_hint: str,
    llm: Any,
    analysis_report: Optional[AnalysisReport],
    gate_decision: Optional[GateDecision],
    scope: str = "mvp",
) -> Crew:
    mode_config = _get_mode_config(mode)
    project_type = _project_type_for_mode(mode)
    approved_context = build_budgeted_codegen_context(
        gate_decision,
        analysis_report,
        max_chars=max(2500, CODEGEN_CONTEXT_MAX_CHARS // 2),
        include_analyst_findings=False,
    )
    mode_rule_text = "\n".join(_resolved_codegen_rule_lines(mode_config, gate_decision, scope=scope))
    # Validation-scope gate overrides user scope for recovery agent prompts too.
    effective_prompt_scope = "mvp" if _gate_is_validation_scope(gate_decision) else str(scope or "mvp").strip().lower()
    if effective_prompt_scope in ("full", "production"):
        recovery_goal = (
            f"Recover from a transient code generation failure and emit a valid "
            f"{effective_prompt_scope}-scope CodeBundle JSON response."
        )
        recovery_backstory = (
            "You are handling a retry after an upstream timeout.\n"
            "- Return only one valid CodeBundle JSON object.\n"
            f"- Attempt a {effective_prompt_scope}-scope implementation following the rules; "
            "if context is too limited, produce the most complete runnable artifact set you can.\n"
            "- Do not silently downgrade to a minimal scaffold without attempting the full scope."
        )
        recovery_label = effective_prompt_scope.upper()
    else:
        recovery_goal = (
            "Recover from a transient code generation failure and emit a minimal valid CodeBundle JSON response."
        )
        recovery_backstory = (
            "You are handling a retry after an upstream timeout.\n"
            "- Return only one valid CodeBundle JSON object.\n"
            "- Prefer the smallest runnable artifact set that still satisfies the approved gate context.\n"
            "- If context is incomplete, choose a conservative minimal scaffold over prose."
        )
        recovery_label = "MVP"
    generator = Agent(
        role="CodeGen Recovery",
        goal=recovery_goal,
        backstory=recovery_backstory,
        allow_delegation=False,
        verbose=False,
        llm=llm,
    )
    task = Task(
        description=(
            f"Recover runnable {recovery_label} code generation after a transient timeout.\n"
            f"Mode: {mode_config.name}\n"
            f"project_type must be '{project_type}'.\n"
            f"Language hint: {language_hint}\n\n"
            "Return exactly one valid CodeBundle JSON object.\n"
            "No markdown, no commentary, no code fences.\n\n"
            "User problem:\n"
            f"{limit_text(user_problem, 3000)}\n\n"
            "Approved context (reduced for timeout recovery):\n"
            f"{approved_context}\n\n"
            "Rules:\n"
            f"{mode_rule_text}"
        ),
        agent=generator,
        expected_output="CodeBundle JSON only.",
    )
    crew = Crew(
        agents=[generator],
        tasks=[task],
        process=Process.sequential,
        verbose=False,
    )
    setattr(
        crew,
        "_retry_policy",
        RetryPolicy(max_attempts=6, backoff_seconds=3.0, retry_on_json_fail=True),
    )
    setattr(crew, "_crew_name", "codegen_crew_fallback")
    return crew


def run_codegen_stage(
    user_problem: str,
    *,
    mode: str,
    language_hint: str,
    llm: Any,
    analysis_report: Optional[AnalysisReport],
    gate_decision: Optional[GateDecision],
    run_snapshot: Optional[RunSnapshot] = None,
    scope: str = "mvp",
) -> Tuple[Any, Optional[CodeBundle]]:
    if not CODEGEN_STAGED_ENABLED:
        return _LEGACY_RUN_CODEGEN_STAGE(
            user_problem,
            mode=mode,
            language_hint=language_hint,
            llm=llm,
            analysis_report=analysis_report,
            gate_decision=gate_decision,
            run_snapshot=run_snapshot,
            scope=scope,
        )

    bundle_failure_reason = "parse_failed"
    bundle_failure_note = "Staged CodeBundle generation failed."
    usage_record_start = len(get_usage_records())
    staged_prompt_chars = 0
    _snapshot_record_stage(
        run_snapshot, stage="codegen_crew.kickoff", status="started"
    )
    _cost_trace("codegen_crew.kickoff", mode=mode)
    try:
        log_event(
            LOGGER,
            20,
            "codegen_kickoff_start",
            "Starting staged codegen pipeline.",
            mode=mode,
        )
        result, bundle = _run_staged_codegen_pipeline(
            user_problem,
            mode=mode,
            language_hint=language_hint,
            llm=llm,
            analysis_report=analysis_report,
            gate_decision=gate_decision,
            run_snapshot=run_snapshot,
            scope=scope,
        )
    except _OperationCancelledError:
        raise
    except Exception as e:
        log_exception(
            LOGGER,
            "codegen_kickoff_failed",
            "Staged CodeGen pipeline failed.",
            mode=mode,
        )
        print(f"[Error] CodeGen stage failed: {e}")
        _ckpt_path = getattr(e, "_codegen_partial_checkpoint_path", None)
        if _ckpt_path:
            print(f"[Info] Partial codegen output (completed batches) saved to: {_ckpt_path}")
        staged_prompt_chars = int(getattr(e, "_staged_codegen_prompt_chars", 0) or 0)
        try:
            usage_records = get_usage_records()[usage_record_start:]
            _record_codegen_usage_slice(
                stage="codegen_crew.kickoff",
                agent_name="codegen",
                usage_records=usage_records,
                success=False,
                outcome="execution_error",
                fallback_input_tokens=max(0, staged_prompt_chars // 3)
                if staged_prompt_chars > 0
                else len(user_problem) // 3,
                fallback_output_tokens=0,
            )
        except Exception:
            pass
        _snapshot_record_stage(
            run_snapshot,
            stage="codegen_crew.kickoff",
            status="failed",
            failure_type=_classify_runtime_exception_failure(e),
            notes=str(e),
            extra={"prompt_chars": staged_prompt_chars} if staged_prompt_chars > 0 else None,
        )
        return None, None

    staged_prompt_chars = int((result or {}).get("prompt_total_chars", 0) or 0)
    mismatch_reason = _code_bundle_mode_mismatch_reason(bundle, mode)
    if mismatch_reason:
        print(f"[Warn] {mismatch_reason}")
        bundle = None
        bundle_failure_reason = "mode_mismatch"
        bundle_failure_note = mismatch_reason

    try:
        result_text = json.dumps(result, ensure_ascii=False, sort_keys=True, default=str)
        usage_records = get_usage_records()[usage_record_start:]
        _record_codegen_usage_slice(
            stage="codegen_crew.kickoff",
            agent_name="codegen",
            usage_records=usage_records,
            success=bundle is not None,
            outcome="success" if bundle is not None else bundle_failure_reason,
            fallback_input_tokens=max(0, staged_prompt_chars // 3)
            if staged_prompt_chars > 0
            else len(user_problem) // 3,
            fallback_output_tokens=len(result_text) // 3,
        )
    except Exception:
        pass

    if bundle is None:
        _snapshot_record_stage(
            run_snapshot,
            stage="codegen_crew.kickoff",
            status="failed",
            failure_type=FailureType.JSON_INVALID,
            notes=bundle_failure_note,
            extra={"prompt_chars": staged_prompt_chars} if staged_prompt_chars > 0 else None,
        )
    else:
        _snapshot_record_stage(
            run_snapshot,
            stage="codegen_crew.kickoff",
            status="completed",
            failure_type=FailureType.NONE,
            notes=f"staged_batches={result.get('batch_count', 0)}",
            extra={"prompt_chars": staged_prompt_chars} if staged_prompt_chars > 0 else None,
        )
    return result, bundle


# END MANUAL OUTPUT SAVE OVERRIDES

# =========================
# CodeGen Auto-Optimize (autoresearch-style iterative refinement)
# =========================


class CritiqueBundle(BaseModel):
    """Critic review of a CodeBundle. Output of the codegen_critic agent."""

    score: float = Field(
        ...,
        description="Quality score 0.0 (unusable) to 1.0 (production-ready MVP).",
    )
    issues: List[str] = Field(
        default_factory=list,
        description="Concrete, actionable issues found in the generated code.",
    )
    suggestions: List[str] = Field(
        default_factory=list,
        description="Concrete improvement suggestions for the next generation round.",
    )
    summary: str = Field("", description="1-2 sentence overall quality assessment.")


def _format_code_bundle_for_critic(bundle: "CodeBundle", max_chars: int = 12000) -> str:
    """Serialise a CodeBundle into a char-limited text block for the critic prompt."""
    parts: List[str] = [
        f"project_type: {bundle.project_type}",
        f"file_count: {len(bundle.files)}",
    ]
    total_chars = 0
    for gf in bundle.files:
        header = f"\n--- {gf.path} ---\n"
        budget = max(0, max_chars - total_chars - len(header) - 40)
        if budget <= 0:
            remaining = len(bundle.files) - bundle.files.index(gf)
            parts.append(
                f"\n--- {gf.path} --- (omitted: char budget exhausted; "
                f"{remaining} file(s) not shown)"
            )
            break
        content = gf.content[:budget]
        if len(gf.content) > budget:
            content += "\n...[truncated]..."
        parts.append(header + content)
        total_chars += len(header) + len(content)
    return "\n".join(parts)


def _build_codegen_critique_crew(
    bundle: "CodeBundle",
    *,
    user_problem: str,
    mode: str,
    language_hint: str,
    llm: Any,
) -> "Crew":
    """Build a one-shot critic crew that reviews a CodeBundle and returns CritiqueBundle JSON."""
    code_text = _format_code_bundle_for_critic(bundle, max_chars=12000)
    problem_text = limit_text(user_problem, 2000)

    agent_spec = AgentSpec(
        name="codegen_critic",
        role="CodeGen Critic",
        goal=(
            "Review generated code for correctness, completeness, and alignment with "
            "requirements. Output a CritiqueBundle JSON."
        ),
        backstory=(
            "You are a strict senior code reviewer.\n"
            "- Identify concrete, actionable issues only; no generic criticism.\n"
            "- Score 0.0 (entirely unusable) to 1.0 (clean, runnable MVP).\n"
            "- Output CritiqueBundle JSON only — no prose, no markdown fences."
        ),
        output_schema_name="CritiqueBundle",
        parallel_safe=False,
        retry_policy=RetryPolicy(max_attempts=6, backoff_seconds=2.0, retry_on_json_fail=True),
        version="v1.0.0",
        behavior_contract="Output CritiqueBundle JSON only.",
    )
    json_schema_example = (
        '{\n'
        '  "score": <float 0.0-1.0>,\n'
        '  "issues": ["<specific issue 1>", ...],\n'
        '  "suggestions": ["<concrete suggestion 1>", ...],\n'
        '  "summary": "<1-2 sentence overall assessment>"\n'
        '}'
    )
    task_description = (
        "Review the generated code below and output exactly one CritiqueBundle JSON object.\n\n"
        f"Mode: {mode}\n"
        f"Language hint: {language_hint}\n\n"
        "User requirement:\n"
        f"{problem_text}\n\n"
        "Generated code:\n"
        f"{code_text}\n\n"
        "Required output format (JSON only, no markdown fences):\n"
        f"{json_schema_example}"
    )
    # Create the Task directly — bypassing _build_task_from_spec / _render_prompt_template —
    # because task_description already contains fully-rendered code content that may include
    # literal { } characters (e.g. dict literals, JSON snippets) that would be misinterpreted
    # as format-string placeholders by string.Formatter.parse().
    critic_agent = _create_agent_from_spec(agent_spec, llm)
    task = Task(
        description=task_description,
        agent=critic_agent,
        expected_output="CritiqueBundle JSON only.",
    )
    crew = Crew(
        agents=[critic_agent],
        tasks=[task],
        process=Process.sequential,
        verbose=False,
    )
    setattr(crew, "_retry_policy", agent_spec.retry_policy)
    setattr(crew, "_crew_name", "codegen_critique_crew")
    return crew


def _extract_critique_bundle(result: Any) -> Optional[CritiqueBundle]:
    """Parse a CritiqueBundle from a crew kickoff result, trying all text candidates."""
    candidates: List[Any] = [result] + list(_collect_text_candidates_from_result(result))
    for raw in reversed(candidates):
        text = (
            _extract_text_from_result(raw) if not isinstance(raw, str) else raw
        )
        if not text:
            continue
        parsed = _extract_first_json_object(text)
        if parsed is None:
            continue
        try:
            score_raw = parsed.get("score")
            if score_raw is None:
                continue
            score = float(score_raw)
            score = max(0.0, min(1.0, score))
            issues = [str(x) for x in list(parsed.get("issues") or [])]
            suggestions = [str(x) for x in list(parsed.get("suggestions") or [])]
            summary = str(parsed.get("summary") or "").strip()
            return CritiqueBundle(
                score=score,
                issues=issues,
                suggestions=suggestions,
                summary=summary,
            )
        except Exception:
            continue
    return None


def _build_auto_optimize_critique_note(
    critique: CritiqueBundle,
    *,
    round_num: int,
    threshold: float,
) -> str:
    """Build the feedback block injected into the next codegen round's problem context."""
    lines: List[str] = [
        f"=== CODEGEN AUTO-OPTIMIZE FEEDBACK (Round {round_num}) ===",
        f"Previous attempt score: {critique.score:.2f} (threshold: {threshold:.2f})",
    ]
    if critique.summary:
        lines.append(f"Assessment: {critique.summary}")
    if critique.issues:
        lines.append("Issues to fix:")
        for issue in critique.issues[:8]:
            lines.append(f"- {issue}")
    if critique.suggestions:
        lines.append("Suggestions:")
        for suggestion in critique.suggestions[:8]:
            lines.append(f"- {suggestion}")
    lines.append("Please address ALL issues above in this generation.")
    lines.append("=" * 52)
    return "\n".join(line for line in lines if line)


def run_codegen_auto_optimize(
    user_problem: str,
    *,
    mode: str,
    language_hint: str,
    llm: Any,
    analysis_report: Optional["AnalysisReport"],
    gate_decision: Optional["GateDecision"],
    run_snapshot: Optional["RunSnapshot"] = None,
    max_rounds: int = 3,
    threshold: float = 0.80,
    budget_policy: Optional["BudgetPolicy"] = None,
    scope: str = "mvp",
    plateau_rounds: int = 2,     # Stop if score doesn't improve for this many rounds
    min_improvement: float = 0.01,  # Minimum score improvement to count as progress
) -> Tuple[Any, Optional["CodeBundle"]]:
    """
    Iterative codegen optimiser (autoresearch-style generate → critique → refine loop).

    Every round follows the same sequence: codegen → critic → score.
    The critic runs on *every* round (including the last) so that every bundle
    receives a score and the highest-scored bundle is always returned.

    After the critic scores:
      - If score >= threshold → stop, return this bundle.
      - If this is the last round → stop, return the highest-scored bundle.
      - Otherwise → inject critique feedback into the problem context and
        start the next round.

    Budget hard limits are checked before each non-first round.

    Parameters
    ----------
    max_rounds : int
        Maximum number of generate+critique cycles (minimum 1).
    threshold : float
        Critic score [0.0, 1.0] at which optimisation stops early.
    budget_policy : BudgetPolicy | None
        If provided, optimisation stops before a round when hard limits are hit.
    """
    max_rounds = max(1, int(max_rounds))
    threshold = max(0.0, min(1.0, float(threshold)))

    best_bundle: Optional["CodeBundle"] = None
    best_score: float = -1.0
    current_problem = user_problem
    rounds_without_improvement: int = 0
    prev_best_score: float = float("-inf")  # -inf ensures round 0 always registers as improvement

    for round_num in range(max_rounds):
        # ── Budget guard (skip round 0 — first codegen is always attempted) ──
        if round_num > 0 and budget_policy is not None:
            budget_state = _evaluate_budget_state(budget_policy)
            if budget_state.get("over_hard_limit") or budget_state.get("over_token_limit"):
                print(
                    f"[AutoOptimize] Budget hard limit reached before round {round_num + 1}. "
                    "Returning best bundle so far."
                )
                log_event(
                    LOGGER,
                    30,
                    "auto_optimize_budget_stop",
                    "Auto-optimize stopped: budget hard limit.",
                    round_num=round_num,
                    best_score=best_score,
                )
                break

        # ── CodeGen round ──────────────────────────────────────────────────────
        print(f"[AutoOptimize] CodeGen round {round_num + 1}/{max_rounds}...")
        log_event(
            LOGGER,
            20,
            "auto_optimize_codegen_start",
            "Auto-optimize codegen round starting.",
            round_num=round_num,
            max_rounds=max_rounds,
        )
        _snapshot_record_stage(
            run_snapshot,
            stage=f"auto_optimize.r{round_num}.codegen",
            status="started",
        )

        _, bundle = run_codegen_stage(
            current_problem,
            mode=mode,
            language_hint=language_hint,
            llm=llm,
            analysis_report=analysis_report,
            gate_decision=gate_decision,
            run_snapshot=run_snapshot,
            scope=scope,
        )

        if bundle is None:
            print(f"[AutoOptimize] Round {round_num + 1}: CodeBundle parse failed.")
            log_event(
                LOGGER,
                30,
                "auto_optimize_codegen_failed",
                "Auto-optimize codegen round produced no bundle.",
                round_num=round_num,
            )
            _snapshot_record_stage(
                run_snapshot,
                stage=f"auto_optimize.r{round_num}.codegen",
                status="failed",
                failure_type=FailureType.JSON_INVALID,
                notes="CodeBundle parse failed.",
            )
            # Return whatever was best so far (may be None on round 0)
            if best_bundle is not None:
                print(
                    f"[AutoOptimize] Using best bundle from previous round "
                    f"(score={best_score:.2f})."
                )
            return None, best_bundle

        _snapshot_record_stage(
            run_snapshot,
            stage=f"auto_optimize.r{round_num}.codegen",
            status="completed",
        )

        # ── Critic round (runs on EVERY round including the last) ─────────────
        print(f"[AutoOptimize] Running critic for round {round_num + 1}...")
        log_event(
            LOGGER,
            20,
            "auto_optimize_critic_start",
            "Auto-optimize critic starting.",
            round_num=round_num,
        )
        _snapshot_record_stage(
            run_snapshot,
            stage=f"auto_optimize.r{round_num}.critic",
            status="started",
        )

        critic_crew = _build_codegen_critique_crew(
            bundle,
            user_problem=user_problem,
            mode=mode,
            language_hint=language_hint,
            llm=llm,
        )
        critic_prompt_chars = len(str(getattr(
            critic_crew.tasks[0], "description", ""
        ) if critic_crew.tasks else ""))
        try:
            critic_result = kickoff_crew_with_retry(
                critic_crew,
                logger=LOGGER,
                log_fields={"stage": f"auto_optimize_critic_r{round_num}", "mode": mode},
            )
        except _OperationCancelledError:
            # Cooperative cancellation must abort the entire auto-optimize loop.
            # Do not treat as a non-fatal critic failure — propagate to caller.
            raise
        except Exception as critic_exc:
            log_exception(
                LOGGER,
                "auto_optimize_critic_failed",
                "Auto-optimize critic crew failed.",
                round_num=round_num,
            )
            try:
                _record_cost(
                    stage=f"auto_optimize.r{round_num}.critic",
                    agent_name="codegen_critic",
                    input_tokens=max(0, critic_prompt_chars // 3),
                    output_tokens=0,
                    success=False,
                    outcome="execution_error",
                )
            except Exception:
                pass
            _snapshot_record_stage(
                run_snapshot,
                stage=f"auto_optimize.r{round_num}.critic",
                status="failed",
                failure_type=_classify_runtime_exception_failure(critic_exc),
                notes=str(critic_exc),
            )
            # Critic failure is non-fatal: keep current bundle if no previous best
            if best_bundle is None:
                best_bundle = bundle
            print(
                f"[AutoOptimize] Critic failed at round {round_num + 1}. "
                "Keeping current bundle."
            )
            break

        critique = _extract_critique_bundle(critic_result)
        critic_result_text = _extract_text_from_result(critic_result) or ""

        if critique is None:
            log_event(
                LOGGER,
                30,
                "auto_optimize_critique_parse_failed",
                "Auto-optimize: CritiqueBundle parse failed.",
                round_num=round_num,
            )
            try:
                _record_cost(
                    stage=f"auto_optimize.r{round_num}.critic",
                    agent_name="codegen_critic",
                    input_tokens=max(0, critic_prompt_chars // 3),
                    output_tokens=len(critic_result_text) // 3,
                    success=False,
                    outcome="parse_failed",
                )
            except Exception:
                pass
            _snapshot_record_stage(
                run_snapshot,
                stage=f"auto_optimize.r{round_num}.critic",
                status="failed",
                failure_type=FailureType.JSON_INVALID,
                notes="CritiqueBundle parse failed.",
            )
            if best_bundle is None:
                best_bundle = bundle
            print(
                f"[AutoOptimize] Critique parse failed at round {round_num + 1}. "
                "Keeping current bundle."
            )
            break

        # ── Critic succeeded: record cost, update best, decide next step ──────
        try:
            _record_cost(
                stage=f"auto_optimize.r{round_num}.critic",
                agent_name="codegen_critic",
                input_tokens=max(0, critic_prompt_chars // 3),
                output_tokens=len(critic_result_text) // 3,
                success=True,
                outcome="success",
            )
        except Exception:
            pass

        _snapshot_record_stage(
            run_snapshot,
            stage=f"auto_optimize.r{round_num}.critic",
            status="completed",
            extra={"score": critique.score},
        )
        print(
            f"[AutoOptimize] Round {round_num + 1} score: {critique.score:.2f} "
            f"(threshold={threshold:.2f})"
        )
        log_event(
            LOGGER,
            20,
            "auto_optimize_critique_done",
            "Auto-optimize critique complete.",
            round_num=round_num,
            score=critique.score,
            threshold=threshold,
            passed=critique.score >= threshold,
        )

        # Track the best bundle seen so far
        if critique.score > best_score:
            best_score = critique.score
            best_bundle = bundle

        # After updating best_score, check for plateau
        # Compare critique.score (this round's actual score) against prev_best_score
        # and update prev_best_score to this round's score (not best_score, which may
        # have been set by a prior round and would mask stagnation).
        if critique.score > prev_best_score + min_improvement:
            rounds_without_improvement = 0
            prev_best_score = critique.score
        else:
            rounds_without_improvement += 1
            log_event(
                LOGGER,
                20,
                "auto_optimize_plateau",
                "Auto-optimize: score plateau detected.",
                round_num=round_num,
                rounds_without_improvement=rounds_without_improvement,
                score=critique.score,
                best_score=best_score,
                plateau_threshold=plateau_rounds,
            )
            if rounds_without_improvement >= plateau_rounds and round_num < max_rounds - 1:
                print(
                    f"[AutoOptimize] Score plateau detected ({rounds_without_improvement} "
                    f"rounds without \u2265{min_improvement:.2f} improvement). "
                    "Stopping early with best bundle."
                )
                log_event(
                    LOGGER,
                    20,
                    "auto_optimize_plateau_stop",
                    "Auto-optimize stopped: score plateau.",
                    round_num=round_num,
                    best_score=best_score,
                    rounds_without_improvement=rounds_without_improvement,
                )
                break

        # Threshold met → done
        if critique.score >= threshold:
            print(
                f"[AutoOptimize] Score {critique.score:.2f} >= threshold {threshold:.2f}. "
                "Optimisation complete."
            )
            log_event(
                LOGGER,
                20,
                "auto_optimize_threshold_met",
                "Auto-optimize threshold met. Stopping.",
                round_num=round_num,
                score=critique.score,
            )
            break

        # Last round reached → done (best_bundle is already the highest scored)
        if round_num == max_rounds - 1:
            print(
                f"[AutoOptimize] Final round {round_num + 1} complete. "
                f"Returning best bundle (score={best_score:.2f})."
            )
            log_event(
                LOGGER,
                20,
                "auto_optimize_final_round",
                "Auto-optimize reached final round.",
                round_num=round_num,
                best_score=best_score,
            )
            break

        # Prepare next round: inject critique feedback into problem context
        critique_note = _build_auto_optimize_critique_note(
            critique,
            round_num=round_num + 1,
            threshold=threshold,
        )
        current_problem = user_problem + "\n\n" + critique_note

    return None, best_bundle
