"""统一风格封面:浅色影棚底(MiniMax 生图)+ 上方超大粗字标题 + 下方真车官图抠图。

版式参考高赞选车号:浅色柔和渐变底,第一行深色粗体大字(车名 / 价格 / 问题),第二行强调色副标,
车放在下半部,完整、干净、占画面一半。信息一眼看完。

- 风格统一靠固定的 4 个色调 `TONES`(底色 + 主字色 + 强调色)和固定版式;项目按 id 落到某个色调,
  B 版换下一个色调,A/B 一眼分得开。
- 底图只生成**空影棚**(不出车不出字),每个色调 × 画幅只生一次,缓存在 `data/_cache/cover_theme/`,
  所有项目共用。生图失败用同色调的代码渐变垫底,底图永远拿得到。
- 车用素材池里的白底官图抠出来(`motion_assets.cutout`),车型不会画错。抠不出来(不是白底图、
  非车品类)返回 None,调用方退回 `narration.make_cover` 的老封面。封面不能拦住成片。
"""
from __future__ import annotations

import hashlib
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

from .. import config

log = logging.getLogger(__name__)

#: 改了提示词 / 色调就升版本号,旧图自然失效(不会混用两套风格)
STYLE_VERSION = 3


@dataclass(frozen=True)
class Tone:
    name: str
    #: 写进生图提示词的底色描述
    prompt: str
    #: 生图失败时代码渐变的上 / 下两色
    top: tuple[int, int, int]
    bottom: tuple[int, int, int]
    text: tuple[int, int, int]
    accent: tuple[int, int, int]
    #: 底图平均亮度的合格区间(浅底配深字要够亮,灰绿底配白字要够暗)
    luma: tuple[int, int] = (150, 250)

    @property
    def light_text(self) -> bool:
        return sum(self.text) > 600


TONES = (
    Tone("beige", "warm cream beige fading to soft sand", (240, 232, 218), (214, 202, 184),
         (32, 34, 38), (40, 98, 74)),
    Tone("sage", "muted sage grey-green, slightly deeper towards the floor", (112, 136, 130),
         (86, 108, 104), (250, 250, 247), (246, 214, 140), luma=(70, 150)),  # 再亮白字就压不住
    Tone("mist", "pale misty blue-grey", (226, 232, 238), (196, 205, 216),
         (30, 34, 42), (28, 84, 176)),
    Tone("silver", "light neutral silver grey", (232, 232, 230), (204, 204, 202),
         (30, 32, 36), (178, 56, 44)),
)

STUDIO_PROMPT = (
    "Minimalist seamless photo studio backdrop for a premium car advertisement, {tone} colour, "
    "smooth even wall with a gentle vertical gradient in the upper two thirds, light matte floor "
    "in the lower third with a soft subtle reflection and diffused light pooling in the centre, "
    "clean, airy, high-end commercial look, photorealistic, 8k. Completely empty scene: no car, "
    "no vehicle, no objects, no people, no text, no letters, no logo, no watermark."
)

RATIOS = {"16x9": "16:9", "9x16": "9:16", "3x4": "3:4"}

#: 上半部(压字的地方)亮度标准差上限:太花字就压不住
BG_MAX_SPREAD = 26
VLM_CHECK = ("Does this image contain a car or any vehicle, readable text or letters, a logo, "
             "or a watermark? Answer with only YES or NO.")



# --------------------------------------------------------------------------
# 底图:生图与缓存
# --------------------------------------------------------------------------
def cache_dir() -> Path:
    return config.DATA_DIR / "_cache" / "cover_theme" / f"v{STYLE_VERSION}"


def ratio_of(size: tuple[int, int]) -> str:
    w, h = size
    if w >= h:
        return "16x9"
    return "9x16" if h / w > 1.5 else "3x4"


def bg_index(project_id: str, variant: str = "a") -> int:
    """色调下标:同一项目每次都一样;B 版取下一个,保证和 A 不同。"""
    base = int(hashlib.sha1(project_id.encode("utf-8")).hexdigest(), 16) % len(TONES)
    return (base + (1 if variant == "b" else 0)) % len(TONES)


def tone_of(project_id: str, variant: str = "a") -> Tone:
    return TONES[bg_index(project_id, variant)]


def _luma(image: Any) -> float:
    from PIL import ImageStat

    return float(ImageStat.Stat(image.convert("L").resize((64, 64))).mean[0])


def _upper_spread(image: Any) -> float:
    from PIL import ImageStat

    g = image.convert("L").resize((64, 64))
    return float(ImageStat.Stat(g.crop((0, 0, 64, 32))).stddev[0])


