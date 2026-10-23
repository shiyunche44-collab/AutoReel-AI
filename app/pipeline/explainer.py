"""横屏解说轨(profile `track: explainer`):对标「三驱兄弟」式的信息型长视频。

核心是**画面的角色**:实拍只是"这台车的背景画面",不要求对上旁白 ——
随便哪段干净的都行,短镜头连拼(`library.fill`),信息全部靠叠加层传达。

一拍 = 一段旁白 = 一个版式:

    broll      全屏背景画面
    panel      背景 + 左侧参数面板(核对过事实的 3–5 行)
    highlight  背景缩成小窗 + 两行大字关键词
    card       整屏数据卡(spec / compare / bars / timeline,沿用 cards.py)
    stat       背景压暗 + 中央一个大数字从 0 滚到目标值(核对过事实的一个参数)
    concept    整屏概念动画(HyperFrames 渲染 motion/scenes/,电池、补能、雷达这类
               "看不见的东西";数字同样只来自核对过的事实,渲染不了退回数据卡)

broll 拍按规则自动轮换三种样式(不经 LLM):全屏 / 左上角角标 / 两三栏分屏。
拍内各段之间用 xfade 转场,静图运镜按片段散开挑;每章第一拍开头叠 1.2 秒章节标牌。

时长不再是 6 秒栅格:门禁 A 确认后**先跑 TTS**,每拍的时长 = 它那几句旁白的实际
时长(含句间停顿),画面按这个时长去铺。所以字幕、旁白、画面天然对齐。

底部章节条常驻(当前章高亮),直接烧进每一拍。
"""
from __future__ import annotations

import functools
import logging
import re
import shutil
import subprocess
import zlib
from dataclasses import replace
from pathlib import Path
from typing import Any, Sequence

from .. import config, db
from ..categories import CategoryProfile, Vocab
from ..models import Fact, ScriptSegment, ShotSpec
from . import aspect, cards, library, looks, motion, motion_assets, narration, overlays, rhythm
from . import render as render_mod
from .grounding import numbers_in

_log = logging.getLogger(__name__)

LAYOUTS: tuple[str, ...] = ("broll", "panel", "highlight", "card", "stat", "concept", "hook")
#: 不需要任何数据就能成立的版式 —— 其它版式核对失败都退回它
SAFE_LAYOUT = "broll"
#: 黄金三秒:开场拍前几秒是快切 + 大字的冷开场,每刀多长、大字停多久
HOOK_SECONDS = 3.0
HOOK_CUT = 1.0
#: 连续几拍整屏卡片就太像 PPT 了
MAX_CARD_RUN = 2
#: 概念动画全片最多几拍(每拍要渲染半分钟左右,也不该喧宾夺主)
MAX_CONCEPT = 6
#: 连着最多几拍 broll(都是底图换镜头,没有任何叠加层)。再长就是整段静默
MAX_BROLL_RUN = 3
#: broll 拍的样式,按出现顺序轮换(见 assign_broll_styles)
BROLL_STYLES: tuple[str, ...] = ("plain", "lower_third", "split")
#: 分屏至少要这么长,太短的拍几栏还没滑完就切走了
SPLIT_MIN_SECONDS = 2.5
#: 拍内相邻两段之间的转场时长与样式(按段号轮换)
XFADE = 0.3
XFADE_KINDS: tuple[str, ...] = ("fade", "smoothleft", "slideleft", "circleopen", "zoomin",
                                "fadewhite")
#: 关键词每行最多几个字
KEYWORD_CHARS = 12
#: 横屏字幕单行字数。竖屏是 15 —— 两者都定义在 `aspect.LINE_CHARS`,按项目取用 `aspect.line_chars`
SUBTITLE_LINE_CHARS = aspect.LINE_CHARS[config.LANDSCAPE]
#: 图片背景的运镜(按片段散开挑,见 motion_for)
STILL_MOTIONS = render_mod.STILL_MOTIONS
#: 中间产物的编码参数:拼接用 -c copy,所以所有片段必须完全一致
_ENC = ["-c:v", "libx264", "-preset", "veryfast", "-crf", "18", "-pix_fmt", "yuv420p",
        "-r", str(render_mod.FPS)]

SYSTEM = """你是信息型{domain}解说视频(横屏 16:9,对标「三驱兄弟」)的剪辑导演。
画面是{subject_noun}的官方宣传片做背景,信息靠叠加层传达。你的任务:给每一拍选**版式**并填叠加数据。

版式:
- broll:全屏宣传片,无叠加。旁白在讲感受、观点、过渡时用。
- panel:宣传片 + 左侧参数面板。旁白提到 2 个以上参数时用。给 `panel`:
  {{"title": "面板标题(10 字内)", "rows": [{{"label": "参数名", "values": ["取值含单位"], "fact_id": "f_xxx"}}]}}
  rows 2–5 行。
- highlight:宣传片缩成小窗 + 两行大字。旁白在强调一个卖点 / 关键配置 / 结论时用。给 `keywords`:
  1–2 行,每行 {kw} 字以内,是这一拍最想让观众记住的词(如 {keyword_examples})。
- hook:**只有第 1 拍可选**(黄金三秒冷开场):前 3 秒快切 + 屏幕中央大字弹出。给 `keywords`
  (1–2 行,每行 {kw} 字以内,是旁白第一句里最抓人的词),旁白第一句若围绕一个核心数字,
  再给 `stat`(同下)。段落「可选版式」里有 hook 就优先用它。
- card:整屏数据卡。段落标了「卡片」时优先用。给 `card`:
  {{"title": "卡片标题(12 字内)", "columns": [两个对比对象名,仅对比卡需要],
    "rows": [{{"label": "...", "values": ["..."], "fact_id": "f_xxx"}}]}}
- 对比卡(compare):每行引用**一条同时写着两台车取值的事实**(事实清单里带 [对比:车名] 的那些),
  `values` 按 `columns` 的顺序给两台车的取值,只填这一条 fact_id。两台车没写在同一条事实里
  就别做对比卡 —— 核不过会自动作废。口径不一致的数字不放在一起比。
- stat:宣传片压暗 + 中央一个大数字滚动。旁白只围绕**一个**核心数字时用({stat_examples})。给 `stat`:{{"label": "参数名(8 字内)", "value": "取值含单位", "fact_id": "f_xxx",
  "note": "这个数字对观众意味着什么({note_chars} 字内,从这一拍旁白里取,如「吃顿饭的工夫」「同价位最低一档」)"}}{concept_guide}

硬性要求:
1. 只输出一个 JSON 对象,不要 Markdown 围栏或解释文字。
2. `beats` 数组,每项 {{"idx": 序号, "layout": 版式, "view": "{view_enum}",
   "visual": "背景画面一句话(15 字内)", "panel": ..., "keywords": [...], "card": ...,
   "stat": ...}};
   用不到的键可以省略。数量与给定拍数一致,idx 从 1 开始连续。
3. layout 只能从该拍「可选版式」里选。
4. panel / card 的每一行和 stat 的数字**只能**取自「可用事实」,fact_id 填那条事实的 id;values 里的数字
   必须与事实原文一字不差(含单位),不得换算、取整或估算。找不到就换版式,不要编。
5. keywords 里的型号、芯片名、英文和数字必须在这一拍的旁白或可用事实里出现过,不得自创。
6. 节奏:不要连续 3 拍以上用 card;panel、highlight、stat 穿插着用,不要连续两拍 stat,
   **全片 broll 不超过三成**(共 {count} 拍,最多 {broll_cap} 拍)。旁白里讲到了参数、数字,
   这一拍就用 panel / stat / concept 把它放上屏 —— broll 只留给讲感受、讲取舍、做过渡的拍。
7. view:{view_guide}

必需键: beats"""

USER = """{subject_label}:{subject}
总拍数:{count}

逐拍清单(版式要服务于这一拍的旁白):
{beats}

可用事实:
{facts}

输出 JSON。"""


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


def ground_keywords(lines: Any, narration_text: str, facts: Sequence[Fact],
                    drop_echo: bool = False) -> list[str]:
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
        out.append(text[: KEYWORD_CHARS + 4])
    return out[:2]


#: 大数字下面那行"所以呢"的字数上限
STAT_NOTE_CHARS = 14


def ground_note(raw: Any, narration: str) -> str:
    """大数字的"所以呢"(对观众意味着什么)。数字只能是旁白里说过的,超长或编了数字就不上屏。

    单摆一个数字没有意义:"23.35 万"要跟着"同价位最低一档","15 分钟"要跟着"吃顿饭的工夫"。"""
    note = re.sub(r"\s+", " ", str(raw or "")).strip(" ,，。.")
    if not note or len(note) > STAT_NOTE_CHARS:
        return ""
    if numbers_in(note) - numbers_in(narration or ""):
        return ""
    return note


def ground_stat(raw: Any, facts: Sequence[Fact], narration: str = "") -> tuple[dict | None, list[str]]:
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
    note = ground_note(raw.get("note"), narration)
    if note:
        out["note"] = note
    return out, dropped


#: 取值要带单位,不然屏幕上只有一个"643",观众不知道那是什么
_UNITS = ("万元", "公里", "分钟", "秒", "马力", "匹", "度电", "kWh", "KWh", "TOPS",
          "rpm", "km/h", "N·m", "MPa", "GPa", "kg", "mm", "cm", "km", "Nm", "nm",
          "kg", "L", "V", "kW", "W", "m", "元", "块", "%", "°", "度")
#: 这些 topic 当不了参数行名
_BAD_TOPICS = {"对比", "quality", ""}


def _fact_value(claim: str, wanted: set[str]) -> str:
    """从事实原文里取出旁白提到的那个数字(带单位)。取不到返回空串。

    `wanted` 是旁白里的数字集合(`numbers_in` 的去小数点形式)。
    """
    for m in re.finditer(r"\d+(?:\.\d+)?", claim or ""):
        if m.group(0).replace(".", "") not in wanted:
            continue
        tail = claim[m.end():]
        for u in _UNITS:
            if tail.startswith(u):
                return m.group(0) + u
        return m.group(0)
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



def concept_min(beats: int) -> int:
    """全片至少几拍概念动画。

    概念动画是全片视觉质量最高的部分(电池包格子图、补能曲线、尺寸线稿这类"看不见的东西"
    只有它讲得清),但实测一部片只用了 2–3 段。下限按拍数给:片子越长下限越高,
    但不超过上限 6 拍。
    """
    return min(MAX_CONCEPT, max(3, round(beats * 0.13)))


def concept_guide(vocab: Vocab, beats: int = 0) -> str:
    """分镜提示词里 concept 版式的说明。品类没配动画模板时为空(提示词与之前一字不差)。
    返回值作为 format 参数填进 SYSTEM,里面的花括号不会再被解析。"""
    if not vocab.concept_scenes:
        return ""
    scenes = "\n".join(f"    {name}:{when}" for name, when in vocab.concept_scenes.items())
    lo = concept_min(beats) if beats else 0
    quota = f"全片 concept {lo}–{MAX_CONCEPT} 拍" if lo else f"全片最多 {MAX_CONCEPT} 拍"
    return (
        f"\n- concept:整屏概念动画,讲{vocab.subject_noun}身上看不见、拍不出来的东西时用。给 `concept`:\n"
        '  {"scene": "模板名", "title": "标题(12 字内)", "columns": [两个对比对象名,仅 compare 需要],\n'
        '    "rows": [{"label": "参数名", "values": ["取值含单位"], "fact_id": "f_xxx"}]}\n'
        "  rows 1–4 行,同样只能取自「可用事实」。scene 只能从下面选:\n"
        f"{scenes}\n"
        "  讲看不见的东西(电池、补能、性能、感知)的拍优先用 concept;"
        f"不要连续两拍 concept,{quota}。"
    )


