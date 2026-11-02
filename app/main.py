"""FastAPI 应用:Web 控制台 + 后台调度器。

设计要点:
- **后台调度循环**独立于请求。逐拍渲染要几分钟,由常驻循环每轮渲几拍推进,
  而不是让 HTTP 请求等待。
- **健康横幅**在启动时检测 ffmpeg 与生成通道,缺失能力以红色横幅明示,
  绝不静默降级。
- 所有密钥只从本地 .env 读取,**永远不会**出现在任何 API 响应里。
"""
from __future__ import annotations

import logging
import math
import mimetypes
import time
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from . import config, costs, db, logging_setup, maintenance
from .categories import available_categories, default_category, load_profile, suggest_theme
from .llm.deepseek import DeepSeekProvider
from .metrics import worker as metrics_worker
from .pipeline import aspect, explainer, library, orchestrator
from .pipeline import render as render_mod
from .pipeline.locks import ProjectBusy
from .pipeline.locks import busy_project_ids as locks_busy_ids
from .providers import cli
from .publish import accounts as publish_accounts
from .publish import service as publish_service
from .team import scheduler as team_scheduler
from .views import ProjectNotFound, build_detail, build_summary, needs_attention

log = logging.getLogger(__name__)

# Windows 注册表可能把 .js 登记成 text/plain,浏览器会拒绝按 ES Module 加载前端脚本
mimetypes.add_type("text/javascript", ".js")
mimetypes.add_type("text/css", ".css")

from .routes import library as library_routes
from .routes import projects as projects_routes
from .routes import publish as publish_routes
from .routes import team as team_routes
from .scheduler import (  # noqa: F401 - 旧代码与测试从 app.main 引用这些名字
    AUTO_ADVANCE_STATES,
    BACKOFF_BASE,
    BACKOFF_MAX,
    MAX_CONSECUTIVE_FAILURES,
    SCHEDULER_INTERVAL,
    Scheduler,
    scheduler,
)
from .templating import BASE_DIR, templates

HEALTH: dict[str, Any] = {}
# 侧边栏每页都要显示健康状态和套餐到期,做成模板全局,免得每个路由各传一遍
templates.env.globals.update(health=HEALTH, plan_expires_on=config.PLAN_EXPIRES_ON,
                             static_v=int(time.time()))  # 静态资源缓存戳:每次重启换新,改完样式不用强刷


@asynccontextmanager
async def lifespan(app: FastAPI):
    config.ensure_dirs()
    db.init_db()
    # 日志:控制台 + data/logs/AutoReel-AI.log(按天轮转)。此前整个项目
    # 一次也没用过 logging,排障只能看 serve.ps1 重定向的 stdout 文本。
    logging_setup.setup()
    # 清掉上次的临时物(data/_probe、data/_tmp 与散落的 _tmp_*.mp4)。
    # 它们是纯中间产物,却一直没有清理入口,已经积了 40 多 MB。
    try:
        maintenance.prune_temp()
    except Exception:  # noqa: BLE001 - 清理失败不该挡住服务启动
        log.warning("清理临时目录失败", exc_info=True)
    try:
        # 缓存按龄 + 体积上限(motion 的文件都很新,光按龄清不动它)
        pruned = maintenance.prune_caches()
        if pruned["removed"]:
            log.info("缓存清理:删 %d 项,腾出 %s", pruned["removed"], pruned["freed_human"])
    except Exception:  # noqa: BLE001 - 清理失败不该挡住服务启动
        log.warning("清理缓存失败", exc_info=True)

    # ---- 启动自检 ----
    ffmpeg_ok, ffmpeg_msg = config.ffmpeg_status()
    cli_ok, cli_msg = cli.health_check()
    motion_ok, motion_msg = config.motion_status()
    deepseek = DeepSeekProvider()
    HEALTH.update(
        {
            "ffmpeg": {"ok": ffmpeg_ok, "message": ffmpeg_msg},
            "connector": {"ok": cli_ok, "message": cli_msg},
            "motion": {"ok": motion_ok, "message": motion_msg},
            "llm": {
                "deepseek": deepseek.available(),
                "active": "deepseek" if deepseek.available() else None,
            },
            "plan_expires_on": config.PLAN_EXPIRES_ON,
            "checked_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
    )

    scheduler.start()
    # 分发侧的后台循环:发布任务、指标抓取。
    # 都是守护线程,进程退出即结束,不挡 Web。
    publish_accounts.import_from_file()
    publish_service.start()
    metrics_worker.start()
    team_scheduler.start()
    yield
    await scheduler.stop()
    publish_service.stop()
    metrics_worker.stop()
    team_scheduler.stop()


app = FastAPI(title="AutoReel-AI", version="0.1.0", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "web" / "static")), name="static")


