"""发布员顺手做:主片发出去之后,用账号自己在评论区发第一条评论(拉互动的二选一提问)。

文案是包装阶段就写好、过了校验的那条(`packaging.json` 的 `comment.text`),这里不再生成。
**只发,不置顶** —— 置顶要在评论上再点一次菜单,那一步没在真实账号上验证过;
作者自己的评论抖音默认会带「作者」标,排在前面。

默认关(`DOUYIN_FIRST_COMMENT`):这是以账号名义往外发公开内容,真实发送路径第一次要有人盯着。
刚发出去的作品还在审核,打不开 —— 发布后先等一阵,失败就隔几小时再试,试够次数就放弃
(记在项目目录的 `douyin_first_comment.json` 里)。只管最近发的,不给老作品补发。
每轮最多处理一条;弹验证码不绕过,当天不再试。
"""
from __future__ import annotations

import logging
import time
from collections.abc import Callable
from typing import Any

from .. import config, db

log = logging.getLogger(__name__)

STATE_FILE = "douyin_first_comment.json"
#: 发布后至少等这么久再去发(审核期间作品打不开)
FIRST_DELAY = 20 * 60.0
#: 发布超过这么久的不补发(评论区的第一波早过去了;也防止开关一开就给老作品刷一遍)
MAX_AGE = 48 * 3600.0
#: 两次尝试之间隔多久
RETRY_GAP = 2 * 3600.0
#: 最多试几次
MAX_ATTEMPTS = 3
#: 弹了验证码之后停多久
VERIFY_PAUSE = 24 * 3600.0
_PAUSE_KEY = "first_comment_paused_until"
_OUT = {"published", "submitted"}


def comment_text(project_id: str) -> str:
    from ..pipeline import packaging

    pack = packaging.load(project_id)
    return (pack.comment.text if pack and pack.comment else "").strip()


def due(now: float | None = None) -> list[tuple[dict[str, Any], str, dict[str, Any]]]:
    """该去发第一条评论的抖音主片:[(发布记录, 文案, 上次状态)],新的在前。"""
    now = time.time() if now is None else now
    out = []
    for pub in db.list_publications(limit=200):
        if pub.get("platform") != "douyin" or pub.get("piece") or pub.get("state") not in _OUT:
            continue
        at = float(pub.get("published_at") or 0)
        if not pub.get("remote_id") or not at or not FIRST_DELAY <= now - at <= MAX_AGE:
            continue
        st = db.read_json(pub["project_id"], STATE_FILE, {}) or {}
        if st.get("result") == "sent" or int(st.get("attempts") or 0) >= MAX_ATTEMPTS:
            continue
        if now - float(st.get("last") or 0) < RETRY_GAP:
            continue
        text = comment_text(pub["project_id"])
        if text:
            out.append((pub, text, st))
    out.sort(key=lambda x: -float(x[0].get("published_at") or 0))
    return out


def _send(aweme_id: str, text: str) -> None:
    from ..publish import browser
    from ..publish.base import PublishError
    from ..publish.platforms import douyin_outreach

    try:
        with browser.BrowserSession("douyin", headless=True, lock_timeout=90) as session:
            douyin_outreach.comment_video(session, aweme_id, text)
    except browser.BrowserUnavailable as exc:
        raise PublishError(str(exc), retryable=False) from exc


def run_once(sender: Callable[[str, str], None] | None = None,
             now: float | None = None) -> str:
    """处理一条;没有要做的(或没开)返回空串。"""
    from ..publish.base import PublishError
    from ..publish.platforms.douyin_outreach import VerificationNeeded

    if not getattr(config, "DOUYIN_FIRST_COMMENT", False):
        return ""
    now = time.time() if now is None else now
    if now < float(db.get_team_setting(_PAUSE_KEY, 0) or 0):
        return ""
    todo = due(now)
    if not todo:
        return ""
    pub, text, st = todo[0]
    title = pub.get("title") or pub["project_id"]
    result, note = "sent", "第一条评论已发"
    try:
        (sender or _send)(str(pub["remote_id"]), text)
    except VerificationNeeded as exc:
        db.set_team_setting(_PAUSE_KEY, now + VERIFY_PAUSE)
        db.add_event("__team__", "team", reason=f"发第一条评论时弹了验证:{exc.message};停一天")
        result, note = "verify", f"弹了验证,没发:{exc.message}"
    except PublishError as exc:
        result, note = "failed", f"没发出去:{exc.message}"
    db.write_json(pub["project_id"], STATE_FILE, {
        "publication_id": pub["id"], "text": text, "result": result, "note": note,
        "attempts": int(st.get("attempts") or 0) + 1, "last": now,
    })
    log.info("第一条评论:%s → %s %s", title, result, note)
    return f"「{title}」{note}"
