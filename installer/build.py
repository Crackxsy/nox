"""Reproducible build pipeline. Produces `build/app/` - a self-contained, embedded-Python payload
the Inno Setup script (`installer/nox.iss`) turns into an installer.

Steps (in order, each idempotent - re-running `build.py` from a clean `build/` is the normal case):
1. Download the official Python 3.13 embeddable zip (pinned version, matches the dev venv) and
   unpack it into `build/runtime/`.
2. Fix `build/runtime/python31X._pth`: the embeddable distribution ships with `import site`
   commented out and no `Lib\\site-packages` on `sys.path`, so a plain `pip install` target would
   never be importable - uncomment `import site`, add the site-packages path explicitly, and add
   `..\\app`, the folder the `nox` package is copied to (step 5).
3. Install pip into that runtime from its pinned wheel (the embeddable zip has no pip of its
   own): the wheel runs itself (`pip.whl/pip install --no-index pip.whl`), so nothing is fetched
   from PyPI and no unpinned pip ever lands in the runtime. Both downloads are pinned by an
   immutable URL and SHA-256 (`PINNED_SHA256`) - a file whose hash differs is refused, cached or
   not.
4. Regenerate `installer/requirements.lock` via `uv export --extra shell --extra voice
   --extra voice-supertonic` (Supertonic 3 is the default voice; its ~400 MB of model files are
   downloaded after the user agrees, never shipped) - the locked, reproducible dependency set,
   NOT `pyproject.toml`, which this script never touches - *with* hashes, and `pip install
   --require-hashes --only-binary=:all:` everything in it (wheels only: an sdist's build
   dependencies would not be hash-checked) except the `-e .` self-reference (nox's own source is
   copied in step 5, not pip-installed, so the payload has no build backend / editable-install
   machinery).
5. Copy `src/nox`, `config/`, `plugins/`, `ui/pet/dist`, `ui/dashboard/dist` and the launcher
   `installer/start_nox.pyw` into `build/app/`.
6. Write the sandbox-rules helper and its Inno include into `build/sandbox-rules/`
   (`nox.sandbox.wfp_helper`: the installer's one elevated step, pinned by SHA-256).
7. Collect the license files into `build/legal/`: LICENSE, NOTICE, COPYING (GPL-3.0 - the
   bundled `voice` extra makes the installer a GPL-3.0 combined work), the third-party list, and
   SOURCE.txt - the written source offer naming the exact commit this build is made from.
8. Record sizes into `build/app/BUILD_INFO.txt` (installer size target: "< 400 MB without
   models").

Run with the **system** Python (`uv` lives there per ENGINEERING.md), not `.venv`:
    python installer/build.py
`--skip-download` reuses an already-unpacked `build/runtime/` (fast re-runs while iterating on
steps 4-6); `--python-version` overrides the pinned embeddable version.
"""

from __future__ import annotations

import argparse
import hashlib
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

#: Pinned to match the development venv (ENGINEERING.md: Python 3.13.14) so
#: compiled-extension wheels (ctranslate2, onnxruntime, PySide6) resolved against cp313 match.
DEFAULT_PYTHON_VERSION = "3.13.14"
EMBED_URL_TMPL = "https://www.python.org/ftp/python/{v}/python-{v}-embed-amd64.zip"
#: The pip wheel the runtime gets (files.pythonhosted.org URLs are immutable). It installs
#: itself with `--no-index` (a bootstrap script would fetch the newest pip from PyPI, unhashed).
PIP_WHEEL = "pip-26.2.1-py3-none-any.whl"
PIP_WHEEL_URL = (
    "https://files.pythonhosted.org/packages/f3/6e/"
    "1736e5b4ae2b778ef2f81c47d797de9f891d4d8acb047a24ca37a60294dd/" + PIP_WHEEL
)
#: SHA-256 of every file the build downloads, by file name. A version without an entry here
#: cannot be built until its hash is added (from python.org, checked by a person).
PINNED_SHA256 = {
    "python-3.13.14-embed-amd64.zip": (
        "90b4e5b9898b72d744650524bff92377c367f44bd5fbd09e3148656c080ad907"
    ),
    PIP_WHEEL: "71138adf1f4ca900cdb7d289c21b7494329f2332b6d85f0e1c42108c0384ed3e",
}
#: The public repository the written source offer points to.
SOURCE_REPO = "https://github.com/Crackxsy/nox"

