"""Self-extension 2.0: Nox builds a missing capability as a plugin, checks it, and the user switches
it on with one click - without a restart, and without ever touching Nox's own source tree.

The life of a skill, all of it recorded in `<nox home>/skills/<id>/skill.json` and the audit log:

1. **learn** - a wish and the capability that is missing. One skill is built at a time. The work
   folder is `<nox home>/skills/<id>/work`; nothing else is writable for the builder.
2. **build / check / test**, up to `1 + max_repair_rounds` times - the core's checks
   (`nox.skills.checks`) and the tests (`nox.skills.testing`) decide; their findings are the next
   round's instructions.
3. **ready** - the checked files are frozen as `versions/<n>`, bound by `tree_hash`, and the user
   gets a summary: tools and risks, hosts, secrets, test result.
4. **activate** (the dashboard's one click, never a model's) - the frozen files are checked again,
   the PIN is asked for only when the skill wants a host outside the allow-list or a secret, then
   the version is copied to `<nox home>/plugins/<id>`, enabled in `user.yaml`, and the
   `PluginManager` starts it in its own worker. Its tools ask before they run until the user says
   "remember".
5. **deactivate / forget** - stopped, disabled, and on forget deleted with its versions.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import re
import shutil
import unicodedata
from collections.abc import Awaitable, Callable, Iterable, Mapping
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol

from pydantic import BaseModel, Field

from nox.core.config.hands import SkillsConfig
from nox.core.logging import get_logger
from nox.plugins.manifest import PluginManifest
from nox.security.reserved import name_refusal, untrusted_text
from nox.skills.builder import BuildRequest, SkillBuilder
from nox.skills.checks import check_skill, tree_hash
from nox.skills.testing import TestRunner

log = get_logger(__name__)

__all__ = ["SkillRecord", "SkillService", "SkillState", "skill_id_for"]

RECORD_FILE = "skill.json"


class SkillState(StrEnum):
    BUILDING = "building"
    READY = "ready"
    FAILED = "failed"
    ACTIVE = "active"
    INACTIVE = "inactive"


class SkillTool(BaseModel):
    name: str
    risk: str
    description: str = ""


class SkillSummary(BaseModel):
    tools: list[SkillTool] = Field(default_factory=list)
    hosts: list[str] = Field(default_factory=list)
    new_hosts: list[str] = Field(default_factory=list)
    secrets: list[str] = Field(default_factory=list)
    tests_passed: int = 0
    tests_failed: int = 0
    test_output: str = ""
    needs_pin: bool = False


class SkillRecord(BaseModel):
    id: str
    intent: str
    state: SkillState
    created_at: datetime
    updated_at: datetime
    version: int = 0
    rounds: int = 0
    problems: list[str] = Field(default_factory=list)
    summary: SkillSummary | None = None
    tree_hash: str = ""
    approved_hosts: list[str] = Field(default_factory=list)
    message: str = ""


class PluginControl(Protocol):
    def allow_egress(self, entries: Iterable[str]) -> None: ...
    async def rescan(self) -> None: ...
    async def enable(self, plugin_id: str) -> Any: ...
    async def disable(self, plugin_id: str, reason: str = "disabled") -> None: ...
    async def remove(self, plugin_id: str) -> None: ...


class PinGate(Protocol):
    def is_required(self) -> bool: ...
    async def require(self, pin: str | None, *, action: str, by: str) -> None: ...


class AuditLike(Protocol):
    def append(
        self,
        *,
        actor: str,
        tool: str,
        action: str,
        target: str,
        decision: str,
        result: str,
        task_id: str | None = None,
        details: Mapping[str, str] | None = None,
    ) -> Any: ...


Notify = Callable[[str, str], Awaitable[None]]


def _now() -> datetime:
    return datetime.now(UTC)


def skill_id_for(text: str, taken: Iterable[str]) -> str:
    """A plugin id from a capability name or a wish: lower-case ASCII, unique, not reserved."""
    folded = unicodedata.normalize("NFKD", text.lower()).encode("ascii", "ignore").decode()
    words = [w for w in re.findall(r"[a-z0-9]+", folded) if not w.isdigit()][:3]
    base = "_".join(words).lstrip("0123456789_")[:24] or "skill"
    if len(base) < 2 or name_refusal(base) is not None:
        base = f"skill_{base}"[:24]
    used = set(taken)
    candidate, n = base, 2
    while candidate in used:
        candidate, n = f"{base}_{n}", n + 1
    return candidate


class SkillService:
    """Owns every skill's record and lifecycle; see the module docstring."""

    def __init__(
        self,
        *,
        root: Path,
        plugins_root: Path,
        builder: SkillBuilder,
        tests: TestRunner,
        config: Callable[[], SkillsConfig],
        plugins: PluginControl,
        taken_namespaces: Callable[[], set[str]],
        new_hosts: Callable[[PluginManifest], list[str]],
        gate: PinGate | None,
        persist_enabled: Callable[[str, bool], Awaitable[None]],
        notify: Notify | None = None,
        audit: AuditLike | None = None,
        home_host: Callable[[], str] = lambda: "",
        secret_present: Callable[[str], bool] = lambda _name: False,
        clock: Callable[[], datetime] = _now,
    ) -> None:
        self.root = root
        self.plugins_root = plugins_root
        self.builder = builder
        self._tests = tests
        self._config = config
        self._plugins = plugins
        self._taken = taken_namespaces
        self._new_hosts = new_hosts
        self._gate = gate
        self._persist_enabled = persist_enabled
        self._notify = notify
        self._audit = audit
        self._home_host = home_host
        self._secret_present = secret_present
        self._clock = clock
        self._task: asyncio.Task[None] | None = None
        self._halted = False

    # -- records ---------------------------------------------------------------------------------

    def _dir(self, skill_id: str) -> Path:
        return self.root / skill_id

    def _save(self, record: SkillRecord) -> None:
        record.updated_at = self._clock()
        folder = self._dir(record.id)
        folder.mkdir(parents=True, exist_ok=True)
        tmp = folder / f"{RECORD_FILE}.tmp"
        tmp.write_text(record.model_dump_json(indent=2), encoding="utf-8")
        tmp.replace(folder / RECORD_FILE)

    def get(self, skill_id: str) -> SkillRecord | None:
        path = self._dir(skill_id) / RECORD_FILE
        try:
            return SkillRecord.model_validate_json(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def records(self) -> list[SkillRecord]:
        if not self.root.is_dir():
            return []
        found = [self.get(p.name) for p in sorted(self.root.iterdir()) if p.is_dir()]
        return [r for r in found if r is not None]

    def declares_secret(self, name: str) -> bool:
        """Whether an active or ready skill's summary names `name` (for `secrets.set`)."""
        return any(
            r.summary is not None and name in r.summary.secrets
            for r in self.records()
            if r.state in (SkillState.READY, SkillState.ACTIVE, SkillState.INACTIVE)
        )

    def describe(self, record: SkillRecord) -> dict[str, Any]:
        data: dict[str, Any] = json.loads(record.model_dump_json())
        if record.summary is not None:
            data["summary"]["secrets_present"] = {
                name: self._secret_present(name) for name in record.summary.secrets
            }
        return data

    def status(self) -> dict[str, Any]:
        reason = self.builder.unavailable() if self._config().enabled else "skills are switched off"
        return {
            "available": reason is None,
            "reason": reason or "",
            "skills": [self.describe(r) for r in self.records()],
        }

    # -- learning --------------------------------------------------------------------------------

    def building(self) -> SkillRecord | None:
        return next((r for r in self.records() if r.state is SkillState.BUILDING), None)

    async def learn(self, intent: str, *, capability: str = "", by: str) -> dict[str, Any]:
        if not self._config().enabled:
            return {"ok": False, "reason": "learning new skills is switched off (skills.enabled)"}
        if self._halted:
            return {"ok": False, "reason": "the kill switch is engaged"}
        reason = self.builder.unavailable()
        if reason is not None:
            return {"ok": False, "reason": reason}
        busy = self.building()
        if busy is not None and self._task is not None and not self._task.done():
            return {"ok": False, "reason": f"I am still learning {busy.id!r}; one at a time"}
        taken = self._taken() | {r.id for r in self.records()}
        skill_id = skill_id_for(capability or intent, taken)
        now = self._clock()
        record = SkillRecord(
            id=skill_id,
            intent=" ".join(intent.split())[:500],
            state=SkillState.BUILDING,
            created_at=now,
            updated_at=now,
        )
        self._save(record)
        self._record(by, "skill.learn", skill_id, "allow", "started")
        self._task = asyncio.create_task(self._run(record), name=f"nox-skill-{skill_id}")
        return {
            "ok": True,
            "id": skill_id,
            "message": (
                "I am building it now. When it is ready you will see what it can do, which hosts "
                "it talks to and whether its tests passed - and you switch it on in the dashboard."
            ),
        }

    async def wait(self) -> None:
        """For tests and shutdown: until the current build is over."""
        if self._task is not None:
            with contextlib.suppress(asyncio.CancelledError):
                await self._task

    async def _run(self, record: SkillRecord) -> None:
        cfg = self._config()
        work = self._dir(record.id) / "work"
        if work.exists():
            await asyncio.to_thread(shutil.rmtree, work)
        work.mkdir(parents=True)
        feedback: tuple[str, ...] = ()
        try:
            for round_no in range(1, cfg.max_repair_rounds + 2):
                record.rounds = round_no
                self._save(record)
                problems = await self._attempt(record, work, feedback, round_no)
                if not problems:
                    return
                record.problems = problems
                feedback = tuple(problems)
                self._save(record)
                if self.builder.unavailable() is not None:
                    break
            record.state = SkillState.FAILED
            record.message = "the skill did not pass the checks"
            self._save(record)
            self._record("skills", "skill.build", record.id, "deny", "failed")
            await self._tell(record, f"Ich konnte mir {record.id} nicht beibringen.")
        except asyncio.CancelledError:
            record.state = SkillState.FAILED
            record.message = "stopped"
            self._save(record)
            raise

    async def _attempt(
        self, record: SkillRecord, work: Path, feedback: tuple[str, ...], round_no: int
    ) -> list[str]:
        cfg = self._config()
        outcome = await self.builder.build(
            BuildRequest(
                skill_id=record.id,
                intent=record.intent,
                work_dir=work,
                feedback=feedback,
                round=round_no,
            )
        )
        if not outcome.ok:
            return [outcome.message or "the build did not finish"]
        report = await asyncio.to_thread(
            check_skill,
            work,
            skill_id=record.id,
            taken=self._taken(),
            home_host=self._home_host(),
        )
        if not report.ok:
            return report.problems
        tests = await self._tests.run(work, timeout_s=cfg.test_timeout_s)
        if not tests.ok:
            tail = [line for line in tests.output.splitlines() if line.strip()][-12:]
            status = "timed out" if tests.timed_out else f"{tests.failed} failed"
            return [f"tests: {tests.passed} passed, {status}", *tail]
        assert report.manifest is not None
        await asyncio.to_thread(self._freeze, record, work)
        record.summary = self._summary(report.manifest, tests.passed, tests.failed, tests.output)
        record.state = SkillState.READY
        record.problems = []
        record.message = ""
        self._save(record)
        self._record("skills", "skill.build", record.id, "allow", "ready")
        await self._tell(
            record,
            f"Ich habe mir {record.id} beigebracht. Im Dashboard unter Faehigkeiten kannst du es "
            "dir ansehen und aktivieren.",
        )
        return []

    def _freeze(self, record: SkillRecord, work: Path) -> None:
        versions = self._dir(record.id) / "versions"
        number = record.version + 1
        target = versions / str(number)
        if target.exists():
            shutil.rmtree(target)
        shutil.copytree(work, target, ignore=shutil.ignore_patterns("__pycache__", ".*"))
        record.version = number
        record.tree_hash = tree_hash(target)
        keep = self._config().keep_versions
        existing = sorted(int(p.name) for p in versions.iterdir() if p.name.isdigit())
        for old in existing[:-keep]:
            shutil.rmtree(versions / str(old), ignore_errors=True)

    def _summary(
        self, manifest: PluginManifest, passed: int, failed: int, output: str
    ) -> SkillSummary:
        new_hosts = self._new_hosts(manifest)
        return SkillSummary(
            tools=[
                SkillTool(name=p.tool, risk=p.risk.value, description="")
                for p in manifest.permissions
            ],
            hosts=list(manifest.network.egress),
            new_hosts=new_hosts,
            secrets=list(manifest.secrets),
            tests_passed=passed,
            tests_failed=failed,
            test_output=untrusted_text(output[-600:], 600),
            needs_pin=bool(new_hosts or manifest.secrets),
        )

    # -- activation ------------------------------------------------------------------------------

    async def activate(self, skill_id: str, *, pin: str | None, by: str) -> dict[str, Any]:
        record = self.get(skill_id)
        if record is None:
            return {"ok": False, "reason": "unknown skill"}
        if record.state not in (SkillState.READY, SkillState.INACTIVE):
            return {"ok": False, "reason": f"the skill is {record.state.value}"}
        if self._halted:
            return {"ok": False, "reason": "the kill switch is engaged"}
        version_dir = self._dir(skill_id) / "versions" / str(record.version)
        if not version_dir.is_dir() or tree_hash(version_dir) != record.tree_hash:
            self._record(by, "skill.activate", skill_id, "deny", "tampered")
            return {"ok": False, "reason": "its files changed after they were checked"}
        report = await asyncio.to_thread(
            check_skill,
            version_dir,
            skill_id=skill_id,
            taken=self._taken(),
            home_host=self._home_host(),
        )
        if not report.ok or report.manifest is None:
            self._record(by, "skill.activate", skill_id, "deny", "refused")
            return {"ok": False, "reason": "; ".join(report.problems[:3])}
        new_hosts = self._new_hosts(report.manifest)
        needs_pin = bool(new_hosts or report.manifest.secrets)
        if needs_pin and self._gate is not None and self._gate.is_required():
            try:
                await self._gate.require(pin, action="skill.activate", by=by)
            except PermissionError as exc:
                return {"ok": False, "needs_pin": True, "reason": str(exc)}
        target = self.plugins_root / skill_id
        await asyncio.to_thread(_replace_tree, version_dir, target)
        if new_hosts:
            self._plugins.allow_egress(new_hosts)
        await self._persist_enabled(skill_id, True)
        status = await self._plugins.enable(skill_id)
        record.state = SkillState.ACTIVE
        record.approved_hosts = new_hosts
        self._save(record)
        self._record(by, "skill.activate", skill_id, "allow", "ok", hosts=",".join(new_hosts))
        return {"ok": True, "state": getattr(getattr(status, "state", None), "value", "")}

    async def deactivate(self, skill_id: str, *, by: str) -> dict[str, Any]:
        record = self.get(skill_id)
        if record is None:
            return {"ok": False, "reason": "unknown skill"}
        await self._plugins.disable(skill_id, "deactivated")
        await self._persist_enabled(skill_id, False)
        await asyncio.to_thread(shutil.rmtree, self.plugins_root / skill_id, True)
        if record.state is SkillState.ACTIVE:
            record.state = SkillState.INACTIVE
            self._save(record)
        self._record(by, "skill.deactivate", skill_id, "allow", "ok")
        return {"ok": True}

    async def forget(self, skill_id: str, *, by: str) -> dict[str, Any]:
        record = self.get(skill_id)
        if record is None:
            return {"ok": False, "reason": "unknown skill"}
        if record.state is SkillState.BUILDING and self._task is not None:
            self._task.cancel()
            await self.wait()
        await self._plugins.remove(skill_id)
        await self._persist_enabled(skill_id, False)
        await asyncio.to_thread(shutil.rmtree, self.plugins_root / skill_id, True)
        await asyncio.to_thread(shutil.rmtree, self._dir(skill_id), True)
        self._record(by, "skill.forget", skill_id, "allow", "ok")
        return {"ok": True}

    # -- lifecycle -------------------------------------------------------------------------------

    def start(self) -> None:
        """At boot, before the plugin manager starts: re-approve the hosts of active skills (the
        manager then starts them like any enabled plugin) and close builds a restart cut off."""
        records = self.records()
        approved = [h for r in records if r.state is SkillState.ACTIVE for h in r.approved_hosts]
        if approved:
            self._plugins.allow_egress(approved)
        for record in records:
            if record.state is SkillState.BUILDING:
                record.state = SkillState.FAILED
                record.message = "interrupted by a restart"
                self._save(record)

    async def halt(self) -> None:
        """Kill switch: stop a running build at once."""
        self._halted = True
        cancel = getattr(self.builder, "cancel", None)
        if cancel is not None:
            await cancel()
        if self._task is not None and not self._task.done():
            self._task.cancel()
            await self.wait()
        busy = self.building()
        if busy is not None:  # cancelled before its first step ran
            busy.state = SkillState.FAILED
            busy.message = "stopped"
            self._save(busy)

    def resume(self) -> None:
        self._halted = False

    async def stop(self) -> None:
        await self.halt()

    # -- helpers ---------------------------------------------------------------------------------

    async def _tell(self, record: SkillRecord, text: str) -> None:
        if self._notify is None:
            return
        try:
            await self._notify(text, f"skill:{record.id}")
        except Exception as exc:  # noqa: BLE001 - the record is saved; a voice failure is logged
            log.warning("skills.notify_failed", id=record.id, error=type(exc).__name__)

    def _record(
        self, actor: str, action: str, target: str, decision: str, result: str, **details: str
    ) -> None:
        if self._audit is None:
            return
        try:
            self._audit.append(
                actor=actor,
                tool="skills",
                action=action,
                target=target,
                decision=decision,
                result=result,
                details={k: v[:200] for k, v in details.items() if v} or None,
            )
        except Exception as exc:  # noqa: BLE001 - logged; the action itself stands
            log.error("skills.audit_failed", action=action, error=type(exc).__name__)


def _replace_tree(source: Path, target: Path) -> None:
    if target.exists():
        shutil.rmtree(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source, target)
