"""SQLite connection handling and the migration runner.

Migrations are the numbered ``NNN_name.sql`` files next to this module.
Each one is applied exactly once inside a transaction and recorded in
``schema_migrations`` together with its SHA-256, so an edited migration
or a database created by a newer program version is detected instead of
silently producing a mismatched schema.
"""
from __future__ import annotations

import hashlib
import re
import sqlite3
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"
MIGRATION_RE = re.compile(r"^(\d{3})_([a-z0-9_]+)\.sql$")
BUSY_TIMEOUT_MS = 15000


class MigrationError(RuntimeError):
    pass


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    sql: str

    @property
    def checksum(self) -> str:
        return hashlib.sha256(self.sql.encode("utf-8")).hexdigest()


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def connect(path: str | Path) -> sqlite3.Connection:
    """Open a connection with the settings every caller relies on.

    WAL lets the UI server read while the analysis worker writes;
    foreign keys are off by default in SQLite and must be enabled per
    connection. A connection must not be used by two threads at once
    (transactions would interleave): give each thread its own, e.g. via
    ``Database``, or serialise access with a lock.
    """
    conn = sqlite3.connect(str(path), timeout=BUSY_TIMEOUT_MS / 1000, isolation_level=None,
                           check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
    conn.execute("PRAGMA foreign_keys = ON")
    # Switching a new database to WAL needs a lock that SQLite refuses to wait for when several
    # connections open it at the same moment, so retry instead of failing.
    for attempt in range(40):
        try:
            conn.execute("PRAGMA journal_mode = WAL")
            break
        except sqlite3.OperationalError:
            if attempt == 39:
                raise
            time.sleep(0.05)
    conn.execute("PRAGMA synchronous = NORMAL")
    return conn


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """BEGIN IMMEDIATE ... COMMIT, rolling back on any exception."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")


def load_migrations(directory: Path = MIGRATIONS_DIR) -> list[Migration]:
    found: dict[int, Migration] = {}
    for p in sorted(directory.iterdir()):
        if p.suffix != ".sql":
            continue
        m = MIGRATION_RE.match(p.name)
        if not m:
            raise MigrationError(f"migration 檔名格式錯誤：{p.name}（需為 NNN_name.sql）")
        version = int(m.group(1))
        if version in found:
            raise MigrationError(f"migration 版本重複：{version:03d}")
        found[version] = Migration(version, m.group(2), p.read_text(encoding="utf-8"))
    versions = sorted(found)
    if versions != list(range(1, len(versions) + 1)):
        raise MigrationError(f"migration 版本必須從 001 連續編號，目前為 {versions}")
    return [found[v] for v in versions]


def _ensure_table(conn: sqlite3.Connection) -> None:
    conn.execute("""CREATE TABLE IF NOT EXISTS schema_migrations (
        version INTEGER PRIMARY KEY, name TEXT NOT NULL, checksum TEXT NOT NULL, applied_at TEXT NOT NULL)""")


def applied_migrations(conn: sqlite3.Connection) -> dict[int, sqlite3.Row]:
    _ensure_table(conn)
    return {r["version"]: r for r in conn.execute("SELECT * FROM schema_migrations ORDER BY version")}


def split_statements(sql: str) -> Iterator[str]:
    """Split a SQL script into complete statements (comments and quoted semicolons handled by SQLite)."""
    buf = ""
    for line in sql.splitlines(keepends=True):
        buf += line
        if sqlite3.complete_statement(buf):
            if any(l.strip() and not l.strip().startswith("--") for l in buf.splitlines()):
                yield buf.strip()
            buf = ""
    if any(l.strip() and not l.strip().startswith("--") for l in buf.splitlines()):
        yield buf.strip()


def migrate(conn: sqlite3.Connection, directory: Path = MIGRATIONS_DIR) -> list[int]:
    """Apply pending migrations. Returns the versions applied by this call.

    Safe against concurrent callers (server and worker starting together): each migration runs in
    ``BEGIN IMMEDIATE`` and the "already applied?" check is made *after* the write lock is held, so a
    process that lost the race sees the finished migration and skips it.
    """
    migrations = load_migrations(directory)
    known = {m.version: m for m in migrations}
    _ensure_table(conn)
    done: list[int] = []

    def verify_applied() -> dict[int, sqlite3.Row]:
        applied = {r["version"]: r for r in conn.execute("SELECT * FROM schema_migrations ORDER BY version")}
        for version, row in applied.items():
            if version not in known:
                raise MigrationError(f"資料庫的 schema 版本 {version:03d} 比目前程式新，請更新程式後再開啟。")
            if row["checksum"] != known[version].checksum:
                raise MigrationError(f"migration {version:03d}_{row['name']} 在套用後被修改（checksum 不符）。")
        return applied

    verify_applied()
    for m in migrations:
        conn.execute("BEGIN IMMEDIATE")
        try:
            if m.version in verify_applied():          # another process applied it while we waited
                conn.execute("COMMIT")
                continue
            for stmt in split_statements(m.sql):
                conn.execute(stmt)
            conn.execute("INSERT INTO schema_migrations(version, name, checksum, applied_at) VALUES (?,?,?,?)",
                         (m.version, m.name, m.checksum, utcnow()))
            conn.execute("COMMIT")
        except MigrationError:
            conn.execute("ROLLBACK")
            raise
        except sqlite3.Error as exc:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise MigrationError(f"套用 migration {m.version:03d}_{m.name} 失敗：{exc}") from exc
        done.append(m.version)
    return done


def schema_version(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT MAX(version) AS v FROM schema_migrations").fetchone()
    return int(row["v"] or 0)


def open_database(path: str | Path) -> sqlite3.Connection:
    """Connect and bring the schema up to date (used at startup)."""
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = connect(path)
    migrate(conn)
    return conn


class Database:
    """One SQLite connection per thread, all opened on the same migrated database file.

    The API server handles requests on a thread pool; each request thread calls ``repo()`` and gets
    a connection no other thread uses.
    """

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._local = threading.local()
        self._all: list[sqlite3.Connection] = []
        self._lock = threading.Lock()
        boot = open_database(self.path)
        boot.close()

    def conn(self) -> sqlite3.Connection:
        c = getattr(self._local, "conn", None)
        if c is None:
            c = connect(self.path)
            self._local.conn = c
            with self._lock:
                self._all.append(c)
        return c

    def repo(self):
        from .repo import Repo
        r = getattr(self._local, "repo", None)
        if r is None or r.conn is not self.conn():
            r = Repo(self.conn())
            self._local.repo = r
        return r

    def close(self) -> None:
        with self._lock:
            for c in self._all:
                try:
                    c.close()
                except sqlite3.Error:
                    pass
            self._all.clear()
        self._local = threading.local()
