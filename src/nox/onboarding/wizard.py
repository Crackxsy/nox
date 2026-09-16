"""`nox onboard`: guided first-run setup (FR-15.1 "minimal first start", ST-21-03, Spec v1.0
Public Release §5).

Steps, in order: Nox name, UI language, data/vault path, optional Twitch/OBS/Telegram secrets
(stored only in the OS keyring, never in a file), microphone/camera capture consent, and an AI
backend choice backed by a live probe of the actual providers (no assumed/fake availability -
ENGINEERING.md "no fake implementations"). Every step has a safe default and can be declined; the
wizard writes only to the User config layer at ``%APPDATA%\\Nox\\user.yaml`` (ADR-010's four-layer
config: Defaults -> User -> Profile/Preset -> Runtime Override) and never touches
``config/defaults.yaml``.

The interactive driver (`run_onboarding_cli`) is a thin shell around pure, independently testable
functions below it - dependency injection (secret store, AI probe, paths) follows
ENGINEERING.md's "constructor injection, no module-level singletons" convention so tests never
touch a real keyring or make a real network/subprocess call.
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

# The user-layer reader/writer lives in `nox.settings.layers`, so this wizard and the dashboard's
# `config.set` write the very same file the very same way. Re-exported below because both names
# are part of this module's published surface.
from nox.settings.layers import (
    default_user_config_path,
    load_existing_user_layer,
    write_user_config,
)

REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULTS_PATH = REPO_ROOT / "config" / "defaults.yaml"

__all__ = [
    "OnboardingAnswers",
    "build_user_layer_patch",
    "default_user_config_path",
    "load_existing_user_layer",
    "probe_ai_backends",
    "render_capability_summary",
    "run_onboarding_cli",
    "store_secrets",
    "write_user_config",
]

AI_BACKEND_CLAUDE_CODE = "claude_code"
AI_BACKEND_OLLAMA_ONLY = "ollama"


# ---- secret store protocol (matches nox.security.secrets' Keyring/InMemory stores) ---------------


class SecretStore(Protocol):
    def set(self, name: str, value: str) -> None: ...


def default_secret_store() -> SecretStore:
    from nox.security.secrets import KeyringSecretStore

    return KeyringSecretStore()


# ---- AI backend probe (honest availability, never assumed) --------------------------------------


@dataclass(frozen=True, slots=True)
class AiProbeResult:
    id: str
    status: HealthStatus
    reason: str

    @property
    def available(self) -> bool:
        return self.status is HealthStatus.AVAILABLE


async def probe_ai_backends(ai_config: AiConfig | None = None) -> list[AiProbeResult]:
    """Live-probe Claude Code and Ollama the same way `nox doctor` does (src/nox/app.py
    `run_doctor`) - never assume, never report success without checking."""
    from nox.ai.providers.claude_code import ClaudeCodeProvider
    from nox.ai.providers.ollama import OllamaProvider

    cfg = ai_config or load_ai_config(DEFAULTS_PATH)
    results: list[AiProbeResult] = []
    for provider_id, provider in (
        ("claude_code", ClaudeCodeProvider(cfg.providers.claude_code)),
        ("ollama", OllamaProvider(cfg.providers.ollama)),
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
    """What the wizard collected. `None`/absent optional fields mean "declined/skipped" - the
    corresponding config key is left untouched so the Defaults layer's value applies."""

    nox_name: str | None = None
    user_display_name: str | None = None
    ui_language: str | None = None  # "de" | "en"
    data_dir: Path | None = None
    vault_dir: Path | None = None
    microphone_enabled: bool | None = None
    camera_enabled: bool | None = None
    ai_backend: str | None = None  # AI_BACKEND_CLAUDE_CODE | AI_BACKEND_OLLAMA_ONLY
    twitch: tuple[str, str] | None = None  # (oauth_token, bot_username)
    obs_password: str | None = None
    telegram_bot_token: str | None = None


# ---- pure config-layer helpers ------------------------------------------------------------------


def build_user_layer_patch(answers: OnboardingAnswers) -> dict[str, Any]:
    """Only the keys the user actually set/confirmed - declined/skipped steps contribute nothing,
    so the Defaults layer's value keeps applying (ST-21-03 acceptance: "safe default applies")."""
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

    if answers.ai_backend is not None:
        if answers.ai_backend == AI_BACKEND_OLLAMA_ONLY:
            patch["ai"] = {
                "router": {"default_reasoner": "ollama", "fallback_chain": ["ollama", "rules"]},
                "providers": {"claude_code": {"enabled": False}},
            }
        else:
            patch["ai"] = {
                "router": {
                    "default_reasoner": "claude_code",
                    "fallback_chain": ["claude_code", "ollama", "rules"],
                },
                "providers": {"claude_code": {"enabled": True}},
            }
    return patch


def store_secrets(answers: OnboardingAnswers, store: SecretStore) -> list[str]:
    """Write only the secrets the user actually provided; returns the secret names stored (never
    the values) for the capability summary."""
    stored: list[str] = []
    if answers.twitch is not None:
        oauth_token, bot_username = answers.twitch
        if oauth_token:
            store.set("nox/twitch/oauth_token", oauth_token)
            stored.append("nox/twitch/oauth_token")
        if bot_username:
            store.set("nox/twitch/bot_username", bot_username)
            stored.append("nox/twitch/bot_username")
    if answers.obs_password:
        store.set("nox/obs/websocket_password", answers.obs_password)
        stored.append("nox/obs/websocket_password")
    if answers.telegram_bot_token:
        store.set("nox/telegram/bot_token", answers.telegram_bot_token)
        stored.append("nox/telegram/bot_token")
    return stored


# ---- honest capability summary ------------------------------------------------------------------


