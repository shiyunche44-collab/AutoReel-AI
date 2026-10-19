"""真实素材的逐镜头取材:官方素材 → 平台视频 → 网上图片 / 免费图库 → AI 生成。

定位:**能用真实就用真实**。每个镜头按自己的检索词逐级找素材,命中即停;
整条梯子都没找到的镜头才回退 AI 生成。收益有两重:
1. 画面里是真实的那台车,不是模型想象的;
2. 每个真实镜头省掉草稿 + 成片两次生成(成片档每镜 $0.48 + 15–30 分钟墙钟)。

取材梯子(见 `CAR_TIERS` / `OTHER_TIERS`)
---------------------------------------
车辆镜头(画面主体是目标车型):
  ⓪ 官方视频:品牌官方号(B 站 / YouTube)、车企官网 / 媒体中心里的视频
  ⓪ 官方图片:汽车之家 / 懂车帝官图库、车企官网大图 → 推拉动效镜头
  ① 平台视频:B 站 → YouTube → 抖音/西瓜,按镜头检索词搜(官方号结果仍排最前)
  ② 维基共享资源视频、车媒文章页里的直链与嵌入播放器
  ③ 网上真实图片(参考图集、Bing 图片)→ 推拉动效镜头
非车镜头(氛围、行业、人物):
  ① 平台视频 → ② Pexels / Pixabay 视频 → ③ Pexels / Pixabay 图片 → 推拉动效镜头
最后都是 ④ AI 生成(什么都不做,镜头留给生成层)。

为什么注入点在素材层,而不是分镜层
--------------------------------
真实素材是**后验可得性**:先去找,找到就用,找不到必须能无声回退生成。
写进 `ShotSpec` 等于"先决定用真视频,再去找",一旦找不到就变成内容层错误,
整条流水线会被一个素材问题卡住。所以 `ShotSpec` 一列不加字段,决策留在这里。

为什么现有调度一行都不用改
--------------------------
`apply_clips()` 把命中的镜头直接写成 `status="succeeded"` + 落盘路径,
于是:
- `generate.submit_ready()` 的 done 集合含它 → 不会被重复提交,不烧额度;
- `stage_complete()` / `missing_final_shots()` 判定通过;
- `render.compose()` 拿到的就是本地 mp4,`normalise_clip()` 会统一归一化
  到 1080×1920 / 24fps —— 画幅不一致的问题被渲染层现成地解决了。

取材在门禁 A 之后跑一次,结果存成 `clips.json`(取材计划):草稿档直接用它
顶掉草稿生成,门禁 B 按批把同一份计划应用到成片档,不再重新抓网络。
"""
from __future__ import annotations

import json
import re
import subprocess
import time
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

from .. import config, db
from ..categories import CategoryProfile
from ..models import ShotSpec
from ..providers import clips as clips_mod
from ..providers import images, official, platform_video, stock, webassets
from . import asset_resolver, media_query, render

#: 视角 → 是否车辆画面。沿用 media_query 的定义,两处必须一致
REAL_CLIP_ANGLES: tuple[str, ...] = media_query.CAR_ANGLES

CAR_TIERS: tuple[str, ...] = (
    "official_video", "official_still", "platform_video", "page_clip", "web_still",
)
OTHER_TIERS: tuple[str, ...] = ("platform_video", "stock_video", "stock_still")
STILL_TIERS: frozenset[str] = frozenset({"official_still", "web_still", "stock_still"})

#: 每个镜头在每一级最多尝试落地几个候选。落地 = 下载 + 转码,一次十几秒到
#: 一分钟;不设上限的话,一个坏源(全是 403)能把整个素材层拖上半小时。
TRIES_PER_TIER = 2

