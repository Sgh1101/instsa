"""저장 기록(받은 게시물)과 '저장한 프로필' 목록."""
import json
import os
import stat
import sys
import threading
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "android" / "app" / "src" / "main" / "python"))

import android_api  # noqa: E402
import app as app_module  # noqa: E402
import igdl  # noqa: E402

CAR = "https://www.instagram.com/p/CAR1/"


@pytest.fixture(autouse=True)
def data(tmp_path, monkeypatch):
    monkeypatch.setenv("IGDL_DATA_DIR", str(tmp_path / "data"))
    for k in ("IG_COOKIES_FILE", "IG_COOKIES_FROM_BROWSER", "APP_TOKEN"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(igdl, "VIDEO_FORMAT", igdl.VIDEO_FORMAT)
    monkeypatch.setattr(android_api, "_lib", None)
    return tmp_path / "data"


@pytest.fixture
def client():
    app_module.app.config["TESTING"] = True
    return app_module.app.test_client()


def test_empty_library():
    assert igdl.list_profiles() == []
    assert igdl.is_saved("AbC") is False


def test_record_and_profiles_grouped_and_sorted(monkeypatch):
    clock = iter(range(1000, 1100))
    monkeypatch.setattr(igdl.time, "time", lambda: next(clock))
    igdl.record_saved("a1", "alice")
    igdl.record_saved("b1", "bob")
    igdl.record_saved("a2", "alice")
    assert igdl.is_saved("a1") and igdl.is_saved("b1") and not igdl.is_saved("zzz")
    # 가장 최근에 받은 계정이 먼저, 계정별 개수
    assert igdl.list_profiles() == [
        {"name": "alice", "count": 2, "last": 1002},
        {"name": "bob", "count": 1, "last": 1001},
    ]


def test_posts_without_uploader_are_remembered_but_not_listed():
    igdl.record_saved("x1", None)
    igdl.record_saved("x2", "")
    assert igdl.is_saved("x1") and igdl.is_saved("x2")
    assert igdl.list_profiles() == []


def test_remove_profile_forgets_only_that_account():
    igdl.record_saved("a1", "alice")
    igdl.record_saved("b1", "bob")
    igdl.remove_profile("alice")
    assert not igdl.is_saved("a1") and igdl.is_saved("b1")
    assert [p["name"] for p in igdl.list_profiles()] == ["bob"]
    igdl.remove_profile("nobody")  # 없는 계정을 지워도 오류 없음


def test_library_file_is_private_and_atomic(data):
    igdl.record_saved("a1", "alice")
    path = data / "library.json"
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    assert stat.S_IMODE(os.stat(data).st_mode) == 0o700
    assert not list(data.glob("*.tmp"))
    assert json.loads(path.read_text())["saved"]["a1"]["u"] == "alice"


@pytest.mark.parametrize("content", ["not json", "[]", '{"saved": []}', '{"saved": 3}', ""])
def test_corrupt_library_is_tolerated_and_repaired(data, content):
    data.mkdir(parents=True)
    (data / "library.json").write_text(content)
    assert igdl.list_profiles() == [] and not igdl.is_saved("a")
    igdl.record_saved("a1", "alice")  # 깨진 파일 위에도 다시 기록할 수 있어야 함
    assert igdl.is_saved("a1")


def test_concurrent_records_do_not_lose_entries():
    threads = [threading.Thread(target=igdl.record_saved, args=(f"c{i}", f"user{i % 3}")) for i in range(60)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sum(p["count"] for p in igdl.list_profiles()) == 60


# ---- 화면용 정보 / 프로필 주소 안내 ------------------------------------------
def test_post_for_ui_marks_saved(fake_instagram):
    assert igdl.post_for_ui(CAR)["saved"] is False
    igdl.record_saved("CAR1", "tester")
    assert igdl.post_for_ui(CAR)["saved"] is True


def test_profile_url_gets_a_helpful_message():
    with pytest.raises(igdl.IgError) as e:
        igdl.normalize_url("https://www.instagram.com/someone/")
    assert "프로필 전체 불러오기는 지원하지 않아요" in e.value.message and "저장한 프로필" in e.value.message


def test_fetch_post_reports_meta(fake_instagram, tmp_path):
    meta = {}
    igdl.fetch_post(CAR, tmp_path / "x", meta=meta)
    assert meta == {"code": "CAR1", "uploader": "tester"}


# ---- android_api ------------------------------------------------------------
def test_save_skip_saved_does_not_touch_instagram(fake_instagram, tmp_path, monkeypatch):
    igdl.record_saved("CAR1", "tester")
    monkeypatch.setattr(igdl, "fetch_post", lambda *a, **k: pytest.fail("이미 받은 게시물은 요청하면 안 됨"))
    r = json.loads(android_api.save(CAR, 0, str(tmp_path), True))
    assert r == {"skipped": True, "code": "CAR1"}


def test_save_skip_saved_only_applies_to_whole_posts(fake_instagram, tmp_path):
    igdl.record_saved("CAR1", "tester")
    r = json.loads(android_api.save(CAR, 2, str(tmp_path), True))  # 특정 항목 요청은 건너뛰지 않음
    assert [f["name"] for f in r["files"]] == ["CAR1_02.jpg"]
    r = json.loads(android_api.save(CAR, 0, str(tmp_path), False))  # 다시 받기
    assert len(r["files"]) == 3


def test_save_returns_code_and_uploader_but_does_not_record_until_marked(fake_instagram, tmp_path):
    r = json.loads(android_api.save(CAR, 0, str(tmp_path), False))
    assert (r["code"], r["uploader"]) == ("CAR1", "tester")
    assert not igdl.is_saved("CAR1")  # 갤러리 저장이 끝난 뒤 자바가 mark_saved를 불러야 기록됨
    android_api.mark_saved(r["code"], r["uploader"])
    assert igdl.is_saved("CAR1")


def test_save_error_carries_status_so_batches_can_stop(fake_instagram, tmp_path, monkeypatch):
    def limited(*a, **k):
        raise igdl.IgError("제한", 429)

    monkeypatch.setattr(igdl, "fetch_post", limited)
    r = json.loads(android_api.save(CAR, 0, str(tmp_path), False))
    assert r == {"error": "제한", "status": 429, "detail": ""}


def test_profiles_through_handle_and_remove():
    igdl.record_saved("a1", "alice")
    status, ctype, body = android_api.handle("GET", "/api/profiles", "")
    assert (status, ctype) == (200, "application/json")
    assert json.loads(body)["profiles"][0]["name"] == "alice"
    android_api.remove_profile("alice")
    assert json.loads(android_api.handle("GET", "/api/profiles", "")[2])["profiles"] == []


# ---- Flask(웹 버전) ----------------------------------------------------------
def test_flask_download_records_whole_posts_only(client, fake_instagram):
    client.get("/api/download", query_string={"url": CAR, "index": 2}).close()
    assert not igdl.is_saved("CAR1")  # 일부 항목만 받음
    client.get("/api/download", query_string={"url": CAR}).close()
    assert igdl.is_saved("CAR1")
    assert client.get("/api/profiles").get_json()["profiles"][0]["name"] == "tester"


def test_flask_profiles_delete(client):
    igdl.record_saved("a1", "alice")
    r = client.delete("/api/profiles", query_string={"name": "alice"})
    assert r.status_code == 200 and r.get_json() == {"profiles": []}
