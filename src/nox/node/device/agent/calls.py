"""The middle layer of `NodeAgent`: running one call and stopping calls."""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import time
from collections.abc import Awaitable
from typing import Any

from pydantic import BaseModel, ValidationError
from websockets.asyncio.client import ClientConnection
from websockets.exceptions import ConnectionClosed

from nox.core.logging import get_logger
from nox.node.device.agent.base import ERR_INVALID_PARAMS, AgentBase
from nox.node.device.agent.dialog_text import confirmation_text
from nox.node.device.confirm import has_screen
from nox.node.device.tools import NodeRefusal, NodeTool
from nox.node.wire import protocol as p
from nox.node.wire.offload import off_loop
from nox.node.wire.receipts import normalize_arguments

log = get_logger(__name__)


#: Largest result sent in one frame; the protocol caps frames at 1 MiB.
MAX_RESULT_CHARS = 512 * 1024


#: No word from the core for this long while a side effect runs: abort it.
DEAD_MAN_S = 3.0


DEAD_MAN_TICK_S = 0.25


LOCAL_STOP_POLL_S = 0.2


#: A tool result's field holding bytes for the core (a capture), sent as `nox/blob` chunks.
BLOB_KEY = "_blob"


#: A "yes" faster than this after the question appeared is a reflex or a stray key, not a
#: decision: it counts as no.
MIN_DECISION_S = 1.5


#: Consecutive pings from a core that is not stopped after which a dead-man stop lifts.
DEAD_MAN_RESET_PINGS = 3


class DeadManError(Exception):
    """The core went silent while a side effect ran."""


