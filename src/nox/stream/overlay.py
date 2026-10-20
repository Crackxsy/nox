"""The OBS overlay: Nox on stream as a browser source, on a URL that survives restarts.

`OverlayService.setup` issues a fresh overlay credential, builds the pet page's overlay URL with
it and asks the `obs` plugin (through the normal, permission-checked tool path - one
confirmation) to create or update the "Nox Overlay" browser source in the current scene. Only
once OBS points at the new URL does the credential become the overlay's; the previous one is then
revoked and any page still using it is disconnected. If the user declines, or OBS is not there,
the new credential is revoked again and the old overlay keeps working.

The token appears in exactly one place: the URL handed to OBS. Neither the tool result nor the
model ever sees it - the URL the model gets back is shown without its credential.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable
from typing import Any, Protocol

from pydantic import BaseModel, Field

from nox.core.logging import get_logger
from nox.ipc.overlay_tokens import OBS_LABEL, OverlayTokenStore
from nox.tools.executor import ToolExecutor, ToolResult

log = get_logger(__name__)

#: The label a credential carries until OBS has accepted its URL.
PENDING_LABEL = "obs-pending"
#: What a URL looks like when it is shown anywhere but OBS.
HIDDEN = "<hidden>"


def overlay_url(
    http_port: int, token: str, *, variant: str = "neutral", host: str = "127.0.0.1"
) -> str:
    """The pet page in overlay mode, authenticated by an overlay credential in the fragment
    (fragments never reach HTTP logs or Referer headers)."""
    query = "overlay=1" + (f"&variant={variant}" if variant and variant != "neutral" else "")
    return f"http://{host}:{http_port}/pet/?{query}#overlay={token}"


class OverlaySetupRequest(BaseModel):
    """`stream.overlay.setup` (IPC and tool): where and how large the overlay should be."""

    scene_name: str = Field(default="", max_length=200)
    width: int = Field(default=400, ge=64, le=3840)
    height: int = Field(default=400, ge=64, le=2160)


class _Disconnector(Protocol):
    async def disconnect_credential(
        self, credential_ids: Iterable[str], reason: str = ...
    ) -> int: ...


class OverlayService:
    def __init__(
        self,
        store: OverlayTokenStore,
        executor: ToolExecutor,
        *,
        http_port: Callable[[], int],
        variant: Callable[[], str],
        mode: Callable[[], str],
        hub: _Disconnector | None = None,
    ) -> None:
        self._store = store
        self._executor = executor
        self._http_port = http_port
        self._variant = variant
        self._mode = mode
        self._hub = hub

    def status(self) -> dict[str, Any]:
        """What the dashboard shows: whether an overlay credential exists, never the token."""
        active = [c for c in self._store.credentials() if c.label == OBS_LABEL]
        return {
            "configured": bool(active),
            "credential": active[0].public() if active else None,
            "url_shape": overlay_url(self._http_port(), HIDDEN, variant=self._variant()),
        }

    async def setup(
        self, request: OverlaySetupRequest, *, agent: str, origin: str
    ) -> dict[str, Any]:
        credential, token = self._store.issue(PENDING_LABEL)
        url = overlay_url(self._http_port(), token, variant=self._variant())
        result: ToolResult = await self._executor.call(
            agent,
            "obs.overlay.setup",
            {
                "url": url,
                "scene_name": request.scene_name,
                "width": request.width,
                "height": request.height,
            },
            mode=self._mode(),
            origin=origin,
        )
        if not result.ok:
            self._store.revoke(credential.credential_id)
            log.info("stream.overlay_setup_declined", error=result.error)
            return {"ok": False, "error": result.error or "obs.overlay.setup failed"}
        replaced = self._store.adopt(credential.credential_id, OBS_LABEL)
        if replaced and self._hub is not None:
            await self._hub.disconnect_credential(replaced, "overlay credential replaced")
        data = dict(result.data or {})
        log.info("stream.overlay_set_up", credential=credential.credential_id, **_names(data))
        return {
            "ok": True,
            **_names(data),
            "url": overlay_url(self._http_port(), HIDDEN, variant=self._variant()),
        }

    async def revoke(self) -> dict[str, Any]:
        """Revoke the overlay credential; the browser source in OBS stops receiving anything."""
        revoked = self._store.revoke_label(OBS_LABEL) + self._store.revoke_label(PENDING_LABEL)
        closed = 0
        if revoked and self._hub is not None:
            closed = await self._hub.disconnect_credential(revoked, "overlay credential revoked")
        return {"ok": True, "revoked": len(revoked), "disconnected": closed}


def _names(data: dict[str, Any]) -> dict[str, Any]:
    keys = ("input_name", "scene", "created", "added")
    return {k: data[k] for k in keys if k in data}


SetupFn = Callable[[OverlaySetupRequest], Awaitable[dict[str, Any]]]
