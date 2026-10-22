"""Any-amount top-ups, profiles, signed single-use QR codes and the local cashier page."""

import pytest

import identity
import main
from fastapi.testclient import TestClient

client = TestClient(main.app)
counter = TestClient(main.app, client=("127.0.0.1", 50000))   # the cashier is on the server computer


@pytest.fixture
def demo(monkeypatch):
    monkeypatch.setenv("DEMO_MODE", "1")


def new_wallet(adult=True):
    sid = client.post("/api/sessions").json()["session_id"]
    h = {"X-Session-ID": sid}
    if adult:
        client.post("/api/wallet/age-confirm", headers=h, json={"confirm": True})
    return h


def buy(h, dollars, key):
    return client.post("/api/wallet/purchase", headers={**h, "Idempotency-Key": key},
                       json={"product": "topup", "amount_dollars": dollars, "confirmed": True})


# TOP-UPS

def test_topup_any_amount_is_8_points_a_dollar(demo):
    h = new_wallet()
    r = buy(h, 7, "topup-seven-1")
    assert r.status_code == 200
    assert r.json()["points"] == 56 and r.json()["balance"] == 106 and r.json()["price"] == "7.00"
    assert r.json()["label"] == "$7 demo top-up" and r.json()["simulated"] is True


@pytest.mark.parametrize("dollars", [0, 101, -5])
def test_topup_outside_1_to_100_is_refused(demo, dollars):
    assert buy(new_wallet(), dollars, f"topup-bad-{dollars + 10}").status_code == 400


def test_topup_needs_an_amount(demo):
    r = client.post("/api/wallet/purchase", headers={**new_wallet(), "Idempotency-Key": "no-amount-1"},
                    json={"product": "topup", "confirmed": True})
    assert r.status_code == 400


def test_daily_topup_cap(demo):
    h = new_wallet()
    assert buy(h, 100, "cap-one-0001").status_code == 200
    assert buy(h, 90, "cap-two-0001").status_code == 200
    over = buy(h, 11, "cap-three-01")
    assert over.status_code == 409 and "$10 left" in over.json()["detail"]
    assert buy(h, 10, "cap-four-001").status_code == 200
    summary = client.get("/api/wallet", headers=h).json()["topup"]
    assert summary["left_today_dollars"] == 0 and summary["available"] is False


def test_topup_replay_returns_the_same_receipt(demo):
    h = new_wallet()
    first = buy(h, 3, "replay-key-1").json()
    again = buy(h, 3, "replay-key-1").json()
    assert again["payment_id"] == first["payment_id"] and again["replayed"] is True
    assert buy(h, 4, "replay-key-1").status_code == 422


def test_legacy_5_dollar_topup_counts_towards_the_cap(demo):
    h = new_wallet()
    r = client.post("/api/wallet/purchase", headers={**h, "Idempotency-Key": "legacy-5-001"},
                    json={"product": "topup_5", "confirmed": True})
    assert r.json()["points"] == 40
    assert client.get("/api/wallet", headers=h).json()["topup"]["topped_up_today_dollars"] == 5


def test_wallet_summary_lists_the_topup_rules(demo):
    t = client.get("/api/wallet", headers=new_wallet()).json()["topup"]
    assert (t["points_per_dollar"], t["min_dollars"], t["max_dollars"], t["daily_cap_dollars"]) == (8, 1, 100, 200)


# PROFILE

def test_profile_save_and_read():
    h = new_wallet()
    r = client.put("/api/profile", headers=h, json={"nickname": "  Whisker  Fan ", "avatar": "calico"})
    assert r.status_code == 200 and r.json()["nickname"] == "Whisker Fan"
    p = client.get("/api/profile", headers=h).json()
    assert p["avatar"] == "calico" and p["balance"] == 50 and p["stats"]["rounds"] == 0


@pytest.mark.parametrize("nickname,avatar", [("x", "calico"), ("a" * 13, "calico"), ("<b>hi</b>", "calico"),
                                             ("Fine", "dragon")])
