from __future__ import annotations

# v1.1.2 (audit fix G1-1): bridge the WebUI / external ``CRUCIBLE_RUN_ID`` env
# var into the run-correlation ContextVar BEFORE any pipeline code (including
# ``crucible.cli.main``) runs.  Without this bridge the flat-launcher entry
# point silently re-introduces the same run_id desynchronisation the v1.1.2
# section_07 fix closed.  Any error_record emit before section_07 (most
# commonly resilience.py's retry-exhausted path) would otherwise write
# ``run_id=""`` into the Run Insights ledger and orphan it from the WebUI /
# saved-project artefacts.
#
# v1.1.9 (L1): shared helper ``init_run_correlation_from_env`` keeps all
# three entry points (this file, ``crucible/__main__.py``, and
# ``run_crucible_enhanced.py:main()``) in lockstep — see
# ``crucible/run_correlation.py``.
from crucible.run_correlation import init_run_correlation_from_env

init_run_correlation_from_env()

from crucible.cli import main  # noqa: E402  (intentional import order)


if __name__ == "__main__":
    raise SystemExit(main())
