"""逐拍掉人:哪一拍、哪种版式最留不住人。

抖音创作者中心**没有逐秒留存曲线**(`tests/probe_douyin_retention.py` 实测),有的是作品详情页的
「进度条分析」:播放进度上每 8 秒一个点的**跳过率**(拖着进度条往后跳)和回看率。分析师在
24 小时以后的快照上顺手抓它(`douyin.fetch_work_progress` → `progress_curves` 表)。

- `beat_map`:分镜(`storyboard.json`)按时长累加(`timeline.shot_timeline`)得到每拍起止、版式、
  章节、在片里的位置(开头章 / 正文 / 结论章)、素材来源;
- `beat_skips`:每个点落到它所在的拍上,拍的跳过率 ÷ 这条片自己的平均跳过率 = 相对跳过率
  (单位、播放量都约掉了,不同的片能放一起比);
- `learn`:按版式 / 位置 / 素材 / 段落分组,收缩后写成「留人倍数」= 1 ÷ 相对跳过率
  (和别的结论一样,越大越好),进打法手册(`source=beat`);
- 回灌:`layout_hint` 给分镜挑版式的提示词,`playbook.weights("beat_layout")` 给切片挑段;
- 降级:进度条分析全 0(没人拖)或没抓到时,用平均观看秒数定位「平均观众在第几拍走」,只进周报。
"""
from __future__ import annotations

import statistics
from collections import defaultdict
from typing import Any

from .. import db

#: 学这几个维度:(维度, 中文名)
DIMENSIONS = (("beat_layout", "逐拍·版式"), ("beat_role", "逐拍·位置"),
              ("beat_footage", "逐拍·素材"), ("beat_section", "逐拍·段落"))
LAYOUT_LABEL = {"broll": "实拍空镜", "panel": "参数面板", "highlight": "关键词大字",
                "card": "数据卡", "stat": "大数字", "concept": "概念动画", "hook": "开场大字"}
ROLE_LABEL = {"hook": "开头章", "body": "正文", "ending": "结论章"}
FOOTAGE_LABEL = {"official": "官方实拍", "still": "静图", "third_party": "第三方视频",
                 "rendered": "纯渲染"}
#: 收缩:每组先当有这么多拍「和平均一样」
PRIOR = 4.0
#: 一组至少这么多拍、来自至少这么多条片才写结论
MIN_BEATS = 4
MIN_WORKS = 2
HIGH_BEATS = 12
#: 留人倍数夹在这个范围;偏离不到 MIN_EFFECT 不算
HOLD_RANGE = (0.7, 1.3)
MIN_EFFECT = 0.08
#: 相对跳过率超过它算「掉人拍」,周报点名
WORST_REL = 1.5
#: 看最近多少天的片
WINDOW_DAYS = 60


def label(dimension: str, value: str) -> str:
    table = {"beat_layout": LAYOUT_LABEL, "beat_role": ROLE_LABEL,
             "beat_footage": FOOTAGE_LABEL}.get(dimension, {})
    return table.get(value, value)


# --------------------------------------------------------------------------
# 拍的时间轴
# --------------------------------------------------------------------------
def _footage(project_id: str, idx: Any) -> str:
    """这一拍的背景主要是什么(读 `beats/NNN.json`;没渲过 / 卡片拍 = 纯渲染)。"""
    try:
        pieces = db.read_json(project_id, f"beats/{int(idx):03d}.json", None) or []
    except (TypeError, ValueError):
        pieces = []
    secs: dict[str, float] = defaultdict(float)
    for p in pieces:
        kind = ("third_party" if p.get("third_party")
                else "still" if p.get("kind") == "still" else "official")
        secs[kind] += float(p.get("length") or 0.0)
    if not secs:
        return "rendered"
    if secs.get("third_party"):
        return "third_party"  # 掺了第三方就单算:它是最要盯的那种
    return max(secs, key=lambda k: secs[k])