def _vlm_says_clean(path: Path) -> bool:
    """问视觉模型图里有没有车 / 字。模型不可用(返回空)时放行 —— 亮度质检已经过了。"""
    from ..providers import cli, images

    try:
        answer = images.describe([cli.upload_temp_url(path)], VLM_CHECK).strip().upper()
    except Exception as exc:  # noqa: BLE001
        log.info("封面底图视觉质检不可用,跳过:%s", exc)
        return True
    return not (answer.startswith("YES") or "YES" in answer[:12] or answer.startswith(("是", "有")))


def generate(prompt: str, ratio: str, dest: Path, *, luma: tuple[int, int] = (150, 250),
             retries: int = 2) -> Path | None:
    """按提示词生一张底图并质检,合格才写到 dest(已有就直接用)。失败返回 None。"""
    from PIL import Image

    from ..providers import images

    if dest.is_file():
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    for attempt in range(retries + 1):
        ref = images.generate(prompt, output_file=dest.name, aspect_ratio=RATIOS[ratio])
        if ref is None:
            log.warning("封面生图失败(%s,第 %d 次)", dest.name, attempt + 1)
            continue
        raw = dest.with_name(f"{dest.stem}.raw")
        try:
            images.download(ref.url, raw)
            with Image.open(raw) as im:
                im = im.convert("RGB")
            if not luma[0] <= _luma(im) <= luma[1] or _upper_spread(im) > BG_MAX_SPREAD:
                log.info("封面底图亮度 / 干净度不合格,重来(%s)", dest.name)
                continue
            tmp = dest.with_name(f"{dest.stem}.part{dest.suffix}")
            im.save(tmp, quality=94)
            if not _vlm_says_clean(tmp):
                tmp.unlink(missing_ok=True)
                log.info("封面底图里有车或字,重来(%s)", dest.name)
                continue
            os.replace(tmp, dest)
            return dest
        except Exception as exc:  # noqa: BLE001
            log.warning("封面底图下载/处理失败:%s", exc)
        finally:
            raw.unlink(missing_ok=True)
    return None


def studio_bg(tone: Tone, ratio: str) -> Path | None:
    """某色调某画幅的空影棚底图:缓存有就用,没有就生成。"""
    return generate(STUDIO_PROMPT.format(tone=tone.prompt), ratio,
                    cache_dir() / "studio" / f"{tone.name}_{ratio}.jpg", luma=tone.luma)


