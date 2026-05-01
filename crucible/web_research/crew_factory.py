from __future__ import annotations

from typing import Any, Dict


def _apply_input_budget(description: str, max_input_chars: Any) -> str:
    try:
        budget = None if max_input_chars is None else int(max_input_chars)
    except Exception:
        budget = None
    if budget is None or budget <= 0 or len(description) <= budget:
        return description
    suffix = "\n...[truncated]..."
    if budget <= len(suffix):
        return suffix[:budget]
    return description[: budget - len(suffix)] + suffix


def create_agent_from_spec(spec: Any, llm: Any, *, agent_cls: Any) -> Any:
    return agent_cls(
        role=spec.role,
        goal=spec.goal,
        backstory=spec.backstory,
        allow_delegation=spec.allow_delegation,
        verbose=spec.verbose,
        llm=llm,
    )


def build_task_from_spec(
    task_spec: Any,
    *,
    agents: Dict[str, Any],
    task_lookup: Dict[str, Any],
    template_vars: Dict[str, str],
    render_prompt_template: Any,
    strict_json_enabled: bool,
    crewai_output_pydantic: bool,
    output_model_by_name: Any,
    task_cls: Any,
) -> Any:
    description = render_prompt_template(task_spec.description_template, template_vars)
    description = _apply_input_budget(
        description, getattr(task_spec, "max_input_chars", None)
    )
    task_kwargs: Dict[str, Any] = {
        "description": description,
        "agent": agents[task_spec.agent_name],
        "expected_output": task_spec.expected_output,
        "name": task_spec.name,
    }
    if task_spec.context_task_names:
        missing_context_tasks = [
            name for name in task_spec.context_task_names if name not in task_lookup
        ]
        if missing_context_tasks:
            missing_list = ", ".join(missing_context_tasks)
            raise KeyError(
                f"Task {task_spec.name!r} references missing context task(s): {missing_list}"
            )
        task_kwargs["context"] = [
            task_lookup[name] for name in task_spec.context_task_names
        ]
    if strict_json_enabled and crewai_output_pydantic:
        output_model = output_model_by_name(task_spec.output_pydantic_model)
        if output_model is not None:
            task_kwargs["output_pydantic"] = output_model
    task = task_cls(**task_kwargs)
    setattr(task, "_prompt_chars", len(description))
    setattr(task, "_prompt_budget_chars", getattr(task_spec, "max_input_chars", None))
    setattr(task, "_prompt_context_count", len(task_spec.context_task_names or []))
    setattr(task, "_prompt_truncated", "...[truncated]..." in description)
    return task


def aggregate_retry_policy(agent_specs: Dict[str, Any], *, retry_policy_cls: Any) -> Any:
    if not agent_specs:
        return retry_policy_cls()
    max_attempts = 1
    backoff_seconds = 0.0
    retry_on_json_fail = False
    retry_on_low_confidence = False
    for spec in agent_specs.values():
        policy = getattr(spec, "retry_policy", None)
        if policy is None:
            continue
        raw_ma = getattr(policy, "max_attempts", 1)
        max_attempts = max(max_attempts, int(raw_ma) if raw_ma is not None else 1)
        raw_bs = getattr(policy, "backoff_seconds", 0.0)
        backoff_seconds = max(
            backoff_seconds, float(raw_bs) if raw_bs is not None else 0.0
        )
        retry_on_json_fail = retry_on_json_fail or bool(
            getattr(policy, "retry_on_json_fail", False)
        )
        retry_on_low_confidence = retry_on_low_confidence or bool(
            getattr(policy, "retry_on_low_confidence", False)
        )
    return retry_policy_cls(
        max_attempts=max_attempts,
        backoff_seconds=backoff_seconds,
        retry_on_json_fail=retry_on_json_fail,
        retry_on_low_confidence=retry_on_low_confidence,
    )
