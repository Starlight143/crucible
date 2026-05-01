# Auto-generated from OLD_version/crucible_v14.py.
# Import-based section module. Do not edit manually; regenerate from V14.
from __future__ import annotations
import argparse
import ast
import contextlib
import hashlib
import httpx
import ipaddress
import json
import os
import posixpath
import re
import signal
import shutil
import sqlite3
import sys
import string
import subprocess
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import xml.etree.ElementTree as ET
from contextlib import contextmanager
from dataclasses import dataclass
from enum import Enum
import contextvars
from html import unescape
from html.parser import HTMLParser

# Windows 兼容性修復：在 Windows 上 signal 模塊缺少許多 Unix 信號
# 若遇到 CrewAI 因 signal 報錯，可考慮在此處手動補上缺失的 signal 常數
if sys.platform.startswith("win"):
    # Windows 缺少許多 Unix 信號，這裡一次補齊常見的，以防 CrewAI 未來用到其他信號
    # 使用標準 Unix 信號值或不重複的整數，避免 Enum 值碰撞
    _missing = {
        "SIGHUP": 1,
        "SIGUSR1": 10,
        "SIGUSR2": 12,
        "SIGCHLD": 17,
        "SIGPIPE": 13,
        "SIGALRM": 14,
        "SIGTSTP": 20,
        "SIGQUIT": 3,
        "SIGTRAP": 5,
        "SIGABRT": 6,
        "SIGCONT": 18,
        "SIGSTOP": 19,
        "SIGTTIN": 21,
        "SIGTTOU": 22,
        "SIGURG": 23,
        "SIGXCPU": 24,
        "SIGXFSZ": 25,
        "SIGVTALRM": 26,
        "SIGPROF": 27,
        "SIGWINCH": 28,
    }
    for name, val in _missing.items():
        if not hasattr(signal, name):
            setattr(signal, name, val)

from datetime import datetime
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

from pydantic import BaseModel, Field

root_validator = None

try:
    from pydantic import model_validator

    _HAS_PYDANTIC_V2_MODEL_VALIDATOR = True
except ImportError:  # pragma: no cover - exercised only on Pydantic v1
    model_validator = None
    from pydantic import root_validator

    _HAS_PYDANTIC_V2_MODEL_VALIDATOR = False

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# Repo root is one level above the crucible/ package directory.
# All saved_projects/ paths must resolve relative to this root so that
# output lands in crucible/saved_projects/ regardless of which
# sub-module __file__ is used as the anchor.
_REPO_ROOT: str = os.path.dirname(PROJECT_ROOT)
DEFAULT_ENV_FILE_NAME = ".env"
OPENROUTER_API_BASE_URL = "https://openrouter.ai/api/v1"
ALIBABA_CODING_PLAN_API_BASE_URL = "https://coding-intl.dashscope.aliyuncs.com/v1"
LLM_PROVIDER_OPENROUTER = "openrouter"
LLM_PROVIDER_ALIBABA_CODING_PLAN = "alibaba_coding_plan"
LLM_PROVIDER_OLLAMA = "ollama"
DEFAULT_LLM_PROVIDER = LLM_PROVIDER_OPENROUTER
SUPPORTED_LLM_PROVIDERS: Tuple[str, ...] = (
    LLM_PROVIDER_OPENROUTER,
    LLM_PROVIDER_ALIBABA_CODING_PLAN,
    LLM_PROVIDER_OLLAMA,
)
OLLAMA_BASE_URL = os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434/v1")
DEFAULT_OLLAMA_PRIMARY_MODEL_ID = os.environ.get("OLLAMA_PRIMARY_MODEL", "llama3.2")
DEFAULT_PRIMARY_MODEL_ID = "openai/gpt-4o-mini"
DEFAULT_DIRECTION_JUDGE_MODEL_ID = "openai/gpt-5.4"
DEFAULT_LIBRARIAN_MODEL_ID = "minimax/minimax-m2.5"
DEFAULT_ALIBABA_CODING_PLAN_PRIMARY_MODEL_ID = "qwen3.5-plus"
DEFAULT_ALIBABA_CODING_PLAN_DIRECTION_JUDGE_MODEL_ID = "glm-5"
DEFAULT_ALIBABA_CODING_PLAN_LIBRARIAN_MODEL_ID = "minimax-m2.5"

OPENROUTER_MODEL_PRICING: Dict[str, Tuple[float, float]] = {
    "openai/gpt-4o": (2.50 / 1_000_000, 10.00 / 1_000_000),
    "openai/gpt-4o-mini": (0.15 / 1_000_000, 0.60 / 1_000_000),
    "openai/gpt-4-turbo": (10.00 / 1_000_000, 30.00 / 1_000_000),
    "openai/gpt-4": (30.00 / 1_000_000, 60.00 / 1_000_000),
    "openai/gpt-3.5-turbo": (0.50 / 1_000_000, 1.50 / 1_000_000),
    "openai/gpt-5.4": (2.50 / 1_000_000, 15.00 / 1_000_000),
    "anthropic/claude-3.5-sonnet": (3.00 / 1_000_000, 15.00 / 1_000_000),
    "anthropic/claude-3-opus": (15.00 / 1_000_000, 75.00 / 1_000_000),
    "anthropic/claude-3-haiku": (0.25 / 1_000_000, 1.25 / 1_000_000),
    "google/gemini-pro": (0.50 / 1_000_000, 1.50 / 1_000_000),
    "google/gemini-1.5-pro": (3.50 / 1_000_000, 10.50 / 1_000_000),
    "meta-llama/llama-3-70b-instruct": (0.90 / 1_000_000, 0.90 / 1_000_000),
    "meta-llama/llama-3-8b-instruct": (0.06 / 1_000_000, 0.06 / 1_000_000),
    "z-ai/glm-4": (0.10 / 1_000_000, 0.10 / 1_000_000),
    "z-ai/glm-5": (0.72 / 1_000_000, 2.30 / 1_000_000),
    "z-ai/glm-5.1": (0.72 / 1_000_000, 2.30 / 1_000_000),
    "minimax/minimax-m2.5": (0.20 / 1_000_000, 1.17 / 1_000_000),
    "deepseek/deepseek-chat": (0.14 / 1_000_000, 0.28 / 1_000_000),
    "deepseek/deepseek-coder": (0.14 / 1_000_000, 0.28 / 1_000_000),
    "mistralai/mistral-large": (2.00 / 1_000_000, 6.00 / 1_000_000),
    "mistralai/mistral-medium": (2.70 / 1_000_000, 8.10 / 1_000_000),
    "mistralai/mistral-small": (0.20 / 1_000_000, 0.60 / 1_000_000),
}
DEFAULT_MODEL_PRICING = (1.00 / 1_000_000, 3.00 / 1_000_000)
OPENROUTER_MODEL_ALIASES: Dict[str, str] = {
    "gpt-5.4": "openai/gpt-5.4",
    "openai/gpt-5.4": "openai/gpt-5.4",
    "gpt-5.4-pro": "openai/gpt-5.4-pro",
    "openai/gpt-5.4-pro": "openai/gpt-5.4-pro",
    "glm-5": "z-ai/glm-5",
    "z-ai/glm-5": "z-ai/glm-5",
    "glm-5.1": "z-ai/glm-5.1",
    "z-ai/glm-5.1": "z-ai/glm-5.1",
    "minimax-m2.5": "minimax/minimax-m2.5",
    "minimax/minimax-m2.5": "minimax/minimax-m2.5",
}
PRIMARY_MODEL_ENV_KEYS: Tuple[str, ...] = (
    "OPENROUTER_PRIMARY_MODEL",
    "PRIMARY_MODEL",
    "OPENROUTER_MODEL",
    "OPENAI_MODEL",
)
DIRECTION_JUDGE_MODEL_ENV_KEYS: Tuple[str, ...] = (
    "OPENROUTER_DIRECTION_JUDGE_MODEL",
    "DIRECTION_JUDGE_MODEL",
    "OPENROUTER_MODEL",
    "OPENAI_MODEL",
)
LIBRARIAN_MODEL_ENV_KEYS: Tuple[str, ...] = (
    "OPENROUTER_LIBRARIAN_MODEL",
    "LIBRARIAN_MODEL",
    "RESEARCH_MODEL",
)


