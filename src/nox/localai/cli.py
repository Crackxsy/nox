"""The terminal driver for the setup flow: `nox ai setup` and the step in `nox onboard`.

Without a running core there is no tool registry, so a missing Ollama gets instructions rather
than an install offer. The HTTP client is the same egress guard `nox doctor` builds from the
configuration: the configured privacy mode and allow-lists apply here too.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import typer

from nox.ai.config import AiConfig
from nox.core.config import NoxConfig
from nox.core.state import PrivacyMode
from nox.localai.hardware import detect_hardware
from nox.localai.setup import Outcome, Phase, Question, SetupDeps, SetupState, run_setup
from nox.localai.texts import (
    phase_line,
    progress_line,
    question_text,
    render_outcome,
    render_report,
)

__all__ = ["SUCCESS_OUTCOMES", "build_cli_deps", "run_local_ai_setup"]

#: Outcomes that end `nox ai setup` with exit code 0: it did what was asked, or honestly declined.
SUCCESS_OUTCOMES = frozenset({Outcome.DONE, Outcome.RECOMMENDED, Outcome.NO_LLM, Outcome.DECLINED})

#: Print a download progress line at most every this many percent.
_PROGRESS_STEP = 10


def build_cli_deps(config: NoxConfig, user_config_path: Path) -> SetupDeps:
    """Deps for a command with no running core, from the loaded configuration."""
    from nox.entrypoints import _doctor_egress_guard  # noqa: PLC0415 - heavy import, one use

    guard = _doctor_egress_guard(config)
    ai = AiConfig.from_mapping(config.ai.model_dump())
    mode = PrivacyMode(config.privacy.mode)
    return SetupDeps(
        ollama=ai.providers.ollama,
        client_factory=guard.client,
        privacy_mode=lambda: mode,
        user_config_path=user_config_path,
        detect=detect_hardware,
    )


class _Printer:
    """Phase changes and download progress, without flooding the terminal."""

    def __init__(self, language: str | None, echo: Callable[[str], None]) -> None:
        self._language = language
        self._echo = echo
        self._phase = Phase.IDLE
        self._status = ""
        self._step = -1

    def __call__(self, state: SetupState) -> None:
        if state.phase is not self._phase:
            self._phase = state.phase
            line = phase_line(state, self._language)
            if line:
                self._echo(line)
        if state.phase is not Phase.PULL or state.pull is None:
            return
        fraction = state.pull.fraction
        step = -1 if fraction is None else int(fraction * 100) // _PROGRESS_STEP
        if state.pull.status != self._status or step > self._step:
            self._status = state.pull.status
            self._step = step
            line = progress_line(state, self._language)
            if line:
                self._echo(line)


async def run_local_ai_setup(
    *,
    deps: SetupDeps,
    dry_run: bool = False,
    assume_yes: bool = False,
    language: str | None = None,
    ask: Callable[[str], bool] | None = None,
    echo: Callable[[str], None] = typer.echo,
) -> int:
    """Run the flow in a terminal. Returns the exit code."""
    prompt = ask if ask is not None else (lambda text: typer.confirm(text, default=False))
    state = SetupState()
    reported = False

    def report_once() -> None:
        nonlocal reported
        if not reported:
            reported = True
            for line in render_report(state, language):
                echo(line)
            echo("")

    async def confirm(question: Question) -> bool:
        report_once()
        return assume_yes or prompt(question_text(question, state, language))

    state.listeners.append(_Printer(language, echo))
    await run_setup(deps, confirm=confirm, dry_run=dry_run, state=state)
    report_once()
    echo(render_outcome(state, language, base_url=deps.ollama.base_url))
    return 0 if state.outcome in SUCCESS_OUTCOMES else 1
