"""Audit log (append-only) and operator bonus tuning within the safe cap."""

import sqlite3

import pytest

import main
import season_rules
from crash_engine import HOUSE_EDGE
from fastapi.testclient import TestClient

client = TestClient(main.app)
local = TestClient(main.app, client=("127.0.0.1", 50003))


@pytest.fixture(autouse=True)
def default_bonus(monkeypatch):
    monkeypatch.delenv("HAPPY_HOUR", raising=False)
    season_rules.set_bonus_percents(None)
    yield
    season_rules.set_bonus_percents(None)


def new_wallet():
    sid = client.post("/api/sessions").json()["session_id"]
    h = {"X-Session-ID": sid}
    client.post("/api/wallet/age-confirm", headers=h, json={"confirm": True})
    return sid, h


def actions(sid):
    return [e["action"] for e in local.get(f"/api/operator/audit?wallet_id={sid}").json()]


# BONUS CAP

def test_cap_keeps_an_always_bonus_strategy_under_100_percent():
    cap = season_rules.MAX_SAFE_BONUS_PCT
    assert cap == 4
    assert (1 - HOUSE_EDGE) * (1 + cap / 100) < 1.0
    assert (1 - HOUSE_EDGE) * (1 + (cap + 1) / 100) >= 1.0      # one more percent would not be safe


def test_operator_can_lower_the_bonus_and_it_is_used():
    r = local.put("/api/operator/bonus", json={"butterfly": 2, "cocoa": 0})
    assert r.status_code == 200 and r.json()["bonus_pct"] == {"butterfly": 2, "cocoa": 0}
    assert season_rules.bonus_factors() == {"butterfly": 1.02, "cocoa": 1.0}
    assert main.store.get_setting(main.BONUS_SETTING) == {"butterfly": 2, "cocoa": 0}
    change = local.get("/api/operator/audit?limit=5").json()[0]
    assert change["actor"] == "operator" and change["action"] == "setting_changed"
    assert change["detail"]["new"] == {"butterfly": 2, "cocoa": 0}


@pytest.mark.parametrize("body", [{"butterfly": 5, "cocoa": 4}, {"butterfly": -1, "cocoa": 0}, {"butterfly": 2.5, "cocoa": 0}])
def test_bonus_above_the_cap_or_odd_values_are_refused(body):
    assert local.put("/api/operator/bonus", json=body).status_code in (400, 422)
    assert season_rules.bonus_percents() == {"butterfly": 4, "cocoa": 4}


def test_happy_hour_still_wins(monkeypatch):
    local.put("/api/operator/bonus", json={"butterfly": 1, "cocoa": 1})
    monkeypatch.setenv("HAPPY_HOUR", "1")
    assert season_rules.bonus_percents() == {"butterfly": 25, "cocoa": 10}


def test_bonus_and_audit_are_local_only():
    assert client.get("/api/operator/bonus").status_code == 403
    assert client.put("/api/operator/bonus", json={"butterfly": 0, "cocoa": 0}).status_code == 403
    assert client.get("/api/operator/audit").status_code == 403


# AUDIT LOG

def test_play_safe_changes_are_audited():
    sid, h = new_wallet()
    client.put("/api/wallet/limits", headers=h, json={"daily_spend_limit_points": 30, "reality_check_minutes": 30})
    client.post("/api/wallet/cool-off", headers=h, json={"hours": 1, "confirm": True})
    got = actions(sid)
    for a in ("age_confirmed", "spend_limit_set", "reality_check_set", "cool_off_started"):
        assert a in got


def test_cashier_topup_and_code_are_audited(monkeypatch):
    monkeypatch.setenv("DEMO_MODE", "1")
    sid, h = new_wallet()
    code = client.post("/api/profile/qr", headers=h).json()["counter_code"]
    local.post("/api/cashier/topup", json={"code": code, "amount_dollars": 4, "confirmed": True})
    entries = local.get(f"/api/operator/audit?wallet_id={sid}").json()
    topup = next(e for e in entries if e["action"] == "cashier_topup")
    assert topup["actor"] == "cashier" and topup["detail"]["dollars"] == 4 and topup["detail"]["points"] == 32
    assert "cashier_code_issued" in [e["action"] for e in entries]


def test_ticket_lock_is_audited():
    sid, h = new_wallet()
    for _ in range(3):
        client.post("/api/wallet/lottery/redeem", headers=h, json={"code": "wrong"})
    assert "ticket_entry_locked" in actions(sid)


def test_audit_log_is_append_only():
    new_wallet()
    conn = main.store._connect()
    try:
        with pytest.raises(sqlite3.DatabaseError, match="append-only"):
            conn.execute("DELETE FROM audit_log")
        with pytest.raises(sqlite3.DatabaseError, match="append-only"):
            conn.execute("UPDATE audit_log SET action = 'x'")
    finally:
        conn.close()


def test_audit_csv_export():
    new_wallet()
    r = local.get("/api/operator/export/audit.csv")
    assert r.status_code == 200 and r.text.startswith("audit_id,actor,action,wallet_id,detail,created_at")


def test_screens_show_the_tuned_bonus_at_once():
    """The phone's season card and cash button, and the game's banners, read the server's live numbers."""
    local.put("/api/operator/bonus", json={"butterfly": 2, "cocoa": 3})
    sid, h = new_wallet()
    assert client.get("/api/crowd/state").json()["bonus_pct"] == {"butterfly": 2, "cocoa": 3}
    assert client.get("/api/health").json()["bonus_pct"] == {"butterfly": 2, "cocoa": 3}
    spring = season_rules.describe("spring")
    assert "+2%" in spring["rule"] and "+2 %" in season_rules.describe("spring", "fr")["rule"]
    assert "+3%" in season_rules.describe("winter")["rule"]
