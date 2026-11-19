"""重拉高清与回填:老池子的低清段换成 1080p;老段补测动感、补打景别。

重拉(任务 `library.upgrade`)
-----------------------------
B 站不登录只给 480p,老池子里低清段占大头。cookies 配好之后,对低清段占多数的源:

1. 探一下平台现在肯给多高(`probe`,只解析不下载)。B 站还是 ≤ 480p → cookies 没生效,
   B 站的源整批停下(拉了也是 480p);确实只有低清的源记下原因,`RETRY_AFTER` 内不再试;
2. 按原来的窗重新下载(`raw/{源 id}_h{k}.*`,中断后已下好的文件直接复用),
   切段、本地体检、测动感(`ingest.split_window`,段 id `{源 id}_h{k}_{序号}`);
3. 新段进池:和老段同一画面的直接继承标签(`inherit_duplicates`,不花视觉调用),
   其余送视觉模型;`replaces` 记它顶替的老段(时间重叠最多的那段);
4. 某个窗的新段打上了标签,这个窗的老段才退役:标成 `skipped="replaced"`。
   条目和文件都不删(老项目引用的 id、路径照样查得到),只是不再被取材选中。

幂等:按源、按窗做,已经有新段的窗不重下;只差打标签 / 退役的源下次接着做。
限流:每次请求前 `cooldown.check`,撞了 412 / -352 / 429 就 `cooldown.trip` 并停下这个平台;
窗与窗、源与源之间歇一下,一条一条来。

回填(任务 `library.backfill`)
------------------------------
- 动感(`motion`):不调模型,总是做;
- 景别 / 大镜头(`shot` / `hero`):要花视觉调用,只在显式要求时做(`--shots`),只补可用视频段。

命令行:`python -m app.pipeline.library upgrade <车型>|--all`、`backfill <车型>|--all [--shots]`。
"""
from __future__ import annotations

import json
import logging
import re
import time
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any

from ... import config
from ...providers import platform_video
from . import footage_health, motion
from .ingest import download_windows, split_window
from .model import (
    LAUNCH_MAX_TAGGED,
    LAUNCH_WINDOWS,
    LIB_ROOT_NAME,
    MAX_TAGGED_PER_SOURCE,
    TAG_BATCH,
    WINDOWS_PER_LONG_SOURCE,
    Item,
    Library,
    Source,
    _has_host,
    platform_of,
    pool_lock,
    slug,
)
from .tagging import inherit_duplicates, mark_official_duplicates, needs_shot, tag_items, tag_shots

log = logging.getLogger(__name__)

UPGRADE_JOB = "library.upgrade"
BACKFILL_JOB = "library.backfill"
#: `--all` 任务的去重键
ALL_KEY = "__all__"

#: 重拉哪些平台的源(yt-dlp 能按格式挑清晰度、cookies 管用的)
UPGRADE_PLATFORMS = ("bilibili", "youtube")
#: 源里(读到尺寸的)老段低清占比超过它才重拉
LOW_RES_MAJORITY = 0.5
#: 平台肯给的短边不到它就不拉了(拉下来还是低清)
MIN_UPGRADE_HEIGHT = 720
#: 平台确实只有低清的源,隔这么久再试(秒)
RETRY_AFTER = 7 * 86400.0
#: 两个窗之间、两个源之间歇多久(秒)。B 站限流严,宁慢勿快
WINDOW_GAP = 8.0
SOURCE_GAP = 20.0
#: 被限流后冷却多久(秒;连着被拦 cooldown 自己翻倍)
UPGRADE_COOLDOWN = 1800.0
#: 新段 id 里窗号前的标记
HD_TAG = "h"
#: 老段身上"整源"的结论,新段照抄(角标裁不掉、烧录字幕是源的问题,不是清晰度的问题)
SOURCE_SKIPS = ("logo_uncroppable", "burned_text")
#: 新段和老段时间重叠至少这么多秒,才算"顶替"
MIN_OVERLAP = 0.3
#: 下载出来的文件小于这个算空壳(同 platform_video._MIN_SEGMENT_BYTES)
_MIN_RAW_BYTES = 20_000


