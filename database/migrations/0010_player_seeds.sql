-- 0010: player seeds for Schrodinger's Box.
--
-- The server commits to its value first; players then send seeds during the 3 s lead-in and the crash point
-- comes from value XOR mask(seeds). The seeds (JSON list of {label, seed}) and the final value are stored
-- with the round so /verify keeps working after a restart. Both stay NULL on rounds from before this change.
--
-- Rollback (non-destructive): run the previous code version, which ignores these columns, or restore
-- <db>.pre-0010-<timestamp>.bak as described in docs/MIGRATIONS.md. Data-losing SQL
-- (ALTER TABLE rounds DROP COLUMN proof_seeds / proof_final_value) only with written approval: it
-- destroys what players need to check a round's seeds.

ALTER TABLE rounds ADD COLUMN proof_seeds TEXT;
ALTER TABLE rounds ADD COLUMN proof_final_value TEXT;
