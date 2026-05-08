# Auto-generated section module — do not edit manually.
# Regenerate via ``python -m crucible.generate``.
from __future__ import annotations

from . import section_00_bootstrap_and_utils as _prev_00
globals().update({k: v for k, v in _prev_00.__dict__.items() if not k.startswith('__')})
from . import section_01_extraction_and_reformat as _prev_01
globals().update({k: v for k, v in _prev_01.__dict__.items() if not k.startswith('__')})
from . import section_02_research_and_llm as _prev_02
globals().update({k: v for k, v in _prev_02.__dict__.items() if not k.startswith('__')})
from . import section_03_models_and_context as _prev_03
globals().update({k: v for k, v in _prev_03.__dict__.items() if not k.startswith('__')})
from . import section_04_web_research_and_direction as _prev_04
globals().update({k: v for k, v in _prev_04.__dict__.items() if not k.startswith('__')})
from . import section_05_analysis_and_codegen as _prev_05
globals().update({k: v for k, v in _prev_05.__dict__.items() if not k.startswith('__')})

@dataclass
class EntryPointSpec:
    path: str
    attribute: Optional[str] = None
    call: bool = False
    display: Optional[str] = None


def _entrypoint_display(entry: EntryPointSpec) -> str:
    if entry.display:
        return entry.display
    label = os.path.basename(entry.path)
    if entry.attribute:
        suffix = f":{entry.attribute}()" if entry.call else f":{entry.attribute}"
        return f"{label}{suffix}"
    return label


def _split_entrypoint_override(raw: Optional[str]) -> List[str]:
    if not raw:
        return []
    parts = re.split(r"[;,]", raw)
    return [p.strip() for p in parts if p.strip()]


def _parse_entrypoint_spec(spec: str) -> EntryPointSpec:
    raw = spec.strip()
    path = raw
    attr = None
    call = False
    if ":" in raw:
        if re.match(r"^[A-Za-z]:[\\/]", raw) and raw.count(":") == 1:
            path = raw
        else:
            path, attr = raw.rsplit(":", 1)
            path = path.strip()
            attr = attr.strip()
            if attr.endswith("()"):
                call = True
                attr = attr[:-2].strip()
    return EntryPointSpec(path=path, attribute=attr or None, call=call, display=raw)


def _resolve_entrypoint_path(entry_path: str, tmp_dir: str) -> Optional[str]:
    raw = entry_path.strip()
    candidates: List[str] = []
    if os.path.isabs(raw):
        candidates.append(raw)
    else:
        candidates.append(os.path.join(tmp_dir, raw))
        if not raw.endswith(".py"):
            candidates.append(os.path.join(tmp_dir, raw + ".py"))
        if "." in raw and not raw.endswith(".py"):
            candidates.append(os.path.join(tmp_dir, *raw.split(".")) + ".py")
            candidates.append(os.path.join(tmp_dir, *raw.split("."), "__init__.py"))
    # Resolve tmp_dir once so every candidate can be checked against it.
    # This prevents path-traversal via ".." segments in entry_path: an
    # attacker-controlled entrypoint_override="../../../etc/passwd" would
    # otherwise resolve to a path outside the project's tmp directory, giving
    # read access to arbitrary files on the host in a multi-tenant deployment.
    tmp_real = os.path.realpath(tmp_dir)
    for cand in candidates:
        if os.path.isfile(cand):
            resolved = os.path.realpath(cand)
            # For relative candidates, enforce containment within tmp_dir.
            # Absolute entries provided directly by the operator are allowed
            # through (they are already fully qualified and deliberate).
            if not os.path.isabs(raw):
                try:
                    if not (
                        resolved == tmp_real
                        or resolved.startswith(tmp_real + os.sep)
                    ):
                        continue  # traversal attempt — skip
                except (ValueError, TypeError):
                    continue  # e.g. different drive letters on Windows
            return resolved
    return None


def _entrypoint_detection_hint() -> str:
    hint_files = ", ".join(sorted(ENTRYPOINT_FILES))
    hint_patterns = "FastAPI(, Flask(, create_app(, uvicorn.run"
    return (
        "Entrypoint detection looks for filenames: "
        f"{hint_files}; or content hints: {hint_patterns}."
    )


def _run_smoke_test(
    entry: EntryPointSpec, tmp_dir: str, env: Dict[str, str]
) -> Tuple[str, Optional[str]]:
    smoke_code = "\n".join(
        [
            "import importlib.util",
            "import sys",
            "import os",
            "require_snapshot = os.environ.get('CODEX_REQUIRE_SNAPSHOT', '').lower() in ('1', 'true', 'yes')",
            "if require_snapshot:",
            "    print('SMOKE_REQUIRE_SNAPSHOT 1')",
            "path = " + json.dumps(entry.path),
            "entry_attr = " + repr(entry.attribute),
            "entry_call = " + ("True" if entry.call else "False"),
            "spec = importlib.util.spec_from_file_location('entrypoint', path)",
            "mod = importlib.util.module_from_spec(spec)",
            "spec.loader.exec_module(mod)",
            "app = None",
            "def exit_skip(msg):",
            "    print(f'SMOKE_SKIP {msg}')",
            "    sys.exit(0)",
            "def exit_fail(msg):",
            "    print(f'SMOKE_FAIL {msg}')",
            "    sys.exit(2)",
            "if entry_attr:",
            "    obj = getattr(mod, entry_attr, None)",
            "    if obj is None:",
            "        exit_fail(f'entry_attr_missing {entry_attr}')",
            "    if entry_call:",
            "        try:",
            "            obj = obj()",
            "        except Exception as e:",
            "            exit_fail(f'entry_attr_call_failed {entry_attr}: {e}')",
            "    app = obj",
            "else:",
            "    for name in ('app', 'application', 'api'):",
            "        obj = getattr(mod, name, None)",
            "        if obj is not None:",
            "            app = obj",
            "            break",
            "if app is None:",
            "    exit_skip('no_app_object')",
            "is_flask = hasattr(app, 'test_client') and hasattr(app, 'url_map')",
            "is_fastapi = hasattr(app, 'routes') and hasattr(app, 'openapi')",
            "def is_snapshot_path(p):",
            "    return p and 'snapshot' in p",
            "def prefer_snapshot_paths(paths):",
            "    preferred = []",
            "    for p in paths:",
            "        if p.rstrip('/') in ('/v1/snapshot', '/v1/snapshot_labels', '/v1/snapshot-labels'):",
            "            preferred.append(p)",
            "    if not preferred:",
            "        for p in paths:",
            "            if is_snapshot_path(p):",
            "                preferred.append(p)",
            "    return preferred",
            "if is_fastapi:",
            "    try:",
            "        from fastapi.testclient import TestClient",
            "    except Exception:",
            "        exit_skip('fastapi_testclient_unavailable')",
            "    client = TestClient(app)",
            "    candidates = []",
            "    post_candidates = []",
            "    def add_path(p):",
            "        if p and p not in candidates and '{' not in p:",
            "            candidates.append(p)",
            "    def add_post(p):",
            "        if p and p not in post_candidates and '{' not in p:",
            "            post_candidates.append(p)",
            "    try:",
            "        schema = app.openapi()",
            "        for p, methods in schema.get('paths', {}).items():",
            "            if isinstance(methods, dict) and 'get' in methods:",
            "                add_path(p)",
            "            if isinstance(methods, dict) and 'post' in methods:",
            "                add_post(p)",
            "    except Exception:",
            "        pass",
            "    try:",
            "        for route in getattr(app, 'routes', []):",
            "            path = getattr(route, 'path', None)",
            "            methods = getattr(route, 'methods', None)",
            "            if path and methods:",
            "                if 'GET' in methods:",
            "                    add_path(path)",
            "                if 'POST' in methods:",
            "                    add_post(path)",
            "    except Exception:",
            "        pass",
            "    def try_post(paths):",
            "        hard_fail = None",
            "        for p in paths:",
            "            try:",
            "                resp = client.post(p, json={})",
            "            except Exception as e:",
            "                exit_fail(f'exception_on_post {p}: {e}')",
            "            if resp.status_code in (401, 403):",
            "                print(f'SMOKE_AUTH_REQUIRED {p} status={resp.status_code}')",
            "            print(f'SMOKE_TESTED POST {p} status={resp.status_code}')",
            "            if resp.status_code >= 500 or resp.status_code in (404, 405):",
            "                hard_fail = f'POST status_{resp.status_code} {p}'",
            "                continue",
            "            print(f'SMOKE_OK_POST {p} status={resp.status_code}')",
            "            sys.exit(0)",
            "        if hard_fail:",
            "            exit_fail(hard_fail)",
            "    snapshot_posts = prefer_snapshot_paths(post_candidates)",
            "    if snapshot_posts:",
            "        try_post(snapshot_posts)",
            "    def try_get(paths, strict):",
            "        hard_fail = None",
            "        for p in paths:",
            "            try:",
            "                resp = client.get(p)",
            "            except Exception as e:",
            "                exit_fail(f'exception_on_get {p}: {e}')",
            "            if resp.status_code in (401, 403):",
            "                print(f'SMOKE_AUTH_REQUIRED {p} status={resp.status_code}')",
            "            print(f'SMOKE_TESTED GET {p} status={resp.status_code}')",
            "            if resp.status_code >= 500 or (strict and resp.status_code in (404, 405)):",
            "                hard_fail = f'GET status_{resp.status_code} {p}'",
            "                continue",
            "            if strict or resp.status_code < 400:",
            "                print(f'SMOKE_OK {p} status={resp.status_code}')",
            "                sys.exit(0)",
            "        if hard_fail:",
            "            exit_fail(hard_fail)",
            "    snapshot_gets = prefer_snapshot_paths(candidates)",
            "    if snapshot_gets:",
            "        try_get(snapshot_gets, True)",
            "    if require_snapshot and not snapshot_posts and not snapshot_gets:",
            "        exit_fail('snapshot_route_missing')",
            "    defaults = ['/health', '/healthz', '/ready', '/', '/v1/health', '/v1/snapshot', "
            "                '/v1/snapshot_labels', '/v1/labels', '/v1/predict']",
            "    for p in defaults:",
            "        add_path(p)",
            "    try_get(candidates, False)",
            "    exit_skip('no_successful_get')",
            "if is_flask:",
            "    try:",
            "        client = app.test_client()",
            "    except Exception:",
            "        exit_skip('flask_test_client_unavailable')",
            "    candidates = []",
            "    post_candidates = []",
            "    def add_path(p):",
            "        if p and p not in candidates and '<' not in p:",
            "            candidates.append(p)",
            "    def add_post(p):",
            "        if p and p not in post_candidates and '<' not in p:",
            "            post_candidates.append(p)",
            "    try:",
            "        for rule in app.url_map.iter_rules():",
            "            if 'GET' in rule.methods:",
            "                add_path(rule.rule)",
            "            if 'POST' in rule.methods:",
            "                add_post(rule.rule)",
            "    except Exception:",
            "        pass",
            "    def try_post(paths):",
            "        hard_fail = None",
            "        for p in paths:",
            "            try:",
            "                resp = client.post(p, json={})",
            "            except Exception as e:",
            "                exit_fail(f'exception_on_post {p}: {e}')",
            "            if resp.status_code in (401, 403):",
            "                print(f'SMOKE_AUTH_REQUIRED {p} status={resp.status_code}')",
            "            print(f'SMOKE_TESTED POST {p} status={resp.status_code}')",
            "            if resp.status_code >= 500 or resp.status_code in (404, 405):",
            "                hard_fail = f'POST status_{resp.status_code} {p}'",
            "                continue",
            "            print(f'SMOKE_OK_POST {p} status={resp.status_code}')",
            "            sys.exit(0)",
            "        if hard_fail:",
            "            exit_fail(hard_fail)",
            "    snapshot_posts = prefer_snapshot_paths(post_candidates)",
            "    if snapshot_posts:",
            "        try_post(snapshot_posts)",
            "    def try_get(paths, strict):",
            "        hard_fail = None",
            "        for p in paths:",
            "            try:",
            "                resp = client.get(p)",
            "            except Exception as e:",
            "                exit_fail(f'exception_on_get {p}: {e}')",
            "            if resp.status_code in (401, 403):",
            "                print(f'SMOKE_AUTH_REQUIRED {p} status={resp.status_code}')",
            "            print(f'SMOKE_TESTED GET {p} status={resp.status_code}')",
            "            if resp.status_code >= 500 or (strict and resp.status_code in (404, 405)):",
            "                hard_fail = f'GET status_{resp.status_code} {p}'",
            "                continue",
            "            if strict or resp.status_code < 400:",
            "                print(f'SMOKE_OK {p} status={resp.status_code}')",
            "                sys.exit(0)",
            "        if hard_fail:",
            "            exit_fail(hard_fail)",
            "    snapshot_gets = prefer_snapshot_paths(candidates)",
            "    if snapshot_gets:",
            "        try_get(snapshot_gets, True)",
            "    if require_snapshot and not snapshot_posts and not snapshot_gets:",
            "        exit_fail('snapshot_route_missing')",
            "    defaults = ['/health', '/healthz', '/ready', '/', '/v1/health', '/v1/snapshot', "
            "                '/v1/snapshot_labels', '/v1/labels', '/v1/predict']",
            "    for p in defaults:",
            "        add_path(p)",
            "    try_get(candidates, False)",
            "    exit_skip('no_successful_get')",
            "exit_skip('unsupported_app_type')",
        ]
    )
    smoke_path = None
    try:
        smoke_path = os.path.join(tmp_dir, f"__smoke_{uuid.uuid4().hex}.py")
        with open(smoke_path, "w", encoding="utf-8") as fp:
            fp.write(smoke_code)
        result = subprocess.run(
            [sys.executable, smoke_path],
            capture_output=True,
            text=True,
            cwd=tmp_dir,
            env=env,
            timeout=15,
        )
    except subprocess.TimeoutExpired:
        return "fail", f"[smoke {_entrypoint_display(entry)}] timeout after 15s"
    finally:
        if smoke_path:
            try:
                os.remove(smoke_path)
            except Exception:
                pass

    log = _format_proc_result(f"smoke {_entrypoint_display(entry)}", result)
    if result.returncode == 2:
        return "fail", log
    if result.returncode != 0:
        return "fail", log
    if "SMOKE_SKIP" in (result.stdout or ""):
        return "skip", log
    return "ok", log


def _detect_entrypoints(py_files: List[str]) -> List[EntryPointSpec]:
    entrypoints: List[EntryPointSpec] = []
    seen: Set[str] = set()
    for path in py_files:
        if os.path.basename(path) in ENTRYPOINT_FILES:
            entrypoints.append(EntryPointSpec(path=path))
            seen.add(path)
    patterns = [
        "FastAPI(",
        "Flask(",
        "app = FastAPI",
        "app=FastAPI",
        "app = Flask",
        "app=Flask",
        "create_app(",
        "application = FastAPI",
        "application=FastAPI",
        "application = Flask",
        "application=Flask",
        "uvicorn.run",
    ]
    for path in py_files:
        if path in seen:
            continue
        text, _, _ = safe_read_text(path, 200000, 200000)
        if not text:
            continue
        if any(pat in text for pat in patterns):
            entrypoints.append(EntryPointSpec(path=path))
            seen.add(path)
    return entrypoints


PY_COMPILE_TIMEOUT_SECS = 10


def _compile_python_files(
    py_files: List[str], tmp_dir: str, env: Dict[str, str]
) -> Tuple[bool, List[ReviewIssue], str]:
    # NOTE: Avoid `python -m py_compile` because it writes `.pyc` files, which can
    # fail (WinError 5) in some restricted environments. This is a syntax-only
    # compile check (no bytecode written).
    logs: List[str] = [
        f"[py_compile] syntax-only compile (no .pyc). timeout_hint={PY_COMPILE_TIMEOUT_SECS}s"
    ]
    issues: List[ReviewIssue] = []
    for path in py_files:
        label = os.path.basename(path)
        try:
            with open(path, "r", encoding="utf-8") as fp:
                source = fp.read()
            compile(source, path, "exec")
        except SyntaxError as e:
            issues.append(
                ReviewIssue(
                    severity="high",
                    category="bug",
                    description=f"Python compilation failed for {label}.",
                    file=label,
                    suggestion=f"Fix syntax error near line {getattr(e, 'lineno', None)}.",
                )
            )
            logs.append(f"[py_compile {label}] SyntaxError: {e}")
        except Exception as e:
            issues.append(
                ReviewIssue(
                    severity="high",
                    category="bug",
                    description=f"Python compilation failed for {label}.",
                    file=label,
                    suggestion="Fix file read/encoding/runtime errors shown in the log.",
                )
            )
            logs.append(f"[py_compile {label}] error: {e}")
    if issues:
        return False, issues, "\n\n".join(logs)
    return True, [], "\n\n".join(logs).strip()


SNAPSHOT_ROUTE_KEYWORDS = (
    "/v1/snapshot",
    "/v1/snapshot_labels",
    "/v1/snapshot-labels",
    "snapshot_labels",
    "snapshot-labels",
)
SNAPSHOT_SIGNAL_WORDS = ("api", "endpoint", "/v1")
SNAPSHOT_CODE_EXTS = {
    ".py",
    ".js",
    ".ts",
    ".jsx",
    ".tsx",
    ".json",
    ".yaml",
    ".yml",
    ".toml",
}


def _mode_supports_web_runtime_validation(mode_cfg: Optional["ModeConfig"]) -> bool:
    if mode_cfg is None or not mode_cfg.requires_runtime_validation:
        return False
    framework = (mode_cfg.preferred_framework or "").strip().lower()
    return framework not in ("", "pure_python", "python")


def _lookup_registered_mode_config(mode: Optional[str]) -> Optional["ModeConfig"]:
    if not mode:
        return None
    exact = ModeRegistry.get(mode)
    if exact is not None:
        return exact
    normalized = str(mode or "").strip().lower()
    if not normalized:
        return None
    for name, cfg in ModeRegistry.all_modes().items():
        if str(name or "").strip().lower() == normalized:
            return cfg
    return None


def _resolve_validation_mode_config(
    *,
    mode: Optional[str] = None,
    code_bundle: Optional[CodeBundle] = None,
    analysis_report: Optional[AnalysisReport] = None,
) -> Optional["ModeConfig"]:
    for candidate in (
        mode,
        getattr(code_bundle, "project_type", None),
        getattr(analysis_report, "mode_used", None),
    ):
        resolved = _lookup_registered_mode_config(candidate)
        if resolved is not None:
            return resolved
    return None


