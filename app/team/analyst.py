"""分析师:发布后第 1h / 24h / 72h / 7d 从抖音创作者中心各抓一次数据。

**为什么走创作者中心**:网页版发布拿不到作品 id,开放平台的数据接口又要资质。
创作者中心「作品管理」页自己会请求一个作品列表接口(`douyin.fetch_creator_works`),
每条作品带播放、点赞、评论、分享、收藏,还有开放平台都不给的完播率、平均播放秒数、
2 秒跳出率和涨粉。打开一次页面就拿到最近一批作品,所有到点的作品一起落快照。

**先对上作品**:网页版发出去的发布记录没有 `remote_id`。按标题 + 发布时间在作品
列表里找到它,补上作品 id 和链接,状态改成已发布(平台还在审就留在已提交)。
「发布后没找到成功提示」记成失败、其实已经发出去的,也在这里认领回来 ——
不认领的话它会被当成没发,数据也永远对不上。

**不重试轰炸**:登录失效记一次失败、提示去分发台重新扫码,之后就不再开浏览器,
直到登录态文件更新(说明你扫过码了)。
"""
from __future__ import annotations

import datetime as dt
import logging
import re
import time
from typing import Any

from .. import db
from . import Skip, TeamError

log = logging.getLogger(__name__)

#: 发布后这几个时刻各抓一次(小时)
MILESTONES = (1.0, 24.0, 72.0, 168.0)
#: 过了最后一个时刻再宽限一天,还没抓到就算了
GIVE_UP_HOURS = MILESTONES[-1] + 24
#: 发布记录和平台作品的时间最多差这么久才认(提交时刻 vs 平台记的发布时刻)
MATCH_WINDOW = 6 * 3600
#: 还没对上作品的发布记录,只在发出后这么久之内去找(找不到多半是标题被改了,别一直开浏览器)
RECLAIM_HOURS = 24
#: 网页版发布超时的那句报错(`douyin._publish_browser`),这种失败其实可能已经发出去了
_UNSURE_ERROR = "没找到成功提示"
_LOGIN_KEY = "analyst_login_bad"


# --------------------------------------------------------------------------
# 纯函数:对作品、算到点
# --------------------------------------------------------------------------
def _norm(text: str) -> str:
    """比标题用:去掉话题标签、空白和标点。"""
    text = re.sub(r"[#＃]\S+", "", str(text or ""))
    return re.sub(r"[\s\W_]+", "", text).lower()


def _same_title(a: str, b: str) -> bool:
    a, b = _norm(a), _norm(b)
    if not a or not b:
        return False
    if a == b:
        return True
    short, long_ = sorted((a, b), key=len)
    return len(short) >= 6 and long_.startswith(short)  # 平台标题框 30 字,可能截过


def _work_title(work: dict[str, Any]) -> str:
    aweme = work.get("aweme") or {}
    return aweme.get("item_title") or work.get("description") or aweme.get("desc") or ""


def submitted_at(pub: dict[str, Any]) -> float:
    """发布记录发出去的时刻:有平台时间用平台的,没有就用最后一次更新(点完发布那一刻)。"""
    return pub.get("published_at") or pub.get("updated_at") or pub.get("created_at") or 0.0


def match_work(pub: dict[str, Any], works: list[dict[str, Any]],
               taken: set[str] = frozenset()) -> dict[str, Any] | None:
    """在作品列表里找这条发布记录:标题对得上、时间差在窗口内,取时间最近的。"""
    if pub.get("remote_id"):
        return next((w for w in works if w["id"] == pub["remote_id"]), None)
    t0 = submitted_at(pub)
    best, gap = None, MATCH_WINDOW + 1
    for w in works:
        if w["id"] in taken or not _same_title(pub.get("title") or "", _work_title(w)):
            continue
        d = abs(float(w.get("create_time") or 0) - t0)
        if d <= MATCH_WINDOW and d < gap:
            best, gap = w, d
    return best


