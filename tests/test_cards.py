"""数据卡片:结构清洗、事实核对、绘制。不需要网络与额度。"""
from __future__ import annotations

import pytest

from app.models import Fact
from app.pipeline import cards


def _facts() -> list[Fact]:
    return [
        Fact(id="f_1", claim="2026 款汉 EV 的 CLTC 续航为 702 公里", source_tool="t",
             confidence="high", scope="product"),
        Fact(id="f_2", claim="汉 EV 零百加速 3.9 秒", source_tool="t",
             confidence="medium", scope="product"),
        Fact(id="f_3", claim="2020 年款续航 605 公里", source_tool="t",
             confidence="low", scope="product"),
        Fact(id="f_4", claim="海豹 07 售价 18.98 万元起", source_tool="t",
             confidence="high", scope="product"),
    ]


def test_normalise_drops_malformed_rows():
    card = cards.normalise({"title": "核心参数", "rows": [
        {"label": "续航", "values": "702 公里", "fact_id": "f_1"},
        {"label": "", "values": ["x"]},
        "not a row",
        {"label": "加速", "values": []},
    ]}, "spec")
    assert card["kind"] == "spec"
    assert card["rows"] == [{"label": "续航", "values": ["702 公里"], "fact_id": "f_1"}]


def test_normalise_rejects_unknown_kind():
    assert cards.normalise({"rows": []}, "pie") is None
    assert cards.normalise("nope", "spec") is None


def test_ground_keeps_supported_and_drops_invented_numbers():
    card = cards.normalise({"title": "核心参数", "rows": [
        {"label": "CLTC 续航", "values": ["702 公里"], "fact_id": "f_1"},
        {"label": "零百加速", "values": ["3.8 秒"], "fact_id": "f_2"},   # 事实写的是 3.9
        {"label": "旧款续航", "values": ["605 公里"], "fact_id": "f_3"},  # 低置信度
        {"label": "电池", "values": ["85 度"], "fact_id": "f_x"},        # 不存在
    ]}, "spec")
    grounded, dropped = cards.ground(card, _facts())
    assert [r["label"] for r in grounded["rows"]] == ["CLTC 续航"]
    assert len(dropped) == 3
    assert grounded["source"].startswith("数据来源")


def test_ground_voids_card_below_minimum():
    card = cards.normalise({"title": "演进", "rows": [
        {"label": "2026", "values": ["续航 702 公里"], "fact_id": "f_1"},
    ]}, "timeline")
    grounded, dropped = cards.ground(card, _facts())
    assert grounded is None
    assert "作废" in dropped[-1]


def test_ground_compare_needs_two_columns_and_values():
    raw = {"title": "同级对比", "rows": [
        {"label": "续航", "values": ["702 公里", "—"], "fact_id": "f_1"},
    ]}
    assert cards.ground(cards.normalise(raw, "compare"), _facts())[0] is None
    raw["columns"] = ["汉 EV", "海豹 07"]
    grounded, _ = cards.ground(cards.normalise(raw, "compare"), _facts())
    assert grounded["columns"] == ["汉 EV", "海豹 07"]


def test_ground_bars_requires_numeric_values():
    raw = {"title": "对比", "rows": [
        {"label": "汉 EV", "values": ["702 公里"], "fact_id": "f_1"},
        {"label": "海豹 07", "values": ["18.98 万元"], "fact_id": "f_4"},
        {"label": "其他", "values": ["很多"], "fact_id": "f_4"},
    ]}
    grounded, dropped = cards.ground(cards.normalise(raw, "bars"), _facts())
    assert [r["label"] for r in grounded["rows"]] == ["汉 EV", "海豹 07"]
    assert any("不是数字" in d for d in dropped)


def test_render_frames_reveal_rows_one_by_one(tmp_path):
    card = {"kind": "spec", "title": "核心参数", "columns": [], "source": "数据来源:公开资料",
            "rows": [{"label": "续航", "values": ["702 公里"], "fact_id": "f_1"},
                     {"label": "加速", "values": ["3.9 秒"], "fact_id": "f_2"}]}
    frames = cards.render_frames(card, "比亚迪汉 EV", tmp_path)
    assert len(frames) == 3
    from PIL import Image
    with Image.open(frames[-1]) as im:
        assert im.size == (cards.W, cards.H)


def test_render_frames_all_kinds(tmp_path):
    rows = [{"label": "2024", "values": ["12.5 万辆"], "fact_id": "a"},
            {"label": "2025", "values": ["18 万辆"], "fact_id": "b"}]
    for kind in cards.CARD_KINDS:
        card = {"kind": kind, "title": "测试", "columns": ["甲", "乙"], "source": "",
                "rows": [{**r, "values": r["values"] * (2 if kind == "compare" else 1)}
                         for r in rows]}
        frames = cards.render_frames(card, "主体", tmp_path / kind)
        assert len(frames) == len(cards._steps(card))
        expected = {"bars": 1 + cards.GROW_STEPS, "compare": 5}.get(kind, 3)
        assert len(frames) == expected


def test_bars_grow_and_show_values_only_when_full(tmp_path):
    card = {"kind": "bars", "title": "续航", "columns": [], "source": "",
            "rows": [{"label": "A", "values": ["800 km"], "fact_id": "a"},
                     {"label": "B", "values": ["400 km"], "fact_id": "b"}]}
    steps = cards._steps(card)
    progress = [s[1] for s in steps[1:]]
    assert progress == sorted(progress) and progress[-1] == pytest.approx(1.0)
    assert progress[0] > 1 / cards.GROW_STEPS          # 缓出:第一帧就长出一大截
    frames = cards.render_frames(card, "主体", tmp_path)
    from PIL import Image, ImageChops
    with Image.open(frames[1]) as a, Image.open(frames[-1]) as b:
        assert ImageChops.difference(a.convert("RGB"), b.convert("RGB")).getbbox()


def test_compare_lights_left_column_first():
    card = {"kind": "compare", "rows": [{"label": "x", "values": ["1", "2"]}]}
    assert [(r, half) for r, _, half, _ in cards._steps(card)] == \
        [(0, False), (1, True), (1, False)]


def test_profile_rejects_unknown_card_kind():
    import pytest

    from app.categories import ProfileError, _build, _validate

    raw = {"name": "x", "label": "x", "sections": {"甲": {"card": "pie"}},
           "section_plan": {}}
    with pytest.raises(ProfileError):
        _validate(_build("x", raw))
