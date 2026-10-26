"""流程体检 + 有界自动调参:复盘官每周量一次「流水线本身」顺不顺。

体检(只看、只写进周报):
- 各阶段耗时 / 失败率 —— `job_events` 的状态转移:进 X 到离开 X 的时间算 X 这一段;
- 门禁 A / B 最常拦什么 —— 制片替人过闸被拦时记的事件(「拦下:…」);
- 「素材不够」出现几次 —— 成片实测官方视频占比低 / 静图多、快反选题写了素材不够、拦截原因里提到素材;
- TTS 出问题几次、各拍渲染失败率;
- 每条片的成本、各角色失败次数。

**自动调参只动白名单**(`TUNABLES`),每个都有写死的上下限,一周最多动一步,存在 team_settings、
每次都写变更日志;其他问题只给建议,不自动改。调好的值只在服务里生效
(`apply()` 由 `playbook.install()` 调),测试 / 命令行跑的都是默认值。
"""
from __future__ import annotations

import datetime as dt
import re
import statistics
import time
from collections import Counter
from dataclasses import dataclass
from typing import Any, Callable

from .. import config, db

#: 体检看最近几天
WEEK_DAYS = 7
#: 「素材不够」一周出现这么多次就调
THIN_TRIGGER = 2
#: 成片实测:官方视频占比低于这个、或静图占比高于这个,算素材不够
THIN_OFFICIAL = 0.4
THIN_STILL = 0.35
#: TTS 一周出问题这么多次就多补跑两轮
TTS_TRIGGER = 2
#: 各拍渲染:至少这么多拍、失败率到这个数就降并发
RENDER_MIN_SHOTS = 20
RENDER_FAIL_RATE = 0.2
#: 某一段失败率到这个数(且至少失败 2 次)给建议
STAGE_FAIL_WARN = 0.2
TUNING_KEY = "health_tuning"
TUNED_WEEK_KEY = "health_tuned_week"

#: 待在这个状态时流水线在干什么
STAGE_LABEL = {
    "DRAFT_INTENT": "采集事实", "FACTS_RESOLVED": "写脚本", "SCRIPT_READY": "做分镜",
    "STORYBOARD_READY": "进门禁 A", "GATE_A": "门禁 A(审稿 + 配音)", "ASSETS_READY": "备料",
    "PROMPTS_READY": "备料", "DRAFT_RENDERING": "渲染各拍", "GATE_B": "门禁 B(审片)",
    "FINAL_RENDERING": "转成片", "QA_PASSED": "质检", "COMPOSING": "拼接混音",
    "PAUSED_BUDGET": "等额度",
}
_END = {"DONE", "FAILED", "CANCELLED"}


# --------------------------------------------------------------------------
# 白名单
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class Tunable:
    key: str
    label: str
    unit: str
    step: float          # 正数往上调,负数往下调
    bound: float         # 上限(step > 0)或下限(step < 0)
    get: Callable[[], float]
    set: Callable[[float], None]


def _lib():
    from ..pipeline import library

    return library


def _nar():
    from ..pipeline import narration

    return narration


def _set_lead(v: float) -> None:
    config.TEAM_LEAD_HOURS = float(v)


def _set_target(v: float) -> None:
    _lib().CONFIRMED_VIDEO_TARGET = float(v)


def _set_rounds(v: float) -> None:
    _nar().TTS_RETRY_ROUNDS = int(v)


def _set_workers(v: float) -> None:
    config.MOTION_WORKERS = int(v)


TUNABLES: dict[str, Tunable] = {t.key: t for t in (
    Tunable("lead_hours", "开工提前量", "小时", 6, 48,
            lambda: config.TEAM_LEAD_HOURS, _set_lead),
    Tunable("video_target", "素材池目标(确认本车视频)", "秒", 30, 300,
            lambda: _lib().CONFIRMED_VIDEO_TARGET, _set_target),
    Tunable("tts_rounds", "TTS 补跑轮数", "轮", 2, 9,
            lambda: _nar().TTS_RETRY_ROUNDS, _set_rounds),
    Tunable("motion_workers", "动画渲染并发", "个", -1, 1,
            lambda: config.MOTION_WORKERS, _set_workers),
)}
#: 服务启动前的原值(恢复默认 / 摘钩子时用)
_DEFAULTS: dict[str, float] = {}


def tuned() -> dict[str, float]:
    got = db.get_team_setting(TUNING_KEY, {}) or {}
    return {k: float(v) for k, v in got.items() if k in TUNABLES}


