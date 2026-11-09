"""发布员顺手做:栏目片发出去之后,加进抖音上同名的合集。

合集要用户先在创作者中心建好(名字 = 栏目名,如「柒哥帮你挑」)。
刚发出去的作品还在审核,合集抽屉里搜不到 —— 发布后先等一阵,搜不到就隔几小时再试,
试够次数就放弃(记在项目目录的 `douyin_collection.json` 里,页面改版也不会无限重试)。
每轮最多处理一条,别让发布员一次占着浏览器太久。
"""
from __future__ import annotations

import logging
import time
from collections.abc import Callable
from typing import Any

from .. import config, db

log = logging.getLogger(__name__)

STATE_FILE = "douyin_collection.json"
#: 发布后至少等这么久再去加(审核期间抽屉里没有它)
FIRST_DELAY = 30 * 60.0
#: 两次尝试之间隔多久
RETRY_GAP = 2 * 3600.0
#: 最多试几次
MAX_ATTEMPTS = 6
#: 这些结果不用再试
_FINAL = {"added", "already", "no_collection"}
_OUT = {"published", "submitted"}


def _series_name(project_id: str) -> str:
    from ..pipeline import series, variety

    row = db.get_project(project_id) or {}
    return series.of_variant(variety.parse(row.get("variant")))[0]


def due(now: float | None = None) -> list[tuple[dict[str, Any], str, dict[str, Any]]]:
    """该去加合集的抖音主片:[(发布记录, 栏目名, 上次状态)],旧的在前。"""
    now = time.time() if now is None else now
    out = []
    for pub in db.list_publications(limit=200):
        if pub.get("platform") != "douyin" or pub.get("piece") or pub.get("state") not in _OUT:
            continue
        if now - float(pub.get("published_at") or pub.get("updated_at") or now) < FIRST_DELAY:
            continue
        name = _series_name(pub["project_id"])
        if not name:
            continue
        st = db.read_json(pub["project_id"], STATE_FILE, {}) or {}
        if st.get("result") in _FINAL or int(st.get("attempts") or 0) >= MAX_ATTEMPTS:
            continue
        if now - float(st.get("last") or 0) < RETRY_GAP:
            continue
        out.append((pub, name, st))
    out.sort(key=lambda x: float(x[0].get("published_at") or 0))
    return out


def run_once(adder: Callable[[str, str], tuple[str, str]] | None = None,
             now: float | None = None) -> str:
    """处理一条;没有要做的返回空串。"""
    if not getattr(config, "DOUYIN_COLLECTIONS", True):
        return ""
    todo = due(now)
    if not todo:
        return ""
    if adder is None:
        from ..publish.platforms.douyin.collection import add_to_collection as adder
    pub, name, st = todo[0]
    result, note = adder(name, pub.get("title") or "")
    db.write_json(pub["project_id"], STATE_FILE, {
        "collection": name, "publication_id": pub["id"], "result": result, "note": note,
        "attempts": int(st.get("attempts") or 0) + 1,
        "last": time.time() if now is None else now,
    })
    log.info("合集:%s → %s %s", pub.get("title"), result, note)
    return f"「{pub.get('title') or pub['project_id']}」{note}"
