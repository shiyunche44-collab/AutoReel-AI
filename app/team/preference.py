"""你的审核偏好:选题闸 / 成片闸上的每次表态记下来,学成主编排序乘数和「你的偏好」几条话。

- **通过率乘数**:按车型 / 片型统计你批的比例,往全局通过率收缩,n≥3 才算,夹在 0.7–1.3,
  主编排选题时乘上(和打法手册权重一起)。页面上能整体关掉。
- **你的偏好**:每周复盘时 LLM 把驳回理由、改动、改标题归纳成 3–5 条短句,存 team_settings。
  页面上能改、能停用、能自己加;停用的和手写的复盘不动。注入主编提示和写脚本提示。
"""
from __future__ import annotations

import logging
import time
from typing import Any

from .. import db
from ..models import new_id

log = logging.getLogger(__name__)

PREF_KEY = "preferences"
RATE_KEY = "preference_rate"
#: 排序乘数看哪些维度(主编排序那一刻只知道车型和片型)
RATE_DIMENSIONS = ("subject", "theme")
#: 收缩强度:相当于先验里有几条「按全局通过率」的表态
PRIOR = 4
MIN_N = 3
RATE_RANGE = (0.7, 1.3)
WINDOW_DAYS = 60
#: 归纳偏好:看近多少天、至少几条有话可说的表态
SUMMARY_DAYS = 30
SUMMARY_MIN = 3
MAX_ITEMS = 5
TEXT_MAX = 60

FIELD_LABEL = {"subject": "车型", "angle": "角度", "theme": "片型", "aspect": "画幅",
               "duration": "时长", "hook": "开场", "slot_at": "排期", "note": "备注"}


# --------------------------------------------------------------------------
# 记录
# --------------------------------------------------------------------------
def features_of(item: dict[str, Any], stage: str) -> dict[str, str]:
    """表态那一刻这条的样子。成片闸再带上项目的生产旋钮(开头 / 结尾 / 视觉包 / 节奏…)。"""
    from .features import KNOB_DIMENSIONS, duration_bucket

    out = {"subject": str(item.get("subject") or ""), "theme": str(item.get("theme") or ""),
           "duration": duration_bucket(item.get("duration")),
           "aspect": str(item.get("aspect") or ""), "kind": str(item.get("kind") or "")}
    if stage == "cut" and item.get("project_id"):
        from ..pipeline import variety

        pv = variety.parse((db.get_project(item["project_id"]) or {}).get("variant"))
        for k in ("opening", "ending", "look", *KNOB_DIMENSIONS):
            if pv.get(k):
                out[k] = str(pv[k])
    return {k: v for k, v in out.items() if v}


def record(item: dict[str, Any] | None, stage: str, decision: str, *, note: str = "",
           title_from: str = "", title_to: str = "") -> None:
    """记一次表态。记不上不影响批 / 驳本身。"""
    if not item:
        return
    try:
        db.add_review_feedback({
            "item_id": item["id"], "stage": stage, "decision": decision,
            "note": (note or "").strip()[:200], "title_from": title_from[:120],
            "title_to": title_to[:120], "features": features_of(item, stage)})
    except Exception as exc:  # noqa: BLE001
        log.warning("审核表态没记上:%s", exc)


def edit_note(before: dict[str, Any], fields: dict[str, Any]) -> str:
    """选题被你改了哪些:「车型 A → B;角度 … → …」。排期挪动不算口味,不记。"""
    parts = []
    for k, v in fields.items():
        if k in ("slot_at", "note") or str(before.get(k) or "") == str(v or ""):
            continue
        parts.append(f"{FIELD_LABEL.get(k, k)}「{before.get(k) or ''}」→「{v}」")
    return ";".join(parts)


# --------------------------------------------------------------------------
# 通过率乘数
# --------------------------------------------------------------------------
def rate_on() -> bool:
    return bool(db.get_team_setting(RATE_KEY, True))


def rates(now: float | None = None) -> dict[str, Any]:
    """{base: {n, rate}, dims: {维度: {取值: {n, approved, mult}}}}。只看批 / 驳,改动不算。"""
    now = now or time.time()
    votes = [r for r in db.list_review_feedback(since=now - WINDOW_DAYS * 86400, limit=5000)
             if r["decision"] in ("approve", "reject")]
    n = len(votes)
    ok = sum(r["decision"] == "approve" for r in votes)
    base = ok / n if n else 0.0
    dims: dict[str, dict[str, dict[str, Any]]] = {}
    for r in votes:
        for d in RATE_DIMENSIONS:
            v = r["features"].get(d)
            if not v:
                continue
            cell = dims.setdefault(d, {}).setdefault(v, {"n": 0, "approved": 0})
            cell["n"] += 1
            cell["approved"] += r["decision"] == "approve"
    lo, hi = RATE_RANGE
    for cells in dims.values():
        for cell in cells.values():
            mult = 1.0
            if base > 0 and cell["n"] >= MIN_N:
                shrunk = (cell["approved"] + PRIOR * base) / (cell["n"] + PRIOR)
                mult = round(min(hi, max(lo, shrunk / base)), 3)
            cell["mult"] = mult
    return {"base": {"n": n, "rate": round(base, 3) if n else None}, "dims": dims}


def multiplier(values: dict[str, str], table: dict[str, Any] | None = None) -> float:
    """这条选题的通过率乘数:各维度乘数的几何平均(没表态过的维度跳过),关掉时 = 1。"""
    if table is None:
        if not rate_on():
            return 1.0
        table = rates()
    ms = [table["dims"].get(d, {}).get(v, {}).get("mult", 1.0)
          for d, v in values.items() if v and d in RATE_DIMENSIONS]
    ms = [m for m in ms if m != 1.0]
    if not ms:
        return 1.0
    prod = 1.0
    for m in ms:
        prod *= m
    return round(prod ** (1 / len(ms)), 3)


