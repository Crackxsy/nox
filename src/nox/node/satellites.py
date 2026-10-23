"""The core's side of the satellites (C9): voice on a node, OBS where it runs, the pet overlay.

**Voice** (`nox/voice/heard`) - a node's voice satellite sends what it heard as text. The core
takes it only while all of this holds, checked for every utterance:

* the node offers `voice.say` - the group `voice` is enabled on the device and not narrowed here
  - and the owner switched it on for this node in the Devices page, with the PIN (a persisted
  second key, `nox.node.policy.CORE_OPT_IN`; checked again here for every utterance, fail
  closed; removing the node clears it);
* this core's privacy lets a microphone listen (the capture switch, no panic, no safe mode, no
  privacy zone) and the kill switch is not engaged - the same decision is pushed to the node as
  `nox/voice/gate`, so its microphone is closed while this says no;
* at most `MAX_UTTERANCES` per minute per node.

The utterance becomes a turn of its own conversation (`voice:node:<id>`): history, model context
and taint of its own; "hier" and the family default `origin` mean that node (`nox.node.router`);
the turn is recorded - or not - by this core's privacy mode like any other; facts learned from it
wait for confirmation (`turn_origin("node")`). Approvals never come by voice: a question that
needs one is asked where approvals are always asked. The answer goes back sentence by sentence
(`nox/voice/say`), and the node speaks it. Audited: that a node asked, how long it was - never
what.

**Levels** (`nox/voice/level`) - published as `tts.level` with the node's id (`device`), so a pet
for that node (`?device=<id>`) moves its mouth with the node's speech, and no other pet does.

**Overlay credential** (`nox/overlay/credential`) - a node with `obs` enabled (and not narrowed)
gets a read-only overlay credential, label `node:<id>`, at most once a minute; each new one
revokes the previous and disconnects its pages. Revoked when the node is removed or `obs` is
narrowed for it.

**Panic** - `security.panic` sends `nox/obs/panic` to every connected node that enabled `obs`.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections import deque
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel

from nox.core.logging import get_logger
from nox.node import protocol as p
from nox.node.router import VOICE_NODE_CHANNEL
from nox.security.model import AuditLog

log = get_logger(__name__)

__all__ = ["MAX_UTTERANCES", "SatelliteHub", "node_overlay_label"]

#: Utterances a node may send per minute.
MAX_UTTERANCES = 12
#: How often a node may ask for a new overlay credential.
CREDENTIAL_INTERVAL_S = 60.0
#: Said when the turn failed (the node hears it instead of silence).
FAILED = {"de": "Das hat nicht funktioniert.", "en": "That did not work."}


def node_overlay_label(device_id: str) -> str:
    from nox.ipc.tokens import NODE_OVERLAY_PREFIX  # noqa: PLC0415

    return f"{NODE_OVERLAY_PREFIX}{device_id}"


#: Runs one turn: (text, language, on_chunk, channel) -> the turn (its text is unused here).
TurnFn = Callable[[str, str, Callable[[str], Awaitable[None]], str], Awaitable[Any]]
#: Why a microphone may not listen now, or None.
CaptureFn = Callable[[], str | None]


@dataclass
class _Node:
    utterances: deque[float] = field(default_factory=deque)
    task: asyncio.Task[None] | None = None
    #: The last `nox/voice/gate` told to this session: (session, capture, reason).
    told: tuple[object, bool, str] | None = None
    credential_at: float = -CREDENTIAL_INTERVAL_S


class SatelliteHub:
    def __init__(
        self,
        *,
        offers: Callable[[str, str], bool],
        enabled_on_device: Callable[[str, str], bool],
        sessions: Callable[[], Iterable[Any]],
        turn: TurnFn | None,
        capture_refusal: CaptureFn,
        publish: Callable[[str, dict[str, Any]], None],
        audit: AuditLog | None = None,
        overlay: Any = None,
        disconnect_credential: Callable[[list[str]], Awaitable[int]] | None = None,
        online: Callable[[str], bool] = lambda _device_id: True,
        opted_in: Callable[[str, str], bool] = lambda _device_id, _group: False,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        #: Whether node `device_id` offers tool `name` right now (`NodeEntry.offers`).
        self._offers = offers
        #: Whether the device itself enabled `group` (narrowed here or not), as it last said.
        self._enabled_on_device = enabled_on_device
        self._sessions = sessions
        self._turn = turn
        self._capture_refusal = capture_refusal
        self._publish = publish
        self._audit = audit
        self._overlay = overlay
        self._disconnect = disconnect_credential
        self._online = online
        #: The owner's second key for `voice` on this node, from the database (blocking: read in
        #: a worker thread). Default and on any failure: no.
        self._opted_in = opted_in
        self._clock = clock
        self._nodes: dict[str, _Node] = {}
        #: Nodes holding an overlay credential issued by this core since it started.
        self.issued: set[str] = set()
        self._background: set[asyncio.Task[Any]] = set()

    def _node(self, device_id: str) -> _Node:
        node = self._nodes.get(device_id)
        if node is None:
            node = self._nodes[device_id] = _Node()
        return node

    # -- what a node sends -----------------------------------------------------------------------

    async def handle(self, session: Any, method: str, params: BaseModel) -> dict[str, Any] | None:
        if method == p.M_VOICE_HEARD:
            assert isinstance(params, p.VoiceHeard)
            return await self._heard(session, params)
        if method == p.M_VOICE_LEVEL:
            assert isinstance(params, p.VoiceLevel)
            self._level(session.device_id, params)
            return None
        if method == p.M_OVERLAY_CREDENTIAL:
            return await self._credential(session.device_id)
        return {"accepted": False, "reason": "unknown"}

    async def _refusal(self, device_id: str) -> str | None:
        if not self._offers(device_id, "voice.say"):
            return "voice_not_enabled"  # on the device, or switched off here
        try:
            opted_in = bool(await asyncio.to_thread(self._opted_in, device_id, "voice"))
        except Exception:  # noqa: BLE001 - cannot tell: no
            opted_in = False
        if not opted_in:
            # Checked again here, whatever the routing says: without the owner's second key in
            # the Devices page no word from this node becomes a turn.
            return "voice_not_switched_on_here"
        if self._turn is None:
            return "no_assistant"
        why = self._capture_refusal()
        if why is not None:
            return "microphone_off"
        node = self._node(device_id)
        now = self._clock()
        while node.utterances and node.utterances[0] <= now - 60.0:
            node.utterances.popleft()
        if len(node.utterances) >= MAX_UTTERANCES:
            return "too_many"
        node.utterances.append(now)
        return None

    async def _heard(self, session: Any, heard: p.VoiceHeard) -> dict[str, Any]:
        device_id = session.device_id
        reason = await self._refusal(device_id)
        self._record(
            "node.voice_heard",
            device_id,
            "deny" if reason else "allow",
            reason or f"{len(heard.text)} chars",
        )
        if reason is not None:
            return {"accepted": False, "reason": reason}
        node = self._node(device_id)
        if node.task is not None and not node.task.done():
            node.task.cancel()  # a new question: the old answer is not wanted any more
        task = asyncio.get_running_loop().create_task(
            self._answer(session, heard), name=f"nox-node-voice-{device_id[:8]}"
        )
        node.task = task
        self._background.add(task)
        task.add_done_callback(self._background.discard)
        return {"accepted": True}

    async def _answer(self, session: Any, heard: p.VoiceHeard) -> None:
        """One turn, its answer sent sentence by sentence as it is written."""
        from nox.core.orchestrator import Orchestrator  # noqa: PLC0415
        from nox.memory.capture import turn_origin  # noqa: PLC0415

        assert self._turn is not None
        seq = 0
        buffer = ""
        first = True

        async def send(text: str, *, last: bool) -> None:
            nonlocal seq
            if seq >= 200:
                return
            say = p.VoiceSay(
                turn_id=heard.turn_id,
                seq=seq,
                text=text[: p.MAX_SAY_CHARS],
                language=heard.language,
                last=last,
            )
            seq += 1
            with contextlib.suppress(Exception):
                await session.client.notify(p.M_VOICE_SAY, say.model_dump())

        async def on_chunk(delta: str) -> None:
            nonlocal buffer, first
            buffer += delta
            sentences, buffer = Orchestrator._split(buffer, final=False, first=first)  # noqa: SLF001
            for sentence in sentences:
                first = False
                await send(sentence, last=False)

        channel = f"{VOICE_NODE_CHANNEL}{session.device_id}"
        try:
            with turn_origin("node"):
                await self._turn(heard.text, heard.language, on_chunk, channel)
        except asyncio.CancelledError:
            with contextlib.suppress(Exception):
                await session.client.notify(p.M_VOICE_CANCEL, {"turn_id": heard.turn_id})
            raise
        except Exception as exc:  # noqa: BLE001 - the node hears that it failed, not silence
            log.warning("node.voice_turn_failed", error=type(exc).__name__)
            await send(FAILED.get(heard.language, FAILED["en"]), last=True)
            return
        rest, _ = Orchestrator._split(buffer, final=True)  # noqa: SLF001
        for index, sentence in enumerate(rest):
            await send(sentence, last=index == len(rest) - 1)
        if not rest:
            await send("", last=True)

    def _level(self, device_id: str, level: p.VoiceLevel) -> None:
        if not self._offers(device_id, "voice.say"):
            return
        self._publish(
            "tts.level",
            {
                "utterance_id": f"node:{level.turn_id or device_id[:12]}",
                "channel": "private",
                "level": level.level,
                "device": device_id,
            },
        )

    async def _credential(self, device_id: str) -> dict[str, Any]:
        if self._overlay is None:
            return {"ok": False, "reason": "no_overlay"}
        if not self._offers(device_id, "obs.request"):
            return {"ok": False, "reason": "obs_not_enabled"}
        node = self._node(device_id)
        now = self._clock()
        if now - node.credential_at < CREDENTIAL_INTERVAL_S:
            return {"ok": False, "reason": "too_soon"}
        node.credential_at = now
        label = node_overlay_label(device_id)
        revoked = await asyncio.to_thread(self._overlay.revoke_label, label)
        credential, token = await asyncio.to_thread(self._overlay.issue, label)
        if revoked and self._disconnect is not None:
            await self._disconnect(list(revoked))
        self.issued.add(device_id)
        self._record("node.overlay_credential", device_id, "allow", credential.credential_id)
        return {"ok": True, "token": token}

    # -- what the core tells nodes ---------------------------------------------------------------

    async def revoke_overlay(self, device_id: str, reason: str) -> int:
        """The node was removed, or `obs` narrowed for it: its overlay pages go dark now."""
        if self._overlay is None:
            return 0
        self.issued.discard(device_id)
        label = node_overlay_label(device_id)
        revoked = await asyncio.to_thread(self._overlay.revoke_label, label)
        if revoked and self._disconnect is not None:
            await self._disconnect(list(revoked))
        if revoked:
            self._record("node.overlay_revoked", device_id, "allow", reason[:64])
        return len(revoked)

    async def sweep(self) -> None:
        """A node that is connected but no longer offers `obs` (switched off on the device or
        here) keeps no overlay credential."""
        for device_id in list(self.issued):
            if self._online(device_id) and not self._offers(device_id, "obs.request"):
                await self.revoke_overlay(device_id, "obs switched off")

    async def push_gates(self) -> None:
        """Tell every connected node with voice whether its microphone may listen - only when
        that changed for its connection (and always on a new one)."""
        why = self._capture_refusal()
        capture, reason = why is None, why or ""
        for session in list(self._sessions()):
            device_id = session.device_id
            if not getattr(session, "ready", None) or not session.ready.is_set():
                continue
            allowed = capture and self._offers(device_id, "voice.say")
            state = (session, allowed, "" if allowed else (reason or "voice_not_enabled"))
            node = self._node(device_id)
            if node.told == state:
                continue
            node.told = state
            with contextlib.suppress(Exception):
                await session.client.notify(
                    p.M_VOICE_GATE, {"capture": allowed, "reason": state[2][:64]}
                )
            if not allowed and node.task is not None and not node.task.done():
                node.task.cancel()

    async def panic(self, reason: str = "panic") -> int:
        """`security.panic`: OBS on every node that enabled `obs` goes to its privacy scene."""
        sent = 0
        for session in list(self._sessions()):
            if not self._enabled_on_device(session.device_id, "obs"):
                continue
            with contextlib.suppress(Exception):
                await session.client.notify(p.M_OBS_PANIC, {"reason": reason[:64]})
                sent += 1
        return sent

    def disconnected(self, device_id: str) -> None:
        node = self._nodes.get(device_id)
        if node is None:
            return
        node.told = None
        if node.task is not None and not node.task.done():
            node.task.cancel()

    async def close(self) -> None:
        for task in list(self._background):
            task.cancel()
        await asyncio.gather(*self._background, return_exceptions=True)

    def _record(self, action: str, device_id: str, decision: str, result: str) -> None:
        if self._audit is None:
            return
        with contextlib.suppress(Exception):
            self._audit.append(
                actor="node",
                tool="node",
                action=action,
                target=f"device:{device_id}",
                decision=decision,
                result=result,
            )


# ---- the routes: `voice.say` on a node, `obs.request` where OBS runs ----------------------------

#: Longest permission target; longer is refused, never cut (as `nox.node.install`).
MAX_TARGET_CHARS = 1000


def _say_target(payload: dict[str, Any]) -> str:
    from nox.node.protocol import render  # noqa: PLC0415
    from nox.node.router import CannotShow  # noqa: PLC0415

    text = "text " + render(str(payload.get("text") or ""))
    if len(text) > MAX_TARGET_CHARS:
        raise CannotShow("the text is too long to be shown in full")
    return text


def voice_say_spec(registry: Any, defaults: Callable[[], Any]) -> Any:
    """`voice.say`: Nox says something on a paired device's speaker (its voice satellite)."""
    from nox.node.router import routed_spec  # noqa: PLC0415
    from nox.node.voice import SayArgs  # noqa: PLC0415
    from nox.security.model import Risk  # noqa: PLC0415

    return routed_spec(
        name="voice.say",
        description="Say something out loud on a paired device with a voice satellite "
        "(`device`), e.g. the living room speaker.",
        risk=Risk.LOW,
        side_effects=True,
        registry=registry,
        input_model=SayArgs,
        defaults=defaults,
        node_target=_say_target,
    )


