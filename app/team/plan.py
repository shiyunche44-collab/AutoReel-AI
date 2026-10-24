"""选题闸:用户对选题单的批 / 驳 / 改,以及选题 → 建项目参数。"""
from __future__ import annotations

from typing import Any

from .. import config, db
from ..categories import load_profile
from ..models import ProjectCreate, SubjectEntry

STATUS_LABEL = {
    "proposed": "待批", "approved": "已批", "rejected": "已驳回", "expired": "过期未批",
    "producing": "制作中", "review": "成片待审", "scheduled": "已排发布", "published": "已发布",
    "failed": "失败",
}
#: 用户还能改的状态
EDITABLE = ("proposed", "approved")
#: 页面可改的字段
PATCHABLE = ("subject", "angle", "theme", "aspect", "duration", "hook", "slot_at", "note")


class PlanError(ValueError):
    pass


def _get(item_id: str) -> dict[str, Any]:
    item = db.get_plan_item(item_id)
    if not item:
        raise PlanError("选题不存在")
    return item


def approve(item_id: str) -> dict[str, Any]:
    if not db.cas_plan_status(item_id, ("proposed",), "approved"):
        raise PlanError(f"这条选题现在是「{STATUS_LABEL.get(_get(item_id)['status'])}」,不能批")
    return _get(item_id)


def reject(item_id: str, note: str = "") -> dict[str, Any]:
    if not db.cas_plan_status(item_id, EDITABLE, "rejected", note=note[:200]):
        raise PlanError(f"这条选题现在是「{STATUS_LABEL.get(_get(item_id)['status'])}」,不能驳回")
    return _get(item_id)


def approve_all(category: str) -> int:
    return sum(1 for i in db.list_plan_items(category, statuses=("proposed",), limit=500)
               if db.cas_plan_status(i["id"], ("proposed",), "approved"))


def patch(item_id: str, fields: dict[str, Any]) -> dict[str, Any]:
    """改选题。主题 / 画幅 / 时长要合品类配置,不合直接拒(别等建项目时才炸)。"""
    item = _get(item_id)
    if item["status"] not in EDITABLE:
        raise PlanError("已开工的选题不能改")
    fields = {k: v for k, v in fields.items() if k in PATCHABLE and v is not None}
    merged = {**item, **fields}
    profile = load_profile(item["category"])
    if merged["theme"] not in profile.themes:
        raise PlanError(f"没有内容方向「{merged['theme']}」")
    if merged["aspect"] not in config.ASPECTS:
        raise PlanError(f"不支持的画幅 {merged['aspect']}")
    theme = profile.theme_spec(merged["theme"])
    allowed = profile.theme_durations(theme, merged["aspect"])
    if int(merged["duration"]) not in allowed:
        raise PlanError(f"「{theme.label}」{merged['aspect']} 可选时长:{list(allowed)}")
    if theme.requires_angle and not str(merged.get("angle") or "").strip():
        raise PlanError(f"「{theme.label}」必须有角度")
    if "subject" in fields and not str(fields["subject"]).strip():
        raise PlanError("车型不能为空")
    if "duration" in fields:
        fields["duration"] = int(fields["duration"])
    if "slot_at" in fields:
        fields["slot_at"] = float(fields["slot_at"])
    db.update_plan_item(item_id, **fields)
    return _get(item_id)


def to_payload(item: dict[str, Any]) -> ProjectCreate:
    """选题 → 建项目参数。和首页点热点选题卡片填进去的一样。"""
    subjects = item.get("subjects") or []
    return ProjectCreate(
        subject=item["subject"],
        category=item["category"],
        theme=item["theme"] or None,
        duration=int(item["duration"]),
        aspect=item["aspect"],
        angle=(item.get("angle") or "")[:120] or None,
        angle_search=(item.get("search_terms") or "")[:60] or None,
        angle_evidence=(item.get("evidence") or [])[:12] or None,
        hook=(item.get("hook") or "")[:40] or None,
        subjects=[SubjectEntry(name=s[:40]) for s in subjects] or None,
    )


def view(item: dict[str, Any]) -> dict[str, Any]:
    """页面用:补状态中文名、项目进度。"""
    out = {**item, "status_label": STATUS_LABEL.get(item["status"], item["status"])}
    if item.get("project_id"):
        project = db.get_project(item["project_id"])
        out["project_state"] = project.get("state") if project else ""
    if item["status"] == "review":
        from .publisher import review_view

        out["review"] = review_view(item)
    return out
