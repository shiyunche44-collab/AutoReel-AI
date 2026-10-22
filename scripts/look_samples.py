"""视觉包示例图:每套 look 用同一份示例数据画一张(参数面板 + 章节条 + 字幕),供项目页挑选。

    python -m scripts.look_samples              # 写到 app/web/static/looks/<id>.jpg 和 <id>_v.jpg
    python -m scripts.look_samples --sheet x.jpg  # 另外拼一张总览图

背景是中性灰渐变(素材库的帧多带别人的字幕和水印)。改了 looks.py 的配色就重跑一次。
"""
from __future__ import annotations

import argparse
import sys
import tempfile
from pathlib import Path

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.pipeline import cards, looks, overlays  # noqa: E402

OUT = ROOT / "app" / "web" / "static" / "looks"

PANEL = {
    "title": "补能与续航",
    "rows": [
        {"label": "CLTC 续航", "values": ["835 km"], "tag": "Max 版"},
        {"label": "峰值充电功率", "values": ["5.2C"]},
        {"label": "15 分钟补能", "values": ["620 km"], "tag": "厂商口径"},
    ],
    "source": "来源:小米汽车官网 2025-06",
}
CHAPTERS = [("开头", 1.0), ("定位价格", 2.0), ("三电性能", 3.0), ("智能配置", 2.0), ("结论", 1.0)]
SUBTITLE = "15 分钟补 620 公里,吃顿饭就够跑一趟"


def _background(size: tuple[int, int]) -> Image.Image:
    """中性灰的斜向渐变。不用素材库的帧:那些多是别人视频的截图,带烧录字幕和水印。"""
    w, h = size
    small = Image.new("RGB", (64, 64))
    for y in range(64):
        for x in range(64):
            t = (x + y) / 126
            v = int(150 - 90 * t)
            small.putpixel((x, y), (v, v + 4, v + 10))
    return small.resize(size, Image.BICUBIC)


def _subtitle(img: Image.Image, look: looks.Look) -> None:
    """按视觉包的字幕样式画一行示意字幕(真字幕由 libass 烧,这里只示意观感)。"""
    w, h = img.size
    d = ImageDraw.Draw(img, "RGBA")
    size = max(24, int(min(w, h) * 0.056))
    f = cards._font(size, bold=True)
    tw = d.textlength(SUBTITLE, font=f)
    x, y = (w - tw) / 2, h - int(h * 0.115) - size

    def ass_rgb(code: str) -> tuple[int, int, int, int]:
        v = code.replace("&H", "")
        a, b, g, r = (int(v[i:i + 2], 16) for i in (0, 2, 4, 6))
        return r, g, b, 255 - a

    text = ass_rgb(look.sub_text)
    if look.subs == "outline":
        d.text((x, y), SUBTITLE, font=f, fill=text, stroke_width=3, stroke_fill=(0, 0, 0, 255))
        return
    pad = size // 6
    d.rectangle((x - pad, y - pad, x + tw + pad, y + size + pad * 1.6), fill=ass_rgb(look.sub_box))
    d.text((x, y), SUBTITLE, font=f, fill=text)


def sample(look: looks.Look, size: tuple[int, int]) -> Image.Image:
    with looks.use(look), tempfile.TemporaryDirectory() as tmp:
        img = _background(size).convert("RGBA")
        for layer in (overlays.panel(PANEL, "小米YU7", size, Path(tmp) / "panel.png"),
                      overlays.chapter_bar(CHAPTERS, 2, size, Path(tmp) / "bar.png")):
            img = Image.alpha_composite(img, Image.open(layer).convert("RGBA"))
        img = img.convert("RGB")
        _subtitle(img, look)
        return img


def card_sample(look: looks.Look, size: tuple[int, int]) -> Image.Image:
    card = {"kind": "compare", "title": "补能对比", "columns": ["小米YU7", "特斯拉Model Y"],
            "rows": [{"label": "峰值充电", "values": ["5.2C", "4C"]},
                     {"label": "CLTC 续航", "values": ["835 km", "719 km"]}]}
    with looks.use(look), tempfile.TemporaryDirectory() as tmp:
        frames = cards.render_frames(card, "小米YU7", Path(tmp), None, size)
        return Image.open(frames[-1]).convert("RGB")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sheet", help="另外拼一张总览图(每行一套 look:面板 / 数据卡)")
    args = ap.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)
    land, port = (1920, 1080), (1080, 1920)
    rows = []
    for look in looks.LOOKS.values():
        a = sample(look, land)
        a.resize((960, 540), Image.LANCZOS).save(OUT / f"{look.id}.jpg", quality=86)
        b = sample(look, port)
        b.resize((540, 960), Image.LANCZOS).save(OUT / f"{look.id}_v.jpg", quality=86)
        if args.sheet:
            rows.append((a.resize((960, 540)), card_sample(look, land).resize((960, 540))))
        print("写入", look.id)
    if args.sheet:
        sheet = Image.new("RGB", (1920, 540 * len(rows)), (0, 0, 0))
        for n, (a, c) in enumerate(rows):
            sheet.paste(a, (0, 540 * n))
            sheet.paste(c, (960, 540 * n))
        sheet.save(args.sheet, quality=88)
        print("总览", args.sheet)


if __name__ == "__main__":
    main()