def _normalize_llm_provider(provider: Any) -> str:
    cleaned = str(provider or "").strip().lower()
    aliases = {
        "": DEFAULT_LLM_PROVIDER,
        "default": DEFAULT_LLM_PROVIDER,
        "openrouter": LLM_PROVIDER_OPENROUTER,
        "open-router": LLM_PROVIDER_OPENROUTER,
        "alibaba": LLM_PROVIDER_ALIBABA_CODING_PLAN,
        "alibaba_coding_plan": LLM_PROVIDER_ALIBABA_CODING_PLAN,
        "alibaba-coding-plan": LLM_PROVIDER_ALIBABA_CODING_PLAN,
        "coding_plan": LLM_PROVIDER_ALIBABA_CODING_PLAN,
        "coding-plan": LLM_PROVIDER_ALIBABA_CODING_PLAN,
        "ollama": LLM_PROVIDER_OLLAMA,
    }
    normalized = aliases.get(cleaned)
    if normalized:
        return normalized
    return DEFAULT_LLM_PROVIDER


def _llm_provider_label(provider: Any) -> str:
    normalized = _normalize_llm_provider(provider)
    if normalized == LLM_PROVIDER_ALIBABA_CODING_PLAN:
        return "Alibaba Coding Plan"
    if normalized == LLM_PROVIDER_OLLAMA:
        return "Ollama"
    return "OpenRouter"


ACTIVE_LLM_PROVIDER = _normalize_llm_provider(os.environ.get("LLM_PROVIDER"))


def _parse_env_assignment(raw_line: str) -> Optional[Tuple[str, str]]:
    line = raw_line.strip()
    if not line or line.startswith("#"):
        return None
    if line.lower().startswith("export "):
        line = line[7:].lstrip()
    if "=" not in line:
        return None
    key, raw_value = line.split("=", 1)
    key = key.strip()
    if not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", key):
        return None
    value = raw_value.strip()
    if value and value[0] in ("'", '"'):
        quote = value[0]
        if len(value) >= 2 and value.endswith(quote):
            value = value[1:-1]
        else:
            value = value[1:]
        if quote == '"':
            value = value.replace(r"\n", "\n").replace(r"\r", "\r").replace(r"\t", "\t")
    else:
        value = re.split(r"\s+#", value, maxsplit=1)[0].rstrip()
    return key, value


def _load_env_file(path: str, *, override: bool = False) -> int:
    loaded = 0
    with open(path, "r", encoding="utf-8") as env_file:
        for raw_line in env_file:
            parsed = _parse_env_assignment(raw_line)
            if parsed is None:
                continue
            key, value = parsed
            if override or key not in os.environ:
                os.environ[key] = value
                loaded += 1
    return loaded


def _resolve_env_file_path() -> Optional[str]:
    configured_path = (os.environ.get("CRUCIBLE_ENV_FILE") or "").strip()
    candidates: List[str] = []
    if configured_path:
        candidate = configured_path
        if not os.path.isabs(candidate):
            candidate = os.path.join(PROJECT_ROOT, candidate)
        candidates.append(candidate)
    candidates.append(os.path.join(PROJECT_ROOT, DEFAULT_ENV_FILE_NAME))
    cwd = os.getcwd()
    if cwd:
        candidates.append(os.path.join(cwd, DEFAULT_ENV_FILE_NAME))
    seen: Set[str] = set()
    for candidate in candidates:
        normalized = os.path.normcase(os.path.abspath(candidate))
        if normalized in seen:
            continue
        seen.add(normalized)
        if os.path.isfile(candidate):
            return candidate
    return None


def _bootstrap_env_file() -> Optional[str]:
    env_path = _resolve_env_file_path()
    if env_path is None:
        return None
    _load_env_file(env_path, override=False)
    return env_path


LOADED_ENV_FILE = _bootstrap_env_file()


def _clear_crewai_modules() -> None:
    for module_name in list(sys.modules.keys()):
        if module_name == "crewai" or module_name.startswith("crewai."):
            sys.modules.pop(module_name, None)


def _workspace_local_crewai_fallback_root() -> str:
    module_file = os.path.abspath(__file__)
    workspace_root = os.path.dirname(os.path.dirname(os.path.dirname(module_file)))
    return os.path.realpath(os.path.join(workspace_root, ".crewai_local_appdata"))


def _enable_local_crewai_storage_fallback() -> str:
    fallback_root = _workspace_local_crewai_fallback_root()
    os.makedirs(fallback_root, exist_ok=True)
    os.environ["LOCALAPPDATA"] = fallback_root
    os.environ["APPDATA"] = fallback_root
    workspace_root = os.path.dirname(fallback_root)
    project_dir_name = os.path.basename(workspace_root)
    if project_dir_name:
        os.environ["CREWAI_STORAGE_DIR"] = project_dir_name

    def _local_base_dir(
        *, appname: Optional[str], appauthor: Optional[str], version: Optional[str]
    ) -> str:
        parts = [fallback_root]
        if appauthor not in (None, False, ""):
            parts.append(str(appauthor))
        if appname:
            parts.append(str(appname))
        if version:
            parts.append(str(version))
        path = os.path.join(*parts)
        os.makedirs(path, exist_ok=True)
        return path

    def _local_user_data_dir(
        appname: Optional[str] = None,
        appauthor: Optional[str] = None,
        version: Optional[str] = None,
        roaming: bool = False,
    ) -> str:
        del roaming
        return _local_base_dir(appname=appname, appauthor=appauthor, version=version)

    def _local_site_data_dir(
        appname: Optional[str] = None,
        appauthor: Optional[str] = None,
        version: Optional[str] = None,
        multipath: bool = False,
    ) -> str:
        del multipath
        return _local_base_dir(appname=appname, appauthor=appauthor, version=version)

    def _local_user_config_dir(
        appname: Optional[str] = None,
        appauthor: Optional[str] = None,
        version: Optional[str] = None,
        roaming: bool = False,
    ) -> str:
        del roaming
        return _local_base_dir(appname=appname, appauthor=appauthor, version=version)

    def _local_site_config_dir(
        appname: Optional[str] = None,
        appauthor: Optional[str] = None,
        version: Optional[str] = None,
        multipath: bool = False,
    ) -> str:
        del multipath
        return _local_base_dir(appname=appname, appauthor=appauthor, version=version)

    def _local_user_cache_dir(
        appname: Optional[str] = None,
        appauthor: Optional[str] = None,
        version: Optional[str] = None,
        opinion: bool = True,
    ) -> str:
        base_path = _local_base_dir(appname=appname, appauthor=appauthor, version=version)
        if opinion:
            cache_path = os.path.join(base_path, "Cache")
            os.makedirs(cache_path, exist_ok=True)
            return cache_path
        return base_path

    def _local_user_state_dir(
        appname: Optional[str] = None,
        appauthor: Optional[str] = None,
        version: Optional[str] = None,
        roaming: bool = False,
    ) -> str:
        del roaming
        return _local_base_dir(appname=appname, appauthor=appauthor, version=version)

    def _local_log_dir(
        appname: Optional[str] = None,
        appauthor: Optional[str] = None,
        version: Optional[str] = None,
        opinion: bool = True,
    ) -> str:
        path = _local_base_dir(appname=appname, appauthor=appauthor, version=version)
        if opinion:
            path = os.path.join(path, "Logs")
            os.makedirs(path, exist_ok=True)
        return path

    try:
        import appdirs

        appdirs.user_data_dir = _local_user_data_dir
        appdirs.site_data_dir = _local_site_data_dir
        appdirs.user_config_dir = _local_user_config_dir
        appdirs.site_config_dir = _local_site_config_dir
        appdirs.user_cache_dir = _local_user_cache_dir
        appdirs.user_state_dir = _local_user_state_dir
        appdirs.user_log_dir = _local_log_dir
    except Exception:
        pass
    return fallback_root


