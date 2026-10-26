"""안드로이드 앱(Chaquopy) 안에서 igdl을 부르는 진입점. Flask도 서버도 없습니다.

화면(WebView)의 /api/* 요청은 자바가 가로채 `handle`로 넘기고, 저장은 `save`로 합니다.
"""
import json
import mimetypes
import os
import platform
import re
import shutil
import sys
import tempfile
import time
import traceback
import urllib.parse
from pathlib import Path

_lib = None
_ERRORS = []  # 최근 오류(진단 화면에 보여 줌). 앱을 다시 켜면 비워집니다.
_MAX_ERRORS = 10


def _igdl():
    global _lib
    if _lib is None:
        import igdl

        # 안드로이드에는 ffmpeg이 없으므로 영상/음성을 합쳐야 하는 포맷은 받지 않습니다.
        # 인스타그램은 소리가 들어 있는 단일 mp4도 함께 제공합니다.
        igdl.VIDEO_FORMAT = "b[ext=mp4]/b"
        _lib = igdl
    return _lib


def init(data_dir):
    os.makedirs(data_dir, mode=0o700, exist_ok=True)
    os.environ["IGDL_DATA_DIR"] = data_dir


def warmup():
    """첫 조회가 느리지 않도록 무거운 import를 미리 해 둡니다."""
    _igdl()
    import yt_dlp  # noqa: F401
    import yt_dlp.extractor.instagram  # noqa: F401


def log_error(where, url, message, detail=""):
    """실패를 기록합니다. 파이썬뿐 아니라 자바(갤러리 저장 등)에서도 부릅니다."""
    _ERRORS.append({"t": time.strftime("%m-%d %H:%M:%S"), "where": where, "url": url or "", "message": message, "detail": detail or ""})
    del _ERRORS[:-_MAX_ERRORS]


def _json(status, obj):
    return [status, "application/json", json.dumps(obj, ensure_ascii=False).encode("utf-8")]


def handle(method, path, query):
    """WebView의 /api/* 요청 처리. [상태코드, content-type, 본문 bytes]를 돌려줍니다."""
    lib = _igdl()
    q = urllib.parse.parse_qs(query or "")
    try:
        if method == "GET" and path == "/api/info":
            return _json(200, lib.post_for_ui(q.get("url", [""])[0]))
        if method == "GET" and path == "/api/thumb":
            data, ctype = lib.fetch_thumbnail(q.get("u", [""])[0])
            return [200, ctype, data]
        if method == "GET" and path == "/api/cookies":
            return _json(200, lib.cookies_status())
        if method == "GET" and path == "/api/profiles":
            return _json(200, {"profiles": lib.list_profiles()})
        if method == "GET" and path == "/api/recent":
            limit = int(q.get("limit", ["30"])[0] or 30)
            return _json(200, {"items": lib.recent(limit, q.get("u", [None])[0])})
        if method == "GET" and path.startswith("/api/thumbs/"):
            data, ctype = lib.thumb_bytes(path.rsplit("/", 1)[-1])
            return [200, ctype, data]
        if method == "GET" and path == "/api/diag":
            return _json(200, {"text": diagnose(q.get("url", [""])[0])})
        return _json(404, {"error": "없는 주소예요."})
    except lib.IgError as e:
        log_error("조회", q.get("url", [""])[0], e.message, e.detail)
        return _json(e.status, {"error": e.message, "detail": e.detail})
    except Exception as e:
        traceback.print_exc()
        detail = f"{type(e).__name__}: {e}"
        log_error("조회", q.get("url", [""])[0], "예상치 못한 오류", detail + "\n" + traceback.format_exc()[-800:])
        # 내 폰 안의 앱이라 오류 내용을 숨길 이유가 없습니다. '자세히'에서 볼 수 있게 detail로 돌려줍니다.
        return _json(500, {"error": "예상치 못한 오류가 났어요. '자세히'를 확인해 주세요.", "detail": detail})


def save(url, index, cache_dir, skip_saved=False):
    """게시물을 cache_dir 아래 임시 폴더에 받고 JSON 문자열로 결과를 돌려줍니다.

    {"dir", "files": [{"path", "name", "mime"}], "code", "uploader"} / {"skipped": true} /
    {"error": "...", "status": 429}. 갤러리 저장과 임시 폴더 정리는 자바가 하고, 갤러리 저장이 끝나면
    자바가 mark_saved를 불러 '저장한 게시물'로 기록합니다(저장이 실패했는데 기록되는 일이 없도록).
    skip_saved=True면 이미 받은 게시물은 인스타그램에 요청하지 않고 건너뜁니다(묶음 저장용).
    """
    lib = _igdl()
    try:
        code = lib.shortcode(url)
        index = int(index)
        if skip_saved and not index and lib.is_saved(code):
            return json.dumps({"skipped": True, "code": code})
        dest = Path(tempfile.mkdtemp(prefix="save-", dir=cache_dir))
        meta = {}
        try:
            files = lib.fetch_post(url, dest, only=index or None, meta=meta)
        except BaseException:
            shutil.rmtree(dest, ignore_errors=True)
            raise
        return json.dumps(
            {
                "dir": str(dest),
                "code": code,
                "uploader": meta.get("uploader") or "",
                "thumb_url": meta.get("thumb_url") or "",
                "kind": meta.get("kind") or "",
                "files": [
                    {
                        "path": str(p),
                        "name": f"{code}_{n:02d}{p.suffix}",
                        "mime": mimetypes.guess_type(p.name)[0] or "application/octet-stream",
                    }
                    for n, p in files
                ],
            },
            ensure_ascii=False,
        )
    except lib.IgError as e:
        # status: 429(요청 제한)나 401(로그인 필요)이면 묶음 저장을 멈추는 신호로 씁니다.
        log_error("저장", url, e.message, e.detail)
        return json.dumps({"error": e.message, "status": e.status, "detail": e.detail}, ensure_ascii=False)
    except Exception as e:
        traceback.print_exc()
        detail = f"{type(e).__name__}: {e}"
        log_error("저장", url, "예상치 못한 오류", detail + "\n" + traceback.format_exc()[-800:])
        return json.dumps({"error": "저장 중 예상치 못한 오류가 났어요. '자세히'를 확인해 주세요.", "status": 500, "detail": detail}, ensure_ascii=False)


