"""`SecurityContext`: the security core, assembled once, handed to the composition root.

`SecurityContext.build(config, conn=..., bus=...)` wires audit -> privacy -> kill switch ->
profiles -> permission engine -> egress guard -> secrets, PIN and the PIN gate, all sharing one
clock. It takes the typed `NoxConfig`, not a mapping: this is the most security-sensitive
construction in the process, and reading it through `.get("profile")` threw away exactly the type
checking that would catch a renamed key.

The audit log the request paths see is a `QueuedAuditLog`. A permission check and every outbound
request audit their decision, and both happen on the event loop; entries are written in order by
one thread, and `close()` drains it. Boot and shutdown use `audit_store` directly, because they
want the sequence number and are allowed to block.

The privacy mode, panic and the kill switch are restored from `state_store` inside `build()`,
before the context exists for anyone else, and every later change is written back to it
(`nox.security.persisted_state`).

`verify_boot()` checks the hash chain - forwards from the checkpoint and backwards against the
anchors kept outside the database (`nox.security.audit_anchor`) - and, when it is broken, says so
and audits it. It never repairs anything; the composition root turns a failure into safe mode, and
`acknowledge_audit_break()` - called when a person resumes from it - records that the break was
seen and anchors the chain afresh.

`connect_state()` is what a worker or plugin receives when it registers: the effective privacy
mode, capture flags and whether the kill switch is engaged. Events only carry changes, so a
consumer that connects late must start from this rather than from a permissive default.
"""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Callable, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any

from nox.core.config import NoxConfig
from nox.core.events import EventBus
from nox.core.state import PrivacyMode
from nox.security._logging import get_logger
from nox.security.audit import ChainVerification, SqliteAuditLog
from nox.security.audit_anchor import (
    AuditAnchor,
    FileAuditAnchor,
    SecretAuditAnchor,
    anchor_file_name,
)
from nox.security.audit_sink import QueuedAuditLog
from nox.security.constants import fail_closed_connect_state
from nox.security.egress import EgressGuard
from nox.security.gate import SecurityChangeGate
from nox.security.killswitch import KillSwitchService, PanicModeService
from nox.security.model import SecretStore
from nox.security.permissions import DefaultPermissionEngine, GrantStore, InMemoryGrantStore
from nox.security.persisted_state import (
    InMemorySecurityStateStore,
    PersistedSecurityState,
    RestoreOutcome,
    SecurityStateRecorder,
    SecurityStateStore,
    restore_security_state,
)
from nox.security.pin_attempts import SqlitePinAttemptStore
from nox.security.privacy import PrivacyService
from nox.security.profiles import ProfileProvider, YamlProfileProvider
from nox.security.prohibitions import effective_hard_prohibitions
from nox.security.secrets import KeyringSecretStore, PinManager

log = get_logger(__name__)


def connect_state_of(security: SecurityContext | None) -> dict[str, Any]:
    """`SecurityContext.connect_state()`, or the fail-closed state when there is no context yet."""
    if security is None:
        return fail_closed_connect_state()
    return security.connect_state()


