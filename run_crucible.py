from __future__ import annotations

# v1.1.2 (audit fix G1-1): bridge the WebUI / external ``CRUCIBLE_RUN_ID`` env
# var into the run-correlation ContextVar BEFORE any pipeline code (including
# ``crucible.cli.main``) runs.  Without this bridge the flat-launcher entry
# point silently re-introduces the same run_id desynchronisation the v1.1.2
# section_07 fix closed (see ``crucible/__main__.py:19-23`` and
# ``run_crucible_enhanced.py:main()`` which both perform the same call).  Any
# error_record emit before section_07 (most commonly resilience.py's
# retry-exhausted path) would otherwise write ``run_id=""`` into the
# Run Insights ledger and orphan it from the WebUI / saved-project artefacts.
import os as _os

from crucible.run_correlation import set_run_id as _set_run_id

# v1.1.2 (sixth-pass H-3): strip the env var BEFORE the ``or None`` fall-
# through.  Without ``.strip()``, a misconfigured ``CRUCIBLE_RUN_ID="   "``
# (three-space) is truthy and bypasses ``set_run_id``'s own ``.strip()`` +
# UUID fallback; the run_correlation ContextVar then carries whitespace and
# every downstream artefact joins on a value that looks empty but is not.
_set_run_id((_os.environ.get("CRUCIBLE_RUN_ID") or "").strip() or None)

from crucible.cli import main  # noqa: E402  (intentional import order)


if __name__ == "__main__":
    raise SystemExit(main())
