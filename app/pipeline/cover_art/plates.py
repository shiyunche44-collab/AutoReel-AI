"""车牌上别家标识的检测与抹除。"""
from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from .tone import log


def _blobs(mask: Any) -> list[tuple[int, int, int, int, float]]:
    """二值图(numpy bool)里的连通块:先闭运算把字的笔画补上,再逐块给外框。

    返回 [(x0, y0, x1, y1, fill)],fill = 这一块占自己外框的比例。
    有 scipy 就用它标注(一张 1920 的图几十毫秒);没有才退回 PIL 的 floodfill ——
    那是纯 Python 的,碰上大片天空一张图要跑几分钟,背景图以前就是因为这个没法抹白牌。
    """
    import numpy as np
    from PIL import Image, ImageDraw, ImageFilter

    # 大图先抽稀到 1600 宽以内再找:图库原图有 3840 宽的,车牌上的字一笔就十几像素,
    # 5 像素的闭运算补不上,整块牌子被字掏成筛子(实跑:汽车之家的白牌一张都没认出来)
    n = max(1, -(-mask.shape[1] // 1600))
    if n > 1:
        return [(x0 * n, y0 * n, x1 * n, y1 * n, fill) for x0, y0, x1, y1, fill in _blobs(mask[::n, ::n])]
    k = 5 if mask.shape[1] <= 800 else 9
    m = Image.fromarray(np.where(mask, 255, 0).astype(np.uint8), "L")
    m = m.filter(ImageFilter.MaxFilter(k)).filter(ImageFilter.MinFilter(k)).copy()
    out: list[tuple[int, int, int, int, float]] = []
    try:
        from scipy import ndimage
    except ImportError:
        ndimage = None
    if ndimage is not None:
        lab, n = ndimage.label(np.asarray(m) == 255)
        for i, sl in enumerate(ndimage.find_objects(lab), start=1):
            if sl is None:
                continue
            ysl, xsl = sl
            out.append((xsl.start, ysl.start, xsl.stop, ysl.stop, float((lab[sl] == i).mean())))
        return out
    ys, xs = np.nonzero(np.asarray(m)[::3, ::3] == 255)
    for y, x in zip(ys * 3, xs * 3, strict=True):
        if m.getpixel((int(x), int(y))) != 255:
            continue
        ImageDraw.floodfill(m, (int(x), int(y)), 128, thresh=0)
        comp = np.asarray(m) == 128
        m.paste(0, mask=Image.fromarray((comp * 255).astype(np.uint8)))
        yy, xx = np.nonzero(comp)
        x0, x1, y0, y1 = int(xx.min()), int(xx.max()) + 1, int(yy.min()), int(yy.max()) + 1
        out.append((x0, y0, x1, y1, float(comp[y0:y1, x0:x1].mean())))
    return out


def find_plates(im: Any) -> list[tuple[int, int, int, int]]:
    """找车身上印着别家标识的白色车牌(官图库常在车牌上打「汽车之家 AUTOHOME」)。

    车牌 = 车身下半部一块又白又匀的横长方块(宽占车宽 5%–25%,宽高比 1.8–5.5,近乎填满外框),
    中间有一些不白的字。空白车牌(没有字)不算。返回 [(x0, y0, x1, y1)]。"""
    import numpy as np

    a = np.asarray(im.convert("RGBA")).astype(np.int32)
    r, g, b, alpha = a[..., 0], a[..., 1], a[..., 2], a[..., 3]
    luma = (r * 299 + g * 587 + b * 114) // 1000
    chroma = np.maximum(np.maximum(r, g), b) - np.minimum(np.minimum(r, g), b)
    white = (luma >= 200) & (chroma <= 40) & (alpha >= 250)
    H, W = white.shape
    found = []
    for x0, y0, x1, y1, fill in _blobs(white):
        w, h = x1 - x0, y1 - y0
        if not (W * 0.05 <= w <= W * 0.25 and 1.8 <= w / max(1, h) <= 5.5):
            continue
        ink = 1 - white[y0:y1, x0:x1].mean()
        if fill >= 0.8 and 0.03 <= ink <= 0.5 and (y0 + y1) / 2 >= H * 0.4:
            # 字贴着牌边时闭运算会把那一边啃掉几像素:往外扩到亮色(牌底 + 字)结束的地方
            while y0 > 0 and luma[y0 - 1, x0:x1].mean() > 150:
                y0 -= 1
            while y1 < H and luma[y1, x0:x1].mean() > 150:
                y1 += 1
            while x0 > 0 and luma[y0:y1, x0 - 1].mean() > 150:
                x0 -= 1
            while x1 < W and luma[y0:y1, x1].mean() > 150:
                x1 += 1
            found.append((x0, y0, x1, y1))
    return found


def find_yellow_plates(im: Any) -> list[tuple[int, int, int, int]]:
    """找**黄底黑字**的车牌 —— 懂车帝在自己牌子上打了黄底黑字的 logo。

    图库官图拿来做背景时,这个黄方块会明晃晃地挂在车头正中(实跑踩过:28 秒那拍
    整个车头被一块「A懂车帝」黄牌占住)。判据:又黄又匀的横长方块、位置在车身
    下半部、里面有深色字(logo)。返回 [(x0, y0, x1, y1)]。
    """
    import numpy as np

    a = np.asarray(im.convert("RGB")).astype(np.int32)
    r, g, b = a[..., 0], a[..., 1], a[..., 2]
    # 亮度下限放到 135:车尾的牌子常在阴影里(实跑漏过一张 R≈150 的,原来要求 R>=180)。
    # 放宽后靠色相兜住误判:G 在 R 的 0.62-1.0 倍之间才算黄,偏红的橙、偏绿的都不算。
    yellow = ((r >= 135) & (g >= 100) & (b <= 125) & ((r - b) >= 80) & ((g - b) >= 50)
              & (g <= r) & (g * 100 >= r * 62))
    H, W = yellow.shape
    found: list[tuple[int, int, int, int]] = []
    for x0, y0, x1, y1, fill in _blobs(yellow):
        w, h = x1 - x0, y1 - y0
        if not (W * 0.04 <= w <= W * 0.30 and 1.6 <= w / max(1, h) <= 6.0):
            continue
        if fill < 0.75:
            continue  # 车牌是实心方块;虚化背景里的一团黄(落叶、路锥)形状是散的
        if not (H * 0.38 <= (y0 + y1) / 2 <= H * 0.93):
            continue
        inner = a[y0 + 1:y1 - 1, x0 + 1:x1 - 1, :3]
        if inner.size == 0:
            continue
        if float((inner.max(axis=2) < 95).mean()) < 0.03:
            continue  # 里面没有深色字:不是印了 logo 的车牌,是车身上的黄漆
        found.append((x0, y0, x1, y1))
    return found


def scrub_body_plates(im: Any, boxes: Sequence[tuple[int, int, int, int]]) -> Any:
    """把车牌涂成**车身颜色**:取车牌上下各一行的颜色,纵向渐变贴上去。

    直接糊一块纯色会像贴了个补丁;用车身自己的颜色过渡,看起来就是「这块没车牌」。
    """
    import numpy as np
    from PIL import Image, ImageDraw, ImageFilter

    im = im.convert("RGB")
    a = np.asarray(im).astype(np.float64)
    H, W = a.shape[:2]
    for x0, y0, x1, y1 in boxes:
        top = a[max(0, y0 - 3):max(1, y0), x0:x1, :3].reshape(-1, 3).mean(axis=0)
        bot = a[y1:min(H, y1 + 3), x0:x1, :3].reshape(-1, 3).mean(axis=0)
        n = max(1, y1 - y0)
        ramp = np.linspace(0.0, 1.0, n)[:, None]
        grad = top[None, :] * (1 - ramp) + bot[None, :] * ramp
        patch = np.repeat(grad[:, None, :], max(1, x1 - x0), axis=1)
        mask = Image.new("L", (x1 - x0, y1 - y0), 0)
        ImageDraw.Draw(mask).rectangle((0, 0, x1 - x0 - 1, y1 - y0 - 1), fill=255)
        im.paste(Image.fromarray(patch.astype(np.uint8), "RGB"), (x0, y0),
                 mask.filter(ImageFilter.GaussianBlur(1.2)))
    return im


def find_plate_text(im: Any) -> list[tuple[int, int, int, int]]:
    """找白牌上那行**蓝字**(汽车之家把「汽车之家」四个蓝字印在白车牌上)。

    不找牌子的外框:白车的车牌和保险杠连成一片,阴影里的牌子又发灰,按「白色方块」找
    实跑 24 张只认出 3 张。蓝字本身好认 —— 一行饱和的蓝、横长、四周一圈浅色牌底。
    返回 [(x0, y0, x1, y1)],是字的外框,不是牌子的。
    """
    import numpy as np

    a = np.asarray(im.convert("RGB")).astype(np.int32)
    r, g, b = a[..., 0], a[..., 1], a[..., 2]
    blue = (b - r >= 60) & (b >= 110) & (b - g >= 15)
    luma = (r * 299 + g * 587 + b * 114) // 1000
    light = (luma >= 140) & ((a.max(axis=2) - a.min(axis=2)) <= 60)
    H, W = blue.shape
    found: list[tuple[int, int, int, int]] = []
    for x0, y0, x1, y1, fill in _blobs(blue):
        w, h = x1 - x0, y1 - y0
        if not (W * 0.025 <= w <= W * 0.2 and 2.4 <= w / max(1, h) <= 7.0 and fill >= 0.35):
            continue
        if (y0 + y1) / 2 < H * 0.4:
            continue
        # 字四周一圈(上下各半个字高、左右各 1/6 字宽)得是浅色的牌底
        py, px = max(2, h // 2), max(2, w // 6)
        ry0, ry1, rx0, rx1 = max(0, y0 - py), min(H, y1 + py), max(0, x0 - px), min(W, x1 + px)
        ring = np.ones((ry1 - ry0, rx1 - rx0), dtype=bool)
        ring[y0 - ry0:y1 - ry0, x0 - rx0:x1 - rx0] = False
        if float(light[ry0:ry1, rx0:rx1][ring].mean()) < 0.7:
            continue
        found.append((x0, y0, x1, y1))
    return found


def scrub_plate_text(im: Any, boxes: Sequence[tuple[int, int, int, int]]) -> Any:
    """把车牌上的字涂成牌底的颜色(取字四周浅色像素的中位数),留下一块空白车牌。"""
    import numpy as np
    from PIL import Image, ImageDraw, ImageFilter

    im = im.convert("RGB").copy()
    a = np.asarray(im).astype(np.int32)
    H, W = a.shape[:2]
    for x0, y0, x1, y1 in boxes:
        w, h = x1 - x0, y1 - y0
        py, px = max(2, h // 4), max(2, w // 16)
        fx0, fy0, fx1, fy1 = max(0, x0 - px), max(0, y0 - py), min(W, x1 + px), min(H, y1 + py)
        region = a[fy0:fy1, fx0:fx1].reshape(-1, 3)
        luma = (region * np.array([299, 587, 114])).sum(axis=1) // 1000
        base = region[luma >= np.percentile(luma, 70)]
        colour = tuple(int(v) for v in np.median(base, axis=0))
        mask = Image.new("L", im.size, 0)
        ImageDraw.Draw(mask).rectangle((fx0, fy0, fx1 - 1, fy1 - 1), fill=255)
        soft = max(1.0, h / 20)
        im.paste(Image.new("RGB", im.size, colour), (0, 0), mask.filter(ImageFilter.GaussianBlur(soft)))
    return im


def scrub_watermarks(im: Any) -> Any:
    """抹掉车牌上别家的标识:懂车帝的**黄牌**涂成车身色,汽车之家的**白牌**抹成空白牌。
    都没找到就原样返回。"""
    boxes = find_yellow_plates(im)
    if boxes:
        log.info("背景图抹掉 %d 块带标识的黄色车牌", len(boxes))
        im = scrub_body_plates(im, boxes)
    text = find_plate_text(im)  # 汽车之家的白牌:字抹掉,留一块空白车牌
    if text:
        log.info("背景图抹掉 %d 块白牌上的标识", len(text))
        im = scrub_plate_text(im, text)
    return im


def scrub_plates(im: Any) -> Any:
    """把车牌上的字抹成空白车牌:留一圈细牌框,里面填车牌底色(按纵向拟合的渐变,和原来的打光一致)。
    返回新图。"""
    import numpy as np
    from PIL import Image, ImageDraw, ImageFilter

    im = im.convert("RGBA")
    boxes = find_plates(im)
    if not boxes:
        return im
    im = im.copy()
    a = np.asarray(im)
    k = np.array([0.299, 0.587, 0.114])
    for x0, y0, x1, y1 in boxes:
        p = max(2, int((y1 - y0) * 0.07))
        ix0, iy0, ix1, iy1 = x0 + p, y0 + p, x1 - p, y1 - p
        rgb = a[iy0:iy1, ix0:ix1, :3].astype(np.float64)
        luma = rgb @ k
        # 只拿车牌底(最亮那批、去掉字)的像素,按行号拟合一条直线,得到上下的明暗渐变
        yy, _ = np.nonzero(luma >= np.percentile(luma, 75) - 10)
        cols = rgb[luma >= np.percentile(luma, 75) - 10]
        ramp = np.arange(iy1 - iy0)
        grad = np.stack([np.polyval(np.polyfit(yy, cols[:, c], 1), ramp) for c in range(3)], axis=1)
        patch = np.repeat(np.clip(grad, 0, 255)[:, None, :], ix1 - ix0, axis=1).astype(np.uint8)
        mask = Image.new("L", (ix1 - ix0, iy1 - iy0), 0)
        ImageDraw.Draw(mask).rectangle((1, 1, ix1 - ix0 - 2, iy1 - iy0 - 2), fill=255)
        im.paste(Image.fromarray(patch, "RGB"), (ix0, iy0), mask.filter(ImageFilter.GaussianBlur(1)))
    log.info("封面车图抹掉 %d 块带标识的车牌", len(boxes))
    return im


def _car_layer(cut: Any) -> Any:
    """抠图 PNG 裁到车身,抹掉车牌上的别家标识,边缘收一圈去掉白边。"""
    from PIL import Image, ImageFilter

    with Image.open(cut.png) as im:
        im = scrub_plates(im)
    a = im.getchannel("A").filter(ImageFilter.MinFilter(3)).filter(ImageFilter.GaussianBlur(0.6))
    im.putalpha(a)
    box = a.point(lambda v: 255 if v > 24 else 0).getbbox()
    return im.crop(box) if box else im
