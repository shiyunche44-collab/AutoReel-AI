"""对标号自动发现:按品类关键词搜抖音同类热门视频,挑出反复出现的中腰部作者,加进对标列表。

为什么不盯大号:几百万粉的号靠的是账号本身,新号学不来;**1 万–50 万粉、同类关键词下
多条视频都有量**的号,选题、标题、节奏才有可复制性。官方号 / 门店 / 营销号过滤掉。

入库只记账号(`benchmark.collect` 之后会去抓它们的主页作品算爆款倍数),搜索结果本身不入库。

**反爬:** 一次只开一个无头浏览器、关键词之间停几秒、每轮关键词有上限;任何一次失败
(登录失效 / 验证码 / 接口没出来)就停手,不连着试。
"""
from __future__ import annotations

import logging
import math
import re
import time
from collections import defaultdict
from typing import Any, Callable

from .. import db

log = logging.getLogger(__name__)

#: 对标号不足这么多个才去找新的
WANT_ACCOUNTS = 8
#: 一轮最多新增几个
MAX_NEW = 5
#: 一轮最多搜几个关键词 / 关键词之间停几秒
MAX_KEYWORDS = 5
GAP_SECONDS = 10
#: 粉丝范围(可复制的中腰部号)
MIN_FOLLOWERS, MAX_FOLLOWERS = 10_000, 500_000
#: 搜到的视频至少这么多赞才算"同类里有量"
MIN_LIKES = 500
#: 官方号 / 门店 / 营销号
_SKIP_NAME = re.compile(r"官方|旗舰|4S|专营|经销|销售|门店|直营|集团|有限公司|汽车之家|懂车帝|车市")
NOTE = "自动发现"
#: 搜索页被拦(验证码中间页)后冷却多久;连着被拦会翻倍(见 providers/cooldown.py)
SEARCH_COOLDOWN = 6 * 3600.0
SEARCH_COOLDOWN_KEY = "douyin.search"


def keywords(category: str, limit: int = MAX_KEYWORDS) -> list[str]:
    """搜什么:品类 profile 里的口径词 + 最新一批热点里靠前的对象加"测评"。"""
    from .. import categories
    from . import scout

    out: list[str] = []
    try:
        out += list(categories.load_profile(category).trends.bili_queries)
    except Exception as exc:  # noqa: BLE001 - 品类没配口径就只用热点词
        log.info("读品类搜索词失败:%s", exc)
    cur = scout.latest(category)
    if cur:
        for r in db.trend_rows(cur["batch"]):
            car = (r.get("car") or "").strip()
            if car and f"{car} 测评" not in out:
                out.append(f"{car} 测评")
            if len(out) >= limit * 2:
                break
    seen: set[str] = set()
    uniq = [k for k in out if not (k in seen or seen.add(k))]
    return uniq[:limit]


def _int(v: Any) -> int:
    try:
        return int(v or 0)
    except (TypeError, ValueError):
        return 0


def rank_authors(batches: list[list[dict[str, Any]]], exclude: set[str]) -> list[dict[str, Any]]:
    """多个关键词的搜索结果 → 候选作者,分高的在前。

    分 = 出现的视频条数 × 10 + log10(总点赞) × 5;出现在多个关键词下再加 5(同类里稳定有量)。
    粉丝数拿不到(0)的号不过滤粉丝区间,只是分数里没有加成。"""
    by: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"videos": 0, "likes": 0, "keywords": set(), "followers": 0, "nickname": ""})
    for i, items in enumerate(batches):
        for a in items:
            au = a.get("author") or {}
            sec = str(au.get("sec_uid") or "")
            likes = _int((a.get("statistics") or {}).get("digg_count"))
            if not sec or sec in exclude or likes < MIN_LIKES:
                continue
            d = by[sec]
            d["videos"] += 1
            d["likes"] += likes
            d["keywords"].add(i)
            d["followers"] = max(d["followers"], _int(au.get("follower_count")))
            d["nickname"] = d["nickname"] or str(au.get("nickname") or "")
    out = []
    for sec, d in by.items():
        f = d["followers"]
        if _SKIP_NAME.search(d["nickname"]) or (f and not MIN_FOLLOWERS <= f <= MAX_FOLLOWERS):
            continue
        score = d["videos"] * 10 + math.log10(d["likes"] + 1) * 5 + (5 if len(d["keywords"]) > 1 else 0)
        out.append({"sec_uid": sec, "nickname": d["nickname"], "followers": f, "videos": d["videos"],
                    "likes": d["likes"], "score": round(score, 1)})
    return sorted(out, key=lambda x: -x["score"])


def discover(category: str, *, search: Callable[[str], list[dict[str, Any]]] | None = None,
             sleep: Callable[[float], None] = time.sleep, max_new: int = MAX_NEW) -> dict[str, Any]:
    """搜一轮并把新作者加进对标列表。返回 {"keywords", "found", "added": [昵称], "error"}。"""
    from ..publish.base import PublishError
    from ..publish.platforms import douyin
    from . import benchmark

    out: dict[str, Any] = {"keywords": 0, "found": 0, "added": [], "error": ""}
    room = benchmark.MAX_ACCOUNTS - len(db.list_benchmark_accounts())
    if room <= 0:
        out["error"] = f"对标账号已满 {benchmark.MAX_ACCOUNTS} 个"
        return out
    from ..providers import cooldown

    try:
        cooldown.check(SEARCH_COOLDOWN_KEY)
    except cooldown.Cooling as exc:
        out["error"] = str(exc)
        return out
    fetch = search or douyin.fetch_search_posts
    batches: list[list[dict[str, Any]]] = []
    for i, kw in enumerate(keywords(category)):
        if i:
            sleep(GAP_SECONDS)
        try:
            batches.append(fetch(kw))
        except PublishError as exc:
            if exc.credential:
                db.set_team_setting(benchmark._LOGIN_KEY, time.time())
            out["error"] = f"搜「{kw}」:{exc}"
            if not exc.credential:
                # 验证码中间页 / 风控:隔一段再试,别每轮都去撞(登录失效另有提醒,不在这里冷却)
                cooldown.trip(SEARCH_COOLDOWN_KEY, SEARCH_COOLDOWN, out["error"])
            break
        out["keywords"] += 1
    exclude = {a["id"] for a in db.list_benchmark_accounts()}
    ranked = rank_authors(batches, exclude)
    out["found"] = len(ranked)
    for c in ranked[:min(max_new, room)]:
        if db.add_benchmark_account(c["sec_uid"], url=benchmark.USER_URL + c["sec_uid"],
                                    note=f"{NOTE}:{c['videos']}条/{c['followers'] or '?'}粉"):
            if c["nickname"]:
                db.update_benchmark_account(c["sec_uid"], nickname=c["nickname"][:40])
            out["added"].append(c["nickname"] or c["sec_uid"][:12])
    return out
