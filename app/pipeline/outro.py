"""片尾卡:成片末尾的一张静态结尾卡(行动号召 + 账号署名)。

旁白说完之后才出现,所以不改前面任何一拍的时间;音轨由 `render.compose(tail_silent=True)`
补静音,BGM 托底并在最后淡出。素材和署名都来自配置,不写死品牌。
"""
from __future__ import annotations

import logging
import re
from pathlib import Path

from .. import config
from . import render as render_mod
from .narration import _font_file

log = logging.getLogger(__name__)

_BG = (16, 24, 38)
_FG = (255, 255, 255)
_SUB = (170, 182, 200)
#: 卡片淡入秒数
FADE_IN = 0.35


def _wrap(text: str, per_line: int) -> list[str]:
    """先按逗号、空格断成意群(一行一个意群),意群太长才按字数均分,不拆词。"""
    lines: list[str] = []
    for part in re.split(r"[,，、;；\s]+", text):
        if not part:
            continue
        n = -(-len(part) // per_line)  # 需要几行
        size = -(-len(part) // n)      # 均分,不留孤字
        lines += [part[k:k + size] for k in range(0, len(part), size)]
    return lines


def render_card(dest: Path, size: tuple[int, int], text: str, brand: str = "") -> Path | None:
    """画结尾卡的 PNG。Pillow 不可用或没有文字可画返回 None。"""
    text = (text or "").strip()
    if not text and not brand:
        return None
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError:
        return None
    w, h = size
    path = _font_file(bold=True)

    def font(px: int):
        try:
            return ImageFont.truetype(path, px) if path else ImageFont.load_default()
        except OSError:
            return ImageFont.load_default()

    img = Image.new("RGB", (w, h), _BG)
    draw = ImageDraw.Draw(img)
    big = font(max(28, min(w, h) // 11))
    small = font(max(20, min(w, h) // 20))
    per_line = max(6, int(w * 0.8 // big.size)) if hasattr(big, "size") else 12
    lines = _wrap(text, per_line)
    line_h = int(getattr(big, "size", 40) * 1.35)
    block = line_h * len(lines) + (int(getattr(small, "size", 24) * 2.2) if brand else 0)
    y = (h - block) // 2
    for line in lines:
        tw = draw.textlength(line, font=big)
        draw.text(((w - tw) / 2, y), line, font=big, fill=_FG)
        y += line_h
    if brand:
        y += int(getattr(small, "size", 24) * 0.6)
        tw = draw.textlength(brand, font=small)
        draw.text(((w - tw) / 2, y), brand, font=small, fill=_SUB)
    dest.parent.mkdir(parents=True, exist_ok=True)
    img.save(dest)
    return dest


def build(project_dir: Path, size: tuple[int, int], seconds: float | None = None) -> tuple[Path, float] | None:
    """做出片尾卡短片,返回 (路径, 时长);没开、没内容或渲染失败返回 None(不影响成片)。"""
    if not config.OUTRO_ENABLED:
        return None
    seconds = float(seconds or config.OUTRO_SECONDS)
    work = project_dir / "_outro"
    png = render_card(work / "outro.png", size, config.OUTRO_TEXT, config.COVER_BRAND)
    if png is None or not config.FFMPEG_PATH:
        return None
    clip = work / "outro.mp4"
    try:
        proc = render_mod._run(
            ["-y", "-v", "error", "-loop", "1", "-framerate", str(render_mod.FPS), "-i", str(png),
             "-vf", f"fade=t=in:st=0:d={FADE_IN},format=yuv420p",
             "-t", f"{seconds:.3f}", "-an", "-c:v", "libx264", "-preset", "medium", "-crf", "20",
             "-pix_fmt", "yuv420p", str(clip)],
            timeout=300,
        )
    except Exception:  # noqa: BLE001 —— 片尾卡只是加分项
        log.warning("片尾卡渲染异常", exc_info=True)
        return None
    if proc.returncode != 0 or not clip.is_file() or clip.stat().st_size == 0:
        log.warning("片尾卡渲染失败:%s", (proc.stderr or "")[-300:])
        return None
    return clip, seconds
