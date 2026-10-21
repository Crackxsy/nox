"""The node protocol: what a `nox node` and its core say to each other, and nothing more.

A node dials out to the core's node listener (`wss://<core>:47844/node/v1`, mutual TLS against
the installation's own authority, `nox.node.listener`) and the two speak JSON-RPC 2.0 over that
one WebSocket. The core is an MCP *client* of the node (`tools/list`, `tools/call`); the node may
send only the handful of methods in `NODE_TO_CORE` - a node cannot chat, read memory, answer a
permission prompt or reach another node, because no such method exists on this channel.

Every model here forbids unknown fields and bounds every string: what a node sends is untrusted
until it was validated, and a frame larger than `MAX_FRAME_BYTES` ends the connection.

Pairing link (shown as a QR code on a trusted core session):
`nox-node://pair?h=<host>&p=<port>&c=<code>&fp=<SHA-256 of the authority, 64 hex digits>`. The
fingerprint lets the new node pin the authority before it sends anything, so a device in the
home network that answers in the core's place is refused instead of trusted on first contact.
"""

from __future__ import annotations

import ipaddress
import re
import unicodedata
from typing import Annotated, Literal
from urllib.parse import parse_qs, urlencode, urlsplit

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

__all__ = [
    "BLOB_CHUNK_BYTES",
    "CORE_TO_NODE",
    "MAX_FRAME_BYTES",
    "MAX_RESULT_BYTES",
    "NODE_PATH",
    "NODE_PORT",
    "NODE_PROTOCOL",
    "NODE_PROTOCOL_RANGE",
    "NODE_TO_CORE",
    "Blob",
    "ConfirmResult",
    "Killed",
    "NodeHello",
    "NodePairRequest",
    "NodePairResponse",
    "NoxCallMeta",
    "PairLink",
    "PairLinkError",
    "Ping",
    "SecurityKill",
    "escape_controls",
    "safe_name",
]

#: The protocol this release speaks, and the range of node protocols the core accepts. A node
#: outside the range is told "update needed" and offered nothing (spec C §1.8).
NODE_PROTOCOL = 1
NODE_PROTOCOL_RANGE: tuple[int, int] = (1, 1)
NODE_PORT = 47844
NODE_PATH = "/node/v1"
#: One WebSocket frame. Larger results (a screenshot) travel as `nox/blob` chunks.
MAX_FRAME_BYTES = 1024 * 1024
#: One tool result, all its blob chunks together.
MAX_RESULT_BYTES = 8 * 1024 * 1024
BLOB_CHUNK_BYTES = 512 * 1024

# ---- methods ---------------------------------------------------------------------------------

M_INITIALIZE = "initialize"
M_INITIALIZED = "notifications/initialized"
M_TOOLS_LIST = "tools/list"
M_TOOLS_CALL = "tools/call"
M_LIST_CHANGED = "notifications/tools/list_changed"
M_KILL = "nox/kill"
M_KILLED = "nox/killed"
M_RESUME = "nox/resume"
M_REVOKED = "nox/revoked"
M_POLICY = "nox/policy"
M_PING = "nox/ping"
M_PONG = "nox/pong"
M_CONFIRM_RESULT = "nox/confirm_result"
M_SECURITY_KILL = "security.kill"
M_BLOB = "nox/blob"

#: Everything a node may send to the core - the role `node` allow-list. Anything else is answered
#: with "method not found", counted, and after a few the connection is closed.
NODE_TO_CORE: frozenset[str] = frozenset(
    {
        M_INITIALIZE,
        M_INITIALIZED,
        M_KILLED,
        M_PING,
        M_PONG,
        M_CONFIRM_RESULT,
        M_SECURITY_KILL,
        M_LIST_CHANGED,
        M_BLOB,
    }
)
#: Everything the core sends to a node. `nox/policy` may only narrow what the node offers.
CORE_TO_NODE: frozenset[str] = frozenset(
    {M_TOOLS_LIST, M_TOOLS_CALL, M_KILL, M_RESUME, M_REVOKED, M_POLICY, M_PING, M_PONG}
)

