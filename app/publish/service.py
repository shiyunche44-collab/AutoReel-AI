"""发布编排:建任务、状态机、失败重试、后台执行。

**为什么发布要落库**:上传 100MB 可能要几分钟,HTTP 请求不可能等它。所以
「发布」在这里不是一次函数调用,而是一条**持久化的任务**:

```
pending ──► uploading ──► submitted ──► published
   │            │             │
   └────────────┴─────────────┴──────► failed ──(可重试且未超次数)──► pending
```

调度器每轮从库里领几条待办,推进一步就落一次库。好处有三个:
- **幂等**:唯一索引 `(项目, 账号, 画幅)` 保证同一条视频不会发两次;
- **断点续跑**:进程重启后任务还在库里,接着传;
- **多平台不回滚**:B站成了、抖音挂了,B站还是 `published`,抖音单独重试。

执行放在**独立 daemon 线程**里,不和 FastAPI 的事件循环抢时间 ——
上传几��钟绝不能把 Web 卡住。
"""
from __future__ import annotations

import json
import random
import threading
import time
from pathlib import Path
from typing import Any

from .. import config, db
from . import accounts as accounts_mod
from . import metadata as md
from .base import Account, PlatformRef, PublishError, PublishRequest, PublishResult
from .registry import LANDSCAPE, PORTRAIT, adapter_for, rule_for

#: 单条发布最多尝试几次(含首次)
MAX_ATTEMPTS = 3
#: 重试退避基数(秒):1 → 4 → 16
BACKOFF_BASE = 1.0
#: 后台轮询间隔(秒)
TICK_SECONDS = 20

#: 状态取值
PENDING = "pending"
UPLOADING = "uploading"
SUBMITTED = "submitted"
PUBLISHED = "published"
FAILED = "failed"

#: 账号上次发稿时间(内存态),用于"同账号两次发布之间留间隔"
_last_publish_at: dict[str, float] = {}
_lock = threading.Lock()


# --------------------------------------------------------------------------
# 产物
# --------------------------------------------------------------------------
def artifact_name(aspect: str) -> str:
    return "output_vertical.mp4" if aspect == PORTRAIT else "output.mp4"


def cover_name(aspect: str) -> str:
    return "cover_vertical.jpg" if aspect == PORTRAIT else "cover.jpg"


def artifact_path(project_id: str, aspect: str) -> Path:
    return config.project_dir(project_id) / artifact_name(aspect)


def cover_path(project_id: str, aspect: str) -> Path:
    return config.project_dir(project_id) / cover_name(aspect)


# --------------------------------------------------------------------------
# 文案
# --------------------------------------------------------------------------
def build_context(project_id: str) -> md.ContentContext:
    """把项目已有产物拼成文案上下文(不重新跑 LLM)。"""
    row = db.get_project(project_id) or {}
    segments = db.read_json(project_id, "script.json", []) or []
    facts = db.read_json(project_id, "facts.json", []) or []
    render = db.read_json(project_id, "render.json", {}) or {}
    disclaimers: list[str] = []
    subject_label = "产品"
    try:
        from ..categories import load_profile

        profile = load_profile(row.get("category") or "auto_long")
        disclaimers = list(profile.required_disclaimers)
        subject_label = profile.vocab.subject_label
    except Exception:  # noqa: BLE001 - 品类配置坏了不该拦住发布
        pass
    topics: list[str] = []
    for fact in facts:
        topic = str((fact or {}).get("topic") or "").strip()
        if topic and topic not in topics:
            topics.append(topic)
    return md.ContentContext(
        subject=str(row.get("subject") or ""),
        title=str(row.get("title") or row.get("subject") or ""),
        thesis=str(row.get("thesis") or ""),
        segments=[str(s.get("text") or "") for s in segments if isinstance(s, dict)],
        topics=topics,
        disclaimers=disclaimers,
        source_note=str(render.get("source_note") or ""),
        category=str(row.get("category") or ""),
        subject_label=subject_label,
    )


