from decimal import Decimal
from economy import place_bet, calculate_cashout, credit_winnings, calculate_profit
import pytest
from round_manager import RoundManager

def test_place_bet():
    balance = place_bet(Decimal("1000.00"), Decimal("100.00"))
    assert balance == Decimal("900.00")

def test_cashout():
    payout = calculate_cashout(Decimal("100.00"), 2.5)
    assert payout == Decimal("250.00")

def test_winning_round():
    balance = Decimal("1000.00")
    wager = Decimal("100.00")
    balance = place_bet(balance, wager)
    payout = calculate_cashout(wager, 2.5)
    balance = credit_winnings(balance, payout)
    assert balance == Decimal("1150.00")

def test_crashed_round():
    balance = Decimal("1000.00")
    wager = Decimal("100.00")
    balance = place_bet(balance, wager)
    assert balance == Decimal("900.00")
    profit = calculate_profit(wager, Decimal("0.00"))
    assert profit == Decimal("-100.00")

def test_insufficient_balance():
    with pytest.raises(ValueError):
        place_bet(Decimal("100.00"), Decimal("200.00"))

def test_cashout_at_five_seconds(monkeypatch):
    monkeypatch.setattr("round_manager.draw_crash_point", lambda mode: (3.0, None))
    clock = [100.0]
    manager = RoundManager(clock=lambda: clock[0])
    session_id = manager.create_session()["session_id"]
    round_id = manager.start_round(
        session_id,
        Decimal("100.00")
    )["round_id"]

    # Advance time by 5 seconds
    clock[0] += 5.0
    result = manager.cash_out(session_id, round_id)
    assert result["status"] == "cashed_out"
    assert result["multiplier"] == 1.27
    assert result["payout"] == "127.00"
    assert result["balance"] == "1027.00"

def test_cashout_after_ten_seconds(monkeypatch):
    monkeypatch.setattr(
        "round_manager.draw_crash_point",
        lambda mode: (3.0, None)
    )

    clock = [100.0]
    manager = RoundManager(clock=lambda: clock[0])

    session_id = manager.create_session()["session_id"]

    round_id = manager.start_round(
        session_id,
        Decimal("100.00")
    )["round_id"]

    # Advance time by 10 seconds
    clock[0] += 15.0

    result = manager.cash_out(session_id, round_id)

    assert result["status"] == "crashed"
    assert result["payout"] == "0.00"
    assert result["balance"] == "900.00"