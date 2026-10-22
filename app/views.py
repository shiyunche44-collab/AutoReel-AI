"""视图模型层:把库里的行拼成页面与 API 要用的结构。

`app/main.py` 之前把路由、后台调度器、素材库页、发布域全塞在一起,
1200 多行。这里把**视图模型**抽出来 —— 它们是纯函数(读库 → 组结构),
不碰 HTTP:

- `main.py` 只剩路由与调度,职责清楚;
- 视图模型可以脱离 FastAPI 单独测(不用起服务、不用 TestClient);
- 将来要加 CLI 或别的界面,直接复用这一层。
"""
from __future__ import annotations

from typing import Any

from . import config, db
from .categories import load_profile
from .models import (
    Fact,
    ProjectDetail,
    ProjectSummary,
    ScriptSegment,
    Shot,
    ShotSpec,
)


class ProjectNotFound(RuntimeError):
    """项目不存在。

    视图模型层不认识 HTTP —— 由路由把它翻成 404。
    """


#: 需要人处理的状态。
#:
#: 可靠性的一条底线是"失败要可见":项目卡在配额暂停或失败状态时,
#: 不能让人自己翻列表去找 —— 首页要把它顶到最上面。
NEEDS_ATTENTION = ("PAUSED_BUDGET", "FAILED")


def load_specs_from_db(rows: list[dict[str, Any]]) -> list[ShotSpec]:
    """从 storyboard.json 读出这一批拍的规格。"""
    if not rows:
        return []
    project_id = rows[0]["project_id"]
    return [ShotSpec(**d) for d in db.read_json(project_id, "storyboard.json", []) or []]


def shots_to_models(rows: list[dict[str, Any]]) -> list[Shot]:
    """把 shots 表的行拼成带规格的镜头模型。"""
    specs = {s.idx: s for s in load_specs_from_db(rows)}
    shots: list[Shot] = []
    for row in rows:
        spec = specs.get(row["idx"])
        if spec is None:
            continue
        shots.append(
            Shot(
                spec=spec,
                status=row["status"],
                stage=row["stage"],
                model=row.get("model") or "",
                draft_path=row.get("draft_path"),
                final_path=row.get("final_path"),
                attempts=row.get("attempts") or 0,
                error=row.get("error"),
                consistency=row.get("consistency") or "ok",
                est_cost=row.get("est_cost") or 0.0,
            )
        )
    return shots


def theme_label(category: str, theme: str) -> str:
    """方向名 -> 中文标签。未知品类/方向一律返回原值,不阻塞页面渲染。"""
    try:
        profile = load_profile(category)
    except Exception:  # noqa: BLE001 - 列表页不该因单个品类配置问题挂掉
        return theme
    spec = profile.theme_spec(theme or None)
    return spec.label or theme


def build_detail(project_id: str, stage: str = "final") -> ProjectDetail:
    """项目详情视图模型。找不到项目抛 `ProjectNotFound`。"""
    row = db.get_project(project_id)
    if not row:
        raise ProjectNotFound("项目不存在")
    shot_rows = db.get_shots(project_id, stage) or db.get_shots(project_id, "draft")
    project_dir = config.project_dir(project_id)
    output = project_dir / "output.mp4"
    return ProjectDetail(
        id=row["id"],
        title=row["title"],
        subject=row["subject"],
        category=row["category"],
        angle=row.get("angle") or "",
        subjects=db.project_subjects(row),
        duration=row["duration"],
        shot_count=row["shot_count"],
        state=row["state"],
        est_cost=row["est_cost"],
        spent_cost=row["spent_cost"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        has_output=output.is_file(),
        narration_mode=row["narration_mode"],
        quota_budget=row["quota_budget"],
        state_reason=row.get("state_reason"),
        facts=[Fact(**d) for d in db.read_json(project_id, "facts.json", []) or []],
        script=[ScriptSegment(**d) for d in db.read_json(project_id, "script.json", []) or []],
        shots=shots_to_models(shot_rows),
        thesis=row.get("thesis") or "",
    )


def build_summary(row: dict[str, Any]) -> ProjectSummary:
    """项目列表项的视图模型。"""
    project_dir = config.project_dir(row["id"])
    return ProjectSummary(
        id=row["id"],
        title=row["title"],
        subject=row["subject"],
        category=row["category"],
        theme=row.get("theme") or "",
        theme_label=theme_label(row["category"], row.get("theme") or ""),
        angle=row.get("angle") or "",
        subjects=db.project_subjects(row),
        duration=row["duration"],
        shot_count=row["shot_count"],
        state=row["state"],
        state_reason=row.get("state_reason") or "",
        est_cost=row["est_cost"],
        spent_cost=row["spent_cost"],
        quota_budget=row["quota_budget"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        has_output=(project_dir / "output.mp4").is_file(),
    )


def needs_attention(rows: list[dict[str, Any]]) -> list[ProjectSummary]:
    """需要人处理的项目(配额暂停 / 失败)。

    首页把它顶在最上面 —— 卡住的项目不该要人去列表里翻。
    """
    return [build_summary(r) for r in rows if r.get("state") in NEEDS_ATTENTION]
