from __future__ import annotations

if __package__ == "crucible":
    from .cli import main
else:
    from cli import main



if __name__ == "__main__":
    raise SystemExit(main())