def concept_scene_for(rows: Sequence[dict], narration: str, vocab: Vocab) -> str:
    """给一拍拍一张讲得通的概念模板:参数名 / 旁白里命中哪个模板的关键词就选它。

    配错图等于把画面讲错了 —— 讲补能速度的拍配一张电池包格子图,观众会以为在讲电池容量。
    所以这里必须**命中关键词**才给模板,一个都没命中就不配。
    """
    keys = getattr(vocab, "concept_scene_keys", {}) or {}
    if not keys:
        return ""
    text = " ".join([str(r.get("label", "")) for r in rows] + [narration or ""])
    best, best_hits = "", 0
    for scene, words in keys.items():
        hits = sum(1 for w in words if w and w in text)
        if hits > best_hits:
            best, best_hits = scene, hits
    return best if best_hits else ""


def promote_concepts(specs: list[ShotSpec], profile: CategoryProfile) -> list[ShotSpec]:
    """全片概念动画太少就补:把「本来就在讲参数、有核对过的行」的拍换成概念动画。

    复盘:概念动画是全片视觉质量最高的部分,一部片却只用了 2–3 段 —— 它是这行
    最难被抄的差异点,不该只当装饰用。补的时候只动**静态的数据面板**
    (panel / card),不动大数字(stat):大数字本身就是强画面,模型是特意选的;
    数字还是那批核对过的事实,不会多出没出处的东西;
    模板按参数名和旁白里的词选(见 `concept_scene_for`),不连着两拍 concept。
    """
    if not profile.vocab.concept_scenes:
        return specs
    target = concept_min(len(specs))
    count = sum(1 for s in specs if s.layout == "concept")
    if count >= target:
        return specs
    for n, s in enumerate(specs):
        if count >= target:
            break
        if s.layout not in ("panel", "card"):
            continue
        sec = profile.section(s.section)
        if "concept" not in (sec.layouts or ()):
            continue
        if (n and specs[n - 1].layout == "concept") or \
                (n + 1 < len(specs) and specs[n + 1].layout == "concept"):
            continue  # 别连着两拍 concept(前后两边都要看:后一拍可能是模型自己选的)
        ov = s.overlay if isinstance(s.overlay, dict) else {}
        card = s.card if isinstance(s.card, dict) else {}
        if card.get("columns"):
            continue  # 对比卡是两栏的,概念模板装不下
        rows = ov.get("rows") or card.get("rows") or []
        if not rows and ov.get("value"):
            rows = [{"label": ov.get("label", ""), "values": [ov["value"]],
                     "fact_id": ov.get("fact_id", ""), "tag": ov.get("tag", "")}]
        if not rows:
            continue
        scene = concept_scene_for(rows, s.narration_zh, profile.vocab)
        if not scene or scene not in profile.vocab.concept_scenes:
            continue
        s.layout = "concept"
        s.overlay = {"kind": "concept", "scene": scene, "title": "",
                     "rows": list(rows)[:4],
                     "source": ov.get("source") or card.get("source", "")}
        s.card = None
        s.visual_zh = re.sub(r"^\[[^\]]*\]", "[concept]", s.visual_zh)
        count += 1
    return specs


def _beat_line(i: int, seg: ScriptSegment | None, profile: CategoryProfile,
               facts: Sequence[Fact] = ()) -> str:
    sec = profile.section(seg.section if seg else "")
    options = list(sec.layouts) or list(LAYOUTS)
    card = f" | 卡片:{cards.KIND_GUIDE[sec.card]}" if sec.card in cards.KIND_GUIDE else ""
    car = f"〔{seg.subject}〕" if seg is not None and seg.subject else ""
    # 本拍可用事实:脚本给这一拍引用过的那几条。
    #
    # 为什么必须逐拍给:事实清单是全片一起给的,模型得自己猜"这条参数属于哪一拍",
    # 猜不准就退一步选 broll —— 实测九成 broll 是这么来的(核对失败退下来的只有两三拍)。
    # 把该拍的事实摆到它面前,它才敢往 panel / stat 上填。
    own = [f for f in facts if f.id in set(seg.fact_refs or [])] if seg is not None else []
    own_txt = ""
    if own:
        own_txt = " | 本拍事实:" + ";".join(f"{f.claim}({f.id})" for f in own[:3])
    return (f"[第 {i + 1} 拍] 段落:{sec.name or '未分类'}{car}({sec.beat or '无'}) | "
            f"可选版式:{'/'.join(options)}{card} | 旁白:{seg.text if seg else ''}{own_txt}")


#: 横评时追加到分镜 SYSTEM 末尾的规则
ROUNDUP_BEAT_RULE = ("\n8. 这是多车横评:拍号后〔〕里是这一拍讲的车。panel / card / stat / concept 的每一行"
                     "只能用**这台车**名下的事实(事实前的 [车名]);没有〔〕的开场 / 结论拍可以用任何一台车的,"
                     "对比卡、柱状图的行名写车名。")


def build_beats(*, subject: str, profile: CategoryProfile, segments: Sequence[ScriptSegment],
                facts: Sequence[Fact], llm, card_log: list[dict] | None = None,
                subjects: Sequence[dict[str, str]] | None = None) -> list[ShotSpec]:
    """一段脚本 = 一拍。LLM 挑版式、填叠加数据,这里逐项核对,核不过就降级成 broll。

    `subjects`:多车横评车单。给了就按车标注事实与拍,核对时每拍只认本车 + 共用事实。"""
    usable = [f for f in facts if f.enabled and f.confidence in ("high", "medium")]
    if subjects:
        facts_payload = "\n".join(f"- {f.id} [{f.subject or '共用'}]"
                                  + (f" [对比:{f.compare_with}]" if f.compare_with else "")
                                  + f": {f.claim}"
                                  for f in usable)[:9000] or "(无)"
    else:
        facts_payload = "\n".join(
            f"- {f.id}" + (f" [对比:{f.compare_with}]" if f.compare_with else "")
            + f": {f.claim}" for f in usable)[:5000] or "(无)"
    vocab = profile.vocab
    from .script_gen import roster_line
    user = USER.format(
        subject_label=vocab.subject_label if not subjects else "横评车单",
        subject=subject if not subjects else roster_line(subjects), count=len(segments),
        beats="\n".join(_beat_line(i, s, profile, usable) for i, s in enumerate(segments)),
        facts=facts_payload,
    )
    try:
        system = SYSTEM.format(
            kw=KEYWORD_CHARS, domain=vocab.domain, subject_noun=vocab.subject_noun,
            keyword_examples=vocab.keyword_examples, stat_examples=vocab.stat_examples,
            view_enum=vocab.view_enum, view_guide=vocab.view_guide,
            count=len(segments), note_chars=STAT_NOTE_CHARS,
            broll_cap=max(1, int(len(segments) * rhythm.BROLL_RATIO_MAX)),
            concept_guide=concept_guide(vocab, len(segments)))
        if subjects:
            system = system.replace("\n\n必需键: beats", ROUNDUP_BEAT_RULE + "\n\n必需键: beats")
        data = llm.structured(system, user,
                              required_keys=["beats"], temperature=0.5)
        raw = data.get("beats") or []
    except Exception:  # noqa: BLE001 - 版式选不出来也能出片:全部 broll
        raw = []
    by_idx: dict[int, dict] = {}
    for n, item in enumerate(raw):
        if isinstance(item, dict):
            try:
                by_idx[int(item.get("idx", n + 1)) - 1] = item
            except (TypeError, ValueError):
                by_idx[n] = item

    specs = [_ground_beat(i, seg, by_idx.get(i, {}), subject, profile,
                          facts_for(facts, seg.subject, profile.section(seg.section).name),
                          card_log)
             for i, seg in enumerate(segments)]
    # broll 兜底:这一拍讲的数字还有事实支撑,就把它放上屏(见 rescue_broll)
    specs = rescue_broll(specs, segments, facts, profile)
    specs = enforce_rhythm(specs, profile)
    # broll 仍超三成:对超出的拍强制上屏它引用过的事实(见 enforce_broll_cap)
    specs = enforce_broll_cap(specs, segments, facts, profile)
    # 概念动画是全片视觉质量最高的部分,太少了就补(见 promote_concepts)
    specs = promote_concepts(specs, profile)
    return assign_broll_styles(specs)


def facts_for(facts: Sequence[Fact], subject: str, section: str = "") -> list[Fact]:
    """一拍能用来核对叠加数字的事实:讲某台车的拍只认这台车 + 共用事实;共用拍全认。

    竞品对比段之外的拍**不认对比事实** —— 讲别家车的那条数字只该出现在对比卡上,
    混进本车的参数面板就是把别家的参数安到本车头上。
    """
    from .script_gen import _is_comparison_section

    out = list(facts)
    if subject:
        out = [f for f in out if not f.subject or f.subject == subject]
    if not _is_comparison_section(section):
        out = [f for f in out if not f.compare_with]
    return out


def sync_narration(specs: Sequence[ShotSpec], segments: Sequence[ScriptSegment],
                   facts: Sequence[Fact]) -> list[ShotSpec]:
    """把改过的脚本同步进分镜。

    门禁 A 的脚本编辑器改的是 script.json,而 TTS 读的是分镜里的 `narration_zh` ——
    不同步的话,在门禁 A 改的稿根本进不了成片(改完看着像生效,成片还是旧词)。
    这里只同步旁白,并**按新旁白重新核对**依赖旁白的那部分叠加数据
    (highlight / hook 的大字):大字是照抄旁白的话,旁白一改就可能变成复读,
    或者引用了已经被删掉的数字。

    版式、叠加卡片这些是按事实核过的,不受旁白改写影响,原样保留。
    """
    if len(specs) != len(segments):
        raise ValueError(f"分镜 {len(specs)} 拍与脚本 {len(segments)} 段对不上,没法按拍同步")
    out: list[ShotSpec] = []
    for spec, seg in zip(specs, segments):
        text = (seg.text or "").strip()
        spec = spec.model_copy(deep=True)
        if spec.narration_zh != text:
            spec.narration_zh = text
        ov = spec.overlay
        if isinstance(ov, dict) and ov.get("kind") in ("highlight", "hook"):
            kept = ground_keywords(ov.get("lines") or [], text, facts,
                                   drop_echo=(ov.get("kind") == "highlight"))
            if kept:
                spec.overlay = {**ov, "lines": kept}
            elif ov.get("kind") == "highlight" or not ov.get("stat"):
                # 大字全废:这一拍退回 broll(没字可弹,硬留个空框更难看)
                spec.layout = SAFE_LAYOUT
                spec.overlay = None
                spec.card = None
                spec.visual_zh = re.sub(r"^\[[^\]]*\]", f"[{SAFE_LAYOUT}]", spec.visual_zh)
            else:
                spec.overlay = {**ov, "lines": []}
        out.append(spec)
    return out


