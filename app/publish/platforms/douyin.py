"""抖音适配器:开放平台优先,创作者平台网页版兜底。

**两条路**:

1. **开放平台(正规路)**:OAuth2 换 token → 分片上传 → `create_video` 建片。
   需要企业开发者资质与 `video.create` 能力审核。返回 `item_id` 就能查数据。
2. **创作者平台网页版(兜底)**:Playwright 驱动已登录的浏览器走发布页。
   没有资质时的唯一路,但页面改版就得跟进。

**100MB 限制**:开放平台单文件 ≤100MB。我们的片子 1080p 五分钟能有 200MB+,
所以超限时先用 ffmpeg 转一版 720p 再传 —— 平台自己推荐的也是 720p 起。
"""
from __future__ import annotations

import json
import logging
import re
import subprocess
import time
from pathlib import Path
from typing import Any

from ..base import Account, AccountStatus, MetricSnapshot, PlatformRef, PublishError, \
    PublishRequest, PublishResult
from ..http import UPLOAD_TIMEOUT, new_client, request_json
from ..registry import PORTRAIT

log = logging.getLogger(__name__)

OPEN = "https://open.douyin.com"
#: 创作者平台发布页(网页版兜底)
CREATOR_UPLOAD = "https://creator.douyin.com/creator-micro/content/upload"
#: 作品管理页:页面自己会请求 `WORK_LIST_API`,每条作品带播放、完播率、2 秒跳出、涨粉
CREATOR_MANAGE = "https://creator.douyin.com/creator-micro/content/manage"
WORK_LIST_API = "/janus/douyin/creator/pc/work_list"
#: 平台文档给的单文件上限
MAX_UPLOAD = 100 * 1024 * 1024
#: 单片建议 20MB
PART_SIZE = 20 * 1024 * 1024
#: 网页版标题框上限(页面上写着 0/30)
TITLE_MAX = 30
#: 网页版等视频传完(页面出现「重新上传」)的时限
UPLOAD_WAIT_MS = 10 * 60 * 1000
#: 发布页打开后,上传控件要等页面把「加载中,请稍候」跑完才出现 —— 实测约 18 秒,
#: 等不够就判「没找到上传入口」,其实页面只是慢(直连真实 IP 时更慢)。
UPLOAD_ENTRY_TIMEOUT_MS = 60 * 1000
#: 点「发布」后页面先挂「正在发布」(实测 20 秒以上),跳到作品管理才算发完 —— 提前关浏览器等于撤稿
PUBLISH_WAIT_MS = 3 * 60 * 1000
PUBLISH_DONE_TEXTS = ("发布成功", "投稿成功")

#: 网页版封面两个位:(位上的提示字, 弹窗里切到这一步的按钮/页签)
COVER_SLOTS = (("横封面4:3", "设置横封面"), ("竖封面3:4", "设置竖封面"))
#: 封面弹窗里「上传封面」那个隐藏的 file input(按钮本身被上传层盖住,点不到)
COVER_INPUT = ("div:has(> .semi-upload):has-text('上传封面') > .semi-upload "
               "> input.semi-upload-hidden-input")
#: 「作品描述」框:抖音网页版用的是自研编辑器(contenteditable 的 div),不是 textarea
DESC_SELECTORS = (
    "div.zone-container[contenteditable=true]",
    "div[contenteditable=true][data-placeholder*='简介']",
    "div[contenteditable=true]",
)
#: 发布后回查作品 id:作品入库有延迟,查几轮
WORK_RESOLVE_ROUNDS = 4
#: 回查两轮之间隔多久(秒)
WORK_RESOLVE_GAP = 6


def _int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def cover_files(cover: Path | None) -> list[tuple[str, str, Path]]:
    """网页版两个封面位各传哪张图:[(位上的提示字, 弹窗步骤名, 图)]。

    优先用按 4:3 / 3:4 **重新排版**的那张(`cover_art.build` 出的);没有就退回主封面,
    但只在主封面横竖和这个位一致时 —— 竖封面塞进 4:3 位,平台一裁就只剩中间一条。
    两张都没有的位跳过,平台自己从视频里截帧。"""
    from ...pipeline.narration import cover_3x4_path, cover_4x3_path

    if not cover or not Path(cover).is_file():
        return []
    cover = Path(cover)
    landscape = True
    try:
        from PIL import Image

        with Image.open(cover) as im:
            landscape = im.width >= im.height
    except OSError:
        pass
    out: list[tuple[str, str, Path]] = []
    for (tip, step), exact, same_way in zip(COVER_SLOTS, (cover_4x3_path(cover), cover_3x4_path(cover)),
                                            (landscape, not landscape)):
        if exact.is_file():
            out.append((tip, step, exact))
        elif same_way:
            out.append((tip, step, cover))
    return out


