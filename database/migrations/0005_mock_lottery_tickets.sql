-- 0005: mock lottery tickets issued by the server for local demos and automated tests.
--
-- Each mock ticket has a unique server-issued ticket_id and five printed two-digit numbers
-- (e.g. '01 02 04 08 16'). The same numbers may appear on many tickets; what is redeemable once is
-- the ticket_id. Redemptions are recorded in lottery_tickets (migration 0002), whose
-- ticket_number column holds this ticket_id. These are not real lottery tickets.
--
-- Rollback (non-destructive): restore <db>.pre-0005-<timestamp>.bak as described in
-- docs/MIGRATIONS.md, keeping the current file aside; or run the previous code version, which
-- ignores this table. Data-losing SQL (DROP TABLE mock_lottery_tickets) only with written approval.

CREATE TABLE mock_lottery_tickets (
    ticket_id TEXT PRIMARY KEY,
    numbers   TEXT NOT NULL,
    issued_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);
