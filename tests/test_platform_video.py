"""平台视频(B 站 / YouTube / 抖音)检索结果解析、过滤、切段起点。

不联网:只测解析与打分这些纯函数,外加"接口挂了不抛异常"。
"""
from __future__ import annotations

import pytest

from app.providers import platform_video as pv


BILI_PAYLOAD = {
    "code": 0,
    "data": {"result": [
        {"arcurl": "http://www.bilibili.com/video/BV1aaaaaaaaa",
         "title": "<em class=\"keyword\">比亚迪汉</em> EV 静态实拍",
         "author": "比亚迪汽车", "duration": "4:35"},
        {"bvid": "BV1bbbbbbbbb", "title": "汉EV 车主一年用车直播回放",
         "author": "老王说车", "duration": "1:02:03"},
        {"title": "缺地址的条目"},
    ]},
}


def test_parse_bili_results():
    hits = pv.parse_bili_results(BILI_PAYLOAD)
    assert len(hits) == 2
    first = hits[0]
    assert first.url == "https://www.bilibili.com/video/BV1aaaaaaaaa"
    assert first.title == "比亚迪汉 EV 静态实拍", "高亮标签必须剥掉,否则标题匹配会失败"
    assert first.uploader == "比亚迪汽车"
    assert first.duration == 275.0
    assert hits[1].url == "https://www.bilibili.com/video/BV1bbbbbbbbb"
    assert hits[1].duration == 3723.0


@pytest.mark.parametrize("raw,expected", [
    ("4:35", 275.0), ("1:02:03", 3723.0), (90, 90.0), ("", 0.0), ("abc", 0.0),
])
def test_parse_bili_duration(raw, expected):
    assert pv.parse_bili_duration(raw) == expected


def test_parse_youtube_flat_entries():
    info = {"entries": [
        {"id": "abcdefghijk", "url": "https://www.youtube.com/watch?v=abcdefghijk",
         "title": "BYD Han EV walkaround", "channel": "BYD Global",
         "duration": 180, "channel_is_verified": True},
        {"id": "zzzzzzzzzzz", "url": "zzzzzzzzzzz", "title": "x"},
    ]}
    hits = pv.parse_yt_entries(info)
    assert hits[0].verified and hits[0].uploader == "BYD Global"
    assert hits[1].url == "https://www.youtube.com/watch?v=zzzzzzzzzzz"


ALIASES = ["比亚迪", "BYD", "byd"]


@pytest.mark.parametrize("uploader,verified,expected", [
    ("比亚迪汽车", False, True),          # 品牌名 + "汽车"
    ("BYD Global", True, True),          # 认证频道
    ("BYD Official", False, True),
    ("比亚迪车友会", False, False),        # 有品牌名但不是官方
    ("汽车之家", False, False),            # 有"汽车"但不是这个品牌
    ("特斯拉官方", True, False),            # 认证了,但是别家的官方号
    ("", True, False),
])
def test_is_official(uploader, verified, expected):
    hit = pv.PlatformHit(platform="bilibili", url="u", uploader=uploader, verified=verified)
    # "汽车"是汽车品类的官方号特征(profile 的 vocab.official_markers)
    assert pv.is_official(hit, ALIASES, markers=("汽车",)) is expected


@pytest.mark.parametrize("title,ok", [
    ("比亚迪汉EV 静态体验", True),
    ("比亚迪汉 EV 内饰", True),
    ("特斯拉 Model 3 试驾", False),
])
def test_title_relevance_chinese_subject(title, ok):
    assert pv.title_relevant(title, "比亚迪汉 EV") is ok


def test_title_relevance_latin_subject():
    assert pv.title_relevant("2024 BYD Han EV review", "BYD Han")
    assert not pv.title_relevant("Tesla Model 3 review", "BYD Han")


