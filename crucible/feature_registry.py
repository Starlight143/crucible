"""
crucible/feature_registry.py
=====================================
Feature registry pattern for the post-processing pipeline.

Inspired by Claude Code's ``assembleToolPool()`` — instead of 19 hard-coded
``if args.feature_x: ...`` branches in the main runner, each feature is a
self-contained class registered by name.  The runner queries the registry,
resolves enabled features, and dispatches in dependency order.

Design goals
------------
* **Additive only** — adding a new feature never modifies existing code.
* **Explicit ordering** — features declare dependencies; ``resolve_order()``
  returns a topologically sorted execution plan.
* **Uniform result** — every feature returns a ``FeatureResult`` so the
  runner can log / aggregate outcomes consistently.
* **Safe by default** — disabled or missing features are silently skipped;
  exceptions inside a feature don't abort other features unless the feature
  is marked ``critical``.
* **Zero mandatory deps** — registering and querying the registry has no
  imports beyond stdlib; LLM / filesystem imports stay inside feature classes.

Usage::

    # Define a new feature (in its own module):
    from crucible.feature_registry import BaseFeature, FeatureConfig, FeatureResult, register

    @register("my_feature")
    class MyFeature(BaseFeature):
        name = "my_feature"
        label = "My Feature"
        requires: list[str] = []        # feature names that must run first

        def run(self, run_dir: str, config: FeatureConfig) -> FeatureResult:
            ...
            return FeatureResult(feature=self.name, success=True, summary="OK")

    # In the runner:
    from crucible.feature_registry import run_features

    enabled = ["security_scan", "deployment_artifacts", "my_feature"]
    results = run_features(run_dir, enabled_features=enabled, llm=llm)
"""
from __future__ import annotations

import time
from abc import ABC
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Type

if __package__ == "crucible":
    from .runtime_logging import get_logger, log_event
    from .cancellation import raise_if_cancelled, OperationCancelledError
else:  # pragma: no cover
    from runtime_logging import get_logger, log_event  # type: ignore[no-redef]
    from cancellation import raise_if_cancelled, OperationCancelledError  # type: ignore[no-redef]

LOGGER = get_logger(__name__)


# ── Config & Result ───────────────────────────────────────────────────────────

@dataclass
class FeatureConfig:
    """
    Runtime configuration passed to every feature's ``run()`` method.

    All optional fields default to ``None``; features should gracefully handle
    missing values rather than hard-failing.
    """
    llm: Any = None                          # shared LLM instance (may be None)
    args: Any = None                         # argparse Namespace from the runner
    env: Dict[str, str] = field(default_factory=dict)  # copy of os.environ subset
    extra: Dict[str, Any] = field(default_factory=dict)


@dataclass
class FeatureResult:
    """
    Uniform result object returned by every feature's ``run()`` method.
    """
    feature: str
    success: bool
    summary: str = ""
    details: Dict[str, Any] = field(default_factory=dict)
    duration_seconds: float = 0.0
    skipped: bool = False
    skip_reason: str = ""
    error: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "feature": self.feature,
            "success": self.success,
            "summary": self.summary,
            "duration_seconds": round(self.duration_seconds, 2),
            "skipped": self.skipped,
            "skip_reason": self.skip_reason,
            "error": self.error,
            **self.details,
        }


# ── Base class ────────────────────────────────────────────────────────────────

class BaseFeature(ABC):
    """
    Abstract base class for all post-processing features.

    Subclasses must set:
    * ``name`` (str)  — unique registry key.
    * ``label`` (str) — human-readable display name.
    * ``requires`` (list[str]) — names of features that must run before this one.

    Subclasses may override:
    * ``critical`` (bool) — if True, a failure in this feature raises and
                            aborts the remaining feature pipeline.
    """

    name: str = ""
    label: str = ""
    requires: List[str]
    critical: bool = False

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        # Ensure every subclass that does not explicitly declare `requires` gets
        # its own fresh list rather than sharing the parent class's mutable list.
        if "requires" not in cls.__dict__:
            cls.requires = []

    def run(self, run_dir: str, config: FeatureConfig) -> FeatureResult:
        """Execute the feature and return a ``FeatureResult``.

        Subclasses must override this method.
        """
        raise NotImplementedError(
            f"Feature '{self.name}' must implement run()."
        )  # pragma: no cover

    def is_available(self, config: FeatureConfig) -> bool:
        """
        Return True if this feature can run given *config*.

        Override to add pre-flight checks (e.g., LLM required, external tool
        available).  The default implementation always returns True.
        """
        return True

    def skip_reason_if_unavailable(self, config: FeatureConfig) -> str:
        """Human-readable reason returned when ``is_available()`` is False."""
        return ""


