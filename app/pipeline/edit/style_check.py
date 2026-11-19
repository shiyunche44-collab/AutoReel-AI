"""成片验收:出片后量一遍风格指标(`teardown.style_metrics`),和对标基线(`targets`)比。

偏离只出提示,不拦片子:静图占比由门禁 B 的 blocker 管(`orchestrator._still_blocker`),
画面上的问题由成片机审管(`cut_review`)。结果写进 render.json 的 `style`:

    measured_at / video / scope   量的时间、哪个文件、按什么比(landscape 全比,portrait 只比通用项)
    metrics   style_metrics 去掉切点明细(只留个数),加上 cps(字幕字数 ÷ 首句起到末句止)
    checks    [{key, label, value, range, unit, ok, hint}],hint 是偏离时的一句话
    warnings  偏离的那几条,拼好的中文

一条 7 分钟 1080p 成片量一遍约 35 秒(场景检测和帧差各解一遍码)。
"""
from __future__ import annotations

import logging
import re
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .. import teardown
from . import targets

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Rule:
    key: str
    label: str
    lo: float | None
    hi: float | None
    unit: str
    low: str   # 低于下限时的提示
    high: str  # 高于上限时的提示
    #: 只对横屏比(对标片都是横屏讲解片)
    landscape_only: bool = False


_LUFS = (targets.TARGET_LUFS - targets.LUFS_TOLERANCE, targets.TARGET_LUFS + targets.LUFS_TOLERANCE)

RULES: tuple[Rule, ...] = (
    Rule("hook_median", "开场每刀", *targets.HOOK_SHOT_RANGE, "秒",
         "开场切得太碎", "开场一刀太长,前 5 秒抓不住人", landscape_only=True),
    Rule("body_p10", "正片短刀(p10)", *targets.BODY_SHOT_P10_RANGE, "秒",
         "碎刀太多", "缺快切,节奏是匀速的", landscape_only=True),
    Rule("body_median", "正片每刀中位", *targets.BODY_SHOT_MEDIAN_RANGE, "秒",
         "整体切得偏快", "整体切得偏慢", landscape_only=True),
    Rule("body_p90", "正片长镜头(p90)", *targets.BODY_SHOT_P90_RANGE, "秒",
         "缺长镜头(静图多时常见:静图一刀封顶 2.5 秒)", "长镜头拖", landscape_only=True),
    Rule("cuts_per_minute", "每分钟切点", *targets.CUTS_PER_MINUTE_RANGE, "刀",
         "切得太少", "切得太碎", landscape_only=True),
    Rule("dynamic_ratio", "动态画面占比", targets.DYNAMIC_MIN, None, "",
         "画面动得少", ""),
    Rule("still_ratio", "静帧段占比", None, targets.STILL_MAX, "",
         "", "静止画面太多"),
    Rule("lufs", "整片响度", *_LUFS, " LUFS", "偏轻", "偏响"),
    Rule("true_peak", "真峰值", None, targets.TRUE_PEAK_MAX, " dBTP", "", "可能削波"),
    Rule("quiet_gap_db", "停顿处比旁白低", None, targets.MUSIC_BED_GAP_MAX_DB, " dB",
         "", "停顿里没有音乐底,句间是干的"),
    Rule("cps", "语速", *targets.TARGET_CPS_FIT_RANGE, " 字/秒",
         "语速偏慢", "语速偏快", landscape_only=True),
)


def cps(cues: Sequence[Mapping[str, Any]]) -> float | None:
    """字幕字数(去空白)÷ 首句起到末句止的秒数。字幕字数和脚本 `fit.spoken_chars` 逐字相等,
    所以这就是 `LANDSCAPE_CHARS_PER_SECOND` 的口径。"""
    if not cues:
        return None
    chars = sum(len(re.sub(r"\s+", "", str(c.get("text") or ""))) for c in cues)
    span = float(cues[-1].get("end") or 0) - float(cues[0].get("start") or 0)
    return round(chars / span, 2) if span > 0 and chars else None


def flat(metrics: Mapping[str, Any], cues: Sequence[Mapping[str, Any]] = ()) -> dict[str, Any]:
    """style_metrics 的结果摊平成 RULES 认的键(值缺失就是 None)。"""
    hook = metrics.get("hook") or {}
    body = metrics.get("body") or {}
    bed = metrics.get("music_bed_detail") or {}
    return {
        "hook_median": hook.get("median") if hook.get("shots") else None,
        "body_p10": body.get("p10"), "body_median": body.get("median"), "body_p90": body.get("p90"),
        "cuts_per_minute": metrics.get("cuts_per_minute"),
        "dynamic_ratio": metrics.get("dynamic_ratio"), "still_ratio": metrics.get("still_ratio"),
        "lufs": metrics.get("lufs"), "true_peak": metrics.get("true_peak"),
        "quiet_gap_db": bed.get("quiet_gap_db"),
        "cps": cps(cues),
    }


def _fmt(x: float | None) -> str:
    return "" if x is None else f"{x:g}"


def compare(values: Mapping[str, Any], *, portrait: bool = False,
            bgm: bool = True) -> list[dict[str, Any]]:
    """逐条和对标基线比。竖屏只比通用项(动静、响度、音乐底);没配乐不比音乐底;量不出来的跳过。"""
    out: list[dict[str, Any]] = []
    for r in RULES:
        if (portrait and r.landscape_only) or (r.key == "quiet_gap_db" and not bgm):
            continue
        v = values.get(r.key)
        if not isinstance(v, (int, float)):
            continue
        hint = (r.low if r.lo is not None and v < r.lo
                else r.high if r.hi is not None and v > r.hi else "")
        ok = not ((r.lo is not None and v < r.lo) or (r.hi is not None and v > r.hi))
        out.append({"key": r.key, "label": r.label, "value": round(float(v), 2),
                    "range": [r.lo, r.hi], "unit": r.unit, "ok": ok, "hint": "" if ok else hint})
    return out


def warning(check: Mapping[str, Any]) -> str:
    lo, hi = check["range"]
    want = (f"{_fmt(lo)}~{_fmt(hi)}" if lo is not None and hi is not None
            else f"≥{_fmt(lo)}" if lo is not None else f"≤{_fmt(hi)}")
    unit = check["unit"]
    return (f"{check['label']} {_fmt(check['value'])}{unit}(对标 {want}{unit})"
            + (f":{check['hint']}" if check.get("hint") else ""))


def measure(video: Path, cues: Sequence[Mapping[str, Any]] = (), *, portrait: bool = False,
            bgm: bool = True) -> dict[str, Any]:
    """量一条成片,返回 render.json 的 `style` 块。不抛错:量不出来返回带 `error` 的块。"""
    block: dict[str, Any] = {"measured_at": round(time.time(), 1), "video": str(video),
                             "scope": "portrait" if portrait else "landscape"}
    try:
        metrics = teardown.style_metrics(Path(video))
    except Exception as exc:  # noqa: BLE001 —— 验收只是提示,量不出来不能让成片失败
        log.warning("成片风格指标没量出来(%s):%s", video, exc)
        block["error"] = str(exc)[:200]
        return block
    cuts = metrics.pop("cuts", None) or []
    values = flat(metrics, cues)
    checks = compare(values, portrait=portrait, bgm=bgm)
    block.update(metrics=metrics | {"cut_count": len(cuts), "cps": values["cps"]}, checks=checks,
                 warnings=[warning(c) for c in checks if not c["ok"]])
    return block
