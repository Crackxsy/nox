"""The setup flow shared by `nox ai setup`, the onboarding step and the dashboard.

    detect -> recommend -> Ollama there? -> (install?) -> model there? -> (pull) -> probe -> write

Each step can end the run with an `Outcome`, and every outcome is honest about why: a machine that
should not run a model is told so, a missing Ollama gets the install offer or the instructions, a
download in `offline`/`private` is refused before any request, and `user.yaml` is written only
after the probe succeeded. Nothing here prints or prompts - the caller passes `confirm` and reads
`state` - so the CLI, the wizard and the IPC handlers drive the very same code.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

from nox.ai.config import OllamaConfig
from nox.ai.local_models import Hardware, Recommendation, recommend
from nox.ai.providers.ollama import OllamaProvider
from nox.core.logging import get_logger
from nox.core.state import PrivacyMode
from nox.localai.ollama_setup import (
    ClientFactory,
    OllamaInstaller,
    OllamaStatus,
    PullError,
    PullProgress,
    PullRefusedError,
    check_ollama,
    install_instructions,
    pull_allowed,
    pull_model,
)
from nox.localai.probe import PROBE_TIMEOUT_S, ProbeResult, probe_model
from nox.settings.layers import UserConfigError, write_user_config
from nox.util.proc import find_binary

log = get_logger(__name__)

__all__ = [
    "Confirm",
    "Outcome",
    "Phase",
    "Question",
    "SetupDeps",
    "SetupState",
    "run_setup",
]


class Phase(StrEnum):
    IDLE = "idle"
    DETECT = "detect"
    OLLAMA = "ollama"
    INSTALL = "install"
    PULL = "pull"
    PROBE = "probe"
    WRITE = "write"
    FINISHED = "finished"


class Outcome(StrEnum):
    """How a run ended. Stable codes; the CLI and the dashboard translate them."""

    #: `--dry-run`: measured and recommended, nothing changed.
    RECOMMENDED = "recommended"
    DONE = "done"
    NO_LLM = "no_llm"
    OLLAMA_MISSING = "ollama_missing"
    OLLAMA_NOT_RUNNING = "ollama_not_running"
    INSTALL_FAILED = "install_failed"
    PRIVACY_BLOCKED = "privacy_blocked"
    DECLINED = "declined"
    PULL_FAILED = "pull_failed"
    PROBE_FAILED = "probe_failed"
    CONFIG_FAILED = "config_failed"
    FAILED = "failed"


class Question(StrEnum):
    """What the flow asks before it acts."""

    INSTALL_OLLAMA = "install_ollama"
    PULL = "pull"


Confirm = Callable[[Question], Awaitable[bool]]


@dataclass(slots=True)
class SetupDeps:
    """Everything the flow touches, injected so tests use a fake Ollama and a temp `user.yaml`."""

    ollama: OllamaConfig
    client_factory: ClientFactory
    privacy_mode: Callable[[], PrivacyMode]
    user_config_path: Path
    #: Blocking (it may run `nvidia-smi`); called in a worker thread.
    detect: Callable[[], Hardware]
    #: None when no `package.install` tool exists; the flow then gives instructions.
    installer: Callable[[], OllamaInstaller | None] = lambda: None
    find: Callable[[str], str | None] = find_binary
    system: str | None = None


@dataclass(slots=True)
class SetupState:
    """A snapshot of one run, rendered by the CLI and sent to the dashboard as `to_payload()`."""

    phase: Phase = Phase.IDLE
    outcome: Outcome | None = None
    detail: str = ""
    hardware: Hardware | None = None
    recommendation: Recommendation | None = None
    ollama: OllamaStatus | None = None
    can_install: bool = False
    instructions: tuple[str, ...] = ()
    privacy_mode: str = ""
    pull: PullProgress | None = None
    probe: ProbeResult | None = None
    model_written: str = ""
    running: bool = False
    listeners: list[Callable[[SetupState], None]] = field(default_factory=list, repr=False)

    @property
    def model(self) -> str | None:
        return self.recommendation.model if self.recommendation is not None else None

    @property
    def pull_blocked(self) -> bool:
        return bool(self.privacy_mode) and not pull_allowed(PrivacyMode(self.privacy_mode))

    @property
    def model_present(self) -> bool:
        model = self.model
        return model is not None and self.ollama is not None and self.ollama.has_model(model)

    def changed(self) -> None:
        for listener in self.listeners:
            listener(self)

    def to_payload(self) -> dict[str, Any]:
        """The `ai.setup.*` reply. Snake case, like every other request's payload."""
        hw = self.hardware
        tier = self.recommendation.tier if self.recommendation is not None else None
        return {
            "running": self.running,
            "phase": self.phase.value,
            "outcome": self.outcome.value if self.outcome is not None else None,
            "detail": self.detail,
            "hardware": None
            if hw is None
            else {
                "ram_gb": hw.ram_gb,
                "vram_gb": hw.vram_gb,
                "machine": hw.machine,
                "pi_generation": hw.pi_generation,
            },
            "recommendation": None
            if self.recommendation is None
            else {
                "model": self.recommendation.model,
                "reason": self.recommendation.reason.value,
                "basis": tier.basis.value if tier is not None else None,
                "download_gb": tier.download_gb if tier is not None else None,
                "measured": tier.measured if tier is not None else None,
            },
            "ollama": None
            if self.ollama is None
            else {
                "reachable": self.ollama.reachable,
                "installed": self.ollama.installed,
                "version": self.ollama.version,
                "has_model": self.model_present,
                "can_install": self.can_install,
                "instructions": list(self.instructions),
            },
            "privacy_mode": self.privacy_mode,
            "pull_blocked": self.pull_blocked,
            "progress": None
            if self.pull is None
            else {
                "status": self.pull.status,
                "completed": self.pull.completed,
                "total": self.pull.total,
            },
            "probe": None
            if self.probe is None
            else {
                "text_ok": self.probe.text_ok,
                "tool_ok": self.probe.tool_ok,
                "reason": self.probe.reason,
            },
            "model_written": self.model_written,
        }


