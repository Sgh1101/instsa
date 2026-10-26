import os
import stat

import pytest
import yt_dlp

import app as app_module
import igdl
from igdl import IgError

SESSIONID = "12345678901%3AabcDEFghiJKL%3A27%3AAYfSignatureValue123"
CSRF = "AbCdEfGhIjKlMnOpQrStUvWxYz012345"


@pytest.fixture(autouse=True)
def data_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("IGDL_DATA_DIR", str(tmp_path / "data"))
    for k in ("IG_COOKIES_FILE", "IG_COOKIES_FROM_BROWSER", "APP_TOKEN"):
        monkeypatch.delenv(k, raising=False)
    return tmp_path / "data"


@pytest.fixture
def client():
    app_module.app.config["TESTING"] = True
    return app_module.app.test_client()


def test_status_empty(client):
    assert client.get("/api/cookies").get_json() == {"configured": False, "source": None}


def test_save_writes_private_netscape_file(data_dir):
    igdl.save_cookies(SESSIONID, CSRF)
    path = data_dir / "cookies.txt"
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    assert stat.S_IMODE(os.stat(data_dir).st_mode) == 0o700
    text = path.read_text()
    assert text.startswith("# Netscape HTTP Cookie File")
    assert f"sessionid\t{SESSIONID}" in text and f"csrftoken\t{CSRF}" in text
    assert not list(data_dir.glob("*.tmp"))


def test_yt_dlp_really_sees_the_login_cookie(data_dir):
    """yt-dlp의 Instagram 추출기가 '로그인됨'으로 인식하는지, 실제 yt-dlp 코드로 확인합니다."""
    igdl.save_cookies(SESSIONID, CSRF)
    with igdl._ydl(igdl._opts()) as ydl:
        ie = ydl.get_info_extractor("Instagram")
        assert ie._is_logged_in is True
        cookies = ie._get_cookies("https://www.instagram.com")
        assert cookies["sessionid"].value == SESSIONID
        assert cookies["csrftoken"].value == CSRF


def test_no_cookie_means_not_logged_in():
    with igdl._ydl(igdl._opts()) as ydl:
        assert ydl.get_info_extractor("Instagram")._is_logged_in is False


def test_cookie_file_is_not_rewritten_by_yt_dlp(data_dir):
    igdl.save_cookies(SESSIONID, CSRF)
    path = data_dir / "cookies.txt"
    before = path.read_bytes()
    with igdl._ydl(igdl._opts()):
        pass
    assert path.read_bytes() == before  # 동시 요청이 파일을 덮어써 깨뜨리지 않도록 읽기 전용


@pytest.mark.parametrize("raw", [f"sessionid={SESSIONID};", f"  {SESSIONID}  ", f"sessionid={SESSIONID}"])
def test_save_accepts_pasted_variants(data_dir, raw):
    igdl.save_cookies(raw)
    assert f"sessionid\t{SESSIONID}\n" in (data_dir / "cookies.txt").read_text()


@pytest.mark.parametrize("bad", ["", "short", "has space in it aaaaaaaaaaaa", "tab\tinjection\tsessionid\tx1234567890", "a" * 600,
                                 "line\nbreak1234567890abcdef", "한글한글한글한글한글한글한글한글"])
def test_save_rejects_bad_sessionid(bad):
    with pytest.raises(IgError):
        igdl.save_cookies(bad)


def test_save_rejects_bad_csrf_and_writes_nothing(data_dir):
    with pytest.raises(IgError):
        igdl.save_cookies(SESSIONID, "bad value\twith tab")
    assert not (data_dir / "cookies.txt").exists()


def test_api_put_get_delete_never_leaks_value(client):
    r = client.put("/api/cookies", json={"sessionid": SESSIONID, "csrftoken": CSRF})
    assert r.status_code == 200 and r.get_json() == {"configured": True, "source": "app"}
    assert SESSIONID not in client.get("/api/cookies").get_data(as_text=True)
    assert client.delete("/api/cookies").get_json() == {"configured": False, "source": None}


def test_api_put_bad_value_is_400(client):
    r = client.put("/api/cookies", json={"sessionid": "nope"})
    assert r.status_code == 400 and "sessionid" in r.get_json()["error"]


def test_api_put_requires_json_content_type(client):
    # 다른 사이트의 폼(text/plain, urlencoded)으로는 쿠키를 바꿀 수 없어야 합니다.
    r = client.put("/api/cookies", data=f'{{"sessionid": "{SESSIONID}"}}', content_type="text/plain")
    assert r.status_code == 400
    assert client.get("/api/cookies").get_json()["configured"] is False


def test_env_file_takes_precedence(monkeypatch, tmp_path):
    f = tmp_path / "mine.txt"
    f.write_text("# Netscape HTTP Cookie File\n")
    monkeypatch.setenv("IG_COOKIES_FILE", str(f))
    assert igdl.cookies_status() == {"configured": True, "source": "env"}


def test_corrupt_cookie_file_gives_clear_error(data_dir):
    data_dir.mkdir(parents=True)
    (data_dir / "cookies.txt").write_text("this is not a cookie file\n")
    with pytest.raises(IgError) as e:
        igdl._ydl(igdl._opts())
    assert "쿠키" in e.value.message


# ---- 비밀번호 없는 모드: localhost 이외의 Host 거부(DNS 리바인딩 방어) -------
@pytest.mark.parametrize("host", ["localhost", "localhost:5000", "127.0.0.1:5000", "[::1]:5000"])
def test_local_hosts_allowed_without_token(client, host):
    assert client.get("/api/cookies", headers={"Host": host}).status_code == 200


@pytest.mark.parametrize("host", ["evil.example.com", "evil.example.com:5000", "192.168.0.10:5000", "localhost.evil.com"])
def test_other_hosts_rejected_without_token(client, host):
    assert client.get("/api/cookies", headers={"Host": host}).status_code == 403
    assert client.get("/", headers={"Host": host}).status_code == 403


def test_any_host_allowed_with_token_login(monkeypatch, client):
    monkeypatch.setenv("APP_TOKEN", "t0ken-value")
    r = client.get("/login", headers={"Host": "my-phone.tailnet.ts.net"})
    assert r.status_code == 200
