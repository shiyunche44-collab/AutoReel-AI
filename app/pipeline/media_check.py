"""素材的技术体检:分辨率、码率、编码,以及不调视觉模型的本地帧检查。

都只读本地文件(ffprobe / PIL),不联网、不花视觉模型额度。
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

from app import config

#: 视频短边低于它算低清(480×852 的竖屏转载、360p 搬运),不进背景
MIN_VIDEO_SHORT_SIDE = 720
#: 静图长边低于它不当全屏背景(放大到 1920 宽会糊)
MIN_STILL_LONG_SIDE = 1280
#: H.264 1080p 的码率下限(kbps),其他分辨率按像素数折算
H264_1080P_KBPS = 1500
#: 新一代编码同样画质要的码率少得多:AV1 / HEVC / VP9 的下限打这个折
EFFICIENT_CODECS = {"av1": 0.7, "hevc": 0.7, "h265": 0.7, "vp9": 0.7}


def probe_video(path: str | Path) -> dict[str, Any]:
    """读视频流的宽、高、帧率、码率(kbps)、编码。读不了返回空字典。

    码率优先取视频流自己的;mp4 里没写的话用容器总码率减去音频流。
    """
    if not config.FFPROBE_PATH:
        return {}
    try:
        proc = subprocess.run(
            [str(config.FFPROBE_PATH), "-v", "error",
             "-show_entries", "stream=codec_type,codec_name,width,height,avg_frame_rate,bit_rate"
             ":format=bit_rate", "-of", "json", str(path)],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60)
        data = json.loads(proc.stdout or "{}")
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return {}
    streams = data.get("streams") or []
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    if not video:
        return {}
    audio_bps = sum(_int(s.get("bit_rate")) for s in streams if s.get("codec_type") == "audio")
    bps = _int(video.get("bit_rate"))
    if not bps:
        total = _int((data.get("format") or {}).get("bit_rate"))
        bps = max(0, total - audio_bps)
    return {
        "width": _int(video.get("width")),
        "height": _int(video.get("height")),
        "fps": _fps(video.get("avg_frame_rate")),
        "bitrate": round(bps / 1000),
        "vcodec": str(video.get("codec_name") or ""),
    }


def image_size(path: str | Path) -> tuple[int, int]:
    try:
        from PIL import Image

        with Image.open(path) as im:
            return im.size
    except Exception:  # noqa: BLE001 - 读不了就当不知道
        return 0, 0


def is_low_res_video(width: int, height: int) -> bool:
    """短边不到 720。尺寸未知(0)不判,老池子回填之前照常用。"""
    return bool(width and height) and min(width, height) < MIN_VIDEO_SHORT_SIDE


def is_small_still(width: int, height: int) -> bool:
    return bool(width and height) and max(width, height) < MIN_STILL_LONG_SIDE


def min_bitrate(width: int, height: int, vcodec: str) -> float:
    """这个分辨率、这个编码下不算"糊"的最低码率(kbps)。"""
    pixels = max(1, width * height) / (1920 * 1080)
    return H264_1080P_KBPS * pixels * EFFICIENT_CODECS.get(vcodec.lower(), 1.0)


def is_low_bitrate(width: int, height: int, bitrate: int, vcodec: str) -> bool:
    """码率低于该分辨率 / 编码的下限。任一参数未知不判。

    只拿来排序(同档里码率够的先用),不拦:小米官方号的 AV1 普遍只有 400–700k,
    拦掉等于把这台车的视频大半赶回静图。
    """
    if not (width and height and bitrate):
        return False
    return bitrate < min_bitrate(width, height, vcodec)


#: 帧平均亮度低于它算黑(和 library.DARK_LUMA 一致)
DARK_LUMA = 28
#: 体检取几帧(段首、中、尾)
HEALTH_FRAMES = 3
HEALTH_SIZE = (320, 180)
#: 320×180 灰度图的边缘方差低于它算"平"(纯色底、淡入淡出、整屏虚焦)。
#: 按现有池子标定:可用段 p5≈500,视觉模型判 unclear 的段里最平的那几个在 0–120。
#: 再往上和可用段重叠(运动模糊、景深虚化的展示镜头边缘方差也不高),不拿它判糊
FLAT_SHARPNESS = 120.0


def frame_stats(video: str | Path, start: float, end: float) -> list[tuple[float, float]]:
    """段里均匀取 HEALTH_FRAMES 帧,每帧 (平均亮度, 边缘方差)。一次 ffmpeg 调用,
    缩到 320×180 灰度直接从管道读。读不了返回空列表。"""
    from app.providers import clips as clips_mod

    length = max(0.1, end - start)
    w, h = HEALTH_SIZE
    try:
        proc = subprocess.run(
            [clips_mod._ffmpeg(), "-hide_banner", "-nostdin", "-v", "error",
             "-ss", f"{max(0.0, start):.3f}", "-t", f"{length:.3f}", "-i", str(video),
             "-an", "-vf", f"fps={HEALTH_FRAMES / length:.4f},scale={w}:{h}",
             "-frames:v", str(HEALTH_FRAMES), "-f", "rawvideo", "-pix_fmt", "gray", "-"],
            capture_output=True, timeout=120)
    except (OSError, subprocess.TimeoutExpired):
        return []
    buf = proc.stdout or b""
    out = []
    for k in range(len(buf) // (w * h)):
        out.append(_gray_stats(buf[k * w * h:(k + 1) * w * h], (w, h)))
    return out


def image_stats(path: str | Path) -> tuple[float, float] | None:
    """一张图的 (平均亮度, 边缘方差),和 `frame_stats` 同一把尺子。"""
    try:
        from PIL import Image

        with Image.open(path) as im:
            g = im.convert("L").resize(HEALTH_SIZE)
            return _gray_stats(g.tobytes(), HEALTH_SIZE)
    except Exception:  # noqa: BLE001
        return None


def frame_verdict(stats: list[tuple[float, float]]) -> str:
    """"" = 没问题;frame_dark = 取的帧全黑;frame_flat = 多数帧没有内容(纯色、淡场)。
    没取到帧不判。"""
    if not stats:
        return ""
    if all(luma < DARK_LUMA for luma, _ in stats):
        return "frame_dark"
    sharp = sorted(s for _, s in stats)
    if sharp[len(sharp) // 2] < FLAT_SHARPNESS:
        return "frame_flat"
    return ""


def median_sharpness(stats: list[tuple[float, float]]) -> float:
    if not stats:
        return -1.0
    sharp = sorted(s for _, s in stats)
    return round(sharp[len(sharp) // 2], 1)


def _gray_stats(buf: bytes, size: tuple[int, int]) -> tuple[float, float]:
    import numpy as np
    from PIL import Image, ImageFilter

    g = Image.frombytes("L", size, buf)
    edges = np.asarray(g.filter(ImageFilter.FIND_EDGES), dtype=np.float32)[2:-2, 2:-2]
    return float(np.asarray(g, dtype=np.float32).mean()), float(edges.var())


def _int(v: Any) -> int:
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return 0


def _fps(v: Any) -> float:
    try:
        num, _, den = str(v).partition("/")
        return round(float(num) / float(den or 1), 3) if float(den or 1) else 0.0
    except (TypeError, ValueError, ZeroDivisionError):
        return 0.0
