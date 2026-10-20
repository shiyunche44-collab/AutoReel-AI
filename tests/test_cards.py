"""数据卡片:结构清洗、事实核对、绘制、分镜接入。不需要网络与额度。"""
from __future__ import annotations

import pytest

from app.categories import load_profile
from app.models import Fact, ScriptSegment
from app.pipeline import cards, storyboard


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


class _FakeLLM:
    def __init__(self, payload):
        self.payload = payload
        self.user = ""

    def structured(self, system, user, **_):
        self.user = user
        return self.payload


def test_storyboard_attaches_grounded_card_to_card_sections():
    profile = load_profile("auto")
    plan = list(profile.plan_for(60, "overview"))
    card_idx = next(i for i, s in enumerate(plan) if profile.section(s).card)
    segments = [ScriptSegment(idx=i, section=s, text=f"旁白 {i}") for i, s in enumerate(plan)]
    shots = [{"idx": i + 1, "visual": "画面", "on_screen_text": []} for i in range(len(plan))]
    shots[card_idx]["card"] = {"title": "核心参数", "rows": [
        {"label": "续航", "values": ["702 公里"], "fact_id": "f_1"},
        {"label": "加速", "values": ["3.5 秒"], "fact_id": "f_2"},
    ]}
    llm = _FakeLLM({"shots": shots})
    log: list[dict] = []
    specs = storyboard.build(subject="比亚迪汉 EV", profile=profile, segments=segments,
                             facts=_facts(), duration=60, llm=llm, card_log=log)

    assert specs[card_idx].card["rows"][0]["values"] == ["702 公里"]
    assert len(specs[card_idx].card["rows"]) == 1
    assert [s.idx for s in specs if s.card] == [card_idx]
    assert log and log[0]["rendered"] and log[0]["dropped"]
    # 卡片方向要看得到 medium 事实,低置信度永远不给
    assert "f_2" in llm.user and "f_3" not in llm.user


def test_profile_rejects_unknown_card_kind():
    import pytest

    from app.categories import ProfileError, _build, _validate

    raw = {"name": "x", "label": "x", "sections": {"甲": {"card": "pie"}},
           "section_plan": {}}
    with pytest.raises(ProfileError):
        _validate(_build("x", raw))


def test_storyboard_picks_from_pool():
    """分镜看得到素材池:点名的素材要真在清单里、全片只用一次,卡片镜头不取材;
    镜头 angle 跟着素材视角走。"""
    profile = load_profile("auto")
    plan = list(profile.plan_for(60, "overview"))
    card_idx = next(i for i, s in enumerate(plan) if profile.section(s).card)
    segments = [ScriptSegment(idx=i, section=s, text=f"旁白 {i}") for i, s in enumerate(plan)]
    shots = [{"idx": i + 1, "visual": "画面", "on_screen_text": [], "pool": ""}
             for i in range(len(plan))]
    others = [i for i in range(len(plan)) if i != card_idx]
    shots[others[0]]["pool"] = "v1"
    shots[others[1]]["pool"] = "v1"          # 重复点名
    shots[others[2]]["pool"] = "ghost"       # 清单里没有
    shots[card_idx]["pool"] = "v2"           # 卡片镜头(卡片成立就不取材)
    shots[card_idx]["card"] = {"title": "参数", "rows": [
        {"label": "续航", "values": ["702 公里"], "fact_id": "f_1"}]}
    pool = [{"id": "v1", "kind": "video", "view": "seat", "angle": "interior",
             "desc": "橙色座椅", "official": True, "dirt": 0},
            {"id": "v2", "kind": "video", "view": "rear", "angle": "rear",
             "desc": "车尾", "official": False, "dirt": 2}]
    llm = _FakeLLM({"shots": shots})
    specs = storyboard.build(subject="小米YU7", profile=profile, segments=segments,
                             facts=_facts(), duration=60, llm=llm, pool=pool)

    assert specs[others[0]].pool_id == "v1" and specs[others[0]].angle == "interior"
    assert specs[others[0]].visual_zh == "橙色座椅"     # 以素材描述为准
    assert specs[others[1]].visual_zh == "画面"
    assert specs[others[1]].pool_id == "" and specs[others[2]].pool_id == ""
    assert specs[card_idx].pool_id == ""
    assert "v1 [seat / 官方 / 视频] 橙色座椅" in llm.user
    assert "有字幕或主持人" in llm.user


def test_storyboard_without_pool_keeps_prompt_unchanged():
    profile = load_profile("auto")
    plan = list(profile.plan_for(60, "overview"))
    segments = [ScriptSegment(idx=i, section=s, text="x") for i, s in enumerate(plan)]
    llm = _FakeLLM({"shots": []})
    specs = storyboard.build(subject="小米YU7", profile=profile, segments=segments,
                             facts=[], duration=60, llm=llm)
    assert "可用实拍素材" not in llm.user
    assert all(s.pool_id == "" for s in specs)