class Stop(Exception):
    """这个平台本次不能再请求了(冷却中 / cookies 没生效)。"""

    def __init__(self, platform: str, reason: str, detail: str) -> None:
        super().__init__(detail)
        self.platform, self.reason, self.detail = platform, reason, detail


def _pause(seconds: float) -> None:
    if seconds > 0:
        time.sleep(seconds)


# --------------------------------------------------------------------------
# 哪些源要重拉
# --------------------------------------------------------------------------
def _hd_prefix(src: Source) -> str:
    return f"{src.id}_{HD_TAG}"


def window_of(item_id: str, src: Source) -> int | None:
    """段 id → 窗号(老段 `{源}_{k}_{n}`、新段 `{源}_h{k}_{n}` 都认)。认不出 → None。"""
    m = re.match(re.escape(src.id) + r"_(?:" + HD_TAG + r")?(\d+)_\d+$", item_id)
    return int(m.group(1)) if m else None


def old_items(lib: Library, src: Source) -> list[Item]:
    """这个源还没退役的老视频段(id 认得出窗号的;别的命名不归重拉管)。"""
    prefix = _hd_prefix(src)
    return [i for i in lib.items if i.source == src.id and i.kind == "video"
            and not i.id.startswith(prefix) and i.skipped != "replaced"
            and window_of(i.id, src) is not None]


def hd_items(lib: Library, src: Source) -> list[Item]:
    prefix = _hd_prefix(src)
    return [i for i in lib.items if i.source == src.id and i.id.startswith(prefix)]


def low_res_ratio(lib: Library, src: Source) -> float:
    sized = [i for i in old_items(lib, src) if i.width and i.height]
    return sum(1 for i in sized if i.low_res) / len(sized) if sized else 0.0


def promising(lib: Library, src: Source) -> int:
    """老段里"除了清晰度都合格"的段数:低清段打了标签也算不上可用(`usable` 不收低清),
    所以按去掉尺寸后的结论估。被整源早停跳过的不算数(那多半就是因为低清)。"""
    n = 0
    for i in old_items(lib, src):
        if not i.tagged:
            continue
        skipped = "" if i.skipped == "source_rejected" else i.skipped
        if replace(i, width=0, height=0, skipped=skipped).usable and not _has_host(i):
            n += 1
    return n


def needs_upgrade(lib: Library, src: Source, *, now: float | None = None) -> bool:
    """低清段占多数、值得重拉的源。已经重拉过(老段退役了)的自然不再算。"""
    now = time.time() if now is None else now
    if src.status != "done" or src.review_status == "rejected":
        return False
    if (src.platform or platform_of(src.url)) not in UPGRADE_PLATFORMS:
        return False
    if 0 < src.upgraded_height < MIN_UPGRADE_HEIGHT and now - src.upgraded_at < RETRY_AFTER:
        return False  # 平台确实只有低清,过一阵再试
    return low_res_ratio(lib, src) > LOW_RES_MAJORITY and promising(lib, src) > 0


def candidates(lib: Library) -> list[Source]:
    """要重拉的源:官方号在前,"除了清晰度都合格"的段多的在前。"""
    picked = [s for s in lib.sources if needs_upgrade(lib, s)]
    return sorted(picked, key=lambda s: (not s.official, -promising(lib, s)))


def all_subjects() -> list[str]:
    """data/library 下的全部池子(车型名取池文件里记的原名)。"""
    root = config.DATA_DIR / LIB_ROOT_NAME
    out: list[str] = []
    if not root.is_dir():
        return out
    for d in sorted(root.iterdir()):
        path = d / "library.json"
        if not d.is_dir() or not path.is_file():
            continue
        try:
            name = str(json.loads(path.read_text(encoding="utf-8")).get("subject") or "")
        except (OSError, ValueError):
            name = ""
        out.append(name or d.name)
    return out


