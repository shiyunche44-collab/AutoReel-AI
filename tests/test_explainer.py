"""横屏解说轨:profile 继承、版式核对、节拍定时长、背景连拼、叠加层。不需要网络与额度。"""
from __future__ import annotations

import pytest
from PIL import Image

from app import config
from app.categories import load_profile
from app.models import Fact, ScriptSegment, ShotSpec, SubtitleCue
from app.pipeline import explainer, library, overlays


def _facts() -> list[Fact]:
    return [
        Fact(id="f_1", claim="小米YU7 标准版 CLTC 续航 835 公里", source_tool="t",
             confidence="high", scope="product"),
        Fact(id="f_2", claim="小米YU7 标准版售价 25.35 万元", source_tool="t",
             confidence="high", scope="product"),
        Fact(id="f_3", claim="全系标配激光雷达与英伟达 Thor 芯片", source_tool="t",
             confidence="medium", scope="product"),
    ]


class FakeLLM:
    def __init__(self, beats):
        self.beats = beats
        self.calls = 0

    def structured(self, system, user, **_):
        self.calls += 1
        return {"beats": self.beats}


# --------------------------------------------------------------------------
# profile
# --------------------------------------------------------------------------
def test_auto_long_inherits_auto_and_is_landscape():
    p = load_profile("auto_long")
    base = load_profile("auto")
    assert p.is_explainer and not base.is_explainer
    assert p.frame_size == (1920, 1080)
    assert base.frame_size == (1080, 1920)
    # 继承:官方渠道、合规词表来自 auto,不必复制
    assert set(base.official_sites) <= set(p.official_sites)
    assert p.forbidden_terms == base.forbidden_terms
    # themes 不继承:auto 的方向是按 6 秒栅格写的
    assert set(p.themes) == {"buyguide"}
    for d in p.supported_durations():
        assert len(p.plan_for(d)) == p.shots_for(d)


def test_every_explainer_section_declares_layouts():
    p = load_profile("auto_long")
    for name in p.themes["buyguide"].blocks:
        sec = p.section(name["section"])
        assert sec.layouts, sec.name
        assert set(sec.layouts) <= set(explainer.LAYOUTS)


# --------------------------------------------------------------------------
# 版式核对
# --------------------------------------------------------------------------
def test_keywords_reject_invented_numbers_and_model_names():
    facts = _facts()
    text = "全系标配激光雷达,智驾芯片是英伟达 Thor"
    assert explainer.ground_keywords(["激光雷达", "英伟达Thor芯片"], text, facts) == \
        ["激光雷达", "英伟达Thor芯片"]
    # 8650 / Orin 两样都不在旁白和事实里
    assert explainer.ground_keywords(["高通8650", "Orin-X 芯片"], text, facts) == []
    # 数字在事实里有就放行
    assert explainer.ground_keywords(["续航835公里"], "续航很长", facts) == ["续航835公里"]


def _segments(profile) -> list[ScriptSegment]:
    plan = profile.plan_for(120)
    return [ScriptSegment(idx=i, section=s, text=f"第{i}段旁白,讲讲这台车。") for i, s in enumerate(plan)]


def test_build_beats_grounds_panel_and_downgrades_bad_data():
    p = load_profile("auto_long")
    segs = _segments(p)
    i_price = next(i for i, s in enumerate(segs) if s.section == "定位与价格")
    llm = FakeLLM([
        {"idx": i_price + 1, "layout": "panel", "view": "exterior", "panel": {
            "title": "标准版", "rows": [
                {"label": "售价", "values": ["25.35万元"], "fact_id": "f_2"},
                {"label": "续航", "values": ["835公里"], "fact_id": "f_1"},
            ]}},
        # 开场选 highlight 但关键词是编的 → 退回 broll
        {"idx": 1, "layout": "highlight", "keywords": ["零百2.9秒"]},
        # 不在可选版式里 → 用段落默认版式
        {"idx": len(segs), "layout": "nonsense"},
    ])
    log: list[dict] = []
    specs = explainer.build_beats(subject="小米YU7", profile=p, segments=segs,
                                  facts=_facts(), llm=llm, card_log=log)
    assert len(specs) == len(segs)
    panel = specs[i_price]
    assert panel.layout == "panel" and panel.overlay["kind"] == "panel"
    assert [r["values"][0] for r in panel.overlay["rows"]] == ["25.35万元", "835公里"]
    assert specs[0].layout == "broll"
    assert specs[-1].layout == p.section(segs[-1].section).layouts[0]
    assert not explainer.validate_beats(specs)
    assert any(e["idx"] == 0 for e in log)
    assert all(s.chapter for s in specs)


def test_panel_with_unsupported_number_falls_back():
    p = load_profile("auto_long")
    segs = _segments(p)
    i_price = next(i for i, s in enumerate(segs) if s.section == "定位与价格")
    llm = FakeLLM([{"idx": i_price + 1, "layout": "panel", "panel": {"title": "x", "rows": [
        {"label": "售价", "values": ["23.99万元"], "fact_id": "f_2"},
        {"label": "续航", "values": ["835公里"], "fact_id": "f_1"},
    ]}}])
    specs = explainer.build_beats(subject="小米YU7", profile=p, segments=segs,
                                  facts=_facts(), llm=llm)
    # 核掉一行只剩 1 行,不够撑一块面板
    assert specs[i_price].layout == "broll" and specs[i_price].overlay is None


def test_llm_failure_still_yields_valid_beats():
    class Broken:
        def structured(self, *a, **k):
            raise RuntimeError("down")

    p = load_profile("auto_long")
    specs = explainer.build_beats(subject="小米YU7", profile=p, segments=_segments(p),
                                  facts=_facts(), llm=Broken())
    assert not explainer.validate_beats(specs)


