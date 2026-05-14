"""
crucible.features.run_insights
==============================
Run Insights ledger — cross-run telemetry for output methods, error
records, direction-debate rejections, and (in Quant mode) runtime
parameters.  v1.1.0 ships *recording only*; v1.2.0 will add retrieval
and skill distillation.

Public API::

    from crucible.features.run_insights import get_recorder, reset_recorder
    from crucible.features.run_insights.schema import (
        EventKind, OutcomeStatus, extract_signals, compute_content_id,
    )

The recorder is a process-global lazy singleton.  Operators control its
behaviour entirely through env vars (see ``.env.example`` for the
``CRUCIBLE_RUN_INSIGHTS_*`` family).  Call sites never branch on
enablement: when the subsystem is disabled, ``get_recorder()`` returns a
no-op recorder whose emit methods silently return ``None``.

Architectural seams for v1.2+ (Cloudflare Workers + D1 + R2) are
documented in ``backends.py``'s module docstring.
"""
from __future__ import annotations

from .recorder import InsightsRecorder, get_recorder, reset_recorder
from .schema import (
    EventKind,
    InsightEvent,
    OutcomeStatus,
    SCHEMA_VERSION,
    build_env_fingerprint,
    classify_asset_category,
    compute_content_id,
    canonical_json,
    canonical_record_line,
    extract_signals,
    truncate_text,
    utc_now_iso,
)

__all__ = [
    "InsightsRecorder",
    "get_recorder",
    "reset_recorder",
    "EventKind",
    "InsightEvent",
    "OutcomeStatus",
    "SCHEMA_VERSION",
    "build_env_fingerprint",
    "classify_asset_category",
    "compute_content_id",
    "canonical_json",
    "canonical_record_line",
    "extract_signals",
    "truncate_text",
    "utc_now_iso",
]
