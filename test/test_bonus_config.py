import pytest

import main
import season_rules
from fastapi.testclient import TestClient

BUTTERFLY_AT_2S = {"butterfly": [1.0, 3.0], "leaf_storms": []}


@pytest.fixture
def no_happy_hour(monkeypatch):
    monkeypatch.delenv("HAPPY_HOUR", raising=False)


@pytest.fixture
def happy_hour_on(monkeypatch):
    monkeypatch.setenv("HAPPY_HOUR", "1")


def test_default_bonuses_are_safe(no_happy_hour):
    assert season_rules.bonus_factors() == {"butterfly": 1.04, "cocoa": 1.04}
    assert season_rules.bonus_percents() == {"butterfly": 4, "cocoa": 4}
    # 96% base RTP x worst-case bonus must stay under 100%
    assert 0.96 * max(season_rules.bonus_factors().values()) < 1.0

    assert season_rules.apply_bonus("spring", BUTTERFLY_AT_2S, 2.0, 2.00) == (2.08, "butterfly")
    assert season_rules.apply_bonus("winter", BUTTERFLY_AT_2S, 2.0, 2.50) == (2.60, "cocoa")
    assert "+4%" in season_rules.describe("spring")["rule"]
    assert "+4%" in season_rules.describe("winter")["rule"]


def test_happy_hour_restores_demo_bonuses(happy_hour_on):
    assert season_rules.bonus_factors() == {"butterfly": 1.25, "cocoa": 1.10}
    assert season_rules.apply_bonus("spring", BUTTERFLY_AT_2S, 2.0, 2.00) == (2.50, "butterfly")
    assert season_rules.apply_bonus("winter", BUTTERFLY_AT_2S, 2.0, 2.50) == (2.75, "cocoa")
    assert "+25%" in season_rules.describe("spring")["rule"]
    assert "+10%" in season_rules.describe("winter")["rule"]


@pytest.mark.parametrize("value", ["0", "", "yes", "true"])
def test_only_exactly_one_turns_happy_hour_on(monkeypatch, value):
    monkeypatch.setenv("HAPPY_HOUR", value)
    assert season_rules.happy_hour() is False


def test_health_shows_bonus_mode(no_happy_hour, monkeypatch):
    client = TestClient(main.app)
    body = client.get("/api/health").json()
    assert body["happy_hour"] is False and body["promo"] is None
    assert body["bonus_pct"] == {"butterfly": 4, "cocoa": 4}

    monkeypatch.setenv("HAPPY_HOUR", "1")
    body = client.get("/api/health").json()
    assert body["happy_hour"] is True
    assert body["promo"] == "HAPPY HOUR (demo)"
    assert body["bonus_pct"] == {"butterfly": 25, "cocoa": 10}
