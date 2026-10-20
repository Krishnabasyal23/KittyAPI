
import os
import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
# v2 has its own database file; its tables come from database/migrations (see migrate.py).
DEFAULT_DB_PATH = ROOT / "data" / "catino-v2.db"
# The v1 demo database. v2 code never opens it, so the v1 demo data cannot change.
V1_DB_PATH = ROOT / "data" / "kittycat.db"
# Tests, scripts and other deployments point this at their own file.
DB_PATH = Path(os.getenv("KITTY_DB_PATH") or DEFAULT_DB_PATH)


def get_connection(db_path=None):
    path = Path(db_path) if db_path is not None else DB_PATH

    if path.resolve() == V1_DB_PATH.resolve():
        raise RuntimeError("v2 code never opens the v1 database (data/kittycat.db)")

    path.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(path, timeout=5)
    conn.row_factory = sqlite3.Row

    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 5000")

    return conn


def initialize_database(db_path=None) -> dict:
    """Bring the v2 database up to the latest migration (creates the file if needed)."""
    from .migrate import migrate

    return migrate(db_path)