def test_rank_puts_official_first_and_talky_last():
    hits = [
        pv.PlatformHit(platform="bilibili", url="a", title="比亚迪汉 直播回放", duration=600),
        pv.PlatformHit(platform="bilibili", url="b", title="比亚迪汉 实拍", duration=200),
        pv.PlatformHit(platform="bilibili", url="c", title="比亚迪汉 宣传片", duration=90,
                       official=True),
        pv.PlatformHit(platform="bilibili", url="d", title="比亚迪汉 3 秒", duration=3),
        pv.PlatformHit(platform="bilibili", url="e", title="比亚迪汉 超长", duration=5000),
        pv.PlatformHit(platform="bilibili", url="f", title="别的车", duration=200),
        pv.PlatformHit(platform="douyin", url="g"),  # 没标题:判断不了,保留但靠后
    ]
    ranked = pv.rank(hits, subject="比亚迪汉", require_subject=True)
    assert [h.url for h in ranked] == ["c", "b", "a", "g"]


def test_segment_starts_avoid_intro_and_outro():
    starts = pv.segment_starts(200.0, 6.0, 3)
    assert len(starts) == 3
    assert starts[0] >= 200 * 0.15 - 0.01
    assert starts[-1] + 6 <= 200 * 0.70 + 6.01
    assert starts == sorted(set(starts))


def test_segment_starts_short_and_unknown():
    assert pv.segment_starts(0, 6.0) == [5.0]
    assert pv.segment_starts(5.0, 6.0) == [0.0]
    assert all(s + 6 <= 10 for s in pv.segment_starts(10.0, 6.0, 3))


def test_bilibili_api_failure_falls_back_to_ytdlp(monkeypatch):
    import httpx

    def _boom(*a, **k):
        raise httpx.ConnectError("blocked")

    monkeypatch.setattr(httpx, "get", _boom)
    monkeypatch.setattr(pv, "_ytdlp_flat_urls",
                        lambda q: ["https://www.bilibili.com/video/BV1ccccccccc"])
    hits = pv.search_bilibili("比亚迪汉")
    assert [h.url for h in hits] == ["https://www.bilibili.com/video/BV1ccccccccc"]


def test_search_failure_raises_so_it_is_not_cached(monkeypatch):
    """搜索挂了必须抛出:返回空列表会被磁盘缓存 24 小时,
    代理修好之后也一直"搜不到"。"""
    import httpx

    def _boom(*a, **k):
        raise RuntimeError("no proxy")

    monkeypatch.setattr(pv, "_ytdlp_extract", _boom)
    monkeypatch.setattr(httpx, "get", _boom)
    # 提示只在没配代理时出现;本机 .env 配了代理也要能跑
    monkeypatch.setattr(pv.config, "YTDLP_PROXY", "")
    with pytest.raises(pv.PlatformError, match="YTDLP_PROXY"):
        pv.search_youtube("BYD Han")
    with pytest.raises(pv.PlatformError):
        pv.search_bilibili("比亚迪汉")
    monkeypatch.setattr(pv.webassets, "bing_web", _boom)
    with pytest.raises(pv.PlatformError):
        pv.search_douyin("比亚迪汉")


def test_failed_search_is_not_cached(monkeypatch, tmp_path):
    from app import config

    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    state = {"up": False}

    def _search(q, limit=8):
        if not state["up"]:
            raise pv.PlatformError("down")
        return [pv.PlatformHit(platform="youtube", url="https://y/1")]

    monkeypatch.setattr(pv, "_SEARCHERS", {"youtube": _search})
    errors: list[str] = []
    assert pv.search("BYD", platforms=["youtube"], errors=errors) == []
    state["up"] = True
    assert [h.url for h in pv.search("BYD", platforms=["youtube"])] == ["https://y/1"]


def test_douyin_uses_bing_site_search(monkeypatch):
    monkeypatch.setattr(pv.webassets, "bing_web", lambda q, **k: [
        "https://www.douyin.com/video/7300000000000000000?x=1",
        "https://www.douyin.com/user/abc",
        "https://www.ixigua.com/7300000000000000001",
    ])
    hits = pv.search_douyin("比亚迪汉")
    assert [h.url for h in hits] == [
        "https://www.douyin.com/video/7300000000000000000",
        "https://www.ixigua.com/7300000000000000001",
    ]


