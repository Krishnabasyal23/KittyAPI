# v2 database migrations

The v2 server keeps its data in its **own** SQLite file. By default that's `data/catino-v2.db`;
set `KITTY_DB_PATH` to use another file. The v1 demo database `data/kittycat.db` is never opened
by v2 code: `get_connection` refuses that path, and the migration runner also refuses any file
that has the v1 schema.

## How it works

- Migrations are `database/migrations/NNNN_name.sql`. They're applied in order at server start
  (`initialize_database`), or by hand:

  ```bat
  python -m database.migrate status [--db PATH]
  python -m database.migrate up     [--db PATH]
  ```

- Each migration runs in one transaction, together with its row in `schema_migrations`
  (version, name, SHA-256 checksum, applied_at). If it fails, nothing from it remains.
- Before applying new migrations to a database that already has some, the runner copies the
  file to `<db>.pre-NNNN-<timestamp>.bak` in the same folder. Those files are git-ignored and
  stay on D:.
- Applied migrations are immutable. If a file's checksum no longer matches, the runner stops.
  Change the schema with a **new** migration, never by editing an old one.
- `GET /api/health` reports the highest applied version as `db_schema`.

| Version | File | What it adds |
|---|---|---|
| 1 | `0001_wallets_ledger_rounds.sql` | wallets, append-only ledger (triggers), host rounds, crowd bets, once-only unique indexes |
| 2 | `0002_rewards_payments.sql` | simulated payment receipts, lottery redemptions, idempotency keys |
| 3 | `0003_round_fairness_proofs.sql` | fairness-seal columns on rounds |
| 4 | `0004_responsible_gambling_settings.sql` | per-wallet responsible-gambling settings |
| 5 | `0005_mock_lottery_tickets.sql` | server-issued mock lottery tickets |

## Rollback procedures

The rule: **never delete a database file, and never run data-losing SQL as a default rollback.**
Choose the first option that fits.

### A. Roll back the code, keep the data (preferred)

Stop the server and run the previous code version (for example
`git switch --detach v2.0.0-alpha.2`). Older code never reads `catino-v2.db`, so the v2 file
stays untouched for when you come back, and nothing is lost.

**Warning:** alpha.1 and alpha.2 default to `data/kittycat.db`, the v1 demo file, and write to it
at start-up (schema setup, refund recovery). Never start them without `KITTY_DB_PATH` pointing
at a separate file, for example
`set KITTY_DB_PATH=D:\hhh\catt\.tmp\alpha2\kittycat-alpha2.db`.

### B. Undo one migration on an existing database

1. Stop the server.
2. Rename the current file aside. Don't delete it:
   `ren catino-v2.db catino-v2.db.rolledback-<timestamp>`
   (and the `-wal` / `-shm` files with it, if present).
3. Copy the backup the runner made before that migration into place:
   `copy catino-v2.db.pre-NNNN-<timestamp>.bak catino-v2.db`
4. Start the matching code version and run `python -m database.migrate status` to confirm.

Anything written after the backup stays in the renamed file. Reconcile it by hand if needed.

### C. Point the server at a different file

Set `KITTY_DB_PATH` to a new or restored file and restart. The current file is left as it is.

### Data-losing SQL (only with written approval)

Each migration's header names the SQL that would reverse it (`DROP TABLE ...`,
`ALTER TABLE rounds DROP COLUMN proof_*`). Those statements destroy player data and fairness
proofs, so they aren't run by any tool. Use them only after a backup and an explicit decision.

## Checking a database

```python
from database.wallet_store import WalletStore
WalletStore("data/catino-v2.db").audit()   # [] = every balance equals its ledger chain
```
