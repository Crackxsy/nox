"""SP-16 / ST-10-01: reproducible build pipeline. Produces `build/app/` - a self-contained,
embedded-Python payload the Inno Setup script (`installer/nox.iss`) turns into an installer.

Steps (in order, each idempotent - re-running `build.py` from a clean `build/` is the normal case):
1. Download the official Python 3.13 embeddable zip (pinned version, matches the dev venv) and
   unpack it into `build/runtime/`.
2. Fix `build/runtime/python31X._pth`: the embeddable distribution ships with `import site`
   commented out and no `Lib\\site-packages` on `sys.path`, so a plain `pip install` target would
   never be importable - uncomment `import site` and add the site-packages path explicitly.
3. Bootstrap pip into that runtime via `get-pip.py` (the embeddable zip has no pip of its own).
4. Regenerate `installer/requirements.lock` via `uv export --extra shell --extra voice` (the
   locked, reproducible dependency set - NOT `pyproject.toml`, which this script never touches)
   and `pip install` everything in it except the `-e .` self-reference (nox's own source is copied
   in step 5, not pip-installed, so the payload has no build backend / editable-install machinery).
5. Copy `src/nox`, `config/`, `plugins/`, `ui/pet/dist`, `ui/dashboard/dist` into `build/app/`.
6. Record sizes into `build/app/BUILD_INFO.txt` (installer size target: FR-15.4, SP-16 "< 400 MB
   without models").

Run with the **system** Python (`uv` lives there per ENGINEERING.md), not `.venv`:
    python installer/build.py
`--skip-download` reuses an already-unpacked `build/runtime/` (fast re-runs while iterating on
steps 4-6); `--python-version` overrides the pinned embeddable version.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import urllib.request
import zipfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
INSTALLER_DIR = REPO_ROOT / "installer"
BUILD_DIR = REPO_ROOT / "build"
RUNTIME_DIR = BUILD_DIR / "runtime"
APP_DIR = BUILD_DIR / "app"
DOWNLOAD_CACHE = INSTALLER_DIR / ".cache"

#: Pinned to match the dev venv (ENGINEERING.md: "venv `E:\Nox\repo\.venv` (Python 3.13.14)") so
#: compiled-extension wheels (ctranslate2, onnxruntime, PySide6) resolved against cp313 match.
DEFAULT_PYTHON_VERSION = "3.13.14"
EMBED_URL_TMPL = "https://www.python.org/ftp/python/{v}/python-{v}-embed-amd64.zip"
GET_PIP_URL = "https://bootstrap.pypa.io/get-pip.py"

REQUIREMENTS_LOCK = INSTALLER_DIR / "requirements.lock"


def log(msg: str) -> None:
    print(f"[build] {msg}", flush=True)  # noqa: T201 - a standalone CLI build script, not `src/nox`


def download(url: str, dest: Path) -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists() and dest.stat().st_size > 0:
        log(f"cached: {dest.name}")
        return dest
    log(f"downloading {url}")
    urllib.request.urlretrieve(url, dest)  # noqa: S310 - fixed https URLs, not user input
    return dest


def step_download_runtime(python_version: str, *, skip_download: bool) -> Path:
    """Steps 1-2: unpack the embeddable zip and fix the `._pth` file."""
    if skip_download and RUNTIME_DIR.exists() and any(RUNTIME_DIR.glob("python.exe")):
        log("--skip-download: reusing existing build/runtime/")
        return RUNTIME_DIR
    if RUNTIME_DIR.exists():
        shutil.rmtree(RUNTIME_DIR)
    RUNTIME_DIR.mkdir(parents=True)

    zip_name = f"python-{python_version}-embed-amd64.zip"
    zip_path = download(EMBED_URL_TMPL.format(v=python_version), DOWNLOAD_CACHE / zip_name)
    with zipfile.ZipFile(zip_path) as zf:
        zf.extractall(RUNTIME_DIR)
    log(f"unpacked embeddable Python {python_version} -> {RUNTIME_DIR}")

    pth_candidates = list(RUNTIME_DIR.glob("python3*._pth"))
    if not pth_candidates:
        raise RuntimeError(f"no python3*._pth found in {RUNTIME_DIR} - unexpected zip layout")
    pth_path = pth_candidates[0]
    lines = pth_path.read_text(encoding="utf-8").splitlines()
    fixed = []
    for line in lines:
        fixed.append("import site" if line.strip() == "#import site" else line)
    if "Lib\\site-packages" not in fixed:
        fixed.append("Lib\\site-packages")
    pth_path.write_text("\n".join(fixed) + "\n", encoding="utf-8")
    log(f"fixed {pth_path.name}: site-packages importable")
    return RUNTIME_DIR


def _isolated_env() -> dict[str, str]:
    """`PYTHONNOUSERSITE=1` is not optional: once `._pth` enables `import site`, an embeddable
    Python resolves the OS-standard per-user site-packages path purely from the Python version+
    arch tag - which, on this machine, is the *same* path the system/Store Python 3.13 already
    uses for its own per-user packages. Without this, pip's "already installed elsewhere on
    sys.path" dedup logic uninstalls (and can leave uninstalled) packages from that unrelated,
    shared location - a real incident hit during this task's own build run, restored by hand
    (see the SP-16 note's Result section) - never again silently."""
    env = dict(os.environ)
    env["PYTHONNOUSERSITE"] = "1"
    return env


def step_bootstrap_pip(runtime_dir: Path) -> None:
    """Step 3: get-pip.py into the embedded runtime (it ships with no pip)."""
    python_exe = runtime_dir / "python.exe"
    env = _isolated_env()
    probe = subprocess.run(
        [str(python_exe), "-c", "import pip"], capture_output=True, check=False, env=env
    )
    if probe.returncode == 0:
        log("pip already present in runtime")
        return
    get_pip = download(GET_PIP_URL, DOWNLOAD_CACHE / "get-pip.py")
    subprocess.run(
        [str(python_exe), str(get_pip), "--no-warn-script-location"], check=True, env=env
    )
    log("pip bootstrapped into embedded runtime")


def step_export_lock() -> Path:
    """Step 4a: `uv export` - the locked, reproducible dependency set (not pyproject.toml)."""
    uv = shutil.which("uv") or sys.executable  # `python -m uv` also works (ENGINEERING.md)
    cmd = [uv, "export"] if shutil.which("uv") else [sys.executable, "-m", "uv", "export"]
    cmd += ["--color", "never", "--extra", "shell", "--extra", "voice", "--no-hashes"]
    log(f"exporting locked dependencies: {' '.join(cmd)}")
    result = subprocess.run(cmd, cwd=REPO_ROOT, capture_output=True, text=True, check=True)
    REQUIREMENTS_LOCK.write_text(result.stdout, encoding="utf-8")
    log(f"wrote {REQUIREMENTS_LOCK} ({len(result.stdout.splitlines())} lines)")
    return REQUIREMENTS_LOCK


def step_install_dependencies(runtime_dir: Path, lock_path: Path) -> None:
    """Step 4b: pip-install everything in the lock file except `-e .` (nox's own source is
    copied, not pip-installed - the embedded runtime never needs a build backend)."""
    lines = lock_path.read_text(encoding="utf-8").splitlines()
    deps = [
        ln for ln in lines if ln.strip() and not ln.startswith("#") and not ln.startswith("-e ")
    ]
    filtered = INSTALLER_DIR / "_requirements.filtered.txt"
    filtered.write_text("\n".join(deps) + "\n", encoding="utf-8")

    python_exe = runtime_dir / "python.exe"
    subprocess.run(
        [
            str(python_exe),
            "-m",
            "pip",
            "install",
            "--no-warn-script-location",
            "-r",
            str(filtered),
        ],
        check=True,
        env=_isolated_env(),
    )
    filtered.unlink(missing_ok=True)
    log(f"installed {len(deps)} locked dependencies into the embedded runtime")


def step_assemble_app() -> None:
    """Step 5: copy nox's own code, config, plugins and the two built dashboards."""
    if APP_DIR.exists():
        shutil.rmtree(APP_DIR)
    APP_DIR.mkdir(parents=True)

    def copy_tree(src: Path, dst: Path) -> None:
        if not src.exists():
            log(f"WARNING: {src} does not exist, skipping")
            return
        shutil.copytree(src, dst, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))

    copy_tree(REPO_ROOT / "src" / "nox", APP_DIR / "nox")
    copy_tree(REPO_ROOT / "config", APP_DIR / "config")
    copy_tree(REPO_ROOT / "plugins", APP_DIR / "plugins")
    copy_tree(REPO_ROOT / "ui" / "pet" / "dist", APP_DIR / "ui" / "pet")
    copy_tree(REPO_ROOT / "ui" / "dashboard" / "dist", APP_DIR / "ui" / "dashboard")
    log(f"assembled app payload -> {APP_DIR}")


