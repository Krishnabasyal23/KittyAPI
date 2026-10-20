"""Server-authoritative wallets for Catino v2 (SQLite, see database/migrations).

Rules every write follows:
  * one short transaction opened with BEGIN IMMEDIATE, so concurrent writers queue instead of racing;
  * a balance only changes through _post(), which updates the wallet with a guarded UPDATE
    (balance + amount >= 0) and appends the matching ledger row (amount, balance_after) in the
    same transaction: both happen or neither does;
  * the ledger is append-only (database triggers), and once-only rules are unique indexes, so a
    bug or a retried request cannot pay twice.
"""

from datetime import date
from decimal import Decimal
from hashlib import sha256
from math import isfinite
from time import time as wall_clock

from fairness import proof_to_columns
from economy import INITIAL_BALANCE, MAX_BET, MIN_BET, calculate_cashout, validate_points
from errors import InsufficientPoints, WalletError

from .connection import get_connection


def server_day() -> str:
    """The server's calendar day (local time), which daily rewards and limits use."""
    return date.today().isoformat()


class WalletStore:
    def __init__(self, db_path=None, today=server_day):
        self.db_path = db_path
        self.today = today

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
