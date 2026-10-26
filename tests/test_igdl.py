import zipfile

import pytest

import igdl
from igdl import IgError

from conftest import JPEG, MP4


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("https://www.instagram.com/p/AbC_d-1/", "https://www.instagram.com/p/AbC_d-1/"),
        ("https://www.instagram.com/p/AbC_d-1/?igsh=tracking&utm_source=x", "https://www.instagram.com/p/AbC_d-1/"),
        ("instagram.com/reel/XYZ123", "https://www.instagram.com/reel/XYZ123/"),
        ("https://www.instagram.com/someone/reel/XYZ123/", "https://www.instagram.com/reel/XYZ123/"),
        ("https://www.instagram.com/reels/XYZ123/", "https://www.instagram.com/reel/XYZ123/"),
        ("https://m.instagram.com/tv/XYZ123/", "https://www.instagram.com/tv/XYZ123/"),
        ("  https://www.instagram.com/p/AbC/  ", "https://www.instagram.com/p/AbC/"),
    ],
)
def test_normalize_url_accepts(raw, expected):
    assert igdl.normalize_url(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "https://example.com/p/AbC/",
        "https://instagram.com.evil.com/p/AbC/",
        "https://evilinstagram.com/p/AbC/",
        "https://www.instagram.com/someone/",  # 프로필
        "https://www.instagram.com/stories/someone/123/",  # 스토리
        "https://www.instagram.com/p/",
        "https://www.instagram.com/p/가나다/",  # 파일명·헤더에 쓰이므로 ASCII만 허용
        "file:///etc/passwd",
        "javascript:alert(1)",
    ],
)
def test_normalize_url_rejects(raw):
    with pytest.raises(IgError):
        igdl.normalize_url(raw)


@pytest.mark.parametrize(
    "url, ok",
    [
        ("https://scontent-ssn1-1.cdninstagram.com/v/t51/a.jpg", True),
        ("https://scontent.xx.fbcdn.net/v/a.jpg", True),
        ("http://scontent.cdninstagram.com/a.jpg", False),  # https만
        ("https://cdninstagram.com.evil.com/a.jpg", False),
        ("https://evilcdninstagram.com/a.jpg", False),
        ("https://169.254.169.254/latest/meta-data", False),
        ("", False),
    ],
)
def test_is_allowed_media_url(url, ok):
    assert igdl.is_allowed_media_url(url) is ok


def test_translate_login_required():
    err = igdl._translate(
        "ERROR: [Instagram] AbC: Instagram sent an empty media response. "
        "Check if this post is accessible in your browser without being logged-in."
    )
    assert err.status == 401 and "쿠키" in err.message


def test_translate_not_found():
    assert igdl._translate("ERROR: [Instagram] AbC: HTTP Error 404: Not Found").status == 404


def test_get_post_image_only(fake_instagram):
    post = igdl.get_post("https://www.instagram.com/p/IMG1/?igsh=zzz")
    assert post["url"] == "https://www.instagram.com/p/IMG1/"
    assert post["uploader"] == "tester"
    assert [i["kind"] for i in post["items"]] == ["image"]
    assert post["items"][0]["thumbnail"].endswith("/big.jpg")
    assert post["items"][0]["width"] == 1080


def test_get_post_video(fake_instagram):
    post = igdl.get_post("https://www.instagram.com/reel/VID1/")
    assert [i["kind"] for i in post["items"]] == ["video"]
    assert post["items"][0]["duration"] == 3.0


def test_get_post_carousel_mixed(fake_instagram):
    post = igdl.get_post("https://www.instagram.com/p/CAR1/")
    assert [(i["index"], i["kind"]) for i in post["items"]] == [(1, "video"), (2, "image"), (3, "image")]


def test_get_post_missing(fake_instagram):
    with pytest.raises(IgError) as e:
        igdl.get_post("https://www.instagram.com/p/NOPE/")
    assert e.value.status == 404


def test_fetch_image_only_downloads_largest(fake_instagram, tmp_path):
    [(n, path)] = igdl.fetch_post("https://www.instagram.com/p/IMG1/", tmp_path)
    assert n == 1 and path.suffix == ".jpg"
    assert path.read_bytes() == JPEG  # small.jpg가 아니라 big.jpg


def test_fetch_video(fake_instagram, tmp_path):
    [(n, path)] = igdl.fetch_post("https://www.instagram.com/reel/VID1/", tmp_path)
    assert n == 1 and path.suffix == ".mp4"
    assert path.read_bytes() == MP4


def test_fetch_carousel_all(fake_instagram, tmp_path):
    files = igdl.fetch_post("https://www.instagram.com/p/CAR1/", tmp_path)
    assert [(n, p.suffix) for n, p in files] == [(1, ".mp4"), (2, ".jpg"), (3, ".jpg")]
    assert files[0][1].read_bytes() == MP4
    assert files[1][1].read_bytes() == JPEG


def test_fetch_carousel_single_item(fake_instagram, tmp_path):
    files = igdl.fetch_post("https://www.instagram.com/p/CAR1/", tmp_path, only=3)
    assert [(n, p.name) for n, p in files] == [(3, "3.jpg")]
    assert not (tmp_path / "1.mp4").exists()  # 요청하지 않은 영상은 받지 않습니다


def test_fetch_carousel_video_item(fake_instagram, tmp_path):
    files = igdl.fetch_post("https://www.instagram.com/p/CAR1/", tmp_path, only=1)
    assert [(n, p.suffix) for n, p in files] == [(1, ".mp4")]


@pytest.mark.parametrize("url, only", [("https://www.instagram.com/p/CAR1/", 9), ("https://www.instagram.com/p/IMG1/", 2)])
def test_fetch_out_of_range(fake_instagram, tmp_path, url, only):
    with pytest.raises(IgError) as e:
        igdl.fetch_post(url, tmp_path, only=only)
    assert e.value.status == 404


def test_make_zip_names(fake_instagram, tmp_path):
    files = igdl.fetch_post("https://www.instagram.com/p/CAR1/", tmp_path / "raw")
    zp = igdl.make_zip(files, "CAR1", tmp_path / "out.zip")
    with zipfile.ZipFile(zp) as z:
        assert z.namelist() == ["CAR1_01.mp4", "CAR1_02.jpg", "CAR1_03.jpg"]
