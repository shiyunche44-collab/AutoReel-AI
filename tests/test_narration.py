"""旁白拼接的回归测试。

这一段曾经"永远失败但没人知道":滤镜链里出现了一条没有输入标签的
`apad=pad_dur=…[apN]`,ffmpeg 直接拒绝,错误又被 `except: return False`
吞掉,于是成片照常产出、界面照样显示成功 —— 只是没有一句人声。
配音是这套系统的核心,这种静默失败代价太大,所以这里真跑 ffmpeg 验证。
"""
from __future__ import annotations

import subprocess
import wave
from pathlib import Path

import pytest

from app import config
from app.pipeline import narration


def _make_tone(path: Path, seconds: float = 0.4, freq: int = 440) -> Path:
    """生成一段单声道 wav 正弦音,作为合成的输入素材。"""
    import math
    import struct

    rate = 16000
    n = int(rate * seconds)
    frames = b"".join(
        struct.pack("<h", int(12000 * math.sin(2 * math.pi * freq * i / rate)))
        for i in range(n)
    )
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(frames)
    return path


def _wav_duration(path: Path) -> float:
    with wave.open(str(path), "rb") as w:
        return w.getnframes() / float(w.getframerate())


pytestmark = pytest.mark.skipif(
    not config.FFMPEG_PATH or not Path(config.FFMPEG_PATH).is_file(),
    reason="需要本机 ffmpeg",
)


def test_concat_audio_produces_single_track(tmp_path):
    """两句音频必须拼成一条,并在句间插入静音。"""
    a = _make_tone(tmp_path / "a.wav", 0.4)
    b = _make_tone(tmp_path / "b.wav", 0.4)
    dest = tmp_path / "narration.wav"

    ok, err = narration._concat_audio([a, b], dest)
    assert ok, f"拼接应成功:{err}"
    assert dest.is_file() and dest.stat().st_size > 0

    total = _wav_duration(dest)
    gap = config.SENTENCE_GAP_MS / 1000.0
    # 两句各 0.4s,句间各留一段静音 → 总长必须明显大于 0.8s
    assert total > 0.8 + gap, f"总时长 {total:.2f}s 未包含句间停顿"
    assert total < 0.8 + gap * 6, f"总时长 {total:.2f}s 远超预期,静音可能被重复叠加"


def test_concat_audio_output_is_stereo_48k(tmp_path):
    """输出必须是 48kHz 立体声 —— 混音与字幕时间轴都依赖这个前提。"""
    a = _make_tone(tmp_path / "a.wav", 0.3)
    b = _make_tone(tmp_path / "b.wav", 0.3)
    dest = tmp_path / "n.wav"
    ok, err = narration._concat_audio([a, b], dest)
    assert ok, err

    probe = Path(str(config.FFMPEG_PATH)).with_name("ffprobe.exe")
    if not probe.is_file():
        probe = Path(str(config.FFMPEG_PATH)).with_name("ffprobe")
    out = subprocess.run(
        [str(probe), "-v", "error", "-show_entries",
         "stream=channels,sample_rate,codec_name", "-of", "csv=p=0", str(dest)],
        capture_output=True, text=True,
    )
    # csv 输出形如 "pcm_s16le,48000,2" —— 依次是编码 / 采样率 / 声道数
    fields = [f for f in out.stdout.strip().splitlines()[0].split(",") if f]
    assert len(fields) >= 3, out.stdout
    assert fields[1] == "48000", f"采样率应为 48k:{out.stdout}"
    assert fields[2] == "2", f"应为双声道:{out.stdout}"


def test_concat_audio_reports_failure_instead_of_silent_false(tmp_path):
    """失败必须回传原因,不能只给一个 False。"""
    dest = tmp_path / "n.wav"

    ok, err = narration._concat_audio([], dest)
    assert ok is False and "没有待拼接" in err

    ok, err = narration._concat_audio([tmp_path / "missing.wav"], dest)
    assert ok is False
    assert err, "失败原因不能为空字符串"


# 注:这里刻意**不**用"扫描源码找裸 apad"之类的静态断言。
# 滤镜链是多行隐式拼接的,逐行扫描既脆弱又给不了真正的保证;
# 上面两条真跑 ffmpeg 的用例才是有效守卫 —— 修复前它们确实会失败
# (ffmpeg 报 "More input link labels ... 4 > 2")。


