"""Secret names and the errors every secret store raises.

Kept apart from `nox.security.secrets` so the stores (`file_secrets`, the keyring wrapper) and the
code choosing between them can share one definition without importing each other.
"""

from __future__ import annotations

import re

__all__ = [
    "SECRET_NAME_RE",
    "SecretNameError",
    "SecretStoreError",
    "SecretStoreUnavailableError",
    "validate_name",
]

#: `nox/<component>/<key>`, or `nox/mcp/<server>/<key>` for an external MCP server's credentials
#: (one folder per server, so removing a server can find exactly its secrets).
SECRET_NAME_RE = re.compile(r"^nox/(?:mcp/[a-z0-9_]+/|[a-z0-9_\-]+/)[a-z0-9_\-.]+$")


class SecretNameError(ValueError):
    """Secret names must look like nox/<component>/<key> (lowercase)."""


class SecretStoreError(RuntimeError):
    """The store exists but cannot be used safely: unreadable, foreign, or too widely readable.

    The message names the file and what is wrong with it - never a secret value.
    """


class SecretStoreUnavailableError(SecretStoreError):
    """The store this installation uses is not reachable right now (a keyring that went away).

    Raised instead of quietly switching to another, empty store: that would make the PIN look
    unset and stored tokens look missing, which is a security downgrade nobody chose.
    """


def validate_name(name: str) -> str:
    if not SECRET_NAME_RE.match(name):
        raise SecretNameError(f"invalid secret name {name!r}; expected nox/<component>/<key>")
    return name
