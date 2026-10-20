"""FastAPI 应用:Web 控制台 + 后台调度器。

设计要点:
- **后台调度循环**独立于请求。H3 单任务 15-30 分钟,必须由常驻循环轮询推进,
  而不是让 HTTP 请求等待。
- **健康横幅**在启动时检测 ffmpeg 与生成通道,缺失能力以红色横幅明示,
  绝不静默降级。
- 所有密钥只从本地 .env 读取,**永远不会**出现在任何 API 响应里。
"""
from __future__ import annotations

import asyncio
import mimetypes
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import BackgroundTasks, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from . import config, costs, db
from .categories import available_categories, load_profile
from .llm.deepseek import DeepSeekProvider
from .llm.minimax import MiniMaxProvider
from .models import (
    Fact,
    GateRequest,
    ProjectCreate,
    ProjectDetail,
    ProjectSummary,
    RetryRequest,
    ScriptPatch,
    ScriptSegment,
    Shot,
    ShotPatch,
    ShotSpec,
)
from .pipeline import orchestrator
from .pipeline import render as render_mod
from .providers import cli
from .state import State

# Windows 注册表可能把 .js 登记成 text/plain,浏览器会拒绝按 ES Module 加载前端脚本
mimetypes.add_type("text/javascript", ".js")
mimetypes.add_type("text/css", ".css")

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


templates.env.filters["hhmmss"] = _fmt_hhmmss
templates.env.filters["datetime"] = _fmt_datetime
templates.env.filters["basename"] = _basename

#: 后台循环间隔(秒)。H3 轮询间隔本身由 poll_due_for_poll 控制,
#: 这个值只是"多久扫一次项目表"。
SCHEDULER_INTERVAL = 20

#: 后台循环会自动推进的状态。缺任何一个都表现为"流水线静静卡住"——
#: 不会报错、不会告警,镜头只是永远停在 pending。DRAFT_RENDERING 曾被漏掉,
#: 导致草稿档从来不会自己跑起来。
AUTO_ADVANCE_STATES = frozenset({
    str(State.DRAFT_RENDERING),
    str(State.FINAL_RENDERING),
    str(State.COMPOSING),
})


# --------------------------------------------------------------------------
# 后台调度
# --------------------------------------------------------------------------
class Scheduler:
    """扫描处于生成态的项目并推进一次。"""

    def __init__(self) -> None:
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()

    async def _run(self) -> None:
        while not self._stop.is_set():
            try:
                await self.sweep()
            except Exception:  # noqa: BLE001 - 调度循环不能因单项目异常而退出
                pass
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=SCHEDULER_INTERVAL)
            except asyncio.TimeoutError:
                continue

    async def sweep(self) -> None:
        """推进所有处于自动推进态的项目一次。"""
        rows = db.list_projects(limit=50)
        for row in rows:
            if row["state"] not in AUTO_ADVANCE_STATES:
                continue
            try:
                orchestrator.advance(row["id"])
            except orchestrator.PipelineError:
                db.update_project(row["id"], state=str(State.FAILED),
                                  state_reason="后台推进失败")
            except Exception as exc:  # noqa: BLE001
                db.update_project(row["id"], state=str(State.FAILED), state_reason=str(exc))

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._stop.clear()
            self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        self._stop.set()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass


scheduler = Scheduler()

HEALTH: dict[str, Any] = {}


@asynccontextmanager
async def lifespan(app: FastAPI):
    config.ensure_dirs()
    db.init_db()

    # ---- 启动自检 ----
    ffmpeg_ok, ffmpeg_msg = config.ffmpeg_status()
    cli_ok, cli_msg = cli.health_check()
    deepseek = DeepSeekProvider()
    minimax = MiniMaxProvider()
    HEALTH.update(
        {
            "ffmpeg": {"ok": ffmpeg_ok, "message": ffmpeg_msg},
            "connector": {"ok": cli_ok, "message": cli_msg},
            "llm": {
                "deepseek": deepseek.available(),
                "minimax": minimax.available(),
                "active": "minimax" if minimax.available()
                          else ("deepseek" if deepseek.available() else None),
            },
            "plan_expires_on": config.PLAN_EXPIRES_ON,
            "checked_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
    )

    scheduler.start()
    yield
    await scheduler.stop()


app = FastAPI(title="aimovice", version="0.1.0", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "web" / "static")), name="static")


