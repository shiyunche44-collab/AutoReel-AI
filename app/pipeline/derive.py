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

#: 版本差价账的 topic:写稿要求至少用一条,二改不许删它的钱数
LEDGER_TOPIC = "算账"
_TRIM_PRICE_RE = re.compile(r"(\d{3,4})\s*(?:km|公里)\s*([\u4e00-\u9fa5+]{0,8}?版)[^\d,,。;;]{0,8}?"
                            + _NUM + r"\s*万")
_RANGE_RE = re.compile(r"(\d{3,4})\s*(?:km|公里)")
_GAP_RE = re.compile(r"(?:减|差价?|便宜|贵|多花)\D{0,3}" + _NUM + r"\s*(万)?元")
_LEDGER_RE = re.compile(r"差价 (\d+) 元.*?每多 1 公里续航约 (\d+) 元")


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


def wheelbase_ratio(fact: Fact) -> list[Fact]:
    """同一条事实里的车长和轴距 → 轴距占车长的比例(空间取向看这个,不看绝对值)。"""
    wheelbase = re.search(r"轴距[^\d,,。;;]{0,6}(\d{4})", fact.claim)
    length = re.search(r"(?:车长|车身长度|长宽高|长度)[^\d,,。;;]{0,8}(\d{4})", fact.claim) \
        or re.search(r"长\s*(\d{4})", fact.claim)
    if not wheelbase or not length or fact.compare_with:
        return []
    width, total = float(wheelbase.group(1)), float(length.group(1))
    if not 0 < width < total:
        return []
    return [_make(fact, f"车长 {length.group(1)} 毫米、轴距 {wheelbase.group(1)} 毫米,"
                        f"轴距约占车长 {_fmt(width / total * 100)}%", "尺寸")]


def charge_c_rate(fact: Fact) -> list[Fact]:
    """同一条事实里的电池容量和峰值充电功率 → 充电倍率。"""
    kwh = re.search(_NUM + r"\s*(?:kWh|度电)", fact.claim)
    kw = re.search(_NUM + r"\s*(?:kW(?!h)|千瓦(?!时))", fact.claim)
    if not kwh or not kw or fact.compare_with or not re.search("充电|快充|补能", fact.claim):
        return []
    capacity, power = float(kwh.group(1)), float(kw.group(1))
    if capacity <= 0 or power <= 0:
        return []
    return [_make(fact, f"{kwh.group(1)} 度电池配 {kw.group(1)} 千瓦峰值充电功率,"
                        f"充电倍率约 {_fmt(power / capacity)}C", "补能")]


def trim_gap(fact: Fact) -> list[Fact]:
    """同一句里恰好两个售价 → 差价。只算同一条事实内的,跨事实容易错配版本和年款。"""
    if fact.compare_with or not re.search("售价|指导价|价格|起售", fact.claim):
        return []
    prices = re.findall(_NUM + r"\s*万(?:元)?(?![辆台公里次人])", fact.claim)
    if len(prices) != 2 or prices[0] == prices[1]:
        return []
    a, b = float(prices[0]), float(prices[1])
    return [_make(fact, f"{min(a, b):g} 万和 {max(a, b):g} 万相差约 {_fmt(abs(a - b))} 万元", "价格")]


def _ledger(src: Fact, low: int, high: int, gap: int, low_name: str = "", high_name: str = "") -> Fact:
    more = high - low
    return _make(src, f"{low}km{low_name}和{high}km{high_name}差价 {gap} 元,续航多 {more}km,"
                      f"每多 1 公里续航约 {gap / more:.0f} 元", LEDGER_TOPIC)


def _listed_ledgers(fact: Fact) -> list[Fact]:
    """一条事实列了各版本的续航和价格:相邻两档续航,有同名版本比同名的,没有就比最小的那笔加价。"""
    by_range: dict[int, dict[str, float]] = {}
    for km, name, price in _TRIM_PRICE_RE.findall(fact.claim):
        by_range.setdefault(int(km), {})[name] = float(price)
    out = []
    levels = sorted(by_range)
    for low, high in zip(levels, levels[1:], strict=False):
        lows, highs = by_range[low], by_range[high]
        same = next((n for n in lows if n in highs), "")
        low_name = same or max(lows, key=lambda n: lows[n])
        high_name = same or min(highs, key=lambda n: highs[n])
        gap = round((highs[high_name] - lows[low_name]) * 10000)
        if gap > 0:
            out.append(_ledger(fact, low, high, gap, low_name, high_name))
    return out


def trim_ledger(fact: Fact) -> list[Fact]:
    """版本之间的差价和每多 1 公里续航多花多少钱。只算同一条事实内的,跨事实容易错配版本和年款。"""
    if fact.compare_with or not re.search("售价|指导价|价格|起售|权益价|反选", fact.claim):
        return []
    listed = _listed_ledgers(fact)
    if listed:
        return listed
    ranges = sorted({int(km) for km in _RANGE_RE.findall(fact.claim)})
    gap = _GAP_RE.search(fact.claim)
    if len(ranges) != 2 or not gap:
        return []
    yuan = round(float(gap.group(1)) * (10000 if gap.group(2) else 1))
    return [_ledger(fact, ranges[0], ranges[1], yuan)] if yuan > 0 else []


def ledger_numbers(claim: str) -> set[str]:
    """差价账里观众要听到的两个钱数。"""
    m = _LEDGER_RE.search(claim)
    return set(m.groups()) if m else set()


RULES: dict[str, Callable[[Fact], list[Fact]]] = {
    "hp_to_kw": hp_to_kw,
    "charge_rate": charge_rate,
    "pack_consumption": pack_consumption,
    "wheelbase_ratio": wheelbase_ratio,
    "charge_c_rate": charge_c_rate,
    "trim_gap": trim_gap,
    "trim_ledger": trim_ledger,
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
