"""剪辑 v2 时间线:切点跟着分句走、每刀按半句取材、钩子快切、转场语义。"""
from __future__ import annotations

import pytest

from app.models import ShotSpec
from app.pipeline.edit import edl


def _spec(idx, dur, layout="broll", chapter="开场", overlay=None, text=""):
    return ShotSpec(idx=idx, duration=dur, visual_zh="x", layout=layout, chapter=chapter,
                    overlay=overlay, narration_zh=text)


def _cue(i, start, end, text, shot):
    return {"index": i, "start": start, "end": end, "text": text, "shot_idx": shot}


def _picker(log):
    def pick(spec, seconds, text, role):
        log.append((spec.idx, round(seconds, 2), text, role))
        return [{"kind": "video", "path": f"/m/{spec.idx}_{len(log)}.mp4", "start": 1.0,
                 "length": 10.0, "item_id": f"i{len(log)}"}]
    return pick


def test_plan_cuts_prefers_clause_starts_and_bounds():
    spans = edl.plan_cuts(0.0, 6.0, [0.5, 1.6, 2.4, 4.1], min_shot=1.2, max_shot=3.0)
    assert spans[0] == (0.0, 1.6)
    assert all(1.2 - 1e-6 <= b - a <= 3.0 + 1e-6 for a, b in spans)
    assert spans[-1][1] == 6.0
    # 首尾相接,没有空洞
    assert all(x[1] == y[0] for x, y in zip(spans, spans[1:], strict=False))


def test_plan_cuts_splits_long_span_evenly_and_merges_tail():
    spans = edl.plan_cuts(0.0, 7.0, [], min_shot=1.2, max_shot=3.0)
    assert len(spans) == 3 and all(b - a == pytest.approx(7 / 3, abs=0.01) for a, b in spans)
    # 最后一刀太短时并进前一刀
    spans = edl.plan_cuts(0.0, 3.0, [1.5, 2.9], min_shot=1.2, max_shot=3.0)
    assert spans == [(0.0, 1.5), (1.5, 3.0)]


def test_clause_starts_use_char_times_and_skip_decimal_points():
    cue = {"start": 10.0, "end": 14.0, "text": "续航3.5小时,够用了", "shot_idx": 0,
           "char_t": [0.0, 0.2, 0.4, 0.5, 0.6, 0.8, 1.0, 1.2, 2.0, 2.3, 2.6]}
    assert edl.clause_starts(cue) == [10.0, 12.0]


def test_spoken_between_returns_the_half_sentence():
    cues = [_cue(0, 0.0, 4.0, "前脸很凶,尾灯贯穿", 0)]
    assert edl.spoken_between(cues, 0.0, 2.0) == "前脸很凶"
    assert edl.spoken_between(cues, 2.2, 4.0) == "尾灯贯穿"


def test_each_cut_picks_footage_for_its_own_clause():
    specs = [_spec(0, 3.0, layout="hook", overlay={"lines": ["2秒破百?"]}),
             _spec(1, 6.0, chapter="外观")]
    cues = [_cue(0, 0.0, 3.0, "它两秒破百", 0),
            _cue(1, 3.0, 9.0, "前脸是封闭式格栅,侧面线条很低趴,尾灯是贯穿式", 1)]
    log: list = []
    tl = edl.build_timeline(specs, cues, size=(1080, 1920), fps=30, pick=_picker(log),
                            beat_files={}, title="标题")
    texts = [x[2] for x in log if x[0] == 1]
    assert len(texts) >= 2
    assert texts[0] == "前脸是封闭式格栅"  # 第一刀落在分句开口上
    assert any("尾灯" in t for t in texts[1:])
    assert not any("," in t for t in texts)  # 取材文本不带标点(跨分句的刀不被再切开)
    # 钩子段每刀不超过 hook_shot,且首刀白闪
    hook = [c for c in tl.clips if c.start < edl.HOOK_SECONDS]
    assert len(hook) >= 2 and all(c.dur <= edl.Pacing().hook_shot + 1e-6 for c in hook)
    assert tl.clips[0].transition == "flash"
    assert any(o.type == "hook" and o.props["lines"] == ["2秒破百?"] for o in tl.overlays)
    # 换章 whip + 章节标牌
    first_body = next(c for c in tl.clips if c.beat == 1)
    assert first_body.transition == "whip"
    assert any(o.type == "chapter" and o.props["name"] == "外观" for o in tl.overlays)
    # 视频轨首尾相接、铺满全片
    assert tl.clips[-1].start + tl.clips[-1].dur == pytest.approx(tl.duration, abs=0.01)
    for x, y in zip(tl.clips, tl.clips[1:], strict=False):
        assert x.start + x.dur == pytest.approx(y.start, abs=0.01)


