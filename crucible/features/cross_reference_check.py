"""
features/cross_reference_check.py
==================================
Static cross-file consistency checks for generated multi-file Python projects.

This module exists because Python's ``py_compile`` syntax check passes for code
that will still crash on import or first call — the kinds of mistakes LLM
codegen routinely produces when stitching together multiple files:

1. **dataclass-kwargs-mismatch** — ``Trade(side="long", quantity=10)`` while
   the ``Trade`` dataclass in ``trade.py`` only declares ``symbol, price, size``.
   ``py_compile`` happily accepts the call; ``__init__`` raises
   ``TypeError: unexpected keyword argument 'side'`` at first execution.
2. **config-attr-missing** — ``config.POSITION_SIZE_QUOTE`` when
   ``Config`` (or the ``config`` module) has no such attribute. ``AttributeError``
   on first access.
3. **cross-file-import-missing** — ``from data_provider import prepare_data``
   when ``data_provider.py`` exposes ``prepare`` but not ``prepare_data``.
   ``ImportError`` on import.
4. **positional-arg-type-mismatch** — ``fetch_historical_data(symbol, "3mo", "binance")``
   when the target signature is ``fetch_historical_data(symbol: str, days: int = 90,
   source: str = "yfinance")``: the string literal is passed where an int is
   required. Often slips past static type-checkers because the caller has no
   annotations of its own.

Every check is purely AST-based — we never import or exec user code, so this
is safe to run on any bundle that ``compile()`` accepts.

Public entry point::

    from crucible.features.cross_reference_check import analyse_cross_references
    report = analyse_cross_references(code_dir)
    for issue in report.issues:
        print(issue.severity, issue.file, issue.line, issue.description)

The report mirrors ``ReviewIssue`` shape (``severity``/``category``/``description``/
``file``/``suggestion``) so callers can ``inject_issues_into_review`` without
adapter code.
"""
from __future__ import annotations

import ast
import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set, Tuple


__all__ = [
    "CrossReferenceIssue",
    "CrossReferenceReport",
    "analyse_cross_references",
    "analyse_cross_references_from_files",
]


# ─── Dataclasses ───────────────────────────────────────────────────────────────


@dataclass
class CrossReferenceIssue:
    """A single cross-reference inconsistency."""

    severity: str  # "high" | "medium" | "low"
    category: str  # "bug" | "import" | "type"
    description: str
    file: Optional[str]
    line: Optional[int] = None
    suggestion: Optional[str] = None
    rule: str = ""  # rule id, e.g. "X001-dataclass-kwargs"

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


@dataclass
class CrossReferenceReport:
    """Aggregate report across all files."""

    passes: bool = True
    issues: List[CrossReferenceIssue] = field(default_factory=list)
    files_scanned: int = 0
    errors: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "passes": self.passes,
            "files_scanned": self.files_scanned,
            "issues": [i.to_dict() for i in self.issues],
            "errors": self.errors,
        }


# ─── Internal models ───────────────────────────────────────────────────────────


@dataclass
class _ClassSignature:
    """Captured init signature for a user-defined class or dataclass."""

    name: str
    file: str
    line: int
    is_dataclass: bool
    accepts_kwargs: bool  # True if __init__ has **kwargs
    accepts_var_args: bool  # True if __init__ has *args (positional rest)
    init_param_names: Set[str] = field(default_factory=set)
    init_required_names: Set[str] = field(default_factory=set)
    instance_attr_names: Set[str] = field(default_factory=set)


@dataclass
class _FunctionSignature:
    """Captured signature for a top-level function."""

    name: str
    file: str
    line: int
    positional_names: List[str] = field(default_factory=list)
    positional_annotations: List[Optional[str]] = field(default_factory=list)
    positional_default_kinds: List[Optional[str]] = field(default_factory=list)
    has_var_positional: bool = False  # *args
    has_var_keyword: bool = False  # **kwargs
    keyword_only_names: Set[str] = field(default_factory=set)


