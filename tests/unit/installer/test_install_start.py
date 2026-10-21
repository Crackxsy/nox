"""Why an installed Nox did not start, kept from coming back:

* the embedded Python's `._pth` (its whole `sys.path`) did not name the folder holding `nox`, so
  `pythonw -m nox.supervisor` died on `import nox` without a word;
* `nox.paths` looked for `config/` one folder too high for the installed `app/nox` layout.

The launcher the shortcuts now start reports any failed start instead of failing silently.
Nothing here touches an installed copy; the layout is rebuilt in a temporary folder.
"""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

REPO = Path(__file__).resolve().parents[3]
LAUNCHER = REPO / "installer" / "start_nox.pyw"


def _load(path: Path, name: str) -> ModuleType:
    from importlib.machinery import SourceFileLoader

    loader = SourceFileLoader(name, str(path))
    spec = importlib.util.spec_from_loader(name, loader)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def test_pth_names_the_app_folder_and_site_packages() -> None:
    build = _load(REPO / "installer" / "build.py", "nox_installer_build")
    shipped = [
        "python313.zip",
        ".",
        "",
        "# Uncomment to run site.main() automatically",
        "#import site",
    ]
    fixed = build.fixed_pth_lines(shipped)
    assert "import site" in fixed and "#import site" not in fixed
    assert "Lib\\site-packages" in fixed
    assert "..\\app" in fixed  # {app}\runtime\..\app holds the nox package
    assert build.fixed_pth_lines(fixed) == fixed  # idempotent


def test_repo_root_finds_config_in_the_installed_layout(tmp_path: Path) -> None:
    app = tmp_path / "app"
    (app / "config").mkdir(parents=True)
    (app / "config" / "defaults.yaml").write_text("{}", encoding="utf-8")
    (app / "nox").mkdir()
    (app / "nox" / "paths.py").write_text(
        (REPO / "src" / "nox" / "paths.py").read_text(encoding="utf-8"), encoding="utf-8"
    )
    env = {k: v for k, v in os.environ.items() if k != "NOX_REPO_ROOT"}
    out = subprocess.run(
        [
            sys.executable,
            "-I",
            "-c",
            "import runpy; print(runpy.run_path('nox/paths.py')['REPO_ROOT'])",
        ],
        cwd=app,
        capture_output=True,
        text=True,
        check=True,
        env=env,
    )
    assert Path(out.stdout.strip()) == app


def _fake_app(tmp_path: Path, main_body: str) -> Path:
    app = tmp_path / "app"
    (app / "nox" / "supervisor").mkdir(parents=True)
    (app / "nox" / "__init__.py").write_text("", encoding="utf-8")
    (app / "nox" / "supervisor" / "__init__.py").write_text("", encoding="utf-8")
    (app / "nox" / "supervisor" / "main.py").write_text(main_body, encoding="utf-8")
    (app / "start_nox.pyw").write_text(LAUNCHER.read_text(encoding="utf-8"), encoding="utf-8")
    return app


def _launch(app: Path, local_appdata: Path) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "LOCALAPPDATA": str(local_appdata), "NOX_LAUNCH_NO_DIALOG": "1"}
    # `-I` mimics the embedded runtime: neither the script folder's parent nor the working
    # directory may be what makes `nox` importable - the launcher has to do it.
    return subprocess.run(
        [sys.executable, "-I", str(app / "start_nox.pyw")],
        cwd=app.parent,
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )


def test_launcher_starts_the_supervisor_from_its_own_folder(tmp_path: Path) -> None:
    app = _fake_app(tmp_path, "def main():\n    print('supervisor ran')\n    return 0\n")
    result = _launch(app, tmp_path / "lad")
    assert result.returncode == 0, result.stderr
    assert "supervisor ran" in result.stdout
    assert not (tmp_path / "lad" / "Nox" / "startup-error.log").exists()


def test_launcher_reports_a_failed_start(tmp_path: Path) -> None:
    app = _fake_app(tmp_path, "import a_dependency_that_is_missing\n\ndef main():\n    return 0\n")
    result = _launch(app, tmp_path / "lad")
    assert result.returncode == 1
    log = (tmp_path / "lad" / "Nox" / "startup-error.log").read_text(encoding="utf-8")
    assert "Nox could not start." in log and "a_dependency_that_is_missing" in log


@pytest.mark.parametrize("name", ["nox.iss"])
def test_every_shortcut_starts_the_launcher(name: str) -> None:
    script = (REPO / "installer" / name).read_text(encoding="utf-8")
    starts = [
        line for line in script.splitlines() if "pythonw.exe" in line and "Parameters" in line
    ]
    assert starts, "no shortcut found"
    # The core's launcher, or the node's (installer/start_node.pyw) - never `-m nox.supervisor`.
    launchers = ("start_nox.pyw", "start_node.pyw")
    assert all(
        any(launcher in line for launcher in launchers) and "-m nox.supervisor" not in line
        for line in starts
    )
