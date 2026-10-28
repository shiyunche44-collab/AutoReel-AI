"""对标账号主页的作品列表。"""
from __future__ import annotations

import re
from typing import Any
from ...base import PublishError


# --------------------------------------------------------------------------
# 对标账号主页(公开页,只读)
# --------------------------------------------------------------------------
USER_PAGE = "https://www.douyin.com/user/"
PROFILE_POST_API = "/aweme/v1/web/aweme/post/"
_SEC_UID = re.compile(r"/user/(MS4wLjAB[\w-]+)")


def sec_uid_of(text: str) -> str:
    """从主页链接里取 sec_uid;没有(比如 v.douyin.com 短链)返回空串。"""
    m = _SEC_UID.search(text or "")
    if m:
        return m.group(1)
    text = (text or "").strip()
    return text if text.startswith("MS4wLjAB") and " " not in text else ""


def fetch_profile_posts(target: str, *, max_scrolls: int = 1) -> dict[str, Any]:
    """无头打开对标账号主页,截页面自己请求的作品列表接口。只读,不点任何东西。

    `target` 是 sec_uid、主页链接或分享短链(短链让浏览器跳一次,从落地页 URL 取 sec_uid)。
    返回 {"sec_uid", "nickname", "awemes": [作品接口原始条目]}。一个号只翻 `max_scrolls` 次
    (一页 18 条左右,算中位数够了)。登录失效抛 `PublishError(credential=True)`,
    拿不到数据抛普通 `PublishError` —— 都不重试,由调用方记下来等人看。"""
    from ... import browser

    sec = sec_uid_of(target)
    pages: list[dict[str, Any]] = []

    def on_response(resp: Any) -> None:
        if PROFILE_POST_API not in resp.url:
            return
        try:
            body = resp.json()
        except Exception:  # noqa: BLE001
            return
        if isinstance(body, dict) and body.get("aweme_list") is not None:
            pages.append(body)

    try:
        with browser.BrowserSession("douyin", headless=True, lock_timeout=60) as session:
            session.page.on("response", on_response)
            if sec:
                session.goto(USER_PAGE + sec, wait_ms=8000)
            else:
                url = target.strip()
                if not url.startswith("http"):
                    raise PublishError("看不懂这个账号:贴主页链接或分享链接", retryable=False)
                session.goto(url, wait_ms=8000)
                sec = sec_uid_of(session.page.url)
                if not sec:
                    raise PublishError("链接没跳到抖音用户主页", retryable=False)
            if not pages and "扫码登录" in session.page_text():
                raise PublishError("抖音登录已失效:去分发台点「登录」扫码一次",
                                   retryable=False, credential=True)
            for _ in range(max_scrolls):
                if not pages or not pages[-1].get("has_more"):
                    break
                got = len(pages)
                session.page.mouse.wheel(0, 5000)
                session.page.wait_for_timeout(3000)
                if len(pages) == got:
                    break
    except browser.BrowserUnavailable as exc:
        raise PublishError(str(exc), retryable=False) from exc
    if not pages:
        raise PublishError("主页没请求作品列表接口(被风控或页面改版了?)", retryable=False)
    awemes: dict[str, dict[str, Any]] = {}
    for body in pages:
        for a in body.get("aweme_list") or []:
            if a.get("aweme_id"):
                awemes[str(a["aweme_id"])] = a
    first = next(iter(awemes.values()), {})
    return {"sec_uid": sec, "nickname": str((first.get("author") or {}).get("nickname") or ""),
            "awemes": list(awemes.values())}
