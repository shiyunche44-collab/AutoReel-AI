"""片头品牌条:徽标渲染、关着不出、真 ffmpeg 叠上去不改时长。"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from app import config
from app.pipeline import intro, render

HAS_FF = bool(config.FFMPEG_PATH and (shutil.which(str(config.FFMPEG_PATH))
                                      or Path(config.FFMPEG_PATH).is_file()))


def test_disabled_or_no_brand_gives_nothing(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "INTRO_ENABLED", False)
    assert intro.build(tmp_path, (540, 960)) is None
    monkeypatch.setattr(config, "INTRO_ENABLED", True)
    monkeypatch.setattr(config, "COVER_BRAND", "")
    assert intro.build(tmp_path, (540, 960)) is None


def test_badge_is_transparent_full_frame_png(monkeypatch, tmp_path):
    from PIL import Image
    monkeypatch.setattr(config, "INTRO_ENABLED", True)
    monkeypatch.setattr(config, "COVER_BRAND", "测试号")
    png, secs = intro.build(tmp_path, (540, 960))
    img = Image.open(png)
    assert img.size == (540, 960) and img.mode == "RGBA"
    assert img.getpixel((270, 480))[3] == 0          # 画面中央是透明的
    assert img.getbbox() is not None and secs == config.INTRO_SECONDS


@pytest.mark.skipif(not HAS_FF, reason="需要 ffmpeg")
def test_compose_with_intro_keeps_duration(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(config, "PROJECTS_DIR", tmp_path / "data" / "projects")
    monkeypatch.setattr(config, "COVER_BRAND", "测试号")
    monkeypatch.setattr(config, "INTRO_ENABLED", True)
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
    badge = intro.build(pdir, (540, 960))
    res = render.compose(pid, [shot], narration_path=narr, size=(540, 960), durations=[4.0],
                         intro_path=badge[0], intro_seconds=badge[1])
    assert res.ok, res.warnings
    assert res.duration == pytest.approx(4.0, abs=0.2)
    assert not [w for w in res.warnings if "叠" in w or "失败" in w], res.warnings
    # 前 1 秒有标、最后 1 秒没有:两帧不同的左上角区域
    def corner(t: float) -> bytes:
        return subprocess.run(
            [ff, "-v", "error", "-ss", str(t), "-i", str(res.output_path), "-frames:v", "1",
             "-vf", "crop=200:60:27:96", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
            capture_output=True, check=True).stdout
    assert corner(1.0) != corner(3.5)