def due_milestone(pub: dict[str, Any], snaps: list[dict[str, Any]], now: float) -> float | None:
    """该抓的时刻(小时):已过的最近一个时刻还没有快照就返回它,否则 None。

    停机错过了中间的时刻不补,只抓最近的那个。"""
    t0 = pub.get("published_at")
    if not t0:
        return None
    age = (now - t0) / 3600
    if age > GIVE_UP_HOURS:
        return None
    passed = [m for m in MILESTONES if age >= m]
    if not passed:
        return None
    stage = passed[-1]
    if any((s.get("hours_since_publish") or 0) >= stage for s in snaps):
        return None
    return stage


def candidates(now: float) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """(还没对上作品的, 已对上且有时刻到点的) 抖音发布记录。"""
    unmatched, due = [], []
    for pub in db.list_publications(limit=500):
        if pub["platform"] != "douyin":
            continue
        if pub["state"] == "failed":
            if _UNSURE_ERROR in (pub.get("error") or "") and not pub.get("remote_id") \
                    and now - submitted_at(pub) <= RECLAIM_HOURS * 3600:
                unmatched.append(pub)
            continue
        if pub["state"] not in ("submitted", "published"):
            continue
        if not pub.get("remote_id"):
            if now - submitted_at(pub) <= RECLAIM_HOURS * 3600:
                unmatched.append(pub)
            continue
        if due_milestone(pub, db.list_snapshots(pub["id"], limit=1000), now) is not None:
            due.append(pub)
    return unmatched, due


# --------------------------------------------------------------------------
# 落库
# --------------------------------------------------------------------------
def _claim(pub: dict[str, Any], work: dict[str, Any]) -> dict[str, Any]:
    """把平台作品挂到发布记录上:作品 id、链接、平台发布时刻、状态。"""
    from ..metrics.collector import douyin_work_state

    state = douyin_work_state(work)
    fields: dict[str, Any] = {
        "remote_id": work["id"],
        "remote_url": f"https://www.douyin.com/video/{work['id']}",
        "published_at": float(work.get("create_time") or 0) or submitted_at(pub),
        "state": "submitted" if state == "reviewing" else "published",
    }
    if pub["state"] == "failed":
        fields["error"] = ""
    db.update_publication(pub["id"], **fields)
    note = "认领回来(其实已发出)" if pub["state"] == "failed" else "对上作品"
    db.add_event(pub["project_id"], "team",
                 reason=f"分析师{note}:《{pub.get('title') or ''}》→ 抖音作品 {work['id']}")
    return {**pub, **fields}


def _record(pub: dict[str, Any], work: dict[str, Any], now: float) -> bool:
    from ..metrics.collector import douyin_work_state, parse_douyin_creator

    metrics = parse_douyin_creator(work)
    if not metrics or metrics.get("views") is None:
        return False
    metrics["hours_since_publish"] = round((now - pub["published_at"]) / 3600, 2)
    raw = {"metrics": work.get("metrics"), "review": work.get("review"),
           "state": douyin_work_state(work)}
    db.add_snapshot(pub["id"], "douyin", now, metrics, raw)
    return True


def _login_blocked() -> bool:
    """上次登录失效后还没重新扫码:不再开浏览器。"""
    from ..publish import browser

    bad = db.get_team_setting(_LOGIN_KEY, None)
    if not bad:
        return False
    path = browser.state_path("douyin")
    return not (path.is_file() and path.stat().st_mtime > float(bad))


#: 发布满这么多小时的快照顺手抓「进度条分析」(页面说发布半小时后才有;一天后才有人拖)
PROGRESS_AFTER_HOURS = 20.0


def _record_progress(pubs: list[dict[str, Any]], views: dict[str, int | None], now: float,
                     fetch_progress) -> int:
    """抓进度条分析(每个点的跳过率 / 回看率)存库。抓不到只记日志:它只是复盘用的。"""
    from ..publish.base import PublishError

    if not pubs:
        return 0
    try:
        got = fetch_progress([p["remote_id"] for p in pubs])
    except PublishError as exc:
        log.info("进度条分析没抓到:%s", exc.message)
        return 0
    n = 0
    for p in pubs:
        if got.get(p["remote_id"]):
            db.save_progress_curve(p["id"], got[p["remote_id"]], views.get(p["id"]), ts=now)
            n += 1
    return n