def _ground_beat(i: int, seg: ScriptSegment, item: dict, subject: str, profile: CategoryProfile,
                 facts: Sequence[Fact], card_log: list[dict] | None) -> ShotSpec:
    sec = profile.section(seg.section)
    options = list(sec.layouts) or list(LAYOUTS)
    layout = str(item.get("layout") or options[0]).strip()
    if layout not in options:
        layout = options[0]

    overlay: dict | None = None
    card: dict | None = None
    dropped: list[str] = []
    if layout == "panel":
        grounded, dropped = cards.ground(cards.normalise(item.get("panel"), "spec"), facts)
        if grounded and len(grounded["rows"]) >= 2:
            overlay = {**grounded, "kind": "panel"}
        elif grounded:
            # 只剩一行也上屏:能滚成大数字就滚,不能就一行面板。
            # 退成 broll 等于把这条核对过的参数白白扔了 —— 实测 broll 占比四成就是这么来的。
            stat = _stat_overlay(grounded["rows"][0], grounded.get("source", ""))
            if stat:
                overlay, layout = stat, "stat"
            else:
                overlay = {**grounded, "kind": "panel"}
        else:
            layout = SAFE_LAYOUT
    elif layout == "highlight":
        # drop_echo:大字照抄旁白的直接丢。旁白有字幕,同一句话在画面里出现两次
        # 既没加信息又挡住画面 —— 这两行白扔,行都扔光就退回 broll。
        asked = item.get("keywords")
        asked = [asked] if isinstance(asked, str) else list(asked or [])
        lines = ground_keywords(asked, seg.text, facts, drop_echo=True)
        echoed = [str(x).strip() for x in asked
                  if str(x).strip() and str(x).strip() not in lines
                  and echoes_narration(str(x), seg.text)]
        if echoed:
            dropped = [f"大字与旁白逐字重复,已丢:{'、'.join(echoed)}"]
        if lines:
            overlay = {"kind": "highlight", "lines": lines}
        else:
            layout = SAFE_LAYOUT
    elif layout == "stat":
        overlay, dropped = ground_stat(item.get("stat"), facts, seg.text)
        if overlay is None:
            # 大数字做不出来(取值里没有能滚的数字):同一行数据改成参数面板照样上屏
            raw = item.get("stat")
            row = ({"label": raw.get("label"), "values": [raw.get("value")],
                    "fact_id": raw.get("fact_id")} if isinstance(raw, dict) else None)
            rescued = _panel_from_rows([row] if row else [], facts)
            if rescued:
                overlay, layout = rescued, "panel"
            else:
                layout = SAFE_LAYOUT
    elif layout == "hook":
        lines = ground_keywords(item.get("keywords"), seg.text, facts)
        stat = None
        if item.get("stat"):
            stat, dropped = ground_stat(item.get("stat"), facts)
        if lines or stat:
            overlay = {"kind": "hook", "lines": lines}
            if stat:
                overlay["stat"] = {"label": stat["label"], "value": stat["value"]}
        else:
            # 关键词和数字都核对不过:没有可弹的字,退回 broll
            layout = SAFE_LAYOUT
    elif layout == "concept":
        raw = item.get("concept")
        scene = str(raw.get("scene") or "").strip() if isinstance(raw, dict) else ""
        raw_rows = raw.get("rows") if isinstance(raw, dict) else None
        if scene not in profile.vocab.concept_scenes:
            dropped = [f"concept 模板 {scene or '(空)'} 不在可选模板里"]
            rescued = _panel_from_rows(raw_rows, facts)
            if rescued:
                overlay, layout = rescued, "panel"
            else:
                layout = SAFE_LAYOUT
        else:
            kind = "compare" if scene == "compare" else "spec"
            grounded, dropped = cards.ground(cards.normalise(raw, kind), facts)
            if grounded:
                overlay = {**grounded, "kind": "concept", "scene": scene,
                           "rows": grounded["rows"][:4]}
            else:
                rescued = _panel_from_rows(raw_rows, facts)
                if rescued:
                    overlay, layout = rescued, "panel"
                else:
                    layout = SAFE_LAYOUT
    elif layout == "card":
        raw_card = item.get("card")
        kind = sec.card or (raw_card.get("kind") if isinstance(raw_card, dict) else "") or "spec"
        card, dropped = cards.ground(cards.normalise(raw_card, kind), facts)
        if card is None:
            # 整屏卡作废(行不够 / 对比卡两栏不齐):同一批行改成左侧面板,信息不丢。
            # 对比卡除外 —— 它的信息天生是两栏的,压成一栏就是误导。
            rows = raw_card.get("rows") if isinstance(raw_card, dict) else None
            rescued = _panel_from_rows(rows, facts) if kind != "compare" else None
            if rescued:
                overlay, layout = rescued, "panel"
            else:
                layout = SAFE_LAYOUT
    if card_log is not None and (dropped or layout != str(item.get("layout") or layout)):
        card_log.append({"idx": i, "section": sec.name, "asked": item.get("layout"),
                         "layout": layout, "dropped": dropped})

    view = _hint(sec.angle, str(item.get("view") or ""), profile.vocab)
    visual = str(item.get("visual") or "").strip()[:30] or (sec.beat or f"{seg.subject or subject} 宣传画面")
    return ShotSpec(
        idx=i, duration=0.0, section=sec.name,
        visual_zh=f"[{layout}] {visual}",
        camera_zh="", ambience_zh="", narration_zh=seg.text,
        angle=sec.angle, on_screen_text=[],
        card=card, layout=layout, overlay=overlay,
        chapter=seg.heading or seg.subject or sec.chapter or sec.name, view=view,
        subject=seg.subject,
    )


def auto_panel(seg: ScriptSegment, facts: Sequence[Fact]) -> dict | None:
    """给还剩 broll 的拍自动配一个参数面板。

    判据只有一个:**事实里的数字在这一拍的旁白里说过**。
    脚本里的数字按合规 lint 必须来自它引用的事实,所以"数字对得上"就等于
    "这一拍真的在讲这条事实" —— 不会把别的参数安到这一拍上(那正是复盘里
    「画面和旁白各走各的」的病根)。一条都对不上就不配面板,老老实实留 broll。
    """
    said = numbers_in(seg.text or "")
    if not said:
        return None
    by_id = {f.id: f for f in facts}
    rows: list[dict] = []
    for ref in seg.fact_refs or []:
        f = by_id.get(ref)
        if f is None or not f.enabled or f.confidence == "low":
            continue
        value = _fact_value(f.claim, said)
        if not value:
            continue
        rows.append({"label": _fact_label(f), "values": [value], "fact_id": f.id})
        if len(rows) >= 4:
            break
    if not rows:
        return None
    return _panel_from_rows(rows, facts)


def rescue_broll(specs: list[ShotSpec], segments: Sequence[ScriptSegment],
                 facts: Sequence[Fact], profile: CategoryProfile) -> list[ShotSpec]:
    """把还剩 broll 的拍救成参数面板。

    为什么要有这一步:实测九成 broll 是分镜自己选的 —— 提示词原来写着
    「全片大约四成是 broll」,而事实清单是全片一起给的,模型猜不准哪条参数
    属于哪一拍,猜不准就退一步选 broll(核对失败退下来的只有两三拍)。
    复盘要求 broll 压到三成以内,光改提示词不保险,这里用脚本引用过的事实兜底:
    这一拍讲到了哪个数字,就把那个数字放上屏。
    """
    for n, spec in enumerate(specs):
        if spec.layout != SAFE_LAYOUT or n >= len(segments):
            continue
        sec = profile.section(spec.section)
        if "panel" not in (sec.layouts or ()):
            continue  # 这一段的视觉语法里没有面板,不硬塞
        overlay = auto_panel(segments[n], facts)
        if overlay:
            spec.layout = "panel"
            spec.overlay = overlay
            spec.visual_zh = re.sub(r"^\[[^\]]*\]", "[panel]", spec.visual_zh)
    return specs


def _headline_value(claim: str) -> str:
    """事实原文里第一个带单位的数字(没有带单位的,取第一个数字)。取不到返回空串。"""
    first = ""
    for m in re.finditer(r"\d+(?:\.\d+)?", claim or ""):
        tail = claim[m.end():]
        for u in _UNITS:
            if tail.startswith(u):
                return m.group(0) + u
        first = first or m.group(0)
    return first


def _cap_rows(seg: ScriptSegment, facts: Sequence[Fact], comparison: bool,
              offset: int) -> list[dict]:
    """这一拍引用过的事实里,挑出能上屏的行。

    和 `auto_panel` 的区别:不要求数字在旁白里说过 —— 推导数字(「差了 228 公里」)
    和观点拍(「这钱花得值」)说的话对不上事实原文的取值,但它引用的事实就是它的依据。
    `offset` 让同一批引用连着几拍时各拍轮着展示不同的事实,不重复同一屏。
    """
    by_id = {f.id: f for f in facts}
    pool: list[tuple[Fact, str]] = []
    for ref in seg.fact_refs or []:
        f = by_id.get(ref)
        if f is None or not f.enabled or f.confidence == "low":
            continue
        if f.compare_with and not comparison:
            continue
        value = _headline_value(f.claim)
        if value:
            pool.append((f, value))
    if not pool:
        return []
    k = offset % len(pool)
    pool = pool[k:] + pool[:k]
    return [{"label": _fact_label(f), "values": [v], "fact_id": f.id} for f, v in pool[:2]]


def enforce_broll_cap(specs: list[ShotSpec], segments: Sequence[ScriptSegment],
                      facts: Sequence[Fact], profile: CategoryProfile) -> list[ShotSpec]:
    """broll 拍数超三成上限时,把超出的那部分换成它引用过的事实的面板。

    顺序:先旁白带(推导)数字的拍,再没数字但有引用的拍;每档里先隔拍转换
    (不连着出现一样的画面,也缩短静默区间),不够再放开相邻限制。视觉语法里没有
    面板的段(竞品对比 / 结论 / 开场)只在前面都做完仍超标时才破例,
    且只展示这一拍自己引用的事实。核对不过(`cards.ground`)的行不上屏。
    """
    cap = max(1, int(len(specs) * rhythm.BROLL_RATIO_MAX))
    excess = sum(1 for s in specs if s.layout == SAFE_LAYOUT) - cap
    forced: set[int] = set()

    def candidates(tier_numeric: bool, grammar_ok: bool) -> list[int]:
        out = []
        for n, s in enumerate(specs):
            if s.layout != SAFE_LAYOUT or n >= len(segments) or not segments[n].fact_refs:
                continue
            has_panel = "panel" in (profile.section(s.section).layouts or ())
            if has_panel != grammar_ok:
                continue
            if bool(numbers_in(segments[n].text or "")) != tier_numeric:
                continue
            out.append(n)
        return out

    def convert(n: int) -> bool:
        seg, spec = segments[n], specs[n]
        from .script_gen import _is_comparison_section
        comparison = _is_comparison_section(spec.section)
        offset = 0
        m = n - 1
        while m >= 0 and m in forced and specs[m].section == spec.section:
            offset += 1
            m -= 1
        # 前一拍已经是同一批引用的面板:轮换行;和前后任一拍的画面指纹一样就放弃
        rows = _cap_rows(seg, facts, comparison, offset)
        overlay = _panel_from_rows(rows, facts) if rows else None
        if not overlay:
            return False
        trial = spec.model_copy(update={"layout": "panel", "overlay": overlay, "card": None})
        sig = rhythm._overlay_signature(trial)
        if any(0 <= m < len(specs) and rhythm._overlay_signature(specs[m]) == sig
               for m in (n - 1, n + 1)):
            return False
        spec.layout, spec.overlay, spec.card = "panel", overlay, None
        spec.visual_zh = re.sub(r"^\[[^\]]*\]", "[panel]", spec.visual_zh)
        forced.add(n)
        return True

    def run_len(n: int) -> int:
        """第 n 拍所在的连续 broll 串有多长。"""
        lo = hi = n
        while lo > 0 and specs[lo - 1].layout == SAFE_LAYOUT:
            lo -= 1
        while hi + 1 < len(specs) and specs[hi + 1].layout == SAFE_LAYOUT:
            hi += 1
        return hi - lo + 1

    def ordered(tier_numeric: bool, grammar_ok: bool) -> list[int]:
        # 长 broll 串里的拍优先:整段静默比零星几拍更伤节奏
        return sorted(candidates(tier_numeric, grammar_ok), key=lambda n: (-run_len(n), n))

    for grammar_ok in (True, False):
        for tier_numeric in (True, False):
            for spaced in (True, False):
                if excess <= 0:
                    break
                for n in ordered(tier_numeric, grammar_ok):
                    if excess <= 0:
                        break
                    if specs[n].layout != SAFE_LAYOUT:
                        continue
                    if spaced and ((n - 1) in forced or (n + 1) in forced):
                        continue
                    if convert(n):
                        excess -= 1
    # 上限够了也不放过过长的 broll 串:连着 4 拍以上没有任何叠加层,
    # 就是整段静默(复盘:每 5–6 秒必须有一次画面变化)—— 串中间的拍破例转换
    for _ in range(len(specs)):
        longest = [n for n in range(len(specs))
                   if specs[n].layout == SAFE_LAYOUT and run_len(n) >= MAX_BROLL_RUN]
        if not longest:
            break
        longest.sort(key=lambda n: (-run_len(n), n))
        for n in longest:
            lo = n
            while lo > 0 and specs[lo - 1].layout == SAFE_LAYOUT:
                lo -= 1
            if n - lo >= 1 and n < len(segments) and segments[n].fact_refs and convert(n):
                break
        else:
            break
    return specs


