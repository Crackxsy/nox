"""`nox.watch`: one read-only tool, a condition, exactly one report per finding, across restarts."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel, ValidationError

from nox.core.config.hands import WatchConfig
from nox.data.db import Database
from nox.security.model import Risk
from nox.tools.registry import ToolRegistry, ToolSpec
from nox.watch.condition import MISSING, Op, evaluate, number, resolve, value_hash
from nox.watch.tools import WatchCreateInput, register_watch_tools
from nox.watch.watchers import WatcherRepository, WatcherRow, WatcherScheduler

NOW = datetime(2026, 10, 20, 7, 0, tzinfo=UTC)


class Clock:
    def __init__(self) -> None:
        self.now = NOW

    def __call__(self) -> datetime:
        return self.now

    def advance(self, minutes: float) -> None:
        self.now += timedelta(minutes=minutes)


@dataclass
class Result:
    ok: bool
    data: dict[str, Any] | None
    error: str | None = None


@dataclass
class Page:
    """A fake web page whose text the test changes, served by a fake read-only tool."""

    text: str = "07:12 RB 40 Nordstadt 5 puenktlich\n07:20 RE 7 Suedhafen 2 puenktlich"
    calls: list[dict[str, Any]] = field(default_factory=list)

    async def call(self, name: str, args: dict[str, Any]) -> Result:
        self.calls.append({"name": name, **args})
        return Result(True, {"ok": True, "content": self.text, "structured": {"price": 19.99}})


class Empty(BaseModel):
    pass


def tool(name: str, risk: Risk, *, side_effects: bool = False, local: bool = True) -> ToolSpec:
    async def handler(_payload: dict[str, Any]) -> dict[str, Any]:
        return {}

    return ToolSpec(
        name=name,
        description="",
        input_model=Empty,
        risk=risk,
        handler=handler,
        side_effects=side_effects,
        local=local,
    )


@pytest.fixture
def db(tmp_path: Path) -> Iterator[Database]:
    database = Database(tmp_path / "nox.db")
    database.migrate()
    yield database
    database.close()


@pytest.fixture
def registry() -> ToolRegistry:
    reg = ToolRegistry()
    reg.register(tool("web.fetch", Risk.LOW, local=False))
    reg.register(tool("file.delete", Risk.HIGH, side_effects=True))
    reg.register(tool("mcp.shop.buy", Risk.MEDIUM, side_effects=True))
    # Low risk, but it changes something: the kind of tool a watcher must never run.
    reg.register(tool("skill.remove", Risk.LOW, side_effects=True))
    # Low risk and side-effect free, but not one of the named fetchers.
    reg.register(tool("mcp.notes.peek", Risk.LOW))
    # Read risk, yet it declares a side effect.
    reg.register(tool("plug.counter.bump", Risk.READ, side_effects=True))
    reg.register(tool("calendar.upcoming", Risk.READ))
    return reg


def scheduler(
    repo: WatcherRepository,
    page: Page,
    registry: ToolRegistry,
    clock: Clock,
    reports: list[tuple[str, str]],
) -> WatcherScheduler:
    async def report(watcher: WatcherRow, value: str) -> None:
        reports.append((watcher.id, value))

    return WatcherScheduler(
        repo, call_tool=page.call, lookup=registry.get, report=report, clock=clock
    )


def add(repo: WatcherRepository, **overrides: Any) -> WatcherRow:
    values: dict[str, Any] = {
        "title": "Dein Zug RB 40 hat Verspaetung",
        "tool": "web.fetch",
        "args": {"url": "http://127.0.0.1/fahrplan.html"},
        "path": "content",
        "near": "RB 40",
        "op": Op.CONTAINS,
        "value": "Min",
        "interval_min": 5,
        "cooldown_min": 60,
        "source": "test",
        "now": NOW,
    }
    values.update(overrides)
    return repo.add(**values)


# ---- the condition -------------------------------------------------------------------------------


def test_paths_anchors_and_numbers() -> None:
    data = {"structured": {"items": [{"price": "12,99 EUR"}]}, "content": "a\nRB 40 x +12 Min\nb"}
    assert resolve(data, "structured.items.0.price") == "12,99 EUR"
    assert resolve(data, "content", "rb 40") == "x +12 Min"
    assert resolve(data, "structured.nope") is MISSING
    assert resolve(data, "content", "ICE 1") is MISSING
    assert number("12,99 EUR") == 12.99 and number(True) is None and number("none") is None
    with pytest.raises(ValueError):
        resolve(data, "a..b")
    with pytest.raises(ValueError):
        resolve(data, "__class__.mro()")


@pytest.mark.parametrize(
    ("op", "observed", "expected", "holds"),
    [
        (Op.LT, "14,99 EUR", 15, True),
        (Op.LT, 15, 15, False),
        (Op.GT, "+12 Min", 5, True),
        (Op.GT, "no number", 5, False),
        (Op.CONTAINS, "x +12 Min", "min", True),
        (Op.CONTAINS, ["a", "B"], "b", True),
        (Op.CONTAINS, "puenktlich", "Min", False),
        (Op.CONTAINS, "anything", "", False),
    ],
)
def test_operators(op: Op, observed: Any, expected: Any, holds: bool) -> None:
    assert evaluate(op, observed, expected, None) is holds


def test_changed_needs_a_baseline() -> None:
    assert evaluate(Op.CHANGED, "a", None, None) is False
    assert evaluate(Op.CHANGED, "a", None, value_hash("a")) is False
    assert evaluate(Op.CHANGED, "b", None, value_hash("a")) is True


# ---- reporting -----------------------------------------------------------------------------------


async def test_a_delay_is_reported_exactly_once_and_the_cooldown_holds(
    db: Database, registry: ToolRegistry
) -> None:
    repo, page, clock, reports = WatcherRepository(db), Page(), Clock(), list[tuple[str, str]]()

    watcher = add(repo)
    sched = scheduler(repo, page, registry, clock, reports)
    assert await sched.tick() == []  # on time
    page.text = "07:12 RB 40 Nordstadt 5 +12 Min"
    clock.advance(5)
    assert await sched.tick() == [watcher.id]
    assert reports == [(watcher.id, "Nordstadt 5 +12 Min")]
    for _ in range(4):  # the same delay is not news, however often it is checked
        clock.advance(5)
        assert await sched.tick() == []
    page.text = "07:12 RB 40 Nordstadt 5 +20 Min"
    clock.advance(5)
    assert await sched.tick() == []  # a new value, but inside the cooldown
    clock.advance(60)
    assert await sched.tick() == [watcher.id]
    assert len(reports) == 2


async def test_a_watcher_is_not_checked_before_its_interval(
    db: Database, registry: ToolRegistry
) -> None:
    repo, page, clock = WatcherRepository(db), Page(), Clock()
    add(repo)
    sched = scheduler(repo, page, registry, clock, [])
    await sched.tick()
    clock.advance(4)
    await sched.tick()
    assert len(page.calls) == 1
    clock.advance(1)
    await sched.tick()
    assert len(page.calls) == 2


async def test_state_survives_a_restart_and_nothing_is_reported_twice(
    tmp_path: Path, registry: ToolRegistry
) -> None:
    path = tmp_path / "nox.db"
    first = Database(path)
    first.migrate()
    page, clock, reports = Page(text="RB 40 x +12 Min"), Clock(), list[tuple[str, str]]()
    watcher = add(WatcherRepository(first))
    assert await scheduler(WatcherRepository(first), page, registry, clock, reports).tick()
    first.close()

    second = Database(path)  # a restart: new connection, new scheduler, same table
    second.migrate()
    clock.advance(5)
    repo = WatcherRepository(second)
    assert await scheduler(repo, page, registry, clock, reports).tick() == []
    assert [r[0] for r in reports] == [watcher.id]
    row = repo.get(watcher.id)
    assert row is not None and row.fired == 1 and row.checks == 2
    second.close()


async def test_a_price_below_the_limit_is_reported_once_until_it_rises_again(
    db: Database, registry: ToolRegistry
) -> None:
    repo, clock, reports = WatcherRepository(db), Clock(), list[tuple[str, str]]()
    page = Page()
    watcher = add(repo, path="structured.price", near="", op=Op.LT, value=15, cooldown_min=0)
    sched = scheduler(repo, page, registry, clock, reports)

    async def price(value: float) -> Result:
        return Result(True, {"ok": True, "structured": {"price": value}})

    prices = iter([19.99, 14.99, 14.99, 19.99, 14.99])

    async def call(_name: str, _args: dict[str, Any]) -> Result:
        return await price(next(prices))

    sched._call_tool = call  # noqa: SLF001 - swap the page for a price series
    fired = []
    for _ in range(5):
        fired += await sched.tick()
        clock.advance(5)
    assert fired == [watcher.id, watcher.id]  # the sale, and the next sale after it ended


async def test_changed_sets_a_baseline_first(db: Database, registry: ToolRegistry) -> None:
    repo, page, clock, reports = (
        WatcherRepository(db),
        Page(text="v1"),
        Clock(),
        list[tuple[str, str]](),
    )

    add(repo, near="", op=Op.CHANGED, value=None, cooldown_min=0)
    sched = scheduler(repo, page, registry, clock, reports)
    assert await sched.tick() == []
    clock.advance(5)
    assert await sched.tick() == []
    page.text = "v2"
    clock.advance(5)
    assert len(await sched.tick()) == 1
    clock.advance(5)
    assert await sched.tick() == []


async def test_a_tool_that_is_no_longer_read_only_is_never_called(
    db: Database, registry: ToolRegistry
) -> None:
    repo, page, clock = WatcherRepository(db), Page(), Clock()
    watcher = add(repo)
    registry.unregister("web.fetch")
    registry.register(tool("web.fetch", Risk.HIGH))  # re-registered with more power
    sched = scheduler(repo, page, registry, clock, [])
    assert await sched.tick() == []
    assert page.calls == []
    row = repo.get(watcher.id)
    assert row is not None and "risk high" in row.last_error


async def test_tool_errors_and_missing_values_are_recorded_not_reported(
    db: Database, registry: ToolRegistry
) -> None:
    repo, clock, reports = WatcherRepository(db), Clock(), list[tuple[str, str]]()
    watcher = add(repo, near="ICE 9")
    sched = scheduler(repo, Page(), registry, clock, reports)
    await sched.tick()
    row = repo.get(watcher.id)
    assert row is not None and "not found" in row.last_error and reports == []


async def test_nothing_is_checked_while_nothing_could_deliver_a_report(
    db: Database, registry: ToolRegistry
) -> None:
    repo, page = WatcherRepository(db), Page(text="RB 40 +12 Min")
    add(repo)

    async def report(_w: WatcherRow, _v: str) -> None:
        raise AssertionError("must not report")

    sched = WatcherScheduler(
        repo,
        call_tool=page.call,
        lookup=registry.get,
        report=report,
        clock=Clock(),
        deliverable=lambda: False,
    )
    assert await sched.tick() == [] and page.calls == []


# ---- the tools -----------------------------------------------------------------------------------


def create_payload(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "title": "Zug RB 40 verspaetet",
        "tool": "web.fetch",
        "args": {"url": "http://x/fahrplan.html"},
        "path": "content",
        "near": "RB 40",
        "op": "contains",
        "value": "Min",
        "value_from": "user",
        "interval_min": 1,
    }
    payload.update(overrides)
    return payload


async def test_watch_create_refuses_a_tool_that_is_not_read_only(
    db: Database, registry: ToolRegistry
) -> None:
    repo = WatcherRepository(db)
    register_watch_tools(registry, repo, WatchConfig, lambda: NOW)
    create = registry.get("watch.create")
    assert create is not None and create.risk is Risk.MEDIUM
    for name in (
        "file.delete",
        "mcp.shop.buy",
        "watch.list",
        "no.such_tool",
        "skill.remove",
        "mcp.notes.peek",
        "plug.counter.bump",
    ):
        answer = await create.handler(create_payload(tool=name))
        assert answer["ok"] is False, name
    assert repo.list_active() == []
    answer = await create.handler(create_payload(tool="calendar.upcoming", args={}))
    assert answer["ok"] is True


async def test_a_low_tool_with_side_effects_is_never_run_by_an_existing_watcher(
    db: Database, registry: ToolRegistry
) -> None:
    repo, page, clock = WatcherRepository(db), Page(), Clock()
    watcher = add(repo, tool="skill.remove", args={"id": "dice"})
    sched = scheduler(repo, page, registry, clock, [])
    assert await sched.tick() == []
    assert page.calls == []  # never called, although its risk is only `low`
    row = repo.get(watcher.id)
    assert row is not None and "change something" in row.last_error


def test_watch_create_is_never_remembered_and_shows_the_address(
    db: Database, registry: ToolRegistry
) -> None:
    register_watch_tools(registry, WatcherRepository(db), WatchConfig, lambda: NOW)
    create = registry.get("watch.create")
    assert create is not None and create.risk is Risk.MEDIUM
    assert create.rememberable is False
    assert create.targets is not None
    target = create.targets(create_payload(args={"url": "https://beacon.example/x?id=1"}))
    assert target == "web.fetch https://beacon.example/x?id=1"


async def test_watch_create_enforces_the_interval_floor_and_lists(
    db: Database, registry: ToolRegistry
) -> None:
    repo = WatcherRepository(db)
    register_watch_tools(registry, repo, WatchConfig, lambda: NOW)
    create, listing, delete = (
        registry.get(n) for n in ("watch.create", "watch.list", "watch.delete")
    )
    assert create is not None and listing is not None and delete is not None
    answer = await create.handler(create_payload())
    assert answer["ok"] and answer["interval_min"] == 5 and "note" in answer
    assert len((await listing.handler({}))["watchers"]) == 1
    assert (await delete.handler({"id": answer["id"]}))["ok"]
    assert (await listing.handler({}))["watchers"] == []


@pytest.mark.parametrize(
    "overrides",
    [
        {"value": None},
        {"value": ""},
        {"value_from": "none"},
        {"value_from": "guess"},
        {"path": "a..b"},
    ],
)
def test_a_threshold_must_come_from_the_user_or_memory(overrides: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        WatchCreateInput.model_validate(create_payload(**overrides))


def test_changed_needs_no_value() -> None:
    WatchCreateInput.model_validate(create_payload(op="changed", value=None, value_from="none"))
