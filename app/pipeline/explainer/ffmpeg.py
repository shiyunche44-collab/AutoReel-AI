"""ffmpeg 背景片段:裁黑边、适配画幅、拼接与转场。"""
from __future__ import annotations

import functools
import re
import shutil
import subprocess
import zlib
from dataclasses import replace
from pathlib import Path
from typing import Sequence
from ... import config
from .. import aspect, library
from .. import render as render_mod

from .consts import STILL_MOTIONS, XFADE, XFADE_KINDS, _ENC


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


def _clip_luma(clip: Path, frame: Path) -> float:
    """一段背景片开头和中后段两帧里较暗那帧的平均亮度(0–255)。抽不出来按"不暗"算,不拦。
    只看中间会漏掉源片淡入:前 0.3 秒是黑的,大字就弹在黑底上。"""
    lumas: list[float] = []
    for n, at in enumerate((0.2, 0.7)):
        dest = frame.with_name(f"{frame.stem}_{n}.png")
        proc = _ff(["-ss", f"{at:.2f}", "-i", str(clip), "-frames:v", "1", str(dest)], timeout=60)
        if proc.returncode == 0 and dest.is_file():
            lumas.append(library._luma(str(dest)))
    return min(lumas) if lumas else 255.0
