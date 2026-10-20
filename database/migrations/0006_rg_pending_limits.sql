-- 0006: delayed daily-spend-limit increases.
--
-- Lowering a limit applies at once. Raising or removing it is stored here and only takes effect
-- after pending_limit_effective_at (24 hours later), so a limit can't be lifted in the heat of play.
-- A pending row with pending_daily_spend_limit_points NULL means "remove the limit".
--
-- Rollback (non-destructive): restore <db>.pre-0006-<timestamp>.bak as described in
-- docs/MIGRATIONS.md, keeping the current file aside; or run the previous code version, which
-- ignores these columns (a pending increase then never applies, which is the safe direction).
-- Data-losing SQL (ALTER TABLE rg_settings DROP COLUMN pending_*) only with written approval.

ALTER TABLE rg_settings ADD COLUMN pending_daily_spend_limit_points INTEGER
    CHECK (pending_daily_spend_limit_points IS NULL OR pending_daily_spend_limit_points > 0);
ALTER TABLE rg_settings ADD COLUMN pending_limit_effective_at TEXT;   -- ISO-8601 UTC
