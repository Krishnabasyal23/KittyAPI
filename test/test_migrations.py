import shutil
import sqlite3

import pytest

from database import connection
from database.migrate import MIGRATIONS_DIR, MigrationError, discover, latest_version, migrate, status


def tables(path):
    conn = sqlite3.connect(path)
    try:
        return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    finally:
        conn.close()


def partial_dir(tmp_path, upto):
    """A copy of the migrations folder with only the first `upto` files."""
    target = tmp_path / f"migrations_{upto}"
    target.mkdir()
    for path in sorted(MIGRATIONS_DIR.glob("*.sql"))[:upto]:
        shutil.copy(path, target / path.name)
    return target


def test_fresh_database_gets_every_migration(tmp_path):
    db = tmp_path / "v2.db"
    result = migrate(db)

    assert result["applied"] == list(range(1, latest_version() + 1))
    assert result["backup"] is None   # nothing to back up on a new file
    assert {"wallets", "ledger", "rounds", "crowd_bets", "payments", "lottery_tickets",
            "idempotency_keys", "rg_settings", "schema_migrations"} <= tables(db)
    assert status(db)["pending"] == []


def test_running_twice_is_a_no_op(tmp_path):
    db = tmp_path / "v2.db"
    migrate(db)
    assert migrate(db)["applied"] == []


def test_upgrade_backs_up_first_and_keeps_data(tmp_path):
    db = tmp_path / "v2.db"
    migrate(db, directory=partial_dir(tmp_path, 2))
    conn = sqlite3.connect(db)
    with conn:
        conn.execute("INSERT INTO wallets(wallet_id, kind, balance_points) VALUES ('w1', 'pc', 7)")
    conn.close()

    result = migrate(db)

    assert result["applied"] == list(range(3, latest_version() + 1))
    backup = result["backup"]
    assert backup and backup.endswith(".bak") and ".pre-0003-" in backup
    assert "rg_settings" not in tables(backup)          # the backup is the pre-upgrade state
    conn = sqlite3.connect(db)
    try:
        assert conn.execute("SELECT balance_points FROM wallets").fetchone()[0] == 7
        assert "proof_commitment" in {r[1] for r in conn.execute("PRAGMA table_info(rounds)")}
    finally:
        conn.close()


def test_edited_migration_is_refused(tmp_path):
    db = tmp_path / "v2.db"
    folder = partial_dir(tmp_path, latest_version())
    migrate(db, directory=folder)

    first = sorted(folder.glob("*.sql"))[0]
    first.write_text(first.read_text(encoding="utf-8") + "\n-- sneaky edit\n", encoding="utf-8")

    with pytest.raises(MigrationError, match="edited after it was applied"):
        migrate(db, directory=folder)


def test_crlf_checkout_has_the_same_checksum(tmp_path):
    folder = partial_dir(tmp_path, 1)
    path = next(folder.glob("*.sql"))
    before = discover(folder)[0].checksum
    path.write_bytes(path.read_bytes().replace(b"\r\n", b"\n").replace(b"\n", b"\r\n"))
    assert discover(folder)[0].checksum == before


def test_failed_migration_rolls_back(tmp_path):
    folder = partial_dir(tmp_path, 1)
    (folder / "0002_broken.sql").write_text(
        "CREATE TABLE half_done (x INTEGER);\nTHIS IS NOT SQL;\n", encoding="utf-8")
    db = tmp_path / "v2.db"

    with pytest.raises(MigrationError, match="rolled back"):
        migrate(db, directory=folder)

    assert "half_done" not in tables(db)
    assert status(db, directory=folder) == {"db": str(db), "applied": [1], "pending": [2]}


def test_refuses_the_v1_database_path():
    with pytest.raises(MigrationError, match="v1 database"):
        migrate(connection.V1_DB_PATH)
    with pytest.raises(MigrationError, match="v1 database"):
        status(connection.V1_DB_PATH)


def test_refuses_a_file_with_the_v1_schema(tmp_path):
    db = tmp_path / "copy-of-v1.db"
    conn = sqlite3.connect(db)
    with conn:
        conn.execute("CREATE TABLE sessions (session_id TEXT PRIMARY KEY, balance_cents INTEGER)")
    conn.close()

    with pytest.raises(MigrationError, match="v1 schema"):
        migrate(db)
    assert tables(db) == {"sessions"}


def test_ledger_is_append_only(tmp_path):
    db = tmp_path / "v2.db"
    migrate(db)
    conn = sqlite3.connect(db)
    with conn:
        conn.execute("INSERT INTO wallets(wallet_id, kind, balance_points) VALUES ('w1', 'pc', 50)")
        conn.execute("INSERT INTO ledger(wallet_id, kind, amount_points, balance_after, day)"
                     " VALUES ('w1', 'starting_grant', 50, 50, '2026-10-20')")
    for sql in ("UPDATE ledger SET amount_points = 5000", "DELETE FROM ledger"):
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            with conn:
                conn.execute(sql)
    with pytest.raises(sqlite3.IntegrityError):   # CHECK: balances never go negative
        with conn:
            conn.execute("UPDATE wallets SET balance_points = -1")
    conn.close()
