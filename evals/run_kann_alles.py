"""Entry point that works against any checkout: `--nox-src` picks the core under test.

    python evals/run_kann_alles.py --label after                       # this checkout
    python evals/run_kann_alles.py --label before --nox-src ../base/src  # a baseline checkout

The harness itself always comes from this folder, so both runs use the same tasks, checks and judge.
"""

from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    src = HERE.parent / "src"
    if "--nox-src" in args:
        index = args.index("--nox-src")
        src = Path(args[index + 1]).resolve()
        del args[index : index + 2]
    for path in (str(HERE), str(src)):
        if path not in sys.path:
            sys.path.insert(0, path)
    from kann_alles.cli import run

    return run(args, nox_src=src)


if __name__ == "__main__":
    sys.exit(main())
