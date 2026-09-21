import pathlib

p = pathlib.Path("src/nox/app.py")
t = p.read_text(encoding="utf-8")

OLD = '''    def _health_checks(self) -> list[Check]:
        async def db_check() -> tuple[HealthStatus, str]:
            if self.db is None:
                return HealthStatus.UNAVAILABLE, "not opened"
            ok = await asyncio.to_thread(self.db.integrity_check)
            return (HealthStatus.AVAILABLE, "ok") if ok else (HealthStatus.UNAVAILABLE, "corrupt")

        async def vault_check() -> tuple[HealthStatus, str]:
            # The reason is a word, not the path: `/health` needs no authentication, and where the
            # user keeps their notes is not something an unauthenticated caller should learn. The
            # path itself is in the authenticated `/api/state`.
            exists = Path(self.config.paths.vault_dir).exists()
            return (
                (HealthStatus.AVAILABLE, "ok") if exists else (HealthStatus.UNAVAILABLE, "missing")
            )

        async def voice_check() -> tuple[HealthStatus, str]:
            if not self.voice_enabled:
                return HealthStatus.UNAVAILABLE, "disabled"
            worker = self.workers.get("voice")
            if worker is None or (
                worker.process is not None and worker.process.poll() is not None
            ):
                return HealthStatus.UNAVAILABLE, "worker not running"
            return (
                (HealthStatus.AVAILABLE, "worker registered")
                if worker.registered.is_set()
                else (HealthStatus.LIMITED, "worker starting")
            )

        async def tokens_check() -> tuple[HealthStatus, str]:
            # The session token file is a secret on disk. When its permissions could not be
            # tightened, health says so, rather than leaving the promise in a docstring.
            if self.tokens is None:
                return HealthStatus.UNAVAILABLE, "no session token"
            restricted = self.tokens.session_file_restricted
            if restricted is None:
                return HealthStatus.LIMITED, "session token not written yet"
            if restricted:
                return HealthStatus.AVAILABLE, "owner-only"
            return HealthStatus.LIMITED, "could not restrict the token file to this account"

        checks = [
            Check("db", db_check),
            Check("vault", vault_check),
            Check("voice", voice_check),
            Check("tokens", tokens_check),
        ]
        for provider in self.ai_providers:

            async def probe(p: AiProvider = provider) -> tuple[HealthStatus, str]:
                info = await p.health()
                return info.status, info.reason

            checks.append(Check(f"ai.{provider.info.id}", probe, timeout_s=15.0))
        return checks

'''

NEW = '''    def _health_checks(self) -> list[Check]:
        """The core's own checks; see `nox.core.boot.health`. Extensions add theirs on install."""
        return core_health_checks(
            database=lambda: self.db,
            vault_dir=lambda: Path(self.config.paths.vault_dir),
            workers=self.workers,
            voice_enabled=self.voice_enabled,
            tokens=lambda: self.tokens,
            providers=lambda: self.ai_providers,
        )

'''

assert OLD in t
t = t.replace(OLD, NEW, 1)
t = t.replace(
    "from nox.core.boot.extensions import DEFAULT_EXTENSIONS, install_extensions, stop_extensions",
    "from nox.core.boot.extensions import DEFAULT_EXTENSIONS, install_extensions, stop_extensions\n"
    "from nox.core.boot.health import core_health_checks",
    1,
)
p.write_text(t, encoding="utf-8")
print("app.py: health checks extracted")
