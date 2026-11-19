"""审片用的拼图(接触印样):几张缩略图排成网格,每格下面写几行说明。

Claude 用 Read 看图挑素材、过刀表,比一张张开文件快得多。说明文字用剪辑字体
(`render.font_file()`,中文黑体),没有就退回 PIL 默认字体(中文会显示成方块,但编号还在)。
"""
from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from .. import render as render_mod
from . import render as edit_render

#: 每格宽(像素);高按 16:9 算,竖屏素材居中缩放进去
TILE_WIDTH = 360
#: 每格下面说明区的行高 / 最多几行
LINE_HEIGHT = 22
MAX_LINES = 3
COLUMNS = 4
#: 一张拼图最多几格,多了分几张
PER_SHEET = 16


def _font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    path = edit_render.font_file()
    if path is not None:
        try:
            return ImageFont.truetype(str(path), size)
        except OSError:
            pass
    return ImageFont.load_default()


def frame_of(src: Path, at: float, dest: Path, still: bool = False) -> Path | None:
    """取视频某一时刻的一帧(静图直接用原图)。缓存在 `dest`。"""
    if still or src.suffix.lower() in (".jpg", ".jpeg", ".png", ".webp"):
        return src if src.is_file() else None
    if dest.is_file():
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    proc = render_mod._run(["-ss", f"{max(at, 0.0):.2f}", "-i", str(src), "-frames:v", "1",
                            "-vf", f"scale={TILE_WIDTH}:-2", "-q:v", "4", "-y", str(dest)],
                           timeout=60)
    return dest if proc.returncode == 0 and dest.is_file() else None


def _clip(text: str, font: ImageFont.ImageFont, width: int) -> str:
    if font.getlength(text) <= width:
        return text
    while text and font.getlength(text + "…") > width:
        text = text[:-1]
    return text + "…"


def build(tiles: Sequence[tuple[Path | None, Sequence[str]]], dest: Path) -> list[Path]:
    """`tiles` = [(图片路径或 None, 说明行)];按 `PER_SHEET` 分张,写成 `dest` 同名加序号的 jpg。
    返回写出的文件。图片读不了的格子画成灰底,说明照写。"""
    out: list[Path] = []
    tile_h = TILE_WIDTH * 9 // 16
    cell_h = tile_h + LINE_HEIGHT * MAX_LINES + 8
    font = _font(16)
    dest.parent.mkdir(parents=True, exist_ok=True)
    for n in range(0, len(tiles), PER_SHEET):
        chunk = tiles[n:n + PER_SHEET]
        rows = (len(chunk) + COLUMNS - 1) // COLUMNS
        sheet = Image.new("RGB", (COLUMNS * TILE_WIDTH, rows * cell_h), (18, 18, 20))
        draw = ImageDraw.Draw(sheet)
        for k, (img, lines) in enumerate(chunk):
            x, y = (k % COLUMNS) * TILE_WIDTH, (k // COLUMNS) * cell_h
            draw.rectangle([x + 2, y + 2, x + TILE_WIDTH - 3, y + tile_h - 1], fill=(48, 48, 52))
            if img is not None and Path(img).is_file():
                try:
                    with Image.open(img) as im:
                        im = im.convert("RGB")
                        im.thumbnail((TILE_WIDTH - 4, tile_h - 3))
                        sheet.paste(im, (x + (TILE_WIDTH - im.width) // 2,
                                         y + 2 + (tile_h - 3 - im.height) // 2))
                except OSError:
                    pass
            for j, line in enumerate(list(lines)[:MAX_LINES]):
                draw.text((x + 6, y + tile_h + 4 + j * LINE_HEIGHT),
                          _clip(str(line), font, TILE_WIDTH - 12), fill=(235, 235, 235), font=font)
        path = dest if len(tiles) <= PER_SHEET else dest.with_name(
            f"{dest.stem}_{n // PER_SHEET + 1}{dest.suffix}")
        sheet.save(path, quality=86)
        out.append(path)
    return out
