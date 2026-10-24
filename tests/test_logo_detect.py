"""静态角标检测:合成"内容在变、角标不动"的帧。全部离线。"""
from __future__ import annotations

import numpy as np
import pytest

from app.pipeline import library as lib_mod
from app.pipeline import logo_detect
from app.pipeline.library import Item

W, H = logo_detect.SIZE


def _frames(n: int = 12, *, logo: tuple[int, int, int, int] | None = None, seed: int = 0,
            letterbox: int = 0, same: bool = False):
    rng = np.random.default_rng(seed)
    out = []
    base = None
    for _ in range(n):
        if same and base is not None:
            out.append(base.copy())
            continue
        f = np.zeros((H, W), dtype=np.float32) + rng.uniform(40, 200)
        # 每帧不同位置的几个矩形 = 变化的画面内容
        for _ in range(6):
            x, y = rng.integers(0, W - 60), rng.integers(0, H - 40)
            f[y:y + rng.integers(10, 40), x:x + rng.integers(20, 60)] = rng.uniform(0, 255)
        if letterbox:
            f[:letterbox] = 0
            f[-letterbox:] = 0
        if logo:
            x0, y0, x1, y1 = logo
            f[y0:y1, x0:x1] = 255
            f[y0 + 3:y1 - 3, x0 + 3:x1 - 3] = 30   # 空心框:有轮廓
        out.append(f)
        base = f
    return np.stack(out)


def test_finds_top_right_logo():
    x0, y0, x1, y1 = W - 70, 6, W - 10, 22
    boxes = logo_detect.find_logos(_frames(logo=(x0, y0, x1, y1)))
    assert boxes and len(boxes) == 1
    bx0, by0, bx1, by1 = boxes[0]
    assert bx0 == pytest.approx(x0 / W, abs=0.02) and bx1 == pytest.approx(x1 / W, abs=0.02)
    assert by0 == pytest.approx(y0 / H, abs=0.02) and by1 == pytest.approx(y1 / H, abs=0.02)
    crop = logo_detect.crop_for(boxes)
    # 离上边比离右边近:收上边就出画
    assert crop == pytest.approx(2 * (by1 + logo_detect.CROP_MARGIN), abs=1e-3)


def test_clean_source_and_letterbox_lines_are_not_logos():
    assert logo_detect.find_logos(_frames()) == []
    assert logo_detect.find_logos(_frames(letterbox=20)) == []


def test_static_shot_is_inconclusive():
    assert logo_detect.find_logos(_frames(same=True)) is None


def test_logo_too_deep_cannot_be_cropped():
    assert logo_detect.crop_for([[0.6, 0.3, 0.9, 0.5]]) is None
    assert logo_detect.crop_for([]) == 0.0


def test_crop_edge_follows_detection():
    v = dict(kind="video", path="x.mp4")
    assert lib_mod._crop_edge(Item(id="a", official=False, **v)) == lib_mod.EDGE_CROP
    assert lib_mod._crop_edge(Item(id="b", official=True, **v)) == 0.0
    assert lib_mod._crop_edge(Item(id="c", official=True, logo_crop=0.2, **v)) == 0.2
    assert lib_mod._crop_edge(Item(id="d", official=False, logo_crop=0.0, **v)) \
        == lib_mod.THIRD_PARTY_SAFE_CROP
    assert lib_mod._crop_edge(Item(id="e", kind="still", path="x.jpg", logo_crop=0.2)) == 0.0


def test_check_logos_skips_uncroppable_third_party(monkeypatch):
    lib = lib_mod.Library(subject="小米YU7")
    third = lib_mod.Source(url="u1", id="s1", status="done")
    off = lib_mod.Source(url="u2", id="s2", status="done", official=True)
    lib.sources = [third, off]
    lib.items = [Item(id="a", kind="video", path="a.mp4", source="s1"),
                 Item(id="b", kind="video", path="b.mp4", source="s2", official=True)]
    monkeypatch.setattr(logo_detect, "sample_frames", lambda paths: None)
    monkeypatch.setattr(logo_detect, "find_logos", lambda frames: [[0.4, 0.4, 0.6, 0.6]])
    lib_mod.check_logos(lib, third)
    lib_mod.check_logos(lib, off)
    a, b = lib.items
    assert a.skipped == "logo_uncroppable" and third.logo_checked
    assert not b.skipped and b.logo_crop == 0.0


def test_fixed_camera_vlog_is_inconclusive():
    """机位不动、只有人在动:背景纹理每帧都在原地,不能当成台标。"""
    rng = np.random.default_rng(3)
    bg = np.zeros((H, W), dtype=np.float32) + 120
    for _ in range(80):  # 满屏的固定纹理(栏杆、树枝)
        x, y = rng.integers(0, W - 20), rng.integers(0, H - 20)
        bg[y:y + rng.integers(3, 20), x:x + rng.integers(3, 20)] = rng.uniform(0, 255)
    frames = []
    for k in range(12):
        f = bg.copy()
        f[60:200, 40 + k * 22:120 + k * 22] = 30 + k * 15   # 走动的人
        frames.append(f)
    assert logo_detect.find_logos(np.stack(frames)) is None
