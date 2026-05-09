"""
features/mode_validation_matrix.py
====================================
v1.0.5 round 3 (final) — single source of truth for which validation defences
each pipeline mode runs.

Round 1 + 2 layered four defences on top of the Quant track (import smoke,
cross-reference, domain lint, synthetic + live_trader smoke). The other modes
(SaaS / Agent / Scientist) inherited only the legacy web-only validation,
producing a hidden mode-specific debt that was easy to forget.

This module makes the situation explicit. ``MODE_VALIDATION_MATRIX`` declares,
for every supported mode, every defence layer with its current status (active /
opt-in / n/a / deferred) and the rule IDs it can emit. ``ARCHITECTURE.md``
embeds the rendered table so that "what each mode actually checks" is a
visible engineering invariant, not folklore.

The module ALSO contains the lightweight mode-specific AST lint helpers so
``section_06_runtime_quality_api`` can wire them in without circular imports:

- ``analyse_saas_lint_from_files``     — H001 missing requirements declaration
- ``analyse_agent_lint_from_files``    — A001 Agent missing role/goal/backstory,
                                         A002 Tool missing description
- ``analyse_scientist_lint_from_files``— S001 numeric work missing explicit
                                         seed, S002 missing requirements.txt

These are intentionally small (one rule per layer) — they exist so the matrix
is honest about what runs, not to deliver deep mode-specific validation in a
single release. Future rounds extend each mode's rule set.

Public API::

    from crucible.features.mode_validation_matrix import (
        MODE_VALIDATION_MATRIX,
        get_mode_defences,
        mode_validation_summary_markdown,
        analyse_saas_lint_from_files,
        analyse_agent_lint_from_files,
        analyse_scientist_lint_from_files,
    )
"""
from __future__ import annotations

import ast
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple


__all__ = [
    "ModeDefence",
    "ModeMatrix",
    "ModeLintIssue",
    "MODE_VALIDATION_MATRIX",
    "get_mode_defences",
    "mode_validation_summary_markdown",
    "analyse_saas_lint_from_files",
    "analyse_agent_lint_from_files",
    "analyse_scientist_lint_from_files",
]


# ─── Matrix data ─────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ModeDefence:
    """One validation layer for one mode.

    status:
      - ``"active"``   — runs by default in section_06
      - ``"opt-in"``   — runs only when an env var or scope is set
      - ``"n/a"``      — does not apply by design
      - ``"deferred"`` — known gap; tracked here to keep the debt visible
    """

    name: str
    status: str
    rule_ids: Tuple[str, ...]
    notes: str = ""


@dataclass(frozen=True)
class ModeMatrix:
    mode: str
    defences: Tuple[ModeDefence, ...]


_QUANT_DEFENCES: Tuple[ModeDefence, ...] = (
    ModeDefence(
        name="import_smoke",
        status="active",
        rule_ids=("Q010", "Q011"),
        notes="Subprocess import of every Quant entrypoint; surfaces import-time errors.",
    ),
    ModeDefence(
        name="cross_reference",
        status="active",
        rule_ids=("X001", "X002", "X003", "X004", "W001", "W002", "W003"),
        notes="AST cross-file consistency: dataclass kwargs, config attrs, missing imports, positional types, escape paths.",
    ),
    ModeDefence(
        name="domain_lint",
        status="active",
        rule_ids=("Q001", "Q002", "Q003", "Q004"),
        notes="Lookahead bias (4 escape paths), off-by-one stop window, Trade(spread=0), fixed slippage with dynamic flag.",
    ),
    ModeDefence(
        name="synthetic_dryrun",
        status="active",
        rule_ids=("Q012", "Q013", "Q014", "Q015"),
        notes="GBM OHLCV subprocess run of backtest entrypoint; opt-in dirty-data fixture via env var.",
    ),
    ModeDefence(
        name="live_trader_smoke",
        status="active",
        rule_ids=("Q020", "Q021", "Q022", "Q023", "Q024"),
        notes="ccxt-stubbed import + behavioural SL assertion (40% drawdown ramp).",
    ),
    ModeDefence(
        name="production_tests",
        status="opt-in",
        rule_ids=("X005",),
        notes="Enforces tests/*.py when CRUCIBLE_QUANT_REQUIRE_TESTS=1 or codegen_scope='production'.",
    ),
)


