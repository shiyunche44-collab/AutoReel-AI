# -*- coding: utf-8 -*-
"""最终版：左栏深蓝 + 右侧通透亮天空。
抖音白色昵称/抖音号落在画布 x354-745 / y393-511，背景白色文案落在 x130-570 / y145-285，
两者同处左侧，故左侧统一压深蓝，两处都用白字，视觉统一且都可读。
"""
import os, glob
from PIL import Image, ImageDraw, ImageFont, ImageFilter, ImageChops

W, H = 1125, 633
BOLD = r"C:\Windows\Fonts\msyhbd.ttc"
REG = r"C:\Windows\Fonts\msyh.ttc"
WORK = r"F:\code\aimovice\douyin_bg\work"
OUT = r"F:\code\aimovice\douyin_bg"

DEEP = (16, 58, 132)
WHITE = (255, 255, 255)
SUB = (198, 220, 245)

# 真实主页 UI（画布坐标）
AV = (28, 312, 306, 590)       # 头像圈
NICK = (354, 393, 569, 452)    # 昵称
DYID = (354, 478, 745, 511)    # 抖音号
BTN = [(32, 187, 110, 265), (572, 184, 794, 264), (826, 188, 897, 259), (953, 188, 1024, 259)]


def cover(img, w, h):
    sw, sh = img.size
    k = max(w / sw, h / sh)
    img = img.resize((int(sw * k), int(sh * k)), Image.LANCZOS)
    l = (img.size[0] - w) // 2
    t = (img.size[1] - h) // 2
    return img.crop((l, t, l + w, t + h)).convert("RGB")


def _profile(alpha_max, solid_to=470, fade_to=800):
    """全高左侧渐变栏的横向 alpha 剖面。"""
    prof = []
    for x in range(W):
        if x <= solid_to:
            prof.append(alpha_max)
        elif x >= fade_to:
            prof.append(0)
        else:
            prof.append(int(alpha_max * (1 - (x - solid_to) / float(fade_to - solid_to))))
    return prof


def left_panel(img, strength=1.0):
    """全高左侧深蓝渐变栏：没有上下边界，视觉上是一道自然渐变而非色块。"""
    prof = _profile(int(196 * strength), solid_to=520, fade_to=880)
    layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    for x in range(W):
        if prof[x] > 0:
            d.line([(x, 0), (x, H)], fill=DEEP + (prof[x],))
    layer = layer.filter(ImageFilter.GaussianBlur(30))
    base = Image.alpha_composite(img.convert("RGBA"), layer)

    # 全宽底部压深：承载"抖音号"一行（画布 y478-511），让地面自然沉下去
    bot = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    bd = ImageDraw.Draw(bot)
    for y in range(350, H):
        t = (y - 350) / (H - 350)
        bd.line([(0, y), (W, y)], fill=(9, 36, 92, int(165 * (t ** 1.25) * strength)))
    bot = bot.filter(ImageFilter.GaussianBlur(30))
    return Image.alpha_composite(base, bot)


def spaced(d, xy, text, font, fill, sp):
    x, y = xy
    for ch in text:
        d.text((x, y), ch, font=font, fill=fill, anchor="la")
        x += d.textlength(ch, font=font) + sp


RAMPS = {
    # 冷蓝：呼应头像的蓝天白云；高光压到中蓝档，保证白色昵称可读
    "cool": [(0.00, (5, 16, 42)), (0.22, (11, 45, 100)), (0.46, (24, 78, 152)),
             (0.72, (52, 122, 196)), (1.00, (128, 184, 236))],
    # 暖调：保留黄昏橙，但压深底部以承载白字
    "warm": [(0.00, (10, 16, 38)), (0.26, (32, 38, 70)), (0.52, (108, 66, 56)),
             (0.76, (208, 122, 52)), (1.00, (244, 202, 148))],
}


def duotone(img, ramp):
    """按亮度重映射到品牌色阶，整体调性统一。"""
    stops = RAMPS[ramp]
    g = img.convert("L")
    luts = []
    for ch in range(3):
        lut = []
        for i in range(256):
            t = i / 255
            for j in range(len(stops) - 1):
                p0, c0 = stops[j]
                p1, c1 = stops[j + 1]
                if p0 <= t <= p1:
                    k = 0 if p1 == p0 else (t - p0) / (p1 - p0)
                    lut.append(int(round(c0[ch] + (c1[ch] - c0[ch]) * k)))
                    break
            else:
                lut.append(stops[-1][1][ch])
        luts.append(lut)
    return Image.merge("RGB", (g.point(luts[0]), g.point(luts[1]), g.point(luts[2])))


