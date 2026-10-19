"""免费图库(Pexels / Pixabay)响应解析与"没 Key 就跳过"。"""
from __future__ import annotations

from app import config
from app.providers import stock


PEXELS_VIDEOS = {"videos": [{
    "url": "https://www.pexels.com/video/city-night-123/",
    "duration": 14,
    "user": {"name": "Alice"},
    "video_files": [
        {"link": "https://v/4k.mp4", "width": 3840, "height": 2160, "file_type": "video/mp4"},
        {"link": "https://v/hd-land.mp4", "width": 1920, "height": 1080, "file_type": "video/mp4"},
        {"link": "https://v/hd-port.mp4", "width": 1080, "height": 1920, "file_type": "video/mp4"},
        {"link": "https://v/sd-port.mp4", "width": 540, "height": 960, "file_type": "video/mp4"},
    ],
}]}


def test_pexels_picks_portrait_hd_not_4k():
    cands = stock.parse_pexels_videos(PEXELS_VIDEOS)
    assert len(cands) == 1
    assert cands[0].url == "https://v/hd-port.mp4"
    assert cands[0].origin == "stock:pexels"
    assert cands[0].author == "Alice"


def test_pixabay_videos():
    cands = stock.parse_pixabay_videos({"hits": [{
        "pageURL": "https://pixabay.com/videos/x-1/", "tags": "city, night",
        "user": "bob", "duration": 20,
        "videos": {"large": {"url": "https://p/l.mp4", "height": 2160},
                   "medium": {"url": "https://p/m.mp4", "height": 1080}},
    }]})
    assert cands[0].url == "https://p/l.mp4" or cands[0].url == "https://p/m.mp4"
    assert cands[0].origin == "stock:pixabay"


def test_photo_parsers():
    p = stock.parse_pexels_photos({"photos": [{
        "url": "https://www.pexels.com/photo/1/", "photographer": "Al", "alt": "auto show",
        "src": {"portrait": "https://img/portrait.jpg", "original": "https://img/o.jpg"},
    }]})
    assert p[0].url == "https://img/portrait.jpg" and p[0].source_page
    x = stock.parse_pixabay_photos({"hits": [{"largeImageURL": "https://img/l.jpg",
                                              "pageURL": "https://pixabay.com/p/1"}]})
    assert x[0].url == "https://img/l.jpg"


def test_no_keys_means_no_requests(monkeypatch):
    monkeypatch.setattr(config, "PEXELS_API_KEY", "")
    monkeypatch.setattr(config, "PIXABAY_API_KEY", "")

    def _boom(*a, **k):
        raise AssertionError("没配 Key 不该发请求")

    monkeypatch.setattr(stock, "_get_json", _boom)
    assert stock.search_videos("city night") == []
    assert stock.search_photos("city night") == []
    assert not stock.available()


def test_api_failure_is_reported_not_raised(monkeypatch):
    monkeypatch.setattr(config, "PEXELS_API_KEY", "k")
    monkeypatch.setattr(config, "PIXABAY_API_KEY", "")
    monkeypatch.setattr(stock.webassets, "_cached", lambda kind, q, l, fetch, **k: fetch())

    def _boom(*a, **k):
        raise RuntimeError("401")

    monkeypatch.setattr(stock, "_get_json", _boom)
    errors: list[str] = []
    assert stock.search_videos("city", errors=errors) == []
    assert errors and "Pexels" in errors[0]
