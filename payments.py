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
    return os.getenv("DEMO_MODE", "").strip() == "1"


@dataclass(frozen=True)
class Product:
    product_id: str
    label: str
    price_cents: int
    points: int
    ledger_kind: str
    once_per_day: bool


PRODUCTS = {
    "topup_5": Product("topup_5", "$5 demo top-up", 500, 40, "topup", False),
    "parking_5": Product("parking_5", "$5 demo parking bundle", 500, 60, "parking_bundle", True),
}


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


def payment_provider() -> PaymentProvider | None:
    """The active provider, or None when simulated payments are switched off."""
    return FakePaymentProvider() if demo_mode() else None
