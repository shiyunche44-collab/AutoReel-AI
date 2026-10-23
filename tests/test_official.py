"""官方素材渠道:品牌识别、官方号判定、官网 / 汽车之家 / 懂车帝页面解析。

页面解析用的是精简过的样例 HTML —— 对方改版时先在这里补样例再修解析。
"""
from __future__ import annotations

import json

import pytest

from app.categories import load_profile
from app.providers import official, platform_video


SITES = load_profile("auto_long").official_sites


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
    got = official.platform_official("比亚迪汉", ["比亚迪", "BYD"], markers=("汽车",))
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


CATALOG_JS = (
    'var listCompare$100= [{"I":489,"N":"小米汽车","L":"X","List":[{"I":667,"N":"小米汽车",'
    '"List":[{"I":6962,"N":"小米SU7"},{"I":7915,"N":"小米SU7 Ultra"},{"I":7793,"N":"小米YU7"}]}]},'
    '{"I":75,"N":"比亚迪","L":"B","List":[{"I":1,"N":"比亚迪","List":[{"I":5713,"N":"汉"},'
    '{"I":4000,"N":"汉家族"}]}]}];'
)


def test_parse_autohome_catalog():
    rows = official.parse_autohome_catalog(CATALOG_JS)
    assert ("7793", "小米汽车", "小米YU7") in rows
    assert len(rows) == 5
    assert official.parse_autohome_catalog("garbage") == []


def test_match_autohome_series():
    rows = official.parse_autohome_catalog(CATALOG_JS)
    assert official.match_autohome_series("小米YU7", rows) == "7793"
    # SU7 与 SU7 Ultra:取主体里包含的最长车系名
    assert official.match_autohome_series("小米SU7 Ultra", rows) == "7915"
    assert official.match_autohome_series("小米 SU7", rows) == "6962"
    # 品牌 + 单字车系名
    assert official.match_autohome_series("比亚迪汉 EV", rows) == "5713"
    # 主体只写了型号
    assert official.match_autohome_series("YU7", rows) == "7793"
    assert official.match_autohome_series("理想L9", rows) == ""


def test_find_autohome_series_falls_back_to_search(monkeypatch):
    def _boom():
        raise official.webassets.WebAssetError("HTTP 503")

    monkeypatch.setattr(official, "autohome_catalog", _boom)
    queries: list[str] = []

    def _bing(q, **k):
        queries.append(q)
        return ["https://www.autohome.com.cn/7793/"]

    monkeypatch.setattr(official.webassets, "bing_web", _bing)
    errors: list[str] = []
    assert official.find_autohome_series("小米YU7", errors) == "7793"
    assert "图片" not in queries[0]
    assert errors


def test_find_dongchedi_series(monkeypatch):
    payload = ('{"data":[{"display":{"series_id":99,"series_name":"小米SU7"}},'
               '{"display":{"series_id":20041,"series_name":"小米YU7"}}]}')
    monkeypatch.setattr(official.webassets, "_http_text", lambda url, **k: payload)
    assert official.find_dongchedi_series("小米YU7") == "20041"


def test_autohome_images_tag_angles(monkeypatch):
    monkeypatch.setattr(official, "autohome_catalog",
                        lambda: [("5713", "比亚迪", "汉")])
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


# --------------------------------------------------------------------------
# B 站官方号投稿
# --------------------------------------------------------------------------
def test_model_keyword():
    assert official.model_keyword("小米YU7") == "YU7"
    assert official.model_keyword("特斯拉Model Y") == "Model Y"
    assert official.model_keyword("比亚迪海豹") == "比亚迪海豹"
    assert official.model_keyword("比亚迪海豹06", ["比亚迪", "BYD"]) == "海豹06"


def test_sibling_models():
    aliases = ["小米汽车", "小米SU7", "小米YU7", "Xiaomi SU7", "Xiaomi YU7", "Xiaomi EV"]
    assert official.sibling_models(aliases, "YU7") == ["SU7"]
    tesla = ["特斯拉", "Tesla", "Model 3", "Model Y", "Model S", "Model X", "Cybertruck"]
    assert official.sibling_models(tesla, "Model Y") == ["Model 3", "Model S", "Model X"]


def test_rank_uploads_prefers_showcase_in_sweet_spot():
    H = platform_video.PlatformHit
    hits = [H("bilibili", "live", "小米YU7八项直播测试,全程回顾", duration=22000),
            H("bilibili", "launch", "小米YU7发布会", duration=4000),
            H("bilibili", "tvc", "小米YU7 宣传片", duration=90),
            H("bilibili", "misc", "小米YU7 七月", duration=60)]
    assert [h.url for h in official.rank_uploads(hits)] == ["tvc", "misc", "launch", "live"]


