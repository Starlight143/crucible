from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LEGACY_ROOT = ROOT / "OLD_version"
SOURCE_FILE = LEGACY_ROOT / "crucible_v14.py"
TARGET_ROOT = ROOT / "crucible"
MODULES_DIR = TARGET_ROOT / "modules"


SECTIONS = [
    {
        "raw_file": "00_bootstrap_and_utils.py",
        "module_file": "section_00_bootstrap_and_utils.py",
        "start": 1,
        "end": 941,
        "description": "環境載入、平台相容、JSON/模板/基本工具函式。",
    },
    {
        "raw_file": "01_extraction_and_reformat.py",
        "module_file": "section_01_extraction_and_reformat.py",
        "start": 942,
        "end": 2337,
        "description": "結構化輸出抽取、reformatter 與結果修復邏輯。",
    },
    {
        "raw_file": "02_research_and_llm.py",
        "module_file": "section_02_research_and_llm.py",
        "start": 2338,
        "end": 3507,
        "description": "研究流程、方向辯論、LLM 初始化與本地快取。",
    },
    {
        "raw_file": "03_models_and_context.py",
        "module_file": "section_03_models_and_context.py",
        "start": 3508,
        "end": 6210,
        "description": "Pydantic 模型、project context、modes、crew 建構。",
    },
    {
        "raw_file": "04_web_research_and_direction.py",
        "module_file": "section_04_web_research_and_direction.py",
        "start": 6211,
        "end": 11167,
        "description": "Web/context7/GitHub research 與 direction decision 邏輯。",
    },
    {
        "raw_file": "05_analysis_and_codegen.py",
        "module_file": "section_05_analysis_and_codegen.py",
        "start": 11168,
        "end": 12560,
        "description": "analysis/codegen 階段與 code bundle/review 範圍控制。",
    },
    {
        "raw_file": "06_runtime_quality_api.py",
        "module_file": "section_06_runtime_quality_api.py",
        "start": 12561,
        "end": 16027,
        "description": "runtime validation、quality loop、API version check。",
    },
    {
        "raw_file": "07_selfcheck_output_main.py",
        "module_file": "section_07_selfcheck_output_main.py",
        "start": 16028,
        "end": 17419,
        "description": "self-check、輸出保存與 CLI 主流程入口。",
    },
]


MODULE_HEADER = """# Auto-generated section module — do not edit manually.
# Regenerate via ``python -m crucible.generate``.
from __future__ import annotations
"""

OVERRIDE_START_MARKER = "# BEGIN MANUAL OUTPUT SAVE OVERRIDES"
OVERRIDE_END_MARKER = "# END MANUAL OUTPUT SAVE OVERRIDES"
FORCE_REGENERATE_ENV = "CRUCIBLE_FORCE_REGENERATE"


def _slice_lines(lines: list[str], start: int, end: int) -> str:
    chunk = "".join(lines[start - 1 : end])
    if start == 1:
        chunk = chunk.lstrip("\ufeff")
    return chunk


def _write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # Atomic write: write to a sibling .tmp file then rename so that a reader
    # never sees a partially-written module file that could cause import errors.
    tmp = path.with_suffix(path.suffix + ".tmp")
    try:
        tmp.write_text(content, encoding="utf-8", newline="\n")
        tmp.replace(path)
    except Exception:
        try:
            tmp.unlink(missing_ok=True)
        except Exception:
            pass
        raise


def _env_truthy(name: str) -> bool:
    raw = str(os.environ.get(name, "") or "").strip().lower()
    return raw in {"1", "true", "yes", "on"}


def _find_regeneration_conflicts(rendered_files: dict[Path, str]) -> list[Path]:
    conflicts: list[Path] = []
    for path, content in rendered_files.items():
        if not path.is_file():
            continue
        current = path.read_text(encoding="utf-8")
        if current != content:
            conflicts.append(path)
    return sorted(conflicts)


def _ensure_safe_regeneration(rendered_files: dict[Path, str]) -> None:
    if _env_truthy(FORCE_REGENERATE_ENV):
        return
    conflicts = _find_regeneration_conflicts(rendered_files)
    if not conflicts:
        return
    relative_conflicts = ", ".join(path.relative_to(ROOT).as_posix() for path in conflicts)
    raise RuntimeError(
        "Regeneration would overwrite existing files that diverged from generator output: "
        f"{relative_conflicts}. "
        f"Review the diffs first or rerun with {FORCE_REGENERATE_ENV}=1 to force overwrite."
    )


def _module_imports(index: int) -> str:
    if index == 0:
        return ""

    imports = []
    for idx, prev in enumerate(SECTIONS[:index]):
        stem = Path(prev["module_file"]).stem
        alias = f"_prev_{idx:02d}"
        imports.append(f"from . import {stem} as {alias}")
        imports.append(
            f"globals().update({{k: v for k, v in {alias}.__dict__.items() if not k.startswith('__')}})"
        )
    return "\n".join(imports) + "\n\n"


def _load_manual_override_block(module_file: str) -> str:
    current_module = MODULES_DIR / module_file
    if not current_module.is_file():
        return ""
    text = current_module.read_text(encoding="utf-8")
    start = text.find(OVERRIDE_START_MARKER)
    end = text.find(OVERRIDE_END_MARKER)
    if start == -1 or end == -1 or end < start:
        return ""
    block = text[start : end + len(OVERRIDE_END_MARKER)].strip()
    return block + "\n"


def _apply_manual_overrides(section: dict[str, object], body: str) -> str:
    module_file = str(section["module_file"])
    override_block = _load_manual_override_block(module_file)
    if not override_block:
        return body
    main_guard = '\n\nif __name__ == "__main__":\n    main()\n'
    if main_guard in body:
        return body.replace(main_guard, f"\n\n{override_block}\n{main_guard.lstrip()}")
    return body.rstrip() + f"\n\n{override_block}"