def _demote(spec: ShotSpec, profile: CategoryProfile | None = None) -> None:
    """把这一拍降级:能用已核过的数据改成面板就改,否则退回 broll。

    降级不是直接扔回 broll —— 这一拍的数据都已经核对过了,改成左侧面板照样上屏
    (前提是这一段的视觉语法里允许面板)。扔掉就是白扔,broll 占比就是这么滚上去的。
    """
    if profile is not None:
        sec = profile.section(spec.section)
        if "panel" in (sec.layouts or ()):
            ov = _as_panel_overlay(spec)
            if ov:
                spec.layout = "panel"
                spec.overlay = ov
                spec.card = None
                spec.visual_zh = re.sub(r"^\[[^\]]*\]", "[panel]", spec.visual_zh)
                return
    spec.layout, spec.overlay, spec.card = SAFE_LAYOUT, None, None
    spec.visual_zh = re.sub(r"^\[[^\]]*\]", f"[{SAFE_LAYOUT}]", spec.visual_zh)


def enforce_rhythm(specs: list[ShotSpec], profile: CategoryProfile | None = None
                   ) -> list[ShotSpec]:
    """连续超过 MAX_CARD_RUN 拍整屏卡片时,把多出来的那一拍降级;
    连着两拍大数字也把后一拍降级(一个接一个滚数字像在念参数表);
    概念动画同理不连着放,全片超过 MAX_CONCEPT 拍的也降级。

    降级优先改成参数面板(见 `_demote`),实在没有数据才退回 broll。
    """
    run = 0
    prev = ""
    concepts = 0
    for pos, s in enumerate(specs):
        if s.layout == "hook" and pos > 0:
            _demote(s, profile)
        if s.layout in ("stat", "concept") and prev == s.layout:
            _demote(s, profile)
        if s.layout == "concept":
            concepts += 1
            if concepts > MAX_CONCEPT:
                _demote(s, profile)
        prev = s.layout
        if s.layout == "card":
            run += 1
            if run > MAX_CARD_RUN:
                _demote(s, profile)
                run = 0
        else:
            run = 0
    return specs


def assign_broll_styles(specs: list[ShotSpec]) -> list[ShotSpec]:
    """给 broll 拍轮换样式(确定性,不经 LLM):全屏 → 左上角角标 → 分屏 → …

    - 分屏不连着两拍(相邻 broll 都分屏就成了监控墙);
    - 每章第一拍开头已经有章节标牌占着左上角,不再放角标;
    - 别的版式的 overlay 不动;broll 的样式记在 overlay = {"kind": "broll", "style": ...}。
    - 轮换顺序跟着当前视觉包(暗场电影多全屏、杂志编排多分屏),没声明就用 BROLL_STYLES。
    """
    cycle = looks.current().broll_cycle or BROLL_STYLES
    k = 0
    prev_split = False
    for n, s in enumerate(specs):
        if s.layout != "broll":
            prev_split = False
            continue
        style = cycle[k % len(cycle)]
        k += 1
        chapter = s.chapter or s.section
        first = n == 0 or (specs[n - 1].chapter or specs[n - 1].section) != chapter
        if (style == "split" and prev_split) or (style == "lower_third" and first):
            style = "plain"
        s.overlay = None if style == "plain" else {"kind": "broll", "style": style}
        prev_split = style == "split"
    return specs


def broll_style(spec: ShotSpec) -> str:
    if spec.layout != "broll" or not isinstance(spec.overlay, dict):
        return "plain"
    style = spec.overlay.get("style")
    return style if style in BROLL_STYLES else "plain"


def fallback_beats(*, subject: str, profile: CategoryProfile,
                   segments: Sequence[ScriptSegment],
                   subjects: Sequence[str] | None = None) -> list[ShotSpec]:
    """没有 LLM 时:全部 broll。结构、时间轴、合成链路照样能验。"""
    out = []
    for i, seg in enumerate(segments):
        sec = profile.section(seg.section)
        out.append(ShotSpec(idx=i, duration=0.0, section=sec.name,
                            visual_zh=f"[{SAFE_LAYOUT}] {sec.beat or seg.subject or subject}",
                            narration_zh=seg.text, angle=sec.angle, layout=SAFE_LAYOUT,
                            chapter=seg.heading or seg.subject or sec.chapter or sec.name,
                            view=_hint(sec.angle, "", profile.vocab), subject=seg.subject))
    return assign_broll_styles(out)


def beat_subject(spec: ShotSpec, subjects: Sequence[str], fallback: str) -> str:
    """这一拍画面上该是哪台车(背景素材、车名角标、卡片底图都按它取)。

    - 拍上标了车:就是它;
    - 横评的开场 / 结论这类共用拍:按拍号在车单里轮换 —— 背景永远是车单上的车,
      不会出现选题名去查素材池查个空;
    - 单车项目:项目主体。"""
    if spec.subject:
        return spec.subject
    if subjects:
        # 共用拍(对决的每一章、横评的开场结论)旁白只点了一台车的名字,就配那台车:
        # 「Model Y 的后排更宽」配着 YU7 的画面,观众会以为在说 YU7
        named = [s for s in subjects if _names_car(spec.narration_zh, s)]
        if len(named) == 1:
            return named[0]
        return subjects[spec.idx % len(subjects)]
    return fallback


def _names_car(text: str, name: str) -> bool:
    """旁白里提没提到这台车:全名,或车名里最长的字母数字串(「YU7」「Model Y」→「Model」不算)。"""
    text = text or ""
    if name and name in text:
        return True
    tokens = [t for t in re.findall(r"[A-Za-z0-9]+(?: [A-Za-z0-9]+)*", name or "") if len(t) >= 2]
    return any(t in text for t in tokens if any(c.isdigit() for c in t) or " " in t or len(t) >= 4)


def validate_beats(specs: Sequence[ShotSpec]) -> list[str]:
    errors: list[str] = []
    if not specs:
        errors.append("没有任何节拍")
    for s in specs:
        if s.layout not in LAYOUTS:
            errors.append(f"第 {s.idx} 拍版式非法:{s.layout!r}")
        if not s.narration_zh.strip():
            errors.append(f"第 {s.idx} 拍没有旁白(时长由旁白决定,空拍无法定长)")
        if s.layout == "card" and not s.card:
            errors.append(f"第 {s.idx} 拍是 card 但没有卡片数据")
        if s.layout in ("panel", "highlight", "stat", "concept", "hook") and not s.overlay:
            errors.append(f"第 {s.idx} 拍是 {s.layout} 但没有叠加数据")
    return errors


# --------------------------------------------------------------------------
# 定时长:先 TTS,再按旁白回填每拍时长
# --------------------------------------------------------------------------
def beat_durations(cues: Sequence[Any], count: int, gap: float | None = None) -> list[float]:
    """按字幕 cue 把句子时长(含句间停顿)归到各拍。

    与 `narration._concat_audio` 同口径:旁白轨 = Σ(句长 + 停顿),所以
    Σ 各拍时长 == 旁白轨总长,画面和声音不会越走越偏。
    """
    gap_s = config.SENTENCE_GAP_MS / 1000.0 if gap is None else gap
    out = [0.0] * count
    for c in cues:
        if 0 <= c.shot_idx < count:
            out[c.shot_idx] += (c.end - c.start) + gap_s
    return [round(x, 3) for x in out]


def time_beats(project_id: str, specs: list[ShotSpec], profile: CategoryProfile
               ) -> tuple[list[ShotSpec], narration.NarrationResult]:
    """跑 TTS(含字幕与 BGM),把每拍时长写回分镜。"""
    project = db.get_project(project_id)
    nar = narration.build(project_id, specs, profile=profile,
                          size=aspect.frame_size(project), line_chars=aspect.line_chars(project))
    if not nar.ok or not nar.cues:
        raise RuntimeError("旁白生成失败:" + "; ".join(nar.warnings or ["未知原因"]))
    if nar.missing_sentences:
        # 解说轨的时长由旁白决定:缺句子就是缺画面(实测丢的正好是结尾的结论段)。
        # 宁可停在这里让人再点一次确认,也不出一部没有结论的片子
        raise RuntimeError("旁白有句子没合成出来,请重新确认门禁 A 重试:"
                           + "; ".join(nar.warnings))
    if nar.qa_failed:
        # 语音质检:自动重合成后仍读错 / 错位 / 哑句的句子,不带着坏音频去渲染
        why = {f["index"]: f["reason"] for f in nar.qa_report.get("findings", [])
               if f.get("level") == "error"}
        detail = ";".join(f"第 {i + 1} 句 {why.get(i, '不合格')}" for i in nar.qa_failed[:8])
        raise RuntimeError(f"旁白质检未通过:{detail}。已自动重合成仍不合格,请改写这几句后重新确认门禁 A")
    durations = beat_durations(nar.cues, len(specs))
    for s, d in zip(specs, durations):
        s.duration = d
    mark_reveal_times(specs, nar.cues)
    return specs, nar


def mark_reveal_times(specs: Sequence[ShotSpec], cues: Sequence[Any]) -> None:
    """concept 拍的每行参数记下旁白说到它的时刻(`rows[k].at`,相对拍头的秒数),
    动画里这一行就在那一刻出现 —— 参数跟着嘴说的走,而不是按固定间隔一条条排出来。"""
    by_shot: dict[int, list[Any]] = {}
    for c in cues:
        by_shot.setdefault(c.shot_idx, []).append(c)
    for s in specs:
        if s.layout != "concept" or not s.overlay or not by_shot.get(s.idx):
            continue
        mine = sorted(by_shot[s.idx], key=lambda c: c.start)
        rows = list(s.overlay.get("rows") or [])
        times = motion.row_reveal_times(rows, mine, mine[0].start, s.duration)
        new_rows = []
        for r, at in zip(rows, times):
            r = {k: v for k, v in r.items() if k != "at"}
            if at is not None:
                r["at"] = at
            new_rows.append(r)
        s.overlay = {**s.overlay, "rows": new_rows}


