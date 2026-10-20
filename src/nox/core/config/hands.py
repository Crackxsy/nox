"""Configuration of the "hands": the machine-and-world tools (`HandsConfig`: shell, packages,
web - off by default where they carry real risk), and the capabilities Nox loads, teaches itself
and keeps: MCP servers, self-built skills, watchers and computer use.

Only the *shape* is validated here. Whether a server's command is pinned, whether an environment
value is a secret reference, whether a skill may use a host - those are security decisions and live
in `nox.mcp.policy` and `nox.skills.checks`, where they are applied when something is started,
and reported as that thing's status. Failing them here would reject the whole user layer at boot
and take every other setting with it.
"""

from __future__ import annotations

from pydantic import Field, model_validator

from nox.core.config.types import StrictSection

__all__ = [
    "ComputerUseConfig",
    "HandsConfig",
    "DesktopConfig",
    "McpConfig",
    "McpServerConfig",
    "SkillsConfig",
    "WatchConfig",
]

#: Same rule as a plugin id: it becomes the middle segment of `mcp.<server>.<tool>`.
SERVER_ID_PATTERN = r"^[a-z][a-z0-9_]{1,31}$"


class McpServerConfig(StrictSection):
    """One external MCP server: a local command (stdio) or a remote URL (streamable HTTP)."""

    id: str = Field(pattern=SERVER_ID_PATTERN)
    name: str = Field(default="", max_length=80)
    #: argv of a local server, program first. Empty for a remote server.
    command: list[str] = Field(default_factory=list, max_length=32)
    #: `https://...` of a remote server. Empty for a local one.
    url: str = Field(default="", max_length=500)
    #: The version the user pinned. A server started through `npx`/`uvx` must name exactly this
    #: version in its command, so what runs tomorrow is what was approved today.
    version: str = Field(default="", max_length=64)
    #: Environment of a local server. A value is either plain text or a reference
    #: `secret:nox/mcp/<id>/<name>` resolved from the secret store at start - never a secret itself.
    env: dict[str, str] = Field(default_factory=dict, max_length=32)
    #: HTTP headers of a remote server, same rule as `env`.
    headers: dict[str, str] = Field(default_factory=dict, max_length=16)
    enabled: bool = True
    #: Whether a local server needs the network. A server that does is not used in the privacy
    #: modes OFFLINE and PRIVATE. Remote servers always do.
    network: bool = True
    #: Tools the *user* declared read-only (by their name on the server). Only these may back a
    #: watcher. What the server itself claims about a tool is shown, never trusted.
    read_only_tools: list[str] = Field(default_factory=list, max_length=64)

    @model_validator(mode="after")
    def _one_transport(self) -> McpServerConfig:
        if bool(self.command) == bool(self.url.strip()):
            raise ValueError("an MCP server needs exactly one of `command` or `url`")
        return self


class McpConfig(StrictSection):
    """The `mcp` section: Nox as a client of other people's MCP servers."""

    enabled: bool = True
    servers: list[McpServerConfig] = Field(default_factory=list, max_length=32)
    start_timeout_s: float = Field(default=30.0, gt=0.0, le=300.0)
    call_timeout_s: float = Field(default=60.0, gt=0.0, le=600.0)
    max_tools_per_server: int = Field(default=64, ge=1, le=256)
    #: Start local servers as plain processes when no sandbox can be built here. Off, such a
    #: server is not started and its status says why (fail closed). Only the user sets this, in
    #: `user.yaml`: it is not a dashboard setting and no tool can change it.
    allow_unsandboxed: bool = False

    @model_validator(mode="after")
    def _unique_ids(self) -> McpConfig:
        ids = [server.id for server in self.servers]
        if len(ids) != len(set(ids)):
            raise ValueError("MCP server ids must be unique")
        return self


