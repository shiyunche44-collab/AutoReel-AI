"""虚拟主持人层:嘴型序列、隐藏区间、片尾不覆盖、位置避开按钮区。"""
from __future__ import annotations

import wave
from pathlib import Path

import numpy as np
import pytest

from app import config
from app.pipeline import aspect, host

FPS = 24


def test_mouth_closed_outside_cues():
    rms = [1000.0] * 48
    states = host.mouth_track(rms, [{"start": 0.5, "end": 1.0}], FPS)
    assert all(s in ("closed", "blink") for s in states[:12])
    assert all(s in ("closed", "blink") for s in states[24:])
    assert "open" in states[12:24]


def test_mouth_follows_volume():
    rms = [0.0] * 10 + [300.0] * 10 + [1000.0] * 10
    states = host.mouth_track(rms, [{"start": 0, "end": 30 / FPS}], FPS)
    assert states[25] == "open"
    assert states[15] == "half"
    assert states[5] in ("closed", "blink")


def test_min_hold_removes_single_frame_flicker():
    rms = [1000.0] * 10 + [0.0] + [1000.0] * 10
    states = host.mouth_track(rms, [{"start": 0, "end": 21 / FPS}], FPS)
    assert states[10] == "open"


def test_blink_only_while_closed():
    n = FPS * 20
    states = host.mouth_track([0.0] * n, [], FPS, seed=3)
    assert "blink" in states
    assert set(states) <= {"closed", "blink"}


def test_hide_spans_cover_fullframe_beats():
    mask = host.hide_spans(["broll", "concept", "broll"], [1.0, 1.0, 1.0], FPS, 3 * FPS)
    assert not mask[:FPS].any()
    assert mask[FPS:2 * FPS].all()
    assert not mask[2 * FPS:].any()


def test_portrait_placement_left_and_below_band():
    size = config.PORTRAIT_SIZE
    sh = host.sprite_height(size)
    x, y = host.placement(size, sh, int(sh * 0.75))
    g = aspect.portrait_grid(size)
    assert x + int(sh * 0.75) < size[0] * (1 - aspect.PORTRAIT_SAFE_RIGHT)
    assert y >= g.band.bottom
    assert y + sh <= g.data.bottom


def _wav(path: Path, seconds: float, rate: int = 16000) -> Path:
    t = np.arange(int(seconds * rate)) / rate
    pcm = (np.sin(2 * np.pi * 220 * t) * 12000).astype(np.int16)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm.tobytes())
    return path


@pytest.mark.skipif(not host.available(), reason="主持人素材未入库")
def test_build_covers_only_main_beats(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "HOST_ENABLED", True)
    wav = _wav(tmp_path / "n.wav", 3.0)
    nar = {"narration_path": str(wav), "cues": [{"start": 0.0, "end": 2.0}]}
    out = host.build(tmp_path, nar, config.PORTRAIT_SIZE, [1.0, 1.0, 1.0],
                     ["broll", "concept", "broll"], FPS)
    assert out is not None
    listing, x, y = out
    text = listing.read_text(encoding="utf-8")
    total = sum(float(line.split()[1]) for line in text.splitlines()
                if line.startswith("duration"))
    assert total == pytest.approx(3.0, abs=0.05)
    assert "hidden.png" in text


def test_build_disabled(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "HOST_ENABLED", False)
    assert host.build(tmp_path, {}, config.PORTRAIT_SIZE, [1.0], ["broll"], FPS) is None
