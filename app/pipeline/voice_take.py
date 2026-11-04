"""一口气合成:相邻的句子合成一条(一个 take),再按句切开。

逐句独立合成时,每句都是重新起调,句与句之间没有气口的延续,听着像在报幕。整段合成靠 SSML
`<break>` 控制停顿,语气是连着的;切开之后字幕、拍长、语音质检仍然按句工作。

切点来自接口回传的逐字时间戳(`enable_subtitle`),再落到实测的停顿上:时间戳本身有 0.1 秒
上下的偏差,而停顿是准的。停顿里不是纯静音,而是约 -40dB 的底噪和气口。把它换成纯静音、再压到
0.2 秒,听着像憋着一口气(试听反馈),所以切点放在停顿中间,两边的声音原样保留,只抽掉过长的部分。
任何一步对不上都返回 None,这一组退回逐句合成,不拦出片。
"""
from __future__ import annotations

import array
import logging
import re
import statistics
import subprocess
import wave
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape

from .. import config
from ..providers import ark_tts, audio
from . import voice_qa, word_timing
from .timeline import is_strong_end

log = logging.getLogger(__name__)

#: 接口回传的字和送去的文本至少这个比例对得上,才信切点
MIN_ALIGN = 0.8
#: 时间戳偏早时,下一句开口后这么久之内的静音仍算句间静音
SILENCE_REACH = 0.25
_SILENCE_DB = -28
#: 抽掉停顿的接口淡入淡出,短到听不出底噪断开
CUT_FADE = 0.005
_SILENCE_MIN = 0.03
#: 一条 take 里某句的语速偏离全条中位数超过这个倍数,就是切错了位置
#: 接口的时间戳偶尔整段挤在一起(0.06 秒一个字),切点会落到前面的逗号、冒号上,靠这个拦下
RATE_LIMITS = (0.6, 1.6)
RATE_MIN_CHARS = 6
#: 切不开时重新合成的次数:接口结果不确定,同一段文本下一次多半正常
TAKE_RETRIES = 1
#: 间隔不超过这么久的静音段算一段,气声里的杂音会把一段停顿打碎
_SILENCE_MERGE = 0.03
#: 整段合成切出来的音频的标记(AudioAsset.node_id)
TAKE_NODE = "take"
#: 计划停顿短于这个就不插 break
BREAK_MIN = 0.25


@dataclass
class TakeResult:
    #: 句子位置 → 切好的音频
    assets: dict[int, Any] = field(default_factory=dict)


def plan_takes(texts: Sequence[str], sections: Sequence[str], tempos: Sequence[float],
               max_chars: int | None = None) -> list[list[int]]:
    """按顺序把句子分成一条条 take。换段落、语速倍率变化(钩子、结论)、超过字数时另起一条。"""
    limit = config.TTS_TAKE_MAX_CHARS if max_chars is None else max_chars
    takes: list[list[int]] = []
    size = 0
    for i, text in enumerate(texts):
        fresh = (not takes or sections[i] != sections[i - 1]
                 or abs(tempos[i] - tempos[i - 1]) > 0.005 or size + len(text) > limit)
        if fresh:
            takes.append([])
            size = 0
        takes[-1].append(i)
        size += len(text)
    return takes


def to_ssml(texts: Sequence[str], pauses: Sequence[float]) -> str:
    """整条 take 的 SSML。只有该明显停一下的地方(问句后这类)才插 break,其余让 TTS 自己换气。
    文本必须已经过 `normalize_for_tts`,反过来标签里的 300ms 会被改成中文读法。"""
    parts: list[str] = []
    for k, text in enumerate(texts):
        parts.append(escape(text))
        if k < len(texts) - 1 and is_strong_end(text) and pauses[k] >= BREAK_MIN:
            parts.append(f'<break time="{round(pauses[k] * 1000)}ms"/>')
    return "<speak>" + "".join(parts) + "</speak>"


