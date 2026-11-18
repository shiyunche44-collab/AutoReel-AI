import wave

from app.pipeline import looks, render, sfx
from app.pipeline.explainer import ffmpeg as ef


def _cue(start, hot_at=None):
    words = [{"text": "零百", "hot": False, "start": start}]
    if hot_at is not None:
        words.append({"text": "3.23秒", "hot": True, "start": hot_at})
    return {"start": start, "end": start + 2, "words": words}


def test_legacy_events_whip_per_chapter_no_pop():
    # 旧接口:关键词 pop 已删,章节起点 → whip,上屏时刻 → 大字,间隔至少 MIN_GAP
    cues = [_cue(1.0, 1.2), _cue(2.0, 2.2), _cue(5.0, 5.3)]
    ev = sfx.events_for(cues, [0.0, 4.0, 20.0], reveal_times=[6.0, 12.0])
    assert ev == [(4.0, "whip"), (12.0, "headline"), (20.0, "whip")]  # 第 0 章在片头不放;6.0 离换章太近


def test_build_track_length_and_empty(tmp_path):
    assert sfx.build_track([], 5.0, tmp_path / "a.wav") is None
    out = sfx.build_track([(1.0, "pop"), (2.0, "whoosh")], 5.0, tmp_path / "a.wav")
    with wave.open(str(out)) as w:
        assert w.getframerate() == sfx.SR and w.getnframes() == 5 * sfx.SR
    assert sfx.build_track([(1.0, "pop")], 5.0, tmp_path / "b.wav") is None  # pop 不再出声


def test_audio_graph_sfx_is_extra_amix_input():
    plain, _ = render._build_audio_graph(True, True, 0.0, first_input=0)
    with_sfx, _ = render._build_audio_graph(True, True, 0.0, first_input=0, has_sfx=True)
    assert "[2:a]" in with_sfx and "amix=inputs=3" in with_sfx
    assert "sfx" not in plain and "amix=inputs=2" in plain
    narr_only, _ = render._build_audio_graph(True, False, 0.0, first_input=0, has_sfx=True)
    assert "[1:a]" in narr_only and "amix=inputs=2" in narr_only


def test_push_filter_kinds():
    assert "on/" in ef.push_filter("zoom_in", 4.0, (1920, 1080))
    assert "1-(0.5*on/" in ef.push_filter("zoom_out", 4.0, (1920, 1080))
    assert "pow(" in ef.push_filter("punch_in", 4.0, (1920, 1080))


def test_only_lite_pushes_and_has_sfx():
    assert looks.get("lite").cam_push and looks.get("lite").sfx
    # commercial 是剪辑 v2 的包装:音效由时间线定(换章 / 大字),不推镜
    assert not looks.get("commercial").cam_push and looks.get("commercial").sfx
    assert not any(lk.cam_push or lk.sfx for lk in looks.LOOKS.values()
                   if lk.id not in ("lite", "commercial"))
