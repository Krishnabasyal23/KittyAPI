"""Responsible-gambling hooks (backend only; the Phase 3 UI will let players set these).

WalletStore asks its policy before every wager and every purchase, inside the same transaction
that would move the points, so a blocked request changes nothing. Settings live in rg_settings
(migration 0004); a missing row or NULL field means "not set". Replace DefaultPolicy to change
the rules without touching the wallet code.
"""

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Protocol

from errors import PlayBlocked


@dataclass(frozen=True)
class RGSettings:
    daily_spend_limit_points: int | None = None
    cool_off_until: str | None = None        # ISO-8601 UTC
    self_excluded_until: str | None = None   # ISO-8601 UTC
    reality_check_minutes: int | None = None
    age_confirmed_at: str | None = None


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _until(value: str | None) -> datetime | None:
    if not value:
        return None
    moment = datetime.fromisoformat(value)
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


class WalletPolicy(Protocol):
    def check_wager(self, settings: RGSettings, spent_today: int, points: int, now: datetime) -> None:
        """Raise PlayBlocked to refuse a wager of `points` (spent_today = net wagers today)."""

    def check_purchase(self, settings: RGSettings, now: datetime) -> None:
        """Raise PlayBlocked to refuse a (simulated) purchase."""


class DefaultPolicy:
    """Self-exclusion and cool-off block wagers and purchases; the daily limit caps net wagers.
    Reality checks and the 19+ gate are client prompts (Phase 3); the server only stores them."""

    def _paused(self, settings: RGSettings, now: datetime) -> None:
        excluded = _until(settings.self_excluded_until)
        if excluded and now < excluded:
            raise PlayBlocked(f"Self-excluded until {settings.self_excluded_until}")
        cool = _until(settings.cool_off_until)
        if cool and now < cool:
            raise PlayBlocked(f"Cooling off until {settings.cool_off_until}")

    def check_wager(self, settings: RGSettings, spent_today: int, points: int, now: datetime) -> None:
        self._paused(settings, now)
        limit = settings.daily_spend_limit_points
        if limit is not None and spent_today + points > limit:
            raise PlayBlocked(f"Daily spend limit of {limit} points reached ({spent_today} spent today)")

    def check_purchase(self, settings: RGSettings, now: datetime) -> None:
        self._paused(settings, now)
