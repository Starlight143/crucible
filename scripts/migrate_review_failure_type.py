#!/usr/bin/env python3
# ruff: noqa: E402
"""
scripts/migrate_review_failure_type.py
======================================
v1.0.5 round 3 — one-shot migration for ``saved_projects/*/`` produced before
the structured ``ReviewReport.failure_type`` field existed.

In rounds 1-2 the saved-project README banner detected the
``QUALITY_LOOP_GAVE_UP`` early-stop stagnation marker via a substring search
on the review summary. Round 3 removed that substring fallback so the banner
goes through the structured field exclusively (and the Pydantic model now
rejects typo'd values at write time). That hardening would silently lose the
banner on saved bundles that predate the structured field unless they are
backfilled — that is what this script does.

Usage::

    python scripts/migrate_review_failure_type.py            # dry-run
    python scripts/migrate_review_failure_type.py --apply    # write changes
    python scripts/migrate_review_failure_type.py --root /path/to/saved_projects --apply

For each ``saved_projects/<run_id>/`` containing a ``review_report.json`` and
``run_meta.json`` the script:

1. parses ``review_report.json``;
2. if ``failure_type`` is already set to a known value → skip;
3. else if the review summary contains ``QUALITY_LOOP_GAVE_UP`` →
   set ``failure_type`` and mirror the value into ``run_meta.json``'s
   ``quality_loop_failure_type`` key.

The script is idempotent and never destructive — it only adds the missing
key. With ``--apply`` it writes the file back; without, it prints a
diff-style summary to stdout.

Exit codes:
- 0: nothing to do, or all migrations applied successfully
- 1: one or more bundles failed to parse / write
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from typing import List, Tuple


_ALLOWED_TYPES = frozenset({"QUALITY_LOOP_GAVE_UP"})


def _candidate_run_dirs(root: str) -> List[str]:
    if not os.path.isdir(root):
        return []
    out: List[str] = []
    for name in sorted(os.listdir(root)):
        sub = os.path.join(root, name)
        if not os.path.isdir(sub):
            continue
        if os.path.isfile(os.path.join(sub, "review_report.json")):
            out.append(sub)
    return out


def _detect_failure_type(summary: str) -> str:
    """Return the marker found in *summary*, or '' if none."""
    s = (summary or "").upper()
    for marker in _ALLOWED_TYPES:
        if marker in s:
            return marker
    return ""


def _process_bundle(run_dir: str, *, apply: bool) -> Tuple[bool, str]:
    """Returns (changed, message)."""
    review_path = os.path.join(run_dir, "review_report.json")
    meta_path = os.path.join(run_dir, "run_meta.json")
    try:
        with open(review_path, "r", encoding="utf-8") as f:
            review = json.load(f)
    except (OSError, json.JSONDecodeError) as exc:
        return False, f"[skip] {run_dir}: cannot parse review_report.json — {exc}"

    if not isinstance(review, dict):
        return False, f"[skip] {run_dir}: review_report.json is not an object"

    existing = (review.get("failure_type") or "").strip().upper()
    if existing in _ALLOWED_TYPES:
        return False, f"[ok]   {run_dir}: failure_type already set to {existing!r}"

    summary = str(review.get("summary", "") or "")
    detected = _detect_failure_type(summary)
    if not detected:
        return False, f"[ok]   {run_dir}: no marker in summary — nothing to migrate"

    if not apply:
        return True, f"[plan] {run_dir}: would set failure_type={detected!r}"

    review["failure_type"] = detected
    try:
        with open(review_path, "w", encoding="utf-8") as f:
            json.dump(review, f, ensure_ascii=False, indent=2)
    except OSError as exc:
        return False, f"[fail] {run_dir}: cannot write review_report.json — {exc}"

    # Mirror into run_meta.json if present.
    if os.path.isfile(meta_path):
        try:
            with open(meta_path, "r", encoding="utf-8") as f:
                meta = json.load(f)
        except (OSError, json.JSONDecodeError) as exc:
            return True, (
                f"[warn] {run_dir}: review_report.json migrated but "
                f"run_meta.json could not be read — {exc}"
            )
        if isinstance(meta, dict) and not (meta.get("quality_loop_failure_type") or "").strip():
            meta["quality_loop_failure_type"] = detected
            if "quality_passed" not in meta:
                meta["quality_passed"] = bool(review.get("passes", False))
            try:
                with open(meta_path, "w", encoding="utf-8") as f:
                    json.dump(meta, f, ensure_ascii=False, indent=2)
            except OSError as exc:
                return True, (
                    f"[warn] {run_dir}: review migrated, run_meta write failed — {exc}"
                )

    return True, f"[done] {run_dir}: failure_type={detected!r} (review + run_meta)"


def main(argv: List[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    parser.add_argument(
        "--root",
        default=os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "saved_projects"),
        help="Path to saved_projects/ (default: <repo>/saved_projects)",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Write changes (default: dry-run, print plan only)",
    )
    args = parser.parse_args(argv)

    root = os.path.abspath(args.root)
    bundles = _candidate_run_dirs(root)
    if not bundles:
        print(f"[done] no saved_projects bundles found under {root}")
        return 0

    changed = 0
    failed = 0
    for run_dir in bundles:
        was_change, msg = _process_bundle(run_dir, apply=args.apply)
        print(msg)
        if was_change:
            changed += 1
        if msg.startswith("[fail]"):
            failed += 1

    suffix = "applied" if args.apply else "would be applied (dry-run)"
    print(
        f"\n[summary] {changed} bundle(s) {suffix}; "
        f"{failed} failure(s); {len(bundles) - changed - failed} unchanged."
    )
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