def _has_conflicting_validation_mode_metadata(
    *,
    mode: Optional[str] = None,
    code_bundle: Optional[CodeBundle] = None,
    analysis_report: Optional[AnalysisReport] = None,
) -> bool:
    canonical: List[str] = []
    for candidate in (
        mode,
        getattr(code_bundle, "project_type", None),
        getattr(analysis_report, "mode_used", None),
    ):
        resolved = _lookup_registered_mode_config(candidate)
        if resolved is None:
            continue
        canonical_name = str(resolved.name or "").strip().lower()
        if canonical_name:
            canonical.append(canonical_name)
    return len(set(canonical)) > 1


def requires_snapshot_validation(
    user_problem: Optional[str], code_bundle: CodeBundle, mode: Optional[str] = None
) -> bool:
    forced = os.environ.get("CODEX_REQUIRE_SNAPSHOT", "").lower() in (
        "1",
        "true",
        "yes",
    )
    if forced:
        return True
    if _has_conflicting_validation_mode_metadata(mode=mode, code_bundle=code_bundle):
        return False
    mode_cfg = _resolve_validation_mode_config(mode=mode, code_bundle=code_bundle)
    mode_requires = (
        bool(mode_cfg.requires_snapshot_validation) if mode_cfg is not None else False
    )
    # Pure-python modes such as Quant/Agent must not be upgraded into snapshot
    # API validation purely from prompt keywords. If the user truly wants a
    # snapshot endpoint, routing/codegen should switch to a web-capable mode.
    if mode_cfg is not None and not mode_requires and not _mode_supports_web_runtime_validation(
        mode_cfg
    ):
        return False
    heuristic_requires = False
    if user_problem:
        lower = user_problem.lower()
        if any(k in lower for k in SNAPSHOT_ROUTE_KEYWORDS):
            heuristic_requires = True
        if "snapshot" in lower and any(w in lower for w in SNAPSHOT_SIGNAL_WORDS):
            heuristic_requires = True
    for f in code_bundle.files:
        ext = os.path.splitext(f.path)[1].lower()
        if ext not in SNAPSHOT_CODE_EXTS:
            continue
        lower = f.content.lower()
        if any(k in lower for k in SNAPSHOT_ROUTE_KEYWORDS):
            heuristic_requires = True
            break
    return mode_requires or heuristic_requires


def requires_web_validation(
    user_problem: Optional[str],
    mode: Optional[str] = None,
    code_bundle: Optional[CodeBundle] = None,
    analysis_report: Optional[AnalysisReport] = None,
) -> bool:
    if _has_conflicting_validation_mode_metadata(
        mode=mode,
        code_bundle=code_bundle,
        analysis_report=analysis_report,
    ):
        return False
    mode_cfg = _resolve_validation_mode_config(
        mode=mode,
        code_bundle=code_bundle,
        analysis_report=analysis_report,
    )
    if mode_cfg is not None:
        return _mode_supports_web_runtime_validation(mode_cfg)
    # Fail closed: web runtime validation must not be inferred from prompt
    # keywords alone, otherwise a Quant/Agent request mentioning "SaaS" can
    # be silently upgraded into SaaS-specific runtime rules.
    return False


def resolve_quality_runtime_validation_scope(
    *, input_mode: str, mode: Optional[str]
) -> str:
    if input_mode != "project_path":
        return "full"
    return "static"


@contextmanager
def _temporary_directory(prefix: str):
    """
    Create a writable temporary directory.

    Avoid Python's tempfile helpers here: in some locked-down Windows setups
    they can create directories/files that cannot be written/removed (WinError 5).
    """

    base_root = os.environ.get("CODEX_TMP_DIR")
    if not base_root:
        base_root = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".tmp")
    os.makedirs(base_root, exist_ok=True)

    tmp_dir = os.path.join(base_root, f"{prefix}{uuid.uuid4().hex}")
    os.makedirs(tmp_dir, exist_ok=False)
    try:
        probe = os.path.join(tmp_dir, ".__writable_probe__")
        with open(probe, "w", encoding="utf-8") as f:
            f.write("ok")
        yield tmp_dir
    finally:
        try:
            shutil.rmtree(tmp_dir, ignore_errors=True)
        except Exception:
            pass


def run_runtime_validation(
    code_bundle: CodeBundle,
    user_problem: Optional[str] = None,
    mode: Optional[str] = None,
    entrypoint_override: Optional[str] = None,
    validation_scope: str = "full",
) -> Tuple[bool, List[ReviewIssue], str]:
    issues: List[ReviewIssue] = []
    logs: List[str] = []
    scope = (validation_scope or "full").strip().lower()
    if scope not in {"full", "static", "skip"}:
        scope = "full"

    if scope == "skip":
        return True, [], "Runtime validation skipped by validation_scope=skip."

    clean_bundle = _sanitize_code_bundle(code_bundle)
    if clean_bundle is None:
        issues.append(
            ReviewIssue(
                severity="high",
                category="bug",
                description=(
                    "Runtime validation received an invalid CodeBundle. "
                    "project_type must be one of quant, saas, agent, or scientist, and file paths "
                    "must be safe relative paths."
                ),
                file=None,
                suggestion=(
                    "Emit a valid CodeBundle with project_type set to quant, saas, agent, or scientist "
                    "before runtime validation."
                ),
            )
        )
        return False, issues, "\n\n".join(
            [
                "[runtime] Invalid CodeBundle rejected before validation.",
                "[runtime] Runtime validation requires a canonical project_type and safe file paths.",
            ]
        )

    mismatch_reason = _code_bundle_mode_mismatch_reason(clean_bundle, mode)
    if mismatch_reason:
        issues.append(
            ReviewIssue(
                severity="high",
                category="bug",
                description=(
                    "Runtime validation rejected a CodeBundle whose project_type "
                    "conflicted with the explicitly requested mode."
                ),
                file=None,
                suggestion=(
                    "Keep mode and CodeBundle.project_type aligned. Reject or regenerate "
                    "cross-mode outputs instead of validating them under another mode."
                ),
            )
        )
        return False, issues, "\n\n".join(
            [
                "[runtime] Mode/project_type mismatch rejected before validation.",
                f"[runtime] {mismatch_reason}",
            ]
        )

    with _temporary_directory(prefix="qa_validate_") as tmp_dir:
        written = _write_code_bundle_to_dir(clean_bundle, tmp_dir)
        py_files = [p for p in written if p.lower().endswith(".py")]
        # Build a sanitised environment: strip credentials so LLM-generated
        # validation code cannot exfiltrate API keys or secrets.
        # Also strip any inherited PYTHONPATH: if the user has other projects
        # on their PYTHONPATH containing same-named packages (e.g. "src"),
        # Python's namespace-package scan can prefer a regular package (with
        # __init__.py) found later on PYTHONPATH over the generated code's own
        # package directory — causing spurious ImportErrors.  We set PYTHONPATH
        # to *only* tmp_dir below so every import resolves from the generated
        # bundle first.
        _SENSITIVE_PATTERNS = (
            "API_KEY", "API_SECRET", "SECRET_KEY", "TOKEN", "PASSWORD",
            "CREDENTIAL", "OPENROUTER", "OPENAI_API", "ANTHROPIC_API",
            "ALIBABA_", "AWS_SECRET", "TELEGRAM_",
        )
        env = {
            k: v for k, v in os.environ.items()
            if not any(p in k.upper() for p in _SENSITIVE_PATTERNS)
            and k.upper() != "PYTHONPATH"
        }
        env["CODEX_VALIDATION"] = "1"
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        # Only the validation sandbox directory should be on Python's path.
        env["PYTHONPATH"] = tmp_dir
        require_snapshot = scope == "full" and requires_snapshot_validation(
            user_problem, clean_bundle, mode=mode
        )
        require_web = scope == "full" and requires_web_validation(
            user_problem, mode=mode, code_bundle=clean_bundle
        )
        if require_snapshot:
            env["CODEX_REQUIRE_SNAPSHOT"] = "1"
            logs.append("[runtime] Snapshot endpoint validation required.")
        if require_web:
            logs.append("[runtime] Web app validation required.")
        if not py_files:
            logs.append(
                "[runtime] No Python files detected; runtime validation skipped."
            )
            if require_snapshot or require_web:
                logs.append(
                    "[runtime] Snapshot/Web validation requires Python entrypoints."
                )
                if require_snapshot and not require_web:
                    description = (
                        "Snapshot validation required but the code bundle contains no Python entrypoints."
                    )
                elif require_web and not require_snapshot:
                    description = (
                        "Web validation required but the code bundle contains no Python entrypoints."
                    )
                else:
                    description = (
                        "Web and snapshot validation required but the code bundle contains no Python entrypoints."
                    )
                issues.append(
                    ReviewIssue(
                        severity="high",
                        category="bug",
                        description=description,
                        file=None,
                        suggestion=(
                            "Generate a Python web entrypoint or lower the mode/framework "
                            "requirements before runtime validation."
                        ),
                    )
                )
                return False, issues, "\n\n".join(logs).strip()
            return True, [], "\n\n".join(logs).strip()

        entrypoint_override = entrypoint_override or os.environ.get(
            ENTRYPOINT_OVERRIDE_ENV
        )

        compile_ok, compile_issues, compile_log = _compile_python_files(
            py_files, tmp_dir, env
        )
        if compile_log:
            logs.append(compile_log)
        if not compile_ok:
            return False, compile_issues, "\n\n".join(logs).strip()

        if scope == "static":
            logs.append(
                "[runtime] Static validation scope active: syntax-only validation passed; "
                "entrypoint/import/smoke checks skipped for partial or headless project-path review."
            )
            return True, [], "\n\n".join(logs).strip()

        entrypoints: List[EntryPointSpec] = []
        if entrypoint_override:
            raw_specs = _split_entrypoint_override(entrypoint_override)
            logs.append(
                "[runtime] Entrypoint override requested: " + ", ".join(raw_specs)
            )
            tmp_root = os.path.realpath(tmp_dir)
            for raw in raw_specs:
                spec = _parse_entrypoint_spec(raw)
                resolved = _resolve_entrypoint_path(spec.path, tmp_dir)
                if not resolved:
                    issues.append(
                        ReviewIssue(
                            severity="high",
                            category="bug",
                            description=f"Entrypoint override not found: {raw}",
                            file=None,
                            suggestion="Ensure the entrypoint path is relative to the project root.",
                        )
                    )
                    logs.append(f"[runtime] Entrypoint override not found: {raw}")
                    continue
                if not _is_within_root(resolved, tmp_root):
                    issues.append(
                        ReviewIssue(
                            severity="high",
                            category="security",
                            description=f"Entrypoint override resolved outside validation root: {raw}",
                            file=None,
                            suggestion="Use a path inside the generated code bundle.",
                        )
                    )
                    logs.append(
                        f"[runtime] Entrypoint override outside validation root: {raw}"
                    )
                    continue
                entrypoints.append(
                    EntryPointSpec(
                        path=resolved,
                        attribute=spec.attribute,
                        call=spec.call,
                        display=spec.display,
                    )
                )
            if not entrypoints:
                if not issues:
                    issues.append(
                        ReviewIssue(
                            severity="high",
                            category="bug",
                            description="Entrypoint override provided but no valid entrypoints resolved.",
                            file=None,
                            suggestion="Provide --entrypoint path/to/app.py:app or CODEX_ENTRYPOINT=...",
                        )
                    )
                return False, issues, "\n\n".join(logs).strip()
            logs.append(
                "[runtime] Entrypoint override applied: "
                + ", ".join(_entrypoint_display(e) for e in entrypoints)
            )
        else:
            entrypoints = _detect_entrypoints(py_files)

        if not entrypoints:
            logs.append("[runtime] No entrypoints detected; smoke test skipped.")
            logs.append("[runtime] " + _entrypoint_detection_hint())
            logs.append(
                "[runtime] Override with --entrypoint path/to/app.py:app "
                f"or {ENTRYPOINT_OVERRIDE_ENV}=app.py:app."
            )
            if require_snapshot or require_web:
                if require_snapshot and not require_web:
                    desc = (
                        "Snapshot validation required but no entrypoints were detected."
                    )
                elif require_web and not require_snapshot:
                    desc = "Web validation required but no entrypoints were detected."
                else:
                    desc = "Web and snapshot validation required but no entrypoints were detected."
                issues.append(
                    ReviewIssue(
                        severity="high",
                        category="bug",
                        description=desc,
                        file=None,
                        suggestion=(
                            "Expose a FastAPI/Flask entrypoint or set "
                            "--entrypoint / CODEX_ENTRYPOINT."
                        ),
                    )
                )
                return False, issues, "\n\n".join(logs).strip()
            return True, [], "\n\n".join(logs).strip()
        any_smoke_ok = False
        any_smoke_skip = False
        entrypoint_issues: List[ReviewIssue] = []
        entrypoint_failures: List[str] = []
        for entry in entrypoints:
            entry_label = _entrypoint_display(entry)
            code = (
                "import importlib.util, sys; "
                f"path={json.dumps(entry.path)}; "
                "spec=importlib.util.spec_from_file_location('entrypoint', path); "
                "mod=importlib.util.module_from_spec(spec); "
                "spec.loader.exec_module(mod)"
            )
            try:
                result = subprocess.run(
                    [sys.executable, "-c", code],
                    capture_output=True,
                    text=True,
                    cwd=tmp_dir,
                    env=env,
                    timeout=10,
                )
            except subprocess.TimeoutExpired:
                entrypoint_issues.append(
                    ReviewIssue(
                        severity="high",
                        category="bug",
                        description=f"Import check timed out for {entry_label}.",
                        file=entry_label,
                        suggestion="Avoid long-running work at import time.",
                    )
                )
                logs.append(f"[import {entry_label}] timeout after 10s")
                entrypoint_failures.append(f"import timeout: {entry_label}")
                continue

            if result.returncode != 0:
                entrypoint_issues.append(
                    ReviewIssue(
                        severity="high",
                        category="bug",
                        description=f"Import check failed for {entry_label}.",
                        file=entry_label,
                        suggestion="Fix runtime errors shown in the log.",
                    )
                )
                logs.append(_format_proc_result(f"import {entry_label}", result))
                entrypoint_failures.append(f"import failed: {entry_label}")
                continue

            smoke_status, smoke_log = _run_smoke_test(entry, tmp_dir, env)
            if smoke_log:
                logs.append(smoke_log)
            if smoke_status == "skip":
                logs.append(f"[smoke] Skipped for {entry_label}.")
                any_smoke_skip = True
                continue
            if smoke_status != "ok":
                snapshot_route_missing = bool(
                    smoke_log and "snapshot_route_missing" in smoke_log
                )
                description = f"Smoke test failed for {entry_label}."
                suggestion = (
                    "Ensure the app starts and at least one basic GET endpoint or "
                    "snapshot POST returns <500 (404/405 indicate missing route or method)."
                )
                if snapshot_route_missing:
                    description = (
                        f"Smoke test failed for {entry_label}: snapshot route missing."
                    )
                    suggestion = (
                        "Expose a snapshot route such as /v1/snapshot, "
                        "/v1/snapshot_labels, or /v1/snapshot-labels that returns a "
                        "non-5xx response. When snapshot validation is enabled, a "
                        "basic /health endpoint alone is insufficient."
                    )
                entrypoint_issues.append(
                    ReviewIssue(
                        severity="high",
                        category="bug",
                        description=description,
                        file=entry_label,
                        suggestion=suggestion,
                    )
                )
                entrypoint_failures.append(f"smoke failed: {entry_label}")
            else:
                any_smoke_ok = True
                # Performance optimization: once we have a working entrypoint, stop
                # validating additional candidates unless an explicit override was requested.
                if not entrypoint_override and len(entrypoints) > 1:
                    logs.append(
                        f"[runtime] Stopping after first successful smoke test: {entry_label}"
                    )
                    break

        if any_smoke_ok and entrypoint_failures:
            logs.append(
                "[runtime] Some entrypoints failed but at least one passed: "
                + ", ".join(entrypoint_failures)
            )
        if not any_smoke_ok:
            issues.extend(entrypoint_issues)

        if require_web and not any_smoke_ok and any_smoke_skip and not issues:
            issues.append(
                ReviewIssue(
                    severity="high",
                    category="bug",
                    description="Web validation required but smoke test was skipped.",
                    file=None,
                    suggestion="Expose a FastAPI/Flask app object so the smoke test can run.",
                )
            )
        if require_snapshot and not any_smoke_ok and any_smoke_skip and not issues:
            issues.append(
                ReviewIssue(
                    severity="high",
                    category="bug",
                    description="Snapshot validation required but smoke test was skipped.",
                    file=None,
                    suggestion="Expose a FastAPI/Flask app object so the smoke test can run.",
                )
            )

    if issues:
        return False, issues, "\n\n".join(logs)
    return True, [], "\n\n".join(logs).strip()


def _gate_decision_snapshot(gate_decision: Optional[GateDecision]) -> Dict[str, Any]:
    return _build_gate_context_snapshot(gate_decision)


ANALYSIS_HANDOFF_SUMMARY_MAX_CHARS = 1200
ANALYSIS_HANDOFF_DETAIL_MAX_CHARS = 2000
ANALYSIS_HANDOFF_ITEM_MAX_CHARS = 400
ANALYSIS_HANDOFF_GATE_JSON_MAX_CHARS = 3500
ANALYSIS_HANDOFF_ANALYST_FINDING_MAX_CHARS = 1600


