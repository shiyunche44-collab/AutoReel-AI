"""大模块已拆成包(app/pipeline/library/、cover_art/ ……):同一个常量 / 函数可能被多个子模块
各自 import 了一份。

测试想改它,要在包和所有持有该名字的子模块上一起改,否则只有一部分调用点生效。
"""
from __future__ import annotations

import inspect
from types import ModuleType

from app.pipeline import library as lib_pkg


def patch_in_package(monkeypatch, pkg: ModuleType, name: str, value) -> None:
    prefix = pkg.__name__ + "."
    mods = [pkg] + [m for m in vars(pkg).values()
                    if inspect.ismodule(m) and m.__name__.startswith(prefix) and name in vars(m)]
    for mod in mods:
        monkeypatch.setattr(mod, name, value)


def patch_library(monkeypatch, name: str, value) -> None:
    patch_in_package(monkeypatch, lib_pkg, name, value)