def test_prerendered_beats_are_whole_and_zoomed_and_overlays_carried():
    specs = [_spec(0, 4.0), _spec(1, 5.0, layout="concept", overlay={"scene": "battery"}),
             _spec(2, 4.0, layout="stat", overlay={"kind": "stat", "label": "续航",
                                                    "value": "620km", "at": 1.0}),
             _spec(3, 4.0, layout="panel", overlay={"kind": "panel", "title": "三电",
                                                     "rows": [{"label": "电池", "values": ["68.8kWh"]}]})]
    log: list = []
    tl = edl.build_timeline(specs, [], size=(1080, 1920), fps=30, pick=_picker(log),
                            beat_files={1: "/b/1.mp4"})
    concept = [c for c in tl.clips if c.beat == 1]
    assert len(concept) == 1 and concept[0].kind == "beat" and concept[0].transition == "zoom"
    after = next(c for c in tl.clips if c.beat == 2)
    assert after.transition == "zoom"
    stat = next(o for o in tl.overlays if o.type == "stat")
    assert stat.start == pytest.approx(9.0) and stat.props["value"] == "620km" and stat.props["at"] == 1.0
    panel = next(o for o in tl.overlays if o.type == "panel")
    assert panel.props["rows"][0] == {"label": "电池", "value": "68.8kWh", "basis": "", "at": None}


def test_missing_footage_falls_back_to_beat_slice():
    specs = [_spec(0, 5.0)]
    tl = edl.build_timeline(specs, [], size=(1080, 1920), fps=30, pick=lambda *a: [],
                            beat_files={0: "/b/0.mp4"})
    assert tl.clips and all(c.src == "/b/0.mp4" for c in tl.clips)
    assert tl.clips[-1].src_in == pytest.approx(tl.clips[-1].start)


def test_outro_appended(monkeypatch):
    tl = edl.build_timeline([_spec(0, 2.0)], [], size=(1080, 1920), fps=30,
                            pick=_picker([]), beat_files={}, outro=("/o.mp4", 2.5))
    assert tl.duration == pytest.approx(4.5)
    assert tl.clips[-1].src == "/o.mp4" and tl.clips[-1].kind == "beat"


def test_enabled_flag(monkeypatch):
    monkeypatch.setenv("EDIT_V2", "1")
    assert edl.enabled(True) and not edl.enabled(False)
    monkeypatch.setenv("EDIT_V2", "all")
    assert edl.enabled(False)
    monkeypatch.delenv("EDIT_V2")
    assert not edl.enabled(True)


def test_hook_tail_shorter_than_min_shot_is_absorbed():
    tl = edl.build_timeline([_spec(0, 3.3, layout="hook")], [], size=(1080, 1920), fps=30,
                            pick=_picker([]), beat_files={})
    assert min(c.dur for c in tl.clips) >= 0.6
    assert tl.clips[-1].start + tl.clips[-1].dur == pytest.approx(3.3)


def test_card_beats_become_remotion_cards_with_footage():
    card = {"kind": "bars", "title": "续航", "columns": [], "source": "数据来源:官方 2",
            "rows": [{"label": "标准版", "values": ["620km"], "tag": "CLTC | 官方", "at": 0.5},
                     {"label": "长续航", "values": ["1,200km"], "tag": "CLTC | 官方"}]}
    specs = [_spec(0, 4.0), _spec(1, 4.0, layout="card", chapter="续航"),
             _spec(2, 4.0, layout="card", chapter="续航")]
    specs[1].card = card
    specs[2].card = {"kind": "weird", "rows": [{"label": "x", "values": ["1"]}]}
    tl = edl.build_timeline(specs, [], size=(1080, 1920), fps=30, pick=_picker([]),
                            beat_files={1: "/b/1.mp4", 2: "/b/2.mp4"})
    assert all(c.kind == "video" for c in tl.clips if c.beat == 1)  # 素材带照常取材
    ov = next(o for o in tl.overlays if o.type == "card")
    assert ov.start == pytest.approx(4.0) and ov.props["kind"] == "bars"
    assert [r["num"] for r in ov.props["rows"]] == [620.0, 1200.0]
    assert ov.props["rows"][0]["at"] == 0.5 and ov.props["rows"][1]["at"] is None
    # 不认识的卡片种类仍用逐拍成品
    assert [c.kind for c in tl.clips if c.beat == 2] == ["beat"]


