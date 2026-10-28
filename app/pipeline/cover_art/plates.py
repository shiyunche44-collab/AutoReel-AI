"""车牌上别家标识的检测与抹除。"""
from __future__ import annotations

from typing import Any, Sequence

from .tone import log


def find_plates(im: Any) -> list[tuple[int, int, int, int]]:
    """找车身上印着别家标识的白色车牌(官图库常在车牌上打「汽车之家 AUTOHOME」)。

    车牌 = 车身下半部一块又白又匀的横长方块(宽占车宽 5%–25%,宽高比 1.8–5.5,近乎填满外框),
    中间有一些不白的字。空白车牌(没有字)不算。返回 [(x0, y0, x1, y1)]。"""
    import numpy as np
    from PIL import Image, ImageDraw, ImageFilter

    a = np.asarray(im.convert("RGBA")).astype(np.int32)
    r, g, b, alpha = a[..., 0], a[..., 1], a[..., 2], a[..., 3]
    luma = (r * 299 + g * 587 + b * 114) // 1000
    chroma = np.maximum(np.maximum(r, g), b) - np.minimum(np.minimum(r, g), b)
    white = (luma >= 200) & (chroma <= 40) & (alpha >= 250)
    # 闭运算把字的笔画补上,整块车牌连成一片
    m = Image.fromarray(np.where(white, 255, 0).astype(np.uint8), "L")
    m = m.filter(ImageFilter.MaxFilter(5)).filter(ImageFilter.MinFilter(5)).copy()
    H, W = white.shape
    found = []
    ys, xs = np.nonzero(np.asarray(m)[::3, ::3] == 255)
    for y, x in zip(ys * 3, xs * 3):
        if m.getpixel((int(x), int(y))) != 255:
            continue
        ImageDraw.floodfill(m, (int(x), int(y)), 128, thresh=0)
        comp = np.asarray(m) == 128
        m.paste(0, mask=Image.fromarray((comp * 255).astype(np.uint8)))
        yy, xx = np.nonzero(comp)
        x0, x1, y0, y1 = int(xx.min()), int(xx.max()) + 1, int(yy.min()), int(yy.max()) + 1
        w, h = x1 - x0, y1 - y0
        if not (W * 0.05 <= w <= W * 0.25 and 1.8 <= w / max(1, h) <= 5.5):
            continue
        fill = comp[y0:y1, x0:x1].mean()
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
    from PIL import Image, ImageDraw, ImageFilter

    a = np.asarray(im.convert("RGB")).astype(np.int32)
    r, g, b = a[..., 0], a[..., 1], a[..., 2]
    yellow = (r >= 180) & (g >= 130) & (b <= 125) & ((r - b) >= 80) & ((g - b) >= 50)
    m = Image.fromarray(np.where(yellow, 255, 0).astype(np.uint8), "L")
    m = m.filter(ImageFilter.MaxFilter(5)).filter(ImageFilter.MinFilter(5)).copy()
    H, W = yellow.shape
    found: list[tuple[int, int, int, int]] = []
    ys, xs = np.nonzero(np.asarray(m)[::3, ::3] == 255)
    for y, x in zip(ys * 3, xs * 3):
        if m.getpixel((int(x), int(y))) != 255:
            continue
        ImageDraw.floodfill(m, (int(x), int(y)), 128, thresh=0)
        comp = np.asarray(m) == 128
        m.paste(0, mask=Image.fromarray((comp * 255).astype(np.uint8)))
        yy, xx = np.nonzero(comp)
        x0, x1, y0, y1 = int(xx.min()), int(xx.max()) + 1, int(yy.min()), int(yy.max()) + 1
        w, h = x1 - x0, y1 - y0
        if not (W * 0.04 <= w <= W * 0.30 and 1.6 <= w / max(1, h) <= 6.0):
            continue
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


def scrub_watermarks(im: Any) -> Any:
    """抹掉车身上印着别家标识的**黄底**车牌(懂车帝)。没找到就原样返回。"""
    boxes = find_yellow_plates(im)
    if not boxes:
        return im
    log.info("背景图抹掉 %d 块带标识的黄色车牌", len(boxes))
    return scrub_body_plates(im, boxes)


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
