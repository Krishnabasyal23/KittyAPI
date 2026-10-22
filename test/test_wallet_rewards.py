import sqlite3
import threading

import pytest

from database.connection import initialize_database
from database.wallet_store import WalletStore
from errors import (
    AlreadyClaimed,
    FeatureDisabled,
    IdempotencyConflict,
    InvalidTicket,
    LimitReached,
    PlayBlocked,
    TicketLocked,
    WalletError,
)
from lottery import NOT_VERIFIED, SignedTicketValidator
from payments import FakePaymentProvider

DAY = ["2026-10-20"]


@pytest.fixture
def store(tmp_path):
    db = tmp_path / "wallet.db"
    initialize_database(db)
    s = WalletStore(db, today=lambda: DAY[0])
    DAY[0] = "2026-10-20"
    s.ensure_wallet("w1", "pc")
    s.ensure_wallet("w2", "phone", "Ada")
    s.confirm_age("w1")
    s.confirm_age("w2")
    return s


@pytest.fixture
def mock():
    return SignedTicketValidator(b"unit-test-ticket-key-0123456789")


def kinds(store, wallet):
    return [e["kind"] for e in reversed(store.get_ledger(wallet, 100))]


# STARTING GRANT AND DAILY FREE

def test_starting_grant_is_50_once(store):
    store.ensure_wallet("w1", "pc")
    assert store.balance("w1") == 50
    assert kinds(store, "w1") == ["starting_grant"]


def test_daily_free_once_per_server_day(store):
    assert store.claim_daily_free("w1")["balance"] == 70
    with pytest.raises(AlreadyClaimed):
        store.claim_daily_free("w1")

    DAY[0] = "2026-10-21"
    assert store.claim_daily_free("w1")["balance"] == 90
    assert store.audit() == []


def test_unique_index_backs_up_the_daily_rule(store):
    store.claim_daily_free("w1")
    conn = store._connect()
    try:
        with pytest.raises(sqlite3.IntegrityError):
            with conn:
                conn.execute("UPDATE wallets SET balance_points = balance_points + 20 WHERE wallet_id = 'w1'")
                conn.execute("INSERT INTO ledger(wallet_id, kind, amount_points, balance_after, day)"
                             " VALUES ('w1', 'daily_free', 20, 90, '2026-10-20')")
    finally:
        conn.close()
    assert store.balance("w1") == 70


# SIMULATED PURCHASES

def test_purchases_need_demo_mode_and_a_key(store):
    with pytest.raises(FeatureDisabled):
        store.purchase("w1", "topup_5", None, "key-00000001")
    with pytest.raises(WalletError, match="Idempotency-Key"):
        store.purchase("w1", "topup_5", FakePaymentProvider(), None)
    with pytest.raises(WalletError, match="Unknown product"):
        store.purchase("w1", "gold_bars", FakePaymentProvider(), "key-00000001")
    assert kinds(store, "w1") == ["starting_grant"]


def test_topup_gives_40_and_is_labelled_simulated(store):
    receipt = store.purchase("w1", "topup_5", FakePaymentProvider(), "key-00000001")
    assert receipt["status"] == "succeeded" and receipt["points"] == 40 and receipt["balance"] == 90
    assert receipt["simulated"] is True and "SIMULATED" in receipt["notice"]
    assert receipt["price"] == "5.00" and receipt["provider"] == "fake"

    store.purchase("w1", "topup_5", FakePaymentProvider(), "key-00000002")   # top-ups repeat
    assert store.balance("w1") == 130
    ledger = store.get_ledger("w1")
    assert ledger[0]["kind"] == "topup" and ledger[0]["reference"].startswith("fakepay_")
    assert [r["status"] for r in store.get_receipts("w1")] == ["succeeded", "succeeded"]


def test_parking_bundle_gives_60_once_per_day(store):
    assert store.purchase("w1", "parking_5", FakePaymentProvider(), "park-00000001")["balance"] == 110
    with pytest.raises(LimitReached):
        store.purchase("w1", "parking_5", FakePaymentProvider(), "park-00000002")
    DAY[0] = "2026-10-21"
    assert store.purchase("w1", "parking_5", FakePaymentProvider(), "park-00000003")["balance"] == 170


def test_idempotent_purchase_charges_once(store):
    first = store.purchase("w1", "topup_5", FakePaymentProvider(), "same-key-0001")
    again = store.purchase("w1", "topup_5", FakePaymentProvider(), "same-key-0001")
    assert again == {**first, "replayed": True}
    assert store.balance("w1") == 90
    assert len(store.get_receipts("w1")) == 1

    with pytest.raises(IdempotencyConflict):
        store.purchase("w1", "parking_5", FakePaymentProvider(), "same-key-0001")


def test_declined_payment_records_a_receipt_but_no_points(store):
    receipt = store.purchase("w1", "topup_5", FakePaymentProvider(decline=True), "decline-0001")
    assert receipt["status"] == "declined" and receipt["points"] == 0 and receipt["entry_id"] is None
    assert store.balance("w1") == 50
    assert kinds(store, "w1") == ["starting_grant"]


def test_self_exclusion_blocks_purchases(store):
    store.set_rg_settings("w1", self_excluded_until="2999-01-01T00:00:00+00:00")
    with pytest.raises(PlayBlocked):
        store.purchase("w1", "topup_5", FakePaymentProvider(), "key-00000001")
    assert store.get_receipts("w1") == []


# MOCK LOTTERY TICKETS

def test_genuine_code_redeems_for_25_exactly_once(store, mock):
    code = store.issue_ticket(mock)["code"]
    result = store.redeem_lottery_ticket("w1", code, mock)
    assert result["points"] == 25 and result["balance"] == 75 and "not a real lottery ticket" in result["notice"]
    with pytest.raises(InvalidTicket, match=NOT_VERIFIED):
        store.redeem_lottery_ticket("w2", code, mock)          # used: same message as a wrong code
    assert store.balance("w2") == 50


