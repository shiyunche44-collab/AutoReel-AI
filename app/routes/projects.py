"""项目的创建、推进、审阅、素材与产出下载(从 main.py 拆出)。"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request
from fastapi.responses import FileResponse, RedirectResponse
from pydantic import BaseModel

from .. import config, db, maintenance
from ..models import GateRequest, ProjectCreate, RetryRequest, ScriptPatch, ScriptSegment, ShotPatch
from ..pipeline import aspect, concept_styles, orchestrator
from ..scheduler import scheduler
from ..state import State
from ..views import ProjectNotFound, build_detail, build_summary

log = logging.getLogger(__name__)

router = APIRouter()


@router.post("/api/projects")
def create_project(payload: ProjectCreate, background: BackgroundTasks):
    try:
        project_id = orchestrator.create_project(payload)
    except orchestrator.PipelineError as exc:
        raise HTTPException(400, str(exc)) from exc
    # 后台先跑"零生成消耗"的前置阶段(事实→脚本→分镜),直到门禁 A
    background.add_task(_bootstrap, project_id)
    return {"id": project_id, "state": str(State.DRAFT_INTENT)}


def _bootstrap(project_id: str) -> None:
    """在后台把项目推进到门禁 A。不涉及任何生成调用,因此可以自动跑。"""
    try:
        orchestrator.run_until_waiting(project_id, max_steps=6)
    except Exception as exc:  # noqa: BLE001
        db.update_project(project_id, state=str(State.FAILED), state_reason=str(exc))


@router.get("/api/projects")
def list_projects():
    return [build_summary(r) for r in db.list_projects()]


@router.get("/api/projects/{project_id}")
def get_project(project_id: str, stage: str = "final"):
    try:
        return build_detail(project_id, stage)
    except ProjectNotFound as exc:
        raise HTTPException(404, str(exc)) from exc


@router.delete("/api/projects/{project_id}")
def delete_project(project_id: str, pool: bool = False):
    """删项目。

    `pool=true` 连这辆车（或这几台车）的**素材池**一起删 —— 池子是跨项目
    共享的,删之前会先看还有谁在用,有别人在用就拒绝。
    """
    row = db.get_project(project_id)
    if not row:
        raise HTTPException(404, "项目不存在")
    import shutil

    db.delete_shots(project_id, "draft")
    db.delete_shots(project_id, "final")
    db.update_project(project_id, state=str(State.CANCELLED))
    shutil.rmtree(config.project_dir(project_id), ignore_errors=True)
    out: dict[str, Any] = {"ok": True}
    if pool:
        names = [row.get("subject") or ""]
        try:
            items = json.loads(row.get("subjects") or "[]")
        except (json.JSONDecodeError, TypeError):
            items = []
        for item in items or []:
            if isinstance(item, dict) and item.get("name"):
                names.append(str(item["name"]))
        evicted = []
        blocked = []
        for name in names:
            if not name:
                continue
            res = maintenance.evict_pool(name, force=True)
            (evicted if res.get("ok") else blocked).append(name)
        out["pools_evicted"] = evicted
        out["pools_blocked"] = blocked
    return out


class AdvanceRequest(BaseModel):
    stage: str | None = None


@router.post("/api/projects/{project_id}/advance")
def advance_project(project_id: str):
    try:
        out = orchestrator.advance(project_id)
    except orchestrator.PipelineError as exc:
        raise HTTPException(400, str(exc)) from exc
    if out.get("busy"):
        # 同一个项目正被后台调度器推进,这一轮就让给它
        raise HTTPException(409, "该项目正在推进中,请稍后重试")
    return out


@router.post("/api/projects/{project_id}/library/rebuild")
def rebuild_project_library(project_id: str):
    try:
        return orchestrator.rebuild_library(project_id)
    except orchestrator.PipelineError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.post("/api/projects/{project_id}/gate")
def confirm_gate(project_id: str, payload: GateRequest):
    try:
        return orchestrator.confirm_gate(project_id, payload)
    except orchestrator.PipelineError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.get("/api/projects/{project_id}/review")
def review(project_id: str):
    """门禁 B 的审片进度(整片一批)。"""
    row = db.get_project(project_id)
    if not row:
        raise HTTPException(404, "项目不存在")
    return orchestrator.review_progress(project_id)


@router.patch("/api/projects/{project_id}/script")
def patch_script(project_id: str, payload: ScriptPatch):
    project = db.get_project(project_id)
    if not project:
        raise HTTPException(404, "项目不存在")
    segments = payload.segments or [
        ScriptSegment(**d) for d in db.read_json(project_id, "script.json", []) or []
    ]
    try:
        issues = orchestrator.update_script(project_id, segments, payload.title, payload.thesis)
    except orchestrator.PipelineError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"ok": True, "segments": len(segments), "issues": issues}


@router.patch("/api/projects/{project_id}/shots")
def patch_shots(project_id: str, payload: ShotPatch):
    try:
        orchestrator.update_shots(project_id, payload.shots)
    except orchestrator.PipelineError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"ok": True, "shots": len(payload.shots)}


@router.post("/api/projects/{project_id}/shots/{idx}/retry")
def retry_shot(project_id: str, idx: int, payload: RetryRequest | None = None):
    stage = (payload.stage if payload else "final")
    orchestrator.retry(project_id, idx, stage)
    return {"ok": True}


class ConceptStyleRequest(BaseModel):
    style: str


@router.get("/api/concept-styles")
def concept_style_gallery():
    """全部概念动画风格 + 固定示例图(仓库里的静态文件,不生成)。"""
    return concept_styles.gallery()


@router.get("/api/concept-styles/image/{name}")
def concept_style_image(name: str):
    path = concept_styles.sample_image(Path(name).name)
    if not path:
        raise HTTPException(404, "图片不存在")
    return FileResponse(path, media_type="image/jpeg")


@router.get("/api/projects/{project_id}/concept-style")
def concept_style_status(project_id: str):
    if not db.get_project(project_id):
        raise HTTPException(404, "项目不存在")
    return concept_styles.project_status(project_id)


@router.post("/api/projects/{project_id}/concept-style")
def concept_style_set(project_id: str, payload: ConceptStyleRequest):
    try:
        return orchestrator.set_concept_style(project_id, payload.style)
    except orchestrator.PipelineError as exc:
        raise HTTPException(400, str(exc)) from exc


class LookRequest(BaseModel):
    look: str


@router.get("/api/projects/{project_id}/look")
def look_status(project_id: str):
    """项目的视觉包 + 可选列表。渲染开始后只读。"""
    from ..pipeline import looks

    row = db.get_project(project_id)
    if not row:
        raise HTTPException(404, "项目不存在")
    return {
        "look": looks.of_project(project_id).id,
        "options": looks.options(),
        "editable": row["state"] in {str(s) for s in orchestrator._STYLE_FREE},
        "portrait": aspect.is_portrait(row),
    }


@router.post("/api/projects/{project_id}/look")
def look_set(project_id: str, payload: LookRequest):
    try:
        return orchestrator.set_look(project_id, payload.look)
    except orchestrator.PipelineError as exc:
        raise HTTPException(400, str(exc)) from exc


class BudgetRequest(BaseModel):
    budget: float


@router.patch("/api/projects/{project_id}/budget")
def set_project_budget(project_id: str, payload: BudgetRequest):
    """改项目的等效用量上限;项目若正因配额暂停,顺带恢复到暂停前的状态。"""
    try:
        return orchestrator.set_budget(project_id, payload.budget)
    except orchestrator.PipelineError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.post("/project/{project_id}/budget")
async def project_budget_form(project_id: str, request: Request):
    """项目页上的"提额并继续"表单。

    用 urlencoded 表单提交(普通 HTML form),所以**零 JS**。表单自己
    解析,不用 Starlette 的 `request.form()` —— 那个硬依赖
    python-multipart,而本项目不需要为了一个数字输入框多装一个包。
    """
    body = (await request.body()).decode("utf-8", "replace")
    values = dict(parse_qsl(body))
    try:
        budget = float(values.get("budget") or 0)
    except (TypeError, ValueError):
        budget = 0.0
    try:
        orchestrator.set_budget(project_id, budget)
    except orchestrator.PipelineError as exc:
        raise HTTPException(400, str(exc)) from exc
    return RedirectResponse(f"/project/{project_id}", status_code=303)


@router.get("/api/projects/{project_id}/events")
def events(project_id: str):
    return db.get_events(project_id)


@router.get("/api/projects/{project_id}/status")
def status(project_id: str):
    """轻量轮询端点:只返回 UI 需要的字段。"""
    row = db.get_project(project_id)
    if not row:
        raise HTTPException(404, "项目不存在")
    stage = "draft" if row["state"] == str(State.DRAFT_RENDERING) else "final"
    shots = db.get_shots(project_id, stage)
    counts: dict[str, int] = {}
    for s in shots:
        counts[s["status"]] = counts.get(s["status"], 0) + 1
    return {
        "id": row["id"],
        "state": row["state"],
        "state_reason": row.get("state_reason"),
        "spent_cost": row["spent_cost"],
        "quota_budget": row["quota_budget"],
        "stage": stage,
        "counts": counts,
        "total": len(shots),
        # 并行渲染:这个项目现在是在排队还是在跑
        "queued": project_id in scheduler._queued and project_id not in scheduler._running,
        "running": project_id in scheduler._running,
    }


@router.get("/api/projects/{project_id}/download")
def download(project_id: str):
    path = aspect.output_path(project_id)
    if not path.is_file():
        raise HTTPException(404, "成片尚未生成")
    return FileResponse(path, media_type="video/mp4", filename=path.name)


@router.get("/api/projects/{project_id}/cover")
def cover(project_id: str, variant: str = "a", ratio: str = ""):
    """封面。variant=a|b;ratio=3x4 取竖屏的 3:4 版(给小红书)。"""
    from ..pipeline import packaging

    if variant not in packaging.COVER_VARIANTS:
        raise HTTPException(400, "variant 只能是 a 或 b")
    path = packaging.cover_file(project_id, variant, ratio=ratio)
    if path is None:
        raise HTTPException(404, "封面尚未生成")
    return FileResponse(path, media_type="image/jpeg")


@router.get("/media/{project_id}/{kind}/{name}")
def media(project_id: str, kind: str, name: str):
    """素材预览。kind ∈ {drafts, shots, audio}。"""
    allowed = {"drafts", "shots", "audio"}
    if kind not in allowed:
        raise HTTPException(400, f"不支持的目录:{kind}")
    safe = Path(name).name  # 阻断路径穿越
    path = config.project_dir(project_id) / kind / safe
    if not path.is_file():
        raise HTTPException(404, "文件不存在")
    return FileResponse(path)
