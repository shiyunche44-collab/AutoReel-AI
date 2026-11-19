"""打标签与去重:视觉模型标签、废段源、重复画面继承。"""
from __future__ import annotations

import functools
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from ...categories import Vocab
from ...providers import images
from .. import vision_verdict
from .model import (
    DUP_BITS,
    EARLY_STOP_RATIO,
    EARLY_STOP_SAMPLE,
    HERO_MAX,
    LAUNCH_EARLY_STOP_RATIO,
    LAUNCH_EARLY_STOP_SAMPLE,
    TAG_BATCH,
    Item,
    Library,
    confirmed_seconds,
    is_waste,
    normalise_shot,
    vocab_of,
)

# --------------------------------------------------------------------------
# 看:打标签
# --------------------------------------------------------------------------
#: 景别和"宣传片大镜头"两问:打标签的第 6、7 条,补打景别时单独问(`shot_prompt`)
SHOT_QUESTIONS = (
    "6. 景别:远景(主体很小、大环境为主)、全景(主体完整在画面里)、中景(主体大半在画面里)、"
    "近景(主体的一大块局部)、特写(某个部件或细节);\n"
    "7. 像不像宣传片里的大镜头:光线、构图、色彩讲不讲究,有没有电影感,"
    "还是普通的展示画面、随手拍、测评现场;\n"
)


def tag_prompt(subject: str, vocab: Vocab) -> str:
    return (
        f"这张图来自「{subject}」相关的视频或图库。只依据图片内容回答,不要猜。"
        f"{vocab.tag_variant_note}请用文字逐条说明:\n"
        f"1. {vocab.tag_identify}\n"
        f"2. 拍的是哪个角度:{vocab.tag_angles}\n"
        f"3. {vocab.subject_short}是否完整在画面内,画面是否清晰;\n"
        "4. 有没有对白 / 解说字幕(画面下方的句子)、角落台标、水印、大段文字;"
        "是不是宣传海报、网页 / App 截图,或者是在拍大屏幕 / 电视里放的画面;\n"
        "5. 画面里有没有人,人占画面多大;发布会现场若有观众的头、举起的手机挡在画面前景,"
        "概括里要写“前景观众”;画面上有大号标题字或大数字(PPT 页),概括里要写“含大字”。\n"
        f"{SHOT_QUESTIONS}"
        "最后用一句话概括这个画面。"
    )


TAG_SYSTEM = """你把视觉模型对若干图片的描述转成素材标签。目标{label}是「{subject}」。

只依据描述里**明确写出**的内容,保守,不要推理补全:
- target:{target_rule}
  描述里视觉模型自己给的"是 / 不是目标{label}"结论一律忽略,只看它对{target_cues}的描述。
- view,取最贴切的一个:
{view_guide}
  people(主体是人)、text(主体是文字卡 / 参数表 / 片头片尾)、other(风景、别的东西)。
- problem,按顺序取第一条命中的:target 为 "no" → "not_target_model";
  {no_subject}
  明确说{short}被画面边缘切掉(局部特写不算)→ "cropped";明确说模糊、过暗、看不清 → "unclear";
  宣传海报(画面上**有**大标题字,或有代言人摆拍;只是"宣传照 / 官方大片"、画面里没有大字和人的不算)、
  网页 / App 截图,或{short}是**显示在大屏 / 屏幕 / 电视里**的
  翻拍画面(发布会舞台大屏、前景有观众)→ "poster";
  画面下方有对白或解说**字幕**(一行行句子)→ "subtitle";
  只有角落的台标、水印、小字标注 → "text_overlay";都没有 → ""。
- desc:一句中文概括画面(20 字以内),例如"{desc_example}"。
{shot_rules}
- 描述是报错、拒答或为空 → target "unknown",view "other",problem "unclear",shot "",hero 0。

输出 JSON 对象 {{"verdicts": [...]}},每张一项,字段 index(与输入编号一致)、
target、view、problem、desc、shot、hero。"""

#: 景别 / 大镜头的转换规则(打标签和补打景别共用)
SHOT_RULES = """- shot,景别,取一个:wide(远景:{short}很小、大环境为主)、full(全景:{short}完整在画面里)、
  medium(中景:{short}大半在画面里)、close(近景:{short}的一大块局部)、closeup(特写:部件 / 细节);
  描述没说清 → ""。
- hero,像不像宣传片里的大镜头,0–3 的整数:3 = 光线、构图、色彩都讲究的电影感大片;
  2 = 讲究的宣传画面;1 = 普通、清楚的展示画面;0 = 随手拍、测评现场、截图、文字卡。
  描述没提光线构图的,按画面内容保守给 0 或 1。"""

SHOT_SYSTEM = """你把视觉模型对若干图片的描述转成景别标签。目标{label}是「{subject}」。
只依据描述里**明确写出**的内容,保守,不要推理补全:
{shot_rules}
- 描述是报错、拒答或为空 → shot "",hero 0。

输出 JSON 对象 {{"verdicts": [...]}},每张一项,字段 index(与输入编号一致)、shot、hero。"""


