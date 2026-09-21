"""`config.get` / `config.set`: the write half of the dashboard's Settings page.

A `config.set` is accepted per path, not per request: an unknown or non-editable path, and a value
the pydantic models reject, are reported in `errors` while the remaining paths still apply. That
matters for a settings form, where one bad number should not silently discard the four fields next
to it.

Validation is the real thing, never a re-implementation: the candidate user layer is merged onto
the Defaults layer and validated by `NoxConfig` itself, exactly the way `nox.core.config.
load_config` validates the User layer at boot. Only then is `user.yaml` written - a rejected value
never reaches the file.

The audit entry records the *paths* that changed. Values never appear in it, in a log line or in
an event payload: a setting can carry a channel name, a hotkey or a display name, and the audit
log is explicitly designed to hold no private content.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from nox.core.config import NoxConfig, read_yaml_layer
from nox.core.events import E, Event, EventBus
from nox.core.logging import get_logger
from nox.security.gate import SECURITY_SETTING_PREFIXES, PinRequiredError, SecurityChangeGate
from nox.security.model import AuditLog
from nox.settings.layers import deep_merge, load_existing_user_layer, write_user_config
from nox.settings.schema import (
    ConfigFieldSpec,
    UnknownSettingError,
    describe,
    describe_all,
    nest,
    read_value,
)

log = get_logger(__name__)

#: `path -> coroutine(value)`, run after a value validated and before the response is returned.
Applier = Callable[[Any], Awaitable[None]]


def _error_for(path: str, exc: ValidationError) -> str:
    """The message for `path` if pydantic named it, else the first message it did name."""
    messages: list[str] = []
    for err in exc.errors():
        loc = ".".join(str(part) for part in err["loc"])
        if loc == path:
            return str(err["msg"])
        messages.append(f"{loc}: {err['msg']}")
    return "; ".join(messages) or "invalid value"


def security_paths(values: Mapping[str, Any] | Sequence[str]) -> list[str]:
    """The submitted paths that belong to the security or privacy area, in submitted order."""
    return [path for path in values if path.startswith(SECURITY_SETTING_PREFIXES)]


class ConfigEditor:
    """Reads and writes the allow-listed settings of one running core."""

    def __init__(
        self,
        *,
        config: NoxConfig,
        defaults_path: Path,
        user_config_path: Path,
        appliers: Mapping[str, Applier] | None = None,
        audit: AuditLog | None = None,
        bus: EventBus | None = None,
        gate: SecurityChangeGate | None = None,
    ) -> None:
        self._config = config
        self._defaults_path = defaults_path
        self._user_config_path = user_config_path
        self._appliers: dict[str, Applier] = dict(appliers or {})
        self._audit = audit
        self._bus = bus
        self._gate = gate

    # -- read ------------------------------------------------------------------------------------

    def schema(self) -> list[ConfigFieldSpec]:
        return describe_all()

    def values(self) -> dict[str, Any]:
        """Current effective value per editable path, read off the live `NoxConfig`."""
        return {spec.path: read_value(self._config, spec.path) for spec in describe_all()}

    def snapshot(self) -> dict[str, Any]:
        """The `config.get` response."""
        return {
            "values": self.values(),
            "schema": [spec.model_dump(mode="json") for spec in self.schema()],
            "user_config_path": str(self._user_config_path),
        }

    # -- write -----------------------------------------------------------------------------------

    async def apply(
        self, values: Mapping[str, Any], *, by: str = "dashboard", pin: str | None = None
    ) -> dict[str, Any]:
        """Validate, persist and - where the running core can - live-apply a batch of settings.

        Reading the two layers and writing the file are blocking disk work and run in a thread:
        this is an async IPC handler, and a settings form should not stall the event loop.
        """
        await self._authorize(values, by=by, pin=pin)
        defaults, existing = await asyncio.to_thread(self._read_layers)

        patch: dict[str, Any] = {}
        accepted: dict[str, Any] = {}
        errors: dict[str, str] = {}

        for path, value in values.items():
            try:
                candidate = deep_merge(patch, nest(path, self._coerce(path, value)))
            except UnknownSettingError:
                errors[path] = "not an editable setting"
                continue
            try:
                NoxConfig.model_validate(deep_merge(defaults, deep_merge(existing, candidate)))
            except ValidationError as exc:
                errors[path] = _error_for(path, exc)
                continue
            patch = candidate
            accepted[path] = value

        if not accepted:
            return {"ok": not errors, "applied": [], "restart_required": [], "errors": errors}

        await asyncio.to_thread(write_user_config, self._user_config_path, patch)

        applied: list[str] = []
        restart_required: list[str] = []
        for path, value in accepted.items():
            applier = self._appliers.get(path)
            if applier is None:
                restart_required.append(path)
                continue
            try:
                await applier(value)
            except Exception as exc:  # noqa: BLE001 - a failed live apply is not a failed write
                log.warning("settings.live_apply_failed", path=path, error=type(exc).__name__)
                restart_required.append(path)
                continue
            applied.append(path)

        changed = sorted(accepted)
        self._audit_change(changed, by=by)
        log.info("settings.changed", paths=changed, restart_required=restart_required)
        if self._bus is not None:
            await self._bus.publish(Event(name=E.SETTINGS_CHANGED, payload={"paths": changed}))
        return {
            "ok": not errors,
            "applied": applied,
            "restart_required": restart_required,
            "errors": errors,
        }

    # -- helpers ---------------------------------------------------------------------------------

    def _read_layers(self) -> tuple[dict[str, Any], dict[str, Any]]:
        """The two layers a candidate is validated against, read in one thread hop."""
        return read_yaml_layer(self._defaults_path), load_existing_user_layer(
            self._user_config_path
        )

    async def _authorize(self, values: Mapping[str, Any], *, by: str, pin: str | None) -> None:
        """A security or privacy setting needs the PIN, when one is configured.

        These are the paths that can weaken what protects the user: the security profile, the
        capture devices. The gate itself decides whether a PIN is required at all; see
        `nox.security.gate`.
        """
        if self._gate is None or not self._gate.is_required():
            return
        if not security_paths(values):
            return
        try:
            await self._gate.require(pin, action="config.set", by=by)
        except PinRequiredError as exc:
            raise PermissionError(exc.reason) from exc

    def _coerce(self, path: str, value: Any) -> Any:
        """Reject a non-editable path early; the value itself is validated by `NoxConfig`."""
        describe(path)  # raises UnknownSettingError for anything not on the allow-list
        return value

    def _audit_change(self, paths: list[str], *, by: str) -> None:
        if self._audit is None:
            return
        self._audit.append(
            actor=by,
            tool="settings",
            action="config.set",
            target=",".join(paths),
            decision="allow",
            result="ok",
            details={"count": str(len(paths))},
        )
