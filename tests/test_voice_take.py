"""一口气合成:分组、SSML、切句、停顿,以及切不开时退回逐句。"""
from __future__ import annotations

import math
import struct
import wave
from pathlib import Path

import pytest

from app import config
from app.models import ShotSpec
from app.pipeline import narration, timeline, voice_take
from app.pipeline.explainer.timing import beat_durations
from app.pipeline.voice_take import find_cuts, plan_takes, to_ssml

needs_ffmpeg = pytest.mark.skipif(
    not config.FFMPEG_PATH or not Path(config.FFMPEG_PATH).is_file(), reason="需要本机 ffmpeg")


def _tone_with_silences(path: Path, parts: list[tuple[str, float]]) -> Path:
    """按 [("tone"|"mute", 秒)] 写一段 wav,当成接口返回的 take(ffmpeg 按内容识别格式)。"""
    rate = 24000
    frames = bytearray()
    for kind, seconds in parts:
        for i in range(int(rate * seconds)):
            value = 12000 * math.sin(2 * math.pi * 300 * i / rate) if kind == "tone" else 0
            frames += struct.pack("<h", int(value))
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(bytes(frames))
    return path


def _wav_seconds(path: Path) -> float:
    with wave.open(str(path), "rb") as r:
        return r.getnframes() / float(r.getframerate())


def test_plan_takes_breaks_on_section_tempo_and_length():
    texts = ["一" * 40, "二" * 40, "三" * 40, "四" * 40, "五" * 40]
    assert plan_takes(texts, ["a", "a", "b", "b", "b"], [1.0] * 5, 100) == [[0, 1], [2, 3], [4]]
    assert plan_takes(texts, ["a"] * 5, [1.05, 1, 1, 1, 0.95], 500) == [[0], [1, 2, 3], [4]]


def test_to_ssml_breaks_only_after_strong_end_and_escapes():
    ssml = to_ssml(["R&D 投入很大,", "但没用。", "你说呢?", "好。"], [0.3, 0.35, 0.2, 0.2])
    assert ssml == '<speak>R&amp;D 投入很大,但没用。<break time="350ms"/>你说呢?好。</speak>'


def test_find_cuts_lands_on_measured_silence_not_timestamps():
    texts = ["甲乙丙。", "丁戊己。"]
    # 时间戳说第二句 1.2 秒开口,实测静音到 1.35 才结束
    words = [("甲", 0.0, 0.3), ("乙", 0.3, 0.6), ("丙。", 0.6, 0.9),
             ("丁", 1.2, 1.5), ("戊", 1.5, 1.8), ("己。", 1.8, 2.1)]
    assert find_cuts(texts, words, 2.2, [(0.95, 1.35)]) == [(0.95, 1.35)]
    # 没有静音(逗号处连读):切在下一句开口处,不留停顿
    assert find_cuts(texts, words, 2.2, []) == [(1.2, 1.2)]
    # 时间戳和文本对不上:不猜
    assert find_cuts(texts, [("完全", 0.0, 1.0), ("不同", 1.0, 2.0)], 2.2, []) is None


@needs_ffmpeg
def test_split_take_keeps_short_pause_and_shortens_long(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "TTS_PAUSE_CAP", 0.35)
    texts = ["甲乙丙。", "丁戊己。"]
    words = [("甲", 0.0, 0.3), ("乙", 0.3, 0.6), ("丙。", 0.6, 0.9),
             ("丁", 1.3, 1.6), ("戊", 1.6, 1.9), ("己。", 1.9, 2.3)]
    # 0.3 秒的停顿原样留着,总长不变
    raw = _tone_with_silences(tmp_path / "a.mp3", [("tone", 1.0), ("mute", 0.3), ("tone", 1.0)])
    paths = voice_take.split_take(raw, texts, words, tmp_path, "a", [0.1, 0.0])
    assert sum(_wav_seconds(p) for p in paths) == pytest.approx(2.3, abs=0.01)
    # 0.8 秒的压到上限
    raw = _tone_with_silences(tmp_path / "b.mp3", [("tone", 1.0), ("mute", 0.8), ("tone", 1.0)])
    later = [(w, s + 0.5, e + 0.5) if s > 1 else (w, s, e) for w, s, e in words]
    paths = voice_take.split_take(raw, texts, later, tmp_path, "b", [0.1, 0.0])
    assert sum(_wav_seconds(p) for p in paths) == pytest.approx(2.35, abs=0.02)
    # 计划停顿更长(问句后):听计划的
    paths = voice_take.split_take(raw, texts, later, tmp_path, "c", [0.5, 0.0])
    assert sum(_wav_seconds(p) for p in paths) == pytest.approx(2.5, abs=0.02)


@needs_ffmpeg
def test_split_take_shortens_long_pause_inside_sentence(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "TTS_PAUSE_CAP", 0.35)
    raw = _tone_with_silences(tmp_path / "a.mp3", [("tone", 1.0), ("mute", 0.8), ("tone", 1.0)])
    paths = voice_take.split_take(raw, ["甲乙丙丁戊己。"], [], tmp_path, "a", [0.0])
    assert _wav_seconds(paths[0]) == pytest.approx(2.35, abs=0.02)


@needs_ffmpeg
def test_trim_clip_can_keep_edges(tmp_path):
    src = _tone_with_silences(tmp_path / "s.wav", [("mute", 0.3), ("tone", 1.0), ("mute", 0.3)])
    assert _wav_seconds(narration.trim_clip(src, tmp_path / "d.wav")) < 1.3
    kept = narration.trim_clip(src, tmp_path / "k.wav", edges=False)
    assert _wav_seconds(kept) == pytest.approx(1.6, abs=0.01)


