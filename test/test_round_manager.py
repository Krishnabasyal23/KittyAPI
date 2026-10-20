from decimal import Decimal
from round_manager import RoundManager
from unittest.mock import patch

def test_create_session():
    manager = RoundManager()

    session = manager.create_session()

    assert session["balance"] == "50"
    assert session["session_id"] in manager.sessions

def test_start_round(monkeypatch):
    monkeypatch.setattr("round_manager.draw_crash_point", lambda mode: (2.0, None))
    monkeypatch.setattr("round_manager.select_distraction", lambda season: "fish")
    manager = RoundManager()
    session = manager.create_session()
    session_id = session["session_id"]
    result = manager.start_round(session_id, Decimal("10"))
    assert result["status"] == "running"
    assert result["balance"] == "40"
    assert "crash_point" not in result

def test_successful_cashout(monkeypatch):
    monkeypatch.setattr("round_manager.draw_crash_point", lambda mode: (2.0, None))
    clock = [100.0]
    manager = RoundManager( clock=lambda: clock[0])
    session = manager.create_session()
    session_id = session["session_id"]
    round_data = manager.start_round(session_id, Decimal("10"))
    round_id = round_data["round_id"]
    # Advance the clock by five second.
    clock[0] += 5.0
    result = manager.cash_out(session_id, round_id)
    assert result["status"] == "cashed_out"
    assert result["payout"] == "13"
    assert result["balance"] == "53"

def test_duplicate_cashout(monkeypatch):
    monkeypatch.setattr("round_manager.draw_crash_point", lambda mode: (2.0, None))
    clock = [100.0]
    manager = RoundManager(clock=lambda: clock[0])
    session_id = manager.create_session()["session_id"]
    round_id = manager.start_round(session_id, Decimal("10"))["round_id"]
    clock[0] += 5.0
    first = manager.cash_out(session_id, round_id)
    second = manager.cash_out(session_id, round_id)
    assert first["payout"] == second["payout"]
    assert manager.get_balance(session_id)["balance"] == "53"

def test_crashed_round(monkeypatch):
    monkeypatch.setattr("round_manager.draw_crash_point", lambda mode: (2.0, None))
    clock = [100.0]
    manager = RoundManager(clock=lambda: clock[0])
    session_id = manager.create_session()["session_id"]
    round_id = manager.start_round(session_id, Decimal("10"))["round_id"]
    # Advance beyond the 2x crash deadline.
    clock[0] += 10.0
    result = manager.cash_out(session_id, round_id)
    assert result["status"] == "crashed"
    assert result["payout"] == "0"
    assert result["balance"] == "40"

def test_round_lifecycle():
    clock = [100.0]
    manager = RoundManager(clock=lambda: clock[0])

    session = manager.create_session()
    session_id = session["session_id"]

    with (patch("round_manager.draw_crash_point", return_value=(3.0, None)), patch("round_manager.select_distraction", return_value="fish")):
        round_data = manager.start_round(session_id, Decimal("10"))

    round_id = round_data["round_id"]

    assert round_data["balance"] == "40"
    assert round_data["status"] == "running"

    clock[0] += 5.0
    state = manager.get_round_state(session_id, round_id)

    assert state["status"] == "running"
    assert state["multiplier"] == 1.27

    result = manager.cash_out(session_id, round_id)

    assert result["status"] == "cashed_out"
    assert result["payout"] == "13"
    assert result["balance"] == "53"

    duplicate = manager.cash_out(session_id, round_id)

    assert duplicate["payout"] == "13"

    assert manager.get_balance(session_id)["balance"] == "53"

def test_seasonal_round_lifecycle(monkeypatch):
    monkeypatch.setattr(
        "round_manager.draw_crash_point",
        lambda mode: (3.0, None)
    )
    patch("round_manager.select_season", return_value="spring")
    monkeypatch.setattr(
        "round_manager.select_distraction",
        lambda season: "butterfly"
    )

    clock = [100.0]
    manager = RoundManager(clock=lambda: clock[0])

    session_id = manager.create_session()["session_id"]

    started = manager.start_round(
        session_id,
        Decimal("10")
    )
    round_id = started["round_id"]

    clock[0] += 2.9
    state = manager.get_round_state(session_id, round_id)

    assert state["status"] == "running"
    assert state["phase"] == "warmup"
    assert state["multiplier"] == 1.0
    assert state["can_cashout"] is False

    clock[0] += 2.1
    state = manager.get_round_state(session_id, round_id)

    assert state["phase"] == "active"
    assert state["multiplier"] == 1.27

    result = manager.cash_out(session_id, round_id)

    assert result["status"] == "cashed_out"
    assert result["payout"] == "13"
    assert result["balance"] == "53"