def test_punch_times_skip_hook_busy_and_respect_gap():
    specs = [_spec(0, 4.0), _spec(1, 6.0, layout="stat", chapter="b",
                                  overlay={"kind": "stat", "label": "x", "value": "9秒"}),
             _spec(2, 8.0, chapter="c")]

    def cue(t):
        return {"start": t, "end": t + 1, "words": [{"text": "x", "hot": False, "start": t},
                                                     {"text": "620km", "hot": True, "start": t + 0.2}]}

    caps = [cue(1.0), cue(5.0), cue(10.5), cue(11.5), cue(14.0)]
    tl = edl.build_timeline(specs, [], size=(1080, 1920), fps=30, pick=_picker([]),
                            beat_files={}, captions=caps)
    # 1.2 在钩子段、5.2 在大数字期间、11.7 离 10.7 太近
    assert tl.punches == [10.7, 14.2]
    back = edl.Timeline.from_props(tl.to_props())
    assert back.punches == tl.punches


def test_short_tail_piece_is_merged_into_previous():
    def pick(spec, seconds, text, role):
        return [{"kind": "video", "path": "/a.mp4", "start": 0, "length": seconds - 0.1},
                {"kind": "video", "path": "/b.mp4", "start": 0, "length": 5}]

    tl = edl.build_timeline([_spec(0, 6.0, chapter="x")], [], size=(1080, 1920), fps=30,
                            pick=pick, beat_files={})
    body = [c for c in tl.clips if c.start >= edl.HOOK_SECONDS]
    assert all(c.dur >= edl.MIN_PIECE for c in tl.clips)
    assert all(c.src == "/a.mp4" for c in body)


# --------------------------------------------------------------------------
# commercial 包装
# --------------------------------------------------------------------------
def _commercial(specs, cues, log=None, size=(1920, 1080), **kw):
    return edl.build_timeline(specs, cues, size=size, fps=30, pick=_picker([] if log is None else log),
                              beat_files={}, title="标题", style="commercial", **kw)


def test_commercial_keeps_cuts_and_picks_but_swaps_packaging():
    specs = [_spec(0, 3.0, layout="hook", overlay={"lines": ["2秒破百?"]}),
             _spec(1, 6.0, chapter="外观"),
             _spec(2, 5.0, layout="highlight", chapter="外观", overlay={"lines": ["15分钟补620公里", "快"]})]
    cues = [_cue(0, 0.0, 3.0, "它两秒破百", 0),
            _cue(1, 3.0, 9.0, "前脸是封闭式格栅,侧面线条很低趴,尾灯是贯穿式", 1),
            _cue(2, 9.0, 14.0, "十五分钟补六百二十公里", 2)]
    classic_log: list = []
    commercial_log: list = []
    classic = edl.build_timeline(specs, cues, size=(1920, 1080), fps=30, pick=_picker(classic_log),
                                 beat_files={}, title="标题")
    tl = _commercial(specs, cues, commercial_log)
    # 下刀和取材与 classic 一样:包装风格不动剪辑
    assert [(c.start, c.dur, c.key) for c in tl.clips] == [(c.start, c.dur, c.key) for c in classic.clips]
    assert commercial_log == classic_log
    types = {o.type for o in tl.overlays}
    assert "chapter_card" in types and "headline" in types and "hook" in types
    assert not types & {"keyword", "chapter", "panel"}
    card = next(o for o in tl.overlays if o.type == "chapter_card")
    assert card.props == {"n": 1, "name": "外观", "total": 1} and card.dur == edl.CHAPTER_CARD_SECONDS
    head = next(o for o in tl.overlays if o.type == "headline")
    assert head.props["lines"] == ["15分钟补620公里", "快"] and head.start == pytest.approx(9.1)
    assert edl.HEADLINE_MIN <= head.dur <= edl.HEADLINE_MAX
    # 不做冲击推;视频缓推、不再按刀序号交替推拉
    assert tl.punches == []
    assert {c.motion for c in tl.clips} == {edl.MOTION_VIDEO_COMMERCIAL}
    assert tl.clips[0].transition == "flash"
    assert next(c for c in tl.clips if c.beat == 1).transition == "whip"


