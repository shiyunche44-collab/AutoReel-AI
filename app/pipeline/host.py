"""虚拟主持人「柒哥」:成片角落常驻一个 Q版形象,嘴型跟着旁白动。

素材(`app/assets/host/<id>/` 的 closed / half / open / blink 四张透明 PNG)由
`scripts/host_sprites.py` 一次性生成。这里只做两件事:

1. `mouth_track`:按旁白音量逐帧定嘴型。字幕 cue 区间外一律闭嘴;每 3–5 秒眨一次眼(只在闭嘴时)。
2. `build`:把逐帧状态压成 concat 清单(状态不变的连续帧合成一段),交给 render.compose 叠到角落。

全屏卡片 / 概念动画 / 数字卡拍(`HIDE_LAYOUTS`)期间隐藏,片尾卡不覆盖。任何失败都返回 None,成片照出。
"""
from __future__ import annotations

import logging
import random
import wave
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image

from .. import config
from . import aspect

log = logging.getLogger(__name__)

ASSET_ROOT = Path(__file__).resolve().parents[1] / "assets" / "host"
STATES = ("closed", "half", "open", "blink")
HIDDEN = "hidden"
#: 全屏卡片 / 概念动画会被挡;stat / panel 的数字卡画在竖屏数据区,和主持人同一位置
HIDE_LAYOUTS = frozenset({"card", "concept", "stat", "panel"})
#: 音量(相对 cue 内 90 分位)超过这两个阈值分别是半张 / 张大
HALF_AT, OPEN_AT = 0.18, 0.55
#: 嘴型最少保持帧数,防止逐帧乱跳
MIN_HOLD = 2
BLINK_FRAMES = 3


def sprite_dir(host_id: str | None = None) -> Path:
    return ASSET_ROOT / (host_id or config.HOST_ID)


def available(host_id: str | None = None) -> bool:
    d = sprite_dir(host_id)
    return all((d / f"{s}.png").is_file() for s in STATES)


