"""SQLite connection management and idempotent, numbered migrations.

Migrations live in `migrations/NNN_name.sql` and are applied in order at startup inside a
transaction each. The app refuses to start if the database reports a schema version newer than
the code knows about.
"""

from __future__ import annotations

import re
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from importlib import resources
from pathlib import Path

_MIGRATION_RE = re.compile(r"^(\d{3})_[a-z0-9_]+\.sql$")


class SchemaTooNewError(RuntimeError):
    """The database was written by a newer version of the app."""


def utc_now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


def list_migrations() -> list[tuple[int, str, str]]:
    """Return (version, filename, sql) for every bundled migration, ordered by version."""
    found: list[tuple[int, str, str]] = []
    package = resources.files("garmin_live.store") / "migrations"
    for entry in package.iterdir():
        match = _MIGRATION_RE.match(entry.name)
        if match:
            found.append((int(match.group(1)), entry.name, entry.read_text(encoding="utf-8")))
    found.sort()
    versions = [v for v, _, _ in found]
    if versions != list(range(1, len(versions) + 1)):
        raise RuntimeError(f"migrations must be numbered consecutively from 001; found {versions}")
    return found


def connect(db_path: str | Path) -> sqlite3.Connection:
    """Open the database in WAL mode with foreign keys on."""
    path = Path(db_path)
    if str(path) != ":memory:":
        path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def current_version(conn: sqlite3.Connection) -> int:
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='schema_version'"
    ).fetchone()
    if row is None:
        return 0
    value = conn.execute("SELECT COALESCE(MAX(version), 0) FROM schema_version").fetchone()[0]
    return int(value)


def migrate(conn: sqlite3.Connection) -> int:
    """Apply pending migrations; return the resulting schema version."""
    migrations = list_migrations()
    code_version = migrations[-1][0] if migrations else 0
    db_version = current_version(conn)
    if db_version > code_version:
        raise SchemaTooNewError(
            f"database schema version {db_version} is newer than this build supports "
            f"({code_version}); refusing to start"
        )
    for version, _name, sql in migrations:
        if version <= db_version:
            continue
        # executescript() commits any open transaction first, so the transaction must be part
        # of the script itself. `version` comes from the filename and the timestamp is ours.
        script = (
            "BEGIN;\n"
            f"{sql}\n"
            "INSERT INTO schema_version (version, applied_at) "
            f"VALUES ({version}, '{utc_now_iso()}');\n"
            "COMMIT;"
        )
        try:
            conn.executescript(script)
        except Exception:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
    return code_version


class Database:
    """A single shared connection guarded by a lock (one uvicorn worker, short transactions)."""

    def __init__(self, db_path: str | Path) -> None:
        self.path = str(db_path)
        self.conn = connect(db_path)
        self._lock = threading.RLock()
        self.schema_version = migrate(self.conn)

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        """Run a block inside a transaction, serialised across threads."""
        with self._lock:
            self.conn.execute("BEGIN")
            try:
                yield self.conn
                self.conn.execute("COMMIT")
            except Exception:
                self.conn.execute("ROLLBACK")
                raise

    @contextmanager
    def read(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            yield self.conn

    def close(self) -> None:
        with self._lock:
            self.conn.close()
