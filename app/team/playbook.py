"""打法手册:复盘官写进 `learnings` 的结论,怎么回灌到选题、变体和包装。

- `weights(维度)` → {取值: 权重}。播放、完播率、平均观看、2 秒留存各算一个倍数,
  取几何平均,夹在 0.5–2 之间 —— 再好的写法也只是「多挑一点」,不会让别的写法彻底消失。
  几何平均而不是相乘:指标变多以后相乘会把噪声放大(四个 1.1 相乘就是 1.46)。
- `prompt_text()` → 给主编选题提示词的一段「已验证 / 应避免」。
- `install()` 把 `weights` 挂到 `variety.learned` 和 `packaging.learned` 上。只在服务
  启动时挂(`team.scheduler.start`),测试和脚本里不挂,变体和标题顺序跟以前完全一样。

你在 /team 停用的结论(status=off)不生效。
"""
from __future__ import annotations

import math
from collections import defaultdict
from typing import Any

from .. import db
from .features import DIMENSION_LABEL, value_label
from .stats import METRIC_LABEL

W_MIN, W_MAX = 0.5, 2.0
#: 给选题提示词看的维度(主编能决定的)
PROMPT_DIMENSIONS = ("subject", "theme", "duration", "aspect")

#: 配对实验计划存在 team_settings 里的键
PLAN_KEY = "experiment_plan"
#: 一个取值要几条样本才够下一条结论(统计的最低样本数)
BLOCK_SIZE = 3


def _geo_mean(values: list[float]) -> float:
    """几个倍数的几何平均。

    - 都差不多(1.2、1.25)→ 约等于那个数,不会被放大成 1.5;
    - 一好一坏(1.5、0.7)→ 约等于 1,信号互相抵消,不会只捡好听的那个。"""
    vals = [v for v in values if v > 0]
    if not vals:
        return 1.0
    return math.exp(sum(math.log(v) for v in vals) / len(vals))


def weights(dimension: str, learnings: list[dict[str, Any]] | None = None) -> dict[str, float]:
    """`dimension` 维度各取值的权重:播放、完播率、平均观看、2 秒留存各自算一个倍数,
    再取**几何平均** —— 一个指标好、另一个差时互相抵消,不会只挑好听的。

    夹在 0.5–2 之间:再好的写法也只是「多挑一点」,不会让别的写法彻底消失。"""
    rows = db.list_learnings() if learnings is None else learnings
    effects: dict[str, list[float]] = defaultdict(list)
    for r in rows:
        if r["dimension"] == dimension and r.get("status", "active") == "active":
            effects[r["value"]].append(float(r["effect"]))
    return {k: round(min(W_MAX, max(W_MIN, _geo_mean(v))), 3)
            for k, v in effects.items()}


def describe(r: dict[str, Any]) -> str:
    """一条结论的人话:「开头 = 场景冷开:播放 1.60 倍(5 条,置信度高)」。"""
    conf = "高" if r["confidence"] == "high" else "中"
    return (f"{DIMENSION_LABEL.get(r['dimension'], r['dimension'])} = "
            f"{value_label(r['dimension'], r['value'])}:"
            f"{METRIC_LABEL.get(r['metric'], r['metric'])} {r['effect']:.2f} 倍"
            f"({r['n']} 条,置信度{conf})")


def prompt_text(learnings: list[dict[str, Any]] | None = None, limit: int = 6) -> str:
    rows = db.list_learnings() if learnings is None else learnings
    rows = [r for r in rows if r["dimension"] in PROMPT_DIMENSIONS]
    good = sorted((r for r in rows if r["effect"] > 1), key=lambda r: -r["effect"])[:limit]
    bad = sorted((r for r in rows if r["effect"] < 1), key=lambda r: r["effect"])[:limit]
    if not good and not bad:
        return ""
    parts = ["账号近 30 天数据(倍数 = 相对账号平常水平;只作参考,热点优先):"]
    if good:
        parts.append("已验证打法:\n" + "\n".join(f"- {describe(r)}" for r in good))
    if bad:
        parts.append("效果偏差,少排:\n" + "\n".join(f"- {describe(r)}" for r in bad))
    return "\n".join(parts)


def install() -> None:
    from ..pipeline import packaging, variety

    variety.learned = weights
    packaging.learned = weights
    variety.plan = plan
    packaging.plan = plan


def uninstall() -> None:
    from ..pipeline import packaging, variety

    variety.learned = None
    packaging.learned = None
    variety.plan = None
    packaging.plan = None


# --------------------------------------------------------------------------
# 配对实验计划:攒样本用的「这轮固定一个变量」
# --------------------------------------------------------------------------
def used_since(dimension: str, value: str, since: float) -> int:
    """计划开始之后,这个维度的这个取值已经用过几条。

    开头 / 结尾记在项目上,标题写法记在发布记录上。"""
    from ..pipeline import variety

    n = 0
    for pub in db.list_publications(limit=200):
        t0 = pub.get("published_at") or pub.get("created_at") or 0
        if not t0 or t0 < since:
            continue
        if dimension in ("title_kind", "cover"):
            raw = pub.get("variant")
        else:
            proj = db.get_project(pub.get("project_id") or "") or {}
            raw = proj.get("variant")
        if str(variety.parse(raw).get(dimension) or "") == value:
            n += 1
    return n


def plan() -> dict[str, Any] | None:
    """当前在跑的配对实验:{dimension, value}。没有计划 / 这轮已经做够了 → None。

    做够了就自动回到正常挑选 —— 计划是「凑够样本」用的,不是「永远只用这一种」。"""
    raw = db.get_team_setting(PLAN_KEY, None)
    if not isinstance(raw, dict):
        return None
    dimension, value = raw.get("dimension"), raw.get("value")
    if not dimension or not value:
        return None
    size = int(raw.get("size") or BLOCK_SIZE)
    since = float(raw.get("since") or 0)
    if used_since(str(dimension), str(value), since) >= size:
        return None
    return {"dimension": str(dimension), "value": str(value)}
