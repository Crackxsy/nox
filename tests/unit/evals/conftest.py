"""Make the benchmark package (`evals/kann_alles`) importable for its unit tests."""

from __future__ import annotations

import sys
from pathlib import Path

EVALS = Path(__file__).resolve().parents[3] / "evals"
if str(EVALS) not in sys.path:
    sys.path.insert(0, str(EVALS))
