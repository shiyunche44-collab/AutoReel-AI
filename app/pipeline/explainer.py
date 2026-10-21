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
from . import cards, library, motion, motion_assets, narration, overlays
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
#: 横屏字幕单行字数(竖屏是 16)
SUBTITLE_LINE_CHARS = 22
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
- stat:宣传片压暗 + 中央一个大数字滚动。旁白只围绕**一个**核心数字时用({stat_examples})。给 `stat`:{{"label": "参数名(8 字内)", "value": "取值含单位", "fact_id": "f_xxx"}}{concept_guide}

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
   全片大约四成是 broll。
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


def ground_keywords(lines: Any, narration_text: str, facts: Sequence[Fact]) -> list[str]:
    """核对两行大字。中文描述放行;数字、英文、型号这类**最容易编**的记号,
    必须在这一拍的旁白或启用的事实里出现过。核不过的行直接丢。"""
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
        out.append(text[: KEYWORD_CHARS + 4])
    return out[:2]


def ground_stat(raw: Any, facts: Sequence[Fact]) -> tuple[dict | None, list[str]]:
    """核对大数字:当成一行参数卡交给 `cards.ground`(数字必须见于引用的事实),
    再要求取值里确实有个能滚动的数字。核不过返回 (None, 原因)。"""
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
    return {"kind": "stat", "label": r["label"][:KEYWORD_CHARS], "value": value,
            "fact_id": r["fact_id"], "source": grounded.get("source", ""),
            "tag": r.get("tag", "")}, dropped


def concept_guide(vocab: Vocab) -> str:
    """分镜提示词里 concept 版式的说明。品类没配动画模板时为空(提示词与之前一字不差)。
    返回值作为 format 参数填进 SYSTEM,里面的花括号不会再被解析。"""
    if not vocab.concept_scenes:
        return ""
    scenes = "\n".join(f"    {name}:{when}" for name, when in vocab.concept_scenes.items())
    return (
        f"\n- concept:整屏概念动画,讲{vocab.subject_noun}身上看不见、拍不出来的东西时用。给 `concept`:\n"
        '  {"scene": "模板名", "title": "标题(12 字内)", "columns": [两个对比对象名,仅 compare 需要],\n'
        '    "rows": [{"label": "参数名", "values": ["取值含单位"], "fact_id": "f_xxx"}]}\n'
        "  rows 1–4 行,同样只能取自「可用事实」。scene 只能从下面选:\n"
        f"{scenes}\n"
        f"  不要连续两拍 concept,全片最多 {MAX_CONCEPT} 拍。"
    )


def _beat_line(i: int, seg: ScriptSegment | None, profile: CategoryProfile) -> str:
    sec = profile.section(seg.section if seg else "")
    options = list(sec.layouts) or list(LAYOUTS)
    card = f" | 卡片:{cards.KIND_GUIDE[sec.card]}" if sec.card in cards.KIND_GUIDE else ""
    car = f"〔{seg.subject}〕" if seg is not None and seg.subject else ""
    return (f"[第 {i + 1} 拍] 段落:{sec.name or '未分类'}{car}({sec.beat or '无'}) | "
            f"可选版式:{'/'.join(options)}{card} | 旁白:{seg.text if seg else ''}")


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
        facts_payload = "\n".join(f"- {f.id} [{f.subject or '共用'}]: {f.claim}"
                                  for f in usable)[:9000] or "(无)"
    else:
        facts_payload = "\n".join(f"- {f.id}: {f.claim}" for f in usable)[:5000] or "(无)"
    vocab = profile.vocab
    from .script_gen import roster_line
    user = USER.format(
        subject_label=vocab.subject_label if not subjects else "横评车单",
        subject=subject if not subjects else roster_line(subjects), count=len(segments),
        beats="\n".join(_beat_line(i, s, profile) for i, s in enumerate(segments)),
        facts=facts_payload,
    )
    try:
        system = SYSTEM.format(
            kw=KEYWORD_CHARS, domain=vocab.domain, subject_noun=vocab.subject_noun,
            keyword_examples=vocab.keyword_examples, stat_examples=vocab.stat_examples,
            view_enum=vocab.view_enum, view_guide=vocab.view_guide,
            concept_guide=concept_guide(vocab))
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
                          facts_for(facts, seg.subject), card_log)
             for i, seg in enumerate(segments)]
    return assign_broll_styles(enforce_rhythm(specs))


