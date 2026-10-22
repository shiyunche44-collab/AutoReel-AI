"""缺省画幅必须是 16:9 横屏。

`config.OUTPUT_WIDTH/HEIGHT` 曾经是 **1080×1920(竖屏 9:16)** —— 那是已删除
的竖屏链路留下的残留。它是 `cards.geo_for` / `render._size` / `timeline` /
`narration.make_cover` / `providers.clips` 的**兜底**画幅:解说轨正常路径
都显式传 `profile.frame_size`(16:9),所以大多数时候是对的;但任何一处
漏传 size,就会**静默产出竖屏**画面。

竖屏的画幅常量在横屏项目里是活的雷 —— 这条测试把它钉死。
"""
from __future__ import annotations

from app import config
from app.categories import load_profile
from app.pipeline import cards, render


def test_default_frame_is_landscape_16_9():
    assert config.OUTPUT_WIDTH == 1920
    assert config.OUTPUT_HEIGHT == 1080
    assert config.frame_is_landscape((config.OUTPUT_WIDTH, config.OUTPUT_HEIGHT))
    assert not config.frame_is_landscape((1080, 1920))


def test_card_geometry_defaults_to_landscape():
    """数据卡的默认几何不能是竖屏。"""
    geo = cards.geo_for()
    assert geo.w > geo.h, f"数据卡默认几何应是横屏,实际 {geo.w}x{geo.h}"
    assert (geo.w, geo.h) == (config.OUTPUT_WIDTH, config.OUTPUT_HEIGHT)


def test_render_size_defaults_to_landscape():
    assert render._size(None) == (config.OUTPUT_WIDTH, config.OUTPUT_HEIGHT)
    assert render._size(None)[0] > render._size(None)[1]


def test_profile_frame_size_is_landscape():
    """品类声明的画幅也应该是 16:9。"""
    profile = load_profile("auto_long")
    w, h = profile.frame_size
    assert w > h, f"auto_long 的画幅应是横屏,实际 {w}x{h}"
    assert config.frame_is_landscape((w, h))


def test_frame_is_landscape_tolerance():
    """一点点误差算横屏(不同品类的画幅可能差几个像素)。"""
    assert config.frame_is_landscape((1920, 1080))
    assert config.frame_is_landscape((1918, 1080))
    assert not config.frame_is_landscape((1080, 1920))
    assert not config.frame_is_landscape((1000, 1000))