def tag_system(subject: str, vocab: Vocab) -> str:
    return TAG_SYSTEM.format(
        subject=subject, label=vocab.subject_label, short=vocab.subject_short,
        target_rule=vocab.tag_target_rule, target_cues=vocab.tag_target_cues,
        view_guide=vocab.tag_view_guide, no_subject=vocab.tag_no_subject,
        desc_example=vocab.tag_desc_example,
        shot_rules=SHOT_RULES.format(short=vocab.subject_short))


def shot_prompt(subject: str) -> str:
    """补打景别 / 大镜头(老池子的可用视频段)用的看图提示词:只问这两条。"""
    return (f"这张图来自「{subject}」相关的视频。只依据图片内容回答,不要猜。请用文字逐条说明:\n"
            + SHOT_QUESTIONS.replace("6. ", "1. ").replace("7. ", "2. ")
            + "最后用一句话概括这个画面。")


def shot_system(subject: str, vocab: Vocab) -> str:
    return SHOT_SYSTEM.format(subject=subject, label=vocab.subject_label,
                              shot_rules=SHOT_RULES.format(short=vocab.subject_short))


def tag_items(lib: Library, todo: Sequence[Item], llm: Any, errors: list[str], *,
              deadline: float = 0.0, enough_seconds: float = 0.0) -> int:
    """给还没看过的段 / 图打标签。返回打上的个数(含继承重复画面的)。

    超过 `deadline` 就停;`enough_seconds` > 0 时,可用素材够了就不再打视频段。
    每批之后检查各源的样本,废段太多的源剩下的段直接跳过(见 `reject_sources`)。
    """
    done = inherit_duplicates(lib)
    vocab = vocab_of(lib)
    prompt = tag_prompt(lib.subject, vocab)
    system = tag_system(lib.subject, vocab)
    queue = list(todo)
    while queue:
        if deadline and time.monotonic() > deadline:
            break
        if enough_seconds and confirmed_seconds(lib, video_only=True) >= enough_seconds:
            # 发布会回放是整场里挑出来的主力画面:官方来源够用了也要打完
            queue = [i for i in queue if i.kind == "still" or i.launch]
        batch = [i for i in queue[:TAG_BATCH] if not i.tagged and not i.skipped
                 and i.frame and Path(i.frame).is_file()]
        queue = queue[TAG_BATCH:]
        urls: list[str] = []
        slots: list[Item] = []
        for item in batch:
            try:
                urls.append(images.cli.upload_temp_url(item.frame))
                slots.append(item)
            except Exception as exc:  # noqa: BLE001
                errors.append(f"素材池帧上传失败({item.id}):{str(exc)[:100]}")
        if not urls:
            continue
        verdicts, _, _ = vision_verdict.judge(urls, prompt, system, llm, errors, what="素材")
        lib.vlm_calls += len(urls)
        for item, v in zip(slots, verdicts):
            if not v:
                continue
            apply_tag(item, v, vocab.all_views)
            done += 1
        done += inherit_duplicates(lib)
        reject_sources(lib)
        lib.save()
    return done


def reject_sources(lib: Library) -> int:
    """样本打够、废段占比太高的源:剩下没打标签的段标记跳过。返回新跳过的段数。

    同一个源的低清段和高清段分开算:低清段打了标签也一律算废段(`usable` 不收低清),
    重拉高清后新段不能因为老段"全废"被连坐跳过。"""
    by_src: dict[tuple[str, bool], list[Item]] = {}
    for i in lib.items:
        if i.kind == "video" and i.source:
            by_src.setdefault((i.source, i.low_res), []).append(i)
    n = 0
    launch = {s.id for s in lib.sources if s.kind == "launch"}
    for (src, _low), segs in by_src.items():
        sample, ratio = ((LAUNCH_EARLY_STOP_SAMPLE, LAUNCH_EARLY_STOP_RATIO) if src in launch
                         else (EARLY_STOP_SAMPLE, EARLY_STOP_RATIO))
        tagged = [i for i in segs if i.tagged]
        if len(tagged) < sample:
            continue
        if sum(1 for i in tagged if is_waste(i)) / len(tagged) < ratio:
            continue
        for i in segs:
            if not i.tagged and not i.skipped:
                i.skipped = "source_rejected"
                n += 1
    return n


@functools.lru_cache(maxsize=8192)
def _dhash(path: str) -> int | None:
    """代表帧的 64 位差值哈希。读不了返回 None(= 不参与去重)。"""
    try:
        from PIL import Image

        with Image.open(path) as im:
            px = im.convert("L").resize((9, 8)).tobytes()
    except (OSError, ValueError):
        return None
    bits = 0
    for row in range(8):
        for col in range(8):
            bits = (bits << 1) | int(px[row * 9 + col] > px[row * 9 + col + 1])
    return bits