def test_enforce_rhythm_breaks_long_card_runs():
    card = {"kind": "spec", "title": "t", "columns": [], "rows": [], "source": ""}
    specs = [ShotSpec(idx=i, visual_zh="[card] x", narration_zh="a", layout="card", card=card)
             for i in range(5)]
    out = explainer.enforce_rhythm(specs)
    layouts = [s.layout for s in out]
    assert layouts == ["card", "card", "broll", "card", "card"]
    assert out[2].card is None


# --------------------------------------------------------------------------
# 时长与章节
# --------------------------------------------------------------------------
def test_beat_durations_match_narration_track():
    cues = [SubtitleCue(index=1, start=0.0, end=2.0, text="a", shot_idx=0),
            SubtitleCue(index=2, start=2.3, end=5.3, text="b", shot_idx=0),
            SubtitleCue(index=3, start=5.6, end=7.6, text="c", shot_idx=1)]
    d = explainer.beat_durations(cues, 3, gap=0.3)
    assert d == [5.6, 2.3, 0.0]
    # 旁白轨 = Σ(句长 + 停顿)
    assert sum(d) == pytest.approx(7.6 + 0.3)


def test_chapters_group_adjacent_beats():
    specs = [ShotSpec(idx=i, visual_zh="x", chapter=c, duration=d)
             for i, (c, d) in enumerate([("开头", 5), ("外观", 6), ("外观", 7), ("结论", 4)])]
    chapters, where = explainer.chapters_of(specs)
    assert chapters == [("开头", 5), ("外观", 13), ("结论", 4)]
    assert where == [0, 1, 1, 2]


def test_layout_filters_reference_expected_inputs():
    f = explainer.layout_filter("panel", (1920, 1080), True)
    assert "[2:v]" in f and "[1:v]" in f and f.endswith("[out]")
    f = explainer.layout_filter("highlight", (1920, 1080), True)
    assert "pad=" in f and "fade=in" in f
    # 叠加层缺失时退化为只叠章节条
    assert explainer.layout_filter("panel", (1920, 1080), False) == \
        "[0:v][1:v]overlay=0:0,format=yuv420p[out]"


# --------------------------------------------------------------------------
# 背景连拼
# --------------------------------------------------------------------------
@pytest.fixture()
def pool(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    lib = library.Library(subject="测试车")
    vid = tmp_path / "a.mp4"
    vid.write_bytes(b"x")
    still = tmp_path / "s.jpg"
    still.write_bytes(b"x")

    def item(id_, start, end, view="exterior_45", target="yes", problem="", kind="video",
             path=vid):
        return library.Item(id=id_, kind=kind, path=str(path), tagged=True, view=view,
                            target=target, problem=problem, start=start, end=end)

    lib.items = [
        item("clean1", 0, 3), item("clean2", 3, 5.5), item("int1", 6, 9, view="interior"),
        item("sub1", 10, 20, problem="subtitle"),
        item("unknown1", 20, 25, target="unknown"),
        item("tiny", 25, 25.6),
        item("still1", 0, 0, kind="still", path=still),
    ]
    lib.save()
    return lib


def test_fill_prefers_clean_confirmed_footage_and_covers_duration(pool):
    used: dict[str, int] = {}
    pieces = library.fill("测试车", 8.0, hint="exterior", used=used)
    assert sum(p.length for p in pieces) == pytest.approx(8.0)
    ids = [p.item_id for p in pieces]
    assert ids[0] == "clean1"
    assert "tiny" not in ids and "sub1" not in ids
    assert all(p.length <= library.FILL_MAX_PIECE + library.FILL_MIN_PIECE for p in pieces)
    assert all(used[i] == 1 for i in ids)


def test_fill_hint_interior_reorders_but_does_not_filter(pool):
    pieces = library.fill("测试车", 3.0, hint="interior", used={})
    assert pieces[0].item_id == "int1"


def test_fill_rotates_through_unused_material(pool):
    used: dict[str, int] = {}
    first = [p.item_id for p in library.fill("测试车", 5.0, used=used)]
    second = [p.item_id for p in library.fill("测试车", 5.0, used=used)]
    assert not set(first) & set(second[:1])


def test_fill_on_empty_pool_returns_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    assert library.fill("没有这台车", 6.0) == []


# --------------------------------------------------------------------------
# 叠加层
# --------------------------------------------------------------------------
def test_overlays_render_transparent_canvases(tmp_path):
    size = (1920, 1080)
    p = overlays.panel({"title": "标准版", "rows": [{"label": "售价", "values": ["25.35万元"]}],
                        "source": "数据来源"}, "小米YU7", size, tmp_path / "p.png")
    h = overlays.highlight(["激光雷达", "英伟达Thor"], size, tmp_path / "h.png")
    b = overlays.chapter_bar([("开头", 3), ("定位价格", 30), ("结论", 5)], 1, size,
                             tmp_path / "b.png")
    for path in (p, h, b):
        im = Image.open(path)
        assert im.size == size and im.mode == "RGBA"
        # 画面中央必须是透明的(背景画面要透出来)
        if path is not b:
            assert im.getpixel((size[0] - 40, 40))[3] == 0
    bar = Image.open(b)
    assert bar.getpixel((10, 10))[3] == 0
    assert bar.getpixel((10, size[1] - 5))[3] > 0


def test_fill_skips_black_title_cards(pool, tmp_path):
    dark = tmp_path / "dark.png"
    Image.new("RGB", (64, 36), (5, 5, 5)).save(dark)
    lib = library.Library.load("测试车")
    for i in lib.items:
        if i.id == "clean1":
            i.frame = str(dark)
    lib.save()
    ids = [p.item_id for p in library.fill("测试车", 6.0, used={})]
    assert "clean1" not in ids and ids
