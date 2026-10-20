
import math
import os
import secrets

_rng = secrets.SystemRandom()

BUTTERFLY_SECONDS = 1.6
COCOA_LINE = 2.0
LEAF_STORM_SECONDS = 2.5

# Season bonus multipliers. The default keeps every strategy under 100% RTP: the crash formula
# returns 96%, and 0.96 x 1.04 = 99.84% even for a player who always takes the bonus
# (see tests/sim_rtp.py). HAPPY_HOUR=1 restores the generous pitch-demo values (up to ~120% RTP),
# for demos only.
SAFE_BONUSES = {"butterfly": 1.04, "cocoa": 1.04}
HAPPY_HOUR_BONUSES = {"butterfly": 1.25, "cocoa": 1.10}
HAPPY_HOUR_LABEL = "HAPPY HOUR (demo)"


def happy_hour() -> bool:
    return os.getenv("HAPPY_HOUR", "").strip() == "1"


def bonus_factors() -> dict:
    return HAPPY_HOUR_BONUSES if happy_hour() else SAFE_BONUSES


def bonus_percents() -> dict:
    """{"butterfly": 4, "cocoa": 4}: what the screens should promise."""
    return {k: round((f - 1.0) * 100) for k, f in bonus_factors().items()}


RULES = {
    "spring": {
        "title": "Butterfly Catch",
        "axis": "timing",
        "rule": "A golden butterfly flutters by once. Cash out while it's here: +{butterfly}%!",
    },
    "summer": {
        "title": "Summer Zoomies",
        "axis": "speed",
        "rule": "40% chance kitty sprints: the multiplier climbs way faster!",
    },
    "fall": {
        "title": "Leaf Storm",
        "axis": "information",
        "rule": "Gusts of leaves hide the multiplier. Trust your gut!",
    },
    "winter": {
        "title": "Hot Cocoa",
        "axis": "patience",
        "rule": "Slow, sleepy walk. Cash out at 2.00x or more: +{cocoa}% cocoa bonus!",
    },
}


def describe(season: str) -> dict:
    info = dict(RULES.get(season, {"title": "", "axis": "", "rule": ""}))
    info["rule"] = info["rule"].format(**bonus_percents())
    return {"season": season, **info}


def plan(season: str, lead_in: float) -> dict:
    """Roll this walk's season events (seconds since the round started, warm-up included)."""
    events = {"butterfly": None, "leaf_storms": []}

    if season == "spring":
        start = lead_in + 0.8 + _rng.random() * 6.0
        events["butterfly"] = [round(start, 3), round(start + BUTTERFLY_SECONDS, 3)]

    elif season == "fall":
        t = lead_in + 1.0 + _rng.random() * 2.0
        for _ in range(3):
            events["leaf_storms"].append([round(t, 3), round(t + LEAF_STORM_SECONDS, 3)])
            t += LEAF_STORM_SECONDS + 2.0 + _rng.random() * 4.0

    return events


def _inside(window, t: float) -> bool:
    return window is not None and window[0] <= t < window[1]


def live(events: dict | None, elapsed: float) -> dict:
    """What is happening right now (for the game screen and the phones)."""
    events = events or {}
    return {
        "butterfly": _inside(events.get("butterfly"), elapsed),
        "leaf_storm": any(_inside(w, elapsed) for w in events.get("leaf_storms") or []),
    }


def apply_bonus(season: str, events: dict | None, elapsed: float, multiplier: float):
    """Returns (multiplier to pay, bonus tag or None)."""
    if events is None:
        return multiplier, None

    factors = bonus_factors()
    if season == "spring" and _inside(events.get("butterfly"), elapsed):
        factor, tag = factors["butterfly"], "butterfly"
    elif season == "winter" and multiplier >= COCOA_LINE:
        factor, tag = factors["cocoa"], "cocoa"
    else:
        return multiplier, None

    return math.floor(multiplier * factor * 100) / 100, tag