#: 片段质检提问。要问两件事:主体有没有被画幅裁掉(横屏素材裁 9:16 最容易
#: 把车切掉),以及有没有字幕台标。
QC_PROMPT = (
    "下面每张图片是从候选视频片段中点抽出的帧。请逐张判定,只依据图片内容回答。\n"
    "对每张图片输出一个 JSON 对象,字段:\n"
    '  "index": 第几张图(从 1 开始,与给出顺序一致)\n'
    '  "subject_whole": true / false —— 画面里的主体(车)是否**完整**在画面内,'
    "没有被画幅裁掉\n"
    '  "has_text_overlay": true / false —— 是否有字幕、台标、水印或大段文字\n'
    '  "problem": 空字符串表示可用;否则填下面之一 —— '
    "cropped(主体被裁掉) / text_overlay(有字幕台标) / unclear(画面糊或看不清主体) "
    "/ unknown\n"
    "只输出 JSON 数组,不要任何解释文字。"
)

#: 硬弃用的理由。
#:
#: `text_overlay` **刻意不在列**。车媒视频几乎每条都带台标,按它过滤会让
#: 这一整条通路空转 —— 候选全被毙掉,功能等于没做。带角标的真实车画面,
#: 对"个人学习"用途完全够看,所以只记警告不弃用。
#: 真正会**出错**的是主体被裁掉和画面不清,那两条必须拦。
_BLOCKING_PROBLEMS = {"cropped", "unclear"}

#: 默认不设上限:能用真实就用真实。`.env` 的 REAL_CLIP_LIMIT 可以收紧
DEFAULT_LIMIT = 0


# --------------------------------------------------------------------------
# 候选:各级来源
# --------------------------------------------------------------------------
def _candidate_pool(subject: str, *, page_urls: Sequence[str] | None = None
                    ) -> tuple[list[clips_mod.ClipCandidate], list[str]]:
    """第②级:维基共享资源视频 + 车媒文章页(直链与嵌入的平台播放器)。

    任何一路失败都只是少几个候选,绝不抛异常 —— 素材层必须能降级。
    """
    errors: list[str] = []
    pool: list[clips_mod.ClipCandidate] = []

    # 维基共享资源(免密钥、授权明确,但车型视频存量很少)
    for query in (subject, f"{subject} car"):
        try:
            pool.extend(clips_mod.commons_search_videos_cached(query, limit=4))
        except Exception as exc:  # noqa: BLE001
            errors.append(f"维基视频检索失败({query}):{exc}")
        if pool:
            break

    # 车媒文章页:直链实测命中率很低(2/14),余下多是 B 站 / YouTube iframe ——
    # 现在 iframe 也能还原成视频页交给 yt-dlp。
    for page in (page_urls or []):
        try:
            direct, embeds = clips_mod.page_media(page)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"文章页抓取失败({page}):{exc}")
            continue
        for url in direct:
            pool.append(clips_mod.ClipCandidate(
                url=url, page_url=page, origin="clip_media", title=page,
                license=f"车媒公开页:{page}",
            ))
        for url in embeds:
            plat = "bilibili" if "bilibili" in url else "youtube"
            for start in platform_video.segment_starts(0, config.SHOT_GRID_SECONDS):
                pool.append(clips_mod.ClipCandidate(
                    url=url, page_url=page, origin=f"clip_media:{plat}", title=page,
                    license=f"车媒公开页嵌入:{page}", platform=plat, start=start,
                ))

    return _dedupe(pool), errors


def _dedupe(pool: Iterable[clips_mod.ClipCandidate]) -> list[clips_mod.ClipCandidate]:
    seen: set[str] = set()
    out: list[clips_mod.ClipCandidate] = []
    for c in pool:
        key = _key(c)
        if key in seen:
            continue
        seen.add(key)
        out.append(c)
    return out


def _key(c: clips_mod.ClipCandidate) -> str:
    """同一条平台视频的不同起点算不同素材 —— 它们会被切给相邻镜头。"""
    return f"{c.url}@{c.start:.1f}" if c.platform else c.url