def _silences(path: Path) -> list[tuple[float, float]]:
    proc = subprocess.run(
        [str(config.FFMPEG_PATH), "-hide_banner", "-i", str(path), "-af",
         f"silencedetect=n={_SILENCE_DB}dB:d={_SILENCE_MIN}", "-f", "null", "-"],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120)
    starts = re.findall(r"silence_start: ([\d.]+)", proc.stderr or "")
    ends = re.findall(r"silence_end: ([\d.]+)", proc.stderr or "")
    merged: list[tuple[float, float]] = []
    for a, b in ((float(x), float(y)) for x, y in zip(starts, ends, strict=False)):
        if merged and a - merged[-1][1] <= _SILENCE_MERGE:
            merged[-1] = (merged[-1][0], b)
        else:
            merged.append((a, b))
    return merged


def find_cuts(texts: Sequence[str], words: Sequence[tuple[str, float, float]], duration: float,
              silences: Sequence[tuple[float, float]]) -> list[tuple[float, float]] | None:
    """每个句间的 (上一句说完, 下一句开口),单位秒。对不上返回 None。"""
    if len(texts) < 2:
        return []  # 单句不用切,没有时间戳也能用
    joined = "".join(texts)
    times = word_timing.char_times(joined, words, duration, min_match=MIN_ALIGN)
    if times is None:
        return None
    core = word_timing._core(joined)
    cuts: list[tuple[float, float]] = []
    end = 0
    floor = 0.0
    for text in texts[:-1]:
        end += len(text)
        tail = [i for i in core if i < end]
        if not tail or end >= len(times):
            return None
        lo, opening = times[tail[-1]], times[end]
        near = [(a, b) for a, b in silences if b > lo and a < opening + SILENCE_REACH]
        if near:
            speech_end, speech_start = max(near, key=lambda s: s[1] - s[0])
        else:
            speech_end = speech_start = opening
        if speech_end <= floor:
            return None
        cuts.append((speech_end, speech_start))
        floor = speech_start
    return cuts


