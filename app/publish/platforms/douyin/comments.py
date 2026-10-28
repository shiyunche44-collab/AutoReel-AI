"""评论管理页:读评论、按批过的回复逐条发。"""
from __future__ import annotations

import re
from typing import Any
from ...base import PublishError


# --------------------------------------------------------------------------
# 评论(运营官):创作者中心「评论管理」页
# --------------------------------------------------------------------------
#: 评论管理页:默认打开最新一条作品;「选择作品」弹窗里点哪条才会请求哪条的评论
CREATOR_COMMENTS = "https://creator.douyin.com/creator-micro/interactive/comment"
#: 页面自己请求的作品列表(带每条作品的评论数)和单条作品的评论列表
COMMENT_ITEMS_API = "/aweme/v1/creator/item/list/"
COMMENT_LIST_API = "/aweme/v1/creator/comment/list/"
#: 每条作品最多往下翻这么多页评论(一页 10 条)
COMMENT_PAGES = 3
#: 评论里的表情在接口里是「[赞]」,页面上是图片
_EMOJI_RE = re.compile(r"\[[^\[\]]{1,6}\]")

#: 评论行:同时含作者名、评论文字和「回复」的最小那个块,把它里面的「回复」标出来
_MARK_REPLY_JS = """([author, text]) => {
  document.querySelectorAll('[data-aim-reply]').forEach((e) => e.removeAttribute('data-aim-reply'));
  const hits = [];
  for (const el of document.querySelectorAll('div')) {
    const t = el.innerText || '';
    if (t.includes(author) && (!text || t.includes(text)) && t.includes('回复')) hits.push(el);
  }
  const rows = hits.filter((el) => !hits.some((o) => o !== el && el.contains(o)));
  if (!rows.length) return 0;
  const btn = [...rows[0].querySelectorAll('*')].reverse()
    .find((e) => (e.innerText || '').trim() === '回复');
  if (!btn) return 0;
  btn.setAttribute('data-aim-reply', '1');
  return rows.length;
}"""
#: 回复框旁边那个「发送」(框里有字才可点)
_MARK_SEND_JS = """() => {
  document.querySelectorAll('[data-aim-send]').forEach((e) => e.removeAttribute('data-aim-send'));
  const box = document.querySelector('[data-aim-box]');
  for (let el = box; el; el = el.parentElement) {
    const btn = [...el.querySelectorAll('button')].find((b) => (b.innerText || '').trim() === '发送');
    if (btn) { btn.setAttribute('data-aim-send', '1'); return !btn.disabled; }
  }
  return false;
}"""


def comment_text_key(text: str) -> str:
    """页面上能看到的评论文字(去掉表情,取前 20 字,够认出是哪条)。"""
    return _EMOJI_RE.sub("", str(text or "")).strip()[:20]


