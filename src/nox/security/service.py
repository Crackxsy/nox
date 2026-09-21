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

`verify_boot()` checks the hash chain and, when it is broken, says so and audits it. It never
repairs anything; the composition root turns a failure into safe mode.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Sequence
from datetime import datetime
from pathlib import Path

from nox.core.config import NoxConfig
from nox.core.events import EventBus
from nox.security._logging import get_logger
from nox.security.audit import ChainVerification, SqliteAuditLog
from nox.security.audit_sink import QueuedAuditLog
from nox.security.egress import EgressGuard
from nox.security.gate import SecurityChangeGate
from nox.security.killswitch import KillSwitchService, PanicModeService
from nox.security.model import SecretStore
from nox.security.permissions import DefaultPermissionEngine, GrantStore, InMemoryGrantStore
from nox.security.pin_attempts import SqlitePinAttemptStore
from nox.security.privacy import PrivacyService
from nox.security.profiles import ProfileProvider, YamlProfileProvider
from nox.security.prohibitions import effective_hard_prohibitions
from nox.security.secrets import KeyringSecretStore, PinManager

log = get_logger(__name__)


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

    @classmethod
    def build(
        cls,
        config: NoxConfig,
        *,
        conn: sqlite3.Connection,
        profiles_dir: Path,
        bus: EventBus | None = None,
        secret_store: SecretStore | None = None,
        grants: GrantStore | None = None,
        clock: Callable[[], datetime] | None = None,
        global_egress_allowlist: Sequence[str] = (),
        session_id: str | None = None,
    ) -> SecurityContext:
        security = config.security
        hard = effective_hard_prohibitions(security.hard_prohibitions)

        audit_store = SqliteAuditLog(conn, bus=bus, clock=clock)
        audit = QueuedAuditLog(audit_store)

        # The privacy service needs to know whether the kill switch is engaged, and the kill
        # switch needs the privacy service to force offline mode. Building privacy first and
        # telling it about the switch afterwards keeps that dependency explicit; it used to be a
        # one-element list closed over as a mutable cell.
        privacy = PrivacyService.from_config(config.privacy, bus=bus, audit=audit, clock=clock)
        killswitch = KillSwitchService(bus=bus, audit=audit, privacy=privacy, clock=clock)
        privacy.set_safe_mode_source(killswitch.is_engaged)
        panic = PanicModeService(killswitch)

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
        )
        secrets = secret_store if secret_store is not None else KeyringSecretStore()
        pin = PinManager(secrets, audit=audit, clock=clock, attempts=SqlitePinAttemptStore(conn))
        gate = SecurityChangeGate(
            pin, required=security.pin_required_for_security_changes, audit=audit
        )
        log.info(
            "security.context_built",
            profile=engine.active_profile().id,
            privacy=privacy.mode.value,
            hard_prohibitions=len(hard),
            pin_gate=gate.is_required(),
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
        )

    def verify_boot(self) -> ChainVerification:
        """Verify the audit chain since the last checkpoint. A break is audited, never repaired.

        Runs in a worker thread at boot, because it reads rows. The caller decides what a failure
        means; the core enters safe mode, so nothing with a side effect happens on a machine whose
        audit history cannot be trusted.
        """
        verification = self.audit_store.verify_since_checkpoint()
        if not verification.ok:
            log.critical("security.audit_chain_broken", first_bad_seq=verification.first_bad_seq)
            self.audit_store.append(
                actor="system",
                tool="security",
                action="audit.verify",
                target="",
                decision="deny",
                result="failed",
                details={"first_bad_seq": str(verification.first_bad_seq)},
            )
        return verification

    def close(self) -> bool:
        """Drain the queued audit writer. False when something was still pending."""
        return self.audit.stop()
