from datetime import datetime, timedelta, timezone

import pytest

import main
from database.connection import initialize_database
from database.wallet_store import WalletStore
from errors import PlayBlocked, WalletError
from fastapi.testclient import TestClient
from payments import FakePaymentProvider

NOW = [datetime(2026, 10, 20, 15, 0, tzinfo=timezone.utc)]


@pytest.fixture
def store(tmp_path):
    db = tmp_path / "rg-controls.db"
    initialize_database(db)
    NOW[0] = datetime(2026, 10, 20, 15, 0, tzinfo=timezone.utc)
    s = WalletStore(db, today=lambda: NOW[0].date().isoformat(), now=lambda: NOW[0])
    s.ensure_wallet("w1", "pc")
    return s


def later(**delta):
    NOW[0] += timedelta(**delta)


# 19+ GATE

def test_age_gate_blocks_wagers_and_purchases_until_confirmed(store):
    with pytest.raises(PlayBlocked, match="19 or older"):
        store.record_wager("w1", "r1", 5, 2.0, 3.0, "fish")
    with pytest.raises(PlayBlocked, match="19 or older"):
        store.purchase("w1", "topup_5", FakePaymentProvider(), "age-key-0001")
    assert store.balance("w1") == 50
    assert store.responsible_view("w1")["can_play"] is False

    store.confirm_age("w1")
    store.record_wager("w1", "r1", 5, 2.0, 3.0, "fish")
    assert store.responsible_view("w1")["can_play"] is True


def test_age_confirmation_keeps_the_first_time(store):
    first = store.confirm_age("w1").age_confirmed_at
    later(days=1)
    assert store.confirm_age("w1").age_confirmed_at == first


def test_rewards_do_not_need_the_age_gate(store):
    assert store.claim_daily_free("w1")["balance"] == 70


# DAILY SPEND LIMIT

def test_first_limit_and_lowering_apply_at_once(store):
    store.confirm_age("w1")
    assert store.set_spend_limit("w1", 30).daily_spend_limit_points == 30
    assert store.set_spend_limit("w1", 10).daily_spend_limit_points == 10
    with pytest.raises(PlayBlocked, match="Daily spend limit"):
        store.record_wager("w1", "r1", 11, 2.0, 3.0, "fish")


def test_raising_waits_24_hours(store):
    store.set_spend_limit("w1", 10)
    rg = store.set_spend_limit("w1", 40)
    assert rg.daily_spend_limit_points == 10
    view = store.responsible_view("w1")
    assert view["pending_limit"]["daily_spend_limit_points"] == 40

    later(hours=23, minutes=59)
    assert store.get_rg_settings("w1").daily_spend_limit_points == 10
    later(minutes=2)
    assert store.get_rg_settings("w1").daily_spend_limit_points == 40
    assert store.responsible_view("w1")["pending_limit"] is None


def test_removing_a_limit_also_waits(store):
    store.set_spend_limit("w1", 10)
    assert store.set_spend_limit("w1", None).daily_spend_limit_points == 10
    later(hours=25)
    assert store.get_rg_settings("w1").daily_spend_limit_points is None


def test_lowering_cancels_a_pending_raise(store):
    store.set_spend_limit("w1", 10)
    store.set_spend_limit("w1", 40)
    store.set_spend_limit("w1", 5)
    later(hours=25)
    assert store.get_rg_settings("w1").daily_spend_limit_points == 5


def test_limit_must_be_sensible(store):
    for bad in (0, -5, 100_001):
        with pytest.raises((WalletError, ValueError)):
            store.set_spend_limit("w1", bad)


# COOL-OFF AND SELF-EXCLUSION

def test_cool_off_blocks_and_cannot_be_shortened(store):
    store.confirm_age("w1")
    store.start_cool_off("w1", 24)
    until = store.get_rg_settings("w1").cool_off_until
    store.start_cool_off("w1", 1)                      # shorter: ignored
    assert store.get_rg_settings("w1").cool_off_until == until
    with pytest.raises(PlayBlocked, match="Cooling off"):
        store.record_wager("w1", "r1", 5, 2.0, 3.0, "fish")
    with pytest.raises(PlayBlocked):
        store.purchase("w1", "topup_5", FakePaymentProvider(), "cool-key-001")

    later(hours=24, seconds=1)
    store.record_wager("w1", "r1", 5, 2.0, 3.0, "fish")