class CallRunner(AgentBase):
    """One call from the core, from the gate to the answer: the question on the device, the
    dead-man rule, blob chunks, and the cancelling of running calls."""

    async def _cancel_input_calls(self, ws: ClientConnection, *, own: str) -> None:
        """Before a question appears, every other running call with a side effect is stopped -
        none may act on the dialog."""
        doomed = {
            key: entry
            for key, entry in self._calls.items()
            if key != own and self._gate.risk_of(entry[2]) != "read"
        }
        for task, _msg_id, _name in doomed.values():
            task.cancel()
        for task, msg_id, name in doomed.values():
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
            await self._refused(ws, msg_id, name, "dialog_open")

    async def _cancel_calls(self, ws: ClientConnection | None = None) -> list[str]:
        """Cancel every running call; with `ws`, each is answered "stopped". Their ids. A stop,
        a lost connection and the dead-man rule also end what outlives a call (a computer-use
        session, `on_stop`)."""
        try:
            self.on_stop("stopped")
        except Exception as exc:  # noqa: BLE001 - stopping goes on whatever a hook does
            log.error("node.on_stop_failed", error=type(exc).__name__)
        running = dict(self._calls)
        for task, _msg_id, _name in running.values():
            task.cancel()
        for task, _msg_id, _name in running.values():
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        if ws is not None:
            for _task, msg_id, _name in running.values():
                await self._refused(ws, msg_id, "call", "stopped")
        return [k for k in running if not k.startswith("#")]

    async def _watch_local_stop(self, ws: ClientConnection) -> None:
        """`nox node stop` on this device: stop here at once, and ask the core to stop all."""
        while True:
            stopped = self._home.stop_file.exists()
            if stopped and not self._local_stopped:
                self._local_stopped = True
                calls = await self._cancel_calls(ws)
                self._audit_event("node.local_stop", "local", f"stopped {len(calls)} call(s)")
                with contextlib.suppress(ConnectionClosed):
                    await ws.send(
                        json.dumps(
                            {
                                "jsonrpc": "2.0",
                                "method": p.M_SECURITY_KILL,
                                "params": {"reason": "stopped on the device"},
                            }
                        )
                    )
            elif not stopped and self._local_stopped:
                self._local_stopped = False
                self._audit_event("node.local_resume", "local", "ok")
            await asyncio.sleep(LOCAL_STOP_POLL_S)

    def _after_core_ping(self) -> None:
        """The core is back: after `DEAD_MAN_RESET_PINGS` pings in a row while it is not stopped,
        a dead-man stop lifts (the core learns the state from every `nox/pong`)."""
        if not self.dead_man or self.core_killed:
            self._good_pings = 0
            return
        self._good_pings += 1
        if self._good_pings >= DEAD_MAN_RESET_PINGS:
            self.dead_man = False
            self._good_pings = 0
            self._audit_event("node.dead_man_lifted", "core", "pings resumed")

    async def _call(self, ws: ClientConnection, msg_id: Any, params: dict[str, Any]) -> None:
        name = params.get("name")
        try:
            meta = p.NoxCallMeta.model_validate(params.get("_nox") or {})
        except ValidationError:
            await self._reply(ws, msg_id, error=(ERR_INVALID_PARAMS, "missing or invalid _nox"))
            return
        if not isinstance(name, str):
            await self._reply(ws, msg_id, error=(ERR_INVALID_PARAMS, "missing tool name"))
            return
        if not self.compatible and name != "system.info":
            decision_reason = "update_needed"
            await self._refused(ws, msg_id, name, decision_reason)
            return
        # One text for everything: the receipt's hash, the dialog and the handler all see the
        # same NFC form of the arguments (review: never sign one and run another).
        arguments = normalize_arguments(params.get("arguments"))
        # The slow text check first, off the event loop and stateless: pings, the stop and the
        # dead man keep running, and a check that runs out of time is simply abandoned - it
        # never spends budget, consumes a receipt or records an "allow".
        try:
            early = await off_loop(self._gate.precheck, name, arguments)
        except TimeoutError:
            early = "check_timeout"
        if early is not None:
            self._audit_refusal(name, meta.call_id, p.safe_name(early, 64))
            await self._refused(ws, msg_id, name, early)
            return
        decision = self._gate.check(
            name, arguments, call_id=meta.call_id, meta=meta, prechecked=True
        )
        if not decision.allowed or decision.tool is None or decision.args is None:
            await self._refused(ws, msg_id, name, decision.reason)
            return
        if decision.needs_local_confirmation:
            refusal = await self._confirm_here(ws, decision.tool, decision.args, meta)
            if refusal is not None:
                await self._refused(ws, msg_id, name, refusal)
                return
        try:
            work = decision.tool.handler(decision.args)
            if decision.tool.risk != "read":
                result = await self._under_dead_man(work)
            else:
                result = await asyncio.wait_for(work, 30)
        except DeadManError:
            self.dead_man = True
            with contextlib.suppress(Exception):
                self.on_stop("dead_man")
            self._audit_event("node.dead_man", "local", p.safe_name(name))
            await self._refused(ws, msg_id, name, "dead_man")
            return
        except NodeRefusal as exc:
            # The hand's own boundary (outside the allowed folders, no sandbox): a refusal on
            # this device, audited like the gate's.
            reason = p.safe_name(exc.reason, 64)
            self._audit_refusal(name, meta.call_id, reason)
            await self._refused(ws, msg_id, name, reason, detail=exc.detail)
            return
        except Exception as exc:  # noqa: BLE001 - a failing tool is an error result, not a crash
            log.warning("node.tool_failed", tool=p.safe_name(name), error=type(exc).__name__)
            await self._reply(
                ws,
                msg_id,
                result={
                    "isError": True,
                    "content": [{"type": "text", "text": f"{type(exc).__name__}"}],
                },
            )
            return
        blob = result.pop(BLOB_KEY, None) if isinstance(result, dict) else None
        if isinstance(blob, bytes):
            # A capture: its bytes go to the core that asked, in chunks, and are dropped here
            # with this call - the node keeps no copy.
            sent = await self._send_blob(ws, meta.call_id, blob)
            del blob
            if not sent:
                await self._reply(
                    ws,
                    msg_id,
                    result={
                        "isError": True,
                        "content": [{"type": "text", "text": "capture too large"}],
                    },
                )
                return
        text = json.dumps(result, ensure_ascii=False)
        if len(text) > MAX_RESULT_CHARS:
            # Too large for one frame: an honest error rather than a cut result or a broken link.
            await self._reply(
                ws,
                msg_id,
                result={"isError": True, "content": [{"type": "text", "text": "result too large"}]},
            )
            return
        await self._reply(
            ws,
            msg_id,
            result={"content": [{"type": "text", "text": text}], "structuredContent": result},
        )

    async def _send_blob(self, ws: ClientConnection, call_id: str, data: bytes) -> bool:
        """`nox/blob` chunks of one result, before its answer; False when it is too large."""
        if not data or len(data) > p.MAX_RESULT_BYTES:
            return False
        size = p.BLOB_CHUNK_BYTES
        pieces = [data[i : i + size] for i in range(0, len(data), size)]
        for seq, piece in enumerate(pieces):
            params = {
                "call_id": call_id,
                "seq": seq,
                "last": seq == len(pieces) - 1,
                "data": base64.b64encode(piece).decode("ascii"),
            }
            with contextlib.suppress(ConnectionClosed):
                await ws.send(json.dumps({"jsonrpc": "2.0", "method": p.M_BLOB, "params": params}))
        return True

    async def _under_dead_man(self, work: Awaitable[dict[str, Any]]) -> dict[str, Any]:
        """Run a call with a side effect; abort it when the core went silent for `DEAD_MAN_S`."""
        task: asyncio.Task[dict[str, Any]] = asyncio.ensure_future(work)
        try:
            deadline = time.monotonic() + 30
            while True:
                done, _ = await asyncio.wait({task}, timeout=DEAD_MAN_TICK_S)
                if done:
                    return task.result()
                if time.monotonic() - self._last_core > DEAD_MAN_S:
                    raise DeadManError
                if time.monotonic() > deadline:
                    raise TimeoutError
        finally:
            if not task.done():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task

    async def _confirm_here(
        self, ws: ClientConnection, tool: NodeTool, args: BaseModel, meta: p.NoxCallMeta
    ) -> str | None:
        """High risk: the person at this device decides. None when they allowed it."""
        if not self._confirmer.available:
            # `trust_core_for_high` trusts a confirmation a PERSON gave on the core. A call the
            # core left to this device, or allowed by a rule or a remembered answer, was
            # confirmed by nobody for this call: refuse it. And only a device
            # WITHOUT a screen trusts at all: one with a screen but no working dialog program
            # refuses (high risk is confirmed on the device that has a screen).
            screen = getattr(self._confirmer, "screen", None)
            headless = not (has_screen() if screen is None else bool(screen))
            trusted = self._gate.manifest.trust_core_for_high and headless
            if trusted and not meta.confirm_on_device and meta.decided_by == "user":
                self._audit_event("node.confirm", "core", "trusted (no screen)")
                return None
            return "high_needs_local_confirmation"
        shown = tool.shown(args) if tool.shown is not None else args.model_dump()
        text = confirmation_text(tool.name, shown, tainted=meta.tainted)
        if text is None:
            # Never ask about something the person cannot see in full.
            self._audit_event("node.confirm", "local", f"{p.safe_name(tool.name)}:cannot_show")
            return "cannot_show_in_full"
        self._dialogs += 1  # from here on no new input call starts
        await self._cancel_input_calls(ws, own=meta.call_id)
        shown_at = time.monotonic()
        try:
            answer = await self._confirmer.ask(text)
        except Exception as exc:  # noqa: BLE001 - a broken dialog is a "no", never a "yes"
            log.warning("node.confirm_failed", error=type(exc).__name__)
            answer = "deny"
        finally:
            self._dialogs -= 1
        if answer == "allow" and time.monotonic() - shown_at < MIN_DECISION_S:
            answer = "deny"  # too fast to have been read: a reflex or a stray key
        self._audit_event("node.confirm", "local", f"{p.safe_name(tool.name)}:{answer}")
        with contextlib.suppress(ConnectionClosed):
            await ws.send(
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "method": p.M_CONFIRM_RESULT,
                        "params": {"call_id": meta.call_id, "decision": answer},
                    }
                )
            )
        if answer != "allow":
            return "denied_on_device" if answer == "deny" else "confirmation_timeout"
        if self.stopped_reason() is not None:
            return self.stopped_reason()  # stopped while the dialog was open
        return None
