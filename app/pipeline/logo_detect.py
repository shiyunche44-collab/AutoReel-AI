"""静态台标 / 角标检测:不调视觉模型,只看"哪里的边缘在每个镜头里都在"。

原理:台标贴在画面固定位置,画面内容一直在变。把一个源里不同镜头的帧叠起来,
每个像素算"有几成的帧在这里有边缘"—— 内容的边缘随镜头走,只有台标(和黑边的
分界线)的边缘一直在原地。黑边分界线是贯穿整行 / 整列的直线,单独剔掉。

只看四个角(第三方账号的台标几乎都贴角)。半透明的白色台标亮度随底下画面变,
但它的轮廓位置不变,所以按边缘而不是按亮度判。
"""
from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Sequence

#: 检测用的分辨率(灰度)
SIZE = (384, 216)
#: 每个源最多取多少帧
SAMPLE_FRAMES = 24
#: 去掉几乎同一画面的帧后,至少剩这么多张才下结论(少了分不清台标和静止镜头)
MIN_DISTINCT = 6
#: 两帧平均绝对差小于它算同一画面
SAME_SCENE_DIFF = 10.0
#: 灰度梯度幅值超过它算边缘
EDGE_GRAD = 18.0
#: 一个像素在至少这么多成的帧里是边缘,算"固定边缘"
PERSIST = 0.8
#: 一个角区域的范围(占宽 / 高的比例)
CORNER_W, CORNER_H = 0.32, 0.26
#: 一个角里固定边缘像素至少这么多(占整帧像素的比例),才算有台标
MIN_LOGO_AREA = 0.0006
#: 一行 / 一列里固定边缘超过这个比例,当成黑边分界线或画框,不算台标
LINE_FRACTION = 0.5
#: 角以外的区域里固定边缘占比超过它:机位没动过(单机位口播 vlog),背景纹理也"不动",
#: 分不清台标 → 不下结论。实测带真台标的源 ≤ 0.01,阳台口播 0.07
STATIC_SCENE_DENSITY = 0.02
#: 台标框面积上限(占整帧比例)与框内固定边缘的最低密度。实测真台标面积 ≤ 0.013、
#: 密度 ≥ 0.16;机位不动的沙滩 / 阳台背景框出来面积 0.03–0.07、密度 ≤ 0.15
MAX_LOGO_AREA = 0.025
MIN_LOGO_FILL = 0.12
#: 裁切时在台标框外再多收的余量(占宽高比例)
CROP_MARGIN = 0.015
#: 四边收进超过这个比例才能裁掉的台标算裁不掉(放大 1.4 倍以上画面就糊了)
MAX_CROP = 0.3


