"""`nox onboard`: the guided first start.

It asks, in this order: the interface language (so everything after it is in one language), what
Nox should be called, who the user is, where the data folder and the vault live, whether to
connect a streaming account, microphone and camera consent, and which AI backend to use - the last
one behind a live probe of the real providers, never an assumption.

Every step has a safe default and can be skipped. A skipped step writes nothing, so the value from
`config/defaults.yaml` keeps applying. The wizard only ever writes the user layer
(`%APPDATA%\\Nox\\user.yaml`) and the OS keyring; it never touches `config/defaults.yaml`, and a
credential never reaches a file.

The interactive driver is a thin shell around the pure functions below it. Everything it depends
on - the secret store, the AI probe, the paths - is injected, so a test never touches a real
keyring and never makes a network or subprocess call.
"""

from __future__ import annotations

import os
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

import typer

from nox.ai.config import AiConfig, load_ai_config
from nox.core.events import HealthStatus
from nox.paths import DEFAULTS_PATH

# The user-layer reader and writer live in `nox.settings.layers`, so this wizard and the
# dashboard's `config.set` write the very same file the very same way. Re-exported below, because
# both names are part of this module's published surface.
from nox.settings.layers import (
    default_user_config_path,
    load_existing_user_layer,
    write_user_config,
)

__all__ = [
    "DEFAULTS_PATH",
    "AiProbeResult",
    "OnboardingAnswers",
    "build_user_layer_patch",
    "default_user_config_path",
    "load_existing_user_layer",
    "probe_ai_backends",
    "render_capability_summary",
    "run_onboarding_cli",
    "store_secrets",
    "texts",
    "write_user_config",
]

AI_BACKEND_CLAUDE_CODE = "claude_code"
AI_BACKEND_OLLAMA_ONLY = "ollama"

#: The two interface languages. The first question picks one and everything else follows it.
LANGUAGES: tuple[str, ...] = ("de", "en")
DEFAULT_LANGUAGE = "de"

#: Where the dashboard lives once Nox runs, for the "what happens next" part of the summary.
DASHBOARD_URL = "http://127.0.0.1:47801/dashboard"
START_COMMAND = "nox supervisor"


# ---- wording ------------------------------------------------------------------------------------

