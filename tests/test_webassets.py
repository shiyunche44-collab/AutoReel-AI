"""维基素材源测试(不发真实网络请求)。

Wikimedia 有 robot policy,单测里绝不能真去连它 —— 一旦把 CI 的 IP 拖黑,
整条链就再也拿不到真实照片了。这里全部用假响应。
"""
from __future__ import annotations

import json

import pytest

from app.providers import webassets


# --------------------------------------------------------------------------
# 标题过滤:Commons 全文检索对 "EV" 这类缩写极不友好,不筛会混进图表
# --------------------------------------------------------------------------
def test_rejects_charts_and_diagrams():
    tokens = webassets._significant_tokens("BYD Han EV")
    assert tokens == ["BYD", "Han"], tokens
    for bad in (
        "Electric vehicle sales share by vehicle type and country.jpg",
        "Extracellular vesicle diagram.jpg",
        "BYD Han logo.svg",
        "BYD Han sales chart.png",
    ):
        assert webassets._title_matches(bad, tokens) is False, bad


def test_accepts_real_photo_filenames():
    tokens = webassets._significant_tokens("BYD Han EV")
    for good in (
        "2020 BYD Han EV front.jpg",
        "Red BYD Han EV.jpg",
        "BYD Han IAA 2023 1X7A0646.jpg",
    ):
        assert webassets._title_matches(good, tokens) is True, good


def test_chinese_subject_gets_no_token_filter():
    """中文主体不能派生出英文词元 —— Commons 返回的往往是英文文件名,
    用中文词元去匹配会把正确答案全误杀。"""
    assert webassets._significant_tokens("比亚迪汉 EV") == []


def test_no_bogus_latin_variant_from_chinese_subject():
    """曾试图把中文主体正则"翻译"成英文再搜一遍,结果只剩 "EV"。"""
    import inspect

    src = inspect.getsource(webassets.collect)
    assert "A-Za-z0-9" not in src, "collect 里不该再有把中文剥成英文的逻辑"


# --------------------------------------------------------------------------
# utm 追踪参数清理
# --------------------------------------------------------------------------
def test_strip_tracking_params():
    url = ("https://upload.wikimedia.org/x.jpg"
           "?utm_source=commons.wikimedia.org&utm_campaign=api&keep=1")
    out = webassets._strip_tracking(url)
    assert "utm_source" not in out
    assert "utm_campaign" not in out
    assert "keep=1" in out


# --------------------------------------------------------------------------
# imageinfo → ImageRef
# --------------------------------------------------------------------------
def test_imageinfo_to_ref_extracts_license_and_author():
    page = {
        "title": "File:2020 BYD Han EV front.jpg",
        "imageinfo": [{
            "url": "https://upload.wikimedia.org/x/2020_BYD_Han_EV_front.jpg",
            "thumburl": "https://upload.wikimedia.org/x/thumb/1920px.jpg",
            "width": 1920, "height": 1080,
            "extmetadata": {
                "LicenseShortName": {"value": "CC BY-SA 4.0"},
                "Artist": {"value": "<a href='#'>Someone</a>"},
            },
        }],
    }
    ref = webassets._imageinfo_to_ref(page, "BYD Han EV",
                                      webassets._significant_tokens("BYD Han EV"))
    assert ref is not None
    assert ref.origin == "commons"
    assert ref.license == "CC BY-SA 4.0"
    assert ref.author == "Someone", "HTML 标签应被剥掉"
    assert "thumb" in ref.url


def test_imageinfo_without_url_returns_none():
    page = {"title": "File:x.jpg", "imageinfo": [{}]}
    assert webassets._imageinfo_to_ref(page, "q", []) is None


def test_imageinfo_non_image_extension_rejected():
    page = {
        "title": "File:BYD Han logo.svg",
        "imageinfo": [{"url": "https://upload.wikimedia.org/x.svg"}],
    }
    assert webassets._imageinfo_to_ref(page, "BYD Han", ["BYD", "Han"]) is None


# --------------------------------------------------------------------------
# 缓存:Wikimedia 会限流,不能每次都重打
# --------------------------------------------------------------------------
def test_cache_avoids_refetch(monkeypatch, tmp_path):
    monkeypatch.setattr(webassets, "_cache_dir", lambda: tmp_path)
    calls = {"n": 0}

    def fake():
        calls["n"] += 1
        return [webassets.ImageRef(url="https://x/1.jpg", origin="commons")]

    first = webassets._cached("commons", "BYD Han", 5, fake)
    second = webassets._cached("commons", "BYD Han", 5, fake)
    assert calls["n"] == 1, "缓存命中时不该再发请求"
    assert first[0].url == second[0].url


