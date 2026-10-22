
import os

from contextlib import asynccontextmanager
from decimal import Decimal
from typing import Annotated
from unittest import result

from pathlib import Path

from fastapi import (
    Depends,
    FastAPI,
    Header,
    HTTPException,
    Query,
    Request,
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, Response
from pydantic import BaseModel, Field, ConfigDict

from database.connection import initialize_database
from database.migrate import status as migration_status
from database.wallet_store import WalletStore
from economy import MAX_BET, MIN_BET
from lottery import lottery_validator
from payments import CashierProvider, demo_mode, payment_provider
from fairness import NotRevealedYet, clean_seed
from errors import WalletError
from autohost import start_if_enabled as start_auto_host
from guard import local_only, public_guard
from randomness import LIVE_RNG_MODES
from round_manager import RoundManager
import reports
from local_env import load_local_env
from crowd import CrowdTable, make_router as make_crowd_router
from season_rules import (
    HAPPY_HOUR_LABEL,
    MAX_SAFE_BONUS_PCT,
    bonus_percents,
    set_bonus_percents,
    describe as describe_season,
    happy_hour,
)


VERSION = "2.0.0-beta.3"


load_local_env()


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
crowd = CrowdTable(manager, store)


@asynccontextmanager
async def lifespan(app: FastAPI):
    initialize_database()

    # Refund old unfinished rounds once on startup.
    store.recover_interrupted_rounds()

    # Season bonuses tuned by the operator (within the safe cap); a bad stored value falls back to the default.
    try:
        set_bonus_percents(store.get_setting(BONUS_SETTING))
    except ValueError:
        set_bonus_percents(None)

    auto = start_auto_host(manager, crowd, GAME_RNG_MODE)

    yield

    if auto is not None:
        auto[1].set()


app = FastAPI(
    title="Catino API",
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
    allow_methods=["GET", "POST", "PUT"],
    allow_headers=["Content-Type", "X-Session-ID", "Idempotency-Key"],
)

app.middleware("http")(public_guard)

app.include_router(make_crowd_router(crowd))

SessionHeader = Annotated[
    str,
    Header(alias="X-Session-ID", min_length=1),
]

IdempotencyHeader = Annotated[
    str | None,
    Header(alias="Idempotency-Key"),
]

CASHIER_HTML = Path(__file__).resolve().parent / "cashier.html"


DEMO_NOTICE = "DEMO MODE: payments are simulated and tickets are demo tickets"


@app.get("/api/health")
def health():
    return {
        "status": "ok",
        "game": "Catino",
        "version": VERSION,
        "rng_mode": GAME_RNG_MODE,
        "bonus_pct": bonus_percents(),
        "happy_hour": happy_hour(),
        "promo": HAPPY_HOUR_LABEL if happy_hour() else None,
        "min_bet": str(MIN_BET),
        "max_bet": str(MAX_BET),
        "db_schema": max(migration_status()["applied"], default=0),
        "demo_mode": demo_mode(),
        "demo_notice": DEMO_NOTICE if demo_mode() else None,
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


class SeedRequest(BaseModel):
    seed: str = Field(max_length=64)


@app.post("/api/rounds/{round_id}/seed")
def send_seed(
    round_id: str,
    payload: SeedRequest,
    session_id: SessionHeader,
):
    """The player's seed for Schrodinger's Box: 16-64 hex characters, sent after the commitment was
    shown and before the lead-in ends (409 when it is too late or already sent)."""
    try:
        seed = clean_seed(payload.seed)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    try:
        return game_call(manager.submit_seed, session_id, round_id, seed)
    except HTTPException as exc:
        raise HTTPException(status_code=409 if exc.status_code == 400 else exc.status_code, detail=exc.detail) from exc


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
def pick_next_season(payload: SeasonPick, session_id: SessionHeader, lang: str = Query("en", max_length=5)):
    """Host control (key N on the game screen): choose or cycle the next walk's season."""
    return game_call(lambda: describe_season(manager.set_next_season(session_id, payload.season), lang))

@app.get("/api/season/next")
def next_season(session_id: SessionHeader, lang: str = Query("en", max_length=5)):
    """Forecast: the season (and its rule) of this player's next walk, plus the next three in `upcoming`."""
    seasons = game_call(manager.peek_seasons, session_id)
    return {**describe_season(seasons[0], lang), "upcoming": [describe_season(s, lang) for s in seasons]}


@app.get("/api/rounds/{round_id}/season")
def round_season(round_id: str, session_id: SessionHeader):
    """This walk's season + event schedule (golden butterfly, leaf storms) for the game screen."""
    return game_call(manager.get_round_season, session_id, round_id)

@app.get("/api/leaderboard")
def leaderboard(
    limit: int = Query(10, ge=1, le=100),
):
    return store.get_leaderboard(limit)


# WALLET (points; X-Session-ID is the wallet id: PC session or phone player id)

class PurchaseRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    product: str = Field(description="topup (with amount_dollars) or parking_5")
    amount_dollars: int | None = Field(None, description="top-up amount in whole dollars")
    confirmed: bool = Field(False, description="the player ticked 'I understand this is a simulated payment'")


class TicketRedeemRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    code: str = Field(max_length=64, description="the code printed on the ticket")


@app.get("/api/wallet")
def wallet(session_id: SessionHeader):
    """Balance, today's reward availability, products, lottery allowance and limits."""
    return game_call(store.wallet_summary, session_id, demo=demo_mode(), tickets=lottery_validator() is not None)


@app.get("/api/wallet/ledger")
def wallet_ledger(session_id: SessionHeader, limit: int = Query(50, ge=1, le=200)):
    game_call(store.balance, session_id)
    return store.get_ledger(session_id, limit)


@app.get("/api/wallet/receipts")
def wallet_receipts(session_id: SessionHeader, limit: int = Query(50, ge=1, le=200)):
    game_call(store.balance, session_id)
    return store.get_receipts(session_id, limit)


@app.post("/api/wallet/daily-free")
def claim_daily_free(session_id: SessionHeader, idempotency_key: IdempotencyHeader = None):
    result = game_call(store.claim_daily_free, session_id, idempotency_key)
    crowd.sync_balance(session_id)
    return result


@app.post("/api/wallet/purchase")
def purchase(payload: PurchaseRequest, session_id: SessionHeader, idempotency_key: IdempotencyHeader = None):
    """Simulated purchase (DEMO_MODE=1 only). Idempotency-Key and confirmed=true are required."""
    if not payload.confirmed:
        raise HTTPException(status_code=400, detail="Tick the box to confirm this simulated payment")
    receipt = game_call(store.purchase, session_id, payload.product, payment_provider(), idempotency_key,
                        payload.amount_dollars)
    crowd.sync_balance(session_id)
    if receipt["status"] != "succeeded":
        return JSONResponse(status_code=402, content=receipt)
    return receipt


@app.post("/api/wallet/lottery/redeem")
def redeem_ticket(payload: TicketRedeemRequest, session_id: SessionHeader,
                  idempotency_key: IdempotencyHeader = None):
    result = game_call(store.redeem_lottery_ticket, session_id, payload.code, lottery_validator(), idempotency_key)
    crowd.sync_balance(session_id)
    return result


@app.post("/api/demo/lottery/tickets", status_code=201, include_in_schema=False, dependencies=[Depends(local_only)])
def issue_ticket():
    """Development helper: a genuine ticket code (DEMO_MODE=1, this computer only)."""
    if not demo_mode():
        raise HTTPException(status_code=404, detail="Not Found")
    return game_call(store.issue_ticket, lottery_validator())


# RESPONSIBLE GAMBLING (rules enforced in WalletStore; see responsible.py)

class Confirm(BaseModel):
    model_config = ConfigDict(extra="forbid")
    confirm: bool = False


class LimitsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    daily_spend_limit_points: int | None = Field(None, description="null removes the limit (after 24 h)")
    reality_check_minutes: int | None = None


class CoolOffRequest(Confirm):
    hours: int


class SelfExcludeRequest(Confirm):
    days: int


def _confirmed(payload: Confirm, what: str) -> None:
    if not payload.confirm:
        raise HTTPException(status_code=400, detail=f"Confirm to {what}")


@app.get("/api/wallet/limits")
def get_limits(session_id: SessionHeader):
    return game_call(store.responsible_view, session_id)


@app.put("/api/wallet/limits")
def put_limits(payload: LimitsRequest, session_id: SessionHeader):
    """Lowering the daily limit applies now; raising or removing it applies after 24 hours."""
    if "daily_spend_limit_points" in payload.model_fields_set:
        game_call(store.set_spend_limit, session_id, payload.daily_spend_limit_points)
    if payload.reality_check_minutes is not None:
        game_call(store.set_reality_check, session_id, payload.reality_check_minutes)
    return game_call(store.responsible_view, session_id)


@app.post("/api/wallet/age-confirm")
def age_confirm(payload: Confirm, session_id: SessionHeader):
    """Self-attested 19+ confirmation (no ID check). Required before any wager or purchase."""
    _confirmed(payload, "that you are 19 or older")
    game_call(store.confirm_age, session_id)
    return game_call(store.responsible_view, session_id)


@app.post("/api/wallet/cool-off")
def cool_off(payload: CoolOffRequest, session_id: SessionHeader):
    """Pause play and purchases. Can be extended, never shortened."""
    _confirmed(payload, "start a cool-off")
    game_call(store.start_cool_off, session_id, payload.hours)
    return game_call(store.responsible_view, session_id)


@app.post("/api/wallet/self-exclude")
def self_exclude(payload: SelfExcludeRequest, session_id: SessionHeader):
    """Block play and purchases for 30, 180 or 365 days. Cannot be undone early."""
    _confirmed(payload, "self-exclude")
    game_call(store.self_exclude, session_id, payload.days)
    return game_call(store.responsible_view, session_id)


@app.get("/api/wallet/activity")
def activity(session_id: SessionHeader, since: str = Query(..., max_length=40)):
    """Bets, points wagered / won and net since `since` (ISO-8601), for the reality check."""
    return game_call(store.activity, session_id, since)


# PROFILE + SIGNED QR CODE (shown to the cashier)

class ProfileRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    nickname: str = Field(max_length=40)
    avatar: str = Field(max_length=20)


@app.get("/api/profile")
def get_profile(session_id: SessionHeader):
    return game_call(store.profile, session_id)


@app.put("/api/profile")
def put_profile(payload: ProfileRequest, session_id: SessionHeader):
    result = game_call(store.save_profile, session_id, payload.nickname, payload.avatar)
    crowd.rename(session_id, result["nickname"])
    return result


@app.post("/api/profile/qr", status_code=201)
def profile_qr(session_id: SessionHeader):
    """A signed QR code that works once, for a few minutes, at the cashier page."""
    return game_call(store.issue_qr, session_id)


# CASHIER PAGE (this computer only; counter payments are simulated)

class CashierCode(BaseModel):
    model_config = ConfigDict(extra="forbid")
    code: str = Field(min_length=4, max_length=400)


class CashierTopup(CashierCode):
    amount_dollars: int
    confirmed: bool = False


@app.get("/cashier", response_class=HTMLResponse, include_in_schema=False, dependencies=[Depends(local_only)])
def cashier_page():
    return HTMLResponse(CASHIER_HTML.read_text(encoding="utf-8"), headers={"Cache-Control": "no-store"})


@app.post("/api/cashier/lookup", dependencies=[Depends(local_only)])
def cashier_lookup(payload: CashierCode):
    return game_call(store.cashier_lookup, payload.code, demo_mode())


@app.post("/api/cashier/topup", dependencies=[Depends(local_only)])
def cashier_topup(payload: CashierTopup):
    """Record a simulated counter payment and credit the player's points. Spends the QR code."""
    if not payload.confirmed:
        raise HTTPException(status_code=400, detail="Tick the box to confirm this simulated payment")
    receipt = game_call(store.cashier_topup, payload.code, payload.amount_dollars,
                        CashierProvider() if demo_mode() else None)
    crowd.sync_balance(receipt.pop("wallet_id", None) or "")
    return receipt


# OPERATOR DASHBOARD (this computer only, read-only)

OPERATOR_HTML = Path(__file__).resolve().parent / "operator.html"


@app.get("/operator", response_class=HTMLResponse, include_in_schema=False, dependencies=[Depends(local_only)])
def operator_page():
    return HTMLResponse(OPERATOR_HTML.read_text(encoding="utf-8"), headers={"Cache-Control": "no-store"})


@app.get("/api/operator/summary", dependencies=[Depends(local_only)])
def operator_summary(days: int = Query(14, ge=1, le=90)):
    """Live RTP, rounds, players, top-ups, points and safe-play counts, plus one row per day."""
    conn = store._connect()
    try:
        result = reports.summary(conn, days, store.today())
    finally:
        conn.close()
    result["ledger_ok"] = store.audit() == []
    result["version"] = VERSION
    return result


@app.get("/api/operator/export/{name}.csv", dependencies=[Depends(local_only)])
def operator_export(name: str, days: int = Query(90, ge=1, le=365)):
    """CSV export: daily, bets, ledger or payments."""
    conn = store._connect()
    try:
        text = game_call(reports.export_csv, conn, name, days)
    finally:
        conn.close()
    return Response(text, media_type="text/csv",
                    headers={"Content-Disposition": f'attachment; filename="catino-{name}.csv"'})


# OPERATOR: season bonus tuning (capped so no strategy reaches 100% RTP) and the audit log

BONUS_SETTING = "season_bonus_pct"


class BonusRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    butterfly: int
    cocoa: int


def bonus_view() -> dict:
    return {"bonus_pct": bonus_percents(), "max_pct": MAX_SAFE_BONUS_PCT, "happy_hour": happy_hour(),
            "default_pct": {"butterfly": 4, "cocoa": 4}}


@app.get("/api/operator/bonus", dependencies=[Depends(local_only)])
def get_bonus():
    return bonus_view()


@app.put("/api/operator/bonus", dependencies=[Depends(local_only)])
def put_bonus(payload: BonusRequest):
    """Set the butterfly / cocoa bonus (whole percent, 0 to the safe cap). Saved and written to the audit log."""
    wanted = {"butterfly": payload.butterfly, "cocoa": payload.cocoa}
    game_call(set_bonus_percents, wanted)
    store.set_setting(BONUS_SETTING, wanted)
    return bonus_view()


@app.get("/api/operator/audit", dependencies=[Depends(local_only)])
def operator_audit(limit: int = Query(100, ge=1, le=1000), wallet_id: str | None = Query(None, max_length=100)):
    """Newest first: cashier top-ups and codes, play-safe changes, ticket locks, operator changes."""
    return store.audit_entries(limit, wallet_id)

