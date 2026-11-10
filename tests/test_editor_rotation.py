"""主编按星期轮换选题类型:热门车型 / 价位段选购 / 热点话题。"""
from __future__ import annotations

import datetime as dt
from types import SimpleNamespace as NS

from app.categories import load_profile
from app.team import editor

PLAN = ("model", "band", "model", "topic", "model", "band", "topic")


def _slot(day: int) -> float:
    """2026-11-02 是周一。"""
    return dt.datetime(2026, 11, 2 + day, 18, 30).timestamp()


def _idea(subject: str, theme: str, subjects=()) -> NS:
    return NS(subject=subject, theme=theme, subjects=list(subjects), heat=3)


def test_slot_kind_by_weekday():
    assert [editor.slot_kind(_slot(d), PLAN) for d in range(7)] == list(PLAN)
    assert editor.slot_kind(_slot(0), ()) == ""
    assert editor.slot_kind(_slot(0), ("model",)) == ""


def test_idea_kind():
    profile = load_profile("auto_long")
    band = _idea("10-20万新能源怎么选", "priceband", ["A", "B", "C"])
    assert editor.idea_kind(band, profile, set()) == "band"
    assert editor.idea_kind(_idea("问界M9", "review"), profile, {"问界m9"}) == "topic"
    assert editor.idea_kind(_idea("小米YU7", "debate"), profile, set()) == "topic"
    assert editor.idea_kind(_idea("小米YU7", "review"), profile, set()) == "model"


def test_assign_slots_prefers_same_kind_then_falls_back():
    a, b, c = _idea("a", "review"), _idea("b", "priceband", ["x", "y", "z"]), _idea("c", "debate")
    kinds = {id(a): "model", id(b): "band", id(c): "topic"}
    # 周一 model、周二 band、周四 topic
    out = editor.assign_slots([_slot(0), _slot(1), _slot(3)], [c, b, a], kinds)
    assert [i.subject for _, i in out] == ["a", "b", "c"]
    # 没有 band 选题时周二退而求其次拿排名最高的剩余选题
    out = editor.assign_slots([_slot(0), _slot(1)], [c, a], {id(a): "model", id(c): "topic"})
    assert [i.subject for _, i in out] == ["a", "c"]


def test_rotation_hint(monkeypatch):
    monkeypatch.setattr(editor.config, "TEAM_WEEK_PLAN", PLAN)
    text = editor.rotation_hint([_slot(0), _slot(1), _slot(3)])
    assert "热门车型 1 条" in text and "价位段选购 1 条" in text and "热点话题 1 条" in text
    assert editor.config.TEAM_BAND_HINT in text
    monkeypatch.setattr(editor.config, "TEAM_WEEK_PLAN", ())
    assert editor.rotation_hint([_slot(0)]) == ""


# --------------------------------------------------------------------------
# 选题范围:只从榜单前几名里出
# --------------------------------------------------------------------------
ROWS = ([{"source": "dongchedi.sales", "car": c, "rank": i + 1}
         for i, c in enumerate(["星愿", "元UP", "零跑A10"] + [f"车{i}" for i in range(4, 21)])]
        + [{"source": "dongchedi.hot", "car": c, "rank": i + 1}
           for i, c in enumerate(["Model Y", "星愿", "宝马3系"])]
        + [{"source": "news", "car": "宋Ultra", "rank": 1}])


def test_top_cars_takes_best_rank_within_limit():
    top = editor.top_cars(ROWS, 10)
    assert top["星愿"] == {"car": "星愿", "rank": 1, "label": "销量榜"}
    assert top["modely"]["label"] == "热度榜"
    assert "车11" not in top and "宋ultra" not in top      # 11 名以后、非榜单来源都不算
    assert editor.top_cars(ROWS, 0) == {}                    # 0 = 不限
    assert editor.top_cars([{"source": "news", "car": "星愿", "rank": 1}], 10) == {}


def test_gate_hit_matches_brand_prefixed_names():
    top = editor.top_cars(ROWS, 10)
    assert editor.gate_hit(_idea("吉利银河星愿", "review"), top)["rank"] == 1
    assert editor.gate_hit(_idea("特斯拉Model Y", "review"), top)["car"] == "Model Y"
    assert editor.gate_hit(_idea("比亚迪宋Ultra", "review"), top) is None
    # 横评:车单里有一台在圈内就算
    band = _idea("10-20万怎么选", "priceband", ["比亚迪宋Ultra", "比亚迪元UP"])
    assert editor.gate_hit(band, top)["car"] == "元UP"


def test_rank_ideas_puts_outside_gate_last_and_downweights_niche():
    profile = load_profile("auto_long")
    top = editor.top_cars(ROWS, 10)
    hot = NS(subject="比亚迪宋Ultra", theme="review", subjects=[], heat=5)
    tenth = NS(subject="车10", theme="review", subjects=[], heat=2)
    first = NS(subject="吉利银河星愿", theme="review", subjects=[], heat=2)
    got = editor.rank_ideas([hot, tenth, first], set(), set(), top=top, profile=profile)
    assert got == [first, tenth, hot]           # 圈外的热度再高也排最后;圈内名次靠前的在前
    assert editor.in_gate(first, top) and not editor.in_gate(hot, top)
    assert editor.in_gate(hot, top, {"比亚迪宋ultra"})   # 观众点名问的不受限
    assert editor.rank_ideas([hot, tenth], set(), set(), top=top,
                             exempt={"比亚迪宋ultra"})[0] is hot
    # 同一台车:单点深挖排在选购类后面
    deep = NS(subject="吉利银河星愿", theme="deepdive", subjects=[], heat=3)
    buy = NS(subject="吉利银河星愿", theme="buyguide", subjects=[], heat=3)
    assert editor.rank_ideas([deep, buy], set(), set(), top=top, profile=profile) == [buy, deep]
    assert editor.niche_weight(deep, profile) == editor.NICHE_WEIGHT
    # 没有榜单数据:照旧按热度排
    assert editor.rank_ideas([tenth, hot], set(), set(), top={}) == [hot, tenth]


def test_gate_hint_lists_cars_by_rank():
    text = editor.gate_hint(editor.top_cars(ROWS, 3))
    assert text.index("星愿(销量榜第 1)") < text.index("零跑A10(销量榜第 3)")
    assert "该不该买" in text and editor.gate_hint({}) == ""
