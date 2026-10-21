"""实时热点抓取:解析、汽车词过滤、单路失败隔离、缓存。不走网络。"""
from __future__ import annotations

import time

import pytest

from app.providers import platform_video, trends
from app.providers.platform_video import PlatformHit


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    from app import config

    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(trends.time, "sleep", lambda *_: None)


DCD = {"data": {
    "sells_rank_month": [{"text": "2026年09月", "month": 202609}],
    "list": [
        {"series_id": 1, "series_name": "Model Y", "brand_name": "特斯拉", "count": 50359,
         "rank": 1, "last_rank": 3},
        {"series_id": 2, "series_name": "星愿", "brand_name": "吉利银河", "count": 44275,
         "rank": 2, "last_rank": -1},
        {"series_name": ""},
    ],
}}

DOUYIN = {"status_code": 0, "word_list": [
    {"word": "台风正在靠近", "hot_value": 11000000},
    {"word": "问界发布车辆事故说明", "hot_value": 7823000},
    {"word": "星愿第 100 万台下线", "hot_value": 500000},
]}


class _Resp:
    def __init__(self, payload, status=200):
        self._p, self.status_code = payload, status

    def json(self):
        return self._p


def test_parse_dongchedi_sales():
    items = trends.parse_dongchedi(DCD, "sales")
    assert [i.car for i in items] == ["Model Y", "星愿"]
    y = items[0]
    assert y.title == "销量榜第 1 名:特斯拉 Model Y,上期第 3"
    assert y.metric == "月销 5.0 万" and y.date == "2026年09月" and y.kind == "model"
    assert y.url.endswith("/series/1")
    assert "上期" not in items[1].title            # last_rank=-1 是"新上榜",不写名次


def test_parse_douyin_filters_auto_words():
    got = trends.parse_douyin(DOUYIN)
    assert [i.title for i in got] == ["问界发布车辆事故说明"]
    assert got[0].metric == "热搜第 2 · 热度 782.3 万"
    # 榜单车系名补进词表后才认得出"星愿"
    assert len(trends.parse_douyin(DOUYIN, ("星愿",))) == 2


def test_dongchedi_retries_once_then_caches(monkeypatch):
    calls = []

    def fake_get(url, params=None, **_):
        calls.append(params["rank_data_type"])
        return _Resp({"data": {"list": []}} if len(calls) == 1 else DCD)

    monkeypatch.setattr("httpx.get", fake_get)
    assert len(trends.dongchedi_rank("sales")) == 2
    assert calls == [11, 11]
    trends.dongchedi_rank("sales")
    assert len(calls) == 2                          # 命中磁盘缓存


def test_empty_result_is_not_cached(monkeypatch):
    monkeypatch.setattr("httpx.get", lambda *a, **k: _Resp({"data": {"list": []}}))
    with pytest.raises(trends.TrendError):
        trends.dongchedi_rank("hot")
    monkeypatch.setattr("httpx.get", lambda *a, **k: _Resp(DCD))
    assert trends.dongchedi_rank("hot")[0].title.startswith("热度榜第 1 名")


def test_auto_news_dedupes_and_survives_one_failure(monkeypatch):
    n = []

    def fake_call(tool, args, **_):
        n.append(args["query"])
        if len(n) == 2:
            raise RuntimeError("超时")
        return {"data": [{"title": "银河战舰700上市", "link": "https://a", "date": "2026/10/10"},
                         {"title": "银河战舰700上市", "link": "https://b"}]}

    monkeypatch.setattr(trends.cli, "call", fake_call)
    items = trends.auto_news()
    assert [i.title for i in items] == ["银河战舰700上市"]
    assert len(n) == len(trends.NEWS_QUERIES)


def test_bilibili_hot_sorts_by_play(monkeypatch):
    now = int(time.time())
    seen = {}

    def fake_search(q, *, limit=10, order="", since_days=0):
        seen.update(order=order, since_days=since_days)
        return [
            PlatformHit(platform="bilibili", url=f"https://b/{q}1", title=f"{q} 小",
                        extra={"play": 900, "pubdate": now}),
            PlatformHit(platform="bilibili", url=f"https://b/{q}2", title=f"{q} 大",
                        extra={"play": 350000, "pubdate": now}),
            PlatformHit(platform="bilibili", url=f"https://b/{q}3", title="老视频",
                        extra={"play": 9_000_000, "pubdate": now - 30 * 86400}),
        ]

    monkeypatch.setattr(platform_video, "search_bilibili", fake_search)
    items = trends.bilibili_hot(days=7)
    assert seen == {"order": "click", "since_days": 7}
    assert "老视频" not in [i.title for i in items]
    assert items[0].metric.startswith("播放 35.0 万")


