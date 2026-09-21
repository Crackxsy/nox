"""The security vocabulary that more than one module has to agree on.

Profile ids, the origins that make a kill switch a security event, the roles allowed to leave safe
mode and the default loopback allow-list were each written out in two or three places, in
slightly different words. They live here once; the configuration, the profile loader, the kill
switch and the IPC handlers all import them.

This module imports nothing from Nox, so anything may import it.
"""

from __future__ import annotations

from typing import Literal, get_args

__all__ = [
    "DEFAULT_LOOPBACK_ALLOWLIST",
    "RESUME_ROLES",
    "SECURITY_PATH_ORIGINS",
    "SECURITY_PROFILE_IDS",
    "USER_KILL_ORIGINS",
    "SecurityProfileId",
]

#: The permission profiles shipped in `config/profiles/`. `SECURITY_PROFILE_IDS` is derived from
#: the type, so adding an id here is the only edit a new profile needs on the Python side.
SecurityProfileId = Literal[
    "companion", "coding", "stream", "research", "work", "offline", "rocket_league"
]
SECURITY_PROFILE_IDS: tuple[str, ...] = get_args(SecurityProfileId)

#: Loopback services reachable while privacy mode is private or offline: the local model server.
DEFAULT_LOOPBACK_ALLOWLIST: tuple[str, ...] = ("127.0.0.1:11434",)

#: Kill-switch origins that mean "something may be wrong with Nox itself" rather than "the user
#: pressed the button". Resuming from one of these requires the PIN; everything else resumes on an
#: explicit, audited user action.
SECURITY_PATH_ORIGINS: frozenset[str] = frozenset({"tamper", "audit", "panic", "supervisor-tamper"})

#: Origins a UI client may legitimately claim for a kill it triggers itself. Anything else in a
#: `security.kill` payload is replaced by the caller's role, so a client cannot dress its own kill
#: up as a PIN-gated security event.
USER_KILL_ORIGINS: frozenset[str] = frozenset(
    {"ui", "shell", "dashboard", "hotkey", "tray", "voice", "supervisor", "pet"}
)

#: Roles allowed to leave safe mode: the user-controlled surfaces - the desktop shell, the
#: dashboard, and the tray or hotkey path that reaches the core through the supervisor. Never the
#: pet renderer, a worker, a plugin or a paired phone.
RESUME_ROLES: frozenset[str] = frozenset({"shell", "dashboard", "supervisor"})
