"""Pytest config: put the repo root on sys.path so `af` and `tests` import cleanly,
and make `tests.fakes` importable without depending on cwd."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
