"""OBS on a node (C9): `obs.request` where OBS runs, with the core-local rules unchanged.

A streaming PC is often not the PC Nox's core runs on. With the group `obs` enabled on that PC
(`nox node allow obs`, off by default), the core routes `obs.request` there (`nox.node.router`;
the family default from the Devices page, C8). On the node:

* **The node talks to its own OBS only**: obs-websocket on `127.0.0.1` (the port is the one
  setting), never another host. The OBS password stays on this device, in its keyring
  (`nox node obs password`, service `nox-node`); it is never sent to the core and never shown.
* **The same rules as on the core** (`nox.stream.obs_requests`), decided again here with this
  device's own privacy scene and scene set: hard-prohibited request types (stopping the stream,
  the stream key) and blocked ones are refused before anything else; the call's risk is the
  classifier's (reads low here, so every call carries a receipt; changes medium; consequential
  ones high - asked in the dialog on this device); the core may not claim less (`NodeGate`,
  `risk_understated`); the privacy scene and everything in it cannot be changed (asked from OBS
  before every change, refused when OBS cannot say); answers are redacted before they leave.
* **The panic button** (`nox/obs/panic`): OBS goes to this device's privacy scene, and from then
  on nothing but reads reaches OBS until this device says otherwise (`nox node obs resume`) - a
  restart does not lift it (`obs-panic` in the node's folder).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path, PureWindowsPath
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from nox.core.logging import get_logger
from nox.node.tools import NodeRefusal, NodeTool
from nox.security.model import Risk
from nox.stream.obs_requests import (
    TOOL_GUIDE,
    InvalidObsRequestError,
    classify,
    privacy_scene_refusal,
    redact,
    redact_text,
    refusal,
)

log = get_logger(__name__)

__all__ = [
    "OBS_HOST",
    "OBS_PANIC_FILE",
    "OBS_SECRET",
    "NodeObs",
    "ObsArgs",
    "ObsSettings",
    "obs_risk",
    "obs_tools",
]

#: The only host a node's OBS client connects to: the OBS on this device.
OBS_HOST = "127.0.0.1"
#: The keyring entry (service `nox-node`) holding the obs-websocket password.
OBS_SECRET = "obs/websocket_password"  # noqa: S105 - a secret's name, not its value
#: A scene set no real scene is in: without a scene set here, every scene switch is high.
NO_SCENE_SET = "\x00"
#: The panic latch in the node's folder: present, nothing but reads reaches OBS.
OBS_PANIC_FILE = "obs-panic"
#: How long a call waits for OBS to be connected before it says so.
CONNECT_WAIT_S = 3.0
#: An answer larger than this is cut to a preview (the plugin's limit).
MAX_RESPONSE_CHARS = 24_000


class ObsSettings(BaseModel):
    """The `obs:` part of this node's `satellites.yaml`: names of this device's OBS, no secret."""

    model_config = ConfigDict(extra="forbid")

    port: int = Field(default=4455, ge=1, le=65535)
    #: Where the panic button sends the stream; empty: the panic button has nothing to switch to.
    privacy_scene: str = Field(default="", max_length=200)
    #: Scenes Nox may switch to as a medium-risk request; any other switch is high (as on the core).
    scene_set: list[str] = Field(default_factory=list, max_length=64)


class ObsArgs(BaseModel):
    """`obs.request` on a node: the core's input (one obs-websocket v5 request), strict."""

    model_config = ConfigDict(extra="forbid")

    request_type: str = Field(min_length=3, max_length=64, pattern=r"^[A-Z][A-Za-z0-9]+$")
    request_data: dict[str, Any] = Field(default_factory=dict)


def _bounded(args: ObsArgs) -> str | None:
    if len(json.dumps(args.request_data, default=str)) > 64_000:
        return "request_data_too_large"
    return None


def precheck(args: BaseModel, settings: ObsSettings) -> str | None:
    """Refused before the budget and before anyone is asked: never-possible request types,
    malformed ones, and what classifies as critical (the privacy scene by name, blocked types)."""
    assert isinstance(args, ObsArgs)
    big = _bounded(args)
    if big is not None:
        return big
    if refusal(args.request_type) is not None:
        return "obs_request_never_possible"
    try:
        scope = classify(
            args.request_type,
            args.request_data,
            privacy_scene=settings.privacy_scene,
            scene_set=settings.scene_set or (NO_SCENE_SET,),
        )
    except InvalidObsRequestError:
        return "invalid_request_type"
    if scope.risk is Risk.CRITICAL:
        return "obs_request_never_possible"
    return None


def obs_risk(args: BaseModel, settings: ObsSettings) -> str:
    """This call's risk on this device: the classifier's, never below low (every call is signed).
    Without a scene set here, every scene switch is high: nothing was listed as harmless."""
    assert isinstance(args, ObsArgs)
    try:
        scope = classify(
            args.request_type,
            args.request_data,
            privacy_scene=settings.privacy_scene,
            scene_set=settings.scene_set or (NO_SCENE_SET,),
        )
    except InvalidObsRequestError:
        return "critical"
    return "low" if scope.risk is Risk.READ else scope.risk.value


ClientFactory = Callable[[str, Callable[[], Awaitable[str | None]]], Any]


def _default_client(url: str, password: Callable[[], Awaitable[str | None]]) -> Any:
    from nox.stream.obs_ws import ObsWebSocketClient  # noqa: PLC0415

    return ObsWebSocketClient(
        url,
        password_provider=password,
        on_event=lambda _type, _data: None,
        event_subscriptions=0,  # a node only answers requests; it needs none of OBS's events
    )


