"""模型调用账本:每次文字 / 看图 / 生图调用记一行(`llm_calls` 表)。

**为什么**:额度烧在哪、哪个提示词慢、哪类调用老失败,原来只能靠猜(`cli.call_stats`
只在内存里,重启就清零,也分不出是谁调的)。有了账本才能回答"上周写稿花了多少
token""改了提示词之后失败率变没变"。

- 调用方(`purpose`)从调用栈自动取:第一个不在模型通道模块里的帧,`模块.函数`。
- 项目 / 团队角色用 `tag()` 挂在上下文上:`orchestrator.advance` 挂项目,
  `team.run_role` 挂角色,块里所有调用都带上。
- 系统提示词只记指纹(`system_hash`),不落原文:原文很长,而且提示词本身在代码里。
- **记账失败绝不影响调用本身**。
"""
from __future__ import annotations

import contextvars
import hashlib
import logging
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from types import FrameType
from typing import Any

log = logging.getLogger(__name__)

_tags: contextvars.ContextVar[dict[str, str] | None] = contextvars.ContextVar(
    "llm_ledger_tags", default=None)

#: 这些模块是"通道"本身,取调用方时跳过
_CHANNEL_MODULES = ("app.llm.", "app.providers.direct", "app.providers.ark_image",
                    "app.providers.cli", "contextlib")


@contextmanager
def tag(**values: str) -> Iterator[None]:
    """给块里的模型调用挂上项目 / 角色等标签(嵌套时合并,内层优先)。"""
    token = _tags.set({**(_tags.get() or {}), **{k: str(v) for k, v in values.items() if v}})
    try:
        yield
    finally:
        _tags.reset(token)


def current_tags() -> dict[str, str]:
    return dict(_tags.get() or {})


def caller(depth_limit: int = 30) -> str:
    """调用方:调用栈上第一个不属于模型通道的帧,`模块.函数`(去掉 `app.` 前缀)。"""
    frame: FrameType | None = sys._getframe(1)
    for _ in range(depth_limit):
        if frame is None:
            break
        mod = frame.f_globals.get("__name__", "")
        if mod != __name__ and not mod.startswith(_CHANNEL_MODULES):
            name = f"{mod}.{frame.f_code.co_name}"
            return name[4:] if name.startswith("app.") else name
        frame = frame.f_back
    return ""


def fingerprint(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:12] if text else ""


def record(*, kind: str, provider: str, model: str, started: float, ok: bool,
           usage: dict[str, Any] | None = None, system: str = "", prompt_chars: int = 0,
           finish_reason: str = "", error: str = "", purpose: str | None = None) -> None:
    """记一次调用。`started` 是 `time.monotonic()` 取的开始时刻。"""
    try:
        from .. import db

        usage = usage or {}
        details = usage.get("completion_tokens_details") or {}
        tags = _tags.get() or {}
        db.add_llm_call({
            "at": time.time(), "kind": kind, "provider": provider, "model": model or "",
            "purpose": caller() if purpose is None else purpose,
            "project_id": tags.get("project_id", ""), "role": tags.get("role", ""),
            "system_hash": fingerprint(system), "prompt_chars": int(prompt_chars),
            "prompt_tokens": usage.get("prompt_tokens"),
            "completion_tokens": usage.get("completion_tokens"),
            "reasoning_tokens": details.get("reasoning_tokens"),
            "latency_ms": int((time.monotonic() - started) * 1000), "ok": 1 if ok else 0,
            "finish_reason": finish_reason or "", "error": (error or "")[:500],
        })
    except Exception:  # noqa: BLE001 - 记账失败绝不影响调用本身
        log.debug("模型调用记账失败", exc_info=True)