#: Every line the wizard prints, in both languages. One run is entirely in one of them: mixing a
#: German prompt with an English hint is the fastest way to make a first start feel unfinished.
TEXTS: dict[str, dict[str, str]] = {
    "de": {
        "intro": (
            "Ersteinrichtung von Nox. Enter übernimmt den Vorschlag; jeder Schritt ist "
            "überspringbar."
        ),
        "nox_name": "Wie soll Nox heißen",
        "user_name": "Dein Name (optional)",
        "folders_intro": (
            "\nNox legt zwei Ordner an:\n"
            "  Datenordner - Datenbank, Modelle, Logs und Backups. Alles, was Nox selbst braucht.\n"
            "  Vault       - deine Notizen als Markdown-Dateien. Die gehören dir und lassen sich "
            "mit jedem Editor öffnen."
        ),
        "data_dir": "Datenordner",
        "vault_dir": "Vault-Ordner",
        "twitch_intro": (
            "\nTwitch verbindest du am besten später im Dashboard (Einstellungen -> Twitch, "
            "Anmeldung per Code).\nDas ist der kürzere Weg: du bestätigst die Anmeldung im "
            "Browser und musst nirgends einen Token abtippen."
        ),
        "twitch_manual": "Stattdessen jetzt schon einen Token von Hand eintragen?",
        "twitch_token": "Twitch OAuth-Token (oauth:...)",
        "twitch_bot": "Twitch-Benutzername des Bots",
        "twitch_client_id": "Twitch Client-ID (optional, leer lassen wenn unbekannt)",
        "obs_ask": "Passwort für die OBS-Fernsteuerung jetzt hinterlegen?",
        "obs_value": "OBS-Websocket-Passwort",
        "telegram_ask": "Telegram-Bot-Token jetzt hinterlegen?",
        "telegram_value": "Telegram-Bot-Token",
        "microphone": "Mikrofon verwenden?",
        "camera": "Kamera verwenden?",
        "probe_ask": "AI-Backends jetzt prüfen (Claude Code / Ollama)?",
        "probing": "\nPrüfe AI-Backends ...",
        "backend": "AI-Backend ({claude}/{ollama})",
        "summary_title": "Einrichtung abgeschlossen.",
        "summary_config": "Konfiguration",
        "summary_data": "Datenordner",
        "summary_vault": "Vault",
        "summary_mic": "Mikrofon",
        "summary_cam": "Kamera",
        "summary_voice": "Sprachpakete",
        "summary_backends": "AI-Backends",
        "summary_chosen": "gewähltes Backend",
        "summary_secrets": "Zugangsdaten",
        "summary_secrets_none": "keine",
        "summary_secrets_note": "nur im Windows-Anmeldeinformationsspeicher",
        "enabled": "an",
        "disabled": "aus",
        "installed": "vorhanden",
        "missing": "fehlt",
        "next_title": "So geht es weiter:",
        "next_start": f"  1. Nox starten:  {START_COMMAND}",
        "next_dashboard": f"  2. Dashboard öffnen:  {DASHBOARD_URL}",
        "next_index": (
            "  3. Beim ersten Start liest Nox deinen Vault einmal komplett ein. Das dauert je "
            "nach Größe etwa eine Minute; danach läuft es im Hintergrund mit."
        ),
        "next_rerun": "`nox onboard` kannst du jederzeit erneut ausführen.",
    },
    "en": {
        "intro": (
            "Nox first-run setup. Press Enter to accept a suggestion; every step is optional."
        ),
        "nox_name": "What should Nox be called",
        "user_name": "Your name (optional)",
        "folders_intro": (
            "\nNox uses two folders:\n"
            "  Data folder - database, models, logs and backups. Everything Nox needs itself.\n"
            "  Vault       - your notes as Markdown files. They are yours and open in any editor."
        ),
        "data_dir": "Data folder",
        "vault_dir": "Vault folder",
        "twitch_intro": (
            "\nThe easiest way to connect Twitch is later, in the dashboard "
            "(Settings -> Twitch, sign in with a code).\nYou confirm the login in your browser "
            "and never have to copy a token anywhere."
        ),
        "twitch_manual": "Enter a token by hand now instead?",
        "twitch_token": "Twitch OAuth token (oauth:...)",
        "twitch_bot": "Twitch user name of the bot",
        "twitch_client_id": "Twitch client id (optional, leave empty if unknown)",
        "obs_ask": "Store the password for the OBS remote control now?",
        "obs_value": "OBS websocket password",
        "telegram_ask": "Store a Telegram bot token now?",
        "telegram_value": "Telegram bot token",
        "microphone": "Use the microphone?",
        "camera": "Use the camera?",
        "probe_ask": "Check the AI backends now (Claude Code / Ollama)?",
        "probing": "\nChecking AI backends ...",
        "backend": "AI backend ({claude}/{ollama})",
        "summary_title": "Setup complete.",
        "summary_config": "configuration",
        "summary_data": "data folder",
        "summary_vault": "vault",
        "summary_mic": "microphone",
        "summary_cam": "camera",
        "summary_voice": "speech packages",
        "summary_backends": "AI backends",
        "summary_chosen": "chosen backend",
        "summary_secrets": "credentials",
        "summary_secrets_none": "none",
        "summary_secrets_note": "in the Windows credential store only",
        "enabled": "on",
        "disabled": "off",
        "installed": "installed",
        "missing": "missing",
        "next_title": "What happens next:",
        "next_start": f"  1. Start Nox:  {START_COMMAND}",
        "next_dashboard": f"  2. Open the dashboard:  {DASHBOARD_URL}",
        "next_index": (
            "  3. On the first start Nox reads your whole vault once. Depending on its size that "
            "takes about a minute; after that it keeps up in the background."
        ),
        "next_rerun": "You can run `nox onboard` again at any time.",
    },
}


def texts(language: str | None) -> dict[str, str]:
    """The wording for `language`, falling back to the default one."""
    return TEXTS.get((language or DEFAULT_LANGUAGE).lower(), TEXTS[DEFAULT_LANGUAGE])


# ---- secret store -------------------------------------------------------------------------------


