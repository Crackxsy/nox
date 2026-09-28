"""One token per local UI role, all derived from the per-start session token.

The shell, the pet page and the dashboard used to share one token and name their own role in the
handshake, so a pet page or a dashboard tab could connect as `shell` and answer a pending
permission confirmation (`security.permission.reply` is shell-only). Now the token *is* the role:

* `shell` keeps the session token itself - it reads `session.token` from the runtime directory,
  which a web page cannot.
* `pet` and `dashboard` get `HMAC-SHA256(session token, "nox-role-token:v1:<role>")`. The shell
  derives the pet token for the page it hosts; the core hands the dashboard token to the browser
  through a one-time ticket (`nox.ipc.http`). A derived token cannot be turned back into the
  session token, nor into another role's token, so a page can only ever be what it was given.

Derivation instead of more stored tokens: nothing new is written to disk, and every token changes
with the session token at each core start.
"""

from __future__ import annotations

import base64
import hashlib
import hmac

#: The role whose token is the session token itself.
SESSION_ROLE = "shell"
_LABEL = "nox-role-token:v1:"


def derive_role_token(session_token: str, role: str) -> str:
    """The token a client of `role` presents; `shell` gets the session token unchanged."""
    if role == SESSION_ROLE:
        return session_token
    digest = hmac.new(
        session_token.encode("utf-8"), (_LABEL + role).encode("utf-8"), hashlib.sha256
    ).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
