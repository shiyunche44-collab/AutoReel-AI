"""⑥⑦ 生成调度器:分层生成 + 一致性链 + 轮询 + 配额节流。

调度模型是**轮询式**的:每次 `tick()` 做四件事,反复调用即可推进整个项目。
这样做的原因有两个:
1. H3 单任务 15-30 分钟,任何"同步等结果"的写法都会让 FastAPI 请求超时;
2. 状态全在 SQLite,进程重启后继续 tick 即可,不会重复提交(提交前检查
   `provider_task_id` 非空就跳过),满足"不重复消耗配额"的验收标准。

一致性链的约束在这里落地:每条链同时只放行一个镜头,前驱完成后才放行下一个。
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence

from .. import config, costs, db
from ..models import CompiledPrompt, Shot, ShotSpec
from ..pipeline import chains as chains_mod
from ..providers import video

POLL_INTERVAL = video.POLL_INTERVAL


@dataclass
class TickResult:
    """一次 tick 的结果,供 Web 层展示。"""

    submitted: list[int] = field(default_factory=list)
    completed: list[int] = field(default_factory=list)
    failed: list[int] = field(default_factory=list)
    polled: int = 0
    skipped_degraded: list[int] = field(default_factory=list)
    #: 因所选模型不支持参考图而被摘掉参考图的镜头。
    #: 草稿档(Hailuo-2.3)必然命中:一致性靠链内尾帧接首帧,不靠参考图集。
    degraded_refs: list[int] = field(default_factory=list)
    #: 因所选模型实测不支持图生视频而被摘掉链内首帧的镜头。
    degraded_frames: list[int] = field(default_factory=list)
    paused_budget: bool = False
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "submitted": self.submitted,
            "completed": self.completed,
            "failed": self.failed,
            "polled": self.polled,
            "skipped_degraded": self.skipped_degraded,
            "degraded_refs": self.degraded_refs,
            "degraded_frames": self.degraded_frames,
            "paused_budget": self.paused_budget,
            "reason": self.reason,
        }


def _stage_dir(project_id: str, stage: str) -> Path:
    return config.project_dir(project_id) / ("drafts" if stage == "draft" else "shots")


def _prompt_path(project_id: str, stage: str, idx: int) -> Path:
    return config.project_dir(project_id) / "prompts" / f"{stage}_{idx:03d}.txt"


def _model_for(stage: str) -> str:
    return config.DRAFT_MODEL if stage == "draft" else config.FINAL_MODEL


def _resolution_for(stage: str) -> str:
    return "768P" if stage == "draft" else config.FINAL_RESOLUTION


def _retry_cap(stage: str) -> int:
    return config.MAX_RETRY_DRAFT if stage == "draft" else config.MAX_RETRY_FINAL


# --------------------------------------------------------------------------
# 初始化
# --------------------------------------------------------------------------
#: 重新初始化时保留的状态:已完成的,以及已提交、还在跑的(task_id 在,重置会丢)
_KEEP_ON_REINIT = ("succeeded", "submitted", "queued", "running")


def initialise_shots(
    project_id: str,
    specs: Sequence[ShotSpec],
    *,
    stage: str = "draft",
    ref_limit: int = 0,
) -> list[dict[str, Any]]:
    """把分镜表写入 shots 表(运行态)。已存在的记录保留其生成状态。

    ⚠️ "保留"必须在这里显式做:`upsert_shots` 的冲突分支会用新值覆盖 status。
    门禁 B 每放行一批都会调用本函数,不保留的话前几批已经生成好的镜头
    (以及换成真实素材的镜头)会被打回 pending 再提交一遍,白烧额度。
    """
    shot_chains = chains_mod.build_chains(len(specs))
    mapping = chains_mod.assign_to_shots(shot_chains)
    existing = {r["idx"]: r for r in db.get_shots(project_id, stage)}
    rows: list[dict[str, Any]] = []
    for spec in specs:
        old = existing.get(spec.idx)
        # 失败的镜头照旧重置 —— 重新初始化本来就隐含"再试一次"的语义
        if old is not None and old.get("status") in _KEEP_ON_REINIT:
            chain_id, chain_pos = mapping.get(spec.idx, (0, 0))
            rows.append({**old, "chain_id": chain_id, "chain_pos": chain_pos})
            continue
        chain_id, chain_pos = mapping.get(spec.idx, (0, 0))
        est = costs.estimate_shot(
            stage=stage, duration=spec.duration, ref_image_count=ref_limit
        )
        rows.append(
            {
                "project_id": project_id,
                "idx": spec.idx,
                "stage": stage,
                "status": "pending",
                "model": _model_for(stage),
                "provider_task_id": None,
                "submitted_at": None,
                "last_polled_at": None,
                "draft_path": None,
                "final_path": None,
                "attempts": 0,
                "error": None,
                "consistency": "ok",
                "chain_id": chain_id,
                "chain_pos": chain_pos,
                "est_cost": est,
            }
        )
    db.upsert_shots(project_id, rows)
    return rows


def save_prompt(project_id: str, stage: str, idx: int, prompt: str) -> Path:
    path = _prompt_path(project_id, stage, idx)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(prompt, encoding="utf-8")
    return path


def load_prompt(project_id: str, stage: str, idx: int) -> str:
    path = _prompt_path(project_id, stage, idx)
    return path.read_text(encoding="utf-8") if path.is_file() else ""


# --------------------------------------------------------------------------
# 轮询
# --------------------------------------------------------------------------
def _due_for_poll(row: dict[str, Any], now: float) -> bool:
    last = row.get("last_polled_at")
    if not last:
        return True
    interval = POLL_INTERVAL.get(row.get("model") or "", 30)
    return now - last >= interval


def _within_deadline(row: dict[str, Any], now: float) -> bool:
    submitted = row.get("submitted_at")
    if not submitted:
        return True
    return now - submitted <= config.POLL_TIMEOUT_SECONDS


def _download_artifact(row: dict[str, Any], status: video.VideoStatus) -> str | None:
    project_id = row["project_id"]
    idx = row["idx"]
    stage = row["stage"]
    dest = _stage_dir(project_id, stage) / f"{idx:03d}.mp4"
    if dest.is_file() and dest.stat().st_size > 0:
        return str(dest)
    if not status.video_url:
        return None
    try:
        return str(video.download(status.video_url, dest))
    except Exception as exc:  # noqa: BLE001 - 下载失败标记失败,由用户重试
        raise RuntimeError(f"下载成片失败:{exc}") from exc


def poll_pending(project_id: str, stage: str) -> TickResult:
    """轮询所有在途任务,落盘成功产物。"""
    result = TickResult()
    now = time.time()
    rows = db.get_shots(project_id, stage)

    for row in rows:
        if row["status"] not in ("submitted", "queued", "running"):
            continue
        if not row.get("provider_task_id"):
            continue
        if not _within_deadline(row, now):
            db.update_shot(
                project_id, row["idx"], stage, status="failed",
                error=f"轮询超过 {config.POLL_TIMEOUT_SECONDS}s 未返回,已标记超时",
            )
            result.failed.append(row["idx"])
            continue
        if not _due_for_poll(row, now):
            continue

        result.polled += 1
        db.update_shot(project_id, row["idx"], stage, last_polled_at=now)
        try:
            status = video.query(row["provider_task_id"], row["model"])
        except Exception as exc:  # noqa: BLE001 - 单次查询失败不终止 tick
            db.update_shot(project_id, row["idx"], stage, error=f"查询失败:{exc}")
            continue

        if status.is_success:
            try:
                path = _download_artifact(row, status)
            except RuntimeError as exc:
                db.update_shot(project_id, row["idx"], stage, status="failed", error=str(exc))
                result.failed.append(row["idx"])
                continue
            if path:
                field = "draft_path" if stage == "draft" else "final_path"
                db.update_shot(
                    project_id, row["idx"], stage, status="succeeded", error=None, **{field: path}
                )
                result.completed.append(row["idx"])
        elif status.status == "failed":
            db.update_shot(
                project_id, row["idx"], stage, status="failed",
                error=status.error or "provider 返回失败",
            )
            result.failed.append(row["idx"])
        elif status.status == "cancelled":
            db.update_shot(project_id, row["idx"], stage, status="failed", error="任务被取消")
            result.failed.append(row["idx"])
        else:
            db.update_shot(project_id, row["idx"], stage, status=status.status)
    return result


# --------------------------------------------------------------------------
# 提交
# --------------------------------------------------------------------------
def submit_ready(
    project_id: str,
    stage: str,
    *,
    approved: set[int] | None = None,
    ref_urls: Sequence[str] | None = None,
) -> TickResult:
    """按一致性链规则提交当前可生成的镜头。"""
    result = TickResult()
    project = db.get_project(project_id)
    if not project:
        result.reason = "项目不存在"
        return result

    rows = db.get_shots(project_id, stage)
    if not rows:
        result.reason = "尚未初始化镜头"
        return result

    done = {r["idx"] for r in rows if r["status"] == "succeeded"}
    in_flight = {
        r["idx"] for r in rows if r["status"] in ("submitted", "queued", "running")
    }
    chains = chains_mod.chains_for([r["idx"] for r in rows])
    candidates = chains_mod.ready_shots(chains, done | in_flight, approved)

    # 冒烟测试模式:只允许前 N 个镜头进入生成。
    # 注意不能只"取前 N 个候选"——链内是串行的,必须按镜头序号过滤,
    # 否则第 2、3 个镜头永远等不到前驱,阶段永远判不完。
    if config.SHOT_LIMIT:
        candidates = [c for c in candidates if c < config.SHOT_LIMIT]

    if not candidates:
        return result

    project_dir = config.project_dir(project_id)
    ref_limit = config.CATEGORY_REF_LIMIT
    budget_limit = float(project.get("quota_budget") or config.DEFAULT_QUOTA_BUDGET)
    spent = float(project.get("spent_cost") or 0.0)
    retry_cap = _retry_cap(stage)
    model = _model_for(stage)
    resolution = _resolution_for(stage)

    # 已完成镜头 → 尾帧,供链中镜头作首帧。
    # 数据卡片除外:它的尾帧是一张图表,拿去当下一镜的首帧,视频模型会照着
    # 图表往下"续拍"。卡片的后继按链首处理(只靠参考图集),不算降级。
    card_idx = {r["idx"] for r in rows if r.get("consistency") == "card"}
    completed_videos = {
        r["idx"]: Path(r[f"{stage}_path"])
        for r in rows
        if r["status"] == "succeeded" and r.get(f"{stage}_path") and r["idx"] not in card_idx
    }
    frame_plan = chains_mod.plan_first_frames(
        chains, stage, project_dir, ref_limit=ref_limit
    )
    for idx, plan in list(frame_plan.items()):
        if plan.get("from_shot") in card_idx:
            frame_plan[idx] = {"path": None, "source": "reference_set" if ref_limit else "none"}
    first_frames = chains_mod.materialize_first_frames(
        frame_plan, chains, completed_videos, stage
    )
    degraded = set(chains_mod.degraded_shots(frame_plan, first_frames))

    for idx in candidates:
        row = next((r for r in rows if r["idx"] == idx), None)
        if not row:
            continue
        if (row.get("attempts") or 0) >= retry_cap:
            continue

        prompt = load_prompt(project_id, stage, idx)
        if not prompt:
            result.reason = f"镜头 {idx} 缺少提示词"
            continue

        est = float(row.get("est_cost") or 0.0)
        # 配额节流:提交前预检
        if spent + est > budget_limit:
            result.paused_budget = True
            result.reason = (
                f"预计累计等效用量 {spent + est:.2f} 将超过项目上限 {budget_limit:.2f},已暂停"
            )
            db.add_event(project_id, "PAUSED_BUDGET", src=project.get("state"),
                         reason=result.reason)
            break

        first_frame = first_frames.get(idx)
        if idx in degraded:
            result.skipped_degraded.append(idx)

        # 按模型能力降级,而不是直接判失败。
        # 草稿档用 Hailuo-2.3,它不吃多模态参考图:强行传参考图只会让每个
        # 链首都以"模型不支持"失败。正确做法是不传参考图 —— 草稿档的一致性
        # 靠链内尾帧接首帧保证,参考图集是成片档(H3)才用的。
        shot_ref_urls: Sequence[str] | None = ref_urls
        if shot_ref_urls and not video.supports_reference_images(model):
            shot_ref_urls = None
            result.degraded_refs.append(idx)

        # 同理,模型实测收不了图时也别递上去:链内首帧只对支持图生的模型有意义。
        if first_frame and not video.supports_image_input(model):
            result.degraded_frames.append(idx)
            first_frame = None

        try:
            video.validate(
                model,
                duration=int(round(row.get("duration") or config.SHOT_GRID_SECONDS)),
                resolution=resolution,
                ref_count=len(shot_ref_urls or ()),
                # 这里传的是**首**帧(前一镜的尾帧派生而来),不是尾帧。
                # 一度写成 first_frame is not None,导致"链内第二镜"被要求
                # 支持尾帧输入 —— 而模型只支持首帧,于是每个非链首镜头
                # 都以"不支持尾帧输入"失败。尾帧只有成片档首尾帧模式才会用。
                has_last_frame=False,
            )
        except video.VideoValidationError as exc:
            db.update_shot(project_id, idx, stage, status="failed", error=str(exc))
            result.failed.append(idx)
            continue

        try:
            task_id = video.submit(
                model=model,
                prompt=prompt,
                duration=int(round(config.SHOT_GRID_SECONDS)),
                resolution=resolution,
                ratio=config.ASPECT_RATIO,
                first_frame=first_frame,
                reference_images=list(shot_ref_urls or ()) or None,
            )
        except Exception as exc:  # noqa: BLE001
            db.update_shot(
                project_id, idx, stage, status="failed", error=f"提交失败:{exc}",
                attempts=(row.get("attempts") or 0) + 1,
            )
            result.failed.append(idx)
            continue

        # 提交即持久化 —— 断点续跑的关键
        now = time.time()
        db.update_shot(
            project_id, idx, stage,
            status="submitted", model=model, provider_task_id=task_id,
            submitted_at=now, error=None,
            attempts=(row.get("attempts") or 0) + 1,
            consistency="degraded" if idx in degraded else "ok",
        )
        spent += est
        db.update_project(project_id, spent_cost=round(spent, 4))
        db.add_event(
            project_id, "submitted", src="pending",
            reason=f"镜头 {idx} 提交 {model} ({stage})", cost=est,
        )
        result.submitted.append(idx)

    return result


# --------------------------------------------------------------------------
# 组合
# --------------------------------------------------------------------------
def tick(project_id: str, stage: str, *, approved: set[int] | None = None,
         ref_urls: Sequence[str] | None = None) -> TickResult:
    """一次完整调度:先轮询在途任务,再提交新任务。"""
    polled = poll_pending(project_id, stage)
    submitted = submit_ready(project_id, stage, approved=approved, ref_urls=ref_urls)

    combined = TickResult(
        submitted=polled.submitted + submitted.submitted,
        completed=polled.completed,
        failed=polled.failed + submitted.failed,
        polled=polled.polled,
        skipped_degraded=polled.skipped_degraded + submitted.skipped_degraded,
        degraded_refs=polled.degraded_refs + submitted.degraded_refs,
        degraded_frames=polled.degraded_frames + submitted.degraded_frames,
        paused_budget=submitted.paused_budget,
        reason=submitted.reason or polled.reason,
    )
    return combined


def stage_complete(project_id: str, stage: str, approved: set[int] | None = None) -> bool:
    rows = db.get_shots(project_id, stage)
    if not rows:
        return False
    targets = approved if approved is not None else {r["idx"] for r in rows}
    # 冒烟测试模式:SHOT_LIMIT 之外的镜头不参与"本阶段是否跑完"的判定,
    # 否则它们永远停在 pending,阶段永远不结束。
    if config.SHOT_LIMIT:
        targets &= {i for i in targets if i < config.SHOT_LIMIT}
    if not targets:
        return False
    for row in rows:
        if row["idx"] not in targets:
            continue
        if row["status"] != "succeeded":
            return False
    return True


def retry_shot(project_id: str, idx: int, stage: str) -> bool:
    """人工触发单镜头重拍:清空任务状态与产物,重新排队。"""
    art = _stage_dir(project_id, stage) / f"{idx:03d}.mp4"
    if art.is_file():
        try:
            art.unlink()
        except OSError:
            pass
    field = "draft_path" if stage == "draft" else "final_path"
    db.update_shot(
        project_id, idx, stage,
        status="pending", provider_task_id=None, submitted_at=None,
        last_polled_at=None, error=None, **{field: None},
    )
    db.add_event(project_id, "pending", src="failed",
                 reason=f"人工重拍 镜头 {idx} ({stage})")
    return True


def approved_indices(project_id: str, stage: str) -> set[int]:
    return {
        r["idx"] for r in db.get_shots(project_id, stage) if r["status"] == "succeeded"
    }


def degraded_report(project_id: str, stage: str) -> list[int]:
    return [
        r["idx"] for r in db.get_shots(project_id, stage)
        if r.get("consistency") == "degraded"
    ]
