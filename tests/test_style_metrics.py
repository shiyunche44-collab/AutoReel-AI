"""`teardown.style_metrics`:用 ffmpeg 合成一段已知答案的测试片来验证。

测试片 12 秒(320×180,30fps):
  0–4 秒   静止浅灰色块
  4–8 秒   黑底上一个白方块匀速右移        ← 4 秒处硬切
  8–12 秒  静止中灰色块                    ← 8 秒处硬切
音轨:每 2 秒响 1.2 秒的"旁白"(纯音),停顿里是静音 —— 没有音乐底。
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import numpy as np
import pytest

from app import config
from app.pipeline import teardown

HAS_FF = bool(config.FFMPEG_PATH and config.FFMPEG_PATH.is_file()
              and config.FFPROBE_PATH and config.FFPROBE_PATH.is_file())


def _make_clip(path: Path) -> Path:
    graph = (
        "color=c=0xC8C8C8:s=320x180:r=30:d=4[a];"
        "color=c=black:s=320x180:r=30:d=4[bg];"
        "color=c=white:s=60x60:r=30:d=4[sq];"
        "[bg][sq]overlay=x='20+t*60':y=60:shortest=1[b];"
        "color=c=0x808080:s=320x180:r=30:d=4[c];"
        "[a][b][c]concat=n=3:v=1:a=0,format=yuv420p[v];"
        "aevalsrc='0.3*sin(2*PI*440*t)*lt(mod(t\\,2)\\,1.2)':s=16000:d=12[au]"
    )
    r = subprocess.run(
        [str(config.FFMPEG_PATH), "-v", "error", "-y", "-filter_complex", graph,
         "-map", "[v]", "-map", "[au]", "-c:v", "libx264", "-preset", "veryfast", "-crf", "18",
         "-c:a", "aac", "-b:a", "96k", str(path)],
        capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stderr[-500:]
    return path


@pytest.fixture(scope="module")
def clip(tmp_path_factory) -> Path:
    if not HAS_FF:
        pytest.skip("需要 ffmpeg / ffprobe")
    return _make_clip(tmp_path_factory.mktemp("style") / "synthetic.mp4")


def test_style_metrics_on_synthetic_clip(clip):
    m = teardown.style_metrics(clip)
    assert m["duration"] == pytest.approx(12.0, abs=0.2)
    assert (m["width"], m["height"]) == (320, 180)
    # 两个硬切,场景检测都能抓到,别的地方不报
    assert len(m["cuts"]) == 2
    assert m["cuts"][0] == pytest.approx(4.0, abs=0.1)
    assert m["cuts"][1] == pytest.approx(8.0, abs=0.1)
    assert m["cuts_per_minute"] == pytest.approx(10.0, abs=0.1)
    # 起点在前 5 秒的镜头:0–4、4–8;正片只有 8–12
    assert m["hook"]["shots"] == 2 and m["hook"]["median"] == pytest.approx(4.0, abs=0.1)
    assert m["body"]["shots"] == 1 and m["body"]["p90"] == pytest.approx(4.0, abs=0.2)
    mo = m["motion"]
    assert mo["samples"] == pytest.approx(24, abs=1)
    assert mo["cut"] == 2                                      # 两刀各占一个间隔
    assert mo["moving"] == pytest.approx(7, abs=1)             # 4–8 秒的方块在动
    # 动 7 / (动 7 + 静 14):三分之一
    assert m["dynamic_ratio"] == pytest.approx(1 / 3, abs=0.06)
    # 两段 4 秒静止色块(各 7–8 个间隔)占约 2/3
    assert m["still_ratio"] == pytest.approx(0.62, abs=0.06)
    assert m["lufs"] is not None and -40 < m["lufs"] < -5
    assert m["music_bed"] is False


def test_motion_profile_counts():
    still = np.zeros((36, 64), np.uint8)
    moved = still.copy()
    moved[:, :12] = 200                                         # 平均差 200*12/64 = 37.5:动
    flash = np.full((36, 64), 255, np.uint8)                    # 255:切
    frames = np.stack([still, still, still, moved, still, flash, still])
    p = teardown.motion_profile(frames)
    assert (p["static"], p["moving"], p["cut"]) == (2, 2, 2)
    assert p["dynamic_ratio"] == 0.5
    # 头两个间隔连续不动 1 秒,算一段静帧
    assert p["still_ratio"] == pytest.approx(2 / 6, abs=1e-3)
    assert teardown.motion_profile(frames[:1])["dynamic_ratio"] is None


def test_shot_stats_split_hook_and_body():
    s = teardown.shot_stats([1.0, 2.0, 3.0, 6.0, 10.0], 13.0)
    assert s["hook"]["shots"] == 4 and s["hook"]["median"] == 1.0   # 0–1, 1–2, 2–3, 3–6
    assert s["body"]["shots"] == 2 and s["body"]["median"] == 3.5   # 6–10, 10–13
    assert s["body"]["p10"] == 3.1 and s["body"]["p90"] == 3.9
    assert teardown.shot_stats([], 3.0)["body"]["median"] is None


def test_merge_cuts_drops_flash_duplicates():
    assert teardown.merge_cuts([0.1, 3.0, 3.1, 3.5, 7.0]) == [3.0, 3.5, 7.0]


def _speech(sr: int, seconds: float, bed: float = 0.0) -> np.ndarray:
    t = np.arange(int(sr * seconds)) / sr
    voice = 0.3 * np.sin(2 * np.pi * 220 * t) * ((t % 2.0) < 1.3)
    music = bed * (np.sin(2 * np.pi * 330 * t) + 0.5 * np.sin(2 * np.pi * 495 * t))
    rng = np.random.default_rng(0)
    return (voice + music + rng.normal(0, 1e-4, t.size)).astype(np.float32)


def test_music_bed_verdicts():
    sr = 16000
    assert teardown.music_bed(_speech(sr, 60), sr)["verdict"] is False
    # 比旁白低约 20 dB 的持续和弦
    assert teardown.music_bed(_speech(sr, 60, bed=0.03), sr)["verdict"] is True
    # 只有后半段垫了乐:不下结论
    half = _speech(sr, 60)
    half[sr * 30:] = _speech(sr, 30, bed=0.03)
    assert teardown.music_bed(half, sr)["verdict"] is None
    assert teardown.music_bed(np.zeros(sr), sr)["verdict"] is None    # 太短
