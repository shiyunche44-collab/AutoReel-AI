"""HUD 版式 —— 对标 `motion/bakeoff/C.html`(风格 C · 极简科技 HUD)。

这一套是本片自己的视觉识别:浅色工程图纸 + 发丝网格 + 等宽数字 + 橙色强调 +
取景框角标。原先的深色半透明面板 + 黄色竖条是「AI 参数解说号」的通用语言,
满街都是 —— 复盘里说的「越像他,越不会被记住」就是指这个。

配色与字号全部照搬 bakeoff C:

============  ======  ===========================================
元素          取值     来自
============  ======  ===========================================
纸面          #eceee9  ``.scene { background-color: #eceee9 }``
墨色          #12161a  ``#root { color: #12161a }``
次级灰        #5d656e  ``.tag { color: #5d656e }``
强调橙        #ff5a1f  ``.acc { color: #ff5a1f }``
网格线        7% 墨色 ``rgba(18,22,26,0.07)``
标签字距      0.14em   ``.tag { letter-spacing: 0.14em }``
============  ======  ===========================================

混排:数字和拉丁字母走等宽字体(Consolas;机器上装了 JetBrains Mono 就优先用它),
中文走系统黑体 —— Consolas 没有汉字,直接画会出现豆腐块。
"""
from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

from .. import config
from . import cards

# --------------------------------------------------------------------------
# 配色
# --------------------------------------------------------------------------
#: 纸面底色
PAPER = (236, 238, 233)
#: 墨色(正文)
INK = (18, 22, 26)
#: 参数名的次级色(比 bakeoff C 的 .row .k 深一点,小字在纸上要更实)
KEY = (61, 68, 76)
#: 次级灰(单位、来源行)
MUTED = (93, 101, 110)
#: 强调橙
ACCENT = (255, 90, 31)
#: 纸面不透明度:留一点点底,不像贴上去的白纸片
PAPER_ALPHA = 244


def rgba(color: tuple[int, int, int], alpha: int = 255) -> tuple[int, int, int, int]:
    """3 元组颜色补上 alpha。"""
    return (color[0], color[1], color[2], alpha)


def mix(base: tuple[int, int, int], ink: tuple[int, int, int], t: float) -> tuple[int, int, int]:
    """两个颜色按比例混 —— 纸上画淡线用。

    PIL 的 draw 是直接写像素,不做 alpha 混合:直接画一个 alpha=18 的墨色,
    等于在纸上戳了一个近乎透明的洞,叠到画面上就成了黑线。所以纸上所有
    淡色都预先混成不透明的实色。
    """
    return tuple(int(round(base[i] + (ink[i] - base[i]) * t)) for i in range(3))


#: 纸上的淡色(都已预先混成实色)
GRID_COLOR = mix(PAPER, INK, 0.07)
RULE_COLOR = mix(PAPER, INK, 0.26)
EDGE_COLOR = mix(PAPER, INK, 0.55)
#: 纸上小字用的深一档橙:纯橙配浅纸只有 3:1 对比,小字看不清
ACCENT_DARK = mix(ACCENT, INK, 0.24)


# --------------------------------------------------------------------------
# 字体
# --------------------------------------------------------------------------
#: 等宽字体候选:(文件名, 是否粗体)。JetBrains Mono 是 bakeoff C 用的那款,
#: 机器上没装就退到 Consolas —— 都是等宽,数字对齐效果差别不大。
_MONO_FILES = {
    False: ("JetBrainsMono-Regular.ttf", "consola.ttf", "cour.ttf", "lucon.ttf"),
    True: ("JetBrainsMono-Bold.ttf", "consolab.ttf", "courbd.ttf"),
}


def mono_font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    """等宽字体(数字、拉丁、标签)。找不到任何一款就用系统黑体,别画成豆腐块。"""
    fonts = Path(config.FONTS_DIR)
    for name in _MONO_FILES[bold]:
        p = fonts / name
        if p.is_file():
            try:
                return ImageFont.truetype(str(p), size)
            except OSError:
                continue
    return cards._font(size, bold)


def cjk_font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    """中文/全角字体。"""
    return cards._font(size, bold)


def _is_latin(ch: str) -> bool:
    """能不能用等宽字体画。拉丁字母、数字、常见半角符号走等宽,其余走中文字体。"""
    return ord(ch) < 0x0250


