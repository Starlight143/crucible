# ruff: noqa: E402, I001
import os
import sys
import unittest
from types import SimpleNamespace


ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from crucible.web_research.crew_factory import build_task_from_spec


class _FakeTask:
    def __init__(self, **kwargs) -> None:
        self.kwargs = kwargs


class TestCrewFactory(unittest.TestCase):
    def test_build_task_from_spec_preserves_context_order(self) -> None:
        task_spec = SimpleNamespace(
            name="review_task",
            description_template="Prompt: {topic}",
            agent_name="reviewer",
            expected_output="json",
            context_task_names=["collect_a", "collect_b"],
            output_pydantic_model=None,
        )

        task = build_task_from_spec(
            task_spec,
            agents={"reviewer": "agent-reviewer"},
            task_lookup={"collect_a": "task-a", "collect_b": "task-b"},
            template_vars={"topic": "alpha"},
            render_prompt_template=lambda template, vars: template.format(**vars),
            strict_json_enabled=False,
            crewai_output_pydantic=False,
            output_model_by_name=lambda name: None,
            task_cls=_FakeTask,
        )

        self.assertEqual(task.kwargs["description"], "Prompt: alpha")
        self.assertEqual(task.kwargs["context"], ["task-a", "task-b"])

    def test_build_task_from_spec_fails_fast_on_missing_context_tasks(self) -> None:
        task_spec = SimpleNamespace(
            name="review_task",
            description_template="Prompt",
            agent_name="reviewer",
            expected_output="json",
            context_task_names=["collect_a", "collect_b"],
            output_pydantic_model=None,
        )

        with self.assertRaisesRegex(KeyError, "missing context task"):
            build_task_from_spec(
                task_spec,
                agents={"reviewer": "agent-reviewer"},
                task_lookup={"collect_a": "task-a"},
                template_vars={},
                render_prompt_template=lambda template, vars: template,
                strict_json_enabled=False,
                crewai_output_pydantic=False,
                output_model_by_name=lambda name: None,
                task_cls=_FakeTask,
            )


if __name__ == "__main__":
    unittest.main()
