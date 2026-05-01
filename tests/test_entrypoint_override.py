# ruff: noqa: E402
import os
import shutil
import sys
import unittest
import uuid

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from crucible.module_runtime import get_runtime

qsc = get_runtime()


class TestEntrypointOverride(unittest.TestCase):
    def test_split_override(self) -> None:
        raw = "api/main.py:app,service.py:application;factory:create_app()"
        self.assertEqual(
            qsc._split_entrypoint_override(raw),
            ["api/main.py:app", "service.py:application", "factory:create_app()"],
        )

    def test_parse_entrypoint_spec(self) -> None:
        spec = qsc._parse_entrypoint_spec("api/main.py:app")
        self.assertEqual(spec.path, "api/main.py")
        self.assertEqual(spec.attribute, "app")
        self.assertFalse(spec.call)

        spec = qsc._parse_entrypoint_spec("api.main:create_app()")
        self.assertEqual(spec.path, "api.main")
        self.assertEqual(spec.attribute, "create_app")
        self.assertTrue(spec.call)

        spec = qsc._parse_entrypoint_spec(r"C:\app\main.py")
        self.assertEqual(spec.path, r"C:\app\main.py")
        self.assertIsNone(spec.attribute)
        self.assertFalse(spec.call)

    def test_resolve_entrypoint_path(self) -> None:
        tmp_root = os.path.join(ROOT, ".tmp_test")
        os.makedirs(tmp_root, exist_ok=True)
        tmp_dir = os.path.join(tmp_root, "tmp_" + uuid.uuid4().hex)
        os.makedirs(tmp_dir, exist_ok=False)
        try:
            api_dir = os.path.join(tmp_dir, "api")
            os.makedirs(api_dir, exist_ok=True)
            module_path = os.path.join(api_dir, "main.py")
            with open(module_path, "w", encoding="utf-8") as f:
                f.write("app = object()\n")

            resolved = qsc._resolve_entrypoint_path("api/main.py", tmp_dir)
            self.assertEqual(resolved, os.path.realpath(module_path))

            resolved = qsc._resolve_entrypoint_path("api.main", tmp_dir)
            self.assertEqual(resolved, os.path.realpath(module_path))

            resolved = qsc._resolve_entrypoint_path("api/main", tmp_dir)
            self.assertEqual(resolved, os.path.realpath(module_path))

            resolved = qsc._resolve_entrypoint_path("missing.py", tmp_dir)
            self.assertIsNone(resolved)
        finally:
            # Clean the entire .tmp_test tree, not just the UUID sub-directory,
            # to prevent stale directories from accumulating across test runs.
            shutil.rmtree(tmp_root, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
