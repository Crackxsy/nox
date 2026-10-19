"""Storing a consent: `consent.grant/list/revoke` as tools and as dashboard IPC requests.

`mic.record_long` refuses unless a consent for its context is stored, so there has to be a way to
store one - and it must not be one the model can use on its own. Two ways in, both deliberate:

* **The dashboard** (`consent.grant` over IPC, the `shell`/`dashboard` roles only): the user types
  who agreed and the exact wording. While a PIN is configured it is required, the same rule as
  relaxing privacy.
* **The tool** (`consent.grant`), for "Nox, note that the class agreed to ...": HIGH risk and never
  rememberable, so every single grant waits for the user's confirmation in the local dashboard
  (confirmations are answered only by the desktop shell). The model cannot store consent by itself.

Every row records who agreed, the wording, when, and how it was recorded (`recorded_by`), so the
record shows whether a person typed it or confirmed a sentence the model proposed. Revoking needs no
PIN or confirmation: anyone may make Nox *more* careful.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from nox.core.logging import get_logger
from nox.hands.consents import ConsentRepository, ConsentRow
from nox.security.model import Risk
from nox.tools.registry import ToolRegistry, ToolSpec

log = get_logger(__name__)

__all__ = ["ConsentGrant", "ConsentRevoke", "register_consent_ipc", "register_consent_tools"]

UI_ROLES: tuple[str, ...] = ("shell", "dashboard")


class ConsentGrant(BaseModel):
    context: str = Field(min_length=1, max_length=200)
    who: str = Field(min_length=1, max_length=200)
    wording: str = Field(min_length=1, max_length=2000)
    #: IPC only: the PIN, required while one is configured.
    pin: str | None = None


class ConsentRevoke(BaseModel):
    context: str = Field(min_length=1, max_length=200)


class _NoInput(BaseModel):
    pass


def _row(row: ConsentRow) -> dict[str, Any]:
    return {
        "id": row.id,
        "context": row.context,
        "who": row.who,
        "wording": row.wording,
        "granted_at": row.granted_at,
        "recorded_by": row.recorded_by,
    }


def _grant_target(p: dict[str, Any]) -> str:
    """What the confirmation shows: the context, who agreed and the exact wording."""
    return f"{p.get('context', '')} | who: {p.get('who', '')} | wording: {p.get('wording', '')}"


def register_consent_tools(registry: ToolRegistry, consents: ConsentRepository) -> None:
    async def grant(payload: dict[str, Any]) -> dict[str, Any]:
        model = ConsentGrant.model_validate(payload)
        row = consents.grant(
            context=model.context,
            who=model.who,
            wording=model.wording,
            recorded_by="conversation, confirmed by the user",
        )
        log.info("consent.granted", via="tool")
        return {"ok": True, "consent": _row(row)}

    async def list_(_payload: dict[str, Any]) -> dict[str, Any]:
        return {"ok": True, "consents": [_row(r) for r in consents.list_active()]}

    async def revoke(payload: dict[str, Any]) -> dict[str, Any]:
        model = ConsentRevoke.model_validate(payload)
        return {"ok": True, "revoked": consents.revoke(model.context)}

    class _GrantInput(BaseModel):
        context: str = Field(min_length=1, max_length=200)
        who: str = Field(min_length=1, max_length=200)
        wording: str = Field(min_length=1, max_length=2000)

    registry.register(
        ToolSpec(
            name="consent.grant",
            description=(
                "Store that the named people agreed to be recorded in one context (needed before "
                "mic.record_long). Give the context key, who agreed and their exact wording. The "
                "user must confirm every grant."
            ),
            input_model=_GrantInput,
            risk=Risk.HIGH,
            handler=grant,
            side_effects=True,
            local=True,
            rememberable=False,
            targets=_grant_target,
        )
    )
    registry.register(
        ToolSpec(
            name="consent.list",
            description="List the stored, active recording consents.",
            input_model=_NoInput,
            risk=Risk.READ,
            handler=list_,
            side_effects=False,
            local=True,
        )
    )
    registry.register(
        ToolSpec(
            name="consent.revoke",
            description="Withdraw the stored consent for one context (the record is kept).",
            input_model=ConsentRevoke,
            risk=Risk.LOW,
            handler=revoke,
            side_effects=True,
            local=True,
            targets=lambda p: str(p.get("context", "")),
        )
    )


def register_consent_ipc(registry: Any, consents: ConsentRepository, gate: Any) -> None:
    """`consent.list/grant/revoke` for the dashboard; `gate` is the PIN gate."""
    from nox.ipc.dispatch import EmptyPayload, RequestContext
    from nox.ipc.errors import ERR_PERMISSION, IpcError
    from nox.security.gate import PinRequiredError

    async def h_list(_ctx: RequestContext, _p: EmptyPayload) -> dict[str, Any]:
        return {"consents": [_row(r) for r in consents.list_active()]}

    async def h_grant(ctx: RequestContext, p: ConsentGrant) -> dict[str, Any]:
        if gate is not None and gate.is_required():
            try:
                await gate.require(p.pin, action="consent.grant", by=ctx.role)
            except PinRequiredError as exc:
                raise IpcError(ERR_PERMISSION, exc.reason) from exc
        row = consents.grant(
            context=p.context, who=p.who, wording=p.wording, recorded_by=f"dashboard ({ctx.role})"
        )
        log.info("consent.granted", via="dashboard")
        return {"ok": True, "consent": _row(row)}

    async def h_revoke(_ctx: RequestContext, p: ConsentRevoke) -> dict[str, Any]:
        return {"ok": True, "revoked": consents.revoke(p.context)}

    registry.register("consent.list", EmptyPayload, h_list, roles=UI_ROLES)
    registry.register("consent.grant", ConsentGrant, h_grant, roles=UI_ROLES)
    registry.register("consent.revoke", ConsentRevoke, h_revoke, roles=UI_ROLES)
