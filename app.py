"""인스타그램 게시물/영상 다운로더 — 로컬 웹 앱.

실행: python app.py  →  http://127.0.0.1:5000
"""
from __future__ import annotations

import mimetypes
import os
import shutil
import tempfile
from pathlib import Path
from urllib.parse import urlencode

import requests
from flask import Flask, Response, jsonify, request, send_from_directory

import igdl
from igdl import IgError

app = Flask(__name__, static_folder="static", static_url_path="/static")


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


@app.post("/api/info")
def info():
    body = request.get_json(silent=True) or {}
    post = igdl.get_post(body.get("url", ""))
    for item in post["items"]:
        if item["thumbnail"]:
            # 인스타 CDN이 외부 Referer를 막을 수 있어 서버를 거쳐 미리보기를 보여줍니다.
            item["thumbnail"] = "/api/thumb?" + urlencode({"u": item["thumbnail"]})
    return jsonify(post)


@app.get("/api/thumb")
def thumb():
    url = request.args.get("u", "")
    if not igdl.is_allowed_media_url(url):
        raise IgError("허용되지 않은 주소입니다.", 400)
    try:
        upstream = requests.get(
            url,
            headers={"User-Agent": igdl.USER_AGENT, "Referer": "https://www.instagram.com/"},
            timeout=15,
            allow_redirects=False,
        )
        upstream.raise_for_status()
    except requests.RequestException:
        raise IgError("미리보기를 불러오지 못했습니다.", 502)
    ctype = upstream.headers.get("Content-Type", "image/jpeg")
    if not ctype.startswith("image/"):
        raise IgError("이미지가 아닙니다.", 502)
    return Response(upstream.content, content_type=ctype, headers={"Cache-Control": "private, max-age=600"})


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
    # 기본은 내 컴퓨터에서만 접속됩니다. 쿠키(로그인 정보)를 쓰므로 외부에 공개하지 마세요.
    app.run(host=os.environ.get("HOST", "127.0.0.1"), port=int(os.environ.get("PORT", "5000")))
