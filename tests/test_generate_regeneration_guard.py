import os
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


class TestGenerateRegenerationGuard(unittest.TestCase):
    def _existing_repo_file(self) -> Path:
        return ROOT / "tests" / "test_crucible_runtime.py"

    def test_safe_regeneration_raises_when_existing_files_would_be_overwritten(self) -> None:
        from crucible import generate

        target = self._existing_repo_file()
        rendered = {target: "generated legacy content\n"}

        with self.assertRaises(RuntimeError) as ctx:
            generate._ensure_safe_regeneration(rendered)

        self.assertIn("would overwrite existing files", str(ctx.exception))
        self.assertIn("tests/test_crucible_runtime.py", str(ctx.exception))
        self.assertIn(generate.FORCE_REGENERATE_ENV, str(ctx.exception))

    def test_safe_regeneration_allows_same_content_without_force(self) -> None:
        from crucible import generate

        target = self._existing_repo_file()
        rendered = {target: target.read_text(encoding="utf-8")}
        generate._ensure_safe_regeneration(rendered)

    def test_safe_regeneration_allows_force_override(self) -> None:
        from crucible import generate

        target = self._existing_repo_file()
        rendered = {target: "generated legacy content\n"}
        original_env = os.environ.get(generate.FORCE_REGENERATE_ENV)
        try:
            os.environ[generate.FORCE_REGENERATE_ENV] = "1"
            generate._ensure_safe_regeneration(rendered)
        finally:
            if original_env is None:
                os.environ.pop(generate.FORCE_REGENERATE_ENV, None)
            else:
                os.environ[generate.FORCE_REGENERATE_ENV] = original_env


if __name__ == "__main__":
    unittest.main()
