"""发布域的 HTTP 工具。

**为什么不用 `providers/webassets`**:那个模块是"抓公开素材"用的,返回文本、
吞异常、缓存 24 小时。发布要的是**不吞异常**、带 Cookie、带文件流、能重试。
两边混用会让"上传失败"变成"上传成功但返回空",比报错更难查。

约定:
- 4xx(除 429)→ `PublishError(retryable=False)`,重试没意义;
- 429 / 5xx / 网络错 → `PublishError(retryable=True)`;
- **凭据类错误**(B站 code=-101、抖音 28001008)→ `credential=True`,UI 会给账号告警。
"""
from __future__ import annotations

import json
import time
from typing import Any

import httpx

from .base import PublishError

#: 平台接口都认浏览器 UA
BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0 Safari/537.36"
)

DEFAULT_TIMEOUT = 60
#: 上传单片可能很久
UPLOAD_TIMEOUT = 900


def parse_cookie(raw: str) -> dict[str, str]:
    """把 `a=1; b=2` 形式的 Cookie 串解析成字典。"""
    out: dict[str, str] = {}
    for part in str(raw or "").split(";"):
        if "=" not in part:
            continue
        key, _, value = part.partition("=")
        key = key.strip()
        if key:
            out[key] = value.strip()
    return out


def new_client(cookie: str = "", *, timeout: int = DEFAULT_TIMEOUT,
               headers: dict[str, str] | None = None) -> httpx.Client:
    """带 Cookie 的客户端。"""
    head = {"User-Agent": BROWSER_UA, "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.6"}
    if headers:
        head.update(headers)
    cookies = parse_cookie(cookie)
    return httpx.Client(headers=head, cookies=cookies, timeout=timeout, follow_redirects=True)


def request_json(client: httpx.Client, method: str, url: str, *,
                  timeout: int = DEFAULT_TIMEOUT, **kwargs: Any) -> dict[str, Any]:
    """发一个请求并解析 JSON。失败抛 `PublishError`。"""
    try:
        resp = client.request(method, url, timeout=timeout, **kwargs)
    except httpx.TimeoutException as exc:
        raise PublishError(f"请求超时({timeout}s):{url}") from exc
    except httpx.HTTPError as exc:
        raise PublishError(f"网络错误:{type(exc).__name__}: {exc}") from exc
    if resp.status_code == 429:
        raise PublishError("被平台限流(429),稍后重试", retryable=True)
    if resp.status_code >= 500:
        raise PublishError(f"平台返回 {resp.status_code},稍后重试", retryable=True)
    if resp.status_code >= 400:
        raise PublishError(f"平台返回 {resp.status_code}:{resp.text[:200]}",
                           retryable=False)
    try:
        data = resp.json()
    except (json.JSONDecodeError, ValueError) as exc:
        raise PublishError(f"平台返回的不是 JSON:{resp.text[:200]}", retryable=True) from exc
    if not isinstance(data, dict):
        return {"data": data}
    return data


def check_platform_code(data: dict[str, Any], *, credential_codes: tuple[int, ...] = (-101,)) -> None:
    """平台统一用 `code` 表示成功(0)。非 0 → 抛错。"""
    code = data.get("code")
    if code is None:
        return
    try:
        code_int = int(code)
    except (TypeError, ValueError):
        return
    if code_int == 0:
        return
    message = str(data.get("message") or data.get("msg") or "")
    if code_int in credential_codes:
        raise PublishError(f"登录态失效(code={code_int}):{message}", retryable=False, credential=True)
    raise PublishError(f"平台返回 code={code_int}:{message}", retryable=False)


def find_key(payload: Any, *keys: str) -> Any:
    """在嵌套结构里递归找第一个命中的键。

    平台的返回结构会变(同一份数据有时多包一层 `data`),写死路径迟早对不上。
    找不到返回 None。
    """
    if isinstance(payload, dict):
        for key in keys:
            if key in payload and payload[key] not in (None, ""):
                return payload[key]
        for value in payload.values():
            got = find_key(value, *keys)
            if got is not None:
                return got
    elif isinstance(payload, list):
        for item in payload:
            got = find_key(item, *keys)
            if got is not None:
                return got
    return None


def sleep_backoff(attempt_no: int, base: float = 1.0) -> None:
    """指数退避:1 → 4 → 16。"""
    time.sleep(base * (4 ** max(0, attempt_no)))
