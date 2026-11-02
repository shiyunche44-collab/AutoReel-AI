"""火山方舟「豆包搜索 Custom 版」网页搜索(REST,结构化条目)。

不走 Responses API 的 web_search 工具:那条路由模型转述结果,丢掉原文,
事实层要的是可溯源的原始素材。这里直接要条目,Summary(与 query 相关的
500~1000 字片段)是官方推荐给大模型用的字段。

Key:实测方舟 Agent Plan 的 ARK_API_KEY 可直接用;单独建了搜索 Key 就配 ARK_SEARCH_API_KEY。
计费:每个主账号每月 500 次免费,超出按量或用 Agent Plan 的 AFP 抵扣。
"""
from __future__ import annotations

import re
import time
from typing import Any

import httpx

from .. import config
from .cli import ConnectorError

TOOL = "search_news"
_RETRY_HTTP = (429, 500, 502, 503, 504)
_QUERY_MAX = 100
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def enabled() -> bool:
    return bool(config.ARK_SEARCH_API_KEY)


def time_range(start: str | None, end: str | None) -> str | None:
    """YYYY-MM-DD 起止 → 接口的 `A..B` 区间;缺一端或格式不对就不限。"""
    if start and end and _DATE.match(start) and _DATE.match(end):
        return f"{start}..{end}"
    return None


def parse(data: dict[str, Any]) -> list[dict[str, str]]:
    """搜索返回 → 新闻形状的条目。正文用 Summary(缺则 Snippet),权威度带上供抽取层降权。"""
    err = (data.get("ResponseMetadata") or {}).get("Error")
    if err:
        raise ConnectorError(TOOL, f"豆包搜索返回错误:{err}")
    out = []
    for r in (data.get("Result") or {}).get("WebResults") or []:
        if not isinstance(r, dict):
            continue
        row = {
            "title": r.get("Title"),
            "snippet": r.get("Summary") or r.get("Snippet"),
            "date": r.get("PublishTime"),
            "link": r.get("Url"),
            "site": r.get("SiteName"),
            "authority": r.get("AuthInfoDes"),
        }
        out.append({k: str(v) for k, v in row.items() if v})
    return out


def search(query: str, *, count: int = 10, time_range: str | None = None,
           sites: str | None = None, auth_level: int = 0, industry: str | None = None,
           icp_only: bool = False, timeout: float = 60) -> list[dict[str, str]]:
    """一次网页搜索。429 / 5xx / 网络错误重试 3 次,其余错误直接抛 ConnectorError。"""
    if not config.ARK_SEARCH_API_KEY:
        raise ConnectorError(TOOL, "未配置 ARK_API_KEY / ARK_SEARCH_API_KEY")
    flt: dict[str, Any] = {"NeedUrl": True}
    if sites:
        flt["Sites"] = sites
    if auth_level:
        flt["AuthInfoLevel"] = auth_level
    if icp_only:
        flt["IcpHostOnly"] = True
    if industry:
        flt["Industry"] = industry
    body: dict[str, Any] = {"Query": query.strip()[:_QUERY_MAX], "SearchType": "web",
                            "Count": max(1, min(int(count), 50)), "Filter": flt}
    if time_range:
        body["TimeRange"] = time_range
    headers = {"Authorization": f"Bearer {config.ARK_SEARCH_API_KEY}",
               "Content-Type": "application/json"}
    last: ConnectorError | None = None
    for attempt in range(3):
        try:
            resp = httpx.post(config.ARK_SEARCH_URL, json=body, headers=headers, timeout=timeout)
        except httpx.HTTPError as exc:
            last = ConnectorError(TOOL, f"网络错误:{type(exc).__name__}: {exc}")
        else:
            if resp.status_code == 200:
                try:
                    return parse(resp.json())
                except ValueError:
                    raise ConnectorError(TOOL, f"豆包搜索返回格式不对:{resp.text[:200]}")
            last = ConnectorError(TOOL, f"HTTP {resp.status_code}:{resp.text[:200]}",
                                  code=resp.status_code)
            if resp.status_code not in _RETRY_HTTP:
                raise last
        if attempt < 2:
            time.sleep(3 * (attempt + 1))
    assert last is not None
    raise last