@app.exception_handler(ProjectBusy)
async def _project_busy(request: Request, exc: ProjectBusy) -> JSONResponse:
    """项目正被别的执行流推进 → 409。

    这是**预期内**的并发(后台调度器与用户手动操作撞上了),不是错误。
    统一在这里转,免得每个写接口各写一遍 try/except。
    """
    return JSONResponse(status_code=409, content={"detail": str(exc)})


# --------------------------------------------------------------------------
# 视图
# --------------------------------------------------------------------------
# 视图模型在 app/views.py(纯函数,不碰 HTTP)。这里只留路由与调度。


# --------------------------------------------------------------------------
# 页面
# --------------------------------------------------------------------------
@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    rows = db.list_projects()
    projects = [build_summary(r) for r in rows]
    # 需要人处理的项目(配额暂停 / 失败)顶到最上面 —— 卡住的项目不该要人
    # 自己去列表里翻。参见 views.NEEDS_ATTENTION。
    attention = needs_attention(rows)
    cats = available_categories()
    # 片型默认值避开最近两条:连着建项目时不会总停在同一个片型上
    for cat in cats:
        if cat.get("themes"):
            cat["suggested_theme"] = suggest_theme(load_profile(cat["name"]),
                                                   db.recent_themes(cat["name"]))
    # 方向下拉按当前品类联动:默认展示第一个品类的方向
    active = cats[0] if cats else {"themes": [], "default_theme": ""}
    # ⚠️ 新版 Starlette 的签名为 TemplateResponse(request, name, context),
    #    旧的 (name, context) 形式会把 context 误当成 name,报 "unhashable type: dict"。
    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "request": request,
            "projects": projects,
            "attention": attention,
            "categories": cats,
            "themes": active.get("themes", []),
            "default_theme": active.get("default_theme", ""),
            "health": HEALTH,
            "durations": config.SUPPORTED_DURATIONS,
            "plan_expires_on": config.PLAN_EXPIRES_ON,
        },
    )


#: 解说轨每拍的墙钟(秒),含 TTS 分摊与最终合成。YU7 实测 27 拍 5.4 分钟 ≈ 12 秒/拍
EXPLAINER_SECONDS_PER_BEAT = 12
#: 按画幅的墙钟系数。竖屏与横屏像素数相同,模糊铺底多一层滤镜,先按 1.0 记,实测后再调
RENDER_FACTOR = {config.LANDSCAPE: 1.0, config.PORTRAIT: 1.0}


def _explainer_minutes(beats: int, aspect_value: str = config.LANDSCAPE) -> int:
    factor = RENDER_FACTOR.get(aspect_value, 1.0)
    return max(1, math.ceil(beats * EXPLAINER_SECONDS_PER_BEAT * factor / 60))