REQUIREMENTS_LOCK = INSTALLER_DIR / "requirements.lock"
#: `{app}\runtime\python313._pth` -> `{app}\app`, where the `nox` package is copied to.
APP_PTH_ENTRY = "..\\app"
#: The launcher the shortcuts start (puts `app/` on `sys.path` itself and reports a failed start).
LAUNCHER = INSTALLER_DIR / "start_nox.pyw"
#: The node's launcher: chooses between this copy and the versions a node installed itself.
NODE_LAUNCHER = INSTALLER_DIR / "start_node.pyw"
#: The sandbox-rules helper and its Inno include (`nox.sandbox.wfp_helper`).
SANDBOX_RULES_DIR = BUILD_DIR / "sandbox-rules"
#: License texts and the written source offer, installed into `{app}` (GPL-3.0 section 6).
LEGAL_DIR = BUILD_DIR / "legal"
#: Repository files copied into `LEGAL_DIR` unchanged.
LEGAL_FILES = ("LICENSE", "NOTICE", "COPYING", "docs/THIRD_PARTY_LICENSES.md")


def log(msg: str) -> None:
    print(f"[build] {msg}", flush=True)  # noqa: T201 - a standalone CLI build script, not `src/nox`


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def download(url: str, dest: Path) -> Path:
    """`url` into `dest`, accepted only if its SHA-256 is the pinned one (`PINNED_SHA256`)."""
    expected = PINNED_SHA256.get(dest.name)
    if expected is None:
        raise RuntimeError(f"no pinned SHA-256 for {dest.name}; add it to PINNED_SHA256 first")
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists() and sha256_of(dest) == expected:
        log(f"cached: {dest.name} (SHA-256 ok)")
        return dest
    log(f"downloading {url}")
    part = dest.with_suffix(dest.suffix + ".part")
    urllib.request.urlretrieve(url, part)  # noqa: S310 - fixed https URLs, not user input
    actual = sha256_of(part)
    if actual != expected:
        part.unlink(missing_ok=True)
        raise RuntimeError(f"{dest.name}: SHA-256 {actual} is not the pinned {expected}")
    os.replace(part, dest)
    return dest


def fixed_pth_lines(lines: list[str]) -> list[str]:
    """The embeddable Python's `python3*._pth`, made usable: `import site` on, the site-packages
    folder, and the folder holding the `nox` package. A `._pth` file is the whole `sys.path` of an
    embedded Python - not even the working directory is added - so without the last line
    `import nox` failed in every installed copy, silently, because `pythonw` has no console."""
    fixed = ["import site" if line.strip() == "#import site" else line for line in lines]
    for entry in ("Lib\\site-packages", APP_PTH_ENTRY):
        if entry not in fixed:
            fixed.append(entry)
    return fixed


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
    pth_path.write_text("\n".join(fixed_pth_lines(lines)) + "\n", encoding="utf-8")
    log(f"fixed {pth_path.name}: site-packages and the app folder importable")
    return RUNTIME_DIR


def _isolated_env() -> dict[str, str]:
    """`PYTHONNOUSERSITE=1` is not optional: once `._pth` enables `import site`, an embeddable
    Python resolves the OS-standard per-user site-packages path purely from the Python version+
    arch tag - which, on a machine that also has a matching system/Store Python installed, is the
    *same* path that Python already uses for its own per-user packages. Without this, pip's
    "already installed elsewhere on sys.path" dedup logic can uninstall packages from that
    unrelated, shared location - silently, and without restoring them. Set unconditionally."""
    env = dict(os.environ)
    env["PYTHONNOUSERSITE"] = "1"
    return env


