"""네트워크 없이 yt-dlp의 실제 추출·다운로드 파이프라인을 통과시키기 위한 테스트 도구.

가짜 Instagram 추출기(FakeIG)가 게시물 정보를 돌려주고, 미디어 파일은 로컬 HTTP 서버가 제공합니다.
"""
import copy
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
import yt_dlp
from yt_dlp.extractor.common import InfoExtractor

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import igdl  # noqa: E402

JPEG = b"\xff\xd8\xff\xe0" + b"jpeg-bytes" * 10
MP4 = b"\x00\x00\x00\x18ftypmp42" + b"mp4-bytes" * 50

FILES = {
    "/big.jpg": ("image/jpeg", JPEG),
    "/small.jpg": ("image/jpeg", b"small"),
    "/v.mp4": ("video/mp4", MP4),
}


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        ctype, body = FILES.get(self.path.split("?")[0], (None, None))
        if body is None:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


@pytest.fixture(scope="session")
def media_server():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()


def _posts(base):
    video = {
        "id": "x",
        "title": "v",
        "duration": 3.0,
        "formats": [{"format_id": "0", "url": f"{base}/v.mp4", "ext": "mp4", "vcodec": "h264", "acodec": "aac"}],
        "thumbnails": [{"url": f"{base}/small.jpg", "width": 100, "height": 100}],
    }
    image = {
        "id": "x",
        "title": "i",
        "thumbnails": [  # 작은 후보가 먼저 와도 가장 큰 것을 골라야 합니다.
            {"url": f"{base}/small.jpg", "width": 320, "height": 320},
            {"url": f"{base}/big.jpg", "width": 1080, "height": 1080},
        ],
    }
    meta = {"title": "Post by tester", "channel": "tester", "description": "캡션 <b>테스트</b>"}
    return {
        "IMG1": {"id": "IMG1", **meta, **image},
        "VID1": {"id": "VID1", **meta, **video},
        "CAR1": {"id": "CAR1", "_type": "playlist", **meta, "entries": [dict(video), dict(image), dict(image)]},
    }


@pytest.fixture
def fake_instagram(media_server, monkeypatch):
    """igdl이 쓰는 YoutubeDL을, 가짜 Instagram 추출기만 가진 것으로 바꿉니다."""
    posts = _posts(media_server)

    class FakeIG(InfoExtractor):
        IE_NAME = "FakeIG"
        _VALID_URL = r"https?://www\.instagram\.com/(?:p|reel|tv)/(?P<id>[\w-]+)/?"

        def _real_extract(self, url):
            code = self._match_id(url)
            if code not in posts:
                raise yt_dlp.utils.ExtractorError("HTTP Error 404: Not Found", expected=True)
            # 진짜 추출기처럼 매번 새 데이터를 돌려줍니다. yt-dlp는 playlist_items를 처리하면서 받은 딕셔너리의
            # entries를 제자리에서 잘라 버리므로, 같은 객체를 돌려주면 한 번 부분 저장한 뒤의 조회가 깨집니다.
            return copy.deepcopy(posts[code])

    class FakeYDL(yt_dlp.YoutubeDL):
        def __init__(self, params=None, auto_init=True):
            super().__init__({**(params or {}), "allowed_extractors": ["FakeIG"]}, auto_init)
            self.add_info_extractor(FakeIG())

    monkeypatch.setattr(igdl.yt_dlp, "YoutubeDL", FakeYDL)
    monkeypatch.setattr(igdl, "is_allowed_media_url", lambda url: True)  # 로컬 서버 허용
    monkeypatch.setenv("NO_PROXY", "127.0.0.1")
    monkeypatch.setenv("no_proxy", "127.0.0.1")
    return posts