def test_profile_rejects_bad_input(nickname, avatar):
    assert client.put("/api/profile", headers=new_wallet(),
                      json={"nickname": nickname, "avatar": avatar}).status_code == 400


# SIGNED QR + CASHIER

def qr(h):
    r = client.post("/api/profile/qr", headers=h)
    assert r.status_code == 201
    return r.json()


def test_qr_token_is_signed():
    body = qr(new_wallet())
    assert body["svg"].startswith("<svg") and body["png_base64"]
    claims = identity.read_token(body["token"])
    assert claims is not None
    prefix, payload, sig = body["token"].split(".")
    assert identity.read_token(f"{prefix}.{payload}.{'A' * len(sig)}") is None


def test_cashier_tops_up_once_per_code(demo):
    h = new_wallet()
    client.put("/api/profile", headers=h, json={"nickname": "Counter Cat", "avatar": "black"})
    code = qr(h)["token"]
    look = counter.post("/api/cashier/lookup", json={"code": code})
    assert look.status_code == 200 and look.json()["nickname"] == "Counter Cat"
    r = counter.post("/api/cashier/topup", json={"code": code, "amount_dollars": 12, "confirmed": True})
    assert r.status_code == 200 and r.json()["points"] == 96 and r.json()["provider"] == "cashier"
    assert client.get("/api/wallet", headers=h).json()["balance"] == 146
    again = counter.post("/api/cashier/topup", json={"code": code, "amount_dollars": 12, "confirmed": True})
    assert again.status_code == 409


def test_cashier_accepts_the_typed_counter_code(demo):
    h = new_wallet()
    code = qr(h)["counter_code"].lower()
    r = counter.post("/api/cashier/topup", json={"code": code, "amount_dollars": 1, "confirmed": True})
    assert r.status_code == 200 and r.json()["balance"] == 58


def test_cashier_refuses_expired_or_forged_codes(demo, monkeypatch):
    h = new_wallet()
    body = qr(h)
    from datetime import timedelta
    later = main.store.now() + timedelta(minutes=6)
    monkeypatch.setattr(main.store, "now", lambda: later)
    assert counter.post("/api/cashier/lookup", json={"code": body["token"]}).status_code == 400
    assert counter.post("/api/cashier/lookup", json={"code": "CQ1.abc.def"}).status_code == 400
    assert counter.post("/api/cashier/lookup", json={"code": "ZZZZ-ZZZZ"}).status_code == 400


def test_cashier_needs_confirmation_and_respects_the_cap(demo):
    h = new_wallet()
    buy(h, 100, "cashier-cap1")
    buy(h, 95, "cashier-cap2")
    code = qr(h)["token"]
    assert counter.post("/api/cashier/topup", json={"code": code, "amount_dollars": 5}).status_code == 400
    assert counter.post("/api/cashier/topup",
                        json={"code": code, "amount_dollars": 6, "confirmed": True}).status_code == 409
    assert counter.post("/api/cashier/topup",
                        json={"code": code, "amount_dollars": 5, "confirmed": True}).status_code == 200


def test_cashier_respects_cool_off(demo):
    h = new_wallet()
    code = qr(h)["token"]
    client.post("/api/wallet/cool-off", headers=h, json={"hours": 24, "confirm": True})
    assert counter.post("/api/cashier/lookup", json={"code": code}).json()["blocked"] is True
    assert counter.post("/api/cashier/topup",
                        json={"code": code, "amount_dollars": 5, "confirmed": True}).status_code == 403


def test_cashier_is_local_only(demo):
    code = qr(new_wallet())["token"]
    assert client.get("/cashier").status_code == 403
    assert client.post("/api/cashier/lookup", json={"code": code}).status_code == 403
    assert counter.get("/cashier").status_code == 200


def test_cashier_off_without_demo_mode(monkeypatch):
    monkeypatch.delenv("DEMO_MODE", raising=False)
    code = qr(new_wallet())["token"]
    assert counter.post("/api/cashier/topup",
                        json={"code": code, "amount_dollars": 5, "confirmed": True}).status_code == 503
