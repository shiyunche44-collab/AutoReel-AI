"""横屏解说轨的叠加层:侧边参数面板、两行大字关键词、大数字滚动、底部章节条。

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


# --------------------------------------------------------------------------
# 大数字滚动(stat 版式)
# --------------------------------------------------------------------------
_NUM_RE = r"-?\d+(?:\.\d+)?"
#: 数字从 0 滚到目标值用几帧、多久。关键帧 + concat 时长,不逐帧画满整拍
COUNT_STEPS = 16
COUNT_SECONDS = 1.1


def split_number(value: str) -> tuple[str, float, int, str] | None:
    """"3.23 秒" → ("", 3.23, 2, " 秒")。取第一个数字;千分位逗号去掉。没有数字返回 None。"""
    import re

    text = str(value).replace(",", "")
    m = re.search(_NUM_RE, text)
    if not m:
        return None
    num = m.group(0)
    decimals = len(num.split(".")[1]) if "." in num else 0
    return text[:m.start()], float(num), decimals, text[m.end():]


def count_text(value: str, progress: float) -> str:
    """滚到 `progress`(0–1)时屏幕上显示的数字。progress=1 时与原文一字不差。"""
    parts = split_number(value)
    if parts is None or progress >= 1:
        return str(value)
    head, num, decimals, tail = parts
    return f"{head}{num * max(0.0, progress):.{decimals}f}{tail}"


def stat(overlay: dict[str, Any], subject: str, size: tuple[int, int], dest: str | Path,
         progress: float = 1.0) -> Path:
    """画面中央一个大数字:上面一行小字标签,数字本体强调色粗描边,下面一行出处。"""
    w, h = size
    img, d = _canvas(size)
    label = str(overlay.get("label") or "")
    value = count_text(str(overlay.get("value") or ""), progress)
    max_w = int(w * 0.8)
    tag, tf = _fit(d, f"{subject} · {label}" if subject else label, 54, max_w, bold=True)
    d.text((w // 2, int(h * 0.26)), tag, font=tf, fill=_TEXT, anchor="mm",
           stroke_width=4, stroke_fill=(0, 0, 0, 255))
    # 字号按最终值定,滚动时不忽大忽小
    _, nf = _fit(d, count_text(str(overlay.get("value") or ""), 1.0), 210, max_w, bold=True)
    d.text((w // 2, int(h * 0.46)), value, font=nf, fill=_ACCENT, anchor="mm",
           stroke_width=10, stroke_fill=(0, 0, 0, 255))
    src, sf = _fit(d, overlay.get("source", ""), 24, max_w)
    d.text((w // 2, int(h * 0.64)), src, font=sf, fill=_MUTED, anchor="mm")
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    img.save(dest)
    return dest


def stat_frames(overlay: dict[str, Any], subject: str, size: tuple[int, int],
                out_dir: str | Path) -> list[tuple[Path, float]]:
    """滚动关键帧:[(透明 PNG, 停留秒数)]。最后一帧是原值,停留时长由调用方定格到结束。"""
    from .cards import ease_out

    out_dir = Path(out_dir)
    step = COUNT_SECONDS / COUNT_STEPS
    out: list[tuple[Path, float]] = []
    for k in range(COUNT_STEPS + 1):
        p = stat(overlay, subject, size, out_dir / f"stat_{k:02d}.png",
                 progress=ease_out(k / COUNT_STEPS))
        out.append((p, step))
    return out


# --------------------------------------------------------------------------
# 章节标牌(每章第一拍开头 1.2 秒)与角标(broll 变体 lower_third)
# --------------------------------------------------------------------------
#: 标牌:色条擦入几帧 / 多久,停留多久,淡出几帧 / 多久
STING_WIPE_STEPS = 8
STING_WIPE_SECONDS = 0.32
STING_HOLD_SECONDS = 0.55
STING_FADE_STEPS = 6
STING_FADE_SECONDS = 0.3


def _sting(number: int, name: str, size: tuple[int, int], dest: Path, wipe: float,
           alpha: float) -> Path:
    """左上角章节标牌:强调色色块里是章号,右边白字章名。`wipe` 0–1 是色块擦开的进度,
    `alpha` 0–1 是整体不透明度(淡出用)。"""
    w, h = size
    img, d = _canvas(size)
    x0, y0 = int(w * 0.035), int(h * 0.075)
    bh = int(h * 0.105)
    num = f"{number:02d}"
    nf = _font(int(bh * 0.62), bold=True)
    tf = _font(int(bh * 0.46), bold=True)
    num_w = int(d.textlength(num, font=nf)) + int(bh * 0.5)
    name, tf = _fit(d, name, int(bh * 0.46), int(w * 0.40))
    name_w = int(d.textlength(name, font=tf)) + int(bh * 0.6)
    full = num_w + name_w
    shown = int(full * max(0.0, min(1.0, wipe)))
    if shown > 0:
        layer, ld = _canvas(size)
        ld.rectangle((x0, y0, x0 + min(shown, num_w), y0 + bh), fill=_ACCENT)
        if shown > num_w:
            ld.rectangle((x0 + num_w, y0, x0 + shown, y0 + bh), fill=(10, 16, 30, 215))
        ld.text((x0 + num_w // 2, y0 + bh // 2), num, font=nf, anchor="mm", fill=_BAR_ON_TEXT)
        ld.text((x0 + num_w + int(bh * 0.3), y0 + bh // 2), name, font=tf, anchor="lm", fill=_TEXT)
        # 文字只在擦开的部分里显示
        mask = Image.new("L", size, 0)
        ImageDraw.Draw(mask).rectangle((x0, y0, x0 + shown, y0 + bh), fill=255)
        img.paste(layer, (0, 0), mask)
    if alpha < 1:
        a = img.getchannel("A").point(lambda v: int(v * max(0.0, alpha)))
        img.putalpha(a)
    dest.parent.mkdir(parents=True, exist_ok=True)
    img.save(dest)
    return dest


def chapter_sting_frames(number: int, name: str, size: tuple[int, int],
                         out_dir: str | Path) -> list[tuple[Path, float]]:
    """章节标牌关键帧:[(透明 PNG, 停留秒数)]。擦入 → 停留 → 淡出,最后一帧全透明。"""
    from .cards import ease_out

    out_dir = Path(out_dir)
    frames: list[tuple[Path, float]] = []
    step = STING_WIPE_SECONDS / STING_WIPE_STEPS
    for k in range(1, STING_WIPE_STEPS + 1):
        p = _sting(number, name, size, out_dir / f"sting_w{k:02d}.png", ease_out(k / STING_WIPE_STEPS), 1.0)
        frames.append((p, step))
    frames[-1] = (frames[-1][0], STING_HOLD_SECONDS)
    fstep = STING_FADE_SECONDS / STING_FADE_STEPS
    for k in range(1, STING_FADE_STEPS + 1):
        p = _sting(number, name, size, out_dir / f"sting_f{k:02d}.png", 1.0, 1 - k / STING_FADE_STEPS)
        frames.append((p, fstep))
    return frames


def lower_third(subject: str, chapter: str, size: tuple[int, int], dest: str | Path) -> Path:
    """左上角角标:强调色竖条 + 「车名 · 章名」。只用已有的文字,不需要任何数据。"""
    w, h = size
    img, d = _canvas(size)
    x0, y0 = int(w * 0.035), int(h * 0.075)
    bh = int(h * 0.07)
    text = f"{subject} · {chapter}" if chapter else subject
    text, f = _fit(d, text, int(bh * 0.5), int(w * 0.36), bold=True)
    tw = int(d.textlength(text, font=f))
    d.rounded_rectangle((x0, y0, x0 + tw + int(bh * 0.9), y0 + bh), radius=10, fill=(10, 16, 30, 200))
    d.rectangle((x0, y0, x0 + int(bh * 0.14), y0 + bh), fill=_ACCENT)
    d.text((x0 + int(bh * 0.45), y0 + bh // 2), text, font=f, anchor="lm", fill=_TEXT)
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    img.save(dest)
    return dest
