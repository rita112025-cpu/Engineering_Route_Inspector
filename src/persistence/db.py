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
    connection.
    """
    conn = sqlite3.connect(str(path), timeout=BUSY_TIMEOUT_MS / 1000, isolation_level=None,
                           check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
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


def migrate(conn: sqlite3.Connection, directory: Path = MIGRATIONS_DIR) -> list[int]:
    """Apply pending migrations. Returns the versions applied by this call."""
    migrations = load_migrations(directory)
    known = {m.version: m for m in migrations}
    applied = applied_migrations(conn)
    for version, row in applied.items():
        if version not in known:
            raise MigrationError(f"資料庫的 schema 版本 {version:03d} 比目前程式新，請更新程式後再開啟。")
        if row["checksum"] != known[version].checksum:
            raise MigrationError(f"migration {version:03d}_{row['name']} 在套用後被修改（checksum 不符）。")
    done: list[int] = []
    for m in migrations:
        if m.version in applied:
            continue
        # executescript() commits any open transaction first, then runs the
        # script as written, so BEGIN/COMMIT inside the script make the
        # migration and its bookkeeping row atomic.
        script = (f"BEGIN IMMEDIATE;\n{m.sql}\n;"
                  f"INSERT INTO schema_migrations(version, name, checksum, applied_at) "
                  f"VALUES ({m.version}, '{m.name}', '{m.checksum}', '{utcnow()}');\nCOMMIT;")
        try:
            conn.executescript(script)
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