def test_bilibili_official_filters_titles(monkeypatch):
    H = platform_video.PlatformHit
    monkeypatch.setattr(platform_video, "bilibili_users", lambda name, **k: [
        {"mid": 1, "name": "小米汽车", "verified": True},
        {"mid": 2, "name": "小米汽车车友会", "verified": True},   # 名字对得上但不像官方号
        {"mid": 3, "name": "小米汽车官方粉丝", "verified": False},  # 没有机构认证
    ])
    seen: list[int] = []

    def uploads(mid, keyword, **k):
        seen.append(mid)
        assert keyword == "YU7"
        return [H("bilibili", "a", "YU7 GT 高环极速测试", duration=60),
                H("bilibili", "b", "SU7 和 YU7 一起出发", duration=60),
                H("bilibili", "c", "小米汽车七月权益", duration=60)]

    monkeypatch.setattr(platform_video, "bilibili_uploads", uploads)
    monkeypatch.setattr(platform_video, "_cached_hits", lambda p, q, n, fetch: fetch())
    got = official.bilibili_official("小米YU7", ["小米汽车", "小米SU7", "小米YU7"],
                                     markers=("汽车",))
    assert seen == [1]
    assert [h.url for h in got] == ["a"] and got[0].official


def test_url_names_model_codes():
    assert official.url_names("https://www.tesla.cn/modely", "Model Y")
    assert official.url_names("https://x/Model-Y-Premium-Desktop.mp4", "Model Y")
    assert official.url_names("https://s1.xiaomiev.com/images/yu7_20260521/pc/1.jpg", "YU7")
    assert not official.url_names("https://s1.xiaomiev.com/images/su7_20260107/a.png", "YU7")
    assert not official.url_names("https://x/Model-Y-Premium.mp4", "Model S")


STRICT_HTML = """
<video src="https://cdn.xiaomiev.com/yu7/pc/hero.mp4"></video>
<video src="https://cdn.xiaomiev.com/yu7/m/hero.mp4"></video>
<video src="https://cdn.xiaomiev.com/su7/pc/hero.mp4"></video>
<img src="https://cdn.xiaomiev.com/yu7/pc/exterior-01.jpg">
<img src="https://cdn.xiaomiev.com/su7_2026/pc/app_photo.jpg">
"""


def test_site_media_strict_keeps_only_this_model(monkeypatch):
    monkeypatch.setattr(official.webassets, "bing_web", lambda q, **k: [
        "https://www.xiaomiev.com/", "https://www.xiaomiev.com/yu7"])
    pages: list[str] = []

    def fetch(url, **k):
        pages.append(url)
        return STRICT_HTML

    monkeypatch.setattr(official.clips, "fetch_page", fetch)
    spec = {"aliases": ("小米汽车", "小米SU7", "小米YU7"), "sites": ("xiaomiev.com",), "media": ()}
    vids, stills = official.site_media("小米YU7", spec, strict=True)
    assert pages == ["https://www.xiaomiev.com/yu7"]          # 首页不要
    assert [v.url for v in vids] == ["https://cdn.xiaomiev.com/yu7/pc/hero.mp4"]
    assert [s.url for s in stills] == ["https://cdn.xiaomiev.com/yu7/pc/exterior-01.jpg"]


def test_dongchedi_login_shell_returns_nothing(monkeypatch):
    monkeypatch.setattr(official, "dongchedi_cookies", lambda: {})
    monkeypatch.setattr(official, "find_dongchedi_series", lambda s: "20041")
    shell = '<img src="https://p6-dcd.byteimg.com/img/tos-cn-i-dcdx/abc~1200x0.jpg">'
    monkeypatch.setattr(official.webassets, "_http_text", lambda url, **k: shell)
    assert official.dongchedi_images("小米YU7") == []


def _dcd_page(series_id, trims):
    props = {"props": {"pageProps": {
        "seriesInfo": {"series_id": series_id},
        "pictureInfo": {"series_id": series_id, "picture_list": trims}}}}
    return ('<html><script id="__NEXT_DATA__" type="application/json">'
            + json.dumps(props) + "</script></html>")


def test_parse_dongchedi_gallery_upsizes_and_puts_new_year_first():
    body = _dcd_page(20041, [
        {"year": 2025, "large_pic_url": [
            "http://p9-dcd.byteimg.com/a.jpg~tplv-x-original:750:0.jpg?psm=1"]},
        {"year": 2026, "large_pic_url": [
            "http://p9-dcd.byteimg.com/b.jpg~tplv-x-original:750:0.jpg?psm=1"]},
    ])
    urls = official.parse_dongchedi_gallery(body, "20041")
    assert urls == ["https://p9-dcd.byteimg.com/b.jpg~tplv-x-original:1920:0.jpg?psm=1",
                    "https://p9-dcd.byteimg.com/a.jpg~tplv-x-original:1920:0.jpg?psm=1"]
    # 别的车系的页面(跳走了 / 没登录)不认
    assert official.parse_dongchedi_gallery(body, "99999") == []
    assert official.parse_dongchedi_gallery("<html>请登录</html>", "20041") == []


