"""语音质检:哑句、语速异常、转写比对、自动重合成、仍不合格时拦门禁。
不需要网络与额度;转写用可注入的假函数,不加载模型。"""
from __future__ import annotations

import math
import struct
import wave
from pathlib import Path

import pytest

from app import config
from app.pipeline import narration, voice_qa

pytestmark = pytest.mark.skipif(not config.FFMPEG_PATH, reason="需要 ffmpeg")


def _wav(path: Path, seconds: float, *, silent: bool = False, amp: int = 9000) -> Path:
    rate = 16000
    n = int(rate * seconds)
    frames = b"".join(
        struct.pack("<h", 0 if silent else int(amp * math.sin(2 * math.pi * 300 * i / rate)))
        for i in range(n))
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(frames)
    return path


def test_analyze_clip_tells_tone_from_silence(tmp_path):
    tone = voice_qa.analyze_clip(_wav(tmp_path / "t.wav", 1.0))
    mute = voice_qa.analyze_clip(_wav(tmp_path / "m.wav", 1.0, silent=True))
    assert tone.ok and tone.voiced_ratio > 0.9 and tone.mean_db > -30
    assert mute.ok and mute.voiced_ratio < 0.1


def test_mute_clip_is_an_error_without_any_asr(tmp_path):
    texts = ["第一句的旁白。", "第二句的旁白。"]
    clips = {0: _wav(tmp_path / "a.wav", 1.0), 1: _wav(tmp_path / "b.wav", 1.0, silent=True)}
    res = voice_qa.check(texts, clips, transcribe=lambda p: "")
    assert res.errors == [1]
    assert "哑句" in res.findings[0].reason


def test_hinted_sentence_is_transcribed_and_misaligned_audio_is_caught(tmp_path):
    texts = ["高性能版换了大轮胎。", "后排空间也相当够用。"]
    clips = {i: _wav(tmp_path / f"{i}.wav", 1.2) for i in range(2)}
    heard = {str(clips[0]): "后排空间也相当够用", str(clips[1]): "后排空间也相当够用"}
    res = voice_qa.check(texts, clips, hint={0, 1}, transcribe=lambda p: heard[str(p)])
    assert res.errors == [0]            # 第 0 句挂的是别句的音频
    assert res.verified == [1]
    assert res.asr_checked == 2
    assert res.findings[0].heard.startswith("后排空间")


def test_numbers_do_not_cause_false_alarms(tmp_path):
    """转写把读法写回阿拉伯数字也不算错:只比汉字。"""
    texts = ["续航六百四十三公里。"]
    clips = {0: _wav(tmp_path / "0.wav", 1.5)}
    res = voice_qa.check(texts, clips, originals=["续航 643 公里。"], hint={0},
                         transcribe=lambda p: "续航643公里")
    assert res.errors == []


def test_unusual_speaking_rate_is_suspect_and_checked(tmp_path):
    texts = ["这是一句正常长度的旁白啊。"] * 5
    clips = {i: _wav(tmp_path / f"{i}.wav", 2.0) for i in range(5)}
    clips[3] = _wav(tmp_path / "3.wav", 0.5)   # 12 个字念了 0.5 秒:截断
    seen: list[Path] = []

    def asr(p: Path) -> str:
        seen.append(p)
        return "天地玄黄宇宙洪荒日月盈昃"

    res = voice_qa.check(texts, clips, transcribe=asr)
    assert seen == [clips[3]]            # 只复核可疑句,正常的不转写
    assert res.errors == [3]


def test_without_asr_only_extreme_rate_is_an_error(tmp_path, monkeypatch):
    monkeypatch.setattr(voice_qa, "load_asr", lambda: None)
    texts = ["这是一句正常长度的旁白啊。"] * 6
    clips = {i: _wav(tmp_path / f"{i}.wav", 2.0) for i in range(6)}
    clips[2] = _wav(tmp_path / "2.wav", 0.5)    # 极端:疑似截断
    clips[4] = _wav(tmp_path / "4.wav", 3.6)    # 偏慢:只提醒
    res = voice_qa.check(texts, clips)
    assert not res.asr_available
    assert res.errors == [2]
    assert any(f.index == 4 and f.level == "warn" for f in res.findings)


# --------------------------------------------------------------------------
# 接入 narration.build
# --------------------------------------------------------------------------
def _specs():
    from app.models import ShotSpec

    return [ShotSpec(idx=i, visual_zh="v", narration_zh=f"第{i}拍的旁白说明。") for i in range(4)]