# --------------------------------------------------------------------------
# 重拉一个源
# --------------------------------------------------------------------------
def _guard(platform: str) -> None:
    from ...providers import cooldown

    try:
        cooldown.check(footage_health.cooldown_key(platform))
    except cooldown.Cooling as exc:
        raise Stop(platform, "cooldown", str(exc)) from exc


def _blocked(platform: str, exc: Exception) -> Stop | None:
    """撞了限流 → 登记冷却,返回要抛的 Stop;别的错误 → None。"""
    from ...providers import cooldown

    if not platform_video.is_rate_limited(exc):
        return None
    cooldown.trip(footage_health.cooldown_key(platform), UPGRADE_COOLDOWN, str(exc)[:200])
    return Stop(platform, "cooldown", f"{platform} 限流,冷却 {UPGRADE_COOLDOWN / 60:.0f} 分钟:"
                                      f"{str(exc)[:120]}")


def _existing_raw(raw_dir: Path, stem: str) -> Path | None:
    """上次已经下好的窗文件(中断后复用,不重下)。"""
    files = sorted((p for p in raw_dir.glob(stem + ".*")
                    if p.suffix not in (".part", ".ytdl", ".tmp")
                    and p.stat().st_size >= _MIN_RAW_BYTES),
                   key=lambda p: p.stat().st_size, reverse=True)
    return files[0] if files else None


def _windows(src: Source) -> list[tuple[float, float]]:
    return download_windows(src.duration,
                            LAUNCH_WINDOWS if src.kind == "launch" else WINDOWS_PER_LONG_SOURCE)


def _pending_windows(lib: Library, src: Source) -> list[int]:
    """还要重下的窗:有老段、还没有新段的。"""
    have = {window_of(i.id, src) for i in hd_items(lib, src)}
    olds = {window_of(i.id, src) for i in old_items(lib, src)}
    return sorted(k for k in olds - have if k is not None)


def _probe_height(src: Source, platform: str) -> int:
    """平台肯给的最高短边。B 站 ≤ 480p 而 cookies 没确认生效过 → Stop(cookies)。"""
    prev = footage_health.load_check().get("effective")
    try:
        info = platform_video.probe(src.url)
    except platform_video.PlatformError as exc:
        stop = _blocked(platform, exc)
        if stop is not None:
            raise stop from exc
        raise
    height = platform_video.best_short_side(info)
    guest = 0 < height <= footage_health.GUEST_MAX_HEIGHT
    if platform == "bilibili" and guest and prev is not True:
        footage_health.record_probe(platform, src.url, height)
        from ... import degrade

        detail = f"B 站只给 {height}p:cookies 未配置或未生效,B 站的源这次不重拉"
        degrade.note("library.upgrade_no_cookies", detail)
        raise Stop(platform, "cookies", detail)
    if not guest:
        footage_health.record_probe(platform, src.url, height)
    return height


def _download(src: Source, raw_dir: Path, frame_dir: Path, ks: list[int], platform: str,
              errors: list[str]) -> tuple[list[tuple[int, list[Item]]], list[int], Stop | None]:
    """按窗下载 + 切段(不碰池子)。返回 (各窗新段, 没拉成的窗, 中途被限流的 Stop)。"""
    windows = _windows(src)
    got: list[tuple[int, list[Item]]] = []
    failed: list[int] = []
    for n, k in enumerate(ks):
        if k >= len(windows):
            failed.append(k)
            continue
        stem = f"{src.id}_{HD_TAG}{k}"
        raw = _existing_raw(raw_dir, stem)
        if raw is None:
            if n:
                _pause(WINDOW_GAP)
            try:
                _guard(platform)
                start, length = windows[k]
                raw = platform_video.download_segment(src.url, raw_dir / f"{stem}.mp4",
                                                      start=start, seconds=length,
                                                      timeout_hint=600)
            except Stop as halt:
                return got, failed, halt
            except platform_video.PlatformError as exc:
                stop = _blocked(platform, exc)
                if stop is not None:
                    return got, failed, stop
                errors.append(f"重拉高清下载失败({src.url} 窗 {k}):{str(exc)[:160]}")
                failed.append(k)
                continue
        segs = split_window(src, raw, k, frame_dir, tag=HD_TAG)
        sized = [i for i in segs if i.width and i.height]
        if not segs or (sized and all(i.low_res for i in sized)):
            errors.append(f"重拉高清没拿到高清({src.url} 窗 {k})")
            failed.append(k)
            continue
        got.append((k, segs))
    return got, failed, None


