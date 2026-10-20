"""The `nox` command line.

Thin by design: it parses arguments and hands over to the process entry points. Every command here
is one call into `nox.entrypoints`, `nox.supervisor` or `nox.shell` - no business logic lives in
this module, so `nox core` and `python -m nox.app` cannot drift apart.
"""

from __future__ import annotations

import contextlib
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

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
        None, help="Path to your user.yaml. Defaults to the one `nox where` shows."
    ),
) -> None:
    """Run the core process in the foreground (normally the supervisor spawns it)."""
    import asyncio  # noqa: PLC0415 - imported per command so `nox --help` stays instant

    from nox.entrypoints import run_core  # noqa: PLC0415

    raise SystemExit(
        asyncio.run(run_core(profile=profile, voice=not no_voice, user_config=user_config))
    )


@app.command()
def where() -> None:
    """Show where Nox keeps its files on this machine and how it stores secrets."""
    from nox.core.config import load_config  # noqa: PLC0415
    from nox.paths import defaults_path, nox_home, resolve_config_paths  # noqa: PLC0415
    from nox.security.secrets import (  # noqa: PLC0415
        recorded_secret_backend,
        resolve_secret_backend,
    )

    defaults, user = resolve_config_paths()
    configured = load_config(defaults, user).security.secret_store
    # Looking never decides: the choice is made by the core or the first stored secret.
    backend = resolve_secret_backend(configured, record=False)
    recorded = recorded_secret_backend()
    secrets = {
        "keyring": "OS keyring",
        "file": f"owner-only file {nox_home() / 'secrets.json'}",
        "unavailable": "OS keyring - NOT reachable right now; the PIN cannot be checked",
    }[backend]
    rows = [
        ("Nox folder", str(nox_home())),
        ("Settings", str(user) if user else f"{nox_home() / 'user.yaml'} (not created yet)"),
        ("Defaults", str(defaults_path())),
        ("Secrets", secrets if configured == "auto" else f"{secrets} (set in user.yaml)"),
    ]
    if configured != "auto" and recorded is not None and recorded != configured:
        rows.append(
            (
                "Warning",
                f"user.yaml says {configured!r}, but the secrets were kept in {recorded!r} so far; "
                "they were not moved",
            )
        )
    width = max(len(label) for label, _ in rows)
    for label, value in rows:
        typer.echo(f"{label.ljust(width)}  {value}")


@app.command()
def pair(
    name: str = typer.Option("", help="A name for the new device, e.g. 'Kitchen tablet'."),
) -> None:
    """Pair a phone, tablet or PC in the home network: prints a QR code and a link to open."""
    from nox.core.config import load_config  # noqa: PLC0415
    from nox.lan.terminal import PairingError, pair_in_terminal  # noqa: PLC0415
    from nox.paths import resolve_config_paths  # noqa: PLC0415

    config = load_config(*resolve_config_paths())
    try:
        pair_in_terminal(
            Path(config.paths.runtime_dir),
            name=name,
            language=config.identity.ui_language,
            ask_pin=lambda prompt: typer.prompt(prompt, hide_input=True),
            say=typer.echo,
        )
    except PairingError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from exc


@app.command()
def devices() -> None:
    """List the phones, tablets and PCs paired with this Nox."""
    from nox.lan.terminal import PairingError, devices_in_terminal  # noqa: PLC0415

    config = _config()
    try:
        devices_in_terminal(
            Path(config.paths.runtime_dir), language=config.identity.ui_language, say=typer.echo
        )
    except PairingError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from exc


@app.command()
def unpair(
    device: str = typer.Argument(..., help="The device's name, or the start of its id."),
) -> None:
    """Remove a paired device - a lost phone, say. Its open connections end at once."""
    from nox.lan.terminal import PairingError, unpair_in_terminal  # noqa: PLC0415

    config = _config()
    try:
        unpair_in_terminal(
            Path(config.paths.runtime_dir),
            device,
            language=config.identity.ui_language,
            say=typer.echo,
        )
    except PairingError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from exc


def _config() -> Any:
    from nox.core.config import load_config  # noqa: PLC0415
    from nox.paths import resolve_config_paths  # noqa: PLC0415

    return load_config(*resolve_config_paths())


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


@contextlib.contextmanager
def _secret_store_errors() -> Iterator[None]:
    """A store that cannot be used is a sentence and exit code 1, not a traceback."""
    from nox.security.secret_names import SecretNameError, SecretStoreError  # noqa: PLC0415

    try:
        yield
    except (SecretStoreError, SecretNameError) as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(1) from exc


