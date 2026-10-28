"""分发:多平台发布、账号、成片包装、数据看板。"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from .. import db
from ..metrics import store as metrics_store
from ..pipeline import aspect
from ..publish import accounts as publish_accounts
from ..publish import browser as publish_browser
from ..publish import metadata as publish_metadata
from ..publish import service as publish_service
from ..publish.registry import PLATFORMS, platforms_info
from ..templating import templates

router = APIRouter()


# --------------------------------------------------------------------------
# 分发:一键发布多平台
# --------------------------------------------------------------------------
class AccountPayload(BaseModel):
    """账号(凭据)。`id` 为空 = 新建。"""

    id: str | None = None
    platform: str
    name: str = ""
    auth_kind: str = "cookie"
    #: 凭据。**只写不读** —— 读回来永远是打码的
    credential: dict[str, Any] = {}
    #: 平台特定设置:B站分区 tid、YouTube 频道 ID、可见范围 …
    meta: dict[str, Any] = {}
    enabled: bool = True


class PublishPayload(BaseModel):
    """一次「一键发布」。"""

    #: 要发到哪些账号
    account_ids: list[str] = []
    #: 逐平台的文案覆盖:{"bilibili": {"title": ..., "description": ..., "tags": [...]}}
    overrides: dict[str, dict[str, Any]] | None = None
    #: 已发过的是否重发
    force: bool = False


@router.get("/publish", response_class=HTMLResponse)
def publish_page(request: Request):
    """分发台:账号管理 + 发布队列。"""
    accounts = [publish_accounts.account_view(a)
                for a in publish_accounts.list_accounts()]
    queue = [publish_service.publication_view(r) for r in db.list_publications(limit=200)]
    projects = []
    for row in db.list_projects(limit=100):
        if row["state"] != "DONE":
            continue
        projects.append({
            "id": row["id"], "title": row["title"], "subject": row["subject"],
            "has_output": aspect.output_path(row["id"]).is_file(),
            "aspect": aspect.of(row),
        })
    return templates.TemplateResponse(
        request, "publish.html",
        {"request": request, "accounts": accounts, "queue": queue,
         "projects": projects, "platforms": platforms_info()},
    )


@router.get("/analytics", response_class=HTMLResponse)
def analytics_page(request: Request):
    """数据看板:作品指标 + 趋势。"""
    data = metrics_store.overview()
    return templates.TemplateResponse(
        request, "analytics.html",
        {"request": request, "items": data["items"], "by_platform": data["by_platform"],
         "by_aspect": data["by_aspect"], "platforms": platforms_info()},
    )


@router.get("/api/publish/platforms")
def publish_platforms():
    return platforms_info()


@router.get("/api/publish/accounts")
def publish_accounts_list():
    return [publish_accounts.account_view(a) for a in publish_accounts.list_accounts()]


@router.get("/api/publish/queue")
def publish_queue():
    """发布队列(轮询用)。"""
    return [publish_service.publication_view(r) for r in db.list_publications(limit=200)]


@router.post("/api/publish/accounts")
def publish_account_save(payload: AccountPayload):
    try:
        account = publish_accounts.save_account(
            platform=payload.platform, name=payload.name or payload.platform,
            credential=payload.credential, meta=payload.meta,
            auth_kind=payload.auth_kind, account_id=payload.id, enabled=payload.enabled)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return publish_accounts.account_view(account)


@router.delete("/api/publish/accounts/{account_id}")
def publish_account_delete(account_id: str):
    if not db.get_account(account_id):
        raise HTTPException(404, "账号不存在")
    publish_accounts.delete_account(account_id)
    return {"ok": True}


@router.post("/api/publish/accounts/{account_id}/check")
def publish_account_check(account_id: str):
    """连通性自检:登录态还在吗、缺哪个权限。"""
    account = publish_accounts.get_account(account_id)
    if not account:
        raise HTTPException(404, "账号不存在")
    from ..publish.registry import adapter_for

    try:
        status = adapter_for(account.platform).check_account(account)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "message": f"{type(exc).__name__}: {exc}", "hint": ""}
    return {"ok": status.ok, "message": status.message, "hint": status.hint,
            "detail": status.detail}


@router.post("/api/publish/login/{platform}")
def publish_login(platform: str):
    """打开浏览器扫码登录(小红书 / 视频号)。后台线程跑,不占住请求。"""
    urls = {"xiaohongshu": "https://creator.xiaohongshu.com/login",
            "channels": "https://channels.weixin.qq.com/login",
            "douyin": "https://creator.douyin.com/"}
    if platform not in urls:
        raise HTTPException(400, f"这个平台不用扫码登录:{platform}")

    def _run() -> None:
        try:
            publish_browser.login(platform, urls[platform])
        except Exception:  # noqa: BLE001 - 登录失败由用户看浏览器
            pass

    import threading

    threading.Thread(target=_run, name=f"login-{platform}", daemon=True).start()
    return {"ok": True, "message": "浏览器已打开,扫码登录后关掉窗口即可"}


class UnblockPayload(BaseModel):
    url: str


@router.get("/api/browser/blocked")
def browser_blocked():
    """抓取兜底撞上验证码 / 登录墙、等人工处理的站点。"""
    from ..providers import browser_fetch

    return {"items": browser_fetch.blocked_list()}


@router.post("/api/browser/unblock")
def browser_unblock(payload: UnblockPayload):
    """打开有头浏览器让人手动过验证码;关窗后登录态保存,拦截记录清掉。"""
    from ..providers import browser_fetch

    if not payload.url.startswith(("http://", "https://")):
        raise HTTPException(400, "url 要以 http:// 或 https:// 开头")

    def _run() -> None:
        try:
            browser_fetch.unblock(payload.url)
        except Exception:  # noqa: BLE001 - 失败由用户看浏览器
            pass

    import threading

    threading.Thread(target=_run, name="browser-unblock", daemon=True).start()
    return {"ok": True, "message": "浏览器已打开,手动过完验证后关掉窗口即可"}


@router.get("/api/projects/{project_id}/publish")
def project_publish_records(project_id: str):
    if not db.get_project(project_id):
        raise HTTPException(404, "项目不存在")
    return publish_service.list_for_project(project_id)


@router.get("/api/projects/{project_id}/publish/preview")
def project_publish_preview(project_id: str):
    """发布前的预览:各平台的文案、画幅、产物是否就绪。"""
    project = db.get_project(project_id)
    if not project:
        raise HTTPException(404, "项目不存在")
    ctx = publish_service.build_context(project_id)
    accounts = publish_accounts.list_accounts(only_enabled=True)
    by_platform: dict[str, dict[str, Any]] = {}
    for account in accounts:
        if account.platform in by_platform:
            continue
        meta = publish_metadata.metadata_for(account.platform, ctx)
        rule = PLATFORMS[account.platform]
        by_platform[account.platform] = {
            **meta,
            "accounts": [{"id": a.id, "name": a.name}
                         for a in accounts if a.platform == account.platform],
            "aspect": rule.aspect,
            # 平台推荐画幅与项目不一致时的提示(照样能发)
            "aspect_warning": publish_service.aspect_warning(account.platform, aspect.of(project)),
        }
    return {
        "project_id": project_id,
        "title": project["title"],
        "has_output": aspect.output_path(project_id).is_file(),
        "aspect": aspect.of(project),
        "aspect_label": aspect.label(aspect.of(project)),
        "platforms": by_platform,
        "packaging": _packaging_view(project_id),
        "published": publish_service.list_for_project(project_id),
    }


def _packaging_view(project_id: str) -> dict[str, Any] | None:
    from ..pipeline import packaging

    pkg = packaging.load(project_id)
    if pkg is None:
        return None
    return {**pkg.model_dump(),
            "covers": {v: packaging.cover_file(project_id, v) is not None for v in packaging.COVER_VARIANTS},
            "cover_3x4": {v: packaging.cover_file(project_id, v, ratio="3x4") is not None
                          for v in packaging.COVER_VARIANTS}}


class PackagingPick(BaseModel):
    """选标题(序号)和 / 或封面版本;不传的不动。"""

    title: int | None = None
    cover: str | None = None


@router.get("/api/projects/{project_id}/packaging")
def project_packaging(project_id: str):
    """发布包装:3 个标题、2 版封面、置顶评论与备用回复。还没生成返回 null。"""
    if not db.get_project(project_id):
        raise HTTPException(404, "项目不存在")
    return _packaging_view(project_id)


@router.put("/api/projects/{project_id}/packaging")
def project_packaging_pick(project_id: str, payload: PackagingPick):
    from ..pipeline import packaging

    if not db.get_project(project_id):
        raise HTTPException(404, "项目不存在")
    try:
        done = packaging.set_picked(project_id, title=payload.title, cover=payload.cover)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    if done is None:
        raise HTTPException(404, "还没有发布包装")
    return _packaging_view(project_id)


@router.post("/api/projects/{project_id}/packaging/regenerate")
def project_packaging_regenerate(project_id: str):
    """重新生成标题 / 评论 / 回复(约 ¥0.01);封面不重画,封面选择保留。"""
    from ..categories import load_profile
    from ..pipeline import packaging

    project = db.get_project(project_id)
    if not project:
        raise HTTPException(404, "项目不存在")
    if not db.read_json(project_id, "script.json", []):
        raise HTTPException(400, "还没有脚本,先跑到成片")
    profile = load_profile(project.get("category") or "auto_long")
    packaging.ensure(project_id, profile, packaging.default_llm(), force=True)
    return _packaging_view(project_id)


@router.post("/api/projects/{project_id}/publish")
def project_publish(project_id: str, payload: PublishPayload):
    """一键发布:给每个 (账号 × 画幅) 建一条发布任务。"""
    if not db.get_project(project_id):
        raise HTTPException(404, "项目不存在")
    if not payload.account_ids:
        raise HTTPException(400, "没有选平台")
    try:
        result = publish_service.enqueue(
            project_id, account_ids=payload.account_ids,
            overrides=payload.overrides, force=payload.force)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {
        "created": [publish_service.publication_view(r) for r in result["created"]],
        "skipped": [publish_service.publication_view(r) for r in result["skipped"]],
        "warnings": result["warnings"],
    }


@router.post("/api/publish/{pub_id}/retry")
def publish_retry(pub_id: str):
    """重试一条失败的任务。"""
    if not db.get_publication(pub_id):
        raise HTTPException(404, "任务不存在")
    try:
        return publish_service.retry(pub_id)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


# --------------------------------------------------------------------------
# 分发:数据看板
# --------------------------------------------------------------------------
@router.get("/api/metrics/overview")
def metrics_overview():
    return metrics_store.overview()


@router.get("/api/metrics/publications/{pub_id}")
def metrics_publication(pub_id: str):
    if not db.get_publication(pub_id):
        raise HTTPException(404, "作品不存在")
    return {"trend": metrics_store.trend(pub_id)}


@router.get("/api/metrics/platform/{platform}")
def metrics_platform(platform: str, days: int = 30):
    return {"series": metrics_store.platform_series(platform, days=days)}
