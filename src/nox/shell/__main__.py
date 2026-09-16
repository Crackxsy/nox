"""`python -m nox.shell [--runtime DIR]` starts the desktop shell (Process Model: nox-shell)."""

from __future__ import annotations

import argparse
from pathlib import Path

from nox.shell.app import run


def main() -> int:
    parser = argparse.ArgumentParser(prog="nox.shell", description="Nox desktop shell")
    parser.add_argument("--runtime", type=Path, default=None, help="runtime dir with session.token")
    args = parser.parse_args()
    return run(runtime_dir=args.runtime)


if __name__ == "__main__":
    raise SystemExit(main())