def test_commercial_still_pushes_slowly():
    def pick(spec, seconds, text, role):
        return [{"kind": "still", "path": "/m/a.jpg", "item_id": "s"}]

    tl = edl.build_timeline([_spec(0, 4.0)], [_cue(0, 0.0, 4.0, "看看这台车", 0)], size=(1920, 1080),
                            fps=30, pick=pick, beat_files={}, style="commercial")
    assert {c.motion for c in tl.clips} == {edl.MOTION_STILL_COMMERCIAL}


def test_commercial_panel_becomes_chart_or_headline():
    rows3 = [{"label": "续航", "values": ["835km"], "at": 0.5}, {"label": "功率", "values": ["5.2C"]},
             {"label": "补能", "values": ["620km"], "basis": "厂商口径"}]
    rows1 = [{"label": "车身", "values": ["中大型"]}, {"label": "轴距", "values": ["3000mm"], "at": 1.0}]
    specs = [_spec(0, 4.0, chapter="开场"),
             _spec(1, 5.0, layout="panel", chapter="开场", overlay={"title": "补能", "rows": rows3}),
             _spec(2, 5.0, layout="panel", chapter="开场", overlay={"title": "尺寸", "rows": rows1})]
    tl = _commercial(specs, [])
    chart = next(o for o in tl.overlays if o.type == "card")
    assert chart.props["kind"] == "spec" and chart.start == 4.0 and chart.dur == 5.0
    assert [r["values"] for r in chart.props["rows"]] == [["835km"], ["5.2C"], ["620km"]]
    assert chart.props["rows"][0]["at"] == 0.5 and chart.props["rows"][2]["tag"] == "厂商口径"
    assert chart.props["rows"][0]["num"] == 835
    # 两行的面板:挑第一个带数字的行上大字,念到它时上屏,标签做眉题
    head = next(o for o in tl.overlays if o.type == "headline")
    assert head.props == {"lines": ["3000mm"], "kicker": "轴距"}
    assert head.start == pytest.approx(9.0 + 1.0 - 0.15)


def test_commercial_stat_is_kept_and_headlines_are_spaced():
    hl = {"lines": ["一句判断"]}
    specs = [_spec(0, 6.0, chapter="开场"),                                    # 0:钩子大标题
             _spec(1, 3.0, layout="highlight", chapter="开场", overlay=hl),   # 6.1:前后都够远
             _spec(2, 3.5, layout="highlight", chapter="开场", overlay=hl),   # 9.1:离上一条 3 秒,不要
             _spec(3, 4.0, layout="stat", chapter="开场", overlay={"label": "零百", "value": "2.78秒"}),
             _spec(4, 3.0, layout="highlight", chapter="开场", overlay=hl),   # 16.6:离大数字 4 秒,不要
             _spec(5, 4.0, layout="highlight", chapter="开场", overlay=hl)]   # 19.6:够远
    tl = _commercial(specs, [])
    assert [o.start for o in tl.overlays if o.type == "hook"] == [0.0]
    heads = [o.start for o in tl.overlays if o.type == "headline"]
    assert heads == [pytest.approx(6.1), pytest.approx(19.6)]
    assert [o.start for o in tl.overlays if o.type == "stat"] == [12.5]
    # 大数字是这一拍的内容:离大字不到 6 秒也留着,让的是大字(前后都算)
    specs[2] = _spec(2, 3.0, layout="highlight", chapter="开场", overlay=hl)
    tl = _commercial(specs, [])
    assert [o.start for o in tl.overlays if o.type == "stat"] == [12.0]
    assert [o.start for o in tl.overlays if o.type == "headline"] == [pytest.approx(19.1)]


