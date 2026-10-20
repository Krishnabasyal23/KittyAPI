"""Return-to-player simulation using the server's own crash formula and season rules.

Run from KittyAPI (inside the hackathon conda env):
    python tests/sim_rtp.py                 # 400,000 rounds per scenario
    python tests/sim_rtp.py --rounds 20000  # quick check
    python tests/sim_rtp.py --happy-hour    # with the HAPPY_HOUR=1 demo bonuses (+25% / +10%)

Each round goes through the same functions a live round does: select_season, season_growth_rate
(Summer Zoomies), season_rules.plan (butterfly / leaf storms), generate_crash_point,
crash_after_seconds and season_rules.apply_bonus. The RNG is "secure":
a live mode with the same uniform distribution as "hybrid", but without a quantum-simulator call
per round, so 400k rounds finish in seconds instead of hours.

Every bet is 1.00 credit; RTP = total paid / total wagered, shown with its 95% margin of error.
Cash-out is assumed perfect: the player leaves at exactly the planned moment if the cat is still walking.
"""

import argparse
import math
import os
import sys
import time

from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TMP = Path(os.getenv("KITTY_TEST_TMP") or ROOT.parent / ".tmp") / "sim"
TMP.mkdir(parents=True, exist_ok=True)
# Never touch the v1 data, even though the simulation itself needs no database.
os.environ["KITTY_DB_PATH"] = str(TMP / "kittycat-sim.db")
sys.path.insert(0, str(ROOT))

from crash_engine import (  # noqa: E402
    MIN_CRASH_SECONDS,
    crash_after_seconds,
    generate_crash_point,
    multiplier_at,
    season_growth_rate,
    select_season,
)
from season_rules import apply_bonus, bonus_percents, happy_hour, plan  # noqa: E402

RNG_MODE = "secure"
# One unit per bet, paid at the exact multiplier. Live wallets round each payout to whole points
# (economy.calculate_cashout, half-to-even), which is unbiased and so does not shift the RTP.
WAGER = Decimal("1")
TARGETS = (1.5, 2.0, 3.0, 5.0)


def play_round(season: str, cashout_time) -> tuple[Decimal, str, bool]:
    """One live-style round. cashout_time(events, growth) -> seconds since start the player leaves.

    Returns (payout, season, bonus_paid).
    """
    growth, _rush = season_growth_rate(season, mode=RNG_MODE)
    events = plan(season, MIN_CRASH_SECONDS)
    crash_point = generate_crash_point(mode=RNG_MODE)
    crash_time = crash_after_seconds(crash_point, growth_rate=growth, lead_in_seconds=MIN_CRASH_SECONDS)

    t = cashout_time(events, growth)
    if t >= crash_time:
        return Decimal("0.00"), season, False

    # the same rounding the server uses at cash-out
    base = math.floor(multiplier_at(t, growth_rate=growth, lead_in_seconds=MIN_CRASH_SECONDS) * 100) / 100
    paid, bonus = apply_bonus(season, events, t, base)
    return WAGER * Decimal(str(paid)), season, bonus is not None


def at_target(target: float):
    """Leave the moment the multiplier reaches target."""
    def when(_events, growth):
        # +1 ms so floor(multiplier * 100) has really reached the target
        return MIN_CRASH_SECONDS + math.log(target) / growth + 0.001
    return when


def at_butterfly(events, _growth):
    """Spring strategy: leave the moment the golden butterfly appears."""
    return events["butterfly"][0]


def run(name: str, rounds: int, cashout_time, season=None) -> dict:
    paid = Decimal("0.00")
    paid_sq = 0.0
    wins = bonuses = 0
    by_season = {}
    started = time.perf_counter()

    for _ in range(rounds):
        s = season or select_season()
        payout, s, bonus = play_round(s, cashout_time)
        paid += payout
        paid_sq += float(payout) ** 2
        wins += payout > 0
        bonuses += bonus
        row = by_season.setdefault(s, [0, Decimal("0.00")])
        row[0] += 1
        row[1] += payout

    wagered = WAGER * rounds
    rtp = float(paid / wagered)
    variance = max(0.0, paid_sq / rounds - rtp ** 2)
    return {
        "name": name,
        "rounds": rounds,
        "rtp": rtp,
        "margin": 1.96 * math.sqrt(variance / rounds),
        "win_rate": wins / rounds,
        "bonus_rate": bonuses / rounds,
        "by_season": {s: float(p / (WAGER * n)) for s, (n, p) in sorted(by_season.items())},
        "seconds": time.perf_counter() - started,
    }


def show(result: dict) -> None:
    seasons = "  ".join(f"{s} {r:6.2%}" for s, r in result["by_season"].items())
    print(
        f"{result['name']:<34} RTP {result['rtp']:7.2%} +/-{result['margin']:5.2%}   win {result['win_rate']:6.2%}"
        f"   bonus {result['bonus_rate']:6.2%}   ({result['seconds']:.0f}s)"
    )
    if len(result["by_season"]) > 1:
        print(f"{'':<34} {seasons}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--rounds", type=int, default=400_000, help="rounds per scenario")
    parser.add_argument("--happy-hour", action="store_true", help="simulate HAPPY_HOUR=1 demo bonuses")
    args = parser.parse_args()
    if args.happy_hour:
        os.environ["HAPPY_HOUR"] = "1"

    pct = bonus_percents()
    mode = "HAPPY HOUR (demo)" if happy_hour() else "standard"
    print(f"Catino RTP simulation: {args.rounds:,} rounds per scenario, RNG '{RNG_MODE}', wager {WAGER}")
    print(f"Bonuses: {mode}, butterfly +{pct['butterfly']}%, cocoa +{pct['cocoa']}%")
    print("Theoretical base RTP (no season bonus): 96.00%\n")

    print("Fixed cash-out target, random season (as live):")
    for target in TARGETS:
        show(run(f"  cash out at {target:.2f}x", args.rounds, at_target(target)))

    print("\nSeason strategies:")
    show(run("  spring: always at the butterfly", args.rounds, at_butterfly, season="spring"))
    show(run("  winter: always at 2.00x", args.rounds, at_target(2.0), season="winter"))


if __name__ == "__main__":
    main()
