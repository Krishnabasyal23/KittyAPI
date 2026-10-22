"""The forecast shows the next three walks' seasons, and those really are the seasons that come."""

import itertools

import pytest

import main
from fastapi.testclient import TestClient

client = TestClient(main.app)


@pytest.fixture
def cycling_seasons(monkeypatch):
    seasons = itertools.cycle(["spring", "summer", "fall", "winter"])
    monkeypatch.setattr("round_manager.select_season", lambda: next(seasons))


def test_forecast_lists_three_upcoming_seasons(cycling_seasons):
    sid = client.post("/api/sessions").json()["session_id"]
    body = client.get("/api/season/next", headers={"X-Session-ID": sid}).json()
    upcoming = [u["season"] for u in body["upcoming"]]
    assert len(upcoming) == 3 and body["season"] == upcoming[0]
    assert all(u["rule"] for u in body["upcoming"])


def test_walks_use_the_forecast_in_order(cycling_seasons):
    sid = client.post("/api/sessions").json()["session_id"]
    h = {"X-Session-ID": sid}
    client.post("/api/wallet/age-confirm", headers=h, json={"confirm": True})
    planned = [u["season"] for u in client.get("/api/season/next", headers=h).json()["upcoming"]]
    used = []
    for _ in range(2):
        rid = client.post("/api/rounds", headers=h, json={"wager": "1"}).json()["round_id"]
        used.append(main.store.get_round(sid, rid)["season"])
        main.manager._session(sid)["active_round_id"] = None       # let the next walk start at once
        main.store.settle_round(sid, rid, "crashed")
        after = [u["season"] for u in client.get("/api/season/next", headers=h).json()["upcoming"]]
        assert after[:3 - len(used)] == planned[len(used):]   # the rest of the forecast moves up
    assert used == planned[:2]


def test_host_pick_changes_only_the_next_walk(cycling_seasons):
    sid = client.post("/api/sessions").json()["session_id"]
    h = {"X-Session-ID": sid}
    before = [u["season"] for u in client.get("/api/season/next", headers=h).json()["upcoming"]]
    client.post("/api/season/next", headers=h, json={"season": "winter"})
    after = [u["season"] for u in client.get("/api/season/next", headers=h).json()["upcoming"]]
    assert after == ["winter"] + before[1:]
