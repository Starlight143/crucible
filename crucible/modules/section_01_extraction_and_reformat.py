# Auto-generated section module — do not edit manually.
# Regenerate via ``python -m crucible.generate``.
from __future__ import annotations

from . import section_00_bootstrap_and_utils as _prev_00
globals().update({k: v for k, v in _prev_00.__dict__.items() if not k.startswith('__')})
if __package__ in {"crucible.modules", "crucible.sections"}:
    from ..resilience import kickoff_crew_with_retry
    from ..cancellation import OperationCancelledError as _OperationCancelledError
else:  # pragma: no cover - direct script fallback
    from resilience import kickoff_crew_with_retry
    from cancellation import OperationCancelledError as _OperationCancelledError  # type: ignore[no-redef]


def _reformat_llm_model_id(llm: Any) -> str:
    for attr in ("model", "model_name", "model_id"):
        try:
            value = getattr(llm, attr, None)
        except Exception:
            value = None
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _reformat_llm_provider_name(llm: Any) -> str:
    try:
        provider = getattr(llm, "_quant_llm_provider", None)
    except Exception:
        provider = None
    if isinstance(provider, str) and provider.strip():
        return _normalize_llm_provider(provider)
    active_provider = globals().get("ACTIVE_LLM_PROVIDER")
    if isinstance(active_provider, str) and active_provider.strip():
        return _normalize_llm_provider(active_provider)
    return _normalize_llm_provider(None)


REFORMAT_INPUT_MAX_CHARS = 12000
REFORMAT_INPUT_HEAD_CHARS = 8000
REFORMAT_INPUT_TAIL_CHARS = 3000

# Maximum tokens the formatter LLM is allowed to generate.
# Reasoning models (kimi-k2.6, deepseek-r1, o1 class) exhaust their entire
# max_tokens budget on internal chain-of-thought then generate tens-of-thousands
# of completion tokens for a trivial JSON task; the output exceeds the API cap,
# gets silently truncated, and the resulting JSON is invalid → schema validation
# fails → the pipeline retries indefinitely.
# A well-formed ResearchContext JSON is at most ~4 000 tokens; 8 192 is more
# than sufficient for every formatter target in this codebase.
# Use ``_env_int`` (inherited from section_00) so that a malformed env-var value
# falls back to the default rather than crashing module import.
_formatter_max_tokens_env = _env_int("FORMATTER_MAX_TOKENS", 8192)
FORMATTER_MAX_TOKENS: int = (
    _formatter_max_tokens_env
    if _formatter_max_tokens_env is not None and _formatter_max_tokens_env > 0
    else 8192
)


def _make_formatter_llm(main_llm: Any, max_tokens: Optional[int] = None) -> Any:
    """Return an LLM instance capped at FORMATTER_MAX_TOKENS for schema reformatting.

    Optionally override the model via the FORMATTER_MODEL env var (same
    base_url / api_key as the main LLM, so it works with any OpenRouter-
    compatible provider without extra configuration).
    Falls back to ``main_llm`` unchanged if the LLM cannot be constructed.

    Args:
        main_llm:   Source LLM — credentials / base_url / model are copied from it.
        max_tokens: Optional token cap override.  Defaults to ``FORMATTER_MAX_TOKENS``
                    (8 192).  Pass a larger value (e.g. ``CODEGEN_MAX_TOKENS``) when
                    the expected output is a full CodeBundle with complete source files.
    """
    try:
        from crewai import LLM as _CrewAI_LLM  # local import — avoids circular deps

        model_override = str(os.environ.get("FORMATTER_MODEL", "") or "").strip()
        model_id: str = model_override or (
            str(getattr(main_llm, "model", None) or getattr(main_llm, "model_name", None) or "")
        )
        if not model_id:
            return main_llm  # Cannot determine model; use as-is

        effective_max_tokens: int = max_tokens if max_tokens is not None else FORMATTER_MAX_TOKENS
        kwargs: Dict[str, Any] = {
            "model": model_id,
            "provider": "openai",
            "temperature": 0.2,          # Low temperature → deterministic JSON
            "max_tokens": effective_max_tokens,
        }
        for attr, key in (("api_key", "api_key"), ("base_url", "base_url")):
            val = getattr(main_llm, attr, None)
            if val:
                kwargs[key] = val
        timeout_raw = getattr(main_llm, "timeout", None)
        if timeout_raw is not None:
            try:
                kwargs["timeout"] = float(timeout_raw)
            except (TypeError, ValueError):
                pass

        # v1.1.1 — Propagate the OpenRouter usage-include opt-in from the
        # main LLM so the formatter LLM also gets real billed cost in its
        # responses.  Without this, every formatter call would emit
        # ``cost_source="estimated"`` and zero out the cost summary even
        # if the main LLM was correctly configured.
        #
        # v1.1.3 — Wire the HTTP interceptor + callback handler here too.
        # Without these, the request body carries ``usage: {include: true}``
        # but the response's ``usage.cost`` field is silently dropped on
        # the floor (no hook reads it), and cost tracking falls back to the
        # local pricing table → ``cost_source="crewai_metrics_with_pricing"``
        # instead of the authoritative ``"openrouter_api"``.
        try:
            provider_tag = str(getattr(main_llm, "_quant_llm_provider", "") or "").strip().lower()
            if provider_tag == LLM_PROVIDER_OPENROUTER:
                inject_openrouter_usage_extra_body(kwargs)
                callback_handler = get_openrouter_callback_handler()
                if callback_handler is not None:
                    existing_callbacks = kwargs.get("callbacks")
                    if isinstance(existing_callbacks, list):
                        if callback_handler not in existing_callbacks:
                            existing_callbacks.append(callback_handler)
                    else:
                        kwargs["callbacks"] = [callback_handler]
                http_interceptor = get_openrouter_http_interceptor()
                if http_interceptor is not None and "interceptor" not in kwargs:
                    kwargs["interceptor"] = http_interceptor
        except Exception:
            pass

        formatter_llm = _CrewAI_LLM(**kwargs)
        # Carry over the provider tag so cost tracking / cache keying stays correct.
        try:
            provider_tag = getattr(main_llm, "_quant_llm_provider", None)
            if provider_tag:
                setattr(formatter_llm, "_quant_llm_provider", provider_tag)
        except Exception:
            pass
        return formatter_llm
    except Exception:
        return main_llm  # Fallback: use main_llm unchanged


def _limit_reformat_input(raw_text: str, max_chars: int = REFORMAT_INPUT_MAX_CHARS) -> str:
    text = str(raw_text or "")
    if max_chars <= 0 or len(text) <= max_chars:
        return text
    head_chars = min(REFORMAT_INPUT_HEAD_CHARS, max_chars)
    separator = "\n\n...[truncated middle]...\n\n"
    tail_budget = max(0, max_chars - head_chars - len(separator))
    tail_chars = min(REFORMAT_INPUT_TAIL_CHARS, tail_budget)
    if tail_chars <= 0:
        return limit_text(text, max_chars)
    head = text[:head_chars].rstrip()
    tail = text[-tail_chars:].lstrip()
    # Use the original slice sizes (before whitespace stripping) so the omitted
    # count reflects the actual number of characters skipped from the source text,
    # not the post-strip lengths which would overstate the gap.
    omitted = max(0, len(text) - head_chars - tail_chars)
    return head + f"\n\n...[{omitted} chars truncated middle]...\n\n" + tail


def _kickoff_reformat_crew(
    crew: Any,
    *,
    crew_name: str,
    raw_text: str,
    cost_trace_stage: Optional[str],
    error_label: str,
) -> Optional[Any]:
    prompt_chars = 0
    try:
        task_list = list(getattr(crew, "tasks", []) or [])
        if task_list:
            prompt_chars = int(len(getattr(task_list[0], "description", "") or ""))
    except Exception:
        prompt_chars = 0
    try:
        if cost_trace_stage:
            _cost_trace(
                cost_trace_stage,
                raw_text_chars=len(raw_text or ""),
                prompt_chars=prompt_chars,
            )
        return kickoff_crew_with_retry(
            crew,
            crew_name=crew_name,
            log_fields={
                "raw_text_chars": len(raw_text or ""),
                "prompt_chars": prompt_chars,
                "reformat_stage": cost_trace_stage or crew_name,
            },
        )
    except _OperationCancelledError:
        # Cooperative cancellation must propagate — returning None would allow
        # the pipeline to continue running after the user cancelled.
        raise
    except Exception as e:
        print(f"[Error] {error_label} failed: {e}")
        return None


def _extract_pydantic_from_result(
    result: Any,
    model_cls: Any,
    required_keys: Tuple[str, ...],
    attr_order: Tuple[str, ...] = ("json_dict", "output", "raw"),
) -> Optional[Any]:
    def _try_build(candidate: Any) -> Optional[Any]:
        d = _coerce_json_dict(candidate)
        if not isinstance(d, dict):
            return None
        if required_keys and not all(k in d for k in required_keys):
            return None
        try:
            return model_cls(**d)
        except Exception:
            # v1.1.9 (M4 / P1): lenient retry — when the strict build fails
            # because the LLM emitted extra fields the model schema doesn't
            # know about (``ConfigDict(extra="forbid")`` or unknown keyword
            # argument), filter the payload down to the model's declared
            # fields and try once more.  This is the CLAUDE.md § 12.1 P1
            # follow-up — the v1.1.8 known limitation was that one stray
            # extra key from a chatty model would burn the entire retry
            # budget even though the required fields were present.  Strict
            # validation still wins when the model emits a clean payload;
            # the lenient retry only fires after the strict attempt fails.
            try:
                known_fields = set(getattr(model_cls, "model_fields", {}) or {})
                if not known_fields:
                    # pydantic v1 fallback (we ship pydantic>=2 so this is
                    # belt-and-braces only).
                    known_fields = set(getattr(model_cls, "__fields__", {}) or {})
                if known_fields:
                    filtered = {k: v for k, v in d.items() if k in known_fields}
                    if filtered and (
                        not required_keys
                        or all(k in filtered for k in required_keys)
                    ):
                        return model_cls(**filtered)
            except Exception:
                return None
            return None

    if isinstance(result, model_cls):
        return result
    if hasattr(result, "pydantic") and isinstance(
        getattr(result, "pydantic", None), model_cls
    ):
        return result.pydantic

    if isinstance(result, (dict, str)):
        built = _try_build(result)
        if built is not None:
            return built

    # First prefer the final result payload (more stable than tasks_output ordering).
    for attr in attr_order:
        if hasattr(result, attr):
            built = _try_build(getattr(result, attr))
            if built is not None:
                return built

    # Then scan task outputs as a fallback.
    if hasattr(result, "tasks_output") and result.tasks_output:
        try:
            task_outputs = list(result.tasks_output)
        except Exception:
            task_outputs = []
        for t in reversed(task_outputs):
            if isinstance(t, model_cls):
                return t
            if hasattr(t, "pydantic") and isinstance(
                getattr(t, "pydantic", None), model_cls
            ):
                return t.pydantic
            if isinstance(t, (dict, str)):
                built = _try_build(t)
                if built is not None:
                    return built
            for attr in attr_order:
                if not hasattr(t, attr):
                    continue
                built = _try_build(getattr(t, attr))
                if built is not None:
                    return built

    return None