try:
    from crewai import Agent, Task, Crew, Process, LLM
except PermissionError:
    fallback_root = _enable_local_crewai_storage_fallback()
    _clear_crewai_modules()
    print(
        "[Warn] CrewAI default storage path is not writable; "
        f"falling back to local storage at {fallback_root}.",
        file=sys.stderr,
    )
    from crewai import Agent, Task, Crew, Process, LLM

# =========================
# 0) Configuration & Setup
# =========================


def to_json_str(obj: Any) -> str:
    import json

    # Pydantic v2
    if hasattr(obj, "model_dump_json"):
        return obj.model_dump_json(indent=2)  # v2 不吃 ensure_ascii
    # Pydantic v1
    if hasattr(obj, "json"):
        try:
            # v1 可以用 ensure_ascii，但為了兼容也可不用
            return obj.json(indent=2, ensure_ascii=False)
        except TypeError:
            # Fallback if .json() doesn't support arguments or is not Pydantic
            pass
    # dict / other
    return json.dumps(obj, indent=2, ensure_ascii=False, default=str)


def _model_to_dict(obj: Any) -> Dict[str, Any]:
    if obj is None:
        return {}
    if hasattr(obj, "model_dump"):
        try:
            return obj.model_dump()
        except Exception:
            pass
    if hasattr(obj, "dict"):
        try:
            return obj.dict()
        except Exception:
            pass
    if isinstance(obj, dict):
        return dict(obj)
    try:
        return json.loads(to_json_str(obj))
    except Exception:
        return {}


def _model_to_stable_json(obj: Any) -> str:
    payload = _model_to_dict(obj)
    try:
        return json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )
    except Exception:
        return "{}"


def _model_validate_json_compat(model_cls: Any, raw_json: str) -> Any:
    if model_cls is None:
        return None
    if hasattr(model_cls, "model_validate_json"):
        try:
            return model_cls.model_validate_json(raw_json)
        except Exception:
            pass
    try:
        payload = json.loads(raw_json)
    except Exception:
        return None
    if hasattr(model_cls, "model_validate"):
        try:
            return model_cls.model_validate(payload)
        except Exception:
            pass
    if hasattr(model_cls, "parse_obj"):
        try:
            return model_cls.parse_obj(payload)
        except Exception:
            pass
    return None


def _model_copy_compat(model_obj: Any, *, update: Optional[Dict[str, Any]] = None) -> Any:
    update = dict(update or {})
    if hasattr(model_obj, "model_copy"):
        try:
            return model_obj.model_copy(update=update)
        except Exception:
            pass
    if hasattr(model_obj, "copy"):
        try:
            return model_obj.copy(update=update)
        except Exception:
            pass
    payload = _model_to_dict(model_obj)
    payload.update(update)
    model_cls = model_obj.__class__
    if hasattr(model_cls, "model_validate"):
        try:
            return model_cls.model_validate(payload)
        except Exception:
            pass
    if hasattr(model_cls, "parse_obj"):
        try:
            return model_cls.parse_obj(payload)
        except Exception:
            pass
    return model_obj


_ALLOWED_LITERAL_TEMPLATE_FIELDS: Set[str] = {
    "goal, criteria",
    "severity, category, description, file, suggestion",
    "provider,title,url,snippet,query",
    "provider,title,url,snippet,query,source_domain,snippet_hash,verification_status",
}


class _SafeTemplateVars(dict):
    def __missing__(self, key: str) -> str:
        return "{" + str(key) + "}"


def _validate_prompt_template_fields(
    template: str, template_vars: Optional[Dict[str, Any]] = None
) -> None:
    formatter = string.Formatter()
    known_fields = set((template_vars or {}).keys())
    for _, field_name, _, _ in formatter.parse(str(template or "")):
        if not field_name:
            continue
        normalized = str(field_name).strip()
        if normalized in known_fields:
            continue
        if normalized in _ALLOWED_LITERAL_TEMPLATE_FIELDS:
            continue
        raise KeyError(f"Unknown prompt template field: {normalized}")


def _render_prompt_template(template: str, template_vars: Optional[Dict[str, Any]] = None) -> str:
    template_text = str(template or "")
    _validate_prompt_template_fields(template_text, template_vars)
    return template_text.format_map(_SafeTemplateVars(dict(template_vars or {})))


JSON_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL | re.IGNORECASE)


def _parse_json_candidate(candidate: str) -> Optional[dict]:
    try:
        obj = json.loads(candidate)
    except Exception:
        return None
    return obj if isinstance(obj, dict) else None


def _extract_balanced_json_object(text: str) -> Optional[str]:
    in_string = False
    escape = False
    depth = 0
    start = None
    for idx, ch in enumerate(text):
        if in_string:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
            continue
        if ch == "{":
            if depth == 0:
                start = idx
            depth += 1
            continue
        if ch == "}" and depth > 0:
            depth -= 1
            if depth == 0 and start is not None:
                return text[start : idx + 1]
    return None


def _extract_first_json_object(text: str) -> Optional[dict]:
    """Best-effort: extract first JSON object from a string."""
    if not text:
        return None
    s = text.strip()
    if s.startswith("{") and s.endswith("}"):
        obj = _parse_json_candidate(s)
        if obj is not None:
            return obj
    for match in JSON_FENCE_RE.finditer(s):
        candidate = match.group(1).strip()
        obj = _parse_json_candidate(candidate)
        if obj is not None:
            return obj
        nested = _extract_balanced_json_object(candidate)
        if nested:
            obj = _parse_json_candidate(nested)
            if obj is not None:
                return obj
    candidate = _extract_balanced_json_object(s)
    if not candidate:
        return None
    return _parse_json_candidate(candidate)


def contains_cjk(text: str) -> bool:
    return any("\u4e00" <= ch <= "\u9fff" for ch in text)


MULTILINE_INPUT_TERMINATOR = "__END_PROMPT__"


def _strip_surrogates(text: str) -> str:
    """Replace lone UTF-16 surrogate code points with U+FFFD.

    Windows stdin uses the ``surrogateescape`` error handler, which can inject
    lone surrogate characters (U+D800–U+DFFF) into strings when the console
    encoding cannot represent a byte sequence.  These code points are illegal
    in UTF-8 and cause ``UnicodeEncodeError`` when CrewAI calls
    ``task_description.encode('utf-8')`` to compute its MD5 task key.
    """
    return text.encode("utf-8", errors="replace").decode("utf-8")


def read_multiline_input(
    prompt_title: str,
    *,
    required: bool,
    max_chars: Optional[int] = None,
) -> str:
    """
    Read a multi-line text block from stdin until the operator enters the
    terminator line. This preserves paragraph breaks and prevents pasted
    multi-line content from being mis-consumed by later interactive prompts.
    """
    print(f"\n{prompt_title}")
    print(
        "Paste multi-line text below. Blank lines are preserved.\n"
        f"Finish with a line containing only {MULTILINE_INPUT_TERMINATOR}.\n"
        "For optional input, press Enter on the first empty line to skip."
    )
    lines: List[str] = []
    first_line = True

    while True:
        line = sys.stdin.readline()
        if line == "":
            break
        normalized = _strip_surrogates(line.rstrip("\r\n"))
        if normalized == MULTILINE_INPUT_TERMINATOR:
            break
        if first_line and not required and normalized == "":
            return ""
        lines.append(normalized)
        first_line = False

    text = "\n".join(lines)
    if max_chars is not None and len(text) > max_chars:
        raise ValueError(f"Input exceeds the configured limit ({max_chars} characters).")
    if required and not text.strip():
        raise ValueError("Empty input.")
    return text


