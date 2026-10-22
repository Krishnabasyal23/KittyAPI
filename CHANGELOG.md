# Changelog

All notable changes to KittyAPI. Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

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
- Mock lottery tickets behind `DEMO_MODE=1`: server-issued ids, five doubling two-digit
  numbers checked on the server, 25 points, max 3 per wallet per day, each id once ever.
- Idempotency keys for rewards, purchases and ticket redemptions.
- Responsible-gambling backend hooks: replaceable `WalletPolicy`, `rg_settings` (self-exclusion,
  cool-off, daily spend limit, reality-check interval, age confirmation). No UI yet.
- Fairness seals persisted with each round; `/verify` works after a restart.
- Endpoints `/api/wallet`, `/ledger`, `/receipts`, `/daily-free`, `/purchase`, `/lottery/redeem`,
  `/api/demo/lottery/tickets`. `/api/health` adds `min_bet`, `max_bet`, `db_schema`, `demo_mode`.

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

Frozen release shown at the pitch.
