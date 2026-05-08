# ruff: noqa: E402
import os
import sys
import unittest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from crucible.module_runtime import get_runtime

qsc = get_runtime()


class TestJsonExtraction(unittest.TestCase):
    def test_fenced_json(self) -> None:
        text = "prefix\n```json\n{\"a\": 1}\n```\nnoise"
        self.assertEqual(qsc._extract_first_json_object(text), {"a": 1})

    def test_multiple_json_objects(self) -> None:
        text = "xx {\"a\": 1} yy {\"b\": 2}"
        self.assertEqual(qsc._extract_first_json_object(text), {"a": 1})

    def test_brace_in_string(self) -> None:
        text = "noise {\"a\": \"{x}\", \"b\": 2} tail"
        self.assertEqual(qsc._extract_first_json_object(text), {"a": "{x}", "b": 2})

    def test_incomplete_json(self) -> None:
        text = "prefix {\"a\": 1"
        self.assertIsNone(qsc._extract_first_json_object(text))

    def test_non_object_json(self) -> None:
        text = "```json\n[1, 2, 3]\n```"
        self.assertIsNone(qsc._extract_first_json_object(text))

    def test_think_tag_decoy_stripped(self) -> None:
        """Reasoning models embed <think>…</think> ahead of the answer.
        Brace-shape tokens inside the reasoning block must not be returned
        as the "first" outermost JSON object.  Regression: on a Quant Mode
        Direction Debate run with DeepSeek-V4-pro as judge, the model emits
        a tentative ``{"option": "X"}`` inside <think> before the real
        decision JSON; without stripping, the decoy was returned and
        DirectionDecision parsing failed, force-killing the entire stage."""
        text = (
            "<think>I'll consider {\"option\": \"X\"} as a draft.</think>\n"
            "{\"selected_direction\": \"long\", \"confidence\": \"high\"}"
        )
        self.assertEqual(
            qsc._extract_first_json_object(text),
            {"selected_direction": "long", "confidence": "high"},
        )

    def test_thinking_alias_stripped(self) -> None:
        text = (
            "<thinking>{\"foo\": 1}</thinking>"
            "{\"selected_direction\": \"B\"}"
        )
        self.assertEqual(
            qsc._extract_first_json_object(text),
            {"selected_direction": "B"},
        )

    def test_reasoning_alias_stripped(self) -> None:
        text = (
            "<reasoning>some chain of thought {\"draft\": true}</reasoning>"
            "{\"answer\": 99}"
        )
        self.assertEqual(qsc._extract_first_json_object(text), {"answer": 99})

    def test_no_tag_unchanged(self) -> None:
        text = "{\"a\": 1}"
        self.assertEqual(qsc._extract_first_json_object(text), {"a": 1})


if __name__ == "__main__":
    unittest.main()
