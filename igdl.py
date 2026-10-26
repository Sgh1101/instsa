"""인스타그램 게시물/릴스 다운로드 로직 (yt-dlp 기반, Flask와 무관).

- 영상: yt-dlp가 내려받습니다.
- 사진: yt-dlp는 사진 전용 게시물을 지원하지 않으므로(`There is no video in this post`),
  추출 결과의 thumbnails(image_versions2 후보) 중 가장 큰 이미지를 직접 받습니다.
"""
from __future__ import annotations

import http.cookiejar
import json
import os
import re
import threading
import time
import zipfile
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlencode, urlparse

import requests
import yt_dlp
from yt_dlp.utils import DownloadError

ALLOWED_PAGE_HOSTS = {"instagram.com", "www.instagram.com", "m.instagram.com"}
# 미디어 CDN 호스트 접미사. 서버가 임의 주소로 요청을 보내는 것(SSRF)을 막기 위한 허용 목록입니다.
ALLOWED_MEDIA_SUFFIXES = (".cdninstagram.com", ".fbcdn.net")

# 영상 포맷 선택. 기본은 ffmpeg으로 영상/음성을 합치는 것까지 허용하고, ffmpeg이 없는 환경(안드로이드)에서는
# "b[ext=mp4]/b"(소리 포함 단일 파일)로 바꿉니다.
VIDEO_FORMAT = "b[ext=mp4]/b/bv*+ba"

MAX_IMAGE_BYTES = 50 * 1024 * 1024
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

# ASCII만 허용합니다. 게시물 코드는 다운로드 파일명과 응답 헤더에 그대로 쓰입니다.
_PATH_RE = re.compile(r"^/(?:[A-Za-z0-9_.]+/)?(?P<kind>p|tv|reels?)/(?P<code>[A-Za-z0-9_-]+)/?$")
_IMAGE_EXT = {"image/jpeg": ".jpg", "image/webp": ".webp", "image/png": ".png"}


class IgError(Exception):
    """사용자에게 그대로 보여줄 수 있는 오류."""

    def __init__(self, message: str, status: int = 400, detail: str = ""):
        super().__init__(message)
        self.message = message
        self.status = status
        self.detail = detail  # 진단용 원문(yt-dlp가 돌려준 메시지 등). 화면의 '자세히'에만 보입니다.


@dataclass
class Item:
    index: int  # 1부터 시작 (캐러셀 순서)
    kind: str  # "video" | "image"
    thumbnail: str | None
    width: int | None
    height: int | None
    duration: float | None


def normalize_url(raw: str) -> str:
    """게시물 주소를 검증하고 추적용 쿼리를 제거한 정규 주소로 바꿉니다."""
    raw = (raw or "").strip()
    if not raw:
        raise IgError("인스타그램 게시물 주소를 입력해 주세요.")
    if "://" not in raw:
        raw = "https://" + raw
    parsed = urlparse(raw)
    if parsed.scheme not in ("http", "https") or (parsed.hostname or "").lower() not in ALLOWED_PAGE_HOSTS:
        raise IgError("인스타그램 주소만 지원합니다. 예: https://www.instagram.com/p/XXXX/")
    m = _PATH_RE.match(parsed.path)
    if not m:
        raise IgError(
            "게시물(/p/), 릴스(/reel/), IGTV(/tv/) 링크만 지원해요. 프로필 전체 불러오기는 지원하지 않아요. "
            "게시물 링크를 여러 개 붙여넣거나, '저장한 프로필'에서 인스타그램으로 열어 공유해 주세요."
        )
    kind = "reel" if m["kind"].startswith("reel") else m["kind"]
    return f"https://www.instagram.com/{kind}/{m['code']}/"


def shortcode(url: str) -> str:
    return normalize_url(url).rstrip("/").rsplit("/", 1)[-1]


def is_allowed_media_url(url: str) -> bool:
    parsed = urlparse(url or "")
    host = (parsed.hostname or "").lower()
    return parsed.scheme == "https" and any(host.endswith(s) for s in ALLOWED_MEDIA_SUFFIXES)


def _opts(**extra) -> dict:
    opts = {
        "quiet": True,
        "no_warnings": True,
        "no_color": True,
        "noprogress": True,
        "ignore_no_formats_error": True,  # 사진 항목은 포맷이 없는 것이 정상입니다.
        "socket_timeout": 20,
        "retries": 2,
    }
    if browser := os.environ.get("IG_COOKIES_FROM_BROWSER"):
        opts["cookiesfrombrowser"] = (browser,)
    opts.update(extra)
    return opts


