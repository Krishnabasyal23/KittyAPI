"""A house table that runs by itself, so a hosted server can play without the Unity game.

AUTO_HOST=1 starts it. It does what the big-screen game does for the crowd table: it takes the host seat
with its own wallet ("House Cat"), opens betting, bets one point and starts the walk, waits for the crash,
shows the result for a moment and starts again. It only runs while someone is watching (a phone asked
for the table state in the last IDLE_AFTER seconds), so an empty room costs nothing.

If the Unity game takes the host seat, the house steps aside and takes it back only after the table
has stood still for TAKEOVER_AFTER seconds.
"""

import os
import threading
import time

from decimal import Decimal

HOUSE_NAME = "House Cat"


class AutoHost:
    def __init__(self, manager, table, rng_mode="hybrid", port=8001, wager=1, betting_seconds=15.0,
                 result_seconds=12.0, idle_after=45.0, takeover_after=60.0, name=HOUSE_NAME,
                 sleep=time.sleep, now=time.monotonic):
        self.manager = manager
        self.table = table
        self.store = table.store
        self.rng_mode = rng_mode
        self.port = port
        self.wager = wager
        self.betting_seconds = betting_seconds
        self.result_seconds = result_seconds
        self.idle_after = idle_after
        self.takeover_after = takeover_after
        self.name = name
        self.sleep = sleep
        self.now = now
        self.sid = None
        self.rounds = 0
        self._foreign = None            # (snapshot, since) of another host's table

    # ---------- the host seat ----------

    def _can_afford(self) -> bool:
        if self.sid is None:
            return False
        try:
            return Decimal(str(self.manager.get_balance(self.sid)["balance"])) >= self.wager
        except (KeyError, ValueError):
            return False

    def _take_seat(self) -> None:
        if not self._can_afford():                      # a fresh wallet when the old one is spent
            self.sid = self.manager.create_session()["session_id"]
            self.store.confirm_age(self.sid)
        self.table.set_host(self.sid, self.port, self.name)

    def ensure_host(self) -> bool:
        """True when the house holds the host seat (taking it when free or abandoned)."""
        t = self.table
        with t.lock:
            if t.host_session is not None and t.host_session == self.sid:
                self._foreign = None
                if not self._can_afford() and t.phase != "running":
                    self._take_seat()
                return True
            if t.host_session is None:
                self._take_seat()
                return True
            snapshot = (t.host_session, t.phase, t.round_no, round(t.closes_at, 1))
            if self._foreign is None or self._foreign[0] != snapshot:
                self._foreign = (snapshot, self.now())
                return False
            if self.now() - self._foreign[1] >= self.takeover_after:
                self._foreign = None
                self._take_seat()
                return True
            return False

    # ---------- one round ----------

    def watched(self) -> bool:
        return self.now() - self.table.last_seen <= self.idle_after

    def _wait_for_crash(self, timeout: float = 150.0) -> None:
        waited = 0.0
        while waited < timeout and self.table.state()["phase"] == "running":
            self.sleep(0.5)
            waited += 0.5

    def cycle(self) -> bool:
        """Run one whole round if someone is watching. Returns True when a round was played."""
        if not self.watched() or not self.ensure_host():
            return False
        self._wait_for_crash()                          # a walk that is still running finishes first
        self.table.open_betting(self.sid, self.betting_seconds)
        self.sleep(self.betting_seconds)
        if not self.ensure_host():
            return False
        result = self.manager.start_round(self.sid, self.wager, rng_mode=self.rng_mode)
        self.table.attach(self.sid, result)
        self.rounds += 1
        self._wait_for_crash()
        self.sleep(self.result_seconds)
        return True

    def run(self, stop: threading.Event) -> None:
        while not stop.is_set():
            try:
                if not self.cycle():
                    stop.wait(2.0)
            except Exception as exc:                    # keep the table alive whatever happens
                print(f"auto host: {type(exc).__name__}: {exc}", flush=True)
                stop.wait(3.0)


def start_if_enabled(manager, table, rng_mode: str):
    """Start the house table in a background thread when AUTO_HOST=1. Returns (thread, stop event) or None."""
    if os.getenv("AUTO_HOST", "").strip() != "1":
        return None
    port = int(os.getenv("PORT") or 8001)
    host = AutoHost(manager, table, rng_mode=rng_mode, port=port)
    stop = threading.Event()
    thread = threading.Thread(target=host.run, args=(stop,), name="auto-host", daemon=True)
    thread.start()
    return thread, stop
