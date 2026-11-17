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
    spans = edl.plan_cuts(0.0, 6.0, [0.5, 1.6, 2.4, 4.1])
    assert spans[0] == (0.0, 1.6)
    assert all(edl.MIN_SHOT - 1e-6 <= b - a <= edl.MAX_SHOT + 1e-6 for a, b in spans)
    assert spans[-1][1] == 6.0
    # 首尾相接,没有空洞
    assert all(x[1] == y[0] for x, y in zip(spans, spans[1:], strict=False))


def test_plan_cuts_splits_long_span_evenly_and_merges_tail():
    spans = edl.plan_cuts(0.0, 7.0, [])
    assert len(spans) == 3 and all(b - a == pytest.approx(7 / 3, abs=0.01) for a, b in spans)
    # 最后一刀太短时并进前一刀
    spans = edl.plan_cuts(0.0, 3.0, [1.5, 2.9])
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
    assert texts[0].startswith("前脸")
    assert any("尾灯" in t for t in texts[1:])
    # 钩子段每刀不超过 1 秒,且首刀白闪
    hook = [c for c in tl.clips if c.start < edl.HOOK_SECONDS]
    assert all(c.dur <= edl.HOOK_SHOT + 1e-6 for c in hook)
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
