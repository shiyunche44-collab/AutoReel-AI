"""屏上文字的核对:关键词 / 数字 / 对白回声,以及从事实里取值、拼参数面板。"""
from __future__ import annotations

import re
from typing import Any, Sequence
from ...categories import Vocab
from ...models import Fact, ShotSpec
from .. import cards, overlays
from ..grounding import numbers_in

from .consts import KEYWORD_CHARS


# --------------------------------------------------------------------------
# 分镜:逐拍版式 + 叠加数据
# --------------------------------------------------------------------------
def _hint(sec_angle: str, view: str, vocab: Vocab) -> str:
    """背景偏好:分镜给的 view 合法就用,否则按段落 angle 推。"""
    view = (view or "").strip().lower()
    if view in vocab.hint_groups:
        return view
    return vocab.hint_of_angle.get(sec_angle, vocab.default_hint)


def _norm_words(text: str) -> str:
    """去掉标点与空白,只留可比对的字。"""
    return re.sub(r"[^\w一-鿿]+", "", text or "")


#: 一个分句至少这么长才可能被认成"整句照抄"
_ECHO_MIN_CLAUSE = 4
#: 大字本身至少这么长,才算"抄了旁白的一段"而不是一个短标签
_ECHO_MIN_KEY = 6


def echoes_narration(keyword: str, narration: str) -> bool:
    """大字是不是把旁白原话搬上了屏。

    旁白本身有字幕,大字再抄一遍,同一句话就在画面里出现两次(实测观感最差的一类
    问题:大字和字幕逐字重复,大字既没加信息又挡住画面)。判定口径:

    - 大字**包含**旁白的某个完整分句(≥4 字)→ 复读;
    - 大字本身就是旁白里的一段(≥6 字)→ 复读。

    只抄两三个字("标配""终身免费")不算 —— 关键词本来就该出现在旁白里,
    那是它的出处,不是冗余。
    """
    key = _norm_words(keyword)
    if not key:
        return True
    whole = _norm_words(narration)
    clauses = [_norm_words(c) for c in re.split(r"[，,。、;:!?\s]+", narration or "")]
    for c in (x for x in clauses if x):
        if len(c) >= _ECHO_MIN_CLAUSE and c in key:
            return True
    if len(key) >= _ECHO_MIN_KEY and key in whole:
        return True
    return False


def forbidden_on_screen(text: str, profile: Any = None) -> bool:
    """上屏的文字里不能有广告法绝对化用语 / 品类禁用词。

    脚本有 lint 管着,但**上屏的字不止脚本**:大数字下面那行「所以呢」、参数卡的
    行名、大字关键词都是另外几条通道,LLM 会照着事实原文抄,事实里带「第一」
    就把「第一」抄上了屏(实跑踩过:保值率那张卡的注释写着「35万以上新能源SUV第一」,
    脚本改合规了,卡片没改)。所以上屏前统一再过一遍。
    """
    if not text or profile is None:
        return False
    from ..script_gen import forbidden_in

    return bool(forbidden_in(str(text), profile))


def ground_keywords(lines: Any, narration_text: str, facts: Sequence[Fact],
                    drop_echo: bool = False, profile: Any = None) -> list[str]:
    """核对两行大字。中文描述放行;数字、英文、型号这类**最容易编**的记号,
    必须在这一拍的旁白或启用的事实里出现过。核不过的行直接丢。

    `drop_echo=True` 时再丢一遍"照抄旁白"的大字(见 `echoes_narration`)。
    冷开场(`hook`)不这么判:3 秒里把钩子放大本来就是刻意设计,不是冗余。
    """
    if isinstance(lines, str):
        lines = [lines]
    pool = narration_text + " " + " ".join(f.claim for f in facts if f.enabled)
    pool_nums = numbers_in(pool)
    pool_up = pool.upper().replace(" ", "")
    out: list[str] = []
    for raw in lines or []:
        text = str(raw).strip()
        if not text or len(text) > KEYWORD_CHARS + 4:
            continue
        if numbers_in(text) - pool_nums:
            continue
        tokens = re.findall(r"[A-Za-z][A-Za-z0-9\-+]*", text)
        if any(t.upper() not in pool_up for t in tokens):
            continue
        if drop_echo and echoes_narration(text, narration_text):
            continue
        if forbidden_on_screen(text, profile):
            continue
        out.append(text[: KEYWORD_CHARS + 4])
    return out[:2]


#: 大数字下面那行"所以呢"的字数上限
STAT_NOTE_CHARS = 14


def ground_note(raw: Any, narration: str, profile: Any = None) -> str:
    """大数字的"所以呢"(对观众意味着什么)。数字只能是旁白里说过的,超长或编了数字就不上屏。

    单摆一个数字没有意义:"23.35 万"要跟着"同价位最低一档","15 分钟"要跟着"吃顿饭的工夫"。

    另外:这行字也是**上屏**的字,一样不能有广告法绝对化用语(「第一」这种)。
    """
    note = re.sub(r"\s+", " ", str(raw or "")).strip(" ,，。.")
    if not note or len(note) > STAT_NOTE_CHARS:
        return ""
    if numbers_in(note) - numbers_in(narration or ""):
        return ""
    if forbidden_on_screen(note, profile):
        return ""
    return note