#: 今天为记粉丝数单独开过一次浏览器(作品列表是空的就别每小时都开)
_DAILY_TRIED_KEY = "analyst_daily_tried"


def _daily_followers(works: list[dict[str, Any]]) -> int | None:
    """作品列表里带的作者粉丝数(每条作品都带同一个数,取最大的那个)。"""
    from ..metrics.collector import parse_douyin_creator

    got = [(parse_douyin_creator(w) or {}).get("followers") or 0 for w in works]
    return max(got, default=0) or None


def collect(now: float | None = None, fetch=None, fetch_progress=None) -> dict[str, int]:
    """抓一轮。`fetch` / `fetch_progress` 测试用(默认开浏览器抓创作者中心);
    测试只给了 `fetch` 时不抓进度条分析。

    顺手记今天的账号粉丝数(`account_daily`,一天一张,活跃官的效果靠它量):
    没有到点要抓的作品时,今天还没记就单独抓一次作品列表。"""
    from ..metrics.collector import parse_douyin_creator
    from ..publish.base import PublishError
    from ..publish.platforms import douyin

    now = time.time() if now is None else now
    today = dt.date.fromtimestamp(now).isoformat()
    need_daily = (today not in db.list_account_daily("douyin", today)
                  and db.get_team_setting(_DAILY_TRIED_KEY) != today)
    unmatched, due = candidates(now)
    if not unmatched and not due and not need_daily:
        raise Skip("没有到点要抓的作品")
    if fetch is None:
        if _login_blocked():
            raise Skip("抖音登录失效,等你去分发台重新扫码")
        fetch = douyin.fetch_creator_works
        fetch_progress = fetch_progress or douyin.fetch_work_progress
    if not unmatched and not due:
        db.set_team_setting(_DAILY_TRIED_KEY, today)
    oldest = min([submitted_at(p) for p in unmatched] + [p["published_at"] for p in due],
                 default=now - 86400)
    try:
        works = fetch(since=oldest - MATCH_WINDOW)
    except PublishError as exc:
        if exc.credential:
            db.set_team_setting(_LOGIN_KEY, now)
            db.add_event("__team__", "team", reason=f"分析师抓不了抖音数据:{exc.message}")
        raise TeamError(exc.message) from exc
    db.set_team_setting(_LOGIN_KEY, None)
    followers = _daily_followers(works) if need_daily else None
    daily = int(bool(followers) and db.save_account_daily("douyin", today, followers, now))
    taken = {p["remote_id"] for p in db.list_publications(limit=1000) if p.get("remote_id")}
    claimed = recorded = 0
    deep: list[dict[str, Any]] = []
    views: dict[str, int | None] = {}

    def record(pub: dict[str, Any], work: dict[str, Any]) -> bool:
        if not _record(pub, work, now):
            return False
        if now - float(pub.get("published_at") or now) >= PROGRESS_AFTER_HOURS * 3600:
            deep.append(pub)
            views[pub["id"]] = (parse_douyin_creator(work) or {}).get("views")
        return True

    for pub in unmatched:
        work = match_work(pub, works, taken)
        if not work:
            continue
        taken.add(work["id"])
        pub = _claim(pub, work)
        claimed += 1
        if record(pub, work):  # 对上时先落一张,1h 那张就是它
            recorded += 1
    for pub in due:
        work = match_work(pub, works)
        if work and record(pub, work):
            recorded += 1
    curves = _record_progress(deep, views, now, fetch_progress) if fetch_progress else 0
    return {"claimed": claimed, "recorded": recorded, "works": len(works), "curves": curves,
            "daily": daily}


def run() -> str:
    got = collect()
    if not got["claimed"] and not got["recorded"]:
        if got.get("daily"):
            return "记下今天的粉丝数"
        raise Skip(f"创作者中心 {got['works']} 条作品里没对上到点的发布")
    parts = []
    if got["claimed"]:
        parts.append(f"对上 {got['claimed']} 条作品")
    parts.append(f"落 {got['recorded']} 张数据快照")
    if got.get("daily"):
        parts.append("记下今天的粉丝数")
    if got.get("curves"):
        parts.append(f"抓 {got['curves']} 条进度条分析")
    return ",".join(parts)