def _cover_srcs(page: Any) -> list[str]:
    """两个封面位上当前那张图的地址。传成功后会换成新地址 —— 拿它判断有没有设上。"""
    return page.eval_on_selector_all(
        "[class*=coverControl]", "els => els.map(e => (e.querySelector('img') || {}).src || '')")



def slot_covered(slot: Any) -> bool:
    """封面位(一个 locator)正中心上面盖着别的东西 —— 话题联想浮层之类 —— 吗。"""
    return bool(slot.evaluate(
        "e => { const r = e.getBoundingClientRect();"
        " const t = document.elementFromPoint(r.x + r.width / 2, r.y + r.height / 2);"
        " return !(t && (e === t || e.contains(t))); }"))


def clear_topic_popup(page: Any, slot: Any) -> bool:
    """让封面位露出来。成功(本来就没盖 / 关掉了)返回 True,关不掉返回 False。

    简介末尾带话题时,光标停在「#话题」后面,输入框就一直弹着「#话题」联想浮层,盖住封面位。
    Esc、点空白处、失焦都关不掉它,而 `force` 点击会直接点进浮层、把一个话题插进简介。
    所以把光标点到简介第一行(不改字),浮层随光标离开消失。调用方只在返回 True 时才点封面位。"""
    for _ in range(3):
        if not slot_covered(slot):
            return True
        try:
            box = page.locator("[contenteditable=true]").first.bounding_box()
            if box:
                page.mouse.click(box["x"] + 30, box["y"] + 8)
        except Exception:  # noqa: BLE001
            pass
        page.wait_for_timeout(700)
    return not slot_covered(slot)


#: 上传完平台会弹这个引导框(「设置竖封面获更多流量」),它盖住整页:
#: 封面位点不到、底部的「发布」按钮也点不到(按钮还是灰的)。
COVER_PROMPT_TEXTS = ("暂不设置",)


def dismiss_cover_prompt(session: Any) -> bool:
    """关掉上传后弹的封面引导框。关掉了返回 True。"""
    page = session.page
    for text in COVER_PROMPT_TEXTS:
        try:
            btn = page.get_by_text(text, exact=True)
            if btn.count() and btn.first.is_visible():
                btn.first.click(timeout=5000)
                page.wait_for_timeout(1200)
                return True
        except Exception:  # noqa: BLE001
            continue
    return False


def wait_publish_ready(session: Any, timeout_ms: int = 300000) -> bool:
    """等「发布」按钮真的能点。

    平台还在上传 / 处理视频时,这个按钮是灰的 —— 点它等于没点(实跑踩过:
    点完页面一点动静没有,最后报「网页版发布后没找到成功提示」,作品也没发出去)。
    所以这里等按钮可点了再往下走。
    """
    page = session.page
    deadline = time.time() + timeout_ms / 1000
    while time.time() < deadline:
        try:
            btn = page.get_by_role("button", name="发布", exact=True).first
        except Exception:  # noqa: BLE001 - 这个页面没有这套 API(比如测试里的假页面):按就绪算
            return True
        try:
            if btn.count() and btn.is_visible() and btn.is_enabled():
                return True
        except Exception:  # noqa: BLE001
            pass
        dismiss_cover_prompt(session)
        page.wait_for_timeout(2000)
    return False


def wait_upload_done(session: Any, timeout_ms: int = 600000) -> bool:
    """等视频**真的**传完(页面上的「重新上传」出现)。

    注意:页面里那个「上传中」的字样**不会自己消失**(一直挂在提示里),
    拿它判断会永远等下去(实跑踩过:等到 10 分钟超时,其实早就传完了)。
    """
    page = session.page
    deadline = time.time() + timeout_ms / 1000
    while time.time() < deadline:
        try:
            body = session.page_text() or ""
        except Exception:  # noqa: BLE001
            body = ""
        if "重新上传" in body:
            return True
        dismiss_cover_prompt(session)
        page.wait_for_timeout(3000)
    return False


def publish_done(session: Any) -> bool:
    """发布成功的判据:页面上有**正好是**「发布成功 / 投稿成功」的气泡,或者已经跳到作品管理页。

    不能用「页面里包含『发布成功』」来判断 —— 页面上有「发布成功后不可修改」这类提示,
    包含判断会把它当成已经发出去(实跑踩过:报成功,作品列表里根本没有这条)。
    """
    page = session.page
    try:
        for text in PUBLISH_DONE_TEXTS:
            if page.get_by_text(text, exact=True).count():
                return True
    except Exception:  # noqa: BLE001 - 页面没有这套 API:退回包含判断
        try:
            body = session.page_text() or ""
        except Exception:  # noqa: BLE001
            body = ""
        return any(t in body for t in PUBLISH_DONE_TEXTS)
    return "content/manage" in (page.url or "")