# --------------------------------------------------------------------------
# 渲染
# --------------------------------------------------------------------------
def _ff(args: Sequence[str], timeout: int = 600) -> subprocess.CompletedProcess:
    return render_mod._run(["-y", "-v", "error", *args], timeout=timeout)


#: 黑边小于这个比例就不裁(压缩噪点、一两像素的边)
_BARS_MIN_RATIO = 0.04


@functools.lru_cache(maxsize=256)
def letterbox_crop(path: str) -> str:
    """源视频自带的黑边(宽银幕官方片上下各一条):返回 `crop=w:h:x:y`,没有就返回空串。

    不裁的话,黑边会被当作画面缩进 16:9 画幅,成片里车被压在一条窄带里。
    `reset=0` 让检测框只增不减,整片取各帧非黑区域的并集 —— 夜景、暗场单帧会被
    误判成黑边,取并集就不会把暗画面裁掉。按文件缓存:同一个源只测一次。
    """
    ffmpeg = str(config.FFMPEG_PATH or "ffmpeg")
    try:
        proc = subprocess.run(
            [ffmpeg, "-v", "info", "-t", "180", "-i", path, "-an",
             "-vf", "fps=1,cropdetect=limit=24:round=2:reset=0:skip=0", "-f", "null", "-"],
            capture_output=True, text=True, timeout=300, encoding="utf-8", errors="replace")
    except (OSError, subprocess.SubprocessError):
        return ""
    last = re.findall(r"crop=(\d+):(\d+):(\d+):(\d+)", proc.stderr or "")
    size = re.search(r"Stream #0:\d+.*?Video:.*?, (\d{3,5})x(\d{3,5})", proc.stderr or "")
    if not last or not size:
        return ""
    cw, ch, cx, cy = (int(x) for x in last[-1])
    iw, ih = int(size.group(1)), int(size.group(2))
    if cw <= 0 or ch <= 0 or cw > iw or ch > ih:
        return ""
    if (iw - cw) / iw < _BARS_MIN_RATIO and (ih - ch) / ih < _BARS_MIN_RATIO:
        return ""
    return f"crop={cw}:{ch}:{cx}:{cy}"


def _fit_filter(w: int, h: int, crop_bottom: float = 0.0, crop_edge: float = 0.0,
                bars: str = "") -> str:
    """任意比例的视频放进 w×h:前景完整缩进画幅,空出的地方用同一画面放大模糊铺满。
    横屏源基本铺满,竖屏抖音片也不会被裁掉车头车尾。

    `crop_bottom` > 0(烧录字幕段):裁掉底部这一截后放大**铺满**画幅,不留模糊边 ——
    这类段基本是横屏评测,裁完再缩进画幅会上下各空出一条模糊带,一眼就是裁过的。
    `crop_edge` > 0(第三方视频):先把四边各收一半这个比例,角上的台标就出了画。
    `bars`:源自带的黑边(见 `letterbox_crop`),最先裁掉,后面的比例都按裁完的画面算。"""
    pre = f"{bars}," if bars else ""
    edge = (f"crop=trunc(iw*{1 - crop_edge:.3f}/2)*2:trunc(ih*{1 - crop_edge:.3f}/2)*2,"
            if crop_edge > 0 else "")
    if crop_bottom > 0:
        return (f"[0:v]{pre}crop=iw:trunc(ih*{1 - crop_bottom:.3f}/2)*2:0:0,{edge}"
                f"scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h},"
                f"setsar=1,fps={render_mod.FPS},format=yuv420p")
    return (f"[0:v]{pre}{edge}split[a][b];[a]scale={w}:{h}:force_original_aspect_ratio=increase,"
            f"crop={w}:{h},boxblur=20:2[bg];[b]scale={w}:{h}:force_original_aspect_ratio=decrease[fg];"
            f"[bg][fg]overlay=(W-w)/2:(H-h)/2,setsar=1,fps={render_mod.FPS},format=yuv420p")


def _piece_clip(piece: library.Piece, dest: Path, size: tuple[int, int], motion: str) -> Path:
    w, h = size
    if piece.kind == "still":
        return render_mod.still_to_clip(piece.path, dest, seconds=piece.length,
                                        motion=motion, size=size)
    # 视频不够长(原片在这一段后面就结束了)时,用最后一帧补齐。
    # 竖屏也照常裁烧录字幕:实拍只放在 16:9 的画面带里,和横屏是同一个比例
    vf = _fit_filter(w, h, piece.crop_bottom, piece.crop_edge,
                     letterbox_crop(piece.path)) + f",tpad=stop_mode=clone:stop_duration={piece.length:.3f}[v]"
    proc = _ff(["-ss", f"{piece.start:.3f}", "-i", piece.path, "-filter_complex", vf,
                "-map", "[v]", "-t", f"{piece.length:.3f}", "-an", *_ENC, str(dest)])
    if proc.returncode != 0 or not dest.is_file():
        raise render_mod.RenderError(f"背景片段截取失败:{(proc.stderr or '')[-300:]}")
    return dest


def _solid(dest: Path, seconds: float, size: tuple[int, int]) -> Path:
    w, h = size
    proc = _ff(["-f", "lavfi", "-i", f"color=c=0x101826:s={w}x{h}:r={render_mod.FPS}",
                "-t", f"{seconds:.3f}", "-an", *_ENC, str(dest)])
    if proc.returncode != 0:
        raise render_mod.RenderError(f"纯色底生成失败:{(proc.stderr or '')[-300:]}")
    return dest


def motion_for(item_id: str, n: int, prev: str = "") -> str:
    """静图运镜:按片段 id 散开挑(同一张图在不同拍里动法也不同),不与上一刀相同。"""
    k = (zlib.crc32(item_id.encode("utf-8")) + n) % len(STILL_MOTIONS)
    motion = STILL_MOTIONS[k]
    if motion == prev:
        motion = STILL_MOTIONS[(k + 1) % len(STILL_MOTIONS)]
    return motion


def xfade_offsets(lengths: Sequence[float]) -> list[float]:
    """各段原长 → 每个转场的起点。除最后一段外每段都加长 XFADE 去和下一段重叠,
    所以第 k 个转场正好从前 k 段原长之和开始,拼完的总长仍是原长之和。"""
    out, acc = [], 0.0
    for length in lengths[:-1]:
        acc += length
        out.append(round(acc, 3))
    return out


def _xfade(parts: Sequence[Path], lengths: Sequence[float], seconds: float, dest: Path,
           seed: int) -> Path:
    inputs: list[str] = []
    for p in parts:
        inputs += ["-i", str(p)]
    chain, prev = [], "[0:v]"
    for k, offset in enumerate(xfade_offsets(lengths), start=1):
        kind = XFADE_KINDS[(seed + k) % len(XFADE_KINDS)]
        label = f"[x{k}]"
        chain.append(f"{prev}[{k}:v]xfade=transition={kind}:duration={XFADE}:offset={offset:.3f}{label}")
        prev = label
    graph = ";".join(chain) + f";{prev}format=yuv420p[v]"
    proc = _ff([*inputs, "-filter_complex", graph, "-map", "[v]", "-t", f"{seconds:.3f}",
                "-an", *_ENC, str(dest)])
    if proc.returncode != 0 or not dest.is_file():
        raise render_mod.RenderError(f"背景转场失败:{(proc.stderr or '')[-300:]}")
    return dest


def background_clip(pieces: Sequence[library.Piece], seconds: float, size: tuple[int, int],
                    work: Path, motion_seed: int = 0) -> Path:
    """把几段背景拼成一条定长背景:段与段之间 xfade 转场(失败退回硬切拼接)。
    没有片段时给纯色底(不应发生,但不能卡死流水线)。"""
    work.mkdir(parents=True, exist_ok=True)
    dest = work / "bg.mp4"
    if not pieces:
        return _solid(dest, seconds, size)
    lengths = [p.length for p in pieces]
    blend = len(pieces) >= 2 and min(lengths) >= XFADE * 2
    parts, prev = [], ""
    for n, p in enumerate(pieces):
        motion = motion_for(p.item_id, motion_seed + n, prev)
        prev = motion
        extra = XFADE if blend and n < len(pieces) - 1 else 0.0
        parts.append(_piece_clip(replace(p, length=round(p.length + extra, 3)),
                                 work / f"piece_{n:02d}.mp4", size, motion))
    if len(parts) == 1:
        shutil.copyfile(parts[0], dest)
        return dest
    if blend:
        try:
            return _xfade(parts, lengths, seconds, dest, motion_seed)
        except render_mod.RenderError:
            pass  # 退回硬切:每段多出的 XFADE 秒被 -t 截在结尾,画面只是略早切
    listing = render_mod.timeline_mod.concat_list_file(parts, work / "pieces.txt")
    proc = _ff(["-f", "concat", "-safe", "0", "-i", str(listing), "-c", "copy",
                "-t", f"{seconds:.3f}", str(dest)])
    if proc.returncode != 0 or not dest.is_file():
        raise render_mod.RenderError(f"背景拼接失败:{(proc.stderr or '')[-300:]}")
    return dest


def footage_size(size: tuple[int, int]) -> tuple[int, int]:
    """实拍按什么尺寸渲。横屏 = 整个画面;竖屏 = 三段式中间的画面带(1080×608,16:9)。

    竖屏的实拍**先按画面带渲**(横屏那套背景逻辑原样复用:拼接、转场、运镜、
    裁烧录字幕、分屏),再由 `band_frame` 放进竖屏画布。"""
    if aspect.is_portrait_size(size):
        return aspect.portrait_grid(size).band_size
    return size


def band_frame(band_clip: Path, size: tuple[int, int], dest: Path) -> Path:
    """画面带放进竖屏画布:同一段画面放大模糊压暗铺满全幅,清晰的 16:9 画面压在画面带上。
    上下不是黑边,而是同一画面的延伸,标题区和数据区的字压在上面也看得清。"""
    w, h = size
    g = aspect.portrait_grid(size)
    graph = (f"[0:v]split[a][b];[a]scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h},"
             f"boxblur=30:2,eq=brightness=-0.24:saturation=0.8[bg];"
             f"[bg][b]overlay=0:{g.band.top},setsar=1,fps={render_mod.FPS},format=yuv420p[v]")
    proc = _ff(["-i", str(band_clip), "-filter_complex", graph, "-map", "[v]", "-an", *_ENC, str(dest)])
    if proc.returncode != 0 or not dest.is_file():
        raise render_mod.RenderError(f"竖屏画面带合成失败:{(proc.stderr or '')[-300:]}")
    return dest


def _framed(clip: Path, size: tuple[int, int], work: Path) -> Path:
    """竖屏时把画面带包成整幅画面;横屏原样返回。"""
    if not aspect.is_portrait_size(size):
        return clip
    return band_frame(clip, size, work / f"{clip.stem}_framed.mp4")


#: 概念动画的实拍底从外观组里挑(行驶、航拍也在这组),车内镜头压上数据不好看
CONCEPT_FOOTAGE_HINT = "exterior"


