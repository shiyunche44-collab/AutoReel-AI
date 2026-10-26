"""每条发出去的作品抽特征 + 结果,给复盘官统计用。

**特征**:做这条片时我们能选的东西 —— 车型、片型、开头 / 结尾写法、视觉包、标题写法、
封面版本、画幅、时长档、发布星期 / 时段。都从项目和发布记录上读,不用 LLM。

**结果**:发布后约 24 小时那张快照(分析师 24h 那次)。各条作品在同一个时间点上比,
才不会把「发得早、累计久」当成「效果好」。还没到 24 小时的作品不算。
"""
from __future__ import annotations

import datetime as dt
import json
import statistics
from typing import Any

from .. import db

#: 统计的维度:(键, 中文名)
DIMENSIONS = (
    ("subject", "车型"), ("theme", "片型"), ("opening", "开头"), ("ending", "结尾"),
    ("look", "视觉包"), ("title_kind", "标题写法"), ("cover", "封面"), ("aspect", "画幅"),
    ("duration", "时长"), ("weekday", "发布星期"), ("hour", "发布时段"),
    # 生产旋钮(`pipeline.knobs`):建项目时挑的「想要」
    ("pace", "剪辑节奏"), ("hook_visual", "开场画面"), ("concept_level", "概念动画量"),
    ("speed", "语速"), ("voice", "配音音色"), ("cover_tone", "封面底色"),
    # 实测(成片 features.json 分档):实际做成的样子,素材不够 / 降级时和旋钮不一样
    ("m_cuts", "实测切换密度"), ("m_first", "实测开场版式"), ("m_concept", "实测概念动画"),
    ("m_official", "实测官方实拍占比"), ("m_cps", "实测语速"),
)
#: 逐拍维度(`beat_retention`)只用于展示:不从作品特征里抽,单独学
DIMENSION_LABEL = {**dict(DIMENSIONS), "beat_layout": "逐拍·版式", "beat_role": "逐拍·位置",
                   "beat_footage": "逐拍·素材", "beat_section": "逐拍·段落",
                   "reply_style": "回复写法"}
#: 旋钮维度(取值就是 `knobs` 里的取值)
KNOB_DIMENSIONS = ("pace", "hook_visual", "concept_level", "speed", "voice", "cover_tone")
#: 取哪个时间窗里的第一张快照当「24 小时结果」(小时)
DAY_WINDOW = (20.0, 72.0)
#: 早期诊断取最早那张快照,发布后多久之内算「早期」(小时)
EARLY_WINDOW = 24.0
#: 学这些平台的作品;一稿多发的切片 / 笔记也学,各自的基线分开算(`stats.lifts`)
PLATFORMS = ("douyin", "bilibili", "xiaohongshu")
#: 主阵地:周报的总数、迭代趋势、早期诊断、逐拍、对标对照只看这里的主片
MAIN_PLATFORMS = ("douyin",)
#: 切片 / 笔记只学这些维度 —— 开场、节奏、成片实测说的是主片,切出来的段不一定带着
PIECE_DIMENSIONS = ("subject", "theme", "title_kind", "cover", "aspect", "weekday", "hour")
PLATFORM_LABEL = {"douyin": "抖音", "bilibili": "B站", "xiaohongshu": "小红书"}
PIECE_LABEL = {"": "主片", "note": "图文笔记", "clip1": "切片", "clip2": "切片"}


def is_main(item: dict[str, Any]) -> bool:
    """发布记录 / 作品行是不是主阵地的主片。"""
    return (item.get("platform") or "douyin") in MAIN_PLATFORMS and not item.get("piece")


def main_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [r for r in rows if is_main(r)]


def piece_kind(piece: str | None) -> str:
    """clip1 / clip2 都是切片,基线放一起。"""
    p = str(piece or "")
    return "clip" if p.startswith("clip") else p


def platform_label(platform: str, piece: str | None = "") -> str:
    name = PLATFORM_LABEL.get(platform, platform)
    kind = piece_kind(piece)
    return name if not kind else f"{name}{PIECE_LABEL.get(str(piece), kind)}"


def value_label(dimension: str, value: str) -> str:
    """取值的中文名(开头 conflict → 冲突开场);查不到原样返回。"""
    from ..pipeline import looks, packaging, variety

    table: dict[str, Any] = {
        "opening": variety.OPENINGS, "ending": variety.ENDINGS, "look": looks.LOOKS,
    }.get(dimension, {})
    if value in table:
        return table[value].label
    if dimension == "title_kind":
        return packaging.KIND_LABEL.get(value, value)
    if dimension == "cover":
        return f"封面 {value.upper()}"
    if dimension == "reply_style":
        from .engagement import REPLY_STYLES

        return REPLY_STYLES.get(value, value)
    if dimension.startswith("beat_"):
        from . import beat_retention

        return beat_retention.label(dimension, value)
    if dimension in KNOB_DIMENSIONS:
        from ..pipeline import knobs

        return knobs.value_name(dimension, value)
    return value