def test_space_headlines_waits_for_chapter_card_and_skips_charts():
    O = edl.Overlay
    out = edl.space_headlines([
        O(10.0, 1.0, "chapter_card", {}), O(10.1, 3.0, "headline", {"lines": ["a"]}),
        O(20.0, 5.0, "card", {}), O(21.0, 2.0, "headline", {"lines": ["b"]}),
    ])
    heads = [o for o in out if o.type == "headline"]
    assert len(heads) == 1 and heads[0].start == pytest.approx(11.05)
    assert heads[0].start + heads[0].dur == pytest.approx(13.1)


def test_space_headlines_drops_chapter_card_under_stat_or_chart():
    O = edl.Overlay
    out = edl.space_headlines([
        O(28.3, 1.0, "chapter_card", {"n": 2}), O(28.3, 4.0, "stat", {}),
        O(60.0, 1.0, "chapter_card", {"n": 3}), O(60.0, 6.0, "card", {}),
        O(90.0, 1.0, "chapter_card", {"n": 4}), O(91.2, 3.0, "stat", {}),
    ])
    assert [o.props["n"] for o in out if o.type == "chapter_card"] == [4]
    assert [o.type for o in out].count("stat") == 2 and [o.type for o in out].count("card") == 1


def test_classic_style_unchanged_by_default():
    specs = [_spec(0, 4.0, chapter="开场"), _spec(1, 4.0, layout="highlight", chapter="外观",
                                                   overlay={"lines": ["关键词"]})]
    tl = edl.build_timeline(specs, [], size=(1920, 1080), fps=30, pick=_picker([]), beat_files={})
    assert {o.type for o in tl.overlays} == {"chapter", "keyword"}


# --------------------------------------------------------------------------
# 节奏(Pacing)
# --------------------------------------------------------------------------
def test_rhythm_cuts_follow_the_cycle_and_snap_to_clause_starts():
    p = edl.Pacing(rhythm=(2.0, 1.0))
    spans, phase = edl.rhythm_cuts(0.0, 10.0, [2.3, 5.0], p, 0)
    # 2.0 附近 ±0.7 有开口 2.3 就落在开口上;1.0 附近 ±0.35 没有开口就在句中切
    assert spans == [(0.0, 2.3), (2.3, 3.3), (3.3, 5.0), (5.0, 6.0), (6.0, 8.0), (8.0, 10.0)]
    assert phase == 6
    # 节奏全片接着数:从第 1 个开始,第一刀是 1.0
    spans, phase = edl.rhythm_cuts(0.0, 3.0, [], p, 1)
    assert spans == [(0.0, 1.0), (1.0, 3.0)] and phase == 3


def test_rhythm_cuts_never_leave_a_short_tail():
    p = edl.Pacing(rhythm=(2.0,), tail=1.2)
    # 2.0 之后只剩 0.9 秒:整段给一刀,不切碎刀
    assert edl.rhythm_cuts(0.0, 2.9, [], p, 0)[0] == [(0.0, 2.9)]
    # 拍尾 tail 秒以内的开口不当切点
    assert edl.rhythm_cuts(0.0, 5.0, [2.1, 4.2], p, 0)[0] == [(0.0, 2.1), (2.1, 5.0)]


def test_rhythm_cuts_hold_longer_on_chapter_open_and_calm_beats():
    p = edl.Pacing(rhythm=(1.0, 0.8), calm=2.0)
    spans, _ = edl.rhythm_cuts(0.0, 10.0, [], p, 0, first_min=2.5)
    assert spans[0] == (0.0, 2.5) and spans[1] == (2.5, 3.3)
    spans, _ = edl.rhythm_cuts(0.0, 10.0, [], p, 0, calm=True)
    assert all(b - a >= 2.0 - 1e-6 for a, b in spans)


def test_pacing_scaled_keeps_floor_and_still_cap():
    p = edl.Pacing()
    assert p.scaled(1.0) is p
    fast = p.scaled(0.5)
    assert min(fast.rhythm) == p.floor  # 0.7 × 0.5 不低于下限
    assert fast.hook_shot == pytest.approx(p.hook_shot / 2)
    assert fast.chapter_open == pytest.approx(p.chapter_open / 2)
    assert (fast.still_max, fast.tail, fast.snap) == (p.still_max, p.tail, p.snap)
    assert p.scaled(0.01) == p.scaled(0.3)  # 夹到 0.3