def concept_background(piece: library.Piece, seconds: float, size: tuple[int, int], work: Path) -> Path:
    """概念动画的底:一个长镜头(`library.long_take`),比整拍短就放慢铺满。
    放慢的行车镜头本身有质感,也省掉压着数据时底在跳切的乱。"""
    work.mkdir(parents=True, exist_ok=True)
    dest = work / "bg.mp4"
    w, h = size
    factor = max(1.0, seconds / max(0.1, piece.length))
    vf = (_fit_filter(w, h, piece.crop_bottom, piece.crop_edge,
                      letterbox_crop(piece.path)) + f",setpts={factor:.4f}*PTS,fps={render_mod.FPS},"
          f"tpad=stop_mode=clone:stop_duration={seconds:.3f}[v]")
    proc = _ff(["-ss", f"{piece.start:.3f}", "-t", f"{piece.length:.3f}", "-i", piece.path,
                "-filter_complex", vf, "-map", "[v]", "-t", f"{seconds:.3f}", "-an", *_ENC, str(dest)])
    if proc.returncode != 0 or not dest.is_file():
        raise render_mod.RenderError(f"概念动画底生成失败:{(proc.stderr or '')[-300:]}")
    return dest


def split_filter(count: int, seconds: float, size: tuple[int, int]) -> str:
    """分屏:count 栏不同画面,各取中间一条,从下往上依次滑入(缓出)。"""
    w, h = size
    gap = 12
    pw = (w - gap * (count - 1)) // count // 2 * 2
    parts = [f"color=c=0x070c18:s={w}x{h}:r={render_mod.FPS}:d={seconds:.3f}[s0]"]
    for k in range(count):
        parts.append(f"[{k}:v]crop={pw}:{h}:(iw-{pw})/2:0,setsar=1[p{k}]")
    for k in range(count):
        delay = 0.12 * k
        x = k * (pw + gap)
        y = f"{h}*pow(1-min(1,max(0,(t-{delay:.2f})/0.45)),3)"
        parts.append(f"[s{k}][p{k}]overlay=x={x}:y='{y}':eof_action=repeat[s{k + 1}]")
    parts.append(f"[s{count}]format=yuv420p[v]")
    return ";".join(parts)


def split_clip(clips: Sequence[Path], seconds: float, size: tuple[int, int], dest: Path) -> Path:
    inputs: list[str] = []
    for c in clips:
        inputs += ["-i", str(c)]
    proc = _ff([*inputs, "-filter_complex", split_filter(len(clips), seconds, size),
                "-map", "[v]", "-t", f"{seconds:.3f}", "-an", *_ENC, str(dest)])
    if proc.returncode != 0 or not dest.is_file():
        raise render_mod.RenderError(f"分屏合成失败:{(proc.stderr or '')[-300:]}")
    return dest


def chapters_of(specs: Sequence[ShotSpec]) -> tuple[list[tuple[str, float]], list[int]]:
    """相邻同章的拍合成一章。返回 ([(章名, 时长)], 每拍所在的章序号)。"""
    chapters: list[tuple[str, float]] = []
    where: list[int] = []
    for s in specs:
        name = s.chapter or s.section or "正片"
        if chapters and chapters[-1][0] == name:
            chapters[-1] = (name, chapters[-1][1] + s.duration)
        else:
            chapters.append((name, s.duration))
        where.append(len(chapters) - 1)
    return chapters, where


def layout_filter(layout: str, size: tuple[int, int], has_layer: bool) -> str:
    """把背景 [0]、章节条 [1]、叠加层 [2] 合成 [out] 的滤镜图。

    `layout` 除了版式名,还可以是 broll 的样式 `lower_third`(角标从左擦入)。"""
    w, h = size
    if layout == "highlight" and has_layer and aspect.is_portrait_size(size):
        # 竖屏:实拍本来就在画面带里,不用再缩小窗;关键词占标题区,淡入
        return (
            f"[2:v]format=rgba,fade=in:st=0.15:d=0.35:alpha=1[txt];"
            f"[0:v][txt]overlay=0:0[v1];[v1][1:v]overlay=0:0,format=yuv420p[out]"
        )
    if layout == "highlight" and has_layer:
        pip_w = int(w * 0.62) // 2 * 2
        pip_h = int(h * 0.62) // 2 * 2
        # 模糊底用同一段画面的镜像放大版:和小窗里的画面错开,不像同一张图叠了两层
        return (
            f"[0:v]split[a][b];[a]hflip,scale=iw*1.3:-2,crop={w}:{h},"
            f"boxblur=22:2,eq=brightness=-0.18[bgb];"
            f"[b]scale={pip_w}:{pip_h},pad={pip_w + 12}:{pip_h + 12}:6:6:color=white[pip];"
            f"[bgb][pip]overlay=(W-w)/2:{int(h * 0.05)}[v1];"
            f"[2:v]format=rgba,fade=in:st=0.15:d=0.35:alpha=1[txt];"
            f"[v1][txt]overlay=0:0[v2];[v2][1:v]overlay=0:0,format=yuv420p[out]"
        )
    if layout == "hook" and has_layer:
        # [2] 是大字弹出关键帧(播完透明);前 3 秒压暗托字、不放章节条,之后条再淡入
        t = HOOK_SECONDS
        return (
            f"[0:v]eq=brightness=-0.14:enable='lt(t,{t})'[bgd];[2:v]format=rgba[txt];"
            f"[bgd][txt]overlay=0:0:eof_action=pass[v1];"
            f"[1:v]format=rgba,fade=in:st={t}:d=0.4:alpha=1[bar];"
            f"[v1][bar]overlay=0:0,format=yuv420p[out]"
        )
    if layout == "stat" and has_layer:
        # [2] 是逐帧滚动的数字(concat 关键帧),播完停在最后一帧
        return (
            f"[0:v]eq=brightness=-0.16:saturation=0.85[bgd];[2:v]format=rgba[num];"
            f"[bgd][num]overlay=0:0:eof_action=repeat[v1];"
            f"[v1][1:v]overlay=0:0,format=yuv420p[out]"
        )
    if layout == "panel" and has_layer:
        slide = int(w * 0.36)
        return (
            f"[0:v][2:v]overlay=x='-{slide}*(1-min(1,t/0.35))':y=0[v1];"
            f"[v1][1:v]overlay=0:0,format=yuv420p[out]"
        )
    if layout == "lower_third" and has_layer:
        slide = int(w * 0.45)
        return (
            f"[0:v][2:v]overlay=x='-{slide}*pow(1-min(1,max(0,(t-0.25)/0.4)),3)':y=0[v1];"
            f"[v1][1:v]overlay=0:0,format=yuv420p[out]"
        )
    return "[0:v][1:v]overlay=0:0,format=yuv420p[out]"


def with_sting(graph: str, index: int) -> str:
    """在合成好的 [out] 上再叠章节标牌(输入 `index`,关键帧播完就透明露出下面)。"""
    head, _, _ = graph.rpartition("[out]")
    return (f"{head}[pre];[{index}:v]format=rgba[st];"
            f"[pre][st]overlay=0:0:eof_action=pass,format=yuv420p[out]")


def _frames_listing(frames: Sequence[tuple[Path, float]], listing: Path,
                    hold_to: float | None = None) -> Path:
    """关键帧写成 concat 清单。`hold_to` 给了就把最后一帧定格到这个总时长。"""
    lines: list[str] = []
    rolled = 0.0
    for n, (path, dur) in enumerate(frames):
        if hold_to is not None and n == len(frames) - 1:
            dur = max(0.5, hold_to - rolled)
        rolled += dur
        lines += [f"file '{path.as_posix()}'", f"duration {dur:.3f}"]
    lines.append(f"file '{frames[-1][0].as_posix()}'")  # concat 要求末帧重复一次
    listing.write_text("\n".join(lines), encoding="utf-8")
    return listing


def _stat_listing(overlay: dict, subject: str, size: tuple[int, int], work: Path,
                  seconds: float) -> Path:
    """大数字滚动的关键帧写成 concat 清单:滚完后最后一帧定格到拍尾。"""
    frames = overlays.stat_frames(overlay, subject, size, work / "stat")
    return _frames_listing(frames, work / "stat.txt", hold_to=seconds)


def concept_card(overlay: dict) -> dict:
    """概念动画渲染不了时退回的数据卡(同一批核对过的行)。"""
    kind = "compare" if overlay.get("scene") == "compare" else "spec"
    return {"kind": kind, "title": overlay.get("title") or "", "rows": overlay.get("rows") or [],
            "columns": list(overlay.get("columns") or []), "source": overlay.get("source", "")}


def _broll_base(spec: ShotSpec, subject: str, seconds: float, size: tuple[int, int], work: Path,
                used: dict[str, int], recent: list[str], card_bg: Path | None
                ) -> tuple[Path, list[library.Piece]]:
    """broll / panel / highlight / stat 的背景。分屏样式铺 2–3 条不同的背景再并排。

    `detail` 把这一拍的旁白交给素材池:讲刹车时别配一个门把手特写
    (特写太抢戏,观众会以为屏幕上那个部件就是正在讲的东西)。
    """
    detail = spec.narration_zh or spec.visual_zh or ""
    if broll_style(spec) == "split" and seconds >= SPLIT_MIN_SECONDS:
        count = 3 if seconds <= 5 else 2
        clips, pieces = [], []
        for k in range(count):
            got = library.fill(subject, seconds, hint=spec.view, used=used, recent=recent,
                               detail=detail)
            if not got:
                break
            pieces += got
            clips.append(background_clip(got, seconds, size, work / f"panel_{k}",
                                         motion_seed=spec.idx * 7 + k))
        if len(clips) >= 2:
            return split_clip(clips, seconds, size, work / "split.mp4"), pieces
        if clips:
            return clips[0], pieces
    pieces = library.fill(subject, seconds, hint=spec.view, used=used, recent=recent,
                          detail=detail)
    if not pieces and card_bg:
        pieces = [library.Piece("bg", "still", str(card_bg), 0.0, seconds)]
    return background_clip(pieces, seconds, size, work, motion_seed=spec.idx), pieces


def hook_background(subject: str, seconds: float, size: tuple[int, int], work: Path,
                    used: dict[str, int], recent: list[str], hint: str, seed: int
                    ) -> tuple[Path, list[library.Piece]]:
    """黄金三秒的背景:前几秒每 1 秒一刀硬切(不 xfade),之后接正常背景铺满这一拍。

    整拍只用确认是这台车的素材(`confirmed`):前三秒观众只确认"是不是我要看的那台车",
    配错车或挂着别人的台标就划走了。确认过的一条都没有才退回普通取法。"""
    n = 3 if seconds >= 3.5 else 2 if seconds >= 2.0 else 1
    cuts: list[library.Piece] = []
    for _ in range(n):
        got = (library.fill(subject, HOOK_CUT, hint=hint, used=used, recent=recent, confirmed=True)
               or library.fill(subject, HOOK_CUT, hint=hint, used=used, recent=recent))
        if got:
            cuts.append(replace(got[0], length=HOOK_CUT))
    if not cuts:
        raise render_mod.RenderError("开场没有可用素材")
    head = HOOK_CUT * len(cuts)
    rest = round(seconds - head, 3)
    if rest < 0.2:  # 余下太短就并进最后一刀
        cuts[-1] = replace(cuts[-1], length=round(seconds - HOOK_CUT * (len(cuts) - 1), 3))
        rest = 0.0
    work.mkdir(parents=True, exist_ok=True)
    parts: list[Path] = []
    prev = ""
    for k, p in enumerate(cuts):
        m = motion_for(p.item_id, seed + k, prev)
        prev = m
        parts.append(_piece_clip(p, work / f"cut_{k:02d}.mp4", size, m))
    pieces = list(cuts)
    if rest > 0:
        more = (library.fill(subject, rest, hint=hint, used=used, recent=recent, confirmed=True)
                or library.fill(subject, rest, hint=hint, used=used, recent=recent))
        parts.append(background_clip(more, rest, size, work / "rest", motion_seed=seed + 9))
        pieces += more
    dest = work / "hook_bg.mp4"
    listing = render_mod.timeline_mod.concat_list_file(parts, work / "hook_parts.txt")
    proc = _ff(["-f", "concat", "-safe", "0", "-i", str(listing), "-c", "copy",
                "-t", f"{seconds:.3f}", str(dest)])
    if proc.returncode != 0 or not dest.is_file():
        raise render_mod.RenderError(f"开场背景拼接失败:{(proc.stderr or '')[-300:]}")
    return dest, pieces