def forbidden_hits(text: str, category: str, theme: str = "") -> list[str]:
    """文案里的违禁词。命中就**不发布** —— 投放被拒是白忙一场。

    复用品类 YAML 的禁用词表(广告法绝对化用语 + 品类专属)与方向的附加禁用词。
    """
    try:
        from ..categories import load_profile

        profile = load_profile(category or "auto_long")
    except Exception:  # noqa: BLE001
        return []
    terms = set(profile.forbidden_terms)
    try:
        spec = profile.theme_spec(theme or None)
        terms.update(spec.extra_forbidden_terms)
    except Exception:  # noqa: BLE001
        pass
    return sorted(t for t in terms if t and t in text)


# --------------------------------------------------------------------------
# 建任务
# --------------------------------------------------------------------------
def enqueue(project_id: str, *, account_ids: list[str], aspects: list[str] | None = None,
            overrides: dict[str, dict[str, Any]] | None = None,
            force: bool = False) -> dict[str, list[dict[str, Any]]]:
    """为 (账号 × 画幅) 建发布任务。已发过的默认跳过(幂等)。

    `aspects` 为空时**按平台推荐自动选**:B站 / YouTube 走 16:9,
    抖音 / 小红书 / 视频号走 9:16 —— 竖屏平台发横屏会被重排裁切。
    """
    project = db.get_project(project_id)
    if not project:
        raise ValueError("项目不存在")
    if not account_ids:
        return {"created": [], "skipped": []}
    overrides = overrides or {}
    ctx = build_context(project_id)
    created: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []

    for account_id in account_ids:
        account = accounts_mod.get_account(account_id)
        if not account or not account.enabled:
            continue
        rule = rule_for(account.platform)
        wanted = aspects or [rule.aspect]
        for aspect in wanted:
            existing = db.find_publication(project_id, account_id, aspect)
            if existing and not force:
                skipped.append(existing)
                continue
            meta = md.metadata_for(account.platform, ctx)
            over = overrides.get(account.platform) or {}
            title = str(over.get("title") or meta["title"])
            description = str(over.get("description") or meta["description"])
            tags = [str(t) for t in (over.get("tags") or meta["tags"])]
            rule = rule_for(account.platform)
            title = md.clamp_title(title, rule.title_max, rule.title_soft)
            description = md.clamp_title(description, rule.desc_max) if len(description) > rule.desc_max \
                else description

            if existing and force:
                db.update_publication(existing["id"], state=PENDING, error="", attempts=0,
                                      title=title, description=description,
                                      tags=json.dumps(tags, ensure_ascii=False),
                                      remote_id="", remote_url="", upload_id="",
                                      published_at=None)
                row = db.get_publication(existing["id"])
            else:
                from ..models import new_id

                pub_id = new_id("pub_")
                now = time.time()
                db.create_publication({
                    "id": pub_id, "project_id": project_id, "account_id": account_id,
                    "platform": account.platform, "aspect": aspect,
                    "artifact": artifact_name(aspect), "title": title,
                    "description": description, "tags": json.dumps(tags, ensure_ascii=False),
                    "state": PENDING, "created_at": now, "updated_at": now,
                })
                row = db.get_publication(pub_id)
            assert row is not None
            db.add_event(project_id, f"publish:{account.platform}", src=str(project.get("state")),
                         reason=f"排队发布到{rule.label}({aspect})")
            created.append(row)

    return {"created": created, "skipped": skipped}


# --------------------------------------------------------------------------
# 推进
# --------------------------------------------------------------------------
def _tags_of(row: dict[str, Any]) -> list[str]:
    try:
        data = json.loads(row.get("tags") or "[]")
        return [str(t) for t in data] if isinstance(data, list) else []
    except (json.JSONDecodeError, TypeError):
        return []


def _request_for(row: dict[str, Any], account: Account) -> PublishRequest:
    project_id = row["project_id"]
    aspect = row.get("aspect") or LANDSCAPE
    extra = dict(account.meta)
    # 分区 / 频道等平台特定设置从账号 meta 继承
    for key in ("tid", "category_id", "privacy_status", "playlist_id"):
        if key in account.meta:
            extra[key] = account.meta[key]
    pub_id = row["id"]

    def _save_session(session: str) -> None:
        """上传会话落库 —— 进程重启后能接着传,不用从头再来。"""
        try:
            db.update_publication(pub_id, upload_id=session[:4000])
        except Exception:  # noqa: BLE001 - 存不下也不该让发布失败
            pass

    return PublishRequest(
        project_id=project_id,
        account_id=account.id,
        platform=row["platform"],
        aspect=aspect,
        video_path=artifact_path(project_id, aspect),
        cover_path=cover_path(project_id, aspect) if cover_path(project_id, aspect).is_file() else None,
        title=row.get("title") or "",
        description=row.get("description") or "",
        tags=_tags_of(row),
        extra=extra,
        upload_id=row.get("upload_id") or "",
        on_session=_save_session,
    )


