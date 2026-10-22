"""竖版(9:16)管线测试。

竖版不是另一套渲染逻辑,是同一条跑第二遍。这里锁三件事:

1. **画幅常量**对 —— 1080×1920;
2. **字幕字号按短边算** —— 竖版不能因为"高"就把字放大到一行放不下;
3. **背景不硬裁** —— 竖版走「前景完整 + 模糊铺底」,不切车头车尾。
"""
from __future__ import annotations

import pytest

from app.models import SubtitleCue
from app.pipeline import explainer, timeline, vertical


def _cue(text: str = "小米YU7 是今年最热门的车型") -> SubtitleCue:
    return SubtitleCue(index=0, start=0.0, end=3.0, text=text, shot_idx=0)


# --------------------------------------------------------------------------
# 画幅
# --------------------------------------------------------------------------
def test_vertical_size():
    assert vertical.VERTICAL_SIZE == (1080, 1920)


def test_vertical_output_names():
    assert vertical.OUTPUT_NAME == "output_vertical.mp4"
    assert vertical.COVER_NAME == "cover_vertical.jpg"


def test_vertical_line_chars_fit_the_canvas():
    """15 个 60px 的字 = 900px,画幅内可用宽 = 1080 - 2×64 = 952px,放得下。"""
    font = int(min(vertical.VERTICAL_SIZE) * 0.056)
    margin = int(vertical.VERTICAL_SIZE[0] * 0.06)
    available = vertical.VERTICAL_SIZE[0] - margin * 2
    assert font * vertical.VERTICAL_LINE_CHARS <= available


# --------------------------------------------------------------------------
# 字幕字号
# --------------------------------------------------------------------------
def test_ass_font_size_is_same_for_both_aspects():
    """按短边算字号:横屏 1920×1080 与竖屏 1080×1920 都是 60px。"""
    wide = timeline.format_ass([_cue()], width=1920, height=1080, line_chars=22)
    tall = timeline.format_ass([_cue()], width=1080, height=1920,
                                line_chars=vertical.VERTICAL_LINE_CHARS)
    assert "PlayResX: 1920" in wide and "PlayResY: 1080" in wide
    assert "PlayResX: 1080" in tall and "PlayResY: 1920" in tall
    assert ",60," in wide
    assert ",60," in tall


def test_ass_uses_given_line_chars():
    out = timeline.format_ass([_cue()], width=1080, height=1920,
                              line_chars=vertical.VERTICAL_LINE_CHARS)
    style = [line for line in out.splitlines() if line.startswith("Style:")][0]
    # 样式行末尾是 MarginL, MarginR, MarginV
    assert style.rstrip().endswith(f",64,64,{int(1920 * 0.115)},1")


# --------------------------------------------------------------------------
# 背景:竖版不硬裁
# --------------------------------------------------------------------------
def test_fit_filter_keeps_whole_frame():
    """前景完整缩进画幅 + 模糊铺底 —— 16:9 源进 9:16 不会切掉车头车尾。

    背景那半边当然要裁(它是放大模糊的底),要保证完整的是前景那半边。
    """
    out = explainer._fit_filter(1080, 1920, 0.0)
    assert "force_original_aspect_ratio=decrease" in out
    assert "boxblur" in out
    foreground = out.split("[bg]")[1]
    assert "force_original_aspect_ratio=decrease" in foreground
    assert "crop=" not in foreground.split("[fg]")[0].split("scale=")[1]


def test_fit_filter_with_crop_bottom_fills_the_frame():
    """横屏那一支:裁掉底部烧录字幕后放大铺满,不留模糊边。"""
    out = explainer._fit_filter(1920, 1080, 0.22)
    assert "crop=iw" in out
    assert "force_original_aspect_ratio=increase" in out


def test_fit_background_flag_is_scoped():
    """上下文退出后标志复原,别把横版的渲染也改了。"""
    assert explainer._FIT_BACKGROUND is False
    with explainer.fit_background():
        assert explainer._FIT_BACKGROUND is True
    assert explainer._FIT_BACKGROUND is False


def test_fit_background_restores_on_error():
    with pytest.raises(RuntimeError):
        with explainer.fit_background():
            raise RuntimeError("boom")
    assert explainer._FIT_BACKGROUND is False


# --------------------------------------------------------------------------
# 产物
# --------------------------------------------------------------------------
def test_clean_removes_vertical_artifacts(tmp_path, monkeypatch):
    from app import config, db

    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(config, "PROJECTS_DIR", tmp_path / "data" / "projects")
    db._INITIALISED = False
    db.init_db()
    try:
        out = vertical.output_path("p_1")
        cover = vertical.cover_path("p_1")
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(b"x")
        cover.write_bytes(b"x")
        assert vertical.exists("p_1")
        vertical.clean("p_1")
        assert not vertical.exists("p_1")
        assert not cover.exists()
    finally:
        db._INITIALISED = False
