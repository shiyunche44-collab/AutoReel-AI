"""分发官:成片进了成片闸,顺带出小红书图文笔记 + 1–2 条竖版切片,和主片一起审。

**产物**见 `pipeline/repurpose.py`(全取自已有成片 / 卡片 / 事实,不花生成额度)。
这里管三件事:
- 找成片待审、还没出衍生品的选题,一轮只做一条(切片要跑 ffmpeg);
- 成片闸卡片上把衍生品摆出来:笔记的图和正文、切片预览、各自发到哪些账号;
- 主片通过时,勾了的衍生品一起建发布任务:笔记比主片晚 `NOTE_DELAY_HOURS`,
  切片一天一条错开(同号连发相近内容容易被判重复)。

衍生品不进复盘官的统计、也不进限流判断(`is_main`)—— 切片播放天然比主片低,
算进去会把账号中位数拉低,还可能误判限流。
"""
from __future__ import annotations

import logging
from typing import Any

from .. import config, db
from . import Skip, TeamError
from .plan import PlanError

log = logging.getLogger(__name__)

#: 笔记能发的平台
NOTE_PLATFORMS = ("xiaohongshu",)
#: 切片能发的平台(竖屏为主的)
CLIP_PLATFORMS = ("douyin", "xiaohongshu", "channels")
#: 笔记比主片晚几小时
NOTE_DELAY_HOURS = 3
#: 第 k 条切片在主片之后 k 天
CLIP_GAP_DAYS = 1


def is_main(pub: dict[str, Any]) -> bool:
    return not pub.get("piece")


def media_url(project_id: str, name: str) -> str:
    return f"/api/projects/{project_id}/repurpose/{name.split('/')[-1]}"


def pending(limit: int = 20) -> list[dict[str, Any]]:
    """成片待审、成片文件在、还没出衍生品的选题。"""
    from ..pipeline import aspect, repurpose

    out = []
    for item in db.list_plan_items(statuses=("review",), limit=200):
        pid = item.get("project_id")
        if pid and repurpose.load(pid) is None and aspect.output_path(pid).is_file():
            out.append(item)
            if len(out) >= limit:
                break
    return out


def run(*, llm: Any = None) -> str:
    from ..pipeline import orchestrator, repurpose

    todo = pending(limit=1)
    if not todo:
        raise Skip("没有待出衍生品的成片")
    item = todo[0]
    llm = llm if llm is not None else orchestrator._llm()
    try:
        manifest = repurpose.build(item["project_id"], llm=llm)
    except repurpose.RepurposeError as exc:
        raise TeamError(f"{item['subject']}:{exc}") from exc
    note = manifest.get("note")
    parts = []
    if note:
        parts.append(f"笔记 {len(note['images'])} 张图" + ("(有红项)" if note["problems"] else ""))
    if manifest["clips"]:
        parts.append(f"{len(manifest['clips'])} 条切片")
    if not parts:
        raise TeamError(f"{item['subject']}:一件都没出成:" + ";".join(manifest["errors"])[:200])
    tail = f";{len(manifest['errors'])} 件失败" if manifest["errors"] else ""
    return f"{item['subject']}:" + "、".join(parts) + tail


# --------------------------------------------------------------------------
# 成片闸
# --------------------------------------------------------------------------
def _accounts_for(platforms: tuple[str, ...], default: bool) -> list[dict[str, Any]]:
    from . import publisher

    return [{**a, "default": default and not a["paused"]}
            for a in publisher.accounts() if a["platform"] in platforms]


