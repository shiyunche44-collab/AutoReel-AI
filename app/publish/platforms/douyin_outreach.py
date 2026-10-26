"""抖音「主动互动」动作层:去**别人**的作品下评论 / 点赞 / 分享 / 关注。

**为什么走推荐流弹层而不是作品详情页**(2026-10-25 实测):
`https://www.douyin.com/video/{id}` 的详情页**没有评论输入框** —— 评论面板
(`[data-e2e="comment-list"]`)里只有评论条目和「加载中」,整页找不到任何
`contenteditable` / 评论占位框。能评论的那套布局是**推荐流弹层**:
`https://www.douyin.com/?modal_id={id}`(会跳到 `/jingxuan?modal_id={id}`),
点赞 / 评论 / 收藏 / 分享四个按钮都在那儿,而且弹层里第一个就是目标作品。

**节点**(实测;类名是构建期哈希,**别靠类名定位**):

| 动作 | 节点 |
|---|---|
| 点赞 | `div[data-e2e="video-player-digg"]`,元素文字是点赞数;**`data-e2e-state` 跟着点赞态变**(实测:没点赞是 `video-player-no-digged`,点了是 `video-player-is-digged`) |
| 评论入口 | `div[data-e2e="feed-comment-icon"]`,文字是评论数 |
| 收藏 | `div[data-e2e="video-player-collect"]` |
| 分享 | `div[data-e2e="video-player-share"]` → 面板里 `button` 文字「复制链接」 |
| 评论输入 | `div.comment-input-container` → 点开是 DraftEditor(`.richtext-container` 里的 `contenteditable`),占位「留下你的精彩评论吧」 |
| 作者关注 | `[data-e2e="user-info"]` 里的 `button` 文字「关注」,点了变「已关注」 |

**点了不算成功,状态变了才算** —— 照 `douyin.set_covers` 看预览地址有没有变的思路:
点赞看图标颜色,评论看评论列表里有没有我们那句,关注看按钮字。

**分享只是复制链接**:抖音网页版没有「转发到动态」,分享面板里能做的只有
「复制链接」。所以这一步**不会**给对方作品增加分享计数 —— 它只是把链接拿到手,
留给人去 App 里真分享。代码里如实记着,不当成"分享成功"自欺。
"""
from __future__ import annotations

import logging
import time
from typing import Any, Callable

from ..base import PublishError

log = logging.getLogger(__name__)

#: 推荐流弹层:能评论的那套布局
MODAL_URL = "https://www.douyin.com/?modal_id={aweme_id}"
#: 作者主页(关注动作的备用入口)
USER_URL = "https://www.douyin.com/user/{sec_uid}"
#: 页面加载后等多久
WAIT_MS = 9000
#: 评论面板 / 分享面板打开后等多久
PANEL_WAIT_MS = 2500
#: 点完动作等平台回执
ACK_WAIT_MS = 6000
#: 打字间隔(毫秒)—— 像人打字,别一口气灌进去
TYPE_DELAY_MS = 55
#: 两条动作之间的间隔(毫秒)
ACTION_GAP_MS = 4000

#: 新手引导浮层:挡着点击,先关掉
_GUIDE_TEXTS = ("我知道了", "知道了")
#: 撞上验证码 / 风控的标志
_VERIFY_TEXTS = ("验证码", "安全验证", "拖动滑块", "点击验证", "访问频繁", "操作过于频繁")
#: 登录失效的标志
_LOGIN_TEXTS = ("扫码登录", "登录后查看", "请登录")
#: 二次验证浮层(实测):`#uc-second-verify` 里盖一层 `second-verify-mask`,
#: **它会拦掉所有点击**,页面上却没有「验证码」那几个字 —— 得单独认。
_VERIFY_SELECTORS = ("#uc-second-verify", ".second-verify-mask")

#: 动作名
ACTIONS = ("like", "comment", "share", "follow")
#: 同一批里的先后顺序:先点赞再评论,最后分享
_ACTION_ORDER = {"like": 0, "comment": 1, "share": 2, "follow": 3}


class VerificationNeeded(PublishError):
    """撞上验证码 / 风控了。人工过一下才能继续,机器不许重试。"""

    def __init__(self, message: str = "抖音要验证码,需要人工过一下") -> None:
        super().__init__(message, retryable=False)


# --------------------------------------------------------------------------
# 页面小工具
# --------------------------------------------------------------------------
def _page_text(session: Any) -> str:
    try:
        return session.page.inner_text("body") or ""
    except Exception:  # noqa: BLE001
        return ""


