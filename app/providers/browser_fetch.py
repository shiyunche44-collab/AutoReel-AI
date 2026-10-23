"""抓取兜底:直连被拒时,用真浏览器再抓一次。

**什么时候用**:`webassets._http_bytes`、`car_specs._get_json` 直连拿到 403 / 412 /
429 这类「认出是脚本」的状态码时,自动走这里。浏览器带着真实的 TLS 指纹、JS 执行、
cookie,很多只拦脚本的站就放行了;JS 渲染的空壳页这里拿到的也是渲染后的 DOM。

**浏览器从哪来**:复用 `publish/browser.py` 的 `BrowserSession` —— 本机 Chrome / Edge,
不用 `playwright install`。登录态单独一份(key = ``fetch``),和发布账号互不影响。

**验证码 / 登录墙**:检测到就抛 `BrowserBlocked`,把站点记到
``data/_browser/blocked.json``,**不点、不解、不绕过**。人工跑
``python scripts/browser_unblock.py``(或调 ``POST /api/browser/unblock``)打开有头
浏览器过一次,关窗后登录态存下来,下次带着它跑。被记下的站点在人工处理前不再
用浏览器重试 —— 反复撞验证码只会让风控更紧。

**失败**:任何问题都抛 `BrowserFetchError` 的子类,调用方当作「兜底也失败了」,
沿用原来的 fail-soft 路径。
"""
from __future__ import annotations

import hashlib
import json
import re
import threading
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from .. import config

#: 直连拿到这些状态码才值得用浏览器再试:都是「认出你是脚本」的典型回应。
#: 404 / 500 这种换浏览器也一样,不试。
FALLBACK_STATUS = frozenset({403, 412, 418, 429, 503, 521})

#: 同一 host 两次浏览器请求的最小间隔(秒)
_MIN_INTERVAL = 2.0
#: 风控严的站单独放慢(后缀匹配):易车一分钟内连开几页就提示「操作过于频繁」
_HOST_INTERVAL = {"yiche.com": 8.0}
#: 浏览器抓到的结果缓存多久
_CACHE_TTL = 24 * 3600
#: 同一 host 浏览器连续失败(非验证码)几次后,本进程内不再试
_MAX_FAILS = 2

#: 验证码 / 风控页的特征。只收「出现了就基本肯定是拦截页」的标记 ——
#: 单独一个「验证码」三个字不算,很多正常页面的登录框里都有。
_BLOCK_MARKERS = (
    "geetest", "nc_1_wrapper", "nocaptcha", "captcha-container", "tcaptcha",
    "cf-challenge", "challenge-platform", "cf-turnstile", "g-recaptcha", "h-captcha",
    "anomaly-modal",
    "安全验证", "访问验证", "人机验证", "滑动验证", "请完成验证", "请按住滑块",
    "访问过于频繁", "请求过于频繁", "操作过于频繁", "异常访问",
)
_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)
_TITLE_MARKERS = ("验证", "captcha", "just a moment", "attention required", "access denied", "登录")


class BrowserFetchError(RuntimeError):
    pass


class BrowserUnavailable(BrowserFetchError):
    """没装 Playwright / 找不到浏览器 / 兜底被关掉。"""


class BrowserBlocked(BrowserFetchError):
    """撞上验证码或登录墙,等人工处理。"""


_lock = threading.Lock()
_last_hit: dict[str, float] = {}
_fails: dict[str, int] = {}


# -- 纯函数 ------------------------------------------------------------------
def host_of(url: str) -> str:
    return (urlsplit(url).hostname or "").lower()


def host_allowed(host: str, allow: list[str] | None = None) -> bool:
    """`BROWSER_FETCH_HOSTS` 按后缀匹配;为空表示全部放行。"""
    allow = config.BROWSER_FETCH_HOSTS if allow is None else allow
    if not allow:
        return True
    return any(host == a or host.endswith("." + a) for a in allow)


