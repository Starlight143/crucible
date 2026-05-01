"""
features/project_memory.py
==========================
Persistent cross-run project memory.

Saves direction decisions, confirmed tech choices, failed experiments, and
blocking risks from each completed run so future runs can avoid repeating
the same mistakes and can build on established choices.

Storage
-------
Memory is persisted in ``{workspace_dir}/project_memory.jsonl`` as an
**append-only JSONL ledger**: one JSON object per line, newest last.  This
design avoids read-modify-write races that corrupted the previous single-JSON
implementation under concurrent access.

On first use, any legacy ``project_memory.json`` is automatically migrated:
the file is renamed to ``project_memory.json.bak`` and its contents are
imported into the new JSONL file.

File locking
------------
* **POSIX** (Linux/macOS): ``fcntl.lockf`` advisory exclusive lock on the
  JSONL file during append and prune operations.
* **Windows**: process-level ``threading.Lock`` only (cross-process file
  locking not available without additional dependencies).  Concurrent writers
  from separate processes on Windows should be avoided.

Context window
--------------
``build_memory_prompt_prefix`` uses a rolling **token-budget** window
(``PROJECT_MEMORY_PROMPT_CHARS`` env var, default 16 000 chars ≈ 4 000 tokens)
rather than a fixed entry count, so the injected history always fits within
the caller's context window.

Schema validation
-----------------
``MemoryEntry.from_dict`` raises ``ValueError`` on missing or empty required
fields (``project_name``, ``timestamp``), preventing silent data corruption.

Usage::

    from crucible.features.project_memory import (
        ProjectMemoryStore,
        create_memory_entry_from_output,
    )

    store = ProjectMemoryStore("/path/to/workspace")

    # After a run completes:
    entry = create_memory_entry_from_output("/path/to/saved_projects/run_dir")
    if entry:
        store.add_entry(entry)

    # Before the next run:
    prefix = store.build_memory_prompt_prefix("my_project")
    if prefix:
        print(prefix)
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import warnings
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, Iterator, List, Optional

# ── File-locking portability ──────────────────────────────────────────────────
# fcntl is POSIX-only; on Windows we fall back to in-process threading.Lock.

try:
    import fcntl as _fcntl
    _HAS_FCNTL: bool = True
except ImportError:
    _fcntl = None  # type: ignore[assignment]
    _HAS_FCNTL = False

# ── Configuration ─────────────────────────────────────────────────────────────

try:
    MAX_ENTRIES_PER_PROJECT: int = max(
        1, int(os.environ.get("ENHANCED_PROJECT_MEMORY_MAX_ENTRIES") or "50")
    )
except (ValueError, TypeError):
    MAX_ENTRIES_PER_PROJECT = 50

try:
    _PROMPT_BUDGET_CHARS: int = max(
        500, int(os.environ.get("PROJECT_MEMORY_PROMPT_CHARS") or "16000")
    )
except (ValueError, TypeError):
    _PROMPT_BUDGET_CHARS = 16_000

_JSONL_FILENAME = "project_memory.jsonl"
_LEGACY_JSON_FILENAME = "project_memory.json"

# Required fields for schema validation
_REQUIRED_FIELDS = frozenset({"project_name", "timestamp"})


# ── File-lock context manager ─────────────────────────────────────────────────

@contextmanager
def _file_lock(fh: Any) -> Iterator[None]:
    """
    Advisory exclusive lock on an open file handle for the duration of the
    context.

    On POSIX (Linux/macOS) uses ``fcntl.lockf(LOCK_EX)``.
    On Windows (where fcntl is unavailable) this is a no-op; callers must
    rely on the module-level ``threading.Lock`` for in-process safety.
    """
    if _HAS_FCNTL and _fcntl is not None:
        try:
            _fcntl.lockf(fh, _fcntl.LOCK_EX)
        except OSError:
            pass  # best-effort; don't break on NFS or unusual filesystems
    try:
        yield
    finally:
        if _HAS_FCNTL and _fcntl is not None:
            try:
                _fcntl.lockf(fh, _fcntl.LOCK_UN)
            except OSError:
                pass


# ── Public data model ─────────────────────────────────────────────────────────

@dataclass
class MemoryEntry:
    """One saved run's worth of project context."""

    timestamp: str
    project_name: str
    direction_selected: Optional[str]
    direction_summary: Optional[str]
    score: Optional[float]
    risk_level: Optional[str]
    confirmed_tech_choices: List[str] = field(default_factory=list)
    failed_experiments: List[str] = field(default_factory=list)
    blocking_risks: List[str] = field(default_factory=list)
    notes: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "timestamp": self.timestamp,
            "project_name": self.project_name,
            "direction_selected": self.direction_selected,
            "direction_summary": self.direction_summary,
            "score": self.score,
            "risk_level": self.risk_level,
            "confirmed_tech_choices": self.confirmed_tech_choices,
            "failed_experiments": self.failed_experiments,
            "blocking_risks": self.blocking_risks,
            "notes": self.notes,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "MemoryEntry":
        """
        Construct a MemoryEntry from a raw dict.

        Raises
        ------
        ValueError
            If any required field (``project_name``, ``timestamp``) is
            missing or empty.
        """
        missing = _REQUIRED_FIELDS - set(data.keys())
        if missing:
            raise ValueError(
                f"MemoryEntry missing required fields: {sorted(missing)}"
            )
        pn = str(data["project_name"]).strip()
        ts = str(data["timestamp"]).strip()
        if not pn:
            raise ValueError("MemoryEntry: 'project_name' must not be empty.")
        if not ts:
            raise ValueError("MemoryEntry: 'timestamp' must not be empty.")
        return cls(
            timestamp=ts,
            project_name=pn,
            direction_selected=data.get("direction_selected"),
            direction_summary=data.get("direction_summary"),
            score=_safe_float(data.get("score")),
            risk_level=data.get("risk_level"),
            confirmed_tech_choices=_safe_str_list(data.get("confirmed_tech_choices")),
            failed_experiments=_safe_str_list(data.get("failed_experiments")),
            blocking_risks=_safe_str_list(data.get("blocking_risks")),
            notes=str(data.get("notes") or ""),
        )


