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
from . import (Skip, activator, beat_retention, benchmark, engagement, features, iteration,
               playbook, preference, stats)

log = logging.getLogger(__name__)

#: 看多少天的作品
WINDOW_DAYS = 30
#: 本周新发的(周报「本周」那几个数)
WEEK_DAYS = 7
#: 配对实验按这个顺序挑维度(越靠前越先补样本:开头和标题决定前 2 秒和点不点进来)。
#: 也是周报「下周多试试」的维度 —— 写法变体 + 生产旋钮(`pipeline.knobs`)
PLAN_PRIORITY = ("opening", "title_kind", "hook_visual", "pace", "ending", "look",
                 "cover_tone", "concept_level", "speed")
EXPERIMENT_DIMENSIONS = PLAN_PRIORITY

SYSTEM = """你是一个中文汽车短视频账号的复盘官,要给账号主人写本周复盘的结论段。

只输出一个 JSON 对象:{"summary": "...", "next": ["...", "..."]}
- summary:3-5 句话,先说本周整体(发了几条、播放多少),再说最明显的一两条打法结论。
- next:2-3 条下周具体怎么做(多做哪种、少做哪种、试什么)。
- **所有数字只能逐字来自下面的数据**,不许估算、换算或编;数据里没有的结论不许说。
- 样本少(置信度「中」)的结论要说「初步」。不要用营销腔。
- 「早期观察」是发布 1 小时的数据,还没定稿:可以指出要改什么,但不要说成结论。"""


def week_key(now: dt.datetime) -> str:
    y, w, _ = now.isocalendar()
    return f"{y}-W{w:02d}"


