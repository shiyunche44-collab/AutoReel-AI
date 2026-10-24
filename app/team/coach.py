"""复盘官:每周日看近 30 天作品,更新打法手册(`learnings`),出一份周报。

**统计归统计,文字归文字。** 哪种做法好 / 差全由 `stats.learn` 算(确定性、可复查);
LLM 只把算好的数字写成三五句结论,拿不到 LLM 就用模板拼。数字一律来自统计结果,
提示词里明确不许编。

**下周实验**:开头、结尾、标题写法里样本最少的那几种,下周多试 —— 打法手册只会让
效果好的多挑一点(加权 + 30% 探索),样本少的写法要有人专门去补数据。
"""
from __future__ import annotations

import datetime as dt
import logging
import statistics
import time
from typing import Any

from .. import db
from . import Skip, benchmark, features, playbook, stats

log = logging.getLogger(__name__)

#: 看多少天的作品
WINDOW_DAYS = 30
#: 本周新发的(周报「本周」那几个数)
WEEK_DAYS = 7
#: 实验从这几个维度里挑
EXPERIMENT_DIMENSIONS = ("opening", "ending", "title_kind")

SYSTEM = """你是一个中文汽车短视频账号的复盘官,要给账号主人写本周复盘的结论段。

只输出一个 JSON 对象:{"summary": "...", "next": ["...", "..."]}
- summary:3-5 句话,先说本周整体(发了几条、播放多少),再说最明显的一两条打法结论。
- next:2-3 条下周具体怎么做(多做哪种、少做哪种、试什么)。
- **所有数字只能逐字来自下面的数据**,不许估算、换算或编;数据里没有的结论不许说。
- 样本少(置信度「中」)的结论要说「初步」。不要用营销腔。"""


def week_key(now: dt.datetime) -> str:
    y, w, _ = now.isocalendar()
    return f"{y}-W{w:02d}"


