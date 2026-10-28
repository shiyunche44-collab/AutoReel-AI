"""对标账号 / 对标作品 / 留存曲线 / 账号日数据。"""
from __future__ import annotations

import json
import logging
import time
from typing import Any

from .core import connect, transaction
from .schema import init_db

log = logging.getLogger(__name__)


# --------------------------------------------------------------------------
# 对标账号 / 对标作品 / 行业日历
# --------------------------------------------------------------------------
_BENCH_FIELDS = {"url", "nickname", "note", "enabled", "median_likes", "last_fetched_at",
                 "last_error"}


def add_benchmark_account(sec_uid: str, url: str = "", note: str = "") -> bool:
    """加一个对标账号。已经有了返回 False。"""
    init_db()
    with connect() as conn:
        cur = conn.execute(
            "INSERT OR IGNORE INTO benchmark_accounts (id, url, note, created_at) VALUES (?,?,?,?)",
            (sec_uid, url, note, time.time()))
    return bool(cur.rowcount)


def list_benchmark_accounts(*, enabled_only: bool = False) -> list[dict[str, Any]]:
    init_db()
    sql = "SELECT * FROM benchmark_accounts"
    if enabled_only:
        sql += " WHERE enabled = 1"
    with connect() as conn:
        return [dict(r) for r in conn.execute(sql + " ORDER BY created_at").fetchall()]


def get_benchmark_account(sec_uid: str) -> dict[str, Any] | None:
    init_db()
    with connect() as conn:
        row = conn.execute("SELECT * FROM benchmark_accounts WHERE id = ?", (sec_uid,)).fetchone()
    return dict(row) if row else None


def save_benchmark_tag(post_id: str, tags: dict[str, Any] | None, error: str = "",
                       ts: float | None = None) -> None:
    init_db()
    with connect() as conn:
        conn.execute("INSERT OR REPLACE INTO benchmark_tags (post_id, tags, error, tagged_at)"
                     " VALUES (?, ?, ?, ?)",
                     (post_id, json.dumps(tags or {}, ensure_ascii=False), error[:200],
                      ts or time.time()))


def list_benchmark_tags(since: float | None = None) -> dict[str, dict[str, Any]]:
    """{post_id: {tags, error, tagged_at}},`since` 按打标签的时间。"""
    init_db()
    with connect() as conn:
        rows = conn.execute("SELECT * FROM benchmark_tags WHERE tagged_at >= ?",
                            (since or 0,)).fetchall()
    return {r["post_id"]: {**dict(r), "tags": json.loads(r["tags"] or "{}")} for r in rows}


def save_progress_curve(pub_id: str, points: list[list[float]], views: int | None = None,
                        ts: float | None = None) -> None:
    init_db()
    with connect() as conn:
        conn.execute("INSERT OR REPLACE INTO progress_curves (pub_id, points, views, fetched_at)"
                     " VALUES (?, ?, ?, ?)",
                     (pub_id, json.dumps(points), views, ts or time.time()))


def save_account_daily(platform: str, day: str, followers: int, ts: float | None = None) -> bool:
    """记当天的粉丝数。一天只记第一张(当天早些的数更接近「前一天结束时」)。返回是不是新记的。"""
    init_db()
    with connect() as conn:
        cur = conn.execute("INSERT OR IGNORE INTO account_daily (platform, day, followers, captured_at)"
                           " VALUES (?, ?, ?, ?)", (platform, day, int(followers), ts or time.time()))
    return bool(cur.rowcount)


def list_account_daily(platform: str = "douyin", since_day: str = "") -> dict[str, int]:
    """{日期: 粉丝数},按日期升序。"""
    init_db()
    with connect() as conn:
        rows = conn.execute("SELECT day, followers FROM account_daily WHERE platform = ? AND day >= ?"
                            " ORDER BY day", (platform, since_day)).fetchall()
    return {r["day"]: int(r["followers"]) for r in rows}


def list_progress_curves(since: float | None = None) -> dict[str, dict[str, Any]]:
    """{pub_id: {points, views, fetched_at}},`since` 按抓取时间。"""
    init_db()
    with connect() as conn:
        rows = conn.execute("SELECT * FROM progress_curves WHERE fetched_at >= ?",
                            (since or 0,)).fetchall()
    return {r["pub_id"]: {**dict(r), "points": json.loads(r["points"] or "[]")} for r in rows}


def update_benchmark_account(sec_uid: str, **fields: Any) -> None:
    bad = set(fields) - _BENCH_FIELDS
    if bad:
        raise ValueError(f"不能改的对标账号字段:{sorted(bad)}")
    if not fields:
        return
    init_db()
    sets = ", ".join(f"{k} = ?" for k in fields)
    with connect() as conn:
        conn.execute(f"UPDATE benchmark_accounts SET {sets} WHERE id = ?",
                     (*fields.values(), sec_uid))


def delete_benchmark_account(sec_uid: str) -> None:
    """删账号连带它的作品(对标数据是抓来的,随时能再抓,不是用户资产)。"""
    init_db()
    with transaction() as conn:
        conn.execute("DELETE FROM benchmark_posts WHERE account_id = ?", (sec_uid,))
        conn.execute("DELETE FROM benchmark_accounts WHERE id = ?", (sec_uid,))


def upsert_benchmark_posts(rows: list[dict[str, Any]]) -> int:
    """作品入库,已有的刷新数据。返回新增条数。"""
    init_db()
    now = time.time()
    n = 0
    cols = ("id", "account_id", "title", "car", "duration", "posted_at", "views", "likes",
            "comments", "shares", "collects", "pinned", "burst", "video_url", "cover_url")
    text = ("id", "account_id", "title", "car", "video_url", "cover_url")
    with transaction() as conn:
        for r in rows:
            exists = conn.execute("SELECT 1 FROM benchmark_posts WHERE id = ?", (r["id"],)).fetchone()
            vals = [r.get(c, "" if c in text else 0) for c in cols]
            conn.execute(
                f"INSERT OR REPLACE INTO benchmark_posts ({', '.join(cols)}, fetched_at)"
                f" VALUES ({','.join('?' * len(cols))}, ?)", (*vals, now))
            n += 0 if exists else 1
    return n


def list_benchmark_posts(*, account_id: str | None = None, since: float | None = None,
                         min_burst: float | None = None, limit: int = 200) -> list[dict[str, Any]]:
    """新发的在前。"""
    init_db()
    where, args = [], []
    if account_id:
        where.append("account_id = ?")
        args.append(account_id)
    if since is not None:
        where.append("posted_at >= ?")
        args.append(since)
    if min_burst is not None:
        where.append("burst >= ?")
        args.append(min_burst)
    sql = "SELECT * FROM benchmark_posts"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY posted_at DESC LIMIT ?"
    args.append(limit)
    with connect() as conn:
        return [dict(r) for r in conn.execute(sql, args).fetchall()]