def concept_inputs(spec: ShotSpec, subject: str, seconds: float, size: tuple[int, int], work: Path,
                   used: dict[str, int], recent: list[str], style: str | None = None
                   ) -> tuple[Path | None, motion_assets.Cutout | None, list[library.Piece]]:
    """concept 拍的素材:(实拍底, 车图, 用到的片段)。正式渲染和选风格预览共用,保证预览看到的就是成片的样子。
    产品镜(studio)用模板自带的影棚底,不铺实拍;通用模版自带底;其余压在一段实拍上。"""
    scene = (spec.overlay or {}).get("scene", "")
    bg_clip: Path | None = None
    pieces: list[library.Piece] = []
    if motion.scene_background(scene, style) == "footage":
        # 一个长镜头放慢铺满;库里没有够长的才退回多段拼接
        take = library.long_take(subject, seconds, hint=CONCEPT_FOOTAGE_HINT, used=used, recent=recent)
        got = [take] if take else library.fill(subject, seconds, hint=CONCEPT_FOOTAGE_HINT,
                                               used=used, recent=recent)
        if got:
            try:
                fsize = footage_size(size)
                bg_clip = (concept_background(take, seconds, fsize, work) if take else
                           background_clip(got, seconds, fsize, work, motion_seed=spec.idx))
                # 竖屏也给 16:9 的画面带片段:概念动画把它放进自己的虚拟舞台(画面带)里
                pieces = got
            except Exception:
                bg_clip = None
    views = motion.scene_photo_views(scene, style)
    photo = motion_assets.hero_still(subject, views) if views else None
    return bg_clip, photo, pieces


def portrait_concept_style(scene: str, style: str | None,
                           size: tuple[int, int]) -> tuple[str | None, str]:
    """竖屏下这一拍实际用哪套模版:(模版 id 或 None, 降级说明)。

    模版文件头写了 `@portrait yes` 才算有竖屏版式;没适配的模版按竖屏尺寸渲出来
    还是横屏那套排布,会挤、会出界。顺序:选定模版 → 默认模版(场景专用)→ None(退回数据卡)。
    横屏原样返回选定模版。"""
    if not aspect.is_portrait_size(size) or motion.supports_portrait(style, scene):
        return style, ""
    chosen = motion.get_style(style).name
    if motion.supports_portrait(motion.DEFAULT_STYLE, scene):
        return motion.DEFAULT_STYLE, f"模版「{chosen}」还没有竖屏版,改用默认模版"
    return None, f"模版「{chosen}」与默认模版都还没有竖屏版,画成数据卡"


def concept_style_of(project_id: str) -> str:
    """项目选定的概念动画风格(`concept_style.json`);没单独选过 = 视觉包配的那套模版。"""
    got = db.read_json(project_id, "concept_style.json", {}) or {}
    return motion.get_style(got.get("style") or looks.of_project(project_id).concept).id


def render_beat(spec: ShotSpec, *, subject: str, size: tuple[int, int], out_dir: Path,
                chapters: Sequence[tuple[str, float]], chapter_idx: int,
                used: dict[str, int], card_bg: Path | None,
                recent: list[str] | None = None,
                sting: tuple[int, str] | None = None,
                style: str | None = None, headline: str = "",
                notes: list[str] | None = None,
                ranges: dict[str, str] | None = None) -> tuple[Path, list[dict]]:
    """渲染一拍,返回 (成品路径, 用到的背景片段)。

    `recent` 是全片共享的最近用过的片段(相邻多样性,见 `library.fill`);
    `sting` = (章号, 章名) 时在开头叠章节标牌(每章第一拍)。
    `headline` 是竖屏标题区常驻的两行大标题(横屏不用)。
    `notes` 给了就往里记降级说明(竖屏模版没适配 → 换模版 / 退回数据卡),调用方写进项目事件。
    `ranges` 是品类的概念动画量程(`vocab.concept_ranges`),动画里表示数量的图形按它画。

    竖屏(三段式):实拍先按画面带尺寸渲(`footage_size`),再用 `band_frame`
    放进 1080×1920;数据卡和概念动画直接按整幅渲。"""
    seconds = max(0.5, spec.duration)
    recent = recent if recent is not None else []
    work = out_dir / "_work" / f"{spec.idx:03d}"
    if work.exists():
        shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True, exist_ok=True)

    pieces: list[library.Piece] = []
    fsize = footage_size(size)
    #: 竖屏标题区要不要让位:关键词 / 开场大字占它;通用模版的概念动画是整屏设计,也占它
    clear_title = spec.layout in ("highlight", "hook")
    if spec.layout == "hook" and spec.overlay:
        try:
            base, pieces = hook_background(subject, seconds, fsize, work / "hook", used, recent,
                                           spec.view, spec.idx)
            base = _framed(base, size, work)
        except Exception:  # noqa: BLE001 - 冷开场做不出来:同一批关键词退回 highlight 版式
            lines = spec.overlay.get("lines") or []
            fallback = spec.model_copy(update={
                "layout": "highlight" if lines else SAFE_LAYOUT,
                "overlay": {"kind": "highlight", "lines": lines} if lines else None})
            return render_beat(fallback, subject=subject, size=size, out_dir=out_dir,
                               chapters=chapters, chapter_idx=chapter_idx, used=used,
                               card_bg=card_bg, recent=recent, sting=sting, style=style,
                               headline=headline, ranges=ranges)
    elif spec.layout == "card" and spec.card:
        base = cards.render(spec.card, subject, work / "card.mp4", seconds=seconds,
                            background=card_bg, size=size)
    elif spec.layout == "concept" and spec.overlay:
        scene = spec.overlay.get("scene", "")
        use_style, why = portrait_concept_style(scene, style, size)
        if why and notes is not None:
            notes.append(f"第 {spec.idx} 拍概念动画:{why}")
        try:
            if use_style is None:
                raise motion.MotionError(why)
            bg_clip, photo, pieces = concept_inputs(spec, subject, seconds, size, work / "concept_bg",
                                                    used, recent, use_style)
            base = motion.render(scene, motion.scene_data(spec.overlay, subject, ranges), seconds, size,
                                 work / "concept.mp4", background=bg_clip, photo=photo, style=use_style)
            clear_title = motion.get_style(use_style).path is not None
        except motion.MotionError as exc:
            # 动画渲染不了:同一批核对过的行画成数据卡,不让整拍失败 —— 但要留痕,
            # 否则"概念动画全变成数据卡"看起来像正常产出
            _log.warning("第 %s 拍概念动画 %s 退回数据卡:%s", spec.idx, scene, exc)
            base = cards.render(concept_card(spec.overlay), subject, work / "card.mp4",
                                seconds=seconds, background=card_bg, size=size)
    else:
        base, pieces = _broll_base(spec, subject, seconds, fsize, work, used, recent, card_bg)
        base = _framed(base, size, work)

    # 竖屏标题区:大标题让位时只留章节进度
    bar = overlays.chapter_bar(chapters, chapter_idx, size, work / "bar.png",
                               headline="" if clear_title else headline)
    layer: Path | None = None
    layer_input: list[str] = []
    flt = spec.layout
    if spec.layout == "panel" and spec.overlay:
        layer = overlays.panel(spec.overlay, subject, size, work / "layer.png")
    elif spec.layout == "highlight" and spec.overlay:
        layer = overlays.highlight(spec.overlay.get("lines") or [], size, work / "layer.png")
    elif spec.layout == "hook" and spec.overlay:
        frames = overlays.hook_frames(spec.overlay, subject, size, work / "hook_txt",
                                      HOOK_SECONDS)
        layer = _frames_listing(frames, work / "hook_txt.txt")
        layer_input = ["-f", "concat", "-safe", "0", "-i", str(layer)]
    elif spec.layout == "stat" and spec.overlay:
        layer = _stat_listing(spec.overlay, subject, size, work, seconds)
        layer_input = ["-f", "concat", "-safe", "0", "-i", str(layer)]
    elif broll_style(spec) == "lower_third" and not sting:
        name = chapters[chapter_idx][0] if 0 <= chapter_idx < len(chapters) else ""
        if name == subject:  # 横评:章名就是车名,角标改写「车名 · 段落」
            name = spec.section
        layer = overlays.lower_third(subject, name, size, work / "layer.png")
        flt = "lower_third"
    if layer and not layer_input:
        layer_input = ["-loop", "1", "-framerate", str(render_mod.FPS), "-i", str(layer)]

    dest = out_dir / f"{spec.idx:03d}.mp4"
    inputs = ["-i", str(base), "-loop", "1", "-framerate", str(render_mod.FPS), "-i", str(bar)]
    inputs += layer_input
    graph = layout_filter(flt, size, bool(layer))
    if sting:
        frames = overlays.chapter_sting_frames(sting[0], sting[1], size, work / "sting")
        inputs += ["-f", "concat", "-safe", "0", "-i",
                   str(_frames_listing(frames, work / "sting.txt"))]
        graph = with_sting(graph, 3 if layer else 2)
    proc = _ff([*inputs, "-filter_complex", graph,
                "-map", "[out]", "-t", f"{seconds:.3f}", "-an", *_ENC, str(dest)])
    if proc.returncode != 0 or not dest.is_file():
        raise render_mod.RenderError(f"第 {spec.idx} 拍合成失败:{(proc.stderr or '')[-400:]}")
    shutil.rmtree(work, ignore_errors=True)
    return dest, [p.__dict__ for p in pieces]


def sting_for(specs: Sequence[ShotSpec], where: Sequence[int],
              chapters: Sequence[tuple[str, float]], idx: int) -> tuple[int, str] | None:
    """这一拍是不是某章(开头章除外)的第一拍:是就返回 (章号, 章名)。"""
    pos = next((n for n, s in enumerate(specs) if s.idx == idx), None)
    if pos is None or pos == 0:
        return None
    ch = where[pos]
    if ch == 0 or where[pos - 1] == ch:
        return None
    return ch + 1, chapters[ch][0]


# --------------------------------------------------------------------------
# 接入流水线
# --------------------------------------------------------------------------
def beats_dir(project_id: str) -> Path:
    d = config.project_dir(project_id) / "beats"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _load_used(project_id: str) -> dict[str, int]:
    return db.read_json(project_id, "beats_used.json", {}) or {}


def _load_recent(project_id: str) -> list[str]:
    return list(db.read_json(project_id, "beats_recent.json", []) or [])


def card_backgrounds(subject: str) -> list[Path]:
    """数据卡的底图候选:池子里干净的主视角官图(汽车是外观;虚化压暗后做底),
    按视角散开排 —— 各张卡按拍号轮换,不再全片共用一张。"""
    lib = library.Library.load(subject)
    stills = [i for i in lib.items if i.kind == "still" and i.usable and Path(i.path).is_file()]
    primary = library.vocab_of(lib).primary_views
    stills.sort(key=lambda i: (i.view not in primary, library._dirt(i)))
    groups = library.near_dup_groups(stills)
    seen: set[str] = set()
    out: list[Path] = []
    for i in stills:  # 同一簇(几乎同一张)只留一张
        if groups[i.id] not in seen:
            seen.add(groups[i.id])
            out.append(Path(i.path))
    return out


