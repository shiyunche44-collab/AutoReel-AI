"""HUD 版式面板(对标 motion/bakeoff/C.html)的回归测试。

这些测试盯的是三件容易悄悄坏掉的事:
1. 纸面还是浅色工程图纸,不是又变回深色玻璃卡片;
2. 纸上的淡线是"混好的实色",不是低 alpha 的洞(叠到画面上会变成黑线);
3. 数字走等宽字体,单位小一号 —— HUD C 的辨识度全在这两点上。
"""
from __future__ import annotations

from PIL import Image, ImageDraw

from app.pipeline import hud, overlays

SIZE = (1920, 1080)


def _overlay(tag: str = "") -> dict:
    return {
        "kind": "panel",
        "title": "车身尺寸",
        "rows": [
            {"label": "车长", "values": ["4999mm"], "tag": tag},
            {"label": "车宽", "values": ["1996mm"], "tag": tag},
        ],
        "source": "数据来源:官方 2 | 口径:标准版 | 2026-10",
    }


def _panel(tmp_path, tag: str = "", name: str = "p.png"):
    dest = overlays.panel(_overlay(tag), "小米YU7", SIZE, tmp_path / name)
    return Image.open(dest).convert("RGB")


def _plate_box() -> tuple[int, int, int, int]:
    w, h = SIZE
    return int(w * 0.035), int(h * 0.14), int(w * 0.335), int(h * 0.78)


def _count(img: Image.Image, color, tol: int = 14, box=None) -> int:
    region = img.crop(box) if box else img
    px = region.load()
    n = 0
    for y in range(region.height):
        for x in range(region.width):
            r, g, b = px[x, y]
            if abs(r - color[0]) < tol and abs(g - color[1]) < tol and abs(b - color[2]) < tol:
                n += 1
    return n


# --------------------------------------------------------------------------
# 纸面
# --------------------------------------------------------------------------
def test_panel_is_a_light_paper_sheet(tmp_path):
    """面板是浅色工程图纸 —— 深色玻璃卡片是「AI 参数解说号」的通用语言。"""
    img = _panel(tmp_path)
    x0, y0, x1, y1 = _plate_box()
    assert _count(img, hud.PAPER, 8, (x0 + 40, y0 + 40, x1 - 40, y1 - 40)) > 40000


def test_panel_keeps_the_rest_of_the_frame_transparent(tmp_path):
    dest = overlays.panel(_overlay(), "小米YU7", SIZE, tmp_path / "p.png")
    im = Image.open(dest).convert("RGBA")
    assert im.getpixel((SIZE[0] - 40, 40))[3] == 0
    assert im.getpixel((20, SIZE[1] // 2))[3] == 0


def test_panel_has_the_orange_tracking_brackets(tmp_path):
    """bakeoff C 的取景框角标:四个橙角,这是这套版式的辨识点。"""
    img = _panel(tmp_path)
    x0, y0, x1, y1 = _plate_box()
    for pt in ((x0 + 2, y0 + 2), (x1 - 3, y0 + 2), (x0 + 2, y1 - 3), (x1 - 3, y1 - 3)):
        r, g, b = img.load()[pt]
        assert r > 200 and 50 < g < 130 and b < 90, (pt, (r, g, b))


def test_paper_lines_are_preblended_opaque(tmp_path):
    """纸上的淡线必须是"混好的实色"。

    PIL 的 draw 直接写像素、不做 alpha 混合:直接画一个 alpha=18 的墨色,
    等于在纸上戳了个近乎透明的洞,叠到画面上就是黑线。
    """
    assert len(hud.GRID_COLOR) == 3 and len(hud.RULE_COLOR) == 3
    for color in (hud.GRID_COLOR, hud.RULE_COLOR):
        assert all(hud.PAPER[i] >= color[i] >= hud.INK[i] for i in range(3))
    assert _count(_panel(tmp_path), hud.GRID_COLOR, 4) > 100


# --------------------------------------------------------------------------
# 数字与口径
# --------------------------------------------------------------------------
def test_numbers_are_tabular_and_units_are_smaller(tmp_path):
    """数字走等宽字体:同样位数的数,画出来一样宽。"""
    d = ImageDraw.Draw(Image.new("RGB", (10, 10)))
    w1 = hud.value_width(d, "1111", 60, 30)
    w8 = hud.value_width(d, "8888", 60, 30)
    assert w1 == w8
    with_unit = hud.value_width(d, "4999mm", 60, 30)
    assert with_unit > w1


def test_mixed_font_falls_back_to_cjk_for_chinese(tmp_path):
    """Consolas 没有汉字,中文必须走中文字体,否则是豆腐块。"""
    d = ImageDraw.Draw(Image.new("RGB", (10, 10)))
    assert hud.mixed_width(d, "毫米", 60, 30) > 0
    assert hud.mono_font(40) is not None


def test_panel_draws_the_basis_tag_beside_the_parameter(tmp_path):
    """口径小标签要画在参数名右边 —— 数字靠不靠谱就靠这一行。"""
    plain = _panel(tmp_path, "", "plain.png")
    tagged = _panel(tmp_path, "官方 | 标准版", "tagged.png")
    assert _count(tagged, hud.MUTED, 10) > _count(plain, hud.MUTED, 10)


def test_panel_shows_the_source_line(tmp_path):
    """来源行(口径 + 来源类型 + 月份)必须留在纸面上。"""
    img = _panel(tmp_path)
    assert _count(img, hud.MUTED, 12) > 200


# --------------------------------------------------------------------------
# 强调色
# --------------------------------------------------------------------------
def test_overlay_accent_is_the_hud_orange(tmp_path):
    """整条叠加层的强调色统一到 HUD 的橙,别再是黄色的通用色。"""
    assert overlays._ACCENT[:3] == hud.ACCENT
    bar = Image.open(overlays.chapter_bar([("开头", 3), ("外观", 30)], 1, SIZE,
                                          tmp_path / "b.png")).convert("RGB")
    w, h = SIZE
    # 当前章的高亮块:在条的上半部分找橙色
    assert _count(bar, hud.ACCENT, 24, (0, h - int(h * 0.068), w, h)) > 500


def test_hud_tokens_match_bakeoff_c():
    """配色必须和 motion/bakeoff/C.html 一致,别各改各的。"""
    assert hud.PAPER == (236, 238, 233)
    assert hud.INK == (18, 22, 26)
    assert hud.ACCENT == (255, 90, 31)
    assert hud.MUTED == (93, 101, 110)


# --------------------------------------------------------------------------
# 边界
# --------------------------------------------------------------------------
def test_panel_survives_edge_cases(tmp_path):
    """没有行、没有单位、标签特别长、标题为空 —— 都不能画崩。"""
    cases = [
        {"kind": "panel", "title": "", "rows": [], "source": ""},
        {"kind": "panel", "title": "核心参数",
         "rows": [{"label": "布局", "values": ["五座"]}], "source": ""},
        {"kind": "panel", "title": "很长的标题" * 6,
         "rows": [{"label": "参数名" * 8, "values": ["1,200 km"], "tag": "口径" * 8}],
         "source": "数据来源" * 10},
    ]
    for n, ov in enumerate(cases):
        dest = overlays.panel(ov, "小米YU7", SIZE, tmp_path / f"c{n}.png")
        im = Image.open(dest)
        assert im.size == SIZE and im.mode == "RGBA"
