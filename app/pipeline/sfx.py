"""音效:章节切换 whoosh、关键词弹出 pop。

素材不外带版权库,第一次用时在 `data/sfx/` 里合成(纯 numpy,确定性,几十毫秒),
之后读缓存。`build_track` 把事件按时间摆进一条与成片等长的 wav,由 `render._mix_audio`
当第三路混进去(和旁白、BGM 一起过 loudnorm)。
"""
from __future__ import annotations

import logging
import wave
from pathlib import Path
from typing import Any, Mapping, Sequence

import numpy as np

from .. import config

_log = logging.getLogger(__name__)

SR = 48000
SFX_DIR = config.DATA_DIR / "sfx"
#: 各音效相对旁白的电平(旁白 ≈ 0 dBFS 峰值,音效压在底下)
GAINS = {"pop": 0.22, "whoosh": 0.30}
#: 弹字音效至少隔这么久,免得一句话里每个数字都"啵"
POP_MIN_GAP = 1.6
WHOOSH_LEAD = 0.12


def _pop() -> np.ndarray:
    t = np.arange(int(SR * 0.11)) / SR
    freq = 1100 - 5200 * t  # 下滑的短促"啵"
    tone = np.sin(2 * np.pi * np.cumsum(freq) / SR) * np.exp(-t * 38)
    click = np.exp(-t * 400) * np.sin(2 * np.pi * 3200 * t) * 0.5
    return tone + click


def _whoosh() -> np.ndarray:
    n = int(SR * 0.5)
    rng = np.random.default_rng(7)  # 固定种子:每次合成结果一致
    noise = rng.standard_normal(n)
    u = np.linspace(0, 1, n)
    env = np.sin(np.pi * u) ** 2.2 * (0.35 + 0.65 * u)  # 渐强、尾部收得快
    alpha = 0.02 + 0.5 * u  # 单极点低通的截止随时间升高 = 由闷到亮的"呼"
    out = np.empty(n)
    acc = 0.0
    for i in range(n):
        acc += alpha[i] * (noise[i] - acc)
        out[i] = acc
    out *= env
    return out / max(1e-9, float(np.abs(out).max()))


def _write(path: Path, data: np.ndarray) -> None:
    peak = float(np.abs(data).max()) or 1.0
    pcm = (data / peak * 0.9 * 32767).astype("<i2")
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(pcm.tobytes())


def _read(path: Path) -> np.ndarray:
    with wave.open(str(path), "rb") as w:
        raw = w.readframes(w.getnframes())
    return np.frombuffer(raw, dtype="<i2").astype(np.float64) / 32768.0


def sample(kind: str) -> np.ndarray:
    """一个音效的单声道采样(-1~1);缓存在 data/sfx/<kind>.wav,坏了就重新合成。"""
    path = SFX_DIR / f"{kind}.wav"
    try:
        if path.is_file():
            return _read(path)
    except (OSError, wave.Error, ValueError):
        pass
    data = {"pop": _pop, "whoosh": _whoosh}[kind]()
    try:
        SFX_DIR.mkdir(parents=True, exist_ok=True)
        _write(path, data)
        return _read(path)
    except OSError:
        return data / (float(np.abs(data).max()) or 1.0) * 0.9


def events_for(cues: Sequence[Mapping[str, Any]], chapter_starts: Sequence[float]
               ) -> list[tuple[float, str]]:
    """事件表 [(秒, 音效名)]:每个章节切换一个 whoosh(提前一点,"呼"完正好换章),
    关键词弹出(`caption_layer.build_layer` 里 hot 词的起点)一个 pop,同类间隔不小于 `POP_MIN_GAP`。"""
    events = [(max(0.0, float(t) - WHOOSH_LEAD), "whoosh") for t in chapter_starts if t > 0.5]
    last = -POP_MIN_GAP
    for cue in cues:
        for w in cue.get("words") or []:
            if w.get("hot") and float(w["start"]) - last >= POP_MIN_GAP:
                last = float(w["start"])
                events.append((last, "pop"))
                break  # 一条字幕最多一个
    return sorted(events)


def build_track(events: Sequence[tuple[float, str]], seconds: float, dest: Path) -> Path | None:
    """事件按时间摆进一条 `seconds` 秒的 48k 单声道 wav。没有事件 / 写失败返回 None。"""
    if not events or seconds <= 0:
        return None
    buf = np.zeros(int(seconds * SR) + SR)
    for t, kind in events:
        if kind not in GAINS:
            continue
        s = sample(kind) * GAINS[kind]
        i = int(t * SR)
        if i >= len(buf):
            continue
        j = min(len(buf), i + len(s))
        buf[i:j] += s[: j - i]
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        with wave.open(str(dest), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(SR)
            w.writeframes((np.clip(buf[: int(seconds * SR)], -1, 1) * 32767).astype("<i2").tobytes())
    except OSError as exc:
        _log.warning("音效轨写入失败:%s", exc)
        return None
    return dest