def step_bootstrap_pip(runtime_dir: Path) -> None:
    """Step 3: pip into the embedded runtime (it ships with none), from the pinned wheel only."""
    python_exe = runtime_dir / "python.exe"
    env = _isolated_env()
    probe = subprocess.run(
        [str(python_exe), "-c", "import pip"], capture_output=True, check=False, env=env
    )
    if probe.returncode == 0:
        log("pip already present in runtime")
        return
    wheel = download(PIP_WHEEL_URL, DOWNLOAD_CACHE / PIP_WHEEL)
    subprocess.run(pip_bootstrap_command(python_exe, wheel), check=True, env=env)
    log("pip bootstrapped into embedded runtime")


def pip_bootstrap_command(python_exe: Path, wheel: Path) -> list[str]:
    """pip, run from its own wheel, installing exactly that wheel - no index, no other file.

    `-c` with an explicit `sys.path` entry rather than `wheel/pip` as a script: the embedded
    runtime's `._pth` fixes `sys.path`, so a script's folder is not added to it. `sys.argv[0]`
    becomes the wheel's path: pip refuses to replace itself on Windows when it was started as
    `pip`/`pip.exe`."""
    code = (
        "import runpy, sys; sys.path.insert(0, sys.argv[1]); "
        "sys.argv = sys.argv[1:]; runpy.run_module('pip', run_name='__main__')"
    )
    return [
        str(python_exe),
        "-c",
        code,
        str(wheel),
        "install",
        "--no-index",
        "--no-deps",
        "--no-warn-script-location",
        str(wheel),
    ]


def step_export_lock() -> Path:
    """Step 4a: `uv export` - the locked, reproducible dependency set (not pyproject.toml)."""
    uv = shutil.which("uv") or sys.executable  # `python -m uv` also works (ENGINEERING.md)
    cmd = [uv, "export"] if shutil.which("uv") else [sys.executable, "-m", "uv", "export"]
    cmd += ["--color", "never", "--frozen", "--extra", "shell", "--extra", "voice"]
    cmd += ["--extra", "voice-supertonic"]
    log(f"exporting locked dependencies: {' '.join(cmd)}")
    result = subprocess.run(cmd, cwd=REPO_ROOT, capture_output=True, text=True, check=True)
    REQUIREMENTS_LOCK.write_text(result.stdout, encoding="utf-8")
    log(f"wrote {REQUIREMENTS_LOCK} ({len(result.stdout.splitlines())} lines)")
    return REQUIREMENTS_LOCK


def filtered_requirements(text: str) -> list[str]:
    """The requirement entries of a hashed `uv export`, without `-e .` and without comments.

    An entry spans its continuation lines (`pkg==1.0 \\` followed by `--hash=...` lines); each
    returned entry is one joined line, hashes included."""
    entries: list[str] = []
    current = ""
    for raw in text.splitlines():
        line = raw.strip()
        if not current and (not line or line.startswith("#")):
            continue
        continued = line.endswith("\\")
        stripped = line.removesuffix("\\").strip()
        current = f"{current} {stripped}".strip()
        if continued:
            continue
        if not current.startswith("-e "):
            entries.append(current)
        current = ""
    if current and not current.startswith("-e "):
        entries.append(current)
    return entries


def step_install_dependencies(runtime_dir: Path, lock_path: Path) -> None:
    """Step 4b: pip-install everything in the lock file except `-e .` (nox's own source is
    copied, not pip-installed - the embedded runtime never needs a build backend). Every package
    must match one of its locked hashes (`--require-hashes`), and only wheels are accepted
    (`--only-binary=:all:`): an sdist would pull its build dependencies unhashed."""
    deps = filtered_requirements(lock_path.read_text(encoding="utf-8"))
    if not deps or any("--hash=sha256:" not in dep for dep in deps):
        raise RuntimeError(f"{lock_path} has entries without hashes; export it again")
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
            "--require-hashes",
            "--only-binary=:all:",
            "-r",
            str(filtered),
        ],
        check=True,
        env=_isolated_env(),
    )
    filtered.unlink(missing_ok=True)
    log(f"installed {len(deps)} locked, hash-checked dependencies into the embedded runtime")


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
    shutil.copy2(LAUNCHER, APP_DIR / LAUNCHER.name)
    shutil.copy2(NODE_LAUNCHER, APP_DIR / NODE_LAUNCHER.name)
    # The libraries this payload ships, by their lock: a node applies a code-only update
    # (nox.node.update) only when the update was built from the same uv.lock.
    lock = hashlib.sha256((REPO_ROOT / "uv.lock").read_bytes()).hexdigest()
    (APP_DIR / "uv.lock.sha256").write_text(lock + "\n", encoding="ascii")
    log(f"assembled app payload -> {APP_DIR}")