class SkillsConfig(StrictSection):
    """The `skills` section: capabilities Nox builds for itself, outside its source tree."""

    enabled: bool = True
    #: Repair rounds after a failed check; the spec caps them at three.
    max_repair_rounds: int = Field(default=3, ge=0, le=3)
    build_timeout_s: float = Field(default=900.0, gt=0.0, le=3600.0)
    test_timeout_s: float = Field(default=120.0, gt=0.0, le=900.0)
    max_turns: int = Field(default=40, ge=1, le=200)
    #: Claude Code model for building; empty = the CLI's default.
    model: str = Field(default="", max_length=80)
    #: Versions kept per skill.
    keep_versions: int = Field(default=5, ge=1, le=50)


class WatchConfig(StrictSection):
    """The `watch` section: things Nox looks at on a schedule and reports on."""

    enabled: bool = True
    poll_s: float = Field(default=30.0, gt=0.0, le=600.0)
    #: Shortest interval a watcher may have. Five minutes is the floor: anything faster is
    #: polling someone's server, not watching.
    min_interval_min: int = Field(default=5, ge=5, le=1440)
    default_cooldown_min: int = Field(default=60, ge=0, le=10080)
    max_watchers: int = Field(default=50, ge=1, le=500)
    call_timeout_s: float = Field(default=30.0, gt=0.0, le=120.0)


class ComputerUseConfig(StrictSection):
    """Mouse and key combinations. Off unless the user turns it on, with the PIN when one is set."""

    enabled: bool = False
    #: Pressing this stops a computer-use session immediately.
    stop_hotkey: str = Field(default="ctrl+alt+shift+k", max_length=40)
    #: Actions one confirmed session may send before it has to be confirmed again.
    max_actions_per_session: int = Field(default=200, ge=1, le=2000)
    #: A session ends on its own after this long.
    session_minutes: int = Field(default=15, ge=1, le=120)


class DesktopConfig(StrictSection):
    """The `desktop` section."""

    computer_use: ComputerUseConfig = Field(default_factory=ComputerUseConfig)


class HandsConfig(StrictSection):
    #: Master switch for `shell.run`. Off until the user deliberately turns it on.
    shell_enabled: bool = False
    #: Wall-clock ceiling for one sandboxed command.
    shell_timeout_s: float = Field(default=60.0, ge=1.0, le=600.0)
    #: `web.fetch` / `web.search`. Reading public pages; behind the privacy cloud gate.
    web_enabled: bool = True
    #: SearXNG instance for `web.search` (e.g. on the Pi). Empty => `web.search` says it is
    #: unconfigured rather than scraping anything.
    searxng_base_url: str = ""
    #: `package.install` / `package.remove`. Off until turned on; always confirmed, never
    #: remembered.
    packages_enabled: bool = False
    #: Prefer the stronger Windows AppContainer sandbox when its profile can be created. Turning
    #: this off forces the low-integrity fallback (weaker: it cannot hide the user's files or the
    #: credential store), which is reported honestly by the sandbox's guarantees.
    windows_appcontainer: bool = True
    #: Run sandboxed code even when the sandbox is weaker than the Security Model asks (the Windows
    #: low-integrity fallback reads the user's files and reaches the network; an AppContainer on a
    #: machine without loopback isolation reaches local services). Only the user can set this; off,
    #: every such run is refused and says why.
    accept_weak_sandbox: bool = False
    #: Extra folders where `shell.run` may find a program given by bare name (for example the Git
    #: install's `cmd` folder). Only the system folders are trusted by default; argv0 with a path
    #: is never resolved anywhere. User-set only.
    trusted_program_dirs: list[str] = Field(default_factory=list)
    #: `browser.*`: Nox's own headless Chromium (needs the `browser` extra and `playwright install
    #: chromium`). Reading is allowed; clicking, typing and submitting always ask first.
    browser_enabled: bool = True
    #: `screen.capture`, `camera.snapshot`, `vision.describe`. Capturing always asks, and the
    #: privacy capture flags (camera off by default) and zones still decide.
    senses_enabled: bool = True
    #: Preferred local vision model in Ollama for `vision.describe`; empty = the first installed
    #: model that declares vision. Nox never downloads one by itself.
    vision_model: str = ""
