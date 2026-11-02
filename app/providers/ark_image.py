"""火山方舟 Agent Plan 的豆包 Seedream 生图(doubao-seedream-5-0-flash / 5-0-pro)。

走套餐专用的 `{ARK_BASE_URL}/images/generations`(路径里带 `/plan`),鉴权就是专属 API Key。
文生图、图生图都是同一个接口:图生图在 `image` 里放参考图(公网 URL 或 `data:image/…;base64,…`)。
接口没有 seed,同一提示词每次出的图不同;调用方要复现就自己缓存。
"""
from __future__ import annotations

import logging
import time

import httpx

from .. import config
from .cli import ConnectorError

log = logging.getLogger(__name__)

TOOL = "ark_image"
_RETRY_HTTP = (429, 500, 502, 503, 504)
#: 总像素必须落在 [1280x720, 2048x2048x1.1025],宽高比 1/16 ~ 16。取 ~2.1M 像素,足够抠图 / 做封面
_TARGET_PIXELS = 2048 * 1024
_MIN_PIXELS, _MAX_PIXELS = 1280 * 720, 4624220


def size_for(aspect_ratio: str | None) -> str:
    """`16:9` → `1932x1086`。没给或解析不了就给 2K(模型自己决定)。"""
    try:
        w, h = (float(x) for x in str(aspect_ratio).split(":"))
        ratio = w / h
        if not 1 / 16 <= ratio <= 16:
            raise ValueError
    except (ValueError, ZeroDivisionError):
        return "2K"
    pixels = min(_MAX_PIXELS, max(_MIN_PIXELS, _TARGET_PIXELS))
    height = round((pixels / ratio) ** 0.5)
    return f"{round(height * ratio)}x{height}"


def generate_one(prompt: str, *, aspect_ratio: str | None = None,
                 input_urls: list[str] | None = None, timeout: float = 180,
                 retries: int = 2) -> str:
    """出一张图,返回图片地址。失败抛 ConnectorError。"""
    if not config.ARK_API_KEY:
        raise ConnectorError(TOOL, "未配置 ARK_API_KEY(方舟 Agent Plan 专属 API Key)")
    body: dict = {"model": config.IMAGE_MODEL, "prompt": prompt[:2000],
                  "size": size_for(aspect_ratio), "response_format": "url",
                  "watermark": False}
    if input_urls:
        body["image"] = input_urls if len(input_urls) > 1 else input_urls[0]
    headers = {"Authorization": f"Bearer {config.ARK_API_KEY}"}
    last: ConnectorError | None = None
    for attempt in range(retries + 1):
        try:
            resp = httpx.post(f"{config.ARK_BASE_URL}/images/generations", json=body,
                              headers=headers, timeout=timeout)
        except httpx.HTTPError as exc:
            last = ConnectorError(TOOL, f"网络错误:{type(exc).__name__}: {exc}")
        else:
            if resp.status_code == 200:
                try:
                    return str(resp.json()["data"][0]["url"])
                except (ValueError, KeyError, IndexError, TypeError):
                    raise ConnectorError(TOOL, f"方舟返回格式不对:{resp.text[:200]}")
            last = ConnectorError(TOOL, f"HTTP {resp.status_code}:{resp.text[:300]}",
                                  code=resp.status_code)
            if resp.status_code not in _RETRY_HTTP:
                raise last
        if attempt < retries:
            time.sleep(3 * (attempt + 1))
    assert last is not None
    raise last


def generate(prompt: str, *, n: int = 1, aspect_ratio: str | None = None,
             input_urls: list[str] | None = None, timeout: float = 180) -> list[str]:
    """出 n 张候选(逐张请求)。部分失败时返回已成功的;一张都没有才抛第一个错误。"""
    urls: list[str] = []
    first: ConnectorError | None = None
    for _ in range(max(1, n)):
        try:
            urls.append(generate_one(prompt, aspect_ratio=aspect_ratio,
                                     input_urls=input_urls, timeout=timeout))
        except ConnectorError as exc:
            log.info("方舟生图失败:%s", exc.message)
            first = first or exc
    if not urls and first is not None:
        raise first
    return urls
