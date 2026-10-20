-- 0002: demo payments (receipts), lottery tickets and idempotency keys.
--
-- lottery_tickets.ticket_number is the PRIMARY KEY, so a ticket can be redeemed once, ever, by anyone.
-- idempotency_keys stores the response of a request so a retried request with the same
-- Idempotency-Key header returns the same result instead of paying twice.
--
-- Rollback (non-destructive): the runner copies the database to
-- <db>.pre-0002-<timestamp>.bak before applying. Stop the server, rename the current file aside
-- (never delete it) and copy the backup into place; or deploy the previous code version, which
-- ignores these tables. Data-losing SQL (DROP TABLE payments / lottery_tickets / idempotency_keys)
-- only with written approval. Full procedure: docs/MIGRATIONS.md.

CREATE TABLE payments (
    payment_id   TEXT PRIMARY KEY,
    provider     TEXT NOT NULL,              -- 'fake' while DEMO_PAYMENTS=1
    wallet_id    TEXT NOT NULL REFERENCES wallets(wallet_id),
    product      TEXT NOT NULL CHECK (product IN ('topup_5', 'parking_5')),
    amount_cents INTEGER NOT NULL CHECK (amount_cents > 0),
    currency     TEXT NOT NULL DEFAULT 'CAD',
    points       INTEGER NOT NULL CHECK (points >= 0),
    status       TEXT NOT NULL CHECK (status IN ('succeeded', 'declined')),
    day          TEXT NOT NULL,
    entry_id     INTEGER REFERENCES ledger(entry_id),
    created_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE INDEX idx_payments_wallet ON payments(wallet_id, created_at DESC);

CREATE TABLE lottery_tickets (
    ticket_number TEXT PRIMARY KEY,
    wallet_id     TEXT NOT NULL REFERENCES wallets(wallet_id),
    day           TEXT NOT NULL,
    entry_id      INTEGER NOT NULL REFERENCES ledger(entry_id),
    redeemed_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE INDEX idx_lottery_wallet_day ON lottery_tickets(wallet_id, day);

CREATE TABLE idempotency_keys (
    wallet_id     TEXT NOT NULL REFERENCES wallets(wallet_id),
    idem_key      TEXT NOT NULL,
    endpoint      TEXT NOT NULL,
    request_hash  TEXT NOT NULL,
    response_json TEXT NOT NULL,
    created_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    PRIMARY KEY (wallet_id, idem_key)
);