def fmt_limit(value: Optional[int]) -> str:
    return "unlimited" if value is None else str(value)


def _env_int(name: str, default: Optional[int]) -> Optional[int]:
    raw = os.environ.get(name)
    if raw is None:
        return default
    raw = raw.strip()
    if not raw:
        return default
    if raw.lower() in ("none", "null", "unlimited", "inf", "infinite"):
        return None
    try:
        return int(raw)
    except ValueError:
        return default


def _env_float(name: str, default: Optional[float]) -> Optional[float]:
    raw = os.environ.get(name)
    if raw is None:
        return default
    raw = raw.strip()
    if not raw:
        return default
    if raw.lower() in ("none", "null", "unlimited", "inf", "infinite"):
        return None
    try:
        return float(raw)
    except ValueError:
        return default


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    raw = raw.strip().lower()
    if raw in ("1", "true", "yes", "y", "on"):
        return True
    if raw in ("0", "false", "no", "n", "off"):
        return False
    return default


def _resolve_env_setting(
    names: Tuple[str, ...],
    *,
    default: Optional[str] = None,
    ignore_placeholders: bool = False,
) -> Tuple[Optional[str], Optional[str]]:
    for name in names:
        raw = os.environ.get(name)
        if raw is None:
            continue
        value = str(raw).strip()
        if not value:
            continue
        if ignore_placeholders and value.lower().startswith("replace_with"):
            continue
        return value, name
    return default, None


OPENCODE_LIBRARIAN_DEFAULT_PROVIDERS: List[str] = [
    "websearch",
    "context7",
    "grep_app",
    "github",
    "arxiv",
    "paperswithcode",
]


LIBRARIAN_PROVIDER_ALIASES: Dict[str, str] = {
    "web": "websearch",
    "websearch": "websearch",
    "exa": "websearch",
    "exaweb": "websearch",
    "context7": "context7",
    "docs": "context7",
    "grep": "grep_app",
    "grepapp": "grep_app",
    "grep_app": "grep_app",
    "codesearch": "grep_app",
    "github": "github",
    "githubsearch": "github",
    "arxiv": "arxiv",
    "paper": "paperswithcode",
    "paperswithcode": "paperswithcode",
    "paperswith": "paperswithcode",
    "pwc": "paperswithcode",
}


def _normalize_librarian_provider_names(values: Any) -> List[str]:
    if values is None:
        raw_items: List[str] = []
    elif isinstance(values, str):
        raw_items = re.split(r"[\s,;+|]+", values)
    else:
        raw_items = [str(v) for v in list(values)]
    normalized: List[str] = []
    for item in raw_items:
        key = re.sub(r"[^a-z0-9]", "", (item or "").strip().lower())
        if key in ("opencode", "ohmyopencode", "librarian"):
            for provider in OPENCODE_LIBRARIAN_DEFAULT_PROVIDERS:
                if provider not in normalized:
                    normalized.append(provider)
            continue
        mapped = LIBRARIAN_PROVIDER_ALIASES.get(key)
        if mapped and mapped not in normalized:
            normalized.append(mapped)
    return normalized


def limit_text(text: str, max_chars: Optional[int]) -> str:
    if max_chars is None or len(text) <= max_chars:
        return text
    return text[:max_chars] + "\n...[truncated]..."


def _truncate_text_preserve_lines(text: str, max_chars: int) -> Tuple[str, bool]:
    """
    Trim text to budget while preferring line boundaries to reduce broken snippets.
    """
    if max_chars <= 0:
        return "", bool(text)
    if len(text) <= max_chars:
        return text, False
    cut = text[:max_chars]
    # Keep at least ~60% of budget before snapping to previous newline.
    min_keep = max(1, int(max_chars * 0.6))
    nl = cut.rfind("\n")
    if nl >= min_keep:
        cut = cut[:nl]
    return cut, True


_OPENROUTER_USAGE_CONTEXT: contextvars.ContextVar[Dict[str, Any]] = contextvars.ContextVar(
    "openrouter_usage_context", default=None  # type: ignore[assignment]
)

_OPENROUTER_USAGE_RECORDS: contextvars.ContextVar[List[Dict[str, Any]]] = contextvars.ContextVar(
    "openrouter_usage_records", default=None  # type: ignore[assignment]
)


@dataclass
class OpenRouterUsageData:
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    cached_tokens: int = 0
    reasoning_tokens: int = 0
    input_cost_usd: float = 0.0
    output_cost_usd: float = 0.0
    cache_cost_usd: float = 0.0
    total_cost_usd: float = 0.0
    model_id: str = ""
    cost_source: str = "estimated"
    llm_provider: str = DEFAULT_LLM_PROVIDER


def get_last_openrouter_usage() -> OpenRouterUsageData:
    ctx = _OPENROUTER_USAGE_CONTEXT.get({})
    return OpenRouterUsageData(
        input_tokens=ctx.get("input_tokens", 0),
        output_tokens=ctx.get("output_tokens", 0),
        total_tokens=ctx.get("total_tokens", 0),
        cached_tokens=ctx.get("cached_tokens", 0),
        reasoning_tokens=ctx.get("reasoning_tokens", 0),
        input_cost_usd=ctx.get("input_cost_usd", 0.0),
        output_cost_usd=ctx.get("output_cost_usd", 0.0),
        cache_cost_usd=ctx.get("cache_cost_usd", 0.0),
        total_cost_usd=ctx.get("total_cost_usd", 0.0),
        model_id=ctx.get("model_id", ""),
        cost_source=ctx.get("cost_source", "estimated"),
        llm_provider=_resolve_usage_provider(ctx.get("llm_provider")),
    )


def _normalize_openrouter_usage_payload(usage: Any) -> Optional[Dict[str, Any]]:
    if not isinstance(usage, dict):
        return None

    normalized = dict(usage)

    if "prompt_tokens" not in normalized and "input_tokens" in normalized:
        normalized["prompt_tokens"] = normalized.get("input_tokens", 0)
    if "completion_tokens" not in normalized and "output_tokens" in normalized:
        normalized["completion_tokens"] = normalized.get("output_tokens", 0)
    if "total_tokens" not in normalized:
        normalized["total_tokens"] = (
            (normalized.get("prompt_tokens", 0) or 0)
            + (normalized.get("completion_tokens", 0) or 0)
        )

    if (
        "prompt_tokens_details" not in normalized
        and isinstance(normalized.get("input_tokens_details"), dict)
    ):
        normalized["prompt_tokens_details"] = dict(normalized["input_tokens_details"])
    if (
        "completion_tokens_details" not in normalized
        and isinstance(normalized.get("output_tokens_details"), dict)
    ):
        normalized["completion_tokens_details"] = dict(normalized["output_tokens_details"])

    if "cached_prompt_tokens" in normalized:
        prompt_details = dict(normalized.get("prompt_tokens_details") or {})
        prompt_details.setdefault(
            "cached_tokens",
            normalized.get("cached_prompt_tokens", 0) or 0,
        )
        normalized["prompt_tokens_details"] = prompt_details

    # Use None-sentinel defaults (not 0) so the any(v is not None) guard below
    # correctly returns None for payloads that contain none of these keys.
    # Using get(key, 0) would make every field non-None and the guard unreachable.
    token_fields = (
        normalized.get("prompt_tokens"),
        normalized.get("completion_tokens"),
        normalized.get("total_tokens"),
    )
    cost_details = normalized.get("cost_details") or {}
    cost_fields = (
        normalized.get("cost"),
        cost_details.get("upstream_inference_prompt_cost")
        if isinstance(cost_details, dict)
        else None,
        cost_details.get("upstream_inference_completions_cost")
        if isinstance(cost_details, dict)
        else None,
    )
    if not any(v is not None for v in token_fields) and not any(v is not None for v in cost_fields):
        return None

    return normalized