@app.get("/project/{project_id}", response_class=HTMLResponse)
def project_page(request: Request, project_id: str):
    try:
        detail = build_detail(project_id, stage="final")
        if not detail.shots:
            detail = build_detail(project_id, stage="draft")
    except ProjectNotFound as exc:
        raise HTTPException(404, str(exc)) from exc
    profile = load_profile(detail.category)
    w, h = aspect.size_for(detail.aspect)
    beats = len(detail.shots) or detail.shot_count
    estimate = costs.estimate_explainer(detail.duration)
    facts_report = db.read_json(project_id, "facts_report.json", {}) or {}
    roster = []
    if detail.subjects:
        # 横评:每台车一个池子,费用按车累加;门禁 A 按车看事实数和素材秒数
        per = facts_report.get("per_subject") or {}
        thin = set(facts_report.get("subjects_thin") or [])
        vlm = 0
        for s in detail.subjects:
            st = library.Library.load(s.name).stats()
            vlm += st["vlm_calls"]
            got = per.get(s.name) or {}
            roster.append({"name": s.name, "group": s.group, "facts": got.get("facts"),
                           "product": got.get("product"), "thin": s.name in thin,
                           "price_range": got.get("price_range"),
                           "off_band": got.get("in_band") is False,
                           "usable_seconds": st["usable_seconds"],
                           "confirmed_video_seconds": st["confirmed_video_seconds"],
                           "building": library.is_building(s.name)})
        lib = {"vlm_calls": vlm}
        # 横评每台车只占一小段,门槛按轻量池算
        thin_at = library.ROUNDUP_ENOUGH_SECONDS / 2
        footage_thin = [{"name": r["name"], "seconds": r["confirmed_video_seconds"]}
                        for r in roster if not r["building"]
                        and r["confirmed_video_seconds"] < thin_at]
    else:
        lib = library.Library.load(detail.subject).stats()
        gaps = [] if library.is_building(detail.subject) else library.footage_gaps(lib)
        footage_thin = ([{"name": detail.subject, "seconds": lib["confirmed_video_seconds"],
                          "reasons": gaps}] if gaps else [])
    estimate["library_vlm_calls"] = lib["vlm_calls"]
    estimate["library"] = costs.estimate_library(lib["vlm_calls"])
    wall_clock = {"render_minutes": _explainer_minutes(beats, detail.aspect)}
    return templates.TemplateResponse(
        request,
        "project.html",
        {
            "request": request,
            "p": detail,
            "health": HEALTH,
            "lint": db.read_json(project_id, "lint.json", []) or [],
            "facts_report": facts_report,
            "roster": roster,
            "footage_thin": footage_thin,
            "footage": None if detail.subjects else lib,
            "footage_mix": explainer.footage_mix(project_id, orchestrator._specs(project_id)),
            "render": db.read_json(project_id, "render.json", {}) or {},
            "events": db.get_events(project_id, limit=40),
            "wall_clock": wall_clock,
            "estimate": estimate,
            "frame": f"{w} × {h}",
            "forbidden_terms": list(profile.forbidden_terms),
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
    out = dict(HEALTH)
    # 外部能力调用统计:之前完全不可见,只能靠猜额度烧在哪
    out["provider_calls"] = cli.call_stats()
    # 调度器当前盯着的项目与失败次数
    out["scheduler"] = {
        "concurrency": config.PIPELINE_CONCURRENCY,
        "interval": SCHEDULER_INTERVAL,
        "failures": dict(scheduler._failures),
        "busy_projects": locks_busy_ids(),
        "queued": sorted(scheduler._queued - scheduler._running),
        "running": sorted(scheduler._running),
    }
    # 磁盘水位:data/ 只会涨,并行渲染会成倍放大
    size = maintenance.data_size()
    out["disk"] = {
        "data_bytes": size,
        "data_human": maintenance.human(size),
        "over_budget": size > config.DATA_SIZE_WARN_BYTES,
        "warn_bytes": config.DATA_SIZE_WARN_BYTES,
    }
    return out


@app.get("/api/categories")
def categories():
    return available_categories()


@app.get("/api/estimate")
def estimate(duration: int = 240, category: str = "", aspect: str = config.LANDSCAPE):
    """时长跟着旁白走,这里只给节拍数的估计。"""
    profile = load_profile(category or default_category())
    beats = profile.shots_for(duration)
    if aspect not in config.ASPECTS:
        aspect = config.LANDSCAPE
    return {
        "duration": duration,
        "aspect": aspect,
        "cost": costs.estimate_explainer(duration),
        "wall_clock": {"render_minutes": _explainer_minutes(beats, aspect),
                       "note": f"逐拍渲染约 {EXPLAINER_SECONDS_PER_BEAT} 秒/拍,无生成排队"},
        "shot_count": beats,
    }


#: 热点选题内存缓存:category -> 结果。抓取 + LLM 要几十秒,30 分钟内复用
_TOPICS_CACHE: dict[str, dict[str, Any]] = {}
TOPICS_TTL = 30 * 60


@app.get("/api/topics")
def hot_topics(category: str = "", refresh: int = 0):
    """抓实时热点 -> LLM 归纳单车 / 横评选题。同步函数,FastAPI 放线程池里跑。"""
    from .pipeline import topics
    from .providers import trends

    category = category or default_category()
    cached = _TOPICS_CACHE.get(category)
    if cached and not refresh and time.time() - cached["fetched_at"] < TOPICS_TTL:
        return cached
    profile = load_profile(category)
    # 热点口径按品类走 profile.trends:母婴不调懂车帝,新闻 / 抖音 / B站换母婴词
    items, errors = trends.collect(spec=profile.trends, brands=profile.trend_brands)
    llm = orchestrator._llm()
    if llm is None:
        errors = {**errors, "llm": "没有可用的 LLM,只能列出原始热点"}
    try:
        ideas = topics.suggest(items, profile=profile, llm=llm)
    except Exception as exc:  # noqa: BLE001 - 选题失败仍返回原始热点,让用户自己挑
        ideas, errors = [], {**errors, "llm": f"选题生成失败:{exc}"}
    result = {
        "topics": [t.model_dump() for t in ideas],
        "trends": [i.to_dict() if hasattr(i, "to_dict") else dict(i) for i in items],
        "errors": errors,
        "sources_label": profile.trends.sources_label,
        "fetched_at": time.time(),
    }
    if ideas:
        _TOPICS_CACHE[category] = result
    return result


@app.get("/api/render/selfcheck")
def render_selfcheck():
    return render_mod.self_check()


app.include_router(library_routes.router)
app.include_router(projects_routes.router)
app.include_router(publish_routes.router)
app.include_router(team_routes.router)


@app.get("/favicon.ico")
def favicon():
    return RedirectResponse("/static/favicon.svg")