def test_pace_knob_scales_by_square_root(monkeypatch):
    from app.pipeline.edit import project as edit_project

    monkeypatch.setattr(edit_project.knobs, "piece_seconds", lambda: 3.0)  # fast
    fast = edit_project.pacing()
    assert fast.hook_shot == pytest.approx(2.5 * (3.0 / 4.5) ** 0.5, abs=1e-3)
    monkeypatch.setattr(edit_project.knobs, "piece_seconds", lambda: 4.5)  # mid
    assert edit_project.pacing() == edl.Pacing()


def test_pick_text_drops_clause_breaks():
    assert edl.pick_text("前脸很凶,尾灯贯穿。") == "前脸很凶 尾灯贯穿"
    assert edl.pick_text("续航3.5小时") == "续航3 5小时"  # 取材只看词,小数点无所谓


def test_long_still_is_split_and_each_part_gets_its_own_pick():
    log: list = []

    def pick(spec, seconds, text, role):
        log.append((spec.idx, round(seconds, 2), role))
        return [{"kind": "still", "path": f"/m/{len(log)}.jpg", "item_id": f"s{len(log)}"}]

    p = edl.Pacing(rhythm=(6.0,), hook_shot=3.0)
    tl = edl.build_timeline([_spec(0, 9.0, chapter="x")], [], size=(1920, 1080), fps=30,
                            pick=pick, beat_files={}, pacing=p)
    assert [(c.start, c.dur) for c in tl.clips] == [(0.0, 1.5), (1.5, 1.5), (3.0, 2.0),
                                                    (5.0, 2.0), (7.0, 2.0)]
    assert all(c.kind == "still" and c.dur <= p.still_max for c in tl.clips)
    # 每段静图各取一次,取材键按调用次序排,回放时一一对上
    assert [c.key for c in tl.clips] == [f"0:{n}" for n in range(5)]
    assert len({c.src for c in tl.clips}) == 5
    # 每次取材要的是剩下的整段(素材够长就是一刀),拿到静图才切开
    assert [x[1] for x in log] == [3.0, 1.5, 6.0, 4.0, 2.0]


def _view_specs():
    specs = [_spec(0, 3.0, layout="hook"), _spec(1, 2.0, chapter="外观", text="前脸很凶"),
             _spec(2, 2.0, chapter="外观", text="大灯很亮"), _spec(3, 2.0, chapter="外观", text="侧面")]
    specs[1].view, specs[2].view, specs[3].view = "front", "front", "side"
    cues = [_cue(0, 0.0, 3.0, "开场", 0), _cue(1, 3.0, 5.0, "前脸很凶", 1),
            _cue(2, 5.0, 7.0, "大灯很亮", 2), _cue(3, 7.0, 9.0, "侧面", 3)]
    return specs, cues


def test_same_chapter_same_view_beats_share_long_shots():
    specs, cues = _view_specs()
    log: list = []
    tl = edl.build_timeline(specs, cues, size=(1920, 1080), fps=30, pick=_picker(log),
                            beat_files={}, pacing=edl.Pacing(rhythm=(3.0,)))
    # 1、2 两拍同章同视角:一刀 4 秒跨过拍界,归刀头那一拍;3 拍换了视角,另起
    long = next(c for c in tl.clips if c.start == 3.0)
    assert (long.dur, long.beat, long.key) == (4.0, 1, "1:0")
    assert long.text == "前脸很凶大灯很亮"
    assert [x[0] for x in log if x[0] in (1, 2, 3)] == [1, 3]
    assert next(c for c in tl.clips if c.start == 7.0).key == "3:0"


def test_cross_beat_shot_without_footage_falls_back_beat_by_beat():
    specs, cues = _view_specs()
    files = {s.idx: f"/b/{s.idx}.mp4" for s in specs}
    tl = edl.build_timeline(specs, cues, size=(1920, 1080), fps=30, pick=lambda *a: [],
                            beat_files=files, pacing=edl.Pacing(rhythm=(3.0,)))
    run = [c for c in tl.clips if 3.0 <= c.start < 7.0]
    assert [(c.start, c.dur, c.src, c.src_in, c.beat) for c in run] == [
        (3.0, 2.0, "/b/1.mp4", 0.0, 1), (5.0, 2.0, "/b/2.mp4", 0.0, 2)]
    assert {c.key for c in run} == {"1:0"} and {c.via for c in run} == {"fallback"}
