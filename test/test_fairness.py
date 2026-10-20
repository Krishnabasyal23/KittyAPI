import math

from decimal import Decimal

import pytest

import main
from fairness import NotRevealedYet, commitment_for, verify
from fastapi.testclient import TestClient
from round_manager import RoundManager

SECRET_FIELDS = ("proof", "salt", "value", "crash_point", "crash_time", "quantum_samples", "classical_mask")


def finished_round(rng_mode="secure", season_rules=False):
    clock = [100.0]
    manager = RoundManager(clock=lambda: clock[0], season_rules=season_rules)
    sid = manager.create_session()["session_id"]
    started = manager.start_round(sid, Decimal("10.00"), rng_mode=rng_mode)
    clock[0] += 10_000  # long after any crash
    return manager, sid, started


def test_commit_reveal_secure_mode():
    manager, sid, started = finished_round()
    rid = started["round_id"]
    assert len(started["commitment"]) == 64

    result = manager.get_round_result(sid, rid)
    reveal = result["proof"]
    assert reveal["commitment"] == started["commitment"]

    checks = verify(rid, reveal, result["crash_point"], started["commitment"])
    assert checks["hash"] is True
    assert checks["math"] is True


def test_quantum_samples_are_published(monkeypatch):
    # fake qubits so the test is fast and deterministic
    monkeypatch.setattr("randomness.generate_quantum_number", lambda qubits, samples: [5, 17, 63, 0, 42, 9])
    manager, sid, started = finished_round(rng_mode="hybrid")
    rid = started["round_id"]
    result = manager.get_round_result(sid, rid)
    reveal = result["proof"]
    assert reveal["source"] == "hybrid"
    assert reveal["quantum_samples"] == [5, 17, 63, 0, 42, 9]
    assert isinstance(reveal["value"], str) and isinstance(reveal["classical_mask"], str)
    checks = verify(rid, reveal, result["crash_point"], started["commitment"])
    assert checks == {"hash": True, "math": True, "quantum": True}


def test_reveal_matches_unity_quantum_proof_fields():
    manager, sid, started = finished_round()
    reveal = manager.get_round_result(sid, started["round_id"])["proof"]
    # QuantumProof in Assets/KittyCrash/Scripts/Core/Fairness.cs
    assert set(reveal) == {
        "scheme", "commitment", "salt", "value", "bits", "qubits", "source", "quantum_samples",
        "classical_mask", "house_edge", "max_multiplier", "crash_point_exact",
    }
    assert reveal["scheme"] == "value" and reveal["bits"] == 36 and reveal["qubits"] == 6


def test_tampered_value_is_detected():
    manager, sid, started = finished_round()
    rid = started["round_id"]
    result = manager.get_round_result(sid, rid)
    reveal = dict(result["proof"])
    reveal["value"] = str(int(reveal["value"]) + 1)   # pretend the server changed the outcome afterwards
    assert verify(rid, reveal, result["crash_point"], started["commitment"])["hash"] is False


def test_tampered_crash_point_is_detected():
    manager, sid, started = finished_round()
    rid = started["round_id"]
    result = manager.get_round_result(sid, rid)
    checks = verify(rid, result["proof"], result["crash_point"] + 0.5, started["commitment"])
    assert checks["hash"] is True
    assert checks["math"] is False


def test_tampered_qubits_are_detected(monkeypatch):
    monkeypatch.setattr("randomness.generate_quantum_number", lambda qubits, samples: [5, 17, 63, 0, 42, 9])
    manager, sid, started = finished_round(rng_mode="hybrid")
    rid = started["round_id"]
    result = manager.get_round_result(sid, rid)
    reveal = dict(result["proof"])
    reveal["quantum_samples"] = [6, 17, 63, 0, 42, 9]
    assert verify(rid, reveal, result["crash_point"], started["commitment"])["quantum"] is False


def test_wrong_commitment_is_detected():
    manager, sid, started = finished_round()
    rid = started["round_id"]
    result = manager.get_round_result(sid, rid)

    # a self-consistent reveal that is NOT the one published at round start
    forged = dict(result["proof"])
    forged["salt"] = "00" * 16
    forged["commitment"] = commitment_for(rid, forged["value"], forged["salt"])
    assert verify(rid, forged, result["crash_point"])["hash"] is True   # consistent with itself...
    assert verify(rid, forged, result["crash_point"], started["commitment"])["hash"] is False

    # the real reveal checked against another round's id
    assert verify("another-round", result["proof"], result["crash_point"])["hash"] is False


