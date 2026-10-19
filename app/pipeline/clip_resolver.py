"""真实视频片段的采集、选片与注入。

定位:素材层的**补充**通路。外观/主体镜头能用网上真实片段就用,
穿插/氛围镜头仍然走生成。这么做有两重收益:
1. 画面里是真实的那台车,不是模型想象的;
2. 每个成片镜头省 $0.48 等效用量 + 15–30 分钟墙钟(H3 走 account credits)。

为什么注入点在这里,而不是分镜层
--------------------------------
真实素材是**后验可得性**:先去找,找到就用,找不到必须能无声回退生成。
写进 `ShotSpec` 等于"先决定用真视频,再去找",一旦找不到就变成内容层错误,
整条流水线会被一个素材问题卡住。所以 `ShotSpec` 一列不加字段,
决策留在这里。

为什么现有调度一行都不用改
--------------------------
`apply_clips()` 把命中的镜头直接写成 `status="succeeded"` + 落盘路径,
于是:
- `generate.submit_ready()` 的 done 集合含它 → 不会被重复提交,不烧额度;
- `stage_complete()` 判定通过;
- `missing_final_shots()` 判定通过;
- `render.compose()` 拿到的就是本地 mp4,`normalise_clip()` 会统一归一化
  到 1080×1920 / 24fps —— 画幅不一致的问题被渲染层现成地解决了。
"""
from __future__ import annotations

import json
import subprocess
from dataclasses import asdict
from pathlib import Path
from typing import Any, Sequence

from .. import config, db
from ..categories import CategoryProfile
from ..models import ShotSpec
from ..providers import clips as clips_mod
from ..providers import images
from . import asset_resolver

#: 只给这些视角的镜头配真实片段。
#:
#: 外观镜头才值得动用真实素材 —— 那里 AI 最容易画错车型。氛围/过场镜头
#: 画错了也看不出来,而且生成更快更便宜。
REAL_CLIP_ANGLES: tuple[str, ...] = ("exterior_45", "rear", "detail")

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

#: 每个项目最多用多少条真实片段。上限存在的理由和参考图集一样:
#: 真实素材混得太多,风格断层会盖过"车型准确"这一个好处。
DEFAULT_LIMIT = 4


# --------------------------------------------------------------------------
# 候选采集
# --------------------------------------------------------------------------
def _candidate_pool(subject: str, *, page_urls: Sequence[str] | None = None
                    ) -> tuple[list[clips_mod.ClipCandidate], list[str]]:
    """汇总两条来源的候选片段。返回 (候选, 错误信息)。

    任何一路失败都只是少几个候选,绝不抛异常 —— 素材层必须能降级。
    """
    errors: list[str] = []
    pool: list[clips_mod.ClipCandidate] = []

    # V1:维基共享资源(免密钥、授权明确,但车型视频存量很少)
    for query in (subject, f"{subject} car"):
        try:
            pool.extend(clips_mod.commons_search_videos_cached(query, limit=4))
        except Exception as exc:  # noqa: BLE001
            errors.append(f"维基视频检索失败({query}):{exc}")
        if pool:
            break

    # V2:车媒文章页里嵌的直链。实测命中率很低(探到 2/14),
    # 余下多是 YouTube / B 站 iframe —— 取不到是正常结果,不是错误。
    for page in (page_urls or []):
        try:
            for url in clips_mod.page_video_urls(page):
                pool.append(clips_mod.ClipCandidate(
                    url=url,
                    page_url=page,
                    origin="clip_media",
                    title=page,
                    license=f"车媒公开页:{page}",
                ))
        except Exception as exc:  # noqa: BLE001
            errors.append(f"文章页抓取失败({page}):{exc}")

    # 去重
    seen: set[str] = set()
    unique: list[clips_mod.ClipCandidate] = []
    for c in pool:
        if c.url in seen:
            continue
        seen.add(c.url)
        unique.append(c)
    return unique, errors


def _materialise(candidate: clips_mod.ClipCandidate, dest: Path,
                 seconds: float) -> clips_mod.ClipCandidate:
    """抓取并定长裁剪。源比目标短就循环补足。"""
    duration = clips_mod.probe_duration(candidate.local_path) if candidate.local_path else 0.0
    loop = 0 < duration < seconds
    referer = None
    if candidate.origin == "clip_media":
        host = candidate.page_url.split("/")[2] if "//" in candidate.page_url else ""
        referer = f"{candidate.page_url.split('/')[0]}//{host}/" if host else None
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