def test_dongchedi_gallery_requires_login(monkeypatch):
    monkeypatch.setattr(official, "dongchedi_cookies", lambda: {})
    with pytest.raises(official.DongchediLoginRequired):
        official.GALLERIES["dongchedi"].fetch("小米YU7", {}, [])


def test_dongchedi_logged_in_takes_official_first(monkeypatch):
    import httpx

    monkeypatch.setattr(official, "dongchedi_cookies", lambda: {"sessionid": "x"})
    monkeypatch.setattr(official, "find_dongchedi_series", lambda s: "20041")

    def handler(request):
        cat = str(request.url).rsplit("-", 1)[1]
        urls = [f"http://p9-dcd.byteimg.com/{cat}{i}.jpg~t-original:750:0.jpg" for i in range(20)]
        return httpx.Response(200, text=_dcd_page(20041, [{"year": 2026, "large_pic_url": urls}]))

    real = httpx.Client
    monkeypatch.setattr(httpx, "Client",
                        lambda **kw: real(transport=httpx.MockTransport(handler), **kw))
    refs = official.dongchedi_images("小米YU7", require_login=True)
    assert [r.note for r in refs[:12]] == ["懂车帝官方图"] * 12
    assert [r.angle for r in refs[12:]] == ["exterior_45"] * 6 + ["interior"] * 6
    assert all("original:1920:0" in r.url for r in refs)


def test_dongchedi_expired_login_raises(monkeypatch):
    import httpx

    monkeypatch.setattr(official, "dongchedi_cookies", lambda: {"sessionid": "old"})
    monkeypatch.setattr(official, "find_dongchedi_series", lambda s: "20041")
    real = httpx.Client
    monkeypatch.setattr(httpx, "Client", lambda **kw: real(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, text="<html></html>")), **kw))
    with pytest.raises(official.DongchediLoginRequired):
        official.dongchedi_images("小米YU7", require_login=True)


YICHE_HTML = """
<title>小米YU7图片_小米YU7外观内饰图片-易车</title>
<a href="/xiaomiyu7/tupian/">图片</a><a href="https://example.com/tupian/">别家</a>
<img src="//image.bitautoimg.com/autoalbum/files/20250626/abc_1200x800.jpg">
<img src="//image.bitautoimg.com/autoalbum/files/20250626/thumb_120x90.jpg">
<img src="https://cdn.other.com/ad_1200x800.jpg">
<img src="//image.bitautoimg.com/logo.png">
"""


def test_find_yiche_series(monkeypatch):
    monkeypatch.setattr(official.webassets, "bing_web", lambda q, **k: [
        "https://car.yiche.com/tupian/", "https://car.yiche.com/xiaomiyu7/tupian/",
        "https://www.yiche.com/"])
    assert official.find_yiche_series("小米YU7") == "xiaomiyu7"


def test_parse_yiche_images_keeps_only_yiche_cdn_big_pics():
    urls = official.parse_yiche_images(YICHE_HTML, "https://car.yiche.com/xiaomiyu7/")
    assert urls == ["https://image.bitautoimg.com/autoalbum/files/20250626/abc_1200x800.jpg"]
    assert official.yiche_gallery_links(YICHE_HTML, "https://car.yiche.com/xiaomiyu7/") == [
        "https://car.yiche.com/xiaomiyu7/tupian/"]


def test_yiche_images_via_browser(monkeypatch):
    monkeypatch.setattr(official, "find_yiche_series", lambda s: "xiaomiyu7")
    pages: list[str] = []

    def fetch(url, **k):
        pages.append(url)
        return YICHE_HTML

    monkeypatch.setattr(official.browser_fetch, "fetch", fetch)
    refs = official.yiche_images("小米YU7")
    assert pages == ["https://car.yiche.com/xiaomiyu7/", "https://car.yiche.com/xiaomiyu7/tupian/"]
    assert [r.origin for r in refs] == ["official:yiche"]


def test_yiche_captcha_raises_unavailable(monkeypatch):
    monkeypatch.setattr(official, "find_yiche_series", lambda s: "xiaomiyu7")

    def blocked(url, **k):
        raise official.browser_fetch.BrowserBlocked("页面含「tcaptcha」")

    monkeypatch.setattr(official.browser_fetch, "fetch", blocked)
    with pytest.raises(official.YicheUnavailable, match="browser_unblock"):
        official.GALLERIES["yiche"].fetch("小米YU7", {}, [])


def test_yiche_other_series_page_gives_nothing_and_retries(monkeypatch):
    monkeypatch.setattr(official, "find_yiche_series", lambda s: "xiaomiyu7")
    monkeypatch.setattr(official.browser_fetch, "fetch",
                        lambda url, **k: '<img src="//image.bitautoimg.com/a_1200x800.jpg">')
    with pytest.raises(official.YicheUnavailable):
        official.yiche_images("小米YU7")
