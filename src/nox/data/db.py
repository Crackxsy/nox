"""`Database`: thin sqlite3 wrapper (WAL, foreign keys, migrations, integrity check) per ADR-006.

Concurrency model (deliberate choice): one connection opened with `check_same_thread=False` and
guarded by a `threading.RLock`. Every call is synchronous and short (single statements on a local
WAL database); callers on the event loop call them directly, and a caller that expects a long
operation (bulk re-index, VACUUM) wraps it in `asyncio.to_thread`, which the lock makes safe. The
connection runs in autocommit mode (`isolation_level=None`); `transaction()` issues BEGIN
IMMEDIATE / COMMIT / ROLLBACK explicitly and nests via SAVEPOINTs so repositories compose.
Migrations are `NNNN_name.sql` files applied in order, each inside one transaction, recorded in
`schema_migrations`.
"""

from __future__ import annotations

import re
import sqlite3
import threading
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from nox.core.logging import get_logger

MIGRATIONS_DIR = Path(__file__).with_name("migrations")
_MIGRATION_FILE = re.compile(r"^(\d{4})_([a-z0-9_]+)\.sql$")
Params = Sequence[Any] | dict[str, Any]


class MigrationError(RuntimeError):
    pass


def split_statements(script: str) -> list[str]:
    """Split a SQL script into complete statements (handles `CREATE TRIGGER ... BEGIN ...;
    END;`)."""
    statements: list[str] = []
    buffer: list[str] = []
    for line in script.splitlines():
        stripped = line.strip()
        if not buffer and (not stripped or stripped.startswith("--")):
            continue
        buffer.append(line)
        candidate = "\n".join(buffer)
        if sqlite3.complete_statement(candidate):
            statements.append(candidate.strip())
            buffer = []
    rest = "\n".join(buffer).strip()
    if rest and not all(ln.strip().startswith("--") or not ln.strip() for ln in rest.splitlines()):
        raise MigrationError(f"incomplete SQL statement at end of script: {rest[:80]!r}")
    return statements


def utcnow_iso() -> str:
    return datetime.now(UTC).isoformat()