def _append_analysis_handoff_context(
    parts: List[str],
    analysis_report: Optional[AnalysisReport],
    *,
    include_analyst_findings: bool,
) -> None:
    if analysis_report is None:
        return

    parts.append("=== FORMAT CHECKER ORGANIZED HANDOFF ===")
    parts.append(
        f"Summary: {limit_text(analysis_report.summary, ANALYSIS_HANDOFF_SUMMARY_MAX_CHARS)}"
    )
    parts.append(
        f"Consensus: {limit_text(analysis_report.consensus, ANALYSIS_HANDOFF_DETAIL_MAX_CHARS)}"
    )
    parts.append(
        f"Disagreement: {limit_text(analysis_report.disagreement, ANALYSIS_HANDOFF_DETAIL_MAX_CHARS)}"
    )
    parts.append(f"Score: {analysis_report.score}/100")
    parts.append(f"Mode: {analysis_report.mode_used}")
    parts.append(f"Risk level: {analysis_report.risk_level}")

    if analysis_report.experiments:
        parts.append("\nExperiments:")
        for exp in analysis_report.experiments:
            parts.append(
                f"- {limit_text(exp.goal, ANALYSIS_HANDOFF_ITEM_MAX_CHARS)} | "
                f"{limit_text(exp.criteria, ANALYSIS_HANDOFF_ITEM_MAX_CHARS)}"
            )

    if analysis_report.codegen_handoff_summary:
        parts.append(
            "\nImplementation summary:\n"
            + limit_text(
                analysis_report.codegen_handoff_summary,
                ANALYSIS_HANDOFF_DETAIL_MAX_CHARS,
            )
        )

    if analysis_report.codegen_requirements:
        parts.append("\nRequired implementation details:")
        parts.extend(
            f"- {limit_text(item, ANALYSIS_HANDOFF_ITEM_MAX_CHARS)}"
            for item in analysis_report.codegen_requirements
        )

    if analysis_report.codegen_constraints:
        parts.append("\nImplementation constraints:")
        parts.extend(
            f"- {limit_text(item, ANALYSIS_HANDOFF_ITEM_MAX_CHARS)}"
            for item in analysis_report.codegen_constraints
        )

    if analysis_report.codegen_validation_focus:
        parts.append("\nValidation focus:")
        parts.extend(
            f"- {limit_text(item, ANALYSIS_HANDOFF_ITEM_MAX_CHARS)}"
            for item in analysis_report.codegen_validation_focus
        )

    if analysis_report.gate_context_snapshot:
        parts.append("\nPreserved gate snapshot:")
        parts.append(
            limit_text(
                json.dumps(
                    analysis_report.gate_context_snapshot,
                    ensure_ascii=False,
                    indent=2,
                    sort_keys=True,
                ),
                ANALYSIS_HANDOFF_GATE_JSON_MAX_CHARS,
            )
        )

    if include_analyst_findings and analysis_report.analyst_findings:
        parts.append("\nPreserved analyst findings:")
        role_order = list(ANALYST_AGENT_ORDER) + ["gate_controller", "format_checker"]
        seen: Set[str] = set()
        for role_name in role_order + sorted(analysis_report.analyst_findings):
            if role_name in seen or role_name not in analysis_report.analyst_findings:
                continue
            seen.add(role_name)
            parts.append(f"[{role_name}]")
            parts.append(
                limit_text(
                    analysis_report.analyst_findings[role_name],
                    ANALYSIS_HANDOFF_ANALYST_FINDING_MAX_CHARS,
                )
            )


def _quality_focus_sort_key(path: str, priority_files: Set[str]) -> Tuple[int, str]:
    normalized = _normalize_bundle_relpath(path)
    base = os.path.basename(normalized)
    if normalized in priority_files:
        return (0, normalized)
    if base in ENTRYPOINT_FILES:
        return (1, normalized)
    return (2, normalized)


def _select_quality_focus_paths(
    code_bundle: CodeBundle,
    *,
    priority_files: Set[str],
    requested_scope: Optional[Set[str]],
    round_idx: int,
) -> Set[str]:
    normalized_scope = _safe_scope_files(code_bundle, requested_scope)
    candidates: List[str] = []
    seen: Set[str] = set()
    for generated in list(code_bundle.files or []):
        path = _normalize_bundle_relpath(getattr(generated, "path", ""))
        if not path or path in seen:
            continue
        if normalized_scope is not None and path not in normalized_scope:
            continue
        seen.add(path)
        candidates.append(path)
    if not candidates:
        return set()
    try:
        candidates.sort(key=lambda item: _quality_focus_sort_key(item, priority_files))
    except Exception:
        candidates.sort()
    max_files = (
        QUALITY_MAX_FILES_WITH_CONTENT_ROUND0
        if round_idx == 0
        else QUALITY_MAX_FILES_WITH_CONTENT_ROUNDN
    )
    # Guard against None: _env_int returns None for "none"/"unlimited" env vars;
    # None > 0 raises TypeError in Python 3.
    if max_files is not None and max_files > 0:
        candidates = candidates[:max_files]
    return set(candidates)


def _resolve_quality_prompt_scope(
    code_bundle: CodeBundle,
    *,
    runtime_log: Optional[str],
    affected_files: Optional[Set[str]],
    round_idx: int,
) -> Tuple[Set[str], Optional[Set[str]], Set[str]]:
    priority = _extract_relevant_paths_from_runtime_log(
        runtime_log, code_bundle
    ) | _detect_entrypoint_paths(code_bundle)
    requested_scope = (set(affected_files) | priority) if affected_files is not None else None
    content_scope = _select_quality_focus_paths(
        code_bundle,
        priority_files=priority,
        requested_scope=requested_scope,
        round_idx=round_idx,
    )
    if not content_scope and priority:
        content_scope = _select_quality_focus_paths(
            code_bundle,
            priority_files=priority,
            requested_scope=priority,
            round_idx=round_idx,
        )
    return priority, requested_scope, content_scope


def build_quality_context(
    user_problem: str,
    analysis_report: Optional[AnalysisReport],
    code_bundle: CodeBundle,
    runtime_log: Optional[str] = None,
    affected_files: Optional[Set[str]] = None,
    round_idx: int = 0,
) -> str:
    parts: List[str] = []
    parts.append("=== USER GOAL ===")
    parts.append(limit_text(user_problem, QUALITY_CONTEXT_MAX_CHARS))
    if analysis_report:
        parts.append("")
        _append_analysis_handoff_context(
            parts, analysis_report, include_analyst_findings=False
        )
    if runtime_log:
        parts.append("\n=== RUNTIME VALIDATION LOG ===")
        parts.append(
            format_runtime_log_for_llm(runtime_log, QUALITY_RUNTIME_LOG_MAX_CHARS)
        )
    priority, requested_scope, content_scope = _resolve_quality_prompt_scope(
        code_bundle,
        runtime_log=runtime_log,
        affected_files=affected_files,
        round_idx=round_idx,
    )
    parts.append("\n=== PROJECT TREE (TEXT ONLY) ===")
    parts.append(
        _format_code_bundle_tree_limited(code_bundle, QUALITY_CONTEXT_TREE_MAX_CHARS)
    )
    if priority:
        parts.append("\n=== PRIORITY FILES ===")
        parts.append(", ".join(sorted(priority)))
    if requested_scope:
        parts.append("\n=== REQUESTED FIX/REVIEW SCOPE ===")
        parts.append(", ".join(sorted(requested_scope)))
    parts.append("\n=== FOCUSED CODE FILES ===")
    parts.append(
        format_code_bundle_for_review(
            code_bundle,
            QUALITY_CODE_BUNDLE_MAX_CHARS,
            affected_files=content_scope,
            priority_files=priority,
            max_files_with_content=None,
        )
    )
    parts.append("\n=== ALL FILE PATHS (NO CONTENT) ===")
    parts.append(_format_code_bundle_paths_only(code_bundle))
    return "\n".join(parts)


def run_quality_review(
    user_problem: str,
    analysis_report: Optional[AnalysisReport],
    code_bundle: CodeBundle,
    llm: Any,
    runtime_log: Optional[str] = None,
    affected_files: Optional[Set[str]] = None,
    round_idx: int = 0,
) -> Tuple[Optional[ReviewReport], str]:
    reviewer = Agent(
        role="Quality Reviewer",
        goal="檢查需求符合度、邏輯正確性與明顯 bug。",
        backstory=(
            "你是嚴格 reviewer。"
            "你必須確認程式碼符合目標且邏輯一致。"
            "若有問題，必須精確列出檔案路徑與修正方式。"
            "若沒有問題，必須設 passes=true 且 issues=[]."
        ),
        allow_delegation=False,
        verbose=True,
        llm=llm,
    )

    review_prompt = build_quality_context(
        user_problem,
        analysis_report,
        code_bundle,
        runtime_log,
        affected_files=affected_files,
        round_idx=round_idx,
    )

    last_error: Optional[Exception] = None
    last_raw_output: Optional[str] = None
    last_failure_reason = "quality_review_failed"
    for attempt in range(QUALITY_JSON_RETRY_ATTEMPTS):
        retry_prefix = ""
        if attempt > 0:
            retry_prefix = (
                "IMPORTANT: Your previous answer was not valid JSON.\n"
                "Return ONLY one JSON object matching this schema (no markdown, no extra text):\n"
                "{\n"
                '  "passes": true,\n'
                '  "summary": "",\n'
                '  "issues": [\n'
                "    {\n"
                '      "severity": "low|medium|high",\n'
                '      "category": "requirements|logic|bug|security|performance|usability|other",\n'
                '      "description": "...",\n'
                '      "file": "optional/path",\n'
                '      "suggestion": "optional fix"\n'
                "    }\n"
                "  ]\n"
                "}\n\n"
            )

        if attempt > 0 and last_raw_output:
            # Cost optimization: on retry, do NOT resend the full project/code context.
            # Ask the model to convert its previous output into valid JSON.
            description = (
                retry_prefix
                + "Convert the PREVIOUS OUTPUT into a single valid JSON object matching ReviewReport.\n"
                + "Preserve the meaning of the previous output; do not introduce new issues.\n"
                + "Output JSON only.\n\n"
                + "PREVIOUS OUTPUT:\n"
                + limit_text(last_raw_output, 12000)
            )
        else:
            description = (
                retry_prefix
                + "Review the code for requirement fit, bugs, and logic errors. "
                + "Output a single JSON object matching ReviewReport schema ONLY. "
                + "No markdown, no code fences, no commentary.\n\n"
                + "Keep the summary concise (<= 500 chars). Do not restate code.\n\n"
                + "Note: If CODE FILES is scoped (not full), assume other files are unchanged. "
                + "If you believe a fix is needed in another file, include it explicitly in issues[].file.\n\n"
                + f"{review_prompt}"
            )

        task = Task(
            description=description,
            agent=reviewer,
            expected_output="Structured ReviewReport JSON.",
        )

        crew = Crew(
            agents=[reviewer], tasks=[task], process=Process.sequential, verbose=True
        )
        _cost_trace(
            "quality_review.kickoff",
            attempt=attempt + 1,
            round=round_idx + 1,
            prompt_chars=len(description),
        )
        try:
            result = kickoff_crew_with_retry(
                crew,
                crew_name="quality_review",
                logger=LOGGER,
                log_fields={
                    "outer_attempt": attempt + 1,
                    "round": round_idx + 1,
                    "prompt_chars": len(description),
                },
            )
        except _OperationCancelledError:
            # Cooperative cancellation must abort the quality-review retry loop —
            # do not record as a per-attempt execution error and continue.
            raise
        except Exception as e:
            last_error = e
            last_failure_reason = "execution_error"
            try:
                _record_cost(
                    stage="quality_review.kickoff",
                    agent_name="QualityReviewer",
                    input_tokens=len(description) // 3,
                    output_tokens=0,
                    success=False,
                    retry_count=attempt,
                    outcome="execution_error",
                )
            except Exception:
                pass
            print(f"[Error] Quality review task execution failed: {e}")
            continue

        parsed = extract_review_report(result)
        if parsed is None:
            language_hint = (
                "Traditional Chinese" if contains_cjk(user_problem) else "English"
            )
            # Phase 1: cheap parse on every candidate first.  The previous
            # interleaved loop spent an LLM reformat call on raw_i the moment
            # its parse failed, even when raw_{i+1} would have parsed for
            # free (CrewAI exposes the same output via several attrs).
            text_candidates = _collect_text_candidates_from_result(result)
            for raw_candidate in reversed(text_candidates):
                parsed = extract_review_report(raw_candidate)
                if parsed is not None:
                    break
            # Phase 2: only when every cheap parse fails do we fall back to
            # the LLM-driven schema reformatter.
            if parsed is None and STRICT_JSON_ENABLED:
                for raw_candidate in reversed(text_candidates):
                    parsed = _reformat_review_report(
                        raw_candidate,
                        llm=llm,
                        language_hint=language_hint,
                    )
                    if parsed is not None:
                        break
        if parsed is not None:
            # Record successful quality review cost
            try:
                _record_cost(
                    stage="quality_review.kickoff",
                    agent_name="QualityReviewer",
                    input_tokens=len(description) // 3,
                    output_tokens=len(_extract_text_from_result(result) or "") // 3,
                    success=True,
                    retry_count=attempt,
                )
            except Exception:
                pass
            return parsed, "success"
        last_raw_output = _extract_text_from_result(result) or last_raw_output
        last_failure_reason = "parse_failed"
        try:
            _record_cost(
                stage="quality_review.kickoff",
                agent_name="QualityReviewer",
                input_tokens=len(description) // 3,
                output_tokens=len(last_raw_output or "") // 3,
                success=False,
                retry_count=attempt,
                outcome="parse_failed",
            )
        except Exception:
            pass
        print(
            "[Warn] Quality review output not parsed; retrying with stricter JSON instructions..."
        )

    if last_error is not None:
        print(f"[Error] Quality review failed after retries: {last_error}")
    return None, last_failure_reason


def _format_review_report_for_prompt(review_report: ReviewReport) -> str:
    parts: List[str] = []
    summary = str(getattr(review_report, "summary", "") or "").strip()
    parts.append("Summary: " + (summary if summary else "(no summary)"))
    issues = list(getattr(review_report, "issues", []) or [])
    parts.append(f"Issues count: {len(issues)}")
    if issues:
        parts.append("Issues:")
    for issue in issues[:QUALITY_MAX_ISSUES_IN_PROMPT]:
        severity = str(getattr(issue, "severity", "") or "").strip() or "unknown"
        category = str(getattr(issue, "category", "") or "").strip() or "other"
        file_path = str(getattr(issue, "file", "") or "").strip() or "(unspecified)"
        description = limit_text(
            str(getattr(issue, "description", "") or "").strip(),
            QUALITY_REVIEW_ISSUE_MAX_CHARS,
        )
        suggestion = limit_text(
            str(getattr(issue, "suggestion", "") or "").strip(),
            QUALITY_REVIEW_ISSUE_MAX_CHARS,
        )
        line = f"- [{severity}/{category}] {file_path}: {description}"
        if suggestion:
            line += f" | Suggested fix: {suggestion}"
        parts.append(line)
    return limit_text("\n".join(parts), QUALITY_REVIEW_REPORT_MAX_CHARS)


def build_quality_fixer_context(
    user_problem: str,
    analysis_report: Optional[AnalysisReport],
    code_bundle: CodeBundle,
    review_report: ReviewReport,
    runtime_log: Optional[str] = None,
    affected_files: Optional[Set[str]] = None,
    round_idx: int = 0,
) -> str:
    parts: List[str] = []
    parts.append("=== USER GOAL ===")
    parts.append(limit_text(user_problem, QUALITY_CONTEXT_MAX_CHARS))

    if analysis_report:
        parts.append("")
        _append_analysis_handoff_context(
            parts, analysis_report, include_analyst_findings=False
        )

    if runtime_log:
        parts.append("\n=== RUNTIME VALIDATION LOG ===")
        parts.append(
            format_runtime_log_for_llm(runtime_log, QUALITY_RUNTIME_LOG_MAX_CHARS)
        )

    priority, requested_scope, content_scope = _resolve_quality_prompt_scope(
        code_bundle,
        runtime_log=runtime_log,
        affected_files=affected_files,
        round_idx=round_idx,
    )
    parts.append("\n=== PROJECT TREE (TEXT ONLY) ===")
    parts.append(
        _format_code_bundle_tree_limited(code_bundle, QUALITY_CONTEXT_TREE_MAX_CHARS)
    )

    parts.append("\n=== ENTRYPOINT FILENAMES (TEXT ONLY) ===")
    entry_names = _detect_entrypoint_filenames(code_bundle)
    parts.append(", ".join(entry_names) if entry_names else "(none detected)")

    parts.append("\n=== THIS ROUND REVIEW SUMMARY ===")
    parts.append(
        review_report.summary.strip() if review_report.summary else "(no summary)"
    )
    parts.append(f"Issues count: {len(review_report.issues or [])}")

    if priority:
        parts.append("\n=== PRIORITY FILES ===")
        parts.append(", ".join(sorted(priority)))
    if requested_scope:
        parts.append("\n=== REQUESTED FIX SCOPE ===")
        parts.append(", ".join(sorted(requested_scope)))
    parts.append("\n=== FOCUSED CODE FILES ===")
    parts.append(
        format_code_bundle_for_review(
            code_bundle,
            QUALITY_CODE_BUNDLE_MAX_CHARS,
            affected_files=content_scope,
            priority_files=priority,
            max_files_with_content=None,
        )
    )
    parts.append("\n=== ALL FILE PATHS (NO CONTENT) ===")
    parts.append(_format_code_bundle_paths_only(code_bundle))

    return "\n".join(parts)


def _recover_quality_fix_patch_from_last_raw_output(
    *,
    last_raw_output: Optional[str],
    user_problem: str,
    llm: Any,
    mode_name: str,
) -> Optional[CodeBundle]:
    if not last_raw_output or not str(last_raw_output).strip():
        return None
    if not STRICT_JSON_ENABLED:
        return None
    return _reformat_code_bundle(
        str(last_raw_output),
        llm=llm,
        language_hint="Traditional Chinese" if contains_cjk(user_problem) else "English",
        mode=mode_name,
    )


