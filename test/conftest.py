import os
from pathlib import Path

# Must run before any test imports main / database.connection (DB_PATH is read at import time).
# Default: <project>/.tmp/pytest-db, i.e. D:\hhh\catt\.tmp\pytest-db next to the Unity project.
_TEST_DB_DIR = Path(
    os.getenv("KITTY_TEST_TMP")
    or Path(__file__).resolve().parents[2] / ".tmp" / "pytest-db"
)
_TEST_DB_DIR.mkdir(parents=True, exist_ok=True)
os.environ["KITTY_DB_PATH"] = str(_TEST_DB_DIR / f"kittycat-test-{os.getpid()}.db")
os.environ["TICKET_SECRET"] = "test-only-ticket-key-0123456789"      # real keys live in .env.local

import pytest

from database.connection import DB_PATH, DEFAULT_DB_PATH, V1_DB_PATH, initialize_database

if DB_PATH.resolve() in (V1_DB_PATH.resolve(), DEFAULT_DB_PATH.resolve()):
    raise RuntimeError("Tests must use their own database, never data/kittycat.db or data/catino-v2.db")

initialize_database(DB_PATH)   # the app's module-level store uses this file


@pytest.fixture(autouse=True)
def fixed_test_season(monkeypatch):
    monkeypatch.setattr(
        "round_manager.select_season",
        lambda: "spring"
    )