def apply() -> None:
    """把调过的值写进各模块(服务启动时由 `playbook.install()` 调)。"""
    for key, t in TUNABLES.items():
        _DEFAULTS.setdefault(key, float(t.get()))
    for key, value in tuned().items():
        TUNABLES[key].set(value)


def restore() -> None:
    """恢复原值(`playbook.uninstall()` 调)。"""
    for key, value in _DEFAULTS.items():
        TUNABLES[key].set(value)


def default_of(key: str) -> float:
    return _DEFAULTS.get(key, float(TUNABLES[key].get()))


def tuned_view() -> list[dict[str, Any]]:
    """页面用:调过的参数(现值 / 默认值 / 边界)。"""
    return [{"key": k, "label": TUNABLES[k].label, "unit": TUNABLES[k].unit, "value": v,
             "default": default_of(k), "bound": TUNABLES[k].bound} for k, v in tuned().items()]


def reset(key: str, ts: float | None = None) -> bool:
    """恢复某一项的默认值(页面上点的)。"""
    got = db.get_team_setting(TUNING_KEY, {}) or {}
    if key not in got:
        return False
    old = got.pop(key)
    db.set_team_setting(TUNING_KEY, got)
    if key in _DEFAULTS:
        TUNABLES[key].set(_DEFAULTS[key])
    t = TUNABLES[key]
    db.add_iteration_log("param", f"{t.label}恢复默认:{_fmt(old)}→{_fmt(default_of(key))} {t.unit}",
                         {"key": key, "old": old, "new": default_of(key), "manual": True}, ts=ts)
    return True


def _fmt(v: float) -> str:
    return f"{v:g}"


# --------------------------------------------------------------------------
# 体检
# --------------------------------------------------------------------------
def _events_since(since: float) -> dict[str, list[dict[str, Any]]]:
    """窗口里动过的项目 → 它的全部事件(按时间正序;进窗口前的那段也要,好算第一段耗时)。"""
    db.init_db()
    with db.connect() as conn:
        pids = [r[0] for r in conn.execute(
            "SELECT DISTINCT project_id FROM job_events WHERE created_at >= ?", (since,))]
        out: dict[str, list[dict[str, Any]]] = {}
        for pid in pids:
            cur = conn.execute("SELECT * FROM job_events WHERE project_id = ? ORDER BY created_at, id",
                               (pid,))
            out[pid] = [dict(r) for r in cur.fetchall()]
    return out


def stages(events: dict[str, list[dict[str, Any]]], since: float) -> list[dict[str, Any]]:
    """各阶段:{stage, label, n, median_min, failed, fail_rate}。只算进窗口后开始的那一段。"""
    from ..state import State

    states = {str(s) for s in State}
    took: dict[str, list[float]] = {}
    failed: Counter = Counter()
    for evs in events.values():
        moves = [e for e in evs if e["dst"] in states]
        for a, b in zip(moves, moves[1:]):
            # 以失败收场的那段只算一次失败,不再当耗时样本
            if a["dst"] in _END or a["created_at"] < since or b["dst"] == "FAILED":
                continue
            took.setdefault(a["dst"], []).append(max(0.0, b["created_at"] - a["created_at"]))
        for e in moves:
            if e["dst"] == "FAILED" and e["created_at"] >= since and e.get("src"):
                failed[e["src"]] += 1
                took.setdefault(e["src"], [])
    order = [str(s) for s in State]
    out = []
    for stage in sorted(took, key=lambda s: order.index(s) if s in order else 99):
        n = len(took[stage]) + failed[stage]
        out.append({"stage": stage, "label": STAGE_LABEL.get(stage, stage), "n": n,
                    "median_min": round(statistics.median(took[stage]) / 60, 1) if took[stage] else None,
                    "failed": failed[stage], "fail_rate": round(failed[stage] / n, 3) if n else 0.0})
    return out


def _norm_reason(text: str) -> str:
    """「第 3, 7 拍没渲染成功」和「第 12 拍没渲染成功」算一种。"""
    text = re.sub(r"^门禁 [AB] 没放行[::]", "", text.strip())
    text = re.sub(r"\d+(\.\d+)?", "N", text)
    text = re.sub(r"N(\s*,\s*N)+", "N", text)
    return text[:60]