def _extract_analysis_report_raw(result: Any) -> Optional["AnalysisReport"]:
    return _extract_pydantic_from_result(
        result,
        AnalysisReport,
        ("project_name", "summary", "score"),
    )


def extract_analysis_report(
    result: Any, *, mode: Optional[str] = None
) -> Optional["AnalysisReport"]:
    """Extract AnalysisReport across CrewAI versions."""
    return _normalize_analysis_report(_extract_analysis_report_raw(result), mode=mode)


def _extract_code_bundle_raw(result: Any) -> Optional[CodeBundle]:
    return _extract_pydantic_from_result(
        result,
        CodeBundle,
        ("project_type", "files"),
        attr_order=("raw", "output", "json_dict"),
    )


def extract_code_bundle(result: Any) -> Optional[CodeBundle]:
    """Extract and sanitize CodeBundle across CrewAI versions."""
    return _sanitize_code_bundle(_extract_code_bundle_raw(result))


def _bundle_has_files(bundle: Optional[CodeBundle]) -> bool:
    try:
        return bool(bundle and list(bundle.files or []))
    except Exception:
        return False


_UNESCAPE_MAX_PASSES = 5


def _syntax_ok_for_unescape(src: str) -> bool:
    try:
        compile(src, "<unescape_check>", "exec")
        return True
    except Exception:
        return False


def _do_one_unescape_pass(content: str) -> str:
    """Single pass of escape-sequence reduction.

    Protects genuine double-backslashes (``\\\\``) with a placeholder, reduces
    ``\\n`` / ``\\t`` / ``\\"`` / ``\\'`` to their literal counterparts, then
    restores the placeholders.  Returns *content* unchanged when there is
    nothing to reduce.
    """
    if not content:
        return content
    if "\\" not in content:
        return content
    out = content
    out = out.replace("\\\\", "\x00_BSLASH_\x00")
    out = out.replace("\\n", "\n")
    out = out.replace("\\t", "\t")
    out = out.replace("\\r", "\r")
    out = out.replace('\\"', '"')
    out = out.replace("\\'", "'")
    out = out.replace("\x00_BSLASH_\x00", "\\")
    return out


def _unescape_llm_code_content(content: str) -> str:
    """Fix multi-level-escaped code content from LLM responses.

    Some models return generated code with literal escape sequences after
    JSON parsing:
    - ``\\n`` as two characters (backslash + n) instead of an actual newline
    - ``\\"`` as two characters (backslash + quote) instead of a quote
    - ``\\t`` as two characters (backslash + t) instead of a tab

    This causes ``compile()`` to fail with
    ``SyntaxError: unexpected character after line continuation character``.

    A subset of LLMs (notably reasoning-class models running under STRICT_JSON)
    apply the JSON escape pass *twice* — emitting ``\\\\n`` (3 chars) and
    ``\\\\\\"`` (4 chars) instead of the single-escape forms above.  After one
    reduction pass the content is still half-escaped (``\\n``, ``\\"`` left
    over) and compile() still fails — historically that left codegen burning
    LLM tokens on syntax-repair calls for what is a deterministic substitution.
    The pass is therefore iterated up to :data:`_UNESCAPE_MAX_PASSES` times,
    stopping early when the result either compiles cleanly or reaches a fixed
    point (no further reduction possible).

    Strategy: attempt to unescape, then use ``compile()`` to decide whether
    the unescaped version is an improvement.  This avoids false positives
    from content that legitimately contains backslash sequences (regex
    patterns, Windows paths, etc.).  When neither the original nor any
    intermediate reduction compiles, the most-reduced version is returned —
    it is structurally closer to what the LLM intended and gives downstream
    validators a more meaningful error to surface.
    """
    if not content:
        return content

    # Quick reject: no backslash → no escape sequences → nothing to do.
    if "\\" not in content:
        return content

    # Step 1: Try compiling the original content.
    if _syntax_ok_for_unescape(content):
        return content  # Original compiles fine — don't touch it.

    # Step 2: Iteratively reduce escape sequences until either the result
    # compiles, no further reduction is possible (fixed point), or we hit
    # the defensive cap.  ``_UNESCAPE_MAX_PASSES = 5`` covers the realistic
    # ceiling — even pathological triple-encoded content needs at most 3
    # passes; the extra headroom guards against unforeseen escape patterns.
    current = content
    for _ in range(_UNESCAPE_MAX_PASSES):
        nxt = _do_one_unescape_pass(current)
        if nxt == current:
            break  # Fixed point — no more reduction possible.
        current = nxt
        if _syntax_ok_for_unescape(current):
            return current  # Unescape fixed the syntax error.

    # Neither original nor any intermediate compiles.  Return the most-
    # reduced form — it's structurally closer to what the LLM intended,
    # so downstream validators emit clearer errors and the
    # syntax-repair LLM call (if any) sees a less-confusing input.
    return current


# ──────────────────────────────────────────────────────────────────────────────
# CJK / fullwidth punctuation repair
# ──────────────────────────────────────────────────────────────────────────────
# When the LLM is prompted in Chinese it routinely emits Chinese-language
# punctuation (e.g. ``。`` U+3002, ``（`` U+FF08, ``，`` U+FF0C) inside generated
# Python source.  ``compile()`` rejects these as ``invalid character`` /
# ``invalid decimal literal``, which previously triggered the syntax-repair
# supplement / cross-batch repair loop / AutoOptimize round to keep retrying
# the same kind of mistake — burning hours of LLM time and producing no
# progress (the ``infinite codegen retry`` symptom).
#
# Mechanical repair is safe because Python ``compile()`` short-circuits on the
# first invalid token: by replacing **only** the character at the reported
# ``(lineno, offset)`` we never touch CJK punctuation that legitimately lives
# inside string literals or comments (those don't trigger SyntaxError, so
# compile() never points at them).  When the repair table cannot resolve the
# reported character we abort — the file is returned to the LLM-driven repair
# path with its original content, so this layer only ever helps and never
# loses information.
_CJK_PUNCT_TO_ASCII: Dict[str, str] = {
    # Sentence terminators / list separators
    "。": ".",       # U+3002 ideographic full stop
    "．": ".",       # U+FF0E fullwidth full stop
    "，": ",",       # U+FF0C fullwidth comma
    "、": ",",       # U+3001 ideographic comma
    "；": ";",       # U+FF1B fullwidth semicolon
    "：": ":",       # U+FF1A fullwidth colon
    # Brackets
    "（": "(",       # U+FF08 fullwidth left parenthesis
    "）": ")",       # U+FF09 fullwidth right parenthesis
    "［": "[",       # U+FF3B fullwidth left square bracket
    "］": "]",       # U+FF3D fullwidth right square bracket
    "｛": "{",       # U+FF5B fullwidth left curly bracket
    "｝": "}",       # U+FF5D fullwidth right curly bracket
    "〔": "[",       # U+3014 left tortoise shell bracket
    "〕": "]",       # U+3015 right tortoise shell bracket
    "【": "[",       # U+3010 left black lenticular bracket
    "】": "]",       # U+3011 right black lenticular bracket
    "「": '"',       # U+300C left corner bracket
    "」": '"',       # U+300D right corner bracket
    "『": '"',       # U+300E left white corner bracket
    "』": '"',       # U+300F right white corner bracket
    "《": "<",       # U+300A left double angle bracket
    "》": ">",       # U+300B right double angle bracket
    "〈": "<",       # U+3008 left angle bracket
    "〉": ">",       # U+3009 right angle bracket
    # Quotes
    "“": '"',        # U+201C left double quotation
    "”": '"',        # U+201D right double quotation
    "‘": "'",        # U+2018 left single quotation
    "’": "'",        # U+2019 right single quotation
    # Operators
    "！": "!",       # U+FF01 fullwidth exclamation mark
    "？": "?",       # U+FF1F fullwidth question mark
    "％": "%",       # U+FF05 fullwidth percent sign
    "＋": "+",       # U+FF0B fullwidth plus sign
    "－": "-",       # U+FF0D fullwidth hyphen-minus
    "＊": "*",       # U+FF0A fullwidth asterisk
    "／": "/",       # U+FF0F fullwidth solidus
    "＼": "\\",      # U+FF3C fullwidth reverse solidus
    "＝": "=",       # U+FF1D fullwidth equals sign
    "＜": "<",       # U+FF1C fullwidth less-than sign
    "＞": ">",       # U+FF1E fullwidth greater-than sign
    "＆": "&",       # U+FF06 fullwidth ampersand
    "｜": "|",       # U+FF5C fullwidth vertical line
    "～": "~",       # U+FF5E fullwidth tilde
    "＾": "^",       # U+FF3E fullwidth circumflex accent
    "＠": "@",       # U+FF20 fullwidth commercial at
    "＃": "#",       # U+FF03 fullwidth number sign
    "＄": "$",       # U+FF04 fullwidth dollar sign
    "｀": "`",       # U+FF40 fullwidth grave accent
    "＿": "_",       # U+FF3F fullwidth low line
    # Misc
    "—": "-",        # U+2014 em dash (very common in Chinese text)
    "–": "-",        # U+2013 en dash
    "…": "...",      # U+2026 horizontal ellipsis
    "·": ".",        # U+00B7 middle dot
    "→": "->",       # U+2192 rightwards arrow
}


_CJK_REPAIR_MAX_ITERATIONS = 500
_CJK_REPAIR_TARGETED_ERRORS = (
    "invalid character",
    "invalid decimal literal",
    "invalid non-printable character",
    "invalid syntax",
)


