from decimal import Decimal

import pytest

from economy import (
    INITIAL_BALANCE,
    calculate_cashout,
    calculate_profit,
    credit_winnings,
    place_bet,
    validate_points,
)
from round_manager import RoundManager


def test_starting_balance_is_50_points():
    assert INITIAL_BALANCE == 50


def test_place_bet():
    assert place_bet(50, 10) == 40


def test_cashout_whole_points():
    assert calculate_cashout(10, 2.5) == 25


@pytest.mark.parametrize("wager, multiplier, expected", [
    (10, 1.27, 13),     # 12.70 -> 13
    (3, 1.50, 4),       # 4.50 -> 4  (half to even)
    (5, 1.50, 8),       # 7.50 -> 8  (half to even)
    (1, 1.01, 1),       # 1.01 -> 1  (never below the wager: multiplier >= 1)
    (7, Decimal("2.13"), 15),  # 14.91 -> 15
])
def test_payout_rounding_is_half_even(wager, multiplier, expected):
    assert calculate_cashout(wager, multiplier) == expected


@pytest.mark.parametrize("value, points", [(10, 10), ("10", 10), ("10.00", 10), (Decimal("7"), 7), (0, 0)])
def test_validate_points_accepts_whole_numbers(value, points):
    assert validate_points(value) == points


@pytest.mark.parametrize("value", ["10.5", Decimal("0.01"), -1, "abc", "NaN", 1.0, True])
def test_validate_points_rejects_everything_else(value):
    with pytest.raises((ValueError, TypeError)):
        validate_points(value)


def test_winning_round():
    balance = place_bet(50, 10)
    balance = credit_winnings(balance, calculate_cashout(10, 2.5))
    assert balance == 65


def test_crashed_round():
    assert place_bet(50, 10) == 40
    assert calculate_profit(10, 0) == -10


def test_insufficient_balance():
    with pytest.raises(ValueError):
        place_bet(5, 10)


def test_cashout_at_five_seconds(monkeypatch):
    monkeypatch.setattr("round_manager.draw_crash_point", lambda mode: (3.0, None))
    clock = [100.0]
    manager = RoundManager(clock=lambda: clock[0])
    session_id = manager.create_session()["session_id"]
    round_id = manager.start_round(session_id, Decimal("10"))["round_id"]

    clock[0] += 5.0
    result = manager.cash_out(session_id, round_id)
    assert result["status"] == "cashed_out"
    assert result["multiplier"] == 1.27
    assert result["payout"] == "13"
    assert result["balance"] == "53"


def test_cashout_after_ten_seconds(monkeypatch):
    monkeypatch.setattr("round_manager.draw_crash_point", lambda mode: (3.0, None))
    clock = [100.0]
    manager = RoundManager(clock=lambda: clock[0])
    session_id = manager.create_session()["session_id"]
    round_id = manager.start_round(session_id, Decimal("10"))["round_id"]

    clock[0] += 15.0
    result = manager.cash_out(session_id, round_id)
    assert result["status"] == "crashed"
    assert result["payout"] == "0"
    assert result["balance"] == "40"


def test_fractional_wager_is_refused():
    manager = RoundManager()
    session_id = manager.create_session()["session_id"]
    with pytest.raises(ValueError, match="whole number"):
        manager.start_round(session_id, Decimal("2.50"))
    assert manager.get_balance(session_id)["balance"] == "50"
