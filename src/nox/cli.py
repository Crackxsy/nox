"""Nox command-line entry point (`nox ...`). Thin: parses arguments and delegates to the process
entry points; no business logic lives here (ADR-002 process model)."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import typer

from nox import __version__

app = typer.Typer(add_completion=False, no_args_is_help=True, help="Nox — personal AI companion.")


@app.callback()
def _root(
    version: bool = typer.Option(False, "--version", help="Print version and exit."),
) -> None:
    if version:
        typer.echo(f"nox {__version__}")
        raise typer.Exit()


@app.command()
def core(
    profile: str | None = typer.Option(None, help="Security profile id (companion, coding, ...)."),
    no_voice: bool = typer.Option(False, "--no-voice", help="Do not spawn the voice worker."),
    user_config: Path | None = typer.Option(
        None, help="Path to user.yaml (default: paths.config)."
    ),
) -> None:
    """Run the core process in the foreground (normally spawned by the supervisor)."""
    from nox.app import run_core

    raise SystemExit(
        asyncio.run(run_core(profile=profile, voice=not no_voice, user_config=user_config))
    )


@app.command()
def shell() -> None:
    """Run the PySide6 desktop shell (pet window, tray, hotkeys)."""
    from nox.shell.app import run as shell_run

    raise SystemExit(shell_run())


@app.command()
def supervisor() -> None:
    """Run the supervisor (watchdog, kill-switch hotkey); it spawns core and shell."""
    from nox.supervisor.main import main as sup_main

    raise SystemExit(sup_main())


@app.command()
def dev(
    no_voice: bool = typer.Option(False, "--no-voice"),
    no_shell: bool = typer.Option(False, "--no-shell"),
    profile: str | None = typer.Option(None),
) -> None:
    """Developer mode: core (+ shell) in one console without the supervisor."""
    from nox.app import run_dev

    raise SystemExit(asyncio.run(run_dev(voice=not no_voice, shell=not no_shell, profile=profile)))


@app.command()
def doctor() -> None:
    """Check the environment (Python, config, database, providers, devices) and print a report."""
    from nox.app import run_doctor

    raise SystemExit(asyncio.run(run_doctor()))


@app.command()
def onboard() -> None:
    """Guided first-run setup: name, language, data/vault paths, optional secrets, mic/camera
    consent, AI backend (FR-15.1 minimal first start)."""
    from nox.onboarding.wizard import run_onboarding_cli

    raise SystemExit(asyncio.run(run_onboarding_cli()))


rl_app = typer.Typer(help="Rocket League Coach (Spec v0.3 EPIC-12).")
app.add_typer(rl_app, name="rl")


@rl_app.command("calibrate")
def rl_calibrate() -> None:
    """Capture one screenshot while Rocket League shows an in-match HUD and (re)calibrate the HUD
    regions (ST-12-04). Run this while a real match, freeplay, or a training pack is visible."""
    plugin_src = Path(__file__).resolve().parents[2] / "plugins" / "rl" / "src"
    if plugin_src.is_dir() and str(plugin_src) not in sys.path:
        sys.path.insert(0, str(plugin_src))
    from nox_plugin_rl import calibration

    state_path = Path.home() / ".nox" / "rl" / "calibration.json"
    result = calibration.calibrate(state_path=state_path)
    if result.calibrated:
        typer.echo(f"calibrated (state saved to {state_path})")
    else:
        typer.echo("uncalibrated - failing region(s):")
        for region, reason in result.failures.items():
            typer.echo(f"  {region}: {reason}")
        raise typer.Exit(code=1)


secrets_app = typer.Typer(help="Manage secrets in the Windows Credential Manager (never in files).")
app.add_typer(secrets_app, name="secrets")


@secrets_app.command("set")
def secrets_set(
    name: str = typer.Argument(..., help="Secret name, e.g. nox/obs/websocket_password"),
) -> None:
    """Store a secret. The value is read from a hidden prompt, never from the command line."""
    from nox.security.secrets import KeyringSecretStore

    value = typer.prompt("Value", hide_input=True)
    KeyringSecretStore().set(name, value)
    typer.echo(f"stored {name}")


@secrets_app.command("delete")
def secrets_delete(name: str) -> None:
    from nox.security.secrets import KeyringSecretStore

    KeyringSecretStore().delete(name)
    typer.echo(f"deleted {name}")


@secrets_app.command("check")
def secrets_check(name: str) -> None:
    """Report whether a secret exists (never prints the value)."""
    from nox.security.secrets import KeyringSecretStore

    typer.echo("present" if KeyringSecretStore().get(name) is not None else "missing")


def main() -> None:
    try:
        app()
    except KeyboardInterrupt:
        sys.exit(130)


if __name__ == "__main__":
    main()
