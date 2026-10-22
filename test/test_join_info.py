from fastapi.testclient import TestClient

import main

client = TestClient(main.app, base_url="http://catino.example.com")


def test_join_info_uses_public_url_when_set(monkeypatch):
    monkeypatch.setenv("PUBLIC_URL", "https://play.example.org/")
    info = client.get("/api/crowd/join-info").json()
    assert info["join_url"] == "https://play.example.org/crowd"
    assert isinstance(info["qr"], list)


def test_join_info_follows_the_request_host_and_proxy_scheme(monkeypatch):
    monkeypatch.delenv("PUBLIC_URL", raising=False)
    assert client.get("/api/crowd/join-info").json()["join_url"] == "http://catino.example.com/crowd"
    forwarded = client.get("/api/crowd/join-info", headers={"X-Forwarded-Proto": "https"}).json()
    assert forwarded["join_url"] == "https://catino.example.com/crowd"


def test_join_info_does_not_take_the_host_seat(monkeypatch):
    monkeypatch.delenv("PUBLIC_URL", raising=False)
    before = main.crowd.host_session
    client.get("/api/crowd/join-info")
    assert main.crowd.host_session == before