def _guard(session: Any) -> None:
    """登录失效 / 验证码,直接抛 —— 别在出问题的页面上继续点。

    二次验证浮层(`#uc-second-verify`)页面上没有「验证码」三个字,但它盖着一层
    遮罩,**所有点击都会被它吃掉** —— 实测就栽在这儿:点赞看着点上了,其实
    点在了遮罩上。所以每次动作前后都要查一遍。
    """
    for selector in _VERIFY_SELECTORS:
        try:
            if session.page.locator(selector).count():
                raise VerificationNeeded("抖音弹了二次验证,需要人工过一下")
        except VerificationNeeded:
            raise
        except Exception:  # noqa: BLE001 - 查不到就当没有
            continue
    body = _page_text(session)
    if any(t in body for t in _VERIFY_TEXTS):
        raise VerificationNeeded()
    if any(t in body for t in _LOGIN_TEXTS):
        raise PublishError("抖音登录已失效:去分发台点「登录」扫码一次",
                           retryable=False, credential=True)


def _dismiss_guide(session: Any) -> None:
    """关掉「我知道了」新手引导浮层(它会盖住按钮)。"""
    for text in _GUIDE_TEXTS:
        try:
            btn = session.page.get_by_text(text, exact=True)
            if btn.count() and btn.first.is_visible():
                btn.first.click()
                session.page.wait_for_timeout(1200)
                return
        except Exception:  # noqa: BLE001 - 关不掉就算了,靠 force 点击
            continue


def _first_visible(session: Any, selector: str) -> Any | None:
    """页面上第一个可见的匹配元素(没有就 None)。"""
    try:
        loc = session.page.locator(selector)
        for i in range(min(loc.count(), 8)):
            item = loc.nth(i)
            if item.is_visible():
                return item
    except Exception:  # noqa: BLE001
        return None
    return None


# --------------------------------------------------------------------------
# 打开作品
# --------------------------------------------------------------------------
def open_video(session: Any, aweme_id: str) -> dict[str, Any]:
    """打开作品(推荐流弹层),返回能抓到的 {"title", "author", "sec_uid"}。

    **弹层布局里这些多半是空的**(标题在「详情」页签里,作者区默认不展开)——
    生产链路的标题来自对标分析员抓到的 `benchmark_posts`,不靠这里抓。
    这段只是尽力而为,抓到了算加分。
    """
    session.goto(MODAL_URL.format(aweme_id=aweme_id), wait_ms=WAIT_MS)
    _guard(session)
    _dismiss_guide(session)
    info = session.page.evaluate(
        """() => {
          const info = document.querySelector('[data-e2e="detail-video-info"]');
          const user = document.querySelector('[data-e2e="user-info"]');
          const link = user && user.querySelector('a[href*="/user/"]');
          const title = info ? (info.querySelector('h1') || {}).innerText || '' : '';
          const author = user ? ((user.querySelector('a[href*="/user/"] span') || {}).innerText
                               || '').trim() : '';
          return {
            title: (title || '').trim(),
            author: author,
            sec_uid: link ? ((link.getAttribute('href') || '').split('/user/')[1] || '') : '',
          };
        }""")
    return {k: str(v or "") for k, v in (info or {}).items()}


# --------------------------------------------------------------------------
# 点赞
# --------------------------------------------------------------------------
#: 点赞按钮的当前状态。**`data-e2e-state` 才是判据**(实测):
#: 没点赞是 "video-player-no-digged",点了是 "video-player-is-digged";
#: 图标类名和颜色也会跟着变,但那是构建期哈希 + 主题色,别拿来当判据。
_DIGG_STATE_JS = """() => {
  const el = document.querySelector('[data-e2e="video-player-digg"]');
  if (!el) return null;
  const icon = el.querySelector('span[role=img]');
  return {
    state: el.getAttribute('data-e2e-state') || '',
    count: (el.innerText || '').trim(),
    color: icon ? getComputedStyle(icon).color : '',
  };
}"""


def _is_liked(state: str) -> bool:
    """是不是已经点过赞了。"""
    state = str(state or "")
    return "is-digged" in state and "no-digged" not in state


def _digg_state(session: Any) -> dict[str, Any] | None:
    try:
        return session.page.evaluate(_DIGG_STATE_JS)
    except Exception:  # noqa: BLE001
        return None