def view(item: dict[str, Any]) -> dict[str, Any]:
    """成片闸卡片上的衍生品。`ready=False` 表示分发官还没轮到这条。"""
    from ..pipeline import aspect, repurpose

    pid = item["project_id"]
    manifest = repurpose.load(pid)
    if manifest is None:
        return {"ready": False, "pieces": []}
    pieces = []
    note = manifest.get("note")
    if note:
        pieces.append({
            "id": "note", "kind": "note", "label": "小红书图文",
            "title": note["title"], "body": note["body"], "tags": note["tags"],
            "images": [media_url(pid, n) for n in note["images"]],
            "problems": note["problems"], "dropped": note.get("dropped") or [],
            "accounts": _accounts_for(NOTE_PLATFORMS, not note["problems"]),
        })
    # 竖屏主片的切片发同一个抖音号是自己抄自己,默认不勾;横屏主片转竖版才默认发抖音
    portrait = aspect.is_portrait(db.get_project(pid))
    for clip in manifest.get("clips") or []:
        accs = _accounts_for(CLIP_PLATFORMS, False)
        for a in accs:
            a["default"] = not portrait and a["platform"] == "douyin" and not a["paused"]
        pieces.append({
            "id": clip["id"], "kind": "clip", "label": f"竖版切片 {clip['id'][-1]}",
            "title": clip["title"], "why": clip["why"], "duration": clip["duration"],
            "chapters": clip.get("chapters") or [], "url": media_url(pid, clip["file"]),
            "problems": [], "accounts": accs,
        })
    return {"ready": True, "pieces": pieces, "errors": manifest.get("errors") or []}


def _clip_text(project_id: str, clip: dict[str, Any]) -> str:
    from ..publish import service

    ctx = service.build_context(project_id)
    tail = "\n".join(ctx.disclaimers)
    return f"{clip['title']}。完整版看主页。" + (f"\n{tail}" if tail else "")


def approve(item: dict[str, Any], picks: dict[str, list[str]], main_at: float, *,
            dry_run: bool = False) -> list[str]:
    """主片通过时一起排衍生品。返回排了哪些(给提示用)。挑了有红项的笔记直接报错。
    `dry_run` 只做校验 —— 发布员在建主片任务之前先查一遍。"""
    from ..pipeline import repurpose
    from ..publish import accounts as accounts_mod
    from ..publish import service

    picks = {k: list(v) for k, v in (picks or {}).items() if v}
    if not picks:
        return []
    pid = item["project_id"]
    manifest = repurpose.load(pid) or {}
    note = manifest.get("note")
    clips = {c["id"]: c for c in manifest.get("clips") or []}
    unknown = [k for k in picks if k != "note" and k not in clips]
    if unknown or ("note" in picks and not note):
        raise PlanError("要发的衍生品不在了,刷新一下")
    if "note" in picks and note["problems"]:
        raise PlanError("图文笔记有红项,不能发:" + ";".join(note["problems"]))
    for piece, ids in picks.items():
        allowed = NOTE_PLATFORMS if piece == "note" else CLIP_PLATFORMS
        for aid in ids:
            acc = accounts_mod.get_account(aid)
            if not acc or acc.platform not in allowed:
                raise PlanError(f"{'笔记' if piece == 'note' else '切片'}不能发到这个账号")
            if (db.get_account_health(aid) or {}).get("paused"):
                raise PlanError("选的账号里有被合规审核官暂停的")
    if dry_run:
        return []
    done = []
    for piece, ids in picks.items():
        if piece == "note":
            at = main_at + NOTE_DELAY_HOURS * 3600
            kw = {"aspect": "3:4", "artifact": note["images"][0], "title": note["title"],
                  "description": note["body"], "tags": note["tags"]}
            label = "图文笔记"
        else:
            clip = clips[piece]
            k = sorted(clips).index(piece) + 1
            at = main_at + k * CLIP_GAP_DAYS * 86400  # 和主片同一时段,隔天发
            kw = {"aspect": config.PORTRAIT, "artifact": clip["file"], "title": clip["title"],
                  "description": _clip_text(pid, clip), "tags": []}
            label = f"切片 {k}"
        n = sum(1 for aid in ids
                if service.enqueue_piece(pid, aid, piece, scheduled_at=at, **kw) is not None)
        if n:
            done.append(f"{label} × {n}")
    if done:
        db.add_event(pid, "team", reason="分发官衍生品已排:" + "、".join(done))
    return done
