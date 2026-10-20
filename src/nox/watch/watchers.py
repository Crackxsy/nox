"""Watchers: the table, and the loop that checks them and reports what it finds.

Built like the reminders next to it (`nox.day.reminders`): a table rather than a task, so a watcher
survives a restart; a poll rather than a timer per watcher, so a laptop that slept is simply late;
and the record is written *before* Nox speaks, so a crash mid-sentence never repeats a report.

What a watcher may call is the part that matters. It runs unattended, for days, so it gets exactly
one tool, and only one that cannot change anything: an explicit allow-list, not a risk ceiling. A
tool qualifies when it declares no side effects *and* is risk `read`, or is one of the named
read-only fetchers in `WATCHABLE_LOW_RISK_TOOLS` (`web.fetch`, `web.search`). A `low` tool with
side effects (`skill.remove`, `watch.delete`) never qualifies, however harmless its risk sounds.
A fetcher reaches the network with the same arguments every few minutes, so `watch.create` - never
rememberable - shows the user the address it will call before the watcher exists. That is checked
when it is created *and* before every run - a tool can be re-registered with a higher risk later
(a plugin update, an MCP server the user marked differently), and a watcher must never start
acting on the world because something else changed. The call goes through the `ToolExecutor` like
any other, so the permission engine, the privacy mode and the audit log apply to it as well.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

from nox.core.logging import get_logger
from nox.data.db import Database
from nox.data.repos import Row, written_row
from nox.security.model import Risk
from nox.security.reserved import untrusted_text
from nox.watch.condition import MISSING, Op, evaluate, parse_path, preview, resolve, value_hash

log = get_logger(__name__)

__all__ = [
    "WATCHABLE_LOW_RISK_TOOLS",
    "WatchError",
    "WatcherRepository",
    "WatcherRow",
    "WatcherScheduler",
    "watchable_refusal",
]

#: The only `low`-risk tools a watcher may use: read-only fetchers whose one effect - a request to
#: the address in their arguments - the user saw and confirmed when the watcher was created.
WATCHABLE_LOW_RISK_TOOLS: frozenset[str] = frozenset({"web.fetch", "web.search"})
MAX_TITLE_CHARS = 200
MAX_ARGS_BYTES = 4000


class WatchError(ValueError):
    """A watcher that cannot be created as asked; the message says why, in words."""


class ToolLike(Protocol):
    @property
    def name(self) -> str: ...
    @property
    def risk(self) -> Risk: ...
    @property
    def side_effects(self) -> bool: ...


class WatcherRow(Row):
    id: str
    title: str
    tool: str
    args: dict[str, Any]
    path: str
    near: str
    op: Op
    value: Any
    interval_min: int
    cooldown_min: int
    created_at: datetime
    source: str = ""
    next_check_at: datetime
    last_checked_at: datetime | None = None
    last_value: str = ""
    last_fired_at: datetime | None = None
    last_fired_hash: str | None = None
    last_error: str = ""
    checks: int = 0
    fired: int = 0


def _utc(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.astimezone()
    return value.astimezone(UTC).isoformat(timespec="seconds")


def _now() -> datetime:
    return datetime.now(UTC)


def watchable_refusal(tool: ToolLike | None, name: str) -> str | None:
    """Why `name` cannot back a watcher, or None when it can."""
    if tool is None:
        return f"there is no tool named {name!r}"
    if name.startswith("watch."):
        return "a watcher cannot watch the watchers"
    # Unknown means "it may change something": a watcher never guesses in the tool's favour.
    if getattr(tool, "side_effects", True):
        return f"{name} can change something; a watcher may only use a tool that only reads"
    if tool.risk is not Risk.READ and not (
        tool.risk is Risk.LOW and name in WATCHABLE_LOW_RISK_TOOLS
    ):
        return (
            f"{name} has risk {tool.risk.value}; a watcher may only use a read-only tool "
            "(risk read, or web.fetch / web.search), because it runs unattended"
        )
    return None


class WatcherRepository:
    def __init__(self, db: Database) -> None:
        self._db = db

    def _row(self, raw: Any) -> WatcherRow:
        data = dict(raw)
        data["args"] = json.loads(data["args"] or "{}")
        data["value"] = json.loads(data["value"] or "null")
        data.pop("deleted_at", None)
        return WatcherRow(**data)

    def add(
        self,
        *,
        title: str,
        tool: str,
        args: dict[str, Any],
        path: str,
        near: str,
        op: Op,
        value: Any,
        interval_min: int,
        cooldown_min: int,
        source: str,
        now: datetime | None = None,
    ) -> WatcherRow:
        cleaned = " ".join(title.split())[:MAX_TITLE_CHARS]
        if not cleaned:
            raise WatchError("a watcher needs a title that says what to report")
        parse_path(path)
        args_json = json.dumps(args, ensure_ascii=False, sort_keys=True)
        if len(args_json) > MAX_ARGS_BYTES:
            raise WatchError("the tool arguments are too large for a watcher")
        start = now or _now()
        wid = uuid.uuid4().hex[:12]
        self._db.execute(
            "INSERT INTO watchers (id, title, tool, args, path, near, op, value, interval_min, "
            "cooldown_min, created_at, source, next_check_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                wid,
                cleaned,
                tool,
                args_json,
                path.strip(),
                near.strip(),
                op.value,
                json.dumps(value, ensure_ascii=False),
                interval_min,
                cooldown_min,
                _utc(start),
                source,
                _utc(start),
            ),
        )
        return written_row(self.get(wid), "watchers", wid)

    def get(self, watcher_id: str) -> WatcherRow | None:
        row = self._db.fetch_one(
            "SELECT * FROM watchers WHERE id = ? AND deleted_at IS NULL", (watcher_id,)
        )
        return None if row is None else self._row(row)

    def list_active(self) -> list[WatcherRow]:
        rows = self._db.fetch_all(
            "SELECT * FROM watchers WHERE deleted_at IS NULL ORDER BY created_at"
        )
        return [self._row(r) for r in rows]

    def count_active(self) -> int:
        row = self._db.fetch_one("SELECT COUNT(*) AS n FROM watchers WHERE deleted_at IS NULL")
        return int(row["n"]) if row is not None else 0

    def list_due(self, now: datetime) -> list[WatcherRow]:
        rows = self._db.fetch_all(
            "SELECT * FROM watchers WHERE deleted_at IS NULL AND next_check_at <= ? "
            "ORDER BY next_check_at",
            (_utc(now),),
        )
        return [self._row(r) for r in rows]

    def delete(self, watcher_id: str, now: datetime | None = None) -> bool:
        cur = self._db.execute(
            "UPDATE watchers SET deleted_at = ? WHERE id = ? AND deleted_at IS NULL",
            (_utc(now or _now()), watcher_id),
        )
        return cur.rowcount == 1

    def record_check(
        self,
        watcher: WatcherRow,
        now: datetime,
        *,
        last_value: str,
        error: str,
        fired_hash: str | None,
        keep_fired_hash: bool,
    ) -> None:
        """One check done: schedule the next one and keep what was seen."""
        next_check = now + timedelta(minutes=watcher.interval_min)
        if keep_fired_hash:
            self._db.execute(
                "UPDATE watchers SET last_checked_at = ?, next_check_at = ?, last_value = ?, "
                "last_error = ?, checks = checks + 1 WHERE id = ?",
                (_utc(now), _utc(next_check), last_value, error, watcher.id),
            )
            return
        self._db.execute(
            "UPDATE watchers SET last_checked_at = ?, next_check_at = ?, last_value = ?, "
            "last_error = ?, last_fired_hash = ?, checks = checks + 1 WHERE id = ?",
            (_utc(now), _utc(next_check), last_value, error, fired_hash, watcher.id),
        )

    def mark_fired(self, watcher_id: str, now: datetime, fired_hash: str) -> bool:
        """Claim this report. False when another tick claimed it first (or it was deleted)."""
        cur = self._db.execute(
            "UPDATE watchers SET last_fired_at = ?, last_fired_hash = ?, fired = fired + 1 "
            "WHERE id = ? AND deleted_at IS NULL AND (last_fired_hash IS NULL OR "
            "last_fired_hash != ?)",
            (_utc(now), fired_hash, watcher_id, fired_hash),
        )
        return cur.rowcount == 1


class CallResult(Protocol):
    ok: bool
    data: dict[str, Any] | None
    error: str | None


CallTool = Callable[[str, dict[str, Any]], Awaitable[CallResult]]
LookupTool = Callable[[str], ToolLike | None]
Report = Callable[[WatcherRow, str], Awaitable[None]]


class WatcherScheduler:
    """Checks due watchers and reports each finding once; see the module docstring."""

    def __init__(
        self,
        repo: WatcherRepository,
        *,
        call_tool: CallTool,
        lookup: LookupTool,
        report: Report,
        clock: Callable[[], datetime] = _now,
        poll_s: float = 30.0,
        deliverable: Callable[[], bool] = lambda: True,
        call_timeout_s: float = 30.0,
    ) -> None:
        self._repo = repo
        self._call_tool = call_tool
        self._lookup = lookup
        self._report = report
        self._clock = clock
        self._poll_s = poll_s
        self._deliverable = deliverable
        self._call_timeout_s = call_timeout_s
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        self._task = asyncio.create_task(self._loop(), name="nox-watchers")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    async def tick(self) -> list[str]:
        """Check what is due now. Returns the ids that reported, for tests and logs."""
        if not self._deliverable():
            # A finding nobody would hear would be marked reported and lost.
            return []
        reported: list[str] = []
        for watcher in self._repo.list_due(self._clock()):
            try:
                if await self._check(watcher):
                    reported.append(watcher.id)
            except Exception as exc:  # noqa: BLE001 - one broken watcher never stops the others
                log.warning("watch.check_failed", id=watcher.id, error=type(exc).__name__)
        if reported:
            log.info("watch.reported", count=len(reported))
        return reported

    async def _check(self, watcher: WatcherRow) -> bool:
        now = self._clock()
        refusal = watchable_refusal(self._lookup(watcher.tool), watcher.tool)
        if refusal is not None:
            self._repo.record_check(
                watcher, now, last_value="", error=refusal, fired_hash=None, keep_fired_hash=True
            )
            log.warning("watch.tool_refused", id=watcher.id, tool=watcher.tool)
            return False
        try:
            result = await asyncio.wait_for(
                self._call_tool(watcher.tool, dict(watcher.args)), self._call_timeout_s
            )
        except TimeoutError:
            result = None
        if result is None or not result.ok or result.data is None:
            error = "the tool did not answer in time" if result is None else (result.error or "")
            self._repo.record_check(
                watcher,
                now,
                last_value="",
                error=error or "the tool failed",
                fired_hash=None,
                keep_fired_hash=True,
            )
            return False
        data = result.data
        if data.get("ok") is False:
            error = untrusted_text(data.get("error") or "the tool reported an error", 200)
            self._repo.record_check(
                watcher, now, last_value="", error=error, fired_hash=None, keep_fired_hash=True
            )
            return False
        observed = resolve(data, watcher.path, watcher.near)
        if observed is MISSING:
            self._repo.record_check(
                watcher,
                now,
                last_value="",
                error="the value was not found in the answer",
                fired_hash=None,
                keep_fired_hash=True,
            )
            return False
        shown = untrusted_text(preview(observed), 120)
        current = value_hash(observed)
        if watcher.op is Op.CHANGED and watcher.last_fired_hash is None:
            # The first look sets the baseline; nothing has changed yet.
            self._repo.record_check(
                watcher, now, last_value=shown, error="", fired_hash=current, keep_fired_hash=False
            )
            return False
        holds = evaluate(watcher.op, observed, watcher.value, watcher.last_fired_hash)
        if not holds:
            # A condition that stopped holding is re-armed: the next time it holds is news again.
            # `changed` keeps its baseline.
            self._repo.record_check(
                watcher,
                now,
                last_value=shown,
                error="",
                fired_hash=None,
                keep_fired_hash=watcher.op is Op.CHANGED,
            )
            return False
        self._repo.record_check(
            watcher, now, last_value=shown, error="", fired_hash=None, keep_fired_hash=True
        )
        if current == watcher.last_fired_hash:
            return False  # the same value was reported already
        cooling = watcher.last_fired_at is not None and now - watcher.last_fired_at < timedelta(
            minutes=watcher.cooldown_min
        )
        if cooling:
            return False
        if not self._repo.mark_fired(watcher.id, now, current):
            return False
        try:
            await self._report(watcher, shown)
        except Exception as exc:  # noqa: BLE001 - the report is recorded; a voice failure is logged
            log.warning("watch.report_failed", id=watcher.id, error=type(exc).__name__)
        return True

    async def _loop(self) -> None:
        while True:
            with contextlib.suppress(Exception):
                await self.tick()
            await asyncio.sleep(self._poll_s)