@dataclass
class _ModuleInfo:
    """Top-level names a module exposes."""

    name: str
    file: str
    classes: Dict[str, _ClassSignature] = field(default_factory=dict)
    functions: Dict[str, _FunctionSignature] = field(default_factory=dict)
    constants: Set[str] = field(default_factory=set)
    star_imports: bool = False  # at least one `from X import *` — be lenient


# ─── Internal helpers ──────────────────────────────────────────────────────────


_TYPING_INT_NAMES = {"int", "Integral"}
_TYPING_STR_NAMES = {"str", "bytes"}
_TYPING_FLOAT_NAMES = {"float", "Real"}
_TYPING_BOOL_NAMES = {"bool"}


def _annotation_root_name(node: Optional[ast.AST]) -> Optional[str]:
    """
    Return the outermost type name from an annotation AST.

    Examples:
        int                 -> "int"
        Optional[int]       -> "Optional"  (we treat Optional/Union/List as wrappers)
        List[str]           -> "List"
        str                 -> "str"
        "MyType"            -> "MyType"

    For Optional[int]/Union[int, None] we follow into the wrapped type.
    """
    if node is None:
        return None
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value.strip().split("[", 1)[0].split(".")[-1]
    if isinstance(node, ast.Subscript):
        outer = _annotation_root_name(node.value)
        if outer in ("Optional", "Union"):
            # Look inside to find a non-None type.
            slice_node = node.slice
            if isinstance(slice_node, ast.Tuple):
                for elt in slice_node.elts:
                    if (
                        isinstance(elt, ast.Constant) and elt.value is None
                    ) or (isinstance(elt, ast.Name) and elt.id == "None"):
                        continue
                    inner = _annotation_root_name(elt)
                    if inner:
                        return inner
            else:
                inner = _annotation_root_name(slice_node)
                if inner:
                    return inner
        return outer
    return None


def _classify_constant_kind(node: ast.AST) -> Optional[str]:
    """Classify a literal/default node into one of {'int','float','str','bool','bytes','none'}."""
    if isinstance(node, ast.Constant):
        v = node.value
        if isinstance(v, bool):
            return "bool"
        if isinstance(v, int):
            return "int"
        if isinstance(v, float):
            return "float"
        if isinstance(v, str):
            return "str"
        if isinstance(v, bytes):
            return "bytes"
        if v is None:
            return "none"
    if isinstance(node, ast.Tuple):
        return "tuple"
    if isinstance(node, ast.List):
        return "list"
    if isinstance(node, ast.Dict):
        return "dict"
    return None


def _is_dataclass(decorator_list: List[ast.AST]) -> bool:
    for deco in decorator_list:
        if isinstance(deco, ast.Name) and deco.id == "dataclass":
            return True
        if isinstance(deco, ast.Attribute) and deco.attr == "dataclass":
            return True
        if isinstance(deco, ast.Call):
            f = deco.func
            if isinstance(f, ast.Name) and f.id == "dataclass":
                return True
            if isinstance(f, ast.Attribute) and f.attr == "dataclass":
                return True
    return False


def _collect_self_attrs(class_body: List[ast.stmt]) -> Set[str]:
    """Find ``self.x = …`` assignments inside ``__init__``."""
    attrs: Set[str] = set()
    for node in class_body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if node.name != "__init__":
            continue
        for stmt in ast.walk(node):
            if isinstance(stmt, ast.Assign):
                for target in stmt.targets:
                    if (
                        isinstance(target, ast.Attribute)
                        and isinstance(target.value, ast.Name)
                        and target.value.id == "self"
                    ):
                        attrs.add(target.attr)
            elif isinstance(stmt, ast.AnnAssign):
                target = stmt.target
                if (
                    isinstance(target, ast.Attribute)
                    and isinstance(target.value, ast.Name)
                    and target.value.id == "self"
                ):
                    attrs.add(target.attr)
    return attrs