_SAAS_DEFENCES: Tuple[ModeDefence, ...] = (
    ModeDefence(
        name="web_smoke",
        status="active",
        rule_ids=("HTTP-smoke",),
        notes="Existing ASGI/WSGI app start + GET / probe (legacy section_06 path).",
    ),
    ModeDefence(
        name="cross_reference",
        status="active",
        rule_ids=("X001", "X002", "X003", "X004", "W001", "W002", "W003"),
        notes="v1.0.5 round 3 final: now runs on all non-Quant modes too.",
    ),
    ModeDefence(
        name="mode_specific_lint",
        status="active",
        rule_ids=("H001",),
        notes="Web framework imported but missing from requirements.txt declaration.",
    ),
    ModeDefence(
        name="dependency_audit",
        status="opt-in",
        rule_ids=("dep-audit",),
        notes="pip-audit via --dependency-audit flag.",
    ),
    ModeDefence(
        name="openapi_consistency",
        status="deferred",
        rule_ids=(),
        notes="OpenAPI spec ↔ route handler consistency — planned for v1.0.6.",
    ),
)


_AGENT_DEFENCES: Tuple[ModeDefence, ...] = (
    ModeDefence(
        name="cross_reference",
        status="active",
        rule_ids=("X001", "X002", "X003", "X004", "W001", "W002", "W003"),
        notes="v1.0.5 round 3 final.",
    ),
    ModeDefence(
        name="mode_specific_lint",
        status="active",
        rule_ids=("A001", "A002"),
        notes="Agent(...) missing role/goal/backstory; Tool/BaseTool missing description.",
    ),
    ModeDefence(
        name="tool_use_smoke",
        status="deferred",
        rule_ids=(),
        notes="Stubbed tool-use round-trip — planned for v1.0.6.",
    ),
)


_SCIENTIST_DEFENCES: Tuple[ModeDefence, ...] = (
    ModeDefence(
        name="cross_reference",
        status="active",
        rule_ids=("X001", "X002", "X003", "X004", "W001", "W002", "W003"),
        notes="v1.0.5 round 3 final.",
    ),
    ModeDefence(
        name="mode_specific_lint",
        status="active",
        rule_ids=("S001", "S002"),
        notes="Numerical work without explicit seed/RandomState; missing requirements.txt.",
    ),
    ModeDefence(
        name="data_leakage_check",
        status="deferred",
        rule_ids=(),
        notes="Train/test split leakage detection — planned for v1.0.6.",
    ),
)


MODE_VALIDATION_MATRIX: Dict[str, ModeMatrix] = {
    "quant": ModeMatrix("quant", _QUANT_DEFENCES),
    "saas": ModeMatrix("saas", _SAAS_DEFENCES),
    "agent": ModeMatrix("agent", _AGENT_DEFENCES),
    "scientist": ModeMatrix("scientist", _SCIENTIST_DEFENCES),
}


def get_mode_defences(mode: Optional[str]) -> Tuple[ModeDefence, ...]:
    """Return the defence tuple for *mode*, or empty when unknown."""
    key = (mode or "").strip().lower()
    matrix = MODE_VALIDATION_MATRIX.get(key)
    return tuple(matrix.defences) if matrix is not None else ()


def mode_validation_summary_markdown() -> str:
    """Render the matrix as a markdown table for ARCHITECTURE.md inclusion."""
    lines: List[str] = [
        "| Mode | Defence | Status | Rules | Notes |",
        "|------|---------|--------|-------|-------|",
    ]
    for mode in ("quant", "saas", "agent", "scientist"):
        matrix = MODE_VALIDATION_MATRIX[mode]
        for d in matrix.defences:
            rules = ", ".join(d.rule_ids) if d.rule_ids else "—"
            lines.append(
                f"| `{mode}` | {d.name} | {d.status} | {rules} | {d.notes} |"
            )
    return "\n".join(lines)