def sample_frames(videos: Sequence[str | Path], total: int = SAMPLE_FRAMES):
    """从一个源的几个窗文件里均匀取帧,返回 (N, H, W) float32 灰度数组(可能 N=0)。"""
    import numpy as np

    from app.providers import clips as clips_mod
    from app.providers.clips import probe_duration

    w, h = SIZE
    videos = [Path(v) for v in videos if Path(v).is_file()]
    if not videos:
        return np.zeros((0, h, w), dtype=np.float32)
    per = max(2, -(-total // len(videos)))
    frames = []
    for v in videos:
        dur = probe_duration(v)
        if dur <= 0:
            continue
        # 头尾各让 5%,避开片头黑场和片尾卡
        lo, span = dur * 0.05, dur * 0.9
        try:
            proc = subprocess.run(
                [clips_mod._ffmpeg(), "-hide_banner", "-nostdin", "-v", "error",
                 "-ss", f"{lo:.3f}", "-t", f"{span:.3f}", "-i", str(v), "-an",
                 "-vf", f"fps={per / span:.5f},scale={w}:{h}", "-frames:v", str(per),
                 "-f", "rawvideo", "-pix_fmt", "gray", "-"],
                capture_output=True, timeout=300)
        except (OSError, subprocess.TimeoutExpired):
            continue
        buf = proc.stdout or b""
        n = len(buf) // (w * h)
        if n:
            frames.append(np.frombuffer(buf[:n * w * h], dtype=np.uint8).reshape(n, h, w))
    if not frames:
        return np.zeros((0, h, w), dtype=np.float32)
    return np.concatenate(frames).astype(np.float32)


def distinct(frames):
    """去掉和已留下的某帧几乎一样的帧(静止镜头连取几帧,会把整个画面误当成台标)。"""
    kept = []
    for f in frames:
        if all(abs(f - k).mean() >= SAME_SCENE_DIFF for k in kept):
            kept.append(f)
    return kept


def persistent_edges(frames):
    """(H, W) 布尔:在至少 PERSIST 的帧里都是边缘的像素,剔掉贯穿整行 / 整列的直线。"""
    import numpy as np

    stack = np.stack(frames)
    gy, gx = np.gradient(stack, axis=(1, 2))
    edge = (np.abs(gx) + np.abs(gy)) > EDGE_GRAD
    mask = edge.mean(axis=0) >= PERSIST
    mask[:2, :] = mask[-2:, :] = False
    mask[:, :2] = mask[:, -2:] = False
    h, w = mask.shape
    rows = mask.mean(axis=1) > LINE_FRACTION
    cols = mask.mean(axis=0) > LINE_FRACTION
    # 分界线是 1–2 像素粗的线:连同上下 / 左右各一像素一起去掉
    for r in np.flatnonzero(rows):
        mask[max(0, r - 1):r + 2, :] = False
    for c in np.flatnonzero(cols):
        mask[:, max(0, c - 1):c + 2] = False
    return mask


def find_logos(frames) -> list[list[float]] | None:
    """在一组帧里找角标。返回归一化框 [x0, y0, x1, y1] 的列表(空 = 干净);
    不同画面太少、下不了结论返回 None。"""
    import numpy as np

    frames = distinct(list(frames))
    if len(frames) < MIN_DISTINCT:
        return None
    mask = persistent_edges(frames)
    h, w = mask.shape
    cw, ch = int(w * CORNER_W), int(h * CORNER_H)
    corners = ((0, 0), (w - cw, 0), (0, h - ch), (w - cw, h - ch))
    outside = mask.copy()
    for x0, y0 in corners:
        outside[y0:y0 + ch, x0:x0 + cw] = False
    if outside.sum() / max(1, mask.size - 4 * cw * ch) > STATIC_SCENE_DENSITY:
        return None
    boxes: list[list[float]] = []
    for x0, y0 in corners:
        sub = mask[y0:y0 + ch, x0:x0 + cw]
        if sub.sum() < MIN_LOGO_AREA * w * h:
            continue
        ys, xs = np.nonzero(sub)
        # 用分位数而不是最值:零星的噪点不该把框撑大
        bx0, bx1 = np.percentile(xs, [2, 98]) + x0
        by0, by1 = np.percentile(ys, [2, 98]) + y0
        bx1, by1 = bx1 + 1, by1 + 1
        area = (bx1 - bx0) * (by1 - by0)
        fill = sub[int(by0 - y0):int(by1 - y0), int(bx0 - x0):int(bx1 - x0)].mean()
        if area > MAX_LOGO_AREA * w * h:
            return None            # 一大片稀疏的"不动"边缘:机位没动,不是台标
        if fill < MIN_LOGO_FILL:
            continue
        boxes.append([round(float(bx0) / w, 4), round(float(by0) / h, 4),
                      round(float(bx1) / w, 4), round(float(by1) / h, 4)])
    return boxes


def crop_for(boxes: Sequence[Sequence[float]]) -> float | None:
    """四边一起往里收多少(和 explainer._fit_filter 的 crop_edge 同义:总比例,
    每边收一半)才能把这些角标都裁出画。台标离最近的横边或竖边任一方向出画即可。
    裁不掉(要收得太多)返回 None。"""
    need = 0.0
    for x0, y0, x1, y1 in boxes:
        dx = min(x1, 1 - x0)       # 到左右最近一边的深度
        dy = min(y1, 1 - y0)       # 到上下最近一边的深度
        need = max(need, 2 * (min(dx, dy) + CROP_MARGIN))
    if need > MAX_CROP:
        return None
    return round(need, 4)
