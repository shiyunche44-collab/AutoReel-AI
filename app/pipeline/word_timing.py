"""逐字时间:用 faster-whisper 的词级时间戳,量出每句旁白里每个字实际是什么时候念到的。

字幕层原先按字数比例估算词的弹出时间,一句里前快后慢就会错位。这里对每句 TTS 音频转写一遍,
把听到的字和脚本原文对齐,得到原文每个字相对本句开头的秒数(`char_t`)。
对不上(转写失败、数字读法差异过大)时返回 None,调用方退回字数比例,不拦出片。
"""
from __future__ import annotations

import difflib
import logging
import re
from pathlib import Path
from typing import Any, Sequence

from .. import config

log = logging.getLogger(__name__)

#: 原文和听到的内容至少这个比例对得上,才信这份时间戳
MIN_MATCH = 0.5
_SKIP = re.compile(r"[\s，。！？；：、,.!?;:\"'“”‘’《》()（）\-—…·]")


def _core(text: str) -> list[int]:
    """原文里参与对齐的字(非标点非空白)的下标。"""
    return [i for i, ch in enumerate(text) if not _SKIP.match(ch)]


def char_times(text: str, words: Sequence[tuple[str, float, float]],
               duration: float, *, min_match: float = MIN_MATCH) -> list[float] | None:
    """原文每个字的起始秒(相对本句开头,单调不降)。`words` 是转写出的 (词, 起, 止)。

    听到的每个词内部按字数均分时间;原文和听到的内容做字符级对齐,对上的字取听到的时间,
    对不上的(如 "643" 读成 "六百四十三")在前后锚点之间线性插值。
    """
    core = _core(text)
    heard: list[tuple[str, float]] = []
    for w, s, e in words:
        chars = [c for c in w if not _SKIP.match(c)]
        for k, c in enumerate(chars):
            heard.append((c, s + (e - s) * k / len(chars)))
    if not core or not heard:
        return None
    a = "".join(text[i] for i in core)
    b = "".join(c for c, _ in heard)
    blocks = difflib.SequenceMatcher(None, a, b, autojunk=False).get_matching_blocks()
    matched = sum(m.size for m in blocks)
    if matched / len(a) < min_match:
        return None
    anchor: dict[int, float] = {}
    for m in blocks:
        for k in range(m.size):
            anchor[m.a + k] = heard[m.b + k][1]
    # 核心字:锚点之间线性插值,首尾外推到 0 / duration
    known = sorted(anchor)
    core_t: list[float] = []
    for j in range(len(a)):
        if j in anchor:
            core_t.append(anchor[j])
            continue
        lo = max((k for k in known if k < j), default=None)
        hi = min((k for k in known if k > j), default=None)
        t0, p0 = (anchor[lo], lo) if lo is not None else (0.0, -1)
        t1, p1 = (anchor[hi], hi) if hi is not None else (duration, len(a))
        core_t.append(t0 + (t1 - t0) * (j - p0) / (p1 - p0))
    # 铺回全部字符:标点取下一个核心字的时间
    out = [0.0] * len(text)
    pos = {idx: core_t[j] for j, idx in enumerate(core)}
    nxt = duration
    for i in range(len(text) - 1, -1, -1):
        if i in pos:
            nxt = pos[i]
        out[i] = nxt
    run = 0.0
    for i, t in enumerate(out):
        run = max(run, min(t, duration))
        out[i] = round(run, 3)
    return out


def transcribe_words(path: Path) -> list[tuple[str, float, float]] | None:
    """一句音频 → [(词, 起, 止)];转写不可用或失败返回 None。复用语音质检的模型单例。"""
    from . import voice_qa

    if voice_qa.load_asr() is None:
        return None
    model: Any = voice_qa._model
    try:
        segments, _ = model.transcribe(voice_qa._decode_16k(path), language="zh", beam_size=1,
                                       vad_filter=False, temperature=0.0, word_timestamps=True,
                                       initial_prompt="以下是普通话的句子。")
        return [(w.word, float(w.start), float(w.end))
                for s in segments for w in (s.words or [])]
    except Exception as exc:  # noqa: BLE001 - 字幕细节不能拦出片
        log.warning("逐字时间:转写失败,退回字数比例:%s", exc)
        return None


def measure(paths: Sequence[Path], texts: Sequence[str],
            durations: Sequence[float]) -> list[list[float] | None]:
    """每句一份 `char_t`(失败的那句为 None)。关了开关或没装 faster-whisper 时全是 None。"""
    empty: list[list[float] | None] = [None] * len(texts)
    if not config.CAPTION_WORD_TIMING:
        return empty
    from . import voice_qa

    if not voice_qa.asr_available():
        return empty
    out: list[list[float] | None] = []
    try:
        for p, t, d in zip(paths, texts, durations, strict=False):
            words = transcribe_words(p)
            out.append(char_times(t, words, d) if words else None)
    finally:
        voice_qa.release_asr()
    return out
