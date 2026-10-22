-- 0007: player profiles, signed single-use QR codes and any-amount top-ups.
--
-- profiles: a nickname and a cat avatar per wallet (the nickname is mirrored in wallets.display_name).
-- qr_tokens: one row per QR code a player asked for. used_at is set by the first cashier top-up, so a
--   code works once; expires_at makes an unused code die after a few minutes.
-- payments is rebuilt (SQLite cannot change a CHECK) so product may be 'topup' with any amount of
--   $1-$100 (amount_cents 100-10000), and provider may be 'cashier'. Every row is copied unchanged.
--
-- Rollback (non-destructive): restore <db>.pre-0007-<timestamp>.bak as described in
-- docs/MIGRATIONS.md, keeping the current file aside. The previous code version also runs on this
-- schema except for 'topup' rows, which its receipts screen shows by product id. Data-losing SQL
-- (DROP TABLE profiles / qr_tokens) only with written approval.

CREATE TABLE profiles (
    wallet_id  TEXT PRIMARY KEY REFERENCES wallets(wallet_id),
    nickname   TEXT NOT NULL CHECK (length(nickname) BETWEEN 2 AND 12),
    avatar     TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE TABLE qr_tokens (
    nonce        TEXT PRIMARY KEY,
    counter_code TEXT NOT NULL UNIQUE,
    wallet_id    TEXT NOT NULL REFERENCES wallets(wallet_id),
    issued_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    expires_at   TEXT NOT NULL,                -- ISO-8601 UTC
    used_at      TEXT,
    payment_id   TEXT
);

CREATE INDEX idx_qr_wallet ON qr_tokens(wallet_id, issued_at DESC);

CREATE TABLE payments_new (
    payment_id   TEXT PRIMARY KEY,
    provider     TEXT NOT NULL,              -- 'fake' (wallet screen) or 'cashier', both simulated
    wallet_id    TEXT NOT NULL REFERENCES wallets(wallet_id),
    product      TEXT NOT NULL CHECK (product IN ('topup', 'topup_5', 'parking_5')),
    amount_cents INTEGER NOT NULL CHECK (amount_cents BETWEEN 100 AND 10000),
    currency     TEXT NOT NULL DEFAULT 'CAD',
    points       INTEGER NOT NULL CHECK (points >= 0),
    status       TEXT NOT NULL CHECK (status IN ('succeeded', 'declined')),
    day          TEXT NOT NULL,
    entry_id     INTEGER REFERENCES ledger(entry_id),
    created_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

INSERT INTO payments_new (payment_id, provider, wallet_id, product, amount_cents, currency, points,
                          status, day, entry_id, created_at)
SELECT payment_id, provider, wallet_id, product, amount_cents, currency, points,
       status, day, entry_id, created_at
FROM payments;

DROP TABLE payments;
ALTER TABLE payments_new RENAME TO payments;
CREATE INDEX idx_payments_wallet ON payments(wallet_id, created_at DESC);
CREATE INDEX idx_payments_day ON payments(wallet_id, day, product);
