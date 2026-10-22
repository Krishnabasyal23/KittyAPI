-- 0008: ticket entry lock.
--
-- One row per wallet that has typed a ticket code. failures counts wrong codes since the last good
-- one; after three in a row locked_until is set an hour ahead and failures starts again from 0.
-- A wallet with no row has never been locked.
--
-- Rollback (non-destructive): restore <db>.pre-0008-<timestamp>.bak as described in
-- docs/MIGRATIONS.md, keeping the current file aside; or run the previous code version, which
-- ignores this table. Data-losing SQL (DROP TABLE ticket_entry) only with written approval.

CREATE TABLE ticket_entry (
    wallet_id    TEXT PRIMARY KEY REFERENCES wallets(wallet_id),
    failures     INTEGER NOT NULL DEFAULT 0 CHECK (failures >= 0),
    locked_until TEXT,                         -- ISO-8601 UTC
    updated_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);
