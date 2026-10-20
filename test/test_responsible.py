from datetime import datetime, timedelta, timezone

import pytest

from database.connection import initialize_database
from database.wallet_store import WalletStore
from errors import PlayBlocked
from responsible import DefaultPolicy, RGSettings

NOW = datetime(2026, 10, 20, 15, 0, tzinfo=timezone.utc)


@pytest.fixture
def store(tmp_path):
    db = tmp_path / "rg.db"
    initialize_database(db)
    s = WalletStore(db, today=lambda: "2026-10-20", now=lambda: NOW)
    s.ensure_wallet("pc1", "pc")
    return s


def iso(moment):
    return moment.isoformat()


def test_no_settings_means_no_limits(store):
    assert store.get_rg_settings("pc1") == RGSettings()
    store.record_wager("pc1", "r1", 50, 2.0, 3.0, "fish")
    assert store.balance("pc1") == 0


def test_self_exclusion_blocks_wagers_without_touching_points(store):
    store.set_rg_settings("pc1", self_excluded_until=iso(NOW + timedelta(days=30)))
    with pytest.raises(PlayBlocked, match="Self-excluded"):
        store.record_wager("pc1", "r1", 10, 2.0, 3.0, "fish")
    assert store.balance("pc1") == 50
    assert store.get_round("pc1", "r1") is None


def test_cool_off_ends_on_time(store):
    store.set_rg_settings("pc1", cool_off_until=iso(NOW + timedelta(minutes=15)))
    with pytest.raises(PlayBlocked, match="Cooling off"):
        store.record_wager("pc1", "r1", 10, 2.0, 3.0, "fish")

    store.now = lambda: NOW + timedelta(minutes=16)
    store.record_wager("pc1", "r1", 10, 2.0, 3.0, "fish")
    assert store.balance("pc1") == 40


def test_daily_spend_limit_counts_net_wagers_today(store):
    store.set_rg_settings("pc1", daily_spend_limit_points=15)
    store.record_wager("pc1", "r1", 10, 2.0, 3.0, "fish")
    store.settle_round("pc1", "r1", "crashed")

    with pytest.raises(PlayBlocked, match="Daily spend limit"):
        store.record_wager("pc1", "r2", 6, 2.0, 3.0, "fish")
    store.record_wager("pc1", "r2", 5, 2.0, 3.0, "fish")   # exactly at the limit is allowed

    store.today = lambda: "2026-10-21"                       # a new server day resets it
    store.settle_round("pc1", "r2", "crashed")
    store.record_wager("pc1", "r3", 10, 2.0, 3.0, "fish")


def test_settings_upsert_and_clear(store):
    store.set_rg_settings("pc1", daily_spend_limit_points=20, reality_check_minutes=15)
    store.set_rg_settings("pc1", daily_spend_limit_points=None)
    assert store.get_rg_settings("pc1") == RGSettings(reality_check_minutes=15)
    with pytest.raises(ValueError, match="Unknown setting"):
        store.set_rg_settings("pc1", max_fun=3)


def test_policy_is_replaceable(store):
    class NoWagersOnTuesday(DefaultPolicy):
        def check_wager(self, settings, spent_today, points, now):
            raise PlayBlocked("closed")

    store.policy = NoWagersOnTuesday()
    with pytest.raises(PlayBlocked, match="closed"):
        store.record_wager("pc1", "r1", 10, 2.0, 3.0, "fish")
    assert store.balance("pc1") == 50