# ---- 로그인 쿠키 -----------------------------------------------------------
# yt-dlp는 `sessionid` 쿠키가 있으면 로그인한 것으로 봅니다(extractor/instagram.py). 값은 저장만 하고
# 절대 응답으로 돌려주지 않습니다.
_SESSIONID_RE = re.compile(r"^[A-Za-z0-9%:_.\-]{16,512}$")
_CSRF_RE = re.compile(r"^[A-Za-z0-9]{16,128}$")


def data_dir() -> Path:
    return Path(os.environ.get("IGDL_DATA_DIR") or Path.home() / ".igdl")


def _stored_cookie_file() -> Path:
    return data_dir() / "cookies.txt"


def _cookie_file() -> Path | None:
    """IG_COOKIES_FILE(직접 지정)이 우선, 없으면 앱에서 등록한 파일."""
    if env := os.environ.get("IG_COOKIES_FILE"):
        return Path(env)
    stored = _stored_cookie_file()
    return stored if stored.is_file() else None


def cookies_status() -> dict:
    if os.environ.get("IG_COOKIES_FILE"):
        return {"configured": True, "source": "env"}
    if os.environ.get("IG_COOKIES_FROM_BROWSER"):
        return {"configured": True, "source": "browser"}
    return {"configured": _stored_cookie_file().is_file(), "source": "app" if _stored_cookie_file().is_file() else None}


def save_cookies(sessionid: str, csrftoken: str = "") -> None:
    sessionid = (sessionid or "").strip().removeprefix("sessionid=").rstrip(";").strip()
    csrftoken = (csrftoken or "").strip().removeprefix("csrftoken=").rstrip(";").strip()
    if not _SESSIONID_RE.match(sessionid):
        raise IgError("sessionid 형식이 올바르지 않아요. 브라우저 쿠키에서 sessionid의 값만 그대로 복사해 주세요.")
    if csrftoken and not _CSRF_RE.match(csrftoken):
        raise IgError("csrftoken 형식이 올바르지 않아요. 비워 두어도 됩니다.")
    expiry = int(time.time()) + 365 * 24 * 3600
    lines = ["# Netscape HTTP Cookie File", f"#HttpOnly_.instagram.com\tTRUE\t/\tTRUE\t{expiry}\tsessionid\t{sessionid}"]
    if csrftoken:
        lines.append(f".instagram.com\tTRUE\t/\tTRUE\t{expiry}\tcsrftoken\t{csrftoken}")
    path = _stored_cookie_file()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = path.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)  # 다른 사용자가 읽지 못하게
    with os.fdopen(fd, "w") as f:
        f.write("\n".join(lines) + "\n")
    os.replace(tmp, path)


def clear_cookies() -> None:
    _stored_cookie_file().unlink(missing_ok=True)


# ---- 저장 기록 ---------------------------------------------------------------
# 받은 게시물(code → 계정, 시각)을 data_dir/library.json에 남깁니다. 이미 받은 게시물을 건너뛰고,
# '저장한 프로필' 목록(받은 게시물의 계정별 묶음)을 만드는 데 씁니다. 화면이 없는 백그라운드 저장에서도
# 남아야 해서 화면(JS)이 아니라 여기에 저장합니다.
_LIBRARY_LOCK = threading.Lock()


def _library_path() -> Path:
    return data_dir() / "library.json"


def _load_library() -> dict:
    try:
        d = json.loads(_library_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"saved": {}}
    return d if isinstance(d, dict) and isinstance(d.get("saved"), dict) else {"saved": {}}


def _write_library(lib: dict) -> None:
    path = _library_path()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = path.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(lib, f, ensure_ascii=False)
    os.replace(tmp, path)


def record_saved(code: str, uploader: str | None) -> None:
    with _LIBRARY_LOCK:
        lib = _load_library()
        lib["saved"][code] = {"u": uploader or "", "t": int(time.time())}
        _write_library(lib)


def is_saved(code: str) -> bool:
    return code in _load_library()["saved"]


def list_profiles() -> list[dict]:
    """받은 게시물의 계정별 묶음. 최근에 받은 계정이 먼저."""
    groups: dict[str, dict] = {}
    for v in _load_library()["saved"].values():
        name = v.get("u") or ""
        if not name:
            continue
        g = groups.setdefault(name, {"name": name, "count": 0, "last": 0})
        g["count"] += 1
        g["last"] = max(g["last"], int(v.get("t", 0)))
    return sorted(groups.values(), key=lambda g: g["last"], reverse=True)


