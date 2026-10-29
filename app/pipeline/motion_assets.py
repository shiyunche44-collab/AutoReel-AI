"""概念动画用的车图:从素材库挑一张白底棚拍官图,抠出车身,估出轮位与地线。

概念动画要"讲这台车",而不是画一个通用电池图标 —— 模板拿到抠好的车身 PNG 和几何信息,
就能把电池包放在两轮之间的底板上、把引线标注落在车身上。

只用 PIL + numpy:白底棚拍图的背景和车身反差大,从四边做洪水填充就能分开,
车窗被车身围住不会被填掉。抠不干净(不是白底、车贴边、面积不对)就返回 None,模板走无车图退路。
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Sequence

import numpy as np
from PIL import Image, ImageDraw, ImageFilter

from .. import config
from . import library

#: 处理尺寸:长边缩到这么宽再抠,够清晰,洪水填充也快
WORK_WIDTH = 1600
#: 背景候选:亮度 ≥ 这个、色度 ≤ CHROMA 的像素(白底棚拍的纸背景,允许轻微渐变)
BG_LUMA = 205
BG_CHROMA = 20
#: 四周一圈的平均亮度 ≥ 这个才算白底图
BORDER_LUMA = 228
#: 车身占画面面积的合理范围
AREA_RANGE = (0.08, 0.75)
#: 外接框四周留白(相对车宽),给模板的辉光和倒影留位置
MARGIN = 0.02
#: 车顶轮廓采样的列数
PROFILE_SAMPLES = 32
#: 缓存版本:几何信息的字段变了就加一,旧缓存自动作废
CACHE_VERSION = 2


@dataclass
class Cutout:
    """抠好的车身。坐标都按 PNG 自身尺寸归一化到 0–1。"""
    png: str
    width: int
    height: int
    #: 两个车轮中心的 x(左、右)
    wheels: tuple[float, float]
    #: 轮胎着地的 y
    floor: float
    #: 车顶的 y(车身最高处)
    roof: float
    #: 原图的 item id,便于追溯
    item_id: str = ""
    #: 车头朝向(left / right),车往前开时按它决定要不要镜像
    facing: str = "right"
    #: 车身左右边的 x(去掉留白)
    body: tuple[float, float] = (0.0, 1.0)
    #: 车顶轮廓:沿车身等距 PROFILE_SAMPLES 列,每列最上面一个车身像素的 y。
    #: 模板用它找激光雷达位置(车顶前沿)、画车高尺寸线
    profile: tuple[float, ...] = ()
    #: 线稿 PNG(车身轮廓 + 内部结构线,透明底白线),X 光效果用;空 = 没有
    edges: str = ""

    def geometry(self) -> dict:
        d = asdict(self)
        d.pop("png")
        d.pop("edges")
        return d


def cutout_dir() -> Path:
    return config.DATA_DIR / "_cache" / "motion" / "cutouts"


def _sha(path: Path) -> str:
    return hashlib.sha1(path.read_bytes()).hexdigest()[:20]


def is_studio(im: Image.Image) -> bool:
    """白底棚拍:四周一圈像素又亮又匀。"""
    g = np.asarray(im.convert("L"), dtype=np.float32)
    h, w = g.shape
    b = max(4, min(h, w) // 40)
    ring = np.concatenate([g[:b].ravel(), g[-b:].ravel(), g[:, :b].ravel(), g[:, -b:].ravel()])
    return float(ring.mean()) >= BORDER_LUMA and float(ring.std()) <= 18


def _foreground(rgb: np.ndarray) -> np.ndarray:
    """车身蒙版(True = 车):背景候选里和四边连通的部分是背景,其余是车。"""
    r, g, b = (rgb[..., k].astype(np.int32) for k in range(3))
    luma = (r * 299 + g * 587 + b * 114) // 1000
    chroma = np.maximum(np.maximum(r, g), b) - np.minimum(np.minimum(r, g), b)
    cand = (luma >= BG_LUMA) & (chroma <= BG_CHROMA)
    h, w = cand.shape
    # copy():fromarray 出来的图和数组共享只读缓冲,floodfill 写不进去
    m = Image.fromarray(np.where(cand, 255, 0).astype(np.uint8), "L").copy()
    seeds = [(x, 0) for x in range(0, w, 8)] + [(x, h - 1) for x in range(0, w, 8)] + \
            [(0, y) for y in range(0, h, 8)] + [(w - 1, y) for y in range(0, h, 8)]
    for s in seeds:
        if m.getpixel(s) == 255:
            ImageDraw.floodfill(m, s, 128, thresh=0)
    bg = np.asarray(m) == 128
    return ~bg


def _strip_shadow(fg: np.ndarray, rgb: np.ndarray) -> np.ndarray:
    """车底的接地阴影是浅灰、扁长的一条,会被当成车身。从下往上剥掉"宽而浅"的行。"""
    luma = rgb.astype(np.float32) @ np.array([0.299, 0.587, 0.114], dtype=np.float32)
    rows = np.where(fg.any(axis=1))[0]
    if rows.size == 0:
        return fg
    out = fg.copy()
    for y in rows[::-1]:
        sel = out[y]
        if not sel.any():
            continue
        # 阴影行:前景像素大多是中灰(不是轮胎的深黑,也不是车漆)
        vals = luma[y][sel]
        if np.median(vals) > 120 and vals.min() > 60:
            out[y] = False
        else:
            break
    return out


def line_art(rgba: Image.Image) -> Image.Image:
    """车身线稿:亮度梯度(车窗、轮毂、腰线)+ 外轮廓,透明底白线。颜色交给模板用 CSS 染。"""
    g = np.asarray(rgba.convert("L"), dtype=np.float32) / 255.0
    a = np.asarray(rgba.getchannel("A"), dtype=np.float32) / 255.0

    def grad(x: np.ndarray) -> np.ndarray:
        gx = np.zeros_like(x)
        gy = np.zeros_like(x)
        gx[:, 1:-1] = x[:, 2:] - x[:, :-2]
        gy[1:-1, :] = x[2:, :] - x[:-2, :]
        return np.hypot(gx, gy)

    inner = grad(g) * a
    inner = np.clip((inner - 0.05) * 5.0, 0, 1) * 0.75
    outline = np.clip(grad(a) * 2.5, 0, 1)
    line = np.maximum(inner, outline)
    alpha = Image.fromarray((line * 255).astype(np.uint8), "L").filter(ImageFilter.GaussianBlur(0.6))
    out = Image.new("RGBA", rgba.size, (255, 255, 255, 0))
    out.putalpha(alpha)
    return out


def _wheels(fg: np.ndarray, top: int, bottom: int, left: int, right: int) -> tuple[float, float]:
    """在车身最下面一条带里找两个轮胎:只有轮胎伸到最底下,按列统计前景能看出两个驼峰。"""
    hgt = bottom - top
    band = fg[bottom - max(3, int(hgt * 0.10)):bottom, left:right]
    cols = band.mean(axis=0)
    w = right - left
    guess = (0.18, 0.82)
    if w < 20:
        return guess
    on = cols > 0.5
    runs, start = [], None
    for x, v in enumerate(np.append(on, False)):
        if v and start is None:
            start = x
        elif not v and start is not None:
            runs.append((start, x))
            start = None
    runs = [r for r in runs if (r[1] - r[0]) >= w * 0.05]
    lefts = [r for r in runs if (r[0] + r[1]) / 2 < w / 2]
    rights = [r for r in runs if (r[0] + r[1]) / 2 >= w / 2]
    if not lefts or not rights:
        return guess
    lw = max(lefts, key=lambda r: r[1] - r[0])
    rw = max(rights, key=lambda r: r[1] - r[0])
    a, b = float((lw[0] + lw[1]) / 2 / w), float((rw[0] + rw[1]) / 2 / w)
    if not (0.05 < a < 0.4 and 0.6 < b < 0.95):
        return guess
    return round(a, 4), round(b, 4)


def _facing(fg: np.ndarray, top: int, bottom: int, left: int, right: int) -> str:
    """侧面图的车头朝向:离两端 8% 处量车身高度,引擎盖那头矮。"""
    w = right - left

    def height(x: int) -> int:
        col = np.where(fg[top:bottom, x])[0]
        return int(bottom - top - col[0]) if col.size else 0

    lh, rh = height(left + int(w * 0.08)), height(right - 1 - int(w * 0.08))
    return "left" if lh < rh else "right"


_ml_session = None


def _ml_foreground(im: Image.Image) -> np.ndarray | None:
    """用 rembg(isnet-general-use)从实拍图里抠出车身蒙版;没装 rembg 返回 None。"""
    global _ml_session
    try:
        from rembg import new_session, remove
    except ImportError:
        return None
    if _ml_session is None:
        _ml_session = new_session("isnet-general-use")
    out = remove(im, session=_ml_session, only_mask=True)
    return np.asarray(out.convert("L")) >= 128


def cutout(path: str | Path, item_id: str = "", ml: bool = False) -> Cutout | None:
    """抠一张白底图。不是白底、车贴边或面积离谱时返回 None。结果按原图内容缓存。

    `ml=True`:实拍图(非白底)改用抠图模型出蒙版,缓存单独一份(`_ml`)。"""
    path = Path(path)
    if not path.is_file():
        return None
    key = f"{_sha(path)}_v{CACHE_VERSION}" + ("_ml" if ml else "")
    d = cutout_dir()
    png, edges, meta = d / f"{key}.png", d / f"{key}_edges.png", d / f"{key}.json"
    if png.is_file() and meta.is_file():
        info = json.loads(meta.read_text(encoding="utf-8"))
        if not info:
            return None
        for k in ("wheels", "body", "profile"):
            info[k] = tuple(info[k])  # JSON 只有数组
        return Cutout(png=str(png), edges=str(edges) if edges.is_file() else "", **info)

    def remember(c: Cutout | None) -> Cutout | None:
        d.mkdir(parents=True, exist_ok=True)
        meta.write_text(json.dumps(c.geometry() if c else {}, ensure_ascii=False), encoding="utf-8")
        return c

    try:
        im = Image.open(path).convert("RGB")
    except OSError:
        return None
    if im.width > WORK_WIDTH:
        im = im.resize((WORK_WIDTH, round(im.height * WORK_WIDTH / im.width)), Image.LANCZOS)
    if ml:
        fg = _ml_foreground(im)
        if fg is None:
            return None  # 没装抠图模型:不记缓存,装上之后还能再试
    elif not is_studio(im):
        return remember(None)
    else:
        rgb = np.asarray(im)
        fg = _strip_shadow(_foreground(rgb), rgb)
    h, w = fg.shape
    area = fg.mean()
    ys, xs = np.where(fg)
    if xs.size == 0 or not (AREA_RANGE[0] <= area <= AREA_RANGE[1]):
        return remember(None)
    top, bottom, left, right = ys.min(), ys.max() + 1, xs.min(), xs.max() + 1
    edge = 3
    if left <= edge or right >= w - edge or top <= edge or bottom >= h - edge:
        return remember(None)  # 车被画面截断,做不了完整车身
    wheels = _wheels(fg, top, bottom, left, right)
    facing = _facing(fg, top, bottom, left, right)

    # 羽化边缘,裁到外接框加一点留白
    alpha = Image.fromarray((fg * 255).astype(np.uint8), "L").filter(ImageFilter.GaussianBlur(1.2))
    rgba = im.copy()
    rgba.putalpha(alpha)
    pad = int((right - left) * MARGIN)
    box = (max(0, left - pad), max(0, top - pad), min(w, right + pad), min(h, bottom + pad))
    crop = rgba.crop(box)
    d.mkdir(parents=True, exist_ok=True)
    crop.save(png)
    line_art(crop).save(edges)
    cw, ch = crop.size
    cols = np.linspace(left, right - 1, PROFILE_SAMPLES).astype(int)
    profile = []
    for x in cols:
        hit = np.where(fg[top:bottom, x])[0]
        profile.append(round(float(top + hit[0] - box[1]) / ch, 4) if hit.size else round(float(bottom - box[1]) / ch, 4))
    c = Cutout(png=str(png), edges=str(edges), width=cw, height=ch,
               wheels=(round(float(left - box[0] + wheels[0] * (right - left)) / cw, 4),
                       round(float(left - box[0] + wheels[1] * (right - left)) / cw, 4)),
               floor=round(float(bottom - box[1]) / ch, 4), roof=round(float(top - box[1]) / ch, 4),
               item_id=item_id, facing=facing,
               body=(round(float(left - box[0]) / cw, 4), round(float(right - box[0]) / cw, 4)),
               profile=tuple(profile))
    return remember(c)


def hero_still(subject: str, views: Sequence[str]) -> Cutout | None:
    """按视角优先级挑一张能抠的白底官图。先官方、再按面积大的(分辨率高);都抠不了返回 None。"""
    try:
        lib = library.Library.load(subject)
    except Exception:
        return None
    for view in views:
        cands = [i for i in lib.items
                 if i.kind == "still" and i.view == view and i.usable and Path(i.path).is_file()]
        cands.sort(key=lambda i: (not i.official, -Path(i.path).stat().st_size))
        for i in cands[:8]:
            c = cutout(i.path, item_id=i.id)
            if c is not None:
                return c
    return None
