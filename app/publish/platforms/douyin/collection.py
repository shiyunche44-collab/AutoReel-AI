"""把已发布的作品加进同名合集(栏目 = 合集,名字一字不差)。

发布页和「编辑作品」页都**没有**合集选项(2026-11-09 实看),只能走合集编辑页:

1. 作品管理 →「作品合集」页签,找标题正好是栏目名的合集卡片,点「编辑合集」;
2. 合集编辑页点「点击添加作品」,右侧抽屉「作品列表」里按作品描述搜;
3. 命中的那一行点右边的「+」,再点页面底部「保存」。

抽屉里只列**已公开**的作品:刚发出去还在审核的搜不到,调用方隔一阵再来。
类名都是构建期哈希(`drawer-item-mTdppj`),只按前缀 + 文案定位。
"""
from __future__ import annotations

import logging
import re
from typing import Any

from .consts import CREATOR_MANAGE

log = logging.getLogger(__name__)

COLLECTIONS_URL = f"{CREATOR_MANAGE}?tab=collections"
#: 搜作品用标题前多少个字(抽屉按描述模糊搜,太长反而搜不到)
SEARCH_CHARS = 12

#: 结果:加进去了 / 本来就在 / 合集不存在 / 作品还搜不到(多半在审核)/ 出错
ADDED, ALREADY, NO_COLLECTION, NOT_FOUND, FAILED = (
    "added", "already", "no_collection", "not_found", "failed")


def search_key(title: str) -> str:
    """搜索词:标题去掉空白和话题,取前 SEARCH_CHARS 个字。"""
    text = re.sub(r"#\S+", "", title or "")
    text = re.sub(r"\s+", "", text)
    return text[:SEARCH_CHARS]


def card_count(text: str) -> int | None:
    """合集卡片上的「N个作品」→ N。"""
    m = re.search(r"(\d+)\s*个作品", text or "")
    return int(m.group(1)) if m else None


def _card(page: Any, name: str) -> Any:
    """标题正好是 name 的合集卡片;没有返回 None。"""
    title = page.get_by_text(name, exact=True)
    card = page.locator("div[class*='collection-card-new']").filter(has=title)
    return card.first if card.count() else None


def _close_drawer(page: Any) -> None:
    """关「作品列表」抽屉:它的遮罩盖着底部「保存」,不关点不到。

    Escape 关不掉(实测);点标题右边的 ×,还关不掉就点遮罩。"""
    mask = page.locator(".semi-sidesheet-mask")
    if not mask.count():
        return
    try:
        head = page.locator("div[class*='title-']", has_text="作品列表").first
        head.locator("span[class*='icon-']").first.click(timeout=3000)
        page.wait_for_timeout(800)
    except Exception as exc:  # noqa: BLE001 - 换点遮罩
        log.debug("点 × 没关上抽屉: %s", exc)
    if mask.count():
        mask.first.click(position={"x": 40, "y": 300}, timeout=3000)
        page.wait_for_timeout(800)


def add_work(session: Any, name: str, title: str) -> tuple[str, str]:
    """把标题为 title 的作品加进合集 name。返回 (结果, 说明)。

    不抛异常:页面对不上就返回 FAILED + 说明,调用方记下来下次再试。"""
    key = search_key(title)
    if not name or not key:
        return FAILED, "栏目名或标题为空"
    page = session.page
    try:
        session.goto(COLLECTIONS_URL, wait_ms=4000)
        card = _card(page, name)
        if card is None:
            return NO_COLLECTION, f"抖音上没有叫「{name}」的合集(去作品管理 → 作品合集建一个)"
        before = card_count(card.inner_text())
        card.get_by_text("编辑合集", exact=True).click()
        page.wait_for_url(re.compile(r"collection/detail"), timeout=20000)
        page.wait_for_timeout(2500)
        # 合集内作品区已经有这条:不用再加
        if key in (session.page_text() or ""):
            return ALREADY, "作品已在合集里"
        page.get_by_text("点击添加作品", exact=True).click()
        box = page.locator("input[placeholder*='查找作品']").first
        box.wait_for(timeout=10000)
        box.fill(key)
        box.press("Enter")
        page.wait_for_timeout(2500)
        row = page.locator("div[class*='drawer-item-']").filter(has_text=key)
        if not row.count():
            return NOT_FOUND, f"作品列表里搜不到「{key}」(还在审核或不是公开作品)"
        row.first.locator("[class*='plus-area']").first.click()
        page.wait_for_timeout(1000)
        _close_drawer(page)
        page.locator("button", has_text=re.compile(r"^\s*保存\s*$")).first.click()
        page.wait_for_timeout(4000)
        # 回列表核对卡片上的作品数涨了
        session.goto(COLLECTIONS_URL, wait_ms=4000)
        card = _card(page, name)
        after = card_count(card.inner_text()) if card is not None else None
        if before is not None and after is not None and after > before:
            return ADDED, f"已加进合集「{name}」(现在 {after} 个作品)"
        return FAILED, f"点了保存但合集「{name}」作品数没变({before} → {after})"
    except Exception as exc:  # noqa: BLE001 - 页面改版 / 超时:下次再试
        log.info("加合集失败: %s", exc)
        return FAILED, f"加合集出错:{type(exc).__name__}: {str(exc)[:120]}"


def add_to_collection(name: str, title: str) -> tuple[str, str]:
    """开一个已登录的浏览器会话把作品加进合集。"""
    from ... import browser

    try:
        with browser.BrowserSession("douyin") as session:
            result = add_work(session, name, title)
            session.save_state()
            return result
    except browser.BrowserUnavailable as exc:
        return FAILED, str(exc)[:160]
