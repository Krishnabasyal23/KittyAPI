import pytest

import main
from fastapi.testclient import TestClient

client = TestClient(main.app)


@pytest.fixture
def demo(monkeypatch):
    monkeypatch.setenv("DEMO_MODE", "1")


@pytest.fixture
def no_demo(monkeypatch):
    monkeypatch.delenv("DEMO_MODE", raising=False)


def new_wallet():
    sid = client.post("/api/sessions").json()["session_id"]
    return {"X-Session-ID": sid}


def test_new_session_is_a_50_point_wallet(no_demo):
    h = new_wallet()
    body = client.get("/api/wallet", headers=h).json()
    assert body["balance"] == 50 and body["kind"] == "pc"
    assert body["daily_free"] == {"points": 20, "available": True}
    assert body["demo_mode"] is False
    assert all(p["available"] is False and p["simulated"] for p in body["products"])
    assert client.get("/api/player/balance", headers=h).json() == {"balance": "50"}


def test_unknown_wallet_is_404():
    assert client.get("/api/wallet", headers={"X-Session-ID": "nobody"}).status_code == 404


def test_daily_free_endpoint(no_demo):
    h = new_wallet()
    assert client.post("/api/wallet/daily-free", headers=h).json()["balance"] == 70
    second = client.post("/api/wallet/daily-free", headers=h)
    assert second.status_code == 409
    assert client.get("/api/wallet", headers=h).json()["daily_free"]["available"] is False


def test_purchase_needs_demo_mode(no_demo):
    h = new_wallet() | {"Idempotency-Key": "buy-00000001"}
    response = client.post("/api/wallet/purchase", headers=h, json={"product": "topup_5"})
    assert response.status_code == 503


def test_purchase_needs_an_idempotency_key(demo):
    response = client.post("/api/wallet/purchase", headers=new_wallet(), json={"product": "topup_5"})
    assert response.status_code == 400 and "Idempotency-Key" in response.json()["detail"]


def test_purchase_and_retry(demo):
    h = new_wallet() | {"Idempotency-Key": "buy-00000001"}
    first = client.post("/api/wallet/purchase", headers=h, json={"product": "parking_5"})
    assert first.status_code == 200
    receipt = first.json()
    assert receipt["points"] == 60 and receipt["balance"] == 110 and receipt["simulated"] is True

    retry = client.post("/api/wallet/purchase", headers=h, json={"product": "parking_5"})
    assert retry.json() == {**receipt, "replayed": True}
    assert client.get("/api/wallet", headers=h).json()["balance"] == 110

    again = client.post("/api/wallet/purchase", headers=h | {"Idempotency-Key": "buy-00000002"},
                        json={"product": "parking_5"})
    assert again.status_code == 409
    assert len(client.get("/api/wallet/receipts", headers=h).json()) == 1


def test_lottery_flow(demo):
    h = new_wallet()
    ticket = client.post("/api/demo/lottery/tickets").json()
    assert "not a real lottery ticket" in ticket["notice"]

    redeemed = client.post("/api/wallet/lottery/redeem", headers=h,
                           json={"ticket_id": ticket["ticket_id"], "numbers": ticket["numbers"]})
    assert redeemed.status_code == 200 and redeemed.json()["balance"] == 75

    reused = client.post("/api/wallet/lottery/redeem", headers=new_wallet(),
                         json={"ticket_id": ticket["ticket_id"], "numbers": ticket["numbers"]})
    assert reused.status_code == 409

    bad = client.post("/api/demo/lottery/tickets", json={"numbers": "01 02 04 08 15"}).json()
    refused = client.post("/api/wallet/lottery/redeem", headers=h,
                          json={"ticket_id": bad["ticket_id"], "numbers": bad["numbers"]})
    assert refused.status_code == 400


def test_lottery_off_without_demo_mode(no_demo):
    assert client.post("/api/demo/lottery/tickets").status_code == 503


def test_ledger_shows_every_move(demo):
    h = new_wallet()
    client.post("/api/wallet/daily-free", headers=h)
    client.post("/api/wallet/purchase", headers=h | {"Idempotency-Key": "buy-00000009"}, json={"product": "topup_5"})
    ledger = client.get("/api/wallet/ledger", headers=h).json()
    assert [e["kind"] for e in ledger] == ["topup", "daily_free", "starting_grant"]
    assert [e["balance_after"] for e in ledger] == [110, 70, 50]


def test_health_reports_wallet_settings(demo):
    body = client.get("/api/health").json()
    assert body["demo_mode"] is True and "simulated" in body["demo_notice"]
    assert body["db_schema"] >= 5
    assert body["min_bet"] == "1" and body["max_bet"] == "500"


def test_blocked_wager_is_403(no_demo):
    h = new_wallet()
    main.store.set_rg_settings(h["X-Session-ID"], self_excluded_until="2999-01-01T00:00:00+00:00")
    response = client.post("/api/rounds", headers=h, json={"wager": "5"})
    assert response.status_code == 403
    assert client.get("/api/wallet", headers=h).json()["balance"] == 50


def test_fractional_wager_is_400(no_demo):
    response = client.post("/api/rounds", headers=new_wallet(), json={"wager": "2.5"})
    assert response.status_code == 400
