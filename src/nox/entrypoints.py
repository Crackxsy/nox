"""How the core process is started and stopped from the outside.

`nox core`, `nox dev`, `nox doctor` and `python -m nox.app` all end up here. The composition root
itself (`nox.app`) knows nothing about signals, argument parsing or console output; this module
does, and nothing else does.

`run_doctor` writes through one `report` callable rather than calling `print` in nine places, so a
caller can capture the report and the linter needs no suppressions to allow it.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import nox
from nox.ai.base import AiProvider
from nox.ai.config import AiConfig
from nox.ai.providers.claude_code import ClaudeCodeProvider
from nox.ai.providers.ollama import OllamaProvider
from nox.ai.providers.rules import RulesProvider
from nox.app import NoxCore
from nox.core.config import ConfigError, NoxConfig, load_config
from nox.core.events import HealthStatus
from nox.core.logging import get_logger
from nox.core.state import PrivacyMode
from nox.paths import PROFILES_DIR, REPO_ROOT, resolve_config_paths
from nox.security.egress import EgressGuard
from nox.security.profiles import YamlProfileProvider

log = get_logger(__name__)

__all__ = [
    "Reporter",
    "build_config",
    "main",
    "run_core",
    "run_dev",
    "run_doctor",
    "store_python_note",
]

#: Where `run_doctor` sends a line. The default writes to standard output.
Reporter = Callable[[str], None]

#: The optional speech packages `nox doctor` reports on. A missing one is a warning, never an
#: error: Nox runs without speech, it just says so.
VOICE_MODULES: tuple[str, ...] = ("faster_whisper", "sounddevice", "piper", "PySide6")


def build_config(profile: str | None, user_config: Path | None) -> NoxConfig:
    """Load the configuration the way every Nox process loads it."""
    defaults, user = resolve_config_paths(user_config)
    return load_config(defaults, user, profile)


async def _run(core: NoxCore) -> int:
    """Start `core`, wait for a signal or a stop request, and shut it down again."""
    loop = asyncio.get_running_loop()
    stop = asyncio.Event()

    def request_stop(*_: Any) -> None:
        stop.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError, ValueError):
            loop.add_signal_handler(sig, request_stop)
    if sys.platform == "win32":
        signal.signal(signal.SIGINT, request_stop)

    try:
        await core.start()
    except Exception as exc:
        log.error("core.boot_failed", error=str(exc), type=type(exc).__name__)
        try:
            await core.stop()
        except Exception as stop_exc:  # noqa: BLE001 - a failed boot still releases what it built
            log.error(
                "core.stop_after_boot_failure", error=f"{type(stop_exc).__name__}: {stop_exc}"
            )
        return 1

    try:
        await _wait_for_shutdown(stop, core.shutdown_requested)
    finally:
        await core.stop()
    return 0


async def _wait_for_shutdown(*events: asyncio.Event) -> None:
    """Return as soon as any of `events` is set; leave no pending waiter behind."""
    waiters = [asyncio.ensure_future(event.wait()) for event in events]
    try:
        await asyncio.wait(waiters, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for waiter in waiters:
            waiter.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await waiter


async def run_core(
    *, profile: str | None = None, voice: bool = True, user_config: Path | None = None
) -> int:
    try:
        config = build_config(profile, user_config)
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)  # noqa: T201 - there is no logger yet
        return 2
    return await _run(NoxCore(config, voice=voice))


def _spawn_dev_shell(config: NoxConfig) -> subprocess.Popen[bytes]:
    """Start the desktop shell beside a developer-mode core, pointed at its runtime directory."""
    env = dict(os.environ)
    env["NOX_RUNTIME_DIR"] = str(config.paths.runtime_dir)
    return subprocess.Popen(  # noqa: S603 - fixed argv, never a shell
        [sys.executable, "-m", "nox.shell"], env=env, cwd=str(REPO_ROOT)
    )


async def run_dev(*, voice: bool = True, shell: bool = True, profile: str | None = None) -> int:
    """Core and shell in one console, without the supervisor."""
    config = build_config(profile, None)
    core = NoxCore(config, voice=voice)
    shell_process = _spawn_dev_shell(config) if shell else None
    try:
        return await _run(core)
    finally:
        if shell_process is not None and shell_process.poll() is None:
            shell_process.terminate()


def store_python_note() -> str | None:
    """Warn about a Python installed from the Microsoft Store, which redirects `%APPDATA%`.

    Writes land in the package's own cache, where tools outside the package - the permissions
    tool, the file explorer, an editor - cannot see them. Nox works, but the config and token
    paths it prints are not where the files actually are.
    """
    if sys.platform != "win32" or "WindowsApps" not in sys.base_prefix:
        return None
    return (
        "python from the Microsoft Store: %APPDATA% writes are redirected into the package cache, "
        "so the config and token paths above are not their real location. Install python.org "
        "Python and recreate .venv to avoid this."
    )


def _stdout(line: str) -> None:
    print(line)  # noqa: T201 - `nox doctor` is a console report; this is its only writer


async def run_doctor(report: Reporter = _stdout) -> int:
    """Report the environment without starting anything: config, folders, models, speech extras."""
    report(f"nox {nox.__version__} on {sys.platform}, python {sys.version.split()[0]}")
    try:
        config = build_config(None, None)
    except ConfigError as exc:
        report(f"[FAIL] config: {exc}")
        return 1
    report(f"[ ok ] config loaded (profile={config.profile_id or config.security.profile})")
    if (note := store_python_note()) is not None:
        report(f"[warn] {note}")
    for warning in config.warnings:
        report(f"[warn] {warning}")
    for line in await asyncio.to_thread(_folder_lines, config):
        report(line)
    await _report_providers(config, report)
    for module in VOICE_MODULES:
        report(_import_line(module))
    return 0


def _folder_lines(config: NoxConfig) -> list[str]:
    """Whether the two folders a user cares about exist. Checked in a thread: a cloud-synced or
    disconnected drive can make a single `exists()` take seconds."""
    return [
        f"[{' ok ' if Path(path).exists() else 'warn'}] {name}: {path}"
        for name, path in (
            ("vault", config.paths.vault_dir),
            ("database_dir", config.paths.database_dir),
        )
    ]


async def _report_providers(config: NoxConfig, report: Reporter) -> None:
    """Probe the model providers the way the core would: through the egress guard.

    Building a client without the guard here would be an outbound request that no privacy mode and
    no allow-list ever saw, in the one command whose whole purpose is to tell the truth about this
    installation.
    """
    ai_config = AiConfig.from_mapping(config.ai.model_dump())
    guard = _doctor_egress_guard(config)
    providers: list[AiProvider] = [
        RulesProvider(),
        OllamaProvider(ai_config.providers.ollama, client_factory=guard.client),
        ClaudeCodeProvider(ai_config.providers.claude_code),
    ]
    for provider in providers:
        info = await provider.health()
        mark = " ok " if info.status is HealthStatus.AVAILABLE else "warn"
        report(f"[{mark}] ai.{info.id}: {info.status.value} ({info.reason})")


def _doctor_egress_guard(config: NoxConfig) -> EgressGuard:
    """An egress guard for a command with no running core.

    It uses the configured profile and allow-lists and writes no audit entries: there is no
    session to audit against, and the report itself is the record.
    """
    profile = YamlProfileProvider(PROFILES_DIR).get(config.security.profile)
    return EgressGuard(
        profile=lambda: profile,
        privacy=_DoctorPrivacy(config),
        global_allowlist=tuple(config.security.egress_allowlist),
        loopback_allowlist=tuple(config.security.loopback_allowlist),
    )


class _DoctorPrivacy:
    """The privacy mode the configuration says this installation runs in."""

    def __init__(self, config: NoxConfig) -> None:
        self._mode = PrivacyMode(config.privacy.mode)

    @property
    def mode(self) -> PrivacyMode:
        return self._mode


def _import_line(module: str) -> str:
    try:
        __import__(module)
    except ImportError as exc:
        return f"[warn] {module}: not installed ({exc.name or module})"
    except Exception as exc:  # noqa: BLE001 - a broken optional install must still be reported
        return f"[warn] {module}: {type(exc).__name__}: {exc}"
    return f"[ ok ] {module} importable"


def main() -> None:
    """`python -m nox.app`: the same core as `nox core`, with argparse instead of typer."""
    import argparse  # noqa: PLC0415 - only this entry point parses arguments

    parser = argparse.ArgumentParser(prog="python -m nox.app")
    parser.add_argument("--profile", default=None, help="security profile id")
    parser.add_argument("--no-voice", action="store_true", help="do not spawn the voice worker")
    parser.add_argument("--user-config", default=None, help="path to user.yaml")
    args = parser.parse_args()
    code = asyncio.run(
        run_core(
            profile=args.profile,
            voice=not args.no_voice,
            user_config=Path(args.user_config) if args.user_config else None,
        )
    )
    raise SystemExit(code)