class SecurityContext:
    """Every security service of one running core, plus the wiring between them."""

    def __init__(
        self,
        *,
        audit: QueuedAuditLog,
        audit_store: SqliteAuditLog,
        privacy: PrivacyService,
        killswitch: KillSwitchService,
        panic: PanicModeService,
        engine: DefaultPermissionEngine,
        egress: EgressGuard,
        secrets: SecretStore,
        pin: PinManager,
        gate: SecurityChangeGate,
        profiles: ProfileProvider,
        grants: GrantStore,
        hard_prohibitions: frozenset[str],
        state_recorder: SecurityStateRecorder | None = None,
        restored: RestoreOutcome | None = None,
        audit_anchors: Sequence[AuditAnchor] = (),
    ) -> None:
        self.audit = audit
        self.audit_store = audit_store
        self.privacy = privacy
        self.killswitch = killswitch
        self.panic = panic
        self.engine = engine
        self.egress = egress
        self.secrets = secrets
        self.pin = pin
        self.gate = gate
        self.profiles = profiles
        self.grants = grants
        self.hard_prohibitions = hard_prohibitions
        self.state_recorder = state_recorder
        #: What the boot restored from the stored security state (None: nothing was restored).
        self.restored = restored
        #: The anchors checked at boot; the first is the file anchor kept at the head per row.
        self.audit_anchors = tuple(audit_anchors)
        #: The broken verification the boot found, until a person acknowledges it on resume.
        self.audit_break: ChainVerification | None = None
        #: Whether the chain was verified (or a break acknowledged) in this process. Only then
        #: does shutdown move the anchors: a head nobody checked must not become the reference.
        self._chain_trusted = False

    @classmethod
    def build(
        cls,
        config: NoxConfig,
        *,
        conn: sqlite3.Connection,
        profiles_dir: Path,
        db_lock: threading.RLock | None = None,
        bus: EventBus | None = None,
        secret_store: SecretStore | None = None,
        grants: GrantStore | None = None,
        clock: Callable[[], datetime] | None = None,
        global_egress_allowlist: Sequence[str] = (),
        session_id: str | None = None,
        state_store: SecurityStateStore | None = None,
        audit_anchor_dir: Path | None = None,
        database_path: Path | str = ":memory:",
    ) -> SecurityContext:
        """Build and wire every security service.

        `state_store` holds the privacy mode, panic and kill switch across restarts; without one
        they live for this process only. `audit_anchor_dir` is where the audit head is anchored
        outside the database (plus the credential store); without one the boot check has no
        anchors, which is what a unit test that builds a throwaway database wants.
        """
        security = config.security
        hard = effective_hard_prohibitions(security.hard_prohibitions)

        audit_store = SqliteAuditLog(conn, bus=bus, clock=clock, lock=db_lock)
        audit = QueuedAuditLog(audit_store)

        # The privacy service needs to know whether the kill switch is engaged, and the kill
        # switch needs the privacy service to force offline mode. Building privacy first and
        # telling it about the switch afterwards keeps that dependency explicit; it used to be a
        # one-element list closed over as a mutable cell.
        privacy = PrivacyService.from_config(config.privacy, bus=bus, audit=audit, clock=clock)
        killswitch = KillSwitchService(bus=bus, audit=audit, privacy=privacy, clock=clock)
        privacy.set_safe_mode_source(killswitch.is_engaged)
        panic = PanicModeService(killswitch)

        # Restored before anything else is built on top: from here on, every service that reads
        # the privacy mode or the kill switch sees what was in force when the core went down.
        store = state_store if state_store is not None else InMemorySecurityStateStore()
        restored = restore_security_state(
            store,
            configured_mode=PrivacyMode(config.privacy.mode),
            privacy=privacy,
            killswitch=killswitch,
        )
        recorder = SecurityStateRecorder(store, snapshot=lambda: _snapshot(privacy, killswitch))
        privacy.set_change_listener(recorder.record)
        killswitch.set_change_listener(recorder.record)

        profile_provider = YamlProfileProvider(profiles_dir)
        grant_store = grants if grants is not None else InMemoryGrantStore()
        engine = DefaultPermissionEngine(
            profiles=profile_provider,
            privacy=privacy,
            grants=grant_store,
            audit=audit,
            bus=bus,
            clock=clock,
            initial_profile=security.profile,
            session_id=session_id,
        )
        # An explicit argument narrows the list further; otherwise the global one from the
        # configuration applies. Without this the configured list was never wired at all.
        allowlist = tuple(global_egress_allowlist) or tuple(security.egress_allowlist)
        egress = EgressGuard(
            profile=engine.active_profile,
            privacy=privacy,
            audit=audit,
            global_allowlist=allowlist,
            loopback_allowlist=tuple(security.loopback_allowlist),
            safe_mode=killswitch.is_engaged,
        )
        secrets = secret_store if secret_store is not None else KeyringSecretStore()
        anchors: tuple[AuditAnchor, ...] = ()
        if audit_anchor_dir is not None:
            anchors = (
                FileAuditAnchor(
                    audit_anchor_dir / anchor_file_name(database_path), database=database_path
                ),
                SecretAuditAnchor(secrets, database=database_path),
            )
        pin = PinManager(
            secrets, audit=audit, clock=clock, attempts=SqlitePinAttemptStore(conn, lock=db_lock)
        )
        gate = SecurityChangeGate(
            pin, required=security.pin_required_for_security_changes, audit=audit
        )
        log.info(
            "security.context_built",
            profile=engine.active_profile().id,
            privacy=privacy.mode.value,
            hard_prohibitions=len(hard),
            pin_gate=gate.describe(),
            pin_algorithm=pin.algorithm,
        )
        return cls(
            audit=audit,
            audit_store=audit_store,
            privacy=privacy,
            killswitch=killswitch,
            panic=panic,
            engine=engine,
            egress=egress,
            secrets=secrets,
            pin=pin,
            gate=gate,
            profiles=profile_provider,
            grants=grant_store,
            hard_prohibitions=hard,
            state_recorder=recorder,
            restored=restored,
            audit_anchors=anchors,
        )

    def connect_state(self) -> dict[str, Any]:
        """The state a worker or plugin starts from when it (re)registers."""
        return {
            "privacy_mode": self.privacy.mode.value,
            "capture": self.privacy.effective_capture().model_dump(mode="json"),
            "safe_mode": self.killswitch.is_engaged(),
        }

    def verify_boot(self) -> ChainVerification:
        """Verify the audit chain since the last checkpoint. A break is audited, never repaired.

        Runs in a worker thread at boot, because it reads rows. The caller decides what a failure
        means; the core enters safe mode, so nothing with a side effect happens on a machine whose
        audit history cannot be trusted.
        """
        verification = self.audit_store.verify_since_checkpoint(self.audit_anchors)
        if verification.ok:
            self._chain_trusted = True
            if self.audit_anchors:
                self.audit_store.arm_anchor(self.audit_anchors[0])
            return verification
        self.audit_break = verification
        log.critical(
            "security.audit_chain_broken",
            first_bad_seq=verification.first_bad_seq,
            reason=verification.reason,
            anchors=verification.anchors,
        )
        self.audit_store.append(
            actor="system",
            tool="security",
            action="audit.verify",
            target="",
            decision="deny",
            result="failed",
            details={
                "first_bad_seq": str(verification.first_bad_seq),
                "reason": verification.reason,
            },
        )
        return verification

    def acknowledge_audit_break(self, *, by: str) -> bool:
        """After a resume from a broken chain: record it as seen and anchor the chain afresh.

        Until then every boot finds the same break again. Returns False when there was nothing
        to acknowledge. Runs the database writes in the caller's thread; the IPC handler calls it
        through `asyncio.to_thread`.
        """
        verification = self.audit_break
        if verification is None:
            return False
        self.audit.flush()
        self.audit_store.acknowledge_break(
            by=by, verification=verification, anchors=self.audit_anchors
        )
        if self.audit_anchors:
            self.audit_store.arm_anchor(self.audit_anchors[0])
        self.audit_break = None
        self._chain_trusted = True
        return True

    def close(self) -> bool:
        """Drain the queued audit writer and anchor the final head. False when something was
        still pending.

        The head goes to every anchor, the credential store included, only once the chain was
        verified in this process: after an unacknowledged break - or with no check at all - the
        anchors keep what they had.
        """
        drained = self.audit.stop()
        if drained and self._chain_trusted and self.audit_anchors:
            self.audit_store.anchor_now(self.audit_anchors)
        return drained


def _snapshot(privacy: PrivacyService, killswitch: KillSwitchService) -> PersistedSecurityState:
    return PersistedSecurityState(
        privacy_mode=privacy.mode,
        panic=privacy.panic,
        kill_engaged=killswitch.is_engaged(),
        kill_security_path=killswitch.security_path,
        kill_origin=killswitch.origin,
        kill_reason=killswitch.reason,
    )
