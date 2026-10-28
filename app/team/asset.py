"""素材官:选题一批准就给它的车提前建 / 补素材池。

素材池原本在项目走到分镜时才开始建,门禁 A 那几分钟根本攒不够 —— 确认是
本车的视频经常只有一两分钟,背景只能反复用官图。选题批准到开工通常隔一两天,
正好拿来建池。

**只建不够的。** 确认本车视频(官方 / 认出是这台车,无主持人无台标)够
`CONFIRMED_VIDEO_TARGET` 秒(横评每台车 `ROUNDUP_ENOUGH_SECONDS`)就跳过。
建池要花视觉打标签的额度,但这时还没有项目可以记账,所以有总开关
`TEAM_ASSET_PREBUILD`。
"""
from __future__ import annotations

from typing import Any

from .. import config, db
from ..categories import load_profile
from . import Skip, chief

#: 这些状态的选题还用得上提前建的池子
_WANT = ("approved",)


def cars_of(item: dict[str, Any]) -> list[str]:
    return [s for s in (item.get("subjects") or []) if s] or [item["subject"]]


def _thin(name: str, target: float) -> bool:
    from ..pipeline import library

    return library.confirmed_seconds(library.Library.load(name), video_only=True) < target


def wanted(items: list[dict[str, Any]]) -> list[tuple[str, str, float]]:
    """要建池的 (车, 品类, 目标秒数),去重,在建的跳过。"""
    from ..pipeline import library

    out: list[tuple[str, str, float]] = []
    seen: set[str] = set()
    for item in items:
        cars = cars_of(item)
        target = library.ROUNDUP_ENOUGH_SECONDS if len(cars) > 1 else library.CONFIRMED_VIDEO_TARGET
        for name in cars:
            key = library.slug(name)
            if key in seen or library.is_building(name):
                continue
            seen.add(key)
            if _thin(name, target):
                out.append((name, item["category"], target))
    return out


def run() -> str:
    if not config.TEAM_ASSET_PREBUILD or not config.USE_LIBRARY:
        raise Skip("提前建池已关闭")
    from ..pipeline import library, orchestrator

    if chief.render_busy():
        raise Skip("渲染名额占满,素材预建先让路")
    todo = wanted(db.list_plan_items(statuses=_WANT, limit=200))
    if not todo:
        raise Skip("已批选题的素材池都够了")
    llm = orchestrator._llm()
    # 同品类同目标的车交给一个后台线程依次建(视觉打标签有限流,并发只会排队)
    groups: dict[tuple[str, float], list[str]] = {}
    for name, category, target in todo:
        groups.setdefault((category, target), []).append(name)
    started: list[str] = []
    for (category, target), names in groups.items():
        kwargs: dict[str, Any] = {"profile": load_profile(category), "llm": llm}
        if target < library.CONFIRMED_VIDEO_TARGET:
            kwargs.update(enough_seconds=library.ROUNDUP_ENOUGH_SECONDS,
                          max_sources=library.ROUNDUP_MAX_SOURCES)
        started += library.build_many_async(names, **kwargs)
    if not started:
        raise Skip("要补的素材池都在建")
    return f"开始补素材池:{'、'.join(started)}"