def _repair_cjk_punctuation_in_python_source(content: str, path: str) -> str:
    """Repair CJK / fullwidth punctuation that breaks Python ``compile()``.

    Only mutates a single character at a time, at the exact ``(lineno,
    offset)`` reported by the next SyntaxError, and only when that character
    has a known ASCII equivalent in :data:`_CJK_PUNCT_TO_ASCII`.  Returns
    the (possibly identical) original content when:

    - The path does not look like a Python module.
    - The original source already compiles cleanly.
    - The first SyntaxError is not at a CJK character we know how to repair
      (e.g. an unterminated string literal — that needs the LLM, not us).
    - Iteration cap is reached (defensive — should not happen in practice).
    """
    if not content or not isinstance(content, str):
        return content
    if not (path or "").endswith(".py"):
        return content

    repaired = content
    seen_keys: Set[Tuple[int, int]] = set()
    for _ in range(_CJK_REPAIR_MAX_ITERATIONS):
        try:
            compile(repaired, path or "<cjk_repair>", "exec")
            return repaired  # Clean — done.
        except SyntaxError as exc:
            lineno = getattr(exc, "lineno", None)
            offset = getattr(exc, "offset", None)
            msg = str(getattr(exc, "msg", "") or "")
            if not isinstance(lineno, int) or not isinstance(offset, int):
                return content  # No location info; bail to original.
            if lineno < 1 or offset < 1:
                return content
            if not any(token in msg.lower() for token in _CJK_REPAIR_TARGETED_ERRORS):
                # SyntaxError is something else (e.g. unterminated string) —
                # not our problem.  Fall through to LLM-driven repair.
                return content
            key = (lineno, offset)
            if key in seen_keys:
                # Already tried to repair this exact spot and it didn't help —
                # something deeper is wrong; stop touching the file.
                return content
            seen_keys.add(key)
            lines = repaired.split("\n")
            if lineno > len(lines):
                return content
            target_line = lines[lineno - 1]
            col = offset - 1
            if col >= len(target_line):
                # Reported offset past EOL — try the previous char (some
                # Python versions report the position of the *next* token).
                col = len(target_line) - 1
            if col < 0:
                return content
            ch = target_line[col]
            replacement = _CJK_PUNCT_TO_ASCII.get(ch)
            if replacement is None:
                # Try one column to the left — Python sometimes reports the
                # position of the token *following* the bad character (e.g.
                # ``invalid decimal literal`` points at the digit, but the
                # offending CJK punctuation precedes it).
                if col - 1 >= 0:
                    ch_prev = target_line[col - 1]
                    replacement_prev = _CJK_PUNCT_TO_ASCII.get(ch_prev)
                    if replacement_prev is not None:
                        target_line = (
                            target_line[: col - 1]
                            + replacement_prev
                            + target_line[col:]
                        )
                        lines[lineno - 1] = target_line
                        repaired = "\n".join(lines)
                        continue
                # No known repair for this character — give up.
                return content
            target_line = target_line[:col] + replacement + target_line[col + 1 :]
            lines[lineno - 1] = target_line
            repaired = "\n".join(lines)
        except (TypeError, ValueError, MemoryError):
            return content
    return content


def _sanitize_code_bundle(bundle: Optional[CodeBundle]) -> Optional[CodeBundle]:
    if bundle is None:
        return None
    try:
        normalized_project_type = str(getattr(bundle, "project_type", "") or "").strip().lower()
        if normalized_project_type not in {"quant", "saas", "agent", "scientist"}:
            return None
        normalized_files: List[GeneratedFile] = []
        by_path: Dict[str, GeneratedFile] = {}
        order: List[str] = []
        for f in list(bundle.files or []):
            raw_path = getattr(f, "path", "")
            if not _is_safe_bundle_path_input(raw_path):
                continue
            key = _normalize_bundle_relpath(raw_path)
            if not key or not _is_safe_bundle_relpath(key):
                continue
            content = getattr(f, "content", "")
            if not isinstance(content, str):
                content = "" if content is None else str(content)
            # Fix double-escaped content from LLM responses.
            content = _unescape_llm_code_content(content)
            # Mechanically repair CJK / fullwidth punctuation that breaks
            # Python ``compile()`` — see ``_repair_cjk_punctuation_in_python_source``.
            # This must run BEFORE any syntax-validation gate so the repair
            # supplement / cross-batch repair loop never wastes LLM tokens
            # on errors a deterministic substitution can fix.
            content = _repair_cjk_punctuation_in_python_source(content, key)
            normalized = GeneratedFile(path=key, content=content)
            if key not in by_path:
                order.append(key)
            by_path[key] = normalized
        for key in order:
            normalized_files.append(by_path[key])
        return CodeBundle(project_type=normalized_project_type, files=normalized_files)
    except Exception:
        return None


def _extract_review_report_raw(result: Any) -> Optional[ReviewReport]:
    return _extract_pydantic_from_result(
        result,
        ReviewReport,
        ("passes", "issues"),
    )


def extract_review_report(result: Any) -> Optional[ReviewReport]:
    """Extract ReviewReport across CrewAI versions."""
    return _extract_review_report_raw(result)


def _extract_direction_decision_raw(result: Any) -> Optional["DirectionDecision"]:
    return _extract_pydantic_from_result(
        result,
        DirectionDecision,
        (
            "selected_direction",
            "summary",
            "options",
            "go_conditions",
            "kill_criteria",
            "confidence",
            "verify_plan",
        ),
    )


def extract_direction_decision(result: Any) -> Optional["DirectionDecision"]:
    """Extract and normalize DirectionDecision across CrewAI versions."""
    return _normalize_direction_decision(_extract_direction_decision_raw(result))


def _extract_direction_comparator_report_raw(
    result: Any,
) -> Optional["DirectionComparatorReport"]:
    return _extract_pydantic_from_result(
        result,
        DirectionComparatorReport,
        ("items", "top_keys", "comparison_notes"),
    )


def extract_direction_comparator_report(
    result: Any,
) -> Optional["DirectionComparatorReport"]:
    """Extract and normalize DirectionComparatorReport across CrewAI versions."""
    parsed = _extract_direction_comparator_report_raw(result)
    if parsed is None:
        return None
    return _normalize_direction_comparator_report_instance(parsed)


def _extract_evidence_audit_report_raw(result: Any) -> Optional["EvidenceAuditReport"]:
    return _extract_pydantic_from_result(
        result,
        EvidenceAuditReport,
        ("items", "top_keys", "global_warnings"),
    )


def extract_evidence_audit_report(result: Any) -> Optional["EvidenceAuditReport"]:
    """Extract and normalize EvidenceAuditReport across CrewAI versions."""
    parsed = _extract_evidence_audit_report_raw(result)
    if parsed is None:
        return None
    return _normalize_evidence_audit_report_instance(parsed)


def _preprocess_research_context_dict(d: dict) -> dict:
    """Normalise common format mismatches before passing a dict to ResearchContext(**d).

    GLM-5.1 (and other reasoning models) often:
      - Emit hallucination_flags / unknowns as list[dict] instead of list[str]
      - Emit claim_attributions without the required 'category' key
      - Emit citations without the required 'provider' / 'title' keys
      - Emit evidence_coverage with list / dict values instead of int
      - Omit user_problem and search_strategy entirely

    We fix all of these so Pydantic validation succeeds without inventing
    substantive new data.
    """
    d = dict(d)  # shallow copy — never mutate the caller's dict

    # ── user_problem / search_strategy: Pydantic Field(...) means no default ──
    if not isinstance(d.get("user_problem"), str) or not d.get("user_problem"):
        # Try to infer from lane-report fields before falling back to ""
        d["user_problem"] = str(d.get("core_objective") or "")
    if not isinstance(d.get("search_strategy"), str):
        d["search_strategy"] = ""

    # ── hallucination_flags: must be List[str] ────────────────────────────────
    flags = d.get("hallucination_flags")
    if isinstance(flags, list):
        coerced: List[str] = []
        for f in flags:
            if isinstance(f, str):
                coerced.append(f)
            elif isinstance(f, dict):
                text = str(
                    f.get("filtered_claim") or f.get("claim")
                    or f.get("flag") or f.get("text") or ""
                )
                reason = str(f.get("reason") or "")
                combined = f"{text}: {reason}".strip(": ")
                coerced.append(combined or str(f))
        d["hallucination_flags"] = [s for s in coerced if s]

    # ── unknowns: must be List[str] ───────────────────────────────────────────
    unknowns = d.get("unknowns")
    if isinstance(unknowns, list):
        coerced_u: List[str] = []
        for u in unknowns:
            if isinstance(u, str):
                coerced_u.append(u)
            elif isinstance(u, dict):
                text = str(
                    u.get("topic") or u.get("question")
                    or u.get("unknown") or u.get("text") or ""
                )
                gap = str(u.get("evidence_gap") or u.get("gap") or "")
                combined = f"{text}: {gap}".strip(": ")
                coerced_u.append(combined or str(u))
        d["unknowns"] = [s for s in coerced_u if s]

    # ── claim_attributions: List[ClaimAttribution], 'category'+'claim' required
    attrs = d.get("claim_attributions")
    if isinstance(attrs, list):
        valid_attrs: List[dict] = []
        for a in attrs:
            if not isinstance(a, dict):
                continue
            a = dict(a)
            if not a.get("category"):
                a["category"] = str(
                    a.get("claim_type") or a.get("type")
                    or a.get("area") or "general"
                )
            if not a.get("claim"):
                a["claim"] = str(
                    a.get("text") or a.get("finding")
                    or a.get("assertion") or a.get("statement") or ""
                )
            if a.get("claim") and a.get("category"):
                valid_attrs.append(a)
        d["claim_attributions"] = valid_attrs

    # ── citations: List[ResearchCitation], 'provider'+'title'+'url' required ─
    cits = d.get("citations")
    if isinstance(cits, list):
        valid_cits: List[dict] = []
        for c in cits:
            if not isinstance(c, dict):
                continue
            c = dict(c)
            if not c.get("url"):
                continue  # url is absolutely required; skip without it
            if not c.get("provider"):
                domain = str(c.get("domain") or c.get("source_domain") or "")
                if "github" in domain:
                    c["provider"] = "github"
                elif "arxiv" in domain:
                    c["provider"] = "arxiv"
                elif "paperswithcode" in domain:
                    c["provider"] = "paperswithcode"
                else:
                    c["provider"] = "websearch" if domain else "unknown"
            if not c.get("title"):
                c["title"] = str(
                    c.get("name") or c.get("source") or c.get("url") or ""
                )
            valid_cits.append(c)
        d["citations"] = valid_cits

    # ── evidence_coverage: Dict[str, int] — drop any non-int-coercible values ─
    ev_cov = d.get("evidence_coverage")
    if isinstance(ev_cov, dict):
        coerced_ev: Dict[str, int] = {}
        for k, v in ev_cov.items():
            if isinstance(v, int):
                coerced_ev[str(k)] = v
            elif isinstance(v, (float, str)):
                try:
                    coerced_ev[str(k)] = int(float(str(v)))
                except (ValueError, TypeError):
                    pass
            # list / dict values are silently dropped (not Dict[str, int]-compatible)
        d["evidence_coverage"] = coerced_ev

    return d


