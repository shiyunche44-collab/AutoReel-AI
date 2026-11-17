"""剪辑 v2 语义配画面:半句 → 视角、文字模型复选、对题判定、时间线读回。"""
from __future__ import annotations

import pytest

from app import config
from app.categories import load_profile
from app.models import ShotSpec
from app.pipeline import library
from app.pipeline.edit import edl, match
from app.pipeline.edit import project as edit_project
from app.pipeline.library.select import CHOOSE_TOP


@pytest.fixture()
def vocab():
    return load_profile("auto_long").vocab


@pytest.fixture()
def pool(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    lib = library.Library(subject="测试车")
    vid = tmp_path / "a.mp4"
    vid.write_bytes(b"x")

    def item(id_, start, end, view, desc=""):
        return library.Item(id=id_, kind="video", path=str(vid), tagged=True, view=view,
                            target="yes", start=start, end=end, official=True, desc=desc)

    lib.items = [
        item("front", 0, 3, "exterior_45", "白色车前 45 度停在路边"),
        item("rear", 3, 6, "rear", "车尾贯穿式尾灯点亮"),
        item("screen", 6, 9, "screen", "中控大屏显示导航"),
        item("side", 9, 12, "exterior_side", "正侧面停车"),
    ]
    lib.save()
    return lib


def test_views_for_reads_vocab(vocab):
    assert match.views_for("尾灯是贯穿式的", vocab) == {"rear"}
    assert match.views_for("这车卖二十万", vocab) == set()


def test_fill_views_put_matching_view_first(pool):
    pieces = library.fill("测试车", 2.0, used={}, detail="尾灯是贯穿式的", views={"rear"})
    assert pieces[0].item_id == "rear"
    pieces = library.fill("测试车", 2.0, used={}, detail="中控屏很大", views={"screen"})
    assert pieces[0].item_id == "screen"


def test_fill_choose_picks_among_top_and_ignores_bad_index(pool):
    seen = []

    def choose(text, cands):
        seen.append([c.id for c in cands])
        return [c.id for c in cands].index("side")

    pieces = library.fill("测试车", 2.0, used={}, detail="随便一句话", choose=choose)
    assert pieces[0].item_id == "side" and len(seen[0]) <= CHOOSE_TOP
    first = library.fill("测试车", 2.0, used={}, detail="随便一句话")[0].item_id
    pieces = library.fill("测试车", 2.0, used={}, detail="随便一句话", choose=lambda t, c: 99)
    assert pieces[0].item_id == first

    def boom(text, cands):
        raise RuntimeError("x")

    assert library.fill("测试车", 2.0, used={}, detail="随便一句话", choose=boom)[0].item_id == first


class FakeLLM:
    def __init__(self, pick):
        self.pick, self.calls = pick, []

    def structured(self, system, user, **kw):
        self.calls.append(user)
        if isinstance(self.pick, Exception):
            raise self.pick
        return {"pick": self.pick, "why": "x"}


def test_llm_chooser_parses_caches_and_degrades():
    items = [library.Item(id="a", kind="video", path="", view="rear", desc="车尾"),
             library.Item(id="b", kind="video", path="", view="screen", desc="中控屏")]
    llm = FakeLLM(2)
    choose = match.llm_chooser(llm, "测试车")
    assert choose("中控屏有多大", items) == 1
    assert choose("中控屏有多大", items) == 1 and len(llm.calls) == 1
    assert "[screen] 中控屏" in llm.calls[0]
    assert choose("嗯", items) is None  # 太短不问
    assert match.llm_chooser(FakeLLM(7), "x")("中控屏有多大", items) is None
    assert match.llm_chooser(FakeLLM(RuntimeError()), "x")("中控屏有多大", items) is None


def test_judge(vocab):
    rear = library.Item(id="r", kind="video", path="", view="rear", desc="")
    closeup = library.Item(id="d", kind="video", path="", view="detail", desc="碳陶刹车卡钳特写")
    assert match.judge("尾灯是贯穿式的", rear, vocab) == (True, "view")
    assert match.judge("碳陶刹车", closeup, vocab) == (True, "detail")
    assert match.judge("尾灯是贯穿式的", closeup, vocab) == (True, "")
    assert match.judge("这台车卖二十万", rear, vocab) == (False, "")


def test_picker_end_to_end_marks_need_match_via(pool, vocab):
    pick = edit_project._picker("测试车", vocab, FakeLLM(1))
    spec = ShotSpec(idx=0, visual_zh="x", duration=2.0)
    got = pick(spec, 2.0, "尾灯是贯穿式的", "body")
    assert got[0]["item_id"] == "rear"
    assert got[0]["need"] is True and got[0]["match"] == "view" and got[0]["via"] == "llm"
    got = pick(spec, 2.0, "这台车卖二十万", "body")
    assert got[0]["need"] is False


def test_match_stats_and_props_roundtrip():
    tl = edl.Timeline(width=1080, height=1920, fps=30, duration=4.0)
    tl.clips = [edl.Clip(0, 2, "video", "/a.mp4", need=True, match="view", via="llm"),
                edl.Clip(2, 2, "video", "/b.mp4", need=True, via="rank")]
    tl.overlays = [edl.Overlay(0, 1, "hook", {"lines": ["x"]})]
    assert tl.match_stats() == {"need": 2, "hit": 1, "rate": 0.5, "llm": 1, "fallback": 0}
    back = edl.Timeline.from_props(tl.to_props() | {"match": {}, "notes": ["n"]})
    assert back.to_props() == tl.to_props() and back.notes == ["n"]


def test_theme_colors_readable_on_dark_cards():
    from app.pipeline import looks

    for lk in looks.LOOKS.values():
        th = edit_project.theme_of(lk)
        for key in ("accent", "accent2"):
            rgb = tuple(int(th[key][i:i + 2], 16) for i in (1, 3, 5))
            assert edit_project._luma(rgb) >= edit_project.MIN_LUMA, (lk.id, key, th[key])
        assert th["subs"] in ("box", "outline", "band")


def test_sitting_inside_means_interior(vocab):
    assert "interior" in match.views_for("坐进车里", vocab)
