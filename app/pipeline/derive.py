"""推算事实:把已采集的参数换算成观众能感知的量。

"15 分钟补能 405 公里"念出来只是一个数,"每分钟补 27 公里"才有体感;
"1003 匹"换成"约 738 千瓦"才能和别的车的功率对上。这类数字不是材料原文,
所以单独成条、来源标 derived、claim 里写清算式和源事实 —— 脚本引用它和引用
普通事实一样走 fact_refs,卡片核对(cards.ground)也照常按 claim 里的数字核。

纯规则,不调 LLM:换算错了是 bug,不是"模型偶尔发挥"。
"""
from __future__ import annotations

import re
from typing import Callable, Iterable, Sequence

from ..models import Fact

#: 1 公制马力 = 0.7355 千瓦
_PS_TO_KW = 0.7355
_CONFIDENCE_RANK = {"low": 0, "medium": 1, "high": 2}

_NUM = r"(\d+(?:\.\d+)?)"


def _fmt(x: float) -> str:
    """推算值的写法:≥100 取整,≥10 留一位,更小留两位,末尾 0 去掉。"""
    digits = 0 if x >= 100 else 1 if x >= 10 else 2
    text = f"{x:.{digits}f}"
    return text.rstrip("0").rstrip(".") if "." in text else text


def _make(src: Fact, claim: str, topic: str) -> Fact:
    return Fact(
        claim=f"{claim}(推算,源自 {src.id})",
        source_tool="derived",
        source_query=src.id,
        retrieved_at=src.retrieved_at,
        confidence=src.confidence,
        topic=topic,
        scope="product",
        basis=src.basis,
        source_kind="derived",
        raw_ref=src.raw_ref,
    )


def hp_to_kw(fact: Fact) -> list[Fact]:
    """马力 → 千瓦。"""
    out = []
    for m in re.finditer(_NUM + r"\s*(?:匹|马力|PS|ps)", fact.claim):
        hp = float(m.group(1))
        if hp < 50:  # "3 匹"多半不是功率
            continue
        out.append(_make(fact, f"{m.group(1)} 匹马力约合 {_fmt(hp * _PS_TO_KW)} 千瓦", "功率"))
    return out


def charge_rate(fact: Fact) -> list[Fact]:
    """"N 分钟补能 M 公里" → 平均每分钟补多少公里。"""
    m = re.search(_NUM + r"\s*分钟[^,,。;;]{0,6}?(?:补能|补充|充电|充)[^\d,,。]{0,4}" + _NUM
                  + r"\s*(?:公里|km)", fact.claim)
    if not m:
        return []
    minutes, km = float(m.group(1)), float(m.group(2))
    if minutes <= 0:
        return []
    return [_make(fact, f"按 {m.group(1)} 分钟补能 {m.group(2)} 公里折算,"
                        f"平均每分钟约补 {_fmt(km / minutes)} 公里", "补能")]


def pack_consumption(fact: Fact) -> list[Fact]:
    """电池标称容量 ÷ 标称续航 → 每百公里大约用掉多少度电(粗算,不是官方电耗)。

    只在一句话里同时出现一个容量和一个续航时才算 —— 跨句配对很容易把
    A 版本的电池和 B 版本的续航凑到一起。
    """
    caps = re.findall(_NUM + r"\s*(?:kWh|度)(?!电)", fact.claim)
    ranges = re.findall(r"续航\D{0,4}" + _NUM + r"\s*(?:公里|km)", fact.claim)
    if len(caps) != 1 or len(ranges) != 1:
        return []
    kwh, km = float(caps[0]), float(ranges[0])
    if km <= 0 or not 20 <= kwh <= 250:
        return []
    std = next((t for t in ("CLTC", "WLTP", "EPA", "NEDC") if t in fact.claim), "")
    label = f"{std} 续航" if std else "续航"
    return [_make(fact, f"按 {caps[0]}kWh 电池、{label} {ranges[0]} 公里粗算,"
                        f"每百公里约用 {_fmt(kwh / km * 100)} 度电", "电耗")]


RULES: dict[str, Callable[[Fact], list[Fact]]] = {
    "hp_to_kw": hp_to_kw,
    "charge_rate": charge_rate,
    "pack_consumption": pack_consumption,
}


def derive(facts: Sequence[Fact], rules: Iterable[str]) -> list[Fact]:
    """对启用的、非低置信度的 product 事实跑推算规则,返回新增的推算事实(已去重)。

    未知规则名直接跳过(profile 写错一个名字不该让事实层整段失败)。
    """
    fns = [RULES[r] for r in rules if r in RULES]
    seen: set[str] = set()
    out: list[Fact] = []
    for fact in facts:
        if (not fact.enabled or fact.scope != "product" or fact.source_kind == "derived"
                or _CONFIDENCE_RANK.get(fact.confidence, 0) < 1):
            continue
        for fn in fns:
            for new in fn(fact):
                key = new.claim.split("(推算")[0]
                if key not in seen:
                    seen.add(key)
                    out.append(new)
    return out