def beat_map(project_id: str, storyboard: list[dict[str, Any]] | None = None
             ) -> list[dict[str, Any]]:
    """每拍:{idx, start, end, layout, section, role, footage}。没分镜 → []。"""
    from ..pipeline import timeline
    from ..pipeline.explainer import beat_role

    if storyboard is None:
        storyboard = db.read_json(project_id, "storyboard.json", []) or []
    shots = [s for s in storyboard if isinstance(s, dict) and float(s.get("duration") or 0) > 0]
    names: list[str] = []
    where: list[int] = []
    for s in shots:
        name = str(s.get("chapter") or s.get("section") or "正片")
        if not names or names[-1] != name:
            names.append(name)
        where.append(len(names) - 1)
    spans = timeline.shot_timeline([float(s["duration"]) for s in shots])
    return [{"idx": s.get("idx"), "start": seg.start, "end": seg.end,
             "layout": str(s.get("layout") or "broll"), "section": str(s.get("section") or ""),
             "role": beat_role(where, pos, len(names)), "footage": _footage(project_id, s.get("idx"))}
            for pos, (s, seg) in enumerate(zip(shots, spans))]


def beat_skips(points: list[list[float]], beats: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """进度条分析的点落到拍上:每拍加 `skip`(落进来的点的平均跳过率)和 `rel`(÷ 全片平均)。

    没有点落进来的拍(短于 8 秒常见)不返回;全片没人跳过(全 0)→ [](没信号,不是「都很好」)。"""
    if not beats or not points:
        return []
    end = beats[-1]["end"]
    inside = [p for p in points if 0 <= p[0] < end]
    mean = statistics.fmean(p[1] for p in inside) if inside else 0.0
    if mean <= 0:
        return []
    out = []
    for b in beats:
        got = [p[1] for p in inside if b["start"] <= p[0] < b["end"]]
        if got:
            skip = statistics.fmean(got)
            out.append({**b, "skip": round(skip, 3), "rel": round(skip / mean, 3)})
    return out


def leave_beat(beats: list[dict[str, Any]], avg_watch_sec: float | None) -> dict[str, Any] | None:
    """降级:平均观看秒数落在哪一拍 —— 「平均观众在这一拍走了」。"""
    if not beats or not avg_watch_sec or avg_watch_sec <= 0:
        return None
    for pos, b in enumerate(beats):
        if b["start"] <= avg_watch_sec < b["end"]:
            return {**b, "pos": pos + 1, "n_beats": len(beats), "at": round(avg_watch_sec, 1)}
    return None  # 平均看完了全片


# --------------------------------------------------------------------------
# 学习
# --------------------------------------------------------------------------
def works(since: float | None = None) -> list[dict[str, Any]]:
    """抓到了进度条分析、而且有人跳过的作品:[{pub_id, project_id, title, beats}]。"""
    curves = db.list_progress_curves()
    out = []
    for pub in db.list_publications(limit=1000):
        curve = curves.get(pub["id"])
        if not curve or pub.get("piece"):
            continue
        if since is not None and (pub.get("published_at") or 0) < since:
            continue
        beats = beat_skips(curve["points"], beat_map(pub["project_id"]))
        if beats:
            out.append({"pub_id": pub["id"], "project_id": pub["project_id"],
                        "title": pub.get("title") or "", "beats": beats})
    return out


def learn(rows: list[dict[str, Any]], week: str) -> list[dict[str, Any]]:
    """`works()` → 结论行(`db.replace_learnings(..., source="beat")`)。

    一组 = 所有作品里这种拍的相对跳过率,收缩到 1(PRIOR 拍)后取倒数当留人倍数。"""
    groups: dict[tuple[str, str], list[tuple[dict[str, Any], float]]] = defaultdict(list)
    for w in rows:
        for b in w["beats"]:
            for dim, _ in DIMENSIONS:
                value = b.get(dim.removeprefix("beat_"))
                if value:
                    groups[(dim, value)].append((w, float(b["rel"])))
    lo, hi = HOLD_RANGE
    out = []
    for (dim, value), members in groups.items():
        n = len(members)
        pubs = {w["pub_id"] for w, _ in members}
        if n < MIN_BEATS or len(pubs) < MIN_WORKS:
            continue
        rel = (sum(r for _, r in members) + PRIOR) / (n + PRIOR)
        hold = min(hi, max(lo, 1 / rel))
        if abs(hold - 1) < MIN_EFFECT:
            continue
        per: dict[str, list[float]] = defaultdict(list)
        titles = {}
        for w, r in members:
            per[w["pub_id"]].append(r)
            titles[w["pub_id"]] = w["title"]
        ev = sorted(((pid, statistics.fmean(rs)) for pid, rs in per.items()),
                    key=lambda x: -x[1] if hold < 1 else x[1])
        out.append({
            "id": f"beat:{dim}:{value}:hold", "week": week, "dimension": dim, "value": value,
            "metric": "hold", "effect": round(hold, 3), "n": n,
            "confidence": "high" if n >= HIGH_BEATS and len(pubs) >= 4 else "medium",
            "evidence": [{"pub_id": pid, "title": titles[pid], "lift": round(1 / max(r, 0.2), 2)}
                         for pid, r in ev[:5]],
        })
    out.sort(key=lambda r: -abs(r["effect"] - 1))
    return out


# --------------------------------------------------------------------------
# 回灌
# --------------------------------------------------------------------------
def layout_hint(learnings: list[dict[str, Any]] | None = None) -> str:
    """给分镜挑版式的一段话:本账号观众最常拖过去的版式 / 最留得住人的版式。没结论 → 空串。"""
    rows = [r for r in (db.list_learnings() if learnings is None else learnings)
            if r["dimension"] == "beat_layout" and r.get("status", "active") == "active"]
    bad = [LAYOUT_LABEL.get(r["value"], r["value"])
           for r in sorted(rows, key=lambda r: r["effect"]) if r["effect"] < 1][:2]
    good = [LAYOUT_LABEL.get(r["value"], r["value"])
            for r in sorted(rows, key=lambda r: -r["effect"]) if r["effect"] > 1][:2]
    parts = []
    if bad:
        parts.append(f"本账号观众最常拖过去的版式:{'、'.join(bad)} —— 少排,非用不可就压短、"
                     "放在有新信息的地方")
    if good:
        parts.append(f"最留得住人的版式:{'、'.join(good)} —— 关键信息优先用它")
    return ";".join(parts)


def install() -> None:
    from ..pipeline import explainer, repurpose
    from . import playbook

    explainer.layout_hint = layout_hint
    repurpose.layout_weights = lambda: playbook.weights("beat_layout")


def uninstall() -> None:
    from ..pipeline import explainer, repurpose

    explainer.layout_hint = None
    repurpose.layout_weights = None


# --------------------------------------------------------------------------
# 周报
# --------------------------------------------------------------------------
def report(rows: list[dict[str, Any]], limit: int = 5) -> list[dict[str, Any]]:
    """最近几条作品各自的逐拍情况:有跳过率就点名掉人拍,没有就用平均观看秒数说观众在第几拍走。

    `rows` 是 `features.rows`(带 outcome.avg_watch_sec),按发布时间新的在前取 `limit` 条。"""
    curves = db.list_progress_curves()
    out = []
    for r in sorted(rows, key=lambda r: -r["published_at"])[:limit]:
        beats = beat_map(r["project_id"])
        if not beats:
            continue
        item: dict[str, Any] = {"title": r["title"], "n_beats": len(beats), "worst": [],
                                "leave": None}
        curve = curves.get(r["pub_id"])
        scored = beat_skips(curve["points"], beats) if curve else []
        if scored:
            pos = {b["idx"]: n + 1 for n, b in enumerate(beats)}
            item["worst"] = [{"pos": pos[b["idx"]], "start": b["start"],
                              "layout": LAYOUT_LABEL.get(b["layout"], b["layout"]),
                              "section": b["section"], "rel": b["rel"]}
                             for b in sorted(scored, key=lambda b: -b["rel"])
                             if b["rel"] >= WORST_REL][:3]
        left = leave_beat(beats, r["outcome"].get("avg_watch_sec"))
        if left:
            item["leave"] = {"pos": left["pos"], "at": left["at"], "section": left["section"],
                             "layout": LAYOUT_LABEL.get(left["layout"], left["layout"])}
        item["source"] = "progress" if scored else "avg_watch"
        out.append(item)
    return out


def report_lines(items: list[dict[str, Any]]) -> list[str]:
    lines = []
    for it in items:
        bits = []
        if it.get("leave"):
            lv = it["leave"]
            bits.append(f"平均观众看到第 {lv['pos']}/{it['n_beats']} 拍({lv['at']:g} 秒,"
                        f"{lv['section'] or '—'}·{lv['layout']})就走了")
        for w in it.get("worst") or []:
            bits.append(f"第 {w['pos']} 拍({w['start']:g} 秒起,{w['section'] or '—'}·{w['layout']})"
                        f"被拖过去的是全片平均的 {w['rel']:.1f} 倍")
        if bits:
            lines.append(f"逐拍:《{it['title']}》" + ";".join(bits))
    return lines
