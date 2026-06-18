# Auto-generated section module — do not edit manually.
# Regenerate via ``python -m crucible.generate``.
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
import threading
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
    # v1.1.1 — 2026-era DeepSeek variants the operator is actively using.
    # The /flash family tracks deepseek-chat pricing; /pro tracks the
    # reasoner-class tier on OpenRouter.  These keep cost-tracking honest
    # for any operator on the v4 line without waiting for the OpenRouter
    # `usage: {include: true}` opt-in path (some legacy CrewAI / litellm
    # callsites still don't forward the extra_body).
    "deepseek/deepseek-v3-chat": (0.14 / 1_000_000, 0.28 / 1_000_000),
    "deepseek/deepseek-v3-coder": (0.14 / 1_000_000, 0.28 / 1_000_000),
    "deepseek/deepseek-v3-reasoner": (0.55 / 1_000_000, 2.19 / 1_000_000),
    "deepseek/deepseek-r1": (0.55 / 1_000_000, 2.19 / 1_000_000),
    "deepseek/deepseek-v4-flash": (0.14 / 1_000_000, 0.28 / 1_000_000),
    "deepseek/deepseek-v4-pro": (0.55 / 1_000_000, 2.19 / 1_000_000),
    "mistralai/mistral-large": (2.00 / 1_000_000, 6.00 / 1_000_000),
    "mistralai/mistral-medium": (2.70 / 1_000_000, 8.10 / 1_000_000),
    "mistralai/mistral-small": (0.20 / 1_000_000, 0.60 / 1_000_000),
}
# v1.1.1 — Family-prefix fallback so a brand-new model variant within a
# known vendor family (e.g. `deepseek/deepseek-v5-...`) does not silently
# emit `total_cost_usd=0` / `cost_source="estimated"`.  The fallback
# pricing is the cheapest entry in each family, which keeps the cost
# estimate CONSERVATIVE (under-reports rather than over-reports —
# operators are more tolerant of a $0.10 under-estimate than an $1.00
# over-charge surprise).  Order matters: longer prefixes win; iteration
# order is the insertion order below.
OPENROUTER_MODEL_FAMILY_PRICING: Dict[str, Tuple[float, float]] = {
    "deepseek/deepseek-r": (0.55 / 1_000_000, 2.19 / 1_000_000),
    "deepseek/": (0.14 / 1_000_000, 0.28 / 1_000_000),
    "openai/gpt-5": (2.50 / 1_000_000, 15.00 / 1_000_000),
    "openai/gpt-4o": (2.50 / 1_000_000, 10.00 / 1_000_000),
    "openai/gpt-4": (10.00 / 1_000_000, 30.00 / 1_000_000),
    "openai/gpt-3": (0.50 / 1_000_000, 1.50 / 1_000_000),
    # Generic OpenAI fallback (future gpt-6+ etc) — same as gpt-5 tier
    # rather than the ancient gpt-3 floor, since future models are
    # almost certainly priced at least as high as the current frontier.
    "openai/": (2.50 / 1_000_000, 15.00 / 1_000_000),
    "anthropic/claude-3-opus": (15.00 / 1_000_000, 75.00 / 1_000_000),
    "anthropic/claude-3-haiku": (0.25 / 1_000_000, 1.25 / 1_000_000),
    "anthropic/": (3.00 / 1_000_000, 15.00 / 1_000_000),
    "google/gemini-1.5-pro": (3.50 / 1_000_000, 10.50 / 1_000_000),
    "google/": (0.50 / 1_000_000, 1.50 / 1_000_000),
    "z-ai/glm-4": (0.10 / 1_000_000, 0.10 / 1_000_000),
    "z-ai/": (0.72 / 1_000_000, 2.30 / 1_000_000),
    "minimax/": (0.20 / 1_000_000, 1.17 / 1_000_000),
    "meta-llama/llama-3-8b": (0.06 / 1_000_000, 0.06 / 1_000_000),
    "meta-llama/": (0.90 / 1_000_000, 0.90 / 1_000_000),
    "mistralai/mistral-small": (0.20 / 1_000_000, 0.60 / 1_000_000),
    "mistralai/": (2.00 / 1_000_000, 6.00 / 1_000_000),
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

# Reasoning-model "thinking" wrappers.  DeepSeek-V3/V4, GLM-5.1, Qwen-3.5,
# o1-style and similar reasoning models emit their internal chain of thought
# inside one of these tags before the actual answer.  When the reasoning text
# itself contains a brace-shape token (a hypothetical example dict, a
# pretty-printed sub-result, a regex literal, …), the forward JSON scan
# captures that token first and the real answer that follows is discarded.
# Strip these blocks before JSON extraction so the scanner only sees the
# model's final answer.
_REASONING_TAG_RE = re.compile(
    r"<\s*(?:think|thinking|reasoning|reflection|scratchpad)\b[^>]*>"
    r"[\s\S]*?"
    r"<\s*/\s*(?:think|thinking|reasoning|reflection|scratchpad)\s*>",
    re.IGNORECASE,
)


def _strip_reasoning_blocks(text: str) -> str:
    """Strip ``<think>…</think>`` and similar reasoning blocks from LLM output.

    Idempotent and safe on text that contains no such tags (returns the input
    unchanged).  Always preserves a trailing newline-trimmed body so downstream
    JSON scanners do not see a leading whitespace-only fragment.
    """
    if not text or "<" not in text:
        return text
    return _REASONING_TAG_RE.sub("", text)


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
    text = _strip_reasoning_blocks(text)
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


# v1.0.3: ``_env`` is the project-wide centralised env-parsing module.
# This file is loaded under three different module-resolution shapes
# (regular package import, runtime-loader exec, script-mode entrypoint), so
# the import has to tolerate all three:
#   1. Normal package: ``from .. import _env`` succeeds.
#   2. Script-mode (``python crucible/__main__.py``): no parent package, so
#      relative imports raise ``ImportError``.  Fall back to a top-level
#      ``crucible._env`` lookup once ``crucible/`` is on ``sys.path``.
#   3. Runtime-loader exec (``module_runtime`` synthesises ``crucible``
#      from individual section files): the ``crucible`` package is already
#      registered, so ``importlib.import_module`` resolves cleanly.
try:
    from .. import _env  # type: ignore[no-redef]
except ImportError:  # pragma: no cover - script-mode fallback
    import importlib
    _env = importlib.import_module("crucible._env")  # type: ignore[no-redef]


def _env_int(name: str, default: Optional[int]) -> Optional[int]:
    return _env.env_optional_int(name, default)


def _env_float(name: str, default: Optional[float]) -> Optional[float]:
    return _env.env_optional_float(name, default)


def _env_bool(name: str, default: bool = False) -> bool:
    return _env.env_bool(name, default, extended=True)


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
    # v1.1.10 (S2): ``grep_app`` removed from the default list.  grep.app is
    # fronted by Vercel Bot Protection which now serves a JS PoW challenge
    # to every unauthenticated client (status 429 with X-Vercel-Mitigated:
    # challenge); no pure-HTTP client can solve it, so every request burns
    # the 3-attempt retry budget and triggers a 60s cooldown for the whole
    # session.  The grep.app endpoint also offers no API-key tier, so the
    # block cannot be lifted by configuring credentials.  ``github`` is
    # the natural replacement for the "code" query class — its
    # ``search/code`` endpoint requires authentication anyway and its
    # 30 req/min authenticated quota is comfortably above what the
    # librarian needs.  The ``_search_grep_app`` helper itself is
    # preserved so operators who pin ``grep_app`` explicitly in
    # ``LIBRARIAN_SEARCH_PROVIDERS`` still work (the function will just
    # 429 for them until grep.app drops the Vercel challenge or exposes
    # an auth tier).
    "websearch",
    "context7",
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

# ── Authoritative OpenRouter billed-cost ledger (v1.1.12; fed via LiteLLM in v1.2.3) ─
# One row per billed OpenRouter response, carrying the exact ``usage.cost`` and
# token counts OpenRouter returned, appended exactly once (response-id dedup).
# ``get_openrouter_billed_total()`` / ``get_openrouter_billed_tokens()`` therefore
# equal the exact Σ across the whole run — the USD and token numbers the operator
# sees on the dashboard — and ``section_07._reconcile_cost_summary_with_billing``
# promotes both to the headline.  It is a plain module global guarded by a
# ``Lock`` (NOT a ContextVar) so writes from any thread/context are visible to the
# reader, and it is reset ONLY at run start (``reset_openrouter_billed_ledger()``).
#
# v1.2.3: the feeder is now the LiteLLM success callback
# (``_record_litellm_success`` → ``_append_openrouter_billed_entry``), the one
# chokepoint CrewAI actually routes through.  The previous CrewAI HTTP
# ``BaseInterceptor`` feeder never fired in real runs — crewai 1.14.x ``LLM``
# silently drops the ``interceptor=`` kwarg — so this ledger stayed empty and the
# headline fell back to the lossy, multi-x-inflated CrewAI usage-metrics path.
_OPENROUTER_BILLED_LEDGER: List[Dict[str, Any]] = []
_OPENROUTER_BILLED_LEDGER_LOCK = threading.Lock()


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
    # NB: deliberately does NOT touch the authoritative billed-cost ledger.
    # That ledger must survive every per-stage clear and is reset only at run
    # start via ``reset_openrouter_billed_ledger()``.
    _OPENROUTER_USAGE_CONTEXT.set({})
    _OPENROUTER_USAGE_RECORDS.set([])


def _append_openrouter_billed_entry(
    *,
    model_id: str = "",
    total_cost_usd: float = 0.0,
    input_tokens: int = 0,
    output_tokens: int = 0,
    cached_tokens: int = 0,
    reasoning_tokens: int = 0,
) -> None:
    """Append one authoritative billed-cost row (exact OpenRouter ``usage.cost``).

    Fed from the LiteLLM success callback (``_record_litellm_success``) for every
    billed OpenRouter response.  The ledger is the single source of truth for the
    per-run USD and token totals; it is never cleared by
    ``clear_openrouter_usage()`` — only by ``reset_openrouter_billed_ledger()``
    at run start.

    Rejects NaN / ±inf / non-positive costs so a malformed payload can never
    poison the sum (``not (x > 0)`` rejects NaN and ``-inf``; ``x == inf`` is
    rejected explicitly).
    """
    try:
        cost_val = float(total_cost_usd)
    except (TypeError, ValueError):
        return
    if not (cost_val > 0.0) or cost_val == float("inf"):
        return

    def _nonneg_int(value: Any) -> int:
        try:
            return max(0, int(value or 0))
        except (TypeError, ValueError):
            return 0

    entry = {
        "model_id": str(model_id or ""),
        "total_cost_usd": cost_val,
        "input_tokens": _nonneg_int(input_tokens),
        "output_tokens": _nonneg_int(output_tokens),
        "cached_tokens": _nonneg_int(cached_tokens),
        "reasoning_tokens": _nonneg_int(reasoning_tokens),
    }
    with _OPENROUTER_BILLED_LEDGER_LOCK:
        _OPENROUTER_BILLED_LEDGER.append(entry)


def reset_openrouter_billed_ledger() -> None:
    """Clear the authoritative billed-cost ledger (call once at run start).

    Clears in place (``list.clear()``) rather than rebinding so the reference
    shared across section modules (``module_runtime`` / ``globals().update``)
    stays valid.
    """
    with _OPENROUTER_BILLED_LEDGER_LOCK:
        _OPENROUTER_BILLED_LEDGER.clear()


def get_openrouter_billed_ledger() -> List[Dict[str, Any]]:
    """Return a snapshot copy of the authoritative billed-cost ledger rows."""
    with _OPENROUTER_BILLED_LEDGER_LOCK:
        return [dict(row) for row in _OPENROUTER_BILLED_LEDGER]


def get_openrouter_billed_count() -> int:
    """Return how many billed OpenRouter responses were captured this run."""
    with _OPENROUTER_BILLED_LEDGER_LOCK:
        return len(_OPENROUTER_BILLED_LEDGER)


def get_openrouter_billed_total() -> float:
    """Return the exact Σ(usage.cost) across every billed OpenRouter response.

    This is the authoritative per-run USD total — it equals what the operator
    is billed on the OpenRouter dashboard, because every billed response feeds
    exactly one ledger row carrying its returned ``usage.cost``.
    """
    with _OPENROUTER_BILLED_LEDGER_LOCK:
        total = 0.0
        for row in _OPENROUTER_BILLED_LEDGER:
            try:
                total += float(row.get("total_cost_usd", 0.0) or 0.0)
            except (TypeError, ValueError):
                continue
        return total


def get_openrouter_billed_tokens() -> Dict[str, int]:
    """Return the authoritative per-run token totals from the billed-cost ledger.

    Keys: ``input_tokens`` / ``output_tokens`` / ``cached_tokens`` /
    ``reasoning_tokens`` / ``total_tokens`` (``input + output``).  Every billed
    OpenRouter response contributes exactly one row, so these equal the exact
    token counts the operator sees on the dashboard — the authoritative override
    the cost summary uses for the headline ``total_tokens``.
    """
    totals = {
        "input_tokens": 0,
        "output_tokens": 0,
        "cached_tokens": 0,
        "reasoning_tokens": 0,
    }
    with _OPENROUTER_BILLED_LEDGER_LOCK:
        for row in _OPENROUTER_BILLED_LEDGER:
            for key in totals:
                try:
                    totals[key] += max(0, int(row.get(key, 0) or 0))
                except (TypeError, ValueError):
                    continue
    totals["total_tokens"] = totals["input_tokens"] + totals["output_tokens"]
    return totals


def _get_model_pricing(model_id: str) -> Tuple[float, float]:
    """Resolve per-token input/output USD price for ``model_id``.

    Search order:

    1. Exact / short-name / `endswith(/short)` match against
       ``OPENROUTER_MODEL_PRICING``.
    2. Family-prefix fallback against ``OPENROUTER_MODEL_FAMILY_PRICING``
       (v1.1.1).  Longest matching prefix wins, so
       ``deepseek/deepseek-r1-distill`` falls under the
       ``deepseek/deepseek-r`` entry (reasoner-class pricing) rather than
       the generic ``deepseek/`` chat-class pricing.  Without this fallback,
       any new model variant within a known family would silently emit
       ``total_cost_usd=0`` and ``cost_source="estimated"`` — exactly the
       v1.1.0-fifth-pass cost-zero regression the v1.1.1 round closed.
    3. Final fallback: ``(0.0, 0.0)`` (caller emits
       ``cost_source="estimated"`` with zero cost).
    """
    if not model_id:
        return (0.0, 0.0)
    candidates = list(_iter_model_id_candidates(model_id))
    # ── Tier 1: exact / short-name / endswith match ──────────────────────────
    for candidate in candidates:
        for key, pricing in OPENROUTER_MODEL_PRICING.items():
            key_lower = key.lower()
            key_short = key.split("/")[-1].lower()
            if candidate == key_lower or candidate == key_short or candidate.endswith("/" + key_short):
                return pricing
    # ── Tier 2: family-prefix fallback (v1.1.1) ──────────────────────────────
    # Choose the LONGEST matching prefix so a more-specific family
    # (e.g. "deepseek/deepseek-r") beats the generic vendor prefix
    # ("deepseek/").  Without the length sort, dict insertion order would
    # decide and a generic prefix could shadow the specific one.
    best_prefix: str = ""
    best_pricing: Optional[Tuple[float, float]] = None
    for family_prefix, pricing in OPENROUTER_MODEL_FAMILY_PRICING.items():
        prefix_lower = family_prefix.lower()
        for candidate in candidates:
            if candidate.startswith(prefix_lower) and len(prefix_lower) > len(best_prefix):
                best_prefix = prefix_lower
                best_pricing = pricing
                break
    if best_pricing is not None:
        return best_pricing
    return (0.0, 0.0)


# ── v1.2.3 — LiteLLM-native cost/usage capture (single source of truth) ──────
# CrewAI delegates every LLM call to ``litellm.completion`` and LiteLLM exposes a
# stable, framework-version-independent callback that carries BOTH token usage
# and billed cost.  We register one ``CustomLogger`` as the SOLE recorder.
#
# Why this replaced the prior machinery (the operator-visible bug): the old
# CrewAI ``BaseInterceptor`` + langchain ``BaseCallbackHandler`` were handed to
# ``crewai.LLM(...)`` via ``interceptor=`` / ``callbacks=`` kwargs that
# crewai 1.14.x silently drops (those params do not exist on its ``LLM``), so
# real runs NEVER captured a single response.  Cost/tokens then fell back to the
# CrewAI usage-metrics path (``extract_and_set_usage_from_crew``), whose
# ContextVar skip-guard fails across CrewAI worker threads and re-counted
# ``crew.calculate_usage_metrics()`` (cumulative across retries) — the multi-x
# inflation on both USD and tokens.  The LiteLLM callback fires once per
# completion in the call's own thread/context, so it cannot double-count and does
# not depend on any CrewAI/langchain hook plumbing.

# Per-run de-duplication keyed by response id: LiteLLM calls ``log_success_event``
# once per completion, but we guard so a stream+success double-fire (or any
# re-invocation) can never bill one response twice.
_LLM_USAGE_SEEN_KEYS: Set[str] = set()
_LLM_USAGE_SEEN_LOCK = threading.Lock()

# Best-effort (stage, agent) attribution for the per-stage / per-agent breakdown.
# Set by the orchestration around each crew kickoff
# (resilience.kickoff_crew_with_retry).  Missing attribution is harmless: the row
# lands under ``unattributed`` and the headline totals stay exact regardless.
_COST_ATTRIBUTION_DEFAULT: Tuple[str, str] = ("unattributed", "llm")
_COST_ATTRIBUTION: contextvars.ContextVar[Tuple[str, str]] = contextvars.ContextVar(
    "crucible_cost_attribution", default=_COST_ATTRIBUTION_DEFAULT
)


def set_cost_attribution(stage: str, agent: str = "") -> Any:
    """Stamp the (stage, agent) tag the LiteLLM callback writes onto each row.

    Returns the ``contextvars.Token`` so the caller can restore the prior value
    with :func:`reset_cost_attribution`.
    """
    stage_clean = str(stage or "").strip() or "unattributed"
    agent_clean = str(agent or "").strip() or "llm"
    return _COST_ATTRIBUTION.set((stage_clean, agent_clean))


def reset_cost_attribution(token: Any) -> None:
    if token is None:
        return
    try:
        _COST_ATTRIBUTION.reset(token)
    except (ValueError, LookupError):
        # Token created in a different context (e.g. a crew worker thread).
        _COST_ATTRIBUTION.set(_COST_ATTRIBUTION_DEFAULT)


def reset_llm_usage_dedup() -> None:
    """Clear the per-run response-dedup set (call once at run start)."""
    with _LLM_USAGE_SEEN_LOCK:
        _LLM_USAGE_SEEN_KEYS.clear()


def _cost_is_finite_positive(value: Any) -> bool:
    """True iff ``value`` is a finite, strictly-positive float (NaN/inf safe)."""
    try:
        v = float(value)
    except (TypeError, ValueError):
        return False
    # ``v == v`` rejects NaN; the explicit inf check rejects +inf.
    return v > 0.0 and v != float("inf") and v == v


def _usage_obj_to_dict(usage: Any) -> Dict[str, Any]:
    """Coerce a LiteLLM ``Usage`` (pydantic) / dict / namespace into a plain dict."""
    if usage is None:
        return {}
    if isinstance(usage, dict):
        return dict(usage)
    for attr in ("model_dump", "dict"):
        fn = getattr(usage, attr, None)
        if callable(fn):
            try:
                dumped = fn()
                if isinstance(dumped, dict):
                    return dumped
            except Exception:
                pass
    out: Dict[str, Any] = {}
    for key in (
        "prompt_tokens",
        "completion_tokens",
        "total_tokens",
        "prompt_tokens_details",
        "completion_tokens_details",
        "cost",
    ):
        val = getattr(usage, key, None)
        if val is None:
            continue
        if key.endswith("_details") and not isinstance(val, dict):
            val = _usage_obj_to_dict(val)
        out[key] = val
    return out


def _resolve_litellm_call_provider(kwargs: Dict[str, Any], response_obj: Any) -> str:
    """Best-effort provider resolution from the LiteLLM call kwargs / response."""
    try:
        litellm_params = kwargs.get("litellm_params") or {}
        api_base = litellm_params.get("api_base") or kwargs.get("api_base") or ""
        host_provider = _host_to_usage_provider(str(api_base or ""))
        if host_provider:
            return host_provider
    except Exception:
        pass
    try:
        model = str(kwargs.get("model") or getattr(response_obj, "model", "") or "").lower()
        if "openrouter" in model:
            return LLM_PROVIDER_OPENROUTER
        if "dashscope" in model or "coding-intl" in model:
            return LLM_PROVIDER_ALIBABA_CODING_PLAN
    except Exception:
        pass
    return _resolve_usage_provider()


def _resolve_litellm_cost(
    kwargs: Dict[str, Any],
    response_obj: Any,
    usage: Dict[str, Any],
    provider: str,
) -> Tuple[float, str]:
    """Return ``(total_cost_usd, cost_source)`` for one completion.

    Priority: OpenRouter authoritative ``usage.cost`` (sent via
    ``usage: {include: true}``) → LiteLLM ``response_cost`` threaded into the
    logging ``kwargs`` → ``litellm.completion_cost(response_obj)``.  Alibaba
    coding-plan is token-only (cost forced to 0).
    """
    if provider == LLM_PROVIDER_ALIBABA_CODING_PLAN:
        return 0.0, "alibaba_coding_plan_tokens_only"
    raw_cost = usage.get("cost")
    if _cost_is_finite_positive(raw_cost):
        return float(raw_cost), "openrouter_api"
    rc = kwargs.get("response_cost")
    if _cost_is_finite_positive(rc):
        return float(rc), "litellm_computed"
    try:
        import litellm

        computed = litellm.completion_cost(completion_response=response_obj)
        if _cost_is_finite_positive(computed):
            return float(computed), "litellm_computed"
    except Exception:
        pass
    return 0.0, "estimated"


def _extract_usage_counts(usage: Dict[str, Any]) -> Tuple[int, int, int, int, int]:
    """Return ``(prompt, completion, total, cached, reasoning)`` from a usage dict.

    Robust to BOTH the nested OpenAI/OpenRouter shape
    (``prompt_tokens_details.cached_tokens`` / ``completion_tokens_details.reasoning_tokens``)
    AND CrewAI's flattened shape (``cached_prompt_tokens`` / ``reasoning_tokens``),
    so the same recorder serves the CrewAI event listener and the LiteLLM
    callback without divergence.
    """
    prompt = int(usage.get("prompt_tokens", usage.get("input_tokens", 0)) or 0)
    completion = int(usage.get("completion_tokens", usage.get("output_tokens", 0)) or 0)
    _raw_total = usage.get("total_tokens")
    total = int(_raw_total if _raw_total is not None else (prompt + completion))
    prompt_details = usage.get("prompt_tokens_details") or {}
    if not isinstance(prompt_details, dict):
        prompt_details = _usage_obj_to_dict(prompt_details)
    completion_details = usage.get("completion_tokens_details") or {}
    if not isinstance(completion_details, dict):
        completion_details = _usage_obj_to_dict(completion_details)
    cached = int(
        prompt_details.get(
            "cached_tokens",
            usage.get("cached_prompt_tokens", usage.get("cached_tokens", 0)) or 0,
        )
        or 0
    )
    reasoning = int(
        completion_details.get(
            "reasoning_tokens",
            usage.get("reasoning_tokens", usage.get("native_tokens_reasoning", 0)) or 0,
        )
        or 0
    )
    return prompt, completion, total, cached, reasoning


def _record_llm_usage(
    *,
    response_id: Any,
    model: Any,
    usage: Any,
    response_cost: Any = None,
    response_obj: Any = None,
) -> None:
    """THE single cost/usage recording chokepoint (v1.2.3).

    Shared by the CrewAI ``LLMCallCompletedEvent`` listener (the PRIMARY capture —
    crucible's LLM is CrewAI's native ``OpenAICompletion`` provider, which uses the
    OpenAI SDK directly and never routes through LiteLLM) and the LiteLLM success
    callback (a fallback for any direct-LiteLLM path).

    Anti-double-count contract:
    * **One stable dedup key per call** (``response_id`` = the provider generation
      id).  A call observed by BOTH paths (e.g. a LiteLLM-backed provider) shares
      the same id, so it is recorded exactly once.  A call with no usable id is
      SKIPPED rather than risk an un-dedupable double count.
    * All recording funnels through here — no other code path calls
      ``get_cost_accountant().record`` for live LLM cost (pinned by a structural
      test), so a future additive change cannot silently introduce a second
      counting path.

    Best-effort: never raises (cost accounting must not crash a run).
    """
    try:
        if not isinstance(usage, dict) or not usage:
            return
        dedup_key = str(response_id or "").strip()
        if not dedup_key:
            # No stable id → cannot guarantee dedup → skip (undercount-safe) rather
            # than risk the double-counting the whole subsystem was rewritten to kill.
            return
        with _LLM_USAGE_SEEN_LOCK:
            if dedup_key in _LLM_USAGE_SEEN_KEYS:
                return
            _LLM_USAGE_SEEN_KEYS.add(dedup_key)

        prompt_tokens, completion_tokens, total_tokens, cached_tokens, reasoning_tokens = (
            _extract_usage_counts(usage)
        )
        # Skip control calls that did no measurable work.
        if total_tokens <= 0:
            return

        # Reuse the existing provider/cost resolvers via a minimal kwargs shim so
        # the event path and the LiteLLM path share identical resolution logic.
        shim_kwargs: Dict[str, Any] = {"model": model}
        if response_cost is not None:
            shim_kwargs["response_cost"] = response_cost
        provider = _resolve_litellm_call_provider(shim_kwargs, response_obj)
        model_id = _canonicalize_usage_model_id(str(model or ""), provider)
        total_cost, cost_source = _resolve_litellm_cost(shim_kwargs, response_obj, usage, provider)

        # No authoritative/computed cost → fall back to the local pricing table so
        # USD is a labelled estimate (``openrouter_tokens_with_pricing``) rather
        # than silently 0.  Authoritative ``usage.cost`` (openrouter_api) always
        # wins above and keeps the billed-ledger headline exact.
        if (
            provider == LLM_PROVIDER_OPENROUTER
            and cost_source == "estimated"
            and not (total_cost > 0)
        ):
            input_price, output_price = _get_model_pricing(model_id)
            if input_price > 0 or output_price > 0:
                est_in = prompt_tokens * input_price
                est_out = completion_tokens * output_price
                est_save = _estimate_cache_savings(
                    prompt_tokens=int(prompt_tokens),
                    cached_tokens=int(cached_tokens),
                    input_cost=float(est_in),
                    input_price=float(input_price),
                )
                total_cost = max(0.0, float(est_in + est_out - est_save))
                cost_source = "openrouter_tokens_with_pricing"

        # Split the total cost across input/output proportionally to tokens.
        input_cost = 0.0
        output_cost = 0.0
        if total_cost > 0:
            denom = prompt_tokens + completion_tokens
            if denom > 0:
                input_cost = total_cost * (prompt_tokens / denom)
                output_cost = total_cost - input_cost
            else:
                input_cost = total_cost
        cache_cost = _estimate_cache_savings(
            prompt_tokens=int(prompt_tokens),
            cached_tokens=int(cached_tokens),
            input_cost=float(input_cost),
        )

        stage, agent = _COST_ATTRIBUTION.get(_COST_ATTRIBUTION_DEFAULT)

        get_cost_accountant().record(
            agent_name=agent,
            stage=stage,
            input_tokens=prompt_tokens,
            output_tokens=completion_tokens,
            success=True,
            cache_hit=cached_tokens > 0,
            outcome="success",
            cached_tokens=cached_tokens,
            reasoning_tokens=reasoning_tokens,
            input_cost_usd=float(input_cost),
            output_cost_usd=float(output_cost),
            cache_cost_usd=float(cache_cost),
            total_cost_usd=float(total_cost),
            model_id=model_id,
            cost_source=cost_source,
        )

        # Authoritative billed-cost ledger: OpenRouter responses carrying a real
        # billed ``usage.cost`` only.  Drives the v1.1.12 reconcile path as the
        # headline authority (``get_openrouter_billed_total/tokens()``).
        if provider == LLM_PROVIDER_OPENROUTER and cost_source == "openrouter_api":
            _append_openrouter_billed_entry(
                model_id=model_id,
                total_cost_usd=float(total_cost),
                input_tokens=int(prompt_tokens),
                output_tokens=int(completion_tokens),
                cached_tokens=int(cached_tokens),
                reasoning_tokens=int(reasoning_tokens),
            )
    except Exception:
        pass


def _record_litellm_success(kwargs: Any, response_obj: Any) -> None:
    """LiteLLM success-callback adapter → the shared ``_record_llm_usage`` sink.

    Fallback path: fires only for calls that actually route through
    ``litellm.completion`` (crucible's native ``OpenAICompletion`` does not).
    """
    try:
        if not isinstance(kwargs, dict):
            kwargs = {}
        usage = _usage_obj_to_dict(getattr(response_obj, "usage", None))
        if not usage:
            usage = _usage_obj_to_dict(kwargs.get("usage"))
        try:
            resp_id = str(getattr(response_obj, "id", "") or "")
        except Exception:
            resp_id = ""
        _record_llm_usage(
            response_id=resp_id,
            model=(kwargs.get("model") or getattr(response_obj, "model", "")),
            usage=usage,
            response_cost=kwargs.get("response_cost"),
            response_obj=response_obj,
        )
    except Exception:
        pass


def _on_crewai_llm_call_completed(source: Any, event: Any) -> None:
    """CrewAI ``LLMCallCompletedEvent`` listener — PRIMARY cost capture (v1.2.3).

    crucible's LLM is CrewAI's native ``OpenAICompletion`` (OpenAI SDK, often
    streaming), which bypasses LiteLLM entirely — so the LiteLLM callback never
    fires for it.  CrewAI emits this event once per completed call (streaming and
    non-streaming) carrying token usage + model + the provider generation id; the
    OpenRouter ``usage.cost`` it normally drops is restored by
    ``_install_crewai_usage_cost_passthrough``.
    """
    try:
        usage = _usage_obj_to_dict(getattr(event, "usage", None))
        response_id = (
            str(getattr(event, "response_id", "") or "").strip()
            or str(getattr(event, "call_id", "") or "").strip()
            or str(getattr(event, "event_id", "") or "").strip()
        )
        _record_llm_usage(
            response_id=response_id,
            model=str(getattr(event, "model", "") or ""),
            usage=usage,
        )
    except Exception:
        pass


_LITELLM_USAGE_LOGGER: Optional[Any] = None

try:
    from litellm.integrations.custom_logger import CustomLogger as _LiteLLMCustomLogger

    class _LiteLLMUsageLogger(_LiteLLMCustomLogger):
        """Sole cost/usage recorder — fires once per completed LiteLLM call."""

        def log_success_event(self, kwargs, response_obj, start_time, end_time):  # noqa: D401,ANN001
            _record_litellm_success(kwargs, response_obj)

        async def async_log_success_event(self, kwargs, response_obj, start_time, end_time):  # noqa: D401,ANN001
            _record_litellm_success(kwargs, response_obj)

    _LITELLM_USAGE_LOGGER = _LiteLLMUsageLogger()
except Exception:  # pragma: no cover - litellm is always present in practice
    _LiteLLMUsageLogger = None  # type: ignore[assignment,misc]
    _LITELLM_USAGE_LOGGER = None


def get_litellm_usage_logger() -> Optional[Any]:
    return _LITELLM_USAGE_LOGGER


def ensure_litellm_usage_logger_registered() -> bool:
    """Idempotently register the usage logger into ``litellm.callbacks``.

    Safe to call on every LLM build; returns True once the logger is live.
    """
    logger = _LITELLM_USAGE_LOGGER
    if logger is None:
        return False
    try:
        import litellm

        callbacks = getattr(litellm, "callbacks", None)
        if not isinstance(callbacks, list):
            litellm.callbacks = [logger]
            return True
        if logger not in callbacks:
            callbacks.append(logger)
        return True
    except Exception:
        return False


# ── CrewAI native-provider cost/usage capture (v1.2.3 — the PRIMARY path) ─────
# crucible's LLM is CrewAI's native ``OpenAICompletion`` (OpenAI SDK, not
# LiteLLM), so the LiteLLM callback above never fires for it.  CrewAI DOES emit a
# per-call ``LLMCallCompletedEvent`` (streaming and non-streaming) on its event
# bus — that is the one reliable, provider-agnostic capture point.  Two module
# flags guarantee the two pieces are installed EXACTLY ONCE; re-subscribing the
# listener would make every event record N times (the classic event-bus
# double-count), so idempotency here is a hard invariant pinned by tests.
_CREWAI_USAGE_LISTENER_REGISTERED: bool = False
_CREWAI_COST_PASSTHROUGH_INSTALLED: bool = False


def _install_crewai_usage_cost_passthrough() -> bool:
    """Preserve OpenRouter's ``usage.cost`` through CrewAI's usage extractor.

    CrewAI's ``OpenAICompletion._extract_*_token_usage`` copies only token counts
    into the usage dict it emits on ``LLMCallCompletedEvent`` — it drops the
    ``cost`` field OpenRouter returns (which survives openai-SDK parsing as a
    pydantic extra on ``response.usage``).  Without ``cost`` the headline USD
    would fall back to a local pricing estimate.  This wraps the extractor(s) to
    re-attach ``cost`` so the event carries the authoritative billed amount.

    Idempotent and fail-soft: if the (version-specific) method is renamed in a
    future CrewAI, the wrap simply isn't installed and USD degrades gracefully to
    the pricing-table estimate — never a crash.  A structural test pins that the
    extractor methods still exist so the silent degrade is caught at test time.
    """
    global _CREWAI_COST_PASSTHROUGH_INSTALLED
    if _CREWAI_COST_PASSTHROUGH_INSTALLED:
        return True
    try:
        from crewai.llms.providers.openai.completion import OpenAICompletion
    except Exception:
        return False

    def _make_wrapper(orig: Any) -> Any:
        def _wrapped(self: Any, response: Any, *args: Any, **kwargs: Any) -> Any:
            result = orig(self, response, *args, **kwargs)
            try:
                if isinstance(result, dict) and "cost" not in result:
                    usage_obj = getattr(response, "usage", None)
                    cost = None
                    if usage_obj is not None:
                        cost = getattr(usage_obj, "cost", None)
                        if cost is None:
                            extra = getattr(usage_obj, "model_extra", None)
                            if isinstance(extra, dict):
                                cost = extra.get("cost")
                    if cost is not None:
                        result["cost"] = cost
            except Exception:
                pass
            return result

        _wrapped._crucible_cost_wrapped = True  # type: ignore[attr-defined]
        return _wrapped

    installed_any = False
    for meth_name in ("_extract_openai_token_usage", "_extract_responses_token_usage"):
        orig = getattr(OpenAICompletion, meth_name, None)
        if orig is None or getattr(orig, "_crucible_cost_wrapped", False):
            continue
        try:
            setattr(OpenAICompletion, meth_name, _make_wrapper(orig))
            installed_any = True
        except Exception:
            pass
    _CREWAI_COST_PASSTHROUGH_INSTALLED = True
    return installed_any


def ensure_crewai_usage_listener_registered() -> bool:
    """Idempotently subscribe the CrewAI cost listener (v1.2.3 — PRIMARY capture).

    Registers ``_on_crewai_llm_call_completed`` for ``LLMCallCompletedEvent``
    exactly once per process (guarded by ``_CREWAI_USAGE_LISTENER_REGISTERED``).
    Double-subscription would record every call twice — the invariant a structural
    test pins.  Also installs the cost-passthrough wrap.  Returns True once live.
    """
    global _CREWAI_USAGE_LISTENER_REGISTERED
    if _CREWAI_USAGE_LISTENER_REGISTERED:
        return True
    try:
        from crewai.events import crewai_event_bus
        from crewai.events.types.llm_events import LLMCallCompletedEvent
    except Exception:
        return False
    try:
        _install_crewai_usage_cost_passthrough()
        crewai_event_bus.register_handler(
            LLMCallCompletedEvent, _on_crewai_llm_call_completed
        )
        _CREWAI_USAGE_LISTENER_REGISTERED = True
        return True
    except Exception:
        return False


def inject_openrouter_usage_extra_body(llm_kwargs: Dict[str, Any]) -> Dict[str, Any]:
    """Mutate ``llm_kwargs`` so the OpenRouter request includes ``usage: {"include": true}``.

    OpenRouter only populates ``response.usage.cost`` (the actual billed USD
    amount) when the request body carries ``"usage": {"include": true}``.
    Without that opt-in, ``usage.cost`` is omitted from the response and the
    Crucible cost-tracking pipeline falls back to the local pricing table —
    which silently returns 0 for any model variant not enumerated there.
    This was the v1.1.0-era cost-zero bug for ``deepseek-v4-*``.

    The flag is forwarded via crewai.LLM's ``additional_params`` → litellm's
    ``extra_body`` → openai SDK's request body merger.  All three layers
    preserve unknown keys verbatim, so this is a pure additive change.

    Idempotent: a second call is a no-op if the flag is already set.
    Defensive: tolerates ``llm_kwargs["additional_params"]`` being absent,
    pre-set to a non-dict (returns unchanged), or pre-set with an unrelated
    ``extra_body`` value (merges instead of clobbering).

    Returns ``llm_kwargs`` for chained-call ergonomics.
    """
    if not isinstance(llm_kwargs, dict):
        return llm_kwargs
    additional = llm_kwargs.setdefault("additional_params", {})
    if not isinstance(additional, dict):
        # Caller passed something we can't merge into — leave it alone to
        # avoid breaking a deliberate override.
        return llm_kwargs
    raw_extra_body = additional.get("extra_body")
    extra_body = dict(raw_extra_body) if isinstance(raw_extra_body, dict) else {}
    raw_usage = extra_body.get("usage")
    usage_block = dict(raw_usage) if isinstance(raw_usage, dict) else {}
    usage_block.setdefault("include", True)
    extra_body["usage"] = usage_block
    additional["extra_body"] = extra_body
    return llm_kwargs


# ── v1.2.3 — CrewAI/langchain cost hooks removed ─────────────────────────────
# The langchain ``BaseCallbackHandler`` and CrewAI ``BaseInterceptor`` cost hooks
# that used to live here were dead in real runs: crewai 1.14.x ``LLM.__init__``
# silently drops the ``callbacks=`` / ``interceptor=`` kwargs they were wired
# through, so neither ever fired and cost/tokens fell back to the lossy CrewAI
# usage-metrics path (the multi-x inflation).  Cost/usage is now captured solely
# by the LiteLLM success callback (``_LiteLLMUsageLogger`` /
# ``ensure_litellm_usage_logger_registered``), the one chokepoint CrewAI actually
# routes through.  The ``inject_openrouter_usage_extra_body`` opt-in above stays:
# it makes OpenRouter return the authoritative billed ``usage.cost``.


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
    """Deprecated no-op (v1.2.3).

    Cost/usage is now captured exclusively by the LiteLLM success callback
    (``_record_litellm_success``), which fires once per completion at the
    SDK chokepoint with real token counts and billed cost.  The old per-stage
    path read an accumulating ContextVar and char-count estimates, which
    double-counted against the callback and mis-attributed across CrewAI worker
    threads.  This stub is retained so the ~30 existing call sites need not
    change; it intentionally does nothing.  Stage attribution now flows through
    ``set_cost_attribution`` around each crew kickoff.
    """
    return None


def _coerce_json_dict(value: Any) -> Optional[dict]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        return _extract_first_json_object(value)
    try:
        # v1.1.2 (sixth-pass M-7): ``json.dumps`` defaults to ``allow_nan=True``
        # which produces the non-standard literals ``NaN`` / ``Infinity`` /
        # ``-Infinity`` (RFC 8259 forbids them).  ``json.loads`` then accepts
        # them back, so a payload like ``{"confidence": float("nan")}`` round-
        # trips with NaN intact and silently bypasses every downstream
        # ``if x > 0.5`` gate (``NaN > anything`` is always False).  Force
        # ``allow_nan=False`` to raise on non-finite floats and let the
        # caller see ``None``, symmetric with ``output_validation._coerce``'s
        # finite-only contract.
        return json.loads(json.dumps(value, ensure_ascii=False, allow_nan=False))
    except (TypeError, ValueError):
        return None
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
        try:
            from .._atomic_io import atomic_write_text
        except ImportError:  # flat-launcher mode
            from _atomic_io import atomic_write_text  # type: ignore[no-redef]
        for candidate_dir in candidate_dirs:
            try:
                os.makedirs(candidate_dir, exist_ok=True)
                path = os.path.join(candidate_dir, filename)
                # atomic_write_text handles the .tmp write + os.replace + tmp
                # cleanup-on-failure internally, and fsyncs the parent dir
                # (CLAUDE.md §13.1, v1.1.11).
                atomic_write_text(
                    path,
                    json.dumps(payload, ensure_ascii=False, indent=2, default=str),
                )
                return path
            except Exception:
                continue
        return None
    except Exception:
        return None