# ---- text from the other side ----------------------------------------------------------------

#: A tool, method or group name as it may be recorded verbatim. Anything else is recorded escaped.
_SAFE_NAME = re.compile(r"^[A-Za-z0-9_.*:/-]{1,96}$")


def escape_controls(text: str) -> str:
    """`text` with every C0/C1 control character, DEL, line break and format character (bidi
    overrides such as U+202E) written as an escape (`\\x1b`, `\\u202e`). What a node or a core
    sends can then neither drive a terminal nor fake a line of an audit display."""
    out: list[str] = []
    for char in text:
        code = ord(char)
        if code < 0x20 or 0x7F <= code <= 0x9F:
            out.append(f"\\x{code:02x}")
        elif unicodedata.category(char) in ("Cf", "Zl", "Zp", "Co", "Cs", "Cn"):
            out.append(f"\\u{code:04x}" if code <= 0xFFFF else f"\\U{code:08x}")
        else:
            out.append(char)
    return "".join(out)


def safe_name(value: object, limit: int = 96) -> str:
    """A name for an audit entry or a log line: itself when it is a plain name, otherwise its
    escaped form - never raw control characters."""
    text = value if isinstance(value, str) else repr(value)
    if _SAFE_NAME.fullmatch(text):
        return text
    return escape_controls(text)[:limit]


# ---- field types -----------------------------------------------------------------------------

_Id = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9_.:-]{1,64}$")]
_Hex64 = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
_Short = Annotated[str, StringConstraints(max_length=64)]
OsName = Literal["windows", "linux", "macos"]
RiskName = Literal["read", "low", "medium", "high", "critical"]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class NodeHello(_Strict):
    """`initialize` from the node: who it is and what it offers (by hash; the list comes later)."""

    protocolVersion: str = Field(max_length=32)  # noqa: N815 - MCP's field name
    node_protocol: int = Field(ge=0, le=1000)
    node_version: str = Field(max_length=32)
    os: OsName
    arch: str = Field(max_length=16, pattern=r"^[A-Za-z0-9_]{1,16}$")
    manifest_hash: _Hex64
    #: Whether the person at this device can be asked (a screen and a working dialog). Only then
    #: does the core leave a high-risk confirmation to the device (D1).
    confirmer: bool = False
    clientInfo: dict[_Short, _Short] = Field(default_factory=dict, max_length=4)  # noqa: N815


class NoxCallMeta(_Strict):
    """`_nox` on every `tools/call`: which call this is, how risky the core judged it, the core's
    receipt for a side effect (checked on the node, `nox.node.gate`) and when it stops being
    valid."""

    call_id: _Id
    turn_id: str = Field(default="", max_length=64)
    risk: RiskName
    receipt: str | None = Field(default=None, max_length=512)
    deadline: float = Field(ge=0)
    #: Who allowed it on the core (`policy`, `user`, `core`), as signed in the receipt.
    decided_by: str = Field(default="", max_length=32)
    #: High risk: the core left the decision to this device (owner decision D1).
    confirm_on_device: bool = False


class Ping(_Strict):
    seq: int = Field(ge=0)
    ts: float = Field(ge=0)


class Killed(_Strict):
    """The node's answer to `nox/kill`: the calls it stopped."""

    call_ids: list[_Id] = Field(default_factory=list, max_length=256)


class ConfirmResult(_Strict):
    """The outcome of a confirmation shown on the node itself."""

    call_id: _Id
    decision: Literal["allow", "deny", "timeout"]


class SecurityKill(_Strict):
    """A stop pressed on the node. Always honoured: stopping is always safe (DoS accepted)."""

    reason: str = Field(default="", max_length=200)


