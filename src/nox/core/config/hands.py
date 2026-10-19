"""`hands` configuration: the machine-and-world tools (shell, packages, web), all off-by-default
where they carry real risk.

`shell.run` and the package tools are the sharpest tools Nox has, so `shell_enabled` ships False: a
fresh install can answer questions but not run commands until the user turns it on, exactly like
`files.roots` ships empty. `web.*` is on by default because it only reads public pages and already
sits behind the privacy engine's cloud gate.
"""

from __future__ import annotations

from pydantic import Field

from nox.core.config.types import StrictSection

__all__ = ["HandsConfig"]


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
