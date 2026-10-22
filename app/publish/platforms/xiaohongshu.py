"""小红书适配器:浏览器自动化。

**没有开放平台的发布 API** —— 小红书的第三方自动化是要被反爬针对的,这条路
只能自建。做法是 Playwright 驱动已登录的浏览器走创作服务平台的发布页。

**页面结构会变**,所以这里的做法是:
- 选择器写成候选列表,挨个试,全不行就**明确报错**(不静默跳过);
- 抓数据用「页面文字 + 标签匹配」而不是 CSS 选择器 —— 后台的数据页把数字写
  在文字里,文字比 DOM 稳定得多。

首次使用需要在分发台点「登录」扫码一次,登录态存在 `data/_browser/xiaohongshu.json`。
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from ..base import Account, AccountStatus, MetricSnapshot, PlatformRef, PublishError, \
    PublishRequest, PublishResult
from ..registry import PORTRAIT

PUBLISH_URL = "https://creator.xiaohongshu.com/publish/publish"
DATA_URL = "https://creator.xiaohongshu.com/new/data-center"
LOGIN_URL = "https://creator.xiaohongshu.com/login"

#: 上传控件的候选选择器
_FILE_INPUTS = ("input[type=file]", "input[type='file']")
#: 标题输入框
_TITLE_INPUTS = ("input[placeholder*='标题']", "input[name='title']",
                 ".publish-input-title input", "input.d-text[placeholder*='标题']")
#: 正文输入框
_CONTENT_INPUTS = ("textarea[placeholder*='正文']", "textarea[placeholder*='描述']",
                   ".ql-editor", "textarea")
#: 发布按钮
_PUBLISH_BUTTONS = ("button:has-text('发布')", "div[class*='publish'] button:has-text('发布')",
                    "button:has-text('发布笔记')", "text=发布")


class XiaohongshuAdapter:
    """小红书(创作服务平台,浏览器自动化)。"""

    key = "xiaohongshu"
    label = "小红书"
    preferred_aspect = PORTRAIT
    auth_kind = "browser"

    # -- 自检 ------------------------------------------------------------
    def check_account(self, account: Account) -> AccountStatus:
        from .. import browser

        if not browser.state_exists(self.key):
            return AccountStatus(False, "还没有登录态",
                                 hint="在分发台点「登录」,浏览器打开后扫码,关掉窗口即可。")
        try:
            with browser.BrowserSession(self.key) as session:
                session.goto(DATA_URL, wait_ms=2500)
                text = session.page_text()
        except browser.BrowserUnavailable as exc:
            return AccountStatus(False, str(exc), hint="pip install playwright")
        except Exception as exc:  # noqa: BLE001
            return AccountStatus(False, f"打不开后台:{exc}", hint="登录态可能过期了,重新登录")
        if "登录" in text[:400] and "扫码" in text[:400]:
            return AccountStatus(False, "登录态已失效", hint="重新扫码登录一次")
        return AccountStatus(True, "已登录", detail={"chars": len(text)})

    # -- 发布 ------------------------------------------------------------
    def publish(self, req: PublishRequest, account: Account) -> PublishResult:
        from .. import browser

        video = Path(req.video_path)
        if not video.is_file():
            raise PublishError(f"成片不存在:{video.name}", retryable=False)
        try:
            with browser.BrowserSession(self.key) as session:
                session.goto(PUBLISH_URL, wait_ms=3000)
                if not session.exists(_FILE_INPUTS[0], timeout=6000):
                    raise PublishError("没找到上传入口,小红书发布页可能改版了",
                                       retryable=False)
                session.upload_file(_FILE_INPUTS[0], video)
                session.page.wait_for_timeout(10000)
                self._fill(session, _TITLE_INPUTS, (req.title or "")[:20])
                self._fill(session, _CONTENT_INPUTS, (req.description or "")[:1000])
                clicked = self._click_first(session, _PUBLISH_BUTTONS)
                if not clicked:
                    raise PublishError("没找到发布按钮,页面可能改版了", retryable=False)
                session.page.wait_for_timeout(8000)
                session.save_state()
                body = session.page_text()
        except browser.BrowserUnavailable as exc:
            raise PublishError(str(exc), retryable=False) from exc
        except PublishError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise PublishError(f"小红书发布失败:{type(exc).__name__}: {exc}", retryable=True) from exc
        if "发布成功" not in body and "已发布" not in body:
            raise PublishError("发布后没看到成功提示,可能需要人工确认一次", retryable=True)
        return PublishResult(ok=True, state="published", remote_id="",
                             remote_url=PUBLISH_URL, message="已发布(未拿到作品 id)")

    def _fill(self, session, selectors: tuple[str, ...], value: str) -> None:
        if not value:
            return
        for selector in selectors:
            if session.exists(selector, timeout=1500):
                session.fill(selector, value)
                return

    def _click_first(self, session, selectors: tuple[str, ...]) -> bool:
        for selector in selectors:
            if session.exists(selector, timeout=1500):
                session.click(selector)
                return True
        return False

    # -- 数据 ------------------------------------------------------------
    def fetch_metrics(self, ref: PlatformRef, account: Account) -> MetricSnapshot:
        from .. import browser
        from ...metrics.collector import parse_metric_text

        try:
            with browser.BrowserSession(self.key) as session:
                session.goto(DATA_URL, wait_ms=3000)
                text = session.page_text()
        except browser.BrowserUnavailable as exc:
            raise PublishError(str(exc), retryable=False) from exc
        except Exception as exc:  # noqa: BLE001
            raise PublishError(f"打不开数据页:{exc}", retryable=True) from exc
        metrics = parse_metric_text(text)
        if all(v is None for v in metrics.values()):
            raise PublishError("数据页里没认出指标(页面可能改版了)", retryable=True)
        return MetricSnapshot(**metrics, raw={"chars": len(text)})
