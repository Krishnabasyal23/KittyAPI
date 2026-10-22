"""Simulated payments. No payment network is ever contacted.

Purchases are only possible when the server runs with DEMO_MODE=1 (server-side switch; the
client cannot turn it on). FakePaymentProvider approves (or, for tests, declines) instantly and
returns a fake payment id. Every receipt is labelled as simulated. A real provider would implement
the same PaymentProvider protocol, with a pending -> confirmed flow instead of an instant answer.
"""

import os
import uuid

from dataclasses import dataclass
from typing import Protocol

SIMULATED_NOTICE = "SIMULATED PAYMENT (demo): no money was charged"


def demo_mode() -> bool:
    """Simulated purchases. On a public server (PUBLIC_MODE=1) they also need ALLOW_PUBLIC_DEMO=1."""
    if os.getenv("DEMO_MODE", "").strip() != "1":
        return False
    return os.getenv("PUBLIC_MODE", "").strip() != "1" or os.getenv("ALLOW_PUBLIC_DEMO", "").strip() == "1"


@dataclass(frozen=True)
class Product:
    product_id: str
    label: str
    price_cents: int
    points: int
    ledger_kind: str
    once_per_day: bool


# Top-ups: any whole-dollar amount, $1 = 8 points, $1-$100 each, at most $200 of top-ups per wallet
# per server day (wallet and cashier together).
POINTS_PER_DOLLAR = 8
TOPUP_MIN_DOLLARS = 1
TOPUP_MAX_DOLLARS = 100
TOPUP_DAILY_CAP_DOLLARS = 200
TOPUP_PRODUCTS = ("topup", "topup_5")


def topup_product(dollars) -> Product:
    """The top-up for a whole-dollar amount (raises ValueError outside $1-$100)."""
    if isinstance(dollars, bool) or not isinstance(dollars, int):
        raise ValueError("Top-up amount must be a whole number of dollars")
    if not TOPUP_MIN_DOLLARS <= dollars <= TOPUP_MAX_DOLLARS:
        raise ValueError(f"Top-up amount must be ${TOPUP_MIN_DOLLARS}-${TOPUP_MAX_DOLLARS}")
    return Product("topup", f"${dollars} demo top-up", dollars * 100, dollars * POINTS_PER_DOLLAR, "topup", False)


PRODUCTS = {
    "topup_5": Product("topup_5", "$5 demo top-up", 500, 40, "topup", False),   # older clients: $5 = 40
    "parking_5": Product("parking_5", "$5 demo parking bundle", 500, 60, "parking_bundle", True),
}


def product_label(product_id: str, amount_cents: int) -> str:
    if product_id == "topup":
        return f"${amount_cents // 100} demo top-up"
    return PRODUCTS[product_id].label if product_id in PRODUCTS else product_id


@dataclass(frozen=True)
class PaymentResult:
    payment_id: str
    status: str          # 'succeeded' | 'declined'
    provider: str


class PaymentProvider(Protocol):
    name: str

    def charge(self, wallet_id: str, product: Product, idempotency_key: str) -> PaymentResult:
        ...


class FakePaymentProvider:
    """Approves every charge instantly (decline=True for tests). No network, no card data."""

    name = "fake"

    def __init__(self, decline: bool = False):
        self.decline = decline

    def charge(self, wallet_id: str, product: Product, idempotency_key: str) -> PaymentResult:
        return PaymentResult(
            payment_id="fakepay_" + uuid.uuid4().hex,
            status="declined" if self.decline else "succeeded",
            provider=self.name,
        )


class CashierProvider(FakePaymentProvider):
    """A simulated counter payment: the cashier page records that the player paid at the counter."""

    name = "cashier"

    def charge(self, wallet_id: str, product: Product, idempotency_key: str) -> PaymentResult:
        return PaymentResult(payment_id="counter_" + uuid.uuid4().hex, status="succeeded", provider=self.name)


def payment_provider() -> PaymentProvider | None:
    """The active provider, or None when simulated payments are switched off."""
    return FakePaymentProvider() if demo_mode() else None