def _specs() -> list[ShotSpec]:
    return [ShotSpec(idx=0, visual_zh="x", section="a", narration_zh="开场一句话。"),
            ShotSpec(idx=1, visual_zh="x", section="a", narration_zh="甲乙丙丁戊?己庚辛壬癸。"),
            ShotSpec(idx=2, visual_zh="x", section="b", narration_zh="收尾说一句。")]


def _fake_take(tmp_path, calls, *, words_ok=True):
    def fake(ssml, **_kw):
        calls.append(ssml)
        plain = ssml.replace("<speak>", "").replace("</speak>", "")
        chunks = [c for c in plain.split("<break") if c]
        parts: list[tuple[str, float]] = []
        words: list[tuple[str, float, float]] = []
        cursor = 0.0
        for n, chunk in enumerate(chunks):
            text = chunk.split("/>")[-1]
            if n:
                parts.append(("mute", 0.4))
                cursor += 0.4
            parts.append(("tone", 0.15 * len(text)))
            for ch in text:
                words.append((ch, cursor, cursor + 0.15))
                cursor += 0.15
        raw = _tone_with_silences(tmp_path / f"fake_{len(calls)}.wav", parts)
        return raw.read_bytes(), (words if words_ok else [])
    return fake


def _no_side_effects(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "TTS_ONE_TAKE", True)
    monkeypatch.setattr(config, "CAPTION_WORD_TIMING", False)
    monkeypatch.setattr(config, "project_dir", lambda pid: tmp_path / pid)
    monkeypatch.setattr(narration.db, "write_json", lambda *a, **k: None)


@needs_ffmpeg
def test_build_with_takes_keeps_track_and_beats_in_sync(tmp_path, monkeypatch):
    _no_side_effects(monkeypatch, tmp_path)
    calls: list[str] = []
    monkeypatch.setattr(voice_take.ark_tts, "synthesize_take", _fake_take(tmp_path, calls))

    def no_batch(*_a, **_k):
        raise AssertionError("不该退回逐句")

    monkeypatch.setattr(narration.audio, "synthesize_batch", no_batch)

    res = narration.build("p_take", _specs(), voice_id="v_bigtts", with_bgm=False, speed=1.1)
    assert res.ok and res.narration_path
    assert any("<break" in c for c in calls)
    assert [c.shot_idx for c in res.cues] == [0, 1, 1, 2]
    beats = beat_durations(res.cues, 3)
    assert sum(beats) == pytest.approx(_wav_seconds(res.narration_path), abs=0.05)
    # 同一条 take 里不再插静音:停顿留在音频里,下一句的字幕紧接着上一句
    assert res.cues[2].start - res.cues[1].end == pytest.approx(0.0, abs=0.01)


@needs_ffmpeg
def test_build_falls_back_to_sentences_when_take_cannot_split(tmp_path, monkeypatch):
    _no_side_effects(monkeypatch, tmp_path)
    monkeypatch.setattr(voice_take.ark_tts, "synthesize_take",
                        _fake_take(tmp_path, [], words_ok=False))
    asked: list[str] = []

    def fake_batch(batch, *, output_dir, file_prefix="tts", **_kw):
        out = []
        for n, text in enumerate(batch):
            asked.append(text)
            path = _tone_with_silences(Path(output_dir) / f"{file_prefix}_{n:03d}.wav",
                                       [("tone", 0.6)])
            out.append(narration.audio.AudioAsset(text=text, local_path=path, node_id="n",
                                                  index=n))
        return out

    monkeypatch.setattr(narration.audio, "synthesize_batch", fake_batch)
    res = narration.build("p_take", _specs(), voice_id="v_bigtts", with_bgm=False, speed=1.1)
    assert res.ok
    # 单句 take 没有切点可错,照常可用;只有两句那条退回逐句
    assert asked == ["甲乙丙丁戊?", "己庚辛壬癸。"]
    assert any("退回逐句" in w for w in res.warnings)


def test_varied_gaps_follow_meaning_and_seed():
    texts = ["先说这个,", "这是一句话。", "你觉得呢?", "但是有个问题。", "换一段了。", "结束。"]
    sections = ["a", "a", "a", "a", "a", "b"]
    gaps = timeline.varied_gaps(texts, "p1", sections)
    assert gaps == timeline.varied_gaps(texts, "p1", sections)
    assert gaps != timeline.varied_gaps(texts, "p2", sections)
    assert gaps[0] < gaps[1] < gaps[4]  # 逗号 < 句号 < 换段
    assert gaps[2] > gaps[1]  # 问句后接转折
    assert max(gaps) <= timeline.PAUSE_MAX
    assert gaps[-1] == timeline.gap_after(texts[-1])


def test_normalize_reads_model_years_liang_and_km():
    norm = timeline.normalize_for_tts
    assert norm("2025款") == "二零二五款"
    assert norm("25款") == "二五款"
    assert norm("20款车型") == "二十款车型"
    assert norm("12款") == "十二款"
    assert norm("2000元") == "两千元"
    assert norm("28000转") == "两万八千转"
    assert norm("220伏") == "两百二十伏"
    assert norm("2个人") == "两个人"
    assert norm("第2个") == "第二个"
    assert norm("12个") == "十二个"
    assert norm("2.5秒") == "二点五秒"
    assert norm("二次充电") == "二次充电"
    assert norm("续航700km") == "续航七百公里"
    assert norm("100km/h") == "一百公里每小时"
