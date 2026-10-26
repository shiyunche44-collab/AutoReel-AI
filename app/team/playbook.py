"""打法手册:复盘官写进 `learnings` 的结论,怎么回灌到选题、变体和包装。

- `weights(维度)` → {取值: 权重}。播放、完播率、平均观看、2 秒留存各算一个倍数,
  取几何平均,夹在 0.5–2 之间 —— 再好的写法也只是「多挑一点」,不会让别的写法彻底消失。
  几何平均而不是相乘:指标变多以后相乘会把噪声放大(四个 1.1 相乘就是 1.46)。
- `prompt_text()` → 给主编选题提示词的一段「已验证 / 应避免」。
- `predict({维度: 取值})` → 一个选题的预测倍数(各维度权重的几何平均)。选题闸卡片显示它,
  主编排序也用它。
- `script_hint()` → 写脚本时带上的一段账号经验:开场留人最好 / 最差的真实第一句、
  最近诊断出的问题。不带数字,≤600 字。
- `install()` 把这些挂到 `variety` / `packaging` / `script_gen` 上。只在服务
  启动时挂(`team.scheduler.start`),测试和脚本里不挂,变体、标题顺序和脚本提示词跟以前完全一样。

你在 /team 停用的结论(status=off)不生效。
"""
from __future__ import annotations

import math
import time
from collections import Counter, defaultdict
from typing import Any, Mapping

from .. import db
from . import beat_retention, engagement
from .features import DIMENSION_LABEL, value_label
from .stats import METRIC_LABEL

W_MIN, W_MAX = 0.5, 2.0
#: 给选题提示词看的维度(主编能决定的)
PROMPT_DIMENSIONS = ("subject", "theme", "duration", "aspect")

#: 配对实验计划存在 team_settings 里的键
PLAN_KEY = "experiment_plan"
#: 一个取值要几条样本才够下一条结论(统计的最低样本数)
BLOCK_SIZE = 3
#: 同时最多几个实验(每个维度最多一个)
MAX_PARALLEL = 3


def _geo_mean(values: list[float]) -> float:
    """几个倍数的几何平均。

    - 都差不多(1.2、1.25)→ 约等于那个数,不会被放大成 1.5;
    - 一好一坏(1.5、0.7)→ 约等于 1,信号互相抵消,不会只捡好听的那个。"""
    vals = [v for v in values if v > 0]
    if not vals:
        return 1.0
    return math.exp(sum(math.log(v) for v in vals) / len(vals))