# ─── Mode-specific lint helpers ──────────────────────────────────────────────


@dataclass
class ModeLintIssue:
    severity: str
    category: str
    description: str
    file: Optional[str]
    line: Optional[int] = None
    suggestion: Optional[str] = None
    rule: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "severity": self.severity,
            "category": self.category,
            "description": self.description,
            "file": self.file,
            "line": self.line,
            "suggestion": self.suggestion,
            "rule": self.rule,
        }


_WEB_FRAMEWORK_PACKAGES: Tuple[str, ...] = (
    "fastapi", "flask", "starlette", "aiohttp", "django", "sanic", "tornado",
    "bottle", "falcon", "quart",
)


def _looks_like_requirements_declaration(content: str, *, package: str) -> bool:
    """Cheap multi-format check: is *package* mentioned in a requirements-style
    file (requirements.txt / pyproject.toml / setup.py / Pipfile)?

    Three patterns recognised:
      1. requirements.txt — bare ``fastapi``, ``fastapi==0.110``, ``fastapi[all]>=0.100``
         at line head.
      2. PEP 621 pyproject.toml — quoted ``"fastapi"`` / ``'fastapi==0.110'`` inside
         a ``dependencies = [...]`` list.
      3. Pipfile / Poetry — ``fastapi = "*"`` / ``fastapi = "^0.110"``.

    Substring fallback (last resort): the bare lowercase package name appears
    anywhere in the requirements text. Conservative — but cheaper than
    parsing TOML and false-negatives are worse here than false-positives.
    """
    if not content:
        return False
    pkg = package.lower()
    text_lower = content.lower()

    # Pattern 1: requirements.txt line-head.
    for line in content.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        head = (
            stripped.split(";")[0].split("[")[0].split("=")[0]
            .split("<")[0].split(">")[0].split("~")[0]
        )
        head = head.strip().lower()
        if head == pkg:
            return True

    # Pattern 2: pyproject.toml dependency list — quoted string starting with pkg.
    import re as _re
    quoted = _re.compile(
        r"""['"]\s*""" + _re.escape(pkg) + r"""\s*(?:[\[<>=!~]|['"])""",
        _re.IGNORECASE,
    )
    if quoted.search(content):
        return True

    # Pattern 3: Pipfile / Poetry — `pkg = "..."`.
    poetry = _re.compile(
        r"^\s*" + _re.escape(pkg) + r"\s*=\s*['\"]",
        _re.IGNORECASE | _re.MULTILINE,
    )
    if poetry.search(content):
        return True

    # Final fallback: bare token surrounded by non-alphanum chars. Catches
    # `[fastapi]` extras, `optional-dependencies.web = ['fastapi']` etc.
    bare = _re.compile(r"(?<![a-z0-9_-])" + _re.escape(pkg) + r"(?![a-z0-9_-])")
    return bare.search(text_lower) is not None