def _runs(text: str) -> list[tuple[str, str]]:
    """把一段文字切成 (字体种类, 片段) 序列。"""
    out: list[tuple[str, str]] = []
    for ch in text:
        kind = "mono" if _is_latin(ch) else "cjk"
        if out and out[-1][0] == kind:
            out[-1] = (kind, out[-1][1] + ch)
        else:
            out.append((kind, ch))
    return out


# --------------------------------------------------------------------------
# 文字
# --------------------------------------------------------------------------
def mixed_width(draw: ImageDraw.ImageDraw, text: str, mono_size: int, cjk_size: int,
                 mono_bold: bool = True) -> int:
    """混排文字的宽度。"""
    total = 0
    for kind, part in _runs(text):
        f = mono_font(mono_size, mono_bold) if kind == "mono" else cjk_font(cjk_size)
        total += draw.textlength(part, font=f)
    return int(total)


def tracked_width(draw: ImageDraw.ImageDraw, text: str, mono_size: int, cjk_size: int,
                  tracking: float, mono_bold: bool = False) -> int:
    """带字距展开的文字宽度。"""
    total = 0.0
    for ch in text:
        f = mono_font(mono_size, mono_bold) if _is_latin(ch) else cjk_font(cjk_size)
        total += draw.textlength(ch, font=f) + tracking
    return int(total)


def draw_tracked(draw: ImageDraw.ImageDraw, x: float, y: float, text: str,
                 mono_size: int, cjk_size: int, fill, tracking: float = 0.0,
                 mono_bold: bool = False) -> float:
    """逐字画,每字之间留 `tracking` 像素 —— bakeoff C 的标签字距。

    返回画完后的右边界 x。
    """
    cx = x
    for ch in text:
        f = mono_font(mono_size, mono_bold) if _is_latin(ch) else cjk_font(cjk_size)
        draw.text((cx, y), ch, font=f, fill=fill)
        cx += draw.textlength(ch, font=f) + tracking
    return cx


def fit_mixed(draw: ImageDraw.ImageDraw, text: str, mono_size: int, cjk_size: int,
              max_w: int, mono_bold: bool = False) -> tuple[str, int, int]:
    """缩小字号直到一行放得下,仍放不下就截断。返回 (文字, 等宽字号, 中文字号)。"""
    scale = 1.0
    while scale >= 0.55:
        ms = max(8, int(mono_size * scale))
        cs = max(8, int(cjk_size * scale))
        if mixed_width(draw, text, ms, cs, mono_bold) <= max_w:
            return text, ms, cs
        scale -= 0.05
    ms = max(8, int(mono_size * 0.55))
    cs = max(8, int(cjk_size * 0.55))
    keep = text
    while keep and mixed_width(draw, keep + "…", ms, cs, mono_bold) > max_w:
        keep = keep[:-1]
    return keep + "…", ms, cs


def value_width(draw: ImageDraw.ImageDraw, text: str, number_size: int, unit_size: int,
                mono_bold: bool = True) -> int:
    """取值的总宽度(数字 + 单位),用来判断字号要不要缩。"""
    import re

    text = str(text or "")
    m = re.search(r"[-]?[0-9][0-9,]*[.]?[0-9]*", text)
    if not m:
        f = cjk_font(number_size, bold=True)
        return int(draw.textlength(text, font=f))
    head, num, tail = text[:m.start()], m.group(0), text[m.end():]
    total = 0.0
    if head:
        total += draw.textlength(head, font=cjk_font(unit_size))
    total += draw.textlength(num, font=mono_font(number_size, mono_bold))
    if tail:
        uf = mono_font(unit_size) if all(_is_latin(c) for c in tail) else cjk_font(unit_size)
        total += draw.textlength(tail, font=uf) + number_size * 0.10
    return int(total)


