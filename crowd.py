
import asyncio
import json
import math
import os
import socket
import threading
import time
import uuid

from pathlib import Path

from fastapi import APIRouter, Depends, Header, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.encoders import jsonable_encoder
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel, Field

from crash_engine import SEASON_GROWTH, MIN_CRASH_SECONDS, multiplier_at
from economy import MIN_BET, MAX_BET, calculate_cashout, validate_points
from errors import WalletError
from fairness import NotRevealedYet
import guard
from season_rules import describe as describe_season, live as season_live, apply_bonus as season_bonus, bonus_percents, COCOA_LINE

MAX_PLAYERS = 60
HTML_FILE = Path(__file__).resolve().parent / "crowd.html"
PUSH_SECONDS = 0.2     # WebSocket push interval (phones used to poll every 0.3 s)
ASSET_DIR = Path(__file__).resolve().parent / "crowd_assets"   # cat1.png ... cat6.png (the phone's cat)


def _lan_ip() -> str:
    """This computer's address on the Wi-Fi (what phones must use)."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))  # no packet is sent, it just picks the right network card
        ip = s.getsockname()[0]
        s.close()
        return ip
    except OSError:
        return "127.0.0.1"


def _qr_rows(text: str):
    """QR code as rows of '0'/'1' (Unity draws it). Needs: pip install segno"""
    try:
        import segno
    except ImportError:
        return []
    qr = segno.make(text, error="m")
    return ["".join("1" if c else "0" for c in row) for row in qr.matrix]


def _money(v) -> float:
    return float(v) if v is not None else 0.0


def _round_multiplier(rd: dict, elapsed: float) -> float:
    """Same math as the round manager: this round's season speed, after the warm-up."""
    return math.floor(multiplier_at(
        max(0.0, elapsed),
        growth_rate=rd.get("growth_rate") or SEASON_GROWTH["spring"],
        lead_in_seconds=rd.get("lead_in_seconds") or 0.0,
    ) * 100) / 100


def _wallet_call(method, *args):
    """Run a wallet operation, turning wallet errors into HTTP errors for the phones."""
    try:
        return method(*args)
    except WalletError as exc:
        raise HTTPException(status_code=exc.status, detail=str(exc)) from exc
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Player not found - join again") from exc


