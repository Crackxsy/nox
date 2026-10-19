"""Ollama for the setup flow: is it there, how to get it, and downloading a model with progress.

Every request goes through the client factory the caller hands in - the core's egress guard, or
the same guard `nox doctor` builds - never a bare `httpx.AsyncClient`. Ollama itself is loopback,
but the download it performs is not: `/api/pull` makes Ollama fetch gigabytes from its registry,
an outbound transfer the guard cannot see. That is why `pull_model` checks the privacy mode itself
and refuses in `offline` and `private`, before any request is sent.

Installing Ollama is never done behind the user's back. When a `package.install` tool is
registered, `tool_installer` offers to run it through the `ToolExecutor`, so the permission engine
asks exactly as it would for the model; without one the user gets honest per-OS instructions.
"""

from __future__ import annotations

import json
import platform
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field
from typing import Any, Protocol

import httpx

from nox.core.logging import get_logger
from nox.core.state import PrivacyMode
from nox.util.proc import find_binary

log = get_logger(__name__)

__all__ = [
    "OLLAMA_PACKAGE",
    "PACKAGE_INSTALL_TOOL",
    "PULL_BLOCKING_MODES",
    "ClientFactory",
    "InstallResult",
    "OllamaInstaller",
    "OllamaStatus",
    "PullError",
    "PullProgress",
    "PullRefusedError",
    "check_ollama",
    "install_instructions",
    "pull_allowed",
    "pull_model",
    "tool_installer",
]

ClientFactory = Callable[[], httpx.AsyncClient]

#: The tool another part of Nox registers to install system packages, and the package it is asked
#: for. Looked up at call time: it may be registered after this extension, or not at all.
PACKAGE_INSTALL_TOOL = "package.install"
OLLAMA_PACKAGE = "ollama"
_AGENT = "localai"

#: Privacy modes in which nothing is downloaded.
PULL_BLOCKING_MODES = frozenset({PrivacyMode.OFFLINE, PrivacyMode.PRIVATE})

#: Gap allowed between two progress lines of a download before it counts as stalled.
PULL_READ_TIMEOUT_S = 300.0


# ---- is it there --------------------------------------------------------------------------------


def _normalise(name: str) -> str:
    return name if ":" in name else f"{name}:latest"


@dataclass(frozen=True, slots=True)
class OllamaStatus:
    """What the setup knows about Ollama. `reachable` is the server, `installed` the binary."""

    reachable: bool
    installed: bool
    version: str = ""
    models: tuple[str, ...] = ()
    error: str = ""

    def has_model(self, model: str) -> bool:
        """Exact match (`qwen3` means `qwen3:latest`) - unlike the provider's health check, a
        different tag of the same family does not count as the model being present."""
        wanted = _normalise(model)
        return any(_normalise(name) == wanted for name in self.models)


async def check_ollama(
    base_url: str,
    client_factory: ClientFactory,
    *,
    find: Callable[[str], str | None] = find_binary,
) -> OllamaStatus:
    """Ask the server for its version and its models; look for the binary on PATH."""
    installed = find("ollama") is not None
    root = base_url.rstrip("/")
    try:
        async with client_factory() as client:
            version = await client.get(f"{root}/api/version", timeout=5.0)
            version.raise_for_status()
            tags = await client.get(f"{root}/api/tags", timeout=5.0)
            tags.raise_for_status()
            listing = tags.json()
    except (httpx.HTTPError, ValueError, PermissionError) as exc:
        # PermissionError: the egress guard's refusal (`EgressDenied`) - reported, never raised.
        return OllamaStatus(
            reachable=False, installed=installed, error=f"{type(exc).__name__}: {exc}"
        )
    models = tuple(
        str(entry.get("name", ""))
        for entry in listing.get("models", [])
        if isinstance(entry, dict) and entry.get("name")
    )
    return OllamaStatus(
        reachable=True,
        # A server that answers is proof enough, even when the binary is not on this PATH (a
        # service install, or Ollama on another machine).
        installed=True,
        version=str(_json_or_empty(version).get("version", "")),
        models=models,
    )


def _json_or_empty(response: httpx.Response) -> dict[str, Any]:
    try:
        data = response.json()
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def install_instructions(system: str | None = None) -> tuple[str, ...]:
    """The official way to install Ollama on this OS, as commands a person can paste."""
    name = (system if system is not None else platform.system()).lower()
    if name == "windows":
        return (
            "winget install --id Ollama.Ollama -e",
            "or the installer from https://ollama.com/download",
        )
    if name == "darwin":
        return ("brew install ollama", "or the app from https://ollama.com/download")
    # Linux, including Raspberry Pi OS: the official install script sets up a service.
    return ("curl -fsSL https://ollama.com/install.sh | sh",)