def analyse_saas_lint_from_files(
    files: List[Tuple[str, str]],
) -> List[ModeLintIssue]:
    """v1.0.5 round 3 (final) H001: web framework imported but absent from
    requirements.txt / pyproject.toml.

    Hits the most common deploy-time failure for SaaS bundles: the LLM
    happily ``import fastapi`` everywhere but never declares it as a
    runtime dependency, so the container builds green and crashes on first
    request.
    """
    issues: List[ModeLintIssue] = []
    py_files = [(p, c) for p, c in files if p.endswith(".py")]
    req_files_text = "\n".join(
        c for p, c in files
        if p.endswith(("requirements.txt", "pyproject.toml", "setup.py", "setup.cfg", "Pipfile"))
    )
    imported_frameworks: Dict[str, Tuple[str, int]] = {}
    for path, source in py_files:
        try:
            tree = ast.parse(source)
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    root = alias.name.split(".")[0].lower()
                    if root in _WEB_FRAMEWORK_PACKAGES and root not in imported_frameworks:
                        imported_frameworks[root] = (path, node.lineno)
            elif isinstance(node, ast.ImportFrom) and node.module:
                root = node.module.split(".")[0].lower()
                if root in _WEB_FRAMEWORK_PACKAGES and root not in imported_frameworks:
                    imported_frameworks[root] = (path, node.lineno)
    for pkg, (path, lineno) in sorted(imported_frameworks.items()):
        if not _looks_like_requirements_declaration(req_files_text, package=pkg):
            issues.append(
                ModeLintIssue(
                    severity="high",
                    category="bug",
                    description=(
                        f"`{pkg}` is imported by the bundle but is not declared in "
                        "any requirements file (requirements.txt / pyproject.toml / "
                        "setup.py / Pipfile). The container build will succeed; the "
                        "app will crash on first request with ModuleNotFoundError."
                    ),
                    file=path,
                    line=lineno,
                    suggestion=(
                        f"Add `{pkg}` (with a version pin) to requirements.txt "
                        "or pyproject.toml's [project.dependencies]."
                    ),
                    rule="H001-web-framework-undeclared",
                )
            )
    return issues


_AGENT_CLASS_NAMES: Tuple[str, ...] = (
    "Agent", "CrewAgent", "BaseAgent", "AssistantAgent", "ReActAgent",
)
_TOOL_CLASS_NAMES: Tuple[str, ...] = ("Tool", "BaseTool", "FunctionTool", "StructuredTool")
_REQUIRED_AGENT_KW: Tuple[str, ...] = ("role", "goal", "backstory")


def analyse_agent_lint_from_files(
    files: List[Tuple[str, str]],
) -> List[ModeLintIssue]:
    """v1.0.5 round 3 (final):

    - A001-agent-missing-required-kwargs: ``Agent(role=..., goal=..., backstory=...)``
      / ``CrewAgent(...)`` instantiation missing one of the required kwargs.
      LLM-generated bundles routinely create agents with only ``role=`` set,
      producing CrewAI runtime errors that would pass syntax check.
    - A002-tool-missing-description: ``Tool(...)`` / ``BaseTool`` subclass with
      no ``description`` field — agents cannot route correctly without one.
    """
    issues: List[ModeLintIssue] = []
    for path, source in files:
        if not path.endswith(".py"):
            continue
        try:
            tree = ast.parse(source)
        except SyntaxError:
            continue

        for node in ast.walk(tree):
            # A001: Agent(...) instantiation missing role/goal/backstory.
            if isinstance(node, ast.Call):
                callee = node.func
                callee_name: Optional[str] = None
                if isinstance(callee, ast.Name):
                    callee_name = callee.id
                elif isinstance(callee, ast.Attribute):
                    callee_name = callee.attr
                if callee_name in _AGENT_CLASS_NAMES:
                    declared_kw = {kw.arg for kw in node.keywords if kw.arg}
                    missing = [k for k in _REQUIRED_AGENT_KW if k not in declared_kw]
                    has_kwargs_unpack = any(kw.arg is None for kw in node.keywords)
                    if missing and not has_kwargs_unpack:
                        issues.append(
                            ModeLintIssue(
                                severity="medium",
                                category="bug",
                                description=(
                                    f"{callee_name}(...) is missing required kwargs: "
                                    f"{missing}. CrewAI / LangChain agents need these to "
                                    "produce useful tool-routing prompts."
                                ),
                                file=path,
                                line=node.lineno,
                                suggestion=(
                                    f"Pass explicit values for {missing} when "
                                    f"constructing {callee_name}."
                                ),
                                rule="A001-agent-missing-required-kwargs",
                            )
                        )

            # A002: Tool subclass without `description = "..."`.
            if isinstance(node, ast.ClassDef):
                base_names: List[str] = []
                for base in node.bases or []:
                    if isinstance(base, ast.Name):
                        base_names.append(base.id)
                    elif isinstance(base, ast.Attribute):
                        base_names.append(base.attr)
                if any(b in _TOOL_CLASS_NAMES for b in base_names):
                    has_description = False
                    for stmt in node.body:
                        if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
                            if stmt.target.id == "description":
                                has_description = True
                                break
                        if isinstance(stmt, ast.Assign):
                            for target in stmt.targets:
                                if isinstance(target, ast.Name) and target.id == "description":
                                    has_description = True
                                    break
                            if has_description:
                                break
                    if not has_description:
                        issues.append(
                            ModeLintIssue(
                                severity="medium",
                                category="bug",
                                description=(
                                    f"Tool subclass {node.name} has no `description` "
                                    "field. Agents cannot route to it without a "
                                    "description string."
                                ),
                                file=path,
                                line=node.lineno,
                                suggestion=(
                                    f"Add `description: str = \"...\"` to {node.name}."
                                ),
                                rule="A002-tool-missing-description",
                            )
                        )
    return issues


