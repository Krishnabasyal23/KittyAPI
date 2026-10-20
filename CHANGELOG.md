# Changelog

All notable changes to KittyAPI. Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

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
