from __future__ import annotations

# v1.1.0: bind the run-correlation contextvar at the very start so every
# downstream emit (telemetry, structured logs, run_insights ledger) carries
# a consistent run_id.  When the WebUI spawned this process, the WebUI's own
# run_id was passed via CRUCIBLE_RUN_ID so the per-run Insights tab populates.
# Direct CLI invocations (`python -m crucible ...` / `python crucible/__main__.py`)
# fall back to a fresh UUID4 inside set_run_id.  Without this bridge, ledger
# rows would persist with run_id="" and v1.2.0 retrieval could not group them.
#
# v1.1.9 (L1): the three entry points (__main__, run_crucible, run_crucible_enhanced)
# now share ``init_run_correlation_from_env()`` so the strip-before-or-None
# rule stays in lockstep with set_run_id's own whitespace defence.
if __package__ == "crucible":
    from .run_correlation import init_run_correlation_from_env
    from .cli import main
else:
    from run_correlation import init_run_correlation_from_env  # type: ignore[no-redef]
    from cli import main  # type: ignore[no-redef]

init_run_correlation_from_env()


if __name__ == "__main__":
    raise SystemExit(main())