def facts_for(facts: Sequence[Fact], subject: str) -> list[Fact]:
    """一拍能用来核对叠加数字的事实:讲某台车的拍只认这台车 + 共用事实;共用拍全认。"""
    if not subject:
        return list(facts)
    return [f for f in facts if not f.subject or f.subject == subject]


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
        else:
            layout = SAFE_LAYOUT
    elif layout == "highlight":
        lines = ground_keywords(item.get("keywords"), seg.text, facts)
        if lines:
            overlay = {"kind": "highlight", "lines": lines}
        else:
            layout = SAFE_LAYOUT
    elif layout == "stat":
        overlay, dropped = ground_stat(item.get("stat"), facts)
        if overlay is None:
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
        if scene not in profile.vocab.concept_scenes:
            dropped = [f"concept 模板 {scene or '(空)'} 不在可选模板里"]
            layout = SAFE_LAYOUT
        else:
            kind = "compare" if scene == "compare" else "spec"
            grounded, dropped = cards.ground(cards.normalise(raw, kind), facts)
            if grounded:
                overlay = {**grounded, "kind": "concept", "scene": scene,
                           "rows": grounded["rows"][:4]}
            else:
                layout = SAFE_LAYOUT
    elif layout == "card":
        raw_card = item.get("card")
        kind = sec.card or (raw_card.get("kind") if isinstance(raw_card, dict) else "") or "spec"
        card, dropped = cards.ground(cards.normalise(raw_card, kind), facts)
        if card is None:
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
        chapter=seg.subject or sec.chapter or sec.name, view=view,
        subject=seg.subject,
    )


def enforce_rhythm(specs: list[ShotSpec]) -> list[ShotSpec]:
    """连续超过 MAX_CARD_RUN 拍整屏卡片时,把多出来的那拍退回 broll;
    连着两拍大数字也把后一拍退回 broll(一个接一个滚数字像在念参数表);
    概念动画同理不连着放,全片超过 MAX_CONCEPT 拍的也退回 broll。"""
    run = 0
    prev = ""
    concepts = 0
    for pos, s in enumerate(specs):
        if s.layout == "hook" and pos > 0:
            s.layout, s.overlay = SAFE_LAYOUT, None
            s.visual_zh = re.sub(r"^\[hook\]", f"[{SAFE_LAYOUT}]", s.visual_zh)
        if s.layout in ("stat", "concept") and prev == s.layout:
            old = s.layout
            s.layout, s.overlay = SAFE_LAYOUT, None
            s.visual_zh = re.sub(rf"^\[{old}\]", f"[{SAFE_LAYOUT}]", s.visual_zh)
        if s.layout == "concept":
            concepts += 1
            if concepts > MAX_CONCEPT:
                s.layout, s.overlay = SAFE_LAYOUT, None
                s.visual_zh = re.sub(r"^\[concept\]", f"[{SAFE_LAYOUT}]", s.visual_zh)
        prev = s.layout
        if s.layout == "card":
            run += 1
            if run > MAX_CARD_RUN:
                s.layout, s.card = SAFE_LAYOUT, None
                s.visual_zh = re.sub(r"^\[card\]", f"[{SAFE_LAYOUT}]", s.visual_zh)
                run = 0
        else:
            run = 0
    return specs


