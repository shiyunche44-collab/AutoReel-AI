"""片尾卡:卡片渲染、混音补静音、整条链路用真 ffmpeg 合出的成片音画长度对得上。"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from app import config
from app.pipeline import outro, render


@pytest.fixture()
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(config, "PROJECTS_DIR", tmp_path / "data" / "projects")
    yield tmp_path
    from app import db
    db._INITIALISED = False


def test_wrap_breaks_at_phrases_then_splits_evenly():
    assert outro._wrap("关注我,看更多真实数据解读", 10) == ["关注我", "看更多真实数据解读"]
    assert outro._wrap("一二三四五六七", 4) == ["一二三四", "五六七"]


def test_build_is_none_when_disabled(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "OUTRO_ENABLED", False)
    assert outro.build(tmp_path, (540, 960)) is None


def test_no_text_no_card(tmp_path):
    assert outro.render_card(tmp_path / "o.png", (540, 960), "", "") is None


def test_graph_pads_only_when_asked():
    plain, _ = render._build_audio_graph(True, True, 10.0, first_input=0)
    padded, _ = render._build_audio_graph(True, True, 10.0, first_input=0, pad_to=10.0)
    assert "apad" not in plain and "apad=whole_dur=10.000" in padded


@pytest.mark.skipif(not (config.FFMPEG_PATH and shutil.which(str(config.FFMPEG_PATH)) or
                         config.FFMPEG_PATH and Path(config.FFMPEG_PATH).is_file()),
                    reason="需要 ffmpeg")
def test_compose_with_outro_has_full_length_audio(store, monkeypatch):
    monkeypatch.setattr(config, "OUTRO_ENABLED", True)
    monkeypatch.setattr(config, "OUTRO_TEXT", "关注我")
    monkeypatch.setattr(config, "COVER_BRAND", "测试号")
    pid = "p1"
    pdir = config.project_dir(pid)
    pdir.mkdir(parents=True, exist_ok=True)
    ff = str(config.FFMPEG_PATH)
    shot = pdir / "shot.mp4"
    subprocess.run([ff, "-y", "-v", "error", "-f", "lavfi", "-i", "testsrc=s=540x960:d=4:r=24",
                    "-pix_fmt", "yuv420p", str(shot)], check=True)
    narr = pdir / "narr.wav"
    subprocess.run([ff, "-y", "-v", "error", "-f", "lavfi", "-i", "sine=f=300:d=4",
                    "-ar", "48000", str(narr)], check=True)
    card = outro.build(pdir, (540, 960), seconds=2.0)
    assert card is not None
    res = render.compose(pid, [shot, card[0]], narration_path=narr, size=(540, 960),
                         durations=[4.0, 2.0], tail_silent=True)
    assert res.ok, res.warnings
    assert res.duration == pytest.approx(6.0, abs=0.2)
    assert not [w for w in res.warnings if "音轨" in w], res.warnings
