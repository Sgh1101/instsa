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
    assert meta["code"] == "CAR1" and meta["uploader"] == "tester"
    assert meta["kind"] == "video"  # 묶음 안에 영상이 하나라도 있으면 video
    assert meta["thumb_url"].endswith("/small.jpg")  # 첫 항목의 작은 미리보기


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


# ---- 최근 저장 / 미리보기 -----------------------------------------------------
def test_recent_lists_newest_first_with_details(monkeypatch):
    clock = iter(range(2000, 2100))
    monkeypatch.setattr(igdl.time, "time", lambda: next(clock))
    igdl.record_saved("a1", "alice", count=3, kind="image", uri="content://media/external/images/media/7", mime="image/jpeg")
    igdl.record_saved("b1", "bob", count=1, kind="video")
    igdl.record_saved("a2", "alice")
    items = igdl.recent()
    assert [i["code"] for i in items] == ["a2", "b1", "a1"]
    a1 = items[2]
    assert a1 == {
        "code": "a1", "uploader": "alice", "t": 2000, "count": 3, "kind": "image", "thumb": False,
        "uri": "content://media/external/images/media/7", "mime": "image/jpeg",
        "url": "https://www.instagram.com/p/a1/",
    }
    assert [i["code"] for i in igdl.recent(uploader="alice")] == ["a2", "a1"]
    assert igdl.recent(uploader="nobody") == []
    assert len(igdl.recent(limit=1)) == 1
    assert len(igdl.recent(limit=0)) == 1 and len(igdl.recent(limit=10_000)) == 3  # 범위 보정


def test_old_library_entries_without_new_fields_still_listed(data):
    data.mkdir(parents=True)
    (data / "library.json").write_text('{"saved": {"old1": {"u": "alice", "t": 5}}}')
    [item] = igdl.recent()
    assert item["code"] == "old1" and item["count"] == 0 and item["thumb"] is False and item["uri"] == ""


def test_record_saved_keeps_thumbnail(fake_instagram, media_server, data):
    igdl.record_saved("T1", "alice", thumb_url=f"{media_server}/small.jpg")
    body, ctype = igdl.thumb_bytes("T1")
    assert body == b"small" and ctype == "image/jpeg"
    path = data / "thumbs" / "T1.jpg"
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    assert igdl.recent()[0]["thumb"] is True
    # 같은 게시물을 다시 저장하되 미리보기를 못 받아도 기존 파일을 유지
    igdl.record_saved("T1", "alice", thumb_url="")
    assert igdl.thumb_bytes("T1")[0] == b"small"


def test_thumbnail_failure_does_not_block_recording(fake_instagram, media_server):
    igdl.record_saved("T2", "alice", thumb_url=f"{media_server}/missing.jpg")
    assert igdl.is_saved("T2") and igdl.recent()[0]["thumb"] is False
    with pytest.raises(igdl.IgError) as e:
        igdl.thumb_bytes("T2")
    assert e.value.status == 404


def test_thumbnail_from_disallowed_host_is_ignored(data):
    igdl.record_saved("T3", "alice", thumb_url="https://evil.example/x.jpg")
    assert igdl.is_saved("T3") and not (data / "thumbs").exists()


@pytest.mark.parametrize("code", ["../x", "a/b", "", "a" * 65, "..", "a b"])
def test_thumb_paths_reject_unsafe_codes(code):
    assert igdl._thumb_file(code, "jpg") is None
    with pytest.raises(igdl.IgError):
        igdl.thumb_bytes(code)


def test_thumbs_are_pruned_to_the_newest(fake_instagram, media_server, data, monkeypatch):
    monkeypatch.setattr(igdl, "MAX_THUMBS", 2)
    clock = iter(range(3000, 3100))
    monkeypatch.setattr(igdl.time, "time", lambda: next(clock))
    for c in ("p1", "p2", "p3"):
        igdl.record_saved(c, "alice", thumb_url=f"{media_server}/small.jpg")
    assert sorted(p.name for p in (data / "thumbs").iterdir()) == ["p2.jpg", "p3.jpg"]
    assert igdl.is_saved("p1")  # 기록은 남고 미리보기 파일만 사라짐
    by_code = {i["code"]: i for i in igdl.recent()}
    assert by_code["p1"]["thumb"] is False and by_code["p3"]["thumb"] is True


def test_remove_profile_deletes_its_thumbnails(fake_instagram, media_server, data):
    igdl.record_saved("a1", "alice", thumb_url=f"{media_server}/small.jpg")
    igdl.record_saved("b1", "bob", thumb_url=f"{media_server}/small.jpg")
    igdl.remove_profile("alice")
    assert [p.name for p in (data / "thumbs").iterdir()] == ["b1.jpg"]


def test_server_recent_and_thumbs_endpoints(client, fake_instagram, media_server):
    igdl.record_saved("S1", "alice", count=2, kind="video", thumb_url=f"{media_server}/small.jpg")
    igdl.record_saved("S2", "bob")
    r = client.get("/api/recent?u=alice")
    assert [i["code"] for i in r.get_json()["items"]] == ["S1"]
    assert len(client.get("/api/recent").get_json()["items"]) == 2
    t = client.get("/api/thumbs/S1")
    assert t.status_code == 200 and t.data == b"small" and t.content_type == "image/jpeg"
    assert client.get("/api/thumbs/S2").status_code == 404
    assert client.get("/api/thumbs/..%2Fetc").status_code in (400, 404)


def test_android_api_recent_and_thumbs(fake_instagram, media_server):
    igdl.record_saved("S1", "alice", count=2, kind="video", thumb_url=f"{media_server}/small.jpg")
    status, ctype, body = android_api.handle("GET", "/api/recent", "u=alice")
    assert status == 200 and json.loads(body)["items"][0]["code"] == "S1"
    status, ctype, body = android_api.handle("GET", "/api/thumbs/S1", "")
    assert (status, ctype, body) == (200, "image/jpeg", b"small")
    assert android_api.handle("GET", "/api/thumbs/nope", "")[0] == 404


def test_mark_saved_passes_extras_through(fake_instagram, media_server):
    android_api.mark_saved("M1", "alice", f"{media_server}/small.jpg", 2, "image", "content://media/external/images/media/1", "image/jpeg")
    [item] = igdl.recent()
    assert (item["count"], item["kind"], item["thumb"], item["mime"]) == (2, "image", True, "image/jpeg")
    assert item["uri"].startswith("content://media/")