def assign_broll_styles(specs: list[ShotSpec]) -> list[ShotSpec]:
    """给 broll 拍轮换样式(确定性,不经 LLM):全屏 → 左上角角标 → 分屏 → …

    - 分屏不连着两拍(相邻 broll 都分屏就成了监控墙);
    - 每章第一拍开头已经有章节标牌占着左上角,不再放角标;
    - 别的版式的 overlay 不动;broll 的样式记在 overlay = {"kind": "broll", "style": ...}。
    """
    k = 0
    prev_split = False
    for n, s in enumerate(specs):
        if s.layout != "broll":
            prev_split = False
            continue
        style = BROLL_STYLES[k % len(BROLL_STYLES)]
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
                            chapter=seg.subject or sec.chapter or sec.name,
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
        return subjects[spec.idx % len(subjects)]
    return fallback


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
    nar = narration.build(project_id, specs, profile=profile,
                          size=profile.frame_size, line_chars=SUBTITLE_LINE_CHARS)
    if not nar.ok or not nar.cues:
        raise RuntimeError("旁白生成失败:" + "; ".join(nar.warnings or ["未知原因"]))
    if nar.missing_sentences:
        # 解说轨的时长由旁白决定:缺句子就是缺画面(实测丢的正好是结尾的结论段)。
        # 宁可停在这里让人再点一次确认,也不出一部没有结论的片子
        raise RuntimeError("旁白有句子没合成出来,请重新确认门禁 A 重试:"
                           + "; ".join(nar.warnings))
    durations = beat_durations(nar.cues, len(specs))
    for s, d in zip(specs, durations):
        s.duration = d
    return specs, nar


# --------------------------------------------------------------------------
# 渲染
# --------------------------------------------------------------------------
def _ff(args: Sequence[str], timeout: int = 600) -> subprocess.CompletedProcess:
    return render_mod._run(["-y", "-v", "error", *args], timeout=timeout)


def _fit_filter(w: int, h: int, crop_bottom: float = 0.0) -> str:
    """任意比例的视频放进 w×h:前景完整缩进画幅,空出的地方用同一画面放大模糊铺满。
    横屏源基本铺满,竖屏抖音片也不会被裁掉车头车尾。

    `crop_bottom` > 0(烧录字幕段):裁掉底部这一截后放大**铺满**画幅,不留模糊边 ——
    这类段基本是横屏评测,裁完再缩进画幅会上下各空出一条模糊带,一眼就是裁过的。"""
    if crop_bottom > 0:
        return (f"[0:v]crop=iw:trunc(ih*{1 - crop_bottom:.3f}/2)*2:0:0,"
                f"scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h},"
                f"setsar=1,fps={render_mod.FPS},format=yuv420p")
    return (f"[0:v]split[a][b];[a]scale={w}:{h}:force_original_aspect_ratio=increase,"
            f"crop={w}:{h},boxblur=20:2[bg];[b]scale={w}:{h}:force_original_aspect_ratio=decrease[fg];"
            f"[bg][fg]overlay=(W-w)/2:(H-h)/2,setsar=1,fps={render_mod.FPS},format=yuv420p")


def _piece_clip(piece: library.Piece, dest: Path, size: tuple[int, int], motion: str) -> Path:
    w, h = size
    if piece.kind == "still":
        return render_mod.still_to_clip(piece.path, dest, seconds=piece.length,
                                        motion=motion, size=size)
    # 视频不够长(原片在这一段后面就结束了)时,用最后一帧补齐
    vf = _fit_filter(w, h, piece.crop_bottom) + f",tpad=stop_mode=clone:stop_duration={piece.length:.3f}[v]"
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


#: 概念动画的实拍底从外观组里挑(行驶、航拍也在这组),车内镜头压上数据不好看
CONCEPT_FOOTAGE_HINT = "exterior"


