"""网络探针:抖音「主动互动」的节点是怎么长的(手动跑)。

    python tests/probe_douyin_outreach.py              # 只读,把节点结构打出来
    python tests/probe_douyin_outreach.py --like <作品id>  # 点一次赞,对比前后

只读模式里不点赞、不评论、不关注。`--like` 模式**会真的点一次赞**(前后的
差异就是「点赞成功」的可观测信号)。

实测结论(2026-10-25):
- **作品详情页(`/video/{id}`)没有评论输入框**;能评论的是**推荐流弹层**
  `https://www.douyin.com/?modal_id={id}`(跳到 `/jingxuan?modal_id=`)。
- 弹层里:`data-e2e="video-player-digg"`(点赞,元素文字=点赞数)、
  `data-e2e="feed-comment-icon"`(评论)、`data-e2e="video-player-collect"`(收藏)、
  `data-e2e="video-player-share"`(分享)。弹层里第一个是目标作品。
- 弹层上有「我知道了」新手引导浮层,要先关掉。
- 评论输入区:`div.comment-input-container` → 点开是 DraftEditor
  (`.richtext-container` 里的 contenteditable),占位「留下你的精彩评论吧」。
- 分享面板:标题「分享给朋友」,里面 `button` 文字「复制链接」。
- 作者关注:作品页 `[data-e2e="user-info"]` 里最后一个 `button`,文字「关注」。
- 详情页的动作栏 = `[data-e2e="detail-video-info"]` 里四个兄弟节点,顺序
  点赞数 / 评论数 / 收藏 / 分享;类名是构建期哈希,靠**位置**定位。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.publish import browser  # noqa: E402

MODAL_URL = "https://www.douyin.com/?modal_id={aweme_id}"
VIDEO_URL = "https://www.douyin.com/video/{aweme_id}"
DEFAULT_VIDEOS = ["7676325036050877746"]

#: 弹层里目标作品的信息(标题 / 作者 / 作品 id)
_DUMP_MODAL_JS = """() => {
  const out = {title: '', author: '', sec_uid: '', aweme_id: '', tabs: [], digg: null};
  // 标题:弹层右栏的「详情」页签里有;也可能在标题栏
  const h1 = document.querySelector('h1');
  out.title = h1 ? (h1.innerText || '').trim() : '';
  const user = document.querySelector('[data-e2e="user-info"]');
  if (user) {
    const link = user.querySelector('a[href*="/user/"]');
    out.author = ((link || {}).innerText || '').trim();
    out.sec_uid = link ? ((link.getAttribute('href') || '').split('/user/')[1] || '') : '';
  }
  document.querySelectorAll('[role=tab]').forEach((t) => {
    if (out.tabs.length < 8) out.tabs.push({text: (t.innerText || '').trim(),
                                           sel: t.getAttribute('aria-selected')});
  });
  const digg = document.querySelector('[data-e2e="video-player-digg"]');
  if (digg) {
    const icon = digg.querySelector('span[role=img]');
    const svg = digg.querySelector('svg');
    const path = digg.querySelector('path');
    out.digg = {
      cls: String(digg.className || ''),
      state: digg.getAttribute('data-e2e-state') || '',
      text: (digg.innerText || '').trim(),
      iconCls: icon ? String(icon.className || '') : '',
      iconColor: icon ? getComputedStyle(icon).color : '',
      svgColor: svg ? getComputedStyle(svg).color : '',
      pathFill: path ? (path.getAttribute('fill') || '') : '',
      html: digg.outerHTML.slice(0, 900),
    };
  }
  return out;
}"""

#: 详情页的动作栏结构
_DUMP_ACTION_JS = """() => {
  const host = document.querySelector('[data-e2e="detail-video-info"]');
  if (!host) return {error: 'no detail-video-info'};
  const row = Array.from(host.querySelectorAll('div')).find(
    (d) => d.children.length === 4 && Array.from(d.children).some(
      (c) => (c.innerText || '').trim() === '分享'));
  if (!row) return {error: 'no action row'};
  return {
    items: Array.from(row.children).map((c) => ({
      text: (c.innerText || '').trim(),
      e2e: c.getAttribute('data-e2e') || '',
      cls: String(c.className || '').slice(0, 40),
    })),
  };
}"""


def _show(title: str, data: object) -> None:
    print(f"\n-- {title} --")
    if isinstance(data, (dict, list)):
        print(json.dumps(data, ensure_ascii=False, indent=1)[:3000])
    else:
        print(data)


def probe(aweme_id: str) -> int:
    print(f"\n===== 作品 {aweme_id}(推荐流弹层) =====")
    try:
        with browser.BrowserSession("douyin", headless=True, lock_timeout=90) as session:
            session.goto(MODAL_URL.format(aweme_id=aweme_id), wait_ms=10000)
            guide = session.page.get_by_text("我知道了", exact=True)
            if guide.count():
                guide.first.click()
                session.page.wait_for_timeout(1500)
                print("已关掉新手引导浮层")
            _show("弹层信息 + 点赞按钮", session.page.evaluate(_DUMP_MODAL_JS))

            print("\n===== 切到详情页看动作栏 =====")
            session.goto(VIDEO_URL.format(aweme_id=aweme_id), wait_ms=8000)
            _show("动作栏", session.page.evaluate(_DUMP_ACTION_JS))
    except browser.BrowserUnavailable as exc:
        print("浏览器起不来:", exc)
        return 1
    except Exception as exc:  # noqa: BLE001
        print(f"探针失败:{type(exc).__name__}: {exc}")
        return 1
    return 0


def compose_probe(aweme_id: str) -> int:
    """点开评论框、打几个字,把输入区整个扒出来 —— 找「发布」按钮和编辑器。

    **不会发送**:打完字就关掉页面,内容不会发出去。
    """
    print(f"\n===== 评论输入区 {aweme_id}(不会发送) =====")
    with browser.BrowserSession("douyin", headless=True, lock_timeout=90) as session:
        session.goto(MODAL_URL.format(aweme_id=aweme_id), wait_ms=10000)
        guide = session.page.get_by_text("我知道了", exact=True)
        if guide.count():
            guide.first.click()
            session.page.wait_for_timeout(1500)
        icon = session.page.locator('[data-e2e="feed-comment-icon"]').first
        icon.click()
        session.page.wait_for_timeout(3000)
        box = session.page.locator(".comment-input-container").first
        box.click()
        session.page.wait_for_timeout(1500)
        editor = session.page.locator(".comment-input-container div[contenteditable=true]").first
        editor.click()
        session.page.keyboard.type("续航这块讲得挺实在", delay=40)
        session.page.wait_for_timeout(1500)
        print(json.dumps(session.page.evaluate(
            """() => {
              const out = {right: [], publish: [], container: []};
              const right = document.querySelector('.commentInput-right-ct');
              if (right) {
                right.querySelectorAll('*').forEach((el) => {
                  const r = el.getBoundingClientRect();
                  if (el.tagName === 'SVG' || el.tagName === 'PATH') return;
                  out.right.push({tag: el.tagName.toLowerCase(),
                                  cls: String(el.className || '').slice(0, 55),
                                  text: (el.innerText || '').trim().slice(0, 16),
                                  e2e: el.getAttribute('data-e2e') || '',
                                  vis: r.width > 0 && r.height > 0,
                                  disabled: el.disabled === true,
                                  html: el.outerHTML.slice(0, 200)});
                });
              }
              document.querySelectorAll('button, div, span').forEach((el) => {
                const t = (el.innerText || '').trim();
                if (t !== '发布' && t !== '发送') return;
                const r = el.getBoundingClientRect();
                out.publish.push({tag: el.tagName.toLowerCase(),
                                  cls: String(el.className || '').slice(0, 55),
                                  text: t,
                                  e2e: el.getAttribute('data-e2e') || '',
                                  vis: r.width > 0 && r.height > 0,
                                  html: el.outerHTML.slice(0, 200)});
              });
              const scope = document.querySelector('.comment-input-container');
              if (scope) {
                out.container.push(scope.parentElement.outerHTML.slice(0, 2500));
              }
              return out;
            }"""), ensure_ascii=False, indent=1)[:7000])
        print("\n-- 全页面找「发布 / 发送」 --")
        print(json.dumps(session.page.evaluate(
            """() => {
              const out = [];
              document.querySelectorAll('button, div, span').forEach((el) => {
                const t = (el.innerText || '').trim();
                if (t !== '发布' && t !== '发送') return;
                const r = el.getBoundingClientRect();
                out.push({tag: el.tagName.toLowerCase(),
                          cls: String(el.className || '').slice(0, 60),
                          text: t, vis: r.width > 0 && r.height > 0,
                          html: el.outerHTML.slice(0, 220)});
              });
              return out;
            }"""), ensure_ascii=False, indent=1)[:4000])
    return 0


def like_diff(aweme_id: str) -> int:
    """点一次赞,对比前后的差异 —— 这就是「点赞成功」的可观测信号。"""
    print(f"\n===== 点赞对比 {aweme_id}(会真的点一下) =====")
    try:
        with browser.BrowserSession("douyin", headless=True, lock_timeout=90) as session:
            session.goto(MODAL_URL.format(aweme_id=aweme_id), wait_ms=10000)
            guide = session.page.get_by_text("我知道了", exact=True)
            if guide.count():
                guide.first.click()
                session.page.wait_for_timeout(1500)
            before = session.page.evaluate(_DUMP_MODAL_JS).get("digg")
            print("\n-- 点之前 --")
            print(json.dumps(before, ensure_ascii=False, indent=1))
            session.page.locator('[data-e2e="video-player-digg"]').first.click()
            session.page.wait_for_timeout(2500)
            after = session.page.evaluate(_DUMP_MODAL_JS).get("digg")
            print("\n-- 点之后 --")
            print(json.dumps(after, ensure_ascii=False, indent=1))
            print("\n-- 差异 --")
            for key in (before or {}):
                if (before or {}).get(key) != (after or {}).get(key):
                    print(f"  {key}: {json.dumps((before or {}).get(key), ensure_ascii=False)}"
                          f" → {json.dumps((after or {}).get(key), ensure_ascii=False)}")
    except browser.BrowserUnavailable as exc:
        print("浏览器起不来:", exc)
        return 1
    except Exception as exc:  # noqa: BLE001
        print(f"探针失败:{type(exc).__name__}: {exc}")
        return 1
    return 0


def comments_probe(aweme_id: str) -> int:
    """只读:看这条作品最新的几条评论,顺便看有没有二次验证浮层。"""
    print(f"\n===== 评论列表 {aweme_id} =====")
    with browser.BrowserSession("douyin", headless=True, lock_timeout=90) as session:
        session.goto(MODAL_URL.format(aweme_id=aweme_id), wait_ms=10000)
        guide = session.page.get_by_text("我知道了", exact=True)
        if guide.count():
            guide.first.click()
            session.page.wait_for_timeout(1200)
        print("二次验证浮层:", session.page.locator("#uc-second-verify").count() > 0
              or session.page.locator(".second-verify-mask").count() > 0)
        icon = session.page.locator('[data-e2e="feed-comment-icon"]').first
        icon.click()
        session.page.wait_for_timeout(3000)
        print(json.dumps(session.page.evaluate(
            """() => {
              const list = document.querySelector('[data-e2e="comment-list"]');
              if (!list) return {error: 'no comment list'};
              const out = [];
              list.querySelectorAll('[data-e2e="comment-item"]').forEach((el) => {
                out.push((el.innerText || '').replace(/\\n+/g, ' | ').slice(0, 80));
              });
              return out.slice(0, 12);
            }"""), ensure_ascii=False, indent=1))
    return 0


def main() -> int:
    args = sys.argv[1:]
    if args and args[0] == "--like":
        return like_diff(args[1] if len(args) > 1 else DEFAULT_VIDEOS[0])
    if args and args[0] == "--compose":
        return compose_probe(args[1] if len(args) > 1 else DEFAULT_VIDEOS[0])
    if args and args[0] == "--comments":
        return comments_probe(args[1] if len(args) > 1 else DEFAULT_VIDEOS[0])
    videos = args or DEFAULT_VIDEOS
    code = 0
    for vid in videos:
        code |= probe(vid)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
