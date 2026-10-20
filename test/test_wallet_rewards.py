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
    TicketAlreadyUsed,
    WalletError,
)
from lottery import MockLotteryValidator, check_pattern, parse_numbers
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
    return MockLotteryValidator()


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

@pytest.mark.parametrize("numbers", ["01 02 04 08 16", "06 12 24 48 96", "03-06-12-24-48", ["05", "10", "20", "40", "80"]])
def test_valid_doubling_patterns(numbers):
    check_pattern(parse_numbers(numbers))


@pytest.mark.parametrize("numbers, message", [
    ("01 02 04 08", "exactly five"),
    ("1 2 4 8 16", "two digits"),
    ("01 02 04 08 17", "double"),
    ("07 14 28 56 112", "two digits"),
    ("00 00 00 00 00", "between 01 and 99"),
    ("01 03 06 12 24", "double"),
])
def test_invalid_patterns_are_refused(numbers, message):
    with pytest.raises(InvalidTicket, match=message):
        check_pattern(parse_numbers(numbers))


def test_issued_ticket_redeems_for_25_exactly_once(store, mock):
    ticket = store.issue_mock_ticket(mock)
    assert ticket["ticket_id"].startswith("MOCK-") and "not a real lottery ticket" in ticket["notice"]

    result = store.redeem_lottery_ticket("w1", ticket["ticket_id"], ticket["numbers"], mock)
    assert result["points"] == 25 and result["balance"] == 75
    assert store.get_ledger("w1")[0]["reference"] == ticket["ticket_id"]

    with pytest.raises(TicketAlreadyUsed):
        store.redeem_lottery_ticket("w2", ticket["ticket_id"], ticket["numbers"], mock)
    assert store.balance("w2") == 50


def test_same_numbers_on_different_tickets_both_count(store, mock):
    a = store.issue_mock_ticket(mock, "01 02 04 08 16")
    b = store.issue_mock_ticket(mock, "01 02 04 08 16")
    assert a["ticket_id"] != b["ticket_id"]
    store.redeem_lottery_ticket("w1", a["ticket_id"], a["numbers"], mock)
    store.redeem_lottery_ticket("w1", b["ticket_id"], b["numbers"], mock)
    assert store.balance("w1") == 100


def test_server_checks_the_pattern_and_the_printed_numbers(store, mock):
    bad = store.issue_mock_ticket(mock, "01 02 04 08 17")   # mis-printed ticket
    with pytest.raises(InvalidTicket, match="double"):
        store.redeem_lottery_ticket("w1", bad["ticket_id"], bad["numbers"], mock)

    good = store.issue_mock_ticket(mock, "02 04 08 16 32")
    with pytest.raises(InvalidTicket, match="do not match"):
        store.redeem_lottery_ticket("w1", good["ticket_id"], "01 02 04 08 16", mock)
    with pytest.raises(InvalidTicket, match="Unknown ticket"):
        store.redeem_lottery_ticket("w1", "MOCK-0000-0000-0000", "01 02 04 08 16", mock)
    assert store.balance("w1") == 50


def test_three_tickets_per_wallet_per_day(store, mock):
    tickets = [store.issue_mock_ticket(mock) for _ in range(5)]
    for t in tickets[:3]:
        store.redeem_lottery_ticket("w1", t["ticket_id"], t["numbers"], mock)
    with pytest.raises(LimitReached):
        store.redeem_lottery_ticket("w1", tickets[3]["ticket_id"], tickets[3]["numbers"], mock)

    store.redeem_lottery_ticket("w2", tickets[3]["ticket_id"], tickets[3]["numbers"], mock)  # per wallet
    DAY[0] = "2026-10-21"
    store.redeem_lottery_ticket("w1", tickets[4]["ticket_id"], tickets[4]["numbers"], mock)
    assert store.balance("w1") == 50 + 4 * 25
    assert store.audit() == []


def test_lottery_off_without_demo_mode(store, mock):
    with pytest.raises(FeatureDisabled):
        store.issue_mock_ticket(None)
    ticket = store.issue_mock_ticket(mock)
    with pytest.raises(FeatureDisabled):
        store.redeem_lottery_ticket("w1", ticket["ticket_id"], ticket["numbers"], None)


def test_validator_is_replaceable(store):
    class AlwaysNo:
        name = "always-no"

        def validate(self, ticket_id, numbers, conn=None):
            raise InvalidTicket("nope")

    with pytest.raises(InvalidTicket, match="nope"):
        store.redeem_lottery_ticket("w1", "MOCK-0000-0000-0000", "01 02 04 08 16", AlwaysNo())


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
    ticket = store.issue_mock_ticket(mock)
    wallets = [f"racer{i}" for i in range(12)]
    for w in wallets:
        store.ensure_wallet(w, "phone")
    results = race(12, lambda i: store.redeem_lottery_ticket(wallets[i], ticket["ticket_id"], ticket["numbers"], mock))
    assert sum(r[0] == "ok" for r in results) == 1
    assert sum(store.balance(w) for w in wallets) == 12 * 50 + 25


def test_concurrent_tickets_respect_the_daily_cap(store, mock):
    tickets = [store.issue_mock_ticket(mock) for _ in range(10)]
    results = race(10, lambda i: store.redeem_lottery_ticket("w1", tickets[i]["ticket_id"], tickets[i]["numbers"], mock))
    assert sum(r[0] == "ok" for r in results) == 3
    assert store.balance("w1") == 50 + 3 * 25


def test_concurrent_same_key_purchases_charge_once(store):
    results = race(10, lambda i: store.purchase("w1", "topup_5", FakePaymentProvider(), "race-key-001"))
    assert all(r[0] == "ok" for r in results)
    assert store.balance("w1") == 90
    assert len(store.get_receipts("w1")) == 1
    assert store.audit() == []
