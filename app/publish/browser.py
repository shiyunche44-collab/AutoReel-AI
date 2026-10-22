"""浏览器自动化(Playwright)。

**为什么自己驱动浏览器而不是调 API**:小红书、视频号都没有开放平台的发布 API,
抖音的网页版也带签名。浏览器自动化是这几个平台唯一不用等资质审核的路子。

**为什么不装一份自己的浏览器**:本机已经有 Chrome / Edge(`config.MOTION_BROWSER`
在找渲染概念动画的浏览器时就解析过了),直接复用。只需要 `pip install playwright`,
不必 `playwright install` 再下一百多兆。

**登录态怎么保持**:Playwright 的 `storage_state` 存成一个 JSON 文件(放 `data/`
下面,已被 .gitignore 覆盖)。第一次用「登录」按钮打开有头浏览器扫码,之后
就带着这份 state 跑无头模式。

**Playwright 没装时**:所有入口都抛 `BrowserUnavailable`,提示装依赖 ——
绝不静默跳过,否则用户会以为"发布成功了但其实没发"。
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from .. import config
from ..publish.base import PublishError


class BrowserUnavailable(RuntimeError):
    """Playwright 没装,或者找不到可用的浏览器。"""


def _import_playwright():
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:  # pragma: no cover - 取决于环境
        raise BrowserUnavailable(
            "没装 Playwright。跑 `pip install -r requirements.txt` 即可 —— "
            "不需要 `playwright install`,浏览器复用本机 Chrome / Edge。") from exc
    return sync_playwright


#: 本机常见的有界面浏览器
_HEADED_CANDIDATES = (
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/usr/bin/google-chrome",
)


def headed_browser(executable: str | None) -> str | None:
    """要弹窗让人登录时用的浏览器。

    渲染概念动画用的 chrome-headless-shell 没有界面,拿它开"有头"窗口什么也看不到;
    这种情况换成本机装的 Chrome / Edge。"""
    if executable and "headless" not in Path(executable).name.lower():
        return executable
    for cand in _HEADED_CANDIDATES:
        if Path(cand).is_file():
            return cand
    return executable


def state_path(key: str) -> Path:
    """某个平台的登录态文件。"""
    path = config.DATA_DIR / "_browser" / f"{key}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


class BrowserSession:
    """一次浏览器会话。

    用法:

    ```python
    with BrowserSession("xiaohongshu") as page:
        page.goto("https://creator.xiaohongshu.com/publish/publish")
        page.upload_file("input[type=file]", video)
    ```
    """

    def __init__(self, key: str, *, headless: bool | None = None,
                 storage: str | None = None) -> None:
        self.key = key
        self.headless = config.BROWSER_HEADLESS if headless is None else headless
        self.storage = storage
        self._pw = None
        self._browser = None
        self._context = None
        self.page = None

    # -- 生命周期 --------------------------------------------------------
    def __enter__(self) -> "BrowserSession":
        sync_playwright = _import_playwright()
        self._pw = sync_playwright().start()
        executable = config.BROWSER_PATH
        if not self.headless:
            executable = headed_browser(executable)
        launch: dict[str, Any] = {"headless": self.headless}
        if executable and Path(executable).is_file():
            launch["executable_path"] = executable
        try:
            self._browser = self._pw.chromium.launch(**launch)
        except Exception as exc:  # noqa: BLE001
            self._pw.stop()
            self._pw = None
            raise BrowserUnavailable(
                f"启动浏览器失败:{exc}。在 .env 里用 BROWSER_PATH 指定 Chrome / Edge 的路径。"
            ) from exc
        state = self.storage or str(state_path(self.key))
        ctx_kwargs: dict[str, Any] = {"viewport": {"width": 1440, "height": 900}}
        if Path(state).is_file():
            ctx_kwargs["storage_state"] = state
        self._context = self._browser.new_context(**ctx_kwargs)
        self.page = self._context.new_page()
        return self

    def __exit__(self, *exc: Any) -> None:
        try:
            if self._context:
                self._context.close()
            if self._browser:
                self._browser.close()
        finally:
            if self._pw:
                self._pw.stop()
            self._pw = None
            self._browser = None
            self._context = None
            self.page = None

    # -- 操作 ------------------------------------------------------------
    def goto(self, url: str, *, wait_ms: int = 2500) -> None:
        self.page.goto(url, wait_until="domcontentloaded", timeout=60000)
        if wait_ms:
            self.page.wait_for_timeout(wait_ms)

    def exists(self, selector: str, *, timeout: int = 4000) -> bool:
        try:
            self.page.wait_for_selector(selector, timeout=timeout, state="attached")
            return True
        except Exception:  # noqa: BLE001 - 找不到就是 False
            return False

    def text(self, selector: str, *, timeout: int = 4000) -> str:
        el = self.page.wait_for_selector(selector, timeout=timeout)
        return (el.inner_text() or "").strip()

    def fill(self, selector: str, value: str, *, timeout: int = 8000) -> None:
        el = self.page.wait_for_selector(selector, timeout=timeout)
        el.fill(value)

    def click(self, selector: str, *, timeout: int = 8000) -> None:
        el = self.page.wait_for_selector(selector, timeout=timeout)
        el.click()

    def upload_file(self, selector: str, path: str | Path, *, timeout: int = 20000) -> None:
        el = self.page.wait_for_selector(selector, timeout=timeout, state="attached")
        el.set_input_files(str(path))

    def wait_text(self, text: str, *, timeout: int = 20000) -> bool:
        try:
            self.page.wait_for_function(
                "(t) => document.body && document.body.innerText.includes(t)",
                arg=text, timeout=timeout)
            return True
        except Exception:  # noqa: BLE001
            return False

    def save_state(self) -> Path:
        """把当前登录态存下来,下次直接用。"""
        path = state_path(self.key)
        path.parent.mkdir(parents=True, exist_ok=True)
        self._context.storage_state(path=str(path))
        return path

    def page_text(self) -> str:
        try:
            return self.page.inner_text("body") or ""
        except Exception:  # noqa: BLE001
            return ""


#: 登录窗口开着时,每隔多少秒存一次登录态
LOGIN_SAVE_EVERY = 5.0


def login(key: str, url: str, *, headless: bool = False, timeout: int = 300,
          done_cookie: str = "") -> Path:
    """打开有头浏览器让人扫码登录,存下登录态。

    用于小红书 / 视频号这类只能扫码的平台。
    **用户关掉浏览器窗口就立刻结束**,不用干等满 5 分钟。
    登录态每隔几秒存一次:用户登完忘了关窗,登录态也已经落盘。
    给了 `done_cookie`(登录后才有的 cookie 名)时,一出现就存好并自动关窗。
    """
    path = state_path(key)
    with BrowserSession(key, headless=headless) as session:
        session.goto(url, wait_ms=1500)
        deadline = time.time() + timeout
        last_save = 0.0
        while time.time() < deadline:
            try:
                # 轻量探活:窗口被关掉时页面会变成 closed,或者这里直接抛
                if session.page.is_closed() or not session._browser.is_connected():
                    break
                session.page.title()
                if done_cookie and any(c.get("name") == done_cookie and c.get("value")
                                       for c in session._context.cookies()):
                    session.page.wait_for_timeout(2000)  # 让登录后的其他 cookie 也写完
                    return session.save_state()
                if time.time() - last_save >= LOGIN_SAVE_EVERY:
                    session.save_state()
                    last_save = time.time()
            except Exception:  # noqa: BLE001 - 窗口关了就是登完了
                break
            time.sleep(1)
        try:
            return session.save_state()
        except Exception:  # noqa: BLE001 - 窗口已关,用最后一次存下的
            return path


def state_exists(key: str) -> bool:
    return state_path(key).is_file()


def read_state(key: str) -> dict[str, Any]:
    path = state_path(key)
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}