def _build_module_content(index: int, lines: list[str]) -> str:
    section = SECTIONS[index]
    body = _slice_lines(lines, section["start"], section["end"])
    body = body.replace(
        'parser = argparse.ArgumentParser(\n        description="Quant / SaaS / Agent Analysis Crew + Project Scan"\n    )',
        'parser = argparse.ArgumentParser(\n        prog="run_crucible.py",\n        description="Quant / SaaS / Agent Analysis Crew + Project Scan"\n    )',
    )
    body = body.replace(
        'help="Enable API version check after CodeGen (v14). Checks for outdated library usage.",',
        'help="Enable API version check after CodeGen. Checks for outdated library usage.",',
    )
    body = body.replace(
        'print("   Quant / SaaS / Agent Analysis Crew (v14.0) ")',
        'print("   Quant / SaaS / Agent Analysis Crew")',
    )
    body = body.replace(
        'if __package__ == "crucible.modules":\n'
        '    from ..resilience import kickoff_crew_with_retry\n'
        "    from ..runtime_logging import (\n"
        "        configure_logging,\n"
        "        get_logger,\n"
        "        log_event,\n"
        "        log_exception,\n"
        "        update_log_context,\n"
        "    )\n"
        "else:  # pragma: no cover - direct script fallback\n"
        "    from resilience import kickoff_crew_with_retry\n"
        "    from runtime_logging import (\n"
        "        configure_logging,\n"
        "        get_logger,\n"
        "        log_event,\n"
        "        log_exception,\n"
        "        update_log_context,\n"
        "    )",
        'if __package__ == "crucible.modules":\n'
        '    from ..resilience import kickoff_crew_with_retry, reset_circuit_breakers\n'
        "    from ..runtime_logging import (\n"
        "        clear_log_context,\n"
        "        configure_logging,\n"
        "        get_logger,\n"
        "        log_event,\n"
        "        log_exception,\n"
        "        update_log_context,\n"
        "    )\n"
        "else:  # pragma: no cover - direct script fallback\n"
        "    from resilience import kickoff_crew_with_retry, reset_circuit_breakers\n"
        "    from runtime_logging import (\n"
        "        clear_log_context,\n"
        "        configure_logging,\n"
        "        get_logger,\n"
        "        log_event,\n"
        "        log_exception,\n"
        "        update_log_context,\n"
        "    )",
    )
    body = body.replace(
        "def _reset_pipeline_runtime_state() -> None:\n"
        "    # Main can be called repeatedly inside one process via module_runtime; clear prior run state.\n"
        "    clear_openrouter_usage()\n"
        "    reset_cost_accountant()\n"
        "    clear_last_librarian_debug()\n"
        "    reset_research_llm_cache()\n"
        "    reset_api_version_cache()\n",
        "def _reset_pipeline_runtime_state() -> None:\n"
        "    # Main can be called repeatedly inside one process via module_runtime; clear prior run state.\n"
        "    clear_openrouter_usage()\n"
        "    reset_cost_accountant()\n"
        "    reset_circuit_breakers()\n"
        "    clear_last_librarian_debug()\n"
        "    reset_research_llm_cache()\n"
        "    reset_api_version_cache()\n"
        "    clear_log_context()\n",
    )
    body = body.replace(
        '        "- Build a pure-Python strategy/execution project",\n'
        '        "- Prefer strategy.py plus a backtest or execution runner",\n'
        '        "- Do not introduce a web framework unless explicitly required by the prompt",',
        '        "- Build a pure-Python strategy/execution project",\n'
        '        "- Quant mode must include strategy logic, a backtest runner, a trading/execution module, and a signals/results export module",\n'
        '        "- Prefer concrete filenames such as strategy.py, backtest.py, trade.py, export.py, and config.py unless the prompt requires equivalent names",\n'
        '        "- Do not introduce a web framework unless explicitly required by the prompt",',
    )
    body = _apply_manual_overrides(section, body)
    imports = _module_imports(index)
    return MODULE_HEADER + "\n" + imports + body


def _build_modules_init() -> str:
    lines = [
        '"""Import-based section modules — auto-generated; do not edit by hand."""',
        "",
    ]
    lines.append("__all__ = [")
    for section in SECTIONS:
        stem = Path(section["module_file"]).stem
        lines.append(f'    "{stem}",')
    lines.append("]")
    lines.append("")
    return "\n".join(lines)


def main() -> None:
    if not SOURCE_FILE.is_file():
        raise FileNotFoundError(f"Legacy source not found: {SOURCE_FILE}")

    source_text = SOURCE_FILE.read_text(encoding="utf-8")
    lines = source_text.splitlines(keepends=True)

    MODULES_DIR.mkdir(parents=True, exist_ok=True)

    manifest_lines = [
        "# crucible section manifest",
        "",
        "Module-by-module breakdown of `crucible/modules/`:",
        "",
    ]

    rendered_files: dict[Path, str] = {}

    for index, section in enumerate(SECTIONS):
        module_content = _build_module_content(index, lines)
        rendered_files[MODULES_DIR / section["module_file"]] = module_content

        manifest_lines.append(
            f"- `{section['module_file']}`: {section['description']}"
        )

    rendered_files[MODULES_DIR / "__init__.py"] = _build_modules_init()
    rendered_files[TARGET_ROOT / "SECTION_MANIFEST.md"] = "\n".join(manifest_lines) + "\n"

    _ensure_safe_regeneration(rendered_files)

    for path, content in rendered_files.items():
        _write_text(path, content)


if __name__ == "__main__":
    main()
