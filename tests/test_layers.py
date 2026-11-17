"""模块分层约束:下层不许引用上层(包括函数里的延迟 import)。

层级从低到高(同层可以互相引用):

    0  config state models logging_setup hang_watch instance_lock templating
    1  db
    2  costs llm jobs degrade
    3  categories
    4  providers
    5  pipeline
    6  publish metrics maintenance
    7  team
    8  scheduler views routes background worker main

为什么要守:原来全靠自觉,函数里的延迟 import 有四百多处,越层引用一多就成了
循环依赖,改一个底层模块牵动一片。`KNOWN` 是写这条测试时就存在的越层引用,
**只许减不许增**:新增越层会挂;修掉一处后,这里也会挂着提醒你把它从清单删掉。
"""
from __future__ import annotations

import ast
from pathlib import Path

APP = Path(__file__).resolve().parent.parent / "app"

RANK = {
    **dict.fromkeys(("config", "state", "models", "logging_setup", "hang_watch",
                     "instance_lock", "templating"), 0),
    "db": 1,
    **dict.fromkeys(("costs", "llm", "jobs", "degrade"), 2),
    "categories": 3,
    "providers": 4,
    "pipeline": 5,
    **dict.fromkeys(("publish", "metrics", "maintenance"), 6),
    "team": 7,
    **dict.fromkeys(("scheduler", "views", "routes", "background", "worker", "main"), 8),
}

#: 已知的越层引用 "文件 -> 目标层"。只许减不许增。
KNOWN = {
    # 品类配置回头找流水线的版式 / 片型常量:应把那几个常量下沉到 categories
    "categories/__init__.py -> pipeline",
    "categories/build.py -> pipeline",
    # 浏览器会话(BrowserSession)放在 publish 里,抓取也要用:应挪到 providers 或单独一层
    "providers/browser_fetch.py -> publish",
    "providers/official.py -> publish",
    # 素材片段处理借用了流水线的工具函数
    "providers/clips.py -> pipeline",
}


def _module(path: Path) -> list[str]:
    parts = list(path.relative_to(APP.parent).with_suffix("").parts)
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return parts


def _layer(dotted: str) -> str | None:
    parts = dotted.split(".")
    return parts[1] if len(parts) > 1 and parts[0] == "app" and parts[1] in RANK else None


def _imports(path: Path):
    mod = _module(path)
    pkg = mod if path.name == "__init__.py" else mod[:-1]
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield alias.name
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                base = pkg[:len(pkg) - (node.level - 1)]
                target = ".".join(base + ([node.module] if node.module else []))
            else:
                target = node.module or ""
            if node.module:
                yield target
            else:  # from .. import pipeline, db
                for alias in node.names:
                    yield f"{target}.{alias.name}"


def _violations() -> set[str]:
    out = set()
    for path in APP.rglob("*.py"):
        src = _layer(".".join(_module(path)))
        if src is None:
            continue
        for target in _imports(path):
            dst = _layer(target)
            if dst and RANK[dst] > RANK[src]:
                out.add(f"{path.relative_to(APP).as_posix()} -> {dst}")
    return out


def test_every_top_level_module_has_a_layer():
    names = {p.stem for p in APP.glob("*.py") if p.stem != "__init__"}
    names |= {p.name for p in APP.iterdir() if (p / "__init__.py").exists()}
    assert names - set(RANK) == set(), "新模块 / 包要在 RANK 里定层"


def test_no_new_upward_imports():
    new = _violations() - KNOWN
    assert not new, "下层引用了上层(把要用的东西下沉,或者改成上层把它传进来):\n" + \
        "\n".join(sorted(new))


def test_known_exceptions_are_still_real():
    gone = KNOWN - _violations()
    assert not gone, "这些越层引用已经修掉了,把它们从 KNOWN 里删掉:\n" + "\n".join(sorted(gone))