def _same_frame(a: Item, b: Item) -> bool:
    if a.source != b.source or not a.frame or not b.frame:
        return False
    ha, hb = _dhash(a.frame), _dhash(b.frame)
    return ha is not None and hb is not None and bin(ha ^ hb).count("1") <= DUP_BITS


def inherit_duplicates(lib: Library) -> int:
    """同源里和已打标签的段几乎同一画面的未打段,直接照抄标签,不花视觉调用。"""
    tagged: dict[str, list[Item]] = {}
    for i in lib.items:
        if i.kind == "video" and i.tagged:
            tagged.setdefault(i.source, []).append(i)
    n = 0
    for i in lib.items:
        if i.kind != "video" or i.tagged or i.skipped or not i.frame:
            continue
        twin = next((t for t in tagged.get(i.source, ()) if _same_frame(i, t)), None)
        if twin is None:
            continue
        i.view, i.target, i.problem, i.desc = twin.view, twin.target, twin.problem, twin.desc
        i.shot, i.hero = twin.shot, twin.hero
        i.tagged = True
        tagged[i.source].append(i)
        n += 1
    return n


def mark_official_duplicates(lib: Library) -> int:
    """第三方视频里和某条官方视频同一画面的段(搬运 / 二剪的官方片):标成跳过,
    记下是哪条官方段的重复。永远用官方原片 —— 原片更清楚、没有别人的台标,
    这些段也不再花视觉调用。返回新标的段数。"""
    heads: list[tuple[str, int]] = []
    for i in lib.items:
        if i.kind == "video" and i.official and i.frame:
            h = _dhash(i.frame)
            if h is not None:
                heads.append((i.id, h))
    if not heads:
        return 0
    n = 0
    for i in lib.items:
        if i.kind != "video" or i.official or not i.frame or i.dup_of:
            continue
        h = _dhash(i.frame)
        if h is None:
            continue
        twin = next((oid for oid, oh in heads if bin(h ^ oh).count("1") <= DUP_BITS), None)
        if twin is None:
            continue
        i.dup_of = twin
        i.skipped = i.skipped or "dup_official"
        n += 1
    return n


def apply_tag(item: Item, v: dict[str, Any], views: Sequence[str]) -> None:
    target = str(v.get("target") or "unknown").lower()
    item.target = target if target in ("yes", "no", "unknown") else "unknown"
    view = str(v.get("view") or "other").lower()
    item.view = view if view in views else "other"
    problem = str(v.get("problem") or "").lower()
    item.problem = "" if problem in ("", "none") else problem
    if item.target == "no":
        item.problem = "not_target_model"
    item.desc = str(v.get("desc") or "")[:60]
    item.tagged = True
    # 景别 / 大镜头只给能当背景的视频段记(废段、静图记了也用不上)
    if item.kind == "video" and item.usable:
        apply_shot(item, v)
    else:
        item.shot, item.hero = "", -1


def _hero(value: Any) -> int:
    try:
        return max(0, min(HERO_MAX, int(round(float(value)))))
    except (TypeError, ValueError):
        return -1


def apply_shot(item: Item, v: dict[str, Any]) -> None:
    item.shot = normalise_shot(v.get("shot"))
    item.hero = _hero(v.get("hero"))


def needs_shot(item: Item) -> bool:
    """可用视频段里还没打景别 / 大镜头的(老池子)。看过一次就算打过:
    景别没说清(shot 为空)的不反复送;hero 解析不出来的仍算没打(回填时同一轮不重送)。"""
    return item.kind == "video" and item.usable and item.hero < 0


def tag_shots(lib: Library, todo: Sequence[Item], llm: Any, errors: list[str], *,
              should_stop: Callable[[], bool] | None = None) -> int:
    """给已打过标签的可用视频段补打景别和大镜头分(不动 target / view / problem)。
    返回补上的段数。每批存一次盘,中途停了下次接着补(补过的不重补)。"""
    vocab = vocab_of(lib)
    prompt = shot_prompt(lib.subject)
    system = shot_system(lib.subject, vocab)
    queue = [i for i in todo if needs_shot(i) and i.frame and Path(i.frame).is_file()]
    done = 0
    while queue:
        if should_stop is not None and should_stop():
            break
        batch, queue = queue[:TAG_BATCH], queue[TAG_BATCH:]
        urls: list[str] = []
        slots: list[Item] = []
        for item in batch:
            try:
                urls.append(images.cli.upload_temp_url(item.frame))
                slots.append(item)
            except Exception as exc:  # noqa: BLE001
                errors.append(f"素材池帧上传失败({item.id}):{str(exc)[:100]}")
        if not urls:
            continue
        verdicts, _, _ = vision_verdict.judge(urls, prompt, system, llm, errors, what="景别")
        lib.vlm_calls += len(urls)
        for item, v in zip(slots, verdicts):
            if v:
                apply_shot(item, v)
                done += 1
        lib.save()
    return done