def _extract_research_context_raw(result: Any) -> Optional["ResearchContext"]:
    """Extract ResearchContext with lenient field validation.

    Uses only 'synthesized_summary' as the gate key (all other ResearchContext
    fields either have Pydantic defaults or are injected by
    _preprocess_research_context_dict), so partial formatter outputs — which
    previously needed all 15 fields — now succeed on the first try.
    """

    def _try_build(candidate: Any) -> Optional["ResearchContext"]:
        d = _coerce_json_dict(candidate)
        if not isinstance(d, dict):
            return None
        # Only require synthesized_summary; everything else has a Pydantic default
        # or will be filled-in by _preprocess_research_context_dict.
        if "synthesized_summary" not in d:
            return None
        d = _preprocess_research_context_dict(d)
        try:
            return ResearchContext(**d)
        except Exception:
            return None

    if isinstance(result, ResearchContext):
        return result
    if hasattr(result, "pydantic") and isinstance(
        getattr(result, "pydantic", None), ResearchContext
    ):
        return result.pydantic

    if isinstance(result, (dict, str)):
        built = _try_build(result)
        if built is not None:
            return built

    for attr in ("json_dict", "output", "raw"):
        if hasattr(result, attr):
            built = _try_build(getattr(result, attr))
            if built is not None:
                return built

    if hasattr(result, "tasks_output") and result.tasks_output:
        try:
            task_outputs = list(result.tasks_output)
        except Exception:
            task_outputs = []
        for t in reversed(task_outputs):
            if isinstance(t, ResearchContext):
                return t
            if hasattr(t, "pydantic") and isinstance(
                getattr(t, "pydantic", None), ResearchContext
            ):
                return t.pydantic
            if isinstance(t, (dict, str)):
                built = _try_build(t)
                if built is not None:
                    return built
            for attr in ("json_dict", "output", "raw"):
                if not hasattr(t, attr):
                    continue
                built = _try_build(getattr(t, attr))
                if built is not None:
                    return built

    return None


def extract_research_context(result: Any) -> Optional["ResearchContext"]:
    """Extract and stabilize ResearchContext across CrewAI versions."""
    parsed = _extract_research_context_raw(result)
    if parsed is None:
        return None
    return _stabilize_research_context(parsed)


def _extract_gate_decision_raw(result: Any) -> Optional["GateDecision"]:
    return _extract_pydantic_from_result(
        result,
        GateDecision,
        ("consensus", "disagreement", "experiments"),
    )


def extract_gate_decision(result: Any) -> Optional["GateDecision"]:
    """Extract and normalize GateDecision across CrewAI versions."""
    return _normalize_gate_decision(_extract_gate_decision_raw(result))


# Legacy formatter/prompt snapshots are kept only for audit/reference.
# Active runtime paths must use the canonical helpers and non-legacy builders below.
def _legacy_build_research_context_reformat_description(
    *, raw_text: str, language_hint: str
) -> str:
    return (
        "請把 INPUT 轉成完整的 ResearchContext JSON，所有欄位都必須存在：\n"
        "- user_problem: string\n"
        "- search_strategy: string\n"
        "- providers_used: list[string]\n"
        "- suggested_search_queries: list[string]\n"
        "- market_examples: list[string]\n"
        "- existing_tools: list[string]\n"
        "- technical_patterns: list[string]\n"
        "- key_risks: list[string]\n"
        "- unknowns: list[string]\n"
        "- synthesized_summary: string\n"
        "- citations: list of {provider,title,url,snippet,query}\n"
        "- provider_errors: dict\n"
        "- evidence_coverage: dict\n"
        "- hallucination_flags: list[string]\n"
        "- claim_attributions: list of {category,claim,citation_indices,citation_urls,support_score}\n\n"
        "規則：\n"
        "- 只能保留有根據的 grounded claims；不得捏造 tools、risks、patterns、citations、URLs 或 providers。\n"
        "- 若支持度不明確，應移到 unknowns 或 hallucination_flags，不可寫成既定事實。\n"
        '- 若 INPUT 缺少欄位，請使用保守空值（[], {} 或 ""），不要自行補內容。\n'
        "- claim_attributions 只能引用 citations 裡已存在的項目。\n"
        "- 只輸出 JSON，不要 markdown，不要額外文字。\n\n"
        f"語言：{language_hint}\n\n"
        "INPUT：\n" + _limit_reformat_input(raw_text)
    )


def _legacy_build_direction_decision_reformat_description(
    *, raw_text: str, language_hint: str
) -> str:
    return (
        "請把 INPUT 轉成完整的 DirectionDecision JSON，所有欄位都必須存在：\n"
        '- selected_direction: "A"|"B"|"C"|"D"|"E"|"F"|"G"|"none"\n'
        "- summary: string\n"
        "- options: 恰好 7 項，key 必須覆蓋 A..G；每項包含 name/thesis/primary_metric/fastest_test/major_risk\n"
        "- backup_candidates: 0-2 個方向 key，且不得包含 selected_direction\n"
        "- go_conditions: 1-5 strings\n"
        "- kill_criteria: 1-5 strings\n"
        '- confidence: "low"|"medium"|"high"\n'
        "- verify_plan: 1-5 strings\n\n"
        "規則：\n"
        "- 只能保留可從 INPUT 恢復出的方向與 claims。\n"
        "- 不得新增 INPUT 中不存在的 directions、metrics、risks 或 experiments。\n"
        "- 若細節不足，請使用保守占位值，例如「insufficient evidence」，不可編造具體內容。\n"
        "- 只輸出 JSON，不要 markdown，不要額外文字。\n\n"
        f"語言：{language_hint}\n\n"
        "INPUT：\n" + _limit_reformat_input(raw_text)
    )


def _json_formatter_backstory(schema_name: str) -> str:
    return (
        "You are a strict JSON formatter.\n"
        f"Convert the provided input into valid {schema_name} JSON only.\n"
        "Do not add facts, markdown fences, commentary, or unsupported fields."
    )


def _run_schema_reformatter(
    *,
    cache_namespace: str,
    cache_payload: Dict[str, Any],
    model_cls: Any,
    raw_text: str,
    llm: Any,
    role: str,
    goal: str,
    description: str,
    expected_output: str,
    parse_fn: Callable[[Any], Optional[Any]],
    postprocess_fn: Optional[Callable[[Any], Optional[Any]]] = None,
    validate_fn: Optional[Callable[[Any], bool]] = None,
    cost_trace_stage: Optional[str] = None,
    error_label: str,
    formatter_max_tokens: Optional[int] = None,
) -> Optional[Any]:
    cached = _cache_get_pydantic(cache_namespace, cache_payload, model_cls)
    if cached is not None:
        if postprocess_fn is not None:
            cached = postprocess_fn(cached)
        if cached is not None and (validate_fn is None or validate_fn(cached)):
            return cached

    # Use a capped formatter LLM to prevent reasoning models from generating
    # 65 000+ tokens of JSON output that exceeds the API cap and gets truncated.
    # ``formatter_max_tokens`` lets callers raise the cap for large payloads like
    # CodeBundle (which can contain complete source files totalling 10 000+ tokens).
    formatter_llm = _make_formatter_llm(llm, max_tokens=formatter_max_tokens)
    formatter = Agent(
        role=role,
        goal=goal,
        backstory=_json_formatter_backstory(model_cls.__name__),
        allow_delegation=False,
        verbose=False,
        llm=formatter_llm,
    )
    task_kwargs = {
        "description": description,
        "agent": formatter,
        "expected_output": expected_output,
    }
    if STRICT_JSON_ENABLED and CREWAI_OUTPUT_PYDANTIC:
        task_kwargs["output_pydantic"] = model_cls
    task = Task(**task_kwargs)
    crew = Crew(
        agents=[formatter], tasks=[task], process=Process.sequential, verbose=False
    )
    try:
        result = _kickoff_reformat_crew(
            crew,
            crew_name=f"{cache_namespace}_reformat",
            raw_text=raw_text,
            cost_trace_stage=cost_trace_stage,
            error_label=error_label,
        )
        if result is None:
            return None
    except _OperationCancelledError:
        raise
    except Exception as e:
        print(f"[Error] {error_label} failed: {e}")
        return None

    parsed = parse_fn(result)
    if parsed is not None and postprocess_fn is not None:
        parsed = postprocess_fn(parsed)
    if parsed is None:
        return None
    if validate_fn is not None and not validate_fn(parsed):
        return None
    _cache_set_pydantic(cache_namespace, cache_payload, parsed)
    return parsed


def _build_direction_decision_reformat_description(
    *, raw_text: str, language_hint: str
) -> str:
    return (
        "Reformat the INPUT into a valid DirectionDecision JSON object.\n"
        "Required fields:\n"
        '- selected_direction: "A"|"B"|"C"|"D"|"E"|"F"|"G"|"none"\n'
        "- summary: string\n"
        "- options: exactly 7 items with keys A..G; each item must include name, thesis, primary_metric, fastest_test, and major_risk\n"
        "- backup_candidates: 0-2 direction keys and they must exclude selected_direction\n"
        "- go_conditions: 1-5 strings\n"
        "- kill_criteria: 1-5 strings\n"
        '- confidence: "low"|"medium"|"high"\n'
        "- verify_plan: 1-5 strings\n\n"
        "Rules:\n"
        "- Do not invent claims, directions, metrics, risks, or experiments that are not supported by the INPUT.\n"
        "- Do not add directions that do not exist in the INPUT.\n"
        "- If the INPUT lacks enough evidence, keep the structure valid but remain conservative and reflect insufficient evidence in the summary.\n"
        "- Return JSON only.\n\n"
        f"Language hint: {language_hint}\n\n"
        "INPUT:\n" + _limit_reformat_input(raw_text)
    )


