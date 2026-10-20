"""Crowd-mode load test: the real server, 60 fake phones and a fake big screen.

Run from KittyAPI (inside the hackathon conda env):
    python tests/load_test.py               # 60 phones for 60 seconds
    python tests/load_test.py --seconds 20  # shorter run

Starts `uvicorn main:app` on port 8001 with a throwaway database under D:\\hhh\\catt\\.tmp\\load
(never port 8000, never data/kittycat.db), then:
  * the big screen creates a session, becomes crowd host, opens betting, starts a round and
    polls it until the cat gets distracted, over and over;
  * each phone joins, polls /api/crowd/state 3 times a second, bets while betting is open and
    cashes out at its own random target.
Reports errors (connection failures and 5xx), rejected requests (4xx) and response times, then
shuts the server down.
"""

import argparse
import asyncio
import os
import random
import socket
import statistics
import subprocess
import sys
import time

from collections import Counter, defaultdict
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
TMP = Path(os.getenv("KITTY_TEST_TMP") or ROOT.parent / ".tmp") / "load"
V1_DB = (ROOT / "data" / "kittycat.db").resolve()

PORT = 8001
PHONES = 60
POLL_HZ = 3
BETTING_SECONDS = 4.0


class Stats:
    def __init__(self):
        self.latency = defaultdict(list)  # endpoint -> seconds
        self.errors = Counter()           # "endpoint: reason"
        self.rejected = Counter()         # "endpoint: detail" (4xx, game said no)
        self.rounds = 0
        self.bets = 0
        self.cashouts = 0


async def call(client: httpx.AsyncClient, stats: Stats, label: str, method: str, url: str, **kw):
    started = time.perf_counter()
    try:
        response = await client.request(method, url, **kw)
    except httpx.HTTPError as exc:
        stats.errors[f"{label}: {type(exc).__name__}"] += 1
        return None
    stats.latency[label].append(time.perf_counter() - started)

    if response.status_code >= 500:
        stats.errors[f"{label}: HTTP {response.status_code}"] += 1
        return None
    if response.status_code >= 400:
        try:
            detail = response.json().get("detail")
        except ValueError:
            detail = response.text[:60]
        stats.rejected[f"{label}: {detail}"] += 1
        return None
    return response.json()


async def big_screen(client, stats, stop_at):
    session = await call(client, stats, "POST /api/sessions", "POST", "/api/sessions")
    if session is None:
        return
    headers = {"X-Session-ID": session["session_id"]}
    await call(client, stats, "POST /api/crowd/host", "POST", "/api/crowd/host",
               json={"name": "Load Test"}, headers=headers)

    while time.monotonic() < stop_at:
        await call(client, stats, "POST /api/crowd/open", "POST", "/api/crowd/open",
                   json={"seconds": BETTING_SECONDS}, headers=headers)
        await asyncio.sleep(BETTING_SECONDS)

        started = await call(client, stats, "POST /api/rounds", "POST", "/api/rounds",
                             json={"wager": "1.00"}, headers=headers)
        if started is None:
            await asyncio.sleep(1)
            continue
        stats.rounds += 1

        while True:
            state = await call(client, stats, "GET /api/rounds/{id}/state", "GET",
                               f"/api/rounds/{started['round_id']}/state", headers=headers)
            if state is None or state["status"] != "running":
                break
            await asyncio.sleep(1 / POLL_HZ)
        await asyncio.sleep(1.0)


async def phone(client, stats, number, stop_at):
    await asyncio.sleep(random.random())  # people don't all scan the QR in the same millisecond
    joined = await call(client, stats, "POST /api/crowd/join", "POST", "/api/crowd/join",
                        json={"name": f"Phone{number}"})
    if joined is None:
        return
    pid = joined["player_id"]
    target = None

    while time.monotonic() < stop_at:
        state = await call(client, stats, "GET /api/crowd/state", "GET", "/api/crowd/state",
                           params={"player_id": pid})
        you = (state or {}).get("you") or {}

        if state and state["phase"] == "betting" and not you.get("bet"):
            target = random.uniform(1.2, 3.0)
            if await call(client, stats, "POST /api/crowd/bet", "POST", "/api/crowd/bet",
                          json={"player_id": pid, "wager": str(random.randint(1, 5))}):
                stats.bets += 1

        elif (
            state and state["phase"] == "running" and you.get("bet") and not you.get("cashed_at")
            and state["elapsed"] >= state["warmup"] and state["multiplier"] >= (target or 99)
        ):
            if await call(client, stats, "POST /api/crowd/cashout", "POST", "/api/crowd/cashout",
                          json={"player_id": pid}):
                stats.cashouts += 1

        await asyncio.sleep(1 / POLL_HZ)