def _overlap(a: Item, b: Item) -> float:
    return max(0.0, min(a.end, b.end) - max(a.start, b.start))


def _add_new(lib: Library, src: Source, batches: list[tuple[int, list[Item]]]) -> list[Item]:
    """新段进池(已在池里的 id 跳过),记下顶替的老段、照抄整源的角标结论。"""
    ids = {i.id for i in lib.items}
    olds = old_items(lib, src)
    added: list[Item] = []
    for k, segs in batches:
        same_window = [o for o in olds if window_of(o.id, src) == k]
        for i in segs:
            if i.id in ids:
                continue
            twin = max(same_window, key=lambda o: _overlap(i, o), default=None)
            if twin is not None and _overlap(i, twin) >= MIN_OVERLAP:
                i.replaces = twin.id
            ref = twin or (same_window[0] if same_window else None)
            if ref is not None:
                i.logo_crop = ref.logo_crop
                if ref.skipped in SOURCE_SKIPS and not i.skipped:
                    i.skipped = ref.skipped
            lib.items.append(i)
            added.append(i)
    return added


def _integrate(lib: Library, src: Source, llm: Any, errors: list[str]) -> tuple[int, int]:
    """给新段打标签(能继承的继承),再把"新段打上了标签"的窗的老段退役。
    返回 (这次打上标签的新段数, 退役的老段数)。调用方持池锁。"""
    news = hd_items(lib, src)
    room = (LAUNCH_MAX_TAGGED if src.kind == "launch" else MAX_TAGGED_PER_SOURCE) \
        - sum(1 for i in news if i.tagged)
    todo = sorted((i for i in news if not i.tagged and not i.skipped and i.frame),
                  key=lambda i: -i.length)[:max(0, room)]
    before = sum(1 for i in news if i.tagged)
    if todo and llm is not None:
        tag_items(lib, todo, llm, errors)
    else:
        inherit_duplicates(lib)
    news = hd_items(lib, src)
    tagged = sum(1 for i in news if i.tagged) - before
    # 窗里有新段打上了标签(或者新段全被本地体检毙掉 —— 那老段同样是废的)才退役老段
    ready = set()
    for k in {window_of(i.id, src) for i in news}:
        segs = [i for i in news if window_of(i.id, src) == k]
        if any(i.tagged for i in segs) or all(i.skipped for i in segs):
            ready.add(k)
    retired = 0
    for i in old_items(lib, src):
        if window_of(i.id, src) in ready:
            i.skipped = "replaced"
            retired += 1
    return tagged, retired


