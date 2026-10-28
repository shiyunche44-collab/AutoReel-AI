"""连接与事务。"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from .. import config


def db_path() -> Path:
    return config.DATA_DIR / config.DB_FILENAME


@contextmanager
def connect() -> Iterator[sqlite3.Connection]:
    """打开一个带 WAL 的连接,退出时关闭。

    ⚠️ **这个上下文管理器不提供事务**。连接是 autocommit
    (`isolation_level=None`),所以 `with connect() as conn:` 那个 with 块
    只是"用完关连接",并不会开启事务 —— 多语句操作在崩溃时会只落一半。

    要原子性(比如"改状态 + 记事件"必须同时成功或同时失败)就用
    `transaction()`。
    """
    config.ensure_dirs()
    conn = sqlite3.connect(db_path(), timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=30000")
        yield conn
    finally:
        conn.close()


@contextmanager
def transaction() -> Iterator[sqlite3.Connection]:
    """显式事务:块里的语句要么全成,要么全不成。

    为什么需要它:`job_events` 是本项目**唯一的排障依据**(状态卡住了,
    靠事件流回放卡在哪一步)。但事件与状态是两条独立语句、两次独立
    连接,进程在中间被杀就会留下"状态变了但没事件"或"有事件但状态
    没变"的裂缝 —— 排障时看到的就是一段自相矛盾的记录。

    `BEGIN IMMEDIATE` 立刻拿写锁,避免两个写事务先读后写升级锁时死锁。
    """
    config.ensure_dirs()
    conn = sqlite3.connect(db_path(), timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield conn
        except BaseException:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:  # pragma: no cover - 回滚本身失败时别盖掉原异常
                pass
            raise
        conn.execute("COMMIT")
    finally:
        conn.close()