def gate_blocks(events: dict[str, list[dict[str, Any]]], since: float) -> list[dict[str, Any]]:
    """门禁最常拦的原因(制片替人过闸被拦时记的「拦下:…」)。"""
    counts: Counter = Counter()
    gates: dict[str, str] = {}
    for evs in events.values():
        for e in evs:
            reason = str(e.get("reason") or "")
            if e["dst"] != "team" or not reason.startswith("拦下") or e["created_at"] < since:
                continue
            gate = "B" if "门禁 B" in reason or e.get("src") == "GATE_B" else "A"
            for part in reason.split(":", 1)[-1].split(";"):
                if part.strip():
                    key = _norm_reason(part)
                    counts[key] += 1
                    gates[key] = gate
    return [{"reason": r, "gate": gates[r], "n": n} for r, n in counts.most_common(5)]


def thin_signals(events: dict[str, list[dict[str, Any]]], since: float) -> list[str]:
    """「素材不够」的几处来源,一处一条(说明是哪个项目 / 选题)。"""
    out: list[str] = []
    for pid, evs in events.items():
        done = any(e["dst"] == "DONE" and e["created_at"] >= since for e in evs)
        m = db.read_json(pid, "features.json", None) if done else None
        if m:
            official, still = m.get("official_video_ratio"), m.get("still_ratio")
            if (official is not None and official < THIN_OFFICIAL) or (still is not None and still >= THIN_STILL):
                out.append(f"{pid}:成片官方视频占比 {official}、静图 {still}")
                continue
        if any(e["dst"] == "team" and str(e.get("reason") or "").startswith("拦下") and "素材" in str(e["reason"])
               and e["created_at"] >= since for e in evs):
            out.append(f"{pid}:门禁因素材被拦")
    for item in db.list_plan_items(limit=500):
        if "素材不够" in str(item.get("note") or "") and (item.get("updated_at") or 0) >= since:
            out.append(f"{item['id']}:选题写了素材不够")
    return out


_TTS_RE = re.compile(r"TTS (部分)?失败|TTS 返回|无旁白|TTS 未产出|旁白.*失败")


def tts_problems(events: dict[str, list[dict[str, Any]]], since: float) -> int:
    """窗口里 TTS 出过问题的项目数(事件 / 拦截原因里带 TTS 失败、缺句)。"""
    return sum(1 for evs in events.values()
               if any(e["created_at"] >= since and _TTS_RE.search(str(e.get("reason") or "")) for e in evs))