class SecretStore(Protocol):
    def set(self, name: str, value: str) -> None: ...


def default_secret_store() -> SecretStore:
    from nox.security.secrets import KeyringSecretStore  # noqa: PLC0415 - keyring imports slowly

    return KeyringSecretStore()


# ---- AI backend probe ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class AiProbeResult:
    id: str
    status: HealthStatus
    reason: str

    @property
    def available(self) -> bool:
        return self.status is HealthStatus.AVAILABLE


async def probe_ai_backends(ai_config: AiConfig | None = None) -> list[AiProbeResult]:
    """Ask the real providers whether they work, the way `nox doctor` does.

    Never assume and never report success without checking: what the user picks here decides where
    their conversations go.
    """
    from nox.ai.providers.claude_code import ClaudeCodeProvider  # noqa: PLC0415
    from nox.ai.providers.ollama import OllamaProvider  # noqa: PLC0415

    config = ai_config or load_ai_config(DEFAULTS_PATH)
    results: list[AiProbeResult] = []
    for provider_id, provider in (
        (AI_BACKEND_CLAUDE_CODE, ClaudeCodeProvider(config.providers.claude_code)),
        (AI_BACKEND_OLLAMA_ONLY, OllamaProvider(config.providers.ollama)),
    ):
        try:
            info = await provider.health()
            results.append(AiProbeResult(id=provider_id, status=info.status, reason=info.reason))
        except Exception as exc:  # noqa: BLE001 - a probe must never crash the wizard
            results.append(
                AiProbeResult(
                    id=provider_id,
                    status=HealthStatus.UNAVAILABLE,
                    reason=f"probe failed: {type(exc).__name__}: {exc}",
                )
            )
    return results


AiProbeFn = Callable[[], Awaitable[list[AiProbeResult]]]


# ---- answers ------------------------------------------------------------------------------------


@dataclass(slots=True)
class OnboardingAnswers:
    """What the wizard collected.

    A field that is `None` means the step was skipped or declined, and the corresponding
    configuration key is left untouched so the defaults layer's value applies.
    """

    nox_name: str | None = None
    user_display_name: str | None = None
    ui_language: str | None = None  # "de" | "en"
    data_dir: Path | None = None
    vault_dir: Path | None = None
    microphone_enabled: bool | None = None
    camera_enabled: bool | None = None
    ai_backend: str | None = None  # AI_BACKEND_CLAUDE_CODE | AI_BACKEND_OLLAMA_ONLY
    twitch: tuple[str, str] | None = None  # (oauth token, bot user name)
    twitch_client_id: str | None = None
    obs_password: str | None = None
    telegram_bot_token: str | None = None


# ---- pure config-layer helpers ------------------------------------------------------------------


def build_user_layer_patch(answers: OnboardingAnswers) -> dict[str, Any]:
    """Only the keys the user actually set.

    A declined or skipped step contributes nothing, so the defaults layer's value keeps applying.
    """
    patch: dict[str, Any] = {}

    identity: dict[str, Any] = {}
    if answers.nox_name is not None:
        identity["name"] = answers.nox_name
    if answers.user_display_name:
        identity["user_display_name"] = answers.user_display_name
    if answers.ui_language is not None:
        identity["ui_language"] = answers.ui_language
    if identity:
        patch["identity"] = identity

    paths: dict[str, Any] = {}
    if answers.data_dir is not None:
        paths["data_dir"] = str(answers.data_dir)
    if answers.vault_dir is not None:
        paths["vault_dir"] = str(answers.vault_dir)
    if paths:
        patch["paths"] = paths

    capture: dict[str, Any] = {}
    if answers.microphone_enabled is not None:
        capture["microphone"] = answers.microphone_enabled
    if answers.camera_enabled is not None:
        capture["camera"] = answers.camera_enabled
    if capture:
        patch["privacy"] = {"capture": capture}

    if answers.ai_backend == AI_BACKEND_OLLAMA_ONLY:
        patch["ai"] = {
            "router": {"default_reasoner": "ollama", "fallback_chain": ["ollama", "rules"]},
            "providers": {"claude_code": {"enabled": False}},
        }
    elif answers.ai_backend is not None:
        patch["ai"] = {
            "router": {
                "default_reasoner": "claude_code",
                "fallback_chain": ["claude_code", "ollama", "rules"],
            },
            "providers": {"claude_code": {"enabled": True}},
        }
    return patch


