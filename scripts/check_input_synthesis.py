"""Security Model §10 guard: input synthesis stays in two game-guarded files, nowhere else.

Run by CI (`python scripts/check_input_synthesis.py`) and by the unit tests. It scans every
Python file under `src/` and under all of `plugins/` (not only one plugin):

* The input-synthesis entry points - `SendInput`, `keybd_event`, `mouse_event`, `SetCursorPos` -
  may appear only in `src/nox/desktop/keyboard.py` and `src/nox/desktop/computer_use.py`, each of
  which must keep its game guard (`GameRunningError` and an `if game_running:` check).
* Posting or sending keyboard or mouse-button window messages (`PostMessage`/`SendMessage` in a
  file that also names `WM_KEY*`, `WM_SYSKEY*`, `WM_CHAR` or `WM_*BUTTON*`) is synthesis too and
  is allowed nowhere.
* The third-party automation libraries and process-memory calls are forbidden everywhere.

Names are matched as whole words, so a pydantic model called `ChatSendInput` is not a hit. The
patterns are assembled from pieces so this file does not trip the scan it implements.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ALLOWED = ("src/nox/desktop/keyboard.py", "src/nox/desktop/computer_use.py")

SYNTHESIS = re.compile(
    r"\b(?:"
    + "|".join(["Send" + "Input", "keybd" + "_event", "mouse" + "_event", "SetCursor" + "Pos"])
    + r")\b"  # fmt: skip
)
MESSAGE_CALL = re.compile(r"\b(?:Post|Send)" + "Message" + r"[AW]?\b")
INPUT_MESSAGE = re.compile(r"\bWM_(?:KEY\w*|SYSKEY\w*|(?:SYS)?CHAR|\w*BUTTON\w*)\b")
FORBIDDEN = re.compile(
    "|".join(
        [
            "pydirect" + "input",
            "pyauto" + "gui",
            "ReadProcess" + "Memory",
            "WriteProcess" + "Memory",
        ]
    )  # fmt: skip
)


def sources(root: Path = ROOT) -> list[Path]:
    return sorted([*root.joinpath("src").rglob("*.py"), *root.joinpath("plugins").rglob("*.py")])


def problems(root: Path = ROOT) -> list[str]:
    found: list[str] = []
    for path in sources(root):
        rel = path.relative_to(root).as_posix()
        text = path.read_text(encoding="utf-8", errors="replace")
        if FORBIDDEN.search(text):
            found.append(f"{rel}: automation or process-memory API")
        if SYNTHESIS.search(text) and rel not in ALLOWED:
            found.append(f"{rel}: input synthesis outside {', '.join(ALLOWED)}")
        if MESSAGE_CALL.search(text) and INPUT_MESSAGE.search(text):
            found.append(f"{rel}: posts keyboard or mouse-button window messages")
    for rel in ALLOWED:
        text = root.joinpath(rel).read_text(encoding="utf-8")
        if "GameRunningError" not in text or "if game_running:" not in text:
            found.append(f"{rel}: the game guard is gone from a file allowed to synthesise input")
    return found


def main() -> int:
    found = problems()
    for line in found:
        print(f"::error::{line} (Security Model §10)")
    if not found:
        print("clean: input synthesis only in the two game-guarded files")
    return 1 if found else 0


if __name__ == "__main__":
    sys.exit(main())
