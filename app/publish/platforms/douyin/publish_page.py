"""发布页的其余操作:等上传 / 发布、简介、自主声明、点发布。"""
from __future__ import annotations

import time
from typing import Any

from .consts import DESC_SELECTORS, PUBLISH_DONE_TEXTS
from .cover import dismiss_cover_prompt


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
    from .... import config

    return str(getattr(config, key, "") or "")


def _declare_enabled(req: Any) -> bool:
    """这条要不要挂「内容由AI生成」标识。

    默认按配置来(本项目出的片子全是 AI 生成);单次请求可以用
    `extra={"declare_ai": False}` 关掉 —— 比如发一条真人拍的素材。"""
    from .... import config

    raw = (req.extra or {}).get("declare_ai")
    if raw is not None:
        return bool(raw)
    return bool(getattr(config, "DOUYIN_DECLARE_AI", True))