def _canonicalize_model_id(model_id: str) -> str:
    cleaned = str(model_id or "").strip().lower()
    if not cleaned:
        return ""
    if cleaned.startswith("openrouter/"):
        cleaned = cleaned[len("openrouter/") :]
    cleaned = cleaned.strip("/")
    direct = OPENROUTER_MODEL_ALIASES.get(cleaned)
    if direct:
        return direct
    for alias, canonical in OPENROUTER_MODEL_ALIASES.items():
        if cleaned.startswith(alias + "-") or cleaned.startswith(alias + ":"):
            return canonical
    return cleaned


def _iter_model_id_candidates(model_id: str) -> List[str]:
    if not model_id:
        return []
    candidates: List[str] = []
    for raw_part in re.split(r"[\s,|]+", str(model_id or "")):
        cleaned = str(raw_part or "").strip()
        if not cleaned:
            continue
        for candidate in (cleaned.lower(), _canonicalize_model_id(cleaned)):
            if candidate and candidate not in candidates:
                candidates.append(candidate)
    return candidates


def _estimate_cache_savings(
    *,
    prompt_tokens: int,
    cached_tokens: int,
    input_cost: float,
    input_price: float = 0.0,
) -> float:
    if cached_tokens <= 0:
        return 0.0
    # Guard against IEEE 754 subnormal floats (e.g. 5e-324):
    # `> 0.0` would let them through and the resulting division produces
    # ~1e+300 phantom prices.  `not (x > 1e-14)` rejects subnormals safely.
    if (float(prompt_tokens) > 1e-14) and (float(input_cost) > 1e-14):
        effective_prompt_price = float(input_cost) / float(prompt_tokens)
        return float(cached_tokens) * effective_prompt_price * 0.5
    if float(input_price) > 1e-14:
        return float(cached_tokens) * float(input_price) * 0.5
    return 0.0


_USAGE_COST_SOURCE_PRIORITY: Dict[str, int] = {
    "estimated": 0,
    "alibaba_coding_plan_tokens_only": 1,
    "crewai_metrics_with_pricing": 2,
    "openrouter_tokens_with_pricing": 3,
    "openrouter_api": 4,
}


def _merge_usage_cost_source(existing_source: str, new_source: str) -> str:
    existing = str(existing_source or "").strip() or "estimated"
    new = str(new_source or "").strip() or "estimated"
    if _USAGE_COST_SOURCE_PRIORITY.get(new, 0) >= _USAGE_COST_SOURCE_PRIORITY.get(existing, 0):
        return new
    return existing


def _merge_usage_model_ids(existing_model_id: str, new_model_id: str) -> str:
    merged: List[str] = []
    seen: Set[str] = set()

    for raw_value in (existing_model_id, new_model_id):
        for token in str(raw_value or "").split(","):
            cleaned = token.strip()
            if not cleaned or cleaned in seen:
                continue
            seen.add(cleaned)
            merged.append(cleaned)

    return ",".join(merged)


def _resolve_usage_provider(provider: Optional[str] = None) -> str:
    if provider is not None and str(provider or "").strip():
        return _normalize_llm_provider(provider)
    active_provider = globals().get("ACTIVE_LLM_PROVIDER")
    if isinstance(active_provider, str) and active_provider.strip():
        return _normalize_llm_provider(active_provider)
    env_provider = str(os.environ.get("LLM_PROVIDER") or "").strip()
    if env_provider:
        return _normalize_llm_provider(env_provider)
    return _normalize_llm_provider(os.environ.get("LLM_PROVIDER"))


def _usage_context_matches_provider(existing: Optional[Dict[str, Any]], provider: str) -> bool:
    if not isinstance(existing, dict) or not existing:
        return False
    existing_provider = str(existing.get("llm_provider") or "").strip()
    if not existing_provider:
        return True
    return _normalize_llm_provider(existing_provider) == _normalize_llm_provider(provider)


def _canonicalize_usage_model_id(model_id: str, provider: Optional[str] = None) -> str:
    resolved_provider = _resolve_usage_provider(provider)
    cleaned = str(model_id or "").strip()
    if not cleaned:
        return ""
    if resolved_provider == LLM_PROVIDER_OPENROUTER:
        return _canonicalize_model_id(cleaned)
    return cleaned


def _host_to_usage_provider(host: str) -> Optional[str]:
    normalized_host = str(host or "").strip().lower()
    if not normalized_host:
        return None
    if "openrouter.ai" in normalized_host:
        return LLM_PROVIDER_OPENROUTER
    if "coding-intl.dashscope.aliyuncs.com" in normalized_host:
        return LLM_PROVIDER_ALIBABA_CODING_PLAN
    return None


def _capture_openrouter_usage_from_http_response(response: Any) -> bool:
    try:
        if response is None or getattr(response, "status_code", 0) < 200:
            return False
        if getattr(response, "status_code", 0) >= 400:
            return False

        request = getattr(response, "request", None)
        request_url = getattr(request, "url", None)
        provider = None
        if request_url is not None:
            host = (getattr(request_url, "host", "") or "").lower()
            provider = _host_to_usage_provider(host)
            if host and provider is None:
                return False

        content_type = ""
        headers = getattr(response, "headers", None)
        if headers is not None:
            try:
                content_type = str(headers.get("content-type", "") or "").lower()
            except Exception:
                content_type = ""
        if content_type and "json" not in content_type:
            return False

        payload = response.json()
        if not isinstance(payload, dict):
            return False

        usage = _normalize_openrouter_usage_payload(payload.get("usage"))
        if usage is None:
            return False

        model_id = str(payload.get("model", "") or "")
        set_openrouter_usage(usage, model_id=model_id, accumulate=True, provider=provider)
        return True
    except Exception:
        return False


