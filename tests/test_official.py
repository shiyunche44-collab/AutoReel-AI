"""官方素材渠道:品牌识别、官方号判定、官网 / 汽车之家 / 懂车帝页面解析。

页面解析用的是精简过的样例 HTML —— 对方改版时先在这里补样例再修解析。
"""
from __future__ import annotations

from app.categories import load_profile
from app.providers import official, platform_video


SITES = load_profile("auto").official_sites


def test_profile_declares_official_sites():
    assert {"byd", "tesla", "xiaomi", "bmw"} <= set(SITES)
    assert "bydauto.com.cn" in SITES["byd"]["sites"]


def test_detect_brand_from_subject():
    key, spec = official.detect_brand("比亚迪汉 EV", SITES)
    assert key == "byd" and spec["sites"]


def test_detect_brand_prefers_llm_hint():
    key, _ = official.detect_brand("SU7 Ultra", SITES, hint="小米汽车")
    assert key == "xiaomi"


def test_detect_brand_longest_alias_wins():
    # "Model Y" 属于特斯拉;不能被更短的通用别名抢走
    key, _ = official.detect_brand("Tesla Model Y 焕新版", SITES)
    assert key == "tesla"


def test_detect_brand_unknown():
    assert official.detect_brand("某不知名新势力 X1", SITES) == ("", {})


def test_platform_official_keeps_only_official(monkeypatch):
    hits = [
        platform_video.PlatformHit(platform="bilibili", url="a", uploader="比亚迪汽车",
                                   title="比亚迪汉 官方宣传片", duration=60),
        platform_video.PlatformHit(platform="bilibili", url="b", uploader="老王说车",
                                   title="比亚迪汉 试驾", duration=300),
    ]
    monkeypatch.setattr(platform_video, "search", lambda q, **k: list(hits))
    got = official.platform_official("比亚迪汉", ["比亚迪", "BYD"])
    assert [h.url for h in got] == ["a"]
    assert got[0].official


SITE_HTML = """
<html><head>
<meta property="og:image" content="https://www.bydauto.com.cn/img/han/hero.jpg">
</head><body>
<img src="/img/logo.png">
<img src="//cdn.bydauto.com.cn/han/exterior-01.jpg">
<img src="https://cdn.bydauto.com.cn/han/thumb_120x90.jpg">
<img src="https://cdn.bydauto.com.cn/icons/wechat.png">
<video src="https://cdn.bydauto.com.cn/han/tvc.mp4"></video>
<iframe src="//player.bilibili.com/player.html?bvid=BV1xx411c7mD"></iframe>
</body></html>
"""


def test_extract_images_from_site_page():
    imgs = official.extract_images(SITE_HTML, "https://www.bydauto.com.cn/han")
    assert imgs[0] == "https://www.bydauto.com.cn/img/han/hero.jpg", "og:image 应排第一"
    assert "https://cdn.bydauto.com.cn/han/exterior-01.jpg" in imgs
    assert not any("logo" in u or "wechat" in u or "120x90" in u for u in imgs)


def test_site_media_collects_videos_embeds_and_images(monkeypatch):
    monkeypatch.setattr(official.webassets, "bing_web", lambda q, **k: [
        "https://www.bydauto.com.cn/pc/han", "https://elsewhere.com/byd-han",
    ])
    monkeypatch.setattr(official.clips, "fetch_page",
                        lambda url, **k: SITE_HTML if "bydauto" in url else "")
    vids, stills = official.site_media("比亚迪汉", {"sites": ("bydauto.com.cn",), "media": ()})
    urls = [v.url for v in vids]
    assert "https://cdn.bydauto.com.cn/han/tvc.mp4" in urls
    assert "https://www.bilibili.com/video/BV1xx411c7mD" in urls
    embed = next(v for v in vids if "bilibili" in v.url)
    assert embed.platform == "bilibili" and embed.official
    assert stills and all(s.origin == "official:site" for s in stills)


AUTOHOME_HTML = """
<ul class="uibox-con">
<li><a href="/photo/1/1.html"><img src="//car3.autoimg.cn/cardfs/product/g30/M08/AB/CD/480x360_0_q95_c42_autohomecar__ChxkPWT0001.jpg"></a></li>
<li><img src="https://car2.autoimg.cn/cardfs/product/g30/M01/AA/BB/240x180_0_q95_c42_autohomecar__ChxkPWT0002.jpg"></li>
<li><img src="https://x.autoimg.cn/www/logo.png"></li>
</ul>
"""


def test_parse_autohome_images_upgrades_thumbnails():
    imgs = official.parse_autohome_images(AUTOHOME_HTML)
    assert imgs == [
        "https://car3.autoimg.cn/cardfs/product/g30/M08/AB/CD/autohomecar__ChxkPWT0001.jpg",
        "https://car2.autoimg.cn/cardfs/product/g30/M01/AA/BB/autohomecar__ChxkPWT0002.jpg",
    ]


def test_autohome_series_id_from_search_results():
    assert official.autohome_series_id([
        "https://www.autohome.com.cn/news/1.html",
        "https://car.autohome.com.cn/pic/series/5713.html",
    ]) == "5713"
    assert official.autohome_series_id(["https://www.autohome.com.cn/5713/"]) == "5713"
    assert official.autohome_series_id(["https://club.autohome.com.cn/bbs/1"]) == ""


def test_autohome_images_tag_angles(monkeypatch):
    monkeypatch.setattr(official.webassets, "bing_web",
                        lambda q, **k: ["https://car.autohome.com.cn/pic/series/5713.html"])
    pages: list[str] = []

    def _http(url, **k):
        pages.append(url)
        return AUTOHOME_HTML.replace("0001", url[-8:-5]).replace("0002", url[-7:-5] + "x")

    monkeypatch.setattr(official.webassets, "_http_text", _http)
    refs = official.autohome_images("比亚迪汉")
    assert pages[0] == "https://car.autohome.com.cn/pic/series/5713-1.html"
    angles = {r.angle for r in refs}
    assert {"exterior_45", "interior", "detail"} <= angles
    assert all(r.origin == "official:autohome" for r in refs)


DCD_HTML = r"""
<script>{"cover":"https://p3-dcd.byteimg.com/img/motor-mis-img/abc~1200x0.jpg",
"thumb":"https://p1-dcd.byteimg.com/img/motor-mis-img/def~96x64.webp"}</script>
"""


def test_parse_dongchedi_images():
    imgs = official.parse_dongchedi_images(DCD_HTML)
    assert imgs == ["https://p3-dcd.byteimg.com/img/motor-mis-img/abc~1200x0.jpg"]


def test_gallery_failures_are_isolated(monkeypatch):
    def _boom(*a, **k):
        raise RuntimeError("改版了")

    monkeypatch.setattr(official.webassets, "_cached", lambda kind, q, l, fetch, **k: fetch())
    monkeypatch.setattr(official, "autohome_images", _boom)
    monkeypatch.setattr(official, "dongchedi_images", lambda *a, **k: [])
    errors: list[str] = []
    assert official.gallery_images("比亚迪汉", errors=errors) == []
    assert errors
