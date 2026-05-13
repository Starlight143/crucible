"""
Tests for canonical JSON + content_id determinism.

The canonicalisation algorithm must produce byte-identical output across
Python (here) and the future Cloudflare Worker / JavaScript implementation,
so the same event computes the same content_id on both ends.
"""
from __future__ import annotations

import hashlib
import json
import math

from crucible.features.run_insights.schema import (
    canonical_json,
    compute_content_id,
)


def test_canonical_json_sorts_keys():
    a = {"b": 2, "a": 1}
    b = {"a": 1, "b": 2}
    assert canonical_json(a) == canonical_json(b)


def test_canonical_json_drops_content_id():
    e1 = {"a": 1, "b": 2}
    e2 = {"a": 1, "b": 2, "content_id": "sha256:fake"}
    assert canonical_json(e1) == canonical_json(e2)


def test_canonical_json_non_finite_floats_become_none():
    a = {"x": math.nan, "y": math.inf, "z": -math.inf}
    out = json.loads(canonical_json(a).decode("utf-8"))
    assert out == {"x": None, "y": None, "z": None}


def test_canonical_json_separators_compact():
    out = canonical_json({"a": [1, 2, 3]}).decode("utf-8")
    assert " " not in out  # compact separators (",", ":")


def test_canonical_json_unicode_preserved():
    out = canonical_json({"name": "黃金策略"}).decode("utf-8")
    assert "黃金策略" in out  # ensure_ascii=False


def test_canonical_json_array_order_preserved():
    out1 = canonical_json({"x": [3, 1, 2]}).decode("utf-8")
    out2 = canonical_json({"x": [3, 1, 2]}).decode("utf-8")
    assert out1 == out2
    assert "[3,1,2]" in out1


def test_compute_content_id_format():
    cid = compute_content_id({"a": 1})
    assert cid.startswith("sha256:")
    assert len(cid) == len("sha256:") + 64


def test_compute_content_id_deterministic():
    e = {"kind": "error", "msg": "boom", "n": 3}
    assert compute_content_id(e) == compute_content_id(e)


def test_compute_content_id_ignores_content_id_self():
    e = {"a": 1, "b": 2}
    cid1 = compute_content_id(e)
    e["content_id"] = cid1
    cid2 = compute_content_id(e)
    assert cid1 == cid2


def test_compute_content_id_changes_on_field_change():
    e1 = {"a": 1}
    e2 = {"a": 2}
    assert compute_content_id(e1) != compute_content_id(e2)


def test_known_vector_matches_sha256_of_canonical_bytes():
    """A frozen fixture: any change to canonicalisation that affects the
    sha256 output will break this assertion.  Useful as a guard against
    accidental algorithm drift.
    """
    e = {"a": 1, "b": "x"}
    expected = "sha256:" + hashlib.sha256(
        json.dumps(e, sort_keys=True, ensure_ascii=False,
                   separators=(",", ":"), allow_nan=False).encode("utf-8")
    ).hexdigest()
    assert compute_content_id(e) == expected


def test_nested_dicts_normalised():
    e = {"outer": {"nan": math.nan, "ok": 1}}
    out = json.loads(canonical_json(e).decode("utf-8"))
    assert out == {"outer": {"nan": None, "ok": 1}}
