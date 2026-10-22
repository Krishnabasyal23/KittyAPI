"""Wallet errors with the HTTP status the API should answer with."""


class WalletError(ValueError):
    status = 400


class InsufficientPoints(WalletError):
    status = 400


class AlreadyClaimed(WalletError):
    status = 409


class LimitReached(WalletError):
    status = 409


class InvalidTicket(WalletError):
    status = 400


class TicketAlreadyUsed(WalletError):
    status = 409


class PaymentDeclined(WalletError):
    status = 402


class FeatureDisabled(WalletError):
    status = 503


class IdempotencyConflict(WalletError):
    status = 422


class PlayBlocked(WalletError):
    """Responsible-gambling settings (self-exclusion, cool-off, spend limit) forbid this."""
    status = 403


class TicketLocked(WalletError):
    """Too many wrong ticket codes in a row: entry is paused for a while."""
    status = 429
