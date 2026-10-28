"""版式与排字:网格、字体、断行、放车。字体全局变量 HEAVY / BOLD 在这里读写。"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from ... import config

from .tone import Tone
from .plates import _car_layer


@dataclass(frozen=True)
class Frame:
    """版式网格。竖屏的内容全放在**中间 3:4 安全区**里:抖音主页宫格把 9:16 封面裁成中间的 3:4,
    出了安全区的字和车在主页上直接看不到。"""
    w: int
    h: int
    landscape: bool
    top: int      # 安全区上沿
    height: int   # 安全区高度
    margin: int

    @classmethod
    def of(cls, size: tuple[int, int]) -> "Frame":
        w, h = size
        if w >= h:
            return cls(w, h, True, 0, h, max(48, int(w * 0.05)))
        sh = min(h, w * 4 // 3)
        return cls(w, h, False, (h - sh) // 2, sh, max(40, int(w * 0.07)))

    def y(self, k: float) -> int:
        """安全区内的纵向位置(0 = 安全区上沿,1 = 下沿)。"""
        return int(self.top + self.height * k)


def _car_fit(car: Any, f: Frame) -> tuple[Any, int, int]:
    """车的尺寸和位置:竖屏占满宽度落在安全区下部,横屏放右半边。返回 (缩放后的车, x, 地面 y)。"""
    from PIL import Image

    if f.landscape:
        max_w, max_h, floor = f.w * 0.60, f.h * 0.60, f.y(0.90)
    else:
        max_w, max_h, floor = f.w * 1.0, f.height * 0.42, f.y(0.90)
    k = min(max_w / car.width, max_h / car.height)
    car = car.resize((max(1, int(car.width * k)), max(1, int(car.height * k))), Image.LANCZOS)
    x = int(f.w - f.margin * 0.5 - car.width) if f.landscape else (f.w - car.width) // 2
    return car, x, int(floor)


def _ghost(canvas: Any, f: Frame, tone: Tone, word: str, car_box: tuple[int, int, int, int]) -> None:
    """车身背后一行超大的型号字(YU7、i9),只比底色深一点点:杂志封面的层次感,也让型号一眼认得出。"""
    from PIL import Image, ImageDraw

    if not word:
        return
    x0, y0, x1, y1 = car_box
    target = (x1 - x0) * (0.98 if f.landscape else 0.92)
    font = _font(HEAVY, 100)
    font = _font(HEAVY, max(40, int(100 * target / max(1.0, font.getlength(word)))))
    left, top, right, bottom = font.getbbox(word)
    if bottom - top > (y1 - y0) * 0.9:  # 字太高会顶到标题区
        font = _font(HEAVY, int(font.size * (y1 - y0) * 0.9 / (bottom - top)))
        left, top, right, bottom = font.getbbox(word)
    # 字脚压到车身一半:上半截从车顶露出来,下半截藏在车后
    # 横屏右对齐车尾,别和左边的标题叠在一起
    gx = int(x1 - right) if f.landscape else int((x0 + x1) / 2 - (right + left) / 2)
    gy = int(y0 + (y1 - y0) * 0.45 - bottom)
    layer = Image.new("L", canvas.size, 0)
    ImageDraw.Draw(layer).text((gx, gy), word, font=font, fill=44 if tone.light_text else 38)
    ink = (255, 255, 255) if tone.light_text else tone.text
    canvas.paste(Image.new("RGB", canvas.size, ink), (0, 0), layer)


def place_car(canvas: Any, cut: Any, f: Frame, *, tone: Tone | None = None, ghost: str = "") -> None:
    """车落在地面线上:背后型号大字 → 淡倒影 → 接触阴影 → 车。直接画在 canvas 上。"""
    from PIL import Image, ImageChops, ImageDraw, ImageFilter

    car, x, floor = _car_fit(_car_layer(cut), f)
    y = floor - car.height
    if tone is not None:
        _ghost(canvas, f, tone, ghost, (x, y, x + car.width, floor))

    # 倒影:翻转后从 22% 渐隐到 0,只露车高的 30%
    ref_h = int(car.height * 0.30)
    ref = car.transpose(Image.FLIP_TOP_BOTTOM).crop((0, 0, car.width, ref_h))
    fade = Image.new("L", ref.size)
    fd = ImageDraw.Draw(fade)
    for yy in range(ref_h):
        fd.line([(0, yy), (ref.width, yy)], fill=int(56 * (1 - yy / max(1, ref_h)) ** 1.6))
    ref.putalpha(ImageChops.multiply(ref.getchannel("A"), fade))
    canvas.paste(ref, (x, floor - 2), ref)

    # 接触阴影:车底一条压扁的模糊椭圆
    cx = x + car.width / 2
    sh = Image.new("L", canvas.size, 0)
    sw, shh = car.width * 0.90, max(8, car.height * 0.10)
    ImageDraw.Draw(sh).ellipse((cx - sw / 2, floor - shh * 0.55, cx + sw / 2, floor + shh * 0.45),
                               fill=130)
    sh = sh.filter(ImageFilter.GaussianBlur(max(6, int(shh * 0.6))))
    canvas.paste(Image.new("RGB", canvas.size, (20, 20, 22)), (0, 0), sh)
    canvas.paste(car, (x, y), car)


# --------------------------------------------------------------------------
# 文字
# --------------------------------------------------------------------------
def _text(canvas: Any, xy: tuple[int, int], text: str, font: Any, fill: tuple[int, int, int],
          shadow: int = 0) -> None:
    """字;shadow > 0 时垫一层很淡的柔和投影(只有浅色字用)。"""
    from PIL import Image, ImageDraw, ImageFilter

    if not text:
        return
    if shadow:
        size = getattr(font, "size", 40)
        layer = Image.new("L", canvas.size, 0)
        ImageDraw.Draw(layer).text((xy[0], xy[1] + max(2, size // 24)), text, font=font, fill=shadow)
        layer = layer.filter(ImageFilter.GaussianBlur(max(3, size // 14)))
        canvas.paste(Image.new("RGB", canvas.size, (0, 0, 0)), (0, 0), layer)
    ImageDraw.Draw(canvas).text(xy, text, font=font, fill=fill)


def _face(weight: int) -> tuple[str | None, int]:
    """封面字体:思源黑体可变字重(Windows 自带 NotoSansSC-VF)能调到 Black,比雅黑粗体更有冲击力;
    没有就用雅黑粗体。返回 (路径, 字重)。"""
    from ..narration import _font_file

    vf = Path(config.FONTS_DIR) / "NotoSansSC-VF.ttf"
    return (str(vf), weight) if vf.is_file() else (_font_file(bold=True), 0)


HEAVY: Any = None  # 主标题 / 型号大字,首次用到时定下来
BOLD: Any = None   # 色带副标 / 角标


def _faces() -> None:
    global HEAVY, BOLD
    if HEAVY is None:
        HEAVY, BOLD = _face(900), _face(760)


def _font(spec: Any, size: int) -> Any:
    """spec:字体路径,或 (路径, 可变字重)。"""
    from PIL import ImageFont

    path, weight = spec if isinstance(spec, tuple) else (spec, 0)
    try:
        font = ImageFont.truetype(path, size) if path else ImageFont.load_default()
        if weight:
            font.set_variation_by_axes([weight])
        return font
    except (OSError, AttributeError):
        return ImageFont.load_default()


def _line_h(font: Any, k: float = 1.2) -> int:
    return int(getattr(font, "size", 40) * k)


def _ink(rgb: tuple[int, int, int]) -> float:
    return 0.299 * rgb[0] + 0.587 * rgb[1] + 0.114 * rgb[2]


#: 没有标点时的断行禁忌:量词 / 助词不能起行,指示词不能收行(「这几 / 个数字」)
_NO_START = set("个的了吗呢吧啊们着过得地么")
_NO_END = set("这那几每第")


def _soft_cuts(title: str) -> list[int]:
    """没有标点的标题能断的位置:不拆英文数字串(YU7、23.35),不违反上面的禁忌。
    只取中间一段,免得断成一长一短。"""
    n = len(title)
    out = []
    for k in range(max(2, n // 3), min(n - 1, n - n // 3) + 1):
        a, b = title[k - 1], title[k]
        if (a.isascii() and a.isalnum() or a == ".") and (b.isascii() and b.isalnum() or b == "."):
            continue
        if b in _NO_START or a in _NO_END:
            continue
        out.append(k)
    return out


def _title_lines(title: str, bold: str | None, max_width: int, biggest: int,
                 smallest: int = 56) -> tuple[list[str], Any]:
    """一段字最多两行:放得下就一行;否则断在标点 / 安全位置上,在 biggest..smallest 里取最大字号。"""
    from ..narration import _COVER_BREAK, _cover_wrap

    font = _font(bold, biggest)
    if font.getlength(title) <= max_width:
        return [title], font
    cuts = [k + 1 for k, ch in enumerate(title[:-1]) if ch in _COVER_BREAK] or _soft_cuts(title)
    if cuts:
        k = min(cuts, key=lambda c: (abs(c - len(title) / 2), -c))  # 一样近取靠后的,上行长一点
        halves = [title[:k].strip(), title[k:].strip()]
        for size in range(biggest, smallest - 1, -6):
            font = _font(bold, size)
            if font.getlength(title) <= max_width:
                return [title], font
            if all(font.getlength(x) <= max_width for x in halves):
                return halves, font
    size = biggest
    while True:
        font = _font(bold, size)
        if font.getlength(title) <= max_width:
            return [title], font
        lines = _cover_wrap(title, font, max_width, 2)
        fits = all(font.getlength(x) <= max_width for x in lines) and not lines[-1].endswith("…")
        if fits or size <= smallest:
            return lines, font
        size -= 6


def _fit(text: str, bold: str | None, max_width: int, biggest: int, smallest: int,
         one_line_min: int) -> tuple[list[str], Any]:
    """先缩字号保一行(不小于 one_line_min),放不下一行再按 `_title_lines` 断两行。
    「23.35万起」「小米YU7」拆成两行比字小一号难看得多。"""
    for size in range(biggest, one_line_min - 1, -4):
        font = _font(bold, size)
        if font.getlength(text) <= max_width:
            return [text], font
    return _title_lines(text, bold, max_width, one_line_min, smallest)


def short_name(kicker: str) -> str:
    """kicker「小米YU7 · 25.35万元起」→「小米YU7」"""
    return (kicker or "").split("·")[0].strip()


def split_title(title: str, name: str = "") -> tuple[str, str]:
    """标题拆成(主行大字, 强调色副标):断在第一处标点;没有标点就车名当主行、其余当副标。"""
    from ..narration import _COVER_BREAK

    title = title.strip()
    for k, ch in enumerate(title[:-1]):
        if ch in _COVER_BREAK and k >= 2:
            main, sub = title[:k].strip(), title[k + 1:].strip()
            if ch in "?？!！":
                main += ch
            return main, sub
    if name and name != title:
        rest = title[len(name):].strip(" :：") if title.startswith(name) else title
        return name, rest or title
    return title, ""


def strip_figure(text: str, figure: str) -> str:
    """副标里去掉已经当大字用的那个数字。

    B 版大字是价格「38.28万」,副标是整条标题「38.28万起,换电加420kW快充」——
    同一屏上这个数字出现两次,看着像手滑(实跑踩过)。这里把大字从副标里摘掉,
    连跟在后面的「起」「元起」一起摘,副标只留剩下的信息。
    """
    if not text or not figure or figure not in text:
        return text
    out = text.replace(figure, "", 1)
    out = out.lstrip(" :：,，、")
    for prefix in ("万元起", "元起", "起"):
        if out.startswith(prefix):
            out = out[len(prefix):]
            break
    return out.strip(" :：,，、")


def usable_hook(hook: str) -> str:
    """B 版大字只用带数字的钩子(价格、时长、续航)。主论点硬截出来的半句(「小米YU7版本跨」)
    放大成主视觉只会更难看,这种情况 B 版改排标题。"""
    from ..packaging import figures_in

    # figures_in 会剥掉型号代号(YU7 的 7 不算数字)
    return hook if hook and len(hook) <= 8 and figures_in(hook) else ""


def model_word(name: str) -> str:
    """车名里的型号(「小米YU7」→「YU7」「理想i9」→「i9」),当车身背后的大字;没有就不画。"""

    words = re.findall(r"[A-Za-z0-9][A-Za-z0-9.\-]*", name or "")
    word = max(words, key=len) if words else ""
    return word if 2 <= len(word) <= 6 else ""


def _pill(canvas: Any, xy: tuple[int, int], text: str, font: Any, fill: tuple[int, int, int],
          color: tuple[int, int, int], pad: tuple[float, float] = (0.5, 0.22),
          radius: float = 0.28) -> tuple[int, int]:
    """字压在圆角色块上(副标色带、车名角标)。xy 是色块左上角,返回色块 (宽, 高)。"""
    from PIL import ImageDraw

    size = getattr(font, "size", 40)
    left, top, right, bottom = font.getbbox(text)
    px, py = int(size * pad[0]), int(size * pad[1])
    bw, bh = int(right - left + px * 2), int(bottom - top + py * 2)
    d = ImageDraw.Draw(canvas)
    d.rounded_rectangle((xy[0], xy[1], xy[0] + bw, xy[1] + bh), radius=int(size * radius), fill=fill)
    d.text((xy[0] + px - left, xy[1] + py - top), text, font=font, fill=color)
    return bw, bh


def render(canvas: Any, tone: Tone, *, main: str, sub: str = "", chip: str = "",
           brand: str = "") -> Any:
    """排字(原地画,返回 canvas)。纯本地,单测直接调。

    信息层级(从上到下,一屏只讲一件事):
    1. 角标 —— 车名小胶囊,告诉划过的人「这条讲哪台车」(主标题已经是车名就不重复);
    2. 主标题 —— 最粗最大的一行:价格 / 车名 / 结论,缩字号也不拆行;
    3. 色带副标 —— 强调色实底 + 反白字:问题 / 冲突,缩略图里也最先被看到;
    4. 署名 —— 安全区右下角的账号名,主页一排封面靠它和固定版式认出同一个号。"""
    from PIL import Image, ImageDraw

    _faces()
    f = Frame.of(canvas.size)
    shadow = 90 if tone.light_text else 0
    band_text = tone.text if _ink(tone.accent) > 150 else (255, 255, 255)
    chip_fill, chip_text = tone.text, tone.top
    if f.landscape:
        col_w = int(f.w * 0.48)
        x, y = f.margin, f.y(0.12)
        mains, mf = _fit(main, HEAVY, col_w, int(f.h * 0.16), int(f.h * 0.07), int(f.h * 0.10))
        if chip:
            cf = _font(BOLD, int(f.h * 0.05))
            y += _pill(canvas, (x, y), chip, cf, chip_fill, chip_text)[1] + int(cf.size * 0.55)
        for ln in mains:
            top = mf.getbbox(ln)[1]
            _text(canvas, (x, y - top), ln, mf, tone.text, shadow)
            y += _line_h(mf, 1.08)
        y += int(mf.size * 0.10)
        if sub:
            subs, sf = _fit(sub, BOLD, col_w - int(f.h * 0.05), max(40, int(mf.size * 0.42)),
                            int(f.h * 0.04), int(f.h * 0.05))
            for ln in subs:
                y += _pill(canvas, (x, y), ln, sf, tone.accent, band_text)[1] + int(sf.size * 0.18)
    else:
        max_w = f.w - f.margin * 2
        y = f.y(0.07)
        mains, mf = _fit(main, HEAVY, max_w, int(f.w * 0.21), int(f.w * 0.09), int(f.w * 0.12))
        if chip:
            cf = _font(BOLD, int(f.w * 0.042))
            bw = cf.getbbox(chip)[2] - cf.getbbox(chip)[0] + int(cf.size * 1.0)
            y += _pill(canvas, ((f.w - bw) // 2, y), chip, cf, chip_fill, chip_text)[1] \
                + int(cf.size * 0.5)
        for ln in mains:
            left, top, right, _ = mf.getbbox(ln)
            _text(canvas, (int((f.w - (right - left)) / 2 - left), y - top), ln, mf, tone.text, shadow)
            y += _line_h(mf, 1.06)
        y += int(mf.size * 0.12)
        if sub:
            subs, sf = _fit(sub, BOLD, max_w - int(f.w * 0.08), max(56, int(mf.size * 0.46)),
                            int(f.w * 0.05), int(f.w * 0.065))
            for ln in subs:
                l, _, r, _ = sf.getbbox(ln)
                bw = r - l + int(sf.size * 1.0)
                y += _pill(canvas, ((f.w - bw) // 2, y), ln, sf, tone.accent, band_text)[1] \
                    + int(sf.size * 0.18)
    if brand:
        bf = _font(BOLD, int(min(f.w, f.h) * 0.032))
        l, t, r, b = bf.getbbox(brand)
        bx, by = f.w - f.margin - (r - l), f.y(0.965) - (b - t)
        layer = Image.new("L", canvas.size, 0)
        ImageDraw.Draw(layer).text((bx - l, by - t), brand, font=bf, fill=150)
        canvas.paste(Image.new("RGB", canvas.size, tone.text), (0, 0), layer)
    return canvas
