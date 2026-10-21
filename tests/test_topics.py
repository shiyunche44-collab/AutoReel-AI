"""热点选题:LLM 输出的后处理(编造车型、重复、方向校验、url 白名单)。不需要网络。"""
from __future__ import annotations

from app.categories import load_profile
from app.pipeline import topics

TRENDS = [
    {"source": "dongchedi.sales", "kind": "model", "title": "销量榜第 1", "car": "小米YU7",
     "metric": "月销 32000", "url": "https://dcd/1", "date": "2026-10"},
    {"source": "news", "kind": "event", "title": "比亚迪秦L 官降 1 万", "car": "",
     "metric": "", "url": "https://news/2", "date": "2026-10-18"},
]


class _LLM:
    def __init__(self, topics_out):
        self.out = topics_out
        self.calls = []

    def structured(self, system, user, required_keys=(), **_):
        self.calls.append((system, user))
        return {"topics": self.out}


def _t(subject, angle, **kw):
    return {"subject": subject, "angle": angle, "theme": "buyguide", "hook": "h",
            "why_hot": "w", "sources": [], "heat": 3, **kw}


def test_filters_invented_and_duplicates():
    profile = load_profile("auto_long")
    llm = _LLM([
        _t("小米 YU7", "月销破 3 万背后", heat=4, sources=["https://dcd/1", "https://fake"]),
        _t("小米YU7", "月销破 3 万背后"),            # 同角度重复
        _t("小米YU7", "智驾到底几斤几两"),
        _t("小米YU7", "第三个角度"),                 # 同车超过 2 次
        _t("理想i8", "热点里根本没有"),              # 编造车型
        _t("比亚迪秦L", "官降后值不值", theme="nope", heat=9),
        _t("", "空车型"),
        "garbage",
    ])
    out = topics.suggest(TRENDS, profile=profile, llm=llm)
    assert [(t.subject, t.angle) for t in out] == [
        ("比亚迪秦L", "官降后值不值"), ("小米 YU7", "月销破 3 万背后"), ("小米YU7", "智驾到底几斤几两"),
    ]
    qin = out[0]
    assert qin.theme == profile.default_theme and qin.heat == 5
    assert out[1].sources == ["https://dcd/1"]


def test_clean_subject_collapses_brand_overlap():
    assert topics.clean_subject("吉利银河银河战舰700") == "吉利银河战舰700"
    assert topics.clean_subject(" 特斯拉Model Y ") == "特斯拉Model Y"
    assert topics.clean_subject("极氪001") == "极氪001"


def test_prompt_contains_trends_and_themes():
    profile = load_profile("auto_long")
    llm = _LLM([])
    topics.suggest(TRENDS, profile=profile, llm=llm, n=6)
    system, user = llm.calls[0]
    assert "6 个" in system and "buyguide" in system and "车型" in system
    assert "[dongchedi.sales] | 销量榜第 1 | 车系:小米YU7 | 指标:月销 32000" in user
    assert "url:https://news/2" in user


def test_no_trends_or_llm_returns_empty():
    profile = load_profile("auto_long")
    assert topics.suggest([], profile=profile, llm=_LLM([_t("x", "y")])) == []
    assert topics.suggest(TRENDS, profile=profile, llm=None) == []


def test_accepts_objects_with_to_dict():
    class Item:
        def __init__(self, d):
            self.d = d

        def to_dict(self):
            return self.d

    assert "小米YU7" in topics.format_trends([Item(TRENDS[0])])


def test_api_topics(monkeypatch):
    import sys
    import types

    from fastapi.testclient import TestClient

    from app import main
    from app.pipeline import orchestrator

    fake = types.ModuleType("app.providers.trends")
    calls = []

    def collect(*, spec=None, brands=()):
        calls.append(spec.rank)
        return TRENDS, {"douyin": "接口变了"}

    fake.collect = collect
    monkeypatch.setitem(sys.modules, "app.providers.trends", fake)
    import app.providers as providers_pkg
    monkeypatch.setattr(providers_pkg, "trends", fake, raising=False)
    monkeypatch.setattr(orchestrator, "_llm", lambda: _LLM([_t("小米YU7", "月销破 3 万背后")]))
    monkeypatch.setattr(main, "_TOPICS_CACHE", {})

    client = TestClient(main.app)
    body = client.get("/api/topics?category=auto_long").json()
    assert body["topics"][0]["subject"] == "小米YU7"
    assert body["trends"] == TRENDS
    assert body["errors"] == {"douyin": "接口变了"}
    client.get("/api/topics?category=auto_long")
    assert len(calls) == 1                      # 命中缓存
    client.get("/api/topics?category=auto_long&refresh=1")
    assert len(calls) == 2


def test_compliance_and_evidence():
    profile = load_profile("auto_long")
    llm = _LLM([
        _t("小米YU7", "月销重回第一,还值得买吗"),                       # 角度带禁用词 -> 丢
        _t("小米YU7", "月销3万背后的版本选择", hook="这是今年最强的一台中型SUV之一吧",
           search_terms="  小米YU7   销量  9月 ", sources=["https://dcd/1"]),
    ])
    out = topics.suggest(TRENDS, profile=profile, llm=llm)
    assert [t.angle for t in out] == ["月销3万背后的版本选择"]
    t = out[0]
    assert t.hook == ""                                   # 超长又带「最强」
    assert t.search_terms == "小米YU7 销量 9月"
    assert t.evidence == ["[dongchedi.sales] 销量榜第 1 | 车系:小米YU7 | 指标:月销 32000 | 日期:2026-10 | 链接:https://dcd/1"]
    system = llm.calls[0][0]
    assert "「第一」" in system and "search_terms" in system