@secrets_app.command("set")
def secrets_set(
    name: str = typer.Argument(..., help="Secret name, e.g. nox/obs/websocket_password"),
) -> None:
    """Store a secret. The value is read from a hidden prompt, never from the command line."""
    from nox.security.secrets import open_configured_secret_store  # noqa: PLC0415

    value = typer.prompt("Value", hide_input=True)
    with _secret_store_errors():
        open_configured_secret_store().set(name, value)
    typer.echo(f"stored {name}")


@secrets_app.command("delete")
def secrets_delete(name: str = typer.Argument(..., help="Secret name to remove.")) -> None:
    """Remove a stored secret."""
    from nox.security.secrets import open_configured_secret_store  # noqa: PLC0415

    with _secret_store_errors():
        open_configured_secret_store().delete(name)
    typer.echo(f"deleted {name}")


@secrets_app.command("check")
def secrets_check(name: str = typer.Argument(..., help="Secret name to look for.")) -> None:
    """Report whether a secret exists. The value is never printed."""
    from nox.security.secrets import open_configured_secret_store  # noqa: PLC0415

    with _secret_store_errors():
        present = open_configured_secret_store().get(name) is not None
    typer.echo("present" if present else "missing")


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


sandbox_app = typer.Typer(help="The command sandbox (Windows: AppContainer access rights).")
app.add_typer(sandbox_app, name="sandbox")


@sandbox_app.command("reset")
def sandbox_reset() -> None:
    """Remove every access right Nox granted its sandbox on runtime folders (also run on
    uninstall). Harmless to run twice; Nox grants them again when it next needs them."""
    from nox.sandbox.windows import RuntimeGrants  # noqa: PLC0415

    grants = RuntimeGrants()
    # The legacy check reads the manifest, so it runs before `reset` empties it.
    legacy = grants.migrate_legacy(force=True)
    removed = grants.reset()
    typer.echo(f"removed {removed} sandbox access right(s) and {legacy} legacy one(s)")


firewall_app = typer.Typer(
    help="Windows: the network rules that keep the sandbox off loopback and the LAN."
)
sandbox_app.add_typer(firewall_app, name="firewall")


def _firewall(action: str) -> None:
    from nox.sandbox.wfp import main as wfp_main  # noqa: PLC0415

    code = wfp_main([action])
    if code:
        raise typer.Exit(code=code)


@firewall_app.command("install")
def sandbox_firewall_install() -> None:
    """Add the rules (needs an administrator prompt; the installer does this for you)."""
    _firewall("install")


@firewall_app.command("remove")
def sandbox_firewall_remove() -> None:
    """Remove the rules again (needs an administrator prompt; uninstall does this for you)."""
    _firewall("remove")


@firewall_app.command("show")
def sandbox_firewall_show() -> None:
    """Print every rule Nox installs: one line per filter, its container SID and addresses."""
    _firewall("show")


ai_app = typer.Typer(help="The local model: pick, download and check one that fits this machine.")
app.add_typer(ai_app, name="ai")


@ai_app.command("setup")
def ai_setup(
    dry_run: bool = typer.Option(False, "--dry-run", help="Measure and recommend only."),
    yes: bool = typer.Option(False, "--yes", "-y", help="Do not ask before the download."),
) -> None:
    """Set up a local model: measure RAM/VRAM, check Ollama, download, probe, save."""
    import asyncio  # noqa: PLC0415

    from nox.entrypoints import build_config  # noqa: PLC0415
    from nox.localai import cli as localai_cli  # noqa: PLC0415
    from nox.settings.layers import resolve_user_config_path  # noqa: PLC0415

    config = build_config(None, None)
    deps = localai_cli.build_cli_deps(config, resolve_user_config_path())
    raise typer.Exit(
        asyncio.run(
            localai_cli.run_local_ai_setup(
                deps=deps,
                dry_run=dry_run,
                assume_yes=yes,
                language=config.identity.ui_language,
            )
        )
    )


eval_app = typer.Typer(help="Benchmarks that measure what Nox can do (source checkout only).")
app.add_typer(eval_app, name="eval")


@eval_app.command(
    "kann-alles",
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
)
def eval_kann_alles(ctx: typer.Context) -> None:
    """Run the capability benchmark (tasks, answer quality, latency).

    Options go to the runner unchanged; `python evals/run_kann_alles.py --help` lists them.

    The benchmark lives next to the source (`evals/`), not in an installed package: it boots a core
    in a temporary world and is a development tool.
    """
    runner = Path(__file__).resolve().parents[2] / "evals" / "run_kann_alles.py"
    if not runner.is_file():
        typer.echo("The benchmark ships with the source checkout (evals/), not with this install.")
        raise typer.Exit(code=1)
    import runpy

    namespace = runpy.run_path(str(runner))
    raise typer.Exit(code=int(namespace["main"](list(ctx.args))))


def main() -> None:
    try:
        app()
    except KeyboardInterrupt:
        sys.exit(130)


if __name__ == "__main__":
    main()
