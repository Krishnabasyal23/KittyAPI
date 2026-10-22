-- 0009: audit log and operator settings.
--
-- audit_log: append-only record of actions that matter for safety and money outside the points ledger:
--   cashier top-ups and QR codes, play-safe changes (19+, limits, breaks, self-exclusion), ticket locks,
--   operator changes (season bonus tuning). Triggers refuse UPDATE and DELETE, like the ledger.
-- settings: small operator settings by key (JSON value), e.g. the tuned season bonuses.
--
-- Rollback (non-destructive): restore <db>.pre-0009-<timestamp>.bak as described in
-- docs/MIGRATIONS.md, keeping the current file aside; or run the previous code version, which
-- ignores both tables (bonuses then fall back to the built-in +4%). Data-losing SQL
-- (DROP TABLE audit_log / settings) only with written approval.

CREATE TABLE audit_log (
    audit_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    actor      TEXT NOT NULL CHECK (actor IN ('player', 'cashier', 'operator', 'system')),
    action     TEXT NOT NULL,
    wallet_id  TEXT,
    detail     TEXT NOT NULL DEFAULT '{}',          -- JSON
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE INDEX idx_audit_created ON audit_log(created_at DESC);
CREATE INDEX idx_audit_wallet ON audit_log(wallet_id, created_at DESC);

CREATE TRIGGER audit_log_no_update BEFORE UPDATE ON audit_log
BEGIN
    SELECT RAISE(ABORT, 'audit_log is append-only');
END;

CREATE TRIGGER audit_log_no_delete BEFORE DELETE ON audit_log
BEGIN
    SELECT RAISE(ABORT, 'audit_log is append-only');
END;

CREATE TABLE settings (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,                        -- JSON
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);
