"""观众实际搜的词:创作者中心「流量来源 → 搜索词」读回来的,喂给主编选题。

播放九成以上来自搜索,带来播放的词就那几种写法(「车名」「A和B对比」「车名+落地价」
「车名+能买吗」)。这些数据原来只存在于人读出来的 `docs/growth-log.md`,进不了选题。
平台不给接口,由每周的增长任务读页面后 POST `/api/team/search-terms` 写进来。
"""
from __future__ import annotations

import time
from typing import Any

from .. import db

KEY = "search_terms"
#: 最多留这么多条、留这么久(搜索热度过期很快)
MAX_TERMS = 200
KEEP_DAYS = 45
#: 给主编看的条数
PROMPT_TOP = 15


def _clean(term: Any) -> str:
    return " ".join(str(term or "").split())[:40]


def record(terms: list[dict[str, Any]], now: float | None = None) -> int:
    """记一批搜索词 [{"term", "views", "title"?}]。同一个词的播放累加,返回本次收下几条。"""
    now = time.time() if now is None else now
    store: dict[str, dict[str, Any]] = {
        t["term"]: t for t in (db.get_team_setting(KEY, []) or [])
        if isinstance(t, dict) and t.get("term") and now - float(t.get("at") or 0) < KEEP_DAYS * 86400}
    taken = 0
    for row in terms:
        term = _clean(row.get("term") if isinstance(row, dict) else row)
        if len(term) < 2:
            continue
        try:
            views = max(0, int(row.get("views") or 0)) if isinstance(row, dict) else 0
        except (TypeError, ValueError):
            views = 0
        old = store.get(term) or {"term": term, "views": 0, "titles": []}
        title = _clean(row.get("title")) if isinstance(row, dict) else ""
        titles = [*old.get("titles", [])]
        if title and title not in titles:
            titles.append(title)
        store[term] = {"term": term, "views": int(old.get("views") or 0) + views,
                       "titles": titles[-3:], "at": now}
        taken += 1
    rows = sorted(store.values(), key=lambda t: (-t["views"], -t["at"]))[:MAX_TERMS]
    db.set_team_setting(KEY, rows)
    return taken


def top(limit: int = PROMPT_TOP, now: float | None = None) -> list[dict[str, Any]]:
    now = time.time() if now is None else now
    rows = [t for t in (db.get_team_setting(KEY, []) or [])
            if isinstance(t, dict) and now - float(t.get("at") or 0) < KEEP_DAYS * 86400]
    return sorted(rows, key=lambda t: -int(t.get("views") or 0))[:limit]


def prompt_text(now: float | None = None) -> str:
    """给主编的一段话;没有数据返回空串。"""
    rows = top(now=now)
    if not rows:
        return ""
    lines = "\n".join(f"- 「{t['term']}」带来 {t['views']} 次播放" for t in rows)
    return ("## 观众实际在搜的词(本账号作品的搜索来源,按带来的播放排)\n" + lines +
            "\n账号的播放九成来自搜索。选题和 hook 优先对准这些问法:同一种问法换一台"
            "热度榜上的车(「A和B对比」「车名+落地价」「车名+能买吗」),"
            "不要重复已经做过的同一台车。")
