import pytest
import main
from round_manager import RoundManager
from fastapi.testclient import TestClient
from main import app

@pytest.fixture(autouse=True)
def fresh_api_manager(monkeypatch):
    monkeypatch.setattr(
        main,
        "manager",
        RoundManager()
    )
    
client = TestClient(app)

def test_health():
    response = client.get("/api/health")

    assert response.status_code == 200
    assert response.json()["status"] == "ok"

def test_create_session():
    response = client.post("/api/sessions")

    assert response.status_code == 201

    data = response.json()

    assert "session_id" in data
    assert data["balance"] == "50"

def test_missing_session():
    response = client.get(
        "/api/player/balance"
    )

    assert response.status_code == 422

def test_invalid_wager():
    session = client.post(
        "/api/sessions"
    ).json()

    headers = {
        "X-Session-ID": session["session_id"]
    }

    response = client.post(
        "/api/rounds",
        headers=headers,
        json={"wager": "-100.00"}
    )

    assert response.status_code == 422

def test_round_start(monkeypatch):
    import main
    from round_manager import RoundManager

    clock = [100.0]

    monkeypatch.setattr(
        main,
        "manager",
        RoundManager(clock=lambda: clock[0])
    )

    monkeypatch.setattr(
        "round_manager.draw_crash_point",
        lambda mode: (3.0, None)
    )

    monkeypatch.setattr(
        "round_manager.select_distraction",
        lambda season: "fish"
    )

    session = client.post("/api/sessions").json()

    response = client.post(
        "/api/rounds",
        headers={
            "X-Session-ID": session["session_id"]
        },
        json={"wager": "10"}
    )

    assert response.status_code == 201
    assert response.json()["balance"] == "40"
    assert "crash_point" not in response.json()


def test_health_reports_version():
    assert client.get("/api/health").json()["version"] == main.VERSION
