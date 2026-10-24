"""合规审核官:发布前出审核单,发布后盯限流。

**发布前**:成片闸卡片上一份审核单 —— 脚本用语(广告法绝对化 / 品类禁用词)、
数字出处、发布标题、封面和置顶评论、第三方素材占比。有红项时「通过」按钮
不让点,服务端 `publisher.approve_cut` 也再查一遍(按你最终填的标题)。
检查全部复用现有的 lint(`script_gen.compliance_lint`、`packaging.check_title`
……),这里只负责汇总成一张单子,不另立一套规则。

**发布后**:每小时看一眼已发作品。24 小时播放远低于这个账号的中位数(样本够了
才判),或者一直卡在平台审核里 → 判定疑似限流,**暂停这个账号的自动发布**
(`account_health`,发布线程不再领这个账号的任务),在 /team 告警,等人看过
再手动恢复。每条作品只判一次,恢复之后不会因为同一条再被暂停。

抖音的播放数据要等分析师(第三期)接上创作者中心才有;在那之前限流判断
只会因为「样本不够」跳过,审核卡住这条照常生效。
"""
from __future__ import annotations

import statistics
import time
from typing import Any

from .. import config, db
from . import Skip

#: 审核单一项的三种结论
OK, WARN, RED = "ok", "warn", "red"
#: 24 小时播放取发布后这个时间窗里的第一张快照(小时)
DAY_WINDOW = (20.0, 72.0)
#: 判过的作品记在这里,只判一次
_SEEN_KEY = "compliance_seen"


# --------------------------------------------------------------------------
# 发布前:审核单
# --------------------------------------------------------------------------
def _entry(key: str, label: str, problems: list[str], level: str = RED,
           ok_note: str = "") -> dict[str, Any]:
    return {"key": key, "label": label, "level": level if problems else OK,
            "detail": ";".join(problems[:4]) + (f"(共 {len(problems)} 处)" if len(problems) > 4 else "")
            if problems else ok_note}


def _script_entries(pid: str, project: dict[str, Any]) -> list[dict[str, Any]]:
    """脚本:与门禁 A 同一份 lint(`orchestrator._gate_a_blockers`,读当前脚本),
    按问题性质拆成三项。节奏那几条门禁 A 已经管过,和合规无关,这里不列。"""
    from ..pipeline import orchestrator

    try:
        profile, theme = orchestrator._theme_of(project)
        issues = orchestrator._gate_a_blockers(pid, profile, theme)
    except Exception as exc:  # noqa: BLE001 - 查不了就直接红,别让它悄悄过
        return [_entry("script", "脚本", [f"查不了脚本:{exc}"])]
    words, sources, other = [], [], []
    for i in issues:
        if i.get("code") == "rhythm":
            continue
        text = str(i.get("message") or "")
        idx = i.get("segment_idx")
        msg = f"第 {idx + 1} 段 {text}" if isinstance(idx, int) else text
        if "禁用词" in text or "绝对化" in text:
            words.append(msg)
        elif "事实" in text or "数字" in text or "引用" in text:
            sources.append(msg)
        else:
            other.append(msg)
    out = [_entry("words", "脚本用语(广告法 / 禁用词)", words, ok_note="没有绝对化用语和禁用词"),
           _entry("sources", "数字出处", sources, ok_note="每个数字都挂了事实")]
    if other:
        out.append(_entry("script", "脚本其他问题", other))
    return out


def _title_entry(pid: str, project: dict[str, Any], title: str) -> dict[str, Any]:
    from ..pipeline import orchestrator, packaging

    try:
        inp = packaging.build_inputs(pid, orchestrator._profile(project))
    except Exception as exc:  # noqa: BLE001
        return _entry("title", "发布标题", [f"读不到包装输入:{exc}"])
    problems = packaging.check_title(title, inp["allowed"], inp["terms"], inp["avoid"])
    # 禁用词、数字没出处、空标题是硬伤;长短、搜索词式写法只提醒
    hard = [p for p in problems if "禁用词" in p or "数字" in p or p == "空标题"]
    if hard:
        return _entry("title", "发布标题", problems)
    return _entry("title", "发布标题", problems, WARN, ok_note=title)


def _cover_entry(pid: str, project: dict[str, Any]) -> dict[str, Any]:
    """封面大字(片名 + 封面 B 的钩子)与置顶评论。"""
    from ..pipeline import orchestrator, packaging

    problems: list[str] = []
    try:
        inp = packaging.build_inputs(pid, orchestrator._profile(project))
        pkg = packaging.load(pid)
    except Exception:  # noqa: BLE001 - 没有包装就只查片名
        inp, pkg = None, None
    terms = inp["terms"] if inp else ()
    cover_text = str(project.get("title") or "")
    if pkg:
        cover_text += " " + packaging.cover_hook(pkg.titles, price=inp["price"], thesis=inp["thesis"])
    hits = packaging.forbidden_hits(cover_text, terms)
    if hits:
        problems.append("封面文字命中禁用词:" + "、".join(hits))
    if pkg and pkg.comment.text:
        problems += ["置顶评论" + p for p in
                     packaging.check_comment(pkg.comment.text, pkg.comment.options, inp["allowed"], terms)]
    note = "没有禁用词" + (",评论数字有出处" if pkg and pkg.comment.text else "")
    return _entry("cover", "封面和置顶评论", problems, ok_note=note)


