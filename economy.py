"""Points economy.

Wallet balances, wagers and payouts are whole points (int). Multipliers are Decimal, and a payout is
wager x multiplier rounded to whole points with ROUND_HALF_EVEN (banker's rounding): unbiased, so
rounding neither adds to nor takes from the RTP on average. Multipliers reach this module already
floored to 2 decimals by the round logic, e.g. 10 x 1.27 = 12.70 -> 13 and 5 x 1.50 = 7.50 -> 8.
"""

from decimal import Decimal, ROUND_HALF_EVEN

INITIAL_BALANCE = 50          # starting grant, once per wallet
MIN_BET = 1
MAX_BET = 500
PAYOUT_ROUNDING = ROUND_HALF_EVEN
ONE_POINT = Decimal("1")


def validate_points(amount) -> int:
    """A non-negative whole number of points. Accepts int, Decimal or numeric text ('10', '10.00')."""
    if isinstance(amount, bool):
        raise TypeError("Points must be a number")

    if isinstance(amount, int):
        value = Decimal(amount)
    elif isinstance(amount, (Decimal, str)):
        try:
            value = Decimal(str(amount).strip())
        except ArithmeticError as exc:
            raise ValueError("Points must be a number") from exc
    else:
        raise TypeError("Points must be int, Decimal or text")

    if not value.is_finite():
        raise ValueError("Points must be finite")
    if value < 0:
        raise ValueError("Points cannot be negative")
    if value != value.to_integral_value():
        raise ValueError("Points must be a whole number")
    return int(value)


def calculate_cashout(wager: int, multiplier: float | Decimal) -> int:
    wager = validate_points(wager)
    if wager <= 0:
        raise ValueError("Wager must be positive")

    m = Decimal(str(multiplier))

    if not m.is_finite() or m < 1:
        raise ValueError("Invalid multiplier")

    return int((Decimal(wager) * m).quantize(ONE_POINT, rounding=PAYOUT_ROUNDING))


def place_bet(balance: int, wager: int) -> int:
    balance = validate_points(balance)
    wager = validate_points(wager)
    if not MIN_BET <= wager <= MAX_BET:
        raise ValueError("Wager outside betting limits")

    if wager > balance:
        raise ValueError("Insufficient points")

    return balance - wager


def credit_winnings(balance: int, payout: int) -> int:
    return validate_points(balance) + validate_points(payout)


def calculate_profit(wager: int, payout: int) -> int:
    wager = validate_points(wager)
    payout = validate_points(payout)
    if wager <= 0:
        raise ValueError("Wager must be positive")
    return payout - wager