def card_background(subject: str) -> Path | None:
    bgs = card_backgrounds(subject)
    return bgs[0] if bgs else None


def init_beats(project_id: str, specs: Sequence[ShotSpec]) -> None:
    """门禁 A 之后:清掉两档旧记录,按当前分镜重建 shots 行(全部 pending)。"""
    for stage in ("draft", "final"):
        db.delete_shots(project_id, stage)
        db.upsert_shots(project_id, [
            {"idx": s.idx, "stage": stage, "model": "explainer", "provider_task_id": None,
             "submitted_at": None, "last_polled_at": None, "draft_path": None,
             "final_path": None, "error": None}
            for s in specs
        ])


def _shot_status(project_id: str, idx: int) -> str:
    """取某一拍在草稿档的当前状态(找不到就是空串)。"""
    for r in db.get_shots(project_id, "draft"):
        if r["idx"] == idx:
            return str(r.get("status") or "")
    return ""


def render_pending(project_id: str, specs: Sequence[ShotSpec], *, subject: str,
                   profile: CategoryProfile, limit: int = 3,
                   subjects: Sequence[str] | None = None,
                   should_stop: Any = None) -> dict[str, Any]:
    """渲染还没出片的拍(每次最多 `limit` 拍,后台循环反复调用)。

    产物写成 draft 档 succeeded —— 门禁 B 的审片页按草稿档展示。
    `subjects`:多车横评车单 —— 每拍按 `beat_subject` 取自己那台车的素材,绝不借别的车的。
    `should_stop`:可选的回调,返回 True 就**在拍与拍之间收手**。服务停机
    时用它把手头这一拍做完就停下 —— 剩下的拍留到下次,而不是被砍在
    半截(渲染到一半的 mp4 是坏的)。
    """
    subjects = list(subjects or [])
    rows = {r["idx"]: r for r in db.get_shots(project_id, "draft")}
    todo = [s for s in specs if s.duration > 0
            and rows.get(s.idx, {}).get("status") != "succeeded"][:limit]
    chapters, where = chapters_of(specs)
    used = _load_used(project_id)
    recent = _load_recent(project_id)
    beats_dir(project_id)  # 每拍的背景片段记录放这里
    # 成品放 drafts/:审片页按 /media/{项目}/drafts/{文件} 取草稿预览
    out_dir = config.project_dir(project_id) / "drafts"
    bg_cache: dict[str, list[Path]] = {}
    style = concept_style_of(project_id)
    # 画幅是项目属性(创建时定),不是品类属性
    project = db.get_project(project_id) or {}
    size = aspect.frame_size(project)
    # 竖屏标题区常驻的大标题:脚本阶段 LLM 起的片名
    headline = str(project.get("title") or "") if aspect.is_portrait(project) else ""
    done, errors = [], []
    notes: list[str] = []
    for s in todo:
        # 停机信号:在**拍与拍之间**收手。手头这一拍做完就停,剩下的
        # 留到下次 —— 正在渲的 mp4 被砍在中间是坏的。
        if should_stop is not None and should_stop():
            break
        # 逐拍重读一次状态。todo 是开头那份快照 —— 若这一拍在快照之后
        # 已经被渲好了(succeeded),再渲一遍会与它抢同一个输出文件,
        # 产出半截 mp4。重拍(置回 pending)不受影响:那样本来就该重渲。
        if _shot_status(project_id, s.idx) == "succeeded":
            continue
        pos = next(n for n, x in enumerate(specs) if x.idx == s.idx)
        car = beat_subject(s, subjects, subject)
        if car not in bg_cache:
            bg_cache[car] = card_backgrounds(car)
        bgs = bg_cache[car]
        try:
            path, pieces = render_beat(s, subject=car, size=size,
                                       out_dir=out_dir, chapters=chapters,
                                       chapter_idx=where[pos], used=used,
                                       card_bg=bgs[s.idx % len(bgs)] if bgs else None,
                                       recent=recent,
                                       sting=sting_for(specs, where, chapters, s.idx),
                                       style=style, headline=headline, notes=notes,
                                       ranges=profile.vocab.concept_ranges)
        except (render_mod.RenderError, RuntimeError, OSError) as exc:
            errors.append(f"第 {s.idx} 拍:{exc}")
            db.update_shot(project_id, s.idx, "draft", status="failed", error=str(exc)[:500])
            continue
        db.write_json(project_id, f"beats/{s.idx:03d}.json", pieces)
        db.update_shot(project_id, s.idx, "draft", status="succeeded", draft_path=str(path),
                       provider_task_id=None, error=None, est_cost=0.0,
                       consistency="card" if s.layout in ("card", "concept") else "real")
        done.append(s.idx)
    db.write_json(project_id, "beats_used.json", used)
    db.write_json(project_id, "beats_recent.json", recent)
    for note in notes:
        # 降级要留痕:不然「概念动画全变成数据卡」看起来像正常产出
        db.add_event(project_id, "concept:degraded", reason=note)
    rows = {r["idx"]: r for r in db.get_shots(project_id, "draft")}
    remaining = [s.idx for s in specs if s.duration > 0
                 and rows.get(s.idx, {}).get("status") != "succeeded"]
    return {"rendered": done, "errors": errors, "remaining": remaining,
            "stage_complete": not remaining}


def rerender(project_id: str, idx: int) -> None:
    """门禁 B「重拍」:换一组背景重渲这一拍。刚用过的片段记成多用几次,排到后面去。"""
    pieces = db.read_json(project_id, f"beats/{idx:03d}.json", []) or []
    used = _load_used(project_id)
    for p in pieces:
        used[p["item_id"]] = used.get(p["item_id"], 0) + 3
    db.write_json(project_id, "beats_used.json", used)
    db.update_shot(project_id, idx, "draft", status="pending", draft_path=None, error=None)
    db.update_shot(project_id, idx, "final", status="pending", final_path=None, error=None)


def promote_all(project_id: str, specs: Sequence[ShotSpec]) -> int:
    """门禁 B 通过:草稿就是成片(画面不再重新生成),写进成片档。"""
    rows = {r["idx"]: r for r in db.get_shots(project_id, "draft")}
    n = 0
    for s in specs:
        r = rows.get(s.idx)
        if r and r.get("status") == "succeeded" and r.get("draft_path"):
            db.update_shot(project_id, s.idx, "final", status="succeeded",
                           final_path=r["draft_path"], consistency=r.get("consistency") or "real",
                           est_cost=0.0, error=None)
            n += 1
    return n


def _price_of(facts: Sequence[Any]) -> tuple[str, str]:
    """从事实里取入门价。返回 (价格文案, 事实 id)。

    封面上的价格和脚本里的数字一样,必须来自事实层 —— 不能手写"23.35万"。
    取**最低**的那一条:入门价就是最低售价,封面报最高价是误导。
    口径(basis)在封面上放不下,只在这里记下,供 render.json 留痕。

    `facts` 是 facts.json 里的原始字典(不是 Fact 模型):落盘的是 model_dump,
    早期版本的 enabled 存的是字符串,所以取值都做兼容。
    """
    def get(f: Any, key: str, default: Any = "") -> Any:
        if isinstance(f, dict):
            return f.get(key, default)
        return getattr(f, key, default)

    best: tuple[float, str, str, str] | None = None  # (值, 文案, fact id, basis)
    for f in facts or []:
        if get(f, "enabled", True) in (False, "False", "false", 0, "0"):
            continue
        if get(f, "scope", "") != "product":
            continue
        if get(f, "confidence", "") == "low":
            continue
        claim = str(get(f, "claim", "") or "")
        if not any(w in claim for w in ("售价", "价格", "起售", "定价", "官降", "落地")):
            continue
        for m in re.finditer(r"(\d+(?:\.\d+)?)\s*(万)?元", claim):
            value = float(m.group(1)) * (10000 if m.group(2) else 1)
            text = f"{m.group(1)}{'万' if m.group(2) else ''}元"
            if best is None or value < best[0]:
                best = (value, text, str(get(f, "id", "")), str(get(f, "basis", "") or ""))
    if best is None:
        return "", ""
    return f"{best[1]}起", best[2]


def cover_meta(project_id: str, profile: CategoryProfile, total: float) -> dict[str, Any]:
    """封面上的两行文字:角标(主体 + 入门价)和副标题(主论点)。

    副标题用主论点而不是"X分X秒 · 品类名":主论点是全片那句判断,
    放在封面上就是一句完整的钩子;时长信息对观众没有意义。
    """
    project = db.get_project(project_id) or {}
    subject = str(project.get("subject") or "")
    thesis = str(project.get("thesis") or "").strip()
    facts = db.read_json(project_id, "facts.json", []) or []
    price, fact_id = _price_of(facts)
    kicker = " · ".join(x for x in (subject, price) if x)
    subtitle = thesis or f"{int(total // 60)}分{int(total % 60)}秒 · {profile.label}"
    return {"kicker": kicker, "subtitle": subtitle, "price_fact": fact_id}


def compose(project_id: str, specs: Sequence[ShotSpec], profile: CategoryProfile,
            title: str) -> dict[str, Any]:
    """成片:拼各拍 + 旁白/BGM 混音 + 字幕。旁白在门禁 A 之后已经生成好了。"""
    rows = {r["idx"]: r for r in db.get_shots(project_id, "final")}
    beats = [s for s in specs if s.duration > 0]
    missing = [s.idx for s in beats if not (rows.get(s.idx) or {}).get("final_path")]
    if missing:
        raise RuntimeError(f"还有 {len(missing)} 拍没有成片:{missing[:12]}")
    nar = db.read_json(project_id, "narration.json", {}) or {}
    project_dir = config.project_dir(project_id)
    project = db.get_project(project_id) or {}
    size = aspect.frame_size(project)
    # 拼接缓存按序号命名,重拍过的拍必须重新归一化
    shutil.rmtree(project_dir / "_render", ignore_errors=True)
    result = render_mod.compose(
        project_id, [Path(rows[s.idx]["final_path"]) for s in beats],
        ass_path=Path(nar["ass_path"]) if nar.get("ass_path") else None,
        narration_path=Path(nar["narration_path"]) if nar.get("narration_path") else None,
        bgm_path=Path(nar["bgm_path"]) if nar.get("bgm_path") else None,
        size=size, durations=[s.duration for s in beats],
        output_name=aspect.output_name(project),
    )
    if not result.ok or not result.output_path:
        raise RuntimeError("合成失败:" + "; ".join(result.warnings or []))
    total = sum(s.duration for s in beats)
    meta = cover_meta(project_id, profile, total)
    # 封面底图优先用素材池里的干净官图(白底棚拍图没有第三方水印);
    # 没有官图才从成片抽帧。
    subject = str(project.get("subject") or "")
    still = library.cover_still(subject) if subject else None
    cover = render_mod.build_cover(result.output_path, title, project_dir / aspect.cover_name(project),
                                   subtitle=meta["subtitle"], size=size,
                                   still=still, kicker=meta["kicker"])
    payload = {**result.to_dict(), "cover": str(cover) if cover else None, "narration": nar,
               "cover_meta": meta, "cover_still": str(still) if still else None,
               "source_note": "画面素材:品牌官方宣传片与官方图片,仅作"
               f"{profile.vocab.subject_label}展示"}
    db.write_json(project_id, "render.json", payload)
    return payload