def _hits_to_candidates(hits: Iterable[platform_video.PlatformHit], *, seconds: float,
                        segments: int = 3) -> list[clips_mod.ClipCandidate]:
    """平台视频 → 候选。一条视频按不同起点展开成多个候选,
    同源片段连着用就是免费的强一致性。"""
    out: list[clips_mod.ClipCandidate] = []
    for h in hits:
        starts = (platform_video.segment_starts(h.duration, seconds, segments)
                  if h.duration else [5.0, 20.0, 35.0][:segments])
        prefix = "official" if h.official else "platform"
        for start in starts:
            out.append(clips_mod.ClipCandidate(
                url=h.url, page_url=h.url, origin=f"{prefix}:{h.platform}",
                title=h.title, license=f"{h.platform}:{h.uploader or '未知上传者'}",
                author=h.uploader, duration=h.duration, platform=h.platform,
                start=start, official=h.official, uploader=h.uploader,
            ))
    return out


def _stills_to_candidates(refs: Iterable[images.ImageRef]) -> list[clips_mod.ClipCandidate]:
    out: list[clips_mod.ClipCandidate] = []
    for r in refs:
        if asset_resolver._junk_reason(r):
            continue
        out.append(clips_mod.ClipCandidate(
            url=r.url, page_url=r.source_page, origin=r.origin, title=r.note,
            license=r.license, author=r.author, kind="still", view=r.angle,
            official=r.origin.startswith("official"),
        ))
    return out


@dataclass
class _Ctx:
    """一次取材的共享状态。候选按 (来源, 检索词) 缓存:相邻镜头检索词常常相同,
    同一个搜索没必要打两遍。"""
    subject: str
    profile: CategoryProfile
    seconds: float
    errors: list[str]
    page_urls: Sequence[str] = ()
    ref_images: Sequence[images.ImageRef] = ()
    brand_key: str = ""
    brand_spec: dict[str, tuple[str, ...]] = field(default_factory=dict)
    aliases: list[str] = field(default_factory=list)
    cache: dict[str, list[clips_mod.ClipCandidate]] = field(default_factory=dict)
    tried: set[str] = field(default_factory=set)
    bad_urls: set[str] = field(default_factory=set)
    sources_tried: list[str] = field(default_factory=list)

    def memo(self, key: str, fn: Callable[[], list[clips_mod.ClipCandidate]]
             ) -> list[clips_mod.ClipCandidate]:
        if key not in self.cache:
            try:
                got = fn()
            except Exception as exc:  # noqa: BLE001 - 一个来源挂了只是少一级
                self.errors.append(f"{key} 失败:{type(exc).__name__}: {str(exc)[:120]}")
                got = []
            self.cache[key] = _dedupe(got)
            self.sources_tried.append(f"{key}:{len(self.cache[key])}")
        return self.cache[key]


def _platforms_for(lang: str) -> list[str]:
    """中文检索词给国内平台,英文检索词给 YouTube。"""
    zh = [p for p in config.MEDIA_PLATFORMS if p in ("bilibili", "douyin")]
    en = [p for p in config.MEDIA_PLATFORMS if p == "youtube"]
    return zh if lang == "zh" else en


