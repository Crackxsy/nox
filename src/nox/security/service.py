"""SecurityContext: composition of the security core for the composition root (Security Model,
ADR-007).

`SecurityContext.build(config, conn=..., bus=...)` wires audit -> privacy -> kill switch -> profiles
->
permission engine -> egress guard -> secrets/PIN with a shared clock. `verify_boot()` checks the
audit
chain (ADR-011: verified at boot).
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any

from nox.core.events import EventBus
from nox.security._logging import get_logger
from nox.security.audit import ChainVerification, SqliteAuditLog
from nox.security.egress import EgressGuard
from nox.security.killswitch import KillSwitchService, PanicModeService
from nox.security.model import SecretStore
from nox.security.permissions import DefaultPermissionEngine, GrantStore, InMemoryGrantStore
from nox.security.privacy import PrivacyService
from nox.security.profiles import ProfileProvider, YamlProfileProvider
from nox.security.prohibitions import effective_hard_prohibitions
from nox.security.secrets import KeyringSecretStore, PinManager

log = get_logger(__name__)


def _as_mapping(config: Any) -> Mapping[str, Any]:
    if isinstance(config, Mapping):
        return config
    dump = getattr(config, "model_dump", None)
    if callable(dump):
        result: Mapping[str, Any] = dump(mode="json")
        return result
    raise TypeError("config must be a mapping or a pydantic model")


class SecurityContext:
    def __init__(
        self,
        *,
        audit: SqliteAuditLog,
        privacy: PrivacyService,
        killswitch: KillSwitchService,
        panic: PanicModeService,
        engine: DefaultPermissionEngine,
        egress: EgressGuard,
        secrets: SecretStore,
        pin: PinManager,
        profiles: ProfileProvider,
        grants: GrantStore,
        hard_prohibitions: frozenset[str],
    ) -> None:
        self.audit = audit
        self.privacy = privacy
        self.killswitch = killswitch
        self.panic = panic
        self.engine = engine
        self.egress = egress
        self.secrets = secrets
        self.pin = pin
        self.profiles = profiles
        self.grants = grants
        self.hard_prohibitions = hard_prohibitions

    @classmethod
    def build(
        cls,
        config: Any,
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
        cfg = _as_mapping(config)
        security_cfg: Mapping[str, Any] = cfg.get("security") or {}
        privacy_cfg: Mapping[str, Any] = cfg.get("privacy") or {}
        hard = effective_hard_prohibitions(security_cfg.get("hard_prohibitions") or ())

        audit = SqliteAuditLog(conn, bus=bus, clock=clock)
        killswitch_ref: list[KillSwitchService] = []
        privacy = PrivacyService.from_config(
            privacy_cfg,
            bus=bus,
            audit=audit,
            clock=clock,
            safe_mode=lambda: bool(killswitch_ref) and killswitch_ref[0].is_engaged(),
        )
        killswitch = KillSwitchService(bus=bus, audit=audit, privacy=privacy, clock=clock)
        killswitch_ref.append(killswitch)
        panic = PanicModeService(killswitch)

        profiles = YamlProfileProvider(profiles_dir)
        grant_store = grants if grants is not None else InMemoryGrantStore()
        engine = DefaultPermissionEngine(
            profiles=profiles,
            privacy=privacy,
            grants=grant_store,
            audit=audit,
            bus=bus,
            clock=clock,
            initial_profile=str(security_cfg.get("profile") or "companion"),
            session_id=session_id,
        )
        # B-11: the global allow-list comes from `security.egress_allowlist`; without this it was
        # never wired. An explicit argument still wins so a caller can narrow it further.
        global_allowlist = tuple(global_egress_allowlist) or tuple(
            security_cfg.get("egress_allowlist") or ()
        )
        egress = EgressGuard(
            profile=engine.active_profile,
            privacy=privacy,
            audit=audit,
            global_allowlist=global_allowlist,
            loopback_allowlist=tuple(security_cfg.get("loopback_allowlist") or ()),
        )
        secrets = secret_store if secret_store is not None else KeyringSecretStore()
        pin = PinManager(secrets, audit=audit, clock=clock)
        log.info(
            "security.context_built",
            profile=engine.active_profile().id,
            privacy=privacy.mode.value,
            hard_prohibitions=len(hard),
        )
        return cls(
            audit=audit,
            privacy=privacy,
            killswitch=killswitch,
            panic=panic,
            engine=engine,
            egress=egress,
            secrets=secrets,
            pin=pin,
            profiles=profiles,
            grants=grant_store,
            hard_prohibitions=hard,
        )

    def verify_boot(self) -> ChainVerification:
        """Audit chain check at boot; a broken chain is audited and reported, never repaired
        silently."""
        verification = self.audit.verify_chain_detailed()
        if not verification.ok:
            log.critical("security.audit_chain_broken", first_bad_seq=verification.first_bad_seq)
            self.audit.append(
                actor="system",
                tool="security",
                action="audit.verify",
                target="",
                decision="deny",
                result="failed",
                details={"first_bad_seq": str(verification.first_bad_seq)},
            )
        return verification
