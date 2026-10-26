import time

import pytest

import app as app_module

TOKEN = "s3cret-token"


@pytest.fixture
def secured(monkeypatch):
    monkeypatch.setenv("APP_TOKEN", TOKEN)
    monkeypatch.setattr(app_module, "FAIL_DELAY", 0)
    app_module._fails.clear()
    app_module.app.config["TESTING"] = True
    yield app_module.app.test_client()
    app_module._fails.clear()


def login(client, token=TOKEN, nxt="/"):
    return client.post("/login", data={"token": token, "next": nxt})


def test_open_when_no_token(monkeypatch):
    monkeypatch.delenv("APP_TOKEN", raising=False)
    c = app_module.app.test_client()
    assert c.get("/").status_code == 200
    assert c.post("/api/info", json={"url": "x"}).status_code == 400  # 로그인이 아니라 입력 오류


def test_page_redirects_to_login_keeping_query(secured):
    r = secured.get("/?text=hello&url=https%3A%2F%2Fwww.instagram.com%2Fp%2FAbC%2F")
    assert r.status_code == 302
    loc = r.headers["Location"]
    assert loc.startswith("/login?next=")
    assert "text%3Dhello" in loc  # 공유로 열었을 때 로그인 뒤 같은 요청으로 돌아오기 위해


def test_api_returns_401_with_auth_flag(secured):
    for r in (secured.post("/api/info", json={"url": "x"}), secured.get("/api/download?url=x"), secured.get("/api/thumb?u=x")):
        assert r.status_code == 401 and r.get_json()["auth"] is True


@pytest.mark.parametrize("path", ["/login", "/manifest.webmanifest", "/sw.js", "/static/icon-192.png", "/static/icon.svg"])
def test_public_paths_do_not_need_login(secured, path):
    assert secured.get(path).status_code == 200


def test_static_app_page_needs_login(secured):
    assert secured.get("/static/index.html").status_code == 302


def test_login_success_sets_cookie_and_unlocks(secured):
    r = login(secured, nxt="/?text=hi")
    assert r.status_code == 302 and r.headers["Location"] == "/?text=hi"
    cookie = r.headers["Set-Cookie"]
    assert "HttpOnly" in cookie and "SameSite=Lax" in cookie
    assert "Secure" not in cookie  # http에서는 붙이지 않음(붙이면 localhost에서 쿠키가 저장되지 않음)
    assert secured.get("/").status_code == 200
    assert secured.post("/api/info", json={"url": "x"}).status_code == 400


def test_cookie_is_secure_behind_https_proxy(secured):
    r = secured.post("/login", data={"token": TOKEN}, headers={"X-Forwarded-Proto": "https"})
    assert "Secure" in r.headers["Set-Cookie"]


def test_wrong_token_rejected(secured):
    r = login(secured, token="nope")
    assert r.status_code == 302 and r.headers["Location"].startswith("/login?error=1")
    assert "Set-Cookie" not in r.headers
    assert secured.get("/").status_code == 302


def test_brute_force_lockout(secured):
    for _ in range(app_module.MAX_FAILS):
        assert login(secured, token="bad").status_code == 302
    r = login(secured)  # 맞는 토큰이어도 잠겨 있는 동안은 거부
    assert r.status_code == 429
    assert secured.get("/").status_code == 302


def test_lockout_expires(secured, monkeypatch):
    for _ in range(app_module.MAX_FAILS):
        login(secured, token="bad")
    app_module._fails[:] = [t - app_module.FAIL_WINDOW - 1 for t in app_module._fails]
    assert login(secured).status_code == 302


@pytest.mark.parametrize("nxt", ["//evil.example.com", "https://evil.example.com", "/\\evil.example.com", "evil", ""])
def test_open_redirect_blocked(secured, nxt):
    assert login(secured, nxt=nxt).headers["Location"] == "/"


def test_forged_and_expired_cookies_rejected(secured):
    future = int(time.time()) + 1000
    expired = int(time.time()) - 10
    for value in ("garbage", f"{future}.deadbeef", f"{expired}.{app_module._sign(TOKEN, expired)}",
                  f"{future}.{app_module._sign('other-token', future)}"):
        secured.set_cookie(app_module.COOKIE_NAME, value, domain="localhost")
        assert secured.get("/").status_code == 302, value


def test_cookie_from_old_token_stops_working(secured, monkeypatch):
    login(secured)
    assert secured.get("/").status_code == 200
    monkeypatch.setenv("APP_TOKEN", "rotated-token")  # 비밀번호를 바꾸면 기존 로그인이 모두 무효
    assert secured.get("/").status_code == 302


def test_container_guard_refuses_to_start_without_token():
    import os
    import subprocess
    import sys
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    env = {k: v for k, v in os.environ.items() if k != "APP_TOKEN"} | {"REQUIRE_APP_TOKEN": "1"}
    bad = subprocess.run([sys.executable, "-c", "import app"], cwd=root, env=env, capture_output=True, text=True)
    assert bad.returncode != 0 and "APP_TOKEN" in bad.stderr
    good = subprocess.run([sys.executable, "-c", "import app"], cwd=root, env=env | {"APP_TOKEN": "x"}, capture_output=True, text=True)
    assert good.returncode == 0, good.stderr