def _dir_size_mb(path: Path) -> float:
    if not path.exists():
        return 0.0
    total = sum(f.stat().st_size for f in path.rglob("*") if f.is_file())
    return total / (1024 * 1024)


def step_record_sizes(python_version: str) -> None:
    """Step 6: honest size accounting against the SP-16 "< 400 MB without models" target."""
    runtime_mb = _dir_size_mb(RUNTIME_DIR)
    app_mb = _dir_size_mb(APP_DIR)
    total_mb = runtime_mb + app_mb
    info = (
        f"Nox build payload\n"
        f"python_version={python_version}\n"
        f"runtime_mb={runtime_mb:.1f}\n"
        f"app_mb={app_mb:.1f}\n"
        f"total_mb={total_mb:.1f}\n"
        f"target_mb=400 (FR-15.4 / SP-16, no models bundled)\n"
    )
    (APP_DIR / "BUILD_INFO.txt").write_text(info, encoding="utf-8")
    log(info.replace("\n", " | "))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--python-version", default=DEFAULT_PYTHON_VERSION)
    parser.add_argument(
        "--skip-download", action="store_true", help="reuse an existing build/runtime/"
    )
    parser.add_argument(
        "--skip-lock-export",
        action="store_true",
        help="reuse the existing installer/requirements.lock instead of re-running `uv export`",
    )
    args = parser.parse_args()

    runtime_dir = step_download_runtime(args.python_version, skip_download=args.skip_download)
    step_bootstrap_pip(runtime_dir)
    reuse_lock = args.skip_lock_export and REQUIREMENTS_LOCK.exists()
    lock_path = REQUIREMENTS_LOCK if reuse_lock else step_export_lock()
    step_install_dependencies(runtime_dir, lock_path)
    step_assemble_app()
    step_record_sizes(args.python_version)
    log("done: build/app/ is ready for installer/nox.iss")


if __name__ == "__main__":
    main()
