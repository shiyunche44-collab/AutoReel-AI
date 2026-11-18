"""配乐编辑:drop 落在钩子结束处、小节线上收尾、片长超过曲长时在章节交界换曲、拍点吸附。"""
from __future__ import annotations

import json
import wave

import numpy as np
import pytest

from app import config
from app.pipeline import render
from app.pipeline.edit import music

BPM = 120.0
PERIOD = 60.0 / BPM


def track(name="a", length=120.0, drops=((16.0, 6.0),), mood=None, beats=True, pulse=0.8,
          first_beat=0.0, lufs=-14.0, path=None):
    beat_list = [round(first_beat + i * PERIOD, 3) for i in range(int((length - first_beat) / PERIOD))]
    t = {"path": path or f"/lib/{name}.mp3", "title": name, "mood": mood, "duration": length,
         "lufs": lufs}
    if beats:
        t.update(bpm=BPM, pulse=pulse, beats=beat_list, downbeats=beat_list[::4],
                 drops=[d for d, _ in drops], drop_db=[s for _, s in drops])
    return t


def covered(seg, tr_len):
    """段里实际用到的曲内区间 [offset, offset + 时长] 不超过曲长(不循环)。"""
    return seg["offset"] + (seg["end"] - seg["start"]) <= tr_len + 1e-6


def test_drop_lands_on_hook_end():
    p = music.plan(60.0, 3.0, [], [track()])
    seg = p["segments"][0]
    assert p["mode"] == "beats" and len(p["segments"]) == 1
    assert seg["start"] == 0.0 and seg["offset"] == pytest.approx(13.0)
    assert p["drop_at"] == pytest.approx(3.0)
    assert 3.0 in p["beats"]  # drop 那一拍映射到时间线上正好在钩子结束处


def test_early_drop_delays_entry_instead_of_cutting_it():
    p = music.plan(60.0, 3.0, [], [track(drops=((2.0, 6.0),))])
    seg = p["segments"][0]
    assert seg["offset"] == 0.0 and seg["start"] == pytest.approx(1.0)
    assert p["drop_at"] == pytest.approx(3.0)


def test_weak_drops_ignored_and_downbeat_fallback():
    # 弱起势(不到最强的一半)不认;只有弱的那个够得着就退回强拍对齐
    p = music.plan(60.0, 3.0, [], [track(drops=((10.0, 1.0), (40.0, 8.0)))])
    assert p["segments"][0]["offset"] == pytest.approx(37.0)
    p = music.plan(60.0, 3.0, [], [track(drops=())])
    seg = p["segments"][0]
    assert p["drop_at"] is None
    assert seg["offset"] + 3.0 - seg["start"] in [pytest.approx(d) for d in track()["downbeats"]]


def test_ends_on_bar_line_with_fade():
    p = music.plan(61.3, 3.0, [], [track()])
    seg = p["segments"][-1]
    fade_at = p["fade_out_start"]
    assert any(abs(fade_at - d) < 1e-6 for d in p["downbeats"])  # 淡出从小节线起
    assert seg["fade_out"] == pytest.approx(music.FADE_OUT)
    assert seg["end"] == pytest.approx(fade_at + music.FADE_OUT)
    assert seg["end"] <= 61.3 and 61.3 - seg["end"] <= 4 * PERIOD  # 离片尾不到一小节


def test_long_video_switches_track_at_chapter_cut_without_looping():
    a, b = track("a", length=100.0), track("b", length=200.0)
    p = music.plan(180.0, 3.0, [40.0, 70.0, 150.0], [a, b])
    s0, s1 = p["segments"]
    assert p["switches"] == [70.0]  # 曲子收尾前最晚的章节交界
    assert s0["path"] != s1["path"]
    assert s0["end"] - s1["start"] == pytest.approx(music.XFADE)  # 以切点为中心交叉 2 秒
    assert s1["drop_at"] == pytest.approx(70.0)  # 第二首的起势也压在换章处
    assert covered(s0, 100.0) and covered(s1, 200.0)
    assert not any(s.get("reused") for s in p["segments"])
    assert p["segments"][-1]["end"] <= 180.0