def set_openrouter_usage(
    usage: Dict[str, Any],
    model_id: str = "",
    accumulate: bool = True,
    provider: Optional[str] = None,
) -> None:
    normalized_usage = _normalize_openrouter_usage_payload(usage)
    if normalized_usage is None:
        return
    usage = normalized_usage
    resolved_provider = _resolve_usage_provider(provider)
    model_id = _canonicalize_usage_model_id(model_id, resolved_provider)

    prompt_tokens = usage.get("prompt_tokens", 0) or 0
    completion_tokens = usage.get("completion_tokens", 0) or 0
    # Use explicit None-check instead of falsy `or`: when the API legitimately
    # returns total_tokens=0 (e.g. a failed/empty call), `0 or sum` would inflate
    # the token count and cause phantom cost records to be created downstream.
    _raw_total = usage.get("total_tokens")
    total_tokens = (
        _raw_total if _raw_total is not None
        else (prompt_tokens + completion_tokens)
    )

    prompt_details = usage.get("prompt_tokens_details") or {}
    completion_details = usage.get("completion_tokens_details") or {}

    cached_tokens = prompt_details.get("cached_tokens", 0) or 0
    reasoning_tokens = completion_details.get("reasoning_tokens", 0) or 0

    total_cost = usage.get("cost", 0.0) or 0.0

    cost_details = usage.get("cost_details") or {}
    input_cost = cost_details.get("upstream_inference_prompt_cost", 0.0) or 0.0
    output_cost = cost_details.get("upstream_inference_completions_cost", 0.0) or 0.0

    if (
        resolved_provider == LLM_PROVIDER_OPENROUTER
        and total_cost > 0
        and input_cost == 0
        and output_cost == 0
    ):
        if prompt_tokens > 0 and completion_tokens > 0:
            total_t = prompt_tokens + completion_tokens
            input_cost = total_cost * (prompt_tokens / total_t) if total_t > 0 else 0
            output_cost = total_cost - input_cost
        elif prompt_tokens > 0:
            input_cost = total_cost
        elif completion_tokens > 0:
            output_cost = total_cost
        else:
            # No token breakdown — split evenly rather than silently assigning
            # all cost to output, which would produce incorrect cost attribution.
            input_cost = total_cost / 2.0
            output_cost = total_cost / 2.0

    if resolved_provider == LLM_PROVIDER_ALIBABA_CODING_PLAN:
        total_cost = 0.0
        input_cost = 0.0
        output_cost = 0.0
        cost_source = "alibaba_coding_plan_tokens_only"
    else:
        cost_source = "openrouter_api" if total_cost > 0 else "estimated"
    if (
        resolved_provider == LLM_PROVIDER_OPENROUTER
        and total_cost <= 0
        and total_tokens > 0
        and model_id
    ):
        input_price, output_price = _get_model_pricing(model_id)
        if input_price > 0 or output_price > 0:
            estimated_input_cost = prompt_tokens * input_price
            estimated_output_cost = completion_tokens * output_price
            estimated_cache_savings = _estimate_cache_savings(
                prompt_tokens=int(prompt_tokens),
                cached_tokens=int(cached_tokens),
                input_cost=float(estimated_input_cost),
                input_price=float(input_price),
            )
            total_cost = max(
                0.0,
                float(estimated_input_cost + estimated_output_cost - estimated_cache_savings),
            )
            input_cost = float(estimated_input_cost)
            output_cost = float(estimated_output_cost)
            cost_source = "openrouter_tokens_with_pricing"

    cache_cost = _estimate_cache_savings(
        prompt_tokens=int(prompt_tokens),
        cached_tokens=int(cached_tokens),
        input_cost=float(input_cost),
    )
    if resolved_provider == LLM_PROVIDER_ALIBABA_CODING_PLAN:
        cache_cost = 0.0

    this_call_data = {
        "input_tokens": int(prompt_tokens),
        "output_tokens": int(completion_tokens),
        "total_tokens": int(total_tokens),
        "cached_tokens": int(cached_tokens),
        "reasoning_tokens": int(reasoning_tokens),
        "input_cost_usd": float(input_cost),
        "output_cost_usd": float(output_cost),
        "cache_cost_usd": float(cache_cost),
        "total_cost_usd": float(total_cost),
        "model_id": str(model_id),
        "cost_source": cost_source,
        "llm_provider": resolved_provider,
    }

    records = list(_OPENROUTER_USAGE_RECORDS.get([]))
    records.append(this_call_data)
    _OPENROUTER_USAGE_RECORDS.set(records)

    existing = _OPENROUTER_USAGE_CONTEXT.get({})

    if accumulate and existing and _usage_context_matches_provider(existing, resolved_provider):
        prompt_tokens = existing.get("input_tokens", 0) + int(prompt_tokens)
        completion_tokens = existing.get("output_tokens", 0) + int(completion_tokens)
        total_tokens = existing.get("total_tokens", 0) + int(total_tokens)
        cached_tokens = existing.get("cached_tokens", 0) + int(cached_tokens)
        reasoning_tokens = existing.get("reasoning_tokens", 0) + int(reasoning_tokens)
        input_cost = existing.get("input_cost_usd", 0.0) + float(input_cost)
        output_cost = existing.get("output_cost_usd", 0.0) + float(output_cost)
        cache_cost = existing.get("cache_cost_usd", 0.0) + float(cache_cost)
        total_cost = existing.get("total_cost_usd", 0.0) + float(total_cost)

        model_id = _merge_usage_model_ids(existing.get("model_id", ""), model_id)
        cost_source = _merge_usage_cost_source(existing.get("cost_source", "estimated"), cost_source)
        resolved_provider = _resolve_usage_provider(existing.get("llm_provider") or resolved_provider)

    ctx_data = {
        "input_tokens": int(prompt_tokens),
        "output_tokens": int(completion_tokens),
        "total_tokens": int(total_tokens),
        "cached_tokens": int(cached_tokens),
        "reasoning_tokens": int(reasoning_tokens),
        "input_cost_usd": float(input_cost),
        "output_cost_usd": float(output_cost),
        "cache_cost_usd": float(cache_cost),
        "total_cost_usd": float(total_cost),
        "model_id": str(model_id),
        "cost_source": cost_source,
        "llm_provider": resolved_provider,
    }
    _OPENROUTER_USAGE_CONTEXT.set(ctx_data)


def get_usage_records() -> List[OpenRouterUsageData]:
    records = _OPENROUTER_USAGE_RECORDS.get([])
    return [
        OpenRouterUsageData(
            input_tokens=r.get("input_tokens", 0),
            output_tokens=r.get("output_tokens", 0),
            total_tokens=r.get("total_tokens", 0),
            cached_tokens=r.get("cached_tokens", 0),
            reasoning_tokens=r.get("reasoning_tokens", 0),
            input_cost_usd=r.get("input_cost_usd", 0.0),
            output_cost_usd=r.get("output_cost_usd", 0.0),
            cache_cost_usd=r.get("cache_cost_usd", 0.0),
            total_cost_usd=r.get("total_cost_usd", 0.0),
            model_id=r.get("model_id", ""),
            cost_source=r.get("cost_source", "estimated"),
            llm_provider=_resolve_usage_provider(r.get("llm_provider")),
        )
        for r in records
    ]


def clear_openrouter_usage() -> None:
    _OPENROUTER_USAGE_CONTEXT.set({})
    _OPENROUTER_USAGE_RECORDS.set([])


def _get_model_pricing(model_id: str) -> Tuple[float, float]:
    if not model_id:
        return (0.0, 0.0)
    for candidate in _iter_model_id_candidates(model_id):
        for key, pricing in OPENROUTER_MODEL_PRICING.items():
            key_lower = key.lower()
            key_short = key.split("/")[-1].lower()
            if candidate == key_lower or candidate == key_short or candidate.endswith("/" + key_short):
                return pricing
    return (0.0, 0.0)