class _CommentPage:
    """开着的评论管理页:截作品列表和评论列表接口,按标题切作品。只读,除非调 `reply`。"""

    def __init__(self, session: Any) -> None:
        self.s = session
        self.items: dict[str, dict[str, Any]] = {}
        self.lists: list[dict[str, Any]] = []
        self.posts: list[dict[str, Any]] = []
        session.page.on("response", self._on_response)

    def _on_response(self, resp: Any) -> None:
        url = resp.url
        if COMMENT_ITEMS_API not in url and COMMENT_LIST_API not in url \
                and not (resp.request.method == "POST" and "comment" in url):
            return
        try:
            body = resp.json()
        except Exception:  # noqa: BLE001
            return
        if not isinstance(body, dict):
            return
        if COMMENT_ITEMS_API in url:
            for it in body.get("item_info_list") or []:
                wid = str(it.get("item_id_plain") or "")
                if wid:
                    self.items[wid] = it
        elif COMMENT_LIST_API in url:
            self.lists.append(body)
        else:
            self.posts.append(body)

    def open(self) -> None:
        self.s.goto(CREATOR_COMMENTS, wait_ms=6000)
        if "扫码登录" in self.s.page_text():
            raise PublishError("抖音登录已失效:去分发台点「登录」扫码一次",
                               retryable=False, credential=True)
        if not self.items:
            raise PublishError("评论管理页没请求作品列表接口(页面改版了?)", retryable=False)

    def select(self, work_id: str) -> list[dict[str, Any]]:
        """切到这条作品,返回它评论列表接口的第一页(没请求到返回空)。"""
        page = self.s.page
        title = str((self.items.get(work_id) or {}).get("title") or "").strip()
        if not title:
            return []
        got = len(self.lists)
        page.get_by_text("选择作品", exact=True).first.click()
        page.wait_for_timeout(2500)
        target = page.get_by_text(title[:12], exact=False)
        if not target.count():
            page.keyboard.press("Escape")
            return []
        target.last.click()
        for _ in range(16):
            if len(self.lists) > got:
                break
            page.wait_for_timeout(500)
        return self.lists[got:]

    def comments(self, work_id: str) -> list[dict[str, Any]]:
        bodies = self.select(work_id)
        for _ in range(COMMENT_PAGES - 1):
            if not bodies or not bodies[-1].get("has_more"):
                break
            got = len(self.lists)
            self.s.page.mouse.wheel(0, 4000)
            self.s.page.wait_for_timeout(2500)
            if len(self.lists) == got:
                break
            bodies += self.lists[got:]
        out: dict[str, dict[str, Any]] = {}
        for body in bodies:
            for c in body.get("comment_info_list") or []:
                if c.get("comment_id"):
                    out[str(c["comment_id"])] = c
        return list(out.values())

    def reply(self, author: str, text: str, reply: str) -> None:
        """在当前作品下找到这条评论,点「回复」、填字、点「发送」,等平台回执。"""
        page = self.s.page
        key = comment_text_key(text)
        found = 0
        for _ in range(COMMENT_PAGES):
            found = page.evaluate(_MARK_REPLY_JS, [author, key])
            if found:
                break
            page.mouse.wheel(0, 4000)
            page.wait_for_timeout(2000)
        if not found:
            raise PublishError("评论列表里没找到这条评论(可能被删了)", retryable=False)
        if found > 1 and not key:
            raise PublishError("同一个人有好几条只有表情的评论,分不清回哪条,去抖音手动回",
                               retryable=False)
        page.locator("[data-aim-reply]").first.click()
        page.wait_for_timeout(1200)
        box = page.locator('[contenteditable="true"][placeholder^="回复"]')
        if not box.count():
            raise PublishError("点了「回复」没出现回复框(页面改版了?)", retryable=False)
        box = box.last
        box.evaluate("(e) => e.setAttribute('data-aim-box', '1')")
        box.click()
        page.keyboard.type(reply, delay=40)
        ready = False
        for _ in range(10):
            ready = page.evaluate(_MARK_SEND_JS)
            if ready:
                break
            page.wait_for_timeout(300)
        if not ready:
            raise PublishError("回复框填了字,「发送」还是灰的", retryable=False)
        got = len(self.posts)
        page.locator("[data-aim-send]").first.click()
        for _ in range(16):
            if len(self.posts) > got:
                break
            page.wait_for_timeout(500)
        answers = [b for b in self.posts[got:] if "status_code" in b]
        if answers:
            code = answers[-1].get("status_code")
            if code not in (0, "0"):
                raise PublishError(f"抖音没收这条回复:{answers[-1].get('status_msg') or code}",
                                   retryable=False)
            return
        if reply[:10] not in self.s.page_text():
            raise PublishError("点了发送,没等到平台回执,页面上也没看到这条回复;去抖音核对一下"
                               "再决定要不要重发", retryable=False)


def fetch_creator_comments(known: dict[str, int]) -> dict[str, Any]:
    """无头打开评论管理页,只读。`known` 是 {作品 id: 上次看到的评论数}。

    作品列表接口带每条作品的评论数,只切到评论数比上次多的那几条去拉评论(作品不在
    `known` 里的不看 —— 只管团队发的)。返回 {"counts": {作品 id: 评论数},
    "comments": {作品 id: [评论接口原始条目]}}。登录失效抛 `PublishError(credential=True)`。"""
    from ... import browser

    try:
        with browser.BrowserSession("douyin", headless=True, lock_timeout=60) as session:
            cp = _CommentPage(session)
            cp.open()
            counts = {wid: int(it.get("comment_count") or 0) for wid, it in cp.items.items()}
            got: dict[str, list[dict[str, Any]]] = {}
            for wid, seen in known.items():
                if counts.get(wid, 0) > seen:
                    got[wid] = cp.comments(wid)
    except browser.BrowserUnavailable as exc:
        raise PublishError(str(exc), retryable=False) from exc
    return {"counts": {w: counts[w] for w in known if w in counts}, "comments": got}


def send_comment_replies(jobs: list[dict[str, Any]], on_done: Any) -> None:
    """按你批过的回复逐条发。`jobs` 每条:{id, work_id, author, text, reply}。

    每发完一条调 `on_done(job, error_or_None)`;遇到第一条失败就停(不连着试,免得触发风控)。
    条与条之间停几秒,像人在回。"""
    from ... import browser

    try:
        with browser.BrowserSession("douyin", headless=True, lock_timeout=60) as session:
            cp = _CommentPage(session)
            cp.open()
            current = None
            for i, job in enumerate(jobs):
                try:
                    if job["work_id"] != current:
                        if job["work_id"] not in cp.items or not cp.select(job["work_id"]):
                            raise PublishError("评论管理页里切不到这条作品", retryable=False)
                        current = job["work_id"]
                    cp.reply(job["author"], job["text"], job["reply"])
                except PublishError as exc:
                    on_done(job, exc.message)
                    return
                on_done(job, None)
                if i + 1 < len(jobs):
                    session.page.wait_for_timeout(4000 + 1000 * (i % 3))
    except browser.BrowserUnavailable as exc:
        raise PublishError(str(exc), retryable=False) from exc