def like_open(session: Any) -> str:
    """给**当前页面**的作品点赞。返回 "liked"(这次点的) / "already"(本来就有赞)。

    **已经点过赞的不再点** —— 抖音的赞是开关,再点一下就取消了。
    """
    _guard(session)
    before = _digg_state(session)
    if before is None:
        raise PublishError("页面上找不到点赞按钮(页面改版了?)", retryable=False)
    if _is_liked(before.get("state", "")):
        return "already"
    btn = _first_visible(session, '[data-e2e="video-player-digg"]')
    if btn is None:
        raise PublishError("点赞按钮不可见(页面改版了?)", retryable=False)
    btn.click()
    session.page.wait_for_timeout(1800)
    _guard(session)  # 点了以后才发现弹了二次验证 → 这次不算成功
    after = _digg_state(session) or {}
    if _is_liked(after.get("state", "")):
        return "liked"
    # 点赞数变了也算数(小号上是「3」→「4」这种)
    if after.get("count") and after.get("count") != before.get("count"):
        return "liked"
    raise PublishError("点了赞但状态没变(可能是页面改版了)", retryable=False)


def like_video(session: Any, aweme_id: str) -> str:
    """打开作品并点赞。"""
    open_video(session, aweme_id)
    return like_open(session)


# --------------------------------------------------------------------------
# 评论
# --------------------------------------------------------------------------
#: 标出「发布」按钮(只留最内层那个,免得点到外层)
_MARK_PUBLISH_JS = """() => {
  document.querySelectorAll('[data-aim-publish]').forEach((e) => e.removeAttribute('data-aim-publish'));
  const scope = document.querySelector('.comment-input-container');
  const root = scope ? scope.parentElement : document;
  let found = 0;
  root.querySelectorAll('button, div, span').forEach((el) => {
    if ((el.innerText || '').trim() !== '发布') return;
    if (el.querySelector('div, span, button')) return;   // 只留最内层那个
    const r = el.getBoundingClientRect();
    if (r.width < 2 || r.height < 2) return;
    el.setAttribute('data-aim-publish', '1');
    found++;
  });
  return found;
}"""

#: 输入区右侧的图标(html 片段)—— 打字前后对比,多出来的那个才是发送
_RIGHT_ICONS_JS = """() => {
  const host = document.querySelector('.commentInput-right-ct');
  if (!host) return [];
  const out = [];
  host.querySelectorAll('span, button').forEach((el) => {
    if (!el.querySelector('svg')) return;
    const r = el.getBoundingClientRect();
    if (r.width < 8 || r.height < 8) return;
    out.push(el.innerHTML.slice(0, 120));
  });
  return out;
}"""

#: 标出「打字之后才出现」的图标 —— 发送按钮就是它
_MARK_NEW_ICON_JS = """(before) => {
  document.querySelectorAll('[data-aim-send]').forEach((e) => e.removeAttribute('data-aim-send'));
  const host = document.querySelector('.commentInput-right-ct');
  if (!host) return 0;
  let found = 0;
  host.querySelectorAll('span, button').forEach((el) => {
    if (!el.querySelector('svg')) return;
    const r = el.getBoundingClientRect();
    if (r.width < 8 || r.height < 8) return;
    const html = el.innerHTML.slice(0, 120);
    if ((before || []).includes(html)) return;
    el.setAttribute('data-aim-send', '1');
    found++;
  });
  return found;
}"""

#: 评论列表的文字(用来确认我们那句发出去了)
_COMMENT_LIST_JS = """() => {
  const list = document.querySelector('[data-e2e="comment-list"]');
  return list ? (list.innerText || '') : '';
}"""


def _open_comment_panel(session: Any) -> None:
    """点开评论面板(点评论图标会切到「评论」页签,输入框才会渲染)。"""
    icon = _first_visible(session, '[data-e2e="feed-comment-icon"]')
    if icon is None:
        raise PublishError("页面上找不到评论入口(页面改版了?)", retryable=False)
    icon.click()
    session.page.wait_for_timeout(PANEL_WAIT_MS)


def _wait_for_comment(session: Any, text: str, timeout_ms: int = 4000) -> bool:
    """评论列表里出现我们那句没有。"""
    deadline = time.time() + timeout_ms / 1000
    while time.time() < deadline:
        body = session.page.evaluate(_COMMENT_LIST_JS) or ""
        if text[:8] in body:
            return True
        session.page.wait_for_timeout(600)
    return False