def _legacy_reformat_gate_decision(
    raw_text: str, *, llm: Any, language_hint: str
) -> Optional["GateDecision"]:
    """Reformat raw text into GateDecision using a formatter agent."""
    cache_payload = {
        "model": _reformat_llm_model_id(llm),
        "llm_provider": _reformat_llm_provider_name(llm),
        "strict_json": bool(STRICT_JSON_ENABLED),
        "language_hint": language_hint,
        "raw_text_len": len(raw_text or ""),
        "raw_text_sha256": _text_sha256(raw_text or ""),
    }
    cached = _cache_get_pydantic("reformat_gate_decision", cache_payload, GateDecision)
    if cached is not None:
        return _normalize_gate_decision(cached)

    formatter = Agent(
        role="Gate Decision Formatter",
        goal="把內容轉成合法的 GateDecision JSON 物件。",
        backstory=(
            "你是嚴格的 JSON formatter。"
            "只能輸出一個符合 GateDecision 的 JSON 物件。"
            "不要 markdown、不要 code fence、不要額外文字。"
        ),
        allow_delegation=False,
        verbose=False,
        llm=_make_formatter_llm(llm),
    )
    task_kwargs = {
        "description": (
            "請把 INPUT 轉成合法的 GateDecision JSON，且所有欄位都必須存在：\n"
            "- consensus: string (各角色明確一致的結論)\n"
            "- disagreement: string (角色間衝突的假設、判斷或風險認知)\n"
            "- experiments: list of {goal, criteria}\n"
            "- ready_for_codegen: boolean (是否準備好進入 CodeGen)\n"
            "- blocking_risks: list of strings (阻斷性風險清單)\n"
            "- required_experiments_before_codegen: list of strings\n"
            "- advisory_experiments_after_codegen: list of strings\n"
            "- codegen_scope: 'production'|'validation'\n"
            "- validation_scope_reason: string|null\n"
            "- validation_objectives: list of strings\n"
            "- agents_needing_rerun: list of agent names that need re-execution\n"
            "- rerun_reasons: object mapping agent_name -> reason\n"
            "- direction_feedback_needed: boolean\n"
            "- direction_feedback_reason: string|null\n"
            "- direction_feedback_type: 'evidence'|'detail'|null\n"
            "- direction_feedback_evidence_gaps: list of strings\n"
            "- direction_feedback_questions: list of strings\n"
            "- overall_score: integer 0-100\n"
            "- score_breakdown: object with feasibility/risk/roi/uncertainty (0-100)\n"
            '- confidence: "low"|"medium"|"high"\n'
            '- failure_type: "NONE"|"JSON_INVALID"|"EXECUTION_ERROR"|"LOW_CONFIDENCE"|"COST_OVER_BUDGET"|"CONFLICTING_OUTPUT"|"POLICY_VIOLATION"|"NON_DETERMINISTIC"\n'
            "- failure_details: string|null\n"
            "- should_kill: boolean (是否應該終止流程)\n\n"
            "規則：\n"
            "- 只允許根據 INPUT 可恢復的資訊填欄。\n"
            "- 不得新增 INPUT 中不存在的結論或風險。\n"
            "- 只輸出 JSON，不要 markdown，不要額外文字。\n\n"
            f"語言：{language_hint}\n\n"
            "INPUT：\n" + _limit_reformat_input(raw_text)
        ),
        "agent": formatter,
        "expected_output": "GateDecision JSON only.",
    }
    if STRICT_JSON_ENABLED and CREWAI_OUTPUT_PYDANTIC:
        task_kwargs["output_pydantic"] = GateDecision
    task = Task(**task_kwargs)
    crew = Crew(
        agents=[formatter], tasks=[task], process=Process.sequential, verbose=False
    )
    try:
        result = _kickoff_reformat_crew(
            crew,
            crew_name="gate_decision_reformat",
            raw_text=raw_text,
            cost_trace_stage="gate_decision_reformat.kickoff",
            error_label="GateDecision reformat task",
        )
        if result is None:
            return None
    except _OperationCancelledError:
        raise
    except Exception as e:
        print(f"[Error] GateDecision reformat task failed: {e}")
        return None
    parsed = extract_gate_decision(result)
    if parsed is not None:
        parsed = _normalize_gate_decision(parsed)
    if parsed is not None:
        _cache_set_pydantic("reformat_gate_decision", cache_payload, parsed)
    return parsed


def _extract_text_from_result(result: Any) -> Optional[str]:
    """
    Best-effort extraction of raw text output from CrewAI results across versions.

    Used for "reformat" retries when the structured JSON parse fails.
    """
    if result is None:
        return None
    if isinstance(result, str):
        return result
    for attr in ("raw", "output", "text", "content"):
        try:
            v = getattr(result, attr, None)
        except Exception:
            v = None
        if isinstance(v, str) and v.strip():
            return v

    task_outputs = []
    if hasattr(result, "tasks_output"):
        try:
            task_outputs = list(getattr(result, "tasks_output") or [])
        except Exception:
            task_outputs = []
    for t in reversed(task_outputs):
        candidate_text = _coerce_json_text(t if not isinstance(t, str) else t)
        if candidate_text:
            return candidate_text
        for attr in ("json_dict", "raw", "output", "text", "content"):
            try:
                v = getattr(t, attr, None)
            except Exception:
                v = None
            candidate_text = _coerce_json_text(v)
            if candidate_text:
                return candidate_text

    try:
        s = str(result)
        return s if s.strip() else None
    except Exception:
        return None


def _collect_text_candidates_from_result(result: Any) -> List[str]:
    """
    Collect multiple text candidates from CrewAI results/tasks_output.

    Used for strict-mode "repair" steps when we need to reformat output into a
    schema. Ordering is best-effort: later items tend to be more relevant.
    """
    candidates: List[str] = []

    def _add(v: Any) -> None:
        s = _coerce_json_text(v)
        if not s:
            return
        candidates.append(s)

    if isinstance(result, str):
        _add(result)
    else:
        for attr in ("json_dict", "raw", "output", "text", "content"):
            try:
                _add(getattr(result, attr, None))
            except Exception:
                pass

    task_outputs: List[Any] = []
    if hasattr(result, "tasks_output"):
        try:
            task_outputs = list(getattr(result, "tasks_output") or [])
        except Exception:
            task_outputs = []

    for t in task_outputs:
        _add(t if isinstance(t, str) else None)
        for attr in ("json_dict", "raw", "output", "text", "content"):
            try:
                _add(getattr(t, attr, None))
            except Exception:
                pass

    # De-dup while preserving order.
    seen: Set[str] = set()
    uniq: List[str] = []
    for s in candidates:
        if s in seen:
            continue
        seen.add(s)
        uniq.append(s)
    return uniq


def _coerce_direction_option_payloads(value: Any) -> Optional[List[Dict[str, Any]]]:
    if not isinstance(value, list):
        return None
    normalized: List[Dict[str, Any]] = []
    seen: Set[str] = set()
    for raw_option in value:
        payload = _model_to_dict(raw_option)
        key = str(payload.get("key", "") or "").strip().upper()
        if key not in _DIRECTION_OPTION_KEYS or key in seen:
            return None
        option_payload = {
            "key": key,
            "name": str(payload.get("name", "") or "").strip(),
            "thesis": str(payload.get("thesis", "") or "").strip(),
            "primary_metric": str(payload.get("primary_metric", "") or "").strip(),
            "fastest_test": str(payload.get("fastest_test", "") or "").strip(),
            "major_risk": str(payload.get("major_risk", "") or "").strip(),
        }
        if not all(option_payload.values()):
            return None
        normalized.append(option_payload)
        seen.add(key)
    if tuple(sorted(seen)) != _DIRECTION_OPTION_KEYS:
        return None
    return sorted(normalized, key=lambda item: item["key"])


def _extract_first_json_array(text: str) -> Optional[List[Any]]:
    if not text:
        return None
    # Reasoning-model defence: any ``[…]`` array literal embedded inside a
    # ``<think>…</think>`` block is part of the model's chain-of-thought, not
    # the real answer.  Strip those blocks before scanning so the forward
    # ``raw_decode`` does not capture an example/draft array as the result.
    text = _strip_reasoning_blocks(text)

    def _decode_first_array(candidate: str) -> Optional[List[Any]]:
        decoder = json.JSONDecoder()
        stripped = candidate.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            try:
                parsed = json.loads(stripped)
            except Exception:
                parsed = None
            if isinstance(parsed, list):
                return parsed
        for index, ch in enumerate(candidate):
            if ch != "[":
                continue
            try:
                parsed, _ = decoder.raw_decode(candidate[index:])
            except Exception:
                continue
            if isinstance(parsed, list):
                return parsed
        return None

    direct = _decode_first_array(text)
    if direct is not None:
        return direct
    for match in JSON_FENCE_RE.finditer(text):
        fenced = _decode_first_array((match.group(1) or "").strip())
        if fenced is not None:
            return fenced
    return None


def _recover_direction_option_payloads_from_result(
    result: Any,
) -> Optional[List[Dict[str, Any]]]:
    structured_candidates = _collect_structured_candidates_from_result(result)
    for candidate in structured_candidates:
        option_payloads = _coerce_direction_option_payloads(candidate.get("options"))
        if option_payloads is not None:
            return option_payloads

    for candidate_text in _collect_text_candidates_from_result(result):
        object_payload = _extract_first_json_object(candidate_text)
        if isinstance(object_payload, dict):
            option_payloads = _coerce_direction_option_payloads(
                object_payload.get("options")
            )
            if option_payloads is not None:
                return option_payloads
        array_payload = _extract_first_json_array(candidate_text)
        option_payloads = _coerce_direction_option_payloads(array_payload)
        if option_payloads is not None:
            return option_payloads
    return None


def _salvage_direction_decision_from_result(
    result: Any,
) -> Optional["DirectionDecision"]:
    structured_candidates = _collect_structured_candidates_from_result(result)
    recovered_options = _recover_direction_option_payloads_from_result(result)

    for candidate in reversed(structured_candidates):
        selected_direction = str(candidate.get("selected_direction", "") or "").strip()
        summary = str(candidate.get("summary", "") or "").strip()
        if not selected_direction or not summary:
            continue
        payload = dict(candidate)
        if recovered_options is not None:
            payload["options"] = payload.get("options") or recovered_options
        payload["backup_candidates"] = payload.get("backup_candidates") or []
        payload["go_conditions"] = payload.get("go_conditions") or [
            "Proceed only after grounded evidence review is complete."
        ]
        payload["kill_criteria"] = payload.get("kill_criteria") or [
            "Stop if grounded evidence contradicts the selected direction."
        ]
        payload["verify_plan"] = payload.get("verify_plan") or [
            "Re-run validation against comparator and auditor evidence."
        ]
        payload["confidence"] = payload.get("confidence") or "low"
        try:
            return _normalize_direction_decision(DirectionDecision(**payload))
        except Exception:
            continue
    return None


def _build_provisional_direction_decision_from_stage_reports(
    result: Any,
    *,
    research_context: Optional[ResearchContext],
    comparator_report: Optional["DirectionComparatorReport"],
    audit_report: Optional["EvidenceAuditReport"],
) -> Optional["DirectionDecision"]:
    if research_context is None:
        return None
    recovered_options = _recover_direction_option_payloads_from_result(result)
    if recovered_options is None:
        return None

    shortlist = _structured_direction_shortlist(comparator_report, audit_report)
    ranked_shortlist = [
        (key, _structured_direction_option_score(key, comparator_report, audit_report))
        for key in shortlist
    ]
    ranked_shortlist = [(key, score) for key, score in ranked_shortlist if score > 0]
    ranked_shortlist.sort(key=lambda item: (-item[1], item[0]))
    selected_key = (
        ranked_shortlist[0][0]
        if ranked_shortlist
        else shortlist[0]
        if shortlist
        else ""
    )
    if not selected_key:
        return None

    option_map = {item["key"]: item for item in recovered_options}
    if selected_key not in option_map:
        return None

    coverage = dict(research_context.evidence_coverage or {})
    grounded_claims = int(coverage.get("grounded_claims") or 0)
    selected_audit = next(
        (
            item
            for item in list(getattr(audit_report, "items", []) or [])
            if item.key == selected_key
        ),
        None,
    )
    critical_unknowns = list(
        getattr(selected_audit, "decision_critical_unknowns", []) or []
    )
    unsupported_fields = list(getattr(selected_audit, "unsupported_fields", []) or [])

    go_conditions = [
        "Proceed only after validating the selected direction on fresh market data.",
        "Keep live risk guardrails active before any downstream build or deployment.",
    ]
    kill_criteria = [
        "Stop if forward validation breaks the expected drawdown or risk budget.",
        "Stop if grounded evidence contradicts the selected direction during validation.",
    ]
    verify_plan = [
        "Replay the short-listed direction on out-of-sample data and measure drawdown, win rate, and trade frequency.",
        "Re-check execution feasibility and slippage assumptions on the target venue before committing downstream work.",
    ]
    if critical_unknowns:
        verify_plan.append(
            "Resolve decision-critical unknowns first: "
            + "; ".join(critical_unknowns[:2])
        )
    elif research_context.unknowns:
        verify_plan.append(
            "Resolve remaining unknowns before scaling: "
            + "; ".join(list(research_context.unknowns or [])[:2])
        )

    summary = (
        f"Provisional fallback selected {selected_key} from structured explorer/comparator/auditor outputs "
        f"because the judge stage did not yield a parseable DirectionDecision. "
        f"Grounded claims: {grounded_claims}. "
        f"Unsupported fields: {len(unsupported_fields)}."
    ).strip()
    decision_payload = {
        "selected_direction": selected_key,
        "summary": summary,
        "options": recovered_options,
        "backup_candidates": _normalize_direction_key_list(
            shortlist,
            exclude={selected_key},
            limit=2,
        ),
        "go_conditions": go_conditions,
        "kill_criteria": kill_criteria,
        "confidence": "low",
        "verify_plan": verify_plan,
    }
    try:
        return _normalize_direction_decision(DirectionDecision(**decision_payload))
    except Exception:
        return None