def port_free(port: int) -> bool:
    with socket.socket() as s:
        return s.connect_ex(("127.0.0.1", port)) != 0


def start_server(db_path: Path, log_path: Path) -> subprocess.Popen:
    env = dict(os.environ)
    env["KITTY_DB_PATH"] = str(db_path)
    # conda env DLLs (numpy/qiskit) when the env isn't activated
    prefix = Path(sys.prefix)
    extra = [prefix, prefix / "Library" / "mingw-w64" / "bin", prefix / "Library" / "usr" / "bin",
             prefix / "Library" / "bin", prefix / "Scripts"]
    env["PATH"] = os.pathsep.join([*map(str, extra), env.get("PATH", "")])

    log = open(log_path, "w", encoding="utf-8")
    return subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "main:app", "--host", "127.0.0.1", "--port", str(PORT),
         "--log-level", "warning"],
        cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT,
    )


def wait_ready(server: subprocess.Popen, timeout: float = 90) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if server.poll() is not None:
            raise RuntimeError("server exited during start-up (see the server log)")
        try:
            if httpx.get(f"http://127.0.0.1:{PORT}/api/health", timeout=2).status_code == 200:
                return
        except httpx.HTTPError:
            pass
        time.sleep(0.5)
    raise RuntimeError("server did not answer /api/health in time")


def report(stats: Stats, seconds: float) -> None:
    all_times = sorted(t for times in stats.latency.values() for t in times)
    total = len(all_times) + sum(c for k, c in stats.errors.items() if "HTTP" not in k)
    errors = sum(stats.errors.values())

    print(f"\n{PHONES} phones, {seconds:.0f}s, {stats.rounds} rounds, {stats.bets} bets, "
          f"{stats.cashouts} cash-outs, {total} requests ({total / seconds:.0f}/s)")
    print(f"ERRORS (connection / 5xx): {errors}")
    for key, count in stats.errors.most_common():
        print(f"    {count:6d}  {key}")
    print(f"Rejected by game rules (4xx): {sum(stats.rejected.values())}")
    for key, count in stats.rejected.most_common():
        print(f"    {count:6d}  {key}")

    def ms(values, q):
        return statistics.quantiles(values, n=100)[q - 1] * 1000 if len(values) > 1 else values[0] * 1000

    if all_times:
        print(f"Response time, all requests: median {statistics.median(all_times) * 1000:.1f} ms, "
              f"p95 {ms(all_times, 95):.1f} ms, max {all_times[-1] * 1000:.1f} ms")
    print(f"\n    {'endpoint':<30} {'count':>7} {'median ms':>10} {'p95 ms':>8}")
    for label, times in sorted(stats.latency.items()):
        print(f"    {label:<30} {len(times):7d} {statistics.median(times) * 1000:10.1f} {ms(times, 95):8.1f}")


async def run(seconds: float) -> Stats:
    stats = Stats()
    stop_at = time.monotonic() + seconds
    limits = httpx.Limits(max_connections=PHONES + 10, max_keepalive_connections=PHONES + 10)
    async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{PORT}", timeout=10, limits=limits) as client:
        await asyncio.gather(
            big_screen(client, stats, stop_at),
            *(phone(client, stats, n + 1, stop_at) for n in range(PHONES)),
        )
    return stats


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--seconds", type=float, default=60)
    args = parser.parse_args()

    if PORT == 8000:
        sys.exit("refusing to load-test port 8000 (the v1 demo)")
    if not port_free(PORT):
        sys.exit(f"port {PORT} is already in use; stop that server first")

    TMP.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    db_path = TMP / f"kittycat-load-{stamp}.db"
    log_path = TMP / f"server-{stamp}.log"
    if db_path.resolve() == V1_DB:
        sys.exit("refusing to use the v1 database")

    print(f"Starting server on port {PORT} (db {db_path}, log {log_path}) ...")
    server = start_server(db_path, log_path)
    try:
        wait_ready(server)
        print(f"Server up. Running {PHONES} phones for {args.seconds:.0f}s ...")
        stats = asyncio.run(run(args.seconds))
        report(stats, args.seconds)
    finally:
        server.terminate()
        try:
            server.wait(timeout=10)
        except subprocess.TimeoutExpired:
            server.kill()
        print("\nServer stopped.")


if __name__ == "__main__":
    main()
