"""素材池已拆成 app/pipeline/library/ 包:同一个常量 / 函数可能被多个子模块各自 import 一份。

测试想改它,要在包和所有持有该名字的子模块上一起改,否则只有一部分调用点生效。
"""
from __future__ import annotations

import inspect

from app.pipeline import library as lib_pkg


def patch_library(monkeypatch, name: str, value) -> None:
    prefix = lib_pkg.__name__ + "."
    mods = [lib_pkg] + [m for m in vars(lib_pkg).values()
                        if inspect.ismodule(m) and m.__name__.startswith(prefix) and name in vars(m)]
    for mod in mods:
        monkeypatch.setattr(mod, name, value)
