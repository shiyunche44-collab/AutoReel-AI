"""动感:段内 25% / 50% / 75% 三帧的帧差。不调模型。

推拉摇移、行驶中的镜头帧差大;静物摆拍、空镜帧差接近压缩噪点。段是按场景切点切开的,
三帧之间一般不跨硬切,所以帧差量的就是镜头本身的运动。

一个窗文件(下载下来的一段原片)只解码一次:按 `MOTION_FPS` 抽成小灰度图,
窗里所有段都从这一叠帧里取 —— 比每段起一次 ffmpeg 快一个数量级。
"""
from __future__ import annotations

import logging
import subprocess
from collections.abc import Callable, Sequence
from pathlib import Path

import numpy as np

from .model import Item, Library, pool_lock

log = logging.getLogger(__name__)

#: 取帧位置(段长的比例)
MOTION_POINTS = (0.25, 0.5, 0.75)
#: 缩到多大比:16:9 的小灰度图,压缩噪点被平均掉,运动还留着
MOTION_SIZE = (64, 36)
#: 整窗解码时每秒抽几帧(取帧位置就近取,误差半帧 = 0.05 秒)
MOTION_FPS = 10
#: 帧差(灰度均值绝对差,0–255,按 1 秒间隔归一后)的噪声底和满分刻度:噪声底以下算 0
#: (静止镜头的压缩噪点),噪声底 + 刻度 算 1。2026-11-18 在本地 80 段 1080p 可用段上标定:
#: 归一帧差的 10 / 25 / 50 / 75 / 90 分位是 4.8 / 10.9 / 20.3 / 32.5 / 47.5,
#: 对应分数约 0.04 / 0.16 / 0.35 / 0.59 / 0.89
MOTION_NOISE = 3.0
MOTION_SCALE = 50.0
#: 取帧间隔按 1 秒归一:两帧隔得越久帧差越大,短段和长段同一把尺子。
#: 只开平方(运动不是匀速的,线性归一会把长段压得太狠)
MOTION_REF_GAP = 1.0
#: 比这个短的段不测(闪白、转场)
MOTION_MIN_SECONDS = 0.6
#: 窗文件最长解码这么久(秒);整条下载的源最长 6 分钟,10 帧 / 秒的小图不到 10MB
MOTION_TIMEOUT = 180


def window_frames(path: str | Path, fps: int = MOTION_FPS,
                  size: tuple[int, int] = MOTION_SIZE) -> np.ndarray | None:
    """把一个视频文件按 `fps` 抽成灰度小图,形状 (帧数, 高, 宽);第 k 帧约在 k / fps 秒。
    读不了返回 None。"""
    from ...providers import clips as clips_mod

    w, h = size
    try:
        proc = subprocess.run(
            [clips_mod._ffmpeg(), "-hide_banner", "-nostdin", "-v", "error", "-i", str(path),
             "-an", "-vf", f"fps={fps},scale={w}:{h},format=gray",
             "-f", "rawvideo", "-pix_fmt", "gray", "-"],
            capture_output=True, timeout=MOTION_TIMEOUT)
    except Exception:  # noqa: BLE001 - 找不到 ffmpeg / 超时 / 文件坏:这一窗不测,不拦建池
        return None
    buf = proc.stdout or b""
    n = len(buf) // (w * h)
    if proc.returncode != 0 or n < 2:
        return None
    return np.frombuffer(buf[:n * w * h], dtype=np.uint8).reshape(n, h, w).astype(np.float32)


def score(frames: Sequence[np.ndarray], gap: float) -> float:
    """相邻帧的平均绝对差 → 0–1 的动感分。`gap` 是相邻两帧隔的秒数。

    取几对帧差里**最小**的:场景检测漏掉的硬切 / 闪白只影响一对,真在动的镜头每对都大。"""
    if len(frames) < 2:
        return 0.0
    mad = min(float(np.mean(np.abs(b - a))) for a, b in zip(frames, frames[1:]))
    mad /= max(0.25, gap / MOTION_REF_GAP) ** 0.5
    return round(min(1.0, max(0.0, (mad - MOTION_NOISE) / MOTION_SCALE)), 3)


def segment_score(stack: np.ndarray, start: float, end: float,
                  fps: int = MOTION_FPS) -> float | None:
    """从整窗的帧叠里取一段的三帧算分。段太短、超出帧叠 → None。"""
    length = end - start
    if length < MOTION_MIN_SECONDS:
        return None
    idx = [min(len(stack) - 1, max(0, round((start + length * p) * fps))) for p in MOTION_POINTS]
    if len(set(idx)) < len(idx) or (start + length * MOTION_POINTS[-1]) * fps > len(stack):
        return None
    gap = (idx[1] - idx[0]) / fps
    return score([stack[k] for k in idx], gap)


def measure_many(items: Sequence[Item]) -> dict[str, float]:
    """一批视频段的动感分(段 id → 分)。同一个窗文件只解码一次;测不了的段不在结果里。"""
    by_path: dict[str, list[Item]] = {}
    for i in items:
        if i.kind == "video" and i.length >= MOTION_MIN_SECONDS and i.path:
            by_path.setdefault(i.path, []).append(i)
    out: dict[str, float] = {}
    for path, segs in by_path.items():
        if not Path(path).is_file():
            continue
        stack = window_frames(path)
        if stack is None:
            log.info("动感:读不了 %s", path)
            continue
        for i in segs:
            value = segment_score(stack, i.start, i.end)
            if value is not None:
                out[i.id] = value
    return out


def measure(item: Item) -> float | None:
    """一段视频的动感分。不是视频、太短、文件读不了 → None(保持没测过)。"""
    return measure_many([item]).get(item.id)


def needs_motion(item: Item) -> bool:
    """还没测动感、值得测的段。低清段不测:不当背景,重拉高清后新段另测。"""
    return item.kind == "video" and item.motion < 0 and not item.skipped and not item.low_res


def fill_motion(subject: str, *, should_stop: Callable[[], bool] | None = None,
                chunk: int = 20) -> int:
    """给这台车池子里没测过动感的视频段补上 `motion`(回填老池子)。返回测上的段数。

    解码在锁外做(一个池子几百个窗,几分钟);每测完 `chunk` 个窗文件,拿池锁、重新 load、
    把分数按段 id 写回、存盘 —— 不拿着旧副本覆盖建池 / 重拉期间别人写的东西。
    中途停了下次接着测(测过的不重测)。"""
    todo = [i for i in Library.load(subject).items if needs_motion(i)]
    by_path: dict[str, list[Item]] = {}
    for i in todo:
        by_path.setdefault(i.path, []).append(i)
    paths = list(by_path)
    n = 0
    for k in range(0, len(paths), max(1, chunk)):
        if should_stop and should_stop():
            break
        got = measure_many([i for p in paths[k:k + chunk] for i in by_path[p]])
        if not got:
            continue
        with pool_lock(subject):
            lib = Library.load(subject)
            for i in lib.items:
                if i.id in got and i.motion < 0:
                    i.motion = got[i.id]
                    n += 1
            lib.save()
    return n