def _hours(snap: dict[str, Any], published_at: float) -> float:
    h = snap.get("hours_since_publish")
    return float(h) if h is not None else (snap["captured_at"] - published_at) / 3600


def _usable(snap: dict[str, Any]) -> bool:
    """小红书以前抓的是数据中心的**账号汇总**(每篇都记成一样的数),只认对上了单篇笔记的快照。"""
    if snap.get("platform") != "xiaohongshu":
        return True
    try:
        raw = json.loads(snap.get("raw") or "{}")
    except (TypeError, ValueError):
        return False
    return isinstance(raw, dict) and raw.get("scope") == "note"


def day_snapshot(pub: dict[str, Any]) -> dict[str, Any] | None:
    t0 = pub.get("published_at")
    if not t0:
        return None
    lo, hi = DAY_WINDOW
    for snap in db.list_snapshots(pub["id"], limit=1000):
        if lo <= _hours(snap, t0) <= hi and snap.get("views") is not None and _usable(snap):
            return snap
    return None


def duration_bucket(seconds: Any) -> str:
    try:
        s = int(seconds)
    except (TypeError, ValueError):
        return ""
    if s <= 90:
        return "≤90s"
    if s <= 180:
        return "90-180s"
    if s <= 300:
        return "3-5min"
    return ">5min"


def hour_bucket(ts: float) -> str:
    h = dt.datetime.fromtimestamp(ts).hour
    if h < 11:
        return "上午"
    if h < 14:
        return "中午"
    if h < 18:
        return "下午"
    if h < 21:
        return "晚上"
    return "深夜"


def _band(x: Any, edges: tuple[float, ...], names: tuple[str, ...]) -> str:
    """x 落在哪一档:edges 是升序分界,names 比 edges 多一个。取不到 → 空串。"""
    try:
        v = float(x)
    except (TypeError, ValueError):
        return ""
    for edge, name in zip(edges, names):
        if v < edge:
            return name
    return names[-1]


def measured(m: dict[str, Any] | None) -> dict[str, str]:
    """成片实测(`features.json`)→ 分档特征。老项目没有这个文件 → 全空(统计跳过)。"""
    m = m or {}
    return {
        "m_cuts": _band(m.get("cuts_per_minute"), (12, 20), ("慢切", "常规", "快切")),
        "m_first": str(m.get("first_layout") or ""),
        "m_concept": _band(m.get("concept_share"), (0.1, 0.18), ("少", "中", "多")),
        "m_official": _band(m.get("official_video_ratio"), (0.4, 0.7), ("低", "中", "高")),
        "m_cps": _band(m.get("chars_per_second"), (4.5, 5.3), ("慢", "中", "快")),
    }


def extract(pub: dict[str, Any], project: dict[str, Any] | None) -> dict[str, str]:
    """一条发布记录的特征。取不到的维度留空串(统计时跳过)。"""
    from ..pipeline import variety

    project = project or {}
    pv = variety.parse(project.get("variant"))
    tv = variety.parse(pub.get("variant"))
    t0 = pub.get("published_at") or 0
    out = {
        "subject": str(project.get("subject") or ""),
        "theme": str(project.get("theme") or ""),
        "opening": str(pv.get("opening") or ""),
        "ending": str(pv.get("ending") or ""),
        "look": str(pv.get("look") or ""),
        "title_kind": str(tv.get("title_kind") or ""),
        "cover": str(tv.get("cover") or ""),
        "aspect": str(pub.get("aspect") or project.get("aspect") or ""),
        "duration": duration_bucket(project.get("duration")),
        "weekday": "周" + "一二三四五六日"[dt.datetime.fromtimestamp(t0).weekday()] if t0 else "",
        "hour": hour_bucket(t0) if t0 else "",
        **{k: str(pv.get(k) or "") for k in KNOB_DIMENSIONS},
        **measured(db.read_json(project["id"], "features.json", None) if project.get("id") else None),
    }
    if pub.get("piece"):
        out = {k: (v if k in PIECE_DIMENSIONS else "") for k, v in out.items()}
    return out


