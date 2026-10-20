# KittyAPI: Catino game server

FastAPI server for **Catino**, a crash game where the "crash" is the cat getting distracted.
The server is the source of truth: it decides crash points, seasons, bonuses and payouts
(Decimal credits, payout = wager × multiplier, server clock only). The Unity game and the
phones (crowd mode) only display what it says.

## Run

Requires the `hackathon` conda env (`environment.yml`: Python 3.13, FastAPI, Qiskit, pytest).

```bat
conda activate hackathon
cd D:\hhh\catt\KittyAPI
uvicorn main:app --host 0.0.0.0 --port 8000
```

Or double-click `start_server.bat` in the Unity project root. `--host 0.0.0.0` lets phones on
the same Wi-Fi reach `/crowd`. Optional: `pip install segno` so the game can draw the join QR code.

**v1 and v2 side by side:** the v1 demo runs on port **8000** with `data/kittycat.db`. Run v2 on
port **8001** with its own database:

```bat
set KITTY_DB_PATH=D:\hhh\catt\.tmp\v2\kittycat-v2.db
uvicorn main:app --host 0.0.0.0 --port 8001
```

## Environment variables

| Variable | Default | Purpose |
|---|---|---|
| `KITTY_DB_PATH` | `data/kittycat.db` | SQLite file for sessions, rounds and the ledger. Point tests, scripts and v2 elsewhere. |
| `GAME_RNG_MODE` | `hybrid` | `hybrid` (simulated quantum XOR `secrets`) or `secure` (`secrets` only). Any other value, including the seeded `classical` mode, stops the server at start-up. |
| `CORS_ORIGINS` | localhost dev ports | Comma-separated browser origins allowed to call the API. |
| `PUBLIC_URL` | LAN IP + port | Base URL put in the crowd-mode join QR (e.g. when behind a tunnel). |
| `KITTY_TEST_TMP` | `<project>/.tmp` | Where tests and scripts put their throwaway databases and logs. |

## Endpoints

Player endpoints take the session in the `X-Session-ID` header.

| Method | Path | What it does |
|---|---|---|
| GET | `/api/health` | Status, version, RNG mode |
| POST | `/api/sessions` | New player session with starting credits |
| POST | `/api/rounds` | Bet (`{"wager": "10.00"}`) and start a walk |
| GET | `/api/rounds/{id}/state` | Live multiplier, phase, season events |
| POST | `/api/rounds/{id}/cashout` | Cash out at the current multiplier (+ season bonus) |
| GET | `/api/rounds/{id}/result` | Crash point and distraction once the round is over |
| GET | `/api/rounds/{id}/season` | Season and event schedule for the game screen |
| GET / POST | `/api/season/next` | Forecast / choose the next walk's season (host key N) |
| GET | `/api/player/balance` | Current credits |
| GET | `/api/player/history` | Recent rounds |
| GET | `/api/player/transactions` | Credit ledger |
| GET | `/api/leaderboard` | Top balances |
| GET | `/crowd` | Phone page (crowd mode, up to 60 phones) |
| POST | `/api/crowd/host`, `/api/crowd/open` | Big screen: become host, open betting |
| POST | `/api/crowd/join`, `/api/crowd/bet`, `/api/crowd/cashout` | Phone actions |
| GET | `/api/crowd/state` | Shared table state (phones poll this) |

Interactive docs: `http://127.0.0.1:8000/docs`.

## Tests and tools

Run from this folder with the env active. Tests always use a temp database, never `data/kittycat.db`.

```bat
python -m pytest -q --basetemp=D:\hhh\catt\.tmp\pytest
python tests\sim_rtp.py           REM 400k-round RTP per cash-out target and season strategy
python tests\load_test.py         REM real server on 8001 + 60 fake phones, reports errors and latency
```

If numpy crashes on import with `0xc06d007f`, the env is not activated: use `conda activate hackathon`,
or add `D:\Anaconda\envs\hackathon\Library\bin` to `PATH`.

## Credits

See the Catino README for credits.