def store_secrets(answers: OnboardingAnswers, store: SecretStore) -> list[str]:
    """Write only the credentials the user actually provided.

    Returns the names that were stored - never the values - for the summary. They are exactly the
    names the dashboard's Settings page manages, so a credential entered here and one entered
    there are the same entry.
    """
    stored: list[str] = []

    def keep(name: str, value: str | None) -> None:
        if value:
            store.set(name, value)
            stored.append(name)

    if answers.twitch is not None:
        oauth_token, bot_username = answers.twitch
        keep("nox/twitch/oauth_token", oauth_token)
        keep("nox/twitch/bot_username", bot_username)
    keep("nox/twitch/client_id", answers.twitch_client_id)
    keep("nox/obs/websocket_password", answers.obs_password)
    keep("nox/telegram/bot_token", answers.telegram_bot_token)
    return stored


# ---- summary ------------------------------------------------------------------------------------


def _import_ok(module: str) -> bool:
    import importlib  # noqa: PLC0415 - only the summary needs it

    try:
        importlib.import_module(module)
    except ImportError:
        return False
    except Exception:  # noqa: BLE001 - a broken optional install counts as unavailable too
        return False
    return True


def render_capability_summary(
    *,
    answers: OnboardingAnswers,
    ai_probes: list[AiProbeResult],
    stored_secret_names: list[str],
    user_config_path: Path,
) -> str:
    """What this installation can now do, and what to do next.

    No fake capabilities: every backend is reported as available, limited or unavailable, with the
    reason its probe gave.
    """
    t = texts(answers.ui_language)
    width = 16
    lines = ["", t["summary_title"], ""]

    def row(label: str, value: str) -> None:
        lines.append(f"  {label:<{width}}: {value}".rstrip())

    row(t["summary_config"], str(user_config_path))
    if answers.data_dir is not None:
        row(t["summary_data"], str(answers.data_dir))
    if answers.vault_dir is not None:
        row(t["summary_vault"], str(answers.vault_dir))
    row(t["summary_mic"], t["enabled"] if answers.microphone_enabled else t["disabled"])
    row(t["summary_cam"], t["enabled"] if answers.camera_enabled else t["disabled"])
    row(
        t["summary_voice"],
        ", ".join(
            f"{module}={t['installed'] if _import_ok(module) else t['missing']}"
            for module in ("faster_whisper", "sounddevice", "piper")
        ),
    )
    row(t["summary_backends"], "")
    for probe in ai_probes:
        lines.append(f"    - {probe.id}: {probe.status.value} ({probe.reason})")
    row(t["summary_chosen"], answers.ai_backend or AI_BACKEND_CLAUDE_CODE)
    if stored_secret_names:
        row(t["summary_secrets"], f"{', '.join(stored_secret_names)} ({t['summary_secrets_note']})")
    else:
        row(t["summary_secrets"], t["summary_secrets_none"])

    lines += [
        "",
        t["next_title"],
        t["next_start"],
        t["next_dashboard"],
        t["next_index"],
        "",
        t["next_rerun"],
    ]
    return "\n".join(lines)


# ---- interactive driver -------------------------------------------------------------------------

#: Marks "the user asked for a probe" between the questions and the probe itself. It never reaches
#: `build_user_layer_patch`; `run_onboarding_cli` replaces it with a real choice or with None.
_PROBE_REQUESTED = "__probe__"


def _ask_language(existing_identity: dict[str, Any]) -> str:
    """The first question. Asked in both languages, because none has been chosen yet."""
    default = str(existing_identity.get("ui_language", DEFAULT_LANGUAGE))
    answer = typer.prompt("Sprache / Language (de/en)", default=default).strip().lower()
    return answer if answer in LANGUAGES else DEFAULT_LANGUAGE