def _tier_candidates(ctx: _Ctx, tier: str, q: media_query.ShotQuery
                     ) -> list[clips_mod.ClipCandidate]:
    car = q.kind == "car"
    if tier == "official_video":
        if not ctx.aliases:
            return []

        def _official_video() -> list[clips_mod.ClipCandidate]:
            hits = official.platform_official(
                ctx.subject, ctx.aliases, platforms=config.MEDIA_PLATFORMS,
                errors=ctx.errors)
            vids, _ = _site_media(ctx)
            return _hits_to_candidates(hits, seconds=ctx.seconds) + vids
        return ctx.memo("official_video", _official_video)

    if tier == "official_still":
        def _official_still() -> list[clips_mod.ClipCandidate]:
            refs = official.gallery_images(ctx.subject, errors=ctx.errors)
            if ctx.brand_spec:
                refs = refs + _site_media(ctx)[1]
            return _stills_to_candidates(refs)
        return _by_angle(ctx.memo("official_still", _official_still), q.angle)

    if tier == "platform_video":
        def _platform(query: str, lang: str) -> list[clips_mod.ClipCandidate]:
            hits = platform_video.search(query, platforms=_platforms_for(lang),
                                         errors=ctx.errors)
            for h in hits:
                h.official = h.official or bool(
                    ctx.aliases and platform_video.is_official(h, ctx.aliases))
            ranked = platform_video.rank(hits, subject=ctx.subject, require_subject=car)
            return _hits_to_candidates(ranked, seconds=ctx.seconds)
        zh = ctx.memo(f"platform:zh:{q.query_zh}", lambda: _platform(q.query_zh, "zh"))
        en = ctx.memo(f"platform:en:{q.query_en}", lambda: _platform(q.query_en, "en"))
        # 官方号的结果不论来自哪个平台都排最前
        return sorted(zh + en, key=lambda c: 0 if c.official else 1)

    if tier == "page_clip":
        def _pages() -> list[clips_mod.ClipCandidate]:
            pool, errs = _candidate_pool(ctx.subject, page_urls=ctx.page_urls)
            ctx.errors.extend(errs)
            return pool
        return ctx.memo("page_clip", _pages)

    if tier == "web_still":
        if not config.USE_STILL_SHOTS:
            return []
        refs = _stills_to_candidates(ctx.ref_images)
        bing = ctx.memo(f"bing:{q.query_zh}", lambda: _stills_to_candidates(
            webassets.bing_images_cached(q.query_zh, limit=12)))
        return _by_angle(refs, q.angle) + bing

    if tier == "stock_video":
        return ctx.memo(f"stock_video:{q.query_en}", lambda: stock.search_videos(
            q.query_en, errors=ctx.errors))

    if tier == "stock_still":
        if not config.USE_STILL_SHOTS:
            return []
        return ctx.memo(f"stock_still:{q.query_en}", lambda: _stills_to_candidates(
            stock.search_photos(q.query_en, errors=ctx.errors)))
    return []


def _site_media(ctx: _Ctx) -> tuple[list[clips_mod.ClipCandidate], list[images.ImageRef]]:
    """车企官网一次抓取同时产出视频和图片,两级共用一份结果。"""
    if "_site" not in ctx.cache:
        vids: list[clips_mod.ClipCandidate] = []
        stills: list[images.ImageRef] = []
        if ctx.brand_spec:
            try:
                vids, stills = official.site_media(ctx.subject, ctx.brand_spec,
                                                   errors=ctx.errors)
            except Exception as exc:  # noqa: BLE001
                ctx.errors.append(f"车企官网抓取失败:{str(exc)[:120]}")
            for v in vids:
                if v.platform and not v.start:
                    v.start = 5.0
        ctx.cache["_site"] = vids
        ctx.cache["_site_stills"] = stills  # type: ignore[assignment]
        ctx.sources_tried.append(f"official_site:{len(vids)}v/{len(stills)}i")
    return ctx.cache["_site"], ctx.cache["_site_stills"]  # type: ignore[return-value]


def _by_angle(cands: list[clips_mod.ClipCandidate], angle: str) -> list[clips_mod.ClipCandidate]:
    """同视角的图排前面,没标视角的其次,别的视角放最后(外观镜头别配内饰图)。"""
    return sorted(cands, key=lambda c: 0 if c.view == angle else (1 if not c.view else 2))


# --------------------------------------------------------------------------
# 落地
# --------------------------------------------------------------------------
def _motion_for(dest: Path) -> str:
    digits = re.findall(r"\d+", dest.stem)
    n = int(digits[-1]) if digits else 0
    return render.STILL_MOTIONS[n % len(render.STILL_MOTIONS)]


