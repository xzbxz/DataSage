"""Isolated entrypoint for the installed immutable runtime controller."""

from pathlib import Path
import sys


sys.path.insert(0, str(Path(__file__).resolve().parent))

from evaluation.current_runtime_launcher import main  # noqa: E402


if __name__ == "__main__":
    raise SystemExit(main())