def _import_ok(module: str) -> bool:
    import importlib

    try:
        importlib.import_module(module)
    except Exception:  # noqa: BLE001
        return False
    return True


def render_capability_summary(
    *,
    answers: OnboardingAnswers,
    ai_probes: list[AiProbeResult],
    stored_secret_names: list[str],
    user_config_path: Path,
) -> str:
    """ "No fake capabilities: everything reports available / limited / unavailable" (README
    principle) - this is the wizard's version of `nox doctor`'s report."""
    lines = [
        "",
        "Nox onboarding complete.",
        f"  config written : {user_config_path}",
    ]
    if answers.data_dir is not None:
        lines.append(f"  data dir       : {answers.data_dir}")
    if answers.vault_dir is not None:
        lines.append(f"  vault dir      : {answers.vault_dir}")
    lines.append(
        f"  microphone     : {'enabled' if answers.microphone_enabled else 'disabled/default'}"
    )
    lines.append(
        f"  camera         : {'enabled' if answers.camera_enabled else 'disabled/default'}"
    )
    lines.append(
        "  voice libs     : "
        + ", ".join(
            f"{mod}={'ok' if _import_ok(mod) else 'missing'}"
            for mod in ("faster_whisper", "sounddevice", "piper")
        )
    )
    lines.append("  AI backends    :")
    for probe in ai_probes:
        lines.append(f"    - {probe.id}: {probe.status.value} ({probe.reason})")
    chosen = answers.ai_backend or "claude_code (default)"
    lines.append(f"  chosen backend : {chosen}")
    if stored_secret_names:
        lines.append(f"  secrets stored : {', '.join(stored_secret_names)} (OS keyring only)")
    else:
        lines.append("  secrets stored : none")
    lines.append("")
    lines.append("Re-run `nox onboard` any time to change these (dashboard Settings will too).")
    return "\n".join(lines)


# ---- interactive driver -------------------------------------------------------------------------


def _prompt_answers() -> OnboardingAnswers:
    typer.echo("Nox first-run setup. Press Enter to accept a default; every step can be skipped.\n")

    # Re-running the wizard should default each step to what is already configured, not silently
    # reset it - only config/defaults.yaml's own values seed a genuinely first run.
    existing = load_existing_user_layer(default_user_config_path())
    existing_identity = existing.get("identity", {}) if isinstance(existing, dict) else {}
    existing_paths = existing.get("paths", {}) if isinstance(existing, dict) else {}

    answers = OnboardingAnswers()
    answers.nox_name = typer.prompt("Nox name", default=existing_identity.get("name", "Nox"))
    display_name = typer.prompt(
        "Your name (optional)",
        default=existing_identity.get("user_display_name", ""),
        show_default=False,
    )
    answers.user_display_name = display_name or None
    answers.ui_language = typer.prompt(
        "UI language (de/en)", default=existing_identity.get("ui_language", "de")
    )

    appdata = Path(os.environ.get("APPDATA", str(Path.home() / "AppData" / "Roaming")))
    default_data = existing_paths.get("data_dir", str(appdata / "Nox"))
    default_vault = existing_paths.get("vault_dir", str(appdata / "Nox" / "vault"))
    answers.data_dir = Path(typer.prompt("Data folder", default=default_data))
    answers.vault_dir = Path(
        typer.prompt("Vault folder (kept separate from data)", default=default_vault)
    )

    if typer.confirm("Connect Twitch now?", default=False):
        oauth_token = typer.prompt(
            "Twitch OAuth token (oauth:...)", hide_input=True, default="", show_default=False
        )
        bot_username = typer.prompt("Twitch bot username", default="", show_default=False)
        answers.twitch = (oauth_token, bot_username)
    if typer.confirm("Set an OBS websocket password now?", default=False):
        answers.obs_password = typer.prompt("OBS websocket password", hide_input=True)
    if typer.confirm("Set a Telegram bot token now?", default=False):
        answers.telegram_bot_token = typer.prompt("Telegram bot token", hide_input=True)

    answers.microphone_enabled = typer.confirm("Enable microphone capture?", default=True)
    answers.camera_enabled = typer.confirm("Enable camera capture?", default=False)

    if typer.confirm("Probe AI backends now (Claude Code / Ollama)?", default=True):
        answers.ai_backend = "__probe__"  # resolved to a real choice after probing, below
    return answers


async def run_onboarding_cli(
    *,
    appdata: Path | str | None = None,
    secret_store: SecretStore | None = None,
    probe_fn: AiProbeFn | None = None,
) -> int:
    """The real `nox onboard` entry point (async so the AI probe can run without a nested event
    loop). Every dependency is injectable for tests; production defaults are the real keyring and
    the real provider probe."""
    store = secret_store or default_secret_store()
    probe = probe_fn or probe_ai_backends

    answers = _prompt_answers()

    ai_probes: list[AiProbeResult] = []
    if answers.ai_backend == "__probe__":
        typer.echo("\nProbing AI backends...")
        ai_probes = await probe()
        for result in ai_probes:
            typer.echo(f"  {result.id}: {result.status.value} ({result.reason})")
        recommended = next((r.id for r in ai_probes if r.available), AI_BACKEND_CLAUDE_CODE)
        backend = typer.prompt(
            f"AI backend ({AI_BACKEND_CLAUDE_CODE}/{AI_BACKEND_OLLAMA_ONLY})", default=recommended
        )
        answers.ai_backend = (
            AI_BACKEND_OLLAMA_ONLY if backend == AI_BACKEND_OLLAMA_ONLY else AI_BACKEND_CLAUDE_CODE
        )
    else:
        answers.ai_backend = None  # declined -> Defaults layer's choice applies, untouched

    user_config_path = default_user_config_path(appdata)
    patch = build_user_layer_patch(answers)
    write_user_config(user_config_path, patch)
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