def extract_and_set_usage_from_crew(crew: Any, model_id: str = "") -> None:
    try:
        existing = _OPENROUTER_USAGE_CONTEXT.get({})
        resolved_provider = _resolve_usage_provider()
        if _usage_context_matches_provider(existing, resolved_provider) and existing.get("cost_source") in (
            "openrouter_api",
            "openrouter_tokens_with_pricing",
        ) and (existing.get("total_cost_usd", 0.0) or 0.0) > 0:
            return

        usage_metrics = crew.calculate_usage_metrics()
        if usage_metrics is None:
            return

        prompt_tokens = getattr(usage_metrics, "prompt_tokens", 0) or 0
        completion_tokens = getattr(usage_metrics, "completion_tokens", 0) or 0
        # Explicit None-only guard: use the API-reported total when present
        # (including total_tokens=0 for empty/no-cost responses).  The previous
        # `_raw_total > 0` condition re-introduced the falsy-zero bug — it
        # treated total_tokens=0 as absent and fell back to prompt+completion,
        # inconsistent with set_openrouter_usage which uses `is not None` only.
        _raw_total = getattr(usage_metrics, "total_tokens", None)
        total_tokens = (
            _raw_total if _raw_total is not None
            else (prompt_tokens + completion_tokens)
        )
        cached_tokens = getattr(usage_metrics, "cached_prompt_tokens", 0) or 0

        if total_tokens == 0:
            return

        model_id = _canonicalize_usage_model_id(model_id, resolved_provider)
        if resolved_provider == LLM_PROVIDER_ALIBABA_CODING_PLAN:
            _token_only_cost_source = "alibaba_coding_plan_tokens_only"
            ctx_data = {
                "input_tokens": int(prompt_tokens),
                "output_tokens": int(completion_tokens),
                "total_tokens": int(total_tokens),
                "cached_tokens": int(cached_tokens),
                "reasoning_tokens": 0,
                "input_cost_usd": 0.0,
                "output_cost_usd": 0.0,
                "cache_cost_usd": 0.0,
                "total_cost_usd": 0.0,
                "model_id": str(model_id),
                "cost_source": _token_only_cost_source,
                "llm_provider": resolved_provider,
            }
            records = list(_OPENROUTER_USAGE_RECORDS.get([]))
            records.append(dict(ctx_data))
            _OPENROUTER_USAGE_RECORDS.set(records)
            _OPENROUTER_USAGE_CONTEXT.set(ctx_data)
            return

        input_price, output_price = _get_model_pricing(model_id)
        pricing_known = input_price > 0 or output_price > 0
        input_cost = prompt_tokens * input_price if pricing_known else 0.0
        output_cost = completion_tokens * output_price if pricing_known else 0.0
        cache_savings = (
            _estimate_cache_savings(
                prompt_tokens=int(prompt_tokens),
                cached_tokens=int(cached_tokens),
                input_cost=float(input_cost),
                input_price=float(input_price),
            )
            if pricing_known
            else 0.0
        )
        total_cost = input_cost + output_cost - cache_savings if pricing_known else 0.0

        if pricing_known:
            ctx_data = {
                "input_tokens": int(prompt_tokens),
                "output_tokens": int(completion_tokens),
                "total_tokens": int(total_tokens),
                "cached_tokens": int(cached_tokens),
                "reasoning_tokens": 0,
                "input_cost_usd": float(input_cost),
                "output_cost_usd": float(output_cost),
                "cache_cost_usd": float(cache_savings),
                "total_cost_usd": float(total_cost),
                "model_id": str(model_id),
                "cost_source": "crewai_metrics_with_pricing",
                "llm_provider": resolved_provider,
            }
        else:
            ctx_data = {
                "input_tokens": int(prompt_tokens),
                "output_tokens": int(completion_tokens),
                "total_tokens": int(total_tokens),
                "cached_tokens": int(cached_tokens),
                "reasoning_tokens": 0,
                "input_cost_usd": 0.0,
                "output_cost_usd": 0.0,
                "cache_cost_usd": 0.0,
                "total_cost_usd": 0.0,
                "model_id": str(model_id),
                "cost_source": "estimated",
                "llm_provider": resolved_provider,
            }
        records = list(_OPENROUTER_USAGE_RECORDS.get([]))
        records.append(dict(ctx_data))
        _OPENROUTER_USAGE_RECORDS.set(records)
        _OPENROUTER_USAGE_CONTEXT.set(ctx_data)
    except Exception as exc:
        # Billing/usage extraction must never crash a run, but a fully silent
        # swallow makes cost-discrepancy bugs invisible.  Emit a single
        # diagnostic line so the failure shows up in logs.  Use stderr (rather
        # than runtime_logging) so this stays usable during very early bootstrap
        # before the runtime logger is wired up.
        try:
            print(
                f"[WARN] extract_and_set_usage_from_crew failed: "
                f"{type(exc).__name__}: {exc}",
                file=sys.stderr,
            )
        except Exception:
            pass


try:
    from langchain_core.callbacks import BaseCallbackHandler

    class OpenRouterUsageCallbackHandler(BaseCallbackHandler):
        def on_llm_end(self, response: Any, **kwargs: Any) -> None:
            try:
                llm_output = getattr(response, "llm_output", None) or {}
                usage = llm_output.get("token_usage", {}) if isinstance(llm_output, dict) else {}

                if not usage:
                    for attr in ("usage", "response_metadata"):
                        if hasattr(response, attr):
                            candidate = getattr(response, attr, None)
                            if isinstance(candidate, dict) and "prompt_tokens" in candidate:
                                usage = candidate
                                break

                model_id = ""
                if hasattr(response, "model"):
                    model_id = getattr(response, "model", "") or ""
                elif isinstance(llm_output, dict):
                    model_id = llm_output.get("model_name", "") or llm_output.get("model", "") or ""

                if usage:
                    set_openrouter_usage(usage, model_id)
            except Exception:
                pass

    _OPENROUTER_CALLBACK_HANDLER = OpenRouterUsageCallbackHandler()

except ImportError:

    class OpenRouterUsageCallbackHandler:
        pass

    _OPENROUTER_CALLBACK_HANDLER = None


def get_openrouter_callback_handler() -> Optional[Any]:
    return _OPENROUTER_CALLBACK_HANDLER


try:
    from crewai.llms.hooks.base import BaseInterceptor

    class OpenRouterUsageHTTPInterceptor(BaseInterceptor[httpx.Request, httpx.Response]):
        def on_outbound(self, message: httpx.Request) -> httpx.Request:
            return message

        def on_inbound(self, message: httpx.Response) -> httpx.Response:
            _capture_openrouter_usage_from_http_response(message)
            return message

        async def aon_outbound(self, message: httpx.Request) -> httpx.Request:
            return message

        async def aon_inbound(self, message: httpx.Response) -> httpx.Response:
            _capture_openrouter_usage_from_http_response(message)
            return message

    _OPENROUTER_HTTP_INTERCEPTOR = OpenRouterUsageHTTPInterceptor()

except ImportError:

    class OpenRouterUsageHTTPInterceptor:
        pass

    _OPENROUTER_HTTP_INTERCEPTOR = None


def get_openrouter_http_interceptor() -> Optional[Any]:
    return _OPENROUTER_HTTP_INTERCEPTOR


def _cost_trace(stage: str, **fields: Any) -> None:
    """Log cost trace to stderr and record to AgentCostAccountant."""
    if not COST_TRACE_ENABLED:
        return
    try:
        ts = datetime.now().isoformat(timespec="seconds")
        suffix = " ".join(f"{k}={v}" for k, v in fields.items() if v is not None)
        msg = f"[COST_TRACE] {ts} stage={stage}"
        if suffix:
            msg += " " + suffix
        print(msg, file=sys.stderr)
    except Exception:
        pass


def _record_cost(
    stage: str,
    agent_name: str,
    input_tokens: int = 0,
    output_tokens: int = 0,
    success: bool = True,
    cache_hit: bool = False,
    retry_count: int = 0,
    outcome: str = "success",
    use_openrouter_usage: bool = True,
    clear_usage_after_record: bool = True,
) -> None:
    try:
        usage_data = get_last_openrouter_usage() if use_openrouter_usage else None

        if usage_data and usage_data.total_tokens > 0:
            accountant = get_cost_accountant()
            accountant.record(
                agent_name=agent_name,
                stage=stage,
                input_tokens=usage_data.input_tokens,
                output_tokens=usage_data.output_tokens,
                success=success,
                cache_hit=usage_data.cached_tokens > 0,
                retry_count=retry_count,
                outcome=outcome,
                cached_tokens=usage_data.cached_tokens,
                reasoning_tokens=usage_data.reasoning_tokens,
                input_cost_usd=usage_data.input_cost_usd,
                output_cost_usd=usage_data.output_cost_usd,
                cache_cost_usd=usage_data.cache_cost_usd,
                total_cost_usd=usage_data.total_cost_usd,
                model_id=usage_data.model_id,
                cost_source=usage_data.cost_source,
            )
            if clear_usage_after_record:
                clear_openrouter_usage()
        else:
            accountant = get_cost_accountant()
            accountant.record(
                agent_name=agent_name,
                stage=stage,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                success=success,
                cache_hit=cache_hit,
                retry_count=retry_count,
                outcome=outcome,
            )
    except Exception:
        pass


