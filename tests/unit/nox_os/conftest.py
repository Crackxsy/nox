"""os/tools/*.py are scripts, not a package (a folder named `os` would shadow the standard
library): the tests load them from their path."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

REPO = Path(__file__).resolve().parents[3]
TOOLS = REPO / "os" / "tools"
#: The programs O2's sub-stage puts into the image (/usr/local/lib/nox-os).
HARDENING = REPO / "os" / "pi-gen" / "stage-nox" / "03-hardening" / "files"


def load_tool(name: str, folder: Path = TOOLS) -> ModuleType:
    spec = importlib.util.spec_from_file_location(f"nox_os_tools_{name}", folder / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module
