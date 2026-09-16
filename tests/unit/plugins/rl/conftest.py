"""`plugins/rl/src` on `sys.path` for the unit tests importing `nox_plugin_rl` directly in-process
(mirrors `tests/unit/plugins/obs/conftest.py` - the real worker spawns it via
`nox.worker.plugin.load_entry`)."""

from __future__ import annotations

import sys
from pathlib import Path

_RL_SRC = Path(__file__).resolve().parents[4] / "plugins" / "rl" / "src"
if str(_RL_SRC) not in sys.path:
    sys.path.insert(0, str(_RL_SRC))