def _build_class_signature(node: ast.ClassDef, file: str) -> _ClassSignature:
    sig = _ClassSignature(
        name=node.name,
        file=file,
        line=node.lineno,
        is_dataclass=_is_dataclass(node.decorator_list),
        accepts_kwargs=False,
        accepts_var_args=False,
    )

    if sig.is_dataclass:
        # Collect AnnAssign top-level fields
        for stmt in node.body:
            if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
                fname = stmt.target.id
                sig.init_param_names.add(fname)
                sig.instance_attr_names.add(fname)
                if stmt.value is None:
                    # No default — required (unless field(default_factory=...) which
                    # would have value=Call). Treat None-value as required to be safe.
                    sig.init_required_names.add(fname)
        # A dataclass also accepts custom __init__ if user wrote one — fall through.

    # Class-body assignments (``ATTR = value``) are exposed as both class
    # attributes *and* — by virtue of the ``instance.X`` lookup chain —
    # instance attributes. Collect them so ``config.POSITION_SIZE`` does not
    # fire X002 just because the LLM put POSITION_SIZE on the class body
    # instead of inside ``__init__``.
    for stmt in node.body:
        if isinstance(stmt, ast.Assign):
            for target in stmt.targets:
                if isinstance(target, ast.Name):
                    sig.instance_attr_names.add(target.id)
        elif isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
            sig.instance_attr_names.add(stmt.target.id)

    # If __init__ exists, prefer its signature over dataclass-derived params
    init_method: Optional[ast.FunctionDef] = None
    for stmt in node.body:
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)) and stmt.name == "__init__":
            init_method = stmt  # type: ignore[assignment]
            break

    if init_method is not None:
        # Reset param-derived state — explicit __init__ wins.
        explicit_params: Set[str] = set()
        explicit_required: Set[str] = set()
        a = init_method.args
        # skip first 'self'
        positional = (a.posonlyargs or []) + (a.args or [])
        if positional and positional[0].arg in ("self", "cls"):
            positional = positional[1:]
        defaults = list(a.defaults or [])
        # Right-aligned: last len(defaults) positional names have defaults
        n_with_default = len(defaults)
        n_required = max(0, len(positional) - n_with_default)
        for idx, arg in enumerate(positional):
            explicit_params.add(arg.arg)
            if idx < n_required:
                explicit_required.add(arg.arg)
        for arg in a.kwonlyargs or []:
            explicit_params.add(arg.arg)
            # Required iff its slot in kw_defaults is None
        if a.kw_defaults:
            for arg, dval in zip(a.kwonlyargs or [], a.kw_defaults):
                if dval is None:
                    explicit_required.add(arg.arg)
        if a.vararg:
            sig.accepts_var_args = True
        if a.kwarg:
            sig.accepts_kwargs = True
        sig.init_param_names = explicit_params
        sig.init_required_names = explicit_required

    sig.instance_attr_names |= _collect_self_attrs(node.body)
    return sig


def _build_function_signature(node: ast.FunctionDef, file: str) -> _FunctionSignature:
    a = node.args
    fn = _FunctionSignature(name=node.name, file=file, line=node.lineno)
    positional = (a.posonlyargs or []) + (a.args or [])
    fn.positional_names = [p.arg for p in positional]
    fn.positional_annotations = [_annotation_root_name(p.annotation) for p in positional]
    # Defaults right-align onto the tail of `positional`.
    defaults = list(a.defaults or [])
    n_with_default = len(defaults)
    pad = len(positional) - n_with_default
    fn.positional_default_kinds = [None] * pad + [
        _classify_constant_kind(d) for d in defaults
    ]
    fn.has_var_positional = a.vararg is not None
    fn.has_var_keyword = a.kwarg is not None
    fn.keyword_only_names = {p.arg for p in (a.kwonlyargs or [])}
    return fn


def _module_name_from_path(path: str) -> str:
    """``trade.py`` -> ``trade``; ``foo/bar/baz.py`` -> ``baz``."""
    base = os.path.basename(path)
    if base.endswith(".py"):
        base = base[:-3]
    return base