def _referer(page_url: str) -> str | None:
    if "//" not in page_url:
        return None
    scheme, rest = page_url.split("//", 1)
    host = rest.split("/", 1)[0]
    return f"{scheme}//{host}/" if host else None


def _materialise(candidate: clips_mod.ClipCandidate, dest: Path,
                 seconds: float) -> clips_mod.ClipCandidate:
    """把候选落成一个定长本地镜头。三种形态:

    - 静图:下载后做推拉动效;
    - 平台视频:yt-dlp 只下 [start, start+seconds] 一段,再统一转码;
    - 直链:ffmpeg 直接拉流裁剪(源比目标短就循环补足)。
    """
    if candidate.kind == "still":
        raw = dest.with_name(dest.stem + "_src.img")
        headers = {"User-Agent": webassets.BROWSER_UA}
        ref = _referer(candidate.page_url)
        if ref:
            headers["Referer"] = ref
        images.download(candidate.url, raw, headers=headers)
        render.still_to_clip(raw, dest, seconds=seconds, motion=_motion_for(dest))
        candidate.local_path = str(dest)
        return candidate

    if candidate.platform:
        raw = platform_video.download_segment(
            candidate.url, dest.with_name(dest.stem + "_raw.mp4"),
            start=candidate.start, seconds=seconds,
        )
        clip_len = clips_mod.probe_duration(raw)
        path = clips_mod.fetch_clip(str(raw), dest, seconds=seconds,
                                    loop=0 < clip_len < seconds)
        candidate.local_path = str(path)
        return candidate

    duration = clips_mod.probe_duration(candidate.local_path) if candidate.local_path else 0.0
    loop = 0 < duration < seconds
    referer = _referer(candidate.page_url) if candidate.origin.startswith(
        ("clip_media", "official")) else None
    path = clips_mod.fetch_clip(
        candidate.url, dest, seconds=seconds, referer=referer, loop=loop,
    )
    candidate.local_path = str(path)
    return candidate


def _extract_frame(clip_path: str | Path, dest: Path) -> Path | None:
    """抽片段中点的一帧用于质检。"""
    duration = clips_mod.probe_duration(clip_path)
    at = max(0.0, duration / 2) if duration else 1.0
    proc = subprocess.run(
        [clips_mod._ffmpeg(), "-hide_banner", "-nostdin", "-y", "-v", "error",
         "-ss", f"{at:.2f}", "-i", str(clip_path), "-frames:v", "1",
         "-q:v", "3", str(dest)],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120,
    )
    return dest if proc.returncode == 0 and dest.is_file() else None


#: 视觉问答一次最多收 10 张图(`images.describe` 会截断),多了要分批
_QC_BATCH = 10


def _qc(clipped: list[clips_mod.ClipCandidate], work_dir: Path,
        errors: list[str]) -> list[clips_mod.ClipCandidate]:
    """分批质检。逐镜头取材后一部片可能有几十个真实镜头,一次问不完。"""
    if len(clipped) <= _QC_BATCH:
        return _qc_batch(clipped, work_dir, errors)
    kept: list[clips_mod.ClipCandidate] = []
    for n in range(0, len(clipped), _QC_BATCH):
        kept += _qc_batch(clipped[n:n + _QC_BATCH], work_dir / f"batch{n // _QC_BATCH:02d}",
                          errors)
    return kept


