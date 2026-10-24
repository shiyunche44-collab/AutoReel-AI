"""AI 团队:一组按节奏自己跑的角色,共用项目数据库。

**每个角色一个模块、一个 `run()`。** 角色之间不直接调用,只通过数据库交接:
情报员落热点快照 → 主编读快照排选题单 → 用户在 /team 批 → 制片按排期建项目。
这样任何一个角色挂了,别的照常跑;也能单独手动重跑某一个。

**运行记录走 `team_runs` 表**,页面上的「团队动态」就是它。同一角色同一时刻
只跑一份(`db.start_team_run` 在库里抢占),两个服务连同一个库也不会重复跑。

**人只把两道闸**:选题单和成片发布前(发布员)。中间的脚本 / 审片门禁由
制片替人过,机器检查拦下了才停下等人。
"""
from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass, field
from typing import Callable

from .. import db

log = logging.getLogger(__name__)


class TeamError(RuntimeError):
    """角色这次没法干活(没热点、没 LLM……),记一笔失败,下个节点再来。"""


class Skip(Exception):
    """这次没什么可做,不算失败(比如排期已满)。"""


@dataclass(frozen=True)
class Cadence:
    """角色的节奏。daily / weekly 在固定时刻跑,interval 每隔几分钟看一眼。"""

    kind: str  # daily / weekly / interval
    times: tuple[str, ...] = ()
    weekday: int = 0  # weekly 用,周一 = 0
    minutes: int = 0

    def label(self) -> str:
        if self.kind == "interval":
            return f"每 {self.minutes} 分钟"
        days = "每天" if self.kind == "daily" else f"每周{'一二三四五六日'[self.weekday]}"
        return f"{days} {'、'.join(self.times)}"


def _at(day: dt.date, hhmm: str) -> dt.datetime:
    h, m = (int(x) for x in hhmm.split(":"))
    return dt.datetime.combine(day, dt.time(h, m))


def last_slot(cad: Cadence, now: dt.datetime) -> dt.datetime | None:
    """不晚于 now 的最近一个节点。interval 没有固定节点,返回 None。"""
    if cad.kind == "interval" or not cad.times:
        return None
    for back in range(0, 8):
        day = now.date() - dt.timedelta(days=back)
        if cad.kind == "weekly" and day.weekday() != cad.weekday:
            continue
        hits = [t for t in (_at(day, x) for x in cad.times) if t <= now]
        if hits:
            return max(hits)
    return None


def next_slot(cad: Cadence, now: dt.datetime, last_started: float | None) -> dt.datetime:
    """下一次会在什么时候跑(页面显示用)。"""
    if cad.kind == "interval":
        if last_started is None:
            return now
        return max(now, dt.datetime.fromtimestamp(last_started) + dt.timedelta(minutes=cad.minutes))
    if is_due(cad, now, last_started):
        return now
    for ahead in range(0, 8):
        day = now.date() + dt.timedelta(days=ahead)
        if cad.kind == "weekly" and day.weekday() != cad.weekday:
            continue
        hits = [t for t in (_at(day, x) for x in cad.times) if t > now]
        if hits:
            return min(hits)
    return now


def is_due(cad: Cadence, now: dt.datetime, last_started: float | None) -> bool:
    """到点没跑过就该跑。服务停机错过的节点,开机后补跑一次(不补多次)。"""
    if cad.kind == "interval":
        return last_started is None or (now.timestamp() - last_started) >= cad.minutes * 60
    slot = last_slot(cad, now)
    if slot is None:
        return False
    return last_started is None or last_started < slot.timestamp()


@dataclass(frozen=True)
class Role:
    name: str
    label: str
    duty: str
    cadence: Cadence
    #: 执行入口,返回一句话总结
    run: Callable[[], str] = field(compare=False)


def roles() -> list[Role]:
    """按执行顺序排:情报先于主编,主编先于素材官和制片,发布员最后。"""
    from . import asset, editor, producer, publisher, scout

    return [
        Role("scout", "情报员", "抓懂车帝榜单、新闻、抖音热搜、B站热门,落热点快照,算车型名次涨跌",
             Cadence("daily", ("08:00", "18:00")), scout.run),
        Role("editor", "主编", "读最新热点,给未来几天的空档排选题(去重、冷却、按热度),等你批",
             Cadence("daily", ("09:00",)), editor.run),
        Role("asset", "素材官", "选题一批准就给它的车提前建 / 补素材池,确认本车视频不够才建",
             Cadence("interval", minutes=30), asset.run),
        Role("producer", "制片", "已批选题到开工时间就建项目;脚本、审片门禁机器检查全过就替你确认,"
             "拦下就停下等你",
             Cadence("interval", minutes=5), producer.run),
        Role("publisher", "发布员", "成片审核通过后按排期定时发布,跟踪发布结果",
             Cadence("interval", minutes=5), publisher.run),
    ]


def role(name: str) -> Role:
    for r in roles():
        if r.name == name:
            return r
    raise KeyError(name)


def run_role(name: str, trigger: str = "schedule") -> dict[str, str]:
    """跑一次某个角色并记录。同角色正在跑就直接返回 busy。"""
    r = role(name)
    run_id = db.start_team_run(name, trigger)
    if run_id is None:
        return {"status": "busy", "summary": f"{r.label}正在跑"}
    try:
        summary = r.run() or ""
        status = "ok"
    except Skip as exc:
        # 轮询型角色的「这次没事」不值得占一行动态
        summary = str(exc)
        status = "idle" if r.cadence.kind == "interval" else "skipped"
    except TeamError as exc:
        summary, status = str(exc), "failed"
    except Exception as exc:  # noqa: BLE001 - 角色出错只记录,不能带崩调度线程
        log.exception("team role %s failed", name)
        summary, status = f"{type(exc).__name__}: {exc}", "failed"
    db.finish_team_run(run_id, status, summary)
    if status == "idle":
        db.prune_idle_runs(name)
    return {"status": status, "summary": summary}


def paused() -> bool:
    return bool(db.get_team_setting("paused", False))


def set_paused(value: bool) -> None:
    db.set_team_setting("paused", bool(value))
