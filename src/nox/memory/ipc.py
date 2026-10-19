"""The `memory.profile.*` requests the dashboard calls: what Nox knows about the user.

A memory the user cannot see is a memory they cannot trust. These requests give the dashboard
the list, a way to add a fact in the user's own words, a way to forget one, and a way to confirm a
fact Nox learned from a phone message or pasted text (until then it stays out of the prompt).
Writes go through `MemoryService`, so the privacy mode decides here exactly as it does for the
model.
"""

from __future__ import annotations

from typing import Annotated, Any

from pydantic import BaseModel, StringConstraints

from nox.core.logging import get_logger
from nox.ipc.dispatch import EmptyPayload, RequestContext, RequestRegistry
from nox.ipc.errors import ERR_PERMISSION, IpcError
from nox.ipc.roles import LOCAL_UI_ROLES, UI_ROLES
from nox.memory.capture import PROFILE_TYPES, UserProfile, confirmed_source, is_unconfirmed
from nox.memory.importance import MemoryType
from nox.memory.items import MemoryService, MemoryWriteRefusedError

log = get_logger(__name__)

__all__ = ["register_memory_ipc"]

#: The two user-facing clients, never a plugin, a worker or a phone.

_PROFILE_TYPE_NAMES = frozenset(t.value for t in PROFILE_TYPES)


class ProfileAdd(BaseModel):
    text: Annotated[str, StringConstraints(strip_whitespace=True, min_length=3, max_length=300)]
    type: str = MemoryType.USER.value


class ProfileForget(BaseModel):
    id: int


class ProfileConfirm(BaseModel):
    id: int


def register_memory_ipc(
    registry: RequestRegistry, profile: UserProfile, memory: MemoryService
) -> None:
    async def p_list(_ctx: RequestContext, _payload: EmptyPayload) -> dict[str, Any]:
        return {
            "items": [
                {
                    "id": row.id,
                    "type": row.type,
                    "text": row.text,
                    "importance": row.importance,
                    "source": row.source,
                    "learned": row.source.startswith("capture"),
                    "unconfirmed": is_unconfirmed(row.source),
                    "created_at": row.created_at.isoformat(timespec="seconds"),
                }
                for row in profile.items()
            ]
        }

    async def p_add(ctx: RequestContext, payload: ProfileAdd) -> dict[str, Any]:
        kind = payload.type if payload.type in _PROFILE_TYPE_NAMES else MemoryType.USER.value
        try:
            # Typed in by the user: as important as "merk dir", which is what it is.
            row = await memory.create(
                payload.text.strip(), type=kind, source=f"user:{ctx.role}", explicit=True
            )
        except MemoryWriteRefusedError as exc:
            raise IpcError(ERR_PERMISSION, str(exc)) from exc
        log.info("memory.profile_added", id=row.id, by=ctx.role)
        return {"ok": True, "id": row.id}

    async def p_forget(ctx: RequestContext, payload: ProfileForget) -> dict[str, Any]:
        known = {row.id for row in profile.items()}
        if payload.id not in known:
            return {"ok": False, "reason": "not a fact about the user"}
        ok = await memory.delete(payload.id, reason=f"forgotten by {ctx.role}")
        log.info("memory.profile_forgotten", id=payload.id, by=ctx.role, ok=ok)
        return {"ok": ok}

    async def p_confirm(ctx: RequestContext, payload: ProfileConfirm) -> dict[str, Any]:
        row = next((r for r in profile.items() if r.id == payload.id), None)
        if row is None or not is_unconfirmed(row.source):
            return {"ok": False, "reason": "not an unconfirmed fact"}
        ok = memory.repo.set_source(row.id, confirmed_source(row.source))
        log.info("memory.profile_confirmed", id=row.id, by=ctx.role, ok=ok)
        return {"ok": ok}

    registry.register("memory.profile.list", EmptyPayload, p_list, roles=UI_ROLES)
    # What Nox knows about its person is changed on the machine it runs on: a fact added here is
    # part of every prompt - and so is one Nox only picked up from a phone message or pasted text
    # once it is confirmed - and a lost phone must not be able to erase what Nox knows either.
    registry.register("memory.profile.confirm", ProfileConfirm, p_confirm, roles=LOCAL_UI_ROLES)
    registry.register("memory.profile.add", ProfileAdd, p_add, roles=LOCAL_UI_ROLES)
    registry.register("memory.profile.forget", ProfileForget, p_forget, roles=LOCAL_UI_ROLES)
