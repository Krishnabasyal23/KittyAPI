import sqlite3
import sys

from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))

import db_tools                                  # noqa: E402
from database.connection import V1_DB_PATH       # noqa: E402


def make(path):
    con = sqlite3.connect(path)
    con.execute("create table t (x integer)")
    con.execute("insert into t values (7)")
    con.commit()
    con.close()


def test_backup_is_a_readable_copy(tmp_path):
    db = tmp_path / "catino-v2.db"
    make(db)
    copy = db_tools.backup(db)
    assert copy.parent == tmp_path / "backups"
    assert sqlite3.connect(copy).execute("select x from t").fetchone() == (7,)
    assert db.exists()


def test_reset_keeps_the_old_data_and_clears_the_path(tmp_path):
    db = tmp_path / "catino-v2.db"
    make(db)
    copy = db_tools.reset(db)
    assert not db.exists()
    assert copy.exists()
    assert any(p.name.startswith("catino-v2.db.reset-") for p in tmp_path.iterdir())


def test_v1_database_is_refused():
    with pytest.raises(SystemExit):
        db_tools.backup(V1_DB_PATH)
    with pytest.raises(SystemExit):
        db_tools.reset(V1_DB_PATH)