def experiments(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """每个维度里样本最少的可选取值(没用过的优先)。

    **配对实验**:样本少说明这一组没法比。下周要把它补上 —— 同一个题材连发几条,
    只改这一个变量(开头 / 结尾 / 标题写法),其余保持一致。别的变量不动,比出来的
    差值才是这个变量的功劳,攒够 3 条就能进统计。"""
    from ..pipeline import knobs, looks, packaging, variety

    universe = {"opening": list(variety.OPENINGS), "ending": list(variety.ENDINGS),
                "title_kind": list(packaging.KINDS), "look": list(looks.LOOKS),
                **{k: list(v) for k, v in knobs.universe().items()}}
    out = []
    for dim in EXPERIMENT_DIMENSIONS:
        counts = stats.sample_counts(rows, dim)
        value = min(universe[dim], key=lambda v: (counts.get(v, 0), universe[dim].index(v)))
        n = counts.get(value, 0)
        out.append({"dimension": dim, "value": value,
                    "label": f"{features.DIMENSION_LABEL[dim]}:{features.value_label(dim, value)}",
                    "n": n, "why": "还没试过" if not n else f"只有 {n} 条样本",
                    "how": "配对实验:同一题材连发 3 条,只改这一个变量,其余不变"})
    return out


def _work(r: dict[str, Any]) -> dict[str, Any]:
    o = r["outcome"]
    return {"pub_id": r["pub_id"], "project_id": r["project_id"], "title": r["title"],
            "views": o.get("views"), "completion_rate": o.get("completion_rate"),
            "lift": round(r["lift"].get("views", 0), 2) if r.get("lift") else None}


def _pool(rows: list[dict[str, Any]], early_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """24 小时行 + 早期行(按作品去重)。

    样本统计用这个:已经发出去的作品都算,别因为还没满 24 小时就当它不存在 ——
    「这个写法够几条样本了」是生产问题,不是数据问题。"""
    seen = {r["pub_id"] for r in rows}
    return rows + [r for r in early_rows if r["pub_id"] not in seen]


def build_plan(pool: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """下一轮配对实验(最多 `playbook.MAX_PARALLEL` 个,每个维度一个):固定一个取值连发几条。

    统计要同一维度 3 条样本才下得了结论,均匀轮着挑要十几条作品才凑得出第一条。
    所以这里明确点名:哪个写法要补 3 条。几个实验并行,但一条片只照做其中一个
    (`playbook.plan` 轮流),不会两个变量钉在同一批片上。"""
    exps = {e["dimension"]: e for e in experiments(pool)}
    out = []
    for dim in PLAN_PRIORITY:
        e = exps.get(dim)
        if e and e["n"] < playbook.BLOCK_SIZE:
            out.append({"dimension": dim, "value": e["value"],
                        "size": playbook.BLOCK_SIZE, "since": time.time()})
        if len(out) >= playbook.MAX_PARALLEL:
            break
    return out


def build_report(rows: list[dict[str, Any]], learnings: list[dict[str, Any]],
                 now: float, early: list[dict[str, Any]] | None = None,
                 pool: list[dict[str, Any]] | None = None) -> dict[str, Any]:
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
        "experiments": experiments(pool if pool is not None else rows),
        # 早期观察:发布 1 小时的快照,不等 24 小时就能看出前 2 秒留没留住人
        "early": early if early is not None else [],
        # 对标爆款的标题开头 vs 我们最好的几条;再加拆出来的开场 / 节奏 / 封面结构对照
        "benchmark": {**benchmark.title_compare(top, now),
                      "structure": benchmark.structure_compare(rows, now)},
        # 逐拍:最近几条片哪一拍被拖过去最多 / 平均观众在第几拍走
        "beats": beat_retention.report(rows),
    }


def _facts(report: dict[str, Any]) -> str:
    t = report["totals"]
    lines = [f"近 {report['window_days']} 天有 24 小时数据的作品:{t['works']} 条,"
             f"24 小时播放中位数 {t['median_views']}",
             f"本周新发 {t['week_works']} 条,24 小时播放合计 {t['week_views']},涨粉 {t['week_followers']}"]
    if t["completion_rate"] is not None:
        lines.append(f"平均完播率 {t['completion_rate'] * 100:.1f}%")
    others = [p for p in report.get("platforms") or [] if (p["platform"], p["piece"]) != ("douyin", "")]
    if others:
        lines.append("一稿多发(各按自己平台的基线学):" + ";".join(
            f"{p['label']} {p['works']} 条,播放中位数 {p['median_views'] if p['median_views'] is not None else '—'}"
            for p in others))
    lines += [f"最好:《{w['title']}》播放 {w['views']}(账号平常的 {w['lift']} 倍)" for w in report["top"]]
    lines += [f"最差:《{w['title']}》播放 {w['views']}(账号平常的 {w['lift']} 倍)"
              for w in report["bottom"]]
    for e in report.get("early") or []:
        bits = []
        if e.get("retention_2s") is not None:
            bits.append(f"2 秒留存 {e['retention_2s'] * 100:.0f}%")
        if e.get("avg_watch_sec") is not None:
            bits.append(f"平均观看 {e['avg_watch_sec']:.1f} 秒")
        if e.get("views") is not None:
            bits.append(f"播放 {e['views']}")
        lines.append(f"早期观察:《{e['title']}》发布 {e.get('hours')} 小时,"
                     f"{'、'.join(bits)}")
        lines += [f"  要改:{a}" for a in e.get("advice") or []]
    lines += beat_retention.report_lines(report.get("beats") or [])
    engage = report.get("engage") or {}
    lines += [f"回复写法「{s['label']}」:发了 {s['n']} 条,回复后 24 小时平均多 {s['more']:g} 条评论,"
              f"{s['again'] * 100:.0f}% 的人又来留言" for s in engage.get("styles") or []]
    lines += activator.effect_lines(engage.get("active"))
    lines += ["打法结论:" + x for x in report["learnings"]] or ["打法结论:样本还不够,没有结论"]
    lines += [f"下周可试:{e['label']}({e['why']})" for e in report["experiments"]]
    bench = report.get("benchmark") or {}
    lines += [f"对标爆款:《{b['title']}》{b['account']},是该号平常的 {b['burst']:g} 倍"
              for b in bench.get("theirs", [])[:3]]
    for d in (bench.get("structure") or {}).get("dims") or []:
        fmt = lambda xs: "、".join(f"{x['label']} {x['share'] * 100:.0f}%" for x in xs) or "—"  # noqa: E731
        lines.append(f"对标爆款{d['label']}:{fmt(d['theirs'])};我们:{fmt(d['ours'])}")
    return "\n".join(lines)


def fallback_text(report: dict[str, Any]) -> dict[str, Any]:
    t = report["totals"]
    summary = (f"本周新发 {t['week_works']} 条,24 小时播放合计 {t['week_views']},"
               f"涨粉 {t['week_followers']}。")
    early = report.get("early") or []
    bad = [e for e in early if e.get("level") == "bad"]
    if bad:
        summary += f"最新一条《{bad[0]['title']}》前 2 秒就掉人:要改 {bad[0]['advice'][0]}"
    elif report["learnings"]:
        summary += "最明显的结论:" + report["learnings"][0] + "。"
    else:
        summary += f"近 {report['window_days']} 天只有 {t['works']} 条作品有完整数据,还不够下结论。"
    nxt = [f"试试{e['label']}({e['why']})" for e in report["experiments"]][:3]
    for e in early:
        for a in e.get("advice") or []:
            nxt.insert(0, f"《{e['title'][:20]}》: {a}")
    return {"summary": summary, "next": nxt[:3], "source": "template"}


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
    """跑一次复盘:更新手册 + 存周报。返回周报。

    没有 24 小时数据也能复盘 —— 早期观察用发布后 1 小时那张快照,不等满一天。
    这种情况下**不清手册**(没有新数据不等于旧结论失效)。"""
    now = now or dt.datetime.now()
    ts = now.timestamp()
    # 你的偏好不依赖作品数据,先归纳(没作品也照样学你的口味)
    preference.summarize(llm, ts)
    since = ts - WINDOW_DAYS * 86400
    rows = features.rows(since=since)
    early_rows = features.early_rows(since=since)
    early = stats.early_report(early_rows)
    week = week_key(now)
    before = iteration.weight_snapshot(db.list_learnings())
    # 对标先验:自己样本不够的取值先按对标号的规律轻推;没作品也照样更新
    # 学习用全部平台的作品(各算各的基线);周报的总数、实验、逐拍、对标只看主阵地的主片
    main = features.main_rows(rows)
    db.replace_learnings(week, stats.learn_benchmark(benchmark.prior_rows(ts), week, own=main),
                         source="benchmark")
    # 互动效果:哪种回复写法引得来评论;活跃官做得多的日子涨粉有没有多(没作品也照样量)
    outcomes = engagement.reply_outcomes(ts)
    db.replace_learnings(week, engagement.learn_styles(outcomes, week), source="reply")
    active_effect = activator.weekly_effect(ts, week)
    if not rows and not early:
        iteration.log_weight_changes(before, iteration.weight_snapshot(db.list_learnings()), ts=ts)
        raise Skip(f"近 {WINDOW_DAYS} 天还没有发布满 24 小时的作品")
    if rows:
        db.replace_learnings(week, stats.learn(rows, week))
    # 逐拍掉人:进度条分析的跳过率落到拍上,按版式 / 位置 / 素材学
    beat_works = beat_retention.works(since=ts - beat_retention.WINDOW_DAYS * 86400)
    db.replace_learnings(week, beat_retention.learn(beat_works, week), source="beat")
    active = playbook.merged(db.list_learnings())
    # 权重变动超过 0.1 的记进迭代日志:自动改了生产,得看得见改了什么
    iteration.log_weight_changes(before, iteration.weight_snapshot(active), ts=ts)
    pool = _pool(main, early_rows)
    report = build_report(main, sorted(active, key=lambda r: -abs(r["effect"] - 1)), ts,
                          early=early, pool=pool)
    # 各平台 / 切片 / 笔记各有几条数据(它们只进学习,不进上面的总数)
    report["platforms"] = features.platform_summary(rows)
    report["engage"] = {"styles": engagement.style_report(outcomes), "active": active_effect}
    report.update(write_text(report, llm))
    report["made_at"] = time.time()
    db.save_report(week, report)
    # 下这轮配对实验:固定一个写法连发几条,把样本攒够
    old_plans = playbook.progress()
    plans = build_plan(pool)
    # 同一个实验接着跑:沿用原来的起点,已经发的那几条照算,不从零数
    since = {(p["dimension"], p["value"]): p["since"] for p in old_plans if not p["done"]}
    for p in plans:
        p["since"] = since.get((p["dimension"], p["value"]), p["since"])
    db.set_team_setting(playbook.PLAN_KEY, plans)
    iteration.log_plan_changes(old_plans, plans, ts=ts)
    return report


def run() -> str:
    from ..pipeline import orchestrator

    report = review(llm=orchestrator._llm())
    n = len(report["learnings"])
    return (f"复盘 {report['totals']['works']} 条作品,打法手册 {n} 条结论"
            + (",出了本周周报" if report.get("summary") else ""))