def test_next_track_chosen_to_reach_next_chapter_cut():
    # 轮到的 b 太短,撑不到 200 秒的换章处;同档的 c 撑得到 → 先用 c,换曲落在交界上
    tracks = [track("a", length=100.0), track("b", length=80.0), track("c", length=200.0)]
    p = music.plan(300.0, 3.0, [70.0, 200.0], tracks)
    assert [s["title"] for s in p["segments"][:2]] == ["a", "c"]
    assert p["switches"][:2] == [70.0, 200.0]
    assert "b" in [s["title"] for s in p["segments"]]  # b 没丢,排在后面
    assert all(covered(s, {"a": 100.0, "b": 80.0, "c": 200.0}[s["title"]]) for s in p["segments"])


def test_switch_without_chapter_cut_uses_bar_line():
    # 一首就能铺满:不换曲
    p = music.plan(180.0, 3.0, [], [track("a", length=100.0), track("b", length=200.0)])
    assert [s["title"] for s in p["segments"]] == ["b"] and p["switches"] == []
    # 都铺不满、又没有章节交界:换曲落在小节线上
    p = music.plan(180.0, 3.0, [], [track("a", length=100.0), track("b", length=120.0)])
    c = p["switches"][0]
    assert any(abs(c - d) < 1e-6 for d in p["downbeats"]) or c in p["downbeats"]
    assert covered(p["segments"][0], 100.0)


def test_mood_filter_and_rotation():
    tracks = [track("calm", mood="舒缓"), track("hot1", mood="热血"), track("hot2", mood="热血")]
    picks = {music.plan(60.0, 3.0, [], tracks, "热血", seed=f"p{i}")["segments"][0]["title"]
             for i in range(12)}
    assert picks == {"hot1", "hot2"}
    # 没有这个情绪的曲子:不过滤
    assert music.plan(60.0, 3.0, [], tracks, "悬念")["segments"]


def test_strong_pulse_preferred():
    tracks = [track("mushy", pulse=0.1), track("clear", pulse=0.8)]
    assert music.plan(60.0, 3.0, [], tracks, seed="x")["segments"][0]["title"] == "clear"


def test_no_beats_falls_back_to_simple_fades(monkeypatch):
    notes = []
    monkeypatch.setattr(music.degrade, "note", lambda kind, detail="", **kw: notes.append(kind))
    p = music.plan(60.0, 3.0, [20.0], [track(beats=False)])
    seg = p["segments"][0]
    assert p["mode"] == "simple" and notes == ["bgm.no_beats"]
    assert seg["offset"] == 0.0 and seg["fade_in"] == music.SIMPLE_FADE_IN
    assert p["beats"] == [] and seg["end"] == pytest.approx(60.0)
    assert seg["fade_out"] == pytest.approx(music.FADE_OUT)


def test_no_tracks():
    p = music.plan(60.0, 3.0, [], [])
    assert p["mode"] == "none" and p["segments"] == []
    json.dumps(p)


def test_plan_is_json_and_short_tracks_terminate():
    p = music.plan(600.0, 3.0, [], [track("tiny", length=25.0)])
    json.dumps(p)
    assert 1 <= len(p["segments"]) <= 12
    assert all(covered(s, 25.0) for s in p["segments"])


def test_snap_tolerance():
    beats = [9.5, 10.0, 10.5]
    assert music.snap(10.1, beats) == 10.0
    assert music.snap(10.14, beats) == 10.0
    assert music.snap(10.25, beats) == 10.25  # 两边都超过 0.15 秒:不动
    assert music.snap(10.25, beats, tol=0.3) in (10.0, 10.5)
    assert music.snap(3.0, []) == 3.0
    assert music.snap_all([0.02, 9.6], beats) == [0.02, 9.5]


def test_library_dedups_and_prefers_analyzed(tmp_path):
    root = tmp_path / "auto_long"
    (root / "cinema").mkdir(parents=True)
    for d in (root, root / "cinema"):
        (d / "mixkit_1.mp3").write_bytes(b"x")
    src = "https://assets.mixkit.co/music/1/1.mp3"
    (root / "_licenses.json").write_text(json.dumps({"mixkit_1.mp3": {"source": src}}),
                                         encoding="utf-8")
    (root / "cinema" / "_licenses.json").write_text(
        json.dumps({"mixkit_1.mp3": {"source": src, "beats": [0.5], "mood": "悬念"},
                    "missing.mp3": {"source": "y"}}), encoding="utf-8")
    lib = music.library(root)
    assert len(lib) == 1 and lib[0]["mood"] == "悬念"


