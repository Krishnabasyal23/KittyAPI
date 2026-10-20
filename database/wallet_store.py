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

from datetime import date
from decimal import Decimal
from hashlib import sha256
from math import isfinite
from time import time as wall_clock

from fairness import proof_to_columns
from economy import INITIAL_BALANCE, MAX_BET, MIN_BET, calculate_cashout, validate_points
from errors import (
    AlreadyClaimed,
    FeatureDisabled,
    IdempotencyConflict,
    InsufficientPoints,
    LimitReached,
    TicketAlreadyUsed,
    WalletError,
)
from lottery import MOCK_NOTICE, TICKET_DAILY_MAX, TICKET_POINTS, LotteryValidator
from payments import PRODUCTS, SIMULATED_NOTICE, PaymentProvider
from responsible import DefaultPolicy, RGSettings, WalletPolicy, utc_now

from .connection import get_connection


DAILY_FREE_POINTS = 20
IDEMPOTENCY_KEY = re.compile(r"^[A-Za-z0-9_.:\-]{8,100}$")


def server_day() -> str:
    """The server's calendar day (local time), which daily rewards and limits use."""
    return date.today().isoformat()


RG_FIELDS = ("daily_spend_limit_points", "cool_off_until", "self_excluded_until",
             "reality_check_minutes", "age_confirmed_at")


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
        row = conn.execute("SELECT * FROM rg_settings WHERE wallet_id = ?", (wallet_id,)).fetchone()
        return RGSettings(**{f: row[f] for f in RG_FIELDS}) if row else RGSettings()

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
        """Upsert settings (None clears a field). Backend entry point for the Phase 3 controls."""
        unknown = set(fields) - set(RG_FIELDS)
        if unknown:
            raise ValueError(f"Unknown setting(s): {sorted(unknown)}")
        with self._write() as conn:
            current = self._rg(conn, wallet_id).__dict__ | fields
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
                 idempotency_key: str | None) -> dict:
        """Simulated purchase. Returns the receipt (status 'succeeded' or 'declined')."""
        if provider is None:
            raise FeatureDisabled("Simulated payments are off (start the server with DEMO_MODE=1)")
        if idempotency_key is None:
            raise WalletError("Purchases need an Idempotency-Key header")
        product = PRODUCTS.get(product_id)
        if product is None:
            raise WalletError(f"Unknown product: {product_id}")
        request = {"product": product_id}

        with self._write() as conn:
            replay = self._replay(conn, wallet_id, idempotency_key, "purchase", request)
            if replay:
                return replay
            self._require_wallet(conn, wallet_id)
            self._check_purchase(conn, wallet_id)
            day = self.today()
            if product.once_per_day and conn.execute(
                "SELECT 1 FROM ledger WHERE wallet_id = ? AND kind = ? AND day = ?",
                (wallet_id, product.ledger_kind, day),
            ).fetchone():
                raise LimitReached(f"The {product.label} can be bought once per day")

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
                (result.payment_id, result.provider, wallet_id, product_id, product.price_cents,
                 product.points if result.status == "succeeded" else 0, result.status, day, entry_id),
            )
            row = conn.execute("SELECT * FROM payments WHERE payment_id = ?", (result.payment_id,)).fetchone()
            response = {**_receipt(row), "balance": balance}
            self._remember(conn, wallet_id, idempotency_key, "purchase", request, response)
            return response

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

    # MOCK LOTTERY TICKETS

    def issue_mock_ticket(self, validator, numbers=None) -> dict:
        if validator is None or not hasattr(validator, "issue"):
            raise FeatureDisabled("Mock lottery tickets are off (start the server with DEMO_MODE=1)")
        with self._write() as conn:
            return validator.issue(conn, numbers)

    def redeem_lottery_ticket(self, wallet_id: str, ticket_id: str, numbers, validator: LotteryValidator | None,
                              idempotency_key: str | None = None) -> dict:
        """25 points per valid ticket, at most 3 per wallet per server day, each ticket id once ever.
        Validation, the redemption record and the credit happen in one transaction."""
        if validator is None:
            raise FeatureDisabled("Mock lottery tickets are off (start the server with DEMO_MODE=1)")
        request = {"ticket_id": str(ticket_id).strip().upper(), "numbers": str(numbers)}

        with self._write() as conn:
            replay = self._replay(conn, wallet_id, idempotency_key, "lottery", request)
            if replay:
                return replay
            self._require_wallet(conn, wallet_id)
            ticket = validator.validate(ticket_id, numbers, conn=conn)

            if conn.execute(
                "SELECT 1 FROM lottery_tickets WHERE ticket_number = ?", (ticket.ticket_id,)
            ).fetchone():
                raise TicketAlreadyUsed("This ticket was already redeemed")
            day = self.today()
            used_today = conn.execute(
                "SELECT COUNT(*) FROM lottery_tickets WHERE wallet_id = ? AND day = ?", (wallet_id, day)
            ).fetchone()[0]
            if used_today >= TICKET_DAILY_MAX:
                raise LimitReached(f"At most {TICKET_DAILY_MAX} tickets per day")

            entry_id, balance = self._post(conn, wallet_id, "lottery_ticket", TICKET_POINTS,
                                           reference=ticket.ticket_id)
            conn.execute(
                "INSERT INTO lottery_tickets(ticket_number, wallet_id, day, entry_id) VALUES (?, ?, ?, ?)",
                (ticket.ticket_id, wallet_id, day, entry_id),
            )
            response = {"kind": "lottery_ticket", "ticket_id": ticket.ticket_id, "numbers": ticket.numbers,
                        "points": TICKET_POINTS, "balance": balance, "entry_id": entry_id, "day": day,
                        "redeemed_today": used_today + 1, "daily_max": TICKET_DAILY_MAX,
                        "validator": validator.name, "notice": MOCK_NOTICE}
            self._remember(conn, wallet_id, idempotency_key, "lottery", request, response)
            return response

    # SUMMARY (what the Phase 3 wallet screen needs in one call)

    def wallet_summary(self, wallet_id: str, demo: bool = False) -> dict:
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
                for p in PRODUCTS.values()
            ],
            "lottery": {"points": TICKET_POINTS, "redeemed_today": tickets_today,
                        "daily_max": TICKET_DAILY_MAX, "available": demo and tickets_today < TICKET_DAILY_MAX},
            "demo_mode": demo,
            "spent_today": spent,
            "limits": rg.__dict__,
        }

    # AUDIT

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
        "label": PRODUCTS[row["product"]].label if row["product"] in PRODUCTS else row["product"],
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
