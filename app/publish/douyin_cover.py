"""抖音:给**已发布**的作品换封面。

**为什么单独一个模块**:发布页(`platforms/douyin.py`)的 `set_covers` 是"发布前设置",
走的是发布页那个上传弹窗;已发布作品的封面要在**作品管理 → 编辑作品**里改 —— 页面结构
不一样,编辑页的封面位是平铺在页面上的,点开才弹同一个编辑器。

**平台限制**:作品管理页写着「每个作品最多修改5次」—— 每提交一次修改就消耗一次机会,
用完不能再改。所以这个操作**不自动重试**,失败了也不重复提交。

流程:
    作品管理页 → 找到目标作品那条 → 「编辑作品」→ 「设置封面」两个位分别上传 → 「提交修改」

**只换封面**:标题、简介、话题、标签一律不动 —— 改这些是另一件事,而且会连带消耗修改次数。

命令行:
    python -m app.publish.douyin_cover --project p_bea59bb72c3e --dry-run   # 只传不提交
    python -m app.publish.douyin_cover --project p_bea59bb72c3e             # 真替换并提交
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from pathlib import Path
from typing import Any

from ..publish.base import PublishError
from .platforms.douyin import CREATOR_MANAGE, clear_topic_popup, cover_files

log = logging.getLogger(__name__)

#: 编辑作品页(带上作品 id);点「编辑作品」也是跳到这里
EDIT_URL = ("https://creator.douyin.com/creator-micro/content/post/video"
            "?mid={mid}&enter_from=edit_item")
#: 一条作品在管理页里的卡片
CARD = "div[class*=video-card-v2]"
#: 页面提示「最多修改5次」的那句话 —— 出现它说明这次提交要花掉一次机会
LIMIT_HINT = "修改机会"
#: 「提交修改」按钮
SUBMIT_BUTTON = "button:has-text('提交修改')"
#: 弹窗里「上传封面」那块的隐藏 file input(按钮被上传层盖住,点不到,只能直接喂文件)
COVER_INPUT = ("div:has(> .semi-upload):has-text('上传封面') > .semi-upload "
               "> input.semi-upload-hidden-input")
#: 弹窗里上传完要点的确认键(按顺序找)
CONFIRM_KEYS = ("完成", "确定", "确认")
#: 弹窗标题(跟 `platforms.douyin.COVER_SLOTS` 里的第二步同名)
DIALOG_TITLE = {"横封面4:3": "设置横封面", "竖封面3:4": "设置竖封面"}


# --------------------------------------------------------------------------
# 页面定位
# --------------------------------------------------------------------------
def _card_of(page: Any, title: str) -> Any:
    """按标题找到那条作品卡片。找不到抛错。"""
    cards = page.locator(CARD)
    total = cards.count()
    for i in range(total):
        card = cards.nth(i)
        try:
            text = card.inner_text() or ""
        except Exception:  # noqa: BLE001
            continue
        if title and title[:12] in text:
            return card
    raise PublishError(f"作品管理页里找不到标题含「{title[:12]}」的作品(共 {total} 条)",
                       retryable=False)


def _cover_slot(page: Any, tip: str) -> Any:
    """按位上的提示字(「横封面4:3」/「竖封面3:4」)找到那个封面位的容器。

    先按提示字**精确**匹配 —— `has-text` 是子串匹配,外层容器同时含两个字时会认错位。
    """
    slots = page.locator("div[class*=coverControl]")
    total = slots.count()
    for i in range(total):
        slot = slots.nth(i)
        try:
            text = (slot.inner_text() or "").strip()
        except Exception:  # noqa: BLE001
            continue
        if text == tip:
            return slot
    raise PublishError(f"编辑页找不到封面位「{tip}」(页面改版了?)", retryable=False)


def _slot_src(page: Any, tip: str) -> str:
    """某个封面位上当前那张图的地址。"""
    slot = _cover_slot(page, tip)
    img = slot.locator("img")
    if not img.count():
        return ""
    try:
        return img.first.get_attribute("src") or ""
    except Exception:  # noqa: BLE001
        return ""


def _preview_src(page: Any) -> str:
    """弹窗里主预览图(第一个 img)的地址 —— 传完新图它会变。"""
    try:
        return page.eval_on_selector_all(
            "[role=dialog] img", "els => els.length ? (els[0].src || '') : ''") or ""
    except Exception:  # noqa: BLE001
        return ""


# --------------------------------------------------------------------------
# 小工具
# --------------------------------------------------------------------------
def _wait_until(fn: Any, *, timeout: float, gap: float = 0.5) -> bool:
    """轮询到条件成立为止;超时返回 False。"""
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        try:
            if fn():
                return True
        except Exception:  # noqa: BLE001
            pass
        time.sleep(gap)
    return False


def _dismiss_tips(page: Any) -> None:
    """关掉页面上飘出来的引导气泡(「我知道了」之类)。没有就算了。"""
    for text in ("我知道了", "知道了"):
        try:
            tip = page.get_by_text(text, exact=True)
            if tip.count() and tip.first.is_visible():
                tip.first.click()
                page.wait_for_timeout(500)
                return
        except Exception:  # noqa: BLE001
            continue


# --------------------------------------------------------------------------
# 传一张图
# --------------------------------------------------------------------------
def _description(page: Any) -> str:
    """简介输入框里现在的文字。拿来核对:换封面的过程中简介一个字都不能变。"""
    try:
        return page.evaluate(
            "() => { const els = [...document.querySelectorAll('[contenteditable=true]')];"
            " const e = els.sort((a, b) => b.innerText.length - a.innerText.length)[0];"
            " return e ? e.innerText : ''; }") or ""
    except Exception:  # noqa: BLE001
        return ""


def _upload_to_slot(page: Any, tip: str, path: Path) -> str:
    """往一个封面位上传一张图。成功返回一句给日志看的话,失败抛错。"""
    before = _slot_src(page, tip)
    if not clear_topic_popup(page, _cover_slot(page, tip).locator("div[class*=cover-]").first):
        raise PublishError(f"「{tip}」封面位被浮层盖住,关不掉,没敢点(页面改版了?)", retryable=False)
    slot = _cover_slot(page, tip)
    # 点封面图本身:上面盖着 `background-*` 层,hover 时才露出「编辑封面」文字,
    # 那个文字节点是隐藏的模板,点不动 —— 直接点图片区域。
    slot.locator("div[class*=cover-]").first.click(force=True)
    if not _wait_until(lambda: page.locator("[role=dialog]:visible").count() > 0, timeout=15):
        raise PublishError(f"「{tip}」的封面弹窗没打开(页面改版了?)", retryable=False)
    dialog = page.locator("[role=dialog]:visible").last
    want = DIALOG_TITLE.get(tip, "")
    head = (dialog.inner_text() or "").replace("\n", " ")
    if want and want not in head:
        raise PublishError(f"弹窗标题不是「{want}」而是「{head[:24]}」(页面改版了?)",
                           retryable=False)
    inputs = dialog.locator(COVER_INPUT)
    if not inputs.count():
        raise PublishError(f"「{tip}」的封面弹窗里没找到上传控件(页面改版了?)", retryable=False)
    shot = _preview_src(page)
    inputs.first.set_input_files(str(path))
    # 传完 input 的 value 会被组件复位(它读完文件就清空),所以不能靠 `files` 判断 ——
    # 看**弹窗主预览换没换**才是真的。
    if not _wait_until(lambda: bool(_preview_src(page)) and _preview_src(page) != shot,
                       timeout=60):
        raise PublishError(f"「{tip}」传上去后预览没变化,可能没收到", retryable=False)
    page.wait_for_timeout(1500)
    btn = None
    for name in CONFIRM_KEYS:
        cand = dialog.get_by_role("button", name=name, exact=True)
        if cand.count():
            btn = cand.first
            break
    if btn is None:
        raise PublishError(f"「{tip}」的弹窗里没有「完成」按钮(页面改版了?)", retryable=False)
    btn.click()
    if not _wait_until(lambda: page.locator("[role=dialog]:visible").count() == 0, timeout=20):
        try:
            page.keyboard.press("Escape")
        except Exception:  # noqa: BLE001
            pass
        raise PublishError(f"「{tip}」的弹窗没关掉,没敢往下走", retryable=False)
    after = _slot_src(page, tip)
    log.info("「%s」换上:页面上的图 %s -> %s", tip, before[-40:], after[-40:])
    return f"{tip} <- {Path(path).name}"


# --------------------------------------------------------------------------
# 主流程
# --------------------------------------------------------------------------
def _project_title(project_id: str) -> str:
    """从 `packaging.json` 里取这次用的那条标题。"""
    path = Path("data/projects") / project_id / "packaging.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ""
    titles = data.get("titles") or []
    if not titles:
        return ""
    picked = (data.get("picked") or {}).get("title")
    idx = picked if isinstance(picked, int) and 0 <= picked < len(titles) else 0
    return str((titles[idx] or {}).get("text") or "")


def replace_covers(*, project_id: str, title: str = "", cover: Path | None = None,
                   mid: str = "", headless: bool = True, submit: bool = True) -> str:
    """给已发布的作品换封面(横4:3 / 竖3:4 两个位)。成功返回一句说明。

    `cover` 用主封面文件路径 —— 横竖那两张重排版由 `cover_files` 按 4:3 / 3:4 去找。
    `submit=False` 只传不提交(演练,不消耗平台的修改次数)。
    """
    from . import browser

    title = title or _project_title(project_id)
    files = cover_files(cover)
    if not files:
        raise PublishError("没有封面文件,换不了", retryable=False)
    notes: list[str] = []
    try:
        with browser.BrowserSession("douyin", headless=headless, lock_timeout=120) as session:
            page = session.page
            if mid:
                session.goto(EDIT_URL.format(mid=mid), wait_ms=8000)
            else:
                session.goto(CREATOR_MANAGE, wait_ms=8000)
                if "扫码登录" in session.page_text():
                    raise PublishError("抖音登录已失效:去分发台点「登录」扫码一次",
                                       retryable=False, credential=True)
                card = _card_of(page, title)
                card.get_by_text("编辑作品", exact=True).first.click()
                page.wait_for_timeout(3000)
                if not page.url.startswith("https://creator.douyin.com/creator-micro/content/post/video"):
                    raise PublishError("点「编辑作品」没进编辑页(页面改版了?)", retryable=False)
            if "扫码登录" in session.page_text():
                raise PublishError("抖音登录已失效:去分发台点「登录」扫码一次",
                                   retryable=False, credential=True)
            _dismiss_tips(page)
            desc_before = _description(page)
            for tip, _step, path in files:
                notes.append(_upload_to_slot(page, tip, path))
                _dismiss_tips(page)
            if _description(page) != desc_before:
                raise PublishError("换封面过程中简介被改动了,没提交(页面改版了?)", retryable=False)
            if not submit:
                try:
                    shot = Path("data/_cache") / f"douyin_cover_{project_id}.png"
                    shot.parent.mkdir(parents=True, exist_ok=True)
                    page.screenshot(path=str(shot), full_page=True)
                    log.info("演练截图:%s", shot)
                except Exception:  # noqa: BLE001
                    pass
                session.save_state()
                return "演练完成(没提交):" + ";".join(notes)
            # 提交修改 —— 这一步消耗平台的修改次数,不重试
            submit_btn = page.locator(SUBMIT_BUTTON)
            if not submit_btn.count():
                raise PublishError("编辑页没找到「提交修改」按钮(页面改版了?)", retryable=False)
            submit_btn.first.click()
            page.wait_for_timeout(3000)
            # 可能再弹一次二次确认
            if page.locator("[role=dialog]:visible").count():
                dlg2 = page.locator("[role=dialog]:visible").last
                for name in ("确定", "确认", "提交", "我知道了", "知道了"):
                    cand = dlg2.get_by_role("button", name=name, exact=True)
                    if cand.count():
                        cand.first.click()
                        page.wait_for_timeout(2000)
                        break
            page.wait_for_timeout(5000)
            body = session.page_text()
            if LIMIT_HINT in body and "修改成功" not in body and "已提交" not in body:
                log.info("提交后页面文案:%s", body[:200])
            try:
                shot = Path("data/_cache") / f"douyin_cover_submitted_{project_id}.png"
                shot.parent.mkdir(parents=True, exist_ok=True)
                page.screenshot(path=str(shot), full_page=True)
            except Exception:  # noqa: BLE001
                pass
            session.save_state()
    except browser.BrowserUnavailable as exc:
        raise PublishError(str(exc), retryable=False) from exc
    except PublishError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise PublishError(f"换封面失败:{type(exc).__name__}: {exc}", retryable=False) from exc
    return "封面已替换:" + ";".join(notes)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="给已发布的抖音作品换封面")
    parser.add_argument("--project", required=True, help="项目 id,如 p_bea59bb72c3e")
    parser.add_argument("--title", default="", help="作品标题(默认从 packaging.json 取)")
    parser.add_argument("--cover", default="", help="主封面路径(默认 data/projects/<id>/cover.jpg)")
    parser.add_argument("--mid", default="", help="作品 id(给了就直接进编辑页)")
    parser.add_argument("--dry-run", action="store_true", help="只传不提交,不消耗修改次数")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    cover = Path(args.cover) if args.cover else Path("data/projects") / args.project / "cover.jpg"
    try:
        msg = replace_covers(project_id=args.project, title=args.title, cover=cover,
                             mid=args.mid, submit=not args.dry_run)
    except PublishError as exc:
        print(f"失败:{exc}")
        return 1
    print(msg)
    return 0


if __name__ == "__main__":
    sys.exit(main())
