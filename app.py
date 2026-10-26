"""인스타그램 게시물/영상 다운로더 — 로컬 웹 앱.

실행: python app.py  →  http://127.0.0.1:5000
"""
from __future__ import annotations

import hashlib
import hmac
import mimetypes
import os
import shutil
import tempfile
import time
from pathlib import Path
from urllib.parse import urlencode, urlsplit

import requests
from flask import Flask, Response, jsonify, redirect, request, send_from_directory

import igdl
from igdl import IgError

app = Flask(__name__, static_folder="static", static_url_path="/static")

# ---- 접근 제한 (선택) ------------------------------------------------------
# APP_TOKEN을 지정하면 로그인해야만 쓸 수 있습니다. 인터넷에서 닿는 주소로 열 때는 필수입니다.
COOKIE_NAME = "igdl_auth"
COOKIE_MAX_AGE = 90 * 24 * 3600
PUBLIC_PATHS = {"/login", "/manifest.webmanifest", "/sw.js"}  # 앱 설치에 필요하고 민감 정보가 없는 것들
PUBLIC_PREFIXES = ("/static/icon",)
MAX_FAILS, FAIL_WINDOW, FAIL_DELAY = 10, 15 * 60, 1.0
_fails: list[float] = []  # 계정이 하나뿐이라 IP가 아닌 전역으로 셉니다(프록시 뒤 X-Forwarded-For는 믿을 수 없음)


def _token() -> str:
    return os.environ.get("APP_TOKEN", "")


def _sign(token: str, exp: int) -> str:
    return hmac.new(token.encode(), f"igdl:{exp}".encode(), hashlib.sha256).hexdigest()


def _valid_cookie(token: str, value: str) -> bool:
    try:
        exp_s, sig = value.split(".", 1)
        exp = int(exp_s)
    except ValueError:
        return False
    return exp > time.time() and hmac.compare_digest(sig, _sign(token, exp))


def _safe_next(target: str | None) -> str:
    """로그인 뒤 돌아갈 곳. 같은 사이트의 경로만 허용합니다(open redirect 방지)."""
    if target and target.startswith("/") and not target.startswith("//") and "\\" not in target:
        return target
    return "/"


def _is_https() -> bool:
    return request.is_secure or request.headers.get("X-Forwarded-Proto", "").split(",")[0].strip() == "https"


LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "::1"}


@app.before_request
def require_login():
    token = _token()
    if not token:
        # 비밀번호 없이 쓰는 모드(내 기기 안에서만)에서는 localhost 호스트명으로 온 요청만 받습니다.
        # 악성 웹페이지가 DNS를 조작해 내 기기의 localhost 서버를 건드리는 공격(DNS 리바인딩)을 막습니다.
        if (urlsplit("//" + request.host).hostname or "") not in LOOPBACK_HOSTS:
            return "비밀번호(APP_TOKEN) 없이는 localhost로만 접속할 수 있어요.", 403
        return None
    if request.path in PUBLIC_PATHS or request.path.startswith(PUBLIC_PREFIXES):
        return None
    if _valid_cookie(token, request.cookies.get(COOKIE_NAME, "")):
        return None
    if request.path.startswith("/api/"):
        return jsonify(error="로그인이 필요해요.", auth=True), 401  # auth=True: 인스타그램 401과 구분
    return redirect("/login?" + urlencode({"next": request.full_path.rstrip("?")}))


@app.get("/login")
def login_page():
    return send_from_directory(app.static_folder, "login.html")


@app.post("/login")
def login_submit():
    token = _token()
    nxt = _safe_next(request.form.get("next"))
    if not token:
        return redirect(nxt)
    now = time.time()
    _fails[:] = [t for t in _fails if now - t < FAIL_WINDOW]
    if len(_fails) >= MAX_FAILS:
        return "너무 많이 틀렸어요. 15분 뒤에 다시 시도해 주세요.", 429
    if not hmac.compare_digest(request.form.get("token", "").encode(), token.encode()):
        _fails.append(now)
        time.sleep(FAIL_DELAY)
        return redirect("/login?" + urlencode({"error": 1, "next": nxt}))
    exp = int(now) + COOKIE_MAX_AGE
    resp = redirect(nxt)
    resp.set_cookie(COOKIE_NAME, f"{exp}.{_sign(token, exp)}", max_age=COOKIE_MAX_AGE,
                    httponly=True, samesite="Lax", secure=_is_https())
    return resp


