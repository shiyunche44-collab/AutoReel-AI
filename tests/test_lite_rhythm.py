import wave

from app.pipeline import looks, render, sfx
from app.pipeline.explainer import ffmpeg as ef


def _cue(start, hot_at=None):
    words = [{"text": "零百", "hot": False, "start": start}]
    if hot_at is not None:
        words.append({"text": "3.23秒", "hot": True, "start": hot_at})
    return {"start": start, "end": start + 2, "words": words}


def test_events_whoosh_per_chapter_and_pop_gap():
    cues = [_cue(1.0, 1.2), _cue(2.0, 2.2), _cue(5.0, 5.3)]
    ev = sfx.events_for(cues, [0.0, 4.0])
    kinds = [k for _, k in ev]
    assert kinds.count("whoosh") == 1  # 第 0 章在片头,不放
    assert [t for t, k in ev if k == "pop"] == [1.2, 5.3]  # 2.2 离上一个不到 POP_MIN_GAP
    assert ev == sorted(ev)
    assert next(t for t, k in ev if k == "whoosh") < 4.0  # 提前一点


def test_build_track_length_and_empty(tmp_path, monkeypatch):
    monkeypatch.setattr(sfx, "SFX_DIR", tmp_path / "lib")
    assert sfx.build_track([], 5.0, tmp_path / "a.wav") is None
    out = sfx.build_track([(1.0, "pop"), (2.0, "whoosh")], 5.0, tmp_path / "a.wav")
    with wave.open(str(out)) as w:
        assert w.getframerate() == sfx.SR and w.getnframes() == 5 * sfx.SR
    assert (tmp_path / "lib" / "pop.wav").is_file()


def test_audio_graph_sfx_is_extra_amix_input():
    plain, _ = render._build_audio_graph(True, True, 0.0, first_input=0)
    with_sfx, _ = render._build_audio_graph(True, True, 0.0, first_input=0, has_sfx=True)
    assert "[2:a]" in with_sfx and "amix=inputs=3" in with_sfx
    assert "sfx" not in plain and "amix=inputs=2" in plain
    narr_only, _ = render._build_audio_graph(True, False, 0.0, first_input=0, has_sfx=True)
    assert "[1:a]" in narr_only and "amix=inputs=2" in narr_only


def test_push_filter_kinds():
    assert "on/" in ef.push_filter("zoom_in", 4.0, (1920, 1080))
    assert "1-on/" in ef.push_filter("zoom_out", 4.0, (1920, 1080))
    assert "pow(" in ef.push_filter("punch_in", 4.0, (1920, 1080))


def test_only_lite_pushes_and_has_sfx():
    assert looks.get("lite").cam_push and looks.get("lite").sfx
    assert not any(lk.cam_push or lk.sfx for lk in looks.LOOKS.values() if lk.id != "lite")
