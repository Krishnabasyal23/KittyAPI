from decimal import Decimal
import sqlite3

import pytest

from database.connection import get_connection, initialize_database
from database.wallet_store import WalletStore
from round_manager import RoundManager


@pytest.fixture
def setup_db(tmp_path):
    db_path = tmp_path / "catino-test.db"
    initialize_database(db_path)

    return WalletStore(db_path), db_path


def test_new_session_gets_the_starting_grant_once(setup_db):
    store, _ = setup_db
    store.create_session("pc1")
    store.create_session("pc1")   # second call must not grant again

    assert store.get_session("pc1")["balance"] == 50
    grants = [e for e in store.get_ledger("pc1") if e["kind"] == "starting_grant"]
    assert len(grants) == 1


def test_cashout_persisted_exactly_once(setup_db, monkeypatch):
    store, db_path = setup_db
    monkeypatch.setattr("round_manager.draw_crash_point", lambda mode: (2.0, None))

    clock = [100.0]
    mgr = RoundManager(clock=lambda: clock[0], session_store=store)

    sid = mgr.create_session()["session_id"]
    rid = mgr.start_round(sid, Decimal("10"))["round_id"]
    assert store.get_session(sid)["balance"] == 40

    clock[0] += 5

    first = mgr.cash_out(sid, rid)
    second = mgr.cash_out(sid, rid)

    assert first == second
    assert first["balance"] == "53"
    assert store.get_session(sid)["balance"] == 53

    conn = get_connection(db_path)
    try:
        record = conn.execute(
            "SELECT status, payout_points, cashout_multiplier FROM rounds WHERE round_id = ?", (rid,)
        ).fetchone()
        payouts = conn.execute(
            "SELECT COUNT(*) FROM ledger WHERE round_id = ? AND kind = 'payout'", (rid,)
        ).fetchone()[0]
    finally:
        conn.close()

    assert tuple(record) == ("cashed_out", 13, "1.27")
    assert payouts == 1
    assert store.audit() == []

    restarted = RoundManager(session_store=store)
    assert restarted.get_balance(sid)["balance"] == "53"
    # reloaded after a restart: still sealed until the crash time on the wall clock
    assert restarted.get_round_result(sid, rid) == {"status": "running", "result": None}

    later = store.get_round(sid, rid)["started_epoch"] + 3600
    monkeypatch.setattr("round_manager.wall_clock", lambda: later)
    assert restarted.get_round_result(sid, rid)["status"] == "cashed_out"


def test_crash_persists(setup_db, monkeypatch):
    store, _ = setup_db
    monkeypatch.setattr("round_manager.draw_crash_point", lambda mode: (2.0, None))

    clock = [100.0]
    mgr = RoundManager(clock=lambda: clock[0], session_store=store)

    sid = mgr.create_session()["session_id"]
    rid = mgr.start_round(sid, Decimal("10"))["round_id"]

    clock[0] += 10

    assert mgr.get_round_state(sid, rid)["status"] == "crashed"
    assert store.get_round(sid, rid)["status"] == "crashed"
    assert store.get_session(sid)["balance"] == 40


def test_restart_refunds_pending_once(setup_db, monkeypatch):
    store, _ = setup_db
    monkeypatch.setattr("round_manager.draw_crash_point", lambda mode: (2.0, None))

    mgr = RoundManager(session_store=store)
    sid = mgr.create_session()["session_id"]
    rid = mgr.start_round(sid, Decimal("10"))["round_id"]

    assert store.recover_interrupted_rounds() == 1
    assert store.recover_interrupted_rounds() == 0

    resumed = RoundManager(session_store=store)
    assert resumed.get_balance(sid)["balance"] == "50"
    assert resumed.get_round_result(sid, rid)["status"] == "interrupted"
    assert resumed.cash_out(sid, rid)["status"] == "interrupted"

    refunds = [tx for tx in store.get_transactions(sid) if tx["kind"] == "refund"]
    assert len(refunds) == 1
    assert store.audit() == []


def test_failed_duplicate_round_rollback(setup_db):
    store, _ = setup_db
    sid = "session123"
    store.create_session(sid)

    store.record_wager(sid, "same-id", 10, 2.0, 3.0, "fish")

    with pytest.raises(sqlite3.IntegrityError):
        store.record_wager(sid, "same-id", 10, 2.0, 3.0, "fish")

    assert store.get_session(sid)["balance"] == 40

    with pytest.raises(ValueError):
        store.settle_round(sid, "same-id", "cashed_out", 999, 1.5)

    assert store.get_session(sid)["balance"] == 40
    assert store.audit() == []


def test_wager_larger_than_balance_changes_nothing(setup_db):
    store, _ = setup_db
    store.create_session("pc1")

    with pytest.raises(ValueError, match="Not enough points"):
        store.record_wager("pc1", "r1", 60, 2.0, 3.0, "fish")

    assert store.get_session("pc1")["balance"] == 50
    assert store.get_round("pc1", "r1") is None
    assert [e["kind"] for e in store.get_ledger("pc1")] == ["starting_grant"]
