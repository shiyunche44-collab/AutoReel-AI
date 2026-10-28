# -*- coding: utf-8 -*-
"""多配色系列：每款保留自己的主色调，仅用同色系深色做左侧渐变以承载抖音白字。"""
import os, glob
from PIL import Image, ImageDraw, ImageFont, ImageFilter, ImageChops

W, H = 1125, 633
BOLD = r"C:\Windows\Fonts\msyhbd.ttc"
REG = r"C:\Windows\Fonts\msyh.ttc"
WORK = r"F:\code\aimovice\douyin_bg\work"
OUT = r"F:\code\aimovice\douyin_bg"

WHITE = (255, 255, 255)
SUB = (206, 214, 228)

# 每款：deep = 左侧渐变深色（跟随主色调），accent = 眉标/装饰线点缀色
VARIANTS = {
    "a1_墨绿极光":      {"deep": (5, 36, 28),   "accent": (170, 240, 92)},
    "a2_藕紫柔光":      {"deep": (48, 24, 78),  "accent": (228, 178, 255)},
    "a3_落日橙":        {"deep": (78, 30, 14),  "accent": (255, 197, 108)},
    "a4_石墨黑电光青":  {"deep": (6, 20, 28),   "accent": (92, 225, 255)},
    "a5_玫瑰金都市":    {"deep": (60, 32, 28),  "accent": (255, 214, 166)},
    "a6_青柠机械":      {"deep": (28, 32, 16),  "accent": (202, 240, 76)},
    "a7_品红赛博":      {"deep": (42, 10, 50),  "accent": (255, 107, 214)},
    "a8_午夜蓝白":      {"deep": (12, 38, 84),  "accent": (143, 208, 255)},
}

ZONES = [(354, 393, 569, 452), (354, 478, 745, 511)]


def cover(img, w, h):
    sw, sh = img.size
    k = max(w / sw, h / sh)
    img = img.resize((int(sw * k), int(sh * k)), Image.LANCZOS)
    l = (img.size[0] - w) // 2
    t = (img.size[1] - h) // 2
    return img.crop((l, t, l + w, t + h)).convert("RGB")


def _profile(alpha_max, solid_to=520, fade_to=880):
    prof = []
    for x in range(W):
        if x <= solid_to:
            prof.append(alpha_max)
        elif x >= fade_to:
            prof.append(0)
        else:
            prof.append(int(alpha_max * (1 - (x - solid_to) / float(fade_to - solid_to))))
    return prof


def panel(img, deep, strength=1.0):
    """全高左侧渐变栏 + 底部压深，全部使用该款自己的深色调。"""
    prof = _profile(int(200 * strength))
    layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    for x in range(W):
        if prof[x] > 0:
            d.line([(x, 0), (x, H)], fill=deep + (prof[x],))
    layer = layer.filter(ImageFilter.GaussianBlur(30))
    base = Image.alpha_composite(img.convert("RGBA"), layer)

    bot = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    bd = ImageDraw.Draw(bot)
    for y in range(350, H):
        t = (y - 350) / (H - 350)
        bd.line([(0, y), (W, y)], fill=deep + (int(168 * (t ** 1.25) * strength),))
    bot = bot.filter(ImageFilter.GaussianBlur(30))
    return Image.alpha_composite(base, bot)


def zone_luma(img):
    px = img.convert("RGB").load()
    worst = 0.0
    for (x0, y0, x1, y1) in ZONES:
        lums = []
        for y in range(y0, y1, 3):
            for x in range(x0, x1, 3):
                r, g, b = px[x, y]
                lums.append((0.2126 * r + 0.7152 * g + 0.0722 * b) / 255)
        lums.sort()
        worst = max(worst, lums[int(len(lums) * 0.90)])
    return worst


def spaced(d, xy, text, font, fill, sp):
    x, y = xy
    for ch in text:
        d.text((x, y), ch, font=font, fill=fill, anchor="la")
        x += d.textlength(ch, font=font) + sp


def compose(src, out, cfg, title, kicker, sub, with_text=True):
    img = cover(Image.open(src), W, H)
    deep, accent = cfg["deep"], cfg["accent"]
    strength = 1.0
    for _ in range(6):
        base = panel(img, deep, strength)
        if zone_luma(base) <= 0.24:
            break
        strength *= 1.12
    if with_text:
        grad = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        gd = ImageDraw.Draw(grad)
        for i in range(140):
            t = i / 139
            gd.line([(132, 152 + i), (137, 152 + i)],
                    fill=(int(min(255, accent[0] + 40 * t)), int(min(255, accent[1] + 30 * t)), accent[2], 255))
        base = Image.alpha_composite(base, grad)
        d = ImageDraw.Draw(base)
        spaced(d, (158, 150), kicker, ImageFont.truetype(REG, 25), accent, 3)
        spaced(d, (158, 190), title, ImageFont.truetype(BOLD, 44), WHITE, 1)
        spaced(d, (158, 252), sub, ImageFont.truetype(REG, 23), SUB, 1)
    base.convert("RGB").save(out, "JPEG", quality=93, optimize=True, subsampling=1)
    return out