def experiments(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """每个维度里样本最少的可选取值(没用过的优先)。"""
    from ..pipeline import packaging, variety

    universe = {"opening": list(variety.OPENINGS), "ending": list(variety.ENDINGS),
                "title_kind": list(packaging.KINDS)}
    out = []
    for dim in EXPERIMENT_DIMENSIONS:
        counts = stats.sample_counts(rows, dim)
        value = min(universe[dim], key=lambda v: (counts.get(v, 0), universe[dim].index(v)))
        n = counts.get(value, 0)
        out.append({"dimension": dim, "value": value,
                    "label": f"{features.DIMENSION_LABEL[dim]}:{features.value_label(dim, value)}",
                    "n": n, "why": "还没试过" if not n else f"只有 {n} 条样本"})
    return out


def _work(r: dict[str, Any]) -> dict[str, Any]:
    o = r["outcome"]
    return {"pub_id": r["pub_id"], "project_id": r["project_id"], "title": r["title"],
            "views": o.get("views"), "completion_rate": o.get("completion_rate"),
            "lift": round(r["lift"].get("views", 0), 2) if r.get("lift") else None}


def build_report(rows: list[dict[str, Any]], learnings: list[dict[str, Any]],
                 now: float) -> dict[str, Any]:
    lifted = sorted(stats.lifts(rows), key=lambda r: -(r["lift"].get("views") or 0))
    week = [r for r in rows if r["published_at"] >= now - WEEK_DAYS * 86400]
    rates = [r["outcome"]["completion_rate"] for r in rows
             if r["outcome"].get("completion_rate") is not None]
    top = [_work(r) for r in lifted[:3]]
    return {
        "window_days": WINDOW_DAYS,
        "totals": {
            "works": len(rows), "week_works": len(week),
            "week_views": sum(int(r["outcome"].get("views") or 0) for r in week),
            "week_followers": sum(int(r["outcome"].get("followers_gained") or 0) for r in week),
            "median_views": statistics.median([r["outcome"]["views"] for r in rows]) if rows else 0,
            "completion_rate": round(statistics.fmean(rates), 4) if rates else None,
        },
        "top": top,
        "bottom": [_work(r) for r in lifted[-3:][::-1]] if len(lifted) > 3 else [],
        "learnings": [playbook.describe(r) for r in learnings[:10]],
        "experiments": experiments(rows),
        # 对标爆款的标题开头 vs 我们最好的几条(对标分析员的数据)
        "benchmark": benchmark.title_compare(top, now),
    }


def _facts(report: dict[str, Any]) -> str:
    t = report["totals"]
    lines = [f"近 {report['window_days']} 天有 24 小时数据的作品:{t['works']} 条,"
             f"24 小时播放中位数 {t['median_views']}",
             f"本周新发 {t['week_works']} 条,24 小时播放合计 {t['week_views']},涨粉 {t['week_followers']}"]
    if t["completion_rate"] is not None:
        lines.append(f"平均完播率 {t['completion_rate'] * 100:.1f}%")
    lines += [f"最好:《{w['title']}》播放 {w['views']}(账号平常的 {w['lift']} 倍)" for w in report["top"]]
    lines += [f"最差:《{w['title']}》播放 {w['views']}(账号平常的 {w['lift']} 倍)"
              for w in report["bottom"]]
    lines += ["打法结论:" + x for x in report["learnings"]] or ["打法结论:样本还不够,没有结论"]
    lines += [f"下周可试:{e['label']}({e['why']})" for e in report["experiments"]]
    bench = report.get("benchmark") or {}
    lines += [f"对标爆款:《{b['title']}》{b['account']},是该号平常的 {b['burst']:g} 倍"
              for b in bench.get("theirs", [])[:3]]
    return "\n".join(lines)


def fallback_text(report: dict[str, Any]) -> dict[str, Any]:
    t = report["totals"]
    summary = (f"本周新发 {t['week_works']} 条,24 小时播放合计 {t['week_views']},"
               f"涨粉 {t['week_followers']}。")
    if report["learnings"]:
        summary += "最明显的结论:" + report["learnings"][0] + "。"
    else:
        summary += f"近 {report['window_days']} 天只有 {t['works']} 条作品有完整数据,还不够下结论。"
    return {"summary": summary, "next": [f"试试{e['label']}({e['why']})"
                                         for e in report["experiments"]][:3],
            "source": "template"}


def write_text(report: dict[str, Any], llm: Any) -> dict[str, Any]:
    if llm is None:
        return fallback_text(report)
    try:
        data = llm.structured(SYSTEM, _facts(report), required_keys=["summary"], temperature=0.3)
    except Exception as exc:  # noqa: BLE001 - 文字只是锦上添花,出错用模板
        log.warning("周报文字生成失败,改用模板:%s", exc)
        return fallback_text(report)
    summary = str(data.get("summary") or "").strip()
    nxt = [str(x).strip() for x in data.get("next") or [] if str(x).strip()][:3]
    if not summary:
        return fallback_text(report)
    return {"summary": summary[:600], "next": nxt, "source": "llm"}


def review(now: dt.datetime | None = None, llm: Any = None) -> dict[str, Any]:
    """跑一次复盘:更新手册 + 存周报。返回周报。"""
    now = now or dt.datetime.now()
    ts = now.timestamp()
    rows = features.rows(since=ts - WINDOW_DAYS * 86400)
    if not rows:
        raise Skip(f"近 {WINDOW_DAYS} 天还没有发布满 24 小时的抖音作品")
    week = week_key(now)
    learnings = stats.learn(rows, week)
    db.replace_learnings(week, learnings)
    active = db.list_learnings()
    report = build_report(rows, sorted(active, key=lambda r: -abs(r["effect"] - 1)), ts)
    report.update(write_text(report, llm))
    report["made_at"] = time.time()
    db.save_report(week, report)
    return report


def run() -> str:
    from ..pipeline import orchestrator

    report = review(llm=orchestrator._llm())
    n = len(report["learnings"])
    return (f"复盘 {report['totals']['works']} 条作品,打法手册 {n} 条结论"
            + (",出了本周周报" if report.get("summary") else ""))