def run_quality_fix(
    user_problem: str,
    analysis_report: Optional[AnalysisReport],
    code_bundle: CodeBundle,
    review_report: ReviewReport,
    llm: Any,
    runtime_log: Optional[str] = None,
    affected_files: Optional[Set[str]] = None,
    round_idx: int = 0,
) -> Tuple[Optional[CodeBundle], str]:
    mode_name = _mode_name_from_project_type(code_bundle.project_type)
    fixer = Agent(
        role="Quality Fixer",
        goal=(
            "修復已回報問題，包括 runtime validation failure。"
            "若為了 SLO、安全或可靠性需要，可做最小必要工程調整。"
        ),
        backstory=(
            "你是資深工程師。"
            "只能修復已回報問題，且範圍必須最小。"
            "若為了解決可靠性或安全缺口，可做必要工程調整。"
            "輸出必須是包含更新檔案的完整 CodeBundle JSON。"
        ),
        allow_delegation=False,
        verbose=True,
        llm=llm,
    )

    if affected_files is None:
        affected_files = _collect_affected_files(review_report)
    allowed_files = _safe_scope_files(code_bundle, affected_files)
    allow_new_files = _review_allows_new_files(review_report, code_bundle)
    priority = _extract_relevant_paths_from_runtime_log(runtime_log, code_bundle)
    if requires_web_validation(None, mode=mode_name):
        priority |= _detect_entrypoint_paths(code_bundle)
    if allowed_files is not None and priority:
        allowed_files = set(allowed_files) | priority
    elif mode_name == "Agent" and allowed_files is None:
        explicit_issue_files = _collect_affected_files(review_report)
        narrowed = _safe_scope_files(code_bundle, explicit_issue_files)
        if narrowed is not None:
            allowed_files = set(narrowed)
    allowed_label = (
        "ALL (affected_files empty; using full CodeBundle)"
        if allowed_files is None
        else ", ".join(sorted(allowed_files))
    )

    prompt = "\n".join(
        [
            "=== REVIEW REPORT ===",
            _format_review_report_for_prompt(review_report),
            "\nAllowed files to modify: " + allowed_label,
            "\n=== CONTEXT ===",
            build_quality_fixer_context(
                user_problem,
                analysis_report,
                code_bundle,
                review_report,
                runtime_log=runtime_log,
                affected_files=allowed_files,
                round_idx=round_idx,
            ),
            "\nRules:",
            "- Fix only the issues listed (including runtime validation failures)",
            f"- Allowed files to modify: {allowed_label}",
            "- Do not modify or output files outside the allowed list"
            if not allow_new_files
            else "- You may also add new files when directly required to resolve the reported issue",
            "- If a fix seems to require other files, add a ReviewIssue with that file path instead of changing it"
            if not allow_new_files
            else "- New files must be minimal, directly tied to the reported issue, and remain under the project root",
            "- You may make minimal engineering changes required for SLO, security, or reliability",
            "- Avoid new features beyond what is required to fix issues",
            "- Output CodeBundle JSON with ONLY the files you changed. Do NOT include unchanged files.",
            f"- Set project_type to '{code_bundle.project_type}'",
        ]
    )

    last_error: Optional[Exception] = None
    patch_bundle: Optional[CodeBundle] = None
    merged_candidate: Optional[CodeBundle] = None
    last_raw_output: Optional[str] = None
    last_failure_reason = "quality_fix_failed"
    malformed_or_noop_streak = 0
    for attempt in range(QUALITY_JSON_RETRY_ATTEMPTS):
        recovered_patch_bundle: Optional[CodeBundle] = None
        result: Any = None
        retry_prefix = ""
        if attempt > 0:
            retry_prefix = (
                "Repair the previous answer into one valid CodeBundle JSON object.\n"
                "Return ONLY one JSON object with this shape (no markdown, no extra text):\n"
                "{\n"
                f'  "project_type": "{code_bundle.project_type}",\n'
                '  "files": [ { "path": "relative/path.py", "content": "..." } ]\n'
                "}\n\n"
            )

        if attempt > 0 and last_raw_output:
            # Cost optimization: on retry, do NOT resend the full context.
            # Convert the previous output into a valid CodeBundle JSON.
            description = (
                retry_prefix
                + "Convert the PREVIOUS OUTPUT into a valid CodeBundle JSON.\n"
                + (
                    "Only include files you actually changed and keep changes within allowed files.\n"
                    if not allow_new_files
                    else "Only include files you actually changed; new files are allowed only when directly required by the reported issue.\n"
                )
                + "Output JSON only.\n\n"
                + "Allowed files to modify: "
                + allowed_label
                + "\n\nPREVIOUS OUTPUT:\n"
                + limit_text(last_raw_output, 12000)
            )
        else:
            description = retry_prefix + prompt

        task = Task(
            description=description,
            agent=fixer,
            expected_output="Fixed CodeBundle JSON only (no markdown, no extra text).",
        )

        crew = Crew(
            agents=[fixer], tasks=[task], process=Process.sequential, verbose=True
        )
        _cost_trace(
            "quality_fix.kickoff",
            attempt=attempt + 1,
            round=round_idx + 1,
            prompt_chars=len(description),
        )
        try:
            result = kickoff_crew_with_retry(
                crew,
                crew_name="quality_fix",
                logger=LOGGER,
                log_fields={
                    "outer_attempt": attempt + 1,
                    "round": round_idx + 1,
                    "prompt_chars": len(description),
                },
            )
        except _OperationCancelledError:
            # Cooperative cancellation must abort the quality-fix retry loop —
            # do not attempt patch recovery or continue to the next attempt.
            raise
        except Exception as e:
            last_error = e
            last_failure_reason = "execution_error"
            if is_transient_retryable_error(e):
                recovered_patch_bundle = _recover_quality_fix_patch_from_last_raw_output(
                    last_raw_output=last_raw_output,
                    user_problem=user_problem,
                    llm=llm,
                    mode_name=mode_name,
                )
                if recovered_patch_bundle is not None:
                    print(
                        "[Warn] Quality fix task timed out; recovered patch from the last raw output."
                    )
            if recovered_patch_bundle is None:
                try:
                    _record_cost(
                        stage="quality_fix.kickoff",
                        agent_name="QualityFixer",
                        input_tokens=len(description) // 3,
                        output_tokens=0,
                        success=False,
                        retry_count=attempt,
                        outcome=last_failure_reason,
                    )
                except Exception:
                    pass
                print(f"[Error] Quality fix task execution failed: {e}")
                continue

        recovered_from_last_raw_output = recovered_patch_bundle is not None and result is None
        result_output_text = (
            (_extract_text_from_result(result) or "") if result is not None else ""
        )
        patch_bundle = recovered_patch_bundle or extract_code_bundle(result)
        if patch_bundle is None:
            # Phase 1: try cheap parses on every candidate first.  The previous
            # interleaved loop spent an LLM call to reformat raw_i the moment
            # its parse failed, even when a later raw would have parsed for
            # free.  Doing every parse first avoids that wasted LLM call.
            text_candidates = _collect_text_candidates_from_result(result)
            for raw in reversed(text_candidates):
                patch_bundle = extract_code_bundle(raw)
                if patch_bundle is not None:
                    break
            # Phase 2: only when every cheap parse fails do we fall back to
            # the LLM-driven schema reformatter.
            if patch_bundle is None and STRICT_JSON_ENABLED:
                _patch_language_hint = (
                    "Traditional Chinese"
                    if contains_cjk(user_problem)
                    else "English"
                )
                for raw in reversed(text_candidates):
                    patch_bundle = _reformat_code_bundle(
                        raw,
                        llm=llm,
                        language_hint=_patch_language_hint,
                        mode=mode_name,
                    )
                    if patch_bundle is not None:
                        break
        if patch_bundle is not None:
            # Defensive: reject patches that introduce Python SyntaxErrors.
            # The Quality Fixer LLM occasionally emits malformed code (truncated
            # functions, mis-escaped strings, unbalanced brackets).  Without this
            # check, the broken patch is merged, written to disk, and only
            # discovered by `_compile_python_files` in the NEXT
            # run_quality_loop round — wasting one LLM review call and one
            # runtime validation pass.  Catch it here and retry the fix attempt
            # immediately so the next LLM call sees the malformed output and
            # corrects it.  Only check files that THIS patch actually emits
            # (an existing syntax error in `code_bundle` is not the patch's
            # fault and should be surfaced through runtime validation).
            patch_syntax_err = _py_syntax_error_in_bundle(patch_bundle)
            if patch_syntax_err:
                patch_bundle = None
                merged_candidate = None
                last_failure_reason = "patch_syntax_error"
                malformed_or_noop_streak += 1
                last_raw_output = result_output_text or last_raw_output
                try:
                    _record_cost(
                        stage="quality_fix.kickoff",
                        agent_name="QualityFixer",
                        input_tokens=len(description) // 3,
                        output_tokens=len(result_output_text) // 3,
                        success=False,
                        retry_count=attempt,
                        outcome=last_failure_reason,
                    )
                except Exception:
                    pass
                print(
                    "[Warn] Quality fix patch contained Python SyntaxError; "
                    f"retrying ({patch_syntax_err})."
                )
                if (
                    QUALITY_FIX_FUSE_CONSECUTIVE_FAILURES
                    and malformed_or_noop_streak
                    >= QUALITY_FIX_FUSE_CONSECUTIVE_FAILURES
                ):
                    print(
                        "[Warn] Quality fix fuse triggered: repeated no-op/parse failures; "
                        "stopping further fix retries."
                    )
                    break
                continue
            merged_candidate = _merge_code_bundle_patch(
                code_bundle,
                patch_bundle,
                allowed_files=allowed_files,
                allow_new_files=allow_new_files,
            )
            if _code_bundle_effective_change_count(code_bundle, merged_candidate) <= 0:
                patch_bundle = None
                merged_candidate = None
                last_failure_reason = "no_effective_changes"
                malformed_or_noop_streak += 1
                last_raw_output = result_output_text or last_raw_output
                try:
                    _record_cost(
                        stage="quality_fix.kickoff",
                        agent_name="QualityFixer",
                        input_tokens=len(description) // 3,
                        output_tokens=len(result_output_text) // 3,
                        success=False,
                        retry_count=attempt,
                        outcome=last_failure_reason,
                    )
                except Exception:
                    pass
                print("[Warn] Quality fix produced no effective changes; retrying...")
                if (
                    QUALITY_FIX_FUSE_CONSECUTIVE_FAILURES
                    and malformed_or_noop_streak
                    >= QUALITY_FIX_FUSE_CONSECUTIVE_FAILURES
                ):
                    print(
                        "[Warn] Quality fix fuse triggered: repeated no-op/parse failures; "
                        "stopping further fix retries."
                    )
                    break
                continue
            malformed_or_noop_streak = 0
            # Record successful quality fix cost
            try:
                _record_cost(
                    stage="quality_fix.kickoff",
                    agent_name="QualityFixer",
                    input_tokens=len(description) // 3,
                    output_tokens=len(result_output_text) // 3,
                    success=True,
                    retry_count=attempt,
                    outcome=(
                        "recovered_from_last_raw_output"
                        if recovered_from_last_raw_output
                        else "success"
                    ),
                )
            except Exception:
                pass
            break
        last_failure_reason = "parse_failed"
        malformed_or_noop_streak += 1
        last_raw_output = result_output_text or last_raw_output
        try:
            _record_cost(
                stage="quality_fix.kickoff",
                agent_name="QualityFixer",
                input_tokens=len(description) // 3,
                output_tokens=len(result_output_text) // 3,
                success=False,
                retry_count=attempt,
                outcome=last_failure_reason,
            )
        except Exception:
            pass
        print(
            "[Warn] Quality fix output not parsed; retrying with stricter JSON instructions..."
        )
        if (
            QUALITY_FIX_FUSE_CONSECUTIVE_FAILURES
            and malformed_or_noop_streak >= QUALITY_FIX_FUSE_CONSECUTIVE_FAILURES
        ):
            print(
                "[Warn] Quality fix fuse triggered: repeated no-op/parse failures; "
                "stopping further fix retries."
            )
            break

    if not patch_bundle:
        if last_error is not None:
            print(f"[Error] Quality fix failed after retries: {last_error}")
        return None, last_failure_reason
    if merged_candidate is None:
        merged_candidate = _merge_code_bundle_patch(
            code_bundle,
            patch_bundle,
            allowed_files=allowed_files,
            allow_new_files=allow_new_files,
        )
    return merged_candidate, ""


# =========================
# 5.5) Selective Re-run System
# =========================
# 優化3：Selective Re-run - 只重跑失敗或低信心的 agent
# 節省 30-50% token 成本

_configured_selective_rerun_max_attempts = _env_int("SELECTIVE_RERUN_MAX_ATTEMPTS", 5)
SELECTIVE_RERUN_MAX_ATTEMPTS = (
    5
    if _configured_selective_rerun_max_attempts is None
    else _configured_selective_rerun_max_attempts
)


def _resolve_gate_control_enabled_default() -> bool:
    return _env_bool("GATE_CONTROL_ENABLED", True)


def _resolve_selective_rerun_enabled_default() -> bool:
    return _env_bool("SELECTIVE_RERUN_ENABLED", True)


def _resolve_runtime_profile_strict_json_default() -> bool:
    return _env_bool("STRICT_JSON", False)


def _resolve_runtime_profile_cache_default() -> bool:
    return _env_bool("LOCAL_CACHE", False)


GATE_CONTROL_ENABLED = _resolve_gate_control_enabled_default()
SELECTIVE_RERUN_ENABLED = _resolve_selective_rerun_enabled_default()
BUDGET_SOFT_COST_LIMIT = _env_float("BUDGET_SOFT_COST_LIMIT", None)
BUDGET_HARD_COST_LIMIT = _env_float("BUDGET_HARD_COST_LIMIT", None)
BUDGET_MAX_TOTAL_TOKENS = _env_int("BUDGET_MAX_TOTAL_TOKENS", None)


def _resolve_runtime_profile_default_name() -> str:
    return (os.environ.get("RUNTIME_PROFILE") or "pro").strip().lower() or "pro"


RUNTIME_PROFILE_DEFAULT = _resolve_runtime_profile_default_name()


@dataclass(frozen=True)
class RuntimeProfileConfig:
    name: str
    gate_control_default: bool
    selective_rerun_default: bool
    quality_max_rounds: Optional[int]
    strict_json_default: bool
    cache_default: bool
    snapshot_level: str


def _build_runtime_profiles() -> Dict[str, RuntimeProfileConfig]:
    return {
        "lite": RuntimeProfileConfig(
            name="lite",
            gate_control_default=False,
            selective_rerun_default=False,
            quality_max_rounds=3,
            strict_json_default=False,
            cache_default=False,
            snapshot_level="minimal",
        ),
        "pro": RuntimeProfileConfig(
            name="pro",
            gate_control_default=_resolve_gate_control_enabled_default(),
            selective_rerun_default=_resolve_selective_rerun_enabled_default(),
            quality_max_rounds=None,
            strict_json_default=_resolve_runtime_profile_strict_json_default(),
            cache_default=_resolve_runtime_profile_cache_default(),
            snapshot_level="standard",
        ),
        "enterprise": RuntimeProfileConfig(
            name="enterprise",
            gate_control_default=True,
            selective_rerun_default=True,
            quality_max_rounds=None,
            strict_json_default=True,
            cache_default=True,
            snapshot_level="full",
        ),
    }


RUNTIME_PROFILES: Dict[str, RuntimeProfileConfig] = _build_runtime_profiles()


def resolve_runtime_profile(name: Optional[str]) -> RuntimeProfileConfig:
    key = (name or _resolve_runtime_profile_default_name() or "pro").strip().lower()
    profiles = _build_runtime_profiles()
    return profiles.get(key, profiles["pro"])


def should_skip_codegen(
    gate_decision: Optional[GateDecision],
    *,
    budget_state: Optional[Dict[str, Any]] = None,
    budget_policy: Optional[BudgetPolicy] = None,
) -> Tuple[bool, str]:
    """
    Determine if CodeGen should be skipped based on GateDecision.

    Returns (should_skip, reason).
    """
    if budget_state:
        over_budget = bool(budget_state.get("over_hard_limit")) or bool(
            budget_state.get("over_token_limit")
        )
        if over_budget and (
            budget_policy is None or budget_policy.skip_codegen_on_hard_limit
        ):
            return True, "Cost/token budget exceeded hard limit."

    if gate_decision is None:
        return False, ""

    if gate_decision.should_kill:
        _apply_gate_failure(
            gate_decision,
            FailureType.POLICY_VIOLATION,
            gate_decision.kill_reason or "Kill signal raised.",
        )
        return True, f"Flow killed: {gate_decision.kill_reason or 'no reason provided'}"

    if gate_decision.blocking_risks:
        _apply_gate_failure(
            gate_decision,
            FailureType.CONFLICTING_OUTPUT
            if gate_decision.ready_for_codegen
            else FailureType.POLICY_VIOLATION,
            "Blocking risks must be resolved before code generation.",
        )
        return True, f"Blocking risks: {', '.join(gate_decision.blocking_risks[:3])}"

    if gate_decision.required_experiments_before_codegen:
        _apply_gate_failure(
            gate_decision,
            FailureType.POLICY_VIOLATION,
            "Required experiments before CodeGen are pending.",
        )
        return (
            True,
            f"Required experiments pending: {gate_decision.required_experiments_before_codegen[0]}",
        )

    if not gate_decision.ready_for_codegen:
        reason = (
            gate_decision.failure_details
            or "Gate decision marked not ready for code generation."
        )
        _apply_gate_failure(
            gate_decision,
            FailureType.POLICY_VIOLATION,
            reason,
        )
        return True, reason

    return False, ""


def build_conditional_codegen_context(
    gate_decision: Optional[GateDecision],
    analysis_report: Optional[AnalysisReport],
) -> str:
    """
    Build context for CodeGen that respects Gate Controller decisions.
    Prefer the budgeted handoff formatter so the prompt stays compact while
    preserving implementation-critical detail.
    """
    return build_budgeted_codegen_context(
        gate_decision,
        analysis_report,
        max_chars=CODEGEN_CONTEXT_MAX_CHARS,
        include_analyst_findings=True,
    )