def _qc_batch(clipped: list[clips_mod.ClipCandidate], work_dir: Path,
              errors: list[str]) -> list[clips_mod.ClipCandidate]:
    """对已落地的片段抽帧质检:主体被裁 / 画面不清的弃用。

    质检失败(抽不出帧、传不上传、模型不认 JSON)时**一律保留** ——
    宁可让一条可能有点问题的片段进成片,也不要因为质检链路抖动把真实素材
    通路整个废掉。
    """
    if not clipped:
        return []
    frames_dir = work_dir / "qc_frames"
    frame_urls: list[str] = []
    keep_flags: list[bool] = []

    for i, cand in enumerate(clipped):
        frame = _extract_frame(cand.local_path, frames_dir / f"f{i:02d}.jpg")
        if frame is None:
            frame_urls.append("")
            keep_flags.append(True)
            continue
        try:
            frame_urls.append(images.cli.upload_temp_url(str(frame)))
        except Exception as exc:  # noqa: BLE001
            errors.append(f"质检帧上传失败({cand.url[:40]}):{exc}")
            frame_urls.append("")
            keep_flags.append(True)

    valid = [u for u in frame_urls if u]
    if not valid:
        return clipped

    try:
        answer = images.describe(valid, QC_PROMPT)
    except Exception as exc:  # noqa: BLE001
        errors.append(f"片段质检调用失败:{exc}")
        return clipped

    verdicts = asset_resolver.parse_verdicts(answer)
    if not verdicts:
        errors.append("片段质检返回无法解析,已全部保留")
        return clipped

    # index 是按 valid 顺序编号的,和 clipped 顺序要对齐
    index_of = {url: n for n, url in enumerate(valid, start=1)}
    by_index: dict[int, dict[str, Any]] = {}
    for v in verdicts:
        try:
            by_index[int(v.get("index", 0))] = v
        except (TypeError, ValueError):
            continue

    kept: list[clips_mod.ClipCandidate] = []
    rejected: list[str] = []
    for cand, url in zip(clipped, frame_urls):
        if not url:
            kept.append(cand)
            continue
        verdict = by_index.get(index_of.get(url, -1))
        if not verdict:
            kept.append(cand)
            continue
        problem = str(verdict.get("problem") or "").strip().lower()
        if verdict.get("subject_whole") is False and problem not in _BLOCKING_PROBLEMS:
            problem = "cropped"
        if problem in _BLOCKING_PROBLEMS:
            rejected.append(f"{cand.title or cand.url[:40]}:{problem}")
            continue
        if problem:
            # 记录但不弃用 —— 见 _BLOCKING_PROBLEMS 的说明
            cand.license = f"{cand.license}|质检提示:{problem}" if cand.license else f"质检提示:{problem}"
        kept.append(cand)

    if rejected:
        errors.append("片段质检弃用:" + "; ".join(rejected))
    return kept


