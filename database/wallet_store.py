"""Server-authoritative wallets for Catino v2 (SQLite, see database/migrations).

Rules every write follows:
  * one short transaction opened with BEGIN IMMEDIATE, so concurrent writers queue instead of racing;
  * a balance only changes through _post(), which updates the wallet with a guarded UPDATE
    (balance + amount >= 0) and appends the matching ledger row (amount, balance_after) in the
    same transaction: both happen or neither does;
  * the ledger is append-only (database triggers), and once-only rules are unique indexes, so a
    bug or a retried request cannot pay twice.
"""

import json
import re
import secrets

from datetime import date, timedelta
from decimal import Decimal
from hashlib import sha256
from math import ceil, isfinite
from time import time as wall_clock

from fairness import proof_to_columns
from economy import INITIAL_BALANCE, MAX_BET, MIN_BET, calculate_cashout, validate_points
from errors import (
    AlreadyClaimed,
    FeatureDisabled,
    IdempotencyConflict,
    InsufficientPoints,
    LimitReached,
    InvalidTicket,
    TicketAlreadyUsed,
    TicketLocked,
    WalletError,
)
from lottery import (
    NOT_VERIFIED,
    TICKET_DAILY_MAX,
    TICKET_LOCK_MINUTES,
    TICKET_MAX_FAILURES,
    TICKET_NOTICE,
    TICKET_POINTS,
    LotteryValidator,
    normalise as normalise_ticket,
)
from payments import (
    POINTS_PER_DOLLAR,
    PRODUCTS,
    SIMULATED_NOTICE,
    TOPUP_DAILY_CAP_DOLLARS,
    TOPUP_MAX_DOLLARS,
    TOPUP_MIN_DOLLARS,
    TOPUP_PRODUCTS,
    PaymentProvider,
    Product,
    product_label,
    topup_product,
)
import identity
from responsible import (
    COOL_OFF_HOURS,
    DEFAULT_REALITY_CHECK_MINUTES,
    LIMIT_INCREASE_DELAY,
    MAX_DAILY_LIMIT,
    MIN_AGE,
    REALITY_CHECK_MINUTES,
    SELF_EXCLUSION_DAYS,
    DefaultPolicy,
    RGSettings,
    WalletPolicy,
    effective,
    parse_time,
    utc_now,
)

from .connection import get_connection


DAILY_FREE_POINTS = 20
AVATARS = ("ginger", "black", "white", "grey", "calico", "tabby")
NICKNAME = re.compile(r"^[A-Za-z0-9 _\-]+$")
IDEMPOTENCY_KEY = re.compile(r"^[A-Za-z0-9_.:\-]{8,100}$")


def server_day() -> str:
    """The server's calendar day (local time), which daily rewards and limits use."""
    return date.today().isoformat()


RG_FIELDS = ("daily_spend_limit_points", "cool_off_until", "self_excluded_until",
             "reality_check_minutes", "age_confirmed_at",
             "pending_daily_spend_limit_points", "pending_limit_effective_at")


