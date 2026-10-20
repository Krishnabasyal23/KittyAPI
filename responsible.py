"""Responsible-gambling rules (Phase 3 UI edits these through /api/wallet/... endpoints).

WalletStore asks its policy before every wager and every purchase, inside the same transaction
that would move the points, so a blocked request changes nothing. Settings live in rg_settings
(migrations 0004, 0006); a missing row or NULL field means "not set". Replace DefaultPolicy to
change the rules without touching the wallet code.

Player-facing rules (enforced in WalletStore, not just in the UI):
  * 19+ self-attestation before any wager or purchase;
  * daily spend limit: lowering applies at once, raising or removing waits LIMIT_INCREASE_DELAY;
  * cool-off and self-exclusion can be extended but never shortened or cancelled;
  * reality-check interval: the clients remind the player every N minutes (default 15).
"""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Protocol

from errors import PlayBlocked

MIN_AGE = 19
COOL_OFF_HOURS = (1, 24, 168)
SELF_EXCLUSION_DAYS = (30, 180, 365)
REALITY_CHECK_MINUTES = (15, 30, 60)
DEFAULT_REALITY_CHECK_MINUTES = 15
LIMIT_INCREASE_DELAY = timedelta(hours=24)
MAX_DAILY_LIMIT = 100_000


@dataclass(frozen=True)
class RGSettings:
    daily_spend_limit_points: int | None = None
    cool_off_until: str | None = None        # ISO-8601 UTC
    self_excluded_until: str | None = None   # ISO-8601 UTC
    reality_check_minutes: int | None = None
    age_confirmed_at: str | None = None
    pending_daily_spend_limit_points: int | None = None
    pending_limit_effective_at: str | None = None


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    moment = datetime.fromisoformat(value)
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def effective(settings: RGSettings, now: datetime) -> RGSettings:
    """Apply a pending limit change whose waiting time is over."""
    due = parse_time(settings.pending_limit_effective_at)
    if due is None or now < due:
        return settings
    return RGSettings(
        daily_spend_limit_points=settings.pending_daily_spend_limit_points,
        cool_off_until=settings.cool_off_until,
        self_excluded_until=settings.self_excluded_until,
        reality_check_minutes=settings.reality_check_minutes,
        age_confirmed_at=settings.age_confirmed_at,
    )


class WalletPolicy(Protocol):
    def check_wager(self, settings: RGSettings, spent_today: int, points: int, now: datetime) -> None:
        """Raise PlayBlocked to refuse a wager of `points` (spent_today = net wagers today)."""

    def check_purchase(self, settings: RGSettings, now: datetime) -> None:
        """Raise PlayBlocked to refuse a (simulated) purchase."""


class DefaultPolicy:
    """19+ gate, self-exclusion and cool-off block wagers and purchases; the daily limit caps net
    wagers. Settings arrive already passed through effective()."""

    def _allowed(self, settings: RGSettings, now: datetime) -> None:
        if not settings.age_confirmed_at:
            raise PlayBlocked(f"Confirm you are {MIN_AGE} or older to play")
        excluded = parse_time(settings.self_excluded_until)
        if excluded and now < excluded:
            raise PlayBlocked(f"Self-excluded until {settings.self_excluded_until}")
        cool = parse_time(settings.cool_off_until)
        if cool and now < cool:
            raise PlayBlocked(f"Cooling off until {settings.cool_off_until}")

    def check_wager(self, settings: RGSettings, spent_today: int, points: int, now: datetime) -> None:
        self._allowed(settings, now)
        limit = settings.daily_spend_limit_points
        if limit is not None and spent_today + points > limit:
            raise PlayBlocked(f"Daily spend limit of {limit} points reached ({spent_today} spent today)")

    def check_purchase(self, settings: RGSettings, now: datetime) -> None:
        self._allowed(settings, now)
