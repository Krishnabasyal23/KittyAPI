
import os

from contextlib import asynccontextmanager
from decimal import Decimal
from typing import Annotated
from unittest import result

from fastapi import (
    FastAPI,
    Header,
    HTTPException,
    Query,
)
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field, ConfigDict

from database.connection import initialize_database
from database.wallet_store import WalletStore
from fairness import NotRevealedYet
from errors import WalletError
from randomness import LIVE_RNG_MODES
from round_manager import RoundManager
from crowd import CrowdTable, make_router as make_crowd_router
from season_rules import (
    HAPPY_HOUR_LABEL,
    bonus_percents,
    describe as describe_season,
    happy_hour,
)


VERSION = "2.0.0-alpha.2"


class StartRoundRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    wager: Decimal = Field(
        gt=0,
        description="Wager in virtual credits",
    )


def game_call(method, *args, **kwargs):
    try:
        return method(*args, **kwargs)

    except WalletError as exc:
        raise HTTPException(
            status_code=exc.status,
            detail=str(exc),
        ) from exc

    except KeyError as exc:
        raise HTTPException(
            status_code=404,
            detail=str(exc.args[0]),
        ) from exc

    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail=str(exc),
        ) from exc


store = WalletStore()

manager = RoundManager(
    session_store=store,
    season_rules=True,
)

# Crowd mode: phones in the room bet on the same cat (see crowd.py)
crowd = CrowdTable(manager)


@asynccontextmanager
async def lifespan(app: FastAPI):
    initialize_database()

    # Refund old unfinished rounds once on startup.
    store.recover_interrupted_rounds()

    yield


app = FastAPI(
    title="Quantum Cat Crash API",
    description=(
        "Quantum-inspired crash game "
        "with persistent virtual credits."
    ),
    version=VERSION,
    lifespan=lifespan,
)


GAME_RNG_MODE = os.getenv(
    "GAME_RNG_MODE", "hybrid"
).lower()

if GAME_RNG_MODE not in LIVE_RNG_MODES:
    raise RuntimeError(
        "Gameplay RNG must be 'hybrid' or 'secure'"
    )


CORS_ORIGINS = [
    origin.strip()
    for origin in os.getenv(
        "CORS_ORIGINS",
        "http://localhost:5173,"
        "http://127.0.0.1:5173,"
        "http://localhost:3000,"
        "http://localhost:8080",
    ).split(",")
    if origin.strip()
]


app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,
    allow_credentials=False,
    allow_methods=["GET", "POST"],
    allow_headers=["Content-Type", "X-Session-ID"],
)

app.include_router(make_crowd_router(crowd))

SessionHeader = Annotated[
    str,
    Header(alias="X-Session-ID", min_length=1),
]


@app.get("/api/health")
def health():
    return {
        "status": "ok",
        "game": "Quantum Cat Crash",
        "version": VERSION,
        "rng_mode": GAME_RNG_MODE,
        "bonus_pct": bonus_percents(),
        "happy_hour": happy_hour(),
        "promo": HAPPY_HOUR_LABEL if happy_hour() else None,
    }


@app.post("/api/sessions", status_code=201)
def create_session():
    return game_call(manager.create_session)


@app.post("/api/rounds", status_code=201)
def start_round(
    payload: StartRoundRequest,
    session_id: SessionHeader,
):
    result = game_call(
        manager.start_round,
        session_id,
        payload.wager,
        rng_mode=GAME_RNG_MODE,
    )
    crowd.attach(session_id, result)  # the phones ride this same round
    return result


@app.get("/api/rounds/{round_id}/state")
def get_round_state(
    round_id: str,
    session_id: SessionHeader,
):
    return game_call(
        manager.get_round_state,
        session_id,
        round_id,
    )


@app.post("/api/rounds/{round_id}/cashout")
def cash_out(
    round_id: str,
    session_id: SessionHeader,
):
    return game_call(
        manager.cash_out,
        session_id,
        round_id,
    )


@app.get("/api/rounds/{round_id}/result")
def get_round_result(
    round_id: str,
    session_id: SessionHeader,
):
    return game_call(
        manager.get_round_result,
        session_id,
        round_id,
    )


@app.get("/api/rounds/{round_id}/verify")
def verify_round(
    round_id: str,
    session_id: SessionHeader,
):
    """Verify this round: the revealed seal plus hash / math / qubit checks (409 until the crash)."""
    try:
        return game_call(
            manager.verify_round,
            session_id,
            round_id,
        )
    except NotRevealedYet as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.get("/api/player/balance")
def get_balance(session_id: SessionHeader):
    return game_call(
        manager.get_balance,
        session_id,
    )


@app.get("/api/player/history")
def player_history(
    session_id: SessionHeader,
    limit: int = Query(30, ge=1, le=100),
):
    return game_call(
        manager.get_history,
        session_id,
        limit,
    )


@app.get("/api/player/transactions")
def player_transactions(
    session_id: SessionHeader,
    limit: int = Query(50, ge=1, le=100),
):
    return game_call(
        manager.get_transactions,
        session_id,
        limit,
    )

class SeasonPick(BaseModel):
    season: str | None = None


@app.post("/api/season/next")
def pick_next_season(payload: SeasonPick, session_id: SessionHeader):
    """Host control (key N on the game screen): choose or cycle the next walk's season."""
    return game_call(lambda: describe_season(manager.set_next_season(session_id, payload.season)))

@app.get("/api/season/next")
def next_season(session_id: SessionHeader):
    """Forecast: the season (and its rule) of this player's next walk."""
    return game_call(lambda: describe_season(manager.peek_next_season(session_id)))


@app.get("/api/rounds/{round_id}/season")
def round_season(round_id: str, session_id: SessionHeader):
    """This walk's season + event schedule (golden butterfly, leaf storms) for the game screen."""
    return game_call(manager.get_round_season, session_id, round_id)

@app.get("/api/leaderboard")
def leaderboard(
    limit: int = Query(10, ge=1, le=100),
):
    return store.get_leaderboard(limit)
