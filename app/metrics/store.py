"""指标落库与聚合。

**只写成功的快照。** 抓不到就跳过 —— 写一条全 None 的快照进表,趋势图上就
会出现一段"掉到零"的假象,比没有数据更糟。抓失败只记日志,保留上次的值。
"""
from __future__ import annotations

import json
import time
from typing import Any

from .. import db
from ..pipeline import aspect as aspect_mod
from ..publish import accounts as accounts_mod
from . import collector

#: 互动率算哪些指标
_ENGAGEMENT = ("likes", "comments", "favorites", "shares")


def record(publication: dict[str, Any]) -> bool:
    """抓一次并落库。成功返回 True。"""
    account = accounts_mod.get_account(publication.get("account_id") or "")
    if not account:
        return False
    snapshot, error = collector.collect_safe(publication, account)
    if snapshot is None:
        return False
    if snapshot.is_empty():
        return False
    db.add_snapshot(publication["id"], publication.get("platform") or "",
                    time.time(), snapshot.as_dict(), snapshot.raw)
    return True


def record_all(limit: int = 200) -> dict[str, int]:
    """把该抓的都抓一遍。返回 {成功数, 失败数}。"""
    ok = failed = 0
    for publication in db.publications_for_metrics(limit=limit):
        if record(publication):
            ok += 1
        else:
            failed += 1
    return {"ok": ok, "failed": failed}


def engagement_rate(metrics: dict[str, Any]) -> float | None:
    """互动率 =(点赞+评论+收藏+转发)/播放。播放取不到就返回 None。"""
    views = metrics.get("views")
    if not views:
        return None
    total = 0
    hit = False
    for key in _ENGAGEMENT:
        value = metrics.get(key)
        if value is None:
            continue
        total += int(value)
        hit = True
    if not hit:
        return None
    return round(total / float(views), 4)


def latest_for(publication_id: str) -> dict[str, Any] | None:
    return db.latest_snapshot(publication_id)


def trend(publication_id: str, limit: int = 200) -> list[dict[str, Any]]:
    """一条作品的指标时间线。"""
    rows = db.list_snapshots(publication_id, limit=limit)
    out = []
    for row in rows:
        metrics = {k: row.get(k) for k in collector.FIELDS}
        out.append({
            "captured_at": row.get("captured_at"),
            **metrics,
            "engagement_rate": engagement_rate(metrics),
        })
    return out


def overview() -> dict[str, Any]:
    """看板汇总:作品列表 + 平台维度合计 + 最近的快照趋势。"""
    from ..publish import service

    publications = db.list_publications(limit=500)
    items: list[dict[str, Any]] = []
    by_platform: dict[str, dict[str, Any]] = {}
    by_aspect: dict[str, dict[str, Any]] = {}
    by_title_kind: dict[str, dict[str, Any]] = {}
    by_cover: dict[str, dict[str, Any]] = {}
    for row in publications:
        view = service.publication_view(row)
        snap = db.latest_snapshot(row["id"])
        metrics = {k: (snap.get(k) if snap else None) for k in collector.FIELDS}
        view["metrics"] = metrics
        view["engagement_rate"] = engagement_rate(metrics)
        view["captured_at"] = snap.get("captured_at") if snap else None
        view["snapshots"] = len(db.list_snapshots(row["id"], limit=1000))
        view["aspect_label"] = aspect_mod.label(view["aspect"])
        items.append(view)
        _add(by_platform.setdefault(row["platform"], _bucket(
            platform=row["platform"], label=view["platform_label"])), row, metrics)
        _add(by_aspect.setdefault(view["aspect"], _bucket(
            aspect=view["aspect"], label=view["aspect_label"])), row, metrics)
        var = view.get("variant") or {}
        kind = var.get("title_kind") or "unknown"
        cover = var.get("cover") or "a"
        _add(by_title_kind.setdefault(kind, _bucket(title_kind=kind, label=_KIND_LABEL.get(kind, kind))),
             row, metrics)
        _add(by_cover.setdefault(cover, _bucket(cover=cover, label=f"封面 {cover.upper()}")), row, metrics)
    items.sort(key=lambda x: -(x.get("published_at") or x.get("created_at") or 0))
    for group in (by_aspect, by_title_kind, by_cover):
        for bucket in group.values():
            # 横竖屏、标题写法、封面版本比的是「每条平均」,条数不一样时合计没有可比性
            bucket["avg_views"] = round(bucket["views"] / bucket["with_views"]) if bucket["with_views"] else None
            bucket["engagement_rate"] = engagement_rate(bucket)
    return {"items": items, "by_platform": list(by_platform.values()),
            "by_aspect": sorted(by_aspect.values(), key=lambda b: b["aspect"]),
            "by_title_kind": sorted(by_title_kind.values(), key=lambda b: b["title_kind"]),
            "by_cover": sorted(by_cover.values(), key=lambda b: b["cover"])}


#: 标题写法的中文名(包装里 stake/number/person;custom 是发布时手改的;unknown 是没走包装的老记录)
_KIND_LABEL = {"stake": "立场", "number": "数字", "person": "人群", "custom": "手改", "unknown": "未记录"}

_SUM_KEYS = ("views", "likes", "comments", "favorites", "shares", "followers")


def _bucket(**head: Any) -> dict[str, Any]:
    return {**head, "published": 0, "count": 0, "with_views": 0, **{k: 0 for k in _SUM_KEYS}}


def _add(bucket: dict[str, Any], row: dict[str, Any], metrics: dict[str, Any]) -> None:
    bucket["count"] += 1
    if row["state"] == "published":
        bucket["published"] += 1
    if metrics.get("views") is not None:
        bucket["with_views"] += 1
    for key in _SUM_KEYS:
        value = metrics.get(key)
        if value:
            bucket[key] += int(value)


def platform_series(platform: str, days: int = 30) -> list[dict[str, Any]]:
    """某个平台最近 N 天的播放量曲线(所有作品合计)。

    按天把各作品的最新快照加起来 —— 缺哪天就跳过那天,不补 0。
    """
    since = time.time() - days * 86400
    buckets: dict[str, dict[str, Any]] = {}
    for row in db.list_publications(limit=500):
        if row["platform"] != platform:
            continue
        for snap in db.list_snapshots(row["id"], limit=1000):
            at = snap.get("captured_at") or 0
            if at < since:
                continue
            day = time.strftime("%Y-%m-%d", time.localtime(at))
            bucket = buckets.setdefault(day, {"day": day, "views": 0, "likes": 0,
                                              "comments": 0, "count": 0})
            bucket["count"] += 1
            for key in ("views", "likes", "comments"):
                value = snap.get(key)
                if value:
                    bucket[key] += int(value)
    return [buckets[k] for k in sorted(buckets)]
