"""
features/project_profile.py
=============================
Structured project profile loader for the Crucible pipeline.

A project profile is a YAML or JSON file that pre-fills project context —
tech stack, target market, known constraints, previous decisions — so that
the pipeline doesn't need to gather this information interactively or via
the Librarian research stage.

File discovery
--------------
The loader searches for profile files in this order:

1. Path specified by the ``PIPELINE_PROJECT_PROFILE`` environment variable.
2. ``project_profile.yaml`` in the current working directory.
3. ``project_profile.json`` in the current working directory.
4. ``project_profile.yaml`` in the workspace directory.
5. ``project_profile.json`` in the workspace directory.

YAML support
------------
YAML files are loaded via ``PyYAML`` when available.  If PyYAML is not
installed, only JSON profiles are supported.  Install with::

    pip install pyyaml

Profile schema
--------------
All fields are optional.  Unknown fields are preserved as ``extra_context``
so no profile key is ever silently dropped.

Example ``project_profile.yaml``::

    project_name: QuantDash
    target_market: Quantitative trading desks at hedge funds
    tech_stack:
      - Python 3.11
      - FastAPI
      - PostgreSQL
      - Redis
    known_constraints:
      - Must be deployable on AWS GovCloud
      - Max latency 50 ms for order routing
    previous_decisions:
      - Chose Pydantic v2 for strict validation
      - Rejected Kafka — too operationally complex for team size
    competitive_landscape: "Competes with QuantConnect, Lean, Zipline"
    budget_range: "$50k–$200k ARR target in year 1"

Usage::

    from crucible.features.project_profile import load_project_profile

    profile = load_project_profile()          # auto-discover
    if profile:
        prefix = profile.as_prompt_prefix()
        print(prefix)
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

# ── Optional YAML support ─────────────────────────────────────────────────────

try:
    import yaml as _yaml
    _HAS_YAML = True
except ImportError:
    _yaml = None  # type: ignore[assignment]
    _HAS_YAML = False

# ── Profile model ─────────────────────────────────────────────────────────────

@dataclass
class ProjectProfile:
    """
    Structured representation of a project profile file.

    Every field is optional; missing keys are simply ``None`` / empty list.
    """

    project_name: Optional[str] = None
    target_market: Optional[str] = None
    tech_stack: List[str] = field(default_factory=list)
    known_constraints: List[str] = field(default_factory=list)
    previous_decisions: List[str] = field(default_factory=list)
    competitive_landscape: Optional[str] = None
    budget_range: Optional[str] = None
    team_size: Optional[str] = None
    timeline: Optional[str] = None
    notes: Optional[str] = None
    # Any unrecognised keys land here
    extra_context: Dict[str, Any] = field(default_factory=dict)

    # Internal bookkeeping (not included in prompt)
    source_path: Optional[str] = None

    # ── Prompt integration ────────────────────────────────────────────────────

    def as_prompt_prefix(self) -> str:
        """
        Return a formatted string suitable for prepending to pipeline prompts.

        Empty / None fields are omitted so the prefix stays concise.
        """
        lines: List[str] = ["=== Project Profile (Pre-filled Context) ==="]

        if self.project_name:
            lines.append(f"Project Name    : {self.project_name}")
        if self.target_market:
            lines.append(f"Target Market   : {self.target_market}")
        if self.tech_stack:
            lines.append(f"Tech Stack      : {', '.join(self.tech_stack)}")
        if self.known_constraints:
            lines.append("Known Constraints:")
            for c in self.known_constraints:
                lines.append(f"  - {c}")
        if self.previous_decisions:
            lines.append("Previous Decisions:")
            for d in self.previous_decisions:
                lines.append(f"  - {d}")
        if self.competitive_landscape:
            lines.append(f"Competitive Landscape: {self.competitive_landscape}")
        if self.budget_range:
            lines.append(f"Budget / Revenue Target: {self.budget_range}")
        if self.team_size:
            lines.append(f"Team Size       : {self.team_size}")
        if self.timeline:
            lines.append(f"Timeline        : {self.timeline}")
        if self.notes:
            lines.append(f"Notes           : {self.notes}")
        if self.extra_context:
            lines.append("Additional Context:")
            for k, v in self.extra_context.items():
                lines.append(f"  {k}: {v}")

        lines.append("=== End Project Profile ===")
        return "\n".join(lines)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "project_name": self.project_name,
            "target_market": self.target_market,
            "tech_stack": self.tech_stack,
            "known_constraints": self.known_constraints,
            "previous_decisions": self.previous_decisions,
            "competitive_landscape": self.competitive_landscape,
            "budget_range": self.budget_range,
            "team_size": self.team_size,
            "timeline": self.timeline,
            "notes": self.notes,
            "extra_context": self.extra_context,
        }


# ── Parsing ───────────────────────────────────────────────────────────────────

_KNOWN_KEYS = frozenset({
    "project_name", "target_market", "tech_stack",
    "known_constraints", "previous_decisions",
    "competitive_landscape", "budget_range",
    "team_size", "timeline", "notes",
})


def _parse_profile_dict(data: Any, source_path: str) -> Optional[ProjectProfile]:
    """Convert a raw dict (from YAML or JSON parse) to a ProjectProfile."""
    if not isinstance(data, dict):
        return None

    def _str_list(val: Any) -> List[str]:
        if not val:
            return []
        if isinstance(val, list):
            return [str(v) for v in val if v is not None]
        # Accept a single string as a one-element list
        if isinstance(val, str):
            return [val]
        return []

    def _opt_str(val: Any) -> Optional[str]:
        if val is None:
            return None
        s = str(val).strip()
        return s if s else None

    # Separate known keys from extras
    known: Dict[str, Any] = {}
    extra: Dict[str, Any] = {}
    for k, v in data.items():
        if k in _KNOWN_KEYS:
            known[k] = v
        else:
            extra[k] = v

    return ProjectProfile(
        project_name=_opt_str(known.get("project_name")),
        target_market=_opt_str(known.get("target_market")),
        tech_stack=_str_list(known.get("tech_stack")),
        known_constraints=_str_list(known.get("known_constraints")),
        previous_decisions=_str_list(known.get("previous_decisions")),
        competitive_landscape=_opt_str(known.get("competitive_landscape")),
        budget_range=_opt_str(known.get("budget_range")),
        team_size=_opt_str(known.get("team_size")),
        timeline=_opt_str(known.get("timeline")),
        notes=_opt_str(known.get("notes")),
        extra_context=extra,
        source_path=source_path,
    )


def _load_yaml_file(path: str) -> Optional[Dict[str, Any]]:
    if not _HAS_YAML or _yaml is None:
        return None
    try:
        with open(path, "r", encoding="utf-8") as fh:
            # Use safe_load to prevent arbitrary Python object instantiation
            data = _yaml.safe_load(fh)
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def _load_json_file(path: str) -> Optional[Dict[str, Any]]:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def load_profile_from_path(path: str) -> Optional[ProjectProfile]:
    """
    Load a project profile from an explicit file path.

    Supports ``.yaml`` / ``.yml`` (requires PyYAML) and ``.json``.

    Returns None if the file does not exist, cannot be parsed, or contains
    no recognised fields.
    """
    if not os.path.isfile(path):
        return None

    ext = os.path.splitext(path)[1].lower()
    raw: Optional[Dict[str, Any]] = None

    if ext in (".yaml", ".yml"):
        raw = _load_yaml_file(path)
        if raw is None and not _HAS_YAML:
            # Try JSON fallback in case the file has a YAML extension but is valid JSON
            raw = _load_json_file(path)
    elif ext == ".json":
        raw = _load_json_file(path)
    else:
        # Unknown extension — try YAML first, then JSON
        raw = _load_yaml_file(path) or _load_json_file(path)

    if raw is None:
        return None
    return _parse_profile_dict(raw, source_path=path)


def load_project_profile(
    workspace_dir: Optional[str] = None,
) -> Optional[ProjectProfile]:
    """
    Auto-discover and load a project profile.

    Search order:
    1. ``PIPELINE_PROJECT_PROFILE`` env var (explicit override).
    2. ``project_profile.yaml`` in CWD.
    3. ``project_profile.json`` in CWD.
    4. ``project_profile.yaml`` in *workspace_dir* (if given).
    5. ``project_profile.json`` in *workspace_dir* (if given).

    Returns None if no profile is found.
    """
    cwd = os.getcwd()

    candidates: List[str] = []

    env_path = os.environ.get("PIPELINE_PROJECT_PROFILE", "").strip()
    if env_path:
        candidates.append(env_path)

    for search_dir in filter(None, [cwd, workspace_dir]):
        candidates.append(os.path.join(search_dir, "project_profile.yaml"))
        candidates.append(os.path.join(search_dir, "project_profile.yml"))
        candidates.append(os.path.join(search_dir, "project_profile.json"))

    # Deduplicate while preserving order
    seen: set = set()
    deduped: List[str] = []
    for p in candidates:
        norm = os.path.normpath(os.path.abspath(p))
        if norm not in seen:
            seen.add(norm)
            deduped.append(p)

    for path in deduped:
        profile = load_profile_from_path(path)
        if profile is not None:
            return profile

    return None


def save_profile_to_json(profile: ProjectProfile, path: str) -> None:
    """Persist a ProjectProfile to a JSON file (always available, no extra deps)."""
    data = profile.to_dict()
    try:
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        _tmp = path + ".tmp"
        try:
            with open(_tmp, "w", encoding="utf-8") as fh:
                json.dump(data, fh, indent=2, ensure_ascii=False)
            os.replace(_tmp, path)
        except OSError as exc:
            try:
                os.unlink(_tmp)
            except OSError:
                pass
            raise OSError(f"save_profile_to_json: could not write '{path}': {exc}") from exc
    except OSError:
        raise
