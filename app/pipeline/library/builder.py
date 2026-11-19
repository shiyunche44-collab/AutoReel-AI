"""增量构建、后台线程与池状态。"""
from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any

from ... import config
from ...providers import official
from .ingest import check_logos, ingest_gallery, ingest_source
from .model import (
    CONFIRMED_VIDEO_TARGET,
    EARLY_STOP_SAMPLE,
    ENOUGH_USABLE_SECONDS,
    LAUNCH_MAX_TAGGED,
    MAX_AUTO_SOURCES,
    MAX_TAGGED_PER_SOURCE,
    REDISCOVER_MIN_GAP,
    REDISCOVER_SECONDS,
    REDISCOVER_THIN_SECONDS,
    Item,
    Library,
    confirmed_seconds,
    pool_lock,
    profile_of,
    slug,
)
from .sources import discover
from .tagging import _same_frame, mark_official_duplicates, tag_items

log = logging.getLogger(__name__)


# --------------------------------------------------------------------------
# 构建入口
# --------------------------------------------------------------------------
def build(subject: str, *, profile: Any = None, brand: str = "", llm: Any = None,
          budget: float | None = None, max_sources: int = MAX_AUTO_SOURCES,
          errors: list[str] | None = None, log: Any = None,
          enough_seconds: float | None = None, rediscover: bool = False) -> dict[str, Any]:
    """增量构建素材池。返回统计。全程不抛异常。

    `rediscover`:人工点了「重建素材池」—— 不等 7 天 / 6 小时,马上重新找一轮源
    (距上次找源不到 `REDISCOVER_MIN_GAP` 的除外,别把平台接口点成高频请求)。

    `enough_seconds`:确认是这台车的视频够这么多秒就不再给视频段打标签。默认
    `ENOUGH_USABLE_SECONDS`(背景不挑画面);传 0 不设上限。
    """
    errors = errors if errors is not None else []
    if enough_seconds is None:
        enough_seconds = ENOUGH_USABLE_SECONDS if getattr(profile, "is_explainer", False) else 0.0
    say = log or (lambda msg: None)
    budget = config.LIBRARY_BUILD_BUDGET if budget is None else budget
    started = time.monotonic()
    deadline = started + budget if budget else 0.0

    def _late() -> bool:
        return bool(deadline) and time.monotonic() > deadline

    lib = Library.load(subject)
    aliases: list[str] = []
    tiktok_handles: tuple[str, ...] = ()
    spec: dict[str, tuple[str, ...]] = {}
    if profile is not None:
        lib.category = profile.name
        key, spec = official.detect_brand(subject, profile.official_sites, hint=brand)
        aliases = official.brand_aliases(key, spec, brand) if (key or brand) else []
        tiktok_handles = tuple(spec.get("tiktok", ()))

    try:
        age = time.time() - lib.discovered_at
        thin = confirmed_seconds(lib, video_only=True) < CONFIRMED_VIDEO_TARGET
        if (age > REDISCOVER_SECONDS or (thin and age > REDISCOVER_THIN_SECONDS)
                or (rediscover and age > REDISCOVER_MIN_GAP)):
            n = discover(lib, aliases, errors, max_sources=max_sources,
                         tiktok_handles=tiktok_handles, spec=spec)
            say(f"发现新源 {n} 个")
            lib.save()
        if any(g not in lib.galleries_done for g in profile_of(lib).galleries):
            n = ingest_gallery(lib, errors, spec=spec)
            say(f"静图 {n} 张")
            lib.save()
        # 手动加的源先下;同类里官方号优先
        pending = sorted((s for s in lib.sources if s.status == "pending"),
                         key=lambda s: (s.added_by != "manual", not s.official))
        for src in pending:
            if _late():
                break
            n = ingest_source(lib, src, errors)
            say(f"源 {src.title[:30] or src.url}:{n} 段")
            lib.save()
        unchecked = [s for s in lib.sources if s.status == "done" and not s.logo_checked]
        for src in unchecked:
            if _late():
                break
            check_logos(lib, src)
        if mark_official_duplicates(lib) or unchecked:
            lib.save()
        todo = _tag_queue(lib)
        if todo and not _late():
            n = tag_items(lib, todo, llm, errors, deadline=deadline,
                          enough_seconds=enough_seconds)
            say(f"打标签 {n} 个")
    except Exception as exc:  # noqa: BLE001 - 素材池是补充,失败不阻断
        errors.append(f"素材池构建异常:{type(exc).__name__}: {str(exc)[:200]}")
    finally:
        lib.save()
    stats = lib.stats()
    stats["elapsed"] = round(time.monotonic() - started, 1)
    enough = bool(enough_seconds) and confirmed_seconds(lib, video_only=True) >= enough_seconds
    stats["finished"] = not _late() and not stats["sources_pending"] and (
        enough or not _tag_queue(lib))
    return stats


