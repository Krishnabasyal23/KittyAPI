"""Season rule texts in English and French (the bonus numbers are always the server's)."""

import main
from fastapi.testclient import TestClient
from season_rules import bonus_percents, describe

client = TestClient(main.app)


def test_french_rules_carry_the_same_bonus():
    fr = describe("spring", "fr")
    assert fr["title"] == "Chasse au papillon"
    assert f"+{bonus_percents()['butterfly']} %" in fr["rule"]
    assert describe("spring", "xx") == describe("spring")


def test_forecast_in_french():
    sid = client.post("/api/sessions").json()["session_id"]
    body = client.get("/api/season/next?lang=fr", headers={"X-Session-ID": sid}).json()
    assert body["title"] in {"Chasse au papillon", "Folie d'été", "Tempête de feuilles", "Chocolat chaud"}
    assert all(u["title"] for u in body["upcoming"])


def test_crowd_state_accepts_lang():
    assert client.get("/api/crowd/state?lang=fr").status_code == 200
