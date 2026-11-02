"""片头品牌条:成片前几秒叠在画面左上角的一枚小标(账号署名),淡入淡出。

不另加一段片头、不占时长,所以黄金三秒的钩子画面和旁白一点不受影响;
署名来自 `config.COVER_BRAND`,不写死品牌。`render.compose(intro_path=...)` 负责叠。
"""
from __future__ import annotations

from pathlib import Path

from .. import config
from .narration import _font_file

_PILL = (16, 24, 38, 190)
_FG = (255, 255, 255, 255)


def render_badge(dest: Path, size: tuple[int, int], brand: str) -> Path | None:
    """整幅大小的透明 PNG,只在左上安全区画一枚圆角标。没有署名或 Pillow 不可用返回 None。"""
    brand = (brand or "").strip()
    if not brand:
        return None
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError:
        return None
    w, h = size
    px = max(22, min(w, h) // 28)
    path = _font_file(bold=True)
    try:
        font = ImageFont.truetype(path, px) if path else ImageFont.load_default()
    except OSError:
        font = ImageFont.load_default()
    img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    pad = px // 2
    tw = int(draw.textlength(brand, font=font))
    # 竖屏顶部要让开平台的状态栏 / 搜索栏,横屏贴近左上
    x0 = int(w * 0.05)
    y0 = int(h * (0.10 if h > w else 0.06))
    draw.rounded_rectangle([x0, y0, x0 + tw + 2 * pad, y0 + px + 2 * pad],
                           radius=(px + 2 * pad) // 2, fill=_PILL)
    draw.text((x0 + pad, y0 + pad), brand, font=font, fill=_FG)
    dest.parent.mkdir(parents=True, exist_ok=True)
    img.save(dest)
    return dest


def build(project_dir: Path, size: tuple[int, int]) -> tuple[Path, float] | None:
    """(PNG 路径, 显示秒数);没开或没署名返回 None。"""
    if not config.INTRO_ENABLED:
        return None
    png = render_badge(project_dir / "_intro" / "badge.png", size, config.COVER_BRAND)
    return (png, config.INTRO_SECONDS) if png else None