def run_quality_loop(
    user_problem: str,
    analysis_report: Optional[AnalysisReport],
    code_bundle: CodeBundle,
    llm: Any,
    max_rounds: Optional[int] = None,
    mode: Optional[str] = None,
    skip_runtime_validation: bool = False,
    runtime_validation_scope: Optional[str] = None,
    entrypoint_override: Optional[str] = None,
    api_version_report: Optional[ApiVersionReport] = None,
) -> Tuple[CodeBundle, Optional[ReviewReport], Optional[str]]:
    # Do NOT clamp to max(1, ...) — that would make it impossible to disable the
    # quality loop by passing max_rounds=0 (or QUALITY_MAX_ROUNDS=0).
    # The contract is: 0 = skip quality loop entirely.
    _resolved = max_rounds if max_rounds is not None else _resolve_quality_max_rounds_default()
    effective_max_rounds = int(_resolved if _resolved is not None else 80)
    if effective_max_rounds == 0:
        return code_bundle, None, None
    current_code = code_bundle
    last_report: Optional[ReviewReport] = None
    last_runtime_log: Optional[str] = None
    review_scope_files: Optional[Set[str]] = None
    stagnation_rounds = 0
    prev_score: Optional[int] = None
    effective_runtime_scope = (runtime_validation_scope or "").strip().lower() or (
        "skip" if skip_runtime_validation else "full"
    )

    api_issues_injected = False

    def _issue_score(issues: List[ReviewIssue]) -> int:
        score = 0
        for it in issues or []:
            sev = (it.severity or "").lower()
            if sev == "high":
                score += 3
            elif sev == "medium":
                score += 2
            else:
                score += 1
        return score

    def _update_stagnation(
        issues: List[ReviewIssue],
        prev_score: Optional[int],
        stagnation_rounds: int,
    ) -> Tuple[int, Optional[int], bool]:
        score = _issue_score(issues)
        if prev_score is not None and score >= prev_score:
            new_stagnation = stagnation_rounds + 1
        else:
            new_stagnation = 0
        threshold = QUALITY_EARLY_STOP_STAGNATION_ROUNDS
        should_stop = threshold is not None and new_stagnation >= threshold
        return new_stagnation, score, should_stop

    for round_idx in range(effective_max_rounds):
        print(f"\n[System] Quality review round {round_idx + 1}/{effective_max_rounds}")
        runtime_log = None
        # Reset per-round review scope so a stale scope from a previous
        # runtime-fix round does not leak into the next quality review.
        review_scope_files = None
        if effective_runtime_scope == "skip":
            runtime_ok = True
            runtime_issues = []
            runtime_log = "Runtime validation skipped by validation_scope=skip."
        else:
            runtime_ok, runtime_issues, runtime_log = run_runtime_validation(
                current_code,
                user_problem=user_problem,
                mode=mode,
                entrypoint_override=entrypoint_override,
                validation_scope=effective_runtime_scope,
            )
        last_runtime_log = runtime_log
        if not runtime_ok:
            last_report = ReviewReport(
                passes=False,
                summary="Runtime validation failed.",
                issues=runtime_issues,
            )
            stagnation_rounds, prev_score, should_stop = _update_stagnation(
                runtime_issues, prev_score, stagnation_rounds
            )
            if should_stop:
                print(
                    f"[Warn] Early stopping: no improvement for {stagnation_rounds} rounds."
                )
                return current_code, last_report, last_runtime_log
            if round_idx == effective_max_rounds - 1:
                return current_code, last_report, last_runtime_log
            affected_files = _collect_affected_files(last_report)
            fixed, fix_failure_reason = run_quality_fix(
                user_problem,
                analysis_report,
                current_code,
                last_report,
                llm,
                runtime_log=runtime_log,
                affected_files=affected_files,
                round_idx=round_idx,
            )
            if not fixed:
                reason_label = fix_failure_reason or "quality_fix_failed"
                print(
                    f"[Warn] Quality fix aborted ({reason_label}); keeping previous code."
                )
                if last_runtime_log:
                    last_runtime_log = (
                        last_runtime_log + f"\n\n[quality_fix] aborted: {reason_label}"
                    )
                else:
                    last_runtime_log = f"[quality_fix] aborted: {reason_label}"
                return current_code, last_report, last_runtime_log
            current_code = fixed
            review_scope_files = _safe_scope_files(current_code, affected_files)
            continue

        review, review_failure_reason = run_quality_review(
            user_problem,
            analysis_report,
            current_code,
            llm,
            runtime_log=runtime_log,
            affected_files=review_scope_files,
            round_idx=round_idx,
        )
        if not review:
            reason_label = review_failure_reason or "quality_review_failed"
            print(f"[Warn] Quality review aborted ({reason_label}).")
            # FIX: Create fallback ReviewReport to ensure issues are not lost
            # When quality review parsing fails, we still need to return a valid report
            # rather than potentially returning None (which loses all issue information)
            if last_report is None:
                fallback_report = ReviewReport(
                    passes=False,
                    summary=f"Quality review parsing failed: {reason_label}. The reviewer output could not be parsed into a valid ReviewReport.",
                    issues=[
                        ReviewIssue(
                            severity="high",
                            category="other",
                            description=f"Quality review output could not be parsed (reason: {reason_label}). The LLM may have identified issues that were not captured due to JSON parsing failure.",
                            file=None,
                            suggestion="Consider re-running the quality review or checking the raw LLM output for manually identified issues.",
                        )
                    ],
                )
                last_report = fallback_report
            if last_runtime_log:
                last_runtime_log = (
                    last_runtime_log + f"\n\n[quality_review] aborted: {reason_label}"
                )
            else:
                last_runtime_log = f"[quality_review] aborted: {reason_label}"
            return current_code, last_report, last_runtime_log
        last_report = review

        effective_review = review
        # v14: Inject API version issues on first round only
        if (
            round_idx == 0
            and not api_issues_injected
            and api_version_report
            and api_version_report.needs_update
        ):
            api_issues_injected = True
            effective_review = inject_api_issues_into_review(review, api_version_report)
            last_report = effective_review
            if api_version_report.issues:
                print(
                    f"[API Version Check] Injected {len(api_version_report.issues)} API issue(s) into quality review"
                )

        if effective_review.passes:
            return current_code, effective_review, last_runtime_log

        stagnation_rounds, prev_score, should_stop = _update_stagnation(
            effective_review.issues or [], prev_score, stagnation_rounds
        )
        if should_stop:
            print(
                f"[Warn] Early stopping: no improvement for {stagnation_rounds} rounds."
            )
            return current_code, last_report, last_runtime_log

        if round_idx == effective_max_rounds - 1:
            break
        affected_files = _collect_affected_files(effective_review)
        fixed, fix_failure_reason = run_quality_fix(
            user_problem,
            analysis_report,
            current_code,
            effective_review,
            llm,
            runtime_log=runtime_log,
            affected_files=affected_files,
            round_idx=round_idx,
        )
        if not fixed:
            reason_label = fix_failure_reason or "quality_fix_failed"
            print(
                f"[Warn] Quality fix aborted ({reason_label}); keeping previous code."
            )
            if last_runtime_log:
                last_runtime_log = (
                    last_runtime_log + f"\n\n[quality_fix] aborted: {reason_label}"
                )
            else:
                last_runtime_log = f"[quality_fix] aborted: {reason_label}"
            return current_code, last_report, last_runtime_log
        current_code = fixed
        review_scope_files = _safe_scope_files(current_code, affected_files)

    return current_code, last_report, last_runtime_log


# =========================
# 5.6) API Version Checker (v14)
# =========================


def _extract_imports_from_code(code_bundle: "CodeBundle") -> Dict[str, List[str]]:
    """
    Extract import statements from code bundle and map library names to files.
    Returns a dict mapping library_name -> list of file paths that import it.
    """
    import_pattern = re.compile(
        r"^\s*(?:from\s+(\S+)\s+import|import\s+([^#\n;]+))", re.MULTILINE
    )

    library_files: Dict[str, List[str]] = {}

    for gen_file in code_bundle.files or []:
        if not gen_file.path or not gen_file.content:
            continue

        # Only check Python files
        if not gen_file.path.endswith(".py"):
            continue

        for match in import_pattern.finditer(gen_file.content):
            from_module = match.group(1)
            import_modules = match.group(2)

            modules_to_check = []
            if from_module:
                # from X import Y -> X is the library
                modules_to_check.append(from_module.split(".")[0])
            if import_modules:
                # import X, Y, Z -> X, Y, Z are the modules
                for mod in import_modules.split(","):
                    mod = mod.strip().split()[0].split(".")[0]
                    if mod:
                        modules_to_check.append(mod)

            for lib in modules_to_check:
                lib_lower = lib.lower()
                if lib_lower not in library_files:
                    library_files[lib_lower] = []
                if gen_file.path not in library_files[lib_lower]:
                    library_files[lib_lower].append(gen_file.path)

    return library_files


def _canonicalize_api_import_name(library: str) -> str:
    normalized = str(library or "").strip().lower()
    return API_VERSION_IMPORT_NAME_ALIASES.get(normalized, normalized)


def _filter_high_risk_libraries(
    imported_libraries: Dict[str, List[str]],
) -> Tuple[Dict[str, List[str]], List[str]]:
    """
    Filter imported libraries against the high-risk list.
    Returns (high_risk_imports, skipped_libraries).
    """
    high_risk_set = set(lib.lower() for lib in API_VERSION_HIGH_RISK_LIBRARIES)

    high_risk_imports: Dict[str, List[str]] = {}
    skipped: List[str] = []

    for lib, files in imported_libraries.items():
        canonical_lib = _canonicalize_api_import_name(lib)
        if canonical_lib in high_risk_set:
            if canonical_lib not in high_risk_imports:
                high_risk_imports[canonical_lib] = []
            for file_path in files:
                if file_path not in high_risk_imports[canonical_lib]:
                    high_risk_imports[canonical_lib].append(file_path)
        else:
            skipped.append(lib)

    # Limit to max libraries
    if len(high_risk_imports) > API_VERSION_CHECK_MAX_LIBRARIES:
        sorted_libs = sorted(
            high_risk_imports.keys(),
            key=lambda x: len(high_risk_imports[x]),
            reverse=True,
        )
        limited_imports = {}
        for lib in sorted_libs[:API_VERSION_CHECK_MAX_LIBRARIES]:
            limited_imports[lib] = high_risk_imports[lib]
        # Remaining are treated as skipped
        for lib in sorted_libs[API_VERSION_CHECK_MAX_LIBRARIES:]:
            skipped.append(lib)
        high_risk_imports = limited_imports

    return high_risk_imports, skipped


def _api_version_sort_key(version: Optional[str]) -> Tuple[Tuple[int, Any], ...]:
    text = str(version or "").strip()
    if not text:
        return tuple()
    normalized = re.sub(r"^[vV]", "", text)
    parts = re.findall(r"\d+|[A-Za-z]+", normalized)
    key: List[Tuple[int, Any]] = []
    for part in parts:
        if part.isdigit():
            key.append((1, int(part)))
        else:
            key.append((0, part.lower()))
    return tuple(key)


def _is_newer_api_version(candidate: Optional[str], current: Optional[str]) -> bool:
    if not candidate:
        return False
    if not current:
        return True
    return _api_version_sort_key(candidate) > _api_version_sort_key(current)


_API_VERSION_PATTERN = re.compile(
    r"(?:version|v)\s*[=:>]?\s*['\"]?(\d+(?:\.\d+)*(?:[a-zA-Z0-9\-_]*))['\"]?",
    re.IGNORECASE,
)
_API_DEPRECATION_KEYWORDS = ("deprecated", "deprecation", "removed", "migration")


def _update_api_search_signals(
    result: Dict[str, Any], citations: List[Dict[str, str]]
) -> None:
    for citation in citations:
        snippet = str(citation.get("snippet", "") or "")
        matches = _API_VERSION_PATTERN.findall(snippet)
        for version in matches:
            if _is_newer_api_version(version, result.get("latest_version")):
                result["latest_version"] = version
        snippet_lower = snippet.lower()
        if any(keyword in snippet_lower for keyword in _API_DEPRECATION_KEYWORDS):
            notice = snippet[:200]
            if notice and notice not in result["deprecation_notices"]:
                result["deprecation_notices"].append(notice)


# None = not yet loaded; {} = loaded but empty (no entries on disk).
# Using {} as the initial value is wrong: {} is falsy, so _load_api_version_cache
# would bypass the guard and re-open the disk file on every call after the first
# empty-result load.
_API_VERSION_CACHE: Optional[Dict[str, Dict[str, Any]]] = None


def _get_api_version_cache_path() -> str:
    """Get the path to the API version cache file."""
    cache_dir = os.path.join(_REPO_ROOT, "saved_projects", ".cache")
    os.makedirs(cache_dir, exist_ok=True)
    return os.path.join(cache_dir, "api_version_cache.json")


def _load_api_version_cache() -> Dict[str, Dict[str, Any]]:
    """Load the API version cache from disk."""
    global _API_VERSION_CACHE
    # Use `is not None` not truthiness: an empty dict {} is a valid cached
    # "nothing on disk" result and must not be re-loaded on every call.
    if _API_VERSION_CACHE is not None:
        return _API_VERSION_CACHE

    cache_path = _get_api_version_cache_path()
    try:
        if os.path.exists(cache_path):
            with open(cache_path, "r", encoding="utf-8") as f:
                _API_VERSION_CACHE = json.load(f)
                return _API_VERSION_CACHE
    except Exception:
        pass
    _API_VERSION_CACHE = {}
    return _API_VERSION_CACHE


def reset_api_version_cache() -> None:
    """Clear the in-process API version cache between repeated runtime runs."""
    global _API_VERSION_CACHE
    _API_VERSION_CACHE = {}


def _save_api_version_cache(cache: Dict[str, Dict[str, Any]]) -> None:
    """Save the API version cache to disk (atomic write via .tmp + os.replace)."""
    cache_path = _get_api_version_cache_path()
    try:
        _atomic_write_text(cache_path, json.dumps(cache, indent=2, ensure_ascii=False))
    except Exception:
        pass


def _get_cached_api_version(library: str) -> Optional[Dict[str, Any]]:
    """Get cached API version info if still valid (within TTL)."""
    cache = _load_api_version_cache()
    key = library.lower()

    if key not in cache:
        return None

    entry = cache[key]
    timestamp_str = entry.get("timestamp")
    if not timestamp_str:
        return None

    try:
        timestamp = datetime.fromisoformat(timestamp_str)
        age_hours = (datetime.now() - timestamp).total_seconds() / 3600
        if age_hours > API_VERSION_CHECK_CACHE_TTL_HOURS:
            return None
    except Exception:
        return None

    return entry.get("data")


def _set_cached_api_version(library: str, data: Dict[str, Any]) -> None:
    """Cache API version info with current timestamp."""
    cache = _load_api_version_cache()
    key = library.lower()
    cache[key] = {
        "timestamp": datetime.now().isoformat(),
        "data": data,
    }
    _save_api_version_cache(cache)


def _collect_api_target_files(
    code_bundle: "CodeBundle", target_files: Optional[List[str]] = None
) -> List["GeneratedFile"]:
    selected_paths = set(target_files or []) if target_files is not None else None
    selected_files: List[GeneratedFile] = []
    for gen_file in code_bundle.files or []:
        if (
            not gen_file.path
            or not gen_file.content
            or not gen_file.path.endswith(".py")
        ):
            continue
        if selected_paths is not None and gen_file.path not in selected_paths:
            continue
        selected_files.append(gen_file)
    return selected_files


def _extract_ccxt_search_context(
    code_bundle: "CodeBundle", target_files: Optional[List[str]] = None
) -> Dict[str, Any]:
    exchange_ids: List[str] = []
    import_flavors: List[str] = []
    for gen_file in _collect_api_target_files(code_bundle, target_files):
        content = gen_file.content or ""
        try:
            tree = ast.parse(content)
        except SyntaxError:
            tree = None
        if tree is not None:
            _set_ast_parents(tree)
            module_alias_flavors: Dict[str, str] = {}
            constructor_aliases: Dict[str, Dict[str, str]] = {}
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        alias_name = alias.asname or alias.name.split(".")[-1]
                        if alias.name == "ccxt":
                            import_flavors.append("sync")
                            module_alias_flavors[alias_name] = "sync"
                        elif alias.name == "ccxt.async_support":
                            import_flavors.append("async_support")
                            module_alias_flavors[alias_name] = "async_support"
                        elif alias.name == "ccxt.pro":
                            import_flavors.append("pro")
                            module_alias_flavors[alias_name] = "pro"
                elif isinstance(node, ast.ImportFrom):
                    module_name = str(node.module or "")
                    if module_name == "ccxt":
                        for alias in node.names:
                            if alias.name == "async_support":
                                import_flavors.append("async_support")
                                module_alias_flavors[alias.asname or alias.name] = (
                                    "async_support"
                                )
                            elif alias.name == "pro":
                                import_flavors.append("pro")
                                module_alias_flavors[alias.asname or alias.name] = "pro"
                            else:
                                import_flavors.append("sync")
                                constructor_aliases[alias.asname or alias.name] = {
                                    "flavor": "sync",
                                    "exchange_id": alias.name.lower(),
                                }
                    elif module_name == "ccxt.async_support":
                        import_flavors.append("async_support")
                        for alias in node.names:
                            constructor_aliases[alias.asname or alias.name] = {
                                "flavor": "async_support",
                                "exchange_id": alias.name.lower(),
                            }
                    elif module_name == "ccxt.pro":
                        import_flavors.append("pro")
                        for alias in node.names:
                            constructor_aliases[alias.asname or alias.name] = {
                                "flavor": "pro",
                                "exchange_id": alias.name.lower(),
                            }
            factory_returns = _extract_ccxt_factory_returns(
                tree, module_alias_flavors, constructor_aliases
            )
            for node in ast.walk(tree):
                if isinstance(node, ast.Call):
                    constructor_target = _resolve_ccxt_call_target(
                        node.func,
                        module_alias_flavors,
                        constructor_aliases,
                        factory_returns,
                    )
                    if constructor_target:
                        candidate = (
                            str(constructor_target.get("exchange_id") or "")
                            .strip()
                            .lower()
                        )
                        if candidate and candidate not in exchange_ids:
                            exchange_ids.append(candidate)
            continue
        pattern = re.compile(r"\bccxt(?:\.(?:async_support|pro))?\.(\w+)\s*\(")
        if re.search(
            r"\bimport\s+ccxt(?:\s+as\s+[A-Za-z_][A-Za-z0-9_]*)?(?:\s|$)", content
        ):
            import_flavors.append("sync")
        if re.search(r"\b(?:import|from)\s+ccxt\.async_support\b", content):
            import_flavors.append("async_support")
        if re.search(r"\b(?:import|from)\s+ccxt\.pro\b", content):
            import_flavors.append("pro")
        for match in pattern.finditer(content):
            candidate = str(match.group(1) or "").strip().lower()
            if (
                candidate
                and re.match(r"^[a-z0-9_]+$", candidate)
                and candidate not in exchange_ids
            ):
                exchange_ids.append(candidate)
    return {
        "exchange_ids": exchange_ids[:5],
        "import_flavors": _dedupe_text_items(import_flavors, limit=3),
    }


def _build_api_version_cache_key(
    library: str, search_context: Optional[Dict[str, Any]] = None
) -> str:
    normalized_library = str(library or "").strip().lower()
    if normalized_library != "ccxt":
        return normalized_library
    exchange_ids = list((search_context or {}).get("exchange_ids") or [])
    if not exchange_ids:
        return normalized_library
    return f"{normalized_library}|{','.join(exchange_ids[:5])}"


def _build_ccxt_official_search_queries(
    exchange_ids: Optional[List[str]] = None,
) -> List[str]:
    current_year = datetime.now().year
    previous_year = current_year - 1
    queries = [
        (
            "site:github.com/ccxt/ccxt/wiki/Manual "
            f"ccxt latest version {previous_year} {current_year}"
        ),
        "site:github.com/ccxt/ccxt/wiki/Manual ccxt load_markets create_order fetchOHLCV unified symbols",
        "site:github.com/ccxt/ccxt/wiki/Manual ccxt async_support pro migration deprecated",
        (
            "site:github.com/ccxt/ccxt/releases "
            f"ccxt release notes breaking changes {previous_year} {current_year}"
        ),
    ]
    for exchange_id in list(exchange_ids or [])[:3]:
        for domain in CCXT_OFFICIAL_EXCHANGE_DOCS.get(exchange_id, []):
            queries.append(
                f"site:{domain} {exchange_id} api rate limit order symbol futures spot"
            )
    return _dedupe_text_items(queries, limit=8)


