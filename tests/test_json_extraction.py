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


if __name__ == "__main__":
    unittest.main()
