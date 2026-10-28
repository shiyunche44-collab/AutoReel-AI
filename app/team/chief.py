"""总管:每天早上出一份今日简报,角色连续失败时升级成「等你处理」。

**只汇总、不指挥。** 角色之间的交接靠数据库,流程是确定的,不需要 LLM 派活;
总管只读各角色的运行记录和选题单,告诉你现在整体什么状态、哪里卡着、哪个角色在反复失败。
简报的数字全由规则算出,LLM 最多把它们润色成三五句话,出错或关掉就用模板文字。
"""
from __future__ import annotations

import logging
import time
from typing import Any

from .. import config, db
from . import Skip

log = logging.getLogger(__name__)

BRIEF_KEY = "chief_brief"
#: 还在流程里的选题状态
OPEN = ("proposed", "approved", "producing", "review", "scheduled")
#: 算「有结果」的运行状态(idle / running 不算)
_RESULT = ("ok", "failed", "skipped")

SYSTEM = """你是一个中文汽车短视频账号的团队总管,要给账号主人写今天的简报。

只输出一个 JSON 对象:{"summary": "..."}
- summary:2-4 句话,先说有几件事等主人处理,再说哪里卡着或哪个角色在反复出错,最后说今天在产的几条。
- **所有数字只能逐字来自下面的数据**,不许估算、换算或编;没有的事不要说。不要用营销腔。"""


# --------------------------------------------------------------------------
# 全局仲裁:只拦 / 挪,不改选题内容,不替人过闸。TEAM_CHIEF_ARBITRATE=0 一键关
# --------------------------------------------------------------------------
ACTIONS_KEY = "chief_actions"
#: 在渲染的项目超过这么久没动静,就当它卡死了,不再占名额
RENDER_STALE_HOURS = 3.0
_RENDERING = ("DRAFT_RENDERING", "FINAL_RENDERING", "COMPOSING")
#: 不发的时段:(几点起, 到几点),同 rapid.QUIET
QUIET = (23, 8)
#: 挪时段最多试几次
_MAX_SHIFTS = 24


def arbitrating() -> bool:
    return config.TEAM_CHIEF_ARBITRATE


def _day_start(now: float) -> float:
    import datetime as dt

    return dt.datetime.combine(dt.datetime.fromtimestamp(now).date(), dt.time()).timestamp()


def started_today(now: float | None = None) -> int:
    """今天已经开工的选题数(含已失败的:产能已经花出去了)。"""
    now = now or time.time()
    start = _day_start(now)
    pids = {i["project_id"] for i in db.list_plan_items(limit=500) if i.get("project_id")}
    return sum(1 for pid in pids
               if ((db.get_project(pid) or {}).get("created_at") or 0) >= start)


