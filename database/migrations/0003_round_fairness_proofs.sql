-- 0003: persist each round's fairness seal so /verify keeps working after a server restart.
--
-- The seal is written when the round starts (the server needs it to reveal later); the API still
-- reveals it only after the crash. Columns mirror fairness.make_proof(); quantum_samples is JSON.
--
-- Rollback (non-destructive): restore <db>.pre-0003-<timestamp>.bak as described in
-- docs/MIGRATIONS.md, keeping the current file aside; or run the previous code version, which
-- ignores these columns. Data-losing SQL (ALTER TABLE rounds DROP COLUMN proof_*) only with
-- written approval: it destroys the proofs players may still want to verify.

ALTER TABLE rounds ADD COLUMN proof_scheme TEXT;
ALTER TABLE rounds ADD COLUMN proof_commitment TEXT;
ALTER TABLE rounds ADD COLUMN proof_salt TEXT;
ALTER TABLE rounds ADD COLUMN proof_value TEXT;
ALTER TABLE rounds ADD COLUMN proof_bits INTEGER;
ALTER TABLE rounds ADD COLUMN proof_qubits INTEGER;
ALTER TABLE rounds ADD COLUMN proof_source TEXT;
ALTER TABLE rounds ADD COLUMN proof_quantum_samples TEXT;
ALTER TABLE rounds ADD COLUMN proof_classical_mask TEXT;
ALTER TABLE rounds ADD COLUMN proof_house_edge REAL;
ALTER TABLE rounds ADD COLUMN proof_max_multiplier REAL;