def test_bed_graph_shape():
    segs = [{"start": 0.0, "end": 71.0, "fade_in": 0.5, "fade_out": 2.0},
            {"start": 69.0, "end": 180.0, "fade_in": 2.0, "fade_out": 2.5}]
    g = music.bed_graph(segs, 180.0, [-3.0, 1.5])
    assert "adelay=delays=69000:all=1" in g and "amix=inputs=2" in g and "normalize=0" in g
    assert "apad=whole_dur=180.000" in g and g.endswith("[bed]")
    assert "loudnorm" not in g


def _tone_track(path, seconds, freq):
    sr = 48000
    t = np.arange(int(seconds * sr)) / sr
    y = 0.3 * np.sin(2 * np.pi * freq * t) * (0.6 + 0.4 * (np.sin(2 * np.pi * 2 * t) > 0))
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes((y * 32767).astype("<i2").tobytes())


@pytest.mark.skipif(not (config.FFMPEG_PATH and config.FFMPEG_PATH.is_file()), reason="没有 ffmpeg")
def test_render_bed_exact_length_and_sidecar(tmp_path):
    a, b = tmp_path / "a.wav", tmp_path / "b.wav"
    _tone_track(a, 30.0, 220.0)
    _tone_track(b, 40.0, 330.0)
    p = music.plan(45.0, 3.0, [15.0], [track("a", length=30.0, path=str(a), drops=((8.0, 6.0),)),
                                        track("b", length=40.0, path=str(b), drops=((8.0, 6.0),))])
    assert len(p["segments"]) == 2
    out = music.render_bed(p, tmp_path / "_render" / "bgm_bed.wav")
    assert out is not None and render.is_planned_bed(out)
    with wave.open(str(out), "rb") as w:
        assert w.getframerate() == 48000
        assert abs(w.getnframes() / 48000 - 45.0) < 0.05
    assert json.loads((tmp_path / "_render" / "bgm_bed.wav.plan.json").read_text(encoding="utf-8"))[
        "segments"]


def test_mix_does_not_loop_or_refade_planned_bed(tmp_path, monkeypatch):
    calls = []

    class Proc:
        returncode = 0
        stderr = ""

    def fake_run(args, timeout=None):
        calls.append(args)
        from pathlib import Path
        Path(args[-1]).write_bytes(b"RIFF")
        return Proc()

    monkeypatch.setattr(render, "_run", fake_run)
    monkeypatch.setattr(render, "measure_loudness", lambda p, **kw: {"I": -20.0, "TP": -3.0})
    narr, bed = tmp_path / "n.wav", tmp_path / "bed.wav"
    narr.write_bytes(b"x")
    bed.write_bytes(b"x")
    (tmp_path / "bed.wav.plan.json").write_text("{}", encoding="utf-8")
    out, err = render._mix_audio(narr, bed, 30.0, tmp_path / "mix.wav")
    assert out and not err and len(calls) == 2  # 两遍:先出 pre.wav,再出 mix.wav
    first = " ".join(calls[0])
    assert "-stream_loop" not in first and "afade" not in first
    assert "volume=4.00dB" in first and "volume=-6.00dB" in first  # 旁白 -16、BGM -26 LUFS
    assert "sidechaincompress" in first and "release=350" in first
    assert "loudnorm" not in first and "loudnorm" not in " ".join(calls[1])
    assert calls[1][-1].endswith("mix.wav") and "alimiter" in " ".join(calls[1])
    # 普通曲子(没有 .plan.json):照旧循环铺满 + 淡入淡出
    calls.clear()
    plain = tmp_path / "song.wav"
    plain.write_bytes(b"x")
    render._mix_audio(narr, plain, 30.0, tmp_path / "mix2.wav")
    assert "-stream_loop" in calls[0] and "afade" in " ".join(calls[0])
