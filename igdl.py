"""인스타그램 게시물/릴스 다운로드 로직 (yt-dlp 기반, Flask와 무관).

- 영상: yt-dlp가 내려받습니다.
- 사진: yt-dlp는 사진 전용 게시물을 지원하지 않으므로(`There is no video in this post`),
  추출 결과의 thumbnails(image_versions2 후보) 중 가장 큰 이미지를 직접 받습니다.
"""
from __future__ import annotations

import os
import re
import zipfile
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

import requests
import yt_dlp
from yt_dlp.utils import DownloadError

ALLOWED_PAGE_HOSTS = {"instagram.com", "www.instagram.com", "m.instagram.com"}
# 미디어 CDN 호스트 접미사. 서버가 임의 주소로 요청을 보내는 것(SSRF)을 막기 위한 허용 목록입니다.
ALLOWED_MEDIA_SUFFIXES = (".cdninstagram.com", ".fbcdn.net")

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

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.message = message
        self.status = status


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
        raise IgError("게시물(/p/), 릴스(/reel/), IGTV(/tv/) 주소만 지원합니다. 스토리·프로필은 지원하지 않습니다.")
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
    if cookie_file := os.environ.get("IG_COOKIES_FILE"):
        opts["cookiefile"] = cookie_file
    if browser := os.environ.get("IG_COOKIES_FROM_BROWSER"):
        opts["cookiesfrombrowser"] = (browser,)
    opts.update(extra)
    return opts


def _extract(url: str, opts: dict, download: bool) -> dict:
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
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
            "자주 생기면 쿠키 설정을 해 보세요 (README의 '쿠키 설정' 참고).",
            429,
        )
    if any(k in low for k in ("login", "log in", "cookies", "rate-limit", "empty media response")):
        return IgError(
            "로그인이 필요한 게시물이거나 인스타그램이 요청을 제한했습니다. "
            "비공개 계정 게시물이나 제한된 게시물은 쿠키 설정이 필요합니다 (README의 '쿠키 설정' 참고).",
            401,
        )
    if "404" in low or "not found" in low or "unavailable" in low:
        return IgError("게시물을 찾을 수 없습니다. 삭제되었거나 주소가 잘못되었을 수 있습니다.", 404)
    return IgError(f"가져오지 못했습니다: {msg}", 502)


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


def fetch_post(raw_url: str, dest: Path, only: int | None = None) -> list[tuple[int, Path]]:
    """게시물의 영상·사진을 dest에 저장하고 (항목 번호, 파일 경로) 목록을 돌려줍니다.

    only를 주면 캐러셀의 해당 번호(1부터) 항목만 받습니다.
    """
    url = normalize_url(raw_url)
    dest.mkdir(parents=True, exist_ok=True)
    opts = _opts(
        outtmpl=str(dest / "%(playlist_index|1)s.%(ext)s"),
        format="b[ext=mp4]/b/bv*+ba",
        merge_output_format="mp4",
    )
    if only:
        opts["playlist_items"] = str(only)

    results = []
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            # download=True로 한 번에 받으면 사진 항목(포맷 없음)에서 "No video formats found!"로
            # 실패하므로, 정보만 추출한 뒤 영상 항목에만 다운로드를 수행합니다.
            info = ydl.extract_info(url, download=False)
            if not info:
                raise IgError("게시물 정보를 가져오지 못했습니다.", 502)
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