def merged(learnings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """自己的结论和对标先验合并:同一维度同一取值上自己有生效的结论,就不看对标的。"""
    own = {(r["dimension"], r["value"]) for r in learnings
           if r.get("source", "own") != "benchmark" and r.get("status", "active") == "active"}
    return [r for r in learnings
            if r.get("source", "own") != "benchmark" or (r["dimension"], r["value"]) not in own]


def weights(dimension: str, learnings: list[dict[str, Any]] | None = None) -> dict[str, float]:
    """`dimension` 维度各取值的权重:播放、完播率、平均观看、2 秒留存各自算一个倍数,
    再取**几何平均** —— 一个指标好、另一个差时互相抵消,不会只挑好听的。

    夹在 0.5–2 之间:再好的写法也只是「多挑一点」,不会让别的写法彻底消失。"""
    rows = merged(db.list_learnings() if learnings is None else learnings)
    effects: dict[str, list[float]] = defaultdict(list)
    for r in rows:
        if r["dimension"] == dimension and r.get("status", "active") == "active":
            effects[r["value"]].append(float(r["effect"]))
    return {k: round(min(W_MAX, max(W_MIN, _geo_mean(v))), 3)
            for k, v in effects.items()}


def predict(values: Mapping[str, str], learnings: list[dict[str, Any]] | None = None,
            by_dim: Mapping[str, Mapping[str, float]] | None = None) -> float:
    """一个选题 / 一条片的预测倍数:`values` 里各维度取值的权重取几何平均。

    没结论的维度不算(不是按 1 拉平);一条结论都没有 → 1.0。`by_dim` 传了就不再查库。"""
    ws = []
    for dim, value in values.items():
        w = by_dim.get(dim) if by_dim is not None else weights(dim, learnings)
        if w and value in w:
            ws.append(float(w[value]))
    return round(_geo_mean(ws), 3)


def describe(r: dict[str, Any]) -> str:
    """一条结论的人话:「开头 = 场景冷开:播放 1.60 倍(5 条,置信度高)」。"""
    if r.get("source") == "beat":
        conf = "高" if r["confidence"] == "high" else "中"
        return (f"{DIMENSION_LABEL.get(r['dimension'], r['dimension'])} = "
                f"{value_label(r['dimension'], r['value'])}:留人 {r['effect']:.2f} 倍"
                f"({r['n']} 拍,置信度{conf})")
    if r.get("source") == "reply":
        conf = "高" if r["confidence"] == "high" else "中"
        return (f"{DIMENSION_LABEL.get(r['dimension'], r['dimension'])} = "
                f"{value_label(r['dimension'], r['value'])}:"
                f"{engagement.STYLE_METRICS.get(r['metric'], r['metric'])} {r['effect']:.2f} 倍"
                f"({r['n']} 条回复,置信度{conf})")
    if r.get("source") == "benchmark":
        return (f"对标号 {DIMENSION_LABEL.get(r['dimension'], r['dimension'])} = "
                f"{value_label(r['dimension'], r['value'])}:爆款倍数 {r['effect']:.2f} 倍"
                f"({r['n']} 条对标作品)")
    conf = "高" if r["confidence"] == "high" else "中"
    return (f"{DIMENSION_LABEL.get(r['dimension'], r['dimension'])} = "
            f"{value_label(r['dimension'], r['value'])}:"
            f"{METRIC_LABEL.get(r['metric'], r['metric'])} {r['effect']:.2f} 倍"
            f"({r['n']} 条,置信度{conf})")


def prompt_text(learnings: list[dict[str, Any]] | None = None, limit: int = 6) -> str:
    rows = merged(db.list_learnings() if learnings is None else learnings)
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


# --------------------------------------------------------------------------
# 写脚本时带上的账号经验
# --------------------------------------------------------------------------
#: 看最近几天的作品挑开场正反例
HINT_DAYS = 30
#: 诊断只看最近一周发的(老问题可能已经改了)
HINT_DIAGNOSE_DAYS = 7
#: 至少这么多条有 2 秒留存的作品才给正反例(两条好、两条差)
HINT_MIN_WORKS = 4
HINT_MAX_CHARS = 600
#: 早期诊断的问题 → 写脚本的硬规则(`distribution` 是封面 / 标题的事,不进脚本)
SCRIPT_RULE = {
    "hook": "最近有作品前 2 秒就掉人:第一句直接给判断或最硬的反差,不铺垫、不交代背景、不先说这期讲什么。",
    "pace": "最近有作品前 15 秒没留住人:把结论和最有信息量的那一条放进前两段,背景往后挪。",
}


def _opening(project_id: str) -> str:
    from ..pipeline.script_gen import first_sentence

    script = db.read_json(project_id, "script.json", []) or []
    return first_sentence(str(script[0].get("text") or "")) if script else ""


def _hint_rows(since: float) -> list[dict[str, Any]]:
    from . import features

    rows = features.main_rows(features.rows(since=since))
    seen = {r["pub_id"] for r in rows}
    return rows + [r for r in features.early_rows(since=since) if r["pub_id"] not in seen]


def script_hint(now: float | None = None) -> str:
    """给写脚本的 LLM 的一段账号经验。没数据 → 空串(提示词不变)。

    - 近 30 天 2 秒留存最高的两条的**真实第一句**当正例,最低的两条当反例;
    - 最近一周早期诊断出的 hook / pace 问题转成硬规则。
    不放任何播放 / 留存数字:模型拿到数字会往脚本里写。"""
    from . import preference, stats

    now = now or time.time()
    # 你的偏好放最前:截断时先保住它
    parts = [p] if (p := preference.prompt_text()) else []
    scored = sorted(((r["outcome"]["retention_2s"], r) for r in _hint_rows(now - HINT_DAYS * 86400)
                     if r["outcome"].get("retention_2s") is not None), key=lambda x: -x[0])
    if len(scored) >= HINT_MIN_WORKS:
        good = [o for _, r in scored[:2] if (o := _opening(r["project_id"]))]
        bad = [o for _, r in scored[-2:] if (o := _opening(r["project_id"]))]
        bad = [o for o in bad if o not in good]
        if good:
            parts.append("开场留人最好的第一句(学它的写法,别照抄):\n"
                         + "\n".join(f"- 「{o}」" for o in good))
        if bad:
            parts.append("开场留人最差的第一句(避开这种写法):\n"
                         + "\n".join(f"- 「{o}」" for o in bad))
    problems = Counter(p for r in _hint_rows(now - HINT_DIAGNOSE_DAYS * 86400)
                       for p in stats.diagnose(r)["problems"])
    rules = [SCRIPT_RULE[p] for p in SCRIPT_RULE if problems.get(p)]
    if rules:
        parts.append("最近作品的诊断:\n" + "\n".join(f"- {x}" for x in rules))
    return "\n".join(parts)[:HINT_MAX_CHARS]


def install() -> None:
    from .. import config
    from ..pipeline import packaging, script_gen, variety

    variety.learned = weights
    packaging.learned = weights
    variety.plan = plan
    packaging.plan = plan
    script_gen.learned_hint = script_hint
    script_gen.hook_candidates = config.TEAM_HOOK_CANDIDATES
    beat_retention.install()
    engagement.style_weights = lambda: weights(engagement.STYLE_DIMENSION)


def uninstall() -> None:
    from ..pipeline import packaging, script_gen, variety

    variety.learned = None
    packaging.learned = None
    variety.plan = None
    packaging.plan = None
    script_gen.learned_hint = None
    script_gen.hook_candidates = 0
    beat_retention.uninstall()
    engagement.style_weights = None


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


def stored_plans() -> list[dict[str, Any]]:
    """team_settings 里存的实验(新格式是列表;老格式单个 dict 也认)。"""
    raw = db.get_team_setting(PLAN_KEY, None)
    if isinstance(raw, dict):
        raw = [raw]
    if not isinstance(raw, list):
        return []
    seen: set[str] = set()
    out = []
    for p in raw:
        if not isinstance(p, dict) or not p.get("dimension") or not p.get("value"):
            continue
        if p["dimension"] in seen:  # 每个维度最多一个
            continue
        seen.add(str(p["dimension"]))
        out.append(p)
    return out[:MAX_PARALLEL]


def progress() -> list[dict[str, Any]]:
    """每个实验的进度:{dimension, value, size, since, used, done}。"""
    out = []
    for p in stored_plans():
        size = int(p.get("size") or BLOCK_SIZE)
        since = float(p.get("since") or 0)
        used = used_since(str(p["dimension"]), str(p["value"]), since)
        out.append({"dimension": str(p["dimension"]), "value": str(p["value"]),
                    "size": size, "since": since, "used": used, "done": used >= size})
    return out


def plan(dimension: str | None = None) -> dict[str, Any] | None:
    """这条新片该照做的配对实验:{dimension, value}。没有 / 都做够了 → None。

    同时有几个实验时,**一条片只固定其中一个**:挑还差得最多的那个(并列按下发顺序)。
    几个变量钉在同一批片上就分不清是谁的功劳;轮着来,每周 7 条片也不会闲着 ——
    单个实验 3 条就做完,以前剩下的几天只能等下周复盘。

    `dimension`:调用方问的维度;当前轮到的不是它 → None(那个维度照常挑)。
    做够了就自动回到正常挑选 —— 计划是「凑够样本」用的,不是「永远只用这一种」。"""
    live = [p for p in progress() if not p["done"]]
    if not live:
        return None
    turn = min(live, key=lambda p: p["used"] / max(1, p["size"]))
    if dimension is not None and turn["dimension"] != dimension:
        return None
    return {"dimension": turn["dimension"], "value": turn["value"]}