class WalletStore:
    def __init__(self, db_path=None, today=server_day, now=utc_now, policy: WalletPolicy | None = None):
        self.db_path = db_path
        self.today = today          # server calendar day for daily rewards / limits
        self.now = now              # UTC datetime for cool-off / self-exclusion
        self.policy = policy or DefaultPolicy()

    def _connect(self):
        return get_connection(self.db_path)

    # LEDGER CORE

    def _post(self, conn, wallet_id: str, kind: str, amount: int, *, round_id=None, bet_id=None,
              reference=None) -> tuple[int, int]:
        """Move `amount` points (negative = debit). Caller holds the transaction. Returns (entry_id, balance)."""
        if amount == 0:
            raise WalletError("A ledger entry must move points")

        cursor = conn.execute(
            """
            UPDATE wallets
            SET balance_points = balance_points + ?
            WHERE wallet_id = ? AND balance_points + ? >= 0
            """,
            (amount, wallet_id, amount),
        )
        if cursor.rowcount != 1:
            exists = conn.execute("SELECT 1 FROM wallets WHERE wallet_id = ?", (wallet_id,)).fetchone()
            if exists is None:
                raise KeyError("Wallet not found")
            raise InsufficientPoints("Not enough points")

        balance = conn.execute(
            "SELECT balance_points FROM wallets WHERE wallet_id = ?", (wallet_id,)
        ).fetchone()[0]
        entry = conn.execute(
            """
            INSERT INTO ledger(wallet_id, kind, amount_points, balance_after, day, round_id, bet_id, reference)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (wallet_id, kind, amount, balance, self.today(), round_id, bet_id, reference),
        )
        return entry.lastrowid, balance

    def _write(self):
        """Connection with an IMMEDIATE transaction already open (use as `with self._write() as conn`)."""
        return _Txn(self._connect())

    # RESPONSIBLE GAMBLING (policy hooks; settings are edited by the Phase 3 UI)

    def _rg(self, conn, wallet_id: str) -> RGSettings:
        """Settings in force now (a pending limit change whose 24 h are over counts as applied)."""
        row = conn.execute("SELECT * FROM rg_settings WHERE wallet_id = ?", (wallet_id,)).fetchone()
        stored = RGSettings(**{f: row[f] for f in RG_FIELDS}) if row else RGSettings()
        return effective(stored, self.now())

    def _spent_today(self, conn, wallet_id: str) -> int:
        """Net points wagered today (wagers minus refunds)."""
        return conn.execute(
            """
            SELECT COALESCE(-SUM(amount_points), 0) FROM ledger
            WHERE wallet_id = ? AND day = ? AND kind IN ('wager', 'refund')
            """,
            (wallet_id, self.today()),
        ).fetchone()[0]

    def _check_wager(self, conn, wallet_id: str, points: int) -> None:
        self.policy.check_wager(self._rg(conn, wallet_id), self._spent_today(conn, wallet_id), points, self.now())

    def _check_purchase(self, conn, wallet_id: str) -> None:
        self.policy.check_purchase(self._rg(conn, wallet_id), self.now())

    def get_rg_settings(self, wallet_id: str) -> RGSettings:
        conn = self._connect()
        try:
            return self._rg(conn, wallet_id)
        finally:
            conn.close()

    def set_rg_settings(self, wallet_id: str, **fields) -> RGSettings:
        """Low-level upsert with no rules (None clears a field): operators and tests only.
        Players go through confirm_age / set_spend_limit / start_cool_off / self_exclude."""
        unknown = set(fields) - set(RG_FIELDS)
        if unknown:
            raise ValueError(f"Unknown setting(s): {sorted(unknown)}")
        with self._write() as conn:
            return self._save_rg(conn, wallet_id, self._rg(conn, wallet_id).__dict__ | fields)

    def _save_rg(self, conn, wallet_id: str, current: dict) -> RGSettings:
        conn.execute(
            f"""
            INSERT INTO rg_settings(wallet_id, {', '.join(RG_FIELDS)}, updated_at)
            VALUES (?, {', '.join('?' for _ in RG_FIELDS)}, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
            ON CONFLICT(wallet_id) DO UPDATE SET
                {', '.join(f'{f} = excluded.{f}' for f in RG_FIELDS)},
                updated_at = excluded.updated_at
            """,
            (wallet_id, *(current[f] for f in RG_FIELDS)),
        )
        return self._rg(conn, wallet_id)

    # Player controls (the rules live here, not in the UI)

    def confirm_age(self, wallet_id: str) -> RGSettings:
        """Record the player's own 19+ confirmation (self-attested; no ID check). Kept from the first time."""
        with self._write() as conn:
            self._require_wallet(conn, wallet_id)
            current = self._rg(conn, wallet_id).__dict__
            if not current["age_confirmed_at"]:
                current["age_confirmed_at"] = self.now().isoformat()
                self._audit(conn, "player", "age_confirmed", wallet_id)
            return self._save_rg(conn, wallet_id, current)

    def set_spend_limit(self, wallet_id: str, points: int | None) -> RGSettings:
        """Lower (or set a first) daily limit at once; raise or remove it only after 24 hours."""
        if points is not None:
            points = validate_points(points)
            if not 1 <= points <= MAX_DAILY_LIMIT:
                raise WalletError(f"Daily limit must be 1-{MAX_DAILY_LIMIT} points")
        with self._write() as conn:
            self._require_wallet(conn, wallet_id)
            current = self._rg(conn, wallet_id).__dict__
            limit = current["daily_spend_limit_points"]
            if points is not None and (limit is None or points <= limit):
                current["daily_spend_limit_points"] = points          # stricter: now
                current["pending_daily_spend_limit_points"] = None
                current["pending_limit_effective_at"] = None
            elif points != limit:
                current["pending_daily_spend_limit_points"] = points  # looser: in 24 hours
                current["pending_limit_effective_at"] = (self.now() + LIMIT_INCREASE_DELAY).isoformat()
            else:
                current["pending_daily_spend_limit_points"] = None    # same as now: cancel any pending raise
                current["pending_limit_effective_at"] = None
            self._audit(conn, "player", "spend_limit_set", wallet_id, requested=points, was=limit,
                        now=current["daily_spend_limit_points"], pending_from=current["pending_limit_effective_at"])
            return self._save_rg(conn, wallet_id, current)

    def set_reality_check(self, wallet_id: str, minutes: int) -> RGSettings:
        if minutes not in REALITY_CHECK_MINUTES:
            raise WalletError(f"Reality check every {', '.join(map(str, REALITY_CHECK_MINUTES))} minutes")
        with self._write() as conn:
            self._require_wallet(conn, wallet_id)
            self._audit(conn, "player", "reality_check_set", wallet_id, minutes=minutes)
            return self._save_rg(conn, wallet_id, self._rg(conn, wallet_id).__dict__ | {"reality_check_minutes": minutes})

    def _extend_until(self, wallet_id: str, field: str, length: timedelta) -> RGSettings:
        with self._write() as conn:
            self._require_wallet(conn, wallet_id)
            current = self._rg(conn, wallet_id).__dict__
            until = self.now() + length
            existing = parse_time(current[field])
            if existing is None or existing < until:      # extend only, never shorten
                current[field] = until.isoformat()
            action = "cool_off_started" if field == "cool_off_until" else "self_excluded"
            self._audit(conn, "player", action, wallet_id, until=current[field], requested_hours=length.total_seconds() / 3600)
            return self._save_rg(conn, wallet_id, current)

    def start_cool_off(self, wallet_id: str, hours: int) -> RGSettings:
        if hours not in COOL_OFF_HOURS:
            raise WalletError(f"Cool-off lasts {', '.join(map(str, COOL_OFF_HOURS))} hours")
        return self._extend_until(wallet_id, "cool_off_until", timedelta(hours=hours))

    def self_exclude(self, wallet_id: str, days: int) -> RGSettings:
        if days not in SELF_EXCLUSION_DAYS:
            raise WalletError(f"Self-exclusion lasts {', '.join(map(str, SELF_EXCLUSION_DAYS))} days")
        return self._extend_until(wallet_id, "self_excluded_until", timedelta(days=days))

    def responsible_view(self, wallet_id: str) -> dict:
        """Everything the responsible-gambling screens show, with the allowed choices."""
        conn = self._connect()
        try:
            self._require_wallet(conn, wallet_id)
            rg = self._rg(conn, wallet_id)
            spent = self._spent_today(conn, wallet_id)
        finally:
            conn.close()
        now = self.now()
        cool, excluded = parse_time(rg.cool_off_until), parse_time(rg.self_excluded_until)
        limit = rg.daily_spend_limit_points
        return {
            "age_confirmed": bool(rg.age_confirmed_at),
            "min_age": MIN_AGE,
            "daily_spend_limit_points": limit,
            "spent_today": spent,
            "remaining_today": None if limit is None else max(0, limit - spent),
            "pending_limit": (
                {"daily_spend_limit_points": rg.pending_daily_spend_limit_points,
                 "effective_at": rg.pending_limit_effective_at}
                if rg.pending_limit_effective_at else None
            ),
            "reality_check_minutes": rg.reality_check_minutes or DEFAULT_REALITY_CHECK_MINUTES,
            "cooling_off_until": rg.cool_off_until if cool and now < cool else None,
            "self_excluded_until": rg.self_excluded_until if excluded and now < excluded else None,
            "can_play": bool(rg.age_confirmed_at) and not (cool and now < cool) and not (excluded and now < excluded),
            "options": {
                "cool_off_hours": list(COOL_OFF_HOURS),
                "self_exclusion_days": list(SELF_EXCLUSION_DAYS),
                "reality_check_minutes": list(REALITY_CHECK_MINUTES),
                "limit_increase_delay_hours": int(LIMIT_INCREASE_DELAY.total_seconds() // 3600),
            },
        }

    def activity(self, wallet_id: str, since: str) -> dict:
        """What happened since `since` (ISO-8601 UTC, e.g. the server_time the client saw when it
        started): for the reality check."""
        start = parse_time(since)
        if start is None:
            raise WalletError("since must be an ISO-8601 time")
        stamp = start.astimezone(utc_now().tzinfo).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
        conn = self._connect()
        try:
            self._require_wallet(conn, wallet_id)
            rows = conn.execute(
                """
                SELECT kind, COUNT(*) AS n, COALESCE(SUM(amount_points), 0) AS total
                FROM ledger WHERE wallet_id = ? AND created_at >= ?
                GROUP BY kind
                """,
                (wallet_id, stamp),
            ).fetchall()
        finally:
            conn.close()
        by = {r["kind"]: (r["n"], r["total"]) for r in rows}
        wagered = -by.get("wager", (0, 0))[1] - by.get("refund", (0, 0))[1]
        won = by.get("payout", (0, 0))[1]
        minutes = max(0, int((self.now() - start).total_seconds() // 60))
        return {
            "since": since,
            "minutes": minutes,
            "bets": by.get("wager", (0, 0))[0],
            "wagered": wagered,
            "won": won,
            "net": won - wagered,
            "server_time": self.now().isoformat(),
        }

    # WALLETS

    def ensure_wallet(self, wallet_id: str, kind: str = "pc", name: str | None = None) -> dict:
        """Create the wallet with its one-time starting grant, or return the existing one unchanged."""
        with self._write() as conn:
            row = conn.execute("SELECT * FROM wallets WHERE wallet_id = ?", (wallet_id,)).fetchone()
            if row is None:
                conn.execute(
                    "INSERT INTO wallets(wallet_id, kind, display_name) VALUES (?, ?, ?)",
                    (wallet_id, kind, name),
                )
                self._post(conn, wallet_id, "starting_grant", INITIAL_BALANCE)
                row = conn.execute("SELECT * FROM wallets WHERE wallet_id = ?", (wallet_id,)).fetchone()
            return dict(row)

    def get_wallet(self, wallet_id: str) -> dict | None:
        conn = self._connect()
        try:
            row = conn.execute("SELECT * FROM wallets WHERE wallet_id = ?", (wallet_id,)).fetchone()
        finally:
            conn.close()
        return None if row is None else dict(row)

    def balance(self, wallet_id: str) -> int:
        wallet = self.get_wallet(wallet_id)
        if wallet is None:
            raise KeyError("Wallet not found")
        return wallet["balance_points"]

    # RoundManager interface (a PC session is a 'pc' wallet)

    def create_session(self, session_id: str, balance=None):
        self.ensure_wallet(session_id, "pc")

    def get_session(self, session_id: str):
        wallet = self.get_wallet(session_id)
        if wallet is None:
            return None
        return {"session_id": session_id, "balance": wallet["balance_points"]}

    # HOST ROUNDS

    def get_round(self, session_id: str, round_id: str):
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT * FROM rounds WHERE wallet_id = ? AND round_id = ?", (session_id, round_id)
            ).fetchone()
        finally:
            conn.close()
        return None if row is None else dict(row)

    def record_wager(
        self,
        session_id: str,
        round_id: str,
        wager: int,
        crash_point: float,
        crash_time: float,
        distraction: str,
        season: str = "spring",
        growth_rate: float = 0.12,
        rush: bool = False,
        lead_in_seconds: float = 3.0,
        started_epoch: float | None = None,
        proof: dict | None = None,
    ) -> int:
        """Debit the wager and create the round (with its sealed fairness proof) in one transaction.
        Returns the new balance."""
        wager = validate_points(wager)
        if not MIN_BET <= wager <= MAX_BET:
            raise ValueError("Wager outside betting limits")
        if not isfinite(crash_point) or crash_point < 1.0:
            raise ValueError("Invalid crash point")
        if not isfinite(crash_time) or crash_time < 0:
            raise ValueError("Invalid crash time")

        with self._write() as conn:
            self._check_wager(conn, session_id, wager)
            conn.execute(
                """
                INSERT INTO rounds(round_id, wallet_id, wager_points, status, crash_point,
                                   crash_time_seconds, distraction, season, growth_rate, rush,
                                   lead_in_seconds, started_epoch)
                VALUES (?, ?, ?, 'running', ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (round_id, session_id, wager, crash_point, crash_time, distraction, season,
                 growth_rate, int(rush), lead_in_seconds,
                 started_epoch if started_epoch is not None else wall_clock()),
            )
            seal = proof_to_columns(proof)
            if seal:
                conn.execute(
                    f"UPDATE rounds SET {', '.join(f'{c} = ?' for c in seal)} WHERE round_id = ?",
                    (*seal.values(), round_id),
                )
            _, balance = self._post(conn, session_id, "wager", -wager, round_id=round_id)
            return balance

    def settle_round(
        self,
        session_id: str,
        round_id: str,
        status: str,
        payout: int = 0,
        multiplier: float | Decimal | None = None,
    ) -> int:
        """Close a running round exactly once; a cash-out credits the payout. Returns the new balance."""
        if status not in {"cashed_out", "crashed"}:
            raise ValueError("Invalid settlement status")
        payout = validate_points(payout)

        if status == "crashed":
            if payout or multiplier is not None:
                raise ValueError("Crashed round cannot pay winnings")
        elif multiplier is None or Decimal(str(multiplier)) < 1 or payout <= 0:
            raise ValueError("Invalid cash-out")

        with self._write() as conn:
            row = conn.execute(
                "SELECT status, wager_points FROM rounds WHERE wallet_id = ? AND round_id = ?",
                (session_id, round_id),
            ).fetchone()
            if row is None or row["status"] != "running":
                raise ValueError("Round already settled or not found")

            if status == "cashed_out" and payout != calculate_cashout(row["wager_points"], multiplier):
                raise ValueError("Payout does not match wager and multiplier")

            conn.execute(
                """
                UPDATE rounds
                SET status = ?, payout_points = ?, cashout_multiplier = ?,
                    finished_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                WHERE round_id = ? AND status = 'running'
                """,
                (status, payout, None if multiplier is None else str(Decimal(str(multiplier))), round_id),
            )
            if payout:
                _, balance = self._post(conn, session_id, "payout", payout, round_id=round_id)
            else:
                balance = conn.execute(
                    "SELECT balance_points FROM wallets WHERE wallet_id = ?", (session_id,)
                ).fetchone()[0]
            return balance

    # CROWD BETS (phones riding the big screen's walk)

    def place_crowd_bet(self, wallet_id: str, bet_id: str, wager: int) -> int:
        """Debit a phone's bet while betting is open. Returns the new balance."""
        wager = validate_points(wager)
        if not MIN_BET <= wager <= MAX_BET:
            raise ValueError("Wager outside betting limits")
        with self._write() as conn:
            self._check_wager(conn, wallet_id, wager)
            conn.execute(
                "INSERT INTO crowd_bets(bet_id, wallet_id, wager_points, status) VALUES (?, ?, ?, 'open')",
                (bet_id, wallet_id, wager),
            )
            _, balance = self._post(conn, wallet_id, "wager", -wager, bet_id=bet_id)
            return balance

    def attach_crowd_bets(self, bet_ids: list[str], round_id: str) -> int:
        """The host started the walk: open bets now ride round_id. Returns how many."""
        if not bet_ids:
            return 0
        with self._write() as conn:
            cursor = conn.execute(
                f"""
                UPDATE crowd_bets SET round_id = ?, status = 'running'
                WHERE status = 'open' AND bet_id IN ({', '.join('?' for _ in bet_ids)})
                """,
                (round_id, *bet_ids),
            )
            return cursor.rowcount

    def settle_crowd_cashout(self, bet_id: str, multiplier, payout: int) -> int:
        """Pay a phone that cashed out, exactly once. Returns the new balance."""
        payout = validate_points(payout)
        with self._write() as conn:
            bet = conn.execute(
                "SELECT wallet_id, wager_points, status FROM crowd_bets WHERE bet_id = ?", (bet_id,)
            ).fetchone()
            if bet is None or bet["status"] != "running":
                raise ValueError("Bet already settled or not riding")
            if payout != calculate_cashout(bet["wager_points"], multiplier):
                raise ValueError("Payout does not match wager and multiplier")
            conn.execute(
                """
                UPDATE crowd_bets SET status = 'cashed_out', payout_points = ?, cashout_multiplier = ?,
                                      settled_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                WHERE bet_id = ? AND status = 'running'
                """,
                (payout, str(Decimal(str(multiplier))), bet_id),
            )
            _, balance = self._post(conn, bet["wallet_id"], "payout", payout, bet_id=bet_id)
            return balance

    def settle_crowd_crash(self, round_id: str) -> int:
        """The cat got distracted: every bet still riding round_id is lost. Returns how many."""
        with self._write() as conn:
            cursor = conn.execute(
                """
                UPDATE crowd_bets SET status = 'crashed', settled_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                WHERE round_id = ? AND status = 'running'
                """,
                (round_id,),
            )
            return cursor.rowcount

    # SERVER RESTART RECOVERY

    def recover_interrupted_rounds(self) -> int:
        """Refund every wager still in play when the server stopped (startup only). Returns how many."""
        with self._write() as conn:
            rounds = conn.execute(
                "SELECT round_id, wallet_id, wager_points FROM rounds WHERE status = 'running'"
            ).fetchall()
            for r in rounds:
                conn.execute(
                    """
                    UPDATE rounds SET status = 'interrupted', finished_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                    WHERE round_id = ? AND status = 'running'
                    """,
                    (r["round_id"],),
                )
                self._post(conn, r["wallet_id"], "refund", r["wager_points"], round_id=r["round_id"])

            bets = conn.execute(
                "SELECT bet_id, wallet_id, wager_points FROM crowd_bets WHERE status IN ('open', 'running')"
            ).fetchall()
            for b in bets:
                conn.execute(
                    """
                    UPDATE crowd_bets SET status = 'refunded', settled_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                    WHERE bet_id = ? AND status IN ('open', 'running')
                    """,
                    (b["bet_id"],),
                )
                self._post(conn, b["wallet_id"], "refund", b["wager_points"], bet_id=b["bet_id"])

            return len(rounds) + len(bets)

    # HISTORY

    def get_history(self, session_id: str, limit: int = 30) -> list[dict]:
        conn = self._connect()
        try:
            rows = conn.execute(
                """
                SELECT round_id, status, wager_points, payout_points, cashout_multiplier, started_at, finished_at
                FROM rounds WHERE wallet_id = ?
                ORDER BY started_at DESC, rowid DESC
                LIMIT ?
                """,
                (session_id, limit),
            ).fetchall()
        finally:
            conn.close()

        return [
            {
                "round_id": r["round_id"],
                "status": r["status"],
                "wager": str(r["wager_points"]),
                "payout": str(r["payout_points"]),
                "net_profit": (
                    str(r["payout_points"] - r["wager_points"])
                    if r["status"] in {"crashed", "cashed_out"} else None
                ),
                "cashout_multiplier": float(r["cashout_multiplier"]) if r["cashout_multiplier"] else None,
                "started_at": r["started_at"],
                "finished_at": r["finished_at"],
            }
            for r in rows
        ]

    def get_ledger(self, wallet_id: str, limit: int = 50) -> list[dict]:
        conn = self._connect()
        try:
            rows = conn.execute(
                """
                SELECT entry_id, kind, amount_points, balance_after, day, round_id, bet_id, reference, created_at
                FROM ledger WHERE wallet_id = ?
                ORDER BY entry_id DESC
                LIMIT ?
                """,
                (wallet_id, limit),
            ).fetchall()
        finally:
            conn.close()
        return [dict(r) for r in rows]

    def get_transactions(self, session_id: str, limit: int = 50) -> list[dict]:
        """v1-shaped view of the ledger for /api/player/transactions."""
        return [
            {
                "event_id": e["entry_id"],
                "round_id": e["round_id"],
                "kind": e["kind"],
                "amount": str(e["amount_points"]),
                "created_at": e["created_at"],
            }
            for e in self.get_ledger(session_id, limit)
        ]

    def get_leaderboard(self, limit: int = 10) -> list[dict]:
        conn = self._connect()
        try:
            rows = conn.execute(
                """
                SELECT wallet_id,
                       COUNT(*) AS games,
                       SUM(CASE WHEN status = 'cashed_out' THEN 1 ELSE 0 END) AS wins,
                       SUM(payout_points - wager_points) AS net
                FROM rounds
                WHERE status IN ('cashed_out', 'crashed')
                GROUP BY wallet_id
                ORDER BY net DESC, games DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        finally:
            conn.close()

        return [
            {
                "rank": index,
                "player": "Cat-" + sha256(r["wallet_id"].encode()).hexdigest()[:8],
                "games": r["games"],
                "wins": r["wins"],
                "net_profit": str(r["net"]),
            }
            for index, r in enumerate(rows, start=1)
        ]

    # IDEMPOTENCY

    def _replay(self, conn, wallet_id: str, key: str | None, endpoint: str, request: dict) -> dict | None:
        """The stored response for a repeated request, None for a new one."""
        if key is None:
            return None
        if not IDEMPOTENCY_KEY.fullmatch(key):
            raise WalletError("Idempotency-Key must be 8-100 characters of A-Z a-z 0-9 _ . : -")
        row = conn.execute(
            "SELECT endpoint, request_hash, response_json FROM idempotency_keys WHERE wallet_id = ? AND idem_key = ?",
            (wallet_id, key),
        ).fetchone()
        if row is None:
            return None
        if row["endpoint"] != endpoint or row["request_hash"] != _request_hash(request):
            raise IdempotencyConflict("This Idempotency-Key was already used for a different request")
        return {**json.loads(row["response_json"]), "replayed": True}

    def _remember(self, conn, wallet_id: str, key: str | None, endpoint: str, request: dict, response: dict):
        if key is not None:
            conn.execute(
                """
                INSERT INTO idempotency_keys(wallet_id, idem_key, endpoint, request_hash, response_json)
                VALUES (?, ?, ?, ?, ?)
                """,
                (wallet_id, key, endpoint, _request_hash(request), json.dumps(response)),
            )

    def _require_wallet(self, conn, wallet_id: str) -> dict:
        row = conn.execute("SELECT * FROM wallets WHERE wallet_id = ?", (wallet_id,)).fetchone()
        if row is None:
            raise KeyError("Wallet not found")
        return dict(row)

    # REWARDS

    def claim_daily_free(self, wallet_id: str, idempotency_key: str | None = None) -> dict:
        """20 free points, once per server calendar day."""
        request = {}
        with self._write() as conn:
            replay = self._replay(conn, wallet_id, idempotency_key, "daily_free", request)
            if replay:
                return replay
            self._require_wallet(conn, wallet_id)
            day = self.today()
            taken = conn.execute(
                "SELECT 1 FROM ledger WHERE wallet_id = ? AND kind = 'daily_free' AND day = ?", (wallet_id, day)
            ).fetchone()
            if taken:
                raise AlreadyClaimed("Today's free points were already claimed")
            entry_id, balance = self._post(conn, wallet_id, "daily_free", DAILY_FREE_POINTS)
            response = {"kind": "daily_free", "points": DAILY_FREE_POINTS, "balance": balance,
                        "entry_id": entry_id, "day": day}
            self._remember(conn, wallet_id, idempotency_key, "daily_free", request, response)
            return response

    # SIMULATED PURCHASES

    def purchase(self, wallet_id: str, product_id: str, provider: PaymentProvider | None,
                 idempotency_key: str | None, amount_dollars: int | None = None) -> dict:
        """Simulated purchase. Returns the receipt (status 'succeeded' or 'declined').
        product 'topup' takes amount_dollars ($1-$100, 8 points per dollar)."""
        if provider is None:
            raise FeatureDisabled("Simulated payments are off (start the server with DEMO_MODE=1)")
        if idempotency_key is None:
            raise WalletError("Purchases need an Idempotency-Key header")
        product = self._product(product_id, amount_dollars)
        request = {"product": product_id, "amount_cents": product.price_cents}

        with self._write() as conn:
            replay = self._replay(conn, wallet_id, idempotency_key, "purchase", request)
            if replay:
                return replay
            response = self._charge(conn, wallet_id, product, provider, idempotency_key)
            self._remember(conn, wallet_id, idempotency_key, "purchase", request, response)
            return response

    @staticmethod
    def _product(product_id: str, amount_dollars: int | None) -> Product:
        if product_id == "topup":
            if amount_dollars is None:
                raise WalletError("Choose a top-up amount")
            try:
                return topup_product(amount_dollars)
            except ValueError as exc:
                raise WalletError(str(exc)) from exc
        product = PRODUCTS.get(product_id)
        if product is None:
            raise WalletError(f"Unknown product: {product_id}")
        return product

    def _topped_up_today(self, conn, wallet_id: str) -> int:
        """Dollars of successful top-ups today (wallet screen and cashier together)."""
        cents = conn.execute(
            f"""
            SELECT COALESCE(SUM(amount_cents), 0) FROM payments
            WHERE wallet_id = ? AND day = ? AND status = 'succeeded'
              AND product IN ({', '.join('?' for _ in TOPUP_PRODUCTS)})
            """,
            (wallet_id, self.today(), *TOPUP_PRODUCTS),
        ).fetchone()[0]
        return cents // 100

    def _charge(self, conn, wallet_id: str, product: Product, provider: PaymentProvider,
                idempotency_key: str) -> dict:
        """Rules, charge, credit and receipt inside the caller's transaction."""
        self._require_wallet(conn, wallet_id)
        self._check_purchase(conn, wallet_id)
        day = self.today()
        if product.once_per_day and conn.execute(
            "SELECT 1 FROM ledger WHERE wallet_id = ? AND kind = ? AND day = ?",
            (wallet_id, product.ledger_kind, day),
        ).fetchone():
            raise LimitReached(f"The {product.label} can be bought once per day")
        if product.product_id in TOPUP_PRODUCTS:
            left = TOPUP_DAILY_CAP_DOLLARS - self._topped_up_today(conn, wallet_id)
            if product.price_cents > left * 100:
                raise LimitReached(
                    f"Top-ups are capped at ${TOPUP_DAILY_CAP_DOLLARS} a day (${max(0, left)} left today)")

        result = provider.charge(wallet_id, product, idempotency_key)
        entry_id, balance = None, self._require_wallet(conn, wallet_id)["balance_points"]
        if result.status == "succeeded":
            entry_id, balance = self._post(conn, wallet_id, product.ledger_kind, product.points,
                                           reference=result.payment_id)
        conn.execute(
            """
            INSERT INTO payments(payment_id, provider, wallet_id, product, amount_cents, points,
                                 status, day, entry_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (result.payment_id, result.provider, wallet_id, product.product_id, product.price_cents,
             product.points if result.status == "succeeded" else 0, result.status, day, entry_id),
        )
        row = conn.execute("SELECT * FROM payments WHERE payment_id = ?", (result.payment_id,)).fetchone()
        return {**_receipt(row), "balance": balance}

    def topup_view(self, conn, wallet_id: str, demo: bool) -> dict:
        used = self._topped_up_today(conn, wallet_id)
        return {
            "product_id": "topup",
            "label": "Demo top-up (any amount)",
            "currency": "CAD",
            "points_per_dollar": POINTS_PER_DOLLAR,
            "min_dollars": TOPUP_MIN_DOLLARS,
            "max_dollars": TOPUP_MAX_DOLLARS,
            "daily_cap_dollars": TOPUP_DAILY_CAP_DOLLARS,
            "topped_up_today_dollars": used,
            "left_today_dollars": max(0, TOPUP_DAILY_CAP_DOLLARS - used),
            "available": demo and used < TOPUP_DAILY_CAP_DOLLARS,
            "simulated": True,
        }

    def get_receipts(self, wallet_id: str, limit: int = 50) -> list[dict]:
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT * FROM payments WHERE wallet_id = ? ORDER BY created_at DESC, rowid DESC LIMIT ?",
                (wallet_id, limit),
            ).fetchall()
        finally:
            conn.close()
        return [_receipt(r) for r in rows]

    # TICKET BONUS (codes typed by the player; see lottery.py)

    def issue_ticket(self, validator) -> dict:
        """A genuine code for local testing (the API only offers this with DEMO_MODE=1)."""
        if validator is None:
            raise FeatureDisabled("Ticket entry is off on this server")
        return {"code": validator.issue(), "notice": TICKET_NOTICE}

    def _ticket_lock(self, conn, wallet_id: str):
        """(failures so far, locked_until or None) for this wallet; an expired lock counts as none."""
        row = conn.execute("SELECT failures, locked_until FROM ticket_entry WHERE wallet_id = ?", (wallet_id,)).fetchone()
        if row is None:
            return 0, None
        until = parse_time(row["locked_until"])
        return row["failures"], until if until and self.now() < until else None

    def _save_ticket_entry(self, conn, wallet_id: str, failures: int, locked_until: str | None) -> None:
        conn.execute(
            """
            INSERT INTO ticket_entry(wallet_id, failures, locked_until) VALUES (?, ?, ?)
            ON CONFLICT(wallet_id) DO UPDATE SET failures = excluded.failures,
                locked_until = excluded.locked_until, updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
            """,
            (wallet_id, failures, locked_until),
        )

    def redeem_lottery_ticket(self, wallet_id: str, code: str, validator: LotteryValidator | None,
                              idempotency_key: str | None = None) -> dict:
        """25 points for a genuine code, one per wallet per server day, each code once ever.
        Three wrong codes in a row lock entry for an hour. Every wrong code gets the same message."""
        if validator is None:
            raise FeatureDisabled("Ticket entry is off on this server")
        request = {"code": normalise_ticket(code)}
        wrong = False

        with self._write() as conn:
            replay = self._replay(conn, wallet_id, idempotency_key, "lottery", request)
            if replay:
                return replay
            self._require_wallet(conn, wallet_id)
            failures, locked = self._ticket_lock(conn, wallet_id)
            if locked:
                minutes = max(1, ceil((locked - self.now()).total_seconds() / 60))
                raise TicketLocked(f"Ticket entry is paused after too many tries. Try again in {minutes} min")
            day = self.today()
            used_today = conn.execute(
                "SELECT COUNT(*) FROM lottery_tickets WHERE wallet_id = ? AND day = ?", (wallet_id, day)
            ).fetchone()[0]
            if used_today >= TICKET_DAILY_MAX:
                raise LimitReached("You've already redeemed today's ticket. Come back tomorrow")

            try:
                ticket = validator.validate(code, conn=conn)
                if conn.execute("SELECT 1 FROM lottery_tickets WHERE ticket_number = ?", (ticket.ticket_id,)).fetchone():
                    raise InvalidTicket(NOT_VERIFIED)
            except InvalidTicket:
                failures += 1
                lock = None
                if failures >= TICKET_MAX_FAILURES:
                    lock, failures = (self.now() + timedelta(minutes=TICKET_LOCK_MINUTES)).isoformat(), 0
                    self._audit(conn, "system", "ticket_entry_locked", wallet_id, until=lock)
                self._save_ticket_entry(conn, wallet_id, failures, lock)
                wrong = True       # committed below, then refused
            else:
                entry_id, balance = self._post(conn, wallet_id, "lottery_ticket", TICKET_POINTS,
                                               reference=ticket.ticket_id)
                conn.execute(
                    "INSERT INTO lottery_tickets(ticket_number, wallet_id, day, entry_id) VALUES (?, ?, ?, ?)",
                    (ticket.ticket_id, wallet_id, day, entry_id),
                )
                self._save_ticket_entry(conn, wallet_id, 0, None)
                response = {"kind": "lottery_ticket", "points": TICKET_POINTS, "balance": balance,
                            "entry_id": entry_id, "day": day, "redeemed_today": used_today + 1,
                            "daily_max": TICKET_DAILY_MAX, "notice": TICKET_NOTICE}
                self._remember(conn, wallet_id, idempotency_key, "lottery", request, response)
                return response
        if wrong:
            raise InvalidTicket(NOT_VERIFIED)

    # SUMMARY (what the Phase 3 wallet screen needs in one call)

    def wallet_summary(self, wallet_id: str, demo: bool = False, tickets: bool = False) -> dict:
        conn = self._connect()
        try:
            wallet = self._require_wallet(conn, wallet_id)
            day = self.today()
            kinds_today = {
                r[0] for r in conn.execute(
                    "SELECT DISTINCT kind FROM ledger WHERE wallet_id = ? AND day = ?", (wallet_id, day))
            }
            tickets_today = conn.execute(
                "SELECT COUNT(*) FROM lottery_tickets WHERE wallet_id = ? AND day = ?", (wallet_id, day)
            ).fetchone()[0]
            rg = self._rg(conn, wallet_id)
            spent = self._spent_today(conn, wallet_id)
            topup = self.topup_view(conn, wallet_id, demo)
            _, ticket_lock = self._ticket_lock(conn, wallet_id)
        finally:
            conn.close()

        return {
            "wallet_id": wallet_id,
            "kind": wallet["kind"],
            "name": wallet["display_name"],
            "balance": wallet["balance_points"],
            "day": day,
            "daily_free": {"points": DAILY_FREE_POINTS, "available": "daily_free" not in kinds_today},
            "products": [
                {
                    "product_id": p.product_id,
                    "label": p.label,
                    "price": f"{p.price_cents / 100:.2f}",
                    "currency": "CAD",
                    "points": p.points,
                    "once_per_day": p.once_per_day,
                    "available": demo and not (p.once_per_day and p.ledger_kind in kinds_today),
                    "simulated": True,
                }
                for p in PRODUCTS.values() if p.product_id not in TOPUP_PRODUCTS
            ],
            "topup": topup,
            "lottery": {"points": TICKET_POINTS, "redeemed_today": tickets_today,
                        "daily_max": TICKET_DAILY_MAX, "enabled": tickets,
                        "locked_until": ticket_lock.isoformat() if ticket_lock else None,
                        "available": tickets and tickets_today < TICKET_DAILY_MAX and ticket_lock is None},
            "demo_mode": demo,
            "spent_today": spent,
            "limits": rg.__dict__,
            "responsible": self.responsible_view(wallet_id),
            "server_time": self.now().isoformat(),
        }

    # PROFILES

    def profile(self, wallet_id: str) -> dict:
        conn = self._connect()
        try:
            wallet = self._require_wallet(conn, wallet_id)
            row = conn.execute("SELECT * FROM profiles WHERE wallet_id = ?", (wallet_id,)).fetchone()
            stats = self._stats(conn, wallet_id)
        finally:
            conn.close()
        return {
            "wallet_id": wallet_id,
            "kind": wallet["kind"],
            "nickname": row["nickname"] if row else wallet["display_name"],
            "avatar": row["avatar"] if row else AVATARS[0],
            "avatars": list(AVATARS),
            "balance": wallet["balance_points"],
            "member_since": wallet["created_at"],
            "stats": stats,
        }

    def save_profile(self, wallet_id: str, nickname: str, avatar: str) -> dict:
        nickname = " ".join((nickname or "").split())
        if not 2 <= len(nickname) <= 12 or not NICKNAME.fullmatch(nickname):
            raise WalletError("Nickname: 2-12 letters, digits, spaces, - or _")
        if avatar not in AVATARS:
            raise WalletError(f"Avatar must be one of {', '.join(AVATARS)}")
        with self._write() as conn:
            self._require_wallet(conn, wallet_id)
            conn.execute(
                """
                INSERT INTO profiles(wallet_id, nickname, avatar) VALUES (?, ?, ?)
                ON CONFLICT(wallet_id) DO UPDATE SET nickname = excluded.nickname, avatar = excluded.avatar,
                    updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                """,
                (wallet_id, nickname, avatar),
            )
            conn.execute("UPDATE wallets SET display_name = ? WHERE wallet_id = ?", (nickname, wallet_id))
        return self.profile(wallet_id)

    @staticmethod
    def _stats(conn, wallet_id: str) -> dict:
        rows = [dict(r) for r in conn.execute(
            """
            SELECT status, wager_points AS wager, payout_points AS payout, cashout_multiplier AS m
            FROM rounds WHERE wallet_id = ? AND status IN ('cashed_out', 'crashed')
            UNION ALL
            SELECT status, wager_points, payout_points, cashout_multiplier
            FROM crowd_bets WHERE wallet_id = ? AND status IN ('cashed_out', 'crashed')
            """,
            (wallet_id, wallet_id),
        )]
        wins = [r for r in rows if r["status"] == "cashed_out"]
        return {
            "rounds": len(rows),
            "wins": len(wins),
            "best_multiplier": max((float(r["m"]) for r in wins if r["m"]), default=None),
            "wagered": sum(r["wager"] for r in rows),
            "net": sum(r["payout"] - r["wager"] for r in rows),
        }

    # SIGNED SINGLE-USE QR CODES + CASHIER (simulated counter top-ups)

    def issue_qr(self, wallet_id: str) -> dict:
        expires = self.now() + identity.QR_LIFETIME
        nonce = secrets.token_hex(16)
        with self._write() as conn:
            self._require_wallet(conn, wallet_id)
            self._check_purchase(conn, wallet_id)
            for _ in range(5):
                code = identity.new_counter_code()
                if not conn.execute("SELECT 1 FROM qr_tokens WHERE counter_code = ?", (code,)).fetchone():
                    break
            conn.execute(
                "INSERT INTO qr_tokens(nonce, counter_code, wallet_id, expires_at) VALUES (?, ?, ?, ?)",
                (nonce, code, wallet_id, expires.isoformat()),
            )
            self._audit(conn, "player", "cashier_code_issued", wallet_id, expires_at=expires.isoformat())
        token = identity.make_token(wallet_id, nonce, int(expires.timestamp()))
        return {"token": token, "counter_code": f"{code[:4]}-{code[4:]}", "expires_at": expires.isoformat(),
                "seconds": int(identity.QR_LIFETIME.total_seconds()),
                "svg": identity.qr_svg(token), "png_base64": identity.qr_png_base64(token)}

    def _qr_row(self, conn, token_or_code: str):
        """The unused, unexpired qr_tokens row for a scanned token or a typed counter code."""
        text = (token_or_code or "").strip()
        if text.startswith(identity.TOKEN_PREFIX + "."):
            claims = identity.read_token(text)
            row = claims and conn.execute(
                "SELECT * FROM qr_tokens WHERE nonce = ? AND wallet_id = ?", (claims["n"], claims["w"])
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT * FROM qr_tokens WHERE counter_code = ?", (identity.normalise_counter_code(text),)
            ).fetchone()
        if not row:
            raise WalletError("That code isn't valid")
        if row["used_at"]:
            raise TicketAlreadyUsed("That code was already used. Ask the player for a new one")
        if parse_time(row["expires_at"]) <= self.now():
            raise WalletError("That code has expired. Ask the player for a new one")
        return row

    def cashier_lookup(self, token_or_code: str, demo: bool = True) -> dict:
        conn = self._connect()
        try:
            row = self._qr_row(conn, token_or_code)
            topup = self.topup_view(conn, row["wallet_id"], demo)
            rg = self._rg(conn, row["wallet_id"])
        finally:
            conn.close()
        player = self.profile(row["wallet_id"])
        now = self.now()
        blocked = any(t and now < t for t in (parse_time(rg.cool_off_until), parse_time(rg.self_excluded_until)))
        return {"nickname": player["nickname"] or "Player", "avatar": player["avatar"],
                "balance": player["balance"], "expires_at": row["expires_at"], "topup": topup,
                "age_confirmed": bool(rg.age_confirmed_at), "blocked": blocked}

    def cashier_topup(self, token_or_code: str, amount_dollars: int, provider: PaymentProvider | None) -> dict:
        """Credit a counter top-up and spend the QR code, in one transaction."""
        if provider is None:
            raise FeatureDisabled("Simulated payments are off (start the server with DEMO_MODE=1)")
        product = self._product("topup", amount_dollars)
        with self._write() as conn:
            row = self._qr_row(conn, token_or_code)
            receipt = self._charge(conn, row["wallet_id"], product, provider, row["nonce"])
            used = conn.execute(
                "UPDATE qr_tokens SET used_at = ?, payment_id = ? WHERE nonce = ? AND used_at IS NULL",
                (self.now().isoformat(), receipt["payment_id"], row["nonce"]),
            )
            if used.rowcount != 1:
                raise TicketAlreadyUsed("That code was already used. Ask the player for a new one")
            self._audit(conn, "cashier", "cashier_topup", row["wallet_id"], dollars=amount_dollars,
                        points=receipt["points"], payment_id=receipt["payment_id"], status=receipt["status"])
            nickname = self._nickname(conn, row["wallet_id"])
        return {**receipt, "nickname": nickname, "wallet_id": row["wallet_id"]}

    @staticmethod
    def _nickname(conn, wallet_id: str) -> str:
        row = conn.execute("SELECT display_name FROM wallets WHERE wallet_id = ?", (wallet_id,)).fetchone()
        return (row and row[0]) or "Player"

    # AUDIT LOG + OPERATOR SETTINGS (migration 0009)

    @staticmethod
    def _audit(conn, actor: str, action: str, wallet_id: str | None = None, **detail) -> None:
        """Append one audit row inside the caller's transaction (so it exists only if the change does)."""
        conn.execute(
            "INSERT INTO audit_log(actor, action, wallet_id, detail) VALUES (?, ?, ?, ?)",
            (actor, action, wallet_id, json.dumps(detail, sort_keys=True, default=str)),
        )

    def audit_entries(self, limit: int = 100, wallet_id: str | None = None) -> list[dict]:
        conn = self._connect()
        try:
            sql = "SELECT * FROM audit_log" + (" WHERE wallet_id = ?" if wallet_id else "") + " ORDER BY audit_id DESC LIMIT ?"
            rows = conn.execute(sql, ((wallet_id,) if wallet_id else ()) + (limit,)).fetchall()
        finally:
            conn.close()
        return [{**dict(r), "detail": json.loads(r["detail"])} for r in rows]

    def get_setting(self, key: str, default=None):
        conn = self._connect()
        try:
            row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        finally:
            conn.close()
        return default if row is None else json.loads(row[0])

    def set_setting(self, key: str, value, actor: str = "operator") -> None:
        with self._write() as conn:
            old = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
            conn.execute(
                """
                INSERT INTO settings(key, value) VALUES (?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                """,
                (key, json.dumps(value, sort_keys=True)),
            )
            self._audit(conn, actor, "setting_changed", key=key,
                        old=None if old is None else json.loads(old[0]), new=value)

    # LEDGER AUDIT

    def audit(self) -> list[dict]:
        """Wallets whose balance does not equal the sum of their ledger, or whose chain is broken. [] = healthy."""
        problems = []
        conn = self._connect()
        try:
            for w in conn.execute("SELECT wallet_id, balance_points FROM wallets"):
                running = 0
                for e in conn.execute(
                    "SELECT entry_id, amount_points, balance_after FROM ledger WHERE wallet_id = ? ORDER BY entry_id",
                    (w["wallet_id"],),
                ):
                    running += e["amount_points"]
                    if running != e["balance_after"]:
                        problems.append({"wallet_id": w["wallet_id"], "entry_id": e["entry_id"],
                                         "problem": "balance_after does not follow the chain"})
                        break
                if running != w["balance_points"]:
                    problems.append({"wallet_id": w["wallet_id"], "problem": "balance differs from ledger sum",
                                     "balance": w["balance_points"], "ledger_sum": running})
        finally:
            conn.close()
        return problems


def _request_hash(request: dict) -> str:
    return sha256(json.dumps(request, sort_keys=True).encode("utf-8")).hexdigest()


def _receipt(row) -> dict:
    return {
        "payment_id": row["payment_id"],
        "provider": row["provider"],
        "product": row["product"],
        "label": product_label(row["product"], row["amount_cents"]),
        "price": f"{row['amount_cents'] / 100:.2f}",
        "currency": row["currency"],
        "points": row["points"],
        "status": row["status"],
        "entry_id": row["entry_id"],
        "day": row["day"],
        "created_at": row["created_at"],
        "simulated": True,
        "notice": SIMULATED_NOTICE,
    }


class _Txn:
    """`with` block: BEGIN IMMEDIATE ... COMMIT (ROLLBACK on any exception), then close."""

    def __init__(self, conn):
        self.conn = conn

    def __enter__(self):
        self.conn.isolation_level = None
        self.conn.execute("BEGIN IMMEDIATE")
        return self.conn

    def __exit__(self, exc_type, exc, tb):
        try:
            self.conn.execute("ROLLBACK" if exc_type else "COMMIT")
        finally:
            self.conn.close()
        return False