def close_cover_editor(session: Any) -> None:
    """关掉封面编辑器(外层那个大对话框)。

    它开着的时候,底下的「自主声明」「作品描述」都点不到 —— 实跑踩过:
    声明那一步报「没有『内容由AI生成』这一项」,其实是被封面编辑器挡着,
    脚本点到的根本不是声明那个下拉。
    """
    page = session.page
    try:
        btn = page.get_by_role("button", name="完成", exact=True).last
        if btn.count():
            btn.first.click(timeout=5000)
            page.wait_for_timeout(1200)
            return
    except Exception:  # noqa: BLE001
        pass
    try:
        page.keyboard.press("Escape")
        page.wait_for_timeout(800)
    except Exception:  # noqa: BLE001
        pass


def set_covers(session: Any, cover: Path | None) -> str:
    """在发布页的封面弹窗里把竖 / 横封面传上去。成功返回空串;没设上返回一句说明。

    设不上**不拦发布**:平台会自己截帧当封面,片子照样能发;说明写进发布结果给人看。

    实跑踩过的坑(都在这里绕开):
    - 封面位上写的是「选择封面」几个字,点**文字**才弹编辑器;点外层容器没反应,
      于是等弹窗超时,封面一直是平台自动截帧。
    - 填简介时弹出的「#添加话题 / @好友」和话题联想浮层盖住封面区,会吃掉点击(`force` 点击还会
      把话题插进简介)—— 先 `clear_topic_popup` 把光标挪开,封面位露出来才点。
    - 弹窗里的上传控件是 `input.semi-upload-hidden-input`(按钮被上传层盖住,点不到)。
    """
    files = cover_files(cover)
    if not files:
        return "没有封面文件,用平台自动截帧"
    page = session.page
    dialog = None
    step = "找封面位"
    try:
        # 上传后平台弹的封面引导框会盖住整页,先关掉
        dismiss_cover_prompt(session)
        try:
            page.keyboard.press("Escape")
            page.wait_for_timeout(300)
        except Exception:  # noqa: BLE001 - 关不掉就靠 force 点击
            pass
        for tip, _step, path in files:
            step = f"找封面位「{tip}」"
            slot = page.locator("[class*=coverControl]", has_text=tip).first
            if not slot.count():
                return f"没找到封面位「{tip}」,用平台自动截帧"
            # 封面位在页面很下面(y 坐标好几万像素),**不滚过去不能做遮挡检测**:
            # 元素在视口外时 elementFromPoint 拿不到它,一律判成「被浮层盖住」→
            # 两次封面都没设上,竖封面位空着,平台feed就用了横封面(实跑踩过:
            # 线上封面是 720x540 的横版,账号里别的作品都是 540x720)。
            try:
                slot.scroll_into_view_if_needed(timeout=8000)
                page.wait_for_timeout(600)
            except Exception:  # noqa: BLE001
                pass
            step = f"点开「{tip}」的封面弹窗"
            if not clear_topic_popup(page, slot.locator("[class*=cover-]").first):
                return f"封面位「{tip}」被浮层盖住,没敢点,用平台自动截帧"
            btn = slot.get_by_text("选择封面", exact=True)
            if btn.count():
                btn.first.click(force=True)
            else:
                slot.locator("[class*=cover-]").first.click(force=True)
            step = f"等「{tip}」的弹窗"
            dialog = page.locator("[role=dialog]:visible").last
            dialog.wait_for(timeout=15000)
            # 封面编辑器里会叠一层「设置竖封面获更多流量」的引导框,盖住整个编辑器,
            # 先把它关掉,再重新取对话框(关掉后编辑器才是最后一个)
            dismiss_cover_prompt(session)
            dialog = page.locator("[role=dialog]:visible").last
            before = _dialog_preview(dialog)
            step = f"在「{tip}」弹窗里找上传控件"
            upload = None
            for selector in (COVER_INPUT, "input.semi-upload-hidden-input", "input[type=file]"):
                cand = dialog.locator(selector)
                if cand.count():
                    upload = cand
                    break
            if upload is None:
                return f"封面位「{tip}」里没找到上传控件,用平台自动截帧"
            step = f"往「{tip}」传封面"
            upload.first.set_input_files(str(path), timeout=20000)
            page.wait_for_timeout(4000)
            after = _dialog_preview(dialog)
            step = f"点「{tip}」弹窗的完成"
            done = dialog.get_by_role("button", name="完成", exact=True)
            if done.count():
                try:
                    done.first.click(timeout=5000)
                except Exception:  # noqa: BLE001 - 被浮层挡住就 force 点
                    done.first.click(force=True)
            else:
                page.keyboard.press("Escape")
            page.wait_for_timeout(1500)
            if before and after and before == after:
                return f"封面位「{tip}」传上去没变化,用平台自动截帧"
        # 封面都设完了,把编辑器本身关掉 —— 它开着的话底下的声明 / 简介都点不到
        close_cover_editor(session)
    except Exception as exc:  # noqa: BLE001 - 封面设不上不该拦住发布
        try:
            if dialog is not None and dialog.count():
                page.keyboard.press("Escape")
                page.wait_for_timeout(1000)
        except Exception:  # noqa: BLE001
            pass
        return f"封面没设上({step}:{type(exc).__name__}),用平台自动截帧"
    return ""