def render_stats(events: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
    """窗口里动过的项目的各拍:多少拍、失败几拍、重试过几拍。"""
    total = failed = retried = 0
    for pid in events:
        for s in db.get_shots(pid, "draft"):
            total += 1
            failed += s.get("status") == "failed"
            retried += int(s.get("attempts") or 0) > 1
    return {"shots": total, "failed": failed, "retried": retried,
            "fail_rate": round(failed / total, 3) if total else 0.0}


def costs(events: dict[str, list[dict[str, Any]]], since: float) -> dict[str, Any]:
    """窗口里做完的片:几条、每条平均花多少(项目记的已花额度)。"""
    spent, budget = [], []
    for pid, evs in events.items():
        if any(e["dst"] == "DONE" and e["created_at"] >= since for e in evs):
            p = db.get_project(pid) or {}
            spent.append(float(p.get("spent_cost") or 0))
            budget.append(float(p.get("quota_budget") or 0))
    return {"done": len(spent), "per_video": round(statistics.fmean(spent), 3) if spent else None,
            "total": round(sum(spent), 3),
            "budget": round(statistics.fmean(budget), 3) if budget else None}


def roles(since: float) -> list[dict[str, Any]]:
    """各角色这周跑了几次、失败几次、最后一次失败说了什么。"""
    from . import roles as all_roles

    names = {r.name: r.label for r in all_roles()}
    by: dict[str, dict[str, Any]] = {}
    for r in db.list_team_runs(limit=5000):
        if (r.get("started_at") or 0) < since:
            continue
        d = by.setdefault(r["role"], {"role": r["role"], "label": names.get(r["role"], r["role"]),
                                      "runs": 0, "failed": 0, "last_error": ""})
        d["runs"] += 1
        if r.get("status") == "failed":
            d["failed"] += 1
            d["last_error"] = d["last_error"] or str(r.get("summary") or "").strip().split("\n")[0][:120]
    return sorted(by.values(), key=lambda d: (-d["failed"], d["role"]))


def check(now: float | None = None) -> dict[str, Any]:
    """量一次。纯读。"""
    now = now or time.time()
    since = now - WEEK_DAYS * 86400
    events = _events_since(since)
    thin = thin_signals(events, since)
    return {"stages": stages(events, since), "gates": gate_blocks(events, since),
            "thin": thin, "tts": tts_problems(events, since), "render": render_stats(events),
            "cost": costs(events, since), "roles": roles(since)}


# --------------------------------------------------------------------------
# 调参 + 建议
# --------------------------------------------------------------------------
def _wants(h: dict[str, Any]) -> dict[str, str]:
    """这周该动哪几项 → 为什么。"""
    out: dict[str, str] = {}
    if len(h["thin"]) >= THIN_TRIGGER:
        why = f"这周 {len(h['thin'])} 次素材不够"
        out["lead_hours"] = why
        out["video_target"] = why
    if h["tts"] >= TTS_TRIGGER:
        out["tts_rounds"] = f"这周 {h['tts']} 个项目 TTS 出问题"
    r = h["render"]
    if r["shots"] >= RENDER_MIN_SHOTS and r["fail_rate"] >= RENDER_FAIL_RATE:
        out["motion_workers"] = f"各拍渲染失败率 {r['fail_rate'] * 100:.0f}%({r['failed']}/{r['shots']})"
    return out


def tune(h: dict[str, Any], week: str, ts: float | None = None) -> list[dict[str, Any]]:
    """按体检结果动白名单里的参数,每项一步、到边界就停;同一周只动一次。返回动了的。"""
    if db.get_team_setting(TUNED_WEEK_KEY) == week:
        return []
    got = db.get_team_setting(TUNING_KEY, {}) or {}
    changes = []
    for key, why in _wants(h).items():
        t = TUNABLES[key]
        _DEFAULTS.setdefault(key, float(t.get()))
        old = float(got.get(key, t.get()))
        new = min(t.bound, old + t.step) if t.step > 0 else max(t.bound, old + t.step)
        if new == old:
            continue
        got[key] = new
        t.set(new)
        changes.append({"key": key, "label": t.label, "unit": t.unit, "old": old, "new": new, "why": why})
        db.add_iteration_log("param", f"{t.label} {_fmt(old)}→{_fmt(new)} {t.unit}:{why}",
                             {"key": key, "old": old, "new": new, "why": why}, ts=ts)
    db.set_team_setting(TUNING_KEY, got)
    db.set_team_setting(TUNED_WEEK_KEY, week)
    return changes


def advice(h: dict[str, Any]) -> list[str]:
    """不自动改的,只给建议。"""
    out = []
    for s in h["stages"]:
        if s["failed"] >= 2 and s["fail_rate"] >= STAGE_FAIL_WARN:
            out.append(f"「{s['label']}」这周失败 {s['failed']} 次(失败率 {s['fail_rate'] * 100:.0f}%),看看项目页的报错")
    if h["gates"]:
        g = h["gates"][0]
        out.append(f"门禁 {g['gate']} 最常拦的是「{g['reason']}」({g['n']} 次),这一步的生成规则值得先改")
    for r in h["roles"]:
        if r["failed"] >= 2:
            out.append(f"{r['label']}这周失败 {r['failed']} 次:{r['last_error']}")
    budget = h["cost"].get("budget") or 0
    per = h["cost"]["per_video"]
    if per and budget and per > 0.8 * budget:
        out.append(f"每条片平均花 {per:g},快到单项目额度 {budget:g} 了")
    return out


def weekly(now: float | None = None, week: str = "") -> dict[str, Any]:
    """复盘官每周调一次:体检 + 调参 + 建议。"""
    now = now or time.time()
    h = check(now)
    h["changes"] = tune(h, week or dt.date.fromtimestamp(now).isoformat(), now)
    h["tuned"] = tuned_view()
    h["advice"] = advice(h)
    return h


def report_lines(h: dict[str, Any] | None) -> list[str]:
    """周报文字里的几行。"""
    if not h:
        return []
    lines = []
    slow = [s for s in h["stages"] if s["median_min"] is not None and not s["stage"].startswith("GATE")]
    if slow:
        top = max(slow, key=lambda s: s["median_min"])
        lines.append(f"流程体检:最慢的一段是「{top['label']}」,中位 {top['median_min']:g} 分钟")
    if h["thin"]:
        lines.append(f"素材不够 {len(h['thin'])} 次")
    c = h["cost"]
    if c["done"]:
        lines.append(f"这周做完 {c['done']} 条,每条平均花 {c['per_video']:g}")
    lines += [f"自动调参:{x['label']} {_fmt(x['old'])}→{_fmt(x['new'])} {x['unit']}({x['why']})"
              for x in h.get("changes") or []]
    lines += [f"建议:{a}" for a in h.get("advice") or []]
    return lines