def _version(module):
    try:
        from importlib.metadata import version

        return version(module)
    except Exception:
        return "?"


def _writable(directory):
    try:
        os.makedirs(directory, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=directory):
            pass
        return "쓰기 가능"
    except Exception as e:
        return f"쓰기 실패 ({type(e).__name__}: {e})"


def diagnose(url=""):
    """문제가 생겼을 때 원인을 좁히기 위한 점검 결과를 글로 돌려줍니다(복사해서 보낼 수 있게).

    인스타그램에 보내는 요청은 홈페이지 GET 1번이고, url을 주면 그 게시물 조회를 1번 더 합니다.
    """
    lines = [f"[진단 {time.strftime('%Y-%m-%d %H:%M:%S')}]"]
    add = lines.append
    add(f"파이썬 {sys.version.split()[0]} / {platform.platform()}")
    add("패키지: " + ", ".join(f"{m} {_version(m)}" for m in ("yt-dlp", "requests", "urllib3", "certifi", "idna", "charset-normalizer")))
    try:
        import certifi

        add(f"인증서 파일: {'있음' if os.path.exists(certifi.where()) else '없음'} ({certifi.where()})")
    except Exception as e:
        add(f"인증서(certifi) 불러오기 실패: {type(e).__name__}: {e}")
    lib = _igdl()
    add(f"앱 데이터 폴더: {lib.data_dir()} → {_writable(str(lib.data_dir()))}")
    add(f"임시 폴더: {tempfile.gettempdir()} → {_writable(tempfile.gettempdir())}")
    st = lib.cookies_status()
    add(f"인스타그램 로그인 쿠키: {'등록됨' if st['configured'] else '없음'}")
    try:
        import requests

        r = requests.get("https://www.instagram.com/", timeout=10, allow_redirects=False,
                         headers={"User-Agent": lib.USER_AGENT})
        add(f"인스타그램 접속: HTTP {r.status_code}")
    except Exception as e:
        add(f"인스타그램 접속 실패: {type(e).__name__}: {e}")
    if url:
        try:
            post = lib.get_post(url)
            add(f"게시물 조회 시험: 성공 (항목 {len(post['items'])}개, 계정 {post['uploader']})")
        except lib.IgError as e:
            add(f"게시물 조회 시험: 실패 [{e.status}] {e.message}")
            add(f"  원문: {e.detail or '-'}")
        except Exception as e:
            add(f"게시물 조회 시험: 예외 {type(e).__name__}: {e}")
    if _ERRORS:
        add("")
        add(f"최근 오류 {len(_ERRORS)}건:")
        for err in reversed(_ERRORS):
            add(f"- {err['t']} [{err['where']}] {err['message']}")
            if err["url"]:
                add(f"    주소: {err['url']}")
            if err["detail"]:
                add("    원문: " + err["detail"].replace("\n", "\n          "))
    else:
        add("")
        add("최근 오류: 없음")
    return "\n".join(lines)


def mark_saved(code, uploader, thumb_url="", count=0, kind="", uri="", mime=""):
    _igdl().record_saved(code, uploader, count=count, kind=kind, thumb_url=thumb_url, uri=uri, mime=mime)


def is_saved(code):
    """인앱 브라우저가 '이 게시물 저장'/'다시 저장' 표시를 정할 때 씁니다(인스타그램에 요청하지 않음)."""
    lib = _igdl()
    return bool(_valid_code(code) and lib.is_saved(code))


def _valid_code(code):
    return bool(re.fullmatch(r"[A-Za-z0-9_-]{1,64}", code or ""))


def remove_profile(name):
    _igdl().remove_profile(name)


def save_cookies(sessionid, csrftoken):
    lib = _igdl()
    try:
        lib.save_cookies(sessionid, csrftoken or "")
        return json.dumps({"ok": True})
    except lib.IgError as e:
        return json.dumps({"ok": False, "error": e.message}, ensure_ascii=False)


def clear_cookies():
    _igdl().clear_cookies()