def _get_task_output_at_index(result: Any, index: int) -> Any:
    if result is None or index < 0 or not hasattr(result, "tasks_output"):
        return None
    try:
        task_outputs = list(getattr(result, "tasks_output") or [])
    except Exception:
        return None
    if index >= len(task_outputs):
        return None
    return task_outputs[index]


def _get_task_outputs(result: Any) -> List[Any]:
    if result is None or not hasattr(result, "tasks_output"):
        return []
    try:
        return list(getattr(result, "tasks_output") or [])
    except Exception:
        return []


def _tag_direction_stage_task(task: Any, stage_name: str) -> Any:
    normalized_stage = str(stage_name or "").strip().lower()
    try:
        if isinstance(task, dict):
            task["_direction_stage_name"] = normalized_stage
        else:
            setattr(task, "_direction_stage_name", normalized_stage)
    except Exception:
        pass
    return task


def _build_direction_stage_index_map(tasks: Any) -> Dict[str, int]:
    stage_index_map: Dict[str, int] = {}
    try:
        task_list = list(tasks or [])
    except Exception:
        task_list = []
    for index, task in enumerate(task_list):
        stage_name = ""
        if isinstance(task, dict):
            stage_name = (
                str(task.get("_direction_stage_name", "") or "").strip().lower()
            )
        else:
            stage_name = (
                str(getattr(task, "_direction_stage_name", "") or "").strip().lower()
            )
        if stage_name and stage_name not in stage_index_map:
            stage_index_map[stage_name] = index
    return stage_index_map


def _legacy_reformat_analysis_report(
    raw_text: str, *, llm: Any, language_hint: str, mode: str
) -> Optional["AnalysisReport"]:
    cache_payload = {
        "model": _reformat_llm_model_id(llm),
        "llm_provider": _reformat_llm_provider_name(llm),
        "strict_json": bool(STRICT_JSON_ENABLED),
        "mode": mode,
        "language_hint": language_hint,
        "raw_text_len": len(raw_text or ""),
        "raw_text_sha256": _text_sha256(raw_text or ""),
    }
    cached = _cache_get_pydantic(
        "reformat_analysis_report", cache_payload, AnalysisReport
    )
    if cached is not None:
        return cached

    formatter = Agent(
        role="Analysis Formatter",
        goal="把內容轉成合法的 AnalysisReport JSON 物件。",
        backstory=(
            "你是嚴格的 JSON formatter。"
            "只能輸出一個符合 AnalysisReport 的 JSON 物件。"
            "不要 markdown、不要 code fence、不要額外文字。"
        ),
        allow_delegation=False,
        verbose=False,
        llm=_make_formatter_llm(llm),
    )
    task_kwargs = {
        "description": (
            "請把 INPUT 轉成合法的 AnalysisReport JSON，且所有欄位都必須存在：\n"
            "- project_name: short snake_case string\n"
            "- summary: string\n"
            "- consensus: string\n"
            "- disagreement: string\n"
            "- experiments: list of {goal, criteria}\n"
            "- score: integer 0-100\n"
            f"- mode_used: set exactly to {json.dumps(mode)}\n"
            '- risk_level: "Low"|"Medium"|"High"\n\n'
            "- 只保留可由 INPUT 支撐的內容，不得自行補事實。\n"
            "- 只輸出 JSON，不要 markdown，不要額外文字。\n\n"
            f"語言：{language_hint}\n\n"
            "INPUT：\n" + _limit_reformat_input(raw_text)
        ),
        "agent": formatter,
        "expected_output": "AnalysisReport JSON only.",
    }
    # Avoid CrewAI's output_pydantic by default because it can raise ValidationError
    # if the model returns trailing characters. We validate via our own JSON extraction.
    if STRICT_JSON_ENABLED and CREWAI_OUTPUT_PYDANTIC:
        task_kwargs["output_pydantic"] = AnalysisReport
    task = Task(**task_kwargs)
    crew = Crew(
        agents=[formatter], tasks=[task], process=Process.sequential, verbose=False
    )
    try:
        result = _kickoff_reformat_crew(
            crew,
            crew_name="analysis_reformat",
            raw_text=raw_text,
            cost_trace_stage="analysis_reformat.kickoff",
            error_label="AnalysisReport reformat task",
        )
        if result is None:
            return None
    except _OperationCancelledError:
        raise
    except Exception as e:
        print(f"[Error] AnalysisReport reformat task failed: {e}")
        return None
    parsed = extract_analysis_report(result, mode=mode)
    if parsed is not None:
        _cache_set_pydantic("reformat_analysis_report", cache_payload, parsed)
    return parsed


def _legacy_reformat_review_report(
    raw_text: str, *, llm: Any, language_hint: str
) -> Optional["ReviewReport"]:
    cache_payload = {
        "model": _reformat_llm_model_id(llm),
        "llm_provider": _reformat_llm_provider_name(llm),
        "strict_json": bool(STRICT_JSON_ENABLED),
        "language_hint": language_hint,
        "raw_text_len": len(raw_text or ""),
        "raw_text_sha256": _text_sha256(raw_text or ""),
    }
    cached = _cache_get_pydantic("reformat_review_report", cache_payload, ReviewReport)
    if cached is not None:
        return cached

    formatter = Agent(
        role="Review Formatter",
        goal="把內容轉成合法的 ReviewReport JSON 物件。",
        backstory=(
            "你是嚴格的 JSON formatter。"
            "只能輸出一個符合 ReviewReport 的 JSON 物件。"
            "不要 markdown、不要 code fence、不要額外文字。"
        ),
        allow_delegation=False,
        verbose=False,
        llm=_make_formatter_llm(llm),
    )
    task_kwargs = {
        "description": (
            "請把 INPUT 轉成合法的 ReviewReport JSON，且所有欄位都必須存在：\n"
            "- passes: boolean\n"
            "- summary: concise string\n"
            "- issues: list of {severity, category, description, file, suggestion}\n"
            '- severity must be "low" | "medium" | "high"\n'
            '- category must be "requirements" | "logic" | "bug" | "security" | "performance" | "usability" | "other"\n'
            "- file and suggestion may be null when unknown\n\n"
            "- 不得捏造不存在的 issue；未知欄位可用 null。\n"
            "- 只輸出 JSON，不要 markdown，不要額外文字。\n\n"
            f"語言：{language_hint}\n\n"
            "INPUT：\n" + _limit_reformat_input(raw_text)
        ),
        "agent": formatter,
        "expected_output": "ReviewReport JSON only.",
    }
    if STRICT_JSON_ENABLED and CREWAI_OUTPUT_PYDANTIC:
        task_kwargs["output_pydantic"] = ReviewReport
    task = Task(**task_kwargs)
    crew = Crew(
        agents=[formatter], tasks=[task], process=Process.sequential, verbose=False
    )
    try:
        result = _kickoff_reformat_crew(
            crew,
            crew_name="review_reformat",
            raw_text=raw_text,
            cost_trace_stage="review_reformat.kickoff",
            error_label="ReviewReport reformat task",
        )
        if result is None:
            return None
    except _OperationCancelledError:
        raise
    except Exception as e:
        print(f"[Error] ReviewReport reformat task failed: {e}")
        return None
    parsed = extract_review_report(result)
    if parsed is not None:
        _cache_set_pydantic("reformat_review_report", cache_payload, parsed)
    return parsed


def _legacy_reformat_code_bundle(
    raw_text: str, *, llm: Any, language_hint: str, mode: str
) -> Optional["CodeBundle"]:
    # Same token-cap fix as _reformat_code_bundle: CodeBundle output can be very
    # large; use CODEGEN_MAX_TOKENS so reasoning models don't exhaust the budget
    # entirely on chain-of-thought and produce zero code output.
    # Safe env-var parse: malformed values fall back to the default instead of
    # raising at runtime (would otherwise crash this reformat path with
    # ``ValueError: invalid literal for int()``).
    _codegen_reformat_max_tokens_env = _env_int("CODEGEN_MAX_TOKENS", 65536)
    _codegen_reformat_max_tokens: int = (
        _codegen_reformat_max_tokens_env
        if _codegen_reformat_max_tokens_env is not None
        and _codegen_reformat_max_tokens_env > 0
        else 65536
    )

    cache_payload = {
        "model": _reformat_llm_model_id(llm),
        "llm_provider": _reformat_llm_provider_name(llm),
        "strict_json": bool(STRICT_JSON_ENABLED),
        "mode": mode,
        "language_hint": language_hint,
        "raw_text_len": len(raw_text or ""),
        "raw_text_sha256": _text_sha256(raw_text or ""),
    }
    cached = _cache_get_pydantic("reformat_code_bundle", cache_payload, CodeBundle)
    if cached is not None:
        sanitized_cached = _sanitize_code_bundle(cached)
        if _bundle_has_files(sanitized_cached):
            return sanitized_cached

    formatter = Agent(
        role="CodeBundle Formatter",
        goal="把內容轉成合法的 CodeBundle JSON 物件。",
        backstory=(
            "你是嚴格的 JSON formatter。"
            "只能輸出一個符合 CodeBundle 的 JSON 物件。"
            "不要 markdown、不要 code fence、不要額外文字。"
        ),
        allow_delegation=False,
        verbose=False,
        llm=_make_formatter_llm(llm, max_tokens=_codegen_reformat_max_tokens),
    )
    task_kwargs = {
        "description": (
            "請把 INPUT 轉成合法的 CodeBundle JSON，且所有欄位都必須存在：\n"
            '- project_type: "saas", "quant", "agent", or "scientist"\n'
            "- files: list of {path, content}; path must be relative (no leading code/)\n"
            "- files 必須包含完整檔案內容。\n"
            "- 只輸出 JSON，不要 markdown，不要額外文字。\n\n"
            f"語言：{language_hint}\n"
            f"模式：{mode}\n\n"
            "INPUT：\n" + _limit_reformat_input(raw_text)
        ),
        "agent": formatter,
        "expected_output": "CodeBundle JSON only.",
    }
    if STRICT_JSON_ENABLED and CREWAI_OUTPUT_PYDANTIC:
        task_kwargs["output_pydantic"] = CodeBundle
    task = Task(**task_kwargs)
    crew = Crew(
        agents=[formatter], tasks=[task], process=Process.sequential, verbose=False
    )
    try:
        result = _kickoff_reformat_crew(
            crew,
            crew_name="code_bundle_reformat",
            raw_text=raw_text,
            cost_trace_stage="code_bundle_reformat.kickoff",
            error_label="CodeBundle reformat task",
        )
        if result is None:
            return None
    except _OperationCancelledError:
        raise
    except Exception as e:
        print(f"[Error] CodeBundle reformat task failed: {e}")
        return None
    parsed = extract_code_bundle(result)
    if parsed is not None:
        parsed = _sanitize_code_bundle(parsed)
    if _bundle_has_files(parsed):
        _cache_set_pydantic("reformat_code_bundle", cache_payload, parsed)
        return parsed
    return None