#: Step 6 inside the embedded runtime: the helper written by exactly the code the installer ships.
_WRITE_HELPER = (
    "import sys; from pathlib import Path; "
    "from nox.sandbox.wfp_helper import helper_sha256, write_build_files; "
    "write_build_files(Path(sys.argv[1])); print(helper_sha256())"
)


def step_sandbox_rules(runtime_dir: Path | None = None) -> None:
    """Step 6: the sandbox-rules helper script and the Inno include pinning its SHA-256.

    With `runtime_dir`, the embedded runtime writes them from the `nox` package copied in step 5
    (it has every dependency, so the build needs none of Nox's in the Python that runs it);
    without, this process does, from `src/`."""
    if SANDBOX_RULES_DIR.exists():
        shutil.rmtree(SANDBOX_RULES_DIR)
    if runtime_dir is not None:
        result = subprocess.run(
            [str(runtime_dir / "python.exe"), "-c", _WRITE_HELPER, str(SANDBOX_RULES_DIR)],
            capture_output=True,
            text=True,
            check=True,
            env=_isolated_env(),
        )
        digest = result.stdout.strip()
    else:
        sys.path.insert(0, str(REPO_ROOT / "src"))
        from nox.sandbox.wfp_helper import helper_sha256, write_build_files  # noqa: PLC0415

        write_build_files(SANDBOX_RULES_DIR)
        digest = helper_sha256()
    log(f"sandbox-rules helper -> {SANDBOX_RULES_DIR} (SHA-256 {digest})")


def _git(*args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=REPO_ROOT, capture_output=True, text=True, check=False
    )
    return result.stdout.strip() if result.returncode == 0 else ""


def source_offer(version: str, commit: str, tag: str) -> str:
    """SOURCE.txt: where the Corresponding Source of this exact build is, and how to build it."""
    ref = tag or commit or "HEAD"
    lines = [
        f"Nox {version} - Corresponding Source and written offer",
        "=" * 60,
        "",
        "This installer bundles the optional `voice` extra (piper-tts with espeak-ng, licensed",
        "GPL-3.0-or-later). The installer as a whole is therefore distributed under the GNU",
        "General Public License, version 3 or later (COPYING). Nox's own code is Apache-2.0",
        "(LICENSE); the third-party components and their licenses are listed in",
        "THIRD_PARTY_LICENSES.md, and NOTICE explains the combination.",
        "",
        "Corresponding Source (GPL-3.0 section 6):",
        f"  Nox source of exactly this build: {SOURCE_REPO}/tree/{commit or 'unknown'}",
    ]
    if tag:
        lines.append(f"  Release tag: {SOURCE_REPO}/releases/tag/{tag}")
    lines += [
        f"  Archive: {SOURCE_REPO}/archive/{ref}.zip",
        "  The bundled third-party packages are the exact versions pinned (with SHA-256 hashes)",
        "  in installer/requirements.lock of that commit; their sources are on https://pypi.org",
        "  (e.g. https://pypi.org/project/piper-tts/#files) and in their upstream repositories",
        "  (piper: https://github.com/OHF-Voice/piper1-gpl,",
        "  espeak-ng: https://github.com/espeak-ng/espeak-ng).",
        "",
        "How to build this installer from that source (Windows; Python 3.13, uv, Node.js and",
        "Inno Setup 6 installed):",
        "  1. npm ci && npm run build   (in ui/pet and in ui/dashboard)",
        "  2. python installer/build.py",
        "  3. ISCC.exe installer\\nox.iss",
        "",
        "Written offer: for at least three years after this version was distributed, and for as",
        "long as spare parts or support are offered for it, anyone may obtain the complete",
        "Corresponding Source of this installer - including the source of every GPL-licensed",
        "component in it - at no charge beyond the cost of physically performing the transfer,",
        f"by opening an issue at {SOURCE_REPO}/issues. The links above are the primary,",
        "equivalent way to get it at no charge.",
        "",
    ]
    return "\n".join(lines)


