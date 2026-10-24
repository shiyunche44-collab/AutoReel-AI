"""封面:底图来源、标题排版、角标与副标题。

背景:封面是唯一决定点击率的一帧。之前有两个硬伤:
1. 底图直接从成片抽帧 —— 抽出来的那一帧上带着章节条、字幕、大字,
   再压上标题就是三层字叠在一起;
2. 标题 `title[:14]` 硬截断 —— "小米YU7 值不值 值得买吗" 被砍成 "小米YU7 值不值 值得",
   砍在词中间,观众看到的是半句话。

现在:底图优先用素材池里的干净官图(白底棚拍图没有第三方水印),
标题按宽度自适应缩排,浅底自动换深色字。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.pipeline import narration


def _img(path: Path, size=(800, 600), color=(240, 240, 240)) -> Path:
    from PIL import Image

    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", size, color).save(path, "JPEG")
    return path


def _font():
    """封面排版要真字体,没有就退回 PIL 自带字体。"""
    from PIL import ImageFont

    from app import config

    for name in ("msyhbd.ttc", "msyh.ttc", "simhei.ttf"):
        p = Path(config.FONTS_DIR) / name
        if p.is_file():
            return ImageFont.truetype(str(p), 60)
    return ImageFont.load_default()


# --------------------------------------------------------------------------
# 标题排版
# --------------------------------------------------------------------------
def test_cover_title_is_not_hard_truncated():
    """标题不能再被砍成半句。"""
    title = "小米YU7 值不值 值得买吗"
    lines, _ = narration._cover_fit(title, None, 1920 - narration.COVER_MARGIN * 2)
    assert "".join(lines).replace("…", "") != ""
    # 完整标题要能排下来(允许缩排,但不能丢内容)
    assert title[:6] in "".join(lines)
    assert len(lines) <= narration.COVER_MAX_LINES


def test_cover_title_shrinks_to_fit_two_lines():
    """超长标题:缩字号也要控制在两行内;放不下时用省略号收尾,而不是硬切。"""
    title = "2026 款全新升级版深度解析:续航、补能与智能辅助驾驶到底怎么样啊朋友"
    lines, font = narration._cover_fit(title, None, 1920 - narration.COVER_MARGIN * 2)
    assert 1 <= len(lines) <= narration.COVER_MAX_LINES
    assert font is not None
    joined = "".join(lines)
    # 要么完整排下,要么用省略号收尾 —— 不能是砍在半句上的硬截断
    assert joined == title or joined.endswith("…")
    if joined.endswith("…"):
        assert title.startswith(joined[:-1])


def test_cover_wrap_keeps_short_title_on_one_line():
    font = _font()
    lines = narration._cover_wrap("深度解析", font, 1600, 2)
    assert lines == ["深度解析"]


def test_cover_tokens_keep_latin_words_whole():
    """YU7 / CLTC 这类不能被拆成单个字母;中文逐字排。"""
    assert narration._cover_tokens("YU7 CLTC") == ["YU7", " ", "CLTC"]
    assert narration._cover_tokens("小米") == ["小", "米"]


# --------------------------------------------------------------------------
# 明暗自适应:白底图上白字等于隐形
# --------------------------------------------------------------------------
def test_cover_detects_light_background(tmp_path):
    from PIL import Image

    white = _img(tmp_path / "white.jpg", color=(250, 250, 250))
    with Image.open(white) as im:
        assert narration._cover_is_light(im.convert("RGB"))
    dark = _img(tmp_path / "dark.jpg", color=(30, 34, 40))
    with Image.open(dark) as im:
        assert not narration._cover_is_light(im.convert("RGB"))


def test_cover_uses_dark_text_on_light_still(tmp_path):
    """浅底官图当封面时,标题要压得住 —— 浅底用深字。"""
    from PIL import Image

    still = _img(tmp_path / "white.jpg", size=(1200, 900), color=(248, 248, 248))
    dest = tmp_path / "cover.jpg"
    got = narration.make_cover("no-such-video.mp4", "封面标题测试", dest,
                                subtitle="副标题", size=(1920, 1080), still=still,
                                kicker="小米YU7 · 23.35万元起")
    assert got and got.is_file()
    with Image.open(got) as im:
        assert im.size == (1920, 1080)


# --------------------------------------------------------------------------
# 抽帧兜底
# --------------------------------------------------------------------------
def test_cover_falls_back_to_frame_when_still_missing(tmp_path):
    """没有官图时退回抽帧;抽帧失败返回 None(不抛异常)。"""
    dest = tmp_path / "cover.jpg"
    assert narration.make_cover("no-such-video.mp4", "标题", dest) is None
    assert not dest.exists()


def test_cover_build_cover_passes_through_still_and_kicker(tmp_path, monkeypatch):
    """render.build_cover 要把 still / kicker 透传给排版层。"""
    from app.pipeline import render as render_mod

    seen = {}

    def _fake(video, title, dest, subtitle="", size=None, still=None, kicker="", layout="title", hook=""):
        if not seen:  # 只看主封面那一次;后面是补 4:3 / 3:4 的调用
            seen.update({"still": still, "kicker": kicker, "subtitle": subtitle, "size": size,
                         "layout": layout, "hook": hook})
        return Path(dest)

    monkeypatch.setattr(narration, "make_cover", _fake)
    got = render_mod.build_cover("v.mp4", "标题", tmp_path / "c.jpg", subtitle="副",
                                 size=(1920, 1080), still="still.jpg", kicker="角标")
    assert got is not None
    assert seen["still"] == "still.jpg" and seen["kicker"] == "角标"
    assert seen["subtitle"] == "副" and seen["size"] == (1920, 1080)
    assert seen["layout"] == "title" and seen["hook"] == ""


# --------------------------------------------------------------------------
# 封面文案:角标(主体 + 入门价)与副标题(主论点)
# --------------------------------------------------------------------------
def test_cover_meta_uses_thesis_and_fact_price(monkeypatch, tmp_path):
    """副标题用主论点;入门价从事实层取,取最低的那条。"""
    from app.categories import load_profile
    from app.pipeline import explainer

    project = {"subject": "小米YU7", "thesis": "家用完全够,长途要掂量补能"}
    facts = [
        {"id": "f_1", "claim": "小米YU7标准版售价23.35万元。", "scope": "product",
         "confidence": "high", "enabled": True, "basis": "标准版 · 2026款"},
        {"id": "f_2", "claim": "小米YU7 GT版售价38.99万元起。", "scope": "product",
         "confidence": "high", "enabled": True, "basis": "GT版 · 2026款"},
        {"id": "f_3", "claim": "某传闻售价1万元。", "scope": "product",
         "confidence": "low", "enabled": True, "basis": ""},
    ]
    monkeypatch.setattr(explainer.db, "get_project", lambda pid: project)
    monkeypatch.setattr(explainer.db, "read_json", lambda pid, name, default=None: facts)

    meta = explainer.cover_meta("p_x", load_profile("auto_long"), 372.0)
    assert meta["subtitle"] == "家用完全够,长途要掂量补能"
    assert meta["kicker"] == "小米YU7 · 23.35万元起"
    assert meta["price_fact"] == "f_1"          # 取的是最低价那条
    assert "1万元" not in meta["kicker"]        # low 置信度不上屏


def test_cover_meta_falls_back_to_duration_without_thesis(monkeypatch):
    from app.categories import load_profile
    from app.pipeline import explainer

    monkeypatch.setattr(explainer.db, "get_project", lambda pid: {"subject": "某产品"})
    monkeypatch.setattr(explainer.db, "read_json", lambda pid, name, default=None: [])
    meta = explainer.cover_meta("p_x", load_profile("auto_long"), 372.0)
    assert "6分12秒" in meta["subtitle"]
    assert meta["kicker"] == "某产品"


def test_price_of_ignores_disabled_and_company_scope():
    from app.pipeline import explainer

    facts = [
        {"id": "a", "claim": "售价9.99万元。", "scope": "company", "confidence": "high",
         "enabled": True},
        {"id": "b", "claim": "售价18.88万元。", "scope": "product", "confidence": "high",
         "enabled": "False"},
    ]
    assert explainer._price_of(facts) == ("", "")


def test_portrait_cover_breaks_title_at_comma_and_relays_3x4(tmp_path):
    """竖屏实跑:「多花15万买GT,值不 / 值?」;3:4 从 9:16 上截,主论点被截掉半行。"""
    from PIL import Image

    from app.pipeline import narration

    lines, _font = narration._cover_title_lines("多花15万买GT，值不值？", narration._font_file(True), 952)
    if narration._font_file(True):
        assert lines == ["多花15万买GT，", "值不值？"]
    still = tmp_path / "still.jpg"
    Image.new("RGB", (1000, 1000), (240, 240, 240)).save(still)  # 方图白底:最容易把下面挤出去
    dest = tmp_path / "cover_vertical.jpg"
    narration.make_cover(tmp_path / "none.mp4", "多花15万买GT，值不值？", dest,
                         subtitle="多数人买标准版就够", size=(1080, 1920), still=still, kicker="小米YU7")
    assert Image.open(dest).size == (1080, 1920)
    assert Image.open(narration.cover_3x4_path(dest)).size == (1080, 1440)


def test_landscape_fallback_cover_also_writes_4x3_and_3x4(tmp_path):
    """横屏老封面(没有白底官图的兜底)也要带 4:3 / 3:4 重排版,抖音网页版要这两张。"""
    from PIL import Image

    from app.pipeline import render

    still = tmp_path / "still.jpg"
    Image.new("RGB", (1920, 1080), (90, 100, 120)).save(still)
    dest = tmp_path / "cover.jpg"
    got = render.build_cover("no-such-video.mp4", "封面标题测试", dest, "副标题",
                             size=(1920, 1080), still=still)
    assert got == dest
    assert Image.open(narration.cover_4x3_path(dest)).size == (1440, 1080)
    assert Image.open(narration.cover_3x4_path(dest)).size == (1080, 1440)
    assert not narration.cover_3x4_path(narration.cover_3x4_path(dest)).exists()
