from __future__ import annotations

if __package__ == "crucible":
    from .runtime_api import get_runtime
else:
    from runtime_api import get_runtime


_rt = get_runtime()

PROJECT_ROOT = _rt.PROJECT_ROOT
DEFAULT_ENV_FILE_NAME = _rt.DEFAULT_ENV_FILE_NAME
OPENROUTER_API_BASE_URL = _rt.OPENROUTER_API_BASE_URL
DEFAULT_PRIMARY_MODEL_ID = _rt.DEFAULT_PRIMARY_MODEL_ID
DEFAULT_DIRECTION_JUDGE_MODEL_ID = _rt.DEFAULT_DIRECTION_JUDGE_MODEL_ID
DEFAULT_LIBRARIAN_MODEL_ID = _rt.DEFAULT_LIBRARIAN_MODEL_ID
LOADED_ENV_FILE = _rt.LOADED_ENV_FILE

load_api_key = _rt.load_api_key
init_llm = _rt.init_llm
collect_dependency_versions = _rt.collect_dependency_versions
build_project_context = _rt.build_project_context
format_project_context = _rt.format_project_context
sanitize_name = _rt.sanitize_name
save_project_output = _rt.save_project_output
read_multiline_input = _rt.read_multiline_input
to_json_str = _rt.to_json_str
safe_read_text = _rt.safe_read_text
redact_secrets = _rt.redact_secrets

__all__ = [
    "PROJECT_ROOT",
    "DEFAULT_ENV_FILE_NAME",
    "OPENROUTER_API_BASE_URL",
    "DEFAULT_PRIMARY_MODEL_ID",
    "DEFAULT_DIRECTION_JUDGE_MODEL_ID",
    "DEFAULT_LIBRARIAN_MODEL_ID",
    "LOADED_ENV_FILE",
    "load_api_key",
    "init_llm",
    "collect_dependency_versions",
    "build_project_context",
    "format_project_context",
    "sanitize_name",
    "save_project_output",
    "read_multiline_input",
    "to_json_str",
    "safe_read_text",
    "redact_secrets",
]