def outcome(snap: dict[str, Any]) -> dict[str, Any]:
    views = snap.get("views")
    eng = sum(int(snap.get(k) or 0) for k in ("likes", "comments", "favorites", "shares"))
    bounce = snap.get("bounce_2s")
    watch = snap.get("avg_watch_sec")
    rate = snap.get("completion_rate")
    # 抖音在没有播放数据时把深度指标一律填 0:跳出率 0.000000、平均观看 0 秒、完播 0。
    # 那不是「留存 100%」,是「还没人看」—— 播放为 0 时这些数当没有,否则一条零播放的
    # 作品会在统计里拖出一条假的高留存基线,后面每条真实作品都跟着被比低了。
    if not views:
        bounce = watch = rate = None
    elif watch is not None and float(watch) <= 0:
        watch = None
    return {
        "views": views,
        "completion_rate": rate,
        "bounce_2s": bounce,
        # 2 秒留存 = 1 − 2 秒跳出率。跟时长无关,能横着比不同长度的作品。
        "retention_2s": round(1.0 - float(bounce), 4) if bounce is not None else None,
        "avg_watch_sec": watch,
        "followers_gained": snap.get("followers_gained"),
        "engagement": round(eng / views, 4) if views else None,
    }


def rows(since: float | None = None) -> list[dict[str, Any]]:
    """有 24 小时结果的作品:[{pub_id, account_id, platform, piece, title, published_at,
    features, outcome}]。各平台、切片 / 笔记都在里面;只要主片用 `main_rows`。"""
    out = []
    for pub in db.list_publications(limit=1000):
        if pub["platform"] not in PLATFORMS or pub["state"] != "published":
            continue
        if since is not None and (pub.get("published_at") or 0) < since:
            continue
        snap = day_snapshot(pub)
        if not snap:
            continue
        out.append({"pub_id": pub["id"], "account_id": pub["account_id"],
                    "platform": pub["platform"], "piece": pub.get("piece") or "",
                    "project_id": pub["project_id"], "title": pub.get("title") or "",
                    "published_at": pub["published_at"],
                    "features": extract(pub, db.get_project(pub["project_id"])),
                    "outcome": outcome(snap)})
    return out


def platform_summary(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """周报用:各平台 × 主片 / 切片 / 笔记各有几条 24 小时数据、播放中位数、互动率中位数。"""
    groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for r in rows:
        groups.setdefault((r.get("platform") or "douyin", piece_kind(r.get("piece"))), []).append(r)
    out = []
    for (platform, kind), rs in sorted(groups.items(), key=lambda kv: -len(kv[1])):
        views = [r["outcome"]["views"] for r in rs if r["outcome"].get("views") is not None]
        eng = [r["outcome"]["engagement"] for r in rs if r["outcome"].get("engagement") is not None]
        out.append({"platform": platform, "piece": kind,
                    "label": platform_label(platform, rs[0].get("piece")), "works": len(rs),
                    "median_views": statistics.median(views) if views else None,
                    "engagement": round(statistics.median(eng), 4) if eng else None})
    return out


# --------------------------------------------------------------------------
# 早期行:不等 24 小时,发布后 1 小时那张快照就能诊断
# --------------------------------------------------------------------------
def early_snapshot(pub: dict[str, Any]) -> dict[str, Any] | None:
    """最早的那张**有播放数据**的快照(发布后 1 小时左右那张)。

    刚发出去时那张快照播放是 0,深度指标全被平台填成 0 —— 拿它诊断会得出
    「留存 100%」的假结论。跳过它,找下一张真有数的。"""
    t0 = pub.get("published_at")
    if not t0:
        return None
    for snap in db.list_snapshots(pub["id"], limit=1000):
        if not snap.get("views"):
            continue
        if _hours(snap, t0) <= EARLY_WINDOW:
            return snap
    return None


def early_rows(since: float | None = None) -> list[dict[str, Any]]:
    """已经拿到早期快照的作品行。跟 `rows` 一样能抽特征,只是结果取的是最早那张。"""
    out = []
    for pub in db.list_publications(limit=1000):
        # 早期诊断看的是 2 秒留存 / 完播,只有主阵地的主片有
        if not is_main(pub) or pub["state"] != "published":
            continue
        if since is not None and (pub.get("published_at") or 0) < since:
            continue
        snap = early_snapshot(pub)
        if not snap:
            continue
        out.append({"pub_id": pub["id"], "account_id": pub["account_id"],
                    "project_id": pub["project_id"], "title": pub.get("title") or "",
                    "published_at": pub["published_at"],
                    "hours": round(_hours(snap, pub["published_at"]), 2),
                    "features": extract(pub, db.get_project(pub["project_id"])),
                    "outcome": outcome(snap)})
    return out
