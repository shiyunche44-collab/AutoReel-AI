# -*- coding: utf-8 -*-
"""抖音主页背景图合成：底图裁切 + 暗部渐层 + 专业中文排版。
用法: python compose.py <src.jpg> <out.jpg> <主标题> [眉标] [副标题] [--clean]
"""
import sys, os
from PIL import Image, ImageDraw, ImageFont, ImageFilter

W, H = 1125, 633
F_BOLD = r"C:\Windows\Fonts\msyhbd.ttc"
F_REG = r"C:\Windows\Fonts\msyh.ttc"

CYAN = (86, 199, 255)
WHITE = (255, 255, 255)
SUB = (168, 198, 224)


def fit_cover(img, w, h):
    """等比裁切填满画布，居中裁切。"""
    sw, sh = img.size
    scale = max(w / sw, h / sh)
    nw, nh = int(round(sw * scale)), int(round(sh * scale))
    img = img.resize((nw, nh), Image.LANCZOS)
    left = (nw - w) // 2
    top = (nh - h) // 2
    return img.crop((left, top, left + w, top + h)).convert("RGB")


def hscrim(img, x0, x1, a0, a1, color=(4, 10, 26)):
    """水平渐变遮罩层：x0 处 alpha=a0，x1 处 alpha=a1。"""
    layer = Image.new("RGBA", img.size, (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    n = img.size[0]
    for x in range(n):
        if x < x0:
            t = 0.0
        elif x > x1:
            t = 1.0
        else:
            t = (x - x0) / float(x1 - x0)
        a = int(round(a0 + (a1 - a0) * t))
        d.line([(x, 0), (x, img.size[1])], fill=color + (a,))
    return Image.alpha_composite(img.convert("RGBA"), layer)


def vscrim(img, y0, y1, a0, a1, color=(2, 6, 18)):
    """垂直渐变遮罩层。"""
    layer = Image.new("RGBA", img.size, (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    h = img.size[1]
    for y in range(h):
        if y < y0:
            t = 0.0
        elif y > y1:
            t = 1.0
        else:
            t = (y - y0) / float(y1 - y0)
        a = int(round(a0 + (a1 - a0) * t))
        d.line([(0, y), (img.size[0], y)], fill=color + (a,))
    return Image.alpha_composite(img.convert("RGBA"), layer)


def vignette(img, strength=110):
    w, h = img.size
    mask = Image.new("L", (w, h), 0)
    d = ImageDraw.Draw(mask)
    d.ellipse([-w * 0.35, -h * 0.55, w * 1.35, h * 1.55], fill=255)
    mask = mask.filter(ImageFilter.GaussianBlur(160))
    dark = Image.new("RGBA", (w, h), (2, 6, 18, strength))
    inv = mask.point(lambda v: 255 - v)
    dark.putalpha(inv.point(lambda v: int(v * strength / 255)))
    return Image.alpha_composite(img.convert("RGBA"), dark)


def draw_spaced(d, xy, text, font, fill, spacing):
    x, y = xy
    for ch in text:
        d.text((x, y), ch, font=font, fill=fill, anchor="la")
        x += d.textlength(ch, font=font) + spacing
    return x


def glow_text(base, xy, text, font, fill, spacing=0, shadow=True, glow=(40, 150, 255, 90)):
    """带柔光 + 阴影的清晰文字。"""
    if glow:
        g = Image.new("RGBA", base.size, (0, 0, 0, 0))
        gd = ImageDraw.Draw(g)
        draw_spaced(gd, xy, text, font, glow, spacing)
        g = g.filter(ImageFilter.GaussianBlur(9))
        base = Image.alpha_composite(base, g)
    if shadow:
        s = Image.new("RGBA", base.size, (0, 0, 0, 0))
        sd = ImageDraw.Draw(s)
        draw_spaced(sd, (xy[0] + 2, xy[1] + 4), text, font, (0, 0, 0, 165), spacing)
        s = s.filter(ImageFilter.GaussianBlur(5))
        base = Image.alpha_composite(base, s)
    d = ImageDraw.Draw(base)
    draw_spaced(d, xy, text, font, fill, spacing)
    return base


def build(src, out, title, kicker, subtitle, clean=False):
    img = fit_cover(Image.open(src), W, H)

    if not clean:
        # 左侧文字区压暗，保证中文可读性；右侧保留车体高光
        img = hscrim(img, 0, 720, 232, 0)
        img = hscrim(img, 0, 150, 120, 0, color=(2, 6, 18))
        img = vscrim(img, H - 170, H, 0, 120)
        img = vignette(img, 100)

    base = img.convert("RGBA")

    if not clean:
        d = ImageDraw.Draw(base)
        # 左侧竖向青色装饰条
        bar_x, bar_y, bar_w = 106, 152, 5
        f_k = ImageFont.truetype(F_REG, 27)
        f_t = ImageFont.truetype(F_BOLD, 56)
        f_s = ImageFont.truetype(F_REG, 26)

        kb = d.textbbox((0, 0), kicker, font=f_k)
        th = f_t.size + 12
        tb = d.textbbox((0, 0), title, font=f_t)
        sb = d.textbbox((0, 0), subtitle, font=f_s)
        total = (kb[3] - kb[1]) + 22 + (tb[3] - tb[1]) + 16 + (sb[3] - sb[1])
        bar_h = total

        grad = Image.new("RGBA", base.size, (0, 0, 0, 0))
        gd = ImageDraw.Draw(grad)
        for i in range(int(bar_h)):
            t = i / max(1.0, bar_h - 1)
            gd.line([(bar_x, bar_y + i), (bar_x + bar_w, bar_y + i)],
                    fill=(86, 199, 255, int(235 - 90 * t)))
        base = Image.alpha_composite(base, grad.filter(ImageFilter.GaussianBlur(0.6)))

        x_text = bar_x + bar_w + 26
        y = bar_y - 4
        # 眉标
        draw_spaced(d := ImageDraw.Draw(base), (x_text, y), kicker, f_k, CYAN, 3)
        y += (kb[3] - kb[1]) + 22
        # 主标题
        base = glow_text(base, (x_text, y - 4), title, f_t, WHITE, 2)
        d = ImageDraw.Draw(base)
        y += (tb[3] - tb[1]) + 16
        # 副标题
        draw_spaced(d, (x_text, y), subtitle, f_s, SUB, 1)

        # 底部一条细分隔线 + 右下角小标签
        d = ImageDraw.Draw(base)
        d.line([(0, H - 2), (W, H - 2)], fill=(86, 199, 255, 60), width=2)

    base.convert("RGB").save(out, "JPEG", quality=92, optimize=True, subsampling=1)
    return out


if __name__ == "__main__":
    a = sys.argv[1:]
    src, out, title = a[0], a[1], a[2]
    kicker = a[3] if len(a) > 3 else "汽车深度内容"
    subtitle = a[4] if len(a) > 4 else "选车避坑 · 用车干货 · 真实试驾"
    clean = "--clean" in a
    p = build(src, out, title, kicker, subtitle, clean)
    print("OK", p, os.path.getsize(p))
