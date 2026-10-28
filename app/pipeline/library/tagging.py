"""打标签与去重:视觉模型标签、废段源、重复画面继承。"""
from __future__ import annotations

import functools
import time
from pathlib import Path
from typing import Any, Sequence
from ...categories import Vocab
from ...providers import images
from .. import vision_verdict

from .model import DUP_BITS, EARLY_STOP_RATIO, EARLY_STOP_SAMPLE, Item, Library, TAG_BATCH, confirmed_seconds, is_waste, vocab_of


# --------------------------------------------------------------------------
# 看:打标签
# --------------------------------------------------------------------------
def tag_prompt(subject: str, vocab: Vocab) -> str:
    return (
        f"这张图来自「{subject}」相关的视频或图库。只依据图片内容回答,不要猜。"
        f"{vocab.tag_variant_note}请用文字逐条说明:\n"
        f"1. {vocab.tag_identify}\n"
        f"2. 拍的是哪个角度:{vocab.tag_angles}\n"
        f"3. {vocab.subject_short}是否完整在画面内,画面是否清晰;\n"
        "4. 有没有对白 / 解说字幕(画面下方的句子)、角落台标、水印、大段文字;"
        "是不是宣传海报、网页 / App 截图,或者是在拍大屏幕 / 电视里放的画面;\n"
        "5. 画面里有没有人,人占画面多大。\n"
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
- 描述是报错、拒答或为空 → target "unknown",view "other",problem "unclear"。

输出 JSON 对象 {{"verdicts": [...]}},每张一项,字段 index(与输入编号一致)、
target、view、problem、desc。"""


def tag_system(subject: str, vocab: Vocab) -> str:
    return TAG_SYSTEM.format(
        subject=subject, label=vocab.subject_label, short=vocab.subject_short,
        target_rule=vocab.tag_target_rule, target_cues=vocab.tag_target_cues,
        view_guide=vocab.tag_view_guide, no_subject=vocab.tag_no_subject,
        desc_example=vocab.tag_desc_example)


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
            queue = [i for i in queue if i.kind == "still"]
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
    """样本打够、废段占比太高的源:剩下没打标签的段标记跳过。返回新跳过的段数。"""
    by_src: dict[str, list[Item]] = {}
    for i in lib.items:
        if i.kind == "video" and i.source:
            by_src.setdefault(i.source, []).append(i)
    n = 0
    for segs in by_src.values():
        tagged = [i for i in segs if i.tagged]
        if len(tagged) < EARLY_STOP_SAMPLE:
            continue
        if sum(1 for i in tagged if is_waste(i)) / len(tagged) < EARLY_STOP_RATIO:
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