def _scan_module(path: str, source: str) -> Tuple[Optional[_ModuleInfo], Optional[str]]:
    """Parse one file. Returns (info, error)."""
    try:
        tree = ast.parse(source, filename=path)
    except SyntaxError:
        return None, "syntax error"

    info = _ModuleInfo(name=_module_name_from_path(path), file=path)
    for node in tree.body:
        if isinstance(node, ast.ClassDef):
            info.classes[node.name] = _build_class_signature(node, path)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            try:
                info.functions[node.name] = _build_function_signature(
                    node,  # type: ignore[arg-type]
                    path,
                )
            except Exception:
                pass
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    info.constants.add(target.id)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            info.constants.add(node.target.id)
        elif isinstance(node, ast.ImportFrom):
            # `from foo import *` — be lenient on this module's name set.
            for alias in node.names or []:
                if alias.name == "*":
                    info.star_imports = True
        elif isinstance(node, ast.If):
            # Conditionally-defined names (e.g. try/except imports) — walk the body too.
            for sub in ast.walk(node):
                if isinstance(sub, ast.Assign):
                    for target in sub.targets:
                        if isinstance(target, ast.Name):
                            info.constants.add(target.id)
                elif isinstance(sub, ast.FunctionDef):
                    info.functions.setdefault(
                        sub.name, _build_function_signature(sub, path)
                    )
                elif isinstance(sub, ast.ClassDef):
                    info.classes.setdefault(sub.name, _build_class_signature(sub, path))
        elif isinstance(node, ast.Try):
            for sub in ast.walk(node):
                if isinstance(sub, ast.Assign):
                    for target in sub.targets:
                        if isinstance(target, ast.Name):
                            info.constants.add(target.id)

    return info, None


# ─── Per-file checker ──────────────────────────────────────────────────────────