def upgrade_source(subject: str, source_id: str, *, llm: Any = None,
                   errors: list[str] | None = None) -> dict[str, Any]:
    """重拉一个源。返回 {"source", "result", "height", "added", "tagged", "retired", "failed"}。

    result:done(全部窗换好)/ partial(有窗没拉成或还没打上标签)/ low(平台只有低清)/
    failed(解析失败)/ skip(不用拉)。限流、cookies 没生效抛 `Stop`(已拉到的先入池)。"""
    errors = errors if errors is not None else []
    lib = Library.load(subject)
    src = lib.source(source_id)
    out: dict[str, Any] = {"source": source_id, "result": "skip", "height": 0, "added": 0,
                           "tagged": 0, "retired": 0, "failed": []}
    if src is None or not needs_upgrade(lib, src):
        return out
    platform = src.platform or platform_of(src.url)
    ks = _pending_windows(lib, src)
    batches: list[tuple[int, list[Item]]] = []
    stop: Stop | None = None
    height = src.upgraded_height
    if ks:
        _guard(platform)
        try:
            height = _probe_height(src, platform)
        except platform_video.PlatformError as exc:
            errors.append(f"重拉高清解析失败({src.url}):{str(exc)[:160]}")
            with pool_lock(subject):
                lib = Library.load(subject)
                s = lib.source(source_id)
                if s is not None:
                    s.upgraded_at, s.upgrade_error = time.time(), f"解析失败:{str(exc)[:150]}"
                    lib.save()
            out["result"] = "failed"
            return out
        out["height"] = height
        if height < MIN_UPGRADE_HEIGHT:
            with pool_lock(subject):
                lib = Library.load(subject)
                s = lib.source(source_id)
                if s is not None:
                    s.upgraded_at, s.upgraded_height = time.time(), height
                    s.upgrade_error = f"平台最高只给 {height}p" if height else "平台没给出清晰度"
                    lib.save()
            out["result"] = "low"
            return out
        _pause(WINDOW_GAP)
        batches, out["failed"], stop = _download(src, lib.dir / "raw", lib.dir / "frames", ks,
                                                 platform, errors)
    with pool_lock(subject):
        lib = Library.load(subject)
        s = lib.source(source_id)
        if s is None:
            return out
        added = _add_new(lib, s, batches)
        mark_official_duplicates(lib)
        lib.save()
        tagged, retired = _integrate(lib, s, llm, errors)
        s.upgraded_at = time.time()
        if height >= MIN_UPGRADE_HEIGHT:
            s.upgraded_height = height
        left = _pending_windows(lib, s)
        waiting = [i for i in old_items(lib, s) if window_of(i.id, s) not in left]
        s.upgrade_error = (f"窗 {out['failed']} 没拉成" if out["failed"] else
                           "新段还没打上标签,老段先留着" if waiting else "")
        lib.save()
    out.update(added=len(added), tagged=tagged, retired=retired,
               result="partial" if (left or waiting) else "done")
    if stop is not None:
        raise stop
    return out


def upgrade(subjects: list[str], *, llm: Any = None, limit: int = 0,
            errors: list[str] | None = None,
            log_fn: Callable[[str], None] | None = None) -> dict[str, Any]:
    """按车型重拉:每台车的候选源一个一个来,源与源之间歇 `SOURCE_GAP` 秒。
    某个平台被限流 / cookies 没生效,这个平台剩下的源本次都跳过。`limit` > 0 时最多做这么多个源。"""
    errors = errors if errors is not None else []
    say = log_fn or log.info
    stopped: dict[str, str] = {}
    results: list[dict[str, Any]] = []
    for subject in subjects:
        lib = Library.load(subject)
        for src in candidates(lib):
            if limit and len(results) >= limit:
                break
            platform = src.platform or platform_of(src.url)
            if platform in stopped:
                continue
            if results and results[-1]["result"] != "skip":
                _pause(SOURCE_GAP)  # 上一个源发过请求
            try:
                res = upgrade_source(subject, src.id, llm=llm, errors=errors)
            except Stop as stop:
                stopped[stop.platform] = stop.detail
                say(f"{platform} 停下:{stop.detail}")
                results.append({"source": src.id, "result": stop.reason})
                continue
            results.append(res)
            say(f"{subject} {src.title[:30] or src.url}:{res['result']} "
                f"(新 {res['added']} 段,退役 {res['retired']} 段)")
    counts: dict[str, int] = {}
    for r in results:
        counts[r["result"]] = counts.get(r["result"], 0) + 1
    return {"subjects": len(subjects), "sources": len(results), "results": counts,
            "stopped": stopped, "errors": errors[:20]}


