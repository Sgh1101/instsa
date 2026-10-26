import io
import tempfile
import threading
import time
import zipfile
from pathlib import Path

import pytest
import requests
from werkzeug.serving import make_server

import app as app_module
from conftest import JPEG


@pytest.fixture
def client():
    app_module.app.config["TESTING"] = True
    return app_module.app.test_client()


def test_index_served(client):
    r = client.get("/")
    assert r.status_code == 200 and "인스타 다운로더" in r.get_data(as_text=True)


def test_pwa_manifest_and_icons(client):
    r = client.get("/manifest.webmanifest")
    assert r.status_code == 200 and r.mimetype == "application/manifest+json"
    data = r.get_json(force=True)
    # 인스타그램 앱의 공유 시트에서 바로 받을 수 있게 하는 공유 대상 등록
    assert data["share_target"]["method"] == "GET"
    assert data["share_target"]["params"]["url"] == "url"
    assert {i["sizes"] for i in data["icons"]} >= {"192x192", "512x512"}
    for icon in data["icons"]:
        assert client.get(icon["src"]).status_code == 200, icon["src"]


def test_service_worker_served_from_root(client):
    r = client.get("/sw.js")
    assert r.status_code == 200 and "javascript" in r.mimetype
    assert r.headers["Cache-Control"] == "no-cache"


def test_info_proxies_thumbnails(client, fake_instagram):
    r = client.post("/api/info", json={"url": "https://www.instagram.com/p/CAR1/"})
    assert r.status_code == 200
    items = r.get_json()["items"]
    assert len(items) == 3
    assert all(i["thumbnail"].startswith("/api/thumb?u=") for i in items)  # 원본 CDN 주소를 그대로 노출하지 않음


def test_info_rejects_bad_url(client):
    r = client.post("/api/info", json={"url": "https://example.com/p/abc/"})
    assert r.status_code == 400 and "error" in r.get_json()


def test_info_without_body(client):
    assert client.post("/api/info").status_code == 400


def test_thumb_blocks_non_cdn_hosts(client):
    for u in ("http://127.0.0.1:1/x.jpg", "https://evil.example.com/x.jpg", "https://169.254.169.254/"):
        assert client.get("/api/thumb", query_string={"u": u}).status_code == 400


def test_download_single_image(client, fake_instagram):
    r = client.get("/api/download", query_string={"url": "https://www.instagram.com/p/IMG1/"})
    assert r.status_code == 200
    assert r.data == JPEG
    assert 'filename=IMG1_01.jpg' in r.headers["Content-Disposition"]


def test_download_carousel_item(client, fake_instagram):
    r = client.get("/api/download", query_string={"url": "https://www.instagram.com/p/CAR1/", "index": 2})
    assert r.status_code == 200 and r.data == JPEG
    assert "CAR1_02.jpg" in r.headers["Content-Disposition"]


def test_download_all_zip(client, fake_instagram):
    r = client.get("/api/download", query_string={"url": "https://www.instagram.com/p/CAR1/"})
    assert r.status_code == 200
    assert "CAR1.zip" in r.headers["Content-Disposition"]
    with zipfile.ZipFile(io.BytesIO(r.data)) as z:
        assert z.namelist() == ["CAR1_01.mp4", "CAR1_02.jpg", "CAR1_03.jpg"]


def test_download_error_is_json_and_cleans_up(client, fake_instagram, tmp_path, monkeypatch):
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    r = client.get("/api/download", query_string={"url": "https://www.instagram.com/p/NOPE/"})
    assert r.status_code == 404 and "error" in r.get_json()
    assert list(tmp_path.iterdir()) == []  # 실패해도 임시 폴더가 남지 않음


def test_download_cleans_up_after_success(client, fake_instagram, tmp_path, monkeypatch):
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    r = client.get("/api/download", query_string={"url": "https://www.instagram.com/p/IMG1/"})
    r.close()
    assert list(Path(tmp_path).iterdir()) == []


def test_download_cleans_up_with_real_server(fake_instagram, tmp_path, monkeypatch):
    """회귀 테스트: send_file + call_on_close 조합은 실제 서버에서 임시 폴더를 남겼습니다."""
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    srv = make_server("127.0.0.1", 0, app_module.app, threaded=True)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        r = requests.get(
            f"http://127.0.0.1:{srv.server_port}/api/download",
            params={"url": "https://www.instagram.com/p/CAR1/"},
            timeout=30,
        )
        assert r.status_code == 200 and int(r.headers["Content-Length"]) == len(r.content)
        for _ in range(60):  # 서버 스레드가 정리를 마칠 때까지 잠깐 기다립니다
            if not list(tmp_path.iterdir()):
                break
            time.sleep(0.05)
        assert list(tmp_path.iterdir()) == []
    finally:
        srv.shutdown()
