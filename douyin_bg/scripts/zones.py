# -*- coding: utf-8 -*-
"""按真实主页量测坐标生成 UI 遮挡示意图。"""
import os
from PIL import Image, ImageDraw, ImageFont

BOLD = r"C:\Windows\Fonts\msyhbd.ttc"
REG = r"C:\Windows\Fonts\msyh.ttc"
W, H = 1125, 633
OUT = r"F:\code\aimovice\douyin_bg"
SRC = os.path.join(OUT, "背景-a3_落日橙-无文字.jpg")

# 原图 1206 宽 -> 画布 1125 宽，scale=1.1058，左侧裁掉 17.2 画布像素
S = 1.1058
def cx(v): return (v - 17.2) / S
def cy(v): return v / S

boxes = [
    ("状态栏", 0, 60, 1206, 125, (255, 196, 64)),
    ("动态岛", 260, 45, 965, 140, (255, 196, 64)),
    ("返回键", 53, 207, 139, 293, (255, 120, 120)),
    ("求更新", 650, 203, 895, 292, (255, 120, 120)),
    ("搜索", 931, 208, 1009, 286, (255, 120, 120)),
    ("更多", 1070, 208, 1148, 286, (255, 120, 120)),
    ("头像", 48, 345, 355, 652, (254, 44, 85)),
    ("昵称", 408, 435, 645, 500, (254, 44, 85)),
    ("抖音号", 408, 528, 840, 565, (254, 44, 85)),
]

img = Image.open(SRC).convert("RGB").resize((W, H), Image.LANCZOS)
ov = Image.new("RGBA", (W, H), (0, 0, 0, 0))
d = ImageDraw.Draw(ov)
# 先画文案安全区（底层），再画 UI 遮挡框（上层），避免标签被覆盖
d.rectangle([120, 138, 596, 300], outline=(120, 240, 160, 255), width=3)
d.rectangle([120, 138, 596, 300], fill=(120, 240, 160, 30))
for name, x0, y0, x1, y1, c in boxes:
    X0, Y0, X1, Y1 = cx(x0), cy(y0), cx(x1), cy(y1)
    d.rectangle([X0, Y0, X1, Y1], outline=c + (235,), width=3)
out = Image.alpha_composite(img.convert("RGBA"), ov).convert("RGB")
dd = ImageDraw.Draw(out)
f = ImageFont.truetype(BOLD, 19)
fr = ImageFont.truetype(REG, 15)
for name, x0, y0, x1, y1, c in boxes:
    X0, Y0 = cx(x0), cy(y0)
    tw = dd.textlength(name, font=f)
    tx = min(max(X0 + 6, 6), W - tw - 14)
    ty = max(Y0 - 25, 4)
    dd.rectangle([tx - 5, ty - 3, tx + tw + 9, ty + 24], fill=(16, 18, 24))
    dd.text((tx, ty), name, font=f, fill=c)

# 标注可放文案的区域
dd.text((132, 148), "文案安全区", font=f, fill=(150, 255, 190))
dd.text((132, 176), "1125×633 画布坐标 x120-596 / y138-300", font=fr, fill=(200, 255, 225))
dd.text((132, 200), "已避开状态栏、返回键、求更新、头像圈", font=fr, fill=(200, 255, 225))
out.save(os.path.join(OUT, "UI遮挡示意图.png"), "PNG")
print("saved")
