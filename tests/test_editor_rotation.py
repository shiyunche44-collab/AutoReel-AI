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