# --------------------------------------------------------------------------
# 你的偏好
# --------------------------------------------------------------------------
def items() -> list[dict[str, Any]]:
    return [dict(x) for x in db.get_team_setting(PREF_KEY, []) or [] if x.get("text")]


def _save(rows: list[dict[str, Any]]) -> None:
    db.set_team_setting(PREF_KEY, rows)


def add(text: str) -> dict[str, Any]:
    text = text.strip()[:TEXT_MAX]
    if not text:
        raise ValueError("偏好不能为空")
    row = {"id": new_id("pf_"), "text": text, "status": "active", "source": "manual",
           "updated_at": time.time()}
    _save(items() + [row])
    return row


def update(pref_id: str, *, text: str | None = None, status: str | None = None) -> dict[str, Any]:
    rows = items()
    for row in rows:
        if row["id"] != pref_id:
            continue
        if text is not None:
            if not text.strip():
                raise ValueError("偏好不能为空")
            row["text"] = text.strip()[:TEXT_MAX]
            row["source"] = "manual"  # 你改过的就是你的话了,复盘不再覆盖
        if status is not None:
            if status not in ("active", "off"):
                raise ValueError("状态只能是 active / off")
            row["status"] = status
        row["updated_at"] = time.time()
        _save(rows)
        return row
    raise ValueError("没有这条偏好")


def remove(pref_id: str) -> None:
    rows = items()
    left = [r for r in rows if r["id"] != pref_id]
    if len(left) == len(rows):
        raise ValueError("没有这条偏好")
    _save(left)


def prompt_text() -> str:
    """生效中的偏好,给主编 / 写脚本的提示词。没有 → 空串。"""
    act = [r["text"] for r in items() if r.get("status") == "active"]
    if not act:
        return ""
    return "审核人的偏好(照这个来,别违背):\n" + "\n".join(f"- {t}" for t in act)


SUMMARY_SYSTEM = """你在帮一个汽车讲解短视频账号的编辑团队,总结审核人的口味。
下面是审核人最近在选题 / 成片两道闸上驳回的理由、对选题的改动、发布前对标题的改写。
归纳成 3–5 条**可执行的偏好**,每条一句话、不超过 30 字,写成给编辑和写脚本的人看的指令
(例:「少排纯参数对比,多讲买车决策」「标题别用问号结尾」)。
只总结记录里反复出现或很明确的意思,别编;已停用的意思不要再写出来;和已有手写偏好重复的不写。
输出 JSON:{"preferences": ["...", "..."]}"""


def _evidence(now: float) -> list[str]:
    lines = []
    for r in db.list_review_feedback(since=now - SUMMARY_DAYS * 86400, limit=200):
        what = r["features"].get("subject", "")
        stage = "成片" if r["stage"] == "cut" else "选题"
        if r["decision"] == "reject" and r["note"]:
            lines.append(f"[{stage}驳回 {what}] {r['note']}")
        elif r["decision"] == "edit" and r["note"]:
            lines.append(f"[改选题 {what}] {r['note']}")
        if r["title_to"] and r["title_to"] != r["title_from"]:
            lines.append(f"[改标题] 「{r['title_from']}」→「{r['title_to']}」")
    return lines


def summarize(llm: Any, now: float | None = None) -> list[dict[str, Any]] | None:
    """每周复盘时调:重新归纳 LLM 那几条,手写的和停用的原样留着。没料 / 没 LLM → None。"""
    if llm is None:
        return None
    now = now or time.time()
    lines = _evidence(now)
    if len(lines) < SUMMARY_MIN:
        return None
    rows = items()
    keep = [r for r in rows if r.get("source") == "manual" or r.get("status") == "off"]
    user = "记录:\n" + "\n".join(f"- {x}" for x in lines[:60])
    if keep:
        user += "\n\n已有手写偏好 / 已停用的(别重复):\n" + "\n".join(
            f"- {r['text']}{'(已停用)' if r.get('status') == 'off' else ''}" for r in keep)
    try:
        data = llm.structured(SUMMARY_SYSTEM, user, required_keys=["preferences"], temperature=0.2)
    except Exception as exc:  # noqa: BLE001
        log.warning("偏好归纳失败:%s", exc)
        return None
    known = {r["text"] for r in keep}
    fresh = []
    for t in data.get("preferences") or []:
        t = str(t).strip()[:TEXT_MAX]
        if t and t not in known:
            known.add(t)
            fresh.append({"id": new_id("pf_"), "text": t, "status": "active", "source": "llm",
                          "updated_at": now})
    fresh = fresh[:MAX_ITEMS]
    old = {r["text"] for r in rows if r.get("source") == "llm" and r.get("status") != "off"}
    _save(keep + fresh)
    if {r["text"] for r in fresh} != old:
        db.add_iteration_log("preference", f"你的偏好更新:归纳出 {len(fresh)} 条",
                             {"items": [r["text"] for r in fresh], "evidence": len(lines)}, ts=now)
    return fresh


# --------------------------------------------------------------------------
# 页面
# --------------------------------------------------------------------------
def overview(now: float | None = None) -> dict[str, Any]:
    from .features import DIMENSION_LABEL, value_label

    table = rates(now)
    cells = [{"dimension": d, "label": f"{DIMENSION_LABEL.get(d, d)}:{value_label(d, v)}",
              **c} for d, vs in table["dims"].items() for v, c in vs.items()]
    cells = sorted((c for c in cells if c["mult"] != 1.0), key=lambda c: -abs(c["mult"] - 1))
    return {"items": items(), "rate_on": rate_on(), "base": table["base"], "rates": cells[:8]}
