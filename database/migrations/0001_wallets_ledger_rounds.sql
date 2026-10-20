-- 0001: wallets, append-only ledger, host rounds and crowd bets (fresh v2 database).
--
-- Points are whole numbers (INTEGER). Every balance change is one ledger row written in the same
-- transaction as the wallet update; balance_after lets an audit replay the chain.
--
-- Rollback (non-destructive): this migration creates a new database file, so roll back by pointing
-- KITTY_DB_PATH (or the code version) elsewhere and keeping the file for inspection. The v1
-- database (data/kittycat.db) is never opened by the v2 code. Full procedure: docs/MIGRATIONS.md.

CREATE TABLE wallets (
    wallet_id      TEXT PRIMARY KEY,
    kind           TEXT NOT NULL CHECK (kind IN ('pc', 'phone')),
    display_name   TEXT,
    balance_points INTEGER NOT NULL DEFAULT 0 CHECK (balance_points >= 0),
    created_at     TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE TABLE ledger (
    entry_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    wallet_id     TEXT NOT NULL REFERENCES wallets(wallet_id),
    kind          TEXT NOT NULL CHECK (kind IN (
                      'starting_grant', 'daily_free', 'topup', 'parking_bundle',
                      'lottery_ticket', 'wager', 'payout', 'refund')),
    amount_points INTEGER NOT NULL CHECK (amount_points != 0),
    balance_after INTEGER NOT NULL CHECK (balance_after >= 0),
    day           TEXT NOT NULL,              -- server calendar day (YYYY-MM-DD) of the entry
    round_id      TEXT,                       -- host round (PC wager / payout / refund)
    bet_id        TEXT,                       -- crowd bet (phone wager / payout / refund)
    reference     TEXT,                       -- payment id, lottery ticket number, ...
    created_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE INDEX idx_ledger_wallet ON ledger(wallet_id, entry_id DESC);
CREATE INDEX idx_ledger_wallet_day ON ledger(wallet_id, day, kind);

-- once ever / once per day / once per bet, enforced by the database itself
CREATE UNIQUE INDEX uq_ledger_starting_grant ON ledger(wallet_id) WHERE kind = 'starting_grant';
CREATE UNIQUE INDEX uq_ledger_daily_once ON ledger(wallet_id, kind, day)
    WHERE kind IN ('daily_free', 'parking_bundle');
CREATE UNIQUE INDEX uq_ledger_round_money ON ledger(round_id, kind)
    WHERE round_id IS NOT NULL AND kind IN ('wager', 'payout', 'refund');
CREATE UNIQUE INDEX uq_ledger_bet_money ON ledger(bet_id, kind)
    WHERE bet_id IS NOT NULL AND kind IN ('wager', 'payout', 'refund');

CREATE TRIGGER ledger_no_update BEFORE UPDATE ON ledger
BEGIN
    SELECT RAISE(ABORT, 'ledger is append-only');
END;

CREATE TRIGGER ledger_no_delete BEFORE DELETE ON ledger
BEGIN
    SELECT RAISE(ABORT, 'ledger is append-only');
END;

-- A host (PC) round: the crash outcome plus the host's own wager.
CREATE TABLE rounds (
    round_id           TEXT PRIMARY KEY,
    wallet_id          TEXT NOT NULL REFERENCES wallets(wallet_id),
    wager_points       INTEGER NOT NULL CHECK (wager_points > 0),
    payout_points      INTEGER NOT NULL DEFAULT 0 CHECK (payout_points >= 0),
    status             TEXT NOT NULL CHECK (status IN ('running', 'crashed', 'cashed_out', 'interrupted')),
    crash_point        REAL NOT NULL CHECK (crash_point >= 1.0),
    crash_time_seconds REAL NOT NULL CHECK (crash_time_seconds >= 0),
    distraction        TEXT,
    season             TEXT NOT NULL,
    growth_rate        REAL NOT NULL,
    rush               INTEGER NOT NULL DEFAULT 0,
    lead_in_seconds    REAL NOT NULL,
    cashout_multiplier TEXT,                  -- Decimal as text, e.g. '1.27'
    started_epoch      REAL NOT NULL,         -- server wall clock, for reveal timing after a restart
    started_at         TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    finished_at        TEXT
);

CREATE INDEX idx_rounds_wallet ON rounds(wallet_id, started_at DESC);
CREATE UNIQUE INDEX uq_one_running_round_per_wallet ON rounds(wallet_id) WHERE status = 'running';

-- A phone's bet on the big screen's walk. round_id is set when the host starts the round.
CREATE TABLE crowd_bets (
    bet_id             TEXT PRIMARY KEY,
    wallet_id          TEXT NOT NULL REFERENCES wallets(wallet_id),
    round_id           TEXT REFERENCES rounds(round_id),
    wager_points       INTEGER NOT NULL CHECK (wager_points > 0),
    payout_points      INTEGER NOT NULL DEFAULT 0 CHECK (payout_points >= 0),
    status             TEXT NOT NULL CHECK (status IN ('open', 'running', 'cashed_out', 'crashed', 'refunded')),
    cashout_multiplier TEXT,
    created_at         TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    settled_at         TEXT
);

CREATE INDEX idx_crowd_bets_round ON crowd_bets(round_id);
CREATE UNIQUE INDEX uq_one_live_crowd_bet_per_wallet ON crowd_bets(wallet_id)
    WHERE status IN ('open', 'running');
