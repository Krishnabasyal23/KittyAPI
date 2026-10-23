# KittyAPI: Catino game server

FastAPI server for **Catino**, a crash game where the "crash" is the cat getting distracted.
The server is the source of truth: it decides crash points, seasons, bonuses, payouts and
wallet balances (server clock only). The Unity game and the phones (crowd mode) only display
what it says.

## Run

Requires the `hackathon` conda env (`environment.yml`: Python 3.13, FastAPI, Qiskit, pytest).

```bat
conda activate hackathon
cd D:\hhh\catt\KittyAPI
uvicorn main:app --host 0.0.0.0 --port 8001
```

Or double-click `start_server.bat` in the Unity project root. `--host 0.0.0.0` lets phones on
the same Wi-Fi reach `/crowd`. Optional: `pip install segno` so the game can draw the join QR code.

**v1 and v2 side by side:** v2 runs on port **8001** with its own database
(`data/catino-v2.db`, created and migrated at start-up; see [docs/MIGRATIONS.md](docs/MIGRATIONS.md)).
The v1 demo runs from the `v1.0-pitch` tag on port **8000** with `data/kittycat.db`, which v2
code never opens.

## Environment variables

| Variable | Default | Purpose |
|---|---|---|
| `KITTY_DB_PATH` | `data/catino-v2.db` | v2 SQLite file (wallets, ledger, rounds, receipts). Tests and scripts use their own files under `.tmp`. The v1 file is refused. |
| `DEMO_MODE` | unset | `1` (exactly) enables **simulated** purchases (FakePayment, no money moves) and the cashier page top-ups. Off: those endpoints answer 503. `/api/health` shows `demo_mode` and a notice. |
| `TICKET_SECRET` | unset | Key (16+ characters) that decides which ticket codes are genuine. Unset: ticket entry is off. Keep it out of git. |
| `HAPPY_HOUR` | unset | `1` (exactly) restores the demo season bonuses: butterfly +25%, cocoa +10%. Default is +4% for both, which keeps every strategy under 100% RTP. `/api/health` then shows `"promo": "HAPPY HOUR (demo)"`. Demos only. |
| `GAME_RNG_MODE` | `hybrid` | `hybrid` (simulated quantum XOR `secrets`) or `secure` (`secrets` only). Any other value, including the seeded `classical` mode, stops the server at start-up. |
| `CORS_ORIGINS` | localhost dev ports | Comma-separated browser origins allowed to call the API. |
| `PUBLIC_URL` | LAN IP + port | Base URL put in the crowd-mode join QR (e.g. when behind a tunnel). |
| `PUBLIC_MODE` | unset | `1` (exactly) for a server reachable from the internet: closes the cashier/operator pages, limits abuse, lets only the local game host. See [docs/DEPLOY.md](docs/DEPLOY.md). |
| `ALLOW_PUBLIC_DEMO` | unset | `1` lets `DEMO_MODE=1` simulated purchases work in public mode (points only). |
| `QR_SECRET` | random per start | Key that signs the cashier QR codes. Unset: a new random key at each start, so open codes die on restart (they only last 5 minutes anyway). |
| `KITTY_TEST_TMP` | `<project>/.tmp` | Where tests and scripts put their throwaway databases and logs. |

## The odds (house edge)

The house edge is **4%** (`HOUSE_EDGE` in `crash_engine.py`): the crash point is `clamp(0.96 / u, 1, 100)`, so the chance a round
reaches x is `0.96 / x` and every cash-out target returns 96% on average (median crash 1.92x, about 4% crash instantly at 1.00x).
Season bonuses are capped at +4%, so no strategy returns 100% or more. `/api/health` and the crowd state publish
`house_edge_pct` and `rtp_pct`; the phone Rules sheet, the game's RULES page and the landing page show them in plain words.

## Online

`PUBLIC_MODE=1` plus a tunnel, a container host or a VM puts the phone page and the join QR on a public https
address. Steps, the backup/reset tool and a pre-demo checklist: [docs/DEPLOY.md](docs/DEPLOY.md). From the Unity
project root, `start_tunnel.bat` and `start_public.bat` do the tunnel route on Windows.

## Endpoints

