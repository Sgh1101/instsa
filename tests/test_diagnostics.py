"""실패했을 때 원인을 알 수 있게 하는 부분: 원문(detail), 최근 오류 기록, 진단 보고서."""
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "android" / "app" / "src" / "main" / "python"))

import android_api  # noqa: E402
import igdl  # noqa: E402

RAW_LOGIN = (
    "ERROR: [Instagram] AbC: Instagram sent an empty media response. Check if this post is accessible "
    "in your browser without being logged-in."
)


@pytest.fixture(autouse=True)
def fresh(tmp_path, monkeypatch):
    monkeypatch.setattr(igdl, "VIDEO_FORMAT", igdl.VIDEO_FORMAT)
    monkeypatch.setattr(android_api, "_lib", None)
    monkeypatch.setattr(android_api, "_ERRORS", [])
    for k in ("IG_COOKIES_FILE", "IG_COOKIES_FROM_BROWSER"):
        monkeypatch.delenv(k, raising=False)
    android_api.init(str(tmp_path / "data"))


@pytest.fixture
def offline(monkeypatch):
    """진단의 인스타그램 접속 시험을 실제 네트워크 없이 돌립니다."""
    import requests

    class R:
        status_code = 200

    monkeypatch.setattr(requests, "get", lambda *a, **k: R())


def test_translated_errors_keep_the_raw_message_as_detail():
    e = igdl._translate(RAW_LOGIN)
    assert e.status == 401 and "쿠키" in e.message
    assert "empty media response" in e.detail and not e.detail.startswith("ERROR")
    assert igdl._translate("ERROR: [Instagram] X: HTTP Error 429: Too Many Requests").detail.endswith("Too Many Requests")
    assert igdl._translate("ERROR: [Instagram] X: HTTP Error 404: Not Found").detail
    assert igdl._translate("ERROR: [Instagram] X: something odd").detail == "something odd"


def test_handle_returns_detail_and_logs(monkeypatch):
    def limited(_):
        raise igdl.IgError("제한됨", 429, "HTTP Error 429: Too Many Requests")

    monkeypatch.setattr(igdl, "post_for_ui", limited)
    status, _, raw = android_api.handle("GET", "/api/info", "url=https%3A%2F%2Fwww.instagram.com%2Fp%2FAbC%2F")
    data = json.loads(raw)
    assert status == 429 and data["detail"] == "HTTP Error 429: Too Many Requests"
    assert android_api._ERRORS[-1]["where"] == "조회" and "AbC" in android_api._ERRORS[-1]["url"]


def test_save_failure_has_detail_and_is_logged(monkeypatch, tmp_path):
    def limited(*a, **k):
        raise igdl.IgError("제한됨", 429, "HTTP Error 429")

    monkeypatch.setattr(igdl, "fetch_post", limited)
    r = json.loads(android_api.save("https://www.instagram.com/p/AbC/", 0, str(tmp_path), False))
    assert r["status"] == 429 and r["detail"] == "HTTP Error 429"
    assert android_api._ERRORS[-1]["where"] == "저장"


def test_error_log_keeps_only_the_latest():
    for i in range(25):
        android_api.log_error("저장", f"u{i}", f"m{i}")
    assert len(android_api._ERRORS) == 10 and android_api._ERRORS[-1]["message"] == "m24"


def test_diagnose_reports_environment_and_recent_errors(offline):
    android_api.log_error("저장", "https://www.instagram.com/p/AbC/", "제한됨", "HTTP Error 429")
    text = android_api.diagnose()
    for must in ("파이썬 3.", "yt-dlp", "requests", "certifi", "인증서 파일: 있음", "쓰기 가능", "로그인 쿠키: 없음", "인스타그램 접속: HTTP 200",
                 "최근 오류 1건", "[저장] 제한됨", "HTTP Error 429"):
        assert must in text, must


def test_diagnose_shows_network_and_tls_failures_clearly(monkeypatch):
    import requests

    def tls(*a, **k):
        raise requests.exceptions.SSLError("certificate verify failed")

    monkeypatch.setattr(requests, "get", tls)
    assert "인스타그램 접속 실패: SSLError: certificate verify failed" in android_api.diagnose()


def test_diagnose_can_test_one_post(offline, fake_instagram):
    assert "게시물 조회 시험: 성공 (항목 3개, 계정 tester)" in android_api.diagnose("https://www.instagram.com/p/CAR1/")
    out = android_api.diagnose("https://www.instagram.com/p/NOPE/")
    assert "게시물 조회 시험: 실패 [404]" in out and "원문:" in out


def test_diagnose_through_the_app_route(offline):
    status, ctype, raw = android_api.handle("GET", "/api/diag", "")
    assert (status, ctype) == (200, "application/json") and "[진단" in json.loads(raw)["text"]