_NUMERIC_FUNCS_NEEDING_SEED: Tuple[str, ...] = (
    "RandomForestClassifier", "RandomForestRegressor", "GradientBoostingClassifier",
    "GradientBoostingRegressor", "XGBClassifier", "XGBRegressor",
    "train_test_split", "KFold", "StratifiedKFold", "cross_val_score",
    "shuffle", "permutation",
)


def analyse_scientist_lint_from_files(
    files: List[Tuple[str, str]],
) -> List[ModeLintIssue]:
    """v1.0.5 round 3 (final):

    - S001-numeric-without-seed: a stochastic ML / numeric call (RandomForest,
      train_test_split, KFold, ...) without an explicit ``random_state=`` or
      ``seed=`` kwarg. Reproducibility-killer in scientific bundles.
    - S002-missing-requirements: bundle has Python files but no requirements
      manifest of any kind (requirements.txt / pyproject.toml / setup.py /
      Pipfile / environment.yml).
    """
    issues: List[ModeLintIssue] = []
    py_files = [(p, c) for p, c in files if p.endswith(".py")]
    has_requirements = any(
        p.endswith(("requirements.txt", "pyproject.toml", "setup.py", "setup.cfg",
                    "Pipfile", "environment.yml", "environment.yaml"))
        for p, _ in files
    )

    for path, source in py_files:
        try:
            tree = ast.parse(source)
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            callee = node.func
            callee_name: Optional[str] = None
            if isinstance(callee, ast.Name):
                callee_name = callee.id
            elif isinstance(callee, ast.Attribute):
                callee_name = callee.attr
            if callee_name not in _NUMERIC_FUNCS_NEEDING_SEED:
                continue
            kw_names = {kw.arg for kw in node.keywords if kw.arg}
            if any(k in kw_names for k in ("random_state", "seed", "rng", "generator")):
                continue
            if any(kw.arg is None for kw in node.keywords):
                # **kwargs unpack — can't be sure.
                continue
            issues.append(
                ModeLintIssue(
                    severity="medium",
                    category="bug",
                    description=(
                        f"`{callee_name}(...)` called without a `random_state=`, "
                        "`seed=`, or `rng=` kwarg. Results are not reproducible — "
                        "same inputs can produce different outputs across runs."
                    ),
                    file=path,
                    line=node.lineno,
                    suggestion=(
                        f"Pass an explicit seed: `{callee_name}(..., random_state=42)`."
                    ),
                    rule="S001-numeric-without-seed",
                )
            )

    if py_files and not has_requirements:
        issues.append(
            ModeLintIssue(
                severity="medium",
                category="bug",
                description=(
                    "Scientific bundle has no requirements manifest (no "
                    "requirements.txt / pyproject.toml / setup.py / Pipfile / "
                    "environment.yml). The exact dependency versions used to "
                    "produce the results cannot be reconstructed by a reviewer."
                ),
                file=None,
                line=None,
                suggestion=(
                    "Emit a requirements.txt with every imported third-party "
                    "package pinned to the version used during the run."
                ),
                rule="S002-missing-requirements",
            )
        )

    return issues