Player endpoints take the wallet id in the `X-Session-ID` header: the PC session id, or a phone's
`player_id`. Balances, wagers and payouts are **whole points**, returned as text (`"50"`).

| Method | Path | What it does |
|---|---|---|
| GET | `/api/health` | Status, version, RNG mode, season bonus sizes, happy-hour flag |
| POST | `/api/sessions` | New PC wallet with the 50-point starting grant |
| POST | `/api/rounds` | Bet whole points (`{"wager": "10"}`; `"10.00"` also accepted, `"2.5"` refused) and start a walk |
| GET | `/api/rounds/{id}/state` | Live multiplier, phase, season events |
| POST | `/api/rounds/{id}/cashout` | Cash out at the current multiplier (+ season bonus) |
| GET | `/api/rounds/{id}/result` | Crash point, distraction and fairness proof once the cat is distracted |
| POST | `/api/rounds/{id}/seed` | Your seed for Schrödinger's Box: `{"seed": 16-64 hex characters}`, sent after the commitment is shown and before the lead-in ends (409 when late or already sent) |
| GET | `/api/rounds/{id}/verify` | "Verify this round": revealed proof plus hash / math / qubit / seeds checks (409 until the crash) |
| GET | `/api/rounds/{id}/season` | Season and event schedule for the game screen |
| GET / POST | `/api/season/next` | Forecast (next walk's season and rule, plus `upcoming`: the next three) / choose the next walk's season (host key N) |
| GET | `/api/player/balance` | Current points |
| GET | `/api/player/history` | Recent rounds |
| GET | `/api/player/transactions` | Ledger (v1 shape) |
| GET | `/api/wallet` | Balance, daily reward / products / lottery availability, today's spend, limits |
| GET | `/api/wallet/ledger` | Append-only ledger entries with `balance_after` |
| GET | `/api/wallet/receipts` | Simulated payment receipts |
| POST | `/api/wallet/daily-free` | +20 points, once per server calendar day (409 after) |
| POST | `/api/wallet/purchase` | `{"product": "topup", "amount_dollars": 1-100, "confirmed": true}` or `{"product": "parking_5", ...}`, **Idempotency-Key required**, `DEMO_MODE=1` only. `confirmed` is the "simulated payment" checkbox. 402 with the receipt if declined |
| POST | `/api/wallet/lottery/redeem` | `{"code": "..."}`, the code typed from a demo ticket: +25 points, one a day. Needs `TICKET_SECRET` |
| POST | `/api/wallet/age-confirm` | `{"confirm": true}`: self-attested 19+ (no ID check). Required before any wager or purchase |
| GET / PUT | `/api/wallet/limits` | Play-safe state and choices / `{"daily_spend_limit_points": n \| null, "reality_check_minutes": 15 \| 30 \| 60}` |
| POST | `/api/wallet/cool-off` | `{"hours": 1 \| 24 \| 168, "confirm": true}`: pause wagers and purchases; extend only |
| POST | `/api/wallet/self-exclude` | `{"days": 30 \| 180 \| 365, "confirm": true}`: block wagers and purchases; cannot be undone early |
| GET | `/api/wallet/activity?since=ISO` | Bets, points wagered / won and net since a moment (reality check) |
| GET / PUT | `/api/profile` | Nickname, cat avatar and play stats / `{"nickname": 2-12 chars, "avatar": "ginger" \| ...}` |
| POST | `/api/profile/qr` | A signed QR code (SVG + PNG) and an 8-character counter code for the cashier. Works once, for 5 minutes |
| GET | `/cashier` | Cashier page: scan or type the player's code, record a simulated counter payment. **Server computer only** (127.0.0.1) |
| POST | `/api/cashier/lookup`, `/api/cashier/topup` | Cashier page actions (`{"code"}`, then `{"code", "amount_dollars", "confirmed": true}`); local only, `DEMO_MODE=1` for top-ups |
| GET | `/operator` | Operator dashboard: live RTP (today, all time, per season), bets, players, top-ups, points, safe-play counts, ledger check, daily charts. **Server computer only**, refreshes every 5 s |
| GET | `/api/operator/summary?days=14` | The dashboard's numbers as JSON (local only) |
| GET | `/api/operator/export/{daily,bets,payments,ledger,audit}.csv` | CSV exports (local only) |
| GET / PUT | `/api/operator/bonus` | Season bonus tuning: `{"butterfly": 0-4, "cocoa": 0-4}` (whole percent). The cap keeps every strategy under 100% RTP; saved, and logged in the audit log (local only) |
| GET | `/api/operator/audit?limit=100&wallet_id=` | Audit log, newest first (local only) |
| GET | `/api/crowd/join-info` | Where phones join and its QR code, without taking the host seat (the web game shows it) |
| GET | `/play` | The web build of the game, when `web/play` exists (Kitty Crash > Build > WebGL in Unity) |
| GET | `/api/leaderboard` | Top balances |
| GET | `/crowd` | Phone page (crowd mode, up to 60 phones) |
| POST | `/api/crowd/host`, `/api/crowd/open` | Big screen: become host, open betting |
| POST / GET | `/api/crowd/seed`, `/api/crowd/box` | A rider's seed (`{"player_id", "seed"}`, only while kitty stretches) / the opened box of the latest walk (409 until the crash); phones re-check it themselves |
| POST | `/api/crowd/join`, `/api/crowd/bet`, `/api/crowd/cashout` | Phone actions (join takes `player_id` to rejoin and `age_confirmed` from the 19+ box) |
| GET | `/api/crowd/state` | Shared table state (`?player_id=&lang=fr`); the game screen polls it |
| WS | `/ws/crowd?player_id=&lang=` | The same state pushed to phones (up to 5 a second, only when it changed). Phones fall back to polling if it drops |

Interactive docs: `http://127.0.0.1:8001/docs`.

## Wallet rules

| Rule | Value | Enforced by |
|---|---|---|
| Starting grant | 50 points, once per wallet | partial unique index on the ledger |
| Daily free | 20 points, once per server calendar day | unique index (wallet, kind, day) |
| Demo top-up | any whole amount $1-$100, 8 points per $1, at most $200 a day (wallet screen and cashier together) | `WalletStore._charge` in the purchase transaction |
| $5 demo parking bundle | 60 points, once per day | unique index (wallet, kind, day) |
| Ticket bonus | 25 points, one ticket per wallet per day, each code once ever; 3 wrong codes in a row pause entry for 1 hour | `lottery_tickets` primary key, `ticket_entry` (migration 0008) |
| Wager | 1-500 whole points, never more than the balance | guarded UPDATE + `CHECK (balance >= 0)` |
| Payout | wager × multiplier (Decimal), rounded **half to even** to whole points | `economy.calculate_cashout` |

- **Atomic:** each operation is one `BEGIN IMMEDIATE` transaction. The wallet update and its
  ledger row are written together, and concurrent requests queue instead of racing.
- **Append-only:** database triggers reject `UPDATE` and `DELETE` on `ledger`. `WalletStore.audit()`
  checks that every balance equals its ledger chain.
- **Idempotent:** send `Idempotency-Key` (8-100 chars). A retry returns the stored response with
  `"replayed": true`. The same key with a different request gets 422.
- **Simulated payments:** `FakePaymentProvider` never contacts a payment network. Every receipt says
  `"simulated": true` with a notice. Real providers would implement `payments.PaymentProvider`.
- **Audit log:** `audit_log` (migration 0009) is append-only like the ledger. It records cashier
  top-ups and codes, play-safe changes (19+, limits, reality check, breaks, self-exclusion), ticket
  locks and operator changes, each in the same transaction as the change itself.
- **Bonus tuning:** a strategy that always earns a bonus returns 96% x (1 + bonus), so bonuses are
  capped at 4% (`season_rules.MAX_SAFE_BONUS_PCT`, derived from the house edge). `HAPPY_HOUR=1` still wins.
- **Cashier codes:** `identity.py` signs `{wallet, nonce, expiry}` with HMAC-SHA256. The
  `qr_tokens` table (migration 0007) makes each code single-use: the top-up and marking the code used
  happen in one transaction. The 8-character counter code under the QR is for typing when there's no
  scanner. The cashier page only answers on the server computer and follows the same rules as the
  wallet screen (19+, cool-off, self-exclusion, daily cap).
- **Ticket bonus:** the player types the code from a demo ticket. Whether a code is genuine
  depends only on the server's secret key (`TICKET_SECRET`), so nothing in the code or the docs lets
  anyone make one. Every code that doesn't verify (wrong, mistyped or already used) gets the same
  answer: "That ticket couldn't be verified". After three in a row, entry pauses for an hour (429).
  The daily limit is checked first, so it never uses up tries. `lottery.LotteryValidator` is
  replaceable. These aren't real lottery tickets and nothing here validates real ones.
- **Play safe (responsible gambling):** `responsible.WalletPolicy` is consulted before every wager
  and purchase. A blocked request returns 403 and moves nothing. `DefaultPolicy` enforces:
  - the 19+ self-attestation;
  - self-exclusion (30/180/365 days) and cool-off (1 h / 1 day / 7 days), both extend-only;
  - the daily spend limit on net wagers. A first or lower limit applies now; raising or removing
    it waits 24 hours (migration 0006).

  The clients show a reality check every 15/30/60 minutes (the player's choice) using
  `/api/wallet/activity`. The phone page and the game's wallet panel are the UI.
- **Crowd mode:** phones play from persistent `phone` wallets. A phone that saved its `player_id`
  rejoins the same wallet after a restart, and bets still open or riding at a restart are refunded.

## Provably fair seal (Schrödinger's Box)

The box is sealed until the cat is "observed" at the crash. `fairness.py` seals every round before anyone
can cash out:

1. **Round start** (`POST /api/rounds`) returns only `commitment = sha256("{round_id}:{value}:{salt}")`.
   `value` is the random integer the crash point comes from; `salt` is secret.
2. **After the crash** (not after your cash-out: the cat keeps walking for everyone else),
   `/result` and `/verify` reveal `value`, `salt`, `bits`, the qubit samples and the classical mask.
3. **Player seeds.** While kitty stretches (the 3 s lead-in) every rider's device sends a random seed,
   made after it saw the commitment. The crash point comes from
   `final_value = value XOR mask(seeds)`, where `mask` is the top `bits` bits of
   `sha256("{round_id}:seeds:{seed1},{seed2},...")`. The server committed to `value` before any seed
   existed, and nobody sends a seed knowing `value`, so neither side can steer the result: it is uniform if
   either side is honest (same idea as the quantum XOR mask). The crash point is provisional until the
   lead-in ends and nothing can crash before then. With no seeds, `final_value == value`.
4. **Anyone can check:**
   - the hash matches the published commitment;
   - `final_value == value XOR mask(seeds)` for the published seeds (the phones also check their own seed is in);
   - `crash = clamp(0.96 / ((final_value + 0.5) / 2^bits), 1, 100)`;
   - in hybrid mode, `value == concat(quantum_samples) XOR classical_mask`.

Limits: the server could still leave a seed out of the list (a phone notices when its own is missing) or
cancel a round it dislikes (open bets are refunded). A seed sent after the lead-in is refused.

Seasons never touch the sealed value: Summer Zoomies and season events use separate draws, and
bonuses only raise the cash-out multiplier. The JSON matches `QuantumProof` in the Unity client
(`Fairness.cs`), which runs the same checks in the quantum box (key Q).

Seals and seeds are stored with each round (migrations 0003 and 0010), so `/verify` works after a restart. A reloaded
round stays sealed until its crash time on the wall clock. A refunded (interrupted) round opens at once.

## Tests and tools

Run from this folder with the env active. Tests always use a temp database, never `data/kittycat.db`.

```bat
python -m pytest -q --basetemp=D:\hhh\catt\.tmp\pytest
python -m database.migrate status REM which migrations the v2 database has
python tests\sim_rtp.py           REM 400k-round RTP per cash-out target and season strategy
python tests\sim_rtp.py --happy-hour  REM same with the HAPPY_HOUR demo bonuses
python tests\load_test.py         REM real server on 8001 + 60 fake phones, reports errors and latency
python tests\phone_ui_test.py     REM real /crowd page in headless Chrome (needs Chrome), PASS/FAIL per step
```

If numpy crashes on import with `0xc06d007f`, the env is not activated: use `conda activate hackathon`,
or add `D:\Anaconda\envs\hackathon\Library\bin` to `PATH`.

## Credits

See the Catino README for credits.
