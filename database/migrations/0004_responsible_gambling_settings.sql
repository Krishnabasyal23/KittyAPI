-- 0004: per-wallet responsible-gambling settings (backend only; the Phase 3 UI will edit them).
--
-- NULL means "not set". The wager and purchase paths consult responsible.WalletPolicy, which
-- enforces self-exclusion, cool-off and the daily spend limit when present.
--
-- Rollback (non-destructive): restore <db>.pre-0004-<timestamp>.bak as described in
-- docs/MIGRATIONS.md, keeping the current file aside; or run the previous code version, which
-- ignores this table. Data-losing SQL (DROP TABLE rg_settings) only with written approval.

CREATE TABLE rg_settings (
    wallet_id                TEXT PRIMARY KEY REFERENCES wallets(wallet_id),
    daily_spend_limit_points INTEGER CHECK (daily_spend_limit_points IS NULL OR daily_spend_limit_points > 0),
    cool_off_until           TEXT,           -- ISO-8601 UTC
    self_excluded_until      TEXT,           -- ISO-8601 UTC
    reality_check_minutes    INTEGER CHECK (reality_check_minutes IS NULL OR reality_check_minutes > 0),
    age_confirmed_at         TEXT,           -- 19+ gate (Phase 3)
    updated_at               TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);