def test_collect_isolates_failures(monkeypatch):
    def boom(*a, **k):
        raise trends.TrendError("接口改版")

    monkeypatch.setattr(trends, "dongchedi_rank",
                        lambda kind="sales", count=20: trends.parse_dongchedi(DCD, kind))
    monkeypatch.setattr(trends, "auto_news", boom)
    got_words = {}

    def fake_douyin(extra=()):
        got_words["extra"] = extra
        return trends.parse_douyin(DOUYIN, extra)

    monkeypatch.setattr(trends, "douyin_hot", fake_douyin)
    monkeypatch.setattr(trends, "bilibili_hot", lambda: [])
    items, errors = trends.collect()
    assert errors == {"news": "接口改版"}
    assert {"星愿", "Model Y"} <= set(got_words["extra"])
    sources = [i.source for i in items]
    assert sources.count("dongchedi.sales") == 2 and sources.count("douyin") == 2

    items, errors = trends.collect(["douyin"])
    assert {i.source for i in items} == {"douyin"}


def test_auto_profile_matches_module_constants():
    from app.categories import load_profile

    spec = load_profile("auto_long").trends
    assert spec.rank == "dongchedi"
    assert spec.news_queries == trends.NEWS_QUERIES and spec.bili_queries == trends.BILI_QUERIES
    assert spec.hot_words == trends.AUTO_WORDS and spec.brands == trends.AUTO_BRANDS


def test_collect_baby_spec_skips_dongchedi(monkeypatch):
    from app.categories import load_profile

    profile = load_profile("baby_long")
    called = {}

    def no_dcd(*a, **k):
        raise AssertionError("母婴不该调懂车帝")

    def fake_news(queries=()):
        called["news"] = queries
        return [trends.TrendItem(source="news", kind="event", title="新品发布"),
                trends.TrendItem(source="news", kind="rank", title="双11 母婴榜")]

    def fake_douyin(extra=(), words=None):
        called["words"] = words
        return trends.parse_douyin({"word_list": [
            {"word": "台风正在靠近", "hot_value": 1}, {"word": "宝宝推车怎么选", "hot_value": 2},
            {"word": "好孩子回应召回", "hot_value": 3}, {"word": "问界发布会", "hot_value": 4}]},
            extra, words)

    def fake_bili(queries=()):
        called["bili"] = queries
        return [trends.TrendItem(source="bilibili", kind="video", title="零重力座椅 行驶中禁用"),
                trends.TrendItem(source="bilibili", kind="video", title="婴儿推车 16 款横评")]

    monkeypatch.setattr(trends, "dongchedi_rank", no_dcd)
    monkeypatch.setattr(trends, "auto_news", fake_news)
    monkeypatch.setattr(trends, "douyin_hot", fake_douyin)
    monkeypatch.setattr(trends, "bilibili_hot", fake_bili)
    items, errors = trends.collect(spec=profile.trends, brands=profile.trend_brands)
    assert errors == {}
    assert called["news"] == profile.trends.news_queries
    assert called["bili"] == profile.trends.bili_queries
    assert "好孩子" in called["words"]                    # official_sites 别名并进来了
    assert [i.title for i in items] == ["双11 母婴榜", "新品发布", "宝宝推车怎么选", "好孩子回应召回",
                                        "婴儿推车 16 款横评"]


def test_news_rank_kind_and_query_cache_key(monkeypatch):
    seen = []

    def fake_call(tool, args, **_):
        seen.append(args["query"])
        return {"data": [{"title": "天猫母婴销量榜出炉", "link": "https://a"}]}

    monkeypatch.setattr(trends.cli, "call", fake_call)
    items = trends.auto_news(queries=("母婴 榜单",))
    assert items[0].kind == "rank" and seen[0].startswith("母婴 榜单")
    trends.auto_news()                                     # 汽车口径不吃母婴的缓存
    assert len(seen) == 1 + len(trends.NEWS_QUERIES)