def admit(items: list[dict[str, Any]], now: float | None = None
          ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """(今天能开工的, 顺延到明天的)。快反不受日产量限制,而且排在前面。"""
    if not arbitrating():
        return items, []
    now = now or time.time()
    ordered = sorted(items, key=lambda i: (i.get("kind") != "rapid", i["slot_at"]))
    room = max(0, config.TEAM_CHIEF_DAILY_MAX - started_today(now))
    ready, later = [], []
    for i in ordered:
        if i.get("kind") == "rapid" or room > 0:
            ready.append(i)
            room -= i.get("kind") != "rapid"
        else:
            later.append(i)
    return ready, later


def rendering_now(now: float | None = None) -> int:
    now = now or time.time()
    cutoff = now - RENDER_STALE_HOURS * 3600
    return sum(1 for p in db.list_projects(limit=200)
               if p.get("state") in _RENDERING and (p.get("updated_at") or 0) >= cutoff)


def render_busy(now: float | None = None) -> bool:
    """调度器的并发名额(PIPELINE_CONCURRENCY)已被占满:再放闸 / 预建素材池只会排队抢资源。"""
    return arbitrating() and rendering_now(now) >= max(1, config.PIPELINE_CONCURRENCY)


def _quiet(ts: float) -> bool:
    import datetime as dt

    h = dt.datetime.fromtimestamp(ts).hour
    return h >= QUIET[0] or h < QUIET[1]


def free_publish_time(at: float, account_ids: list[str], *, rapid: bool = False,
                      subject: str = "", now: float | None = None) -> float:
    """同一账号两条发布至少隔 `TEAM_CHIEF_PUBLISH_GAP` 分钟,撞了就往后挪到空档(跳过深夜)。

    快反不挪(热点时效);它撞上别人时也不去动已排好的,两条都按原时间发。"""
    if not arbitrating() or rapid:
        return at
    gap = config.TEAM_CHIEF_PUBLISH_GAP * 60
    taken = [p["scheduled_at"] for p in db.list_publications(state="pending", limit=500)
             if p["account_id"] in account_ids and p.get("scheduled_at")]
    cand = at
    for _ in range(_MAX_SHIFTS):
        if _quiet(cand):
            import datetime as dt

            d = dt.datetime.fromtimestamp(cand)
            day = d.date() + dt.timedelta(days=1) if d.hour >= QUIET[0] else d.date()
            cand = dt.datetime.combine(day, dt.time(QUIET[1])).timestamp()
            continue
        hit = [t for t in taken if abs(t - cand) < gap]
        if not hit:
            break
        cand = max(hit) + gap
    else:
        return at  # 一天都排不下就别折腾了,按原时间
    if cand != at:
        record(f"发布时段挪动:《{subject or '选题'}》{_hm(at)} 撞了同账号别的发布,改到 {_hm(cand)}",
               {"from": at, "to": cand})
    return cand


def _hm(ts: float) -> str:
    import datetime as dt

    return dt.datetime.fromtimestamp(ts).strftime("%m-%d %H:%M")


def record(text: str, detail: dict[str, Any] | None = None) -> None:
    """仲裁动作留痕:简报里看得到,迭代记录里也有一条。"""
    log.info("chief: %s", text)
    acts = (db.get_team_setting(ACTIONS_KEY, []) or [])[-19:]
    acts.append({"ts": time.time(), "text": text})
    db.set_team_setting(ACTIONS_KEY, acts)
    db.add_iteration_log("param", "总管:" + text, {"source": "chief", **(detail or {})})


def actions(since: float | None = None) -> list[dict[str, Any]]:
    since = time.time() - 86400 if since is None else since
    return [a for a in db.get_team_setting(ACTIONS_KEY, []) or [] if a["ts"] >= since]


def waiting_lines(now: float | None = None) -> list[str]:
    """眼下被仲裁拦着的事(现算,不存):简报里让你知道为什么没动。"""
    if not arbitrating():
        return []
    from . import producer

    now = now or time.time()
    lines = [f"《{i['subject']}》今天产能已满({config.TEAM_CHIEF_DAILY_MAX} 条),顺延到明天开工"
             for i in admit(producer.due(now), now)[1]]
    if render_busy(now):
        lines.append("渲染名额已被占满,新的门禁放行和素材预建先排队")
    return lines


def escalations(limit: int | None = None) -> list[dict[str, Any]]:
    """最近几次有结果的运行全是失败的角色。恢复了(中间出现一次成功 / 跳过)就不再升级。"""
    from . import roles

    need = limit or config.TEAM_CHIEF_ESCALATE_AFTER
    out = []
    for r in roles():
        if r.name == "chief":
            continue
        runs = [x for x in db.list_team_runs(r.name, limit=30) if x["status"] in _RESULT][:need]
        if len(runs) < need or any(x["status"] != "failed" for x in runs):
            continue
        out.append({"role": r.name, "label": r.label, "failed": need,
                    "last_error": str(runs[0].get("summary") or "").strip().split("\n")[0][:160],
                    "since": runs[-1]["started_at"], "last_at": runs[0]["started_at"]})
    return out


def snapshot(category: str | None = None) -> dict[str, Any]:
    """现在的态势:各状态选题数、等主人处理的几类事。纯读。"""
    category = category or config.TEAM_CATEGORY
    items = db.list_plan_items(category, statuses=OPEN, limit=300)
    held = [i for i in items if i["status"] == "producing" and i.get("hold")]
    counts = {s: sum(1 for i in items if i["status"] == s) for s in OPEN}
    drafts = db.list_comments(states=("draft",), limit=500)
    from . import compliance

    return {
        "counts": counts,
        "producing": counts["producing"] - len(held),
        "held": [{"id": i["id"], "subject": i.get("subject") or "", "hold": str(i.get("hold") or "")[:120]}
                 for i in held],
        "comments": len(drafts),
        "paused_accounts": [{"name": a["name"], "reason": a["reason"]} for a in compliance.health()],
        # 仲裁:眼下拦着的事 + 近 24 小时挪过什么
        "waiting": waiting_lines(),
        "actions": [a["text"] for a in actions()],
    }


def todo_lines(snap: dict[str, Any]) -> list[str]:
    """等主人处理的事,一件一行。"""
    c = snap["counts"]
    lines = []
    if c["proposed"]:
        lines.append(f"{c['proposed']} 条选题等你批")
    if c["review"]:
        lines.append(f"{c['review']} 条成片等你审")
    for h in snap["held"]:
        lines.append(f"《{h['subject']}》被门禁拦下:{h['hold']}")
    if snap["comments"]:
        lines.append(f"{snap['comments']} 条评论回复等你批")
    for a in snap["paused_accounts"]:
        lines.append(f"账号「{a['name']}」已暂停自动发布:{a['reason']}")
    return lines


def facts(snap: dict[str, Any], esc: list[dict[str, Any]]) -> str:
    c = snap["counts"]
    lines = [f"等主人处理:{x}" for x in todo_lines(snap)] or ["等主人处理:没有"]
    lines += [f"仲裁:{x}" for x in snap.get("waiting", [])]
    lines += [f"仲裁(近 24 小时):{x}" for x in snap.get("actions", [])]
    lines += [f"角色异常:{e['label']}连续失败 {e['failed']} 次,最后一次:{e['last_error'] or '没写原因'}"
              for e in esc]
    lines.append(f"在制作 {snap['producing']} 条,已排发布 {c['scheduled']} 条,已批待开工 {c['approved']} 条")
    return "\n".join(lines)


def fallback_text(snap: dict[str, Any], esc: list[dict[str, Any]]) -> str:
    todo = todo_lines(snap)
    parts = [f"今天有 {len(todo)} 件事等你处理:" + ";".join(todo) + "。" if todo else "今天没有要你处理的事。"]
    if esc:
        parts.append("反复出错的角色:" + ";".join(f"{e['label']}连续失败 {e['failed']} 次" for e in esc) + "。")
    arb = list(snap.get("waiting", [])) + list(snap.get("actions", []))
    if arb:
        parts.append("总管仲裁:" + ";".join(arb) + "。")
    c = snap["counts"]
    parts.append(f"在制作 {snap['producing']} 条,已排发布 {c['scheduled']} 条。")
    return "".join(parts)


def write_text(snap: dict[str, Any], esc: list[dict[str, Any]], llm: Any) -> tuple[str, str]:
    """(文字, 来源)。LLM 只润色,出错 / 没有 / 关了都用模板。"""
    if llm is None or not config.TEAM_CHIEF_LLM:
        return fallback_text(snap, esc), "template"
    try:
        data = llm.structured(SYSTEM, facts(snap, esc), required_keys=["summary"], temperature=0.3)
    except Exception as exc:  # noqa: BLE001 - 简报文字只是锦上添花
        log.warning("总管简报文字生成失败,改用模板:%s", exc)
        return fallback_text(snap, esc), "template"
    text = str(data.get("summary") or "").strip()
    if not text:
        return fallback_text(snap, esc), "template"
    return text[:500], "llm"


def brief(llm: Any = None, category: str | None = None) -> dict[str, Any]:
    snap = snapshot(category)
    esc = escalations()
    text, source = write_text(snap, esc, llm)
    return {"text": text, "source": source, "todo": todo_lines(snap), "snapshot": snap,
            "made_at": time.time()}


def view() -> dict[str, Any]:
    """/team 页面要的:存着的简报 + 现算的升级项(角色恢复了横幅就消失,不等明天)。"""
    stored = db.get_team_setting(BRIEF_KEY, None)
    return {"brief": stored, "escalations": escalations()}


def run() -> str:
    from ..pipeline import orchestrator

    data = brief(llm=orchestrator._llm())
    db.set_team_setting(BRIEF_KEY, data)
    if not data["todo"] and not escalations():
        # 没事也存了简报(页面上看得到),但不占一行团队动态
        raise Skip("今天没有要你处理的事")
    return f"{len(data['todo'])} 件事等你处理" + (f",{len(escalations())} 个角色反复出错" if escalations() else "")