# --------------------------------------------------------------------------
# 视图
# --------------------------------------------------------------------------
def _shots_to_models(rows: list[dict[str, Any]]) -> list[Shot]:
    specs = {s.idx: s for s in _load_specs_from_db(rows)}
    shots: list[Shot] = []
    for row in rows:
        spec = specs.get(row["idx"])
        if spec is None:
            continue
        shots.append(
            Shot(
                spec=spec,
                status=row["status"],
                stage=row["stage"],
                model=row.get("model") or "",
                provider_task_id=row.get("provider_task_id"),
                draft_path=row.get("draft_path"),
                final_path=row.get("final_path"),
                attempts=row.get("attempts") or 0,
                error=row.get("error"),
                consistency=row.get("consistency") or "ok",
                chain_id=row.get("chain_id") or 0,
                chain_pos=row.get("chain_pos") or 0,
                est_cost=row.get("est_cost") or 0.0,
            )
        )
    return shots


def _load_specs_from_db(rows: list[dict[str, Any]]) -> list[ShotSpec]:
    if not rows:
        return []
    project_id = rows[0]["project_id"]
    return [ShotSpec(**d) for d in db.read_json(project_id, "storyboard.json", []) or []]


def build_detail(project_id: str, stage: str = "final") -> ProjectDetail:
    row = db.get_project(project_id)
    if not row:
        raise HTTPException(404, "项目不存在")
    shot_rows = db.get_shots(project_id, stage) or db.get_shots(project_id, "draft")
    project_dir = config.project_dir(project_id)
    output = project_dir / "output.mp4"
    return ProjectDetail(
        id=row["id"],
        title=row["title"],
        subject=row["subject"],
        category=row["category"],
        duration=row["duration"],
        shot_count=row["shot_count"],
        state=row["state"],
        est_cost=row["est_cost"],
        spent_cost=row["spent_cost"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        has_output=output.is_file(),
        narration_mode=row["narration_mode"],
        quota_budget=row["quota_budget"],
        state_reason=row.get("state_reason"),
        facts=[Fact(**d) for d in db.read_json(project_id, "facts.json", []) or []],
        script=[ScriptSegment(**d) for d in db.read_json(project_id, "script.json", []) or []],
        shots=_shots_to_models(shot_rows),
        refs=db.read_json(project_id, "refs.json", []) or [],
        clip_report=db.read_json(project_id, "clip_report.json", {}) or {},
        prompt_errors=db.read_json(project_id, "prompt_errors.json", {}) or {},
    )


def _theme_label(category: str, theme: str) -> str:
    """方向名 -> 中文标签。未知品类/方向一律返回原值,不阻塞页面渲染。"""
    try:
        profile = load_profile(category)
    except Exception:  # noqa: BLE001 - 列表页不该因单个品类配置问题挂掉
        return theme
    spec = profile.theme_spec(theme or None)
    return spec.label or theme


def build_summary(row: dict[str, Any]) -> ProjectSummary:
    project_dir = config.project_dir(row["id"])
    return ProjectSummary(
        id=row["id"],
        title=row["title"],
        subject=row["subject"],
        category=row["category"],
        theme=row.get("theme") or "",
        theme_label=_theme_label(row["category"], row.get("theme") or ""),
        duration=row["duration"],
        shot_count=row["shot_count"],
        state=row["state"],
        est_cost=row["est_cost"],
        spent_cost=row["spent_cost"],
        quota_budget=row["quota_budget"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        has_output=(project_dir / "output.mp4").is_file(),
    )


# --------------------------------------------------------------------------
# 页面
# --------------------------------------------------------------------------
@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    projects = [build_summary(r) for r in db.list_projects()]
    cats = available_categories()
    # 方向下拉按当前品类联动:默认展示 auto 的方向,切品类时前端再刷新
    active = cats[0] if cats else {"themes": [], "default_theme": ""}
    # ⚠️ 新版 Starlette 的签名为 TemplateResponse(request, name, context),
    #    旧的 (name, context) 形式会把 context 误当成 name,报 "unhashable type: dict"。
    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "request": request,
            "projects": projects,
            "categories": cats,
            "themes": active.get("themes", []),
            "default_theme": active.get("default_theme", ""),
            "health": HEALTH,
            "durations": config.SUPPORTED_DURATIONS,
            "plan_expires_on": config.PLAN_EXPIRES_ON,
        },
    )


