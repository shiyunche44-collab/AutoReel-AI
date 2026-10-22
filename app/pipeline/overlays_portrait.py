"""竖屏(9:16)叠加层 —— 三段式排版。

和 `overlays.py` 画同样的东西(参数面板、关键词、章节进度、大数字、角标、
章节标牌、开场大字),只是排布换成竖屏三段式。坐标全部来自
`aspect.portrait_grid`,不在这里写比例:

- **标题区**:章节进度 + 两行大标题(常驻);highlight / hook 拍换成关键词;
- **画面带**:完整的 16:9 实拍,叠加层不压它(章节标牌例外,只停 1 秒多);
- **数据区**:参数面板、大数字、角标;
- **字幕区**由 ASS 字幕自己占,这里不画任何东西。

字号按竖屏的 1080 宽直接给像素值 —— 竖屏只有这一种尺寸,换算反而难读。
`overlays.py` 的每个函数在竖屏尺寸下转到这里,调用方不用管画幅。
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Sequence

from PIL import Image, ImageDraw

from . import aspect, hud
from .cards import _fit, _font

_ACCENT = hud.rgba(hud.ACCENT)
_TEXT = (255, 255, 255, 255)
_MUTED = (196, 204, 216, 255)
_EDGE = (0, 0, 0, 255)
_DIM = (255, 255, 255, 70)
#: 标题区 / 数据区的压暗底:模糊铺底上的字要有一层托
_SHADE = (6, 10, 18, 120)

#: 大标题每行最多几个字(1080 宽、两侧各留 64px,96px 的字放 9–10 个)
HEADLINE_CHARS = 10


def _canvas(size: tuple[int, int]) -> tuple[Image.Image, ImageDraw.ImageDraw]:
    img = Image.new("RGBA", size, (0, 0, 0, 0))
    return img, ImageDraw.Draw(img)


def _save(img: Image.Image, dest: str | Path) -> Path:
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    img.save(dest)
    return dest


def split_headline(text: str, max_chars: int = HEADLINE_CHARS) -> list[str]:
    """标题拆成最多两行:优先在标点 / 空格处断,断点尽量靠中间;没有就对半分。"""
    text = re.sub(r"\s+", " ", str(text or "")).strip()
    if len(text) <= max_chars:
        return [text] if text else []
    marks = [m.end() for m in re.finditer(r"[,，:：|｜!！?？、 ]", text)]
    mid = len(text) / 2
    good = [k for k in marks if 2 <= k <= len(text) - 2]
    cut = min(good, key=lambda k: abs(k - mid)) if good else int(round(mid))
    first, second = text[:cut].strip(" ,，:：|｜、"), text[cut:].strip(" ,，:：|｜、")
    return [x for x in (first, second) if x]


def _two_lines(d: ImageDraw.ImageDraw, lines: Sequence[str], g: aspect.PortraitGrid,
               top: int, bottom: int, sizes: tuple[int, int]) -> None:
    """两行大字在 [top, bottom] 里垂直居中:第一行白、第二行强调色,粗描边。"""
    lines = [x for x in lines if x][:2]
    if not lines:
        return
    # 标题区在平台按钮之上,不用让右侧安全区
    max_w = g.w - 2 * g.left
    fitted = []
    for n, text in enumerate(lines):
        t, f = _fit(d, text, sizes[n], max_w, bold=True)
        fitted.append((t, f, int(getattr(f, "size", sizes[n]))))
    total = sum(int(px * 1.18) for _, _, px in fitted)
    y = top + max(0, (bottom - top - total) // 2)
    for n, (t, f, px) in enumerate(fitted):
        d.text((g.w // 2, y), t, font=f, fill=_TEXT if n == 0 else _ACCENT, anchor="mt",
               stroke_width=max(4, px // 12), stroke_fill=_EDGE)
        y += int(px * 1.18)


# --------------------------------------------------------------------------
# 标题区:章节进度 + 大标题
# --------------------------------------------------------------------------
def chapter_bar(chapters: Sequence[tuple[str, float]], current: int, size: tuple[int, int],
                dest: str | Path, headline: str = "") -> Path:
    """标题区:顶上一行「02/06 · 章名」+ 细分段进度条,下面两行大标题。

    `headline` 为空(highlight / hook 拍,标题区要让给关键词)时只画进度。"""
    g = aspect.portrait_grid(size)
    img, d = _canvas(size)
    top = g.title.top
    # 进度行
    if chapters:
        cur = max(0, min(current, len(chapters) - 1))
        name = chapters[cur][0]
        tag = f"{cur + 1:02d}/{len(chapters):02d}"
        tf = _font(34, bold=True)
        d.text((g.left, top + 6), tag, font=tf, fill=_ACCENT, stroke_width=2, stroke_fill=_EDGE)
        tag_w = int(d.textlength(tag + "  ", font=tf))
        n_text, nf = _fit(d, name, 34, g.w - 2 * g.left - tag_w, bold=True)
        d.text((g.left + tag_w, top + 6), n_text, font=nf, fill=_TEXT, stroke_width=2, stroke_fill=_EDGE)
        # 分段条:每章一段(等宽 —— 竖屏窄,按时长分会让短章节细到看不见)
        y = top + 58
        gap = 6
        seg = (g.w - 2 * g.left - gap * (len(chapters) - 1)) / len(chapters)
        for n in range(len(chapters)):
            x0 = int(g.left + n * (seg + gap))
            x1 = int(g.left + n * (seg + gap) + seg)
            d.rectangle((x0, y, x1, y + 6), fill=_ACCENT if n <= cur else _DIM)
    if headline:
        _two_lines(d, split_headline(headline), g, top + 80, g.title.bottom - 16, (100, 88))
    return _save(img, dest)


def highlight(lines: Sequence[str], size: tuple[int, int], dest: str | Path) -> Path:
    """关键词占标题区(大标题这一拍让位),第一行白、第二行强调色。"""
    g = aspect.portrait_grid(size)
    img, d = _canvas(size)
    lines = [str(x).strip() for x in lines if str(x).strip()][:2]
    sizes = (112, 98) if len(lines) > 1 else (124, 124)
    _two_lines(d, lines, g, g.title.top + 80, g.title.bottom - 16, sizes)
    return _save(img, dest)


# --------------------------------------------------------------------------
# 数据区
# --------------------------------------------------------------------------
def panel(overlay: dict[str, Any], subject: str, size: tuple[int, int], dest: str | Path) -> Path:
    """数据区一张 HUD 规格纸:标题行(右边「可查出处」),参数横排(每行最多 3 个:
    标签 / 大数字 / 口径),来源行。

    竖屏数据区只有约 300px 高、手机上看:实跑时沿用横屏的「标签行 + 标题 + 两列网格」,
    标签只剩 21px、数字 45px,看不清。这里砍掉 SPEC/SHEET 标签行,参数横着排,
    数字放到 76px。"""
    g = aspect.portrait_grid(size)
    img, d = _canvas(size)
    x0, x1 = g.left - 24, g.right
    # 底边多留出投影(偏移 + 模糊)的余量,影子也不进字幕区
    y0, y1 = g.data.top + 18, g.data.bottom - 36
    box = (x0, y0, x1, y1)
    img = Image.alpha_composite(img, hud.shadow(size, box, alpha=90, blur=10, offset=8))
    d = ImageDraw.Draw(img)
    hud.plate(d, box, grid_step=34)
    hud.brackets(d, box, length=26, width=4)

    pad = 24
    cx0, cx1 = x0 + pad, x1 - pad
    inner = cx1 - cx0
    note, note_size = "可查出处", 24
    note_w = hud.tracked_width(d, note, note_size, note_size, 3)
    hud.draw_tracked(d, cx1 - note_w, y0 + pad + 8, note, note_size, note_size, hud.ACCENT_DARK, tracking=3)
    y = y0 + pad - 2
    title_text = str(overlay.get("title") or "核心参数")
    if subject and subject not in title_text:
        title_text = f"{subject} · {title_text}"
    title, tf = _fit(d, title_text, 44, inner - note_w - 24, bold=True)
    d.text((cx0, y), title, font=tf, fill=hud.INK)
    y += int(getattr(tf, "size", 44)) + 12
    hud.rule(d, cx0, cx1, y, hud.INK, 3)
    y += 16

    src = str(overlay.get("source") or "")
    bottom = y1 - 14
    if src:
        s, ms, cs = hud.fit_mixed(d, src, 20, 20, inner)
        hud.draw_tracked(d, cx0, bottom - 20, s, ms, cs, hud.MUTED, tracking=1.2)
        bottom -= 30
    rows = list(overlay.get("rows") or [])[:6]
    if not rows:
        return _save(img, dest)
    lines = 1 if len(rows) <= 3 else 2
    per_line = -(-len(rows) // lines)
    gap = 28
    col_w = (inner - (per_line - 1) * gap) // per_line
    rh = (bottom - y) // lines
    # 两行时每格只有约 90px:口径行让掉,标签 + 数字
    with_tag = lines == 1
    key_size = 30 if lines == 1 else 24
    tag_size = 20
    num_size = min(76, rh - key_size - (tag_size + 12 if with_tag else 0) - 10)
    for n, r in enumerate(rows):
        line, col = divmod(n, per_line)
        cx = cx0 + col * (col_w + gap)
        ry = y + line * rh
        if col:
            d.line((cx - gap // 2, ry + 4, cx - gap // 2, ry + rh - 10), fill=hud.MUTED, width=1)
        label, kf = _fit(d, str(r.get("label") or ""), key_size, col_w)
        d.text((cx, ry), label, font=kf, fill=hud.KEY)
        value = str((r.get("values") or [""])[0])
        px = num_size
        while px > 24 and hud.value_width(d, value, px, int(px * 0.45)) > col_w:
            px -= 2
        hud.draw_value(d, cx, ry + key_size + 6, value, px, max(14, int(px * 0.45)))
        if with_tag and r.get("tag"):
            t2, ms, cs = hud.fit_mixed(d, str(r["tag"]), tag_size, tag_size, col_w)
            hud.draw_tracked(d, cx, ry + key_size + 6 + px + 10, t2, ms, cs, hud.MUTED, tracking=1)
    return _save(img, dest)


def stat(overlay: dict[str, Any], subject: str, size: tuple[int, int], dest: str | Path,
         progress: float = 1.0) -> Path:
    """数据区中央一个大数字:上面标签,下面口径 + 出处。"""
    from .overlays import count_text

    g = aspect.portrait_grid(size)
    img, d = _canvas(size)
    max_w = g.right - g.left
    cx = (g.left + g.right) // 2
    d.rectangle((0, g.data.top, g.w, g.data.bottom - 1), fill=_SHADE)
    label = str(overlay.get("label") or "")
    tag, tf = _fit(d, f"{subject} · {label}" if subject else label, 44, max_w, bold=True)
    d.text((cx, g.data.top + 46), tag, font=tf, fill=_TEXT, anchor="mm",
           stroke_width=3, stroke_fill=_EDGE)
    value = count_text(str(overlay.get("value") or ""), progress)
    # 有"所以呢"(这个数字对观众意味着什么)时数据区要多放一行:大数字缩小上移,注解压在它和口径之间
    note = str(overlay.get("note") or "")
    num_size, num_y = (130, g.data.mid - 22) if note else (168, g.data.mid + 4)
    _, nf = _fit(d, count_text(str(overlay.get("value") or ""), 1.0), num_size, max_w, bold=True)
    d.text((cx, num_y), value, font=nf, fill=_ACCENT, anchor="mm",
           stroke_width=8, stroke_fill=_EDGE)
    if note:
        n, f = _fit(d, note, 38, max_w, bold=True)
        d.text((cx, g.data.mid + 70), n, font=f, fill=_TEXT, anchor="mm",
               stroke_width=4, stroke_fill=_EDGE)
    y = g.data.bottom - 70
    if overlay.get("tag"):
        t, f = _fit(d, str(overlay["tag"]), 32, max_w, bold=True)
        d.text((cx, y), t, font=f, fill=_TEXT, anchor="mm", stroke_width=2, stroke_fill=_EDGE)
    src, sf = _fit(d, str(overlay.get("source", "")), 22, max_w)
    d.text((cx, y + 40), src, font=sf, fill=_MUTED, anchor="mm")
    return _save(img, dest)


def lower_third(subject: str, chapter: str, size: tuple[int, int], dest: str | Path) -> Path:
    """数据区左上的角标:强调色竖条 + 「车名 · 章名」。"""
    g = aspect.portrait_grid(size)
    img, d = _canvas(size)
    x0, y0 = g.left, g.data.top + 28
    bh = 76
    text = f"{subject} · {chapter}" if chapter else subject
    text, f = _fit(d, text, 38, g.right - g.left - int(bh * 0.9), bold=True)
    tw = int(d.textlength(text, font=f))
    d.rounded_rectangle((x0, y0, x0 + tw + int(bh * 0.9), y0 + bh), radius=10, fill=(10, 16, 30, 210))
    d.rectangle((x0, y0, x0 + int(bh * 0.14), y0 + bh), fill=_ACCENT)
    d.text((x0 + int(bh * 0.45), y0 + bh // 2), text, font=f, anchor="lm", fill=_TEXT)
    return _save(img, dest)


# --------------------------------------------------------------------------
# 章节标牌:贴在画面带的左上角
# --------------------------------------------------------------------------
def sting(number: int, name: str, size: tuple[int, int], dest: Path, wipe: float,
          alpha: float) -> Path:
    g = aspect.portrait_grid(size)
    img, d = _canvas(size)
    x0, y0 = g.left, g.band.top + 28
    bh = 92
    num = f"{number:02d}"
    nf = _font(int(bh * 0.62), bold=True)
    num_w = int(d.textlength(num, font=nf)) + int(bh * 0.5)
    name, tf = _fit(d, name, int(bh * 0.46), g.right - g.left - num_w - int(bh * 0.6), bold=True)
    name_w = int(d.textlength(name, font=tf)) + int(bh * 0.6)
    shown = int((num_w + name_w) * max(0.0, min(1.0, wipe)))
    if shown > 0:
        layer, ld = _canvas(size)
        ld.rectangle((x0, y0, x0 + min(shown, num_w), y0 + bh), fill=_ACCENT)
        if shown > num_w:
            ld.rectangle((x0 + num_w, y0, x0 + shown, y0 + bh), fill=(10, 16, 30, 215))
        ld.text((x0 + num_w // 2, y0 + bh // 2), num, font=nf, anchor="mm", fill=(20, 20, 20, 255))
        ld.text((x0 + num_w + int(bh * 0.3), y0 + bh // 2), name, font=tf, anchor="lm", fill=_TEXT)
        mask = Image.new("L", size, 0)
        ImageDraw.Draw(mask).rectangle((x0, y0, x0 + shown, y0 + bh), fill=255)
        img.paste(layer, (0, 0), mask)
    if alpha < 1:
        img.putalpha(img.getchannel("A").point(lambda v: int(v * max(0.0, alpha))))
    return _save(img, dest)


# --------------------------------------------------------------------------
# 黄金三秒:关键词占标题区,数字占数据区
# --------------------------------------------------------------------------
def hook_text(overlay: dict[str, Any], subject: str, size: tuple[int, int], dest: str | Path,
              scale: float = 1.0, alpha: float = 1.0) -> Path:
    g = aspect.portrait_grid(size)
    img, d = _canvas(size)
    lines = [str(x).strip() for x in overlay.get("lines") or [] if str(x).strip()][:2]
    sizes = (int(130 * scale), int(112 * scale))
    _two_lines(d, lines, g, g.title.top + 20, g.title.bottom - 10, sizes)
    stat_ = overlay.get("stat") if isinstance(overlay.get("stat"), dict) else None
    if stat_:
        max_w = g.right - g.left
        cx = (g.left + g.right) // 2
        label = str(stat_.get("label") or "")
        tag, tf = _fit(d, f"{subject} · {label}" if subject else label, int(44 * scale), max_w, bold=True)
        d.text((cx, g.data.top + 50), tag, font=tf, fill=_TEXT, anchor="mm",
               stroke_width=3, stroke_fill=_EDGE)
        num, nf = _fit(d, str(stat_.get("value") or ""), int(180 * scale), max_w, bold=True)
        d.text((cx, g.data.mid + 20), num, font=nf, fill=_ACCENT, anchor="mm",
               stroke_width=9, stroke_fill=_EDGE)
    if alpha < 1:
        img.putalpha(img.getchannel("A").point(lambda v: int(v * max(0.0, alpha))))
    return _save(img, dest)
