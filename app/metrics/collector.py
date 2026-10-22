"""平台数据抓取。

**解析是纯函数,抓取是薄壳。** 各平台的返回结构解析拆成纯函数
(`parse_bilibili_stat` / `parse_youtube_statistics` / `parse_douyin_statistics`),
用样例 JSON 就能单测,不用联网、不用账号。

**缺失即 None。** 平台没给的指标留 None,不用 0 顶替 —— 0 和"没权限"在看板上
是两回事,0 会把"没权限"画成"零播放"。
"""
from __future__ import annotations

import re
from typing import Any

from ..publish.base import Account, MetricSnapshot, PlatformRef, PublishError
from ..publish.registry import adapter_for

#: 各平台指标的字段名(看板按这几个字段读)
FIELDS = ("views", "likes", "comments", "favorites", "shares", "coins",
          "danmaku", "followers")


def _int(value: Any) -> int | None:
    """转 int。取不到就 None。"""
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


# --------------------------------------------------------------------------
# 纯解析函数
# --------------------------------------------------------------------------
def parse_bilibili_stat(payload: dict[str, Any]) -> dict[str, Any]:
    """B站 `archive/stat` → 指标字典。

    字段:view / like / reply / favorite / coin / share / danmaku。
    """
    stat = (payload or {}).get("data") or {}
    return {
        "views": _int(stat.get("view")),
        "likes": _int(stat.get("like")),
        "comments": _int(stat.get("reply")),
        "favorites": _int(stat.get("favorite")),
        "shares": _int(stat.get("share")),
        "coins": _int(stat.get("coin")),
        "danmaku": _int(stat.get("danmaku")),
    }


def parse_youtube_statistics(payload: dict[str, Any]) -> dict[str, Any]:
    """YouTube `videos.list(part=statistics)` → 指标字典。

    收藏 / 转发 / 投币在 YouTube 不公开 → None(不是 0)。
    """
    items = (payload or {}).get("items") or []
    if not items:
        return {}
    stats = items[0].get("statistics") or {}
    return {
        "views": _int(stats.get("viewCount")),
        "likes": _int(stats.get("likeCount")),
        "comments": _int(stats.get("commentCount")),
        "favorites": None,
        "shares": None,
        "coins": None,
        "danmaku": None,
    }


def parse_youtube_channel(payload: dict[str, Any]) -> dict[str, Any]:
    """YouTube `channels.list(part=statistics)` → 粉丝数。"""
    items = (payload or {}).get("items") or []
    if not items:
        return {}
    stats = items[0].get("statistics") or {}
    return {"followers": _int(stats.get("subscriberCount"))}


def parse_douyin_statistics(payload: dict[str, Any]) -> dict[str, Any]:
    """抖音 `video_data` → 指标字典。

    `statistics` 里没有收藏数(平台不在开放接口里给)→ None。
    转发用 forward_count,分享用 share_count,两个口径不同,原始值留在 raw 里。
    """
    data = (payload or {}).get("data") or {}
    items = data.get("list") or []
    if not items:
        return {}
    stat = items[0].get("statistics") or {}
    return {
        "views": _int(stat.get("play_count")),
        "likes": _int(stat.get("digg_count")),
        "comments": _int(stat.get("comment_count")),
        "favorites": None,
        "shares": _int(stat.get("share_count")),
        "coins": None,
        "danmaku": None,
    }


#: 页面文字里各指标可能出现的标签(按优先级)
TEXT_LABELS: dict[str, tuple[str, ...]] = {
    "views": ("播放量", "浏览量", "观看量", "曝光量", "播放", "浏览"),
    "likes": ("点赞量", "点赞数", "点赞", "获赞"),
    "comments": ("评论量", "评论数", "评论", "互动数"),
    "favorites": ("收藏量", "收藏数", "收藏"),
    "shares": ("分享量", "分享数", "转发量", "转发数", "分享", "转发"),
    "followers": ("粉丝量", "粉丝数", "粉丝"),
}

#: 中文数量单位
_UNITS = {"万": 10_000, "w": 10_000, "W": 10_000, "亿": 100_000_000, "k": 1_000,
          "K": 1_000, "m": 1_000_000, "M": 1_000_000}


def parse_count(text: str) -> int | None:
    """「1.2万」→ 12000。认不出就 None。"""
    raw = str(text or "").strip()
    if not raw:
        return None
    digits = ""
    unit = ""
    for ch in raw:
        if ch.isdigit() or ch in ".,":
            digits += ch
        elif ch in _UNITS:
            unit = ch
            break
        elif ch in " -":
            continue
        else:
            break
    if not digits:
        return None
    try:
        value = float(digits.replace(",", ""))
    except ValueError:
        return None
    if unit:
        value *= _UNITS[unit]
    return int(value)


def parse_metric_text(text: str) -> dict[str, Any]:
    """从页面文字里抠出指标 —— 给没有数据接口、只能看后台页面的平台用。

    认标签后面的第一个数字:「播放量 1.2万」→「views: 12000」。
    认不出来就 None(不填 0)。
    """
    out: dict[str, Any] = {key: None for key in TEXT_LABELS}
    flat = re.sub(r"[ \t　]+", " ", str(text or ""))
    for key, labels in TEXT_LABELS.items():
        for label in labels:
            pattern = rf"{re.escape(label)}\s*[:：]?\s*([0-9][0-9.,]*\s*[万wW亿kKmM]?)"
            m = re.search(pattern, flat)
            if m:
                got = parse_count(m.group(1))
                if got is not None:
                    out[key] = got
                    break
    return out


# --------------------------------------------------------------------------
# 抓取
# --------------------------------------------------------------------------
def collect(publication: dict[str, Any], account: Account) -> MetricSnapshot:
    """抓一条发布记录的作品数据。"""
    platform = publication.get("platform") or ""
    adapter = adapter_for(platform)
    ref = PlatformRef(remote_id=publication.get("remote_id") or "",
                      remote_url=publication.get("remote_url") or "")
    return adapter.fetch_metrics(ref, account)


def collect_safe(publication: dict[str, Any], account: Account) -> tuple[MetricSnapshot | None, str]:
    """抓数据但不抛异常。返回 (快照, 错误信息)。

    抓取失败不该让整轮监控崩掉 —— 别的平台还等着呢。
    """
    try:
        return collect(publication, account), ""
    except PublishError as exc:
        return None, exc.message
    except Exception as exc:  # noqa: BLE001
        return None, f"{type(exc).__name__}: {exc}"
