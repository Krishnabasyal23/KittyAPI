"""Versioned migrations for the Catino v2 database.

Migrations live in database/migrations/NNNN_name.sql and are applied in order, each in its own
transaction together with its row in schema_migrations (version, name, checksum). The runner:
  * refuses the v1 database (data/kittycat.db) and any file that looks like a v1 schema;
  * refuses to run if an applied migration file was edited afterwards (checksum mismatch);
  * copies an existing database to <db>.pre-NNNN-<timestamp>.bak before applying new migrations.

    python -m database.migrate status [--db PATH]
    python -m database.migrate up     [--db PATH]

Rollback procedures: docs/MIGRATIONS.md (never delete a database file).
"""

import argparse
import hashlib
import re
import sqlite3
import time

from dataclasses import dataclass
from pathlib import Path

from . import connection

MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"
FILE_NAME = re.compile(r"^(\d{4})_([a-z0-9_]+)\.sql$")
V1_TABLES = {"sessions", "credit_transactions"}


class MigrationError(RuntimeError):
    pass


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    sql: str
    checksum: str


def checksum(sql: str) -> str:
    # line endings normalised so a CRLF checkout has the same checksum as LF
    return hashlib.sha256(sql.replace("\r\n", "\n").encode("utf-8")).hexdigest()


def discover(directory: Path = MIGRATIONS_DIR) -> list[Migration]:
    found = []
    for path in sorted(Path(directory).glob("*.sql")):
        match = FILE_NAME.match(path.name)
        if not match:
            raise MigrationError(f"Bad migration file name: {path.name}")
        sql = path.read_text(encoding="utf-8")
        found.append(Migration(int(match.group(1)), match.group(2), sql, checksum(sql)))

    versions = [m.version for m in found]
    if versions != list(range(1, len(found) + 1)):
        raise MigrationError(f"Migration versions must be 1..N without gaps, got {versions}")
    return found


def _resolve(db_path) -> Path:
    path = Path(db_path) if db_path is not None else connection.DB_PATH
    if path.resolve() == connection.V1_DB_PATH.resolve():
        raise MigrationError("Refusing to migrate the v1 database (data/kittycat.db); v2 uses its own file")
    return path


def _tables(conn) -> set[str]:
    return {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}


def _applied(conn) -> dict[int, sqlite3.Row]:
    if "schema_migrations" not in _tables(conn):
        return {}
    return {row["version"]: row for row in conn.execute("SELECT * FROM schema_migrations ORDER BY version")}


def _check(conn, migrations: list[Migration]) -> tuple[dict, list[Migration]]:
    tables = _tables(conn)
    if tables & V1_TABLES and "schema_migrations" not in tables:
        raise MigrationError("This file has the v1 schema; v2 never modifies a v1 database")

    applied = _applied(conn)
    known = {m.version: m for m in migrations}
    for version, row in applied.items():
        m = known.get(version)
        if m is None:
            raise MigrationError(f"Database has migration {version}, which this code does not know (newer code?)")
        if row["checksum"] != m.checksum:
            raise MigrationError(f"Migration {version:04d}_{m.name} was edited after it was applied")
    return applied, [m for m in migrations if m.version not in applied]


def status(db_path=None, directory: Path = MIGRATIONS_DIR) -> dict:
    path = _resolve(db_path)
    migrations = discover(directory)
    if not path.exists():
        return {"db": str(path), "applied": [], "pending": [m.version for m in migrations]}
    conn = connection.get_connection(path)
    try:
        applied, pending = _check(conn, migrations)
    finally:
        conn.close()
    return {"db": str(path), "applied": sorted(applied), "pending": [m.version for m in pending]}


def migrate(db_path=None, directory: Path = MIGRATIONS_DIR, backup: bool = True) -> dict:
    """Apply pending migrations. Returns {"db", "applied": [versions applied now], "backup": path|None}."""
    path = _resolve(db_path)
    migrations = discover(directory)
    conn = connection.get_connection(path)
    backup_path = None

    try:
        conn.isolation_level = None   # explicit BEGIN/COMMIT below
        conn.execute("PRAGMA journal_mode = WAL")
        applied, pending = _check(conn, migrations)

        if pending and applied and backup:
            stamp = time.strftime("%Y%m%d-%H%M%S")
            backup_path = path.with_name(f"{path.name}.pre-{pending[0].version:04d}-{stamp}.bak")
            target = sqlite3.connect(backup_path)
            try:
                conn.backup(target)
            finally:
                target.close()

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS schema_migrations (
                version    INTEGER PRIMARY KEY,
                name       TEXT NOT NULL,
                checksum   TEXT NOT NULL,
                applied_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
            )
            """
        )

        done = []
        for m in pending:
            try:
                conn.executescript(
                    "BEGIN IMMEDIATE;\n"
                    + m.sql
                    + f"\nINSERT INTO schema_migrations(version, name, checksum)"
                      f" VALUES ({m.version}, '{m.name}', '{m.checksum}');\nCOMMIT;"
                )
            except sqlite3.Error as exc:
                if conn.in_transaction:
                    conn.execute("ROLLBACK")
                raise MigrationError(f"Migration {m.version:04d}_{m.name} failed and was rolled back: {exc}") from exc
            done.append(m.version)
    finally:
        conn.close()

    return {"db": str(path), "applied": done, "backup": str(backup_path) if backup_path else None}


def latest_version(directory: Path = MIGRATIONS_DIR) -> int:
    return len(discover(directory))


def main() -> None:
    parser = argparse.ArgumentParser(description="Catino v2 database migrations")
    parser.add_argument("command", choices=["status", "up"])
    parser.add_argument("--db", help="database file (default: KITTY_DB_PATH or data/catino-v2.db)")
    args = parser.parse_args()

    if args.command == "status":
        info = status(args.db)
    else:
        info = migrate(args.db)
    for key, value in info.items():
        print(f"{key}: {value}")


if __name__ == "__main__":
    main()