def _dialog_preview(dialog: Any) -> str:
    """弹窗里主预览图的地址 —— 传完新图它会变,拿它判断有没有真的换上。

    弹窗刚打开时可能一张图都还没有,这时**不能等**:等元素会一直等到超时,
    整个设封面就卡在这一步(实跑踩过)。拿不到就返回空串。
    """
    try:
        loc = dialog.locator("img").first
        if not loc.count():
            return ""
        return loc.get_attribute("src", timeout=2000) or ""
    except Exception:  # noqa: BLE001
        return ""


def dismiss_topic_popup(page: Any) -> bool:
    """把话题联想浮层关掉:光标点到简介第一行(不改字),浮层随光标离开消失。

    简介末尾带「#话题」时,光标停在「#」后面,联想浮层一直弹着。它会盖住页面
    底部的「发布」按钮,点它会一直等到超时(被浮层拦截)。挪开光标就没了。
    """
    try:
        box = page.locator("[contenteditable=true]").first.bounding_box()
        if not box:
            return False
        page.mouse.click(box["x"] + 30, box["y"] + 8)
        page.wait_for_timeout(600)
        return True
    except Exception:  # noqa: BLE001
        return False


def publish_confirmed(session: Any) -> bool:
    """页面上是不是已经出现「发布成功」的迹象。"""
    try:
        body = session.page_text()
    except Exception:  # noqa: BLE001
        body = ""
    if any(t in body for t in PUBLISH_DONE_TEXTS):
        return True
    return "content/manage" in (session.page.url or "")


def click_publish(session: Any) -> str:
    """点提交按钮。返回空串 = 点到了;返回一句说明 = 没点到(调用方拿它当报错文案)。

    点击超时**不等于没点到**:Playwright 点完还要等页面稳下来,这一步超时会把
    异常抛出来,但那一下其实已经生效了(平台已经收了片子)。所以点完异常时先看
    页面上是不是已经有成功迹象 —— 有就当成功,**绝不能再点一下**,再点一遍
    就是同一条片子在账号里多留一条。
    """
    page = getattr(session, "page", None)
    reason = "页面上没有完全等于「发布」/「提交」的按钮"
    for selector in ("button:text-is('发布')", "button:text-is('提交')"):
        try:
            if not session.exists(selector, timeout=1500):
                continue
        except Exception:  # noqa: BLE001
            continue
        try:
            session.click(selector)
            return ""
        except Exception as exc:  # noqa: BLE001 - 被浮层盖住 / 点击超时
            reason = str(exc)[:160]
        # 点不动:可能话题浮层盖着,也可能「点超时但其实已经生效了」。
        # 先确认平台上是不是已经收了片子,没确认上才换 force 点。
        for _ in range(3):
            if publish_confirmed(session):
                return ""
            try:
                page.wait_for_timeout(1500)
            except Exception:  # noqa: BLE001
                break
        if publish_confirmed(session):
            return ""
        try:
            dismiss_topic_popup(page)
            loc = page.locator(selector).first
            loc.scroll_into_view_if_needed(timeout=5000)
            loc.click(force=True, timeout=6000)
            return ""
        except Exception as exc:  # noqa: BLE001
            reason = str(exc)[:160]
    return f"发布页没找到或点不到「发布」按钮(页面改版了?):{reason}"


def _click_publish(session: Any) -> bool:
    """点提交按钮。返回是否点到了。

    「发布」这个字页面上有好几处:左栏「作品发布」、时间选项「立即发布」、
    折叠项「发布设置」—— 按文字模糊匹配会点到这些,点了等于没发。所以只认
    **完全等于**「发布」/「提交」的按钮。
    """
    return click_publish(session) == ""


def set_description(session: Any, description: str, hashtags: str = "") -> str:
    """把简介(免责声明 + 话题)填进「作品描述」框。

    网页版发布页的简介框是自研编辑器:contenteditable 的 div,不是 textarea。
    只填标题不填简介,等于品类免责声明和话题都没发出去。
    填不上**不拦发布**:平台照发,只是简介是空的;说明写进发布结果给人看。
    """
    text = " ".join(x for x in (description, hashtags) if x).strip()
    if not text:
        return ""
    page = session.page
    for selector in DESC_SELECTORS:
        try:
            loc = page.locator(selector).first
            if not loc.count():
                continue
            loc.fill(text)
            page.wait_for_timeout(600)
            got = (loc.inner_text() or "").strip()
            if got:
                return f"简介已填 {len(got)} 字"
        except Exception:  # noqa: BLE001 - 换下一个选择器
            continue
    return "简介没填上(作品描述框找不到,页面改版了?)"