def _finish(state: SetupState, outcome: Outcome, detail: str = "") -> SetupState:
    state.phase = Phase.FINISHED
    state.outcome = outcome
    state.detail = detail
    state.running = False
    state.changed()
    log.info("localai.setup_finished", outcome=outcome.value, model=state.model)
    return state


async def _inspect(deps: SetupDeps, state: SetupState) -> None:
    """Detect, recommend and look at Ollama - the read-only half every run starts with."""
    state.phase = Phase.DETECT
    state.privacy_mode = deps.privacy_mode().value
    state.changed()
    state.hardware = await asyncio.to_thread(deps.detect)
    state.recommendation = recommend(state.hardware)
    if state.recommendation.model is None:
        return  # no model to run, so whether Ollama is there does not matter
    state.phase = Phase.OLLAMA
    state.changed()
    state.ollama = await check_ollama(deps.ollama.base_url, deps.client_factory, find=deps.find)
    state.can_install = not state.ollama.installed and deps.installer() is not None
    state.instructions = () if state.ollama.reachable else install_instructions(deps.system)
    state.changed()


async def run_setup(
    deps: SetupDeps,
    *,
    confirm: Confirm,
    dry_run: bool = False,
    state: SetupState | None = None,
) -> SetupState:
    """One full run. Never raises for an expected failure: the outcome says what happened."""
    state = state if state is not None else SetupState()
    state.running = True
    try:
        return await _run(deps, state, confirm=confirm, dry_run=dry_run)
    except asyncio.CancelledError:
        state.running = False
        raise
    except Exception as exc:  # noqa: BLE001 - an unexpected failure is reported, not a crash
        log.error("localai.setup_crashed", error=f"{type(exc).__name__}: {exc}")
        return _finish(state, Outcome.FAILED, f"{type(exc).__name__}: {exc}")


async def _run(
    deps: SetupDeps, state: SetupState, *, confirm: Confirm, dry_run: bool
) -> SetupState:
    await _inspect(deps, state)
    model = state.model
    if model is None:
        return _finish(state, Outcome.NO_LLM)
    if dry_run:
        return _finish(state, Outcome.RECOMMENDED)

    assert state.ollama is not None
    if not state.ollama.reachable:
        # Installed but not answering is a server to start, not a package to install again.
        installer = None if state.ollama.installed else deps.installer()
        if installer is None or not await confirm(Question.INSTALL_OLLAMA):
            missing = (
                Outcome.OLLAMA_NOT_RUNNING if state.ollama.installed else Outcome.OLLAMA_MISSING
            )
            return _finish(state, missing, state.ollama.error)
        state.phase = Phase.INSTALL
        state.changed()
        installed = await installer()
        if not installed.ok:
            return _finish(state, Outcome.INSTALL_FAILED, installed.detail)
        state.ollama = await check_ollama(deps.ollama.base_url, deps.client_factory, find=deps.find)
        if not state.ollama.reachable:
            return _finish(state, Outcome.OLLAMA_NOT_RUNNING, state.ollama.error)

    if not state.model_present:
        mode = deps.privacy_mode()
        state.privacy_mode = mode.value
        if not pull_allowed(mode):
            return _finish(state, Outcome.PRIVACY_BLOCKED, mode.value)
        if not await confirm(Question.PULL):
            return _finish(state, Outcome.DECLINED)
        state.phase = Phase.PULL
        state.pull = PullProgress()
        state.changed()

        def on_progress(progress: PullProgress) -> None:
            state.pull = progress
            state.changed()

        try:
            state.pull = await pull_model(
                deps.ollama.base_url,
                model,
                deps.client_factory,
                privacy_mode=mode,
                on_progress=on_progress,
            )
        except PullRefusedError as exc:
            return _finish(state, Outcome.PRIVACY_BLOCKED, str(exc))
        except PullError as exc:
            return _finish(state, Outcome.PULL_FAILED, str(exc))

    state.phase = Phase.PROBE
    state.changed()
    provider = OllamaProvider(
        deps.ollama.model_copy(update={"timeout_s": PROBE_TIMEOUT_S, "preload": False}),
        deps.client_factory,
    )
    state.probe = await probe_model(provider, model)
    if not state.probe.ok:
        return _finish(state, Outcome.PROBE_FAILED, state.probe.reason)

    state.phase = Phase.WRITE
    state.changed()
    try:
        await asyncio.to_thread(
            write_user_config,
            deps.user_config_path,
            {"ai": {"providers": {"ollama": {"model": model}}}},
        )
    except (UserConfigError, OSError) as exc:
        return _finish(state, Outcome.CONFIG_FAILED, str(exc))
    state.model_written = model
    return _finish(state, Outcome.DONE)
