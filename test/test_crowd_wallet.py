from decimal import Decimal

import pytest
from fastapi import HTTPException

from crowd import CrowdTable
from database.connection import initialize_database
from database.wallet_store import WalletStore
from round_manager import RoundManager


@pytest.fixture
def table(tmp_path, monkeypatch):
    monkeypatch.setattr("round_manager.draw_crash_point", lambda mode: (3.0, None))
    db = tmp_path / "crowd.db"
    initialize_database(db)
    store = WalletStore(db)
    clock = [100.0]
    manager = RoundManager(clock=lambda: clock[0], session_store=store)
    t = CrowdTable(manager, store)
    host = manager.create_session()["session_id"]
    t.set_host(host, 8001, "Screen")
    return t, store, manager, host, clock


def walk(t, manager, host):
    t.open_betting(host, 5)
    return host


def start_walk(t, manager, host):
    result = manager.start_round(host, Decimal("1"), rng_mode="secure")
    t.attach(host, result)
    return result["round_id"]


def test_join_creates_a_50_point_phone_wallet(table):
    t, store, *_ = table
    joined = t.join("Ada")
    assert joined["balance"] == 50
    assert store.get_wallet(joined["player_id"])["kind"] == "phone"


def test_rejoin_after_restart_keeps_the_wallet(table):
    t, store, manager, host, clock = table
    pid = t.join("Ada")["player_id"]
    t.open_betting(host, 5)
    t.bet(pid, "10")

    restarted = CrowdTable(RoundManager(session_store=store), store)
    store.recover_interrupted_rounds()             # the open bet is refunded at startup
    again = restarted.join("someone else", pid)
    assert again["player_id"] == pid and again["name"] == "Ada" and again["balance"] == 50


def test_a_pc_wallet_is_never_seated_on_a_phone(table):
    t, store, manager, host, _ = table
    joined = t.join("Sneaky", host)
    assert joined["player_id"] != host


def test_bet_ride_and_cash_out_is_persisted(table):
    t, store, manager, host, clock = table
    pid = t.join("Ada")["player_id"]
    t.open_betting(host, 5)
    assert t.bet(pid, "10")["balance"] == 40
    assert store.balance(pid) == 40

    start_walk(t, manager, host)
    clock[0] += 5.0                                 # past the warm-up: 1.27x at the 0.12 growth rate
    cashed = t.cash_out(pid)
    assert cashed["payout"] == 13 and cashed["balance"] == 53
    assert store.balance(pid) == 53
    with pytest.raises(HTTPException):
        t.cash_out(pid)
    assert store.audit() == []


def test_crash_settles_every_riding_bet(table):
    t, store, manager, host, clock = table
    a, b = t.join("Ada")["player_id"], t.join("Bo")["player_id"]
    t.open_betting(host, 5)
    t.bet(a, "5")
    t.bet(b, "7")
    round_id = start_walk(t, manager, host)

    clock[0] += 60.0
    state = t.state(a)
    assert state["phase"] == "crashed"
    conn = store._connect()
    try:
        statuses = {r[0] for r in conn.execute("SELECT status FROM crowd_bets WHERE round_id = ?", (round_id,))}
    finally:
        conn.close()
    assert statuses == {"crashed"}
    assert store.balance(a) == 45 and store.balance(b) == 43


def test_bets_are_whole_points_within_the_wallet(table):
    t, store, manager, host, _ = table
    pid = t.join("Ada")["player_id"]
    t.open_betting(host, 5)
    with pytest.raises(HTTPException) as fractional:
        t.bet(pid, "2.5")
    assert fractional.value.status_code == 400
    with pytest.raises(HTTPException) as too_big:
        t.bet(pid, "60")
    assert too_big.value.status_code == 400 and "Not enough points" in too_big.value.detail
    assert store.balance(pid) == 50


def test_restart_refunds_bets_still_in_play(table):
    t, store, manager, host, _ = table
    pid = t.join("Ada")["player_id"]
    t.open_betting(host, 5)
    t.bet(pid, "10")
    start_walk(t, manager, host)

    assert store.recover_interrupted_rounds() == 2  # the host round and the phone bet
    assert store.balance(pid) == 50
    assert store.audit() == []