class CrowdTable:
    """Phones bet on the big screen's walk. Points live in persistent 'phone' wallets (the store is
    the source of truth); this table only keeps the live round state and a cached balance."""

    def __init__(self, manager, store=None):
        self.manager = manager
        self.store = store if store is not None else manager._session_store
        self.clock = manager._clock
        self.lock = threading.RLock()
        self.players = {}           # player_id -> dict
        self.host_session = None
        self.phase = "waiting"      # waiting | betting | running | crashed
        self.closes_at = 0.0
        self.round = None           # the host's round dict (shared with RoundManager)
        self.round_no = 0
        self.join_url = None
        self.history = []
        self.forecast = None        # season of the next walk (players see it while betting)
        self.host_name = "PC Player"  # the person playing on the big screen (shown on the leaderboard)
        self.last_seen = 0.0          # monotonic time a phone last asked for the table (the auto host sleeps when nobody does)

    # ---------- host (the Unity game) ----------

    def set_host(self, session_id: str, port: int, name: str = "") -> dict:
        with self.lock:
            self.host_session = session_id
            self.host_name = " ".join((name or "").split())[:16] or "PC Player"
            public = os.getenv("PUBLIC_URL", "").strip().rstrip("/")
            self.join_url = (public or f"http://{_lan_ip()}:{port}") + "/crowd"
            return {"join_url": self.join_url, "qr": _qr_rows(self.join_url)}

    def open_betting(self, session_id: str, seconds: float) -> dict:
        with self.lock:
            self._require_host(session_id)
            self._tick()
            if self.phase == "running":
                return {"phase": self.phase}       # host is ahead of itself; ignore
            if self.phase != "betting":
                # new round: clear last round's results (unplayed bets would carry over, see below)
                for p in self.players.values():
                    p["bet"] = None
                    p["cashed_at"] = None
                    p["payout"] = None
                    p["bonus"] = None
                self.round = None
            self._peek_forecast()
            self.phase = "betting"
            self.closes_at = self.clock() + max(1.0, min(float(seconds), 60.0))
            return {"phase": self.phase}

    def attach(self, session_id: str, start_result: dict) -> None:
        """Called by main.py right after the host started an API round."""
        with self.lock:
            if session_id != self.host_session or self.phase != "betting":
                return
            rd = self.manager.rounds.get(start_result.get("round_id"))
            if rd is None:
                return
            self.round = rd
            self.round_no += 1
            self.phase = "running"
            riding = [p["bet_id"] for p in self.players.values() if p["bet"] is not None and p["cashed_at"] is None]
            self.store.attach_crowd_bets(riding, rd["round_id"])
            self._tick()

    def _require_host(self, session_id: str):
        if not self.host_session or session_id != self.host_session:
            raise HTTPException(status_code=403, detail="Only the game screen can do that")

    # ---------- players (phones) ----------

    def join(self, name: str, player_id: str | None = None, age_confirmed: bool = False) -> dict:
        """Seat a phone. With a known player_id (saved on the phone) the same wallet comes back,
        balance included, even after a server restart. age_confirmed records the player's 19+
        confirmation (the phone's join checkbox); without it the phone can watch but not bet."""
        name = " ".join((name or "").split())[:12] or "Kitty fan"
        with self.lock:
            self.last_seen = time.monotonic()
            if player_id and player_id in self.players:
                p = self.players[player_id]
                if age_confirmed:
                    self.store.confirm_age(player_id)
                return {"player_id": player_id, "name": p["name"], "balance": p["balance"]}

            wallet = self.store.get_wallet(player_id) if player_id else None
            if wallet is not None and wallet["kind"] != "phone":
                wallet = None                                   # never seat a PC wallet on a phone
            if len(self.players) >= MAX_PLAYERS:
                raise HTTPException(status_code=400, detail="The room is full")

            if wallet is not None:
                pid, name = player_id, wallet["display_name"] or name
            else:
                pid = uuid.uuid4().hex
            taken = {p["name"].lower() for p in self.players.values()}
            base, n = name, 2
            while name.lower() in taken:
                suffix = str(n)
                name = base[: 12 - len(suffix)] + suffix
                n += 1
            if wallet is None:
                wallet = self.store.ensure_wallet(pid, "phone", name)
            if age_confirmed:
                self.store.confirm_age(pid)
            self.players[pid] = {
                "name": name, "balance": wallet["balance_points"],
                "bet": None, "bet_id": None, "cashed_at": None, "payout": None,
                "best": 0.0, "bonus": None,
            }
            return {"player_id": pid, "name": name, "balance": wallet["balance_points"]}

    def sync_balance(self, pid: str) -> None:
        """Re-read a seated phone's balance after a wallet change outside the table
        (daily reward, simulated purchase, lottery ticket). No-op for anyone else."""
        with self.lock:
            p = self.players.get(pid or "")
            if p is not None:
                p["balance"] = self.store.balance(pid)

    def rename(self, pid: str, name: str) -> None:
        """A seated phone saved a new profile nickname."""
        with self.lock:
            p = self.players.get(pid or "")
            if p is not None:
                p["name"] = name[:12]

    def _player(self, pid: str) -> dict:
        p = self.players.get(pid or "")
        if p is None:
            raise HTTPException(status_code=404, detail="Player not found - join again")
        return p

    def bet(self, pid: str, wager_text: str) -> dict:
        try:
            wager = validate_points(str(wager_text))
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail="Bets are whole points")
        with self.lock:
            self._tick()
            p = self._player(pid)
            if self.phase != "betting":
                raise HTTPException(status_code=400, detail="Bets are closed - wait for the next walk")
            if p["bet"] is not None:
                raise HTTPException(status_code=400, detail="You already bet this round")
            if wager < MIN_BET or wager > MAX_BET:
                raise HTTPException(status_code=400, detail=f"Bet {MIN_BET} to {MAX_BET}")
            bet_id = uuid.uuid4().hex
            p["balance"] = _wallet_call(self.store.place_crowd_bet, pid, bet_id, wager)
            p["bet"] = wager
            p["bet_id"] = bet_id
            p["cashed_at"] = None
            p["payout"] = None
            return {"balance": p["balance"], "bet": wager}

    def cash_out(self, pid: str) -> dict:
        with self.lock:
            self._tick()
            p = self._player(pid)
            if self.phase != "running" or p["bet"] is None or p["cashed_at"] is not None:
                raise HTTPException(status_code=400, detail="Nothing to cash out")
            elapsed = self.clock() - self.round["start_time"]
            if elapsed < (self.round.get("lead_in_seconds") or 0.0):
                raise HTTPException(status_code=400, detail="Kitty is still stretching - wait for the walk!")
            base = _round_multiplier(self.round, elapsed)
            m, bonus = season_bonus(self.round.get("season") or "", self.round.get("events"), elapsed, base)
            payout = calculate_cashout(p["bet"], m)
            p["balance"] = _wallet_call(self.store.settle_crowd_cashout, p["bet_id"], m, payout)
            p["bonus"] = bonus
            p["cashed_at"] = m
            p["payout"] = payout
            p["best"] = max(p["best"], m)
            return {"multiplier": m, "base_multiplier": base, "bonus": bonus,
                    "payout": payout, "balance": p["balance"]}

    # ---------- Schrodinger's Box: player seeds and the opened box ----------

    def seed(self, pid: str, seed: str) -> dict:
        """A rider's own random seed, sent from the phone after it has seen the box's commitment."""
        with self.lock:
            self._tick()
            p = self._player(pid)
            if self.phase != "running" or self.round is None or p["bet"] is None:
                raise HTTPException(status_code=400, detail="Seeds are for riders, while kitty stretches")
            try:
                return self.manager.add_seed(self.round, "phone:" + pid, p["name"], seed)
            except ValueError as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc

    def box(self) -> dict:
        """The opened box of the table's latest walk: the reveal and the server's own checks.
        Phones check it again themselves (hash, seeds, math) with their own copy of the maths."""
        with self.lock:
            self._tick()
            if self.round is None or not self.host_session:
                raise HTTPException(status_code=404, detail="No walk yet")
            if self.phase != "crashed":
                raise HTTPException(status_code=409, detail="The box opens when the cat gets distracted")
            try:
                return self.manager.verify_round(self.host_session, self.round["round_id"])
            except NotRevealedYet as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc
            except KeyError as exc:
                raise HTTPException(status_code=404, detail=str(exc.args[0])) from exc

    # ---------- the big-screen player ----------

    def _peek_forecast(self):
        if not self.host_session:
            return self.forecast
        try:
            self.forecast = self.manager.peek_next_season(self.host_session)
        except (KeyError, AttributeError):
            pass
        return self.forecast

    def _host_rider(self):
        if not self.host_session or self.round is None or self.phase not in ("running", "crashed"):
            return None
        return _rider_from_round(self.host_name, self.round, self.phase)

    def _host_balance(self):
        if not self.host_session:
            return None
        try:
            return float(self.manager.get_balance(self.host_session)["balance"])
        except (KeyError, ValueError, AttributeError):
            return None

    # ---------- shared clock ----------

    def _tick(self):
        if self.phase == "running" and self.round is not None:
            self.manager.finalize_round(self.round)   # lead-in over: the players' seeds make the crash point
            elapsed = self.clock() - self.round["start_time"]
            if elapsed >= self.round["crash_time"]:
                self.phase = "crashed"
                self.store.settle_crowd_crash(self.round["round_id"])
                for p in self.players.values():
                    if p["bet"] is not None and p["cashed_at"] is None:
                        p["payout"] = 0
                self.history = ([round(self.round["crash_point"], 2)] + self.history)[:10]

    def state(self, pid: str = None, lang: str = "en") -> dict:
        with self.lock:
            if pid:
                self.last_seen = time.monotonic()
            self._tick()
            now = self.clock()
            data = {
                "phase": self.phase,
                "round_no": self.round_no,
                "seconds_left": max(0.0, self.closes_at - now) if self.phase == "betting" else 0.0,
                "elapsed": 0.0,
                "multiplier": 1.0,
                "growth_rate": SEASON_GROWTH["spring"],
                "warmup": MIN_CRASH_SECONDS,
                "season": "",
                "rush": False,
                "crash_point": 0.0,
                "distraction": "",
                "join_url": self.join_url or "",
                "history": self.history,
                "forecast": describe_season(self.forecast, lang) if self.forecast and self.phase == "betting" else None,
                "forecast_upcoming": [],
                "rules": None,
                "butterfly": False,
                "leaf_storm": False,
                "cocoa_line": COCOA_LINE,
                "bonus_pct": bonus_percents(),   # phones label bonuses with what the server really pays
            }
            if self.phase == "betting":
                # live: if the game screen changes next walk's season (key N), phones see it at once
                fc = self._peek_forecast()
                data["forecast"] = describe_season(fc, lang) if fc else None
                if self.host_session:
                    try:
                        data["forecast_upcoming"] = self.manager.peek_seasons(self.host_session)
                    except (KeyError, AttributeError):
                        pass
            if self.round is not None and self.phase in ("running", "crashed"):
                elapsed = now - self.round["start_time"]
                data["elapsed"] = max(0.0, elapsed)
                data["growth_rate"] = self.round.get("growth_rate") or SEASON_GROWTH["spring"]
                data["warmup"] = self.round.get("lead_in_seconds") or 0.0
                data["season"] = self.round.get("season") or ""
                data["rush"] = bool(self.round.get("rush"))
                data["rules"] = describe_season(data["season"], lang)
                proof = self.round.get("proof") or {}
                data["commitment"] = proof.get("commitment") or ""
                data["seed_count"] = len(self.round.get("seed_keys") or ())
                data["seed_open"] = self.phase == "running" and not self.round.get("seeds_closed") and elapsed < data["warmup"]
                if self.phase == "running":
                    data.update(season_live(self.round.get("events"), elapsed))
                    data["multiplier"] = _round_multiplier(self.round, elapsed)
                else:
                    data["multiplier"] = data["crash_point"] = round(self.round["crash_point"], 2)
                    data["distraction"] = self.round.get("distraction") or ""

            riders = []
            for p in self.players.values():
                if p["bet"] is None:
                    continue
                if p["cashed_at"] is not None:
                    status = "cashed"
                elif self.phase == "crashed":
                    status = "burned"
                else:
                    status = "riding"
                riders.append({
                    "name": p["name"], "bet": _money(p["bet"]),
                    "cashed_at": p["cashed_at"] or 0.0,
                    "payout": _money(p["payout"]), "status": status,
                    "bonus": p.get("bonus") or "",
                })
            host = self._host_rider()
            if host:
                riders.append(host)
            riders.sort(key=lambda r: -r["bet"])
            data["players"] = riders
            data["player_count"] = len(self.players)

            board = [{"name": p["name"], "balance": float(p["balance"]), "best": p["best"], "host": False} for p in self.players.values()]
            hb = self._host_balance()
            if hb is not None:
                board.append({"name": self.host_name, "balance": hb, "best": 0.0, "host": True})
            board.sort(key=lambda p: p["balance"], reverse=True)
            data["leaderboard"] = board[:5]

            if pid:
                p = self.players.get(pid)
                if p is not None:
                    data["you"] = {
                        "name": p["name"], "balance": float(p["balance"]),
                        "bet": _money(p["bet"]), "cashed_at": p["cashed_at"] or 0.0,
                        "payout": _money(p["payout"]),
                        "bonus": p.get("bonus") or "",
                        "seed_sent": ("phone:" + pid) in ((self.round or {}).get("seed_keys") or ()),
                    }
                else:
                    data["you"] = None
            return data