def test_build_warns_when_narration_concat_fails(monkeypatch, tmp_path):
    """拼接失败必须进 warnings —— 静默降级出"哑片"是最糟的结果。"""
    from app.models import ShotSpec

    specs = [
        ShotSpec(idx=0, duration=6.0, section="开场钩子",
                 visual_zh="a", camera_zh="Static Shot", narration_zh="第一句。"),
        ShotSpec(idx=1, duration=6.0, section="核心卖点",
                 visual_zh="b", camera_zh="Static Shot", narration_zh="第二句。"),
    ]

    class _Asset:
        def __init__(self, i, text):
            self.local_path = _make_tone(tmp_path / f"t{i}.wav", 0.3)
            self.text = text

    monkeypatch.setattr(narration.audio, "synthesize_batch",
                        lambda texts, **kw: [_Asset(i, t) for i, t in enumerate(texts)])
    monkeypatch.setattr(narration.audio, "generate_music", lambda **kw: None)
    monkeypatch.setattr(narration.config, "project_dir", lambda pid: tmp_path)

    def boom(paths, dest):
        return False, "模拟失败"

    monkeypatch.setattr(narration, "_concat_audio", boom)

    result = narration.build("p_test", specs,
                             with_bgm=False, profile=None)
    assert result.narration_path is None
    assert any("旁白拼接失败" in w for w in result.warnings), result.warnings


def _three_specs():
    from app.models import ShotSpec

    return [ShotSpec(idx=i, visual_zh="v", narration_zh=f"第{i}拍的旁白。") for i in range(3)]


def test_tts_mid_failure_is_retried_and_keeps_shot_mapping(monkeypatch, tmp_path):
    """中间一句失败时:补跑一次,并且字幕必须仍挂在原来的镜头上(不整体前移)。"""
    from app.providers.audio import AudioAsset

    calls: list[tuple[list[str], str]] = []

    def fake(texts, **kw):
        prefix = kw.get("file_prefix", "tts")
        calls.append((list(texts), prefix))
        out = []
        for i, t in enumerate(texts):
            if prefix == "tts" and i == 1:
                continue  # 第一轮丢掉第 2 句
            p = _make_tone(tmp_path / f"{prefix}_{i}.wav", 0.3 + 0.1 * i)
            out.append(AudioAsset(text=t, local_path=p, index=i))
        return out

    monkeypatch.setattr(narration.audio, "synthesize_batch", fake)
    monkeypatch.setattr(narration.config, "project_dir", lambda pid: tmp_path)
    monkeypatch.setattr(narration, "_concat_audio", lambda paths, dest: (True, ""))
    result = narration.build("p_test", _three_specs(), with_bgm=False)
    assert [c[1] for c in calls] == ["tts", "tts_retry"]
    assert calls[1][0] == [calls[0][0][1]]  # 只补跑丢掉的那一句
    assert not result.missing_sentences
    assert [c.shot_idx for c in result.cues] == [0, 1, 2]
    assert [c.text for c in result.cues] == [f"第{i}拍的旁白。" for i in range(3)]


def test_subtitles_keep_arabic_numbers_while_tts_reads_chinese(monkeypatch, tmp_path):
    """TTS 吃中文读法,字幕必须是原文:屏幕上打"六百四十三公里"观众没法扫读。"""
    from app.models import ShotSpec
    from app.providers.audio import AudioAsset

    sent: list[str] = []

    def fake(texts, **kw):
        sent.extend(texts)
        return [AudioAsset(text=t, local_path=_make_tone(tmp_path / f"n{i}.wav"), index=i)
                for i, t in enumerate(texts)]

    monkeypatch.setattr(narration.audio, "synthesize_batch", fake)
    monkeypatch.setattr(narration.config, "project_dir", lambda pid: tmp_path)
    monkeypatch.setattr(narration, "_concat_audio", lambda paths, dest: (True, ""))
    specs = [ShotSpec(idx=0, visual_zh="v", narration_zh="CLTC 续航 643 公里,电池 73kWh。")]
    result = narration.build("p_num", specs, with_bgm=False)
    assert "六百四十三" in sent[0] and "千瓦时" in sent[0]
    joined = "".join(c.text for c in result.cues)
    assert "643" in joined and "73kWh" in joined and "六百" not in joined


def test_tts_still_missing_is_reported_without_shifting(monkeypatch, tmp_path):
    from app.providers.audio import AudioAsset

    def fake(texts, **kw):
        if kw.get("file_prefix") == "tts_retry":
            return []
        return [AudioAsset(text=t, local_path=_make_tone(tmp_path / f"t{i}.wav"), index=i)
                for i, t in enumerate(texts) if i != 1]

    monkeypatch.setattr(narration.audio, "synthesize_batch", fake)
    monkeypatch.setattr(narration.config, "project_dir", lambda pid: tmp_path)
    monkeypatch.setattr(narration, "_concat_audio", lambda paths, dest: (True, ""))
    result = narration.build("p_test", _three_specs(), with_bgm=False)
    assert result.missing_sentences == [1]
    assert [c.shot_idx for c in result.cues] == [0, 2]