def _ensure_artifact(row: dict[str, Any]) -> None:
    """竖版产物可能还没生成。缺就先渲染一版(在后台线程里跑,不阻塞 HTTP)。"""
    path = artifact_path(row["project_id"], row.get("aspect") or LANDSCAPE)
    if path.is_file():
        return
    if (row.get("aspect") or LANDSCAPE) != PORTRAIT:
        raise PublishError(f"成片不存在:{path.name}", retryable=False)
    from ..pipeline import vertical

    vertical.build(row["project_id"])


def attempt(row: dict[str, Any]) -> PublishResult:
    """跑一次发布。失败抛 `PublishError`。"""
    account = accounts_mod.get_account(row["account_id"])
    if not account:
        raise PublishError("账号不存在", retryable=False)
    if not account.enabled:
        raise PublishError("账号已停用", retryable=False)
    _ensure_artifact(row)

    req = _request_for(row, account)
    adapter = adapter_for(row["platform"])
    result = adapter.publish(req, account)
    if not result.ok:
        raise PublishError(result.message or "发布失败", retryable=False)
    return result


def advance(pub_id: str) -> dict[str, Any]:
    """推进一条任务一步。返回本次动作描述。"""
    row = db.get_publication(pub_id)
    if not row:
        return {"ok": False, "message": "任务不存在"}
    if row["state"] in (PUBLISHED, SUBMITTED):
        return {"ok": True, "state": row["state"], "skipped": "已完成"}
    account = accounts_mod.get_account(row["account_id"])
    if not account:
        db.update_publication(pub_id, state=FAILED, error="账号不存在")
        return {"ok": False, "state": FAILED, "message": "账号不存在"}

    # 文案违禁词:投放被拒等于白忙,这里直接拦住
    project = db.get_project(row["project_id"]) or {}
    hits = forbidden_hits(f"{row.get('title') or ''} {row.get('description') or ''}",
                          project.get("category", ""), project.get("theme", ""))
    if hits:
        db.update_publication(pub_id, state=FAILED,
                              error=f"文案含禁用词:{'、'.join(hits)}")
        return {"ok": False, "state": FAILED, "message": f"文案含禁用词:{'、'.join(hits)}"}

    db.update_publication(pub_id, state=UPLOADING)
    try:
        result = attempt(row)
    except PublishError as exc:
        attempts = (row.get("attempts") or 0) + 1
        if not exc.retryable or attempts >= MAX_ATTEMPTS:
            # 彻底失败:上传会话也没用了,清掉,下次从头传
            db.update_publication(pub_id, state=FAILED, error=exc.message, attempts=attempts,
                                  upload_id="")
            return {"ok": False, "state": FAILED, "message": exc.message}
        db.update_publication(pub_id, state=PENDING, error=exc.message, attempts=attempts)
        return {"ok": False, "state": PENDING, "message": exc.message, "retryable": True}
    except Exception as exc:  # noqa: BLE001 - 适配器可能抛任何东西
        attempts = (row.get("attempts") or 0) + 1
        db.update_publication(pub_id, state=FAILED, error=f"{type(exc).__name__}: {exc}",
                              attempts=attempts, upload_id="")
        return {"ok": False, "state": FAILED, "message": str(exc)}

    now = time.time()
    db.update_publication(
        pub_id,
        state=result.state or PUBLISHED,
        remote_id=result.remote_id or "",
        remote_url=result.remote_url or "",
        error="" if result.ok else (result.message or ""),
        attempts=(row.get("attempts") or 0) + 1,
        published_at=now if result.state == PUBLISHED else None,
        upload_id="",
    )
    db.add_event(row["project_id"], f"publish:{row['platform']}", src=PUBLISHED,
                 reason=f"发布成功:{result.remote_url or result.remote_id}")
    with _lock:
        _last_publish_at[row["account_id"]] = now
    return {"ok": True, "state": result.state, "remote_url": result.remote_url,
            "remote_id": result.remote_id, "message": result.message}


