"""竖屏自动重构图:16:9 素材放进竖屏素材带会裁掉左右两边,裁切窗口对准画面主体。

参照 Opus Clip / CapCut 的 auto-reframe,但不跑检测模型:抽一帧,按列统计边缘能量
(车身轮廓、车灯、轮毂这些高频细节),取能量重心作为主体的水平位置。
天空、路面、虚化背景边缘少,权重自然低。画面很平(边缘太少)或主体铺满全幅时返回居中。
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
from PIL import Image, ImageEnhance, ImageFilter

#: 分析宽度:够看出主体在哪,又足够快
ANALYSE_W = 160
#: 四边各忽略这个比例:黑边、台标、烧录字幕常贴在边上,会把重心拉偏
EDGE_IGNORE = 0.05
#: 焦点的取值范围:太靠边时裁切窗口会顶到画面外,素材带里只剩半辆车也不好看
FOCUS_MIN, FOCUS_MAX = 0.2, 0.8
#: 能量分布的有效宽度超过这个比例,就算"主体铺满",不偏移
SPREAD_FULL = 0.7


def focus_x(image: Image.Image) -> float:
    """主体的水平位置(0 = 最左,0.5 = 居中,1 = 最右)。"""
    g = image.convert("L")
    w, h = g.size
    if w < 8 or h < 8:
        return 0.5
    g = g.resize((ANALYSE_W, max(8, round(h * ANALYSE_W / w))))
    a = np.asarray(g, dtype=np.float32)
    gx = np.abs(np.diff(a, axis=1))[:-1, :]
    gy = np.abs(np.diff(a, axis=0))[:, :-1]
    mag = gx + gy
    rows, cols = mag.shape
    m_r, m_c = int(rows * EDGE_IGNORE), int(cols * EDGE_IGNORE)
    mag[:m_r, :] = 0
    mag[rows - m_r:, :] = 0
    mag[:, :m_c] = 0
    mag[:, cols - m_c:] = 0
    col = mag.sum(axis=0)
    # 压掉底噪:纹理均匀的背景(草地、路面)每列都有一点能量,不该算主体
    col = np.clip(col - np.median(col[m_c:cols - m_c] if cols > 2 * m_c else col), 0, None)
    total = float(col.sum())
    if total <= 1e-6 or total < mag.size * 0.5:
        return 0.5
    xs = (np.arange(cols) + 0.5) / cols
    cx = float((col * xs).sum() / total)
    # 有效宽度:能量 10%–90% 分位之间的跨度
    cum = np.cumsum(col) / total
    lo, hi = xs[int(np.searchsorted(cum, 0.1))], xs[min(cols - 1, int(np.searchsorted(cum, 0.9)))]
    if hi - lo >= SPREAD_FULL:
        return 0.5
    return round(min(FOCUS_MAX, max(FOCUS_MIN, cx)), 3)


def analyse(frame: Path, backdrop: Path | None = None) -> float:
    """读抽好的一帧:返回焦点;给了 `backdrop` 就顺手写一张缩小、模糊、压暗的竖屏底图。"""
    with Image.open(frame) as im:
        im = im.convert("RGB")
        fx = focus_x(im)
        if backdrop is not None:
            small = im.resize((360, max(1, round(im.height * 360 / im.width))))
            small = small.filter(ImageFilter.GaussianBlur(14))
            small = ImageEnhance.Brightness(small).enhance(0.72)
            small.save(backdrop, quality=80)
    return fx


def aspect_of(frame: Path) -> float:
    """抽出那一帧的宽高比(原片的,抽帧只做了等比缩放)。"""
    with Image.open(frame) as im:
        return round(im.width / max(1, im.height), 4)