def _setup(monkeypatch, tmp_path, make):
    monkeypatch.setattr(config, "VOICE_QA", True)
    monkeypatch.setattr(voice_qa, "load_asr", lambda: None)
    monkeypatch.setattr(narration.audio, "generate_music", lambda **kw: None)
    monkeypatch.setattr(narration.config, "project_dir", lambda pid: tmp_path)
    calls: list[list[str]] = []

    def fake(texts, **kw):
        calls.append(list(texts))
        n = len(calls)
        return [narration.audio.AudioAsset(
            text=t, local_path=make(tmp_path / f"s{n}_{i}.wav", n, i, t), index=i)
            for i, t in enumerate(texts)]

    monkeypatch.setattr(narration.audio, "synthesize_batch", fake)
    return calls


def test_build_redoes_a_bad_sentence_and_keeps_it_out_of_the_cache(monkeypatch, tmp_path):
    def make(path, call, i, text):
        # 第一次合成时 batch 里第 3 句是哑的;重做后正常
        return _wav(path, 1.0, silent=(call == 1 and i == 2))

    calls = _setup(monkeypatch, tmp_path, make)
    res = narration.build("p_qa", _specs(), with_bgm=False)
    assert res.ok and res.qa_failed == []
    assert calls[1] == [calls[0][2]]            # 只重做了那一句
    assert res.qa_report["redone"] == [2]
    assert (tmp_path / "voice_qa.json").is_file() or res.qa_report


def test_build_reports_sentences_that_stay_bad(monkeypatch, tmp_path):
    def make(path, call, i, text):
        return _wav(path, 1.0, silent=("一拍" in text))

    _setup(monkeypatch, tmp_path, make)
    res = narration.build("p_qa", _specs(), with_bgm=False)
    assert res.qa_failed == [1]
    assert any("重做后仍不合格" in w for w in res.warnings)


def test_time_beats_blocks_gate_a_on_failed_qa(monkeypatch, tmp_path):
    from app.pipeline import explainer

    res = narration.NarrationResult(ok=True, cues=[object()], qa_failed=[1],
                                    qa_report={"findings": [{"index": 1, "level": "error",
                                                             "reason": "读错词或音频错位"}]})
    monkeypatch.setattr(explainer.narration, "build", lambda *a, **kw: res)
    monkeypatch.setattr(explainer.db, "get_project", lambda pid: None)
    monkeypatch.setattr(explainer.aspect, "frame_size", lambda p: (1920, 1080))
    monkeypatch.setattr(explainer.aspect, "line_chars", lambda p: 16)
    with pytest.raises(RuntimeError, match="旁白质检未通过.*第 2 句 读错词或音频错位"):
        explainer.time_beats("p_qa", _specs(), None)


def test_cache_hits_are_rechecked_until_verified(monkeypatch, tmp_path):
    """带毒的缓存:命中后转写复核判错 → 清缓存 → 重合成;核对过的再命中就不再转写。"""
    monkeypatch.setattr(config, "VOICE_QA", True)
    monkeypatch.setattr(narration.audio, "generate_music", lambda **kw: None)
    monkeypatch.setattr(narration.config, "project_dir", lambda pid: tmp_path)
    specs = _specs()
    texts = [narration.normalize_for_tts(f"第{i}拍的旁白说明。", None) for i in range(4)]
    voice, speed = narration.audio.DEFAULT_VOICE_ID, config.TTS_SPEED
    cache = narration.tts_cache_dir()
    cache.mkdir(parents=True)
    for i, t in enumerate(texts):
        _wav(cache / f"{narration._tts_key(t, voice, speed, config.TTS_EMOTION)}.wav", 1.0)

    seen: list[str] = []

    def asr(p: Path) -> str:
        seen.append(p.name)
        return "天地玄黄宇宙洪荒日月盈昃"          # 缓存里的音频全是错位的

    monkeypatch.setattr(voice_qa, "load_asr", lambda: asr)
    monkeypatch.setattr(voice_qa, "asr_available", lambda: True)
    synth = []
    monkeypatch.setattr(narration.audio, "synthesize_batch",
                        lambda ts, **kw: synth.append(list(ts)) or [])
    res = narration.build("p_qa", specs, with_bgm=False)
    assert seen, "命中缓存的句子必须被转写复核"
    assert synth and res.qa_failed        # 清了缓存、重合成没拿到音频、最终报不合格
    assert not list(cache.glob("*.wav"))   # 毒缓存已经清掉