def _band_mask():
    """昵称+抖音号落区的柔化横向信息条蒙版：两端渐隐，纵向柔化。"""
    m = Image.new("L", (W, H), 0)
    ImageDraw.Draw(m).rounded_rectangle([326, 380, 786, 526], 30, fill=255)
    fade = Image.new("L", (W, H), 0)
    fd = ImageDraw.Draw(fade)
    for x in range(W):
        if x < 404:
            v = max(0.0, (x - 326) / 78.0)
        elif x > 708:
            v = max(0.0, (786 - x) / 78.0)
        else:
            v = 1.0
        fd.line([(x, 0), (x, H)], fill=int(255 * v))
    m = ImageChops.multiply(m, fade)
    return m.filter(ImageFilter.GaussianBlur(24))


def ui_zone_darken(base, zones, target=0.22, strength=1.0):
    """自适应：整体加强左侧渐变栏（不做局部色块），直到白字对比度达标。"""
    cur = base
    for _ in range(5):
        if _zone_luma(cur, zones) <= target:
            break
        strength *= 1.14
        cur = left_panel(cur, strength)
    return cur


def _zone_luma(img, zones):
    px = img.convert("RGB").load()
    worst = 0.0
    for (x0, y0, x1, y1) in zones:
        lums = []
        for y in range(y0, y1, 3):
            for x in range(x0, x1, 3):
                r, g, b = px[x, y]
                lums.append((0.2126 * r + 0.7152 * g + 0.0722 * b) / 255)
        lums.sort()
        worst = max(worst, lums[int(len(lums) * 0.90)])
    return worst


ZONES = [(354, 393, 569, 452), (354, 478, 745, 511)]


def compose(src, out, title, kicker, sub, with_text, ramp="cool"):
    img = cover(Image.open(src), W, H)
    if ramp:
        img = Image.blend(img, duotone(img, ramp), 0.72)
    strength = 1.0
    for _ in range(6):
        base = left_panel(img, strength)
        if _zone_luma(base, ZONES) <= 0.22:
            break
        strength *= 1.12
    if with_text:
        d = ImageDraw.Draw(base)
        fk = ImageFont.truetype(REG, 25)
        ft = ImageFont.truetype(BOLD, 44)
        fs = ImageFont.truetype(REG, 23)
        grad = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        gd = ImageDraw.Draw(grad)
        for i in range(140):
            t = i / 139
            gd.line([(132, 152 + i), (137, 152 + i)],
                    fill=(int(120 + 120 * t), int(200 + 40 * t), int(255), 255))
        base = Image.alpha_composite(base, grad)
        d = ImageDraw.Draw(base)
        spaced(d, (158, 150), kicker, fk, (150, 215, 255), 3)
        spaced(d, (158, 190), title, ft, WHITE, 1)
        spaced(d, (158, 252), sub, fs, SUB, 1)
    base.convert("RGB").save(out, "JPEG", quality=93, optimize=True, subsampling=1)
    return out


def make_mock(banner, out):
    SW = 390
    k = SW / 1206.0
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

    ax0, ay0, ax1, ay1 = (48, 345, 355, 652)
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


def contrast_report(path):
    """量化抖音白字（柒哥&车 / 抖音号）在背景上的对比度。"""
    im = Image.open(path).convert("RGB")
    px = im.load()
    zones = {"昵称": (354, 393, 569, 452), "抖音号": (354, 478, 745, 511)}
    out = []
    for name, (x0, y0, x1, y1) in zones.items():
        lums = []
        for y in range(y0, y1, 3):
            for x in range(x0, x1, 3):
                r, g, b = px[x, y]
                lums.append((0.2126 * r + 0.7152 * g + 0.0722 * b) / 255)
        lums.sort()
        worst = lums[int(len(lums) * 0.90)]   # 90 分位：接近最亮的地方
        cr = 1.05 / (worst + 0.05)
        out.append((name, worst, cr))
    return out


if __name__ == "__main__":
    for p in sorted(glob.glob(os.path.join(WORK, "raw_v*.jpg"))):
        tag = os.path.basename(p).replace("raw_", "").replace(".jpg", "")
        ramp = "warm" if "黄昏" in tag else "cool"
        a = compose(p, os.path.join(OUT, f"背景-{tag}-推荐无文字.jpg"), "", "", "", False, ramp)
        b = compose(p, os.path.join(OUT, f"背景-{tag}-推荐带文案.jpg"),
                    "专业拆解每一台好车", "汽车深度内容", "选车避坑 · 用车干货 · 真实试驾", True, ramp)
        make_mock(b, os.path.join(OUT, f"预览-{tag}.png"))
        rep = contrast_report(a)
        print(tag, "| " + "  ".join(f"{n}: L={l:.2f} 对比度={c:.1f}:1" for n, l, c in rep))
    print("done")