def _footage_entry(pid: str) -> dict[str, Any]:
    """第三方素材:开场 / 结论里不该有;正文里占比太高有搬运风险。"""
    from ..pipeline import explainer, orchestrator

    try:
        mix = explainer.footage_mix(pid, orchestrator._specs(pid))
    except Exception:  # noqa: BLE001
        mix = {}
    if not mix:
        return _entry("footage", "第三方素材", [], ok_note="没有渲染记录,按一方素材算")
    ratio = mix.get("third_party_ratio") or 0.0
    edge = mix.get("third_party_edge_seconds") or 0.0
    note = f"第三方视频占 {ratio:.0%}"
    red = []
    if edge > 0:
        red.append(f"开场 / 结论里用了 {edge:.0f} 秒第三方视频")
    if ratio >= config.TEAM_THIRD_PARTY_MAX:
        red.append(f"{note},超过 {config.TEAM_THIRD_PARTY_MAX:.0%}")
    if red:
        return _entry("footage", "第三方素材", red)
    if ratio >= config.TEAM_THIRD_PARTY_WARN:
        return _entry("footage", "第三方素材", [f"{note},偏高(角标已裁)"], WARN)
    return _entry("footage", "第三方素材", [], ok_note=note + "(角标都检测过、能裁掉)")


def checklist(project_id: str, title: str) -> dict[str, Any]:
    """成片闸的审核单。`red` > 0 不能通过。"""
    project = db.get_project(project_id) or {}
    items = [*_script_entries(project_id, project), _title_entry(project_id, project, title),
             _cover_entry(project_id, project), _footage_entry(project_id)]
    return {"items": items,
            "red": sum(1 for i in items if i["level"] == RED),
            "warn": sum(1 for i in items if i["level"] == WARN)}


def red_summary(sheet: dict[str, Any]) -> str:
    return ";".join(f"{i['label']}:{i['detail']}" for i in sheet["items"] if i["level"] == RED)


# --------------------------------------------------------------------------
# 发布后:限流监测
# --------------------------------------------------------------------------
def views_at_day(pub: dict[str, Any]) -> int | None:
    """发布后约 24 小时的播放:时间窗里第一张有播放数的快照。"""
    t0 = pub.get("published_at")
    if not t0:
        return None
    lo, hi = DAY_WINDOW
    for snap in db.list_snapshots(pub["id"]):
        age = (snap["captured_at"] - t0) / 3600
        if lo <= age <= hi and snap.get("views") is not None:
            return int(snap["views"])
    return None


def verdict(pub: dict[str, Any], earlier: list[dict[str, Any]], now: float) -> tuple[str, str]:
    """一条作品的结论:("pause", 原因) / ("pass", "") / ("wait", "")。

    `earlier`:同账号更早发出的作品,算基线用。"""
    if pub["state"] == "submitted":
        since = pub.get("published_at") or pub.get("updated_at") or now
        hours = (now - since) / 3600
        if hours >= config.TEAM_REVIEW_STUCK_HOURS:
            return "pause", f"在平台审核里卡了 {hours:.0f} 小时"
        return "wait", ""
    views = views_at_day(pub)
    if views is None:
        age = (now - (pub.get("published_at") or now)) / 3600
        return ("pass", "") if age > DAY_WINDOW[1] else ("wait", "")  # 过了时间窗还没数据就算了
    base = [v for v in (views_at_day(p) for p in earlier) if v is not None]
    if len(base) < config.TEAM_THROTTLE_MIN_BASELINE:
        return "pass", ""  # 样本不够,不下结论
    median = statistics.median(base)
    if median > 0 and views < config.TEAM_THROTTLE_RATIO * median:
        return "pause", (f"24 小时播放 {views},不到账号近 {len(base)} 条中位数 {median:.0f} 的 "
                         f"{config.TEAM_THROTTLE_RATIO:.0%},疑似限流")
    return "pass", ""


def monitor(now: float | None = None) -> list[str]:
    """把已发作品过一遍,该暂停的账号暂停。返回这次暂停的说明。"""
    from ..publish import accounts as accounts_mod

    now = time.time() if now is None else now
    seen = set(db.get_team_setting(_SEEN_KEY, []) or [])
    # 衍生品(切片 / 笔记)播放天然低,不进基线也不判限流
    pubs = [p for p in db.list_publications(limit=500)
            if p["state"] in ("published", "submitted") and not p.get("piece")]
    by_account: dict[str, list[dict[str, Any]]] = {}
    for p in sorted(pubs, key=lambda p: p.get("published_at") or p.get("updated_at") or 0):
        by_account.setdefault(p["account_id"], []).append(p)
    paused: list[str] = []
    for account_id, rows in by_account.items():
        if (db.get_account_health(account_id) or {}).get("paused"):
            continue  # 已经停着,等人处理
        for k, pub in enumerate(rows):
            if pub["id"] in seen:
                continue
            what, why = verdict(pub, rows[:k], now)
            if what == "wait":
                continue
            seen.add(pub["id"])
            if what == "pause":
                acc = accounts_mod.get_account(account_id)
                name = acc.name if acc else account_id
                reason = f"《{pub.get('title') or pub['project_id']}》{why}"
                db.pause_account(account_id, reason)
                db.add_event(pub["project_id"], "team", reason=f"合规审核官暂停账号「{name}」自动发布:{why}")
                paused.append(f"{name}:{reason}")
                break
    db.set_team_setting(_SEEN_KEY, sorted(seen)[-2000:])
    return paused


def health() -> list[dict[str, Any]]:
    """暂停中的账号(页面告警用)。"""
    from ..publish import accounts as accounts_mod

    out = []
    for row in db.list_account_health():
        if not row["paused"]:
            continue
        acc = accounts_mod.get_account(row["account_id"])
        out.append({"account_id": row["account_id"], "name": acc.name if acc else row["account_id"],
                    "reason": row["reason"], "paused_at": row["paused_at"]})
    return out


def run() -> str:
    paused = monitor()
    if not paused:
        raise Skip("已发作品没有异常")
    return "暂停自动发布:" + ";".join(paused)
