"""Phones get the crowd state pushed over a WebSocket (same content as /api/crowd/state)."""

import main
from fastapi.testclient import TestClient

client = TestClient(main.app)


def test_ws_pushes_the_same_state_as_the_poll():
    polled = client.get("/api/crowd/state?lang=fr").json()
    with client.websocket_connect("/ws/crowd?lang=fr") as ws:
        pushed = ws.receive_json()
    assert pushed.keys() == polled.keys()
    assert pushed["phase"] == polled["phase"]


def test_ws_knows_the_player():
    joined = client.post("/api/crowd/join", json={"name": "Socket", "age_confirmed": True}).json()
    with client.websocket_connect(f"/ws/crowd?player_id={joined['player_id']}") as ws:
        state = ws.receive_json()
    assert state["you"]["name"] == joined["name"]
