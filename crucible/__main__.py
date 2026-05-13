from __future__ import annotations

import os as _os

# v1.1.0: bind the run-correlation contextvar at the very start so every
# downstream emit (telemetry, structured logs, run_insights ledger) carries
# a consistent run_id.  When the WebUI spawned this process, the WebUI's own
# run_id was passed via CRUCIBLE_RUN_ID so the per-run Insights tab populates.
# Direct CLI invocations (`python -m crucible ...` / `python crucible/__main__.py`)
# fall back to a fresh UUID4 inside set_run_id.  Without this bridge, ledger
# rows would persist with run_id="" and v1.2.0 retrieval could not group them.
if __package__ == "crucible":
    from .run_correlation import set_run_id as _set_run_id
    from .cli import main
else:
    from run_correlation import set_run_id as _set_run_id  # type: ignore[no-redef]
    from cli import main  # type: ignore[no-redef]

try:
    _set_run_id(_os.environ.get("CRUCIBLE_RUN_ID") or None)
except Exception:
    # Correlation-id binding must never break the pipeline boot.
    pass


if __name__ == "__main__":
    raise SystemExit(main())