# --------------------------------------------------------------------------
# 自主声明:AI 生成内容必须挂标识
# --------------------------------------------------------------------------
#: 平台给的六个选项(2026-10 实跑看到的)
DECLARE_AI = "内容由AI生成"
DECLARE_NONE = "无需添加自主声明"
#: 「自主声明」那一行里下拉触发器的位置:标签右边那个控件。
#: **类名是构建期哈希**(selectText-XSrMFZ,每次发版都变),只能靠结构 + 文案定位。
_DECLARE_ROW = ("xpath=//*[normalize-space(text())='自主声明']"
                "/ancestor::*[1]/following-sibling::*[1]")


def _declaration_trigger(page: Any) -> Any:
    """「自主声明」那一行的下拉触发器。找不到返回 None。

    优先从「自主声明」标签右边的控件里找;找不着再退一步:
    还没选过的时候,触发器上就写着「请选择自主声明」。"""
    try:
        row = page.locator(_DECLARE_ROW)
        if row.count():
            inner = row.locator("[class*='selectText']")
            if inner.count():
                return inner.first
    except Exception:  # noqa: BLE001 - 换下一种找法
        pass
    try:
        loc = page.get_by_text("请选择自主声明", exact=True)
        return loc.first if loc.count() else None
    except Exception:  # noqa: BLE001
        return None


def set_declaration(session: Any, kind: str = DECLARE_AI) -> str:
    """在发布页的「自主声明」里选一项(默认「内容由AI生成」)。

    **不拦发布**:找不到下拉、点不开、平台改了文案 —— 只记一句说明,片子照发。

    为什么必须勾:《人工智能生成合成内容标识办法》2025-09-01 起,AI 生成内容
    应当主动声明;抖音社区规则明确未标识 AI 内容将被限流、多次违规封号。
    不主动声明的话平台会自己打「疑似使用了AI生成技术,请谨慎甄别」——
    作者主动声明是「我说清楚了」,被平台标是「平台怀疑你」,观感完全不同。

    实跑踩过的坑:
    - 触发器的类名是构建期哈希,不能写死,要从「自主声明」标签的结构里找。
    - 选项是 radio,文字包在 `label` 里 —— 直接点文字会被 label 挡住
      (Playwright 报 "<label> intercepts pointer events"),得点 label 本身。
    - 弹层底部有「取消 / 确定」,选完得点「确定」才生效。
    """
    page = session.page
    try:
        trigger = _declaration_trigger(page)
        if trigger is None:
            return "发布页没找到「自主声明」(页面改版了?)"
        current = (trigger.inner_text() or "").strip()
        if current == kind:
            return ""  # 已经声明过了,别重复点
        try:
            trigger.click(timeout=3000)
        except Exception:  # noqa: BLE001 - 被浮层盖住就硬点
            trigger.click(force=True)
        page.wait_for_timeout(600)
        option = page.locator("label", has_text=kind).first
        if not option.count():
            option = page.get_by_text(kind, exact=True).first
        if not option.count():
            return f"「自主声明」里没有「{kind}」这一项(平台改文案了?)"
        option.click()
        page.wait_for_timeout(400)
        confirm = page.locator("span.semi-button-content", has_text="确定").first
        if confirm.count():
            confirm.click()
        page.wait_for_timeout(800)
        now = (trigger.inner_text() or "").strip()
        if now != kind:
            return f"「自主声明」选了「{kind}」但没生效(显示「{now}」)"
        return ""
    except Exception as exc:  # noqa: BLE001 - 声明失败不拦发布
        return (f"「自主声明」没设上({type(exc).__name__}),"
                f"片子照发,但可能被打上「疑似AI」标签")


def _config(key: str) -> str:
    from ... import config

    return str(getattr(config, key, "") or "")


def _declare_enabled(req: Any) -> bool:
    """这条要不要挂「内容由AI生成」标识。

    默认按配置来(本项目出的片子全是 AI 生成);单次请求可以用
    `extra={"declare_ai": False}` 关掉 —— 比如发一条真人拍的素材。"""
    from ... import config

    raw = (req.extra or {}).get("declare_ai")
    if raw is not None:
        return bool(raw)
    return bool(getattr(config, "DOUYIN_DECLARE_AI", True))

