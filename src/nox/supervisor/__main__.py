"""`python -m nox.supervisor` (Process Model entry)."""

from __future__ import annotations

import sys

from nox.supervisor.main import main

if __name__ == "__main__":
    sys.exit(main())
