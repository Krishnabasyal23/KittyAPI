# Changelog

All notable changes to KittyAPI. Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Added
- `/play` serves the Unity WebGL build of the game from `web/play`, on the same address as the API (responses are gzip-compressed).
  `GET /api/crowd/join-info` gives the join address and QR code without taking the host seat.
- Auto host (`AUTO_HOST=1`, `autohost.py`): a house table that runs by itself. The server's own wallet ("House Cat") takes the host
  seat, opens betting, bets one point, runs the walk and starts again while a phone is watching, so a hosted server plays without
  the Unity game. It steps aside for a real host and takes the seat back after 60 s of stillness. Seven tests.

## [2.0.0-beta.3] - 2026-10-22

### Added
- Public mode (`PUBLIC_MODE=1`) for a server that is reachable from the internet, see docs/DEPLOY.md. The
  cashier, operator pages and demo helpers close completely (a tunnel forwards from 127.0.0.1, so "local"
  is no proof), only the game on the server computer may host the crowd table, every visitor is rate limited
  by their real address (wallet creation, join, bets, seeds), request bodies are capped, WebSockets are
  limited per visitor and in total, and simulated purchases need `ALLOW_PUBLIC_DEMO=1` as well as `DEMO_MODE=1`.
- The cashier and operator pages now also refuse any request that carries proxy headers or a non-local Host.
- Safe response headers on every reply (nosniff, no-referrer, frame deny).
- `tools/db_tools.py`: backup (safe while running), reset (renames the file, never deletes) and list.
- Deploy files: `requirements.txt`, `Dockerfile`, `render.yaml`, `fly.toml`, `Caddyfile`, `.env.example`.
- Player seeds (Schrödinger's Box). The server still commits to its value first; every rider's device then
  sends a random seed during the 3 s lead-in and the crash point comes from `value XOR mask(seeds)`, so
  neither the server nor a player can steer it. `POST /api/rounds/{id}/seed`, `POST /api/crowd/seed`,
  `GET /api/crowd/box`; the reveal adds `seeds` and `final_value`, and `/verify` a fourth check, `seeds`.
  Migration 0010 stores both. Phones send their seed on their own, show a "Schrödinger's Box" strip and
  re-check the opened box in the browser (own SHA-256, plain http has no crypto.subtle).
- Crowd state adds `commitment`, `seed_count`, `seed_open` and `you.seed_sent`.
- Test: the phone's season card and cash button, the health check and the rule texts show the
  operator's tuned bonus at once.

## [2.0.0-beta.2] - 2026-10-22

### Added
- Phone picture tour: how to play in four animated slides with no words (bet, the walk, cash out in
  time, distracted). Shown once after the first join and from Menu > How to play; the written rules
  moved to Menu > Rules.
- `/api/health` reports version `2.0.0-beta.2`.
- Phones get the crowd state over a WebSocket (`/ws/crowd`) instead of polling every 0.3 s; they go
  back to polling while the socket is down and reconnect every 3 s.
- Append-only audit log (migration 0009): cashier top-ups and codes, play-safe changes, ticket
  locks, operator changes. Shown on the dashboard, `/api/operator/audit`, `audit.csv`.
- Season bonus tuning on the dashboard (`/api/operator/bonus`): 0-4% each, the cap derived from
  the house edge so no strategy reaches 100% RTP. Saved in `settings` and loaded at start-up.

## [2.0.0-beta.1] - 2026-10-22

First beta of v2.

### Changed
- The API and `/api/health` call the game "Catino". `/api/health` reports version `2.0.0-beta.1`.

### Added
- Season rule texts in French: `GET/POST /api/season/next?lang=fr` and `/api/crowd/state?lang=fr`
  (bonus numbers are still the server's).
- Phone page: the wallet, checkout, receipts, history, profile, play-safe, out-of-points and
  reality-check sheets, and common server messages, follow the French setting.

## [2.0.0-alpha.12] - 2026-10-22

English / French.

### Added
- Phone page strings table (English / French, saved per phone): join screen, game status, bet and
  cash-out buttons, menu, how to play, safe play, demo reset. Switch on the join screen or in the menu.
  Wallet sheets and the server's season rule texts are still English only.
- `/api/health` reports version `2.0.0-alpha.12`.

## [2.0.0-alpha.11] - 2026-10-22

Three-season forecast.

### Added
- `GET /api/season/next` adds `upcoming`: the next three walks' seasons with their rules. They are
  rolled in advance per session and used in that order; the host's pick (key N) changes only the next
  one. Crowd state adds `forecast_upcoming`; the phone shows "Then: ..." under the forecast.

### Changed
- Docs no longer describe the season bonuses in terms of a past event.
- `/api/health` reports version `2.0.0-alpha.11`.

## [2.0.0-alpha.10] - 2026-10-22

Operator dashboard.

### Added
- `/operator` (server computer only): live RTP today / all time / per season, bets, players (active
  today, PC and phone), top-ups by source, points in wallets, safe-play counts, ledger audit, daily
  charts (wagered vs paid back, RTP, top-ups) and a daily table. Light and dark themes, no external files.
- `/api/operator/summary` and CSV exports (`daily`, `bets`, `payments`, `ledger`), local only.
- `reports.py`: read-only report queries. Only settled bets count (interrupted and refunded don't).
- `/api/health` reports version `2.0.0-alpha.10`.

## [2.0.0-alpha.9] - 2026-10-22

Safe play page.

### Added
- Phone menu > Safe play: short plain-language advice (points have no cash value, each walk is
  random, the game pays back less than you bet on average, take breaks), the limits and breaks
  tools, and helplines: ConnexOntario 1-866-531-2600 / text CONNEX to 247247 (24/7) and 9-8-8
  (call or text, 24/7). Numbers checked on connexontario.ca and 988.ca on 2026-10-22.
- `/api/health` reports version `2.0.0-alpha.9`.

## [2.0.0-alpha.8] - 2026-10-22

Tidier phone page. (2.0.0-alpha.7 was a game-only release.)

### Changed
- Phone page: a Menu sheet (wallet, profile and cashier code, safe play, how to play, new demo
  player in `DEMO_MODE`) replaces the play-safe button; "This walk" and "Leaderboard" fold away
  (the leaderboard starts folded).
- `/api/health` reports version `2.0.0-alpha.8`.

## [2.0.0-alpha.6] - 2026-10-22

Ticket bonus with a secret key.

### Changed
- Ticket codes are typed (no picker) and checked against a secret key from `TICKET_SECRET`.
  Without the key, ticket entry is off. `POST /api/wallet/lottery/redeem` takes `{"code"}`.
- One ticket per wallet per day (was 3), still 25 points.
- Every code that doesn't verify gets "That ticket couldn't be verified".
- Three wrong codes in a row pause ticket entry for an hour (429; migration 0008 `ticket_entry`).
- The ticket helper endpoint is hidden from the API docs, local only and `DEMO_MODE=1` only.
- Settings and secrets can live in an untracked `KittyAPI/.env.local` (`local_env.py`).
- `/api/health` reports version `2.0.0-alpha.6`.

## [2.0.0-alpha.5] - 2026-10-22

Phase 4: profiles, signed single-use QR codes and the cashier page.

### Added
- `GET/PUT /api/profile`: nickname, cat avatar and play stats (rounds, cash-outs, best, net).
- `POST /api/profile/qr`: an HMAC-signed QR code (SVG + PNG) plus an 8-character counter code.
  Each code works once and expires after 5 minutes (`qr_tokens`, migration 0007). `QR_SECRET` sets the key.
- `/cashier`: local-only page to look up a player's code and record a simulated counter top-up.
- Phone page: profile sheet with the cashier code and a countdown.

### Changed
- Top-ups take any whole amount: $1-$100 each, 8 points per $1, at most $200 a day (wallet and
  cashier together). `{"product": "topup", "amount_dollars": n}`; `topup_5` still works.
- Migration 0007 rebuilds `payments` (rows copied) so the product and amount checks allow this.
- `/api/health` reports version `2.0.0-alpha.5`.

## [2.0.0-alpha.4] - 2026-10-20

Phase 3: wallet UI and play-safe tools.

### Added
- Play-safe endpoints: `/api/wallet/age-confirm`, `/api/wallet/limits` (GET/PUT), `/cool-off`,
  `/self-exclude`, `/activity`. Each change needs an explicit `confirm`.
- Migration 0006: a raised or removed daily spend limit waits 24 hours; lowering applies at once.
- Phone page: 19+ checkbox on join, wallet sheet (daily points, demo packs, mock ticket entry),
  demo checkout (generic methods, simulated-payment checkbox, receipts), history, play-safe sheet
  (limit, reality check, cool-off, self-exclusion, helpline), out-of-points sheet, reality check.
- `tests/phone_ui_test.py`: the phone page end to end in headless Chrome.

### Changed
- **19+ confirmation is required before any wager or purchase** (rewards and tickets stay open).
- Simulated purchases also need `"confirmed": true` (the checkout checkbox).
- Cool-off and self-exclusion can be extended but never shortened.
- `/api/crowd/join` takes `age_confirmed`. Wallet rewards update a seated phone's balance at once.
- `/api/health` reports version `2.0.0-alpha.4`.

### Fixed
- The phone page showed a second, never-updating mood box.

## [2.0.0-alpha.3] - 2026-10-20

Phase 2: persistent wallet backend.

### Added
- New v2 database `data/catino-v2.db` (`KITTY_DB_PATH` overrides), built by versioned, checksummed
  migrations 0001-0005 with automatic pre-upgrade backups and documented non-destructive rollback
  (`docs/MIGRATIONS.md`, `python -m database.migrate status|up`).
- Wallets with an append-only ledger: 50-point starting grant once, daily free 20 points once
  per server day, `audit()` for ledger consistency.
- Simulated purchases behind `DEMO_MODE=1`, Idempotency-Key required: $5 top-up = 40 points, $5
  parking bundle = 60 points once per day. FakePayment provider, receipts labelled simulated.
- Mock lottery tickets behind `DEMO_MODE=1` (replaced in 2.0.0-alpha.6): 25 points, max 3 per
  wallet per day, each ticket once ever.
- Idempotency keys for rewards, purchases and ticket redemptions.
- Responsible-gambling backend hooks: replaceable `WalletPolicy`, `rg_settings` (self-exclusion,
  cool-off, daily spend limit, reality-check interval, age confirmation). No UI yet.
- Fairness seals persisted with each round; `/verify` works after a restart.
- Endpoints `/api/wallet`, `/ledger`, `/receipts`, `/daily-free`, `/purchase`, `/lottery/redeem`.
  `/api/health` adds `min_bet`, `max_bet`, `db_schema`, `demo_mode`.

### Changed
- **Balances are whole points** (were 2-decimal credits). Starting balance 50 (was 1000). Payouts
  are wager × multiplier rounded half to even. Fractional wagers are refused.
- Crowd-mode phones use persistent wallets and rejoin them by `player_id`. Bet chips are 1/5/10/25.
- v2 never opens the v1 database (`data/kittycat.db`). The default server port is 8001.
- `/api/health` reports version `2.0.0-alpha.3`.

## [2.0.0-alpha.2] - 2026-10-20

Phase 1b: fairness seal and bonus safety.

### Added
- SHA-256 commit-and-reveal seal (`fairness.py`, restored from `a9ac9d9`). Round start
  publishes the commitment; `/result` reveals the proof after the crash.
- `GET /api/rounds/{id}/verify`: "Verify this round" (409 until the crash, 404 if no proof was kept).
- `HAPPY_HOUR=1` env flag for demo bonuses; `/api/health` shows `bonus_pct`, `happy_hour` and
  `"promo": "HAPPY HOUR (demo)"`.
- `bonus_pct` in `/api/rounds/{id}/season` and `/api/crowd/state`.
- `tests/sim_rtp.py --happy-hour` and a 95% margin of error per result.

### Changed
- **Season bonuses default to +4%** (were +25% butterfly / +10% cocoa). Worst-case strategy RTP
  is now 99.84% instead of about 119%.
- `/result` keeps the crash point sealed until the crash time, even after an early cash-out.
- Phone page and rule texts show the bonus the server really pays.
- `/api/health` reports version `2.0.0-alpha.2`.

## [2.0.0-alpha.1] - 2026-10-20

Phase 1a: safety groundwork for v2. The v1 demo (`v1.0-pitch`) is unchanged.

### Added
- `KITTY_DB_PATH` environment variable for the SQLite file (default `data/kittycat.db`).
- `tests/sim_rtp.py`: 400k-round RTP simulation using the server's own crash and season functions.
- `tests/load_test.py`: real server on port 8001 with 60 fake phones; reports errors and latency.
- README and this changelog.

### Changed
- `/api/health` reports version `2.0.0-alpha.1`.
- pytest runs against a throwaway database under `.tmp/pytest-db` and refuses the v1 file.

### Security
- Seeded `classical` and bare `quantum` RNG modes are refused for live rounds, both at server
  start-up and in `RoundManager.start_round`.
- `.gitignore` covers databases, `.env*`, keys, certificates, logs and scratch folders everywhere.

## [1.0-pitch]

Frozen v1 release.