class Blob(_Strict):
    """One chunk of a large result; `data` is base64."""

    call_id: _Id
    seq: int = Field(ge=0, le=64)
    last: bool
    data: str = Field(max_length=(BLOB_CHUNK_BYTES * 4) // 3 + 8)


class NodePairRequest(_Strict):
    """`POST /api/node/pair` from a new node: the one-time code and a certificate request."""

    code: str = Field(min_length=4, max_length=16)
    csr: str = Field(min_length=64, max_length=4096)
    name: _Short = ""
    os: OsName
    arch: str = Field(max_length=16, pattern=r"^[A-Za-z0-9_]{1,16}$")
    version: str = Field(max_length=32)


class NodePairResponse(_Strict):
    node_id: str
    name: str
    cert: str
    ca_cert: str
    core_id: str
    node_port: int
    #: The core's receipt key (Ed25519, base64): the node pins it (`nox.node.receipts`).
    receipt_key: str = ""


# ---- the pairing link ------------------------------------------------------------------------

LINK_SCHEME = "nox-node"
_CODE = re.compile(r"^[23456789ABCDEFGHJKMNPQRSTVWXYZ]{8}$")
_HOST = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,62})(?:\.[a-z0-9](?:[a-z0-9-]{0,62}))*$")
_PAIR_HOST_SUFFIXES = (".local", ".home.arpa")


class PairLinkError(ValueError):
    """Not a pairing link this release understands."""


def _check_host(host: str) -> str:
    """A private or loopback address, or a `.local` / `.home.arpa` name - what the authority may
    vouch for. Anything else cannot be the core and is refused before any connection."""
    host = host.strip().lower().rstrip(".")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        if host != "localhost" and not (
            _HOST.fullmatch(host) and host.endswith(_PAIR_HOST_SUFFIXES)
        ):
            raise PairLinkError("the link names a host Nox cannot be at") from None
        return host
    if not (address.is_private or address.is_loopback or address.is_link_local):
        raise PairLinkError("the link names a public address")
    return str(address)


class PairLink(_Strict):
    host: str
    port: int = Field(ge=1, le=65535)
    code: str
    ca_fingerprint: _Hex64

    def to_url(self) -> str:
        query = urlencode(
            {"h": self.host, "p": self.port, "c": self.code, "fp": self.ca_fingerprint}
        )
        return f"{LINK_SCHEME}://pair?{query}"

    @classmethod
    def make(cls, *, host: str, port: int, code: str, ca_fingerprint: str) -> PairLink:
        """`ca_fingerprint` may be written `AB:CD:..` (as `nox.lan.tls.fingerprint` returns it)."""
        return cls.parse_parts(host=host, port=str(port), code=code, fp=ca_fingerprint)

    @classmethod
    def parse_parts(cls, *, host: str, port: str, code: str, fp: str) -> PairLink:
        code = code.strip().upper()
        if not _CODE.fullmatch(code):
            raise PairLinkError("the link carries no valid pairing code")
        fp = fp.replace(":", "").strip().lower()
        if not re.fullmatch(r"[0-9a-f]{64}", fp):
            raise PairLinkError("the link carries no valid fingerprint")
        if not port.isdigit() or not 0 < int(port) < 65536:
            raise PairLinkError("the link carries no valid port")
        return cls(host=_check_host(host), port=int(port), code=code, ca_fingerprint=fp)

    @classmethod
    def parse(cls, text: str) -> PairLink:
        parts = urlsplit(text.strip())
        if parts.scheme != LINK_SCHEME or parts.netloc != "pair" or parts.fragment:
            raise PairLinkError("not a Nox node pairing link")
        try:
            query = parse_qs(parts.query, strict_parsing=True, max_num_fields=8)
        except ValueError as exc:
            raise PairLinkError("the link is malformed") from exc
        if set(query) != {"h", "p", "c", "fp"} or any(len(v) != 1 for v in query.values()):
            raise PairLinkError("the link is incomplete")
        return cls.parse_parts(
            host=query["h"][0], port=query["p"][0], code=query["c"][0], fp=query["fp"][0]
        )