def concept_background(piece: library.Piece, seconds: float, size: tuple[int, int], work: Path) -> Path:
    """概念动画的底:一个长镜头(`library.long_take`),比整拍短就放慢铺满。
    放慢的行车镜头本身有质感,也省掉压着数据时底在跳切的乱。"""
    work.mkdir(parents=True, exist_ok=True)
    dest = work / "bg.mp4"
    w, h = size
    factor = max(1.0, seconds / max(0.1, piece.length))
    vf = (_fit_filter(w, h, piece.crop_bottom) + f",setpts={factor:.4f}*PTS,fps={render_mod.FPS},"
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
    """broll / panel / highlight / stat 的背景。分屏样式铺 2–3 条不同的背景再并排。"""
    if broll_style(spec) == "split" and seconds >= SPLIT_MIN_SECONDS:
        count = 3 if seconds <= 5 else 2
        clips, pieces = [], []
        for k in range(count):
            got = library.fill(subject, seconds, hint=spec.view, used=used, recent=recent)
            if not got:
                break
            pieces += got
            clips.append(background_clip(got, seconds, size, work / f"panel_{k}",
                                         motion_seed=spec.idx * 7 + k))
        if len(clips) >= 2:
            return split_clip(clips, seconds, size, work / "split.mp4"), pieces
        if clips:
            return clips[0], pieces
    pieces = library.fill(subject, seconds, hint=spec.view, used=used, recent=recent)
    if not pieces and card_bg:
        pieces = [library.Piece("bg", "still", str(card_bg), 0.0, seconds)]
    return background_clip(pieces, seconds, size, work, motion_seed=spec.idx), pieces


def hook_background(subject: str, seconds: float, size: tuple[int, int], work: Path,
                    used: dict[str, int], recent: list[str], hint: str, seed: int
                    ) -> tuple[Path, list[library.Piece]]:
    """黄金三秒的背景:前几秒每 1 秒一刀硬切(不 xfade),之后接正常背景铺满这一拍。"""
    n = 3 if seconds >= 3.5 else 2 if seconds >= 2.0 else 1
    cuts: list[library.Piece] = []
    for _ in range(n):
        got = library.fill(subject, HOOK_CUT, hint=hint, used=used, recent=recent)
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
        more = library.fill(subject, rest, hint=hint, used=used, recent=recent)
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
                   used: dict[str, int], recent: list[str]
                   ) -> tuple[Path | None, motion_assets.Cutout | None, list[library.Piece]]:
    """concept 拍的素材:(实拍底, 车图, 用到的片段)。正式渲染和选风格预览共用,保证预览看到的就是成片的样子。
    产品镜(studio)用模板自带的影棚底,不铺实拍;其余压在一段实拍上。"""
    scene = (spec.overlay or {}).get("scene", "")
    bg_clip: Path | None = None
    pieces: list[library.Piece] = []
    if motion.scene_background(scene) != "studio":
        # 一个长镜头放慢铺满;库里没有够长的才退回多段拼接
        take = library.long_take(subject, seconds, hint=CONCEPT_FOOTAGE_HINT, used=used, recent=recent)
        got = [take] if take else library.fill(subject, seconds, hint=CONCEPT_FOOTAGE_HINT,
                                               used=used, recent=recent)
        if got:
            try:
                bg_clip = (concept_background(take, seconds, size, work) if take else
                           background_clip(got, seconds, size, work, motion_seed=spec.idx))
                pieces = got
            except Exception:
                bg_clip = None
    views = motion.scene_photo_views(scene)
    photo = motion_assets.hero_still(subject, views) if views else None
    return bg_clip, photo, pieces


def concept_style_of(project_id: str) -> str:
    """项目选定的概念动画风格(`concept_style.json`);没选过 = 默认风格。"""
    got = db.read_json(project_id, "concept_style.json", {}) or {}
    return motion.get_style(got.get("style")).id