def _rider_from_round(name: str, rd: dict, phase: str) -> dict:
    """The big-screen player as a rider, from their real server round."""
    if rd.get("status") == "cashed_out":
        return {"name": name, "bet": _money(rd.get("wager")), "cashed_at": rd.get("cashout_multiplier") or 0.0,
                "payout": _money(rd.get("payout")), "status": "cashed", "bonus": rd.get("bonus") or "", "host": True}
    burned = phase == "crashed" or rd.get("status") == "crashed"
    return {"name": name, "bet": _money(rd.get("wager")), "cashed_at": 0.0, "payout": 0.0,
            "status": "burned" if burned else "riding", "bonus": "", "host": True}


# ---------- HTTP routes ----------

class HostBody(BaseModel):
    name: str = Field("", max_length=40)


class OpenBody(BaseModel):
    seconds: float = Field(8.0, ge=0, le=120)


class JoinBody(BaseModel):
    name: str = Field("", max_length=40)
    player_id: str | None = Field(None, max_length=64)   # saved on the phone: rejoin the same wallet
    age_confirmed: bool = False                           # the 19+ checkbox on the join screen


class BetBody(BaseModel):
    player_id: str
    wager: str


class PlayerBody(BaseModel):
    player_id: str


class SeedBody(BaseModel):
    player_id: str
    seed: str = Field(max_length=64)