def looks_blocked(html: str) -> str:
    """是不是验证码 / 风控 / 登录墙页面。是就返回原因,不是返回空串。纯函数,便于测。"""
    if not html:
        return ""
    low = html.lower()
    for marker in _BLOCK_MARKERS:
        if marker.lower() in low:
            return f"页面含「{marker}」"
    m = _TITLE_RE.search(html)
    if m:
        title = m.group(1).strip().lower()
        for marker in _TITLE_MARKERS:
            if marker in title:
                return f"标题「{title[:30]}」"
    return ""


def should_fallback(url: str, status: int | None) -> bool:
    """直连结果是否值得用浏览器再试一次。"""
    if not config.BROWSER_FETCH:
        return False
    if status is not None and status not in FALLBACK_STATUS:
        return False
    host = host_of(url)
    return bool(host) and host_allowed(host)


# -- 被拦站点记录 --------------------------------------------------------------
def _blocked_path() -> Path:
    return config.DATA_DIR / "_browser" / "blocked.json"


def blocked_list() -> list[dict[str, Any]]:
    try:
        data = json.loads(_blocked_path().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    return data if isinstance(data, list) else []


def _write_blocked(items: list[dict[str, Any]]) -> None:
    path = _blocked_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        pass


def mark_blocked(url: str, reason: str) -> None:
    """记下被拦的站点;同一 host 只留最新一条。"""
    host = host_of(url)
    items = [b for b in blocked_list() if b.get("host") != host]
    items.append({"host": host, "url": url, "reason": reason, "ts": time.time()})
    _write_blocked(items)


def clear_blocked(host: str) -> None:
    host = host.lower()
    _write_blocked([b for b in blocked_list() if b.get("host") != host])
    _fails.pop(host, None)


def is_blocked(host: str) -> bool:
    return any(b.get("host") == host for b in blocked_list())


# -- 缓存 --------------------------------------------------------------------
def _cache_path(url: str) -> Path:
    digest = hashlib.sha1(url.encode("utf-8")).hexdigest()
    return config.DATA_DIR / "_cache" / "browser_fetch" / f"{digest}.bin"


def _cache_read(url: str) -> bytes | None:
    path = _cache_path(url)
    try:
        if time.time() - path.stat().st_mtime > _CACHE_TTL:
            return None
        return path.read_bytes()
    except OSError:
        return None


def _cache_write(url: str, body: bytes) -> None:
    if not body:  # 空结果不缓存,免得一次失败卡 24 小时
        return
    path = _cache_path(url)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(body)
    except OSError:
        pass


# -- 抓取 --------------------------------------------------------------------
def _interval(host: str) -> float:
    return max((v for k, v in _HOST_INTERVAL.items() if host == k or host.endswith("." + k)),
               default=_MIN_INTERVAL)


def _throttle(host: str) -> None:
    wait = _last_hit.get(host, 0.0) + _interval(host) - time.time()
    if wait > 0:
        time.sleep(wait)
    _last_hit[host] = time.time()


def _browser_get(url: str, *, referer: str | None, timeout: int) -> bytes:
    """开一次浏览器抓 `url`。HTML 返回渲染后的 DOM,其余(JSON、图片…)返回原始响应体。"""
    from ..publish import browser as publish_browser

    try:
        session = publish_browser.BrowserSession(
            "fetch", headless=config.BROWSER_FETCH_HEADLESS).__enter__()
    except publish_browser.BrowserUnavailable as exc:
        raise BrowserUnavailable(str(exc)) from exc
    try:
        page = session.page
        try:
            resp = page.goto(url, referer=referer, wait_until="domcontentloaded",
                             timeout=timeout * 1000)
        except Exception as exc:  # noqa: BLE001
            raise BrowserFetchError(f"打开失败:{type(exc).__name__}: {exc}") from exc
        if resp is None:
            raise BrowserFetchError("没有响应")
        ctype = (resp.headers.get("content-type") or "").lower()
        if "html" in ctype:
            try:  # 等前端把数据渲染出来;等不到也照样拿当前 DOM
                page.wait_for_load_state("networkidle", timeout=8000)
            except Exception:  # noqa: BLE001
                pass
            html = page.content()
            reason = looks_blocked(html)
            if reason:
                raise BrowserBlocked(reason)
            if resp.status >= 400:
                raise BrowserFetchError(f"浏览器也拿到 HTTP {resp.status}")
            return html.encode("utf-8")
        if resp.status >= 400:
            raise BrowserFetchError(f"浏览器也拿到 HTTP {resp.status}")
        return resp.body()
    finally:
        session.__exit__(None, None, None)


def fetch(url: str, *, referer: str | None = None, as_text: bool = False,
          timeout: int = 30) -> bytes | str:
    """用浏览器抓 `url`。`as_text` 时 HTML 按 UTF-8(渲染后的 DOM),其余按 UTF-8 尽力解。

    抛 `BrowserUnavailable` / `BrowserBlocked` / `BrowserFetchError`。
    """
    if not config.BROWSER_FETCH:
        raise BrowserUnavailable("BROWSER_FETCH=0,浏览器兜底已关闭")
    host = host_of(url)
    if is_blocked(host):
        raise BrowserBlocked(f"{host} 在等人工过验证:python scripts/browser_unblock.py")
    if _fails.get(host, 0) >= _MAX_FAILS:
        raise BrowserFetchError(f"{host} 浏览器兜底已连续失败 {_MAX_FAILS} 次,本次运行不再试")

    body = _cache_read(url)
    if body is None:
        with _lock:  # sync Playwright 不能跨线程共用,一次只开一个浏览器
            _throttle(host)
            try:
                body = _browser_get(url, referer=referer, timeout=timeout)
            except BrowserBlocked as exc:
                mark_blocked(url, str(exc))
                raise
            except BrowserUnavailable:
                raise
            except BrowserFetchError:
                _fails[host] = _fails.get(host, 0) + 1
                raise
        _fails.pop(host, None)
        _cache_write(url, body)
    if as_text:
        return body.decode("utf-8", errors="replace")
    return body


def capture_json(page_url: str, url_pattern: str, *, timeout: int = 30) -> list[Any]:
    """打开页面,截获页面自己发出的、URL 匹配 `url_pattern`(正则)的 JSON 响应。

    给「页面是前端渲染、数据走内部接口、接口本身带签名」的站用:不去逆向签名,
    让页面自己发请求,我们只听结果。
    """
    from ..publish import browser as publish_browser

    if not config.BROWSER_FETCH:
        raise BrowserUnavailable("BROWSER_FETCH=0,浏览器兜底已关闭")
    host = host_of(page_url)
    if is_blocked(host):
        raise BrowserBlocked(f"{host} 在等人工过验证:python scripts/browser_unblock.py")
    pattern = re.compile(url_pattern)
    hits: list[Any] = []

    def _on_response(resp: Any) -> None:
        if not pattern.search(resp.url):
            return
        try:
            hits.append(resp.json())
        except Exception:  # noqa: BLE001 - 不是 JSON 就不要
            pass

    with _lock:
        _throttle(host)
        try:
            session = publish_browser.BrowserSession(
                "fetch", headless=config.BROWSER_FETCH_HEADLESS).__enter__()
        except publish_browser.BrowserUnavailable as exc:
            raise BrowserUnavailable(str(exc)) from exc
        try:
            page = session.page
            page.on("response", _on_response)
            try:
                page.goto(page_url, wait_until="domcontentloaded", timeout=timeout * 1000)
                page.wait_for_load_state("networkidle", timeout=10000)
            except Exception:  # noqa: BLE001 - 超时也看看已经截到了什么
                pass
            reason = looks_blocked(page.content())
            if reason and not hits:
                mark_blocked(page_url, reason)
                raise BrowserBlocked(reason)
        finally:
            session.__exit__(None, None, None)
    return hits


def unblock(url: str, *, timeout: int = 300) -> Path:
    """打开有头浏览器让人手动过验证码 / 登录,关窗后保存登录态并清掉这个站的拦截记录。"""
    from ..publish import browser as publish_browser

    path = publish_browser.login("fetch", url, headless=False, timeout=timeout)
    clear_blocked(host_of(url))
    return path