def _legacy_reformat_research_context(
    raw_text: str, *, llm: Any, language_hint: str
) -> Optional["ResearchContext"]:
    cache_payload = {
        "model": _reformat_llm_model_id(llm),
        "llm_provider": _reformat_llm_provider_name(llm),
        "strict_json": bool(STRICT_JSON_ENABLED),
        "language_hint": language_hint,
        "raw_text_len": len(raw_text or ""),
        "raw_text_sha256": _text_sha256(raw_text or ""),
    }
    cached = _cache_get_pydantic(
        "reformat_research_context", cache_payload, ResearchContext
    )
    if cached is not None:
        return _stabilize_research_context(cached)

    formatter = Agent(
        role="Research Context Formatter",
        goal="把內容轉成合法的 ResearchContext JSON 物件。",
        backstory=(
            "你是嚴格的 JSON formatter。"
            "只能輸出一個符合 ResearchContext 的 JSON 物件。"
            "不要 markdown、不要 code fence、不要額外文字。"
        ),
        allow_delegation=False,
        verbose=False,
        llm=_make_formatter_llm(llm),
    )
    task_kwargs = {
        "description": _build_research_context_reformat_description(
            raw_text=raw_text,
            language_hint=language_hint,
        ),
        "agent": formatter,
        "expected_output": "ResearchContext JSON only.",
    }
    if STRICT_JSON_ENABLED and CREWAI_OUTPUT_PYDANTIC:
        task_kwargs["output_pydantic"] = ResearchContext
    task = Task(**task_kwargs)
    crew = Crew(
        agents=[formatter], tasks=[task], process=Process.sequential, verbose=False
    )
    try:
        result = _kickoff_reformat_crew(
            crew,
            crew_name="research_context_reformat",
            raw_text=raw_text,
            cost_trace_stage="research_context_reformat.kickoff",
            error_label="ResearchContext reformat task",
        )
        if result is None:
            return None
    except _OperationCancelledError:
        raise
    except Exception as e:
        print(f"[Error] ResearchContext reformat task failed: {e}")
        return None
    parsed = extract_research_context(result)
    if parsed is not None:
        parsed = _stabilize_research_context(parsed)
        _cache_set_pydantic("reformat_research_context", cache_payload, parsed)
    return parsed


def _legacy_reformat_direction_decision(
    raw_text: str, *, llm: Any, language_hint: str
) -> Optional["DirectionDecision"]:
    cache_payload = {
        "model": _reformat_llm_model_id(llm),
        "llm_provider": _reformat_llm_provider_name(llm),
        "strict_json": bool(STRICT_JSON_ENABLED),
        "language_hint": language_hint,
        "raw_text_len": len(raw_text or ""),
        "raw_text_sha256": _text_sha256(raw_text or ""),
    }
    cached = _cache_get_pydantic(
        "reformat_direction_decision", cache_payload, DirectionDecision
    )
    if cached is not None:
        normalized_cached = _normalize_direction_decision(cached)
        if normalized_cached is not None:
            return normalized_cached

    formatter = Agent(
        role="Direction Formatter",
        goal="把內容轉成合法的 DirectionDecision JSON 物件。",
        backstory=(
            "你是嚴格的 JSON formatter。"
            "只能輸出一個符合 DirectionDecision 的 JSON 物件。"
            "不要 markdown、不要 code fence、不要額外文字。"
        ),
        allow_delegation=False,
        verbose=False,
        llm=_make_formatter_llm(llm),
    )
    task_kwargs = {
        "description": _build_direction_decision_reformat_description(
            raw_text=raw_text,
            language_hint=language_hint,
        ),
        "agent": formatter,
        "expected_output": "DirectionDecision JSON only.",
    }
    if STRICT_JSON_ENABLED and CREWAI_OUTPUT_PYDANTIC:
        task_kwargs["output_pydantic"] = DirectionDecision
    task = Task(**task_kwargs)
    crew = Crew(
        agents=[formatter], tasks=[task], process=Process.sequential, verbose=False
    )
    try:
        result = _kickoff_reformat_crew(
            crew,
            crew_name="direction_decision_reformat",
            raw_text=raw_text,
            cost_trace_stage="direction_decision_reformat.kickoff",
            error_label="DirectionDecision reformat task",
        )
        if result is None:
            return None
    except _OperationCancelledError:
        raise
    except Exception as e:
        print(f"[Error] DirectionDecision reformat task failed: {e}")
        return None
    parsed = extract_direction_decision(result)
    if parsed is not None:
        parsed = _normalize_direction_decision(parsed)
        if parsed is not None:
            _cache_set_pydantic("reformat_direction_decision", cache_payload, parsed)
    return parsed


def _reformat_gate_decision(
    raw_text: str, *, llm: Any, language_hint: str
) -> Optional["GateDecision"]:
    cache_payload = {
        "model": _reformat_llm_model_id(llm),
        "llm_provider": _reformat_llm_provider_name(llm),
        "strict_json": bool(STRICT_JSON_ENABLED),
        "language_hint": language_hint,
        "raw_text_len": len(raw_text or ""),
        "raw_text_sha256": _text_sha256(raw_text or ""),
    }
    return _run_schema_reformatter(
        cache_namespace="reformat_gate_decision",
        cache_payload=cache_payload,
        model_cls=GateDecision,
        raw_text=raw_text,
        llm=llm,
        role="Gate Decision Formatter",
        goal="Convert malformed output into valid GateDecision JSON.",
        description=(
            "Reformat the INPUT into a valid GateDecision JSON object.\n"
            "Required fields:\n"
            "- consensus: string\n"
            "- disagreement: string\n"
            "- experiments: list of {goal, criteria}\n"
            "- ready_for_codegen: boolean\n"
            "- blocking_risks: list[string]\n"
            "- required_experiments_before_codegen: list[string]\n"
            "- advisory_experiments_after_codegen: list[string]\n"
            "- codegen_scope: 'production'|'validation'\n"
            "- validation_scope_reason: string|null\n"
            "- validation_objectives: list[string]\n"
            "- agents_needing_rerun: list[string]\n"
            "- rerun_reasons: object mapping agent_name -> reason\n"
            "- direction_feedback_needed: boolean\n"
            "- direction_feedback_reason: string|null\n"
            "- direction_feedback_type: 'evidence'|'detail'|null\n"
            "- direction_feedback_evidence_gaps: list[string]\n"
            "- direction_feedback_questions: list[string]\n"
            "- overall_score: integer 0-100\n"
            "- score_breakdown: object with feasibility/risk/roi/uncertainty (0-100)\n"
            '- confidence: "low"|"medium"|"high"\n'
            '- failure_type: "NONE"|"JSON_INVALID"|"EXECUTION_ERROR"|"LOW_CONFIDENCE"|"COST_OVER_BUDGET"|"CONFLICTING_OUTPUT"|"POLICY_VIOLATION"|"NON_DETERMINISTIC"\n'
            "- failure_details: string|null\n"
            "- should_kill: boolean\n"
            "- kill_reason: string|null\n\n"
            "Rules:\n"
            "- Do not invent new facts beyond the INPUT.\n"
            "- Keep the structure valid and conservative when information is incomplete.\n"
            "- Return JSON only.\n\n"
            f"Language hint: {language_hint}\n\n"
            "INPUT:\n" + _limit_reformat_input(raw_text)
        ),
        expected_output="GateDecision JSON only.",
        parse_fn=_extract_gate_decision_raw,
        postprocess_fn=_normalize_gate_decision,
        cost_trace_stage="gate_decision_reformat.kickoff",
        error_label="GateDecision reformat task",
    )


def _reformat_analysis_report(
    raw_text: str, *, llm: Any, language_hint: str, mode: str
) -> Optional["AnalysisReport"]:
    cache_payload = {
        "model": _reformat_llm_model_id(llm),
        "llm_provider": _reformat_llm_provider_name(llm),
        "strict_json": bool(STRICT_JSON_ENABLED),
        "mode": mode,
        "language_hint": language_hint,
        "raw_text_len": len(raw_text or ""),
        "raw_text_sha256": _text_sha256(raw_text or ""),
    }
    return _run_schema_reformatter(
        cache_namespace="reformat_analysis_report",
        cache_payload=cache_payload,
        model_cls=AnalysisReport,
        raw_text=raw_text,
        llm=llm,
        role="Analysis Formatter",
        goal="Convert malformed output into valid AnalysisReport JSON.",
        description=(
            "Reformat the INPUT into a valid AnalysisReport JSON object.\n"
            "Required fields (ALL must be present):\n"
            "- project_name: short snake_case string — synthesize from the strategy/problem description in the INPUT\n"
            "- summary: string — derive from executive_summary or consensus in the INPUT\n"
            "- consensus: string — copy from INPUT consensus field\n"
            "- disagreement: string — copy from INPUT disagreement field\n"
            "- experiments: list of {goal, criteria} — copy from INPUT experiments field\n"
            "- score: integer 0-100 — use overall_score from INPUT if present (rename it to score)\n"
            f"- mode_used: exactly {json.dumps(mode)}\n"
            '- risk_level: "Low"|"Medium"|"High" — derive from INPUT confidence/score '
            "(low confidence or score<50 → High; medium or 50-69 → Medium; otherwise Low)\n"
            "- analyst_findings: object mapping roles to detail text — copy from INPUT analyst_findings if present\n"
            "- gate_context_snapshot: object preserving INPUT GateDecision fields used downstream\n"
            "- codegen_handoff_summary: string — synthesize from INPUT executive_summary and validation_scope_reason\n"
            "- codegen_requirements: list[string] — copy from INPUT implementation_requirements if present\n"
            "- codegen_constraints: list[string] — copy from INPUT implementation_constraints if present\n"
            "- codegen_validation_focus: list[string] — copy from INPUT validation_focus if present\n\n"
            "Rules:\n"
            "- Derive or synthesize all required fields from the INPUT content; do not invent unsupported facts.\n"
            "- overall_score in the INPUT maps to score — rename it.\n"
            "- Preserve implementation-relevant detail already present in the INPUT.\n"
            "- Return JSON only.\n\n"
            f"Language hint: {language_hint}\n\n"
            "INPUT:\n" + _limit_reformat_input(raw_text)
        ),
        expected_output="AnalysisReport JSON only.",
        parse_fn=_extract_analysis_report_raw,
        postprocess_fn=lambda r: _normalize_analysis_report(r, mode=mode),
        cost_trace_stage="analysis_reformat.kickoff",
        error_label="AnalysisReport reformat task",
    )


