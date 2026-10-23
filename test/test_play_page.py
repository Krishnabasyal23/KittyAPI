from fastapi.testclient import TestClient

import main

client = TestClient(main.app)


def test_play_page_is_missing_without_a_build(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "PLAY_DIR", tmp_path)
    assert client.get("/play/").status_code == 404


def test_play_page_fills_the_window(tmp_path, monkeypatch):
    (tmp_path / "index.html").write_text("<html><head><title>x</title></head><body>game</body></html>", encoding="utf-8")
    monkeypatch.setattr(main, "PLAY_DIR", tmp_path)
    r = client.get("/play/")
    assert r.status_code == 200
    assert "#unity-canvas{width:100%!important" in r.text
    assert r.text.index("<style>") < r.text.index("</head>")
    assert "game" in r.text