def _qc(clipped: list[clips_mod.ClipCandidate], work_dir: Path,
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
# 选片与注入
# --------------------------------------------------------------------------
def _assign(specs: Sequence[ShotSpec],
            clipped: Sequence[clips_mod.ClipCandidate]) -> dict[int, clips_mod.ClipCandidate]:
    """把候选片段分配给适合的镜头。

    同源优先连用:同一篇车媒文章里的片段通常拍的是**同一台车**,连着用
    就是免费的强一致性 —— 比跨镜让生成模型自己保持一致可靠得多。
    """
    eligible = [s for s in specs if (s.angle or "") in REAL_CLIP_ANGLES]
    assignment: dict[int, clips_mod.ClipCandidate] = {}
    remaining = list(clipped)
    last_page = ""

    for spec in eligible:
        if not remaining:
            break
        # 优先挑与上一条同源的片段,把一致性白捡下来
        same = next((c for c in remaining if c.page_url and c.page_url == last_page), None)
        cand = same or remaining.pop(0)
        if cand is same:
            remaining.remove(cand)
        assignment[spec.idx] = cand
        if cand.page_url:
            last_page = cand.page_url
    return assignment


def collect(*, subject: str, profile: CategoryProfile,
            specs: Sequence[ShotSpec], project_dir: Path,
            page_urls: Sequence[str] | None = None,
            limit: int = DEFAULT_LIMIT, qc: bool = True) -> tuple[
                dict[int, clips_mod.ClipCandidate], dict[str, Any]]:
    """采集、质检并分配真实片段。返回 (镜头序号 → 候选, 报告)。

    全程不抛异常:任何一步失败都只是这个镜头退回生成。
    """
    report: dict[str, Any] = {
        "subject": subject,
        "candidates": 0,
        "fetched": 0,
        "qc_rejected": 0,
        "selected": {},
        "errors": [],
        "sources_tried": [],
        "savings_estimate": 0.0,
        "enabled": True,
    }
    pool, errors = _candidate_pool(subject, page_urls=page_urls)
    report["candidates"] = len(pool)
    report["errors"].extend(errors)
    if not pool:
        report["enabled"] = False
        report["summary"] = "没有取到任何可用视频片段,全片继续用生成"
        return {}, report

    report["sources_tried"].append(f"pool:{len(pool)}")
    seconds = float(config.SHOT_GRID_SECONDS)
    clip_dir = project_dir / "clips"
    work_dir = project_dir / "_clips"

    clipped: list[clips_mod.ClipCandidate] = []
    for cand in pool:
        if len(clipped) >= limit:
            break
        try:
            dest = clip_dir / f"src_{len(clipped):02d}.mp4"
            clipped.append(_materialise(cand, dest, seconds))
            report["fetched"] += 1
        except Exception as exc:  # noqa: BLE001
            report["errors"].append(f"片段抓取失败({cand.url[:50]}):{str(exc)[:160]}")

    if qc:
        before = len(clipped)
        clipped = _qc(clipped, work_dir, report["errors"])
        report["qc_rejected"] = before - len(clipped)

    assignment = _assign(specs, clipped)
    for idx, cand in assignment.items():
        report["selected"][str(idx)] = {
            "path": cand.local_path,
            "origin": cand.origin,
            "title": cand.title,
            "license": cand.license,
            "author": cand.author,
            "page_url": cand.page_url,
        }

    # 真实片段省下的是成片档那一次生成
    try:
        from .. import costs

        report["savings_estimate"] = round(
            costs.savings_from_clips(len(assignment)), 4
        )
    except Exception:  # noqa: BLE001
        report["savings_estimate"] = 0.0

    report["summary"] = (
        f"{len(assignment)} 个镜头改用真实视频片段"
        f"(候选 {report['candidates']} 条,抓取成功 {report['fetched']} 条)"
    )
    return assignment, report


def apply_clips(project_id: str, assignment: dict[int, clips_mod.ClipCandidate]) -> int:
    """把片段写进 shots 表并落盘到标准成片目录。返回实际生效的镜头数。

    刻意不碰一致性链的排布:片段仍留在原来的链位,只是这一格不再需要
    生成。这样 `materialize_first_frames` 仍会从前驱抽帧 —— 而前驱若是
    真实片段,抽出来的就是**真实车尾帧**,恰恰是最好的锚点。
    """
    applied = 0
    project_dir = config.project_dir(project_id)
    shots_dir = project_dir / "shots"
    shots_dir.mkdir(parents=True, exist_ok=True)

    for idx, cand in sorted(assignment.items()):
        src = Path(cand.local_path)
        if not src.is_file():
            continue
        dest = shots_dir / f"{idx:03d}.mp4"
        try:
            if src.resolve() != dest.resolve():
                dest.write_bytes(src.read_bytes())
        except OSError as exc:
            db.add_event(project_id, "pending", src="assets",
                         reason=f"真实片段落盘失败(镜头 {idx}):{exc}")
            continue

        db.update_shot(
            project_id, idx, "final",
            status="succeeded", final_path=str(dest),
            provider_task_id=None, error=None, attempts=0,
            est_cost=0.0, consistency="real",
        )
        db.add_event(
            project_id, "succeeded", src="assets",
            reason=f"镜头 {idx} 使用真实视频片段({cand.origin})",
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