@app.get("/project/{project_id}", response_class=HTMLResponse)
def project_page(request: Request, project_id: str):
    detail = build_detail(project_id, stage="final")
    if not detail.shots:
        detail = build_detail(project_id, stage="draft")
    return templates.TemplateResponse(
        request,
        "project.html",
        {
            "request": request,
            "p": detail,
            "health": HEALTH,
            "lint": db.read_json(project_id, "lint.json", []) or [],
            "facts_report": db.read_json(project_id, "facts_report.json", {}) or {},
            "qa": db.read_json(project_id, "qa.json", {}) or {},
            "render": db.read_json(project_id, "render.json", {}) or {},
            "events": db.get_events(project_id, limit=40),
            "wall_clock": costs.wall_clock_estimate(detail.duration),
            "estimate": costs.estimate_project(detail.duration),
            "forbidden_terms": list(load_profile(detail.category).forbidden_terms),
        },
    )


@app.get("/project/{project_id}/logs", response_class=HTMLResponse)
def project_logs(request: Request, project_id: str):
    return templates.TemplateResponse(
        request,
        "logs.html",
        {"request": request, "project_id": project_id,
         "events": db.get_events(project_id, limit=200)},
    )


# --------------------------------------------------------------------------
# API
# --------------------------------------------------------------------------
@app.get("/api/health")
def health():
    return HEALTH


@app.get("/api/categories")
def categories():
    return available_categories()


@app.get("/api/estimate")
def estimate(duration: int = 60, category: str = "auto"):
    profile = load_profile(category)
    if profile.is_explainer:
        # 横屏解说轨:不调视频生成,时长跟着旁白走,这里只给节拍数的估计
        return {
            "duration": duration,
            "cost": {"draft": 0.0, "final": 0.0, "with_retry": 0.0},
            "wall_clock": {"note": "逐拍渲染约 5 秒/拍,无生成排队"},
            "shot_count": profile.shots_for(duration),
            "track": "explainer",
            "real_clips": {"enabled": False, "max_shots": 0, "max_savings": 0.0},
        }
    shot_count = config.shots_for_duration(duration)
    # 真实素材最多能覆盖多少镜头:外观视角的镜头才可能拿到真实片段。
    # 这是**上界**,不是承诺 —— 实际能不能取到要看网上有没有可用的直链。
    clip_cap = min(config.REAL_CLIP_LIMIT, shot_count) if config.USE_REAL_CLIPS else 0
    return {
        "duration": duration,
        "cost": costs.estimate_project(duration),
        "wall_clock": costs.wall_clock_estimate(duration),
        "shot_count": shot_count,
        "real_clips": {
            "enabled": bool(clip_cap),
            "max_shots": clip_cap,
            "max_savings": costs.savings_from_clips(clip_cap, duration=config.SHOT_GRID_SECONDS),
        },
    }


@app.post("/api/projects")
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


@app.get("/api/projects")
def list_projects():
    return [build_summary(r) for r in db.list_projects()]


@app.get("/api/projects/{project_id}")
def get_project(project_id: str, stage: str = "final"):
    return build_detail(project_id, stage)


@app.delete("/api/projects/{project_id}")
def delete_project(project_id: str):
    row = db.get_project(project_id)
    if not row:
        raise HTTPException(404, "项目不存在")
    import shutil

    db.delete_shots(project_id, "draft")
    db.delete_shots(project_id, "final")
    db.update_project(project_id, state=str(State.CANCELLED))
    shutil.rmtree(config.project_dir(project_id), ignore_errors=True)
    return {"ok": True}


