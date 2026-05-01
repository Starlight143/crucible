# ruff: noqa: E402, I001
import importlib
import os
import sys
import unittest


ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


class TestRuntimeProfileSettings(unittest.TestCase):
    def test_selective_rerun_attempts_preserve_explicit_zero(self) -> None:
        import crucible.modules.section_06_runtime_quality_api as m06

        original_env = os.environ.get("SELECTIVE_RERUN_MAX_ATTEMPTS")
        try:
            os.environ["SELECTIVE_RERUN_MAX_ATTEMPTS"] = "0"
            importlib.reload(m06)
            self.assertEqual(m06.SELECTIVE_RERUN_MAX_ATTEMPTS, 0)
        finally:
            if original_env is None:
                os.environ.pop("SELECTIVE_RERUN_MAX_ATTEMPTS", None)
            else:
                os.environ["SELECTIVE_RERUN_MAX_ATTEMPTS"] = original_env
            importlib.reload(m06)

    def test_selective_rerun_attempts_fall_back_when_env_requests_none(self) -> None:
        import crucible.modules.section_06_runtime_quality_api as m06

        original_env = os.environ.get("SELECTIVE_RERUN_MAX_ATTEMPTS")
        try:
            os.environ["SELECTIVE_RERUN_MAX_ATTEMPTS"] = "none"
            importlib.reload(m06)
            self.assertEqual(m06.SELECTIVE_RERUN_MAX_ATTEMPTS, 5)
        finally:
            if original_env is None:
                os.environ.pop("SELECTIVE_RERUN_MAX_ATTEMPTS", None)
            else:
                os.environ["SELECTIVE_RERUN_MAX_ATTEMPTS"] = original_env
            importlib.reload(m06)


if __name__ == "__main__":
    unittest.main()