def _reformat_review_report(
    raw_text: str, *, llm: Any, language_hint: str
) -> Optional["ReviewReport"]:
    cache_payload = {
        "model": _reformat_llm_model_id(llm),
        "llm_provider": _reformat_llm_provider_name(llm),
        "strict_json": bool(STRICT_JSON_ENABLED),
        "language_hint": language_hint,
        "raw_text_len": len(raw_text or ""),
        "raw_text_sha256": _text_sha256(raw_text or ""),
    }
    return _run_schema_reformatter(
        cache_namespace="reformat_review_report",
        cache_payload=cache_payload,
        model_cls=ReviewReport,
        raw_text=raw_text,
        llm=llm,
        role="Review Formatter",
        goal="Convert malformed output into valid ReviewReport JSON.",
        description=(
            "Reformat the INPUT into a valid ReviewReport JSON object.\n"
            "Required fields:\n"
            "- passes: boolean\n"
            "- summary: concise string\n"
            "- issues: list of {severity, category, description, file, suggestion}\n"
            '- severity must be "low" | "medium" | "high"\n'
            '- category must be "requirements" | "logic" | "bug" | "security" | "performance" | "usability" | "other"\n'
            "- file and suggestion may be null when unknown\n\n"
            "Rules:\n"
            "- Do not invent issues that are not supported by the INPUT.\n"
            "- Return JSON only.\n\n"
            f"Language hint: {language_hint}\n\n"
            "INPUT:\n" + _limit_reformat_input(raw_text)
        ),
        expected_output="ReviewReport JSON only.",
        parse_fn=_extract_review_report_raw,
        cost_trace_stage="review_reformat.kickoff",
        error_label="ReviewReport reformat task",
    )


def _reformat_code_bundle(
    raw_text: str, *, llm: Any, language_hint: str, mode: str
) -> Optional["CodeBundle"]:
    # CodeBundle output contains complete source files; reasoning models can easily
    # exhaust 8 192 tokens on chain-of-thought alone, leaving zero tokens for actual
    # code.  Read the same CODEGEN_MAX_TOKENS env var that section_05 uses so the
    # cap is consistent and operator-configurable without code changes.
    # Safe env-var parse: malformed values fall back to the default instead of
    # raising at runtime (would otherwise crash this reformat path with
    # ``ValueError: invalid literal for int()``).
    _codegen_reformat_max_tokens_env = _env_int("CODEGEN_MAX_TOKENS", 65536)
    _codegen_reformat_max_tokens: int = (
        _codegen_reformat_max_tokens_env
        if _codegen_reformat_max_tokens_env is not None
        and _codegen_reformat_max_tokens_env > 0
        else 65536
    )

    cache_payload = {
        "model": _reformat_llm_model_id(llm),
        "llm_provider": _reformat_llm_provider_name(llm),
        "strict_json": bool(STRICT_JSON_ENABLED),
        "mode": mode,
        "language_hint": language_hint,
        "raw_text_len": len(raw_text or ""),
        "raw_text_sha256": _text_sha256(raw_text or ""),
    }
    return _run_schema_reformatter(
        cache_namespace="reformat_code_bundle",
        cache_payload=cache_payload,
        model_cls=CodeBundle,
        raw_text=raw_text,
        llm=llm,
        role="CodeBundle Formatter",
        goal="Convert malformed output into valid CodeBundle JSON.",
        description=(
            "Reformat the INPUT into a valid CodeBundle JSON object.\n"
            "Required fields:\n"
            '- project_type: "saas", "quant", "agent", or "scientist"\n'
            "- files: list of {path, content}; each path must be a safe relative path\n\n"
            "Rules:\n"
            "- Do not invent files or contents not supported by the INPUT.\n"
            "- Do not use absolute paths or unsafe relative paths.\n"
            "- Return JSON only.\n\n"
            f"Language hint: {language_hint}\n"
            f"Mode: {mode}\n\n"
            "INPUT:\n" + _limit_reformat_input(raw_text)
        ),
        expected_output="CodeBundle JSON only.",
        parse_fn=_extract_code_bundle_raw,
        postprocess_fn=_sanitize_code_bundle,
        validate_fn=_bundle_has_files,
        error_label="CodeBundle reformat task",
        formatter_max_tokens=_codegen_reformat_max_tokens,
    )


def _reformat_research_context(
    raw_text: str, *, llm: Any, language_hint: str
) -> Optional["ResearchContext"]:
    cache_payload = {
        "model": _reformat_llm_model_id(llm),
        "llm_provider": _reformat_llm_provider_name(llm),
        "strict_json": bool(STRICT_JSON_ENABLED),
        "language_hint": language_hint,
        "raw_text_len": len(raw_text or ""),
        "raw_text_sha256": _text_sha256(raw_text or ""),
    }
    return _run_schema_reformatter(
        cache_namespace="reformat_research_context",
        cache_payload=cache_payload,
        model_cls=ResearchContext,
        raw_text=raw_text,
        llm=llm,
        role="Research Context Formatter",
        goal="Convert malformed output into valid ResearchContext JSON.",
        description=_build_research_context_reformat_description(
            raw_text=raw_text,
            language_hint=language_hint,
        ),
        expected_output="ResearchContext JSON only.",
        parse_fn=_extract_research_context_raw,
        postprocess_fn=_stabilize_research_context,
        error_label="ResearchContext reformat task",
    )


def _reformat_direction_decision(
    raw_text: str, *, llm: Any, language_hint: str
) -> Optional["DirectionDecision"]:
    cache_payload = {
        "model": _reformat_llm_model_id(llm),
        "llm_provider": _reformat_llm_provider_name(llm),
        "strict_json": bool(STRICT_JSON_ENABLED),
        "language_hint": language_hint,
        "raw_text_len": len(raw_text or ""),
        "raw_text_sha256": _text_sha256(raw_text or ""),
    }
    return _run_schema_reformatter(
        cache_namespace="reformat_direction_decision",
        cache_payload=cache_payload,
        model_cls=DirectionDecision,
        raw_text=raw_text,
        llm=llm,
        role="Direction Formatter",
        goal="Convert malformed output into valid DirectionDecision JSON.",
        description=_build_direction_decision_reformat_description(
            raw_text=raw_text,
            language_hint=language_hint,
        ),
        expected_output="DirectionDecision JSON only.",
        parse_fn=_extract_direction_decision_raw,
        postprocess_fn=_normalize_direction_decision,
        error_label="DirectionDecision reformat task",
    )


def _reformat_direction_comparator_report(
    raw_text: str, *, llm: Any, language_hint: str
) -> Optional["DirectionComparatorReport"]:
    cache_payload = {
        "model": _reformat_llm_model_id(llm),
        "llm_provider": _reformat_llm_provider_name(llm),
        "strict_json": bool(STRICT_JSON_ENABLED),
        "language_hint": language_hint,
        "raw_text_len": len(raw_text or ""),
        "raw_text_sha256": _text_sha256(raw_text or ""),
    }
    return _run_schema_reformatter(
        cache_namespace="reformat_direction_comparator_report",
        cache_payload=cache_payload,
        model_cls=DirectionComparatorReport,
        raw_text=raw_text,
        llm=llm,
        role="Direction Comparator Formatter",
        goal="Convert malformed output into valid DirectionComparatorReport JSON.",
        description=(
            "Reformat the INPUT into a valid DirectionComparatorReport JSON object.\n"
            "Required fields:\n"
            "- items: list of per-direction comparison rows\n"
            "- each item must contain key, feasibility_score, reversibility_score, speed_to_test_score, evidence_strength_score,\n"
            "  downside_severity_score, unresolved_unknown_dependency_score, composite_score, and rationale\n"
            "- top_keys: exactly 1 to 3 direction keys from A..G ordered best to worst\n"
            "- comparison_notes: list[string]\n\n"
            "Rules:\n"
            "- Do not invent directions or scores that are unsupported by the INPUT.\n"
            "- Use only A..G as valid direction keys.\n"
            "- Return JSON only.\n\n"
            f"Language hint: {language_hint}\n\n"
            "INPUT:\n" + _limit_reformat_input(raw_text)
        ),
        expected_output="DirectionComparatorReport JSON only.",
        parse_fn=_extract_direction_comparator_report_raw,
        postprocess_fn=_normalize_direction_comparator_report_instance,
        error_label="DirectionComparatorReport reformat task",
    )


def _reformat_evidence_audit_report(
    raw_text: str, *, llm: Any, language_hint: str
) -> Optional["EvidenceAuditReport"]:
    cache_payload = {
        "model": _reformat_llm_model_id(llm),
        "llm_provider": _reformat_llm_provider_name(llm),
        "strict_json": bool(STRICT_JSON_ENABLED),
        "language_hint": language_hint,
        "raw_text_len": len(raw_text or ""),
        "raw_text_sha256": _text_sha256(raw_text or ""),
    }
    return _run_schema_reformatter(
        cache_namespace="reformat_evidence_audit_report",
        cache_payload=cache_payload,
        model_cls=EvidenceAuditReport,
        raw_text=raw_text,
        llm=llm,
        role="Evidence Audit Formatter",
        goal="Convert malformed output into valid EvidenceAuditReport JSON.",
        description=(
            "Reformat the INPUT into a valid EvidenceAuditReport JSON object.\n"
            "Required fields:\n"
            "- items: list of per-direction evidence rows\n"
            "- each item must contain key, evidence_score, supported_fields, summary_only_fields, unsupported_fields,\n"
            "  unsupported_count, and decision_critical_unknowns\n"
            "- top_keys: exactly 1 to 3 direction keys from A..G ordered best to worst\n"
            "- global_warnings: list[string]\n\n"
            "Rules:\n"
            "- Do not invent directions or evidence that are unsupported by the INPUT.\n"
            "- unsupported_count must reflect unsupported_fields.\n"
            "- Use only A..G as valid direction keys.\n"
            "- Return JSON only.\n\n"
            f"Language hint: {language_hint}\n\n"
            "INPUT:\n" + _limit_reformat_input(raw_text)
        ),
        expected_output="EvidenceAuditReport JSON only.",
        parse_fn=_extract_evidence_audit_report_raw,
        postprocess_fn=_normalize_evidence_audit_report_instance,
        error_label="EvidenceAuditReport reformat task",
    )