#: 建池的后台任务类型(见 `app/jobs.py`)。去重键是车型 slug,同一台车同一时刻只建一份。
BUILD_JOB = "library.build"

#: 能放进任务载荷的构建参数(其余参数是对象,不能落库)
_BUILD_OPTS = ("enough_seconds", "max_sources", "rediscover", "budget", "brand")


def build_async(subject: str, *, profile: Any = None, category: str = "",
                project_id: str = "", reserved: float = 0.0, vlm_before: int = 0,
                **opts: Any) -> bool:
    """排一个后台建池任务。同一车型已经在排 / 在建就不重复排。返回是否排上了。

    分镜阶段调用:用户审脚本、分镜的这几分钟正好拿来攒素材,
    门禁 A 确认时取材直接用池子里已有的,不在确认请求里等下载。

    原来是起一次性线程:服务一重启线程就没了,池子永远停在「待打标签」,预留的额度
    也没人结算。现在是持久化任务,重启后接着建、建完结算。

    `project_id` / `reserved` / `vlm_before`:建池前给项目预留的额度,建完按实际视觉
    调用数结算(`costs.commit_project`)。`llm` 之类的对象参数不再接收:任务里现取。
    """
    from ... import jobs

    unknown = set(opts) - set(_BUILD_OPTS) - {"llm"}
    if unknown:
        raise TypeError(f"build_async 不认识的参数:{sorted(unknown)}")
    if not category and profile is not None:
        category = str(getattr(profile, "name", "") or "")
    payload = {"subject": subject, "category": category, "project_id": project_id,
               "reserved": float(reserved), "vlm_before": int(vlm_before),
               "opts": {k: v for k, v in opts.items() if k in _BUILD_OPTS}}
    return jobs.submit(BUILD_JOB, key=slug(subject), payload=payload) is not None


def build_many_async(subjects: list[str], **kwargs: Any) -> list[str]:
    """多台车的素材池:每台车一个任务,按资源类 `library` 的并发上限排队跑
    (VLM 打标签有限流,并发多了只会排队)。已经在建的车跳过。返回这次排上的车名。"""
    return [name for name in subjects if build_async(name, **kwargs)]


def _build_job(payload: dict[str, Any]) -> dict[str, Any]:
    """建池任务的 handler。`build` 全程不抛异常;额度预留无论成败都要结算。"""
    from ... import costs
    from ...categories import load_profile
    from .. import orchestrator

    subject = str(payload["subject"])
    category = str(payload.get("category") or "")
    project_id = str(payload.get("project_id") or "")
    reserved = float(payload.get("reserved") or 0.0)
    stats: dict[str, Any] = {}
    try:
        from ...llm import ledger

        profile = load_profile(category) if category else None
        # 池锁:重拉高清 / 回填也改这个池子的条目,和建池按车型互斥(见 model.pool_lock)
        with pool_lock(subject), ledger.tag(project_id=project_id):  # 看图调用记到项目名下
            stats = build(subject, profile=profile, llm=orchestrator._llm(),
                          **dict(payload.get("opts") or {}))
        return stats
    finally:
        if project_id:
            delta = max(0, int(stats.get("vlm_calls") or 0) - int(payload.get("vlm_before") or 0))
            try:
                costs.commit_project(project_id, reserved, costs.estimate_library(delta))
            except Exception:  # noqa: BLE001 - 记账失败不该影响建池
                log.warning("素材池记账失败(%s)", project_id, exc_info=True)