def _faded(frames: bytes, rate: int, *, head: bool, tail: bool) -> bytes:
    """单声道 s16 的首尾线性淡入淡出,只用在剪掉过东西的接口上。"""
    pcm = array.array("h", frames)
    n = min(round(CUT_FADE * rate), len(pcm) // 2)
    for i in range(n):
        if head:
            pcm[i] = int(pcm[i] * i / n)
        if tail:
            pcm[-1 - i] = int(pcm[-1 - i] * i / n)
    return pcm.tobytes()


def _kept(start: float, stop: float, silences: Sequence[tuple[float, float]],
          cap: float) -> list[tuple[float, float]]:
    """这一句要留下的区间:句内超过 cap 的停顿只抽掉中间,两头的底噪和气口都留着。"""
    out: list[tuple[float, float]] = []
    cursor = start
    for a, b in silences:
        if a > start and b < stop and b - a > cap:
            out.append((cursor, a + cap / 2))
            cursor = b - cap / 2
    out.append((cursor, stop))
    return out


def _slice(src: Path, spans: Sequence[tuple[float, float]], dests: Sequence[Path],
           silences: Sequence[tuple[float, float]], cap: float) -> None:
    """按采样点切 wav。相邻两句之间没剪东西时采样点是连续的,拼回去和原来一样。"""
    with wave.open(str(src), "rb") as r:
        params = r.getparams()
        rate = r.getframerate()
        for k, ((start, stop), dest) in enumerate(zip(spans, dests, strict=True)):
            ranges = _kept(start, stop, silences, cap)
            frames = bytearray()
            for n, (lo, hi) in enumerate(ranges):
                r.setpos(round(lo * rate))
                chunk = r.readframes(max(0, round(hi * rate) - round(lo * rate)))
                head = n > 0 or (k > 0 and start > spans[k - 1][1])
                tail = n < len(ranges) - 1 or (k < len(spans) - 1 and spans[k + 1][0] > stop)
                frames += _faded(chunk, rate, head=head, tail=tail)
            with wave.open(str(dest), "wb") as w:
                w.setparams(params)
                w.writeframes(bytes(frames))


def split_take(raw: Path, texts: Sequence[str], words: Sequence[tuple[str, float, float]],
               work: Path, stem: str, planned: Sequence[float]) -> list[Path] | None:
    """把一条 take 切成逐句的 wav,拼回去不用再加静音。对不上返回 None。
    句间停顿不超过 `TTS_PAUSE_CAP` 就原样留着;更长的抽掉中间。`planned` 里更长的计划停顿优先。"""
    wav = work / f"{stem}.wav"
    proc = subprocess.run(
        [str(config.FFMPEG_PATH), "-y", "-v", "error", "-i", str(raw),
         "-ar", "48000", "-ac", "1", "-sample_fmt", "s16", str(wav)],
        capture_output=True, text=True, timeout=120)
    if proc.returncode != 0 or not wav.is_file():
        return None
    with wave.open(str(wav), "rb") as r:
        duration = r.getnframes() / float(r.getframerate())
    silences = _silences(wav)
    cuts = find_cuts(texts, words, duration, silences)
    if cuts is None:
        return None

    cap = config.TTS_PAUSE_CAP
    spans: list[tuple[float, float]] = []
    start = 0.0
    for k, (speech_end, speech_start) in enumerate(cuts):
        keep = max(cap, planned[k])
        middle = (speech_end + speech_start) / 2
        spans.append((start, min(speech_end + keep / 2, middle)))
        start = max(speech_start - keep / 2, middle)
    spans.append((start, duration))

    seconds = [b - a for a, b in spans]
    if min(seconds) < audio.MIN_CLIP_SECONDS:
        return None
    rates = {k: voice_qa.spoken_chars(texts[k]) / seconds[k] for k in range(len(texts))
             if voice_qa.spoken_chars(texts[k]) >= RATE_MIN_CHARS}
    if rates:
        mid = statistics.median(rates.values())
        if any(not RATE_LIMITS[0] <= r / mid <= RATE_LIMITS[1] for r in rates.values()):
            return None

    dests = [work / f"{stem}_{k:02d}.wav" for k in range(len(texts))]
    _slice(wav, spans, dests, silences, cap)
    return dests


def synthesize(positions: Sequence[int], texts: Sequence[str], pauses: Sequence[float], *,
               voice_id: str, speed: float, emotion: str, output_dir: Path,
               stem: str) -> TakeResult | None:
    """合成一条 take 并切开。`texts` / `pauses` 是全片的,`positions` 是这一条包含的句子。
    单句的 take 也走这里:少一次切分,但语气参数一致。失败返回 None,由调用方退回逐句。"""
    picked = [texts[i] for i in positions]
    planned = [pauses[i] for i in positions]
    ssml = to_ssml(picked, planned)
    pieces = None
    try:
        output_dir.mkdir(parents=True, exist_ok=True)
        raw = output_dir / f"{stem}.mp3"
        for _ in range(TAKE_RETRIES + 1):
            data, words = ark_tts.synthesize_take(ssml, voice_id=voice_id, speed=speed,
                                                  emotion=emotion)
            raw.write_bytes(data)
            pieces = split_take(raw, picked, words, output_dir, stem, planned)
            if pieces is not None:
                break
    except Exception as exc:  # noqa: BLE001 - 退回逐句,不拦出片
        log.warning("整段合成失败,退回逐句(第 %d 句起):%s", positions[0] + 1, exc)
        return None
    if pieces is None:
        log.warning("整段合成切不开,退回逐句(第 %d 句起)", positions[0] + 1)
        return None
    out = TakeResult()
    for k, pos in enumerate(positions):
        out.assets[pos] = audio.AudioAsset(text=texts[pos], local_path=pieces[k],
                                           node_id=TAKE_NODE, index=pos)
    return out