def _coerce_json_dict(value: Any) -> Optional[dict]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        return _extract_first_json_object(value)
    try:
        return json.loads(json.dumps(value, ensure_ascii=False))
    except Exception:
        return None


def _coerce_json_text(value: Any) -> Optional[str]:
    if isinstance(value, str):
        s = value.strip()
        return s or None
    payload = _coerce_json_dict(value)
    if not isinstance(payload, dict):
        return None
    try:
        return json.dumps(payload, ensure_ascii=False)
    except Exception:
        return None


def _collect_structured_candidates_from_result(
    result: Any,
    *,
    attr_order: Tuple[str, ...] = ("json_dict", "output", "raw", "text", "content"),
) -> List[dict]:
    candidates: List[dict] = []

    def _add(value: Any) -> None:
        payload = _coerce_json_dict(value)
        if isinstance(payload, dict):
            candidates.append(payload)

    _add(result)
    if hasattr(result, "pydantic"):
        try:
            _add(getattr(result, "pydantic", None))
        except Exception:
            pass
    for attr in attr_order:
        if hasattr(result, attr):
            try:
                _add(getattr(result, attr))
            except Exception:
                pass

    task_outputs: List[Any] = []
    if hasattr(result, "tasks_output"):
        try:
            task_outputs = list(getattr(result, "tasks_output") or [])
        except Exception:
            task_outputs = []

    for task_output in task_outputs:
        _add(task_output)
        if hasattr(task_output, "pydantic"):
            try:
                _add(getattr(task_output, "pydantic", None))
            except Exception:
                pass
        for attr in attr_order:
            if not hasattr(task_output, attr):
                continue
            try:
                _add(getattr(task_output, attr))
            except Exception:
                pass

    unique: List[dict] = []
    seen: Set[str] = set()
    for payload in candidates:
        try:
            fingerprint = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
        except Exception:
            fingerprint = str(payload)
        if fingerprint in seen:
            continue
        seen.add(fingerprint)
        unique.append(payload)
    return unique


def _debug_serialize_task_output(task_output: Any, *, index: int) -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "index": index,
        "type": type(task_output).__name__,
    }
    for attr_name in ("name", "description", "expected_output"):
        try:
            raw_value = getattr(task_output, attr_name, None)
        except Exception:
            raw_value = None
        if raw_value is not None:
            payload[attr_name] = limit_text(str(raw_value), 1200)

    structured: Optional[Dict[str, Any]] = None
    for attr_name in ("json_dict", "output", "raw", "text", "content"):
        try:
            candidate = getattr(task_output, attr_name, None)
        except Exception:
            candidate = None
        structured = _coerce_json_dict(candidate)
        if structured is not None:
            break
    if structured is not None:
        payload["json_dict"] = structured

    # `_extract_text_from_result` lives in section_01 and is merged into this
    # module's globals() by `module_runtime._sync_module_namespaces` at first
    # `get_runtime()` call.  Guard against the edge case where this debug
    # helper is reached before runtime sync (e.g. direct unit-test imports of
    # section_00, or very early-phase exception paths) so we never raise
    # NameError from a debug-dump path.
    _extract_fn = globals().get("_extract_text_from_result")
    raw_text = (_extract_fn(task_output) or "") if callable(_extract_fn) else ""
    if raw_text:
        payload["raw_text"] = limit_text(raw_text, 8000)
    return payload


def _direction_debug_dump_path() -> str:
    configured = (os.environ.get("DIRECTION_DEBATE_DEBUG_DIR") or "").strip()
    if configured:
        return configured
    return os.path.join(
        _REPO_ROOT,
        "saved_projects",
        "direction_debug",
    )


def _direction_debug_llm_model_id(llm: Any) -> str:
    for attr in ("model", "model_name", "model_id"):
        try:
            value = getattr(llm, attr, None)
        except Exception:
            value = None
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _write_direction_debate_debug_dump(
    *,
    user_problem: str,
    attempt: int,
    llm: Any,
    direction_judge_llm: Any,
    elapsed_seconds: float,
    stage_index_map: Optional[Dict[str, int]],
    result: Any,
    raw_candidates: List[str],
    decision: Optional["DirectionDecision"],
    comparator_report: Optional["DirectionComparatorReport"],
    audit_report: Optional["EvidenceAuditReport"],
    exception: Optional[BaseException] = None,
    note: str = "",
) -> Optional[str]:
    if not _env_bool("DIRECTION_DEBATE_DEBUG_DUMP", True):
        return None
    dump_dir = _direction_debug_dump_path()
    try:
        # Same forward-reference guards as `_debug_serialize_task_output`:
        # `_get_task_outputs` and `_extract_text_from_result` live in section_01
        # and are merged into globals() by `module_runtime._sync_module_namespaces`.
        # Fail-soft to empty/empty rather than raising NameError if this dump
        # path is reached before runtime sync.
        _get_outputs_fn = globals().get("_get_task_outputs")
        _extract_fn = globals().get("_extract_text_from_result")
        _task_outputs_iter = _get_outputs_fn(result) if callable(_get_outputs_fn) else []
        task_outputs = [
            _debug_serialize_task_output(task_output, index=index)
            for index, task_output in enumerate(_task_outputs_iter)
        ]
        _result_text = (_extract_fn(result) or "") if callable(_extract_fn) else ""
        payload = {
            "timestamp": datetime.now().isoformat(timespec="seconds"),
            "note": note,
            "attempt": attempt,
            "elapsed_seconds": round(float(elapsed_seconds or 0.0), 3),
            "llm_model_id": _direction_debug_llm_model_id(llm),
            "direction_judge_model_id": _direction_debug_llm_model_id(direction_judge_llm),
            "stage_index_map": dict(stage_index_map or {}),
            "user_problem": user_problem,
            "exception": repr(exception) if exception is not None else None,
            "result_type": type(result).__name__ if result is not None else None,
            "result_text": limit_text(_result_text, 12000),
            "raw_candidate_count": len(raw_candidates or []),
            "raw_candidates": [
                limit_text(str(candidate or ""), 12000)
                for candidate in list(raw_candidates or [])[:8]
            ],
            "decision": _model_to_dict(decision) if decision is not None else None,
            "comparator_report": _model_to_dict(comparator_report)
            if comparator_report is not None
            else None,
            "audit_report": _model_to_dict(audit_report) if audit_report is not None else None,
            "task_outputs": task_outputs,
        }
        filename = "direction_debate_{ts}_{uid}.json".format(
            ts=datetime.now().strftime("%Y%m%d_%H%M%S"),
            uid=uuid.uuid4().hex[:8],
        )
        fallback_dir = os.path.join(tempfile.gettempdir(), "CrucibleCrew_direction_debug")
        candidate_dirs: List[str] = []
        for candidate_dir in (dump_dir, fallback_dir):
            normalized = os.path.normcase(os.path.abspath(candidate_dir))
            if normalized not in candidate_dirs:
                candidate_dirs.append(normalized)
        for candidate_dir in candidate_dirs:
            _tmp_path = None
            try:
                os.makedirs(candidate_dir, exist_ok=True)
                path = os.path.join(candidate_dir, filename)
                _tmp_path = path + ".tmp"
                with open(_tmp_path, "w", encoding="utf-8") as fh:
                    json.dump(payload, fh, ensure_ascii=False, indent=2, default=str)
                os.replace(_tmp_path, path)
                return path
            except Exception:
                try:
                    if _tmp_path is not None:
                        os.unlink(_tmp_path)
                except Exception:
                    pass
                continue
        return None
    except Exception:
        return None