class _PerFileChecker(ast.NodeVisitor):
    """
    Visit one file and emit issues against ``self.modules`` (the bundle registry).

    Tracks per-file alias state:
      - ``alias_to_module``  — ``import foo as bar`` / ``from . import foo``
      - ``alias_to_class``   — ``from trade import Trade`` (so we resolve
                               ``Trade(...)`` to the actual class definition)
      - ``alias_to_function``
      - ``alias_to_object``  — ``from cfg_module import config`` / star-imports

    Also captures locally-defined classes/functions inside the same file so we
    don't emit false positives for self-reference.
    """

    def __init__(
        self,
        file: str,
        modules: Dict[str, _ModuleInfo],
        local_module: _ModuleInfo,
    ) -> None:
        self.file = file
        self.modules = modules
        self.local_module = local_module
        self.issues: List[CrossReferenceIssue] = []
        # alias name -> module info (for ``import foo as bar`` etc.)
        self.alias_to_module: Dict[str, _ModuleInfo] = {}
        # alias name -> class signature (for ``from trade import Trade``)
        self.alias_to_class: Dict[str, _ClassSignature] = {}
        # alias name -> function signature
        self.alias_to_function: Dict[str, _FunctionSignature] = {}
        # alias name -> set of attribute names available (for star/object imports)
        self.aliased_object_attrs: Dict[str, Set[str]] = {}
        # Track local names so we can skip "undefined name" complaints for them
        self.local_names: Set[str] = set()
        for cls in local_module.classes:
            self.local_names.add(cls)
        for fn in local_module.functions:
            self.local_names.add(fn)
        self.local_names |= local_module.constants

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            target = alias.asname or alias.name.split(".")[0]
            mod = self.modules.get(alias.name) or self.modules.get(
                alias.name.split(".")[0]
            )
            if mod is not None:
                self.alias_to_module[target] = mod

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        if not node.module:
            return
        # Resolve relative imports relative to the file location.
        mod_name = node.module
        # Direct-bundle target lookup — both `from foo import X` and
        # `from package.foo import X` collapse to leaf module name.
        bundle_mod = self.modules.get(mod_name) or self.modules.get(
            mod_name.split(".")[-1]
        )
        if bundle_mod is None:
            # Not a bundle module — could be stdlib or external. Skip.
            return
        # Star-import: too lenient to track.
        if any(a.name == "*" for a in node.names or []):
            return
        for alias in node.names:
            sym = alias.name
            local = alias.asname or sym
            if sym in bundle_mod.classes:
                self.alias_to_class[local] = bundle_mod.classes[sym]
                self.local_names.add(local)
            elif sym in bundle_mod.functions:
                self.alias_to_function[local] = bundle_mod.functions[sym]
                self.local_names.add(local)
            elif sym in bundle_mod.constants:
                self.local_names.add(local)
            elif bundle_mod.star_imports:
                # Cannot resolve symbol; conservatively assume it exists.
                self.local_names.add(local)
            else:
                # Symbol not exposed by the bundle module — high-severity bug.
                self.issues.append(
                    CrossReferenceIssue(
                        severity="high",
                        category="import",
                        description=(
                            f"Import '{sym}' from '{mod_name}' but '{mod_name}' does not "
                            f"define a top-level name '{sym}'."
                        ),
                        file=os.path.basename(self.file),
                        line=node.lineno,
                        suggestion=(
                            f"Either add '{sym}' to {mod_name}.py at module level, "
                            f"or correct the import to use an existing name."
                        ),
                        rule="X003-cross-file-import-missing",
                    )
                )

    def visit_Call(self, node: ast.Call) -> None:
        # ── Class instantiation kwargs check ──────────────────────────────────
        cls_sig = self._resolve_class_call(node.func)
        if cls_sig is not None and not cls_sig.accepts_kwargs:
            for kw in node.keywords:
                if kw.arg is None:
                    # v1.0.5 round 3 (W001): **mapping unpack bypasses static
                    # signature check entirely. This is the LLM's #1 fix-loop
                    # shortcut when X001 fires — it reroutes the assignment
                    # through a dict and the typo flows through unchecked.
                    # Emit a medium-severity warning so reviewers know the
                    # call is unverifiable rather than silently accepting it.
                    valid = sorted(cls_sig.init_param_names) or ["(none)"]
                    self.issues.append(
                        CrossReferenceIssue(
                            severity="medium",
                            category="bug",
                            description=(
                                f"{cls_sig.name}(**...) — kwargs unpack bypasses "
                                "the static signature check. "
                                f"{cls_sig.name} (defined in "
                                f"{os.path.basename(cls_sig.file)}) declares: "
                                f"{valid}. If the dict comes from JSON, "
                                "external config, or LLM output, the unpack "
                                "can carry typo'd keys that only surface as "
                                "TypeError at runtime."
                            ),
                            file=os.path.basename(self.file),
                            line=getattr(kw, "lineno", node.lineno),
                            suggestion=(
                                "Either pass kwargs explicitly, or whitelist "
                                "the dict before unpacking: "
                                f"`valid_keys = {{{', '.join(repr(n) for n in valid[:6])}{', ...' if len(valid) > 6 else ''}}}; "
                                f"{cls_sig.name}(**{{k: v for k, v in d.items() "
                                "if k in valid_keys})`."
                            ),
                            rule="W001-kwargs-unpack-skipped-check",
                        )
                    )
                    continue
                if kw.arg not in cls_sig.init_param_names:
                    self.issues.append(
                        CrossReferenceIssue(
                            severity="high",
                            category="bug",
                            description=(
                                f"{cls_sig.name}({kw.arg}=...) — class {cls_sig.name} "
                                f"defined in {os.path.basename(cls_sig.file)} does not accept "
                                f"keyword argument '{kw.arg}'. Available: "
                                f"{sorted(cls_sig.init_param_names) or '(none)'}."
                            ),
                            file=os.path.basename(self.file),
                            line=getattr(kw, "lineno", node.lineno),
                            suggestion=(
                                f"Pass one of: {sorted(cls_sig.init_param_names)} — "
                                f"or add '{kw.arg}' to {cls_sig.name}'s definition."
                            ),
                            rule="X001-dataclass-kwargs-mismatch",
                        )
                    )

        # ── W002: getattr(module, "literal", ...) attribute warning ──────────
        # v1.0.5 round 3 (escape path): getattr(<module-alias>, "<literal>")
        # without a default arg is the dynamic equivalent of X002. We can't
        # be 100% sure (the third arg might be a default) but if no default
        # is supplied AND the literal isn't declared, the call WILL raise.
        if (
            isinstance(node.func, ast.Name)
            and node.func.id == "getattr"
            and len(node.args) >= 2
            and isinstance(node.args[0], ast.Name)
            and isinstance(node.args[1], ast.Constant)
            and isinstance(node.args[1].value, str)
        ):
            obj_name = node.args[0].id
            attr_name = node.args[1].value
            mod = self.alias_to_module.get(obj_name)
            if mod is not None:
                exposed = (
                    set(mod.classes) | set(mod.functions) | set(mod.constants)
                )
                config_cls = mod.classes.get("Config") or mod.classes.get(
                    obj_name.capitalize()
                )
                if config_cls is not None:
                    exposed |= config_cls.instance_attr_names
                    exposed |= config_cls.init_param_names
                has_default = len(node.args) >= 3
                if not mod.star_imports and attr_name not in exposed:
                    severity = "medium" if has_default else "high"
                    self.issues.append(
                        CrossReferenceIssue(
                            severity=severity,
                            category="bug",
                            description=(
                                f"getattr({obj_name}, {attr_name!r}"
                                + (", <default>" if has_default else "")
                                + f") — '{attr_name}' is not declared in "
                                f"{os.path.basename(mod.file)}. "
                                + (
                                    "A default is supplied so this is silent at runtime, but the "
                                    "intent is unclear."
                                    if has_default
                                    else "Without a default, this raises AttributeError at runtime."
                                )
                            ),
                            file=os.path.basename(self.file),
                            line=node.lineno,
                            suggestion=(
                                f"Either add '{attr_name}' to "
                                f"{os.path.basename(mod.file)} (or its Config class), "
                                "or document why dynamic access is intentional."
                            ),
                            rule="W002-getattr-dynamic-attr-unverifiable",
                        )
                    )

        # ── Function positional-type sniff ────────────────────────────────────
        fn_sig = self._resolve_function_call(node.func)
        if fn_sig is not None and not fn_sig.has_var_positional:
            for idx, pos_arg in enumerate(node.args):
                if idx >= len(fn_sig.positional_names):
                    break
                kind = _classify_constant_kind(pos_arg)
                if kind is None:
                    continue  # not a literal; skip
                ann = fn_sig.positional_annotations[idx] if idx < len(fn_sig.positional_annotations) else None
                default_kind = (
                    fn_sig.positional_default_kinds[idx]
                    if idx < len(fn_sig.positional_default_kinds)
                    else None
                )
                expected_kind: Optional[str] = None
                if ann in _TYPING_INT_NAMES:
                    expected_kind = "int"
                elif ann in _TYPING_STR_NAMES:
                    expected_kind = "str"
                elif ann in _TYPING_FLOAT_NAMES:
                    expected_kind = "float"
                elif ann in _TYPING_BOOL_NAMES:
                    expected_kind = "bool"
                elif default_kind in ("int", "str", "float", "bool"):
                    expected_kind = default_kind
                if expected_kind is None:
                    continue
                if kind == expected_kind:
                    continue
                # int and float are compatible enough to ignore.
                if {kind, expected_kind} <= {"int", "float"}:
                    continue
                # bool literal → int annotation is also legal in Python.
                if kind == "bool" and expected_kind == "int":
                    continue
                self.issues.append(
                    CrossReferenceIssue(
                        severity="high",
                        category="type",
                        description=(
                            f"{fn_sig.name}() positional arg #{idx + 1} "
                            f"('{fn_sig.positional_names[idx]}') expects {expected_kind} but a "
                            f"{kind} literal was passed."
                        ),
                        file=os.path.basename(self.file),
                        line=getattr(pos_arg, "lineno", node.lineno),
                        suggestion=(
                            f"Pass a {expected_kind} value — convert the literal "
                            f"({ast.unparse(pos_arg) if hasattr(ast, 'unparse') else '...'}) or fix the function signature."
                        ),
                        rule="X004-positional-arg-type-mismatch",
                    )
                )

        self.generic_visit(node)

    def visit_Attribute(self, node: ast.Attribute) -> None:
        # ── config.X attribute access check ───────────────────────────────────
        obj = node.value
        attr = node.attr
        if isinstance(obj, ast.Name):
            obj_name = obj.id
            mod = self.alias_to_module.get(obj_name)
            if mod is not None:
                exposed = (
                    set(mod.classes)
                    | set(mod.functions)
                    | set(mod.constants)
                )
                # Treat any class instance attrs from a class named exactly
                # like the module (or the conventional Config class) as also
                # available — guards `config.X` where `config = Config()`.
                config_cls = mod.classes.get("Config") or mod.classes.get(
                    obj_name.capitalize()
                )
                if config_cls is not None:
                    exposed |= config_cls.instance_attr_names
                    exposed |= config_cls.init_param_names
                if mod.star_imports:
                    exposed = None  # type: ignore[assignment]
                if exposed is not None and attr not in exposed:
                    self.issues.append(
                        CrossReferenceIssue(
                            severity="high",
                            category="bug",
                            description=(
                                f"Attribute '{attr}' accessed on module '{obj_name}' but "
                                f"'{os.path.basename(mod.file)}' does not define it."
                            ),
                            file=os.path.basename(self.file),
                            line=node.lineno,
                            suggestion=(
                                f"Define '{attr}' in {os.path.basename(mod.file)} "
                                f"(or correct the attribute name)."
                            ),
                            rule="X002-config-attr-missing",
                        )
                    )
            elif obj_name in self.aliased_object_attrs:
                allowed = self.aliased_object_attrs[obj_name]
                if attr not in allowed:
                    self.issues.append(
                        CrossReferenceIssue(
                            severity="medium",
                            category="bug",
                            description=(
                                f"Attribute '{attr}' accessed on '{obj_name}' but the "
                                f"resolved object does not declare it."
                            ),
                            file=os.path.basename(self.file),
                            line=node.lineno,
                            suggestion=(
                                f"Add '{attr}' to the source class or rename the access."
                            ),
                            rule="X002-config-attr-missing",
                        )
                    )
        self.generic_visit(node)

    def visit_Subscript(self, node: ast.Subscript) -> None:
        # v1.0.5 round 3 (final) W003: dict-style escape for X002.
        # ``config['LITERAL']`` against a known module/Config — same logic as
        # the attribute check above but routed through ast.Subscript. Fires
        # only when the slice is a string Constant; numeric indices and
        # variable indices are out of scope (handled elsewhere or simply too
        # dynamic to verify statically).
        obj = node.value
        s = node.slice
        if isinstance(s, ast.Index):  # py < 3.9 fallback
            s = s.value  # type: ignore[attr-defined]
        if (
            isinstance(obj, ast.Name)
            and isinstance(s, ast.Constant)
            and isinstance(s.value, str)
        ):
            obj_name = obj.id
            key = s.value
            mod = self.alias_to_module.get(obj_name)
            if mod is not None:
                exposed = (
                    set(mod.classes) | set(mod.functions) | set(mod.constants)
                )
                config_cls = mod.classes.get("Config") or mod.classes.get(
                    obj_name.capitalize()
                )
                if config_cls is not None:
                    exposed |= config_cls.instance_attr_names
                    exposed |= config_cls.init_param_names
                if not mod.star_imports and key not in exposed:
                    self.issues.append(
                        CrossReferenceIssue(
                            severity="high",
                            category="bug",
                            description=(
                                f"{obj_name}[{key!r}] — dict-style access on a "
                                "module-level object whose class does not "
                                "declare this key. Will raise KeyError or "
                                "AttributeError at runtime depending on the "
                                "underlying type."
                            ),
                            file=os.path.basename(self.file),
                            line=node.lineno,
                            suggestion=(
                                f"Either add '{key}' to "
                                f"{os.path.basename(mod.file)} (or its Config "
                                "class) or use a `.get(...)` form with an "
                                "explicit default."
                            ),
                            rule="W003-subscript-dynamic-key-unverifiable",
                        )
                    )
        self.generic_visit(node)

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _resolve_class_call(self, func: ast.AST) -> Optional[_ClassSignature]:
        """Resolve the called expression to a known class signature, or None."""
        if isinstance(func, ast.Name):
            sig = self.alias_to_class.get(func.id)
            if sig is not None:
                return sig
            local_sig = self.local_module.classes.get(func.id)
            if local_sig is not None:
                return local_sig
        elif isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
            mod = self.alias_to_module.get(func.value.id)
            if mod is not None:
                return mod.classes.get(func.attr)
        return None

    def _resolve_function_call(self, func: ast.AST) -> Optional[_FunctionSignature]:
        """Resolve the called expression to a known function signature, or None."""
        if isinstance(func, ast.Name):
            sig = self.alias_to_function.get(func.id)
            if sig is not None:
                return sig
            local_sig = self.local_module.functions.get(func.id)
            if local_sig is not None:
                return local_sig
        elif isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
            mod = self.alias_to_module.get(func.value.id)
            if mod is not None:
                return mod.functions.get(func.attr)
        return None


