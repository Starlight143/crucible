"""
features/checkpoint.py
=======================
Stage-level checkpointing and resume for the analysis pipeline.

Saves intermediate stage outputs after each successful stage completion,
allowing interrupted runs to resume from the last checkpoint rather than
restarting from Stage 0.

Checkpoint data is stored as JSON files in ``{run_dir}/checkpoints/``.

v2 additions (Claude Code optimizations)
-----------------------------------------
* ``StageState`` enum — formal state machine for each stage:
  ``PENDING → RUNNING → COMPLETED | FAILED | SKIPPED``.
* ``CheckpointManager.set_state()`` / ``get_state()`` — persist and query
  stage state independent of checkpoint data so the runner knows exactly
  where a failure occurred.
* ``state_context()`` — context manager that automatically transitions
  ``PENDING → RUNNING → COMPLETED | FAILED`` with persistence.
* All new methods are backward-compatible; existing callers that only use
  ``save_stage()``, ``load_stage()``, and ``is_stage_complete()`` are
  unaffected.

Usage::

    from crucible.features.checkpoint import CheckpointManager, StageState

    mgr = CheckpointManager("/path/to/run_dir")

    # Simple resume check (unchanged API):
    if mgr.is_stage_complete("research_swarm"):
        data = mgr.load_stage("research_swarm")
    else:
        with mgr.state_context("research_swarm") as ctx:
            data = run_research_swarm(...)
            ctx.save(data)          # persists data + transitions to COMPLETED

    # Direct state queries:
    mgr.get_state("direction_debate")  # → StageState.FAILED / .COMPLETED / …
    mgr.get_state("codegen")           # → StageState.PENDING (not yet started)
"""
from __future__ import annotations

import contextlib
import json
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, Iterator, List, Optional

if __package__ == "crucible.features":
    from ..cancellation import OperationCancelledError as _OperationCancelledError
else:  # pragma: no cover
    from cancellation import OperationCancelledError as _OperationCancelledError  # type: ignore[no-redef]

# ── Stage ordering ─────────────────────────────────────────────────────────────

# Canonical stage ordering.  Stages must be completed in this order.
STAGE_ORDER: List[str] = [
    "librarian_research",   # Stage 0
    "research_swarm",       # Stage 1
    "direction_debate",     # Stage 2 (optional)
    "analysis_crew",        # Stage 3
    "codegen",              # Stage 4
    "postprocessing",       # post-Stage 4
]


# ── State machine ─────────────────────────────────────────────────────────────

class StageState(str, Enum):
    """
    Formal state for a single pipeline stage.

    Valid transitions::

        PENDING → RUNNING → COMPLETED
        PENDING → RUNNING → FAILED
        PENDING → SKIPPED
        FAILED  → RUNNING  (retry)
    """
    PENDING   = "pending"
    RUNNING   = "running"
    COMPLETED = "completed"
    FAILED    = "failed"
    SKIPPED   = "skipped"

    @property
    def is_terminal(self) -> bool:
        """Return True for states that cannot transition further (unless retried)."""
        return self in (StageState.COMPLETED, StageState.SKIPPED)

    @property
    def is_successful(self) -> bool:
        return self == StageState.COMPLETED

    @classmethod
    def from_str(cls, value: str) -> "StageState":
        """Parse a string to StageState, defaulting to PENDING on unknown values."""
        try:
            return cls(value.strip().lower())
        except (ValueError, AttributeError):
            return cls.PENDING


# ── Public data model ─────────────────────────────────────────────────────────

@dataclass
class StageCheckpoint:
    """Metadata for one saved stage checkpoint."""
    stage_name: str
    timestamp: str
    duration_seconds: float = 0.0
    data_keys: List[str] = field(default_factory=list)
    state: StageState = StageState.COMPLETED   # v2: reflects state at save time


@dataclass
class ResumeInfo:
    """Summary of checkpoint state for a run directory."""
    run_dir: str
    has_checkpoints: bool
    completed_stages: List[str] = field(default_factory=list)
    last_completed_stage: Optional[str] = None
    next_stage: Optional[str] = None
    checkpoints: List[StageCheckpoint] = field(default_factory=list)
    # v2: per-stage state map (all known stages, not only completed ones)
    stage_states: Dict[str, StageState] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "run_dir": self.run_dir,
            "has_checkpoints": self.has_checkpoints,
            "completed_stages": self.completed_stages,
            "last_completed_stage": self.last_completed_stage,
            "next_stage": self.next_stage,
            "stage_states": {k: v.value for k, v in self.stage_states.items()},
            "checkpoints": [
                {
                    "stage_name": c.stage_name,
                    "timestamp": c.timestamp,
                    "duration_seconds": round(c.duration_seconds, 2),
                    "data_keys": c.data_keys,
                    "state": c.state.value,
                }
                for c in self.checkpoints
            ],
        }


