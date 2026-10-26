"""안드로이드 앱(Chaquopy) 안에서 쓰는 android_api를 호스트에서 검증합니다. Flask 없이 igdl만 부릅니다."""
import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
API_DIR = ROOT / "android" / "app" / "src" / "main" / "python"
sys.path.insert(0, str(API_DIR))

import android_api  # noqa: E402
import igdl  # noqa: E402

from conftest import JPEG, MP4  # noqa: E402


@pytest.fixture(autouse=True)
def fresh_api(monkeypatch, tmp_path):
    # android_api가 igdl.VIDEO_FORMAT을 바꾸므로 다른 테스트에 새지 않게 되돌립니다.
    monkeypatch.setattr(igdl, "VIDEO_FORMAT", igdl.VIDEO_FORMAT)
    monkeypatch.setattr(android_api, "_lib", None)
    for k in ("IG_COOKIES_FILE", "IG_COOKIES_FROM_BROWSER"):
        monkeypatch.delenv(k, raising=False)
    android_api.init(str(tmp_path / "appdata"))
    return tmp_path


def body(resp):
    status, ctype, data = resp
    return status, ctype, json.loads(data) if ctype == "application/json" else data


def test_uses_single_file_mp4_format_because_android_has_no_ffmpeg():
    android_api._igdl()
    assert igdl.VIDEO_FORMAT == "b[ext=mp4]/b"
    assert "bv*" not in igdl.VIDEO_FORMAT  # 영상/음성을 합쳐야 하는 포맷은 받지 않음


def test_android_modules_do_not_need_flask():
    code = "import android_api, igdl, sys; android_api._igdl(); assert 'flask' not in sys.modules, 'flask imported'"
    r = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True,
                       env={"PATH": "", "PYTHONPATH": f"{API_DIR}:{ROOT}", "HOME": str(ROOT)})
    assert r.returncode == 0, r.stderr


def test_info(fake_instagram):
    status, ctype, data = body(android_api.handle("GET", "/api/info", "url=https%3A%2F%2Fwww.instagram.com%2Fp%2FCAR1%2F"))
    assert (status, ctype) == (200, "application/json")
    assert [i["kind"] for i in data["items"]] == ["video", "image", "image"]
    assert all(i["thumbnail"].startswith("/api/thumb?u=") for i in data["items"])
    assert data["uploader"] == "tester" and data["code"] == "CAR1"


def test_info_korean_error_and_status(fake_instagram):
    status, _, data = body(android_api.handle("GET", "/api/info", "url=https%3A%2F%2Fexample.com%2Fp%2Fabc%2F"))
    assert status == 400 and "인스타그램" in data["error"]
    status, _, data = body(android_api.handle("GET", "/api/info", "url=https%3A%2F%2Fwww.instagram.com%2Fp%2FNOPE%2F"))
    assert status == 404


def test_thumb_proxy_and_ssrf_guard(fake_instagram, media_server, monkeypatch):
    status, ctype, data = android_api.handle("GET", "/api/thumb", "u=" + f"{media_server}/big.jpg".replace(":", "%3A").replace("/", "%2F"))
    assert (status, ctype) == (200, "image/jpeg") and data == JPEG
    monkeypatch.undo()  # 허용 목록을 원래대로(로컬 서버 허용 해제)
    status, _, data = body(android_api.handle("GET", "/api/thumb", "u=http%3A%2F%2F127.0.0.1%3A1%2Fx.jpg"))
    assert status == 400


def test_unknown_path_and_method(fake_instagram):
    assert body(android_api.handle("GET", "/api/nope", ""))[0] == 404
    assert body(android_api.handle("POST", "/api/info", "url=x"))[0] == 404  # 앱은 GET만 씁니다


def test_save_carousel_returns_gallery_names_and_mimes(fake_instagram, tmp_path):
    cache = tmp_path / "cache"
    cache.mkdir()
    r = json.loads(android_api.save("https://www.instagram.com/p/CAR1/?igsh=xyz", 0, str(cache)))
    assert [f["name"] for f in r["files"]] == ["CAR1_01.mp4", "CAR1_02.jpg", "CAR1_03.jpg"]
    assert [f["mime"] for f in r["files"]] == ["video/mp4", "image/jpeg", "image/jpeg"]
    assert Path(r["files"][0]["path"]).read_bytes() == MP4
    assert Path(r["files"][1]["path"]).read_bytes() == JPEG
    assert Path(r["dir"]).parent == cache  # 자바가 갤러리에 넣은 뒤 이 폴더를 지웁니다


def test_save_single_item(fake_instagram, tmp_path):
    r = json.loads(android_api.save("https://www.instagram.com/p/CAR1/", 3, str(tmp_path)))
    assert [f["name"] for f in r["files"]] == ["CAR1_03.jpg"]


def test_save_errors_are_json_and_leave_no_temp_dir(fake_instagram, tmp_path):
    cache = tmp_path / "c"
    cache.mkdir()
    r = json.loads(android_api.save("https://www.instagram.com/p/NOPE/", 0, str(cache)))
    assert "error" in r and "files" not in r
    assert list(cache.iterdir()) == []  # 실패해도 임시 폴더가 남지 않음
    r = json.loads(android_api.save("https://example.com/x", 0, str(cache)))
    assert "인스타그램" in r["error"]


def test_cookie_roundtrip(fake_instagram):
    sid = "12345678901%3AabcDEFghiJKL%3A27%3AAYfSignatureValue123"
    assert body(android_api.handle("GET", "/api/cookies", ""))[2] == {"configured": False, "source": None}
    assert json.loads(android_api.save_cookies(sid, "")) == {"ok": True}
    assert body(android_api.handle("GET", "/api/cookies", ""))[2] == {"configured": True, "source": "app"}
    bad = json.loads(android_api.save_cookies("nope", ""))
    assert bad["ok"] is False and "sessionid" in bad["error"]
    android_api.clear_cookies()
    assert body(android_api.handle("GET", "/api/cookies", ""))[2]["configured"] is False


def test_unexpected_exception_does_not_leak_details(fake_instagram, monkeypatch):
    def boom(_):
        raise RuntimeError("secret internal path /data/x")

    monkeypatch.setattr(igdl, "post_for_ui", boom)
    status, _, data = body(android_api.handle("GET", "/api/info", "url=x"))
    assert status == 500 and "secret" not in json.dumps(data)
