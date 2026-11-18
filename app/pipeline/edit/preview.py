"""门禁 B 的整片预览:按已存的时间线(`edit.project.plan` 取好的材)出一版半尺寸快编码的片子。

进门禁 B 时和「换一刀」之后排一个 `edit.preview` 任务(`jobs`,资源类 `render`,一次一路)。
任务只读时间线、不取材;项目已经不在门禁 B(人放行了 / 团队自动过闸)就不渲。
渲的过程中时间线又变了(又换了一刀):渲完发现旧了就接着按新的再渲,最多 `MAX_ROUNDS` 轮 ——
同一个项目同时只有一个预览任务在排 / 在跑,换刀时排不进来,靠这里补上。
"""
from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any

from ... import config, db
from ...state import State
from .. import looks
from . import project as edit_project
from . import render as edit_render
from .edl import Timeline

log = logging.getLogger(__name__)

PREVIEW_JOB = "edit.preview"
#: 项目目录里的预览文件(审片页按 /media/{项目}/preview/{文件} 取)
PREVIEW_FILE = "preview_v2.mp4"
MAX_ROUNDS = 3


def enabled() -> bool:
    """`EDIT_V2_PREVIEW`(默认开):关掉后门禁 B 只看逐拍草稿。"""
    return config.edit_v2_preview()


def submit(project_id: str) -> bool:
    """排一个预览任务。已经在排 / 在跑返回 False(那个任务渲完会自己发现时间线变了)。"""
    if not enabled():
        return False
    from ... import jobs

    return jobs.submit(PREVIEW_JOB, project_id, {"project_id": project_id,
                                                 "requested_at": round(time.time(), 1)}) is not None


def path(project_id: str) -> Path:
    return config.project_dir(project_id) / PREVIEW_FILE


def status(project_id: str) -> dict[str, Any]:
    """审片页用:预览在不在、是不是按当前时间线渲的、任务进行到哪。"""
    from ... import jobs

    saved = edit_project.load(project_id)
    planned = float((saved.get("plan") or {}).get("planned_at") or 0.0)
    f = path(project_id)
    done = db.read_json(project_id, "preview_v2.json", None) or {}
    job = jobs.latest(PREVIEW_JOB, project_id) or {}
    return {"exists": f.is_file(), "name": PREVIEW_FILE if f.is_file() else "",
            "fresh": f.is_file() and float(done.get("planned_at") or -1) == planned,
            "rendered_at": done.get("rendered_at"), "planned_at": planned or None,
            "job": {k: job.get(k) for k in ("state", "attempts", "error", "finished_at")} if job else None,
            "enabled": enabled()}


def _state(project_id: str) -> str:
    return str((db.get_project(project_id) or {}).get("state") or "")


def render_once(project_id: str) -> dict[str, Any]:
    """按当前时间线渲一版预览。没有算数的时间线 / 不在门禁 B 返回 skipped。"""
    if _state(project_id) != str(State.GATE_B):
        return {"skipped": "不在门禁 B"}
    saved = edit_project.active(project_id)
    if not saved:
        return {"skipped": "没有剪辑 v2 时间线"}
    planned = float((saved.get("plan") or {}).get("planned_at") or 0.0)
    tl = Timeline.from_props(saved)
    nar = db.read_json(project_id, "narration.json", {}) or {}
    work = config.project_dir(project_id) / "_edit"
    # 任务线程里没有项目的视觉包上下文:配乐 / 音效开不开跟着项目的包走,和成片一致
    with looks.use(looks.of_project(project_id)):
        sfx_path = edit_project._sfx(config.project_dir(project_id), tl, work / "preview_sfx.wav")
        bgm_path = edit_project._bgm(tl, nar, work / "preview_bed.wav")
    out = edit_render.preview(
        project_id, tl,
        narration_path=Path(nar["narration_path"]) if nar.get("narration_path") else None,
        bgm_path=bgm_path, sfx_path=sfx_path, output_name=PREVIEW_FILE)
    db.write_json(project_id, "preview_v2.json",
                  {"planned_at": planned, "rendered_at": round(time.time(), 1),
                   "seconds": tl.duration})
    return {"path": str(out), "planned_at": planned}


def _job(payload: dict[str, Any]) -> dict[str, Any]:
    project_id = str(payload["project_id"])
    result: dict[str, Any] = {}
    for _ in range(MAX_ROUNDS):
        result = render_once(project_id)
        if result.get("skipped"):
            return result
        now = float((edit_project.load(project_id).get("plan") or {}).get("planned_at") or 0.0)
        if now == result["planned_at"]:
            break  # 渲的就是最新的时间线
    return result


def _register_jobs() -> None:
    from ... import jobs

    # 渲染失败多半是环境问题(node / 浏览器),重试两次就够;审片页能看到错误
    jobs.register(PREVIEW_JOB, resource="render", max_attempts=2, lease=300.0)(_job)


_register_jobs()