def _search_ccxt_official_sources(
    exchange_ids: Optional[List[str]] = None,
) -> Dict[str, Any]:
    result = {
        "library": "ccxt",
        "latest_version": None,
        "deprecation_notices": [],
        "breaking_changes": [],
        "citations": [],
        "search_success": False,
        "error": None,
        "official_source_only": True,
    }
    all_citations: List[Dict[str, str]] = []
    for idx, query in enumerate(_build_ccxt_official_search_queries(exchange_ids)):
        try:
            citations = _search_websearch(
                query, timeout_seconds=API_VERSION_CHECK_TIMEOUT_SECONDS
            )
        except _OperationCancelledError:
            raise
        except Exception:
            continue
        new_citations: List[Dict[str, str]] = []
        for citation in citations[:3]:
            payload = {
                "title": citation.title,
                "url": citation.url,
                "snippet": citation.snippet,
            }
            all_citations.append(payload)
            new_citations.append(payload)
        if new_citations:
            _update_api_search_signals(result, new_citations)
        if idx >= 1 and result["latest_version"] and result["deprecation_notices"]:
            break
    if all_citations:
        result["citations"] = all_citations
        result["search_success"] = True
    return result


def _search_library_latest_version(
    library: str, *, search_context: Optional[Dict[str, Any]] = None
) -> Dict[str, Any]:
    """
    Search for the latest version of a library using web search.
    Returns dict with version info, deprecation notices, and citations.
    """
    if str(library or "").strip().lower() == "ccxt":
        return _search_ccxt_official_sources(
            list((search_context or {}).get("exchange_ids") or [])
        )

    result = {
        "library": library,
        "latest_version": None,
        "deprecation_notices": [],
        "breaking_changes": [],
        "citations": [],
        "search_success": False,
        "error": None,
    }

    # Build search query
    doc_domain = API_VERSION_LIBRARY_DOC_DOMAINS.get(library.lower())
    current_year = datetime.now().year
    previous_year = current_year - 1

    queries: List[str] = []
    if doc_domain:
        queries.append(f"site:{doc_domain} {library} latest version")
    queries.extend(
        [
            f"{library} latest version {previous_year} {current_year}",
            f"{library} changelog breaking changes {previous_year} {current_year}",
            f"{library} deprecation migration guide {previous_year} {current_year}",
        ]
    )

    all_citations: List[Dict[str, str]] = []

    for idx, query in enumerate(queries):
        try:
            citations = _search_websearch(
                query, timeout_seconds=API_VERSION_CHECK_TIMEOUT_SECONDS
            )
            new_citations: List[Dict[str, str]] = []
            for citation in citations[:3]:  # Top 3 results per query
                payload = {
                    "title": citation.title,
                    "url": citation.url,
                    "snippet": citation.snippet,
                }
                all_citations.append(payload)
                new_citations.append(payload)
            if new_citations:
                _update_api_search_signals(result, new_citations)
        except _OperationCancelledError:
            raise
        except Exception:
            continue
        if idx >= 1 and result["latest_version"] and result["deprecation_notices"]:
            break
        if idx >= 3:
            break

    if all_citations:
        result["citations"] = all_citations
        result["search_success"] = True

    return result


def _ast_expr_name(expr: Optional[ast.AST]) -> str:
    if expr is None:
        return ""
    if isinstance(expr, ast.Name):
        return expr.id
    if isinstance(expr, ast.Attribute):
        prefix = _ast_expr_name(expr.value)
        return f"{prefix}.{expr.attr}" if prefix else expr.attr
    if isinstance(expr, ast.Subscript):
        prefix = _ast_expr_name(expr.value)
        key_name = _ast_subscript_key_name(expr.slice)
        if prefix and key_name:
            return f"{prefix}[{key_name}]"
    return ""


def _ast_constant_bool(expr: Optional[ast.AST]) -> Optional[bool]:
    if isinstance(expr, ast.Constant) and isinstance(expr.value, bool):
        return bool(expr.value)
    return None


def _ast_constant_str(expr: Optional[ast.AST]) -> Optional[str]:
    if isinstance(expr, ast.Constant) and isinstance(expr.value, str):
        return str(expr.value)
    return None


def _ast_subscript_key_name(expr: Optional[ast.AST]) -> str:
    slice_node = expr
    if isinstance(slice_node, ast.Index):  # pragma: no cover - py<3.9 compatibility
        slice_node = slice_node.value
    key_text = _ast_constant_str(slice_node)
    if key_text is not None:
        return repr(key_text)
    if isinstance(slice_node, ast.Constant) and isinstance(slice_node.value, int):
        return str(slice_node.value)
    return _ast_expr_name(slice_node)


def _set_ast_parents(tree: ast.AST) -> None:
    for parent in ast.walk(tree):
        for child in ast.iter_child_nodes(parent):
            setattr(child, "_parent", parent)


def _parse_ccxt_constructor_target(
    func: ast.AST,
    module_alias_flavors: Dict[str, str],
    constructor_aliases: Dict[str, Dict[str, str]],
) -> Optional[Dict[str, str]]:
    if isinstance(func, ast.Name):
        return constructor_aliases.get(func.id)
    full_name = _ast_expr_name(func)
    if not full_name:
        return None
    parts = full_name.split(".")
    if len(parts) >= 2 and parts[0] in module_alias_flavors:
        flavor = module_alias_flavors[parts[0]]
        if len(parts) == 2:
            return {"flavor": flavor, "exchange_id": parts[1].lower()}
        if len(parts) == 3 and parts[1] in ("async_support", "pro"):
            return {"flavor": parts[1], "exchange_id": parts[2].lower()}
    if len(parts) == 3 and parts[0] == "ccxt" and parts[1] in ("async_support", "pro"):
        return {"flavor": parts[1], "exchange_id": parts[2].lower()}
    if len(parts) == 2 and parts[0] == "ccxt":
        return {"flavor": "sync", "exchange_id": parts[1].lower()}
    return None


def _extract_ccxt_constructor_option_keys(call: ast.Call) -> Set[str]:
    option_keys: Set[str] = set()
    if call.args and isinstance(call.args[0], ast.Dict):
        for key_node in call.args[0].keys:
            key_text = _ast_constant_str(key_node)
            if key_text:
                option_keys.add(key_text)
    for keyword in call.keywords or []:
        if keyword.arg:
            option_keys.add(str(keyword.arg))
    return option_keys


def _extract_except_type_names(node: ast.ExceptHandler) -> Set[str]:
    names: Set[str] = set()

    def _collect(expr: Optional[ast.AST]) -> None:
        if expr is None:
            return
        if isinstance(expr, ast.Tuple):
            for elt in expr.elts:
                _collect(elt)
            return
        name = _ast_expr_name(expr)
        if name:
            names.add(name)

    _collect(node.type)
    return names


def _extract_ccxt_binding_name(target: Optional[ast.AST]) -> str:
    if isinstance(target, (ast.Name, ast.Attribute, ast.Subscript)):
        return _ast_expr_name(target)
    return ""


def _nearest_ast_ancestor(
    node: Optional[ast.AST], ancestor_types: Tuple[type, ...]
) -> Optional[ast.AST]:
    current = getattr(node, "_parent", None)
    while current is not None:
        if isinstance(current, ancestor_types):
            return current
        current = getattr(current, "_parent", None)
    return None


def _is_same_function_scope(node: Optional[ast.AST], function_node: ast.AST) -> bool:
    if node is None:
        return False
    nearest_function = _nearest_ast_ancestor(
        node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)
    )
    return nearest_function is function_node


def _ccxt_factory_name_candidates(function_node: ast.AST) -> List[str]:
    name = str(getattr(function_node, "name", "") or "").strip()
    if not name:
        return []
    names = [name]
    parent_class = _nearest_ast_ancestor(function_node, (ast.ClassDef,))
    if isinstance(parent_class, ast.ClassDef):
        names.extend(
            [
                f"self.{name}",
                f"cls.{name}",
                f"{parent_class.name}.{name}",
            ]
        )
    return _dedupe_text_items(names, limit=6)


def _copy_ccxt_client_meta(meta: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    if not meta:
        return {}
    copied = dict(meta)
    copied["config_keys"] = set(meta.get("config_keys", set()) or set())
    copied["load_markets_lines"] = list(meta.get("load_markets_lines", []) or [])
    copied["has_guards"] = set(meta.get("has_guards", set()) or set())
    return copied


def _resolve_ccxt_call_target(
    func: ast.AST,
    module_alias_flavors: Dict[str, str],
    constructor_aliases: Dict[str, Dict[str, str]],
    factory_returns: Optional[Dict[str, Dict[str, Any]]] = None,
) -> Optional[Dict[str, Any]]:
    constructor_target = _parse_ccxt_constructor_target(
        func, module_alias_flavors, constructor_aliases
    )
    if constructor_target is not None:
        return _copy_ccxt_client_meta(constructor_target)
    full_name = _ast_expr_name(func)
    if full_name:
        factory_target = (factory_returns or {}).get(full_name)
        if factory_target is not None:
            return _copy_ccxt_client_meta(factory_target)
    if isinstance(func, ast.Name):
        factory_target = (factory_returns or {}).get(func.id)
        if factory_target is not None:
            return _copy_ccxt_client_meta(factory_target)
    return None


def _extract_ccxt_factory_returns(
    tree: ast.AST,
    module_alias_flavors: Dict[str, str],
    constructor_aliases: Dict[str, Dict[str, str]],
) -> Dict[str, Dict[str, Any]]:
    factory_returns: Dict[str, Dict[str, Any]] = {}
    function_nodes = sorted(
        [
            node
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        ],
        key=lambda node: getattr(node, "lineno", 0),
    )
    for _ in range(max(len(function_nodes), 1)):
        changed = False
        for function_node in function_nodes:
            local_bindings: Dict[str, Dict[str, Any]] = {}
            for child in ast.walk(function_node):
                if child is function_node or not _is_same_function_scope(
                    child, function_node
                ):
                    continue
                if isinstance(child, ast.Assign) and isinstance(child.value, ast.Call):
                    constructor_target = _resolve_ccxt_call_target(
                        child.value.func,
                        module_alias_flavors,
                        constructor_aliases,
                        factory_returns,
                    )
                    if constructor_target is None:
                        continue
                    constructor_target["config_keys"].update(
                        _extract_ccxt_constructor_option_keys(child.value)
                    )
                    constructor_target["constructor_lineno"] = getattr(
                        child, "lineno", None
                    )
                    for target in child.targets:
                        binding_name = _extract_ccxt_binding_name(target)
                        if binding_name:
                            local_bindings[binding_name] = _copy_ccxt_client_meta(
                                constructor_target
                            )
                elif isinstance(child, ast.AnnAssign) and isinstance(
                    child.value, ast.Call
                ):
                    constructor_target = _resolve_ccxt_call_target(
                        child.value.func,
                        module_alias_flavors,
                        constructor_aliases,
                        factory_returns,
                    )
                    binding_name = _extract_ccxt_binding_name(child.target)
                    if constructor_target is not None and binding_name:
                        constructor_target["config_keys"].update(
                            _extract_ccxt_constructor_option_keys(child.value)
                        )
                        constructor_target["constructor_lineno"] = getattr(
                            child, "lineno", None
                        )
                        local_bindings[binding_name] = _copy_ccxt_client_meta(
                            constructor_target
                        )
                elif isinstance(child, ast.AnnAssign) and isinstance(
                    child.value, (ast.Name, ast.Attribute, ast.Subscript)
                ):
                    binding_target = local_bindings.get(
                        _extract_ccxt_binding_name(child.value)
                    )
                    binding_name = _extract_ccxt_binding_name(child.target)
                    if binding_target is not None and binding_name:
                        local_bindings[binding_name] = binding_target
                elif isinstance(child, ast.Assign):
                    if isinstance(
                        child.value, (ast.Name, ast.Attribute, ast.Subscript)
                    ):
                        binding_target = local_bindings.get(
                            _extract_ccxt_binding_name(child.value)
                        )
                        if binding_target is not None:
                            for target in child.targets:
                                binding_name = _extract_ccxt_binding_name(target)
                                if binding_name:
                                    local_bindings[binding_name] = binding_target
                    for target in child.targets:
                        if not isinstance(target, ast.Attribute):
                            continue
                        owner_var = _ast_expr_name(target.value)
                        if owner_var not in local_bindings:
                            continue
                        attr_name = str(target.attr or "")
                        if attr_name == "enableRateLimit":
                            if _ast_constant_bool(child.value) is True:
                                local_bindings[owner_var].setdefault(
                                    "config_keys", set()
                                ).add("enableRateLimit")
                        elif attr_name == "timeout":
                            local_bindings[owner_var].setdefault(
                                "config_keys", set()
                            ).add("timeout")
                elif isinstance(child, ast.Call) and isinstance(
                    child.func, ast.Attribute
                ):
                    owner_var = _ast_expr_name(child.func.value)
                    if (
                        owner_var in local_bindings
                        and str(child.func.attr or "") == "load_markets"
                    ):
                        local_bindings[owner_var].setdefault(
                            "load_markets_lines", []
                        ).append(getattr(child, "lineno", None))
            return_target: Optional[Dict[str, Any]] = None
            for child in ast.walk(function_node):
                if child is function_node or not _is_same_function_scope(
                    child, function_node
                ):
                    continue
                if isinstance(child, ast.Return):
                    value = child.value
                    if isinstance(value, ast.Call):
                        return_target = _resolve_ccxt_call_target(
                            value.func,
                            module_alias_flavors,
                            constructor_aliases,
                            factory_returns,
                        )
                        if return_target is not None:
                            return_target["config_keys"].update(
                                _extract_ccxt_constructor_option_keys(value)
                            )
                            return_target["constructor_lineno"] = getattr(
                                child, "lineno", None
                            )
                    elif isinstance(value, (ast.Name, ast.Attribute, ast.Subscript)):
                        binding_target = local_bindings.get(
                            _extract_ccxt_binding_name(value)
                        )
                        if binding_target is not None:
                            return_target = _copy_ccxt_client_meta(binding_target)
                    if return_target is not None:
                        break
            if return_target is None:
                continue
            for factory_name in _ccxt_factory_name_candidates(function_node):
                normalized_target = _copy_ccxt_client_meta(return_target)
                if factory_returns.get(factory_name) != normalized_target:
                    factory_returns[factory_name] = normalized_target
                    changed = True
        if not changed:
            break
    return factory_returns


def _extract_ccxt_has_guard(node: ast.AST) -> Optional[Tuple[str, str]]:
    if isinstance(node, ast.Subscript) and isinstance(node.value, ast.Attribute):
        if str(node.value.attr or "") != "has":
            return None
        owner_var = _ast_expr_name(node.value.value)
        slice_node = node.slice
        if isinstance(slice_node, ast.Index):  # pragma: no cover - py<3.9 compatibility
            slice_node = slice_node.value
        guard_key = _ast_constant_str(slice_node)
        if owner_var and guard_key:
            return owner_var, guard_key
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and str(node.func.attr or "") == "get"
        and isinstance(node.func.value, ast.Attribute)
        and str(node.func.value.attr or "") == "has"
    ):
        owner_var = _ast_expr_name(node.func.value.value)
        guard_key = _ast_constant_str(node.args[0]) if node.args else None
        if owner_var and guard_key:
            return owner_var, guard_key
    return None


def _make_ccxt_issue(
    *,
    search_result: Dict[str, Any],
    file_path: str,
    severity: str,
    description: str,
    suggestion: str,
    deprecated_api: Optional[str] = None,
    recommended_api: Optional[str] = None,
    line_hint: Optional[str] = None,
    citation_url: Optional[str] = None,
) -> "ApiVersionIssue":
    default_citation_url = citation_url
    if not default_citation_url:
        default_citation_url = (
            search_result.get("citations", [{}])[0].get("url")
            if search_result.get("citations")
            else CCXT_OFFICIAL_MANUAL_URL
        )
    return ApiVersionIssue(
        library="ccxt",
        latest_version=search_result.get("latest_version"),
        is_deprecated=bool(deprecated_api),
        deprecated_api=deprecated_api,
        recommended_api=recommended_api,
        severity=severity,
        file=file_path,
        line_hint=line_hint,
        description=description,
        suggestion=suggestion,
        citation_url=default_citation_url,
    )