# ── Helpers ───────────────────────────────────────────────────────────────────

def _safe_float(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _safe_str_list(value: Any) -> List[str]:
    if not value:
        return []
    if isinstance(value, list):
        return [str(item) for item in value if item is not None]
    return []


def _normalise_name(name: str) -> str:
    return name.lower().strip()


def _entry_to_jsonl_line(entry: MemoryEntry) -> str:
    return json.dumps(entry.to_dict(), ensure_ascii=False, separators=(",", ":"))


def _parse_jsonl_line(line: str) -> Optional[Dict[str, Any]]:
    line = line.strip()
    if not line:
        return None
    try:
        obj = json.loads(line)
        return obj if isinstance(obj, dict) else None
    except json.JSONDecodeError:
        return None


def _safe_rename(src: str, dst: str) -> None:
    try:
        os.replace(src, dst)
    except OSError:
        pass


def _load_json_file(path: str) -> Optional[Dict[str, Any]]:
    if not os.path.isfile(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else None
    except (json.JSONDecodeError, OSError):
        return None


# ── Store ─────────────────────────────────────────────────────────────────────

class ProjectMemoryStore:
    """
    Thread-safe, file-backed JSONL store for project memory entries.

    Write operations immediately append to the JSONL ledger.  Periodic
    pruning keeps the file from growing unboundedly (triggered automatically
    when ``add_entry`` pushes the per-project count above
    ``MAX_ENTRIES_PER_PROJECT``).

    Cross-process safety:
    * POSIX: ``fcntl.lockf`` exclusive lock during each write.
    * Windows: in-process ``threading.Lock`` only.
    """

    def __init__(self, workspace_dir: str) -> None:
        self._workspace_dir = str(workspace_dir)
        self._jsonl_file = os.path.join(workspace_dir, _JSONL_FILENAME)
        self._legacy_json = os.path.join(workspace_dir, _LEGACY_JSON_FILENAME)
        self._lock = threading.Lock()
        self._migrated = False

    # -- migration ----------------------------------------------------------------

    def _maybe_migrate_legacy(self) -> None:
        """
        One-time migration: if ``project_memory.json`` exists (old format),
        import its entries into the JSONL file and rename it to ``.bak``.
        Runs at most once per store instance.
        """
        if self._migrated:
            return
        self._migrated = True

        if not os.path.isfile(self._legacy_json):
            return
        if os.path.isfile(self._jsonl_file):
            # JSONL already exists — skip import to avoid duplicates.
            _safe_rename(self._legacy_json, self._legacy_json + ".bak")
            return

        try:
            with open(self._legacy_json, "r", encoding="utf-8") as fh:
                raw = json.load(fh)
        except (json.JSONDecodeError, OSError):
            _safe_rename(self._legacy_json, self._legacy_json + ".bak")
            return

        if not isinstance(raw, dict):
            _safe_rename(self._legacy_json, self._legacy_json + ".bak")
            return

        # raw = { "project_key": [ {entry_dict}, ... ], ... }
        migrated_lines: List[str] = []
        for _key, entries in raw.items():
            if not isinstance(entries, list):
                continue
            for raw_entry in entries:
                if not isinstance(raw_entry, dict):
                    continue
                if not raw_entry.get("project_name") or not raw_entry.get("timestamp"):
                    continue
                migrated_lines.append(
                    json.dumps(raw_entry, ensure_ascii=False, separators=(",", ":"))
                )

        if migrated_lines:
            try:
                os.makedirs(self._workspace_dir, exist_ok=True)
                with open(self._jsonl_file, "a", encoding="utf-8") as fh:
                    with _file_lock(fh):
                        for line in migrated_lines:
                            fh.write(line + "\n")
            except OSError as exc:
                warnings.warn(
                    f"ProjectMemoryStore: migration write failed: {exc}",
                    stacklevel=2,
                )
                return

        _safe_rename(self._legacy_json, self._legacy_json + ".bak")

    # -- read -------------------------------------------------------------------

    def _read_all_entries(self) -> List[Dict[str, Any]]:
        """Return all raw entry dicts from the JSONL file (oldest first)."""
        if not os.path.isfile(self._jsonl_file):
            return []
        entries: List[Dict[str, Any]] = []
        try:
            with open(self._jsonl_file, "r", encoding="utf-8") as fh:
                for line in fh:
                    obj = _parse_jsonl_line(line)
                    if obj is not None:
                        entries.append(obj)
        except OSError:
            pass
        return entries

    # -- prune ------------------------------------------------------------------

    def _prune_project(self, project_key: str) -> None:
        """
        Atomically rewrite the JSONL file, keeping at most
        ``MAX_ENTRIES_PER_PROJECT`` recent entries for *project_key* and all
        entries for other projects.

        Uses a temp-file swap to avoid data loss on crash.
        """
        all_entries = self._read_all_entries()

        this_project: List[Dict[str, Any]] = []
        others: List[Dict[str, Any]] = []
        for e in all_entries:
            if _normalise_name(str(e.get("project_name") or "")) == project_key:
                this_project.append(e)
            else:
                others.append(e)

        this_project = this_project[-MAX_ENTRIES_PER_PROJECT:]
        combined = others + this_project  # others first, then trimmed project entries

        try:
            fd, tmp_path = tempfile.mkstemp(
                dir=self._workspace_dir, prefix=".pmem_", suffix=".jsonl"
            )
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    with _file_lock(fh):
                        for entry in combined:
                            line = json.dumps(
                                entry, ensure_ascii=False, separators=(",", ":")
                            )
                            fh.write(line + "\n")
                os.replace(tmp_path, self._jsonl_file)
            except Exception:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
                raise
        except OSError as exc:
            warnings.warn(
                f"ProjectMemoryStore: prune failed for '{project_key}': {exc}",
                stacklevel=3,
            )

    # -- public -----------------------------------------------------------------

    def get_entries(self, project_name: str) -> List[MemoryEntry]:
        """Return all stored entries for *project_name* (oldest first)."""
        with self._lock:
            self._maybe_migrate_legacy()
            key = _normalise_name(project_name)
            entries: List[MemoryEntry] = []
            for raw in self._read_all_entries():
                if _normalise_name(str(raw.get("project_name") or "")) != key:
                    continue
                try:
                    entries.append(MemoryEntry.from_dict(raw))
                except Exception:
                    pass
            return entries

    def add_entry(self, entry: MemoryEntry) -> None:
        """Append *entry* to the ledger, pruning if the project exceeds the limit."""
        with self._lock:
            self._maybe_migrate_legacy()
            line = _entry_to_jsonl_line(entry)
            try:
                os.makedirs(self._workspace_dir, exist_ok=True)
                with open(self._jsonl_file, "a", encoding="utf-8") as fh:
                    with _file_lock(fh):
                        fh.write(line + "\n")
            except OSError as exc:
                warnings.warn(
                    f"ProjectMemoryStore: append failed: {exc}",
                    stacklevel=2,
                )
                return

            key = _normalise_name(entry.project_name)
            count = sum(
                1 for e in self._read_all_entries()
                if _normalise_name(str(e.get("project_name") or "")) == key
            )
            if count > MAX_ENTRIES_PER_PROJECT:
                self._prune_project(key)

    def build_context_text(self, project_name: str) -> str:
        """
        Return compact multi-line text summarising recent runs within the
        configured token budget (``PROJECT_MEMORY_PROMPT_CHARS`` chars).

        Entries are included newest-first until the char budget is exhausted.
        Returns empty string when no history exists.
        """
        entries = self.get_entries(project_name)
        if not entries:
            return ""

        header = (
            f"[Project Memory — {len(entries)} prior run(s) for '{project_name}']"
        )
        budget = _PROMPT_BUDGET_CHARS - len(header) - 10  # leave headroom

        selected_blocks: List[str] = []
        for idx, entry in enumerate(reversed(entries), 1):  # newest first
            block_lines = [f"\n  Run -{idx} ({entry.timestamp})"]
            if entry.direction_selected:
                summary = (entry.direction_summary or "")[:120]
                block_lines.append(
                    f"    Direction : {entry.direction_selected} — {summary}"
                )
            score_str = (
                f"{entry.score}/100" if entry.score is not None else "N/A"
            )
            block_lines.append(
                f"    Score     : {score_str} | Risk: {entry.risk_level or 'unknown'}"
            )
            if entry.confirmed_tech_choices:
                items = ", ".join(entry.confirmed_tech_choices[:5])
                block_lines.append(f"    Confirmed : {items}")
            if entry.failed_experiments:
                items = ", ".join(entry.failed_experiments[:5])
                block_lines.append(f"    Avoid     : {items}  (failed experiments)")
            if entry.blocking_risks:
                items = "; ".join(entry.blocking_risks[:3])
                block_lines.append(f"    Risks     : {items}")
            block = "\n".join(block_lines)
            if budget - len(block) < 0:
                break
            selected_blocks.append(block)
            budget -= len(block)

        if not selected_blocks:
            return ""
        return header + "".join(selected_blocks)

    def build_memory_prompt_prefix(self, project_name: str) -> str:
        """
        Return a prompt prefix embedding historical context.
        Returns empty string when there is no history.
        """
        ctx = self.build_context_text(project_name)
        if not ctx:
            return ""
        # Use concatenation instead of .format() so that curly-brace patterns
        # in user-supplied data (direction names, experiment names, etc.) do not
        # raise KeyError / IndexError from the format engine.
        return (
            "\n"
            "--- Historical Project Memory ---\n"
            + ctx + "\n"
            "\n"
            "When choosing a direction and generating code, take the above\n"
            "history into account: avoid previously failed experiments and\n"
            "confirmed blocking risks; build on confirmed tech choices.\n"
            "---\n"
        )


# ── Factory from saved output ─────────────────────────────────────────────────

def create_memory_entry_from_output(run_dir: str) -> Optional[MemoryEntry]:
    """
    Build a MemoryEntry from a completed run's output directory.

    Reads ``analysis_result.json``, ``run_meta.json``, and (optionally)
    ``run_snapshot.json``.  Returns None if no usable data is found.
    """
    run_dir = str(run_dir)
    analysis = _load_json_file(os.path.join(run_dir, "analysis_result.json")) or {}
    meta = _load_json_file(os.path.join(run_dir, "run_meta.json")) or {}

    if not analysis and not meta:
        return None

    project_name = str(
        analysis.get("project_name")
        or meta.get("project_name")
        or os.path.basename(run_dir)
    ).strip()
    if not project_name:
        return None

    timestamp = str(
        meta.get("timestamp")
        or datetime.now(timezone.utc).isoformat()
    )

    direction_selected: Optional[str] = None
    direction_summary: Optional[str] = None
    snapshot = _load_json_file(os.path.join(run_dir, "run_snapshot.json")) or {}
    dd = snapshot.get("direction_decision") or {}
    if isinstance(dd, dict):
        direction_selected = dd.get("selected_direction")
        direction_summary = dd.get("summary")

    blocking_risks: List[str] = []
    gate_snap = analysis.get("gate_context_snapshot") or {}
    if isinstance(gate_snap, dict):
        blocking_risks = _safe_str_list(gate_snap.get("blocking_risks"))
    if not blocking_risks:
        blocking_risks = _safe_str_list(analysis.get("blocking_risks"))

    experiments_raw = analysis.get("experiments") or []
    failed_experiments: List[str] = []
    for exp in experiments_raw:
        if not isinstance(exp, dict):
            continue
        status = str(exp.get("status") or "").lower()
        if status in ("failed", "rejected", "killed", "invalid"):
            name = str(exp.get("name") or exp.get("title") or "").strip()
            if name:
                failed_experiments.append(name)

    confirmed_tech_choices = _safe_str_list(
        analysis.get("codegen_requirements")
    )[:10]

    return MemoryEntry(
        timestamp=timestamp,
        project_name=project_name,
        direction_selected=direction_selected,
        direction_summary=direction_summary,
        score=_safe_float(analysis.get("score")),
        risk_level=analysis.get("risk_level"),
        confirmed_tech_choices=confirmed_tech_choices,
        failed_experiments=failed_experiments[:10],
        blocking_risks=blocking_risks[:10],
    )


# ── Optional vector / semantic search backend ─────────────────────────────────
#
# When ``chromadb`` is installed the SemanticMemorySearch class can perform
# cosine-similarity search over memory entries, returning the *k* most
# semantically similar runs to a free-text query.  Without chromadb it falls
# back to simple substring / keyword matching so the API remains the same.
#
# Install the optional backend with:
#   pip install chromadb

try:
    import chromadb as _chromadb
    _HAS_CHROMADB = True
except ImportError:
    _chromadb = None  # type: ignore[assignment]
    _HAS_CHROMADB = False


def _entry_to_vector_doc(entry: MemoryEntry) -> str:
    """Flatten a MemoryEntry into a single searchable text document."""
    parts: List[str] = []
    if entry.project_name:
        parts.append(f"project: {entry.project_name}")
    if entry.direction_selected:
        parts.append(f"direction: {entry.direction_selected}")
    if entry.direction_summary:
        parts.append(entry.direction_summary)
    if entry.confirmed_tech_choices:
        parts.append("confirmed: " + ", ".join(entry.confirmed_tech_choices))
    if entry.failed_experiments:
        parts.append("failed: " + ", ".join(entry.failed_experiments))
    if entry.blocking_risks:
        parts.append("risks: " + "; ".join(entry.blocking_risks))
    if entry.notes:
        parts.append(entry.notes)
    return " | ".join(parts)


class SemanticMemorySearch:
    """
    Semantic (cosine-similarity) search over project memory entries.

    Uses ChromaDB as the vector store when available; falls back to
    case-insensitive keyword search otherwise.

    Parameters
    ----------
    store:
        An existing ``ProjectMemoryStore`` instance (JSONL backend).
    persist_dir:
        Directory for ChromaDB on-disk persistence.  Defaults to
        ``{store._workspace_dir}/.chroma_memory``.
    collection_name:
        ChromaDB collection name (default ``"project_memory"``).

    Usage::

        store = ProjectMemoryStore("/path/to/workspace")
        searcher = SemanticMemorySearch(store)

        searcher.rebuild_index()   # index all current entries
        results = searcher.search("high-risk payment processing", k=5)
        for entry in results:
            print(entry.project_name, entry.direction_selected)
    """

    def __init__(
        self,
        store: ProjectMemoryStore,
        *,
        persist_dir: Optional[str] = None,
        collection_name: str = "project_memory",
    ) -> None:
        self._store = store
        self._persist_dir = persist_dir or os.path.join(
            store._workspace_dir, ".chroma_memory"
        )
        self._collection_name = collection_name
        self._chroma_client: Any = None
        self._collection: Any = None
        self._lock = threading.Lock()

    # ── ChromaDB helpers ──────────────────────────────────────────────────────

    def _ensure_chroma(self) -> bool:
        """Initialise ChromaDB client; return True on success."""
        if not _HAS_CHROMADB or _chromadb is None:
            return False
        if self._chroma_client is not None:
            return True
        try:
            os.makedirs(self._persist_dir, exist_ok=True)
            self._chroma_client = _chromadb.PersistentClient(  # type: ignore[attr-defined]
                path=self._persist_dir
            )
            self._collection = self._chroma_client.get_or_create_collection(
                name=self._collection_name,
                metadata={"hnsw:space": "cosine"},
            )
            return True
        except Exception as exc:
            warnings.warn(
                f"SemanticMemorySearch: ChromaDB init failed: {exc}. "
                "Falling back to keyword search.",
                stacklevel=3,
            )
            self._chroma_client = None
            self._collection = None
            return False

    def rebuild_index(self) -> int:
        """
        (Re)build the vector index from all entries in the JSONL store.

        Existing index documents are replaced.  Returns the number of
        documents indexed.
        """
        # Phase 1: check chroma readiness under self._lock.
        with self._lock:
            if not self._ensure_chroma():
                return 0

        # Phase 2: read the JSONL store under self._store._lock WITHOUT holding
        # self._lock.  Nesting self._store._lock inside self._lock would create
        # an ABBA deadlock if another thread holds self._store._lock and then
        # tries to acquire self._lock (e.g. via add_entry → _ensure_chroma path).
        with self._store._lock:
            all_raw = self._store._read_all_entries()
        if not all_raw:
            return 0

        entries: List[MemoryEntry] = []
        for idx_raw, raw in enumerate(all_raw):
            try:
                entries.append(MemoryEntry.from_dict(raw))
            except Exception as _parse_exc:
                warnings.warn(
                    f"SemanticMemorySearch: failed to parse entry {idx_raw}: {_parse_exc}",
                    stacklevel=2,
                )

        if not entries:
            return 0

        # Phase 3: build ids/docs/metas (pure computation, no locks required).
        # Build stable content-based IDs so the same entry always gets the
        # same UID across rebuilds.  Using position (idx) was unstable: if
        # entries were added or removed the position shifted, causing
        # ChromaDB to treat unchanged entries as new documents.
        import hashlib
        ids: List[str] = []
        docs: List[str] = []
        metas: List[Dict[str, Any]] = []

        for entry in entries:
            uid = hashlib.md5(
                (
                    f"{entry.project_name}|{entry.timestamp}"
                    f"|{entry.direction_selected or ''}"
                    f"|{entry.risk_level or ''}"
                ).encode()
            ).hexdigest()
            ids.append(uid)
            docs.append(_entry_to_vector_doc(entry))
            metas.append({
                "project_name": entry.project_name,
                "timestamp": entry.timestamp,
                "score": entry.score if entry.score is not None else -1.0,
                "risk_level": entry.risk_level or "",
                "direction_selected": entry.direction_selected or "",
            })

        # Phase 4: upsert into ChromaDB under self._lock.
        # Re-check self._collection — it may have been invalidated between
        # Phase 1 and now (e.g. by a concurrent _close_chroma() call).
        indexed = 0
        batch_size = 100
        with self._lock:
            if self._collection is None:
                return 0
            for i in range(0, len(ids), batch_size):
                try:
                    self._collection.upsert(
                        ids=ids[i:i + batch_size],
                        documents=docs[i:i + batch_size],
                        metadatas=metas[i:i + batch_size],
                    )
                    indexed += len(ids[i:i + batch_size])
                except Exception as exc:
                    warnings.warn(
                        f"SemanticMemorySearch: batch upsert failed at offset {i}: {exc}",
                        stacklevel=2,
                    )
                    break
        return indexed

    # ── Search ────────────────────────────────────────────────────────────────

    def search(
        self,
        query: str,
        *,
        k: int = 5,
        project_name: Optional[str] = None,
    ) -> List[MemoryEntry]:
        """
        Return the *k* memory entries most semantically similar to *query*.

        Parameters
        ----------
        query:
            Free-text search query (e.g. ``"high-risk payment API"``)
        k:
            Maximum number of results to return.
        project_name:
            When given, restrict results to this project.

        Returns
        -------
        List[MemoryEntry]
            At most *k* entries, ordered by similarity (most similar first).
            Falls back to keyword search if ChromaDB is unavailable.
        """
        if not query.strip():
            return []

        # Acquire self._lock only to check/initialise the ChromaDB collection.
        # Do NOT hold self._lock while calling _chroma_search or _keyword_search:
        # both of those methods acquire self._store._lock internally, creating a
        # potential ABBA deadlock if another thread holds store._lock and calls
        # search() (wanting self._lock).
        with self._lock:
            use_chroma = self._ensure_chroma() and self._collection is not None

        if use_chroma:
            return self._chroma_search(query, k=k, project_name=project_name)
        return self._keyword_search(query, k=k, project_name=project_name)

    def _chroma_search(
        self,
        query: str,
        *,
        k: int,
        project_name: Optional[str],
    ) -> List[MemoryEntry]:
        try:
            where: Optional[Dict[str, Any]] = None
            if project_name:
                where = {"project_name": {"$eq": project_name}}
            resp = self._collection.query(
                query_texts=[query],
                n_results=min(k, max(1, self._collection.count())),
                where=where,
            )
        except Exception:
            return self._keyword_search(query, k=k, project_name=project_name)

        metadatas = (resp.get("metadatas") or [[]])[0]
        entries: List[MemoryEntry] = []
        for meta in metadatas:
            if not isinstance(meta, dict):
                continue
            pname = meta.get("project_name") or ""
            ts = meta.get("timestamp") or ""
            if not pname or not ts:
                continue
            # Re-fetch full entry from the JSONL store
            all_entries = self._store.get_entries(pname)
            for e in all_entries:
                if e.timestamp == ts:
                    entries.append(e)
                    break
        return entries

    def _keyword_search(
        self,
        query: str,
        *,
        k: int,
        project_name: Optional[str],
    ) -> List[MemoryEntry]:
        """
        Simple case-insensitive keyword fallback when ChromaDB is unavailable.
        """
        query_lower = query.lower()
        # Acquire store lock: concurrent add_entry / _prune_project may be
        # mid-write when _keyword_search is called outside rebuild_index().
        with self._store._lock:
            all_raw = self._store._read_all_entries()
        scored: List[tuple] = []

        for raw in all_raw:
            try:
                entry = MemoryEntry.from_dict(raw)
            except Exception:
                continue
            if project_name and _normalise_name(entry.project_name) != _normalise_name(project_name):
                continue
            doc = _entry_to_vector_doc(entry).lower()
            # Score = number of query words found in the document
            score = sum(1 for word in query_lower.split() if word in doc)
            if score > 0:
                scored.append((score, entry))

        scored.sort(key=lambda x: x[0], reverse=True)
        return [e for _, e in scored[:k]]
