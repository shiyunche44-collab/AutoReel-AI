"""横屏解说轨的叠加层:侧边参数面板、两行大字关键词、底部章节条。

全部画成透明 PNG,由 `explainer.py` 用 ffmpeg overlay 叠到背景画面上。
画法与数据卡(`cards.py`)同源 —— 同一套字体与取字号逻辑,数字同样只来自
核对过事实的行,这里只负责"画",不做任何核对。

版式对标「三驱兄弟」:宣传片铺底,信息压在上面,底部常驻章节条告诉观众
"现在讲到哪了、后面还有什么"。
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Sequence

from PIL import Image, ImageDraw

from .cards import _fit, _font

_PANEL_BG = (10, 16, 30, 205)
_PANEL_EDGE = (255, 205, 60, 255)
_TEXT = (255, 255, 255, 255)
_MUTED = (180, 190, 205, 255)
_ACCENT = (255, 205, 60, 255)
_RULE = (255, 255, 255, 46)
_BAR_BG = (0, 0, 0, 175)
_BAR_ON = (255, 205, 60, 235)
_BAR_ON_TEXT = (20, 20, 20, 255)

#: 章节条高度(占画面高度)。字幕的底边距是 0.14H,两者不会打架
BAR_HEIGHT = 0.05


def _canvas(size: tuple[int, int]) -> tuple[Image.Image, ImageDraw.ImageDraw]:
    img = Image.new("RGBA", size, (0, 0, 0, 0))
    return img, ImageDraw.Draw(img)


def panel(overlay: dict[str, Any], subject: str, size: tuple[int, int], dest: str | Path) -> Path:
    """左侧参数面板:车名 / 标题 / 3–5 行参数。`overlay` 形状同 spec 卡片。"""
    w, h = size
    img, d = _canvas(size)
    x0, x1 = int(w * 0.035), int(w * 0.335)
    y0, y1 = int(h * 0.10), int(h * 0.76)
    d.rounded_rectangle((x0, y0, x1, y1), radius=22, fill=_PANEL_BG)
    d.rectangle((x0, y0 + 26, x0 + 8, y0 + 96), fill=_PANEL_EDGE)
    pad = int(w * 0.018)
    inner = x1 - x0 - pad * 2
    y = y0 + 24
    tag, tf = _fit(d, subject, 30, inner)
    d.text((x0 + pad, y), tag, font=tf, fill=_ACCENT)
    y += 46
    title, ttf = _fit(d, overlay.get("title") or "核心参数", 48, inner, bold=True)
    d.text((x0 + pad, y), title, font=ttf, fill=_TEXT)
    y += 84
    rows = overlay.get("rows") or []
    area = (y1 - 56) - y
    rh = min(118, area // max(1, len(rows)))
    for r in rows:
        label, lf = _fit(d, r["label"], 28, inner)
        d.text((x0 + pad, y + 4), label, font=lf, fill=_MUTED)
        value, vf = _fit(d, r["values"][0], 42, inner, bold=True)
        d.text((x0 + pad, y + 4 + int(rh * 0.36)), value, font=vf, fill=_TEXT)
        d.line((x0 + pad, y + rh - 6, x1 - pad, y + rh - 6), fill=_RULE, width=2)
        y += rh
    src, sf = _fit(d, overlay.get("source", ""), 20, inner)
    d.text((x0 + pad, y1 - 40), src, font=sf, fill=_MUTED)
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    img.save(dest)
    return dest


def highlight(lines: Sequence[str], size: tuple[int, int], dest: str | Path) -> Path:
    """两行大字关键词,压在小窗画面的下沿。第一行白、第二行强调色,粗描边。"""
    w, h = size
    img, d = _canvas(size)
    lines = [str(x).strip() for x in lines if str(x).strip()][:2]
    sizes = (112, 96) if len(lines) > 1 else (124,)
    max_w = int(w * 0.84)
    y = int(h * (0.56 if len(lines) > 1 else 0.62))
    for n, text in enumerate(lines):
        t, f = _fit(d, text, sizes[n], max_w, bold=True)
        fill = _TEXT if n == 0 else _ACCENT
        d.text((w // 2, y), t, font=f, fill=fill, anchor="mt",
               stroke_width=max(4, sizes[n] // 12), stroke_fill=(0, 0, 0, 255))
        y += int(sizes[n] * 1.18)
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    img.save(dest)
    return dest


def chapter_bar(chapters: Sequence[tuple[str, float]], current: int, size: tuple[int, int],
                dest: str | Path) -> Path:
    """底部章节条:按各章时长分宽,当前章高亮。`chapters` = [(名字, 时长秒), ...]。

    宽度按时长分,但每章至少给能放下名字的宽度 —— 开头、结尾往往只有一两拍,
    纯按比例会窄到写不下字。
    """
    w, h = size
    img, d = _canvas(size)
    bar_h = int(h * BAR_HEIGHT)
    top = h - bar_h
    d.rectangle((0, top, w, h), fill=_BAR_BG)
    if not chapters:
        img.save(dest)
        return Path(dest)
    font_size = int(bar_h * 0.52)
    font = _font(font_size, bold=True)
    floor = [d.textlength(name, font=font) + 36 for name, _ in chapters]
    total = sum(max(0.0, s) for _, s in chapters) or 1.0
    widths = [max(fl, w * max(0.0, s) / total) for fl, (_, s) in zip(floor, chapters)]
    scale = w / sum(widths)
    x = 0.0
    for n, ((name, _), cw) in enumerate(zip(chapters, widths)):
        cw *= scale
        x0, x1 = int(x), int(x + cw)
        if n == current:
            d.rectangle((x0, top, x1, h), fill=_BAR_ON)
        elif n:
            d.line((x0, top + 10, x0, h - 10), fill=(255, 255, 255, 90), width=2)
        label, lf = _fit(d, name, font_size, max(10, x1 - x0 - 16), bold=True)
        d.text(((x0 + x1) // 2, top + bar_h // 2), label, font=lf, anchor="mm",
               fill=_BAR_ON_TEXT if n == current else _TEXT)
        x += cw
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    img.save(dest)
    return dest
