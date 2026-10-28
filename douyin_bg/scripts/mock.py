# -*- coding: utf-8 -*-
"""生成抖音主页效果预览 + 安全区示意图。"""
import os
from PIL import Image, ImageDraw, ImageFont, ImageFilter

BOLD = r"C:\Windows\Fonts\msyhbd.ttc"
REG = r"C:\Windows\Fonts\msyh.ttc"
OUT = r"F:\code\aimovice\douyin_bg"
os.makedirs(OUT, exist_ok=True)

CYAN = (86, 199, 255)
GREY = (150, 158, 170)
WHITE = (255, 255, 255)


def f(path, size):
    return ImageFont.truetype(path, size)


def preview(banner_path, out):
    SW = 390
    hdr = Image.open(banner_path).convert("RGB").resize((SW, 219), Image.LANCZOS)
    H = 812
    page = Image.new("RGB", (SW, H), (18, 18, 20))
    page.paste(hdr, (0, 0))

    d = ImageDraw.Draw(page)
    # 顶部状态栏（叠加在头图上）
    d.text((16, 14), "19:41", font=f(REG, 13), fill=(255, 255, 255))
    d.text((SW - 92, 14), "5G  ▮▮▮  21%", font=f(REG, 12), fill=(255, 255, 255))

    # 头像（圆形，压在头图左下角）
    av = 86
    ax, ay = 22, 219 - av + 12
    ring = Image.new("RGBA", (av + 8, av + 8), (0, 0, 0, 0))
    ImageDraw.Draw(ring).ellipse([0, 0, av + 7, av + 7], fill=(18, 18, 20, 255))
    page.paste(ring, (ax - 4, ay - 4), ring)
    avimg = Image.new("RGB", (av, av), (26, 44, 78))
    da = ImageDraw.Draw(avimg)
    for i in range(av):
        t = i / av
        da.line([(0, i), (av, i)], fill=(int(14 + 30 * t), int(40 + 60 * t), int(78 + 90 * t)))
    da.ellipse([0, 0, av - 1, av - 1], outline=(86, 199, 255), width=3)
    da.text((av / 2, av / 2), "车", font=f(BOLD, 40), fill=(200, 230, 255), anchor="mm")
    page.paste(avimg, (ax, ay))

    # 昵称 / 抖音号 / 简介
    y = ay + av + 12
    d.text((22, y), "老王说车", font=f(BOLD, 19), fill=WHITE)
    nx = 22 + d.textlength("老王说车", font=f(BOLD, 19)) + 10
    d.rounded_rectangle([nx, y + 2, nx + 92, y + 24], 11, fill=(52, 52, 58), outline=(90, 90, 96))
    d.text((nx + 8, y + 5), "抖音号：laowang", font=f(REG, 12), fill=GREY)
    y += 34
    d.text((22, y), "深蓝系说车人 · 只讲真实的驾驶体验", font=f(REG, 13), fill=(178, 182, 190))
    y += 30

    # 关注按钮
    d.rounded_rectangle([22, y, 108, y + 34], 6, fill=(254, 44, 85))
    d.text((65, y + 17), "关 注", font=f(BOLD, 15), fill=WHITE, anchor="mm")
    y += 50

    # 数据行
    stats = [("关注", "128"), ("粉丝", "8.6万"), ("获赞", "126.4万")]
    x = 22
    for lab, val in stats:
        d.text((x, y), val, font=f(BOLD, 16), fill=WHITE)
        d.text((x, y + 24), lab, font=f(REG, 12), fill=GREY)
        x += 100
    y += 56

    # 标签栏
    d.line([(0, y), (SW, y)], fill=(44, 44, 48))
    d.text((24, y + 14), "作品", font=f(BOLD, 16), fill=WHITE)
    d.text((90, y + 14), "喜欢", font=f(REG, 16), fill=GREY)
    d.text((156, y + 14), "收藏", font=f(REG, 16), fill=GREY)
    d.line([(24, y + 43), (68, y + 43)], fill=WHITE, width=3)
    y += 52

    # 作品九宫格占位
    for i in range(6):
        col, row = i % 3, i // 3
        gx, gy = 2 + col * 130, y + row * 196
        tile = Image.new("RGB", (126, 186), (32, 34, 40))
        dt = ImageDraw.Draw(tile)
        dt.polygon([(55, 70), (55, 116), (96, 93)], fill=(96, 100, 112))
        dt.text((6, 6), f"0{i+1}", font=f(BOLD, 13), fill=(120, 124, 136))
        page.paste(tile, (gx, gy))
    page.save(out, "PNG")
    return out


def safezone(banner_path, out):
    W, H = 1125, 633
    img = Image.open(banner_path).convert("RGB").resize((W, H), Image.LANCZOS)
    layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    # 首屏可见区 (顶部 400px)
    d.rectangle([0, 0, W, 400], fill=(86, 199, 255, 26))
    d.line([(0, 400), (W, 400)], fill=(86, 199, 255, 230), width=3)
    d.text((16, 12), "首屏可见区 1125 × 400", font=f(BOLD, 20), fill=CYAN)
    d.text((16, 40), "打开主页默认只显示这一块", font=f(REG, 16), fill=(200, 220, 240))
    d.text((16, 64), "核心文案请放这里", font=f(REG, 16), fill=(200, 220, 240))
    # 下拉放大区 (中心 633 方形)
    d.rectangle([246, 0, 879, 633], outline=(255, 196, 64, 235), width=3)
    d.text((260, 566), "下拉后放大的中心区 633 × 633", font=f(BOLD, 18), fill=(255, 196, 64))
    d.text((260, 592), "装饰元素放这里会被放大裁切", font=f(REG, 15), fill=(255, 220, 160))
    # 头像遮挡区（位于可见区下缘，向下溢出）
    d.ellipse([16, 318, 336, 638], outline=(254, 44, 85, 235), width=3)
    d.text((356, 432), "头像遮挡区", font=f(BOLD, 19), fill=(254, 100, 130))
    d.text((356, 458), "头像 / 昵称压在这一带，别放关键信息", font=f(REG, 15), fill=(240, 170, 185))
    # 左右裁切提示
    d.text((24, 560), "← 左右边缘易被裁切", font=f(REG, 16), fill=(230, 235, 245))
    d.text((900, 560), "易被裁切 →", font=f(REG, 16), fill=(230, 235, 245))
    Image.alpha_composite(img.convert("RGBA"), layer).convert("RGB").save(out, "PNG")
    return out


if __name__ == "__main__":
    src = r"F:\code\aimovice\.tmp_douyin_bg\out_A_text.jpg"
    print(preview(src, os.path.join(OUT, "抖音主页效果预览.png")))
    print(safezone(r"F:\code\aimovice\.tmp_douyin_bg\out_A_clean.jpg", os.path.join(OUT, "安全区示意图.png")))