def _analyze_ccxt_api_usage(
    code_bundle: "CodeBundle",
    search_result: Dict[str, Any],
    *,
    target_files: Optional[List[str]] = None,
) -> List["ApiVersionIssue"]:
    issues: List[ApiVersionIssue] = []
    for gen_file in _collect_api_target_files(code_bundle, target_files):
        content = gen_file.content or ""
        if "ccxt" not in content.lower():
            continue
        try:
            tree = ast.parse(content)
        except SyntaxError:
            tree = None

        module_alias_flavors: Dict[str, str] = {}
        constructor_aliases: Dict[str, Dict[str, str]] = {}
        used_flavors: Set[str] = set()
        exchange_vars: Dict[str, Dict[str, Any]] = {}
        method_calls: List[Dict[str, Any]] = []
        except_type_names: Set[str] = set()
        sandbox_modes: Set[bool] = set()
        has_market_metadata_check = any(
            marker in content
            for marker in (
                ".market(",
                ".markets[",
                ".amount_to_precision(",
                ".price_to_precision(",
                ".cost_to_precision(",
            )
        )
        if tree is not None:
            _set_ast_parents(tree)
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        alias_name = alias.asname or alias.name.split(".")[-1]
                        if alias.name == "ccxt":
                            module_alias_flavors[alias_name] = "sync"
                            used_flavors.add("sync")
                        elif alias.name == "ccxt.async_support":
                            module_alias_flavors[alias_name] = "async_support"
                            used_flavors.add("async_support")
                        elif alias.name == "ccxt.pro":
                            module_alias_flavors[alias_name] = "pro"
                            used_flavors.add("pro")
                elif isinstance(node, ast.ImportFrom):
                    module_name = str(node.module or "")
                    if module_name == "ccxt":
                        used_flavors.add("sync")
                    elif module_name == "ccxt.async_support":
                        used_flavors.add("async_support")
                    elif module_name == "ccxt.pro":
                        used_flavors.add("pro")
                    if module_name in ("ccxt.async_support", "ccxt.pro"):
                        if module_name.endswith("async_support"):
                            for alias in node.names:
                                alias_name = alias.asname or alias.name
                                constructor_aliases[alias_name] = {
                                    "flavor": "async_support",
                                    "exchange_id": alias.name.lower(),
                                }
                        elif module_name.endswith("pro"):
                            for alias in node.names:
                                alias_name = alias.asname or alias.name
                                constructor_aliases[alias_name] = {
                                    "flavor": "pro",
                                    "exchange_id": alias.name.lower(),
                                }
                    elif module_name == "ccxt":
                        for alias in node.names:
                            if alias.name == "async_support":
                                module_alias_flavors[alias.asname or alias.name] = (
                                    "async_support"
                                )
                            elif alias.name == "pro":
                                module_alias_flavors[alias.asname or alias.name] = "pro"
            factory_returns = _extract_ccxt_factory_returns(
                tree, module_alias_flavors, constructor_aliases
            )
            for node in ast.walk(tree):
                if isinstance(node, ast.Assign) and isinstance(node.value, ast.Call):
                    constructor_target = _resolve_ccxt_call_target(
                        node.value.func,
                        module_alias_flavors,
                        constructor_aliases,
                        factory_returns,
                    )
                    if constructor_target:
                        option_keys = set(
                            constructor_target.get("config_keys", set()) or set()
                        )
                        option_keys.update(
                            _extract_ccxt_constructor_option_keys(node.value)
                        )
                        inherited_load_markets = list(
                            constructor_target.get("load_markets_lines", []) or []
                        )
                        inherited_has_guards = set(
                            constructor_target.get("has_guards", set()) or set()
                        )
                        for target in node.targets:
                            binding_name = _extract_ccxt_binding_name(target)
                            if binding_name:
                                exchange_vars[binding_name] = {
                                    "exchange_id": constructor_target["exchange_id"],
                                    "flavor": constructor_target["flavor"],
                                    "config_keys": set(option_keys),
                                    "constructor_lineno": getattr(node, "lineno", None),
                                    "load_markets_lines": list(inherited_load_markets),
                                    "has_guards": set(inherited_has_guards),
                                }
                elif isinstance(node, ast.AnnAssign) and isinstance(
                    node.value, ast.Call
                ):
                    constructor_target = _resolve_ccxt_call_target(
                        node.value.func,
                        module_alias_flavors,
                        constructor_aliases,
                        factory_returns,
                    )
                    binding_name = _extract_ccxt_binding_name(node.target)
                    if constructor_target and binding_name:
                        option_keys = set(
                            constructor_target.get("config_keys", set()) or set()
                        )
                        option_keys.update(
                            _extract_ccxt_constructor_option_keys(node.value)
                        )
                        exchange_vars[binding_name] = {
                            "exchange_id": constructor_target["exchange_id"],
                            "flavor": constructor_target["flavor"],
                            "config_keys": set(option_keys),
                            "constructor_lineno": getattr(node, "lineno", None),
                            "load_markets_lines": list(
                                constructor_target.get("load_markets_lines", []) or []
                            ),
                            "has_guards": set(
                                constructor_target.get("has_guards", set()) or set()
                            ),
                        }
                elif isinstance(node, ast.AnnAssign) and isinstance(
                    node.value, (ast.Name, ast.Attribute, ast.Subscript)
                ):
                    binding_target = exchange_vars.get(
                        _extract_ccxt_binding_name(node.value)
                    )
                    binding_name = _extract_ccxt_binding_name(node.target)
                    if binding_target is not None and binding_name:
                        exchange_vars[binding_name] = binding_target
                elif isinstance(node, ast.Assign):
                    if isinstance(node.value, (ast.Name, ast.Attribute, ast.Subscript)):
                        binding_target = exchange_vars.get(
                            _extract_ccxt_binding_name(node.value)
                        )
                        if binding_target is not None:
                            for target in node.targets:
                                binding_name = _extract_ccxt_binding_name(target)
                                if binding_name:
                                    exchange_vars[binding_name] = binding_target
                    for target in node.targets:
                        if not isinstance(target, ast.Attribute):
                            continue
                        owner_var = _ast_expr_name(target.value)
                        if owner_var not in exchange_vars:
                            continue
                        attr_name = str(target.attr or "")
                        if attr_name == "enableRateLimit":
                            if _ast_constant_bool(node.value) is True:
                                exchange_vars[owner_var].setdefault(
                                    "config_keys", set()
                                ).add("enableRateLimit")
                        elif attr_name == "timeout":
                            exchange_vars[owner_var].setdefault(
                                "config_keys", set()
                            ).add("timeout")
                elif isinstance(node, ast.Call) and isinstance(
                    node.func, ast.Attribute
                ):
                    owner_name = _ast_expr_name(node.func.value)
                    method_name = str(node.func.attr or "")
                    owner_var = owner_name
                    if owner_var in exchange_vars:
                        if method_name == "load_markets":
                            exchange_vars[owner_var]["load_markets_lines"].append(
                                getattr(node, "lineno", None)
                            )
                        if method_name == "set_sandbox_mode":
                            sandbox_value = None
                            if node.args:
                                sandbox_value = _ast_constant_bool(node.args[0])
                            elif node.keywords:
                                for keyword in node.keywords:
                                    if keyword.arg == "enabled":
                                        sandbox_value = _ast_constant_bool(
                                            keyword.value
                                        )
                                        break
                            if sandbox_value is not None:
                                sandbox_modes.add(sandbox_value)
                        if method_name in CCXT_RISKY_METHODS or method_name == "close":
                            parent = getattr(node, "_parent", None)
                            awaited = isinstance(parent, ast.Await)
                            first_arg = (
                                _ast_constant_str(node.args[0]) if node.args else None
                            )
                            second_arg = (
                                _ast_constant_str(node.args[1])
                                if len(node.args) > 1
                                else None
                            )
                            method_calls.append(
                                {
                                    "var_name": owner_var,
                                    "method": method_name,
                                    "lineno": getattr(node, "lineno", None),
                                    "awaited": awaited,
                                    "first_arg": first_arg,
                                    "second_arg": second_arg,
                                }
                            )
                elif isinstance(node, ast.ExceptHandler):
                    except_type_names.update(_extract_except_type_names(node))
                guard = _extract_ccxt_has_guard(node)
                if guard is not None:
                    owner_var, guard_key = guard
                    if owner_var in exchange_vars:
                        exchange_vars[owner_var].setdefault("has_guards", set()).add(
                            guard_key
                        )

        if "sync" in used_flavors and (
            "async_support" in used_flavors or "pro" in used_flavors
        ):
            issues.append(
                _make_ccxt_issue(
                    search_result=search_result,
                    file_path=gen_file.path,
                    severity="high",
                    description="This file mixes sync ccxt imports with async or websocket ccxt imports.",
                    suggestion="Use one execution model per file. Keep sync, async_support, and pro clients separated to avoid coroutine or socket misuse.",
                    deprecated_api="mixed_ccxt_execution_model",
                    recommended_api="separate_sync_and_async_clients",
                    citation_url=CCXT_OFFICIAL_MANUAL_URL,
                )
            )

        risky_calls = [
            call for call in method_calls if call["method"] in CCXT_RISKY_METHODS
        ]
        if sandbox_modes == {True, False}:
            issues.append(
                _make_ccxt_issue(
                    search_result=search_result,
                    file_path=gen_file.path,
                    severity="high",
                    description="This file toggles both sandbox and live ccxt modes.",
                    suggestion="Use exactly one sandbox mode per runtime path and keep sandbox/live credentials separated.",
                    deprecated_api="mixed_sandbox_live_mode",
                    recommended_api="single_sandbox_mode_per_runtime",
                    citation_url=CCXT_OFFICIAL_MANUAL_URL,
                )
            )

        async_risky_calls = [
            call
            for call in risky_calls
            if exchange_vars.get(call["var_name"], {}).get("flavor") == "async_support"
            and not call["awaited"]
        ]
        if async_risky_calls:
            issues.append(
                _make_ccxt_issue(
                    search_result=search_result,
                    file_path=gen_file.path,
                    severity="high",
                    description="Detected ccxt.async_support API calls without await.",
                    suggestion="Await async ccxt methods such as load_markets, fetch_ohlcv, create_order, and fetch_balance.",
                    deprecated_api="async_ccxt_call_without_await",
                    recommended_api="await_ccxt_async_support_calls",
                    line_hint=f"line {async_risky_calls[0]['lineno']}",
                    citation_url=CCXT_OFFICIAL_MANUAL_URL,
                )
            )

        uses_async_flavor = any(
            details.get("flavor") in ("async_support", "pro")
            for details in exchange_vars.values()
        )
        if uses_async_flavor and not any(
            call["method"] == "close" for call in method_calls
        ):
            issues.append(
                _make_ccxt_issue(
                    search_result=search_result,
                    file_path=gen_file.path,
                    severity="medium",
                    description="Async or websocket ccxt clients are created without a matching close() call.",
                    suggestion="Close ccxt.async_support or ccxt.pro clients explicitly to avoid leaked sessions and sockets.",
                    deprecated_api="missing_ccxt_client_close",
                    recommended_api="await_exchange_close",
                    citation_url=CCXT_OFFICIAL_MANUAL_URL,
                )
            )

        constructor_without_rate_limit = next(
            (
                details
                for details in exchange_vars.values()
                if "enableRateLimit" not in details.get("config_keys", set())
            ),
            None,
        )
        if constructor_without_rate_limit is not None:
            issues.append(
                _make_ccxt_issue(
                    search_result=search_result,
                    file_path=gen_file.path,
                    severity="medium",
                    description="ccxt exchange client is created without enableRateLimit in its constructor config.",
                    suggestion="Set enableRateLimit=True in the exchange constructor to respect exchange throttling automatically.",
                    deprecated_api="missing_enableRateLimit",
                    recommended_api="enableRateLimit=True",
                    citation_url=CCXT_OFFICIAL_MANUAL_URL,
                )
            )

        constructor_without_timeout = next(
            (
                details
                for details in exchange_vars.values()
                if "timeout" not in details.get("config_keys", set())
            ),
            None,
        )
        if constructor_without_timeout is not None:
            issues.append(
                _make_ccxt_issue(
                    search_result=search_result,
                    file_path=gen_file.path,
                    severity="medium",
                    description="ccxt exchange client constructor config is missing an explicit timeout.",
                    suggestion="Set a finite timeout in the exchange constructor so network stalls do not hang the runtime.",
                    deprecated_api="missing_ccxt_timeout",
                    recommended_api="timeout=<milliseconds>",
                    citation_url=CCXT_OFFICIAL_MANUAL_URL,
                )
            )

        first_market_sensitive_call = next(
            (
                call
                for call in risky_calls
                if call["method"] in {"fetch_ohlcv", "create_order", "fetch_balance"}
            ),
            None,
        )
        if first_market_sensitive_call is not None:
            exchange_details = exchange_vars.get(
                first_market_sensitive_call["var_name"], {}
            )
            load_markets_lines = list(exchange_details.get("load_markets_lines") or [])
            first_load_markets_line = (
                min(load_markets_lines) if load_markets_lines else None
            )
            if first_load_markets_line is None or (
                first_market_sensitive_call["lineno"] is not None
                and first_load_markets_line is not None
                and first_load_markets_line > first_market_sensitive_call["lineno"]
            ):
                issues.append(
                    _make_ccxt_issue(
                        search_result=search_result,
                        file_path=gen_file.path,
                        severity="high",
                        description="ccxt market-sensitive methods are used before load_markets() is called.",
                        suggestion="Call load_markets() before fetch_ohlcv, fetch_balance, or create_order so symbols and market metadata are initialized.",
                        deprecated_api="missing_load_markets",
                        recommended_api="load_markets_before_trading",
                        line_hint=f"line {first_market_sensitive_call['lineno']}",
                        citation_url=CCXT_OFFICIAL_MANUAL_URL,
                    )
                )

        for method_name, capability_key in CCXT_CAPABILITY_KEYS.items():
            first_call = next(
                (call for call in risky_calls if call["method"] == method_name),
                None,
            )
            if first_call is None:
                continue
            exchange_details = exchange_vars.get(first_call["var_name"], {})
            has_guards = set(exchange_details.get("has_guards", set()) or set())
            if capability_key not in has_guards:
                issues.append(
                    _make_ccxt_issue(
                        search_result=search_result,
                        file_path=gen_file.path,
                        severity="medium",
                        description=f"ccxt method {method_name} is used without checking exchange.has for {capability_key}.",
                        suggestion=f"Guard {method_name} with exchange.has['{capability_key}'] or exchange.has.get('{capability_key}') before calling it.",
                        deprecated_api=f"missing_has_guard:{capability_key}",
                        recommended_api=f"exchange.has['{capability_key}']",
                        line_hint=f"line {first_call['lineno']}",
                        citation_url=CCXT_OFFICIAL_MANUAL_URL,
                    )
                )

        raw_symbol_call = next(
            (
                call
                for call in risky_calls
                if call["method"] in CCXT_SYMBOL_METHODS
                and call.get("first_arg")
                and "/" not in str(call["first_arg"])
                and ":" not in str(call["first_arg"])
            ),
            None,
        )
        if raw_symbol_call is not None:
            issues.append(
                _make_ccxt_issue(
                    search_result=search_result,
                    file_path=gen_file.path,
                    severity="medium",
                    description="ccxt unified API appears to use a raw market id instead of a unified symbol.",
                    suggestion="Use unified symbols such as BTC/USDT for ccxt unified methods unless you intentionally call exchange-specific raw endpoints.",
                    deprecated_api="raw_market_id_in_unified_call",
                    recommended_api="unified_symbol_format",
                    line_hint=f"line {raw_symbol_call['lineno']}: {raw_symbol_call['first_arg']}",
                    citation_url=CCXT_OFFICIAL_MANUAL_URL,
                )
            )

        invalid_timeframe_call = next(
            (
                call
                for call in risky_calls
                if call["method"] == "fetch_ohlcv"
                and call.get("second_arg")
                and call["second_arg"] not in CCXT_COMMON_TIMEFRAMES
            ),
            None,
        )
        if invalid_timeframe_call is not None:
            issues.append(
                _make_ccxt_issue(
                    search_result=search_result,
                    file_path=gen_file.path,
                    severity="low",
                    description="fetch_ohlcv uses a timeframe literal that is uncommon for ccxt unified APIs.",
                    suggestion="Verify the timeframe against exchange.timeframes or use a known unified timeframe such as 1m, 5m, 1h, or 1d.",
                    deprecated_api="suspicious_ccxt_timeframe",
                    recommended_api="exchange.timeframes",
                    line_hint=f"line {invalid_timeframe_call['lineno']}: {invalid_timeframe_call['second_arg']}",
                    citation_url=CCXT_OFFICIAL_MANUAL_URL,
                )
            )

        create_order_call = next(
            (call for call in risky_calls if call["method"] == "create_order"),
            None,
        )
        if create_order_call is not None and not has_market_metadata_check:
            issues.append(
                _make_ccxt_issue(
                    search_result=search_result,
                    file_path=gen_file.path,
                    severity="medium",
                    description="create_order is used without visible market metadata or precision validation.",
                    suggestion="Validate market metadata and normalize amount/price with market(), amount_to_precision(), and price_to_precision() before create_order.",
                    deprecated_api="missing_ccxt_precision_validation",
                    recommended_api="market_metadata_and_precision_validation",
                    line_hint=f"line {create_order_call['lineno']}",
                    citation_url=CCXT_OFFICIAL_MANUAL_URL,
                )
            )

        if risky_calls and not except_type_names.intersection(CCXT_RETRY_ERROR_NAMES):
            issues.append(
                _make_ccxt_issue(
                    search_result=search_result,
                    file_path=gen_file.path,
                    severity="medium",
                    description="Trading-facing ccxt calls are present without ccxt-specific network/exchange error handling.",
                    suggestion="Handle ccxt.NetworkError, ccxt.ExchangeError, RequestTimeout, and rate-limit related exceptions around exchange calls.",
                    deprecated_api="missing_ccxt_error_handling",
                    recommended_api="handle_ccxt_network_and_exchange_errors",
                    citation_url=CCXT_OFFICIAL_MANUAL_URL,
                )
            )

    deduped_issues: List[ApiVersionIssue] = []
    seen_issue_keys: Set[str] = set()
    for issue in issues:
        issue_key = "|".join(
            [
                str(issue.file or ""),
                str(issue.deprecated_api or ""),
                str(issue.description or ""),
            ]
        )
        if issue_key in seen_issue_keys:
            continue
        seen_issue_keys.add(issue_key)
        deduped_issues.append(issue)
        if len(deduped_issues) >= 15:
            break
    return deduped_issues


