"""
features/run_insights/export.py
================================
CLI: bundle the local ``.crucible_insights/`` directory into a single
``.tar.gz`` archive for backup, transport, or future R2 upload.

The archive layout mirrors the future R2 object hierarchy::

    archive.tar.gz
        output.jsonl
        error.jsonl
        debate.jsonl
        params.jsonl
        blobs/<sha256_hex>.json
        manifest.json

``manifest.json`` records the archive build time, schema version, total
event counts per stream, and a checksum of each stream file — useful for
verifying integrity post-restore or before ingesting into D1.

Usage::

    python -m crucible.features.run_insights.export ./archive.tar.gz
    python -m crucible.features.run_insights.export --root /custom/dir ./archive.tar.gz
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import tarfile
from pathlib import Path
from typing import Any, Dict

# Tri-modal import (see recorder.py for the launcher matrix).  We use the
# canonical timestamp formatter from schema.py so manifest timestamps and
# event timestamps share the same "...Z" suffix (avoiding the v1.0 split
# where the manifest used "+00:00" while events used "Z").
try:
    from .schema import utc_now_iso as _utc_now_iso
except ImportError:  # pragma: no cover — flat-launcher fallback
    from schema import utc_now_iso as _utc_now_iso  # type: ignore[no-redef]


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    try:
        with open(path, "rb") as fh:
            for chunk in iter(lambda: fh.read(65536), b""):
                h.update(chunk)
    except OSError:
        return ""
    return h.hexdigest()


def _count_lines(path: Path) -> int:
    try:
        with open(path, "rb") as fh:
            return sum(1 for ln in fh if ln.strip())
    except OSError:
        return 0


def _build_manifest(root: Path) -> Dict[str, Any]:
    streams = ("output", "error", "debate", "params")
    files: Dict[str, Dict[str, Any]] = {}
    for s in streams:
        path = root / f"{s}.jsonl"
        if not path.exists():
            continue
        files[s] = {
            "filename": path.name,
            "lines": _count_lines(path),
            "sha256": _sha256_file(path),
            "bytes": path.stat().st_size,
        }
    blobs_dir = root / "blobs"
    blob_count = 0
    if blobs_dir.is_dir():
        blob_count = sum(1 for _ in blobs_dir.glob("*.json"))
    return {
        "schema_version": 1,
        "exported_at": _utc_now_iso(),
        "source_root": str(root),
        "streams": files,
        "blob_count": blob_count,
    }


def export_archive(root: Path, dest: Path) -> Dict[str, Any]:
    """Bundle *root* into *dest* (.tar.gz).  Returns the manifest dict."""
    if not root.exists():
        raise FileNotFoundError(f"insights root not found: {root}")
    if not root.is_dir():
        raise NotADirectoryError(f"insights root is not a directory: {root}")

    manifest = _build_manifest(root)

    dest.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(dest, "w:gz") as tar:
        # Add stream files first.
        for s in ("output", "error", "debate", "params"):
            path = root / f"{s}.jsonl"
            if path.exists():
                tar.add(path, arcname=path.name)
        # Add blobs dir if non-empty.
        blobs_dir = root / "blobs"
        if blobs_dir.is_dir():
            for blob in sorted(blobs_dir.glob("*.json")):
                tar.add(blob, arcname=f"blobs/{blob.name}")
        # Write manifest last.
        manifest_bytes = json.dumps(
            manifest, indent=2, ensure_ascii=False, sort_keys=True,
        ).encode("utf-8")
        info = tarfile.TarInfo(name="manifest.json")
        info.size = len(manifest_bytes)
        import io
        tar.addfile(info, io.BytesIO(manifest_bytes))
    return manifest


def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m crucible.features.run_insights.export",
        description=(
            "Bundle the .crucible_insights/ directory into a tar.gz archive. "
            "Archive layout matches the future Cloudflare R2 object hierarchy."
        ),
    )
    parser.add_argument(
        "destination",
        help="path to write the archive to (e.g. ./insights_2026-05-13.tar.gz)",
    )
    parser.add_argument(
        "--root",
        default=os.environ.get("CRUCIBLE_RUN_INSIGHTS_DIR", ".crucible_insights"),
        help="insights ledger directory (default: $CRUCIBLE_RUN_INSIGHTS_DIR or .crucible_insights)",
    )
    parser.add_argument(
        "--quiet", action="store_true", help="suppress progress output",
    )
    args = parser.parse_args(argv)

    root = Path(args.root).resolve()
    dest = Path(args.destination).resolve()

    try:
        manifest = export_archive(root, dest)
    except (FileNotFoundError, NotADirectoryError) as exc:
        print(f"export failed: {exc}", file=sys.stderr)
        return 2
    except OSError as exc:
        print(f"export I/O error: {exc}", file=sys.stderr)
        return 3

    if not args.quiet:
        total_events = sum(
            s.get("lines", 0) for s in manifest.get("streams", {}).values()
        )
        print(f"wrote {dest}")
        print(f"  total events: {total_events}")
        print(f"  blob count:   {manifest.get('blob_count', 0)}")
        for stream, info in manifest.get("streams", {}).items():
            print(f"    {stream}.jsonl: {info['lines']} lines, {info['bytes']} bytes")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
