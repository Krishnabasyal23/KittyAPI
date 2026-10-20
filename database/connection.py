
import os
import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB_PATH = ROOT / "data" / "kittycat.db"
# Tests, scripts and the v2 server point this at their own file so the v1 data is never touched.
DB_PATH = Path(os.getenv("KITTY_DB_PATH") or DEFAULT_DB_PATH)
SCHEMA_PATH = Path(__file__).resolve().parent / "schema.sql"


def get_connection(db_path=None):
    path = Path(db_path) if db_path is not None else DB_PATH

    path.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(path, timeout=5)
    conn.row_factory = sqlite3.Row

    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 5000")

    return conn


def initialize_database(db_path=None):
    conn = get_connection(db_path)

    try:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.executescript(
            SCHEMA_PATH.read_text(encoding="utf-8")
        )
        existing = {
            row["name"]
            for row in conn.execute("PRAGMA table_info(rounds)")
        }

        columns = {
            "season": "TEXT NOT NULL DEFAULT 'spring'",
            "growth_rate": "REAL NOT NULL DEFAULT 0.12",
            "rush": "INTEGER NOT NULL DEFAULT 0",
            "lead_in_seconds": "REAL NOT NULL DEFAULT 3.0",
        }

        for name, definition in columns.items():
            if name not in existing:
                conn.execute(f"ALTER TABLE rounds ADD COLUMN {name} {definition}")

        conn.commit()
    finally:
        conn.close()
