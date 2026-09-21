"""dashboard request handlers wired in by `nox.proactive.install.install` (see that module's
docstring
for why these live here instead of `app.py`): `health.history` (health-history panel) and
`config.effective` (Settings view, read-only). Neither is about proactivity itself - they are
bundled into this task's one `install(core)` integration point because the task that added them
also owns wiring; a future change can move them to their own module without changing behaviour.
"""

from __future__ import annotations

import re
from datetime import date, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from nox.core.config import NoxConfig
from nox.data.db import Database
from nox.data.repos import HealthHistoryRepository
from nox.ipc.dispatch import RequestContext
from nox.ipc.protocol import ConfigEffective, HealthHistoryEntry, HealthHistoryResult

#: Any dotted key matching this is redacted in `config.effective` (defence in depth: `NoxConfig`
# must never carry secrets per the project standards, but the schema is not guaranteed to stay that
# way).
_SECRET_KEY = re.compile(r"(token|secret|password|api_key)", re.IGNORECASE)


class HealthHistoryRequest(BaseModel):
    limit: int = Field(default=100, ge=1, le=1000)
    component: str = ""


def _redact(value: Any, *, key: str = "") -> Any:
    if _SECRET_KEY.search(key):
        return "***"
    if isinstance(value, dict):
        return {str(k): _redact(v, key=str(k)) for k, v in value.items()}
    if isinstance(value, list):
        return [_redact(v) for v in value]
    if isinstance(value, datetime | date):
        return value.isoformat()
    if isinstance(value, Path):
        return str(value)
    return value


def make_health_history_handler(db: Database) -> Any:
    repo = HealthHistoryRepository(db)

    async def handler(_ctx: RequestContext, p: HealthHistoryRequest) -> dict[str, Any]:
        # ST-08: "code defensively if not yet present" - an empty/missing table is not an error;
        # `list_recent` already returns [] rather than raising when there is nothing to show.
        try:
            rows = repo.list_recent(limit=p.limit, component=p.component or None)
        except Exception:  # noqa: BLE001 - a history panel must degrade, never break the dashboard
            rows = []
        entries = [
            HealthHistoryEntry(
                id=r.id,
                ts=r.ts.isoformat(),
                component=r.component,
                status=r.status,
                reason=r.reason,
            )
            for r in rows
        ]
        return HealthHistoryResult(entries=entries).model_dump(mode="json")

    return handler


def make_config_effective_handler(config: NoxConfig) -> Any:
    async def handler(_ctx: RequestContext, _p: BaseModel) -> dict[str, Any]:
        dumped = config.model_dump(mode="json")
        redacted = _redact(dumped)
        return ConfigEffective(config=redacted).model_dump(mode="json")

    return handler
