
from decimal import Decimal
import sqlite3

import pytest

from database.connection import (
    get_connection,
    initialize_database,
)
from database.session_repository import SQLiteSessionStore
from round_manager import RoundManager


@pytest.fixture
def setup_db(tmp_path):
    db_path = tmp_path / "kittycat-test.db"
    initialize_database(db_path)

    return SQLiteSessionStore(db_path), db_path


def test_cashout_persisted_exactly_once(
    setup_db, monkeypatch
):
    store, db_path = setup_db

    monkeypatch.setattr(
        "round_manager.draw_crash_point",
        lambda mode: (2.0, None),
    )

    clock = [100.0]
    mgr = RoundManager(
        clock=lambda: clock[0],
        session_store=store,
    )

    sid = mgr.create_session()["session_id"]
    rid = mgr.start_round(
        sid, Decimal("100.00")
    )["round_id"]

    assert store.get_session(sid)["balance"] == Decimal(
        "900.00"
    )

    clock[0] += 5

    first = mgr.cash_out(sid, rid)
    second = mgr.cash_out(sid, rid)

    assert first == second
    assert first["balance"] == "1027.00"
    assert store.get_session(sid)["balance"] == Decimal("1027.00")

    conn = get_connection(db_path)
    try:
        record = conn.execute(
            """
            SELECT status, payout_cents
            FROM rounds WHERE round_id = ?
            """,
            (rid,),
        ).fetchone()

        payouts = conn.execute(
            """
            SELECT COUNT(*)
            FROM credit_transactions
            WHERE round_id = ? AND kind = 'payout'
            """,
            (rid,),
        ).fetchone()[0]
    finally:
        conn.close()

    assert tuple(record) == ("cashed_out", 12700)
    assert payouts == 1

    restarted = RoundManager(session_store=store)

    assert restarted.get_balance(sid)["balance"] == "1027.00"
    assert restarted.get_round_result(
        sid, rid
    )["status"] == "cashed_out"


def test_crash_persists(setup_db, monkeypatch):
    store, _ = setup_db

    monkeypatch.setattr(
        "round_manager.draw_crash_point",
        lambda mode: (2.0, None),
    )

    clock = [100.0]
    mgr = RoundManager(
        clock=lambda: clock[0],
        session_store=store,
    )

    sid = mgr.create_session()["session_id"]
    rid = mgr.start_round(
        sid, Decimal("100.00")
    )["round_id"]

    clock[0] += 10

    assert mgr.get_round_state(
        sid, rid
    )["status"] == "crashed"

    assert store.get_round(
        sid, rid
    )["status"] == "crashed"

    assert store.get_session(sid)["balance"] == Decimal(
        "900.00"
    )


def test_restart_refunds_pending_once(
    setup_db, monkeypatch
):
    store, _ = setup_db

    monkeypatch.setattr(
        "round_manager.draw_crash_point",
        lambda mode: (2.0, None),
    )

    mgr = RoundManager(session_store=store)
    sid = mgr.create_session()["session_id"]

    rid = mgr.start_round(
        sid, Decimal("100.00")
    )["round_id"]

    assert store.recover_interrupted_rounds() == 1
    assert store.recover_interrupted_rounds() == 0

    resumed = RoundManager(session_store=store)

    assert resumed.get_balance(
        sid
    )["balance"] == "1000.00"

    assert resumed.get_round_result(
        sid, rid
    )["status"] == "interrupted"

    assert resumed.cash_out(
        sid, rid
    )["status"] == "interrupted"

    refunds = [
        tx for tx in store.get_transactions(sid)
        if tx["kind"] == "refund"
    ]

    assert len(refunds) == 1


def test_failed_duplicate_round_rollback(setup_db):
    store, _ = setup_db
    sid = "session123"

    store.create_session(
        sid, Decimal("1000.00")
    )

    store.record_wager(
        sid,
        "same-id",
        Decimal("100.00"),
        2.0,
        3.0,
        "fish",
    )

    with pytest.raises(sqlite3.IntegrityError):
        store.record_wager(
            sid,
            "same-id",
            Decimal("100.00"),
            2.0,
            3.0,
            "fish",
        )

    assert store.get_session(sid)["balance"] == Decimal(
        "900.00"
    )

    with pytest.raises(ValueError):
        store.settle_round(
            sid,
            "same-id",
            "cashed_out",
            Decimal("999.00"),
            1.5,
        )

    assert store.get_session(sid)["balance"] == Decimal(
        "900.00"
    )