def test_codes_are_accepted_however_they_are_typed(store, mock):
    code = store.issue_ticket(mock)["code"]
    messy = " " + code.lower().replace("-", " ") + " "
    assert store.redeem_lottery_ticket("w1", messy, mock)["points"] == 25


@pytest.mark.parametrize("wrong", ["", "hello", "0" * 20, "MOCK-0000-0000-0000"])
def test_wrong_codes_all_get_the_same_message(store, mock, wrong):
    with pytest.raises(InvalidTicket) as err:
        store.redeem_lottery_ticket("w1", wrong, mock)
    assert str(err.value) == NOT_VERIFIED


def test_a_code_from_another_key_is_refused(store, mock):
    other = SignedTicketValidator(b"some-other-key-9876543210").issue()
    with pytest.raises(InvalidTicket, match=NOT_VERIFIED):
        store.redeem_lottery_ticket("w1", other, mock)


def test_one_ticket_per_wallet_per_day(store, mock):
    codes = [store.issue_ticket(mock)["code"] for _ in range(3)]
    store.redeem_lottery_ticket("w1", codes[0], mock)
    with pytest.raises(LimitReached):
        store.redeem_lottery_ticket("w1", codes[1], mock)
    store.redeem_lottery_ticket("w2", codes[1], mock)          # per wallet
    DAY[0] = "2026-10-21"
    store.redeem_lottery_ticket("w1", codes[2], mock)          # next day
    assert store.balance("w1") == 100


def test_three_wrong_codes_lock_entry_for_an_hour(store, mock):
    from datetime import datetime, timedelta, timezone
    now = [datetime(2026, 10, 20, 12, 0, tzinfo=timezone.utc)]
    store.now = lambda: now[0]
    for _ in range(3):
        with pytest.raises(InvalidTicket):
            store.redeem_lottery_ticket("w1", "nope", mock)
    good = store.issue_ticket(mock)["code"]
    with pytest.raises(TicketLocked, match="60 min"):
        store.redeem_lottery_ticket("w1", good, mock)          # even a genuine code waits
    assert store.wallet_summary("w1", tickets=True)["lottery"]["available"] is False
    now[0] += timedelta(minutes=59)
    with pytest.raises(TicketLocked, match="1 min"):
        store.redeem_lottery_ticket("w1", good, mock)
    now[0] += timedelta(minutes=2)
    assert store.redeem_lottery_ticket("w1", good, mock)["points"] == 25


def test_a_good_code_resets_the_wrong_count(store, mock):
    for _ in range(2):
        with pytest.raises(InvalidTicket):
            store.redeem_lottery_ticket("w1", "nope", mock)
    store.redeem_lottery_ticket("w1", store.issue_ticket(mock)["code"], mock)
    DAY[0] = "2026-10-21"
    for _ in range(2):
        with pytest.raises(InvalidTicket):
            store.redeem_lottery_ticket("w1", "nope", mock)
    assert store.redeem_lottery_ticket("w1", store.issue_ticket(mock)["code"], mock)["points"] == 25


def test_daily_limit_is_checked_before_the_code(store, mock):
    store.redeem_lottery_ticket("w1", store.issue_ticket(mock)["code"], mock)
    for _ in range(5):                                          # no lock: these never reach validation
        with pytest.raises(LimitReached):
            store.redeem_lottery_ticket("w1", "nope", mock)


def test_ticket_entry_off_without_a_key(store, mock):
    with pytest.raises(FeatureDisabled):
        store.issue_ticket(None)
    with pytest.raises(FeatureDisabled):
        store.redeem_lottery_ticket("w1", mock.issue(), None)


# CONCURRENCY (each worker thread uses its own connection, like the API's threadpool)

def race(n, fn):
    results, barrier = [], threading.Barrier(n)

    def run(i):
        barrier.wait()
        try:
            results.append(("ok", fn(i)))
        except Exception as exc:   # noqa: BLE001 - collecting outcomes
            results.append(("err", type(exc).__name__))

    threads = [threading.Thread(target=run, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return results


def test_concurrent_daily_claims_pay_once(store):
    results = race(20, lambda i: store.claim_daily_free("w1"))
    assert sum(r[0] == "ok" for r in results) == 1
    assert {r[1] for r in results if r[0] == "err"} == {"AlreadyClaimed"}
    assert store.balance("w1") == 70


def test_concurrent_redemptions_of_one_ticket_pay_once(store, mock):
    code = store.issue_ticket(mock)["code"]
    wallets = [f"racer{i}" for i in range(12)]
    for w in wallets:
        store.ensure_wallet(w, "phone")
    results = race(12, lambda i: store.redeem_lottery_ticket(wallets[i], code, mock))
    assert sum(r[0] == "ok" for r in results) == 1
    assert sum(store.balance(w) for w in wallets) == 12 * 50 + 25


def test_concurrent_tickets_respect_the_daily_cap(store, mock):
    codes = [store.issue_ticket(mock)["code"] for _ in range(10)]
    results = race(10, lambda i: store.redeem_lottery_ticket("w1", codes[i], mock))
    assert sum(r[0] == "ok" for r in results) == 1
    assert store.balance("w1") == 50 + 25


def test_concurrent_same_key_purchases_charge_once(store):
    results = race(10, lambda i: store.purchase("w1", "topup_5", FakePaymentProvider(), "race-key-001"))
    assert all(r[0] == "ok" for r in results)
    assert store.balance("w1") == 90
    assert len(store.get_receipts("w1")) == 1
    assert store.audit() == []
