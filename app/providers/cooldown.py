"""外部接口的冷却登记:被风控 / 验证码拦了,记下"到什么时候之前别再碰"。

**为什么**:B站空间接口几次探测就 412 / -352,风控持续 30 分钟以上;抖音搜索页撞验证码。
原来"别连续重试"只写在 CLAUDE.md 里:每个项目建池、每轮团队角色都会再去撞一次,
把风控越拖越长。各处零散的做法(浏览器兜底的拦截文件、首评的暂停键)也互不相通。

现在统一落库(`cooldowns` 表),跨进程、跨重启都作数:

    cooldown.check("bilibili.space")            # 冷却中就抛 Cooling
    cooldown.trip("bilibili.space", 1800, "HTTP 412")

连续被拦时冷却时间翻倍(`trips` 计数),封顶 `MAX_SECONDS`;冷却过后第一次调用成功就
`clear()` 掉。冷却中的接口在 `/api/health` 的 `cooldowns` 里能看到,人可以手动清。
"""
from __future__ import annotations

import datetime as dt
import logging
import time
from typing import Any

from .. import db

log = logging.getLogger(__name__)

#: 连续被拦时冷却翻倍的上限(秒)
MAX_SECONDS = 24 * 3600.0


class Cooling(RuntimeError):
    """这个接口还在冷却期,本次不调。"""

    def __init__(self, key: str, until: float, reason: str) -> None:
        self.key, self.until, self.reason = key, until, reason
        at = dt.datetime.fromtimestamp(until).strftime("%m-%d %H:%M")
        super().__init__(f"{key} 冷却中(到 {at},因为:{reason or '被拦'}),本次不调")


def _row(key: str) -> dict[str, Any] | None:
    db.init_db()
    with db.connect() as conn:
        r = conn.execute("SELECT * FROM cooldowns WHERE key = ?", (key,)).fetchone()
        return dict(r) if r else None


def remaining(key: str) -> float:
    """还要冷却多少秒(0 = 可以调)。库读不了按可以调算:冷却是保护,不该变成故障点。"""
    try:
        r = _row(key)
    except Exception:  # noqa: BLE001
        log.debug("读冷却登记失败", exc_info=True)
        return 0.0
    return max(0.0, float(r["until"]) - time.time()) if r else 0.0


def check(key: str) -> None:
    """冷却中就抛 `Cooling`。"""
    try:
        r = _row(key)
    except Exception:  # noqa: BLE001
        log.debug("读冷却登记失败", exc_info=True)
        return
    if r and float(r["until"]) > time.time():
        raise Cooling(key, float(r["until"]), str(r["reason"]))


def trip(key: str, seconds: float, reason: str = "") -> float:
    """登记一次被拦,返回冷却到的时刻。上一次冷却刚过(两倍时长内)又被拦,时长翻倍。"""
    now = time.time()
    try:
        r = _row(key)
        trips = 1
        if r and now - float(r["until"]) < float(seconds) * 2:
            trips = int(r["trips"]) + 1
        until = now + min(MAX_SECONDS, float(seconds) * (2 ** (trips - 1)))
        with db.connect() as conn:
            conn.execute(
                "INSERT INTO cooldowns (key, until, reason, tripped_at, trips) VALUES (?,?,?,?,?)"
                " ON CONFLICT(key) DO UPDATE SET until = excluded.until, reason = excluded.reason,"
                " tripped_at = excluded.tripped_at, trips = excluded.trips",
                (key, until, reason[:300], now, trips))
    except Exception:  # noqa: BLE001
        log.warning("登记冷却失败(%s)", key, exc_info=True)
        return now
    log.warning("%s 被拦,冷却 %.0f 分钟(第 %d 次):%s", key, (until - now) / 60, trips, reason)
    return until


def clear(key: str) -> None:
    """接口恢复了(或人手动解除):删掉登记,下次被拦从头计时。"""
    try:
        db.init_db()
        with db.connect() as conn:
            conn.execute("DELETE FROM cooldowns WHERE key = ?", (key,))
    except Exception:  # noqa: BLE001
        log.debug("清冷却登记失败", exc_info=True)


def active() -> list[dict[str, Any]]:
    """还在冷却中的接口(健康检查用)。"""
    db.init_db()
    with db.connect() as conn:
        cur = conn.execute("SELECT * FROM cooldowns WHERE until > ? ORDER BY until",
                           (time.time(),))
        return [dict(r) for r in cur.fetchall()]
