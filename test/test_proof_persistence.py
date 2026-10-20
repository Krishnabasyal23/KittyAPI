from decimal import Decimal

import pytest

from database.connection import initialize_database
from database.wallet_store import WalletStore
from fairness import NotRevealedYet, proof_from_columns, proof_to_columns
from round_manager import RoundManager


@pytest.fixture
def store(tmp_path):
    db = tmp_path / "proofs.db"
    initialize_database(db)
    return WalletStore(db)


def start(store, rng_mode="secure"):
    manager = RoundManager(session_store=store, season_rules=True)
    sid = manager.create_session()["session_id"]
    store.confirm_age(sid)
    started = manager.start_round(sid, Decimal("5"), rng_mode=rng_mode)
    return manager, sid, started


def test_columns_round_trip_exactly(monkeypatch, store):
    monkeypatch.setattr("randomness.generate_quantum_number", lambda qubits, samples: [5, 17, 63, 0, 42, 9])
    manager, sid, started = start(store, rng_mode="hybrid")
    original = manager.rounds[started["round_id"]]["proof"]

    saved = store.get_round(sid, started["round_id"])
    assert proof_from_columns(saved) == original
    assert proof_to_columns(proof_from_columns(saved)) == proof_to_columns(original)


def test_verify_works_after_a_restart(monkeypatch, store):
    manager, sid, started = start(store)
    rid = started["round_id"]
    sealed_crash = manager.rounds[rid]["crash_point"]

    store.recover_interrupted_rounds()          # the server stopped mid-walk: wager refunded
    restarted = RoundManager(session_store=store)

    body = restarted.verify_round(sid, rid)     # refunded rounds open at once
    assert body["commitment"] == started["commitment"]
    assert body["crash_point"] == sealed_crash
    assert body["checks"] == {"hash": True, "math": True, "quantum": None}
    assert body["verified"] is True


def test_seal_stays_closed_after_restart_until_the_crash_time(monkeypatch, store):
    monkeypatch.setattr("randomness.secure_rng", lambda bits=36: 1)   # 100x crash: a long walk
    manager, sid, started = start(store)
    rid = started["round_id"]

    clock = [manager._clock()]
    # cash out right after the warm-up, then "restart" the server
    monkeypatch.setattr(manager, "_clock", lambda: clock[0] + started["warmup_seconds"] + 0.5)
    assert manager.cash_out(sid, rid)["status"] == "cashed_out"

    restarted = RoundManager(session_store=store)
    assert restarted.get_round_result(sid, rid) == {"status": "running", "result": None}
    with pytest.raises(NotRevealedYet):
        restarted.verify_round(sid, rid)

    later = store.get_round(sid, rid)["started_epoch"] + 3600
    monkeypatch.setattr("round_manager.wall_clock", lambda: later)
    body = restarted.verify_round(sid, rid)
    assert body["commitment"] == started["commitment"]
    assert body["checks"]["hash"] is True and body["checks"]["math"] is True


def test_tampered_stored_seal_fails_verification(store):
    manager, sid, started = start(store)
    rid = started["round_id"]
    store.recover_interrupted_rounds()

    conn = store._connect()
    with conn:
        conn.execute("UPDATE rounds SET proof_value = '12345' WHERE round_id = ?", (rid,))
    conn.close()

    body = RoundManager(session_store=store).verify_round(sid, rid)
    assert body["checks"]["hash"] is False
    assert body["verified"] is False
