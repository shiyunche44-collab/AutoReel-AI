"""按业务域拆分的路由。main.py 只负责装配与调度。"""
from __future__ import annotations

from typing import Any

from fastapi import HTTPException

#: Claude 项目在 Web 上点推进 / 门禁时的提示
CLAUDE_ONLY = "这个项目由 Claude Code 推进,请在 Claude Code 里继续(/make-video {pid})"


def web_writable(project_id: str) -> dict[str, Any]:
    """推进 / 门禁 / 重渲 / 换刀这类写接口的入口检查:项目不存在 404;
    Claude 项目 409(Web 只看不改,改动走 Claude Code 会话,自审和硬校验才不会被绕过)。"""
    from .. import db
    from ..pipeline import orchestrator

    row = db.get_project(project_id)
    if not row:
        raise HTTPException(404, "项目不存在")
    if orchestrator.is_claude_driven(row):
        raise HTTPException(409, CLAUDE_ONLY.format(pid=project_id))
    return row
