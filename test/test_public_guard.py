"""PUBLIC_MODE: the server computer's pages stay shut when a tunnel forwards requests, and abuse is limited."""

import pytest

from fastapi.testclient import TestClient

import guard
import main

# A tunnel connects from this computer, so only the headers and the host name give it away.
local = TestClient(main.app, base_url="http://localhost", client=("127.0.0.1", 51000))
tunnel = TestClient(main.app, base_url="https://catino.example.com", client=("127.0.0.1", 51001))
outside = TestClient(main.app, client=("203.0.113.9", 51002))


@pytest.fixture(autouse=True)
def fresh(monkeypatch):
    monkeypatch.delenv("PUBLIC_MODE", raising=False)
    guard.reset()
    yield
    guard.reset()


def test_local_pages_open_on_the_server_computer():
    assert local.get("/operator").status_code == 200
    assert local.get("/cashier").status_code == 200


@pytest.mark.parametrize("header", ["X-Forwarded-For", "CF-Connecting-IP", "X-Real-IP", "Forwarded", "CF-Ray"])
def test_forwarded_requests_are_not_local(header):
    r = local.get("/operator", headers={header: "198.51.100.7"})
    assert r.status_code == 403
    assert local.get("/api/operator/summary", headers={header: "198.51.100.7"}).status_code == 403


def test_a_tunnel_host_name_is_not_local():
    assert tunnel.get("/operator").status_code == 403
    assert tunnel.get("/cashier").status_code == 403
    assert tunnel.post("/api/cashier/lookup", json={"code": "x"}).status_code == 403


def test_public_mode_closes_them_even_for_a_local_looking_request(monkeypatch):
    monkeypatch.setenv("PUBLIC_MODE", "1")
    assert local.get("/operator").status_code == 403
    assert local.get("/cashier").status_code == 403
    assert local.get("/api/operator/bonus").status_code == 403


def test_the_game_itself_still_works_in_public_mode(monkeypatch):
    monkeypatch.setenv("PUBLIC_MODE", "1")
    assert outside.get("/api/health").status_code == 200
    assert outside.get("/crowd").status_code == 200
    assert outside.post("/api/sessions").status_code == 201


def test_demo_purchases_need_an_explicit_opt_in_on_a_public_server(monkeypatch):
    from payments import demo_mode
    monkeypatch.setenv("DEMO_MODE", "1")
    assert demo_mode()
    monkeypatch.setenv("PUBLIC_MODE", "1")
    assert not demo_mode()
    monkeypatch.setenv("ALLOW_PUBLIC_DEMO", "1")
    assert demo_mode()


def test_wallet_creation_is_rate_limited_per_real_client(monkeypatch):
    monkeypatch.setenv("PUBLIC_MODE", "1")
    limit = guard.STRICT_LIMITS[("POST", "/api/sessions")][0]
    codes = [tunnel.post("/api/sessions", headers={"CF-Connecting-IP": "198.51.100.1"}).status_code
             for _ in range(limit + 3)]
    assert codes[:limit] == [201] * limit
    assert set(codes[limit:]) == {429}
    # another visitor behind the same tunnel is not affected
    assert tunnel.post("/api/sessions", headers={"CF-Connecting-IP": "198.51.100.2"}).status_code == 201


def test_limits_are_off_for_the_room_demo():
    for _ in range(guard.STRICT_LIMITS[("POST", "/api/sessions")][0] + 5):
        assert outside.post("/api/sessions").status_code == 201


def test_rate_window_slides():
    assert guard.check_rate("GET", "/x", "k", now=0.0)
    limit, window = guard.GENERAL_LIMIT
    for i in range(limit - 1):
        assert guard.check_rate("GET", "/x", "k", now=1.0)
    assert not guard.check_rate("GET", "/x", "k", now=2.0)
    assert guard.check_rate("GET", "/x", "k", now=window + 3.0)


def test_big_bodies_are_refused_in_public_mode(monkeypatch):
    monkeypatch.setenv("PUBLIC_MODE", "1")
    r = outside.post("/api/crowd/join", content=b"x" * (guard.MAX_BODY_BYTES + 10),
                     headers={"content-type": "application/json"})
    assert r.status_code == 413


def test_safe_headers_are_always_sent():
    r = outside.get("/api/health")
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["x-frame-options"] == "DENY"


def test_websocket_slots_are_limited_per_client(monkeypatch):
    monkeypatch.setenv("PUBLIC_MODE", "1")
    for _ in range(guard.WS_PER_CLIENT):
        assert guard.ws_acquire("1.2.3.4")
    assert not guard.ws_acquire("1.2.3.4")
    assert guard.ws_acquire("5.6.7.8")
    guard.ws_release("1.2.3.4")
    assert guard.ws_acquire("1.2.3.4")


def test_only_the_game_on_this_computer_can_host_a_public_table(monkeypatch):
    monkeypatch.setenv("PUBLIC_MODE", "1")
    sid = outside.post("/api/sessions").json()["session_id"]
    assert outside.post("/api/crowd/host", headers={"X-Session-ID": sid}).status_code == 403
    assert tunnel.post("/api/crowd/host", headers={"X-Session-ID": sid, "CF-Connecting-IP": "198.51.100.3"}).status_code == 403
    assert outside.post("/api/crowd/open", json={"seconds": 5}, headers={"X-Session-ID": sid}).status_code == 403
    mine = local.post("/api/sessions").json()["session_id"]
    assert local.post("/api/crowd/host", headers={"X-Session-ID": mine}).status_code == 200