def gradient_bg(tone: Tone, size: tuple[int, int]) -> Any:
    """生图拿不到时的垫底:同色调的上下渐变 + 下方一片淡淡的地面光。"""
    from PIL import Image, ImageDraw, ImageFilter

    w, h = size
    im = Image.new("RGB", size)
    d = ImageDraw.Draw(im)
    for y in range(h):
        t = y / max(1, h - 1)
        d.line([(0, y), (w, y)], fill=tuple(int(a + (b - a) * t) for a, b in zip(tone.top, tone.bottom)))
    glow = Image.new("L", size, 0)
    ImageDraw.Draw(glow).ellipse((w * 0.1, h * 0.72, w * 0.9, h * 1.02), fill=70)
    glow = glow.filter(ImageFilter.GaussianBlur(max(w, h) // 12))
    im.paste(Image.new("RGB", size, (255, 255, 255)), (0, 0), glow)
    return im


def _fit_bg(path: Path, size: tuple[int, int]) -> Any:
    """底图按 cover 方式铺满。"""
    from PIL import Image

    out_w, out_h = size
    with Image.open(path) as im:
        im = im.convert("RGB")
    k = max(out_w / im.width, out_h / im.height)
    im = im.resize((int(im.width * k) + 1, int(im.height * k) + 1), Image.LANCZOS)
    left, top = (im.width - out_w) // 2, (im.height - out_h) // 2
    return im.crop((left, top, left + out_w, top + out_h))


def background(tone: Tone, size: tuple[int, int]) -> tuple[Any, str]:
    """(画布, "studio"|"gradient")"""
    bg = studio_bg(tone, ratio_of(size))
    if bg is not None:
        try:
            return _fit_bg(bg, size), "studio"
        except OSError as exc:
            log.warning("封面底图读不了,用渐变:%s", exc)
    return gradient_bg(tone, size), "gradient"


# --------------------------------------------------------------------------
# 车
# --------------------------------------------------------------------------
#: 封面车图的视角偏好:前 45° 最像"这台车",正尾最差
VIEW_RANK = {"exterior_45": 0, "exterior_front": 1, "exterior_side": 2, "rear": 4}


def pick_car(subject: str, exclude: Sequence[str | Path] = ()) -> tuple[Any, Path] | None:
    """挑一张能抠干净的整车官图:白底棚拍优先、官方优先、大图优先。返回 (Cutout, 原图)。"""
    from . import library, motion_assets

    if not subject:
        return None
    try:
        lib = library.Library.load(subject)
    except Exception:  # noqa: BLE001
        return None
    vocab = library.vocab_of(lib)
    hero = vocab.primary_views or set(vocab.views)
    used = {str(Path(p)) for p in exclude}
    cands = [i for i in lib.items
             if i.kind == "still" and i.usable and not i.small_still and Path(i.path).is_file()
             and i.view in hero
             and i.problem not in ("cropped", "unclear", "poster") and str(Path(i.path)) not in used]
    cands.sort(key=lambda i: (not library.is_studio_shot(i.path), VIEW_RANK.get(i.view, 9),
                              not i.official, i.target != "yes", -Path(i.path).stat().st_size, i.id))
    for item in cands[:10]:
        c = motion_assets.cutout(item.path, item_id=item.id)
        if c is not None:
            return c, Path(item.path)
    return None


def find_plates(im: Any) -> list[tuple[int, int, int, int]]:
    """找车身上印着别家标识的白色车牌(官图库常在车牌上打「汽车之家 AUTOHOME」)。

    车牌 = 车身下半部一块又白又匀的横长方块(宽占车宽 5%–25%,宽高比 1.8–5.5,近乎填满外框),
    中间有一些不白的字。空白车牌(没有字)不算。返回 [(x0, y0, x1, y1)]。"""
    import numpy as np
    from PIL import Image, ImageDraw, ImageFilter

    a = np.asarray(im.convert("RGBA")).astype(np.int32)
    r, g, b, alpha = a[..., 0], a[..., 1], a[..., 2], a[..., 3]
    luma = (r * 299 + g * 587 + b * 114) // 1000
    chroma = np.maximum(np.maximum(r, g), b) - np.minimum(np.minimum(r, g), b)
    white = (luma >= 200) & (chroma <= 40) & (alpha >= 250)
    # 闭运算把字的笔画补上,整块车牌连成一片
    m = Image.fromarray(np.where(white, 255, 0).astype(np.uint8), "L")
    m = m.filter(ImageFilter.MaxFilter(5)).filter(ImageFilter.MinFilter(5)).copy()
    H, W = white.shape
    found = []
    ys, xs = np.nonzero(np.asarray(m)[::3, ::3] == 255)
    for y, x in zip(ys * 3, xs * 3):
        if m.getpixel((int(x), int(y))) != 255:
            continue
        ImageDraw.floodfill(m, (int(x), int(y)), 128, thresh=0)
        comp = np.asarray(m) == 128
        m.paste(0, mask=Image.fromarray((comp * 255).astype(np.uint8)))
        yy, xx = np.nonzero(comp)
        x0, x1, y0, y1 = int(xx.min()), int(xx.max()) + 1, int(yy.min()), int(yy.max()) + 1
        w, h = x1 - x0, y1 - y0
        if not (W * 0.05 <= w <= W * 0.25 and 1.8 <= w / max(1, h) <= 5.5):
            continue
        fill = comp[y0:y1, x0:x1].mean()
        ink = 1 - white[y0:y1, x0:x1].mean()
        if fill >= 0.8 and 0.03 <= ink <= 0.5 and (y0 + y1) / 2 >= H * 0.4:
            # 字贴着牌边时闭运算会把那一边啃掉几像素:往外扩到亮色(牌底 + 字)结束的地方
            while y0 > 0 and luma[y0 - 1, x0:x1].mean() > 150:
                y0 -= 1
            while y1 < H and luma[y1, x0:x1].mean() > 150:
                y1 += 1
            while x0 > 0 and luma[y0:y1, x0 - 1].mean() > 150:
                x0 -= 1
            while x1 < W and luma[y0:y1, x1].mean() > 150:
                x1 += 1
            found.append((x0, y0, x1, y1))
    return found


def scrub_plates(im: Any) -> Any:
    """把车牌上的字抹成空白车牌:留一圈细牌框,里面填车牌底色(按纵向拟合的渐变,和原来的打光一致)。
    返回新图。"""
    import numpy as np
    from PIL import Image, ImageDraw, ImageFilter

    im = im.convert("RGBA")
    boxes = find_plates(im)
    if not boxes:
        return im
    im = im.copy()
    a = np.asarray(im)
    k = np.array([0.299, 0.587, 0.114])
    for x0, y0, x1, y1 in boxes:
        p = max(2, int((y1 - y0) * 0.07))
        ix0, iy0, ix1, iy1 = x0 + p, y0 + p, x1 - p, y1 - p
        rgb = a[iy0:iy1, ix0:ix1, :3].astype(np.float64)
        luma = rgb @ k
        # 只拿车牌底(最亮那批、去掉字)的像素,按行号拟合一条直线,得到上下的明暗渐变
        yy, _ = np.nonzero(luma >= np.percentile(luma, 75) - 10)
        cols = rgb[luma >= np.percentile(luma, 75) - 10]
        ramp = np.arange(iy1 - iy0)
        grad = np.stack([np.polyval(np.polyfit(yy, cols[:, c], 1), ramp) for c in range(3)], axis=1)
        patch = np.repeat(np.clip(grad, 0, 255)[:, None, :], ix1 - ix0, axis=1).astype(np.uint8)
        mask = Image.new("L", (ix1 - ix0, iy1 - iy0), 0)
        ImageDraw.Draw(mask).rectangle((1, 1, ix1 - ix0 - 2, iy1 - iy0 - 2), fill=255)
        im.paste(Image.fromarray(patch, "RGB"), (ix0, iy0), mask.filter(ImageFilter.GaussianBlur(1)))
    log.info("封面车图抹掉 %d 块带标识的车牌", len(boxes))
    return im


def _car_layer(cut: Any) -> Any:
    """抠图 PNG 裁到车身,抹掉车牌上的别家标识,边缘收一圈去掉白边。"""
    from PIL import Image, ImageFilter

    with Image.open(cut.png) as im:
        im = scrub_plates(im)
    a = im.getchannel("A").filter(ImageFilter.MinFilter(3)).filter(ImageFilter.GaussianBlur(0.6))
    im.putalpha(a)
    box = a.point(lambda v: 255 if v > 24 else 0).getbbox()
    return im.crop(box) if box else im


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
    from .narration import _font_file

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
    from .narration import _COVER_BREAK, _cover_wrap

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
    from .narration import _COVER_BREAK

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


def usable_hook(hook: str) -> str:
    """B 版大字只用带数字的钩子(价格、时长、续航)。主论点硬截出来的半句(「小米YU7版本跨」)
    放大成主视觉只会更难看,这种情况 B 版改排标题。"""
    from .packaging import figures_in

    # figures_in 会剥掉型号代号(YU7 的 7 不算数字)
    return hook if hook and len(hook) <= 8 and figures_in(hook) else ""


def model_word(name: str) -> str:
    """车名里的型号(「小米YU7」→「YU7」「理想i9」→「i9」),当车身背后的大字;没有就不画。"""
    import re

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


# --------------------------------------------------------------------------
# 对外入口
# --------------------------------------------------------------------------
def _save(image: Any, dest: Path) -> None:
    tmp = dest.with_name(f"{dest.stem}.part{dest.suffix}")
    try:
        image.save(tmp, quality=93)
        os.replace(tmp, dest)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def compose(cut: Any, tone: Tone, size: tuple[int, int], *, main: str, sub: str = "",
            name: str = "", brand: str = "") -> tuple[Any, str]:
    """底图 → 型号大字 + 车 → 字。返回 (图, 底图来源)。"""
    canvas, source = background(tone, size)
    place_car(canvas, cut, Frame.of(size), tone=tone, ghost=model_word(name))
    chip = name if name and name not in main and name not in sub else ""
    return render(canvas, tone, main=main, sub=sub, chip=chip, brand=brand), source


def build(project_id: str, dest: str | Path, *, size: tuple[int, int], title: str, subject: str,
          kicker: str = "", variant: str = "a", hook: str = "",
          exclude: Sequence[str | Path] = ()) -> dict[str, Any] | None:
    """出一版封面,同时按 3:4 和 4:3 各重排一张(小红书 3:4;抖音网页版要横 4:3 + 竖 3:4)。
    成功返回 {"path", "tone", "source", "still"},否则 None。

    B 版:有带数字的钩子就钩子当大字、标题当副标;`exclude` 传 A 版用过的官图,尽量换一张车图。"""
    from .narration import cover_3x4_path, cover_4x3_path

    if not config.COVER_ART or not title:
        return None
    try:
        car = (pick_car(subject, exclude) if exclude else None) or pick_car(subject)
        if car is None:
            log.info("封面:%s 没有能抠的白底官图,退回老封面", subject)
            return None
        cut, still = car
        hook = usable_hook(hook) if variant == "b" else ""
        main, sub = (hook, title) if hook else split_title(title, short_name(kicker))
        tone = tone_of(project_id, variant)
        short = min(size)
        jobs = [(Path(dest), size),
                (cover_3x4_path(dest), (short, short * 4 // 3)),
                (cover_4x3_path(dest), (short * 4 // 3, short))]
        name = short_name(kicker)
        rendered = [(path, compose(cut, tone, sz, main=main, sub=sub, name=name, brand=config.COVER_BRAND))
                    for path, sz in jobs]
        for path, (im, _) in rendered:
            _save(im, path)
        return {"path": str(dest), "tone": tone.name, "source": rendered[0][1][1],
                "still": str(still), "main": main, "sub": sub}
    except Exception as exc:  # noqa: BLE001
        log.warning("统一风格封面失败,退回老封面:%s", exc)
        return None


def pick_title(pkg: Any, fallback: str) -> str:
    """封面大标题:包装里选中的那条(≤18 字、数字已核对);没有包装用片名。"""
    if pkg is not None and getattr(pkg, "titles", None):
        idx = pkg.picked.get("title", 0)
        idx = idx if isinstance(idx, int) and 0 <= idx < len(pkg.titles) else 0
        return pkg.titles[idx].text
    return fallback


def b_title(pkg: Any, title_a: str, hook: str) -> str:
    """B 版的标题:大字出自哪条就用哪条;没有能用的大字就换一条**不同写法**的标题,
    让 A/B 的差别落在文案上,而不是同一句话换个底色。"""
    from . import packaging

    if pkg is None or not getattr(pkg, "titles", None):
        return title_a
    if usable_hook(hook):
        return packaging.hook_title(pkg.titles, hook) or title_a
    return next((t.text for t in pkg.titles if t.text != title_a), title_a)


# --------------------------------------------------------------------------
# 命令行:给已完成的项目重出封面
# --------------------------------------------------------------------------
def rebuild(project_id: str) -> dict[str, Any]:
    """按现有 render.json / packaging.json 重画 A、B 封面,不重新合成视频。"""
    from .. import db
    from . import aspect, explainer, packaging

    project = db.get_project(project_id) or {}
    rendered = db.read_json(project_id, "render.json", {}) or {}
    if not project or not rendered:
        return {"ok": False, "reason": "项目不存在或还没成片"}
    meta = rendered.get("cover_meta") or {}
    size = aspect.frame_size(project)
    subject = str(project.get("subject") or "")
    pkg = packaging.load(project_id)
    title_a = pick_title(pkg, str(project.get("title") or ""))
    out: dict[str, Any] = {"ok": True}
    a = build(project_id, config.project_dir(project_id) / aspect.cover_name(project),
              size=size, title=title_a, subject=subject, kicker=meta.get("kicker", ""))
    out["a"] = a
    price, _ = explainer._price_of(db.read_json(project_id, "facts.json", []) or [])
    hook = packaging.cover_hook(pkg.titles if pkg else [], price=price,
                                thesis=str(project.get("thesis") or ""))
    if hook and a:
        out["b"] = build(project_id, config.project_dir(project_id) / aspect.cover_name(project, "b"),
                         size=size, title=b_title(pkg, title_a, hook), subject=subject,
                         kicker=meta.get("kicker", ""), variant="b", hook=hook, exclude=[a["still"]])
    if a:
        rendered["cover_style"] = "art"
        rendered["cover_art"] = {k: v for k, v in (("a", a), ("b", out.get("b"))) if v}
        db.write_json(project_id, "render.json", rendered)
    return out


def main(argv: Sequence[str] | None = None) -> int:
    import argparse
    import json

    from .. import db

    ap = argparse.ArgumentParser(description="统一风格封面:给已成片的项目重出封面")
    ap.add_argument("--project", action="append", default=[], help="重出这个项目的封面(可多次)")
    ap.add_argument("--all", action="store_true", help="重出所有已成片项目的封面")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    ids = list(args.project)
    if args.all:
        ids += [p["id"] for p in db.list_projects(limit=1000)
                if db.read_json(p["id"], "render.json", None)]
    for pid in dict.fromkeys(ids):
        print(pid, json.dumps(rebuild(pid), ensure_ascii=False, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
