"""The house table: runs rounds for watching phones, sleeps when nobody watches, yields to a real host."""

import pytest

from autohost import AutoHost
from crowd import CrowdTable
from database.connection import initialize_database
from database.wallet_store import WalletStore
from round_manager import RoundManager


@pytest.fixture
def house(tmp_path, monkeypatch):
    monkeypatch.setattr("round_manager.draw_crash_point", lambda mode: (3.0, None))
    db = tmp_path / "auto.db"
    initialize_database(db)
    store = WalletStore(db)
    clock = [100.0]
    manager = RoundManager(clock=lambda: clock[0], session_store=store)
    table = CrowdTable(manager, store)

    def sleep(seconds):
        clock[0] += seconds

    auto = AutoHost(manager, table, rng_mode="secure", sleep=sleep, now=lambda: clock[0],
                    betting_seconds=5.0, result_seconds=2.0, idle_after=30.0, takeover_after=20.0)
    return auto, table, manager, store, clock


def watch(table, clock):
    pid = table.join("Ada", age_confirmed=True)["player_id"]
    table.last_seen = clock[0]
    return pid


def test_nobody_watching_means_no_round(house):
    auto, table, manager, store, clock = house
    assert auto.cycle() is False
    assert table.host_session is None and auto.rounds == 0


def test_a_watched_table_plays_a_whole_round(house):
    auto, table, manager, store, clock = house
    pid = watch(table, clock)
    played = []

    original = auto.sleep

    def sleep_and_bet(seconds):
        original(seconds)
        table.last_seen = clock[0]
        if table.phase == "betting" and table.players[pid]["bet"] is None:
            table.bet(pid, "10")
            played.append(1)

    auto.sleep = sleep_and_bet
    assert auto.cycle() is True
    assert table.host_session == auto.sid
    assert auto.rounds == 1
    assert table.phase == "crashed"
    assert table.history == [3.0]
    assert played                                  # the phone could bet during the house's betting window
    assert table.players[pid]["payout"] == 0      # it never cashed out, so it burned


def test_two_rounds_in_a_row_keep_the_same_seat(house):
    auto, table, manager, store, clock = house
    watch(table, clock)
    seen = []
    original = auto.sleep

    def sleep(seconds):
        original(seconds)
        table.last_seen = clock[0]

    auto.sleep = sleep
    auto.cycle()
    seen.append(table.host_session)
    auto.cycle()
    seen.append(table.host_session)
    assert seen[0] == seen[1] == auto.sid
    assert table.round_no == 2


def test_a_spent_wallet_is_replaced(house):
    auto, table, manager, store, clock = house
    watch(table, clock)
    original = auto.sleep

    def sleep(seconds):
        original(seconds)
        table.last_seen = clock[0]

    auto.sleep = sleep
    auto.cycle()
    first = auto.sid
    for _ in range(60):                            # the house burns one point a round until its 50 are gone
        if auto.sid != first:
            break
        auto.cycle()
    assert auto.sid != first                       # a fresh wallet took over
    assert auto._can_afford()
    assert table.host_session == auto.sid
    assert manager.get_balance(first)["balance"] in ("0", 0)


def test_a_real_host_is_left_alone_until_the_table_stands_still(house):
    auto, table, manager, store, clock = house
    watch(table, clock)
    real = manager.create_session()["session_id"]
    store.confirm_age(real)
    table.set_host(real, 8001, "Screen")
    assert auto.ensure_host() is False             # first sight of the other host
    clock[0] += 10
    assert auto.ensure_host() is False             # still inside the grace period
    clock[0] += 15
    assert auto.ensure_host() is True              # nothing moved for 25 s: the house takes the seat
    assert table.host_session == auto.sid != real


def test_an_active_real_host_keeps_the_seat(house):
    auto, table, manager, store, clock = house
    watch(table, clock)
    real = manager.create_session()["session_id"]
    store.confirm_age(real)
    table.set_host(real, 8001, "Screen")
    assert auto.ensure_host() is False
    for _ in range(5):
        clock[0] += 15
        table.open_betting(real, 8)                # the real host keeps opening betting (state changes)
        table.round_no += 1
        assert auto.ensure_host() is False
    assert table.host_session == real


def test_start_only_with_the_switch(monkeypatch, house):
    import autohost
    auto, table, manager, store, clock = house
    monkeypatch.delenv("AUTO_HOST", raising=False)
    assert autohost.start_if_enabled(manager, table, "secure") is None