def _rms(wav_path: Path, fps: int, n_frames: int) -> np.ndarray:
    """逐视频帧的音量(RMS)。读不了返回全 0。"""
    try:
        with wave.open(str(wav_path), "rb") as w:
            ch, width, rate = w.getnchannels(), w.getsampwidth(), w.getframerate()
            raw = w.readframes(w.getnframes())
    except (OSError, wave.Error, EOFError):
        return np.zeros(n_frames)
    dtype = {1: np.int8, 2: np.int16, 4: np.int32}.get(width)
    if dtype is None:
        return np.zeros(n_frames)
    pcm = np.frombuffer(raw, dtype=dtype).astype(np.float32)
    if ch > 1:
        pcm = pcm[: len(pcm) // ch * ch].reshape(-1, ch).mean(axis=1)
    hop = rate / fps
    out = np.zeros(n_frames)
    for i in range(n_frames):
        seg = pcm[int(i * hop): int((i + 1) * hop)]
        if len(seg):
            out[i] = float(np.sqrt(np.mean(seg * seg)))
    return out


def mouth_track(rms: Sequence[float], cues: Sequence[dict], fps: int,
                seed: int = 0) -> list[str]:
    """逐帧嘴型:cue 外闭嘴;cue 内按相对音量分 closed / half / open;闭嘴时偶尔眨眼。"""
    n = len(rms)
    level = np.asarray(rms, dtype=float)
    speaking = np.zeros(n, bool)
    for c in cues:
        a = max(0, int(float(c.get("start", 0)) * fps))
        b = min(n, int(float(c.get("end", 0)) * fps + 0.5))
        speaking[a:b] = True
    ref = float(np.percentile(level[speaking], 90)) if speaking.any() else 0.0
    states: list[str] = []
    for i in range(n):
        if not speaking[i] or ref <= 0:
            states.append("closed")
            continue
        r = level[i] / ref
        states.append("open" if r >= OPEN_AT else "half" if r >= HALF_AT else "closed")
    # 最短保持:短于 MIN_HOLD 的段并入前一段
    i = 0
    while i < n:
        j = i
        while j < n and states[j] == states[i]:
            j += 1
        if j - i < MIN_HOLD and i > 0:
            for k in range(i, j):
                states[k] = states[i - 1]
        i = j
    rng = random.Random(seed)
    t = int(fps * rng.uniform(1.5, 3.0))
    while t < n:
        if all(s == "closed" for s in states[t:t + BLINK_FRAMES]):
            for k in range(t, min(n, t + BLINK_FRAMES)):
                states[k] = "blink"
        t += int(fps * rng.uniform(3.0, 5.0))
    return states


def hide_spans(layouts: Sequence[str], durations: Sequence[float], fps: int,
               n_frames: int) -> np.ndarray:
    """全屏拍对应的帧标 True。"""
    mask = np.zeros(n_frames, bool)
    t = 0.0
    for lay, dur in zip(layouts, durations, strict=False):
        if lay in HIDE_LAYOUTS:
            mask[int(t * fps): min(n_frames, int((t + dur) * fps))] = True
        t += dur
    return mask


def placement(size: tuple[int, int], sprite_h: int, sprite_w: int) -> tuple[int, int]:
    """左下角,避开右侧平台按钮区。竖屏:站在实拍带下方的数据区(不挡实拍),脚落在字幕区上沿;
    横屏:左下留边。"""
    w, h = size
    if aspect.is_portrait_size(size):
        g = aspect.portrait_grid(size)
        return int(w * aspect.PORTRAIT_MARGIN), g.data.bottom - sprite_h
    return int(w * 0.03), h - sprite_h - int(h * 0.03)


def sprite_height(size: tuple[int, int]) -> int:
    w, h = size
    if aspect.is_portrait_size(size):
        g = aspect.portrait_grid(size)
        return g.data.bottom - g.band.bottom - 8
    return int(h * 0.30)


def _segments(states: Sequence[str], fps: int) -> list[tuple[str, float]]:
    segs: list[tuple[str, float]] = []
    for s in states:
        if segs and segs[-1][0] == s:
            segs[-1] = (s, segs[-1][1] + 1 / fps)
        else:
            segs.append((s, 1 / fps))
    return segs


def build(work_dir: Path, nar: dict, size: tuple[int, int], durations: Sequence[float],
          layouts: Sequence[str], fps: int, seed: int = 0,
          host_id: str | None = None) -> tuple[Path, int, int] | None:
    """出主持人层:(concat 清单, x, y)。`durations`/`layouts` 只含正片各拍(不含片尾卡)。"""
    if not config.HOST_ENABLED or not available(host_id):
        return None
    try:
        total = float(sum(durations))
        n = max(1, int(round(total * fps)))
        wav = Path(nar.get("narration_path") or "")
        rms = _rms(wav, fps, n) if wav.is_file() else np.zeros(n)
        states = mouth_track(rms, nar.get("cues") or [], fps, seed=seed)
        hide = hide_spans(layouts, durations, fps, n)
        states = [HIDDEN if hide[i] else s for i, s in enumerate(states)]

        out = work_dir / "_host"
        out.mkdir(parents=True, exist_ok=True)
        sh = sprite_height(size)
        files: dict[str, Path] = {}
        sw = 0
        for s in STATES:
            im = Image.open(sprite_dir(host_id) / f"{s}.png").convert("RGBA")
            im = im.resize((round(im.width * sh / im.height), sh), Image.LANCZOS)
            sw = im.width
            files[s] = out / f"{s}.png"
            im.save(files[s])
        files[HIDDEN] = out / "hidden.png"
        Image.new("RGBA", (sw, sh), (0, 0, 0, 0)).save(files[HIDDEN])

        lines: list[str] = []
        for s, dur in _segments(states, fps):
            lines += [f"file '{files[s].as_posix()}'", f"duration {dur:.4f}"]
        lines.append(f"file '{files[_segments(states, fps)[-1][0]].as_posix()}'")
        listing = out / "host.txt"
        listing.write_text("\n".join(lines), encoding="utf-8")
        x, y = placement(size, sh, sw)
        return listing, x, y
    except Exception:  # noqa: BLE001 —— 主持人只是加分项,不能让成片失败
        log.warning("主持人层生成失败,成片不带主持人", exc_info=True)
        return None


def for_compose(project_dir: Path, nar: dict, size: tuple[int, int], beats: Sequence[Any],
                fps: int, seed: int = 0) -> tuple[Path, int, int] | None:
    """explainer.compose / recompose 的入口:当前 look 开了 host 才出。"""
    from . import looks

    if not looks.current().host:
        return None
    return build(project_dir / "_render", nar, size, [b.duration for b in beats],
                 [str(getattr(b, "layout", "") or "") for b in beats], fps, seed=seed)