def installer_license(offer: str) -> str:
    """The text the setup wizard shows (`LicenseFile`): the licenses in short, then the offer."""
    head = [
        "Nox is free software. Please read this before installing.",
        "",
        "- Nox's own code: Apache License 2.0 (LICENSE, installed with Nox).",
        "- This installer bundles GPL-3.0-or-later components (Piper text-to-speech with",
        "  espeak-ng), so the installer as a whole is distributed under the GNU General Public",
        "  License, version 3 or later (COPYING, installed with Nox).",
        "- Third-party licenses: THIRD_PARTY_LICENSES.md and NOTICE (installed with Nox).",
        "",
        "The GPL grants you the rights to use, study, share and change this software. It comes",
        "WITHOUT ANY WARRANTY, to the extent permitted by applicable law.",
        "",
        "",
    ]
    return "\n".join(head) + offer


def step_legal(version: str) -> None:
    """Step 7: license texts and the written source offer, for `{app}` and the wizard."""
    if LEGAL_DIR.exists():
        shutil.rmtree(LEGAL_DIR)
    LEGAL_DIR.mkdir(parents=True)
    for name in LEGAL_FILES:
        src = REPO_ROOT / name
        if not src.is_file():
            raise RuntimeError(f"{name} is missing - the installer must ship it")
        shutil.copy2(src, LEGAL_DIR / Path(name).name)
    commit = _git("rev-parse", "HEAD")
    tag = _git("describe", "--tags", "--exact-match", "HEAD")
    offer = source_offer(version, commit, tag)
    (LEGAL_DIR / "SOURCE.txt").write_text(offer, encoding="ascii", newline="\r\n")
    (LEGAL_DIR / "INSTALLER_LICENSE.txt").write_text(
        installer_license(offer), encoding="ascii", newline="\r\n"
    )
    log(f"license files and source offer -> {LEGAL_DIR} (commit {commit or 'unknown'})")


def _dir_size_mb(path: Path) -> float:
    if not path.exists():
        return 0.0
    total = sum(f.stat().st_size for f in path.rglob("*") if f.is_file())
    return total / (1024 * 1024)


def step_record_sizes(python_version: str) -> None:
    """Step 6: honest size accounting against the "< 400 MB without models" target."""
    runtime_mb = _dir_size_mb(RUNTIME_DIR)
    app_mb = _dir_size_mb(APP_DIR)
    total_mb = runtime_mb + app_mb
    info = (
        f"Nox build payload\n"
        f"python_version={python_version}\n"
        f"runtime_mb={runtime_mb:.1f}\n"
        f"app_mb={app_mb:.1f}\n"
        f"total_mb={total_mb:.1f}\n"
        f"target_mb=400 (no models bundled)\n"
    )
    (APP_DIR / "BUILD_INFO.txt").write_text(info, encoding="utf-8")
    log(info.replace("\n", " | "))


def app_version() -> str:
    """Nox's version, as `nox.iss` names the installer."""
    for line in (INSTALLER_DIR / "nox.iss").read_text(encoding="ascii").splitlines():
        if line.startswith("#define MyAppVersion "):
            return line.split('"')[1]
    raise RuntimeError("nox.iss has no MyAppVersion")


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
    step_sandbox_rules(runtime_dir)
    step_legal(app_version())
    step_record_sizes(args.python_version)
    log("done: build/app/ is ready for installer/nox.iss")


if __name__ == "__main__":
    main()
