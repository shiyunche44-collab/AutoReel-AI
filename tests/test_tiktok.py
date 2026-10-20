"""TikTok 数据源:官方号主页、DuckDuckGo 检索、链接规范化,以及接进素材池 / 取材的几处。

不联网:yt-dlp 与网页请求全部打桩。
"""
from __future__ import annotations

import pytest

from app import config
from app.categories import load_profile
from app.pipeline import clip_resolver
from app.pipeline import library as lib_mod
from app.providers import official
from app.providers import platform_video as pv


def test_tiktok_url_normalises_and_rejects_non_video_pages():
    assert pv.tiktok_url("https://m.tiktok.com/@byd_global/video/7644173409676578069?lang=en") \
        == "https://www.tiktok.com/@byd_global/video/7644173409676578069"
    assert pv.tiktok_url("https://www.tiktok.com/@byd_global") == ""
    assert pv.tiktok_url("https://www.tiktok.com/tag/byd") == ""


def test_parse_tiktok_entries_uses_description_and_id_fallback():
    info = {"entries": [
        {"url": "https://www.tiktok.com/@nio/video/111", "title": "",
         "description": "NIO ES9 静态展示", "uploader": "nio", "duration": 15},
        {"id": "222", "title": "ET9 night drive"},       # 没有 url:用账号 + id 拼
        {"title": "缺地址也缺 id"},
        "not a dict",
    ]}
    hits = pv.parse_tiktok_entries(info, "nio")
    assert [h.url for h in hits] == ["https://www.tiktok.com/@nio/video/111",
                                     "https://www.tiktok.com/@nio/video/222"]
    assert hits[0].title == "NIO ES9 静态展示", "TikTok 的标题就是文案,标题为空时取描述"
    assert hits[0].platform == "tiktok" and hits[0].duration == 15
    assert hits[1].uploader == "nio"


DDG_BODY = """
<div class="result"><a rel="nofollow" class="result__a"
  href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fwww.tiktok.com%2F%40carview_global%2Fvideo%2F7644173409676578069%3Flang%3Den&amp;rut=x">
  2026 <b>Xiaomi YU7</b> GT walkaround | TikTok</a></div>
<div class="result"><a rel="nofollow" class="result__a"
  href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fwww.tiktok.com%2F%40carview_global&amp;rut=y">carview (@carview_global) | TikTok</a></div>
<div class="result"><a rel="nofollow" class="result__a"
  href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fwww.tiktok.com%2F%40carview_global%2Fvideo%2F7644173409676578069&amp;rut=z">dup</a></div>
"""


def test_search_tiktok_parses_ddg_results(monkeypatch):
    seen: list[str] = []

    def _fake(url, **kw):
        seen.append(url)
        return DDG_BODY

    monkeypatch.setattr(pv.webassets, "_http_text", _fake)
    hits = pv.search_tiktok("Xiaomi YU7")
    assert "site%3Atiktok.com" in seen[0]
    assert [h.url for h in hits] == ["https://www.tiktok.com/@carview_global/video/7644173409676578069"]
    assert hits[0].title == "2026 Xiaomi YU7 GT walkaround", "高亮标签与站名尾巴都要去掉"
    assert hits[0].uploader == "carview_global"


def test_search_tiktok_gives_up_on_bot_challenge(monkeypatch):
    """人机验证页必须抛错:返回空会被缓存 24 小时;也不去绕验证。"""
    monkeypatch.setattr(pv.webassets, "_http_text",
                        lambda url, **kw: '<script src="/anomaly.js?cc=botnet"></script>')
    with pytest.raises(pv.PlatformError, match="人机验证"):
        pv.search_tiktok("Xiaomi YU7")


def test_tiktok_account_failure_raises_with_proxy_hint(monkeypatch, tmp_path):
    import yt_dlp

    class _Boom:
        def __init__(self, opts):
            assert opts["playlistend"] == pv.TIKTOK_FEED_LIMIT

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def extract_info(self, url, download=False):
            raise RuntimeError("blocked")

    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(pv.config, "YTDLP_PROXY", "")
    monkeypatch.setattr(yt_dlp, "YoutubeDL", _Boom)
    with pytest.raises(pv.PlatformError, match="YTDLP_PROXY"):
        pv.tiktok_account("@byd_global")