# --------------------------------------------------------------------------
# 逐镜头取材
# --------------------------------------------------------------------------
def collect(*, subject: str, profile: CategoryProfile,
            specs: Sequence[ShotSpec], project_dir: Path,
            page_urls: Sequence[str] | None = None,
            limit: int = DEFAULT_LIMIT, qc: bool = True,
            queries: Sequence[media_query.ShotQuery] | None = None,
            brand: str = "",
            ref_images: Sequence[images.ImageRef] | None = None) -> tuple[
                dict[int, clips_mod.ClipCandidate], dict[str, Any]]:
    """逐镜头取材、质检。返回 (镜头序号 → 候选, 报告)。

    全程不抛异常:任何一步失败都只是这个镜头往下降一级,最后退回生成。
    `queries` 不给时用模板检索词(不调 LLM)。
    """
    report: dict[str, Any] = {
        "subject": subject,
        "candidates": 0,
        "fetched": 0,
        "qc_rejected": 0,
        "selected": {},
        "fallback": [],
        "origin_counts": {},
        "official_count": 0,
        "errors": [],
        "sources_tried": [],
        "savings_estimate": 0.0,
        "enabled": True,
    }
    seconds = float(config.SHOT_GRID_SECONDS)
    if queries is None:
        queries, _ = media_query.build(subject, profile, specs, llm=None)
    by_idx = {q.idx: q for q in queries}

    brand_key, brand_spec = official.detect_brand(subject, profile.official_sites, hint=brand)
    ctx = _Ctx(
        subject=subject, profile=profile, seconds=seconds, errors=report["errors"],
        page_urls=list(page_urls or []), ref_images=list(ref_images or []),
        brand_key=brand_key, brand_spec=brand_spec,
        aliases=official.brand_aliases(brand_key, brand_spec, brand) if (brand_key or brand) else [],
    )
    report["brand"] = brand_key or brand

    clip_dir = project_dir / "clips"
    work_dir = project_dir / "_clips"
    assignment: dict[int, clips_mod.ClipCandidate] = {}
    tiers_of: dict[int, str] = {}
    last_page = ""
    considered: set[str] = set()

    started = time.monotonic()
    for spec in specs:
        if limit and len(assignment) >= limit:
            break
        if config.MEDIA_TIME_BUDGET and time.monotonic() - started > config.MEDIA_TIME_BUDGET:
            report["errors"].append(
                f"取材超出时长预算({config.MEDIA_TIME_BUDGET}s),镜头 {spec.idx} 起走生成")
            break
        q = by_idx.get(spec.idx) or media_query._fallback(subject, profile, spec)
        tiers = CAR_TIERS if q.kind == "car" else OTHER_TIERS
        dest = clip_dir / f"shot_{spec.idx:03d}.mp4"
        for tier in tiers:
            cands = [c for c in _tier_candidates(ctx, tier, q) if _key(c) not in ctx.tried]
            considered.update(_key(c) for c in cands)
            # 同源优先:上一个镜头用的视频还有别的起点,接着用它
            if last_page:
                cands.sort(key=lambda c: 0 if c.page_url == last_page else 1)
            hit = _try_tier(ctx, cands, dest, seconds, report)
            if hit is not None:
                assignment[spec.idx] = hit
                tiers_of[spec.idx] = tier
                last_page = hit.page_url or last_page
                break

    report["candidates"] = len(considered)
    report["sources_tried"] = ctx.sources_tried

    # 质检只查车辆镜头:非车画面没有"是不是这台车 / 车被裁没裁"的问题
    if qc:
        car_idx = [i for i in assignment if (by_idx.get(i) and by_idx[i].kind == "car")]
        kept = _qc([assignment[i] for i in car_idx], work_dir, report["errors"])
        kept_ids = {id(c) for c in kept}
        for i in car_idx:
            if id(assignment[i]) not in kept_ids:
                del assignment[i]
                tiers_of.pop(i, None)
                report["qc_rejected"] += 1

    for idx, cand in sorted(assignment.items()):
        report["selected"][str(idx)] = {
            "path": cand.local_path,
            "origin": cand.origin,
            "kind": cand.kind,
            "tier": tiers_of.get(idx, ""),
            "official": cand.official,
            "title": cand.title,
            "license": cand.license,
            "author": cand.author,
            "page_url": cand.page_url,
            "start": cand.start,
            "query": by_idx[idx].query_zh if idx in by_idx else "",
        }
        report["origin_counts"][cand.origin] = report["origin_counts"].get(cand.origin, 0) + 1
    report["official_count"] = sum(1 for c in assignment.values() if c.official)
    report["fallback"] = [s.idx for s in specs if s.idx not in assignment]

    if not assignment:
        report["enabled"] = False
        report["summary"] = "没有取到任何可用的真实素材,全片继续用生成"
        return {}, report

    # 真实素材省下的是成片档那一次生成
    try:
        from .. import costs

        report["savings_estimate"] = round(
            costs.savings_from_clips(len(assignment)), 4
        )
    except Exception:  # noqa: BLE001
        report["savings_estimate"] = 0.0

    videos = sum(1 for c in assignment.values() if c.kind != "still")
    report["summary"] = (
        f"{len(assignment)}/{len(specs)} 个镜头用真实素材"
        f"(视频 {videos}、图片 {len(assignment) - videos},其中官方 "
        f"{report['official_count']}),其余 {len(report['fallback'])} 个走生成"
    )
    return assignment, report


