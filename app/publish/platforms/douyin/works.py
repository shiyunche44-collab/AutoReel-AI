"""作品管理页:作品列表、进度条分析、回查刚发出去的作品。"""
from __future__ import annotations

import json
import time
from typing import Any
from ...base import PublishError

from .consts import CREATOR_MANAGE, WORK_LIST_API, WORK_RESOLVE_GAP, WORK_RESOLVE_ROUNDS, log


def _int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def fetch_creator_works(*, since: float | None = None, max_scrolls: int = 4) -> list[dict[str, Any]]:
    """无头打开创作者中心「作品管理」,截页面自己请求的作品列表接口,只读、不点按钮。

    返回 `work_list` 的 `items[]`(带 `metrics`),每条再挂上同一次返回里
    `aweme_list` 对应那条(`work["aweme"]`:标题、审核状态、作者粉丝数)。
    第一页 12 条;给了 `since`(时间戳)且最老一条还比它新,就往下滚着加载,最多
    `max_scrolls` 次。登录失效抛 `PublishError(credential=True)`,不重试。
    """
    from ... import browser

    pages: list[dict[str, Any]] = []

    def on_response(resp: Any) -> None:
        if WORK_LIST_API not in resp.url:
            return
        try:
            body = resp.json()
        except Exception:  # noqa: BLE001
            return
        if isinstance(body, dict) and body.get("items") is not None:
            pages.append(body)

    try:
        with browser.BrowserSession("douyin", headless=True, lock_timeout=60) as session:
            session.page.on("response", on_response)
            session.goto(CREATOR_MANAGE, wait_ms=6000)
            if "扫码登录" in session.page_text():
                raise PublishError("抖音登录已失效:去分发台点「登录」扫码一次",
                                   retryable=False, credential=True)
            for _ in range(max_scrolls):
                if not pages or not pages[-1].get("has_more") or since is None:
                    break
                oldest = min((_int(i.get("create_time")) or 0) for i in pages[-1]["items"])                     if pages[-1]["items"] else 0
                if oldest <= since:
                    break
                got = len(pages)
                session.page.mouse.wheel(0, 6000)
                session.page.wait_for_timeout(3000)
                if len(pages) == got:
                    break
    except browser.BrowserUnavailable as exc:
        raise PublishError(str(exc), retryable=False) from exc
    if not pages:
        raise PublishError("作品管理页没请求作品列表接口(页面改版了?)", retryable=False)
    works: dict[str, dict[str, Any]] = {}
    for body in pages:
        awemes = {str(a.get("aweme_id")): a for a in body.get("aweme_list") or []}
        for item in body.get("items") or []:
            wid = str(item.get("id") or "")
            if wid:
                works[wid] = {**item, "id": wid, "aweme": awemes.get(wid) or {}}
    return list(works.values())


#: 作品详情页「进度条分析」接口(见 `tests/probe_douyin_retention.py`)
WORK_DETAIL = "https://creator.douyin.com/creator-micro/work-management/work-detail/{id}"
PROGRESS_API = "/janus/douyin/creator/bff/data/progress/analysis/v2?item_id={id}"
_FETCH_JSON_JS = """async (url) => {
  const r = await fetch(url, {credentials: 'include', headers: {'agw-js-conv': 'str'}});
  return {status: r.status, text: await r.text()};
}"""


def parse_progress(body: dict[str, Any]) -> list[list[float]]:
    """进度条分析 → [[秒, 跳过率, 回看率], ...],按秒排。key 是播放进度秒数,value 是整数比率
    (单位不重要:只拿来和同一条片自己的均值比)。没数据 → []。"""
    back = {str(p.get("key")): _int(p.get("value")) or 0 for p in body.get("jump_backward") or []
            if isinstance(p, dict)}
    out = []
    for p in body.get("jump_forward") or []:
        if not isinstance(p, dict):
            continue
        try:
            t = float(p.get("key"))
        except (TypeError, ValueError):
            continue
        out.append([t, float(_int(p.get("value")) or 0), float(back.get(str(p.get("key")), 0))])
    return sorted(out)


def fetch_work_progress(remote_ids: list[str]) -> dict[str, list[list[float]]]:
    """无头打开作品详情页,用页面自己的登录态 GET「进度条分析」,只读。
    返回 {作品 id: 点列};某条没拿到就不放进结果。登录失效抛 `PublishError(credential=True)`。"""
    from ... import browser

    out: dict[str, list[list[float]]] = {}
    if not remote_ids:
        return out
    try:
        with browser.BrowserSession("douyin", headless=True, lock_timeout=60) as session:
            session.goto(WORK_DETAIL.format(id=remote_ids[0]), wait_ms=5000)
            if "扫码登录" in session.page_text():
                raise PublishError("抖音登录已失效:去分发台点「登录」扫码一次",
                                   retryable=False, credential=True)
            for rid in remote_ids:
                try:
                    got = session.page.evaluate(_FETCH_JSON_JS, PROGRESS_API.format(id=rid))
                    body = json.loads(got.get("text") or "{}") if got.get("status") == 200 else {}
                except Exception as exc:  # noqa: BLE001 - 一条没拿到不连坐别的
                    log.info("进度条分析没拿到 %s:%s", rid, exc)
                    continue
                points = parse_progress(body) if isinstance(body, dict) else []
                if points:
                    out[rid] = points
    except browser.BrowserUnavailable as exc:
        raise PublishError(str(exc), retryable=False) from exc
    return out


def resolve_published_work(title: str, *, since: float | None = None,
                           rounds: int = WORK_RESOLVE_ROUNDS,
                           gap: float = WORK_RESOLVE_GAP) -> dict[str, Any]:
    """回查作品管理页,按标题找出**刚发出去**的那条作品,取它的 `aweme_id`。

    网页版发布这条路,抖音不把作品 id 给你 —— 只在作品管理页里显示。所以发完得
    自己回去查一遍。作品入库有延迟(刚发那会儿列表里还没有),所以查几轮。

    查不到就返回空 dict,**不报错** —— 作品可能还在审核/入库,发布本身是成功的,
    不该因为查不到 id 就把一次成功的发布判成失败。

    同标题可能有好几条(重发过的),所以取**最新**的那条,不是列表里第一条。
    """
    title = (title or "").strip()
    if not title:
        return {}
    key = title[:12]
    for attempt in range(max(1, rounds)):
        try:
            works = fetch_creator_works(since=since)
        except PublishError:
            return {}
        except Exception as exc:  # noqa: BLE001 - 回查是锦上添花,不该拖垮发布
            log.warning("回查作品 id 失败:%s: %s", type(exc).__name__, exc)
            return {}
        best: tuple[int, str, str] | None = None
        for w in works:
            aweme = w.get("aweme") or {}
            desc = str(aweme.get("desc") or w.get("desc") or "")
            if not desc or key not in desc:
                continue
            created = _int(w.get("create_time")) or _int(aweme.get("create_time")) or 0
            if since and created and created < since - 120:
                continue  # 比这次发布还早的,是上一条
            aid = str(aweme.get("aweme_id") or w.get("id") or "")
            if not aid:
                continue
            if best is None or created > best[0]:
                best = (created, aid, desc)
        if best is not None:
            created, aid, desc = best
            return {"aweme_id": aid,
                    "url": f"https://www.douyin.com/video/{aid}",
                    "title": desc, "create_time": created}
        if attempt < rounds - 1:
            time.sleep(gap)
    return {}
