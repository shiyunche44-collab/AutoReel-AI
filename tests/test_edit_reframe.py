"""竖屏自动重构图:按边缘能量找主体的水平位置。"""
from __future__ import annotations

import numpy as np
from PIL import Image, ImageDraw

from app.pipeline.edit import reframe


def _scene(car_x: float | None, *, w=1920, h=1080, noise=0.0, logo=False, seed=0) -> Image.Image:
    """渐变天空 + 灰路面;car_x 处画一台"车"(有轮廓、车窗、两个轮子)。"""
    rng = np.random.default_rng(seed)
    a = np.zeros((h, w, 3), dtype=np.float32)
    a[: h // 2] = np.linspace(200, 150, h // 2)[:, None, None]
    a[h // 2:] = 90
    if noise:
        a += rng.normal(0, noise, a.shape)
    im = Image.fromarray(np.clip(a, 0, 255).astype(np.uint8))
    d = ImageDraw.Draw(im)
    if car_x is not None:
        cx, cw = int(car_x * w), int(w * 0.28)
        x0, y0 = cx - cw // 2, int(h * 0.45)
        d.rectangle([x0, y0, x0 + cw, y0 + int(h * 0.18)], fill=(230, 230, 235), outline=(20, 20, 20), width=6)
        d.rectangle([x0 + cw // 5, y0 - int(h * 0.09), x0 + cw * 4 // 5, y0], fill=(40, 60, 80), outline=(10, 10, 10), width=5)
        for wx in (x0 + cw // 5, x0 + cw * 4 // 5):
            d.ellipse([wx - 60, y0 + int(h * 0.13), wx + 60, y0 + int(h * 0.13) + 120], fill=(15, 15, 15), outline=(200, 200, 200), width=8)
    if logo:
        d.rectangle([w - 160, 20, w - 30, 90], fill=(255, 255, 255))
        d.text((w - 150, 30), "LOGO", fill=(0, 0, 0))
    return im


def test_subject_on_left_or_right_shifts_focus():
    assert reframe.focus_x(_scene(0.25)) < 0.35
    assert reframe.focus_x(_scene(0.75)) > 0.65
    assert abs(reframe.focus_x(_scene(0.5)) - 0.5) < 0.06


def test_flat_or_empty_frame_stays_centered():
    assert reframe.focus_x(Image.new("RGB", (1920, 1080), (120, 130, 140))) == 0.5
    assert reframe.focus_x(_scene(None)) == 0.5


def test_focus_is_clamped_and_corner_logo_ignored():
    assert reframe.focus_x(_scene(0.04)) >= reframe.FOCUS_MIN
    assert abs(reframe.focus_x(_scene(0.5, logo=True)) - reframe.focus_x(_scene(0.5))) < 0.03


def test_texture_everywhere_counts_as_full_frame():
    rng = np.random.default_rng(1)
    im = Image.fromarray(rng.integers(0, 255, (1080, 1920, 3), dtype=np.uint8))
    assert reframe.focus_x(im) == 0.5


def test_analyse_writes_backdrop(tmp_path):
    f = tmp_path / "f.jpg"
    _scene(0.3).resize((480, 270)).save(f)
    bd = tmp_path / "bd.jpg"
    fx = reframe.analyse(f, bd)
    assert fx < 0.4 and bd.is_file()
    with Image.open(bd) as b:
        assert b.width == 360