if os.environ.get("REQUIRE_APP_TOKEN") == "1" and not _token():
    # 컨테이너(Dockerfile)가 켜는 안전장치: 토큰 없이 인터넷에 열린 채로 뜨는 사고를 막습니다.
    raise RuntimeError("APP_TOKEN이 설정되지 않았습니다. 비밀번호를 정해 환경 변수 APP_TOKEN으로 넘겨 주세요.")


@app.errorhandler(IgError)
def handle_ig_error(e: IgError):
    return jsonify(error=e.message), e.status


@app.get("/")
def index():
    return send_from_directory(app.static_folder, "index.html")


@app.get("/manifest.webmanifest")
def manifest():
    return send_from_directory(app.static_folder, "manifest.webmanifest", mimetype="application/manifest+json")


@app.get("/sw.js")
def service_worker():
    # 서비스 워커는 루트 경로에서 제공돼야 전체 범위(scope "/")를 가질 수 있습니다.
    resp = send_from_directory(app.static_folder, "sw.js", mimetype="text/javascript")
    resp.headers["Cache-Control"] = "no-cache"
    return resp


@app.get("/api/cookies")
def cookies_get():
    return jsonify(igdl.cookies_status())  # 값은 절대 돌려주지 않습니다


@app.put("/api/cookies")
def cookies_put():
    body = request.get_json(silent=True) or {}  # force=True를 쓰지 않아 cross-site 폼 전송으로는 못 바꿉니다
    igdl.save_cookies(str(body.get("sessionid", "")), str(body.get("csrftoken", "")))
    return jsonify(igdl.cookies_status())


@app.delete("/api/cookies")
def cookies_delete():
    igdl.clear_cookies()
    return jsonify(igdl.cookies_status())


@app.route("/api/info", methods=["GET", "POST"])
def info():
    # GET이 기본입니다(안드로이드 앱의 WebView는 POST 본문을 가로챌 수 없음). POST는 하위 호환용.
    if request.method == "GET":
        url = request.args.get("url", "")
    else:
        url = (request.get_json(silent=True) or {}).get("url", "")
    return jsonify(igdl.post_for_ui(url))


@app.get("/api/thumb")
def thumb():
    data, ctype = igdl.fetch_thumbnail(request.args.get("u", ""))
    return Response(data, content_type=ctype, headers={"Cache-Control": "private, max-age=600"})


@app.get("/api/download")
def download():
    """index가 있으면 해당 항목 1개, 없으면 전부(여러 개면 zip)를 내려줍니다."""
    url = request.args.get("url", "")
    index = request.args.get("index", type=int)
    code = igdl.shortcode(url)

    tmp = Path(tempfile.mkdtemp(prefix="igdl-"))
    try:
        files = igdl.fetch_post(url, tmp / "raw", only=index)
        if len(files) == 1:
            n, path = files[0]
            send_path, name = path, f"{code}_{n:02d}{path.suffix}"
        else:
            send_path = igdl.make_zip(files, code, tmp / "post.zip")
            name = f"{code}.zip"
        size = send_path.stat().st_size
    except BaseException:
        shutil.rmtree(tmp, ignore_errors=True)
        raise

    # send_file + call_on_close는 쓰지 않습니다: send_file은 direct_passthrough 응답을 만들어
    # Werkzeug가 close 콜백을 호출하지 않아 임시 폴더가 남습니다. 제너레이터의 finally는
    # 전송이 끝나거나 클라이언트가 연결을 끊을 때 항상 실행됩니다.
    def stream():
        try:
            with open(send_path, "rb") as f:
                while chunk := f.read(64 * 1024):
                    yield chunk
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    resp = Response(stream(), mimetype=mimetypes.guess_type(name)[0] or "application/octet-stream")
    resp.headers.set("Content-Disposition", "attachment", filename=name)
    resp.headers["Content-Length"] = str(size)
    return resp


if __name__ == "__main__":
    # 기본은 내 컴퓨터에서만 접속됩니다.
    host = os.environ.get("HOST", "127.0.0.1")
    if host not in ("127.0.0.1", "localhost", "::1") and not _token():
        raise SystemExit(
            "HOST가 외부에 열려 있는데 APP_TOKEN이 없습니다. 인스타그램 쿠키를 쓰는 앱이라 누구나 쓸 수 있게 두면 위험해요.\n"
            '  APP_TOKEN="$(python -c \'import secrets; print(secrets.token_urlsafe(32))\')" 로 비밀번호를 정하고 다시 실행하세요.'
        )
    app.run(host=host, port=int(os.environ.get("PORT", "5000")))