def retry(pub_id: str) -> dict[str, Any]:
    """手动重试一条失败的任务。"""
    row = db.get_publication(pub_id)
    if not row:
        raise ValueError("任务不存在")
    db.update_publication(pub_id, state=PENDING, error="", attempts=0)
    return advance(pub_id)


# --------------------------------------------------------------------------
# 后台执行
# --------------------------------------------------------------------------
def _wait_for_account_slot(account_id: str) -> bool:
    """同账号两次发布之间留 30–90 秒,避免被判机器批量。

    返回 False 表示"这一轮先别发"(下次调度再来),而不是占着 worker 线程干等。
    """
    with _lock:
        last = _last_publish_at.get(account_id)
    if not last:
        return True
    wait = random.uniform(config.PUBLISH_MIN_INTERVAL, config.PUBLISH_MAX_INTERVAL)
    return (time.time() - last) >= wait


def _process(row: dict[str, Any]) -> None:
    """单条任务的完整执行:最多试 MAX_ATTEMPTS 次,中间指数退避。"""
    pub_id = row["id"]
    for attempt_no in range(MAX_ATTEMPTS):
        current = db.get_publication(pub_id)
        if not current or current["state"] in (PUBLISHED, SUBMITTED, FAILED):
            return
        result = advance(pub_id)
        if result.get("ok") or not result.get("retryable"):
            return
        if attempt_no < MAX_ATTEMPTS - 1:
            time.sleep(BACKOFF_BASE * (4 ** attempt_no))


def _sweep() -> int:
    """领一批任务丢进线程池。返回处理条数。"""
    if not config.PUBLISH_ENABLED:
        return 0
    rows = db.claim_publications(limit=config.PUBLISH_CONCURRENCY)
    if not rows:
        return 0
    pool = _get_pool()
    count = 0
    for row in rows:
        if not _wait_for_account_slot(row["account_id"]):
            continue
        pool.submit(_process, row)
        count += 1
    return count


_pool: Any = None
_pool_lock = threading.Lock()


def _get_pool():
    global _pool
    with _pool_lock:
        if _pool is None:
            from concurrent.futures import ThreadPoolExecutor

            _pool = ThreadPoolExecutor(max_workers=max(1, config.PUBLISH_CONCURRENCY),
                                       thread_name_prefix="publish")
        return _pool


def start() -> None:
    """启动后台发布线程(守护线程,进程退出即结束)。"""
    global _thread
    with _thread_lock:
        if _thread and _thread.is_alive():
            return
        _stop.clear()
        _thread = threading.Thread(target=_loop, name="publish-worker", daemon=True)
        _thread.start()


def stop() -> None:
    _stop.set()


_thread: threading.Thread | None = None
_thread_lock = threading.Lock()
_stop = threading.Event()


def _loop() -> None:
    while not _stop.is_set():
        try:
            _sweep()
        except Exception:  # noqa: BLE001 - 后台线程不能因单次异常而退出
            pass
        _stop.wait(TICK_SECONDS)


# --------------------------------------------------------------------------
# 查询
# --------------------------------------------------------------------------
def publication_view(row: dict[str, Any]) -> dict[str, Any]:
    """给 API 的发布记录视图。"""
    account = accounts_mod.get_account(row["account_id"])
    rule = rule_for(row["platform"])
    return {
        "id": row["id"],
        "project_id": row["project_id"],
        "account_id": row["account_id"],
        "account_name": account.name if account else "(已删除)",
        "platform": row["platform"],
        "platform_label": rule.label,
        "aspect": row.get("aspect") or LANDSCAPE,
        "title": row.get("title") or "",
        "description": row.get("description") or "",
        "tags": _tags_of(row),
        "state": row["state"],
        "remote_id": row.get("remote_id") or "",
        "remote_url": row.get("remote_url") or "",
        "error": row.get("error") or "",
        "attempts": row.get("attempts") or 0,
        "published_at": row.get("published_at"),
        "created_at": row.get("created_at"),
        "updated_at": row.get("updated_at"),
    }


def list_for_project(project_id: str) -> list[dict[str, Any]]:
    return [publication_view(r) for r in db.list_publications(project_id=project_id)]