def test_search_respects_platform_order_and_isolates_failures(monkeypatch):
    calls: list[str] = []

    def _ok(name):
        def _fn(q, limit=8):
            calls.append(name)
            return [pv.PlatformHit(platform=name, url=f"https://{name}/1")]
        return _fn

    def _boom(q, limit=8):
        raise RuntimeError("down")

    monkeypatch.setattr(pv, "_SEARCHERS", {"bilibili": _ok("bilibili"), "youtube": _boom,
                                           "douyin": _ok("douyin")})
    monkeypatch.setattr(pv, "_cached_hits", lambda p, q, l, fetch: fetch())
    errors: list[str] = []
    hits = pv.search("比亚迪汉", platforms=["douyin", "youtube", "bilibili"], errors=errors)
    assert [h.platform for h in hits] == ["douyin", "bilibili"]
    assert any("youtube" in e for e in errors)


def test_ffmpeg_dir_is_put_on_path(monkeypatch, tmp_path):
    """yt-dlp 判断能否分段下载只查 PATH,不读 ffmpeg_location。"""
    monkeypatch.setenv("PATH", "C:\Windows")
    monkeypatch.setattr(pv.config, "FFMPEG_PATH", tmp_path / "ffmpeg.exe")
    pv._base_opts()
    pv._base_opts()
    parts = pv.os.environ["PATH"].split(pv.os.pathsep)
    assert parts.count(str(tmp_path)) == 1


def test_cookie_folder_is_merged_into_one_file(monkeypatch, tmp_path):
    """B 站、YouTube 各导出一份放进同一个文件夹,两边都要生效。"""
    folder = tmp_path / "cookies"
    folder.mkdir()
    (folder / "bilibili.txt").write_text(
        "# Netscape HTTP Cookie File\n.bilibili.com\tTRUE\t/\tFALSE\t0\tSESSDATA\tb\n",
        encoding="utf-8")
    (folder / "youtube.txt").write_text(
        "# Netscape HTTP Cookie File\n#HttpOnly_.youtube.com\tTRUE\t/\tTRUE\t0\tSID\ty\n",
        encoding="utf-8")
    monkeypatch.setattr(pv.config, "YTDLP_COOKIES", str(folder))
    monkeypatch.setattr(pv.config, "DATA_DIR", tmp_path / "data")
    path = pv._base_opts()["cookiefile"]
    text = open(path, encoding="utf-8").read()
    assert text.count("# Netscape HTTP Cookie File") == 1
    assert "SESSDATA" in text and "#HttpOnly_.youtube.com" in text
    assert not path.startswith(str(folder)), "yt-dlp 会写回 cookiefile,不能指向原件"


def test_empty_cookie_folder_means_no_cookies(monkeypatch, tmp_path):
    monkeypatch.setattr(pv.config, "YTDLP_COOKIES", str(tmp_path))
    assert "cookiefile" not in pv._base_opts()


def test_js_runtime_enabled_when_node_is_installed(monkeypatch):
    """YouTube 不解 JS 题就报 "The page needs to be reloaded";yt-dlp 默认只认 deno。"""
    monkeypatch.setattr(pv.shutil, "which", lambda name: "C:/node.exe" if name == "node" else None)
    assert pv._base_opts()["js_runtimes"] == {"node": {}}
    monkeypatch.setattr(pv.shutil, "which", lambda name: None)
    assert "js_runtimes" not in pv._base_opts()


@pytest.mark.parametrize("title,ok", [
    ("Xiaomi YU7 | Debut", True),
    ("All-new Xiaomi YU7 GT - official video", True),
    ("小米yu7宣传片", True),
    ("Xiaomi SU7 Ultra review", False),
    ("XYU70 something", False),
    ("EV review", False),
])
def test_title_relevance_model_code_in_chinese_subject(title, ok):
    """回归:中文主体只认整串,英文标题(YouTube)被全部过滤掉。"""
    assert pv.title_relevant(title, "小米YU7") is ok