def render_beat(spec: ShotSpec, *, subject: str, size: tuple[int, int], out_dir: Path,
                chapters: Sequence[tuple[str, float]], chapter_idx: int,
                used: dict[str, int], card_bg: Path | None,
                recent: list[str] | None = None,
                sting: tuple[int, str] | None = None,
                style: str | None = None) -> tuple[Path, list[dict]]:
    """渲染一拍,返回 (成品路径, 用到的背景片段)。

    `recent` 是全片共享的最近用过的片段(相邻多样性,见 `library.fill`);
    `sting` = (章号, 章名) 时在开头叠章节标牌(每章第一拍)。"""
    seconds = max(0.5, spec.duration)
    recent = recent if recent is not None else []
    work = out_dir / "_work" / f"{spec.idx:03d}"
    if work.exists():
        shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True, exist_ok=True)

    pieces: list[library.Piece] = []
    if spec.layout == "hook" and spec.overlay:
        try:
            base, pieces = hook_background(subject, seconds, size, work / "hook", used, recent,
                                           spec.view, spec.idx)
        except Exception:  # noqa: BLE001 - 冷开场做不出来:同一批关键词退回 highlight 版式
            lines = spec.overlay.get("lines") or []
            fallback = spec.model_copy(update={
                "layout": "highlight" if lines else SAFE_LAYOUT,
                "overlay": {"kind": "highlight", "lines": lines} if lines else None})
            return render_beat(fallback, subject=subject, size=size, out_dir=out_dir,
                               chapters=chapters, chapter_idx=chapter_idx, used=used,
                               card_bg=card_bg, recent=recent, sting=sting, style=style)
    elif spec.layout == "card" and spec.card:
        base = cards.render(spec.card, subject, work / "card.mp4", seconds=seconds,
                            background=card_bg, size=size)
    elif spec.layout == "concept" and spec.overlay:
        scene = spec.overlay.get("scene", "")
        bg_clip, photo, pieces = concept_inputs(spec, subject, seconds, size, work / "concept_bg",
                                                used, recent)
        try:
            base = motion.render(scene, motion.scene_data(spec.overlay, subject), seconds, size,
                                 work / "concept.mp4", background=bg_clip, photo=photo, style=style)
        except motion.MotionError as exc:
            # 动画渲染不了:同一批核对过的行画成数据卡,不让整拍失败 —— 但要留痕,
            # 否则"概念动画全变成数据卡"看起来像正常产出
            _log.warning("第 %s 拍概念动画 %s 退回数据卡:%s", spec.idx, scene, exc)
            base = cards.render(concept_card(spec.overlay), subject, work / "card.mp4",
                                seconds=seconds, background=card_bg, size=size)
    else:
        base, pieces = _broll_base(spec, subject, seconds, size, work, used, recent, card_bg)

    bar = overlays.chapter_bar(chapters, chapter_idx, size, work / "bar.png")
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


def render_pending(project_id: str, specs: Sequence[ShotSpec], *, subject: str,
                   profile: CategoryProfile, limit: int = 3,
                   subjects: Sequence[str] | None = None) -> dict[str, Any]:
    """渲染还没出片的拍(每次最多 `limit` 拍,后台循环反复调用)。

    产物写成 draft 档 succeeded —— 门禁 B 的审片页按草稿档展示。
    `subjects`:多车横评车单 —— 每拍按 `beat_subject` 取自己那台车的素材,绝不借别的车的。
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
    done, errors = [], []
    for s in todo:
        pos = next(n for n, x in enumerate(specs) if x.idx == s.idx)
        car = beat_subject(s, subjects, subject)
        if car not in bg_cache:
            bg_cache[car] = card_backgrounds(car)
        bgs = bg_cache[car]
        try:
            path, pieces = render_beat(s, subject=car, size=profile.frame_size,
                                       out_dir=out_dir, chapters=chapters,
                                       chapter_idx=where[pos], used=used,
                                       card_bg=bgs[s.idx % len(bgs)] if bgs else None,
                                       recent=recent,
                                       sting=sting_for(specs, where, chapters, s.idx),
                                       style=style)
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
    # 拼接缓存按序号命名,重拍过的拍必须重新归一化
    shutil.rmtree(project_dir / "_render", ignore_errors=True)
    result = render_mod.compose(
        project_id, [Path(rows[s.idx]["final_path"]) for s in beats],
        ass_path=Path(nar["ass_path"]) if nar.get("ass_path") else None,
        narration_path=Path(nar["narration_path"]) if nar.get("narration_path") else None,
        bgm_path=Path(nar["bgm_path"]) if nar.get("bgm_path") else None,
        size=profile.frame_size, durations=[s.duration for s in beats],
    )
    if not result.ok or not result.output_path:
        raise RuntimeError("合成失败:" + "; ".join(result.warnings or []))
    total = sum(s.duration for s in beats)
    cover = render_mod.build_cover(result.output_path, title, project_dir / "cover.jpg",
                                   subtitle=f"{int(total // 60)}分{int(total % 60)}秒 · {profile.label}",
                                   size=profile.frame_size)
    payload = {**result.to_dict(), "cover": str(cover) if cover else None, "narration": nar,
               "source_note": "画面素材:品牌官方宣传片与官方图片,仅作"
               f"{profile.vocab.subject_label}展示"}
    db.write_json(project_id, "render.json", payload)
    return payload

