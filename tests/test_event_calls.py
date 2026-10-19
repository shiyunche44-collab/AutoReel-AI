"""静态检查:所有 add_event 调用点的实参与真实签名是否对得上。

起因:generate.py 里两处调用把 dst 同时按位置和关键字传了
(add_event(pid, "xxx", dst="yyy")),运行到那一行才抛
"got multiple values for argument 'dst'"。表现是项目莫名其妙变成 FAILED,
而此时视频其实已经生成成功 —— 一个记账错误毁掉了整条流水线。

这种错误只有真正跑到那一行才会暴露,而那一行可能几小时才执行一次。
所以在单测里按真实签名做一次静态绑定检查。
"""
from __future__ import annotations

import ast
import inspect
from pathlib import Path

import pytest

from app import db

ROOT = Path(__file__).resolve().parent.parent
TARGETS = sorted((ROOT / "app").rglob("*.py"))

#: 这些文件里的 add_event 是"故意"带 dst 关键字的(形如事件流记录)
ALLOW_KEYWORD_DST: set[tuple[str, int]] = set()


def _add_event_calls(path: Path):
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.attr if isinstance(func, ast.Attribute) else (
            func.id if isinstance(func, ast.Name) else None)
        if name == "add_event":
            yield node


def test_there_are_add_event_calls_to_check():
    """防止下面的检查因为"一个调用都找不到"而空转通过。"""
    count = sum(len(list(_add_event_calls(p))) for p in TARGETS)
    assert count >= 4, f"只找到 {count} 处 add_event 调用,检查范围可能已失效"


def test_every_add_event_call_matches_signature():
    """按 db.add_event 的真实签名绑定每个调用点的实参。"""
    problems: list[str] = []
    for path in TARGETS:
        for node in _add_event_calls(path):
            rel = path.relative_to(ROOT).as_posix()
            if (rel, node.lineno) in ALLOW_KEYWORD_DST:
                continue
            args = [ast.unparse(a) for a in node.args]
            kwargs = {kw.arg: ast.unparse(kw.value) for kw in node.keywords
                      if kw.arg}
            try:
                inspect.signature(db.add_event).bind(
                    *["?"] * len(args), **kwargs)
            except TypeError as exc:
                problems.append(f"{rel}:{node.lineno} add_event({', '.join(args)}) "
                                f"关键字 {sorted(kwargs)} → {exc}")
    assert not problems, "add_event 调用与签名不符:\n" + "\n".join(problems)


def test_add_event_never_passes_dst_as_keyword_with_positional_arg():
    """更直接的表述:dst 是第二个位置参数,不能再按关键字重复传。"""
    offenders: list[str] = []
    for path in TARGETS:
        for node in _add_event_calls(path):
            rel = path.relative_to(ROOT).as_posix()
            if (rel, node.lineno) in ALLOW_KEYWORD_DST:
                continue
            kw_names = {kw.arg for kw in node.keywords if kw.arg}
            if len(node.args) >= 2 and "dst" in kw_names:
                offenders.append(f"{rel}:{node.lineno}")
    assert not offenders, "dst 同时按位置与关键字传入:" + ", ".join(offenders)


@pytest.mark.parametrize("path", TARGETS, ids=lambda p: p.name)
def test_app_sources_parse(path: Path):
    """顺带保证这些文件本身语法正确(否则 AST 检查会静默跳过)。"""
    ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