# --------------------------------------------------------------------------
# 回填
# --------------------------------------------------------------------------
def backfill_shots(subject: str, llm: Any, errors: list[str], *,
                   should_stop: Callable[[], bool] | None = None,
                   chunk: int = TAG_BATCH * 5) -> int:
    """给可用视频段补打景别 / 大镜头分。一块一块来:拿池锁、重新 load、打一块、存盘。
    同一轮里送过一次的不再送(模型没给出结果的留到下次)。"""
    tried: set[str] = set()
    n = 0
    while not (should_stop and should_stop()):
        with pool_lock(subject):
            lib = Library.load(subject)
            todo = [i for i in lib.items if needs_shot(i) and i.id not in tried
                    and i.frame and Path(i.frame).is_file()][:chunk]
            if not todo:
                break
            tried.update(i.id for i in todo)
            n += tag_shots(lib, todo, llm, errors)
    return n


def backfill(subject: str, *, shots: bool = False, llm: Any = None,
             errors: list[str] | None = None,
             should_stop: Callable[[], bool] | None = None) -> dict[str, int]:
    """老池子补字段:动感总是补(不调模型);景别 / 大镜头只在 `shots` 时补(花视觉调用)。"""
    errors = errors if errors is not None else []
    out = {"motion": motion.fill_motion(subject, should_stop=should_stop), "shots": 0}
    if shots and llm is not None:
        out["shots"] = backfill_shots(subject, llm, errors, should_stop=should_stop)
    return out


# --------------------------------------------------------------------------
# 后台任务
# --------------------------------------------------------------------------
def job_key(subject: str = "", all_: bool = False) -> str:
    return ALL_KEY if all_ else slug(subject)


def _subjects(payload: dict[str, Any]) -> list[str]:
    return all_subjects() if payload.get("all") else [str(payload["subject"])]


def _llm(payload: dict[str, Any]) -> Any:
    if payload.get("no_llm"):
        return None
    from .. import orchestrator

    return orchestrator._llm()


def _upgrade_job(payload: dict[str, Any]) -> dict[str, Any]:
    """重拉任务的 handler。按源幂等:中断、重试都从没做完的源 / 窗接着做。"""
    return upgrade(_subjects(payload), llm=_llm(payload), limit=int(payload.get("limit") or 0))


def _backfill_job(payload: dict[str, Any]) -> dict[str, Any]:
    llm = _llm(payload) if payload.get("shots") else None
    errors: list[str] = []
    out: dict[str, Any] = {"motion": 0, "shots": 0}
    for subject in _subjects(payload):
        got = backfill(subject, shots=bool(payload.get("shots")), llm=llm, errors=errors)
        out["motion"] += got["motion"]
        out["shots"] += got["shots"]
    out["errors"] = errors[:20]
    return out


def submit_upgrade(subject: str = "", *, all_: bool = False, limit: int = 0,
                   no_llm: bool = False) -> int | None:
    """排一个重拉任务。同一个键已经在排 / 在跑 → None。"""
    from ... import jobs

    payload = {"subject": subject, "all": all_, "limit": int(limit), "no_llm": no_llm}
    return jobs.submit(UPGRADE_JOB, key=job_key(subject, all_), payload=payload)


def submit_backfill(subject: str = "", *, all_: bool = False, shots: bool = False) -> int | None:
    from ... import jobs

    payload = {"subject": subject, "all": all_, "shots": shots}
    return jobs.submit(BACKFILL_JOB, key=job_key(subject, all_), payload=payload)


def register_jobs() -> None:
    """由 `builder._register_jobs` 调(builder 在 jobs.HANDLER_MODULES 里)。"""
    from ... import jobs

    # 重试:下载 / 解析的偶发错误值得再来;限流不抛异常(按平台停下,结果里写着)
    jobs.register(UPGRADE_JOB, resource="library", max_attempts=3, lease=300.0)(_upgrade_job)
    jobs.register(BACKFILL_JOB, resource="library", max_attempts=2, lease=300.0)(_backfill_job)
