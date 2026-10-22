"""Schrodinger's Box: player seeds. The server commits to its value first, players send seeds during the
lead-in, and the crash point comes from value XOR mask(seeds) (see fairness.py)."""

import hashlib
from decimal import Decimal

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

import main
from crash_engine import MIN_CRASH_SECONDS, crash_from_uniform
from crowd import CrowdTable
from database.connection import initialize_database
from database.wallet_store import WalletStore
from fairness import apply_seeds, clean_seed, seed_mask, verify
from round_manager import RoundManager

SEED_A = "a1" * 16
SEED_B = "0f" * 16


def make(tmp_path=None, store=None):
    clock = [100.0]
    manager = RoundManager(clock=lambda: clock[0], session_store=store)
    sid = manager.create_session()["session_id"]
    if store is not None:
        store.confirm_age(sid)
    started = manager.start_round(sid, Decimal("10"), rng_mode="secure")
    return manager, sid, started, clock


def expected_crash(value, round_id, seeds):
    final = value ^ seed_mask(round_id, seeds, 36)
    return final, crash_from_uniform((final + 0.5) / 2**36)


def test_seed_mask_is_the_top_bits_of_a_sha256_of_the_seeds():
    digest = hashlib.sha256(f"r1:seeds:{SEED_A},{SEED_B}".encode()).hexdigest()
    assert seed_mask("r1", [SEED_A, SEED_B], 36) == int(digest[:9], 16)   # 9 hex characters = 36 bits
    assert seed_mask("r1", [], 36) == 0
    assert seed_mask("r1", [SEED_B, SEED_A], 36) != seed_mask("r1", [SEED_A, SEED_B], 36)


def test_a_seed_is_16_to_64_hex_characters():
    assert clean_seed(" ABCDEF0123456789 ") == "abcdef0123456789"
    for bad in ("", "xyz", "abc", "g" * 20, "a" * 65, None):
        with pytest.raises(ValueError):
            clean_seed(bad)


def test_without_seeds_the_servers_value_decides_and_the_proof_says_so(monkeypatch):
    manager, sid, started, clock = make()
    rid = started["round_id"]
    provisional = manager.rounds[rid]["crash_point"]
    clock[0] += 10_000
    result = manager.get_round_result(sid, rid)
    assert result["crash_point"] == provisional
    assert result["proof"]["seeds"] == [] and result["proof"]["final_value"] == result["proof"]["value"]
    assert manager.verify_round(sid, rid)["checks"] == {"hash": True, "math": True, "quantum": None, "seeds": None}


def test_seeds_make_the_crash_point_and_the_checks_pass():
    manager, sid, started, clock = make()
    rid = started["round_id"]
    rd = manager.rounds[rid]
    value = int(rd["proof"]["value"])
    assert manager.submit_seed(sid, rid, SEED_A)["accepted"] is True
    clock[0] += MIN_CRASH_SECONDS + 0.1
    manager.get_round_state(sid, rid)                       # the lead-in is over: the box closes
    final, crash = expected_crash(value, rid, [SEED_A])
    assert rd["crash_point"] == pytest.approx(crash)
    clock[0] += 10_000
    body = manager.verify_round(sid, rid)
    assert body["checks"] == {"hash": True, "math": True, "quantum": None, "seeds": True}
    assert body["proof"]["final_value"] == str(final)
    assert body["proof"]["seeds"] == [{"label": "player", "seed": SEED_A}]
    assert body["verified"] is True


def test_the_servers_provisional_value_is_not_the_final_crash_point():
    """The server can't know the crash point before the seeds arrive: the final one differs from the
    value it committed to (for any seed, with overwhelming probability)."""
    manager, sid, started, clock = make()
    rid = started["round_id"]
    rd = manager.rounds[rid]
    value = int(rd["proof"]["value"])
    manager.submit_seed(sid, rid, SEED_A)
    final, _ = expected_crash(value, rid, [SEED_A])
    assert final != value


def test_a_changed_or_dropped_seed_is_caught():
    manager, sid, started, clock = make()
    rid = started["round_id"]
    manager.submit_seed(sid, rid, SEED_A)
    clock[0] += 10_000
    body = manager.verify_round(sid, rid)
    reveal = dict(body["proof"])
    tampered = dict(reveal, seeds=[{"label": "player", "seed": SEED_B}])
    assert verify(rid, tampered, body["crash_point"], body["commitment"])["seeds"] is False
    dropped = dict(reveal, seeds=[])
    assert verify(rid, dropped, body["crash_point"], body["commitment"])["seeds"] is None   # n/a, but the math still holds
    wrong_final = dict(reveal, final_value=str(int(reveal["final_value"]) ^ (1 << 35)))
    checks = verify(rid, wrong_final, body["crash_point"], body["commitment"])
    assert checks["seeds"] is False and checks["math"] is False


def test_seeds_close_when_the_walk_starts_and_only_one_per_seat():
    manager, sid, started, clock = make()
    rid = started["round_id"]
    manager.submit_seed(sid, rid, SEED_A)
    with pytest.raises(ValueError, match="already sent"):
        manager.submit_seed(sid, rid, SEED_B)
    clock[0] += MIN_CRASH_SECONDS
    with pytest.raises(ValueError, match="close when the walk starts"):
        manager.add_seed(manager.rounds[rid], "late", "late", SEED_B)
    assert [s["seed"] for s in manager.rounds[rid].get("seeds", [])] == [SEED_A]   # the late one never counted


