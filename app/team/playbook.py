"""打法手册:复盘官写进 `learnings` 的结论,怎么回灌到选题、变体和包装。

- `weights(维度)` → {取值: 权重}。播放倍数 × 完播倍数,夹在 0.5–2 之间 ——
  再好的写法也只是「多挑一点」,不会让别的写法彻底消失。
- `prompt_text()` → 给主编选题提示词的一段「已验证 / 应避免」。
- `install()` 把 `weights` 挂到 `variety.learned` 和 `packaging.learned` 上。只在服务
  启动时挂(`team.scheduler.start`),测试和脚本里不挂,变体和标题顺序跟以前完全一样。

你在 /team 停用的结论(status=off)不生效。
"""
from __future__ import annotations

from collections import defaultdict
from typing import Any

from .. import db
from .features import DIMENSION_LABEL, value_label
from .stats import METRIC_LABEL

W_MIN, W_MAX = 0.5, 2.0
#: 给选题提示词看的维度(主编能决定的)
PROMPT_DIMENSIONS = ("subject", "theme", "duration", "aspect")


def weights(dimension: str, learnings: list[dict[str, Any]] | None = None) -> dict[str, float]:
    rows = db.list_learnings() if learnings is None else learnings
    out: dict[str, float] = defaultdict(lambda: 1.0)
    for r in rows:
        if r["dimension"] == dimension and r.get("status", "active") == "active":
            out[r["value"]] *= float(r["effect"])
    return {k: round(min(W_MAX, max(W_MIN, v)), 3) for k, v in out.items()}


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


def uninstall() -> None:
    from ..pipeline import packaging, variety

    variety.learned = None
    packaging.learned = None
