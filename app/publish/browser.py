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
import threading
import time
from pathlib import Path
from typing import Any, Sequence

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


#: 用固定浏览器配置目录的平台。抖音会把"换了设备"的会话踢掉:扫码用有头 Chrome、
#: 发布用无头浏览器 + 另一个 UA、IndexedDB 里的设备号每次丢,登录撑不过一小时。
#: 这些平台扫码和发布共用同一个配置目录、同一个本机 Chrome、同一个 UA,
#: `storage_state` 那份 JSON 只是镜像(给 `douyin_logged_in` 这类只看 cookie 的地方用)。
PROFILE_KEYS = frozenset({"douyin"})
#: 配置目录同一时间只能被一个浏览器打开:发布、扫码、分析师抓数据在进程内排队
_PROFILE_LOCKS: dict[str, threading.Lock] = {}
_PROFILE_LOCKS_GUARD = threading.Lock()


def _profile_lock(key: str) -> threading.Lock:
    with _PROFILE_LOCKS_GUARD:
        return _PROFILE_LOCKS.setdefault(key, threading.Lock())


def profile_dir(key: str) -> Path:
    """某个平台的固定浏览器配置目录。"""
    path = config.DATA_DIR / "_browser" / "profiles" / key
    path.mkdir(parents=True, exist_ok=True)
    return path


# --------------------------------------------------------------------------
# 假 IP 直连
# --------------------------------------------------------------------------
#: 本机如果挂着 Clash 这类代理(常见配置:fake-IP 模式),域名会被解析到
#: 198.18.x.x 的假 IP,再由代理接管转发。**浏览器连过去常常被直接关连接**
#: (ERR_CONNECTION_CLOSED),表现成「页面改版了」「找不到上传入口」这类
#: 让人摸不着头脑的错 —— 实测同一时刻普通 HTTP 客户端访问同一个站点是通的。
#: 抖音、B 站、小红书都是境内站点,本来也不需要代理:用 DoH 问出真实 IP,
#: 让浏览器直接连真实地址,就不经过那层假 IP 了。
DIRECT_IP_HOSTS: dict[str, tuple[str, ...]] = {
    "douyin": ("creator.douyin.com", "www.douyin.com"),
    "bilibili": ("www.bilibili.com", "member.bilibili.com"),
    "xiaohongshu": ("www.xiaohongshu.com", "creator.xiaohongshu.com"),
}

#: 问真实 IP 用的 DoH(境内优先)
_DOH_RESOLVERS = ("https://223.5.5.5/resolve", "https://1.12.12.12/resolve")

_ip_cache: dict[str, tuple[float, str]] = {}
_ip_cache_lock = threading.Lock()
_IP_TTL = 600.0


def _resolve_real_ip(host: str) -> str:
    """用 DoH 问一个域名的真实 A 记录。问不到返回空串。"""
    with _ip_cache_lock:
        hit = _ip_cache.get(host)
        if hit and time.time() - hit[0] < _IP_TTL:
            return hit[1]
    ip = ""
    try:
        import httpx

        for resolver in _DOH_RESOLVERS:
            try:
                resp = httpx.get(resolver, params={"name": host, "type": "A"}, timeout=8,
                                 headers={"accept": "application/dns-json"})
                answers = resp.json().get("Answer") or []
                ips = [a.get("data") for a in answers if a.get("type") == 1 and a.get("data")]
                if ips:
                    ip = str(ips[0])
                    break
            except Exception:  # noqa: BLE001 - 换下一个 DoH
                continue
    except Exception:  # noqa: BLE001 - httpx 不可用就算了,走原来的路
        return ""
    if ip:
        with _ip_cache_lock:
            _ip_cache[host] = (time.time(), ip)
    return ip


def real_ip_args(key: str) -> list[str]:
    """给浏览器加 --host-resolver-rules:把假 IP 换成真实 IP。问不到就不加。"""
    hosts = DIRECT_IP_HOSTS.get(key) or ()
    if not hosts:
        return []
    rules = []
    for host in hosts:
        ip = _resolve_real_ip(host)
        if ip:
            rules.append(f"MAP {host} {ip}")
    if not rules:
        return []
    return [f"--host-resolver-rules={', '.join(rules)}"]


def chrome_major(executable: str | Path | None) -> str:
    """本机 Chrome / Edge 的主版本号:安装目录旁边那个 `155.0.8059.86` 文件夹。找不到返回空串。"""
    if not executable:
        return ""
    versions = []
    try:
        for p in Path(executable).parent.iterdir():
            parts = p.name.split(".")
            if p.is_dir() and len(parts) == 4 and all(x.isdigit() for x in parts):
                versions.append(tuple(int(x) for x in parts))
    except OSError:
        return ""
    return str(max(versions)[0]) if versions else ""


