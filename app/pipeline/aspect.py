"""画幅:这个项目成片是横版还是竖版。

**画幅是项目属性,不是品类属性。** 品类 YAML 里的 `aspect_ratio` 只是
「新建项目时的默认值」;真正的画幅写在 `projects.aspect` 列上,创建时定、
之后不可改(改画幅等于整片重渲)。

这个模块是画幅语义的**单一来源**:像素尺寸、字幕单行字数、产物文件名
都从这里出,别处不再各写一份。

一句话记住两条规则:

1. **两种画幅的短边都是 1080** —— 所以叠加层的字号按短边推导,横竖两版
   字号完全一致,变的只是排布(横屏左三分之一的面板 → 竖屏底部通栏)。
2. **产物按画幅命名** —— 16:9 → `output.mp4` / `cover.jpg`,
   9:16 → `output_vertical.mp4` / `cover_vertical.jpg`。一个项目只有一种
   画幅、只出一份成片;文件名带画幅只是为了一眼看出是哪种(发布层、
   下载、清理都经 `output_path` / `cover_path` 取,不再各写一份文件名)。
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from .. import config

#: 字幕单行字数。横屏 22 字、竖屏 15 字 —— 与各自的可用宽度配套
#: (1080 宽的竖屏放不下 22 个 60px 的字,一行 15 个 = 900px 刚好)。
#:
#: 这两个数是**唯一定义处**:`explainer.SUBTITLE_LINE_CHARS` 引用这里,别处不再写死。
LINE_CHARS: dict[str, int] = {
    config.LANDSCAPE: 22,
    config.PORTRAIT: 15,
}

#: 各画幅的产物文件名。发布层按平台要的画幅取对应文件。
OUTPUT_NAMES: dict[str, str] = {
    config.LANDSCAPE: "output.mp4",
    config.PORTRAIT: "output_vertical.mp4",
}
COVER_NAMES: dict[str, str] = {
    config.LANDSCAPE: "cover.jpg",
    config.PORTRAIT: "cover_vertical.jpg",
}

# --------------------------------------------------------------------------
# 按画幅取值(不依赖项目行)
# --------------------------------------------------------------------------
def normalise(aspect: str | None) -> str:
    """把任意输入归一成合法画幅。空 / 未知 → 横屏。"""
    text = (aspect or "").strip()
    return text if text in config.ASPECTS else config.LANDSCAPE


def size_for(aspect: str | None) -> tuple[int, int]:
    """画幅名 -> 像素尺寸。"""
    return config.size_for_aspect(normalise(aspect))


def line_chars_for(aspect: str | None) -> int:
    """画幅名 -> 字幕单行字数。"""
    return LINE_CHARS[normalise(aspect)]


def output_name_for(aspect: str | None) -> str:
    """画幅名 -> 成片文件名。"""
    return OUTPUT_NAMES[normalise(aspect)]


def cover_name_for(aspect: str | None) -> str:
    """画幅名 -> 封面文件名。"""
    return COVER_NAMES[normalise(aspect)]


def is_portrait_for(aspect: str | None) -> bool:
    """这个画幅是不是竖屏。"""
    return normalise(aspect) == config.PORTRAIT


# --------------------------------------------------------------------------
# 按项目取值
# --------------------------------------------------------------------------
def of(project: dict[str, Any] | None) -> str:
    """项目行 -> 画幅。

    项目行没有这一列(旧库迁移前)、或值不认识时,退回**品类的默认画幅**;
    品类也没声明就退回横屏 —— 与迁移后的默认值一致,旧项目行为零变化。
    """
    if not project:
        return config.LANDSCAPE
    raw = str(project.get("aspect") or "").strip()
    if raw in config.ASPECTS:
        return raw
    try:
        from ..categories import load_profile

        profile = load_profile(str(project.get("category") or ""))
    except Exception:  # noqa: BLE001 - 品类配置坏了不该拦住渲染
        return config.LANDSCAPE
    declared = str(profile.aspect_ratio or "").strip()
    return declared if declared in config.ASPECTS else config.LANDSCAPE


def frame_size(project: dict[str, Any] | None) -> tuple[int, int]:
    """项目行 -> 成片像素尺寸。"""
    return size_for(of(project))


def line_chars(project: dict[str, Any] | None) -> int:
    """项目行 -> 字幕单行字数。"""
    return line_chars_for(of(project))


def output_name(project: dict[str, Any] | None) -> str:
    """项目行 -> 成片文件名。"""
    return output_name_for(of(project))


def cover_name(project: dict[str, Any] | None) -> str:
    """项目行 -> 封面文件名。"""
    return cover_name_for(of(project))


def is_portrait(project: dict[str, Any] | None) -> bool:
    """这个项目是不是竖屏。"""
    return is_portrait_for(of(project))


def label(aspect: str | None) -> str:
    """画幅的中文标签(给事件与界面用)。"""
    return "竖屏 9:16" if is_portrait_for(aspect) else "横屏 16:9"


# --------------------------------------------------------------------------
# 产物路径(按项目 id)
# --------------------------------------------------------------------------
def _row(project_id: str) -> dict[str, Any] | None:
    from .. import db

    return db.get_project(project_id)


def output_path(project_id: str) -> Path:
    """这个项目的成片路径。"""
    return config.project_dir(project_id) / output_name(_row(project_id))


def cover_path(project_id: str) -> Path:
    """这个项目的封面路径。"""
    return config.project_dir(project_id) / cover_name(_row(project_id))


#: 所有画幅的产物文件名 —— 清理任务要全部保留
ARTIFACT_NAMES: tuple[str, ...] = (*OUTPUT_NAMES.values(), *COVER_NAMES.values())
