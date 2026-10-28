"""指标快照。"""
from __future__ import annotations

import json
import logging
from typing import Any

from .core import connect
from .schema import init_db

log = logging.getLogger(__name__)


# --------------------------------------------------------------------------
# 指标快照
# --------------------------------------------------------------------------
def add_snapshot(publication_id: str, platform: str, captured_at: float,
                 metrics: dict[str, Any], raw: dict[str, Any] | None = None) -> None:
    init_db()
    with connect() as conn:
        conn.execute(
            """INSERT OR IGNORE INTO metric_snapshots
               (publication_id, platform, captured_at, views, likes, comments, favorites,
                shares, coins, danmaku, followers, watch_minutes, completion_rate,
                avg_watch_sec, bounce_2s, followers_gained, hours_since_publish, raw)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                publication_id, platform, captured_at,
                metrics.get("views"), metrics.get("likes"), metrics.get("comments"),
                metrics.get("favorites"), metrics.get("shares"), metrics.get("coins"),
                metrics.get("danmaku"), metrics.get("followers"),
                metrics.get("watch_minutes"), metrics.get("completion_rate"),
                metrics.get("avg_watch_sec"), metrics.get("bounce_2s"),
                metrics.get("followers_gained"), metrics.get("hours_since_publish"),
                json.dumps(raw or {}, ensure_ascii=False),
            ),
        )


def list_snapshots(publication_id: str, limit: int = 200) -> list[dict[str, Any]]:
    init_db()
    with connect() as conn:
        cur = conn.execute(
            "SELECT * FROM metric_snapshots WHERE publication_id = ? ORDER BY captured_at LIMIT ?",
            (publication_id, limit),
        )
        return [dict(r) for r in cur.fetchall()]


def latest_snapshot(publication_id: str) -> dict[str, Any] | None:
    init_db()
    with connect() as conn:
        cur = conn.execute(
            "SELECT * FROM metric_snapshots WHERE publication_id = ? ORDER BY captured_at DESC LIMIT 1",
            (publication_id,),
        )
        row = cur.fetchone()
    return dict(row) if row else None
