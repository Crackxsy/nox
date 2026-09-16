"""`python -m nox.worker --service voice` entry point (Process Model: worker processes)."""

from __future__ import annotations

import sys

from nox.worker.main import main

if __name__ == "__main__":
    sys.exit(main())