def _obs_scope(payload: dict[str, Any]) -> Any:
    """The core's classification of a node's OBS request. The core knows neither that device's
    privacy scene nor its scene set: every scene switch counts as outside the set (high, asked on
    the device), and the device checks its privacy scene itself before anything changes."""
    from nox.node.obs import NO_SCENE_SET  # noqa: PLC0415
    from nox.stream.obs_requests import classify  # noqa: PLC0415

    data = payload.get("request_data")
    return classify(
        str(payload.get("request_type") or ""),
        data if isinstance(data, dict) else None,
        scene_set=(NO_SCENE_SET,),
    )


def _obs_precheck(payload: dict[str, Any]) -> str | None:
    from nox.security.model import Risk  # noqa: PLC0415
    from nox.stream.obs_requests import InvalidObsRequestError, refusal  # noqa: PLC0415

    if refusal(str(payload.get("request_type") or "")) is not None:
        return "obs_request_never_possible"
    try:
        scope = _obs_scope(payload)
    except InvalidObsRequestError:
        return "invalid_request_type"
    return "obs_request_never_possible" if scope.risk is Risk.CRITICAL else None


def _obs_risk(payload: dict[str, Any]) -> Any:
    from nox.security.model import Risk  # noqa: PLC0415
    from nox.stream.obs_requests import InvalidObsRequestError  # noqa: PLC0415

    try:
        risk = _obs_scope(payload).risk
    except InvalidObsRequestError:
        return Risk.CRITICAL
    return Risk.LOW if risk is Risk.READ else risk