class DouyinAdapter:
    """抖音(开放平台优先,网页版兜底)。"""

    key = "douyin"
    label = "抖音"
    preferred_aspect = PORTRAIT
    auth_kind = "oauth"

    # -- 凭据 ------------------------------------------------------------
    def _access_token(self, account: Account) -> str:
        token = account.cred("access_token")
        if not token:
            raise PublishError("没有配置抖音 access_token", retryable=False, credential=True)
        return token

    def _open_id(self, account: Account) -> str:
        open_id = account.cred("open_id")
        if not open_id:
            raise PublishError("没有配置抖音 open_id", retryable=False, credential=True)
        return open_id

    def _official_ready(self, account: Account) -> bool:
        return bool(account.cred("access_token") and account.cred("open_id"))

    # -- 自检 ------------------------------------------------------------
    def check_account(self, account: Account) -> AccountStatus:
        if not self._official_ready(account):
            return AccountStatus(
                False, "没配开放平台凭据,只能走创作者平台网页版",
                hint="填 access_token 与 open_id(开放平台),或先在分发台点「登录」扫码创作者平台。")
        token = self._access_token(account)
        open_id = self._open_id(account)
        with new_client() as client:
            try:
                data = request_json(client, "GET", f"{OPEN}/oauth/userinfo/",
                                    params={"open_id": open_id},
                                    headers={"access-token": token}, timeout=30)
            except PublishError as exc:
                return AccountStatus(False, exc.message, hint="token 可能已过期,重新授权一次")
        extra = data.get("data") or {}
        if data.get("error_code") not in (0, None):
            return AccountStatus(False, f"平台返回 error_code={data.get('error_code')}",
                                 hint="重新授权一次")
        return AccountStatus(True, f"已授权:{extra.get('nickname') or open_id}",
                             detail={"open_id": open_id, "nickname": extra.get("nickname")})

    # -- 发布 ------------------------------------------------------------
    def publish(self, req: PublishRequest, account: Account) -> PublishResult:
        if self._official_ready(account):
            return self._publish_official(req, account)
        return self._publish_browser(req, account)

    def _publish_official(self, req: PublishRequest, account: Account) -> PublishResult:
        token = self._access_token(account)
        open_id = self._open_id(account)
        video = Path(req.video_path)
        if not video.is_file():
            raise PublishError(f"成片不存在:{video.name}", retryable=False)
        upload_path = self._fit_size(video)
        try:
            video_id = self._upload(token, open_id, upload_path)
            return self._create(token, open_id, video_id, req)
        finally:
            if upload_path != video:
                try:
                    upload_path.unlink(missing_ok=True)
                except OSError:
                    pass

    def _fit_size(self, video: Path) -> Path:
        """超过 100MB 就转一版 720p。平台自己推荐的也是 720p 起。"""
        if video.stat().st_size <= MAX_UPLOAD:
            return video
        from ... import config

        if not config.FFMPEG_PATH:
            raise PublishError(
                f"文件 {video.stat().st_size // 1024 // 1024}MB 超过抖音 100MB 上限,"
                "又没找到 ffmpeg 可转码", retryable=False)
        out = video.parent / (video.stem + "_720p.mp4")
        if out.is_file() and out.stat().st_size <= MAX_UPLOAD:
            return out
        proc = subprocess.run(
            [str(config.FFMPEG_PATH), "-y", "-v", "error", "-i", str(video),
             "-vf", "scale=-2:720", "-c:v", "libx264", "-preset", "medium", "-crf", "26",
             "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart", str(out)],
            capture_output=True, text=True, timeout=3600)
        if proc.returncode != 0 or not out.is_file():
            raise PublishError(f"转 720p 失败:{(proc.stderr or '')[-200:]}", retryable=True)
        return out

    def _upload(self, token: str, open_id: str, video: Path) -> str:
        """上传,拿 video_id。"""
        url = f"{OPEN}/api/douyin/v1/video/upload_video/?open_id={open_id}"
        with new_client(timeout=UPLOAD_TIMEOUT) as client:
            with video.open("rb") as fh:
                resp = client.post(url, headers={"access-token": token},
                                   files={"video": (video.name, fh, "video/mp4")},
                                   timeout=UPLOAD_TIMEOUT)
        if resp.status_code in (401, 403):
            raise PublishError(f"抖音拒绝上传(HTTP {resp.status_code}):{resp.text[:200]}",
                               retryable=False, credential=True)
        if resp.status_code >= 400:
            raise PublishError(f"抖音返回 {resp.status_code}:{resp.text[:200]}",
                               retryable=resp.status_code >= 500)
        try:
            data = resp.json()
        except (json.JSONDecodeError, ValueError) as exc:
            raise PublishError(f"抖音返回的不是 JSON:{resp.text[:200]}", retryable=True) from exc
        extra = data.get("extra") or {}
        if extra.get("error_code") not in (0, None):
            code = extra.get("error_code")
            if code in (28001003, 28001008):
                raise PublishError("抖音 token 失效,重新授权", retryable=False, credential=True)
            raise PublishError(f"抖音上传失败:error_code={code} {extra.get('description','')}",
                               retryable=code == 2100004)
        video_id = (data.get("data") or {}).get("video_id") or ""
        if not video_id:
            raise PublishError(f"抖音上传返回里没有 video_id:{resp.text[:200]}", retryable=True)
        return str(video_id)

    def _create(self, token: str, open_id: str, video_id: str,
                req: PublishRequest) -> PublishResult:
        """建片(挂文案与封面)。"""
        text = req.title or ""
        if req.tags:
            topics = " ".join(f"#{t}" for t in req.tags[:5])
            text = f"{text} {topics}".strip()
        body: dict[str, Any] = {
            "video_id": video_id,
            "text": text[:1000],
            "download_type": 0,
            "private_status": 0,
        }
        # 封面:用第 1 秒那一帧(平台自己从视频里取)
        body["cover_tsp"] = 1.0
        url = f"{OPEN}/api/douyin/v1/video/create_video/?open_id={open_id}"
        with new_client() as client:
            data = request_json(client, "POST", url, json=body,
                                headers={"access-token": token, "content-type": "application/json"},
                                timeout=120)
        extra = data.get("extra") or {}
        if extra.get("error_code") not in (0, None):
            code = extra.get("error_code")
            if code == 2114007:
                raise PublishError("今天的发布数超过平台上限(75 条)", retryable=False)
            raise PublishError(f"建片失败:error_code={code} {extra.get('description','')}",
                               retryable=code == 2100004)
        payload = data.get("data") or {}
        item_id = str(payload.get("item_id") or "")
        vid = str(payload.get("video_id") or "")
        if not item_id:
            raise PublishError(f"建片返回里没有 item_id:{json.dumps(payload, ensure_ascii=False)[:200]}",
                               retryable=True)
        message = "已提交,平台审核中"
        if _declare_enabled(req):
            # 开放平台的建片接口没有「自主声明」字段(声明是创作者中心侧的),
            # 走这条路只能靠平台自动识别 → 要挂标识得去创作者中心手动勾。
            message += ";开放平台接口无自主声明字段,AI 标识需去创作者中心手动勾"
        return PublishResult(
            ok=True, state="submitted", remote_id=item_id,
            remote_url=f"https://www.douyin.com/video/{vid}" if vid else "",
            message=message, raw={"item_id": item_id, "video_id": vid},
        )

    # -- 网页版兜底 ------------------------------------------------------
    def _publish_browser(self, req: PublishRequest, account: Account) -> PublishResult:
        from .. import browser

        video = Path(req.video_path)
        if not video.is_file():
            raise PublishError(f"成片不存在:{video.name}", retryable=False)
        cover_note = ""
        desc_note = ""
        started_at = time.time()
        try:
            with browser.BrowserSession("douyin") as session:
                # 打开发布页可能撞上瞬时断连(ERR_CONNECTION_CLOSED),重试几次:
                # 一次打不开就判「页面改版」是冤枉平台了(实跑踩过)。
                opened = False
                last_err = ""
                for _ in range(3):
                    try:
                        session.goto(CREATOR_UPLOAD, wait_ms=4000)
                        opened = True
                        break
                    except Exception as exc:  # noqa: BLE001
                        last_err = str(exc)[:120]
                        time.sleep(3)
                if not opened:
                    raise PublishError(f"打不开创作者平台发布页:{last_err}", retryable=True)
                if not session.exists("input[type=file]", timeout=UPLOAD_ENTRY_TIMEOUT_MS):
                    if "扫码登录" in session.page_text():
                        raise PublishError("抖音登录已失效:去分发台点「登录」扫码一次",
                                           retryable=False, credential=True)
                    raise PublishError("创作者平台发布页没找到上传入口(页面改版了?)",
                                       retryable=True)
                # 上传控件要等页面把「加载中」跑完才出现(约 18 秒),给足等待
                session.upload_file("input[type=file]", video, timeout=90000)
                if not session.wait_text("重新上传", timeout=UPLOAD_WAIT_MS):
                    raise PublishError("视频上传没在时限内完成", retryable=True)
                # 「重新上传」出现 = 文件传完了,但平台还在转码/生成封面,这时「发布」
                # 按钮是灰的 —— 点它等于没点。等它可点再往下走。
                wait_publish_ready(session, timeout_ms=180 * 1000)
                for selector in ("input[name=title]", "input[placeholder*='标题']",
                                 "input[class*='title']"):
                    if session.exists(selector, timeout=1500):
                        session.fill(selector, (req.title or "")[:TITLE_MAX])
                        break
                # 顺序要紧:**先设封面,再填简介**。填简介会弹出「#添加话题 / @好友」
                # 的浮层,它就盖在封面区上,封面位上的「选择封面」点了没反应(实跑踩过:
                # 等弹窗等超时,封面一直是平台自动截帧)。
                cover_note = set_covers(session, req.cover_path)
                # 自主声明要赶在填简介**之前**:填简介会弹「#添加话题」浮层,那浮层会
                # 盖住下面「自主声明」这一行的下拉,勾不上。
                declare_note = ""
                if _declare_enabled(req):
                    declare_note = set_declaration(session, DECLARE_AI)
                topics = " ".join(f"#{str(t).strip()}" for t in (req.tags or [])[:5] if str(t).strip())
                desc_note = set_description(session, req.description, hashtags=topics)
                # 点发布前:封面引导框可能又冒出来(它盖住整页),先关掉;
                # 再确认「发布」按钮已经是可点的(处理中的时候它是灰的)
                dismiss_cover_prompt(session)
                wait_publish_ready(session, timeout_ms=90 * 1000)
                # 按文字精确匹配:左栏有个「作品发布」按钮,模糊匹配会先命中它,点了等于没发
                reason = click_publish(session)
                if reason:
                    raise PublishError(reason, retryable=False)
                body = ""
                ok = False
                for _ in range(PUBLISH_WAIT_MS // 3000):
                    session.page.wait_for_timeout(3000)
                    body = session.page_text()
                    if publish_done(session):
                        ok = True
                        break
                    if "接收短信验证码" in body:  # 平台要本人验证,只能人来;重试只会反复发短信
                        raise PublishError("抖音要求短信验证(本人操作):在分发台点「登录」,"
                                           "在打开的窗口里手动发一次稿并输入验证码",
                                           retryable=False, credential=True)
                session.save_state()
        except browser.BrowserUnavailable as exc:
            raise PublishError(str(exc), retryable=False) from exc
        except PublishError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise PublishError(f"网页版发布失败:{type(exc).__name__}: {exc}", retryable=True) from exc
        if not ok:
            # 页面上没看到成功提示,不代表平台没收下 —— 回查作品管理页确认真伪。
            # 确认已经发出去了就按成功算,**不要重试**:重试会在账号里多留一条一样的作品
            # (实跑踩过:一条片子在抖音上留下 4 条)。
            work = resolve_published_work(req.title or "", since=started_at, rounds=2, gap=4)
            if work.get("aweme_id"):
                return PublishResult(
                    ok=True, state="submitted", remote_id=work["aweme_id"],
                    remote_url=work.get("url") or "",
                    message=f"网页版已发布(页面没给成功提示,回查确认),作品 id {work['aweme_id']}",
                    raw=work)
            raise PublishError("网页版发布后没找到成功提示,可能是页面改版或需要验证码",
                               retryable=True)
        message = "网页版已提交"
        # 网页版这条路拿不到作品 id(抖音只在作品管理页里显示它)—— 回查一次补上,
        # 否则项目里的发布记录永远是一条"没有链接"的任务,后面抓数据/对账都无从下手。
        work = resolve_published_work(req.title or "", since=started_at)
        if work.get("aweme_id"):
            message = f"网页版已发布,作品 id {work['aweme_id']}"
        for note in (desc_note, cover_note, declare_note):
            if note:
                message += f";{note}"
        if work.get("aweme_id"):
            return PublishResult(ok=True, state="submitted", remote_id=work["aweme_id"],
                                 remote_url=work.get("url") or "", message=message,
                                 raw=work)
        return PublishResult(ok=True, state="submitted", remote_id="",
                             remote_url=CREATOR_UPLOAD, message=message)

    # -- 数据 ------------------------------------------------------------
    def fetch_metrics(self, ref: PlatformRef, account: Account) -> MetricSnapshot:
        if not self._official_ready(account):
            raise PublishError(
                "没有开放平台凭据,抓不了作品数据(需要 video.data 能力);"
                "网页版发的作品由 AI 团队分析师从创作者中心抓",
                retryable=False, credential=True)
        token = self._access_token(account)
        open_id = self._open_id(account)
        item_id = ref.remote_id
        if not item_id:
            raise PublishError("没有 item_id,无法抓数据", retryable=False)
        url = f"{OPEN}/api/douyin/v1/video/video_data/?open_id={open_id}"
        with new_client() as client:
            data = request_json(client, "POST", url, json={"item_ids": [item_id]},
                                headers={"access-token": token, "content-type": "application/json"},
                                timeout=60)
        extra = data.get("extra") or {}
        if extra.get("error_code") not in (0, None):
            raise PublishError(f"数据接口返回 error_code={extra.get('error_code')}", retryable=True)
        from ...metrics.collector import parse_douyin_statistics

        return MetricSnapshot(**parse_douyin_statistics(data), raw={"data": data.get("data")})


def fetch_creator_works(*, since: float | None = None, max_scrolls: int = 4) -> list[dict[str, Any]]:
    """无头打开创作者中心「作品管理」,截页面自己请求的作品列表接口,只读、不点按钮。

    返回 `work_list` 的 `items[]`(带 `metrics`),每条再挂上同一次返回里
    `aweme_list` 对应那条(`work["aweme"]`:标题、审核状态、作者粉丝数)。
    第一页 12 条;给了 `since`(时间戳)且最老一条还比它新,就往下滚着加载,最多
    `max_scrolls` 次。登录失效抛 `PublishError(credential=True)`,不重试。
    """
    from .. import browser

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
    from .. import browser

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
    from .. import browser

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
    from .. import browser

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
    from .. import browser

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