# --------------------------------------------------------------------------
# 句子缓存与 BGM 曲库
# --------------------------------------------------------------------------
def _recording_tts(tmp_path, calls):
    from app.providers.audio import AudioAsset

    def fake(texts, **kw):
        calls.append(list(texts))
        out = []
        for i, t in enumerate(texts):
            p = _make_tone(tmp_path / f"{kw.get('file_prefix', 'tts')}_{len(calls)}_{i}.wav",
                           0.3 + 0.05 * len(t) % 3)
            out.append(AudioAsset(text=t, local_path=p, index=i))
        return out
    return fake


def test_rebuild_only_synthesizes_changed_sentences(monkeypatch, tmp_path):
    from app.models import ShotSpec

    calls: list[list[str]] = []
    monkeypatch.setattr(narration.audio, "synthesize_batch", _recording_tts(tmp_path, calls))
    monkeypatch.setattr(narration.config, "project_dir", lambda pid: tmp_path / pid)
    monkeypatch.setattr(narration, "_concat_audio", lambda paths, dest: (True, ""))

    first = narration.build("p1", _three_specs(), with_bgm=False)
    assert len(calls[0]) == 3 and first.cached_sentences == 0
    assert first.synth_chars == sum(len(t) for t in calls[0])

    specs = _three_specs()
    specs[1] = ShotSpec(idx=1, visual_zh="v", narration_zh="改过的第一拍旁白。")
    again = narration.build("p1", specs, with_bgm=False)
    assert len(calls[1]) == 1 and "改过" in calls[1][0]
    assert again.cached_sentences == 2
    assert [c.shot_idx for c in again.cues] == [0, 1, 2]
    assert again.cues[1].text == calls[1][0]

    # 别的项目同样的句子也直接命中
    other = narration.build("p2", _three_specs(), with_bgm=False)
    assert len(calls) == 2 and other.cached_sentences == 3 and other.synth_chars == 0


def test_tts_cache_is_per_voice(monkeypatch, tmp_path):
    calls: list[list[str]] = []
    monkeypatch.setattr(narration.audio, "synthesize_batch", _recording_tts(tmp_path, calls))
    monkeypatch.setattr(narration.config, "project_dir", lambda pid: tmp_path / pid)
    monkeypatch.setattr(narration, "_concat_audio", lambda paths, dest: (True, ""))
    narration.build("p1", _three_specs(), with_bgm=False, voice_id="a")
    narration.build("p1", _three_specs(), with_bgm=False, voice_id="b")
    assert len(calls) == 2 and len(calls[1]) == 3


def test_bgm_library_fills_then_reuses(monkeypatch, tmp_path):
    from app.providers.audio import AudioAsset

    made: list[str] = []

    def gen(prompt, output_dir, output_file, **kw):
        made.append(output_file)
        dest = Path(output_dir) / output_file
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"music")
        return AudioAsset(text=prompt, local_path=dest)

    monkeypatch.setattr(narration.audio, "generate_music", gen)
    for n in range(5):
        d = tmp_path / f"proj{n}"
        d.mkdir()
        path, reused = narration.library_bgm("轻快电子", d, f"proj{n}")
        assert path == d / "bgm.mp3" and path.read_bytes() == b"music"
        assert reused == (n >= narration.BGM_LIBRARY_SIZE)
    assert len(made) == narration.BGM_LIBRARY_SIZE

    # 同一项目重新确认:直接用已有的,不生成
    path, reused = narration.library_bgm("轻快电子", tmp_path / "proj0", "proj0")
    assert reused and len(made) == narration.BGM_LIBRARY_SIZE


def test_bgm_falls_back_to_library_when_generation_fails(monkeypatch, tmp_path):
    lib = narration.bgm_library_dir("轻快电子")
    lib.mkdir(parents=True)
    (lib / "00.mp3").write_bytes(b"old")
    monkeypatch.setattr(narration.audio, "generate_music", lambda **kw: None)
    path, reused = narration.library_bgm("轻快电子", tmp_path, "p")
    assert reused and path.read_bytes() == b"old"
    empty, _ = narration.library_bgm("没生成过的风格", tmp_path / "x", "p")
    assert empty is None
