"""Operator dashboard: report numbers, local-only access and CSV exports."""

from datetime import date

import pytest

import main
import reports
from database.connection import initialize_database
from database.wallet_store import WalletStore
from fastapi.testclient import TestClient
from payments import FakePaymentProvider

client = TestClient(main.app)
local = TestClient(main.app, client=("127.0.0.1", 50002))
TODAY = date.today().isoformat()


@pytest.fixture
def store(tmp_path):
    db = tmp_path / "ops.db"
    initialize_database(db)
    s = WalletStore(db)
    for w in ("a", "b"):
        s.ensure_wallet(w, "pc")
        s.confirm_age(w)
    return s


def play(store, wallet, round_id, wager, cashout=None):
    store.record_wager(wallet, round_id, wager, 2.0, 5.0, "fish", season="winter")
    if cashout:
        from economy import calculate_cashout
        store.settle_round(wallet, round_id, "cashed_out", calculate_cashout(wager, cashout), cashout)
    else:
        store.settle_round(wallet, round_id, "crashed")


def test_summary_counts_rtp_players_and_topups(store):
    play(store, "a", "r1", 10, 1.5)      # pays 15
    play(store, "b", "r2", 10)           # pays 0
    store.purchase("a", "topup", FakePaymentProvider(), "ops-topup-1", 7)
    conn = store._connect()
    try:
        s = reports.summary(conn, 7, TODAY)
    finally:
        conn.close()
    assert s["bets"]["today"] == 2 and s["bets"]["wagered"] == 20 and s["bets"]["paid"] == 15
    assert s["rtp"]["today"] == 75.0
    assert s["rtp"]["by_season"] == [{"season": "winter", "bets": 2, "wagered": 20, "paid": 15, "rtp": 75.0}]
    assert s["players"]["wallets"] == 2 and s["players"]["active_today"] == 2
    assert s["topups"]["today_dollars"] == 7 and s["topups"]["by_provider"]["fake"]["points"] == 56
    assert len(s["daily"]) == 7 and s["daily"][-1]["day"] == TODAY and s["daily"][-1]["wagered"] == 20


def test_running_and_interrupted_rounds_are_not_counted(store):
    store.record_wager("a", "r9", 10, 2.0, 5.0, "fish")
    conn = store._connect()
    try:
        assert reports.summary(conn, 1, TODAY)["bets"]["all_time"] == 0
    finally:
        conn.close()


def test_export_csv_has_a_header_and_rows(store):
    play(store, "a", "r1", 10, 2.0)
    conn = store._connect()
    try:
        bets = reports.export_csv(conn, "bets").splitlines()
        daily = reports.export_csv(conn, "daily", 3).splitlines()
        with pytest.raises(KeyError):
            reports.export_csv(conn, "secrets")
    finally:
        conn.close()
    assert bets[0].startswith("day,season,wager,payout") and len(bets) == 2
    assert daily[0].startswith("day,bets,players") and len(daily) == 4


def test_operator_is_local_only():
    assert client.get("/operator").status_code == 403
    assert client.get("/api/operator/summary").status_code == 403
    assert client.get("/api/operator/export/ledger.csv").status_code == 403


def test_operator_endpoints():
    assert local.get("/operator").status_code == 200
    s = local.get("/api/operator/summary?days=3").json()
    assert len(s["daily"]) == 3 and "ledger_ok" in s and s["version"] == main.VERSION
    r = local.get("/api/operator/export/payments.csv")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/csv")
    assert r.text.startswith("payment_id,provider")
    assert local.get("/api/operator/export/nope.csv").status_code == 404