def stable_user_agent(executable: str | Path | None) -> str:
    """有头、无头都报同一个 UA —— 就是有头 Chrome 自己报的那种(版本号只留主版本)。
    无头 Chrome 默认报 HeadlessChrome + 完整版本号,在平台眼里是另一台设备。"""
    major = chrome_major(executable)
    if not major:
        return ""
    return ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            f"(KHTML, like Gecko) Chrome/{major}.0.0.0 Safari/537.36")


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
                 storage: str | None = None, lock_timeout: float = 600) -> None:
        self.key = key
        self.headless = config.BROWSER_HEADLESS if headless is None else headless
        self.storage = storage
        #: 配置目录被别的会话占着时最多等多久(秒);抓数据这类不急的传小一点
        self.lock_timeout = lock_timeout
        self._lock: threading.Lock | None = None
        self._pw = None
        self._browser = None
        self._context = None
        self.page = None

    @property
    def uses_profile(self) -> bool:
        return self.key in PROFILE_KEYS and self.storage is None

    def is_alive(self) -> bool:
        """窗口还开着(登录窗口用来判断人是不是关窗了)。"""
        if self.page is None or self.page.is_closed():
            return False
        return self._browser.is_connected() if self._browser else bool(self._context.pages)

    # -- 生命周期 --------------------------------------------------------
    def __enter__(self) -> "BrowserSession":
        sync_playwright = _import_playwright()
        self._pw = sync_playwright().start()
        if self.uses_profile:
            return self._enter_profile()
        executable = config.BROWSER_PATH
        if not self.headless:
            executable = headed_browser(executable)
        elif not (executable and Path(executable).is_file()):
            # 配置的浏览器不在了(如 motion/.browser 被清掉):退回本机 Chrome / Edge,
            # 不然 playwright 去找它自己下载的那份,没装就直接起不来
            executable = headed_browser(None)
        launch: dict[str, Any] = {"headless": self.headless}
        if executable and Path(executable).is_file():
            launch["executable_path"] = executable
        extra_args = real_ip_args(self.key)
        if extra_args:
            launch["args"] = extra_args
        try:
            self._browser = self._pw.chromium.launch(**launch)
        except Exception as exc:  # noqa: BLE001
            self._pw.stop()
            self._pw = None
            raise BrowserUnavailable(
                f"启动浏览器失败:{exc}。在 .env 里用 BROWSER_PATH 指定 Chrome / Edge 的路径。"
            ) from exc
        state = self.storage or str(state_path(self.key))
        ctx_kwargs: dict[str, Any] = {"viewport": {"width": 1440, "height": 900},
                                      "locale": "zh-CN"}
        if self.headless:
            # 无头模式的 UA 里写着 HeadlessChrome,懂车帝这类站直接回空页;换成普通 Chrome 的
            ctx_kwargs["user_agent"] = (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                f"(KHTML, like Gecko) Chrome/{self._browser.version} Safari/537.36")
        if Path(state).is_file():
            ctx_kwargs["storage_state"] = state
        self._context = self._browser.new_context(**ctx_kwargs)
        self.page = self._context.new_page()
        return self

    def _enter_profile(self) -> "BrowserSession":
        """固定配置目录:扫码和发布永远是同一个本机 Chrome、同一份配置、同一个 UA。"""
        lock = _profile_lock(self.key)
        if not lock.acquire(timeout=self.lock_timeout):
            self._pw.stop()
            self._pw = None
            raise BrowserUnavailable("浏览器配置目录正被发布 / 扫码 / 抓数据占着,稍后再来")
        self._lock = lock
        executable = headed_browser(None) or config.BROWSER_PATH
        kwargs: dict[str, Any] = {"headless": self.headless,
                                  "viewport": {"width": 1440, "height": 900},
                                  "locale": "zh-CN"}
        if executable and Path(executable).is_file():
            kwargs["executable_path"] = executable
        ua = stable_user_agent(executable)
        if ua:
            kwargs["user_agent"] = ua
        extra_args = real_ip_args(self.key)
        if extra_args:
            kwargs["args"] = extra_args
        try:
            self._context = self._pw.chromium.launch_persistent_context(
                str(profile_dir(self.key)), **kwargs)
        except Exception as exc:  # noqa: BLE001
            self._pw.stop()
            self._pw = None
            self._lock = None
            lock.release()
            raise BrowserUnavailable(
                f"启动浏览器失败:{exc}。配置目录同一时间只能被一个浏览器用 ——"
                "扫码窗口还开着的话先关掉。") from exc
        self.page = self._context.pages[0] if self._context.pages else self._context.new_page()
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
            if self._lock:
                self._lock.release()
                self._lock = None

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
                if not session.is_alive():
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