def draw_value(draw: ImageDraw.ImageDraw, x: float, y: float, text: str,
               number_size: int, unit_size: int, number_fill=INK, unit_fill=MUTED,
               mono_bold: bool = True) -> float:
    """画一个取值:HUD C 的写法是「大号等宽数字 + 小一号灰单位」。

    `y` 是文字顶边。数字用等宽粗体,单位小一号(单位是拉丁就用等宽,
    是汉字就用中文字体),两者基线对齐。返回右边界 x。
    """
    import re

    text = str(text or "")
    m = re.search(r"[-]?[0-9][0-9,]*[.]?[0-9]*", text)
    if not m:
        # 没有数字(「五座」「全系标配」):整句用中文字体画
        f = cjk_font(number_size, bold=True)
        draw.text((x, y), text, font=f, fill=number_fill)
        return x + draw.textlength(text, font=f)
    head, num, tail = text[:m.start()], m.group(0), text[m.end():]
    nf = mono_font(number_size, mono_bold)
    try:
        ascent = nf.getmetrics()[0]
    except AttributeError:
        ascent = number_size
    base_y = y + ascent
    cx = x
    if head:
        hf = cjk_font(unit_size)
        draw.text((cx, base_y), head, font=hf, fill=unit_fill, anchor="ls")
        cx += draw.textlength(head, font=hf)
    draw.text((cx, base_y), num, font=nf, fill=number_fill, anchor="ls")
    cx += draw.textlength(num, font=nf)
    if tail:
        uf = mono_font(unit_size) if all(_is_latin(c) for c in tail) else cjk_font(unit_size)
        draw.text((cx + number_size * 0.10, base_y), tail, font=uf, fill=unit_fill, anchor="ls")
        cx += draw.textlength(tail, font=uf) + number_size * 0.10
    return cx


# --------------------------------------------------------------------------
# 图元
# --------------------------------------------------------------------------
def plate(draw: ImageDraw.ImageDraw, box: tuple[int, int, int, int],
          alpha: int = PAPER_ALPHA, grid_step: int = 58) -> None:
    """纸面 + 发丝网格。网格是 bakeoff C 画布上的那种 7% 墨色细线。"""
    x0, y0, x1, y1 = box
    draw.rectangle(box, fill=rgba(PAPER, alpha))
    if grid_step > 0:
        x = x0 + grid_step
        while x < x1:
            draw.line((x, y0, x, y1), fill=GRID_COLOR, width=1)
            x += grid_step
        y = y0 + grid_step
        while y < y1:
            draw.line((x0, y, x1, y), fill=GRID_COLOR, width=1)
            y += grid_step
    draw.rectangle(box, outline=EDGE_COLOR, width=2)


def brackets(draw: ImageDraw.ImageDraw, box: tuple[int, int, int, int],
             color=ACCENT, length: int = 44, width: int = 5) -> None:
    """取景框角标:bakeoff C 里给画面加的四个橙色 L 形角。

    角标贴着纸面的四个内角画(不是骑在边上),这样纸是一张完整的规格页,
    而不是被裁掉四个角。
    """
    x0, y0, x1, y1 = box
    w = width
    for (cx, cy, sx, sy) in ((x0, y0, 1, 1), (x1, y0, -1, 1),
                             (x0, y1, 1, -1), (x1, y1, -1, -1)):
        # 横臂:贴着上边或下边
        y_top = cy if sy > 0 else cy - w + 1
        x_a, x_b = sorted((cx, cx + sx * length))
        draw.rectangle((x_a, y_top, x_b, y_top + w - 1), fill=color)
        # 竖臂:贴着左边或右边
        x_left = cx if sx > 0 else cx - w + 1
        y_a, y_b = sorted((cy, cy + sy * length))
        draw.rectangle((x_left, y_a, x_left + w - 1, y_b), fill=color)


def rule(draw: ImageDraw.ImageDraw, x0: float, x1: float, y: float,
         color=RULE_COLOR, width: int = 2) -> None:
    """发丝分隔线。"""
    draw.line((x0, y, x1, y), fill=color, width=width)


def ruler(draw: ImageDraw.ImageDraw, x0: float, x1: float, y: float,
          color=RULE_COLOR, step: int = 17, minor: int = 7, major: int = 15) -> None:
    """刻度尺:一条基线加长短相间的竖线。

    纯装饰 —— bakeoff C 的速度标尺也是这么画的。刻度间距固定,不表示任何数值,
    不给观众"这是进度条"的错觉。
    """
    draw.line((x0, y, x1, y), fill=color, width=1)
    n = 0
    x = x0
    while x <= x1:
        h = major if n % 5 == 0 else minor
        draw.line((x, y, x, y - h), fill=color, width=1)
        x += step
        n += 1


def shadow(size: tuple[int, int], box: tuple[int, int, int, int],
           alpha: int = 70, blur: int = 24, offset: int = 10) -> Image.Image:
    """纸面的投影:让规格纸浮在画面上,而不是贴在画面上。"""
    img = Image.new("RGBA", size, (0, 0, 0, 0))
    x0, y0, x1, y1 = box
    ImageDraw.Draw(img).rectangle((x0, y0 + offset, x1, y1 + offset), fill=(0, 0, 0, alpha))
    return img.filter(ImageFilter.GaussianBlur(blur))