def test_self_exclusion_extends_only(store):
    store.self_exclude("w1", 180)
    until = store.get_rg_settings("w1").self_excluded_until
    store.self_exclude("w1", 30)
    assert store.get_rg_settings("w1").self_excluded_until == until
    store.self_exclude("w1", 365)
    assert store.get_rg_settings("w1").self_excluded_until > until
    assert store.responsible_view("w1")["self_excluded_until"] is not None


@pytest.mark.parametrize("call, value", [("start_cool_off", 2), ("self_exclude", 1), ("set_reality_check", 7)])
def test_only_offered_choices_are_accepted(store, call, value):
    with pytest.raises(WalletError):
        getattr(store, call)("w1", value)


# REALITY CHECK

def test_reality_check_default_and_choice(store):
    assert store.responsible_view("w1")["reality_check_minutes"] == 15
    store.set_reality_check("w1", 30)
    assert store.responsible_view("w1")["reality_check_minutes"] == 30


def test_activity_since_a_moment(store):
    store.confirm_age("w1")
    start = store.wallet_summary("w1")["server_time"]
    store.record_wager("w1", "r1", 10, 2.0, 3.0, "fish")
    store.settle_round("w1", "r1", "cashed_out", 15, 1.5)
    store.record_wager("w1", "r2", 5, 2.0, 3.0, "fish")
    store.settle_round("w1", "r2", "crashed")
    later(minutes=16)

    act = store.activity("w1", start)
    assert act["bets"] == 2 and act["wagered"] == 15 and act["won"] == 15 and act["net"] == 0
    assert act["minutes"] == 16


# HTTP

client = TestClient(main.app)


def wallet():
    return {"X-Session-ID": client.post("/api/sessions").json()["session_id"]}


def test_controls_need_an_explicit_confirm():
    h = wallet()
    assert client.post("/api/wallet/age-confirm", headers=h, json={}).status_code == 400
    assert client.post("/api/wallet/cool-off", headers=h, json={"hours": 1}).status_code == 400
    assert client.post("/api/wallet/self-exclude", headers=h, json={"days": 30}).status_code == 400
    assert client.get("/api/wallet/limits", headers=h).json()["age_confirmed"] is False


def test_limits_endpoint_round_trip():
    h = wallet()
    body = client.put("/api/wallet/limits", headers=h,
                      json={"daily_spend_limit_points": 20, "reality_check_minutes": 60}).json()
    assert body["daily_spend_limit_points"] == 20 and body["reality_check_minutes"] == 60
    raised = client.put("/api/wallet/limits", headers=h, json={"daily_spend_limit_points": 80}).json()
    assert raised["daily_spend_limit_points"] == 20 and raised["pending_limit"]["daily_spend_limit_points"] == 80
    unchanged = client.put("/api/wallet/limits", headers=h, json={"reality_check_minutes": 30}).json()
    assert unchanged["pending_limit"] is not None    # omitting the limit field leaves it alone


def test_cool_off_endpoint_blocks_rounds():
    h = wallet()
    client.post("/api/wallet/age-confirm", headers=h, json={"confirm": True})
    assert client.post("/api/wallet/cool-off", headers=h, json={"hours": 1, "confirm": True}).status_code == 200
    response = client.post("/api/rounds", headers=h, json={"wager": "5"})
    assert response.status_code == 403 and "Cooling off" in response.json()["detail"]


def test_purchase_needs_the_confirmation_checkbox(monkeypatch):
    monkeypatch.setenv("DEMO_MODE", "1")
    h = wallet() | {"Idempotency-Key": "box-00000001"}
    client.post("/api/wallet/age-confirm", headers=h, json={"confirm": True})
    response = client.post("/api/wallet/purchase", headers=h, json={"product": "topup_5"})
    assert response.status_code == 400 and "Tick the box" in response.json()["detail"]


def test_activity_endpoint():
    h = wallet()
    start = client.get("/api/wallet", headers=h).json()["server_time"]
    body = client.get("/api/wallet/activity", headers=h, params={"since": start}).json()
    assert body["bets"] == 0 and body["net"] == 0


def test_phone_join_records_age():
    joined = client.post("/api/crowd/join", json={"name": "Teen?", "age_confirmed": False}).json()
    h = {"X-Session-ID": joined["player_id"]}
    assert client.get("/api/wallet/limits", headers=h).json()["age_confirmed"] is False
    client.post("/api/crowd/join", json={"name": "x", "player_id": joined["player_id"], "age_confirmed": True})
    assert client.get("/api/wallet/limits", headers=h).json()["age_confirmed"] is True
