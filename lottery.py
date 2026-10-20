"""Mock lottery tickets: a replaceable validator plus a server-side issuer for demos and tests.

A ticket is a server-issued ticket_id and five two-digit numbers, each 01-99, each exactly double
the previous one (e.g. 01 02 04 08 16). The pattern is always checked here on the server. The same
numbers may be printed on many tickets; each ticket_id can be redeemed once, globally (enforced by
the lottery_tickets primary key in the same transaction as the 25-point credit).

These are mock tickets only. Nothing here checks, or claims to check, real lottery tickets.
"""

import re
import secrets

from dataclasses import dataclass
from typing import Protocol

from errors import InvalidTicket

TICKET_POINTS = 25
TICKET_DAILY_MAX = 3
MOCK_NOTICE = "MOCK TICKET (demo): not a real lottery ticket"
TICKET_ID = re.compile(r"^MOCK-[0-9A-F]{4}-[0-9A-F]{4}-[0-9A-F]{4}$")


def parse_numbers(numbers) -> tuple[int, ...]:
    """Five two-digit numbers from '01 02 04 08 16', '01-02-04-08-16' or a list of 2-char strings."""
    parts = re.split(r"[\s,\-]+", numbers.strip()) if isinstance(numbers, str) else list(numbers)
    if len(parts) != 5:
        raise InvalidTicket("A ticket has exactly five numbers")
    values = []
    for part in parts:
        text = str(part)
        if not re.fullmatch(r"\d{2}", text):
            raise InvalidTicket("Each number has exactly two digits (01-99)")
        values.append(int(text))
    return tuple(values)


def check_pattern(values: tuple[int, ...]) -> None:
    if any(not 1 <= v <= 99 for v in values):
        raise InvalidTicket("Each number must be between 01 and 99")
    for previous, current in zip(values, values[1:]):
        if current != previous * 2:
            raise InvalidTicket("Each number must be exactly double the previous one")


def format_numbers(values) -> str:
    return " ".join(f"{v:02d}" for v in values)


@dataclass(frozen=True)
class TicketCheck:
    ticket_id: str
    numbers: str          # normalised, e.g. '01 02 04 08 16'


class LotteryValidator(Protocol):
    name: str

    def validate(self, ticket_id: str, numbers, conn=None) -> TicketCheck:
        """Raise InvalidTicket unless the ticket exists and carries these numbers in a valid pattern."""


class MockLotteryValidator:
    """Accepts only tickets this server issued (mock_lottery_tickets), with matching numbers."""

    name = "mock"

    def validate(self, ticket_id: str, numbers, conn=None) -> TicketCheck:
        ticket_id = (ticket_id or "").strip().upper()
        if not TICKET_ID.fullmatch(ticket_id):
            raise InvalidTicket("Unknown ticket id")
        values = parse_numbers(numbers)
        check_pattern(values)          # server-side, whatever the issuer printed

        row = conn.execute(
            "SELECT numbers FROM mock_lottery_tickets WHERE ticket_id = ?", (ticket_id,)
        ).fetchone()
        if row is None:
            raise InvalidTicket("Unknown ticket id")
        if row["numbers"] != format_numbers(values):
            raise InvalidTicket("Numbers do not match this ticket")
        return TicketCheck(ticket_id, format_numbers(values))

    def issue(self, conn, numbers=None) -> dict:
        """Print a mock ticket. Default: a random valid sequence. Tests may pass any 5 two-digit
        numbers (even an invalid pattern) to prove the server rejects it at redemption."""
        if numbers is None:
            start = secrets.randbelow(6) + 1           # 16 x start must stay <= 99
            values = tuple(start * 2 ** i for i in range(5))
        else:
            values = parse_numbers(numbers)
        ticket_id = "MOCK-" + "-".join(secrets.token_hex(2).upper() for _ in range(3))
        conn.execute(
            "INSERT INTO mock_lottery_tickets(ticket_id, numbers) VALUES (?, ?)",
            (ticket_id, format_numbers(values)),
        )
        return {"ticket_id": ticket_id, "numbers": format_numbers(values), "notice": MOCK_NOTICE}


def lottery_validator(demo: bool) -> LotteryValidator | None:
    """The active validator, or None when mock tickets are switched off (DEMO_MODE unset)."""
    return MockLotteryValidator() if demo else None
