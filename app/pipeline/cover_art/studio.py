"""影棚底图:生图、质检修正、渐变兜底。"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from .tone import BG_MAX_SPREAD, RATIOS, STUDIO_PROMPT, Tone, VLM_CHECK, cache_dir, log, ratio_of


def _luma(image: Any) -> float:
    from PIL import ImageStat

    return float(ImageStat.Stat(image.convert("L").resize((64, 64))).mean[0])


def _upper_spread(image: Any) -> float:
    from PIL import ImageStat

    g = image.convert("L").resize((64, 64))
    return float(ImageStat.Stat(g.crop((0, 0, 64, 32))).stddev[0])


def _vlm_says_clean(path: Path) -> bool:
    """问视觉模型图里有没有车 / 字。模型不可用(返回空)时放行 —— 亮度质检已经过了。"""
    from ...providers import cli, images

    try:
        answer = images.describe([cli.upload_temp_url(path)], VLM_CHECK).strip().upper()
    except Exception as exc:  # noqa: BLE001
        log.info("封面底图视觉质检不可用,跳过:%s", exc)
        return True
    return not (answer.startswith("YES") or "YES" in answer[:12] or answer.startswith(("是", "有")))


def _correct_bg(im: Any, luma: tuple[int, int]) -> Any:
    """把底图**修正**到能压字的状态,而不是直接扔掉重画。

    生图出来的影棚底图亮度经常差一点、上半部又有光斑,直接判不合格就重画,
    实跑踩过:一张封面三个画幅各重画 3 次,9 次生图 + 9 次视觉调用全废,
    最后还是落到纯渐变兜底。这里改成:亮度拉进窗口,上半部太花就抹平 ——
    影棚背景本来就是渐变,抹平之后比纯色渐变还多一点层次。
    """
    from PIL import ImageEnhance, ImageFilter

    lo, hi = luma
    cur = _luma(im)
    if cur < lo:
        k = min(2.4, max(1.05, ((lo + hi) / 2) / max(cur, 1)))
        im = ImageEnhance.Brightness(im).enhance(k)
    elif cur > hi:
        k = max(0.4, ((lo + hi) / 2) / max(cur, 1))
        im = ImageEnhance.Brightness(im).enhance(k)
    if _upper_spread(im) > BG_MAX_SPREAD:
        top = im.crop((0, 0, im.width, int(im.height * 0.6)))
        top = top.filter(ImageFilter.GaussianBlur(max(4, im.width // 70)))
        im = im.copy()
        im.paste(top, (0, 0))
    return im


def generate(prompt: str, ratio: str, dest: Path, *, luma: tuple[int, int] = (150, 250),
             retries: int = 1) -> Path | None:
    """按提示词生一张底图并质检,合格才写到 dest(已有就直接用)。失败返回 None。

    质检不过先**修正**再判一次(见 `_correct_bg`),修正后合格就用它;
    底图里有车 / 字 / 水印(修正也去不掉的)就不用,交给渐变兜底 ——
    宁可背景朴素,也不能出现一个糊在字后面的车影。
    """
    from PIL import Image

    from ...providers import images

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
            fixed = _correct_bg(im, luma)
            if not luma[0] <= _luma(fixed) <= luma[1] or _upper_spread(fixed) > BG_MAX_SPREAD:
                log.info("封面底图亮度 / 干净度修正后仍不合格,重来(%s)", dest.name)
                continue
            tmp = dest.with_name(f"{dest.stem}.part{dest.suffix}")
            fixed.save(tmp, quality=94)
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
