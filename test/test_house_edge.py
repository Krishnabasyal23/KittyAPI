from fastapi.testclient import TestClient

import main
from crash_engine import HOUSE_EDGE, HOUSE_EDGE_PCT

client = TestClient(main.app)


def test_health_says_the_house_edge():
    h = client.get("/api/health").json()
    assert h["house_edge_pct"] == HOUSE_EDGE_PCT == 4
    assert h["rtp_pct"] == 100 - h["house_edge_pct"] == 96


def test_the_published_edge_is_what_the_crash_formula_uses():
    from crash_engine import crash_from_uniform
    # P(crash >= m) = (1 - edge) / m: at u just under the instant-crash line the point is 1.00x
    assert abs((1 - HOUSE_EDGE) * 100 - (100 - HOUSE_EDGE_PCT)) < 1e-9
    assert crash_from_uniform(0.96, HOUSE_EDGE) == 1.0


def test_crowd_state_carries_it_for_the_phone_rules():
    s = client.get("/api/crowd/state").json()
    assert s["house_edge_pct"] == 4
