from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CURRENT_PACKAGE_ENTRY = ROOT / "crucible" / "__main__.py"
CURRENT_ROOT_LAUNCHER = ROOT / "run_crucible.py"


def run_help(script: Path) -> subprocess.CompletedProcess[str]:
    # Hard timeout: a stuck import (deadlock in module_runtime, blocking I/O on
    # a misconfigured plugin, network probe to a dead OpenRouter endpoint, etc.)
    # would leave the smoke test hanging forever.  120 s is generous for a cold
    # ``--help`` invocation; surface a TimeoutExpired as exit-code 124 so the
    # caller sees a deterministic [FAIL] line instead of CI silently wedging.
    try:
        return subprocess.run(
            [sys.executable, str(script), "--help"],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=120,
        )
    except subprocess.TimeoutExpired as exc:
        # Reconstruct a CompletedProcess so the rest of main() handles it
        # exactly like a normal failed run.
        stdout = exc.stdout if isinstance(exc.stdout, str) else ""
        stderr = exc.stderr if isinstance(exc.stderr, str) else ""
        if not stderr:
            stderr = f"--help timed out after {exc.timeout}s"
        return subprocess.CompletedProcess(
            args=[sys.executable, str(script), "--help"],
            returncode=124,
            stdout=stdout,
            stderr=stderr,
        )


def main() -> int:
    results = {
        "mainline_root": run_help(CURRENT_ROOT_LAUNCHER),
        "mainline_pkg": run_help(CURRENT_PACKAGE_ENTRY),
    }

    failed = False
    for name, result in results.items():
        if result.returncode != 0:
            failed = True
            print(f"[FAIL] {name} --help exited with {result.returncode}")
            print(result.stderr)
        elif "usage:" not in result.stdout.lower():
            failed = True
            print(f"[FAIL] {name} --help did not print argparse usage output")
            print(result.stdout[:1000])
        else:
            print(f"[OK] {name} --help")

    sys.path.insert(0, str(ROOT))
    try:
        import crucible as pkg
        from crucible.module_runtime import get_runtime

        runtime = get_runtime()
        required = ("main", "build_crew", "run_quality_loop", "run_api_version_check")
        missing = [name for name in required if not hasattr(runtime, name)]
        runtime_root = getattr(runtime, "PROJECT_ROOT", None)
        loaded_env = getattr(runtime, "LOADED_ENV_FILE", None)
        expected_env = str(ROOT / ".env") if (ROOT / ".env").is_file() else loaded_env
        if (
            not hasattr(pkg, "main")
            or missing
            or runtime_root != str(ROOT)
            or loaded_env != expected_env
        ):
            failed = True
            print(
                "[FAIL] package import/runtime sync failed: "
                f"pkg_main={hasattr(pkg, 'main')} missing={missing} "
                f"runtime_root={runtime_root} loaded_env={loaded_env}"
            )
        else:
            print("[OK] package import")
            print("[OK] module runtime sync")
            print("[OK] root path override")
    except Exception as exc:
        failed = True
        print(f"[FAIL] package import raised: {exc}")

    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