def make_router(table: CrowdTable) -> APIRouter:
    r = APIRouter()

    @r.get("/crowd", response_class=HTMLResponse, include_in_schema=False)
    def crowd_page():
        return HTMLResponse(HTML_FILE.read_text(encoding="utf-8"), headers={"Cache-Control": "no-store"})

    @r.get("/crowd-assets/{name}", include_in_schema=False)
    def crowd_asset(name: str):
        path = ASSET_DIR / name
        if "/" in name or "\\" in name or ".." in name or not path.is_file():
            raise HTTPException(status_code=404, detail="Not found")
        return FileResponse(path, headers={"Cache-Control": "max-age=3600"})

    @r.post("/api/crowd/host", dependencies=[Depends(guard.host_only)])
    def host(request: Request, body: HostBody | None = None, session_id: str = Header(alias="X-Session-ID")):
        return table.set_host(session_id, request.url.port or 8000, body.name if body else "")

    @r.post("/api/crowd/open", dependencies=[Depends(guard.host_only)])
    def open_betting(body: OpenBody, session_id: str = Header(alias="X-Session-ID")):
        return table.open_betting(session_id, body.seconds)

    @r.post("/api/crowd/join")
    def join(body: JoinBody):
        return table.join(body.name, body.player_id, body.age_confirmed)

    @r.post("/api/crowd/bet")
    def bet(body: BetBody):
        return table.bet(body.player_id, body.wager)

    @r.post("/api/crowd/cashout")
    def cash_out(body: PlayerBody):
        return table.cash_out(body.player_id)

    @r.post("/api/crowd/seed")
    def seed(body: SeedBody):
        return table.seed(body.player_id, body.seed)

    @r.get("/api/crowd/box")
    def box():
        return table.box()

    @r.get("/api/crowd/state")
    def state(player_id: str = "", lang: str = "en"):
        return table.state(player_id, lang[:5])

    @r.websocket("/ws/crowd")
    async def state_stream(ws: WebSocket, player_id: str = "", lang: str = "en"):
        """The same state as /api/crowd/state, pushed up to 5 times a second, only when it changed.
        Phones use this instead of polling and fall back to polling if the socket drops."""
        key = guard.client_key((ws.client.host, ws.client.port) if ws.client else None, ws.headers)
        if not guard.ws_acquire(key):
            await ws.close(code=1013)                 # too many open connections: the phone falls back to polling
            return
        await ws.accept()
        last = None
        try:
            while True:
                data = await asyncio.to_thread(table.state, player_id, lang[:5])
                text = json.dumps(jsonable_encoder(data))
                if text != last:
                    await ws.send_text(text)
                    last = text
                await asyncio.sleep(PUSH_SECONDS)
        except (WebSocketDisconnect, RuntimeError, OSError):
            pass                                  # the phone left or the connection dropped
        finally:
            guard.ws_release(key)

    return r