def remove_profile(name: str) -> None:
    """이 계정의 저장 기록을 지웁니다(목록에서 사라지고, '이미 저장함' 표시도 사라짐). 갤러리의 파일은 그대로입니다."""
    with _LIBRARY_LOCK:
        lib = _load_library()
        lib["saved"] = {c: v for c, v in lib["saved"].items() if v.get("u") != name}
        _write_library(lib)


def _ydl(opts: dict) -> yt_dlp.YoutubeDL:
    """쿠키를 읽기 전용으로 불러온 YoutubeDL. (cookiefile 옵션은 종료 때 파일에 다시 써서
    동시 요청이 파일을 깨뜨릴 수 있으므로 쓰지 않습니다.)"""
    ydl = yt_dlp.YoutubeDL(opts)
    path = _cookie_file()
    if path:
        try:
            ydl.cookiejar.load(str(path), ignore_discard=True, ignore_expires=True)
        except (OSError, http.cookiejar.LoadError) as e:
            raise IgError(f"저장된 쿠키 파일을 읽지 못했어요. 다시 등록해 주세요. ({e.__class__.__name__})", 500) from e
    return ydl


def _extract(url: str, opts: dict, download: bool) -> dict:
    try:
        with _ydl(opts) as ydl:
            info = ydl.extract_info(url, download=download)
    except DownloadError as e:
        raise _translate(str(e)) from e
    if not info:
        raise IgError("게시물 정보를 가져오지 못했습니다.", 502)
    return info


def _translate(message: str) -> IgError:
    msg = re.sub(r"^ERROR:\s*(\[[^\]]+\]\s*[\w-]+:\s*)?", "", message).strip()
    low = msg.lower()
    if "429" in low or "too many requests" in low:
        return IgError(
            "인스타그램이 요청을 일시적으로 제한했어요. 몇 분 뒤에 다시 시도해 주세요. "
            "자주 생기면 화면 맨 아래 '인스타그램 로그인 설정'에서 쿠키를 등록해 보세요.",
            429,
            msg,
        )
    if any(k in low for k in ("login", "log in", "cookies", "rate-limit", "empty media response")):
        return IgError(
            "로그인이 필요한 게시물이거나 인스타그램이 요청을 제한했습니다. "
            "화면 맨 아래 '인스타그램 로그인 설정'에서 쿠키를 등록해 주세요.",
            401,
            msg,
        )
    if "404" in low or "not found" in low or "unavailable" in low:
        return IgError("게시물을 찾을 수 없습니다. 삭제되었거나 주소가 잘못되었을 수 있습니다.", 404, msg)
    return IgError(f"가져오지 못했습니다: {msg}", 502, msg)


def _entries(info: dict) -> list[dict]:
    if info.get("_type") == "playlist":
        return [e or {} for e in info.get("entries") or []]
    return [info]


def _best_thumbnail(entry: dict) -> dict | None:
    thumbs = [t for t in entry.get("thumbnails") or [] if t.get("url")]
    if not thumbs:
        return {"url": entry["thumbnail"]} if entry.get("thumbnail") else None
    # 면적이 가장 큰 후보. 크기 정보가 없으면 yt-dlp가 정렬해 둔 마지막(=최선) 후보.
    best = None
    for t in thumbs:
        if best is None or (t.get("width") or 0) * (t.get("height") or 0) >= (best.get("width") or 0) * (best.get("height") or 0):
            best = t
    return best


def _is_video(entry: dict) -> bool:
    return bool(entry.get("formats"))


def build_items(info: dict) -> list[Item]:
    items = []
    for i, entry in enumerate(_entries(info), start=1):
        thumb = _best_thumbnail(entry)
        items.append(
            Item(
                index=i,
                kind="video" if _is_video(entry) else "image",
                thumbnail=thumb["url"] if thumb else None,
                width=(thumb or {}).get("width"),
                height=(thumb or {}).get("height"),
                duration=entry.get("duration"),
            )
        )
    return items


def get_post(raw_url: str) -> dict:
    """다운로드 없이 게시물 메타데이터와 항목 목록만 가져옵니다."""
    url = normalize_url(raw_url)
    info = _extract(url, _opts(skip_download=True), download=False)
    return {
        "url": url,
        "code": shortcode(url),
        "title": info.get("title"),
        "uploader": info.get("channel") or info.get("uploader"),
        "caption": info.get("description"),
        "items": [item.__dict__ for item in build_items(info)],
    }


def post_for_ui(raw_url: str) -> dict:
    """화면에 보여 줄 게시물 정보. 미리보기는 인스타 CDN이 외부 Referer를 막을 수 있어 /api/thumb를 거칩니다."""
    post = get_post(raw_url)
    post["saved"] = is_saved(post["code"])
    for item in post["items"]:
        if item["thumbnail"]:
            item["thumbnail"] = "/api/thumb?" + urlencode({"u": item["thumbnail"]})
    return post