# ── State context helper ──────────────────────────────────────────────────────

class _StateContext:
    """
    Internal helper returned by ``CheckpointManager.state_context()``.

    Provides a ``save(data)`` method to persist data and mark the stage
    COMPLETED inside the ``with`` block.
    """

    def __init__(self, manager: "CheckpointManager", stage_name: str) -> None:
        self._manager = manager
        self._stage_name = stage_name
        self._saved = False

    def save(self, data: Any) -> None:
        """
        Persist *data* as the stage checkpoint and mark state COMPLETED.

        Calling ``save()`` multiple times within the same context block is
        safe; only the last call's data is retained.
        """
        self._manager.save_stage(self._stage_name, data)
        self._saved = True


# ── Checkpoint Manager ────────────────────────────────────────────────────────

class CheckpointManager:
    """
    Manages stage-level checkpoints and state for a pipeline run.

    Thread-unsafe — designed for single-threaded sequential pipeline execution.

    v2: adds formal state machine (``set_state`` / ``get_state`` /
    ``state_context``) on top of the original checkpoint API.
    """

    def __init__(self, run_dir: str) -> None:
        self._run_dir = str(run_dir)
        self._checkpoint_dir = os.path.join(run_dir, "checkpoints")
        self._stage_timers: Dict[str, float] = {}

    # ── Private helpers ───────────────────────────────────────────────────────

    def _stage_path(self, stage_name: str) -> str:
        return os.path.join(self._checkpoint_dir, f"{stage_name}.json")

    def _meta_path(self, stage_name: str) -> str:
        return os.path.join(self._checkpoint_dir, f"{stage_name}.meta.json")

    def _state_path(self, stage_name: str) -> str:
        return os.path.join(self._checkpoint_dir, f"{stage_name}.state.json")

    # ── Original public API (unchanged) ──────────────────────────────────────

    def start_stage(self, stage_name: str) -> None:
        """Record the start time for *stage_name*."""
        self._stage_timers[stage_name] = time.monotonic()

    def save_stage(
        self,
        stage_name: str,
        data: Any,
    ) -> None:
        """
        Persist *data* as a checkpoint for *stage_name*.

        *data* must be JSON-serialisable (dict, list, or primitive).
        If ``start_stage()`` was called, duration is calculated automatically.
        Also persists ``StageState.COMPLETED`` for this stage.
        """
        os.makedirs(self._checkpoint_dir, exist_ok=True)

        duration = 0.0
        if stage_name in self._stage_timers:
            duration = time.monotonic() - self._stage_timers.pop(stage_name)

        data_keys: List[str] = []
        if isinstance(data, dict):
            data_keys = list(data.keys())[:20]

        meta = {
            "stage_name": stage_name,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "duration_seconds": round(duration, 2),
            "data_keys": data_keys,
            "state": StageState.COMPLETED.value,
        }

        # Write atomically: write each file to a .tmp sibling, then rename.
        # os.replace() is atomic on POSIX and best-effort on Windows (replaces
        # in a single kernel call when both paths are on the same volume).
        # This prevents is_stage_complete() from returning True on a partial write
        # if the process is killed between writes.
        data_path = self._stage_path(stage_name)
        meta_path = self._meta_path(stage_name)

        try:
            from .._atomic_io import atomic_write_text
        except ImportError:  # flat-launcher mode (python crucible/__main__.py)
            from _atomic_io import atomic_write_text  # type: ignore[no-redef]
        # v1.1.11: route through the shared atomic writer so each file's parent
        # dir is fsynced after os.replace (POSIX power-loss durability,
        # CLAUDE.md §13.1).  Data is written before meta so a crash between the
        # two leaves at worst an orphan data file (meta absence => is_stage_
        # complete() still returns False), never a meta-without-data state.
        atomic_write_text(
            data_path,
            json.dumps(data, ensure_ascii=False, indent=2, default=str),
        )
        atomic_write_text(
            meta_path,
            json.dumps(meta, ensure_ascii=False, indent=2),
        )

        # v2: persist state file
        self._write_state(stage_name, StageState.COMPLETED, duration=duration)

    def is_stage_complete(self, stage_name: str) -> bool:
        """Return True if *stage_name* has a saved checkpoint."""
        return os.path.isfile(self._stage_path(stage_name))

    def load_stage(self, stage_name: str) -> Optional[Any]:
        """Load and return checkpoint data for *stage_name*, or None."""
        path = self._stage_path(stage_name)
        if not os.path.isfile(path):
            return None
        try:
            with open(path, "r", encoding="utf-8") as fh:
                return json.load(fh)
        except (json.JSONDecodeError, OSError):
            return None

    def clear_stage(self, stage_name: str) -> None:
        """Remove checkpoint for *stage_name*."""
        for path in (
            self._stage_path(stage_name),
            self._meta_path(stage_name),
            self._state_path(stage_name),   # v2
        ):
            try:
                os.remove(path)
            except OSError:
                pass

    def clear_all(self) -> None:
        """Remove all checkpoints for this run."""
        if not os.path.isdir(self._checkpoint_dir):
            return
        try:
            names = os.listdir(self._checkpoint_dir)
        except OSError:
            return
        for fname in names:
            fpath = os.path.join(self._checkpoint_dir, fname)
            if os.path.isfile(fpath):
                try:
                    os.remove(fpath)
                except OSError:
                    pass

    def get_resume_info(self) -> ResumeInfo:
        """
        Return a summary of checkpoint state.

        Identifies the last completed stage and the next stage to resume from.
        v2: also includes per-stage ``StageState`` for all known stages.
        """
        if not os.path.isdir(self._checkpoint_dir):
            return ResumeInfo(
                run_dir=self._run_dir,
                has_checkpoints=False,
            )

        completed: List[str] = []
        checkpoints: List[StageCheckpoint] = []
        stage_states: Dict[str, StageState] = {}

        for stage in STAGE_ORDER:
            # v2: read state file if present
            persisted_state = self._read_state(stage)
            if persisted_state != StageState.PENDING:
                stage_states[stage] = persisted_state

            if not self.is_stage_complete(stage):
                continue

            completed.append(stage)
            meta_path = self._meta_path(stage)
            meta: Dict[str, Any] = {}
            if os.path.isfile(meta_path):
                try:
                    with open(meta_path, "r", encoding="utf-8") as fh:
                        meta = json.load(fh)
                except (json.JSONDecodeError, OSError):
                    pass

            # Determine the effective state for this stage:
            # - Authoritative: the dedicated .state.json file (persisted_state).
            # - Fallback (v1 backward-compat): the meta file, which save_stage()
            #   always hard-codes as COMPLETED.
            # If we have a non-PENDING persisted_state we trust it over the meta
            # (a set_state(FAILED) call after checkpointing must not be silently
            # overwritten with COMPLETED by the meta file's hard-coded value).
            effective_state: StageState
            if persisted_state != StageState.PENDING:
                effective_state = persisted_state
            else:
                effective_state = StageState.from_str(
                    str(meta.get("state", StageState.COMPLETED.value))
                )
                stage_states[stage] = effective_state

            checkpoints.append(StageCheckpoint(
                stage_name=stage,
                timestamp=str(meta.get("timestamp", "")),
                duration_seconds=float(meta.get("duration_seconds", 0)),
                data_keys=meta.get("data_keys", []),
                state=effective_state,
            ))

        last_completed = completed[-1] if completed else None
        next_stage: Optional[str] = None
        if last_completed:
            idx = STAGE_ORDER.index(last_completed)
            if idx + 1 < len(STAGE_ORDER):
                next_stage = STAGE_ORDER[idx + 1]

        return ResumeInfo(
            run_dir=self._run_dir,
            has_checkpoints=len(completed) > 0,
            completed_stages=completed,
            last_completed_stage=last_completed,
            next_stage=next_stage,
            checkpoints=checkpoints,
            stage_states=stage_states,
        )

    def summary_text(self) -> str:
        """Human-readable checkpoint summary."""
        info = self.get_resume_info()
        if not info.has_checkpoints:
            return "No checkpoints found — run will start from Stage 0."
        lines = [
            f"Checkpoint Resume Info: {info.run_dir}",
            f"  Completed stages: {', '.join(info.completed_stages)}",
            f"  Last completed: {info.last_completed_stage}",
            f"  Next stage: {info.next_stage or 'all complete'}",
            "",
        ]
        for cp in info.checkpoints:
            lines.append(
                f"  [{cp.stage_name}] {cp.timestamp}  "
                f"({cp.duration_seconds:.1f}s)  state={cp.state.value}"
            )
        return "\n".join(lines)

    # ── v2: State machine API ─────────────────────────────────────────────────

    def _write_state(
        self,
        stage_name: str,
        state: StageState,
        *,
        duration: float = 0.0,
        error: Optional[str] = None,
    ) -> None:
        """Persist a state record for *stage_name*."""
        os.makedirs(self._checkpoint_dir, exist_ok=True)
        payload: Dict[str, Any] = {
            "stage": stage_name,
            "state": state.value,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "duration_seconds": round(duration, 2),
        }
        if error:
            payload["error"] = str(error)[:500]   # truncate long tracebacks
        state_path = self._state_path(stage_name)
        tmp_path = state_path + ".tmp"
        try:
            with open(tmp_path, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, ensure_ascii=False, indent=2)
            os.replace(tmp_path, state_path)   # atomic on POSIX, best-effort on Windows
        except Exception:
            try:
                os.remove(tmp_path)
            except OSError:
                pass
            raise

    def _read_state(self, stage_name: str) -> StageState:
        """
        Read persisted state for *stage_name*.

        Returns ``StageState.COMPLETED`` when a checkpoint data file exists
        (backward-compatibility: legacy checkpoints have no state file),
        ``StageState.PENDING`` when no record exists at all.
        """
        state_path = self._state_path(stage_name)
        if os.path.isfile(state_path):
            try:
                with open(state_path, "r", encoding="utf-8") as fh:
                    payload = json.load(fh)
                return StageState.from_str(str(payload.get("state", "pending")))
            except (json.JSONDecodeError, OSError):
                pass
        # Backward-compat: data file present → treat as COMPLETED
        if self.is_stage_complete(stage_name):
            return StageState.COMPLETED
        return StageState.PENDING

    def set_state(
        self,
        stage_name: str,
        state: StageState,
        *,
        error: Optional[str] = None,
    ) -> None:
        """
        Explicitly set and persist the state for *stage_name*.

        Use this when you need fine-grained state tracking outside of
        ``state_context()`` (e.g., marking a stage SKIPPED before it starts).
        """
        self._write_state(stage_name, state, error=error)

    def get_state(self, stage_name: str) -> StageState:
        """
        Return the current persisted ``StageState`` for *stage_name*.

        Returns ``StageState.PENDING`` when no state has been recorded.
        """
        return self._read_state(stage_name)

    def get_failed_stages(self) -> List[str]:
        """Return names of all stages currently in FAILED state."""
        failed: List[str] = []
        for stage in STAGE_ORDER:
            if self._read_state(stage) == StageState.FAILED:
                failed.append(stage)
        # Also scan for custom (non-STAGE_ORDER) stages
        if os.path.isdir(self._checkpoint_dir):
            try:
                for fname in os.listdir(self._checkpoint_dir):
                    if not fname.endswith(".state.json"):
                        continue
                    stage = fname[: -len(".state.json")]
                    if stage in STAGE_ORDER:
                        continue
                    if self._read_state(stage) == StageState.FAILED:
                        failed.append(stage)
            except OSError:
                pass
        return failed

    @contextlib.contextmanager
    def state_context(self, stage_name: str) -> Iterator[_StateContext]:
        """
        Context manager that manages the full PENDING → RUNNING → COMPLETED|FAILED
        state transition for *stage_name*.

        Usage::

            with mgr.state_context("codegen") as ctx:
                result = generate_code(...)
                ctx.save(result)   # persists data AND sets COMPLETED

        On normal exit without ``ctx.save()``, the stage is still marked
        COMPLETED (useful when no checkpoint data needs to be saved).

        On exception, the stage is marked FAILED with the exception message,
        then the exception is re-raised.
        """
        start = time.monotonic()
        self.set_state(stage_name, StageState.RUNNING)
        if stage_name not in self._stage_timers:
            self._stage_timers[stage_name] = start

        ctx = _StateContext(self, stage_name)
        try:
            yield ctx
        except _OperationCancelledError:
            # Cooperative cancellation must propagate without recording a FAILED
            # state — the stage was not logically "failed", it was cancelled.
            # Leave the stage state as RUNNING (set at scope entry); resume logic
            # can re-run the stage from scratch if the pipeline is restarted.
            self._stage_timers.pop(stage_name, None)
            raise
        except Exception as exc:
            duration = time.monotonic() - start
            self._stage_timers.pop(stage_name, None)
            self._write_state(
                stage_name,
                StageState.FAILED,
                duration=duration,
                error=f"{type(exc).__name__}: {exc}",
            )
            raise
        else:
            duration = time.monotonic() - start
            self._stage_timers.pop(stage_name, None)
            # If caller didn't call ctx.save(), just write state without data
            if not ctx._saved:
                self._write_state(stage_name, StageState.COMPLETED, duration=duration)