def _analyze_code_for_deprecated_apis(
    library: str,
    code_bundle: "CodeBundle",
    search_result: Dict[str, Any],
    llm: Any,
    *,
    target_files: Optional[List[str]] = None,
) -> List["ApiVersionIssue"]:
    """
    Use LLM to analyze code for deprecated API usage based on search results.
    Returns list of detected issues.
    """
    # Collect code snippets that import the library
    code_snippets: List[Dict[str, str]] = []
    target_file_set = set(target_files or []) if target_files is not None else None

    for gen_file in code_bundle.files or []:
        if not gen_file.path or not gen_file.content:
            continue
        if target_file_set is not None and gen_file.path not in target_file_set:
            continue
        # Get relevant code sections (limit to first 2000 chars per file)
        code_snippets.append(
            {
                "file": gen_file.path,
                "content": gen_file.content[:2000],
            }
        )

    if not code_snippets:
        return []

    # Build analysis prompt
    snippets_text = "\n\n".join(
        f"--- {s['file']} ---\n{s['content']}"
        for s in code_snippets[:3]  # Limit to 3 files
    )

    citations_text = "\n".join(
        f"- [{c.get('title', 'N/A')}]({c.get('url', 'N/A')}): {c.get('snippet', '')[:150]}"
        for c in search_result.get("citations", [])[:3]
    )

    prompt = f"""Analyze the following Python code for deprecated or outdated API usage related to the library "{library}".

LIBRARY: {library}
LATEST VERSION (from search): {search_result.get("latest_version", "unknown")}

DOCUMENTATION EXCERPTS:
{citations_text}

CODE TO ANALYZE:
{snippets_text}

INSTRUCTIONS:
1. Identify any API calls, imports, or patterns that may be deprecated or outdated.
2. For each issue found, provide:
   - The deprecated API/method name
   - The recommended replacement (if known)
   - Severity: "high" (breaking), "medium" (deprecated but works), "low" (warning)
   - A brief description
   - A suggested fix

Return a JSON array of issues. If no issues found, return an empty array [].

Format:
[
  {{
    "deprecated_api": "old_method_name",
    "recommended_api": "new_method_name",
    "severity": "medium",
    "description": "This method is deprecated since version X",
    "suggestion": "Use new_method_name instead"
  }}
]

JSON array only, no additional text:"""

    try:
        from crewai import Agent, Task, Crew

        analyzer = Agent(
            role="API Version Analyst",
            goal=f"Detect deprecated {library} API usage in code",
            backstory=f"You are an expert in {library} API evolution and migration patterns.",
            llm=llm,
            verbose=False,
        )

        task = Task(
            description=prompt,
            expected_output="JSON array of API version issues",
            agent=analyzer,
        )

        crew = Crew(
            agents=[analyzer],
            tasks=[task],
            verbose=False,
        )

        output = kickoff_crew_with_retry(
            crew,
            crew_name="api_version_analysis",
            logger=LOGGER,
            log_fields={"library": library},
        )
        raw_output = str(output).strip()
        # Reasoning-model defence: a chain-of-thought emitted ahead of the
        # answer can contain example / hypothetical JSON arrays (e.g. "Maybe
        # the answer looks like [{"deprecated_api": "..."}]").  The regex
        # below is non-greedy, so the FIRST match wins — without stripping
        # we would return the example instead of the real list.
        raw_output = _strip_reasoning_blocks(raw_output)

        # Extract JSON array from output
        json_match = re.search(r"\[\s*\{.*?\}\s*\]", raw_output, re.DOTALL)
        if json_match:
            issues_data = json.loads(json_match.group())

            issues: List[ApiVersionIssue] = []
            for item in issues_data[:5]:  # Limit to 5 issues per library
                issue = ApiVersionIssue(
                    library=library,
                    latest_version=search_result.get("latest_version"),
                    is_deprecated=True,
                    deprecated_api=item.get("deprecated_api"),
                    recommended_api=item.get("recommended_api"),
                    severity=item.get("severity", "medium"),
                    description=item.get("description", ""),
                    suggestion=item.get("suggestion", ""),
                    citation_url=search_result.get("citations", [{}])[0].get("url")
                    if search_result.get("citations")
                    else None,
                )
                issues.append(issue)

            return issues
    except _OperationCancelledError:
        # Cooperative cancellation must propagate — do not return an empty list
        # as if no deprecated APIs were found.
        raise
    except Exception as e:
        print(f"[Warn] API version analysis failed for {library}: {e}")

    return []


def _api_issues_to_review_issues(
    api_issues: List["ApiVersionIssue"],
) -> List["ReviewIssue"]:
    """Convert ApiVersionIssue list to ReviewIssue list for quality loop."""
    review_issues: List[ReviewIssue] = []

    for api_issue in api_issues:
        # Map severity
        severity = api_issue.severity.lower()
        if severity not in ("low", "medium", "high"):
            severity = "medium"

        # Build description
        desc = api_issue.description
        if api_issue.deprecated_api:
            desc = f"[{api_issue.library}] Deprecated API: {api_issue.deprecated_api}"
            if api_issue.recommended_api:
                desc += f" → Use {api_issue.recommended_api}"

        # Build suggestion
        suggestion = api_issue.suggestion
        if not suggestion and api_issue.recommended_api:
            suggestion = f"Migrate to {api_issue.recommended_api}"
        if api_issue.latest_version:
            suggestion = (
                f"[{api_issue.library} v{api_issue.latest_version}] {suggestion}"
            )

        review_issue = ReviewIssue(
            severity=severity,
            category="other",  # API version issues don't fit standard categories
            description=desc,
            file=api_issue.file,
            suggestion=suggestion,
        )
        review_issues.append(review_issue)

    return review_issues


def run_api_version_check(
    code_bundle: "CodeBundle",
    llm: Any,
    *,
    enabled: bool = True,
) -> "ApiVersionReport":
    """
    Run API version check on generated code.

    This function:
    1. Extracts imports from the code bundle
    2. Filters against high-risk library list
    3. Searches for latest API documentation
    4. Analyzes code for deprecated API usage
    5. Returns a report with issues

    Note: This is a ONE-TIME upfront validation, NOT part of the quality loop.
    It should run after CodeGen but BEFORE quality review.
    """
    if not enabled:
        return ApiVersionReport(
            needs_update=False,
            issues=[],
            checked_libraries=[],
            skipped_libraries=[],
            confidence="low",
            summary="API version check disabled.",
        )

    if not code_bundle or not code_bundle.files:
        return ApiVersionReport(
            needs_update=False,
            issues=[],
            checked_libraries=[],
            skipped_libraries=[],
            confidence="high",
            summary="No code to check.",
        )

    print("[System] Running API version check...")

    # Step 1: Extract imports
    imported_libraries = _extract_imports_from_code(code_bundle)

    if not imported_libraries:
        return ApiVersionReport(
            needs_update=False,
            issues=[],
            checked_libraries=[],
            skipped_libraries=list(imported_libraries.keys()),
            confidence="high",
            summary="No imports found in generated code.",
        )

    # Step 2: Filter high-risk libraries
    high_risk_imports, skipped = _filter_high_risk_libraries(imported_libraries)

    if not high_risk_imports:
        return ApiVersionReport(
            needs_update=False,
            issues=[],
            checked_libraries=[],
            skipped_libraries=skipped,
            confidence="high",
            summary=f"No high-risk libraries found. Skipped: {', '.join(skipped[:10])}",
        )

    print(
        f"[System] Checking {len(high_risk_imports)} high-risk libraries: {', '.join(high_risk_imports.keys())}"
    )

    # Step 3-4: Search and analyze each library
    all_issues: List[ApiVersionIssue] = []
    cache_hits = 0

    for library, files in high_risk_imports.items():
        search_context = (
            _extract_ccxt_search_context(code_bundle, target_files=files)
            if library == "ccxt"
            else None
        )
        cache_key = _build_api_version_cache_key(library, search_context)
        # Check cache first
        cached = _get_cached_api_version(cache_key)
        if cached:
            cache_hits += 1
            search_result = cached
        else:
            # Search for latest version info
            search_result = _search_library_latest_version(
                library, search_context=search_context
            )
            if search_result.get("search_success"):
                _set_cached_api_version(cache_key, search_result)

        # Analyze code for deprecated APIs
        if (
            library == "ccxt"
            or search_result.get("deprecation_notices")
            or search_result.get("latest_version")
        ):
            if library == "ccxt":
                issues = _analyze_ccxt_api_usage(
                    code_bundle,
                    search_result,
                    target_files=files,
                )
            else:
                issues = _analyze_code_for_deprecated_apis(
                    library,
                    code_bundle,
                    search_result,
                    llm,
                    target_files=files,
                )

            # Attach file info to issues
            for issue in issues:
                if files and not issue.file:
                    issue.file = files[0]

            all_issues.extend(issues)

    # Filter issues by severity threshold
    severity_order = {"low": 0, "medium": 1, "high": 2}
    threshold = severity_order.get(API_VERSION_CHECK_SEVERITY_THRESHOLD, 1)
    filtered_issues = [
        issue
        for issue in all_issues
        if severity_order.get(issue.severity.lower(), 1) >= threshold
    ]

    summary = f"Checked {len(high_risk_imports)} libraries, found {len(filtered_issues)} issues."
    if cache_hits:
        summary += f" ({cache_hits} cache hits)"

    print(f"[System] API version check complete: {len(filtered_issues)} issue(s) found")

    return ApiVersionReport(
        needs_update=len(filtered_issues) > 0,
        issues=filtered_issues,
        checked_libraries=list(high_risk_imports.keys()),
        skipped_libraries=skipped,
        cache_hits=cache_hits,
        confidence="medium" if filtered_issues else "high",
        summary=summary,
    )


def inject_api_issues_into_review(
    review_report: "ReviewReport",
    api_report: "ApiVersionReport",
) -> "ReviewReport":
    """
    Merge API version issues into a ReviewReport.
    This allows the quality fix loop to address API version issues.
    """
    if not api_report or not api_report.issues:
        return review_report

    api_review_issues = _api_issues_to_review_issues(api_report.issues)

    # Combine with existing issues
    existing_issues = list(review_report.issues or [])
    combined_issues = existing_issues + api_review_issues

    # If we have API issues, the report should not pass
    passes = review_report.passes and len(api_review_issues) == 0

    return ReviewReport(
        passes=passes,
        summary=review_report.summary + f"\n\n[API Version Check] {api_report.summary}",
        issues=combined_issues,
    )


def _maybe_run_api_version_check(
    code_bundle: Optional["CodeBundle"],
    llm: Any,
    *,
    enabled: bool,
    run_snapshot: Optional["RunSnapshot"] = None,
) -> Optional["ApiVersionReport"]:
    if not enabled or not code_bundle:
        return None
    try:
        api_version_report = run_api_version_check(code_bundle, llm, enabled=True)
    except _OperationCancelledError:
        raise
    except Exception as api_check_err:
        print(f"[Warn] API version check failed: {api_check_err}")
        return None
    if run_snapshot is not None and api_version_report is not None:
        run_snapshot.outputs["api_version_check"] = {
            "needs_update": api_version_report.needs_update,
            "issue_count": len(api_version_report.issues or []),
            "checked_libraries": api_version_report.checked_libraries,
            "cache_hits": api_version_report.cache_hits,
        }
    if api_version_report and api_version_report.needs_update:
        print(
            f"[API Version Check] Found {len(api_version_report.issues)} issue(s) "
            f"in libraries: {', '.join(api_version_report.checked_libraries)}"
        )
    return api_version_report


# BEGIN MANUAL OUTPUT SAVE OVERRIDES
# _env_int returns None when the env var is set to "none"/"unlimited"; guard
# every max() call so we never hit TypeError: '>' not supported between NoneType
# and int at import time.
_raw_ctx_max = _env_int("CODEGEN_CONTEXT_MAX_CHARS", 14000)
_raw_ctx_roles = _env_int("CODEGEN_CONTEXT_ANALYST_ROLES", 4)
_raw_ctx_finding = _env_int("CODEGEN_CONTEXT_ANALYST_FINDING_MAX_CHARS", 700)
_raw_ctx_gate = _env_int("CODEGEN_CONTEXT_GATE_JSON_MAX_CHARS", 2200)
CODEGEN_CONTEXT_MAX_CHARS = max(4000, _raw_ctx_max if _raw_ctx_max is not None else 14000)
CODEGEN_CONTEXT_ANALYST_ROLES = max(1, _raw_ctx_roles if _raw_ctx_roles is not None else 4)
CODEGEN_CONTEXT_ANALYST_FINDING_MAX_CHARS = max(
    300, _raw_ctx_finding if _raw_ctx_finding is not None else 700
)
CODEGEN_CONTEXT_GATE_JSON_MAX_CHARS = max(
    800, _raw_ctx_gate if _raw_ctx_gate is not None else 2200
)


def _dedupe_nonempty_codegen_items(
    items: Optional[List[str]],
    *,
    limit: int,
    max_chars: int,
) -> List[str]:
    seen: Set[str] = set()
    normalized: List[str] = []
    for item in list(items or []):
        value = str(item or "").strip()
        if not value:
            continue
        fingerprint = value.lower()
        if fingerprint in seen:
            continue
        seen.add(fingerprint)
        normalized.append(limit_text(value, max_chars))
        if len(normalized) >= limit:
            break
    return normalized


def _limit_codegen_text_exact(text: str, max_chars: int) -> str:
    value = str(text or "")
    if max_chars <= 0 or len(value) <= max_chars:
        return value[:max_chars] if max_chars > 0 else ""
    suffix = "\n...[truncated]..."
    if max_chars <= len(suffix):
        return suffix[:max_chars]
    return value[: max_chars - len(suffix)] + suffix


def build_budgeted_codegen_context(
    gate_decision: Optional[GateDecision],
    analysis_report: Optional[AnalysisReport],
    *,
    max_chars: Optional[int] = None,
    include_analyst_findings: bool = True,
) -> str:
    budget = CODEGEN_CONTEXT_MAX_CHARS if max_chars is None else max(1200, int(max_chars))
    parts: List[str] = []

    if analysis_report is not None:
        parts.append("=== APPROVED ANALYSIS HANDOFF ===")
        if getattr(analysis_report, "project_name", ""):
            parts.append(
                "Project name: "
                + limit_text(str(analysis_report.project_name), ANALYSIS_HANDOFF_ITEM_MAX_CHARS)
            )
        parts.append(
            "Summary: "
            + limit_text(str(getattr(analysis_report, "summary", "") or ""), 900)
        )
        parts.append(
            "Consensus: "
            + limit_text(str(getattr(analysis_report, "consensus", "") or ""), 1200)
        )
        parts.append(
            "Disagreement: "
            + limit_text(str(getattr(analysis_report, "disagreement", "") or ""), 1200)
        )
        score = getattr(analysis_report, "score", None)
        if score is not None:
            parts.append(f"Score: {score}/100")
        mode_used = str(getattr(analysis_report, "mode_used", "") or "").strip()
        if mode_used:
            parts.append(f"Mode: {mode_used}")
        risk_level = str(getattr(analysis_report, "risk_level", "") or "").strip()
        if risk_level:
            parts.append(f"Risk level: {risk_level}")

        experiment_lines = []
        for exp in list(getattr(analysis_report, "experiments", []) or [])[:4]:
            goal = limit_text(str(getattr(exp, "goal", "") or ""), ANALYSIS_HANDOFF_ITEM_MAX_CHARS)
            criteria = limit_text(
                str(getattr(exp, "criteria", "") or ""), ANALYSIS_HANDOFF_ITEM_MAX_CHARS
            )
            if goal or criteria:
                experiment_lines.append(f"- {goal} | {criteria}".strip())
        if experiment_lines:
            parts.append("\nExperiments:")
            parts.extend(experiment_lines)

        handoff_summary = str(getattr(analysis_report, "codegen_handoff_summary", "") or "").strip()
        if handoff_summary:
            parts.append("\nImplementation summary:")
            parts.append(limit_text(handoff_summary, 1800))

        requirements = _dedupe_nonempty_codegen_items(
            list(getattr(analysis_report, "codegen_requirements", []) or []),
            limit=8,
            max_chars=ANALYSIS_HANDOFF_ITEM_MAX_CHARS,
        )
        if requirements:
            parts.append("\nRequired implementation details:")
            parts.extend(f"- {item}" for item in requirements)

        constraints = _dedupe_nonempty_codegen_items(
            list(getattr(analysis_report, "codegen_constraints", []) or []),
            limit=8,
            max_chars=ANALYSIS_HANDOFF_ITEM_MAX_CHARS,
        )
        if constraints:
            parts.append("\nImplementation constraints:")
            parts.extend(f"- {item}" for item in constraints)

        validation_focus = _dedupe_nonempty_codegen_items(
            list(getattr(analysis_report, "codegen_validation_focus", []) or []),
            limit=6,
            max_chars=ANALYSIS_HANDOFF_ITEM_MAX_CHARS,
        )
        if validation_focus:
            parts.append("\nValidation focus:")
            parts.extend(f"- {item}" for item in validation_focus)

        gate_snapshot = getattr(analysis_report, "gate_context_snapshot", None)
        if gate_snapshot:
            parts.append("\nPreserved gate snapshot:")
            parts.append(
                limit_text(
                    json.dumps(gate_snapshot, ensure_ascii=False, indent=2, sort_keys=True),
                    CODEGEN_CONTEXT_GATE_JSON_MAX_CHARS,
                )
            )

        if include_analyst_findings and getattr(analysis_report, "analyst_findings", None):
            findings: Dict[str, str] = dict(getattr(analysis_report, "analyst_findings", {}) or {})
            role_order = list(ANALYST_AGENT_ORDER) + ["gate_controller", "format_checker"]
            selected_roles: List[str] = []
            for role_name in role_order + sorted(findings):
                if role_name in selected_roles or role_name not in findings:
                    continue
                selected_roles.append(role_name)
                if len(selected_roles) >= CODEGEN_CONTEXT_ANALYST_ROLES:
                    break
            if selected_roles:
                parts.append("\nAnalyst implementation notes:")
                for role_name in selected_roles:
                    parts.append(f"[{role_name}]")
                    parts.append(
                        limit_text(
                            str(findings.get(role_name, "") or ""),
                            CODEGEN_CONTEXT_ANALYST_FINDING_MAX_CHARS,
                        )
                    )

    validation_snapshot: Optional[Dict[str, Any]] = None
    if gate_decision is not None:
        gate_snapshot = _gate_decision_snapshot(gate_decision)
        parts.append("\n=== LIVE GATE CONTROLLER APPROVAL ===")
        parts.append(
            limit_text(
                json.dumps(gate_snapshot, ensure_ascii=False, indent=2, sort_keys=True),
                CODEGEN_CONTEXT_GATE_JSON_MAX_CHARS,
            )
        )
        if _gate_is_validation_scope(gate_decision):
            validation_snapshot = gate_snapshot
    elif analysis_report is not None and getattr(analysis_report, "gate_context_snapshot", None):
        gate_snapshot = dict(getattr(analysis_report, "gate_context_snapshot", {}) or {})
        if str(gate_snapshot.get("codegen_scope", "") or "").strip().lower() == "validation":
            validation_snapshot = gate_snapshot

    if validation_snapshot:
        parts.append("\n=== VALIDATION-FIRST CODEGEN APPROVAL ===")
        parts.append("Approved scope: validation")
        reason = str(validation_snapshot.get("validation_scope_reason", "") or "").strip()
        if reason:
            parts.append("Reason: " + limit_text(reason, 1200))
        objectives = _dedupe_nonempty_codegen_items(
            list(validation_snapshot.get("validation_objectives", []) or []),
            limit=6,
            max_chars=ANALYSIS_HANDOFF_ITEM_MAX_CHARS,
        )
        if objectives:
            parts.append("Validation objectives:")
            parts.extend(f"- {item}" for item in objectives)
        parts.append("Guardrails:")
        parts.extend(
            f"- {limit_text(item, ANALYSIS_HANDOFF_ITEM_MAX_CHARS)}"
            for item in _validation_scope_guardrails()
        )

    if not parts:
        parts.append("=== APPROVED ANALYSIS HANDOFF ===")
        parts.append("No approved analysis context was available.")

    return _limit_codegen_text_exact("\n".join(parts), budget)
# END MANUAL OUTPUT SAVE OVERRIDES


# =========================
# 6) Main Execution
# =========================
