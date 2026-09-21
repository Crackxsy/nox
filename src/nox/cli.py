"""The `nox` command line.

Thin by design: it parses arguments and hands over to the process entry points. Every command here
is one call into `nox.entrypoints`, `nox.supervisor` or `nox.shell` - no business logic lives in
this module, so `nox core` and `python -m nox.app` cannot drift apart.
"""

from __future__ import annotations

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
        None, help="Path to your user.yaml. Defaults to %APPDATA%\\Nox\\user.yaml."
    ),
) -> None:
    """Run the core process in the foreground (normally the supervisor spawns it)."""
    import asyncio  # noqa: PLC0415 - imported per command so `nox --help` stays instant

    from nox.entrypoints import run_core  # noqa: PLC0415

    raise SystemExit(
        asyncio.run(run_core(profile=profile, voice=not no_voice, user_config=user_config))
    )


@app.command()
def shell() -> None:
    """Run the desktop shell: the pet window, the tray icon and the hotkeys."""
    from nox.shell.app import run as shell_run  # noqa: PLC0415

    raise SystemExit(shell_run())


@app.command()
def supervisor() -> None:
    """Run the supervisor (watchdog and kill-switch hotkey); it spawns core and shell."""
    from nox.supervisor.main import main as supervisor_main  # noqa: PLC0415

    raise SystemExit(supervisor_main())


@app.command()
def dev(
    no_voice: bool = typer.Option(False, "--no-voice", help="Do not spawn the voice worker."),
    no_shell: bool = typer.Option(False, "--no-shell", help="Do not start the desktop shell."),
    profile: str | None = typer.Option(None, help="Security profile id."),
) -> None:
    """Developer mode: core and shell in one console, without the supervisor."""
    import asyncio  # noqa: PLC0415

    from nox.entrypoints import run_dev  # noqa: PLC0415

    raise SystemExit(asyncio.run(run_dev(voice=not no_voice, shell=not no_shell, profile=profile)))


@app.command()
def doctor() -> None:
    """Check this machine: Python, configuration, folders, AI backends and the speech extras."""
    import asyncio  # noqa: PLC0415

    from nox.entrypoints import run_doctor  # noqa: PLC0415

    raise SystemExit(asyncio.run(run_doctor()))


@app.command()
def onboard() -> None:
    """Set Nox up for the first time: name, language, folders, microphone, camera, AI backend."""
    import asyncio  # noqa: PLC0415

    from nox.onboarding.wizard import run_onboarding_cli  # noqa: PLC0415

    raise SystemExit(asyncio.run(run_onboarding_cli()))


rl_app = typer.Typer(help="Rocket League coaching helpers.")
app.add_typer(rl_app, name="rl")


@rl_app.command("calibrate")
def rl_calibrate() -> None:
    """Re-measure where the in-match display elements sit on your screen.

    Run this while a real match, freeplay or a training pack is visible: it takes one screenshot
    and works the regions out from it.
    """
    from nox.entrypoints import build_config  # noqa: PLC0415
    from nox.paths import PLUGINS_DIR  # noqa: PLC0415

    # The calibration code ships with the plugin, which is not an installed package. Its source
    # folder is put on the import path for this one command, rather than making the core import
    # plugin code at startup.
    plugin_src = PLUGINS_DIR / "rl" / "src"
    if plugin_src.is_dir() and str(plugin_src) not in sys.path:
        sys.path.insert(0, str(plugin_src))
    try:
        from nox_plugin_rl import calibration  # noqa: PLC0415
    except ImportError:
        typer.echo(f"The Rocket League plugin was not found at {plugin_src}.", err=True)
        raise typer.Exit(code=1) from None

    # The state belongs with the rest of Nox's data, wherever the user put it.
    state_path = Path(build_config(None, None).paths.data_dir) / "rl" / "calibration.json"
    result = calibration.calibrate(state_path=state_path)
    if result.calibrated:
        typer.echo(f"calibrated (saved to {state_path})")
        return
    typer.echo("not calibrated - these regions could not be measured:")
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
    from nox.security.secrets import KeyringSecretStore  # noqa: PLC0415

    value = typer.prompt("Value", hide_input=True)
    KeyringSecretStore().set(name, value)
    typer.echo(f"stored {name}")


@secrets_app.command("delete")
def secrets_delete(name: str = typer.Argument(..., help="Secret name to remove.")) -> None:
    """Remove a stored secret."""
    from nox.security.secrets import KeyringSecretStore  # noqa: PLC0415

    KeyringSecretStore().delete(name)
    typer.echo(f"deleted {name}")


@secrets_app.command("check")
def secrets_check(name: str = typer.Argument(..., help="Secret name to look for.")) -> None:
    """Report whether a secret exists. The value is never printed."""
    from nox.security.secrets import KeyringSecretStore  # noqa: PLC0415

    typer.echo("present" if KeyringSecretStore().get(name) is not None else "missing")


def _register_voice_commands() -> None:
    """Add the `nox voice` group, or one honest line saying why it is not there.

    The voice worker pulls in the audio stack, which is an optional install. Swallowing the import
    error made the whole command group vanish without a word, which to a user looks exactly like a
    typo in the command name.
    """
    try:
        from nox.worker.main import voice_app  # noqa: PLC0415 - optional extra
    except ImportError as exc:
        missing = exc.name or "the speech extras"

        @app.command("voice")
        def voice_unavailable() -> None:
            """Voice commands (needs the optional speech extras)."""
            typer.echo(
                f"The voice commands need the optional speech extras ({missing} is missing).\n"
                "Install them with:  pip install -e .[voice]",
                err=True,
            )
            raise typer.Exit(code=1)

        return
    app.add_typer(voice_app, name="voice")


_register_voice_commands()


def main() -> None:
    try:
        app()
    except KeyboardInterrupt:
        sys.exit(130)


if __name__ == "__main__":
    main()
