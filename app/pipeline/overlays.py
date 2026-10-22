"""解说轨的叠加层:侧边参数面板、两行大字关键词、大数字滚动、底部章节条。

竖屏(9:16)尺寸下每个函数转到 `overlays_portrait`(三段式排版),横屏走这里。

全部画成透明 PNG,由 `explainer.py` 用 ffmpeg overlay 叠到背景画面上。
画法与数据卡(`cards.py`)同源 —— 同一套字体与取字号逻辑,数字同样只来自
核对过事实的行,这里只负责"画",不做任何核对。

外观对标 `motion/bakeoff/C.html`(风格 C · 极简科技 HUD):浅色工程图纸 + 发丝网格
+ 等宽数字 + 橙色强调 + 取景框角标。设计细节见 `hud.py`。
版式仍对标「三驱兄弟」的信息型长视频:信息压在上面,底部常驻章节条告诉观众
"现在讲到哪了、后面还有什么"。
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Sequence

from PIL import Image, ImageDraw

from . import aspect, hud, overlays_portrait
from .cards import _fit, _font

#: 强调色统一到 HUD 的橙(和面板的纸面、角标是同一套视觉语言)
_ACCENT = hud.rgba(hud.ACCENT)
_TEXT = (255, 255, 255, 255)
_MUTED = (180, 190, 205, 255)
_RULE = (255, 255, 255, 46)
_BAR_BG = (0, 0, 0, 175)
_BAR_ON = hud.rgba(hud.ACCENT, 235)
_BAR_ON_TEXT = (20, 20, 20, 255)

#: 章节条高度(占画面高度)。
#:
#: 加高是为了手机端:28px 的章名缩到手机屏宽上只有几个像素,看不清当前在讲哪一章。
#: 字幕的底边距是 0.115H、章节条 0.068H,两者加起来 0.183H,不会打架。
BAR_HEIGHT = 0.068
#: 章名字号下限(像素)。条加高之后按比例算会偏小,这里钉一个下限。
BAR_FONT_MIN = 34


def _canvas(size: tuple[int, int]) -> tuple[Image.Image, ImageDraw.ImageDraw]:
    img = Image.new("RGBA", size, (0, 0, 0, 0))
    return img, ImageDraw.Draw(img)


def panel(overlay: dict[str, Any], subject: str, size: tuple[int, int], dest: str | Path) -> Path:
    """左侧参数面板 —— HUD 规格页:车名 / 标题 / 3–5 行参数 / 来源行。

    版式照搬 bakeoff C:浅色纸面、发丝网格、橙色取景框角标、等宽大数字,
    参数名右边压一行口径小标签。`overlay` 形状同 spec 卡片。
    """
    if aspect.is_portrait_size(size):
        return overlays_portrait.panel(overlay, subject, size, dest)
    w, h = size
    img, d = _canvas(size)
    x0, x1 = int(w * 0.035), int(w * 0.335)
    y0, y1 = int(h * 0.14), int(h * 0.78)
    box = (x0, y0, x1, y1)

    # 纸面浮在画面上:先铺一层投影,再画纸
    img = Image.alpha_composite(img, hud.shadow(size, box, alpha=90, blur=int(w * 0.008),
                                                offset=int(h * 0.009)))
    d = ImageDraw.Draw(img)
    hud.plate(d, box, grid_step=int(w * 0.031))
    hud.brackets(d, box, length=int(w * 0.024), width=max(4, int(w * 0.0026)))

    pad = int(w * 0.018)
    cx0, cx1 = x0 + pad, x1 - pad
    inner = cx1 - cx0

    # 顶部标签行:左边「车名 · SPEC/SHEET」,右边「可查出处」——
    # 本片唯一抄不走的本事,摆在这行最显眼的位置
    tag_size = max(16, int(h * 0.022))
    track = tag_size * 0.16
    tag = f"{subject} · SPEC/SHEET" if subject else "SPEC/SHEET"
    hud.draw_tracked(d, cx0, y0 + pad, tag, tag_size, tag_size,
                     hud.MUTED, tracking=track)
    note = "可查出处"
    hud.draw_tracked(d, cx1 - hud.tracked_width(d, note, tag_size, tag_size, track),
                     y0 + pad, note, tag_size, tag_size, hud.ACCENT_DARK, tracking=track)

    y = y0 + pad + tag_size + 10
    title = str(overlay.get("title") or "核心参数")
    title_size = max(28, int(h * 0.048))
    title, tf = _fit(d, title, title_size, inner, bold=True)
    d.text((cx0, y), title, font=tf, fill=hud.INK)
    y += int(getattr(tf, "size", title_size)) + 16
    hud.rule(d, cx0, cx1, y, hud.INK, 3)
    y += 26

    rows = overlay.get("rows") or []
    src = str(overlay.get("source") or "")
    bottom = y1 - int(h * 0.014)
    src_size = max(14, int(h * 0.019))
    if src:
        s, ms, cs = hud.fit_mixed(d, src, src_size, src_size, inner)
        hud.draw_tracked(d, cx0, bottom - src_size, s, ms, cs, hud.MUTED, tracking=1.2)
    # 刻度尺压在来源行上方:纯装饰,像仪表的刻度,不表示任何数值
    ruler_y = (bottom - src_size - 14) if src else (bottom - 10)
    hud.ruler(d, cx0, cx1, ruler_y)
    area = max(60, ruler_y - 20 - y)
    n = max(1, len(rows))
    rh = min(int(h * 0.125), area // n)
    key_size = max(18, min(28, int(rh * 0.24)))
    num_size = max(30, min(66, int(rh * 0.56)))
    unit_size = max(16, int(num_size * 0.5))
    for r in rows:
        label = str(r.get("label") or "")
        k, kf = _fit(d, label, key_size, inner)
        d.text((cx0, y), k, font=kf, fill=hud.KEY)
        if r.get("tag"):
            # 口径 / 来源小标签贴在参数名右边(和数字一起被看到)
            gap = int(d.textlength(k + "  ", font=kf))
            t2, ms, cs = hud.fit_mixed(d, str(r["tag"]), int(key_size * 0.76),
                                       int(key_size * 0.76), max(40, inner - gap))
            hud.draw_tracked(d, cx0 + gap, y + 3, t2, ms, cs, hud.MUTED, tracking=1.5)
        value = str((r.get("values") or [""])[0])
        size = num_size
        while size > 24 and hud.value_width(d, value, size, unit_size) > inner:
            size -= 2
        hud.draw_value(d, cx0, y + key_size + 4, value, size, unit_size)
        y += rh
        hud.rule(d, cx0, cx1, y - 10, hud.RULE_COLOR, 2)
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    img.save(dest)
    return dest


def highlight(lines: Sequence[str], size: tuple[int, int], dest: str | Path) -> Path:
    """两行大字关键词,压在小窗画面的下沿。第一行白、第二行强调色,粗描边。"""
    if aspect.is_portrait_size(size):
        return overlays_portrait.highlight(lines, size, dest)
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
                dest: str | Path, headline: str = "") -> Path:
    """底部章节条:按各章时长分宽,当前章高亮。`chapters` = [(名字, 时长秒), ...]。

    竖屏下画在标题区,并带上两行大标题 `headline`(横屏不用它)。

    宽度按时长分,但每章至少给能放下名字的宽度 —— 开头、结尾往往只有一两拍,
    纯按比例会窄到写不下字。
    """
    if aspect.is_portrait_size(size):
        return overlays_portrait.chapter_bar(chapters, current, size, dest, headline)
    w, h = size
    img, d = _canvas(size)
    bar_h = int(h * BAR_HEIGHT)
    top = h - bar_h
    d.rectangle((0, top, w, h), fill=_BAR_BG)
    if not chapters:
        img.save(dest)
        return Path(dest)
    font_size = max(BAR_FONT_MIN, int(bar_h * 0.52))
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
#: "4D 毫米波""5G""8K" 里的一位数是名字不是参数,不拿来滚动
_NUM_RE = r"-?(?!\d[DGK](?![A-Za-z]))\d+(?:\.\d+)?"
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
    if aspect.is_portrait_size(size):
        return overlays_portrait.stat(overlay, subject, size, dest, progress)
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
    # "所以呢":这个数字对观众意味着什么。有它就把口径和出处往下让一行
    shift = 0.0
    if overlay.get("note"):
        note, nof = _fit(d, str(overlay["note"]), int(h * 0.05), max_w, bold=True)
        d.text((w // 2, int(h * 0.615)), note, font=nof, fill=_TEXT, anchor="mm",
               stroke_width=5, stroke_fill=(0, 0, 0, 255))
        shift = 0.08
    if overlay.get("tag"):
        # 口径 + 来源类型("CLTC · 标准版 | 官方"):大数字不带口径,懂车的人第一反应就是"哪个工况?"
        tag_txt, tgf = _fit(d, str(overlay["tag"]), int(h * 0.034), max_w, bold=True)
        d.text((w // 2, int(h * (0.60 + shift))), tag_txt, font=tgf, fill=_TEXT, anchor="mm",
               stroke_width=3, stroke_fill=(0, 0, 0, 255))
    src, sf = _fit(d, overlay.get("source", ""), 24, max_w)
    d.text((w // 2, int(h * (0.66 + shift))), src, font=sf, fill=_MUTED, anchor="mm")
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
    if aspect.is_portrait_size(size):
        return overlays_portrait.sting(number, name, size, dest, wipe, alpha)
    w, h = size
    img, d = _canvas(size)
    x0, y0 = int(w * 0.035), int(h * 0.04)
    bh = int(h * 0.085)
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
    if aspect.is_portrait_size(size):
        return overlays_portrait.lower_third(subject, chapter, size, dest)
    w, h = size
    img, d = _canvas(size)
    x0, y0 = int(w * 0.035), int(h * 0.04)
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


# --------------------------------------------------------------------------
# 黄金三秒:开场大字(0 秒起弹出,无淡入;停留后快速淡出)
# --------------------------------------------------------------------------
HOOK_POP_STEPS = 5
HOOK_POP_SECONDS = 0.2
HOOK_FADE_STEPS = 3
HOOK_FADE_SECONDS = 0.3
#: 弹出起始放大倍数,缩回 1.0 时停稳
HOOK_POP_FROM = 1.35


def hook_text(overlay: dict[str, Any], subject: str, size: tuple[int, int], dest: str | Path,
              scale: float = 1.0, alpha: float = 1.0) -> Path:
    """开场大字。有 `stat` 时:上面小字标签、中央大数字,关键词压在下面;
    没有就只有一两行关键词居中。数字取原文,不滚动(三秒里没有时间滚)。"""
    if aspect.is_portrait_size(size):
        return overlays_portrait.hook_text(overlay, subject, size, dest, scale, alpha)
    w, h = size
    img, d = _canvas(size)
    max_w = int(w * 0.86)
    lines = [str(x).strip() for x in overlay.get("lines") or [] if str(x).strip()][:2]
    stat_ = overlay.get("stat") if isinstance(overlay.get("stat"), dict) else None
    edge = (0, 0, 0, 255)
    if stat_:
        label = str(stat_.get("label") or "")
        tag, tf = _fit(d, f"{subject} · {label}" if subject else label,
                       int(54 * scale), max_w, bold=True)
        d.text((w // 2, int(h * 0.24)), tag, font=tf, fill=_TEXT, anchor="mm",
               stroke_width=4, stroke_fill=edge)
        num, nf = _fit(d, str(stat_.get("value") or ""), int(230 * scale), max_w, bold=True)
        d.text((w // 2, int(h * 0.42)), num, font=nf, fill=_ACCENT, anchor="mm",
               stroke_width=11, stroke_fill=edge)
        y, sizes = int(h * 0.60), (96, 84)
    else:
        y, sizes = int(h * (0.34 if len(lines) > 1 else 0.40)), (150, 124)
    for n, text in enumerate(lines):
        px = int(sizes[n] * scale)
        t, f = _fit(d, text, px, max_w, bold=True)
        d.text((w // 2, y), t, font=f, fill=_TEXT if n == 0 else _ACCENT, anchor="mt",
               stroke_width=max(4, px // 12), stroke_fill=edge)
        y += int(px * 1.2)
    if alpha < 1:
        a = img.getchannel("A").point(lambda v: int(v * max(0.0, alpha)))
        img.putalpha(a)
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    img.save(dest)
    return dest


def hook_frames(overlay: dict[str, Any], subject: str, size: tuple[int, int],
                out_dir: str | Path, total: float = 3.0) -> list[tuple[Path, float]]:
    """开场大字关键帧:[(透明 PNG, 停留秒数)],总长 = `total`。
    弹出(1.35×→1.0×)→ 停留 → 淡出,最后一帧全透明。"""
    from .cards import ease_out

    out_dir = Path(out_dir)
    frames: list[tuple[Path, float]] = []
    step = HOOK_POP_SECONDS / HOOK_POP_STEPS
    for k in range(HOOK_POP_STEPS):
        s = HOOK_POP_FROM - (HOOK_POP_FROM - 1.0) * ease_out((k + 1) / HOOK_POP_STEPS)
        frames.append((hook_text(overlay, subject, size, out_dir / f"hook_p{k:02d}.png", s), step))
    hold = max(0.2, total - HOOK_POP_SECONDS - HOOK_FADE_SECONDS)
    frames[-1] = (frames[-1][0], hold)
    fstep = HOOK_FADE_SECONDS / HOOK_FADE_STEPS
    for k in range(1, HOOK_FADE_STEPS + 1):
        frames.append((hook_text(overlay, subject, size, out_dir / f"hook_f{k:02d}.png",
                                 1.0, 1 - k / HOOK_FADE_STEPS), fstep))
    return frames
