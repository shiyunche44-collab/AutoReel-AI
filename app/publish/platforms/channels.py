"""视频号适配器:视频号助手 + 浏览器自动化。

**视频号没有对外的发布 API** —— 它深度绑在微信生态里,开放平台只放小程序和
公众号。所以只能驱动视频号助手(channels.weixin.qq.com)的发布页。

和小红书一样的两条纪律:
- 选择器写成候选列表,全不行就明确报错;
- 抓数据用「页面文字 + 标签匹配」,不绑 DOM 结构。

首次使用需要在分发台点「登录」扫码,登录态存在 `data/_browser/channels.json`。
"""
from __future__ import annotations

from pathlib import Path

from ..base import Account, AccountStatus, MetricSnapshot, PlatformRef, PublishError, \
    PublishRequest, PublishResult
from ..registry import PORTRAIT

PUBLISH_URL = "https://channels.weixin.qq.com/platform/post/create"
DATA_URL = "https://channels.weixin.qq.com/platform/data/post"
LOGIN_URL = "https://channels.weixin.qq.com/login"

_FILE_INPUTS = ("input[type=file]", "input[type='file']")
_TITLE_INPUTS = ("input[placeholder*='标题']", "input[name='title']",
                 ".weui-desktop-form__input")
_CONTENT_INPUTS = ("textarea[placeholder*='描述']", "textarea[placeholder*='简介']", "textarea")
_PUBLISH_BUTTONS = ("button:has-text('发表')", "button:has-text('发布')",
                    "text=发表", "text=发布")


class ChannelsAdapter:
    """视频号(视频号助手,浏览器自动化)。"""

    key = "channels"
    label = "视频号"
    preferred_aspect = PORTRAIT
    auth_kind = "browser"

    # -- 自检 ------------------------------------------------------------
    def check_account(self, account: Account) -> AccountStatus:
        from .. import browser

        if not browser.state_exists(self.key):
            return AccountStatus(False, "还没有登录态",
                                 hint="在分发台点「登录」,浏览器打开后用微信扫码,关掉窗口即可。")
        try:
            with browser.BrowserSession(self.key) as session:
                session.goto(DATA_URL, wait_ms=2500)
                text = session.page_text()
        except browser.BrowserUnavailable as exc:
            return AccountStatus(False, str(exc), hint="pip install playwright")
        except Exception as exc:  # noqa: BLE001
            return AccountStatus(False, f"打不开视频号助手:{exc}", hint="登录态可能过期了,重新登录")
        if "扫码登录" in text[:400] or "微信登录" in text[:400]:
            return AccountStatus(False, "登录态已失效", hint="重新用微信扫码登录一次")
        return AccountStatus(True, "已登录", detail={"chars": len(text)})

    # -- 发布 ------------------------------------------------------------
    def publish(self, req: PublishRequest, account: Account) -> PublishResult:
        from .. import browser

        video = Path(req.video_path)
        if not video.is_file():
            raise PublishError(f"成片不存在:{video.name}", retryable=False)
        try:
            with browser.BrowserSession(self.key) as session:
                session.goto(PUBLISH_URL, wait_ms=3500)
                if not session.exists(_FILE_INPUTS[0], timeout=6000):
                    raise PublishError("没找到上传入口,视频号助手可能改版了", retryable=False)
                session.upload_file(_FILE_INPUTS[0], video)
                session.page.wait_for_timeout(12000)
                self._fill(session, _TITLE_INPUTS, (req.title or "")[:22])
                self._fill(session, _CONTENT_INPUTS, (req.description or "")[:1000])
                clicked = self._click_first(session, _PUBLISH_BUTTONS)
                if not clicked:
                    raise PublishError("没找到发表按钮,页面可能改版了", retryable=False)
                session.page.wait_for_timeout(8000)
                session.save_state()
                body = session.page_text()
        except browser.BrowserUnavailable as exc:
            raise PublishError(str(exc), retryable=False) from exc
        except PublishError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise PublishError(f"视频号发布失败:{type(exc).__name__}: {exc}", retryable=True) from exc
        if "发表成功" not in body and "发布成功" not in body:
            raise PublishError("发表后没看到成功提示,可能需要人工确认一次", retryable=True)
        return PublishResult(ok=True, state="submitted", remote_id="",
                             remote_url=PUBLISH_URL, message="已发表(未拿到作品 id)")

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
