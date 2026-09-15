"""``python -m daemon`` entry point: run the hourly scheduler."""

from __future__ import annotations

from .daemon import main

if __name__ == "__main__":
    raise SystemExit(main())