def comment_open(session: Any, text: str) -> None:
    """在**当前页面**的作品下评论一句。发出去才算成功(评论列表里得有我们那句)。

    发送按钮的样子(实测):抖音网页版**没有「发布」文字按钮**,输入区右侧只有图标,
    而且**打字之后才多出**发送那个。所以这里按三种方式依次尝试:
    有「发布」文字按钮就点它 → 没有就回车(DraftEditor 里回车就是发送) →
    还没有就点「打字后才出现」的那个图标。
    """
    text = str(text or "").strip()
    if not text:
        raise PublishError("评论文案是空的", retryable=False)
    _open_comment_panel(session)
    _guard(session)
    box = _first_visible(session, ".comment-input-container")
    if box is None:
        raise PublishError("评论区没有输入框(页面改版了?)", retryable=False)
    box.click()
    session.page.wait_for_timeout(1200)
    _guard(session)
    before_icons = session.page.evaluate(_RIGHT_ICONS_JS) or []
    editor = _first_visible(session, ".comment-input-container div[contenteditable=true]")
    if editor is None:
        editor = _first_visible(session, "div[contenteditable=true]")
    if editor is None:
        raise PublishError("点开输入框没出现编辑器(页面改版了?)", retryable=False)
    editor.click()
    session.page.keyboard.type(text, delay=TYPE_DELAY_MS)
    session.page.wait_for_timeout(800)
    _guard(session)  # 打字这几十秒里也可能弹出二次验证

    if session.page.evaluate(_MARK_PUBLISH_JS):
        session.page.locator("[data-aim-publish]").first.click()
        if _wait_for_comment(session, text, ACK_WAIT_MS):
            return
    # 回车发送
    session.page.keyboard.press("Enter")
    if _wait_for_comment(session, text, ACK_WAIT_MS):
        return
    # 打字后才出现的那个图标 = 发送
    if session.page.evaluate(_MARK_NEW_ICON_JS, before_icons):
        session.page.locator("[data-aim-send]").first.click()
        if _wait_for_comment(session, text, ACK_WAIT_MS):
            return
    _guard(session)
    raise PublishError("点了发送,但评论列表里没看到我们那句(可能没发出去,去抖音核对)",
                       retryable=False)


def comment_video(session: Any, aweme_id: str, text: str) -> None:
    """打开作品并评论。"""
    open_video(session, aweme_id)
    comment_open(session, text)


# --------------------------------------------------------------------------
# 分享(复制链接)
# --------------------------------------------------------------------------
def share_open(session: Any) -> str:
    """在**当前页面**打开分享面板复制链接。返回一句说明。

    **这不会给对方作品增加分享计数** —— 抖音网页版没有「转发到动态」,
    分享面板里能做的只有复制链接。链接拿到手,方便人再去 App 里真分享。
    """
    btn = _first_visible(session, '[data-e2e="video-player-share"]')
    if btn is None:
        raise PublishError("页面上找不到分享按钮(页面改版了?)", retryable=False)
    _guard(session)
    btn.click()
    session.page.wait_for_timeout(PANEL_WAIT_MS)
    copy_btn = session.page.get_by_text("复制链接", exact=True)
    if not copy_btn.count():
        session.page.keyboard.press("Escape")
        raise PublishError("分享面板里没有「复制链接」(页面改版了?)", retryable=False)
    copy_btn.first.click()
    session.page.wait_for_timeout(1200)
    body = _page_text(session)
    session.page.keyboard.press("Escape")
    return "复制了链接(网页版不会给对方加分享数)" if "复制成功" in body \
        else "点了复制链接(页面没给回执)"


def share_video(session: Any, aweme_id: str) -> str:
    """打开作品并复制链接。"""
    open_video(session, aweme_id)
    return share_open(session)


# --------------------------------------------------------------------------
# 关注
# --------------------------------------------------------------------------
#: 作者区里「关注」按钮的状态:文字
_FOLLOW_STATE_JS = """() => {
  const host = document.querySelector('[data-e2e="user-info"]');
  if (!host) return null;
  const btns = Array.from(host.querySelectorAll('button'));
  const btn = btns.find((b) => /关注|已关注/.test((b.innerText || '').trim()));
  return btn ? {text: (btn.innerText || '').trim()} : null;
}"""

#: 页面里任意位置的「关注」按钮(作者主页上用)
_MARK_FOLLOW_JS = """() => {
  document.querySelectorAll('[data-aim-follow]').forEach((e) => e.removeAttribute('data-aim-follow'));
  let found = 0;
  document.querySelectorAll('button, [role=button]').forEach((el) => {
    const t = (el.innerText || '').trim();
    if (t !== '关注') return;
    const r = el.getBoundingClientRect();
    if (r.width < 2 || r.height < 2) return;
    el.setAttribute('data-aim-follow', '1');
    found++;
  });
  return found;
}"""

