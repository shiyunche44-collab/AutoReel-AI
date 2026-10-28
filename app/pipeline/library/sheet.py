"""联系表:把池子里的帧拼成大图人工过一遍。"""
from __future__ import annotations

from pathlib import Path

from .model import Library


# --------------------------------------------------------------------------
# 预览:联系表
# --------------------------------------------------------------------------
def contact_sheet(subject: str, dest: str | Path, *, usable_only: bool = False,
                  cols: int = 6, cell: int = 320) -> Path | None:
    """把池子里的帧拼成一张大图,每格标视角、车型判定和问题,方便人工过一遍。"""
    from PIL import Image, ImageDraw, ImageFont

    lib = Library.load(subject)
    items = [i for i in lib.items if i.frame and Path(i.frame).is_file()
             and (i.usable or not usable_only)]
    if not items:
        return None
    rows = (len(items) + cols - 1) // cols
    h = int(cell * 9 / 16) + 44
    sheet = Image.new("RGB", (cols * cell, rows * h), "white")
    draw = ImageDraw.Draw(sheet)
    font = None
    for name in ("msyh.ttc", "simhei.ttf", "arial.ttf"):
        try:
            font = ImageFont.truetype(name, 14)
            break
        except OSError:
            continue
    for n, item in enumerate(items):
        x, y = (n % cols) * cell, (n // cols) * h
        try:
            with Image.open(item.frame) as im:
                im = im.convert("RGB")
                im.thumbnail((cell - 4, int(cell * 9 / 16)))
                sheet.paste(im, (x + 2, y + 2))
        except OSError:
            continue
        color = (0, 128, 0) if item.usable else ((160, 160, 160) if not item.tagged else (200, 0, 0))
        mark = "✓" if item.usable else ("?" if not item.tagged else "✗")
        line1 = f"{mark} {item.id[-12:]} {item.view or '-'} {item.target}"
        line2 = (item.problem + " " if item.problem else "") + item.desc
        draw.text((x + 4, y + int(cell * 9 / 16) + 4), line1, fill=color, font=font)
        draw.text((x + 4, y + int(cell * 9 / 16) + 22), line2[:24], fill=(60, 60, 60), font=font)
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(dest)
    return dest