def _register_jobs() -> None:
    from ... import jobs

    # max_attempts=1:build 本身不抛异常,抛了就是配置问题(品类不存在之类),重试无益;
    # 而且额度已在 finally 里结算,重跑会重复结算。进程被杀导致的中断不算尝试,照样接着跑。
    jobs.register(BUILD_JOB, resource="library", max_attempts=1, lease=180.0)(_build_job)
    # 重拉高清 / 回填(library.upgrade / library.backfill)
    from .upgrade import register_jobs

    register_jobs()


_register_jobs()


def is_building(subject: str) -> bool:
    """这台车的素材池是否在排队 / 在建(查任务表,跨进程、跨重启都准)。"""
    from ... import jobs

    return jobs.active(BUILD_JOB, slug(subject))


def pool_status(subject: str) -> str:
    """素材池的状态:
    - ready(有可用素材)
    - building(还在建)
    - tagging(素材下载完了,正在打标签 —— 还没认出哪些能用)
    - empty(建完了但确实没有可用素材)
    - none(压根没有这个池子 —— 品类不用素材池,或采集完全失败)

    渲某台车的拍之前先看这个。素材池采集与逐拍渲染是并行的:池子还在建时
    就把这一拍渲掉,渲出来就是一张**没有这台车画面**的卡片 —— 观众看到的是
    「这车一个镜头都没有」。所以 building / tagging 要等,empty 要留痕。

    **tagging 必须单独认出来**:素材下完了但还没打标签时,一条都还「不可用」,
    这时候看是 empty 就等于「建完了没素材」,于是这一拍被立刻渲成卡片 ——
    等池子再过几分钟打好标签也来不及了(实跑踩过:27 拍全成了卡片)。
    """
    if is_building(subject):
        return "building"
    if not Library.dir_for(subject).is_dir():
        return "none"
    lib = Library.load(subject)
    for i in lib.items:
        if i.usable and Path(i.path).is_file():
            return "ready"
    # 还没打标签的条目 = 池子还在干活,不是「没有素材」
    for i in lib.items:
        if not i.tagged and not i.skipped and i.frame and Path(i.frame).is_file():
            return "tagging"
    return "empty"


def _tag_queue(lib: Library) -> list[Item]:
    """待打标签的:图全打;视频每个源最多 MAX_TAGGED_PER_SOURCE 段,长段优先
    (长段更可能是完整的展示镜头,而不是快剪)。

    视频先排每个源的前 EARLY_STOP_SAMPLE 段(样本),再排各源剩下的 ——
    样本打完就能判出整源都是口播的源,剩下的不用再花钱。
    跳过的段、和同源里已打标签 / 已在队列的段同一画面的段不进队列。
    """
    out: list[Item] = [i for i in lib.items if i.kind == "still" and not i.tagged and i.frame]
    by_src: dict[str, list[Item]] = {}
    for i in lib.items:
        # 重拉高清换下来的老段不占名额、不参与去重(新段同一画面的会直接继承它的标签)
        if i.kind == "video" and i.frame and i.skipped != "replaced":
            by_src.setdefault(i.source, []).append(i)
    samples: list[Item] = []
    rest: list[Item] = []
    # 官方源先打:够用(按确认过的秒数算)就停,第三方的段可能根本不用看
    for segs in sorted(by_src.values(), key=lambda segs: not segs[0].official):
        tagged = sum(1 for i in segs if i.tagged)
        room = (LAUNCH_MAX_TAGGED if segs[0].launch else MAX_TAGGED_PER_SOURCE) - tagged
        if room <= 0:
            continue
        kept = [i for i in segs if i.tagged]
        fresh: list[Item] = []
        for i in sorted((i for i in segs if not i.tagged and not i.skipped),
                        key=lambda i: -i.length):
            if len(fresh) >= room:
                break
            if any(_same_frame(i, k) for k in kept):
                continue
            kept.append(i)
            fresh.append(i)
        head = max(0, EARLY_STOP_SAMPLE - tagged)
        samples += fresh[:head]
        rest += fresh[head:]
    return out + samples + rest