def _ask_twitch(t: dict[str, str], answers: OnboardingAnswers) -> None:
    """Recommend the dashboard login; offer the manual token only as the explicit alternative."""
    typer.echo(t["twitch_intro"])
    if not typer.confirm(t["twitch_manual"], default=False):
        return
    oauth_token = typer.prompt(t["twitch_token"], hide_input=True, default="", show_default=False)
    bot_username = typer.prompt(t["twitch_bot"], default="", show_default=False)
    answers.twitch = (oauth_token, bot_username)
    answers.twitch_client_id = (
        typer.prompt(t["twitch_client_id"], default="", show_default=False) or None
    )


def _prompt_answers() -> OnboardingAnswers:
    """Ask every question, in one language, and collect the answers.

    A re-run defaults each step to what is already configured rather than silently resetting it;
    only a genuinely first run is seeded from `config/defaults.yaml`.
    """
    existing = load_existing_user_layer(default_user_config_path())
    existing_identity = existing.get("identity", {}) if isinstance(existing, dict) else {}
    existing_paths = existing.get("paths", {}) if isinstance(existing, dict) else {}

    answers = OnboardingAnswers()
    answers.ui_language = _ask_language(existing_identity)
    t = texts(answers.ui_language)
    typer.echo("")
    typer.echo(t["intro"])

    answers.nox_name = typer.prompt(t["nox_name"], default=existing_identity.get("name", "Nox"))
    display_name = typer.prompt(
        t["user_name"], default=existing_identity.get("user_display_name", ""), show_default=False
    )
    answers.user_display_name = display_name or None

    typer.echo(t["folders_intro"])
    appdata = Path(os.environ.get("APPDATA", str(Path.home() / "AppData" / "Roaming")))
    answers.data_dir = Path(
        typer.prompt(t["data_dir"], default=existing_paths.get("data_dir", str(appdata / "Nox")))
    )
    answers.vault_dir = Path(
        typer.prompt(
            t["vault_dir"],
            default=existing_paths.get("vault_dir", str(appdata / "Nox" / "vault")),
        )
    )

    _ask_twitch(t, answers)
    if typer.confirm(t["obs_ask"], default=False):
        answers.obs_password = typer.prompt(t["obs_value"], hide_input=True)
    if typer.confirm(t["telegram_ask"], default=False):
        answers.telegram_bot_token = typer.prompt(t["telegram_value"], hide_input=True)

    answers.microphone_enabled = typer.confirm(t["microphone"], default=True)
    answers.camera_enabled = typer.confirm(t["camera"], default=False)

    if typer.confirm(t["probe_ask"], default=True):
        answers.ai_backend = _PROBE_REQUESTED
    return answers


async def run_onboarding_cli(
    *,
    appdata: Path | str | None = None,
    secret_store: SecretStore | None = None,
    probe_fn: AiProbeFn | None = None,
) -> int:
    """The real `nox onboard`.

    Async so the AI probe can run without a nested event loop. Every dependency is injectable for
    tests; the production defaults are the real keyring and the real provider probe.
    """
    store = secret_store or default_secret_store()
    probe = probe_fn or probe_ai_backends

    answers = _prompt_answers()
    t = texts(answers.ui_language)

    ai_probes: list[AiProbeResult] = []
    if answers.ai_backend == _PROBE_REQUESTED:
        typer.echo(t["probing"])
        ai_probes = await probe()
        for result in ai_probes:
            typer.echo(f"  {result.id}: {result.status.value} ({result.reason})")
        recommended = next((r.id for r in ai_probes if r.available), AI_BACKEND_CLAUDE_CODE)
        chosen = typer.prompt(
            t["backend"].format(claude=AI_BACKEND_CLAUDE_CODE, ollama=AI_BACKEND_OLLAMA_ONLY),
            default=recommended,
        )
        answers.ai_backend = (
            AI_BACKEND_OLLAMA_ONLY if chosen == AI_BACKEND_OLLAMA_ONLY else AI_BACKEND_CLAUDE_CODE
        )
    else:
        answers.ai_backend = None  # declined: the defaults layer's choice applies, untouched

    user_config_path = default_user_config_path(appdata)
    write_user_config(user_config_path, build_user_layer_patch(answers))
    stored = store_secrets(answers, store)

    typer.echo(
        render_capability_summary(
            answers=answers,
            ai_probes=ai_probes,
            stored_secret_names=stored,
            user_config_path=user_config_path,
        )
    )
    return 0