class AdvanceRequest(BaseModel):
    stage: str | None = None


@app.post("/api/projects/{project_id}/advance")
def advance_project(project_id: str):
    try:
        return orchestrator.advance(project_id)
    except orchestrator.PipelineError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.post("/api/projects/{project_id}/gate")
def confirm_gate(project_id: str, payload: GateRequest):
    try:
        return orchestrator.confirm_gate(project_id, payload)
    except orchestrator.PipelineError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.get("/api/projects/{project_id}/review")
def review(project_id: str):
    """门禁 B 的分批审片进度。

    批次按一致性链切分,保证一条链不会被劈到两批 ——
    链内镜头靠尾帧接首帧保持连续,劈开就断了。
    """
    row = db.get_project(project_id)
    if not row:
        raise HTTPException(404, "项目不存在")
    return orchestrator.review_progress(project_id)


@app.patch("/api/projects/{project_id}/script")
def patch_script(project_id: str, payload: ScriptPatch):
    project = db.get_project(project_id)
    if not project:
        raise HTTPException(404, "项目不存在")
    segments = payload.segments or [
        ScriptSegment(**d) for d in db.read_json(project_id, "script.json", []) or []
    ]
    try:
        orchestrator.update_script(project_id, segments, payload.title)
    except orchestrator.PipelineError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"ok": True, "segments": len(segments)}


@app.patch("/api/projects/{project_id}/shots")
def patch_shots(project_id: str, payload: ShotPatch):
    try:
        orchestrator.update_shots(project_id, payload.shots)
    except orchestrator.PipelineError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"ok": True, "shots": len(payload.shots)}


@app.post("/api/projects/{project_id}/shots/{idx}/retry")
def retry_shot(project_id: str, idx: int, payload: RetryRequest | None = None):
    stage = (payload.stage if payload else "final")
    orchestrator.retry(project_id, idx, stage)
    return {"ok": True}


@app.get("/api/projects/{project_id}/events")
def events(project_id: str):
    return db.get_events(project_id)


@app.get("/api/projects/{project_id}/status")
def status(project_id: str):
    """轻量轮询端点:只返回 UI 需要的字段。"""
    row = db.get_project(project_id)
    if not row:
        raise HTTPException(404, "项目不存在")
    stage = "final" if row["state"] == str(State.FINAL_RENDERING) else (
        "draft" if row["state"] == str(State.DRAFT_RENDERING) else "final"
    )
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
        "degraded": [s["idx"] for s in shots if s.get("consistency") == "degraded"],
    }


@app.get("/api/projects/{project_id}/download")
def download(project_id: str):
    path = config.project_dir(project_id) / "output.mp4"
    if not path.is_file():
        raise HTTPException(404, "成片尚未生成")
    return FileResponse(path, media_type="video/mp4", filename=path.name)


@app.get("/api/projects/{project_id}/cover")
def cover(project_id: str):
    path = config.project_dir(project_id) / "cover.jpg"
    if not path.is_file():
        raise HTTPException(404, "封面尚未生成")
    return FileResponse(path, media_type="image/jpeg")


@app.get("/media/{project_id}/{kind}/{name}")
def media(project_id: str, kind: str, name: str):
    """素材预览。kind ∈ {drafts, shots, refs, audio, prompts}。"""
    allowed = {"drafts", "shots", "refs", "audio", "prompts"}
    if kind not in allowed:
        raise HTTPException(400, f"不支持的目录:{kind}")
    safe = Path(name).name  # 阻断路径穿越
    path = config.project_dir(project_id) / kind / safe
    if not path.is_file():
        raise HTTPException(404, "文件不存在")
    return FileResponse(path)


@app.get("/api/render/selfcheck")
def render_selfcheck():
    return render_mod.self_check()


@app.get("/favicon.ico")
def favicon():
    return RedirectResponse("/static/favicon.svg")
