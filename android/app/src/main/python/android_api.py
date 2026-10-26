"""안드로이드 앱(Chaquopy) 안에서 igdl을 부르는 진입점. Flask도 서버도 없습니다.

화면(WebView)의 /api/* 요청은 자바가 가로채 `handle`로 넘기고, 저장은 `save`로 합니다.
"""
import json
import mimetypes
import os
import shutil
import tempfile
import traceback
import urllib.parse
from pathlib import Path

_lib = None


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
        return _json(404, {"error": "없는 주소예요."})
    except lib.IgError as e:
        return _json(e.status, {"error": e.message})
    except Exception:
        traceback.print_exc()
        return _json(500, {"error": "예상치 못한 오류가 났어요. 잠시 뒤 다시 시도해 주세요."})


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
        return json.dumps({"error": e.message, "status": e.status}, ensure_ascii=False)
    except Exception:
        traceback.print_exc()
        return json.dumps({"error": "저장 중 예상치 못한 오류가 났어요.", "status": 500}, ensure_ascii=False)


def mark_saved(code, uploader):
    _igdl().record_saved(code, uploader)


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
