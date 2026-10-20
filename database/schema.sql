-- v1 schema (data/kittycat.db), kept for reference only. v2 code never runs it:
-- the v2 database is built by database/migrations/ (see database/migrate.py).

CREATE TABLE IF NOT EXISTS sessions (
    session_id TEXT PRIMARY KEY,
    balance_cents INTEGER NOT NULL
        CHECK (balance_cents >= 0),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS rounds (
    round_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL
        REFERENCES sessions(session_id),

    wager_cents INTEGER NOT NULL
        CHECK (wager_cents > 0),
    payout_cents INTEGER NOT NULL DEFAULT 0
        CHECK (payout_cents >= 0),

    status TEXT NOT NULL CHECK (
        status IN (
            'running',
            'crashed',
            'cashed_out',
            'interrupted'
        )
    ),

    crash_point REAL NOT NULL,
    crash_time_seconds REAL NOT NULL,
    distraction TEXT,

    season TEXT NOT NULL DEFAULT 'spring',
    growth_rate REAL NOT NULL DEFAULT 0.12,
    rush INTEGER NOT NULL DEFAULT 0,
    lead_in_seconds REAL NOT NULL DEFAULT 3.0,

    cashout_multiplier REAL,

    started_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    finished_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_rounds_session_date
ON rounds(session_id, started_at DESC);

CREATE UNIQUE INDEX IF NOT EXISTS idx_one_running_round_per_session
ON rounds(session_id)
WHERE status = 'running';

CREATE TABLE IF NOT EXISTS credit_transactions (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL
        REFERENCES sessions(session_id),
    round_id TEXT
        REFERENCES rounds(round_id),

    kind TEXT NOT NULL CHECK (
        kind IN (
            'initial_grant',
            'wager',
            'payout',
            'refund'
        )
    ),

    amount_cents INTEGER NOT NULL
        CHECK (amount_cents != 0),

    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,

    UNIQUE (round_id, kind)
);

CREATE INDEX IF NOT EXISTS idx_credit_session
ON credit_transactions(session_id, event_id DESC);