def _try_tier(ctx: _Ctx, cands: list[clips_mod.ClipCandidate], dest: Path,
              seconds: float, report: dict[str, Any]) -> clips_mod.ClipCandidate | None:
    attempts = 0
    for cand in cands:
        if attempts >= TRIES_PER_TIER:
            break
        # 一条视频在某个起点下载失败(403、地区限制、要登录),换起点也一样
        if cand.url in ctx.bad_urls:
            continue
        attempts += 1
        ctx.tried.add(_key(cand))
        try:
            got = _materialise(cand, dest, seconds)
        except Exception as exc:  # noqa: BLE001
            ctx.bad_urls.add(cand.url)
            report["errors"].append(
                f"素材落地失败({cand.origin} {cand.url[:50]}):{str(exc)[:160]}")
            continue
        report["fetched"] += 1
        return got
    return None


def apply_clips(project_id: str, assignment: dict[int, clips_mod.ClipCandidate],
                stage: str = "final") -> int:
    """把真实素材写进 shots 表并落盘到该档位的标准目录。返回实际生效的镜头数。

    `stage="draft"` 用在门禁 A 之后:真实镜头不必再生成草稿,门禁 B 审片时
    看到的直接就是真实素材。`stage="final"` 用在门禁 B 放行之后。

    刻意不碰一致性链的排布:片段仍留在原来的链位,只是这一格不再需要
    生成。这样 `materialize_first_frames` 仍会从前驱抽帧 —— 而前驱若是
    真实片段,抽出来的就是**真实车尾帧**,恰恰是最好的锚点。
    """
    applied = 0
    project_dir = config.project_dir(project_id)
    out_dir = project_dir / ("drafts" if stage == "draft" else "shots")
    out_dir.mkdir(parents=True, exist_ok=True)
    path_field = "draft_path" if stage == "draft" else "final_path"

    for idx, cand in sorted(assignment.items()):
        src = Path(cand.local_path)
        if not src.is_file():
            continue
        dest = out_dir / f"{idx:03d}.mp4"
        try:
            if src.resolve() != dest.resolve():
                dest.write_bytes(src.read_bytes())
        except OSError as exc:
            db.add_event(project_id, "pending", src="assets",
                         reason=f"真实素材落盘失败(镜头 {idx}):{exc}")
            continue

        db.update_shot(
            project_id, idx, stage,
            status="succeeded", provider_task_id=None, error=None, attempts=0,
            est_cost=0.0, consistency="real", **{path_field: str(dest)},
        )
        what = "图片动效" if cand.kind == "still" else "视频片段"
        db.add_event(
            project_id, "succeeded", src="assets",
            reason=f"镜头 {idx} 使用真实{what}({cand.origin})",
        )
        applied += 1
    return applied


def persist(project_id: str, report: dict[str, Any],
            assignment: dict[int, clips_mod.ClipCandidate]) -> None:
    """落库 clips.json 与 clip_report.json,供 Web 展示与事后追溯。"""
    db.write_json(project_id, "clips.json", [
        {"idx": idx, **asdict(cand)} for idx, cand in sorted(assignment.items())
    ])
    db.write_json(project_id, "clip_report.json", report)


def load_report(project_id: str) -> dict[str, Any]:
    path = config.project_dir(project_id) / "clip_report.json"
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def load_plan(project_id: str) -> dict[int, clips_mod.ClipCandidate]:
    """读回 `clips.json`(取材计划)。门禁 B 用它把同一批素材应用到成片档,
    不必重新抓网络。"""
    rows = db.read_json(project_id, "clips.json", []) or []
    names = {f.name for f in fields(clips_mod.ClipCandidate)}
    plan: dict[int, clips_mod.ClipCandidate] = {}
    for row in rows:
        try:
            idx = int(row["idx"])
        except (KeyError, TypeError, ValueError):
            continue
        plan[idx] = clips_mod.ClipCandidate(**{k: v for k, v in row.items() if k in names})
    return plan
