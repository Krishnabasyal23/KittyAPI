"""Operator reports: read-only queries over the v2 database for the local operator dashboard.

Rounds = host (PC) rounds plus phone crowd bets, counted only once settled (cashed out or crashed;
interrupted and refunded ones are left out). RTP = points paid back / points wagered.
"""

import csv
import io

from datetime import date, timedelta


SETTLED = "('cashed_out', 'crashed')"

# one row per settled bet: day (server local time, like the ledger), season, wager, payout
BETS = f"""
    SELECT date(started_at, 'localtime') AS day, season, wager_points AS wager, payout_points AS payout,
           wallet_id, 'pc' AS source
    FROM rounds WHERE status IN {SETTLED}
    UNION ALL
    SELECT date(b.created_at, 'localtime'), COALESCE(r.season, '?'), b.wager_points, b.payout_points,
           b.wallet_id, 'phone'
    FROM crowd_bets b LEFT JOIN rounds r ON r.round_id = b.round_id
    WHERE b.status IN {SETTLED}
"""


def _rtp(wagered: int, paid: int) -> float | None:
    return round(100.0 * paid / wagered, 2) if wagered else None


def summary(conn, days: int = 14, today: str | None = None) -> dict:
    today = today or date.today().isoformat()
    first = (date.fromisoformat(today) - timedelta(days=days - 1)).isoformat()

    totals = conn.execute(f"SELECT COUNT(*) n, COALESCE(SUM(wager),0) w, COALESCE(SUM(payout),0) p FROM ({BETS})").fetchone()
    todays = conn.execute(f"SELECT COUNT(*) n, COALESCE(SUM(wager),0) w, COALESCE(SUM(payout),0) p FROM ({BETS}) WHERE day = ?",
                          (today,)).fetchone()
    seasons = [
        {"season": r["season"], "bets": r["n"], "wagered": r["w"], "paid": r["p"], "rtp": _rtp(r["w"], r["p"])}
        for r in conn.execute(f"SELECT season, COUNT(*) n, SUM(wager) w, SUM(payout) p FROM ({BETS}) GROUP BY season ORDER BY season")
    ]

    bets_by_day = {r["day"]: r for r in conn.execute(
        f"SELECT day, COUNT(*) n, SUM(wager) w, SUM(payout) p, COUNT(DISTINCT wallet_id) players FROM ({BETS}) "
        "WHERE day >= ? GROUP BY day", (first,))}
    topups_by_day = {r["day"]: r for r in conn.execute(
        "SELECT day, COUNT(*) n, SUM(amount_cents) cents, SUM(points) pts FROM payments "
        "WHERE status = 'succeeded' AND product IN ('topup', 'topup_5') AND day >= ? GROUP BY day", (first,))}
    daily = []
    for i in range(days):
        d = (date.fromisoformat(first) + timedelta(days=i)).isoformat()
        b, t = bets_by_day.get(d), topups_by_day.get(d)
        w, p = (b["w"], b["p"]) if b else (0, 0)
        daily.append({"day": d, "bets": b["n"] if b else 0, "players": b["players"] if b else 0,
                      "wagered": w, "paid": p, "rtp": _rtp(w, p),
                      "topups": t["n"] if t else 0, "topup_dollars": (t["cents"] // 100) if t else 0,
                      "topup_points": t["pts"] if t else 0})

    wallets = conn.execute(
        "SELECT COUNT(*) n, SUM(kind = 'pc') pc, SUM(kind = 'phone') phone, COALESCE(SUM(balance_points),0) pts FROM wallets"
    ).fetchone()
    active_today = conn.execute("SELECT COUNT(DISTINCT wallet_id) FROM ledger WHERE day = ?", (today,)).fetchone()[0]
    points_by_kind = {r["kind"]: r["total"] for r in conn.execute(
        "SELECT kind, SUM(amount_points) total FROM ledger GROUP BY kind")}
    topups = {r["provider"]: {"count": r["n"], "dollars": r["cents"] // 100, "points": r["pts"]} for r in conn.execute(
        "SELECT provider, COUNT(*) n, SUM(amount_cents) cents, SUM(points) pts FROM payments "
        "WHERE status = 'succeeded' AND product IN ('topup', 'topup_5') GROUP BY provider")}
    now = conn.execute("SELECT strftime('%Y-%m-%dT%H:%M:%f', 'now')").fetchone()[0]
    rg = conn.execute(
        """
        SELECT SUM(daily_spend_limit_points IS NOT NULL) limits,
               SUM(cool_off_until > ?) cooling, SUM(self_excluded_until > ?) excluded
        FROM rg_settings
        """, (now, now)).fetchone()

    return {
        "today": today,
        "rtp": {"all_time": _rtp(totals["w"], totals["p"]), "today": _rtp(todays["w"], todays["p"]),
                "by_season": seasons},
        "bets": {"all_time": totals["n"], "today": todays["n"], "wagered": totals["w"], "paid": totals["p"]},
        "players": {"wallets": wallets["n"], "pc": wallets["pc"] or 0, "phone": wallets["phone"] or 0,
                    "active_today": active_today},
        "points": {"in_wallets": wallets["pts"], "issued_by_kind": points_by_kind},
        "topups": {"by_provider": topups,
                   "today_dollars": daily[-1]["topup_dollars"], "today_count": daily[-1]["topups"]},
        "safe_play": {"daily_limits_set": rg["limits"] or 0, "cooling_off_now": rg["cooling"] or 0,
                      "self_excluded_now": rg["excluded"] or 0},
        "daily": daily,
    }


EXPORTS = {
    "daily": None,   # built from summary()
    "bets": f"SELECT * FROM ({BETS}) ORDER BY day",
    "ledger": "SELECT entry_id, wallet_id, kind, amount_points, balance_after, day, round_id, bet_id, reference, created_at "
              "FROM ledger ORDER BY entry_id",
    "audit": "SELECT audit_id, actor, action, wallet_id, detail, created_at FROM audit_log ORDER BY audit_id",
    "payments": "SELECT payment_id, provider, wallet_id, product, amount_cents, currency, points, status, day, created_at "
                "FROM payments ORDER BY created_at",
}


def export_csv(conn, name: str, days: int = 90) -> str:
    if name not in EXPORTS:
        raise KeyError(f"Unknown export: {name}")
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\n")
    if name == "daily":
        rows = summary(conn, days)["daily"]
        writer.writerow(list(rows[0].keys()))
        writer.writerows([list(r.values()) for r in rows])
    else:
        cursor = conn.execute(EXPORTS[name])
        writer.writerow([c[0] for c in cursor.description])
        writer.writerows(cursor.fetchall())
    return out.getvalue()
