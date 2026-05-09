"""
scripts/migrate_run_meta_cost.py
================================
Back-fill the v1.0.5 round 4 cost-surfacing fields into legacy
``run_meta.json`` files.

Why this exists
---------------
Before v1.0.5 round 4, ``save_project_output`` never wrote
``total_cost`` / ``total_cost_usd`` / ``total_tokens`` / ``cost_source``
to ``run_meta.json``.  The dashboard read ``meta.total_cost`` (None)
and rendered $0.00 for every saved run.  The authoritative cost ledger
was preserved in ``run_snapshot.json::cost_summary`` — this script
copies those values into ``run_meta.json`` so the dashboard renders
real $ amounts for historical runs.

Usage
-----
::

    python scripts/migrate_run_meta_cost.py            # dry-run (no writes)
    python scripts/migrate_run_meta_cost.py --apply    # apply changes
    python scripts/migrate_run_meta_cost.py --root /path/to/saved_projects --apply

Behaviour per run directory
---------------------------
1. If ``run_meta.json`` already has ``total_cost_usd`` AND ``total_tokens``
   set to non-null values → skip (the run was saved post-migration or has
   already been migrated).
2. If ``run_snapshot.json`` exists and has ``cost_summary`` with the
   relevant fields → write those into ``run_meta.json``, preserving full
   float precision (no rounding — the WebUI's display layer is the only
   place rounding belongs).
3. If neither file exists / lacks the data → skip with an explanatory
   log line (e.g. cancelled or pre-snapshot runs).

Persistence rule
----------------
Use the SAME atomic-write pattern as section_07's ``_atomic_write_text``
(write to ``<path>.tmp`` then ``os.replace``) so a crash during the
migration cannot leave a half-written ``run_meta.json``.

Idempotency
-----------
Repeated invocations are safe: step 1's pre-check ensures already-
migrated runs are skipped.  ``--apply`` is the only flag that actually
mutates disk.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, Optional


def _atomic_write_text(path: Path, text: str) -> None:
    """Write *text* to *path* atomically.  Mirrors section_07's helper.

    The temporary file is created in the same directory as *path* so the
    final ``os.replace`` is a same-filesystem rename and therefore atomic.
    On failure we always remove the half-written ``.tmp`` to avoid
    polluting the run directory.
    """
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    try:
        tmp_path.write_text(text, encoding="utf-8")
        os.replace(str(tmp_path), str(path))
    except Exception:
        try:
            if tmp_path.exists():
                tmp_path.unlink()
        except OSError:
            pass
        raise


def _load_json(path: Path) -> Optional[Dict[str, Any]]:
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _coerce_float(value: Any) -> Optional[float]:
    """Convert *value* to a finite ``float`` or return ``None``."""
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    if f != f or f in (float("inf"), float("-inf")):  # NaN / Inf guard
        return None
    return f


def _coerce_int(value: Any) -> Optional[int]:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _migrate_run(run_dir: Path) -> str:
    """Return a single-line status string describing the action taken.

    Possible prefixes:
      ``[ok]``      — already migrated, no action.
      ``[skip]``    — required source data missing (no run_snapshot.json
                       or no cost_summary block).
      ``[migrate]`` — fields would be written (or were written, depending
                       on caller's --apply flag).
      ``[error]``   — could not write run_meta.json after applying.
    """
    meta_path = run_dir / "run_meta.json"
    snap_path = run_dir / "run_snapshot.json"
    meta = _load_json(meta_path)
    if meta is None:
        return f"[skip] {run_dir}: run_meta.json missing or unreadable"
    # Pre-check: already migrated?
    has_usd = meta.get("total_cost_usd") is not None
    has_legacy = meta.get("total_cost") is not None
    has_tokens = meta.get("total_tokens") is not None
    if has_usd and has_tokens:
        return f"[ok]   {run_dir}: total_cost_usd + total_tokens already present"
    snap = _load_json(snap_path)
    if snap is None:
        return f"[skip] {run_dir}: run_snapshot.json missing — cannot back-fill"
    cs = snap.get("cost_summary")
    if not isinstance(cs, dict):
        return f"[skip] {run_dir}: run_snapshot.cost_summary missing"

    new_meta = dict(meta)
    changes: list[str] = []

    if not has_usd:
        usd = _coerce_float(cs.get("total_cost_usd"))
        if usd is not None:
            new_meta["total_cost_usd"] = usd
            changes.append(f"total_cost_usd={usd}")

    if not has_legacy:
        legacy = _coerce_float(cs.get("total_cost"))
        if legacy is not None:
            new_meta["total_cost"] = legacy
            changes.append(f"total_cost={legacy}")

    if not has_tokens:
        tokens = _coerce_int(cs.get("total_tokens"))
        if tokens is not None:
            new_meta["total_tokens"] = tokens
            changes.append(f"total_tokens={tokens}")

    src = str(cs.get("cost_source") or "").strip()
    if src and "cost_source" not in new_meta:
        new_meta["cost_source"] = src
        changes.append(f"cost_source={src!r}")

    if not changes:
        return f"[skip] {run_dir}: cost_summary present but no usable values"

    if _migrate_run.apply:  # type: ignore[attr-defined]
        try:
            _atomic_write_text(meta_path, json.dumps(new_meta, ensure_ascii=False, indent=2))
        except Exception as exc:
            return f"[error] {run_dir}: write failed: {exc}"
        return f"[migrate] {run_dir}: wrote {', '.join(changes)}"
    return f"[migrate] {run_dir}: would write {', '.join(changes)}"


# Default: dry-run.  ``main`` flips this when --apply is passed.
_migrate_run.apply = False  # type: ignore[attr-defined]


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Back-fill total_cost / total_cost_usd / total_tokens "
                    "into legacy run_meta.json files.",
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=Path(__file__).resolve().parents[1] / "saved_projects",
        help="Root directory containing run subdirectories. "
             "Defaults to <repo>/saved_projects/.",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Actually write changes to disk.  Without this flag, the "
             "script reports what WOULD be done (dry-run).",
    )
    args = parser.parse_args(argv)

    if not args.root.exists() or not args.root.is_dir():
        print(f"[fatal] root does not exist or is not a directory: {args.root}",
              file=sys.stderr)
        return 2

    _migrate_run.apply = bool(args.apply)  # type: ignore[attr-defined]

    print(f"[info] scanning {args.root} ({'APPLY' if args.apply else 'DRY-RUN'})")

    counters = {"ok": 0, "skip": 0, "migrate": 0, "error": 0}
    run_dirs = sorted(
        p for p in args.root.iterdir()
        if p.is_dir() and not p.name.startswith(".")
    )
    for run_dir in run_dirs:
        line = _migrate_run(run_dir)
        # Extract the bracket-prefix to update the counter.
        prefix = line[1:line.index("]")]
        counters[prefix] = counters.get(prefix, 0) + 1
        print(line)

    print(
        f"[summary] {sum(counters.values())} runs scanned: "
        f"{counters.get('migrate', 0)} migrated, "
        f"{counters.get('ok', 0)} already-ok, "
        f"{counters.get('skip', 0)} skipped, "
        f"{counters.get('error', 0)} errors."
    )
    if not args.apply and counters.get("migrate", 0) > 0:
        print("[info] re-run with --apply to write changes to disk.")
    return 0 if counters.get("error", 0) == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
