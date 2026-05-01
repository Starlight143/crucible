# ruff: noqa: E402
import os
import sys
import unittest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from crucible.module_runtime import get_runtime

qsc = get_runtime()


class TestQualityScoping(unittest.TestCase):
    def _bundle(self) -> qsc.CodeBundle:
        return qsc.CodeBundle(
            project_type="saas",
            files=[
                qsc.GeneratedFile(path="src/app.py", content="print('x')\n"),
                qsc.GeneratedFile(path="api/main.py", content="app = object()\n"),
                qsc.GeneratedFile(path="README.md", content="# readme\n"),
            ],
        )

    def test_safe_scope_resolves_prefixed_and_absolute_paths(self) -> None:
        bundle = self._bundle()
        affected = {
            "code/src/app.py",
            r"C:\anywhere\project\src\app.py",
            "/tmp/project/api/main.py",
            "README.md",
        }
        resolved = qsc._safe_scope_files(bundle, affected)
        self.assertIsNotNone(resolved)
        self.assertEqual(set(resolved or []), {"src/app.py", "api/main.py", "README.md"})

    def test_safe_scope_falls_back_when_unresolvable(self) -> None:
        bundle = self._bundle()
        affected = {r"C:\anywhere\project\missing.py"}
        resolved = qsc._safe_scope_files(bundle, affected)
        self.assertIsNone(resolved)


if __name__ == "__main__":
    unittest.main()
