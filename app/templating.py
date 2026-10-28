"""Jinja2 模板环境与过滤器(路由层共用)。"""
from __future__ import annotations

import time
from pathlib import Path

from fastapi.templating import Jinja2Templates

BASE_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(BASE_DIR / "web" / "templates"))


def _fmt_hhmmss(value: float) -> str:
    """时间戳格式化为 HH:MM:SS。

    必须在 Python 侧做:Jinja 的 `%` 是 Python 字符串格式化,
    在模板里写 '%H:%M:%S' 会被当成格式串直接抛 ValueError。
    """
    ts = time.localtime(float(value))
    return time.strftime("%H:%M:%S", ts)


def _fmt_datetime(value: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(float(value)))


def _basename(value: str) -> str:
    """取文件名。素材 URL 走 /media/{project}/{kind}/{file},需要剥掉目录。"""
    return str(value).replace("\\", "/").rsplit("/", 1)[-1]


def _media_kind(value: str) -> str:
    """素材所在的子目录。成片阶段的单拍可能直接复用草稿文件(final_path 指向 drafts/),
    URL 必须跟着真实目录走,不能一律当成 shots/。"""
    parts = str(value).replace("\\", "/").rsplit("/", 2)
    kind = parts[-2] if len(parts) >= 2 else ""
    return kind if kind in ("drafts", "shots") else "shots"


templates.env.filters["hhmmss"] = _fmt_hhmmss
templates.env.filters["datetime"] = _fmt_datetime
templates.env.filters["basename"] = _basename
templates.env.filters["media_kind"] = _media_kind