# ── Registry ─────────────────────────────────────────────────────────────────

_REGISTRY: Dict[str, Type[BaseFeature]] = {}


def register(name: str) -> Any:
    """
    Class decorator that registers a ``BaseFeature`` subclass under *name*.

    Usage::

        @register("security_scan")
        class SecurityScanFeature(BaseFeature):
            ...
    """
    def decorator(cls: Type[BaseFeature]) -> Type[BaseFeature]:
        if not issubclass(cls, BaseFeature):
            raise TypeError(
                f"@register: {cls.__name__} must be a subclass of BaseFeature."
            )
        if not name or not isinstance(name, str):
            raise ValueError(f"@register: name must be a non-empty string, got {name!r}.")
        if name in _REGISTRY:
            LOGGER.warning(
                "feature_registry: overwriting existing registration for '%s' "
                "(%s → %s).",
                name,
                _REGISTRY[name].__name__,
                cls.__name__,
            )
        cls.name = name
        _REGISTRY[name] = cls
        log_event(LOGGER, 10, "feature_registered", f"Feature '{name}' registered.",
                  feature=name, cls=cls.__name__)
        return cls
    return decorator


def get_feature(name: str) -> Optional[Type[BaseFeature]]:
    """Return the feature class registered under *name*, or None."""
    return _REGISTRY.get(name)


def list_features() -> List[str]:
    """Return sorted list of all registered feature names."""
    return sorted(_REGISTRY.keys())


# ── Dependency resolution (topological sort) ─────────────────────────────────

class CircularDependencyError(ValueError):
    """Raised when feature dependencies form a cycle."""


def resolve_order(feature_names: List[str]) -> List[str]:
    """
    Return *feature_names* in dependency-respecting execution order.

    Only features present in *feature_names* AND registered in ``_REGISTRY``
    are considered.  Dependencies not in *feature_names* are ignored (the
    caller controls which features are enabled).

    Raises ``CircularDependencyError`` if a cycle is detected.
    """
    # Deduplicate while preserving first-occurrence order.  Duplicate names
    # would cause in_degree double-counting and a false CircularDependencyError
    # because len(result) < len(feature_names) even for acyclic graphs.
    feature_names = list(dict.fromkeys(feature_names))
    enabled_set = set(feature_names)

    # Build adjacency for enabled features only
    adj: Dict[str, List[str]] = {}
    for fname in feature_names:
        cls = _REGISTRY.get(fname)
        if cls is None:
            adj[fname] = []
            continue
        deps = [d for d in (cls.requires or []) if d in enabled_set]
        adj[fname] = deps

    # Kahn's algorithm — in_degree[f] = number of enabled deps of f
    in_degree = {f: 0 for f in feature_names}
    for fname in feature_names:
        for dep in adj.get(fname, []):
            in_degree[fname] += 1

    queue = [f for f in feature_names if in_degree[f] == 0]
    result: List[str] = []

    while queue:
        # Sort for deterministic ordering when multiple nodes are ready
        queue.sort()
        node = queue.pop(0)
        result.append(node)
        # Reduce in-degree for nodes that depend on `node`
        for fname in feature_names:
            if node in adj.get(fname, []):
                in_degree[fname] -= 1
                if in_degree[fname] == 0:
                    queue.append(fname)

    if len(result) != len(feature_names):
        unresolved = set(feature_names) - set(result)
        raise CircularDependencyError(
            f"Circular dependency detected among features: {sorted(unresolved)}"
        )

    return result


# ── Runner ────────────────────────────────────────────────────────────────────