def fetch_thumbnail(url: str) -> tuple[bytes, str]:
    if not is_allowed_media_url(url):
        raise IgError("허용되지 않은 주소입니다.", 400)
    try:
        upstream = requests.get(
            url,
            headers={"User-Agent": USER_AGENT, "Referer": "https://www.instagram.com/"},
            timeout=15,
            allow_redirects=False,
        )
        upstream.raise_for_status()
    except requests.RequestException:
        raise IgError("미리보기를 불러오지 못했습니다.", 502)
    ctype = upstream.headers.get("Content-Type", "image/jpeg")
    if not ctype.startswith("image/"):
        raise IgError("이미지가 아닙니다.", 502)
    return upstream.content, ctype


def _download_image(url: str, dest_stem: Path) -> Path:
    if not is_allowed_media_url(url):
        raise IgError("허용되지 않은 이미지 주소입니다.", 502)
    try:
        resp = requests.get(
            url,
            headers={"User-Agent": USER_AGENT, "Referer": "https://www.instagram.com/"},
            timeout=20,
            stream=True,
            allow_redirects=False,  # 허용 목록 밖으로 리다이렉트되는 것을 막습니다.
        )
        resp.raise_for_status()
        ext = _IMAGE_EXT.get(resp.headers.get("Content-Type", "").split(";")[0].strip(), ".jpg")
        path = dest_stem.with_suffix(ext)
        size = 0
        with open(path, "wb") as f:
            for chunk in resp.iter_content(64 * 1024):
                size += len(chunk)
                if size > MAX_IMAGE_BYTES:
                    raise IgError("이미지가 너무 큽니다.", 502)
                f.write(chunk)
    except requests.RequestException as e:
        raise IgError(f"이미지를 내려받지 못했습니다: {e}", 502) from e
    return path


def fetch_post(raw_url: str, dest: Path, only: int | None = None, meta: dict | None = None) -> list[tuple[int, Path]]:
    """게시물의 영상·사진을 dest에 저장하고 (항목 번호, 파일 경로) 목록을 돌려줍니다.

    only를 주면 캐러셀의 해당 번호(1부터) 항목만 받습니다. meta를 주면 {"code", "uploader"}를 채워 줍니다.
    """
    url = normalize_url(raw_url)
    dest.mkdir(parents=True, exist_ok=True)
    opts = _opts(
        outtmpl=str(dest / "%(playlist_index|1)s.%(ext)s"),
        format=VIDEO_FORMAT,
        merge_output_format="mp4",
    )
    if only:
        opts["playlist_items"] = str(only)

    results = []
    try:
        with _ydl(opts) as ydl:
            # download=True로 한 번에 받으면 사진 항목(포맷 없음)에서 "No video formats found!"로
            # 실패하므로, 정보만 추출한 뒤 영상 항목에만 다운로드를 수행합니다.
            info = ydl.extract_info(url, download=False)
            if not info:
                raise IgError("게시물 정보를 가져오지 못했습니다.", 502)
            if meta is not None:
                meta["code"] = shortcode(url)
                meta["uploader"] = info.get("channel") or info.get("uploader")
            entries = _entries(info)
            is_playlist = info.get("_type") == "playlist"
            if not entries or (only and not is_playlist and only != 1):
                raise IgError("해당 번호의 항목이 없습니다.", 404)
            numbers = [only] if only and is_playlist else list(range(1, len(entries) + 1))

            for n, entry in zip(numbers, entries):
                if _is_video(entry):
                    entry["playlist_index"] = n
                    ydl.process_ie_result(entry, download=True)
                    files = sorted(p for p in dest.glob(f"{n}.*") if p.suffix not in (".part", ".ytdl"))
                    if not files:
                        raise IgError(f"{n}번 영상을 내려받지 못했습니다.", 502)
                    results.append((n, files[0]))
                else:
                    thumb = _best_thumbnail(entry)
                    if not thumb:
                        raise IgError(f"{n}번 사진 주소를 찾지 못했습니다.", 502)
                    results.append((n, _download_image(thumb["url"], dest / str(n))))
    except DownloadError as e:
        raise _translate(str(e)) from e
    return results


def make_zip(files: list[tuple[int, Path]], code: str, zip_path: Path) -> Path:
    # 사진·영상은 이미 압축된 형식이므로 재압축하지 않습니다.
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_STORED) as z:
        for n, path in files:
            z.write(path, f"{code}_{n:02d}{path.suffix}")
    return zip_path
