"""发布页的封面位:各传哪张图、遮挡检测、封面弹窗。"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from .consts import COVER_INPUT, COVER_SLOTS


def cover_files(cover: Path | None) -> list[tuple[str, str, Path]]:
    """网页版两个封面位各传哪张图:[(位上的提示字, 弹窗步骤名, 图)]。

    优先用按 4:3 / 3:4 **重新排版**的那张(`cover_art.build` 出的);没有就退回主封面,
    但只在主封面横竖和这个位一致时 —— 竖封面塞进 4:3 位,平台一裁就只剩中间一条。
    两张都没有的位跳过,平台自己从视频里截帧。"""
    from ....pipeline.narration import cover_3x4_path, cover_4x3_path

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