def test_tiktok_official_keeps_on_topic_posts_and_isolates_failures(monkeypatch):
    feeds = {
        "byd_global": [
            pv.PlatformHit(platform="tiktok", url="https://www.tiktok.com/@byd_global/video/1",
                           title="BYD SEAL 06 on the coast #byd", uploader="byd_global"),
            pv.PlatformHit(platform="tiktok", url="https://www.tiktok.com/@byd_global/video/2",
                           title="Meet the new DOLPHIN", uploader="byd_global"),
            pv.PlatformHit(platform="tiktok", url="https://www.tiktok.com/@byd_global/video/3",
                           title="", uploader="byd_global"),   # 没文案:判断不了对不对题,不要
        ],
    }

    def _account(handle, **kw):
        if handle not in feeds:
            raise pv.PlatformError("gone")
        return feeds[handle]

    monkeypatch.setattr(pv, "tiktok_account", _account)
    errors: list[str] = []
    hits = official.tiktok_official("比亚迪海豹06", ["byd_global", "byd_broken"],
                                    names=["BYD SEAL 06"], errors=errors)
    assert [h.url for h in hits] == ["https://www.tiktok.com/@byd_global/video/1"]
    assert hits[0].official, "YAML 里人工核对过的号直接算官方"
    assert any("byd_broken" in e for e in errors)


def test_tiktok_on_topic_needs_the_model_not_just_the_brand():
    """品牌号每条文案都带品牌名:光凭 "NIO" 不能算在说 ET9。"""
    on = official.tiktok_on_topic
    aliases = ["蔚来", "NIO"]
    assert on("The NIO ET9 at night #NIO", "蔚来ET9", ["nio ET9"], aliases)
    assert not on("NIO ES9 Joins Hands with Yao Ming #NIO", "蔚来ET9", ["nio ET9"], aliases)
    assert not on("NIO ET95 concept", "蔚来ET9", ["nio ET9"], aliases), "型号要整词匹配"
    assert on("全新蔚来ET9 来了", "蔚来ET9", [], aliases), "中文文案整串出现也认"
    assert not on("Lynk & Co Z20 drive", "领克Z10", ["geely Z10"], ["领克", "Lynk", "Geely"])


def test_platform_official_never_name_matches_on_tiktok(monkeypatch):
    """叫 bydglobal 的号发的是油画:TikTok 不能走"上传者名含品牌"的判定。"""
    asked: list[list[str]] = []

    def _search(q, platforms=None, **kw):
        asked.append(list(platforms))
        return []

    monkeypatch.setattr(pv, "search", _search)
    official.platform_official("比亚迪汉", ["比亚迪", "BYD"],
                               platforms=["bilibili", "youtube", "tiktok"])
    assert asked and all("tiktok" not in p for p in asked)


def test_library_recognises_tiktok_urls():
    url = "https://www.tiktok.com/@nio/video/7300000000000000000?is_from_webapp=1&sender_device=pc"
    assert lib_mod.normalise_url(url) == "https://www.tiktok.com/@nio/video/7300000000000000000"
    assert lib_mod.platform_of(url) == "tiktok"


def test_rank_sources_accepts_english_names():
    hits = [pv.PlatformHit(platform="tiktok", url="https://www.tiktok.com/@byd_global/video/1",
                           title="BYD SEAL 06 exterior", official=True)]
    assert lib_mod.rank_sources(hits, "比亚迪海豹06") == []
    assert len(lib_mod.rank_sources(hits, "比亚迪海豹06", names=["BYD SEAL 06"])) == 1


def test_discover_pulls_official_tiktok_feeds(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "MEDIA_PLATFORMS", ("youtube", "tiktok"))
    monkeypatch.setattr(official, "platform_official", lambda *a, **k: [])
    searched: list[list[str]] = []
    monkeypatch.setattr(pv, "search", lambda q, platforms=None, **k: searched.append(
        list(platforms)) or [])
    got: list[tuple] = []

    def _tiktok(subject, handles, names=(), aliases=(), errors=None):
        got.append((subject, tuple(handles), tuple(names)))
        return [pv.PlatformHit(platform="tiktok", url="https://www.tiktok.com/@nio/video/9",
                               title="NIO ET9 official film", uploader="nio",
                               duration=30, official=True)]

    monkeypatch.setattr(official, "tiktok_official", _tiktok)
    lib = lib_mod.Library(subject="蔚来ET9")
    n = lib_mod.discover(lib, ["蔚来", "NIO"], [], tiktok_handles=["nio"])
    assert got and got[0][1] == ("nio",)
    assert n == 1 and lib.sources[0].platform == "tiktok" and lib.sources[0].official
    assert ["youtube", "tiktok"] in searched, "英文检索词也要给 TikTok"


def test_auto_profile_declares_verified_tiktok_handles():
    sites = load_profile("auto").official_sites
    assert sites["byd"]["tiktok"] == ("byd_global",)
    assert "tiktok" not in sites["xiaomi"] or not sites["xiaomi"]["tiktok"]


def test_english_queries_go_to_tiktok(monkeypatch):
    monkeypatch.setattr(config, "MEDIA_PLATFORMS", ("bilibili", "youtube", "tiktok"))
    assert clip_resolver._platforms_for("en") == ["youtube", "tiktok"]
    assert clip_resolver._platforms_for("zh") == ["bilibili"]