def run_features(
    run_dir: str,
    *,
    enabled_features: List[str],
    config: Optional[FeatureConfig] = None,
    llm: Any = None,
    args: Any = None,
) -> List[FeatureResult]:
    """
    Execute all *enabled_features* in dependency order.

    Parameters
    ----------
    run_dir:
        Path to the pipeline output directory.
    enabled_features:
        Ordered (or unordered) list of feature names to run.  Will be
        topologically sorted before execution.
    config:
        Shared ``FeatureConfig``.  If None, a default config is built from
        *llm* and *args*.
    llm:
        LLM instance injected into config when *config* is None.
    args:
        argparse Namespace injected into config when *config* is None.

    Returns
    -------
    List[FeatureResult]
        One result per feature, in execution order (including skipped ones).
    """
    import os as _os

    if config is None:
        config = FeatureConfig(
            llm=llm,
            args=args,
            env=dict(_os.environ),
        )

    # Resolve execution order
    try:
        ordered = resolve_order(enabled_features)
    except CircularDependencyError as exc:
        LOGGER.error("feature_registry: %s — running in input order instead.", exc)
        ordered = list(enabled_features)

    results: List[FeatureResult] = []

    for fname in ordered:
        raise_if_cancelled()
        cls = _REGISTRY.get(fname)
        if cls is None:
            log_event(
                LOGGER, 30, "feature_not_registered",
                f"Feature '{fname}' is not registered; skipping.",
                feature=fname,
            )
            results.append(FeatureResult(
                feature=fname,
                success=True,   # skipped ≠ failed; callers must check skipped=True
                skipped=True,
                skip_reason="Feature not registered in feature_registry.",
            ))
            continue

        instance = cls()

        # Pre-flight check
        if not instance.is_available(config):
            reason = instance.skip_reason_if_unavailable(config) or "Unavailable."
            log_event(
                LOGGER, 20, "feature_skipped",
                f"Feature '{fname}' skipped: {reason}",
                feature=fname, reason=reason,
            )
            results.append(FeatureResult(
                feature=fname,
                success=True,
                skipped=True,
                skip_reason=reason,
            ))
            continue

        # Execute
        start = time.monotonic()
        log_event(LOGGER, 20, "feature_started", f"Feature '{fname}' started.",
                  feature=fname)
        try:
            result = instance.run(run_dir, config)
        except OperationCancelledError:
            # Cancellation always propagates — do not absorb it into a
            # FeatureResult regardless of the feature's `critical` flag.
            # raise_if_cancelled() at the top of the loop only guards the
            # *between-feature* checkpoint; this guard covers cancellation
            # raised *inside* instance.run() (e.g. from a nested checkpoint).
            raise
        except Exception as exc:
            duration = time.monotonic() - start
            log_event(
                LOGGER, 40, "feature_failed",
                f"Feature '{fname}' raised: {type(exc).__name__}: {exc}",
                feature=fname, error=str(exc), duration_seconds=round(duration, 2),
            )
            result = FeatureResult(
                feature=fname,
                success=False,
                summary=f"Exception: {type(exc).__name__}: {exc}",
                duration_seconds=duration,
                error=str(exc),
            )
            if instance.critical:
                results.append(result)
                raise
            # Non-critical failure: record and move to the next feature.
            results.append(result)
            continue

        result = FeatureResult(
            feature=result.feature,
            success=result.success,
            summary=result.summary,
            details=result.details,
            duration_seconds=time.monotonic() - start,
            skipped=result.skipped,
            skip_reason=result.skip_reason,
            error=result.error,
        )
        log_event(
            LOGGER,
            20 if result.success else 30,
            "feature_finished",
            f"Feature '{fname}' {'succeeded' if result.success else 'failed'}: {result.summary}",
            feature=fname,
            success=result.success,
            duration_seconds=round(result.duration_seconds, 2),
        )
        results.append(result)

    return results


def format_results(results: List[FeatureResult]) -> str:
    """Return a human-readable summary table of feature results."""
    lines = ["── Feature Results ──────────────────────────────"]
    for r in results:
        if r.skipped:
            status = f"SKIP  {r.skip_reason}"
        elif r.success:
            status = f"OK    {r.summary}"
        else:
            status = f"FAIL  {r.error or r.summary}"
        lines.append(f"  {r.feature:<30}  [{status}]  ({r.duration_seconds:.1f}s)")
    lines.append("─────────────────────────────────────────────")
    return "\n".join(lines)
