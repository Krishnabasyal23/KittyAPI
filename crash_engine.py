import math
import secrets

from randomness import generate_random

HOUSE_EDGE = 0.04
HOUSE_EDGE_PCT = int(round(HOUSE_EDGE * 100))      # shown to players: the game pays back 100 - this on average
MAX_MULTIPLIER = 100.0

# Retained for compatibility with old standalone math tests.
GROWTH_RATE = 0.18

# No crash in the first 3 seconds.
MIN_CRASH_SECONDS = 3.0

SEASONS = ("spring", "summer", "fall", "winter")

SEASON_GROWTH = {
    "spring": 0.12,
    "summer": 0.13,
    "fall": 0.12,
    "winter": 0.085,
}

SUMMER_RUSH_CHANCE = 0.40
SUMMER_RUSH_GROWTH = 0.20

DISTRACTION_WEIGHTS = {
    "spring": {
        "butterfly": 50,
        "fish": 15,
        "sleep": 10,
        "yarn": 10,
        "laser": 10,
        "cardboard_box": 5,
    },
    "summer": {
        "fish": 30,
        "butterfly": 25,
        "laser": 20,
        "yarn": 10,
        "sleep": 10,
        "cardboard_box": 5,
    },
    "fall": {
        "yarn": 50,
        "fish": 10,
        "sleep": 15,
        "butterfly": 10,
        "laser": 10,
        "cardboard_box": 5,
    },
    "winter": {
        "sleep": 45,
        "cardboard_box": 25,
        "fish": 10,
        "yarn": 10,
        "butterfly": 5,
        "laser": 5,
    },
}


def validate_season(season: str) -> None:
    if season not in SEASONS:
        raise ValueError(f"Unknown season: {season}")


def select_season() -> str:
    return secrets.choice(SEASONS)


def season_growth_rate(
    season: str,
    mode: str = "hybrid"
) -> tuple[float, bool]:

    validate_season(season)

    if season != "summer":
        return SEASON_GROWTH[season], False

    # An independent simulated-quantum/hybrid RNG draw
    # determines whether Summer Rush activates.
    result = generate_random(
        mode=mode,
        qubits=6,
        samples=6
    )

    rush = result["normalized"] < SUMMER_RUSH_CHANCE

    growth_rate = (
        SUMMER_RUSH_GROWTH
        if rush
        else SEASON_GROWTH["summer"]
    )

    return growth_rate, rush


def select_distraction(
    season: str = "spring"
) -> str:

    validate_season(season)

    weights = DISTRACTION_WEIGHTS[season]
    roll = secrets.randbelow(sum(weights.values()))

    for event, weight in weights.items():
        if roll < weight:
            return event
        roll -= weight

    raise RuntimeError("Invalid distraction weights")


def crash_from_uniform(
    u: float,
    house_edge: float = HOUSE_EDGE,
    max_multiplier: float = MAX_MULTIPLIER
) -> float:

    if not 0.0 < u < 1.0:
        raise ValueError("u must be between 0 and 1")

    if not 0.0 <= house_edge < 1.0:
        raise ValueError("Invalid house edge")

    if max_multiplier <= 1.0:
        raise ValueError(
            "Maximum multiplier must exceed 1"
        )

    rtp = 1.0 - house_edge

    crash = rtp / u

    return max(
        1.0,
        min(crash, max_multiplier)
    )


def draw_crash_point(
    mode: str = "hybrid"
) -> tuple[float, dict]:
    """The crash point plus the random draw it came from (sealed by fairness.py)."""

    result = generate_random(
        mode=mode,
        qubits=6,
        samples=6
    )

    return crash_from_uniform(result["normalized"]), result


def generate_crash_point(
    mode: str = "hybrid"
) -> float:

    return draw_crash_point(mode)[0]


def multiplier_at(
    elapsed_seconds: float,
    growth_rate: float = GROWTH_RATE,
    lead_in_seconds: float = 0.0
) -> float:

    if elapsed_seconds < 0:
        raise ValueError(
            "Elapsed time cannot be negative"
        )

    if growth_rate <= 0:
        raise ValueError(
            "Growth rate must be positive"
        )

    if lead_in_seconds < 0:
        raise ValueError(
            "Lead-in cannot be negative"
        )

    active_seconds = max(
        0.0,
        elapsed_seconds - lead_in_seconds
    )

    return math.exp(
        growth_rate * active_seconds
    )


def crash_after_seconds(
    crash_point: float,
    growth_rate: float = GROWTH_RATE,
    lead_in_seconds: float = 0.0
) -> float:

    if crash_point < 1.0:
        raise ValueError(
            "Crash point cannot be below 1"
        )

    if growth_rate <= 0:
        raise ValueError(
            "Growth rate must be positive"
        )

    if lead_in_seconds < 0:
        raise ValueError(
            "Lead-in cannot be negative"
        )

    return (
        lead_in_seconds
        + math.log(crash_point) / growth_rate
    )