def make_mock(banner, out):
    SW, k = 390, 390 / 1206.0
    HH = int(700 * k)
    src = Image.open(banner).convert("RGB").crop((17, 0, 1108, 633)).resize((SW, HH), Image.LANCZOS)
    page = Image.new("RGB", (SW, 900), (255, 255, 255))
    page.paste(src, (0, 0))
    d = ImageDraw.Draw(page)
    d.text((25, 24), "19:49", font=ImageFont.truetype(REG, 12), fill=(255, 255, 255))
    d.text((SW - 62, 24), "21%", font=ImageFont.truetype(REG, 11), fill=(255, 255, 255))
    d.rounded_rectangle([SW / 2 - 115, 10, SW / 2 + 115, 42], 16, fill=(0, 0, 0))
    for cx, cy, r in [(96, 250, 43), (970, 247, 39), (1109, 247, 39)]:
        ov = Image.new("RGBA", page.size, (0, 0, 0, 0))
        ImageDraw.Draw(ov).ellipse([cx * k - r * k, cy * k - r * k, cx * k + r * k, cy * k + r * k],
                                   fill=(255, 255, 255, 115))
        page = Image.alpha_composite(page.convert("RGBA"), ov).convert("RGB")
    d = ImageDraw.Draw(page)
    d.rounded_rectangle([650 * k, 203 * k, 895 * k, 292 * k], 22, fill=(70, 90, 120))
    d.text((772 * k, 247 * k), "求更新", font=ImageFont.truetype(REG, 15), fill=(255, 255, 255), anchor="mm")
    ax0, ay0, ax1, ay1 = 48, 345, 355, 652
    acx, acy = (ax0 + ax1) / 2 * k, (ay0 + ay1) / 2 * k
    ar = (ax1 - ax0) / 2 * k
    av = Image.open(os.path.join(WORK, "avatar_ref.jpg")).convert("RGB").resize((int(2 * ar), int(2 * ar)), Image.LANCZOS)
    mask = Image.new("L", av.size, 0)
    ImageDraw.Draw(mask).ellipse([0, 0, av.size[0] - 1, av.size[1] - 1], fill=255)
    ring = Image.new("RGBA", av.size, (0, 0, 0, 0))
    ImageDraw.Draw(ring).ellipse([0, 0, av.size[0] - 1, av.size[1] - 1], outline=(255, 255, 255, 255), width=5)
    page.paste(av, (int(acx - ar), int(acy - ar)), mask)
    page.paste(ring, (int(acx - ar), int(acy - ar)), ring)
    d = ImageDraw.Draw(page)
    d.text((408 * k, 435 * k), "柒哥&车", font=ImageFont.truetype(BOLD, 30), fill=(255, 255, 255))
    d.text((408 * k, 528 * k), "抖音号：Qiqiqi_777777", font=ImageFont.truetype(REG, 19), fill=(226, 236, 248))
    y = HH + 8
    page.paste(Image.new("RGB", (SW, 900 - y), (255, 255, 255)), (0, y))
    d = ImageDraw.Draw(page)
    d.text((22, y + 16), "2420 获赞", font=ImageFont.truetype(BOLD, 16), fill=(30, 30, 34))
    d.text((150, y + 16), "5 关注", font=ImageFont.truetype(BOLD, 16), fill=(30, 30, 34))
    d.text((256, y + 16), "695 粉丝", font=ImageFont.truetype(BOLD, 16), fill=(30, 30, 34))
    d.text((22, y + 56), "有趣的灵魂配置酷炫的汽车 🚗", font=ImageFont.truetype(REG, 15), fill=(40, 40, 44))
    d.text((22, y + 92), "IP：湖北", font=ImageFont.truetype(REG, 12), fill=(120, 120, 126))
    d.text((100, y + 92), "男 · 31岁", font=ImageFont.truetype(REG, 12), fill=(120, 120, 126))
    d.rounded_rectangle([22, y + 126, 186, y + 164], 6, fill=(246, 246, 248))
    d.text((104, y + 145), "互相关注 ⌄", font=ImageFont.truetype(REG, 14), fill=(30, 30, 34), anchor="mm")
    d.rounded_rectangle([196, y + 126, 368, y + 164], 6, fill=(246, 246, 248))
    d.text((282, y + 145), "发私信", font=ImageFont.truetype(REG, 14), fill=(30, 30, 34), anchor="mm")
    d.text((22, y + 186), "作品 3 ⌄", font=ImageFont.truetype(BOLD, 17), fill=(30, 30, 34))
    for i in range(3):
        page.paste(Image.new("RGB", (126, 186), (236, 232, 224)), (2 + i * 130, y + 220))
    page.save(out, "PNG")
    return out


if __name__ == "__main__":
    for p in sorted(glob.glob(os.path.join(WORK, "raw_a*"))):
        tag = os.path.splitext(os.path.basename(p).replace("raw_", ""))[0]
        cfg = VARIANTS[tag]
        a = compose(p, os.path.join(OUT, f"背景-{tag}-带文案.jpg"), cfg,
                    "专业拆解每一台好车", "汽车深度内容", "选车避坑 · 用车干货 · 真实试驾", True)
        b = compose(p, os.path.join(OUT, f"背景-{tag}-无文字.jpg"), cfg, "", "", "", False)
        make_mock(a, os.path.join(OUT, f"预览-{tag}.png"))
        L = zone_luma(Image.open(b))
        print(f"{tag} | 白字落区 L={L:.3f} 对比度={1.05/(L+0.05):.1f}:1")
    print("done")
