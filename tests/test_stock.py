"""免费图库(Pexels / Pixabay / Coverr / Unsplash / Openverse)响应解析、合并顺序与"没 Key 就跳过"。"""
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
    monkeypatch.setattr(config, "COVERR_API_KEY", "")
    monkeypatch.setattr(config, "UNSPLASH_ACCESS_KEY", "")
    monkeypatch.setattr(config, "USE_OPENVERSE", False)

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


def test_coverr_both_shapes_and_vertical_first():
    hits = {"hits": [
        {"slug": "city-lights", "title": "City lights", "duration": 12, "is_vertical": False,
         "urls": {"mp4_download": "https://c/land.mp4"}},
        {"slug": "rain", "duration": 9, "is_vertical": True,
         "urls": {"mp4": "https://c/port.mp4"}},
        {"slug": "broken", "urls": {}},
    ]}
    cands = stock.parse_coverr_videos(hits)
    assert [c.url for c in cands] == ["https://c/port.mp4", "https://c/land.mp4"]
    assert cands[1].page_url == "https://coverr.co/videos/city-lights"
    assert all(c.origin == "stock:coverr" for c in cands)
    other = stock.parse_coverr_videos({"videos": [
        {"slug": "x", "urls": {"mp4_preview": "https://c/p.mp4"}}]})
    assert [c.url for c in other] == ["https://c/p.mp4"]
    assert stock.parse_coverr_videos({"hits": "nope"}) == []


def test_unsplash_photos():
    p = stock.parse_unsplash_photos({"results": [
        {"urls": {"regular": "https://u/r.jpg", "full": "https://u/f.jpg"},
         "links": {"html": "https://unsplash.com/photos/1"},
         "user": {"name": "Ann"}, "alt_description": "city at night", "width": 4000},
        {"urls": {"full": "https://u/f2.jpg"}},
        {"urls": {}},
    ]})
    assert [x.url for x in p] == ["https://u/r.jpg", "https://u/f2.jpg"]
    assert p[0].origin == "stock:unsplash" and p[0].author == "Ann"
    assert p[0].source_page == "https://unsplash.com/photos/1"


def test_openverse_images_license():
    p = stock.parse_openverse_images({"results": [
        {"url": "https://o/1.jpg", "foreign_landing_url": "https://flickr.com/1",
         "creator": "Bo", "license": "by", "license_version": "4.0", "title": "Car show"},
        {"url": "https://o/2.jpg", "license": "cc0", "license_version": "1.0"},
        {"title": "no url"},
    ]})
    assert [x.url for x in p] == ["https://o/1.jpg", "https://o/2.jpg"]
    assert p[0].license == "CC BY 4.0" and p[0].origin == "stock:openverse"
    assert p[1].license == "CC0 1.0"


def _route(monkeypatch, calls):
    """按 URL 前缀返回对应家的样例响应,并记录调用顺序。"""
    payloads = {
        stock.PEXELS_VIDEO_API: PEXELS_VIDEOS,
        stock.PIXABAY_VIDEO_API: {"hits": [{"videos": {"medium": {"url": "https://p/m.mp4",
                                                                   "height": 1080}}}]},
        stock.COVERR_VIDEO_API: {"hits": [{"slug": "a", "urls": {"mp4": "https://c/a.mp4"}}]},
        stock.PEXELS_PHOTO_API: {"photos": [{"src": {"portrait": "https://e/1.jpg"}}]},
        stock.PIXABAY_PHOTO_API: {"hits": [{"largeImageURL": "https://x/1.jpg"}]},
        stock.UNSPLASH_PHOTO_API: {"results": [{"urls": {"regular": "https://u/1.jpg"}}]},
        stock.OPENVERSE_IMAGE_API: {"results": [{"url": "https://o/1.jpg"}]},
    }

    def _get(url, *, headers=None):
        # 前缀最长的优先,避免 pixabay.com/api/ 吞掉 pixabay.com/api/videos/
        base = max((b for b in payloads if url.startswith(b + "?")), key=len)
        calls.append((base, headers or {}))
        return payloads[base]

    monkeypatch.setattr(stock, "_get_json", _get)
    monkeypatch.setattr(stock.webassets, "_cached", lambda kind, q, l, fetch, **k: fetch())


def _all_keys(monkeypatch):
    for name in ("PEXELS_API_KEY", "PIXABAY_API_KEY", "COVERR_API_KEY", "UNSPLASH_ACCESS_KEY"):
        monkeypatch.setattr(config, name, "k")
    monkeypatch.setattr(config, "USE_OPENVERSE", True)


def test_merge_order(monkeypatch):
    _all_keys(monkeypatch)
    calls: list = []
    _route(monkeypatch, calls)
    vids = stock.search_videos("city night")
    assert [c.origin for c in vids] == ["stock:pexels", "stock:pixabay", "stock:coverr"]
    photos = stock.search_photos("city night")
    assert [p.origin for p in photos] == ["stock:pexels", "stock:pixabay",
                                          "stock:unsplash", "stock:openverse"]
    headers = dict(calls)
    assert headers[stock.UNSPLASH_PHOTO_API]["Authorization"] == "Client-ID k"
    assert headers[stock.COVERR_VIDEO_API]["Authorization"] == "Bearer k"


def test_openverse_needs_no_key_and_can_be_disabled(monkeypatch):
    for name in ("PEXELS_API_KEY", "PIXABAY_API_KEY", "COVERR_API_KEY", "UNSPLASH_ACCESS_KEY"):
        monkeypatch.setattr(config, name, "")
    monkeypatch.setattr(config, "USE_OPENVERSE", True)
    calls: list = []
    _route(monkeypatch, calls)
    assert stock.available()
    assert stock.search_videos("x") == []
    assert [p.origin for p in stock.search_photos("x")] == ["stock:openverse"]
    monkeypatch.setattr(config, "USE_OPENVERSE", False)
    calls.clear()
    assert stock.search_photos("x") == [] and not calls
    assert not stock.available()


def test_one_source_failing_keeps_the_others(monkeypatch):
    _all_keys(monkeypatch)
    calls: list = []
    _route(monkeypatch, calls)
    ok = stock._get_json

    def _get(url, *, headers=None):
        if url.startswith(stock.UNSPLASH_PHOTO_API):
            raise RuntimeError("403 rate limit")
        return ok(url, headers=headers)

    monkeypatch.setattr(stock, "_get_json", _get)
    errors: list[str] = []
    photos = stock.search_photos("city", errors=errors)
    assert [p.origin for p in photos] == ["stock:pexels", "stock:pixabay", "stock:openverse"]
    assert len(errors) == 1 and "Unsplash" in errors[0]