def _obs_action(payload: dict[str, Any]) -> str:
    from nox.stream.obs_requests import InvalidObsRequestError  # noqa: PLC0415

    try:
        return str(_obs_scope(payload).action)
    except InvalidObsRequestError:
        return "request.invalid"


def _obs_target(payload: dict[str, Any]) -> str:
    from nox.node.router import CannotShow  # noqa: PLC0415
    from nox.stream.obs_requests import InvalidObsRequestError  # noqa: PLC0415

    try:
        return str(_obs_scope(payload).target)
    except InvalidObsRequestError as exc:
        raise CannotShow(str(exc)[:200], error="invalid_request_type") from None


def obs_route(registry: Any, defaults: Callable[[], Any]) -> Callable[[Any], Any]:
    """`obs.request` as one tool: this core's OBS (the plugin, while it is there) or the paired
    PC where OBS runs - the same request allow-list and risk either way (`nox.node.obs`)."""
    from nox.node.obs import ObsArgs  # noqa: PLC0415
    from nox.node.router import routed_spec  # noqa: PLC0415
    from nox.security.model import Risk  # noqa: PLC0415
    from nox.stream.obs_requests import TOOL_GUIDE  # noqa: PLC0415

    def build(own: Any) -> Any:
        guide = own.description if own is not None else TOOL_GUIDE
        return routed_spec(
            name="obs.request",
            description=guide + " On this core's OBS or on the paired PC where OBS runs "
            "(`device`).",
            risk=Risk.LOW,  # the floor: each call's risk is the classifier's
            side_effects=True,
            registry=registry,
            core=own,
            input_model=ObsArgs,
            defaults=defaults,
            node_target=_obs_target,
            node_risk=_obs_risk,
            node_action=_obs_action,
            precheck=_obs_precheck,
        )

    return build