# ---- installing it through the tool -------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class InstallResult:
    ok: bool
    detail: str = ""


OllamaInstaller = Callable[[], Awaitable[InstallResult]]


class _ToolResultLike(Protocol):
    @property
    def ok(self) -> bool: ...
    @property
    def error(self) -> str | None: ...


class _RegistryLike(Protocol):
    def get(self, name: str) -> Any: ...


class _ExecutorLike(Protocol):
    async def call(
        self, agent: str, name: str, arguments: dict[str, Any], *, mode: str
    ) -> _ToolResultLike: ...


def tool_installer(
    registry: _RegistryLike | None,
    executor: _ExecutorLike | None,
    *,
    mode: Callable[[], str],
) -> OllamaInstaller | None:
    """An installer that runs `package.install` through the executor, or None without the tool.

    Through the executor rather than the handler, so the permission engine decides - and asks the
    user - exactly as it would if the model had asked for the install.
    """
    if registry is None or executor is None or registry.get(PACKAGE_INSTALL_TOOL) is None:
        return None

    async def install() -> InstallResult:
        result = await executor.call(
            _AGENT, PACKAGE_INSTALL_TOOL, {"package": OLLAMA_PACKAGE}, mode=mode()
        )
        log.info("localai.ollama_install", ok=result.ok, error=result.error)
        return InstallResult(ok=result.ok, detail=result.error or "")

    return install


# ---- downloading a model ------------------------------------------------------------------------


class PullRefusedError(PermissionError):
    """The privacy mode does not allow a download."""


class PullError(RuntimeError):
    """Ollama could not download the model; the message is Ollama's own where it gave one."""


@dataclass(slots=True)
class PullProgress:
    """Ollama's current status line plus the bytes of every layer seen so far."""

    status: str = ""
    completed: int = 0
    total: int = 0
    _layers: dict[str, tuple[int, int]] = field(default_factory=dict, repr=False)

    def feed(self, line: dict[str, Any]) -> None:
        self.status = str(line.get("status", self.status))
        digest = line.get("digest")
        total = line.get("total")
        if isinstance(digest, str) and isinstance(total, int) and total > 0:
            done = line.get("completed")
            self._layers[digest] = (int(done) if isinstance(done, int) else 0, total)
            self.completed = sum(c for c, _ in self._layers.values())
            self.total = sum(t for _, t in self._layers.values())

    @property
    def fraction(self) -> float | None:
        return None if self.total <= 0 else min(1.0, self.completed / self.total)


def pull_allowed(mode: PrivacyMode) -> bool:
    return mode not in PULL_BLOCKING_MODES


async def pull_model(
    base_url: str,
    model: str,
    client_factory: ClientFactory,
    *,
    privacy_mode: PrivacyMode,
    on_progress: Callable[[PullProgress], None] = lambda _p: None,
) -> PullProgress:
    """`POST /api/pull` with streamed NDJSON progress. Raises `PullRefusedError` in `offline` or
    `private` before sending anything, `PullError` when Ollama reports a failure."""
    if not pull_allowed(privacy_mode):
        raise PullRefusedError(f"downloads are off in privacy mode {privacy_mode.value!r}")
    progress = PullProgress()
    url = base_url.rstrip("/") + "/api/pull"
    timeout = httpx.Timeout(10.0, read=PULL_READ_TIMEOUT_S)
    try:
        async with (
            client_factory() as client,
            client.stream(
                "POST", url, json={"model": model, "stream": True}, timeout=timeout
            ) as response,
        ):
            if response.status_code >= 400:
                body = (await response.aread()).decode("utf-8", errors="replace")
                raise PullError(_error_text(body) or f"HTTP {response.status_code}")
            async for raw in response.aiter_lines():
                if not raw.strip():
                    continue
                line = json.loads(raw)
                if not isinstance(line, dict):
                    continue
                if "error" in line:
                    raise PullError(str(line["error"]))
                progress.feed(line)
                on_progress(progress)
    except (httpx.HTTPError, PermissionError) as exc:
        raise PullError(f"{type(exc).__name__}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise PullError(f"invalid progress line: {exc}") from exc
    if progress.status != "success":
        raise PullError(f"the download ended without success (last status: {progress.status!r})")
    return progress


def _error_text(body: str) -> str:
    for raw in _lines(body):
        try:
            line = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if isinstance(line, dict) and "error" in line:
            return str(line["error"])
    return body.strip()[:200]


def _lines(body: str) -> Iterable[str]:
    return (line for line in body.splitlines() if line.strip())