# ─── Public API ────────────────────────────────────────────────────────────────


_DEFAULT_IGNORE_DIRS: Set[str] = {
    "__pycache__",
    ".git",
    ".mypy_cache",
    ".pytest_cache",
    ".tox",
    "dist",
    "build",
    ".eggs",
    "node_modules",
    ".venv",
    "venv",
    "tests",  # ignore tests by default — they often pass intentional bad kwargs
}


def _collect_python_files(code_dir: str) -> List[str]:
    py_files: List[str] = []
    for dirpath, dirnames, filenames in os.walk(code_dir):
        dirnames[:] = [d for d in dirnames if d not in _DEFAULT_IGNORE_DIRS]
        for fname in filenames:
            if fname.endswith(".py"):
                py_files.append(os.path.join(dirpath, fname))
    return py_files


def analyse_cross_references_from_files(
    files: List[Tuple[str, str]],
) -> CrossReferenceReport:
    """
    Analyse a list of (path, source_text) tuples.

    The path is used as the module name root (its basename without ``.py``);
    relative paths are fine since we never read from the filesystem here.
    """
    modules: Dict[str, _ModuleInfo] = {}
    errors: List[str] = []

    for path, source in files:
        info, err = _scan_module(path, source)
        if err is not None:
            errors.append(f"{path}: {err}")
            continue
        if info is not None:
            # Multiple files may share a basename if they live in nested
            # packages — keep the first to avoid clobbering. For Crucible's
            # flat-bundle conventions this is rare.
            modules.setdefault(info.name, info)

    issues: List[CrossReferenceIssue] = []
    for path, source in files:
        try:
            tree = ast.parse(source, filename=path)
        except SyntaxError:
            continue
        local = modules.get(_module_name_from_path(path))
        if local is None:
            continue
        checker = _PerFileChecker(path, modules, local)
        checker.visit(tree)
        issues.extend(checker.issues)

    return CrossReferenceReport(
        passes=not issues,
        issues=issues,
        files_scanned=len(files),
        errors=errors,
    )


def analyse_cross_references(code_dir: str) -> CrossReferenceReport:
    """
    Walk ``code_dir`` for ``*.py`` files (skipping ``tests/``) and run all four
    cross-reference checks.

    Returns a :class:`CrossReferenceReport`. ``passes`` is True iff zero issues.
    """
    if not os.path.isdir(code_dir):
        return CrossReferenceReport(passes=True, errors=[f"Not a directory: {code_dir}"])

    py_files = _collect_python_files(code_dir)
    files: List[Tuple[str, str]] = []
    errors: List[str] = []
    for path in py_files:
        try:
            with open(path, "r", encoding="utf-8") as fh:
                source = fh.read()
        except OSError as exc:
            errors.append(f"{path}: {exc}")
            continue
        # Use path relative to code_dir as the readable identifier.
        rel = os.path.relpath(path, code_dir)
        files.append((rel, source))

    report = analyse_cross_references_from_files(files)
    if errors:
        report.errors.extend(errors)
    return report
