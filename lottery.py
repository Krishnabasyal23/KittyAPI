"""Ticket bonus: a player types the code from a demo ticket and gets 25 points, once a day.

Whether a code is genuine depends only on a secret key (TICKET_SECRET, kept out of git), never on
anything a player could work out from this file. Without the key, ticket entry is switched off.
Each code redeems once, globally (lottery_tickets primary key, in the same transaction as the credit).

Players only ever see one message for a code that doesn't verify, whatever the reason.
These are demo tickets only. Nothing here checks, or claims to check, real lottery tickets.
"""

import hashlib
import hmac
import os
import secrets

from dataclasses import dataclass
from typing import Protocol

from errors import InvalidTicket

TICKET_POINTS = 25
TICKET_DAILY_MAX = 1
TICKET_MAX_FAILURES = 3          # wrong codes in a row before entry locks
TICKET_LOCK_MINUTES = 60
NOT_VERIFIED = "That ticket couldn't be verified"
TICKET_NOTICE = "Demo ticket: not a real lottery ticket"

_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
_SERIAL, _CHECK = 10, 10


def ticket_secret() -> bytes | None:
    key = os.getenv("TICKET_SECRET", "").strip()
    return key.encode("utf-8") if len(key) >= 16 else None


def normalise(code) -> str:
    return "".join(ch for ch in str(code or "").upper() if ch.isalnum())[:64]


def _check(key: bytes, serial: str) -> str:
    digest = hmac.new(key, b"ticket:" + serial.encode("ascii"), hashlib.sha256).digest()
    number = int.from_bytes(digest[:8], "big")
    return "".join(_ALPHABET[(number >> (5 * i)) & 31] for i in range(_CHECK))


@dataclass(frozen=True)
class TicketCheck:
    ticket_id: str           # the normalised code, stored once redeemed


class LotteryValidator(Protocol):
    name: str

    def validate(self, code: str, conn=None) -> TicketCheck:
        """Raise InvalidTicket(NOT_VERIFIED) unless the code is genuine."""


class SignedTicketValidator:
    name = "signed"

    def __init__(self, key: bytes):
        self.key = key

    def validate(self, code: str, conn=None) -> TicketCheck:
        text = normalise(code)
        if len(text) != _SERIAL + _CHECK:
            raise InvalidTicket(NOT_VERIFIED)
        serial, check = text[:_SERIAL], text[_SERIAL:]
        if any(ch not in _ALPHABET for ch in text) or not hmac.compare_digest(check, _check(self.key, serial)):
            raise InvalidTicket(NOT_VERIFIED)
        return TicketCheck(text)

    def issue(self) -> str:
        """A new genuine code (development helper, DEMO_MODE only)."""
        serial = "".join(secrets.choice(_ALPHABET) for _ in range(_SERIAL))
        text = serial + _check(self.key, serial)
        return "-".join(text[i:i + 5] for i in range(0, len(text), 5))


def lottery_validator() -> LotteryValidator | None:
    """The active validator, or None when no TICKET_SECRET is configured."""
    key = ticket_secret()
    return SignedTicketValidator(key) if key else None


if __name__ == "__main__":       # python lottery.py [count]   (reads TICKET_SECRET; local use only)
    import sys

    from local_env import load_local_env

    load_local_env()
    validator = lottery_validator()
    if validator is None:
        sys.exit("Set TICKET_SECRET (16+ characters) first")
    for _ in range(int(sys.argv[1]) if len(sys.argv) > 1 else 5):
        print(validator.issue())