def test_a_stubbed_round_without_a_value_takes_no_seeds(monkeypatch):
    monkeypatch.setattr("round_manager.draw_crash_point", lambda mode: (3.0, None))
    manager, sid, started, clock = make()
    with pytest.raises(ValueError, match="does not take seeds"):
        manager.submit_seed(sid, started["round_id"], SEED_A)


def test_seeds_and_final_value_survive_a_restart(tmp_path):
    db = tmp_path / "seeds.db"
    initialize_database(db)
    store = WalletStore(db)
    manager, sid, started, clock = make(store=store)
    rid = started["round_id"]
    manager.submit_seed(sid, rid, SEED_A)
    clock[0] += MIN_CRASH_SECONDS + 0.1
    manager.get_round_state(sid, rid)
    saved = store.get_round(sid, rid)
    assert saved["proof_final_value"] is not None and "a1a1" in saved["proof_seeds"]
    assert saved["crash_point"] == manager.rounds[rid]["crash_point"]

    store.recover_interrupted_rounds()                      # server stopped mid-walk
    restarted = RoundManager(session_store=store)
    body = restarted.verify_round(sid, rid)
    assert body["checks"]["hash"] is True and body["checks"]["seeds"] is True


def test_the_api_takes_a_seed_and_refuses_late_or_bad_ones():
    client = TestClient(main.app)
    sid = client.post("/api/sessions").json()["session_id"]
    h = {"X-Session-ID": sid}
    client.post("/api/wallet/age-confirm", json={"confirm": True}, headers=h)
    rid = client.post("/api/rounds", json={"wager": "5"}, headers=h).json()["round_id"]
    assert client.post(f"/api/rounds/{rid}/seed", json={"seed": "zzz"}, headers=h).status_code == 400
    ok = client.post(f"/api/rounds/{rid}/seed", json={"seed": SEED_A}, headers=h)
    assert ok.status_code == 200 and ok.json()["accepted"] is True
    assert client.post(f"/api/rounds/{rid}/seed", json={"seed": SEED_B}, headers=h).status_code == 409
    assert client.post(f"/api/rounds/nope/seed", json={"seed": SEED_A}, headers=h).status_code == 404


@pytest.fixture
def crowd(tmp_path):
    db = tmp_path / "crowd-seeds.db"
    initialize_database(db)
    store = WalletStore(db)
    clock = [100.0]
    manager = RoundManager(clock=lambda: clock[0], session_store=store)
    table = CrowdTable(manager, store)
    host = manager.create_session()["session_id"]
    store.confirm_age(host)
    table.set_host(host, 8001, "Screen")
    return table, manager, host, clock


def test_phones_send_seeds_and_open_the_box(crowd):
    table, manager, host, clock = crowd
    ada = table.join("Ada", age_confirmed=True)["player_id"]
    bob = table.join("Bob", age_confirmed=True)["player_id"]
    table.open_betting(host, 5)
    table.bet(ada, "5")
    started = manager.start_round(host, Decimal("1"), rng_mode="secure")
    table.attach(host, started)
    rid = started["round_id"]

    state = table.state(ada)
    assert state["commitment"] == started["commitment"] and state["seed_open"] is True
    assert state["you"]["seed_sent"] is False

    with pytest.raises(HTTPException) as watcher:
        table.seed(bob, SEED_B)                              # Bob didn't bet: he only watches
    assert watcher.value.status_code == 400
    assert table.seed(ada, SEED_A)["count"] == 1
    manager.submit_seed(host, rid, SEED_B)                   # the game screen's own seed
    with pytest.raises(HTTPException) as twice:
        table.seed(ada, SEED_B)
    assert twice.value.status_code == 409
    assert table.state(ada)["you"]["seed_sent"] is True and table.state(ada)["seed_count"] == 2

    with pytest.raises(HTTPException) as early:
        table.box()
    assert early.value.status_code == 409

    clock[0] += MIN_CRASH_SECONDS + 0.1
    assert table.state(ada)["seed_open"] is False
    with pytest.raises(HTTPException) as late:
        table.seed(ada, SEED_A)
    assert late.value.status_code in (400, 409)

    clock[0] += 10_000
    table.state()                                            # the table sees the crash
    body = table.box()
    assert body["checks"] == {"hash": True, "math": True, "quantum": None, "seeds": True}
    assert [s["label"] for s in body["proof"]["seeds"]] == ["Ada", "player"]


def test_mixing_in_seeds_keeps_the_odds_at_96_over_x():
    """Whatever value the server committed to, random seeds make the crash point follow P(C >= m) = 0.96 / m."""
    import secrets
    value = 12345                                            # a fixed, deliberately poor server value
    crashes = []
    for i in range(20000):
        final = value ^ seed_mask(f"round{i}", [secrets.token_hex(16)], 36)
        crashes.append(crash_from_uniform((final + 0.5) / 2**36))
    for m, expect in ((1.5, 0.64), (2.0, 0.48), (5.0, 0.192)):
        share = sum(c >= m for c in crashes) / len(crashes)
        assert abs(share - expect) < 0.015, (m, share)