def test_secret_is_sealed_while_running():
    clock = [100.0]
    manager = RoundManager(clock=lambda: clock[0], season_rules=True)
    sid = manager.create_session()["session_id"]
    started = manager.start_round(sid, Decimal("10.00"), rng_mode="secure")
    rid = started["round_id"]

    assert len(started["commitment"]) == 64
    assert not any(field in started for field in SECRET_FIELDS)
    assert not any(field in manager.get_round_state(sid, rid) for field in SECRET_FIELDS)
    assert not any(field in manager.get_round_season(sid, rid) for field in SECRET_FIELDS)
    assert manager.get_round_result(sid, rid) == {"status": "running", "result": None}
    with pytest.raises(NotRevealedYet):
        manager.verify_round(sid, rid)


def test_secret_stays_sealed_after_early_cashout(monkeypatch):
    monkeypatch.setattr("round_manager.draw_crash_point", lambda mode: (50.0, None))
    clock = [100.0]
    manager = RoundManager(clock=lambda: clock[0])
    sid = manager.create_session()["session_id"]
    rid = manager.start_round(sid, Decimal("10.00"), rng_mode="secure")["round_id"]

    clock[0] += 5.0   # past the warm-up, far from a 50x crash
    assert manager.cash_out(sid, rid)["status"] == "cashed_out"

    # the cat keeps walking for everyone else: crash point and seal stay closed
    assert manager.get_round_result(sid, rid) == {"status": "running", "result": None}
    with pytest.raises(NotRevealedYet):
        manager.verify_round(sid, rid)

    clock[0] += 10_000
    result = manager.get_round_result(sid, rid)
    assert result["status"] == "cashed_out" and result["crash_point"] == 50.0
    assert manager.verify_round(sid, rid)["checks"]["hash"] is True


@pytest.mark.parametrize("season", ["spring", "summer", "fall", "winter"])
def test_seasons_and_bonuses_do_not_change_the_sealed_crash(monkeypatch, season):
    monkeypatch.setattr("round_manager.select_season", lambda: season)
    monkeypatch.setattr("randomness.secure_rng", lambda bits=36: 1)  # tiny u -> 100x crash, cat keeps walking
    clock = [100.0]
    manager = RoundManager(clock=lambda: clock[0], season_rules=True)
    sid = manager.create_session()["session_id"]
    started = manager.start_round(sid, Decimal("10.00"), rng_mode="secure")
    rid = started["round_id"]
    rd = manager.rounds[rid]
    sealed_crash = rd["crash_point"]
    assert sealed_crash == 100.0

    # cash out where the season pays its bonus
    if season == "spring":
        t = rd["events"]["butterfly"][0] + 0.01
    elif season == "winter":
        t = rd["lead_in_seconds"] + math.log(2.05) / rd["growth_rate"]
    else:
        t = rd["lead_in_seconds"] + 0.01
    clock[0] += t
    cashed = manager.cash_out(sid, rid)
    expected_bonus = {"spring": "butterfly", "winter": "cocoa"}.get(season)
    assert cashed["bonus"] == expected_bonus

    clock[0] += 10_000
    body = manager.verify_round(sid, rid)
    assert body["crash_point"] == sealed_crash
    assert body["proof"]["crash_point_exact"] == sealed_crash
    assert body["checks"]["hash"] is True and body["checks"]["math"] is True
    assert body["verified"] is True


def test_verify_endpoint(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(main, "manager", RoundManager(clock=lambda: clock[0]))
    monkeypatch.setattr(main, "GAME_RNG_MODE", "secure")
    client = TestClient(main.app)
    sid = client.post("/api/sessions").json()["session_id"]
    h = {"X-Session-ID": sid}
    started = client.post("/api/rounds", headers=h, json={"wager": "5.00"}).json()
    rid = started["round_id"]

    assert client.get(f"/api/rounds/{rid}/verify", headers=h).status_code == 409
    assert "proof" not in client.get(f"/api/rounds/{rid}/result", headers=h).json()

    clock[0] += 10_000
    body = client.get(f"/api/rounds/{rid}/verify", headers=h).json()
    assert body["commitment"] == started["commitment"]
    assert body["checks"]["hash"] is True and body["checks"]["math"] is True
    assert body["verified"] is True

    other = client.post("/api/sessions").json()["session_id"]
    assert client.get(f"/api/rounds/{rid}/verify", headers={"X-Session-ID": other}).status_code == 404
