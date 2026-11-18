"""节拍分析:合成节拍器上 BPM 误差 ≤ 1、拍点 / 强拍 / 起势位置对得上。"""
from __future__ import annotations

import json
import wave

import numpy as np
import pytest

from app import config
from app.pipeline import beat_track as bt

SR = bt.SR


def metronome(bpm: float, seconds: float, *, phase: float = 0.37, accent_every: int = 4,
              drop_at: float | None = None, seed: int = 0) -> tuple[np.ndarray, np.ndarray]:
    """节拍器:每拍一声短促的高频 click,每小节第一拍加重并叠一个低频 thump;
    `drop_at` 之后音量变大并铺一层噪声(模拟副歌 / 起势)。返回 (信号, 真实拍点秒)。"""
    rng = np.random.default_rng(seed)
    n = int(seconds * SR)
    y = rng.standard_normal(n) * 0.002
    period = 60.0 / bpm
    t = np.arange(int(0.03 * SR)) / SR
    click = np.sin(2 * np.pi * 1500 * t) * np.exp(-t * 120)
    thump = np.sin(2 * np.pi * 60 * t) * np.exp(-t * 40)
    times = []
    k = 0
    while (tb := phase + k * period) < seconds - 0.05:
        i = int(round(tb * SR))
        amp = 0.3 if drop_at is None or tb < drop_at else 0.8
        s = click * amp
        if k % accent_every == 0:
            s = s * 1.6 + thump * amp
        j = min(n, i + len(s))
        y[i:j] += s[: j - i]
        times.append(tb)
        k += 1
    if drop_at:
        i = int(drop_at * SR)
        y[i:] += rng.standard_normal(n - i) * 0.05
    return y, np.array(times)


@pytest.mark.parametrize("bpm", [72, 90, 100, 120, 128, 140])
def test_bpm_within_one_and_beats_on_clicks(bpm):
    y, times = metronome(bpm, 30)
    r = bt.analyze_signal(y)
    assert abs(r["bpm"] - bpm) <= 1.0
    beats = np.array(r["beats"])
    inner = times[2:-2]
    err = np.array([np.min(np.abs(beats - t)) for t in inner])
    assert np.median(err) < 0.02 and err.max() < 0.1
    assert len(beats) >= len(inner)
    assert r["pulse"] > 0.3


def test_downbeats_follow_accents():
    y, times = metronome(120, 30, phase=0.37)
    r = bt.analyze_signal(y)
    accents = times[::4]
    downs = np.array(r["downbeats"])
    assert len(downs) >= len(accents) - 2
    assert np.median([np.min(np.abs(accents - d)) for d in downs]) < 0.03


def test_drop_found_near_energy_jump():
    y, _ = metronome(120, 40, drop_at=20.0)
    r = bt.analyze_signal(y)
    assert r["drops"], r
    assert abs(r["drops"][0] - 20.0) <= 2.0  # 落在一小节(2 秒)以内
    assert r["drop_db"][0] >= bt.DROP_MIN_DB


def test_silence_has_no_beats():
    r = bt.analyze_signal(np.zeros(SR * 10))
    assert r["drops"] == []
    assert r["duration"] == 10.0


def _write_wav(path, y):
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes((np.clip(y, -1, 1) * 32767).astype("<i2").tobytes())


@pytest.mark.skipif(not (config.FFMPEG_PATH and config.FFMPEG_PATH.is_file()), reason="没有 ffmpeg")
def test_analyze_library_writes_ledger(tmp_path):
    root = tmp_path / "bgm"
    (root / "auto_long" / "pool").mkdir(parents=True)
    y, _ = metronome(120, 20)
    _write_wav(root / "auto_long" / "pool" / "a.wav", y)
    ledger = root / "auto_long" / "pool" / "_licenses.json"
    ledger.write_text(json.dumps({"a.wav": {"title": "A", "mood": "科技"},
                                  "gone.mp3": {"title": "缺文件"}}, ensure_ascii=False),
                      encoding="utf-8")
    rep = bt.analyze_library(root)
    assert len(rep["done"]) == 1 and not rep["failed"]
    meta = json.loads(ledger.read_text(encoding="utf-8"))["a.wav"]
    assert meta["mood"] == "科技" and abs(meta["bpm"] - 120) <= 1
    assert meta["beats"] and meta["downbeats"] and meta["beat_version"] == bt.VERSION
    assert meta["lufs"] is not None
    assert bt.analyze_library(root)["skipped"] == 1  # 算过的不重算