class Database:
    def __init__(self, path: Path | str, *, timeout_s: float = 5.0) -> None:
        self._path = Path(path)
        self._memory = str(path) == ":memory:"
        if not self._memory:
            self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._tx_depth = 0
        self._log = get_logger(__name__)
        self._vec_loaded = False
        self._conn = sqlite3.connect(
            str(path), timeout=timeout_s, check_same_thread=False, isolation_level=None
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.execute(f"PRAGMA busy_timeout={int(timeout_s * 1000)}")
        if not self._memory:
            mode = self._conn.execute("PRAGMA journal_mode=WAL").fetchone()[0]
            if str(mode).lower() != "wal":
                self._log.warning("db.wal_unavailable", mode=mode)
            self._conn.execute("PRAGMA synchronous=NORMAL")

    # ---- basics ------------------------------------------------------------------------------

    @property
    def path(self) -> Path:
        return self._path

    @property
    def connection(self) -> sqlite3.Connection:
        """The underlying connection (injected into components that need raw access, e.g. FTS)."""
        return self._conn

    @property
    def lock(self) -> threading.RLock:
        return self._lock

    def execute(self, sql: str, params: Params = ()) -> sqlite3.Cursor:
        with self._lock:
            return self._conn.execute(sql, params)

    def executemany(self, sql: str, seq: Sequence[Params]) -> sqlite3.Cursor:
        with self._lock:
            return self._conn.executemany(sql, seq)

    def fetch_all(self, sql: str, params: Params = ()) -> list[sqlite3.Row]:
        with self._lock:
            return list(self._conn.execute(sql, params).fetchall())

    def fetch_one(self, sql: str, params: Params = ()) -> sqlite3.Row | None:
        with self._lock:
            row = self._conn.execute(sql, params).fetchone()
            return row if row is not None else None

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """BEGIN IMMEDIATE at depth 0, SAVEPOINT when nested. Rolls back on any exception."""
        with self._lock:
            depth = self._tx_depth
            if depth == 0:
                self._conn.execute("BEGIN IMMEDIATE")
            else:
                self._conn.execute(f"SAVEPOINT sp{depth}")
            self._tx_depth += 1
            try:
                yield self._conn
            except BaseException:
                if depth == 0:
                    self._conn.execute("ROLLBACK")
                else:
                    self._conn.execute(f"ROLLBACK TO sp{depth}")
                    self._conn.execute(f"RELEASE sp{depth}")
                raise
            else:
                if depth == 0:
                    self._conn.execute("COMMIT")
                else:
                    self._conn.execute(f"RELEASE sp{depth}")
            finally:
                self._tx_depth -= 1

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def __enter__(self) -> Database:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ---- migrations --------------------------------------------------------------------------

    @staticmethod
    def list_migrations(migrations_dir: Path) -> list[tuple[int, str, Path]]:
        found: list[tuple[int, str, Path]] = []
        for file in sorted(migrations_dir.glob("*.sql")):
            match = _MIGRATION_FILE.match(file.name)
            if match is None:
                raise MigrationError(f"bad migration filename {file.name} (expected NNNN_name.sql)")
            found.append((int(match.group(1)), match.group(2), file))
        found.sort(key=lambda item: item[0])
        ids = [item[0] for item in found]
        if len(ids) != len(set(ids)):
            raise MigrationError(f"duplicate migration ids in {migrations_dir}")
        return found

    def applied_migrations(self) -> list[int]:
        self.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations ("
            "id INTEGER PRIMARY KEY, name TEXT NOT NULL, applied_at TEXT NOT NULL)"
        )
        return [
            int(r["id"]) for r in self.fetch_all("SELECT id FROM schema_migrations ORDER BY id")
        ]

    def migrate(self, migrations_dir: Path | None = None) -> list[str]:
        """Apply pending migrations in order. Returns the names applied in this call."""
        directory = migrations_dir or MIGRATIONS_DIR
        applied = set(self.applied_migrations())
        done: list[str] = []
        for mig_id, name, file in self.list_migrations(directory):
            if mig_id in applied:
                continue
            script = file.read_text(encoding="utf-8")
            with self.transaction() as conn:
                for statement in split_statements(script):
                    try:
                        conn.execute(statement)
                    except sqlite3.Error as exc:
                        raise MigrationError(f"{file.name}: {exc} in {statement[:120]!r}") from exc
                conn.execute(
                    "INSERT INTO schema_migrations (id, name, applied_at) VALUES (?, ?, ?)",
                    (mig_id, name, utcnow_iso()),
                )
            done.append(f"{mig_id:04d}_{name}")
            self._log.info("db.migrated", migration=done[-1])
        return done

    # ---- health ------------------------------------------------------------------------------

    def integrity_check(self) -> bool:
        """`PRAGMA integrity_check` == ok.

        Corrupt databases are renamed by the lifecycle, not here.
        """
        try:
            row = self.fetch_one("PRAGMA integrity_check")
        except sqlite3.DatabaseError as exc:
            self._log.error("db.integrity_error", error=str(exc))
            return False
        ok = row is not None and str(row[0]).lower() == "ok"
        if not ok:
            self._log.error("db.integrity_failed", result=None if row is None else str(row[0]))
        return ok

    def table_names(self) -> list[str]:
        rows = self.fetch_all(
            "SELECT name FROM sqlite_master WHERE type IN ('table','view') ORDER BY 1"
        )
        return [str(r["name"]) for r in rows]

    # ---- sqlite-vec --------------------------------------------------------------------------

    @property
    def vec_loaded(self) -> bool:
        return self._vec_loaded

    def load_sqlite_vec(self) -> bool:
        """Load the sqlite-vec extension. False (never raises) when the package or loader is
        missing."""
        if self._vec_loaded:
            return True
        try:
            import sqlite_vec  # noqa: PLC0415 - optional dependency
        except ImportError:
            self._log.info("db.sqlite_vec_missing")
            return False
        with self._lock:
            try:
                self._conn.enable_load_extension(True)
                sqlite_vec.load(self._conn)
                self._conn.enable_load_extension(False)
            except (AttributeError, sqlite3.Error, OSError) as exc:
                self._log.warning("db.sqlite_vec_failed", error=str(exc))
                return False
        self._vec_loaded = True
        return True

    def ensure_memory_vec(self, dimensions: int = 768) -> bool:
        """Create `memory_vec` (runtime only, needs sqlite-vec).

        Returns False when vectors are off.
        """
        if not self.load_sqlite_vec():
            return False
        self.execute(
            "CREATE VIRTUAL TABLE IF NOT EXISTS memory_vec "
            f"USING vec0(embedding float[{int(dimensions)}])"
        )
        return True