#: 页面上是不是已经显示「已关注」
_ALREADY_FOLLOWED_JS = """() => {
  const hit = Array.from(document.querySelectorAll('button, [role=button], span, div'))
    .some((el) => (el.innerText || '').trim() === '已关注');
  return hit;
}"""


def follow_open(session: Any) -> str:
    """关注**当前页面**的作者。返回 "followed" / "already"。"""
    _guard(session)
    state = session.page.evaluate(_FOLLOW_STATE_JS)
    if state and state.get("text") == "已关注":
        return "already"
    if state:
        btn = session.page.get_by_text("关注", exact=True)
        if btn.count() and btn.first.is_visible():
            btn.first.click()
            session.page.wait_for_timeout(1800)
            after = session.page.evaluate(_FOLLOW_STATE_JS) or {}
            if after.get("text") == "已关注":
                return "followed"
            raise PublishError("点了关注,按钮没变成「已关注」(页面改版了?)", retryable=False)
    # 作者区没有「关注」按钮:退到页面上任意可见的「关注」按钮
    if not session.page.evaluate(_MARK_FOLLOW_JS):
        # 已经关注过(按钮写着「已关注」)也算到了
        if session.page.evaluate(_ALREADY_FOLLOWED_JS):
            return "already"
        raise PublishError("页面上找不到「关注」按钮(页面改版了?)", retryable=False)
    session.page.locator("[data-aim-follow]").first.click()
    session.page.wait_for_timeout(2000)
    if session.page.evaluate(_ALREADY_FOLLOWED_JS) or "已关注" in _page_text(session):
        return "followed"
    raise PublishError("点了关注,页面没显示「已关注」", retryable=False)


def follow_author(session: Any, sec_uid: str, *, aweme_id: str = "") -> str:
    """关注一个作者。先在作品页的作者区找,找不到再去作者主页找。"""
    if aweme_id:
        open_video(session, aweme_id)
        try:
            return follow_open(session)
        except PublishError:
            log.info("作品页里没关注成功,去作者主页再试一次")
    if not sec_uid:
        raise PublishError("不知道要关注谁(没有 sec_uid)", retryable=False)
    session.goto(USER_URL.format(sec_uid=sec_uid), wait_ms=WAIT_MS)
    _guard(session)
    _dismiss_guide(session)
    return follow_open(session)


# --------------------------------------------------------------------------
# 批量执行
# --------------------------------------------------------------------------
def run_actions(jobs: list[dict[str, Any]],
                on_done: Callable[[dict[str, Any], str | None], None]) -> None:
    """按计划执行一批动作。每条 job:{id, action, aweme_id, sec_uid, text}。

    同一个作品的动作挨着做(只开一次页面)。每做完一条调 `on_done(job, error_or_None)`;
    **遇到第一条失败就停** —— 不重试、不换作品接着刷(免得触发风控)。
    """
    from .. import browser

    queue = sorted(jobs, key=lambda j: (_ACTION_ORDER.get(str(j.get("action") or ""), 9),
                                       str(j.get("id") or "")))
    try:
        with browser.BrowserSession("douyin", headless=True, lock_timeout=90) as session:
            current = ""
            for i, job in enumerate(queue):
                action = str(job.get("action") or "")
                try:
                    if action == "follow":
                        follow_author(session, str(job.get("sec_uid") or ""),
                                      aweme_id=str(job.get("aweme_id") or ""))
                    else:
                        aweme = str(job.get("aweme_id") or "")
                        if not aweme:
                            raise PublishError("这条动作没有目标作品", retryable=False)
                        if aweme != current:
                            open_video(session, aweme)
                            current = aweme
                        if action == "like":
                            like_open(session)
                        elif action == "comment":
                            comment_open(session, str(job.get("text") or ""))
                        elif action == "share":
                            share_open(session)
                        else:
                            raise PublishError(f"不认识的动作:{action}", retryable=False)
                except VerificationNeeded:
                    raise  # 验证码要往上抛,让活跃官停当天
                except PublishError as exc:
                    if exc.credential:
                        raise  # 登录失效也要往上抛
                    on_done(job, exc.message)
                    return
                on_done(job, None)
                if i + 1 < len(queue):
                    session.page.wait_for_timeout(ACTION_GAP_MS)
    except browser.BrowserUnavailable as exc:
        raise PublishError(str(exc), retryable=False) from exc