def ground_stat(raw: Any, facts: Sequence[Fact], narration: str = "",
                profile: Any = None) -> tuple[dict | None, list[str]]:
    """核对大数字:当成一行参数卡交给 `cards.ground`(数字必须见于引用的事实),
    再要求取值里确实有个能滚动的数字。核不过返回 (None, 原因)。
    `narration` 是这一拍的旁白,"所以呢"那行(`note`)只能用它里面的数字。"""
    if not isinstance(raw, dict):
        return None, ["stat 缺数据"]
    row = {"label": raw.get("label"), "values": [raw.get("value")], "fact_id": raw.get("fact_id")}
    grounded, dropped = cards.ground(cards.normalise({"rows": [row]}, "spec"), facts)
    if not grounded:
        return None, dropped
    r = grounded["rows"][0]
    value = r["values"][0]
    if overlays.split_number(value) is None:
        return None, dropped + [f"「{value}」里没有可滚动的数字"]
    out = {"kind": "stat", "label": r["label"][:KEYWORD_CHARS], "value": value,
           "fact_id": r["fact_id"], "source": grounded.get("source", ""),
           "tag": r.get("tag", "")}
    if forbidden_on_screen(out["label"], profile):
        out["label"] = ""
    if forbidden_on_screen(out["tag"], profile):
        out["tag"] = ""
    note = ground_note(raw.get("note"), narration, profile)
    if note:
        out["note"] = note
    return out, dropped


#: 取值要带单位,不然屏幕上只有一个"643",观众不知道那是什么
_UNITS = tuple(sorted((
    "万元", "公里", "分钟", "小时", "秒", "马力", "匹", "度电", "kWh", "KWh", "TOPS",
    "rpm", "km/h", "N·m", "MPa", "GPa", "kg", "mm", "cm", "km", "Nm", "nm",
    "L", "V", "kW", "W", "m", "元", "块", "%", "°", "度",
    "毫米", "厘米", "米", "英寸", "立方英尺", "升", "公斤", "吨", "千瓦时", "千瓦",
    "牛米", "转", "座", "天", "个月", "倍", "档", "kPa",
), key=len, reverse=True))  # 长的在前:"千瓦时"先于"千瓦","万元"先于"元"
#: 这些 topic 当不了参数行名
_BAD_TOPICS = {"对比", "quality", ""}


def _fact_value(claim: str, wanted: set[str]) -> str:
    """从事实原文里取出旁白提到的那个数字(带单位)。取不到返回空串。

    `wanted` 是旁白里的数字集合(`numbers_in` 的去小数点形式)。
    """
    for m in re.finditer(r"\d+(?:\.\d+)?", claim or ""):
        if m.group(0).replace(".", "") not in wanted:
            continue
        unit = _unit_after(claim, m.end())
        if unit:  # 没单位的数字("Ioniq 5"的 5、"HW4"的 4)上屏就是一个看不懂的光杆数字
            return m.group(0) + unit
    return ""


def _unit_after(claim: str, end: int) -> str:
    """数字后面紧跟的单位(容忍一个空格);没有返回空串。数字紧贴在字母后面(型号)也不算。"""
    tail = claim[end:].lstrip(" ")
    for u in _UNITS:
        if tail.startswith(u):
            return u
    return ""


def _fact_label(fact: Fact) -> str:
    """参数行名:优先用事实的主题词,不行就从原文里截一小段。

    行名里不能有数字 —— `cards.ground` 会核对"行名 + 取值"里的每个数字是不是
    都在事实原文里,带个 2026 款就会把整行作废。
    """
    topic = str(getattr(fact, "topic", "") or "").strip()
    if topic and topic not in _BAD_TOPICS and len(topic) <= 6:
        return topic
    claim = str(getattr(fact, "claim", "") or "")
    m = re.search(r"\d+(?:\.\d+)?", claim)
    head = claim[:m.start()] if m else claim
    head = re.sub(r"[0-9A-Za-z]+", "", head).strip("的，。,、 ")
    return head[-6:] or "参数"


def _panel_from_rows(rows: Any, facts: Sequence[Fact]) -> dict | None:
    """用一批行做左侧参数面板。核不过返回 None。"""
    if not isinstance(rows, list) or not rows:
        return None
    grounded, _ = cards.ground(cards.normalise({"title": "", "rows": rows}, "spec"), facts)
    if not grounded:
        return None
    return {**grounded, "kind": "panel"}


def _stat_overlay(row: dict, source: str = "") -> dict | None:
    """把一行核对过的参数做成大数字版式。取值里没有能滚的数字就做不了。"""
    values = row.get("values") or []
    value = str(values[0]) if values else ""
    if overlays.split_number(value) is None:
        return None
    return {"kind": "stat", "label": str(row.get("label", ""))[:KEYWORD_CHARS],
            "value": value, "fact_id": row.get("fact_id", ""), "source": source,
            "tag": row.get("tag", "")}


def _as_panel_overlay(spec: ShotSpec) -> dict | None:
    """把一拍的叠加数据改成左侧参数面板 —— 核过的数据换个版式照样上屏。"""
    ov = spec.overlay if isinstance(spec.overlay, dict) else {}
    card = spec.card if isinstance(spec.card, dict) else {}
    rows = ov.get("rows") or card.get("rows") or []
    if not rows and ov.get("value"):
        rows = [{"label": ov.get("label", ""), "values": [ov["value"]],
                 "fact_id": ov.get("fact_id", ""), "tag": ov.get("tag", "")}]
    if not rows:
        return None
    return {"kind": "panel", "title": ov.get("title") or card.get("title", ""),
            "rows": rows, "source": ov.get("source") or card.get("source", "")}