def test_cache_expires(monkeypatch, tmp_path):
    # 用负 TTL 而不是 0:Windows 上 time.time() 分辨率约 15ms,两次取值可能
    # 完全相同,delta 恰好 0.0,`delta > 0` 判定不出来。
    monkeypatch.setattr(webassets, "_cache_dir", lambda: tmp_path)
    monkeypatch.setattr(webassets, "_CACHE_TTL_SECONDS", -1)
    calls = {"n": 0}

    def fake():
        calls["n"] += 1
        return [webassets.ImageRef(url="https://x/1.jpg", origin="commons")]

    webassets._cached("commons", "BYD Han", 5, fake)
    webassets._cached("commons", "BYD Han", 5, fake)
    assert calls["n"] == 2, "TTL 过期后应重新抓取"


def test_cache_survives_corrupt_file(monkeypatch, tmp_path):
    """缓存文件损坏时要能自动重抓,而不是抛异常。"""
    monkeypatch.setattr(webassets, "_cache_dir", lambda: tmp_path)
    path = webassets._cache_key("commons", "BYD Han")
    path.write_text("{ 坏掉的 json", encoding="utf-8")
    calls = {"n": 0}

    def fake():
        calls["n"] += 1
        return [webassets.ImageRef(url="https://x/1.jpg")]

    refs = webassets._cached("commons", "BYD Han", 5, fake)
    assert calls["n"] == 1
    assert refs[0].url == "https://x/1.jpg"


# --------------------------------------------------------------------------
# collect 的降级:任何入口抛错都要继续,而不是整个素材层失败
# --------------------------------------------------------------------------
def test_collect_survives_all_attempts_failing(monkeypatch):
    def boom(*a, **k):
        raise webassets.WebAssetError("403 robot policy")

    monkeypatch.setattr(webassets, "commons_search_cached", boom)
    monkeypatch.setattr(webassets, "wikipedia_images", boom)
    refs, errors = webassets.collect("BYD Han", limit=5)
    assert refs == []
    assert len(errors) == 2
    assert all("抓取失败" in e for e in errors)


def test_collect_merges_both_entries(monkeypatch, tmp_path):
    """两个入口都要跑,合并到 limit —— Commons 常常只给两三张。"""
    monkeypatch.setattr(webassets, "_cache_dir", lambda: tmp_path)
    monkeypatch.setattr(
        webassets, "commons_search_cached",
        lambda q, limit=8: [webassets.ImageRef(url="https://x/a.jpg", origin="commons")],
    )
    monkeypatch.setattr(
        webassets, "wikipedia_images",
        lambda title, limit=12: [
            webassets.ImageRef(url="https://x/b.jpg", origin="wikipedia"),
            webassets.ImageRef(url="https://x/a.jpg", origin="wikipedia"),  # 重复
        ],
    )
    refs, errors = webassets.collect("BYD Han", limit=5)
    assert [r.url for r in refs] == ["https://x/a.jpg", "https://x/b.jpg"]
    assert errors == []


def test_collect_respects_limit(monkeypatch, tmp_path):
    monkeypatch.setattr(webassets, "_cache_dir", lambda: tmp_path)
    monkeypatch.setattr(
        webassets, "commons_search_cached",
        lambda q, limit=8: [webassets.ImageRef(url=f"https://x/{i}.jpg") for i in range(8)],
    )
    refs, _ = webassets.collect("BYD Han", limit=3)
    assert len(refs) == 3


# ==========================================================================
# Bing 图片 / 网页检索解析
# ==========================================================================
BING_IMG_HTML = (
    '<a class="iusc" style="x" m="{&quot;murl&quot;:&quot;https://img.x.com/han.jpg&quot;,'
    '&quot;purl&quot;:&quot;https://news.x.com/a&quot;,&quot;t&quot;:&quot;比亚迪汉 实拍&quot;}" h="ID">'
    '<a class="iusc" m="{&quot;murl&quot;:&quot;https://img.x.com/chart.png&quot;,'
    '&quot;t&quot;:&quot;sales chart&quot;}">'
    '<a class="iusc" m="{&quot;murl&quot;:&quot;https://img.x.com/han.jpg&quot;}">'
    '<a class="iusc" m="not-json">'
)


def test_parse_bing_images():
    refs = webassets.parse_bing_images(BING_IMG_HTML, "比亚迪汉")
    assert [r.url for r in refs] == ["https://img.x.com/han.jpg"]
    assert refs[0].source_page == "https://news.x.com/a"
    assert refs[0].origin == "bing"


BING_WEB_HTML = """
<ol id="b_results">
<li class="b_algo"><div><h2><a href="https://car.autohome.com.cn/pic/series/5713.html" h="x">【比亚迪汉】图片</a></h2></div></li>
<li class="b_algo"><h2 class="t"><a target="_blank" href="https://www.douyin.com/video/730&amp;x=1">抖音</a></h2></li>
<li class="b_ad"><h2><a href="https://ad.example.com">广告</a></h2></li>
</ol>
"""


def test_parse_bing_web():
    refs = webassets.parse_bing_web(BING_WEB_HTML)
    assert [r.url for r in refs] == [
        "https://car.autohome.com.cn/pic/series/5713.html",
        "https://www.douyin.com/video/730&x=1",
    ]
    assert refs[0].note == "【比亚迪汉】图片"