@dataclass
class NodeObs:
    """This device's OBS, reached on loopback only; the client is made on first use."""

    settings: Callable[[], ObsSettings]
    #: The obs-websocket password from this device's keyring (None: OBS needs none, or unset).
    password: Callable[[], str | None]
    make_client: ClientFactory = _default_client
    connect_wait_s: float = CONNECT_WAIT_S
    #: Where the panic latch is kept, so a restart does not lift it (None: in memory only).
    panic_file: Path | None = None
    _panicked: bool = False
    _client: Any = None
    _port: int = 0
    _background: set[asyncio.Task[None]] = field(default_factory=set)

    @property
    def panicked(self) -> bool:
        """Read again before every request: `nox node obs resume` in a terminal (which removes
        the file) takes effect on the running node at once. In memory only when there is no
        file, or it could not be written."""
        return self._panicked or (self.panic_file is not None and self.panic_file.exists())

    def _url(self) -> str:
        return f"ws://{OBS_HOST}:{self.settings().port}"

    async def _secret(self) -> str | None:
        return await asyncio.to_thread(self.password)

    async def client(self) -> Any:
        port = self.settings().port
        if self._client is not None and port != self._port:
            await self.close()  # the port changed in `satellites.yaml`
        if self._client is None:
            self._client = self.make_client(self._url(), self._secret)
            self._port = port
            self._client.start()
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.connect_wait_s
        while not (self._client.connected and self._client.identified):
            if loop.time() >= deadline:
                raise ConnectionError(self._client.last_error or "OBS is not reachable")
            await asyncio.sleep(0.05)
        return self._client

    async def close(self) -> None:
        client, self._client = self._client, None
        if client is not None:
            with contextlib.suppress(Exception):
                await client.stop()

    async def request(self, request_type: str, request_data: dict[str, Any]) -> dict[str, Any]:
        refused = refusal(request_type)
        if refused is not None:
            raise NodeRefusal("obs_request_never_possible", refused)
        settings = self.settings()
        scope = classify(request_type, request_data, privacy_scene=settings.privacy_scene)
        try:
            client = await self.client()
        except ConnectionError as exc:
            return {"ok": False, "error": "obs_unreachable", "detail": redact_text(str(exc))[:200]}
        if scope.risk is not Risk.READ:
            if self.panicked:
                raise NodeRefusal("obs_panic", "the panic button was pressed; OBS stays as it is")
            try:
                reason = await privacy_scene_refusal(
                    client.request, settings.privacy_scene, request_type, request_data
                )
            except PermissionError as exc:
                raise NodeRefusal("privacy_scene_unchecked", str(exc)[:200]) from None
            if reason is not None:
                raise NodeRefusal("privacy_scene", reason)
        from nox.stream.obs_ws import ObsRequestError  # noqa: PLC0415

        try:
            raw = await client.request(request_type, request_data)
        except ObsRequestError as exc:
            return {
                "ok": False,
                "request_type": request_type,
                "code": exc.code,
                "comment": redact_text(exc.comment),
            }
        response = redact(raw)
        if request_type == "GetRecordDirectory" and isinstance(response, dict):
            # Where this PC keeps its recordings is its business: the folder's name at most.
            folder = str(raw.get("recordDirectory") or "") if isinstance(raw, dict) else ""
            name = PureWindowsPath(folder.replace("/", "\\")).name
            response["recordDirectory"] = redact_text(name) if name else "[hidden]"
        text = json.dumps(response, default=str)
        if len(text) > MAX_RESPONSE_CHARS:
            return {
                "ok": True,
                "request_type": request_type,
                "truncated": True,
                "response_preview": text[:MAX_RESPONSE_CHARS],
            }
        return {"ok": True, "request_type": request_type, "response": response}

    async def panic(self, reason: str = "") -> bool:
        """`nox/obs/panic`: latch first (nothing changes OBS from now on), then the privacy scene.
        True when OBS switched. Never raises."""
        written = False
        if self.panic_file is not None:
            try:
                self.panic_file.write_text("panic", encoding="utf-8")
                written = True
            except OSError:
                written = False
        # The file is the latch (this device lifts it); in memory only when it cannot be written.
        self._panicked = not written
        scene = self.settings().privacy_scene
        if not scene:
            log.warning("node.obs_panic_no_privacy_scene")
            return False
        try:
            client = await self.client()
            await client.request("SetCurrentProgramScene", {"sceneName": scene})
        except Exception as exc:  # noqa: BLE001 - a panic never raises
            log.warning("node.obs_panic_failed", error=type(exc).__name__, reason=reason[:64])
            return False
        return True

    def resume(self) -> None:
        """This device lifts the panic latch (`nox node obs resume`); never the core."""
        self._panicked = False
        if self.panic_file is not None:
            self.panic_file.unlink(missing_ok=True)


def obs_tools(obs: NodeObs) -> dict[str, NodeTool]:
    """`obs.request` on this device (group `obs`)."""

    async def handler(args: BaseModel) -> dict[str, Any]:
        assert isinstance(args, ObsArgs)
        return await obs.request(args.request_type, dict(args.request_data))

    tool = NodeTool(
        "obs.request",
        "obs",
        "low",  # reads included: every call carries the core's receipt
        TOOL_GUIDE,
        ObsArgs,
        handler,
        path_safe=True,  # opens no path; OBS's own file-writing requests are refused
        risk_for=lambda args: obs_risk(args, obs.settings()),
        shown=lambda args: {
            "request_type": args.request_type,  # type: ignore[attr-defined]
            "request_data": args.request_data,  # type: ignore[attr-defined]
        },
        precheck=lambda args: precheck(args, obs.settings()),
    )
    return {tool.name: tool}