def test_evidence_includes_cited_news():
    lines = topics.evidence_for("比亚迪秦L", ["https://news/2"], TRENDS)
    assert lines == ["[news] 比亚迪秦L 官降 1 万 | 日期:2026-10-18 | 链接:https://news/2"]


# --------------------------------------------------------------------------
# 多车横评选题
# --------------------------------------------------------------------------
TRENDS3 = TRENDS + [
    {"source": "dongchedi.sales", "kind": "model", "title": "销量榜第 2", "car": "特斯拉Model Y",
     "metric": "月销 30000", "url": "https://dcd/3", "date": "2026-10"},
    {"source": "dongchedi.sales", "kind": "model", "title": "销量榜第 3", "car": "问界M7",
     "metric": "月销 20000", "url": "https://dcd/4", "date": "2026-10"},
]


def test_roundup_topics_keep_only_trending_cars():
    profile = load_profile("auto_long")
    llm = _LLM([
        _t("25 万级纯电 SUV 横评", "月销前三的 SUV 各适合谁", theme="roundup",
           subjects=["小米YU7", "特斯拉 Model Y", "问界M7", "理想i8"]),   # 理想i8 不在热点里
        _t("两车对比", "只有两台", theme="roundup", subjects=["小米YU7", "问界M7"]),  # 不够 3 台
        _t("横评但没车单", "没车", theme="roundup"),
        _t("小米YU7", "单车选题照旧", subjects=["问界M7"]),  # 单车方向忽略 subjects
    ])
    out = topics.suggest(TRENDS3, profile=profile, llm=llm)
    by_angle = {t.angle: t for t in out}
    assert set(by_angle) == {"月销前三的 SUV 各适合谁", "单车选题照旧"}
    r = by_angle["月销前三的 SUV 各适合谁"]
    assert r.theme == "roundup" and r.subject == "25 万级纯电 SUV 横评"
    assert r.subjects == ["小米YU7", "特斯拉 Model Y", "问界M7"]
    assert len(r.evidence) <= 12 and any("问界M7" in e for e in r.evidence)
    assert by_angle["单车选题照旧"].subjects == []


def test_roundup_rule_in_prompt_only_when_profile_has_roundup():
    llm = _LLM([])
    topics.suggest(TRENDS, profile=load_profile("auto_long"), llm=llm, n=6)
    assert "theme=roundup" in llm.calls[0][0] and "3-5 款车" in llm.calls[0][0]
    llm = _LLM([])
    topics.suggest(TRENDS, profile=load_profile("baby_long"), llm=llm, n=6)
    assert "多车横评" not in llm.calls[0][0]


# --------------------------------------------------------------------------
# 母婴:没有榜单,按品牌认热点;婴儿奶粉选题拦掉
# --------------------------------------------------------------------------
BABY_TRENDS = [
    {"source": "news", "kind": "rank", "title": "双11 母婴品类榜:Babycare 婴儿推车销量居前",
     "car": "", "metric": "", "url": "https://news/b1", "date": "2026-10-20"},
    {"source": "douyin", "kind": "event", "title": "好孩子推车召回", "car": "",
     "metric": "热搜第 9", "url": "https://dy/1", "date": "2026-10-21"},
    {"source": "news", "kind": "event", "title": "某品牌婴儿奶粉新品", "car": "",
     "metric": "", "url": "https://news/b2", "date": "2026-10-19"},
]


def test_baby_topics_brand_match_and_exclude():
    profile = load_profile("baby_long")
    llm = _LLM([
        _t("Babycare 婴儿推车 V5", "双11 榜单推车值不值", sources=["https://news/b1"]),
        _t("好孩子 口袋车 GB-POCKIT", "召回之后还能买吗"),
        _t("飞鹤 星飞帆 奶粉", "奶粉新品怎么选"),            # 禁区品类
        _t("Babycare 奶瓶", "配方奶怎么冲"),                  # 角度带禁区词
        _t("小鹿蓝蓝 推车", "热点里根本没有"),                # 品牌不在热点里
    ])
    out = topics.suggest(BABY_TRENDS, profile=profile, llm=llm)
    assert [t.subject for t in out] == ["Babycare 婴儿推车 V5", "好孩子 口袋车 GB-POCKIT"]
    # 型号不在热点里,依据按品牌兜底
    assert out[1].evidence and "好孩子推车召回" in out[1].evidence[0]
    system = llm.calls[0][0]
    assert "品牌+型号" in system and "懂车帝" not in system and "车系" not in system
    assert "母婴" in system


def test_auto_prompt_rules_come_from_profile():
    llm = _LLM([])
    topics.suggest(TRENDS, profile=load_profile("auto_long"), llm=llm)
    system = llm.calls[0][0]
    assert "(销量/热度榜、近期新闻、抖音热搜、B站高播放视频)" in system
    assert ("写成「品牌+车系」(照抄榜单条目里的品牌和车系名,\n     如榜单写「吉利银河 星愿」"
            "就写「吉利银河星愿」),\n     不许写多款对比") in system
    assert "5. 优先「销量/热度榜靠前 + 有新闻或热搜事件」的交叉热点,其次纯榜单车型。" in system


def test_auto_keeps_strict_subject_match():
    # 汽车有榜单:不按品牌放宽,热点里没有的车型照样丢
    llm = _LLM([_t("比亚迪汉L", "编出来的车型")])
    assert topics.suggest(TRENDS, profile=load_profile("auto_long"), llm=llm) == []
