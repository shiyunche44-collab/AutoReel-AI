"""⑩ 合成渲染:镜头拼接 → 字幕烧录 → 旁白混音 → 响度归一化 → 封面。

渲染层采用**两段式拼接**:
1. 先把每个镜头单独归一化到 16:9 横屏(1920×1080)/ 24fps;
2. 再用 concat demuxer 以 `-c copy` 无损拼接。
比直接 concat 更稳 —— 生成片段的分辨率/帧率/时间戳可能不一致,
直接拼会得到花屏或音画漂移。

ffmpeg 不在系统 PATH,因此全部通过 `config.FFMPEG_PATH` 显式调用;
ffprobe 用于时长探测。所有子进程都用 list 形式传参(不过 shell),
避免 Windows 下的引号与注入问题。
"""
from __future__ import annotations

import logging
import os
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .. import config, degrade
from . import final_qa
from . import timeline as timeline_mod

log = logging.getLogger(__name__)

FPS = 30
TARGET_LOUDNESS = f"I={config.TARGET_LUFS}:TP=-1.5:LRA=11"


class RenderError(RuntimeError):
    """渲染失败。"""


@dataclass
class RenderResult:
    ok: bool
    output_path: Path | None = None
    cover_path: Path | None = None
    duration: float = 0.0
    warnings: list[str] | None = None
    log: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "output_path": str(self.output_path) if self.output_path else None,
            "cover_path": str(self.cover_path) if self.cover_path else None,
            "duration": round(self.duration, 3),
            "warnings": self.warnings or [],
        }


# --------------------------------------------------------------------------
# 底层
# --------------------------------------------------------------------------
def _ffmpeg() -> str:
    if not config.FFMPEG_PATH or not config.FFMPEG_PATH.is_file():
        raise RenderError(
            "未找到 ffmpeg。请在 .env 中配置 FFMPEG_BIN 指向 ffmpeg.exe 所在目录。"
        )
    return str(config.FFMPEG_PATH)


def _run(args: Sequence[str], *, timeout: int = 1800, cwd: Path | None = None) -> subprocess.CompletedProcess:
    cmd = [_ffmpeg(), "-hide_banner", "-nostdin", *args]
    return subprocess.run(
        cmd, capture_output=True, text=True, encoding="utf-8",
        errors="replace", timeout=timeout, cwd=str(cwd) if cwd else None,
    )


#: 统一调色:对比 +4%、饱和 +6%,轻微时域颗粒(盖住压缩的色带)。幅度刻意小,
#: 只为让不同来源的素材看起来像同一支片,不改变车漆等产品颜色的判断
GRADE_FILTER = "eq=contrast=1.04:saturation=1.06,noise=alls=3:allf=t"


def _fontsdir_arg() -> str | None:
    """构造 libass 的 fontsdir 参数,转义 Windows 盘符冒号。"""
    fonts = Path(config.FONTS_DIR)
    if not fonts.is_dir():
        return None
    escaped = str(fonts).replace("\\", "/").replace(":", r"\:")
    return f"fontsdir='{escaped}'"


# --------------------------------------------------------------------------
# 第一段:镜头归一化
# --------------------------------------------------------------------------
def _size(size: tuple[int, int] | None) -> tuple[int, int]:
    """目标画幅。缺省是 16:9 横屏(解说轨的画幅)。"""
    out = size or (config.OUTPUT_WIDTH, config.OUTPUT_HEIGHT)
    if not config.frame_is_landscape(out):
        log.warning("渲染画幅不是 16:9:%sx%s", *out)
    return out


def _can_copy_video(src: str | Path, w: int, h: int) -> bool:
    """检查输入镜头是否已满足目标画幅、yuv420p 与 FPS,满足则无需二次重编码视频流。"""
    if not config.FFPROBE_PATH:
        return False
    try:
        proc = subprocess.run(
            [str(config.FFPROBE_PATH), "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=width,height,pix_fmt,r_frame_rate",
             "-of", "csv=p=0", str(src)],
            capture_output=True, text=True, timeout=15,
        )
        if proc.returncode != 0:
            return False
        line = (proc.stdout or "").strip().splitlines()[0]
        parts = line.split(",")
        return (len(parts) >= 4 and int(parts[0]) == w and int(parts[1]) == h
                and parts[2] == "yuv420p" and parts[3] in (f"{FPS}/1", str(FPS)))
    except Exception:  # noqa: BLE001
        return False


def frame_counts(durations: Sequence[float], fps: int = FPS) -> list[int]:
    """各拍该编成几帧:按**累计**时长的帧边界分配,拼接后画面时间轴 = 拍时长之和(差不过半帧)。

    每拍各自取整(1.422 秒 → 43 帧 = 1.433 秒)会让误差一路累加,68 秒的片画面晚近半秒,
    数据卡比旁白念到数字晚 0.2-0.4 秒。第 i 拍 = round(cum_i·fps) - round(cum_{i-1}·fps),
    每拍至少 1 帧(极短的拍挤占后一拍,边界之后照样回到累计值上)。"""
    out: list[int] = []
    edge, cum = 0, 0.0
    for d in durations:
        cum += max(0.0, float(d))
        nxt = max(edge + 1, int(round(cum * fps)))
        out.append(nxt - edge)
        edge = nxt
    return out


def video_frames(path: str | Path) -> int | None:
    """视频流实际帧数(数包,不解码);读不出返回 None。"""
    if not config.FFPROBE_PATH:
        return None
    try:
        proc = subprocess.run(
            [str(config.FFPROBE_PATH), "-v", "error", "-select_streams", "v:0", "-count_packets",
             "-show_entries", "stream=nb_read_packets", "-of", "csv=p=0", str(path)],
            capture_output=True, text=True, timeout=60,
        )
        return int((proc.stdout or "").strip().splitlines()[0].strip(" ,"))
    except (OSError, subprocess.SubprocessError, ValueError, IndexError):
        return None


def normalise_clip(src: str | Path, dest: str | Path, *, size: tuple[int, int] | None = None,
                   trim: float | None = config.SHOT_GRID_SECONDS,
                   frames: int | None = None) -> bool:
    """把单个镜头归一化为目标画幅(默认 1080×1920)/ 30fps / yuv420p / 静音音轨。

    竖屏 9:16 素材源分辨率不统一,统一 scale+crop 到目标画幅可保证拼接不花屏。
    统一生成静音音轨是必需的:concat 要求所有片段的流结构一致。
    `trim` 为 None 时不截断(横屏解说轨的节拍时长各不相同,已经在渲染时定好);
    `frames` 给了就精确编成这么多帧(多了截掉,少了重复末帧补齐,见 `frame_counts`)。
    若原镜头已满足目标分辨率、像素格式与帧率(且帧数正好),视频流直接 `-c:v copy` 免重编码。
    """
    w, h = _size(size)
    if frames is not None:
        return _fit_clip(src, dest, frames, w, h)
    if trim is None and _can_copy_video(src, w, h):
        proc = _run(
            [
                "-y", "-v", "error",
                "-i", str(src),
                "-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=48000",
                "-map", "0:v:0", "-map", "1:a:0",
                "-shortest",
                "-c:v", "copy",
                "-c:a", "aac", "-b:a", "128k",
                str(dest),
            ],
            timeout=600,
        )
        if proc.returncode == 0 and Path(dest).is_file() and Path(dest).stat().st_size > 0:
            return True
    proc = _run(
        [
            "-y", "-v", "error",
            # ⚠️ 所有 -i 必须排在输出选项之前。ffmpeg 按位置判定选项归属,
            #    把 -vf 放在第二个 -i 之前会被当成输入选项并报错。
            "-i", str(src),
            "-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=48000",
            "-vf",
            (
                f"scale={w}:{h}:force_original_aspect_ratio=increase,"
                f"crop={w}:{h},"
                f"fps={FPS},setsar=1,format=yuv420p"
            ),
            "-map", "0:v:0", "-map", "1:a:0",
            "-shortest",
            "-c:v", "libx264", "-preset", "medium", "-crf", "20",
            "-c:a", "aac", "-b:a", "128k",
            *(["-t", str(trim)] if trim else []),
            str(dest),
        ],
        timeout=600,
    )
    return proc.returncode == 0 and Path(dest).is_file() and Path(dest).stat().st_size > 0


def _fit_clip(src: str | Path, dest: str | Path, frames: int, w: int, h: int) -> bool:
    """归一化并精确到 `frames` 帧。静音音轨按同样时长生成。"""
    frames = max(1, int(frames))
    seconds = f"{frames / FPS:.6f}"
    silent = ["-f", "lavfi", "-t", seconds, "-i", "anullsrc=channel_layout=stereo:sample_rate=48000"]
    audio = ["-map", "1:a:0", "-c:a", "aac", "-b:a", "128k", "-t", seconds]
    if _can_copy_video(src, w, h) and video_frames(src) == frames:
        proc = _run(["-y", "-v", "error", "-i", str(src), *silent, "-map", "0:v:0",
                     "-c:v", "copy", *audio, str(dest)], timeout=600)
        if proc.returncode == 0 and video_frames(dest) == frames:
            return True
    # tpad 先在末尾补足够多的重复末帧,-frames:v 再精确截断:短了补、长了裁,一条命令搞定
    proc = _run(
        [
            "-y", "-v", "error", "-i", str(src), *silent,
            "-vf",
            (f"scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h},"
             f"fps={FPS},setsar=1,format=yuv420p,tpad=stop_mode=clone:stop={frames}"),
            "-map", "0:v:0", "-frames:v", str(frames),
            "-c:v", "libx264", "-preset", "medium", "-crf", "20",
            *audio, str(dest),
        ],
        timeout=600,
    )
    return proc.returncode == 0 and Path(dest).is_file() and Path(dest).stat().st_size > 0


#: 静图镜头的动效。连拼背景里按片段 id 散开挑、且不与上一刀相同(explainer.motion_for),
#: 避免满屏都是同一种推近:
#:   zoom_in / zoom_out   居中推近 / 拉远
#:   pan_left / pan_right / pan_up   放大后横移 / 上移
#:   push_diag            左上 → 右下的对角推近
#:   punch_in             0.45 秒内快推到 1.28 后停住(强调)
#:   drift_zoom           边推近边往右漂
STILL_MOTIONS: tuple[str, ...] = ("zoom_in", "pan_right", "zoom_out", "pan_left",
                                  "push_diag", "punch_in", "pan_up", "drift_zoom")


def ease_expr(frames: int) -> str:
    """zoompan 用的进度表达式 0→1:线性与 smoothstep 各占一半。

    起步缓、收尾不停死:全程 ease-in-out 会在硬切前慢下来,像卡了一下;
    半线性半缓动保留切点处的动势,同时去掉机械的匀速感。"""
    t = f"on/{frames}"
    return f"(0.5*{t}+0.25*(1-cos(PI*{t})))"


def still_filter(motion: str, seconds: float, size: tuple[int, int] | None = None) -> str:
    """静图 → 9:16 动效镜头的滤镜图。

    两步:
    1. **模糊铺底 + 完整前景**:横图直接裁 9:16 会把车头车尾切掉(和横屏视频
       同样的问题),所以前景按"完整放进画幅"缩放,空出的上下用同一张图
       放大模糊铺满。
    2. **zoompan 推拉 / 平移**:先放大到 2 倍再 zoompan,否则逐帧取整会抖。
    """
    w, h = _size(size)
    frames = max(1, int(round(seconds * FPS)))
    e = ease_expr(frames)
    center_x, center_y = "iw/2-(iw/zoom/2)", "ih/2-(ih/zoom/2)"
    if motion == "zoom_out":
        z, x, y = f"1.15-0.15*{e}", center_x, center_y
    elif motion == "pan_left":
        z, x, y = "1.12", f"(iw-iw/zoom)*{e}", center_y
    elif motion == "pan_right":
        z, x, y = "1.12", f"(iw-iw/zoom)*(1-{e})", center_y
    elif motion == "pan_up":
        z, x, y = "1.12", center_x, f"(ih-ih/zoom)*(1-{e})"
    elif motion == "push_diag":
        z, x, y = f"1.04+0.16*{e}", f"(iw-iw/zoom)*{e}", f"(ih-ih/zoom)*{e}"
    elif motion == "punch_in":
        punch = max(1, min(frames, int(round(FPS * 0.45))))
        z = f"1+0.28*(1-pow(1-min(1,on/{punch}),3))"
        x, y = center_x, center_y
    elif motion == "drift_zoom":
        z, x, y = f"1.06+0.12*{e}", f"(iw-iw/zoom)*(0.2+0.6*{e})", center_y
    else:  # zoom_in
        z, x, y = f"1+0.15*{e}", center_x, center_y
    return (
        f"[0:v]scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h},"
        f"boxblur=24:2,setsar=1[bg];"
        f"[0:v]scale={w}:{h}:force_original_aspect_ratio=decrease,setsar=1[fg];"
        f"[bg][fg]overlay=(W-w)/2:(H-h)/2,scale={w * 2}:{h * 2},"
        f"zoompan=z='{z}':x='{x}':y='{y}':d={frames}:s={w}x{h}:fps={FPS},"
        f"format=yuv420p[v]"
    )


def still_to_clip(image: str | Path, dest: str | Path, *, seconds: float | None = None,
                  motion: str = "zoom_in", size: tuple[int, int] | None = None) -> Path:
    """把一张图片做成定长动效镜头(默认 9:16)。失败抛 RenderError。"""
    seconds = float(seconds or config.SHOT_GRID_SECONDS)
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    proc = _run(
        [
            "-y", "-v", "error",
            "-i", str(image),
            "-filter_complex", still_filter(motion, seconds, size),
            "-map", "[v]",
            "-t", f"{seconds:.3f}", "-r", str(FPS),
            "-an", "-c:v", "libx264", "-preset", "medium", "-crf", "20",
            "-pix_fmt", "yuv420p", "-movflags", "+faststart",
            str(dest),
        ],
        timeout=600,
    )
    if proc.returncode != 0 or not dest.is_file() or dest.stat().st_size == 0:
        raise RenderError(f"静图转镜头失败({Path(image).name}):{(proc.stderr or '')[-300:]}")
    return dest


def normalise_all(shot_paths: Sequence[Path], work_dir: Path, *,
                  size: tuple[int, int] | None = None,
                  trim: float | None = config.SHOT_GRID_SECONDS,
                  durations: Sequence[float] | None = None) -> tuple[list[Path], list[str]]:
    """归一化全部镜头。返回 (成功列表, 警告列表)。

    `durations` 给了(和镜头一一对应)就按 `frame_counts` 把每拍编成精确帧数,
    拼接后画面时间轴 = 拍时长之和;缓存的 `norm_*.mp4` 帧数不对会重做。"""
    work_dir.mkdir(parents=True, exist_ok=True)
    ok: list[Path] = []
    warnings: list[str] = []
    targets: list[int | None] = (list(frame_counts(durations)) if durations is not None
                                 and len(durations) == len(shot_paths) else [None] * len(shot_paths))
    for i, (src, frames) in enumerate(zip(shot_paths, targets, strict=True)):
        dest = work_dir / f"norm_{i:03d}.mp4"
        if dest.is_file() and dest.stat().st_size > 0 and (frames is None
                                                           or video_frames(dest) == frames):
            ok.append(dest)
            continue
        try:
            if normalise_clip(src, dest, size=size, trim=trim, frames=frames):
                ok.append(dest)
            else:
                warnings.append(f"镜头 {i} 归一化失败,已跳过")
        except Exception as exc:  # noqa: BLE001
            warnings.append(f"镜头 {i} 归一化异常:{exc}")
    return ok, warnings


#: 章节交界短叠化的秒数
DISSOLVE_SECONDS = 0.25


def _dissolve_heads(clips: Sequence[Path], starts: Sequence[int], work_dir: Path,
                    size: tuple[int, int] | None = None) -> list[Path]:
    """这几个镜头的开头从上一镜最后一帧叠化进来。不改任何镜头的时长,旁白和字幕不受影响;
    只动画面,静音音轨原样带过。某一处做失败就留硬切,不拦成片。"""
    out = list(clips)
    for k in starts:
        if not 0 < k < len(clips):
            continue
        dest = work_dir / f"dissolve_{k:03d}.mp4"
        graph = (f"[0:v]tpad=stop_mode=clone:stop_duration=1,trim=duration={DISSOLVE_SECONDS},"
                 f"setpts=PTS-STARTPTS[a];[a][1:v]xfade=transition=fade:"
                 f"duration={DISSOLVE_SECONDS}:offset=0,format=yuv420p[v]")
        proc = _run(["-y", "-v", "error", "-sseof", "-0.1", "-i", str(clips[k - 1]),
                     "-i", str(clips[k]), "-filter_complex", graph, "-map", "[v]", "-map", "1:a:0",
                     "-c:v", "libx264", "-preset", "medium", "-crf", "20", "-c:a", "copy", str(dest)])
        want = video_frames(clips[k])
        if proc.returncode == 0 and dest.is_file() and want is not None \
                and video_frames(dest) != want:
            # 叠化后帧数变了会把后面整条画面带偏:裁回原帧数,裁不了就留硬切
            fitted = work_dir / f"dissolve_{k:03d}_fit.mp4"
            if not _fit_clip(dest, fitted, want, *_size(size)):
                log.warning("章节叠化帧数对不上,留硬切: %s", dest.name)
                continue
            dest = fitted
        if proc.returncode == 0 and dest.is_file():
            out[k] = dest
        else:
            log.warning("章节叠化失败,留硬切: %s", (proc.stderr or "")[-200:])
    return out


# --------------------------------------------------------------------------
# 第二段:拼接 + 字幕 + 混音
# --------------------------------------------------------------------------
#: 混音电平(2026-11-18 起,配合按情绪选曲的 BGM 底床与少量采样音效):
#: 旁白先按实测响度用**静态增益**对齐到 NARRATION_LUFS;BGM 底床对齐到比旁白低 BGM_UNDER_LU;
#: 侧链在旁白出声时再让几 dB,release 350 ms,句间停顿里 BGM 能浮回来;
#: 混好后整体按实测做线性增益到 TARGET_LUFS,只过一道峰值限幅。全程不做动态响度归一,
#: 旁白和 BGM 的相对电平就是这里写的数。
NARRATION_LUFS = -16.0
BGM_UNDER_LU = 10.0
BGM_DUCK_THRESHOLD = 0.05
BGM_DUCK_RATIO = 3
BGM_DUCK_ATTACK = 20
BGM_DUCK_RELEASE = 350
#: 限幅器的天花板(dBFS)。alimiter 是采样峰值限幅,留 0.5 dB 余量给真峰值
LIMIT_DBFS = -2.0
#: 测不出响度时(ffmpeg 测量失败)退回动态 loudnorm:BGM 单独归一到这个电平
FALLBACK_BGM_LUFS = NARRATION_LUFS - BGM_UNDER_LU

#: BGM 淡入 / 淡出秒数(只用于没经 `edit/music.render_bed` 编排的原始曲子)。
#: 片太短(不够淡出)或不知道总时长时只淡入
BGM_FADE_IN = 1.5
BGM_FADE_OUT = 2.5
#: `music.render_bed` 出的底床旁边放的编排说明;有它就说明首尾淡变、拼接都已做好
BED_SIDECAR_SUFFIX = ".plan.json"


def _bgm_fades(seconds: float) -> str:
    """BGM 首尾淡入淡出的滤镜片段(含前导逗号)。只作用在 BGM,不碰旁白。"""
    fades = f",afade=t=in:st=0:d={BGM_FADE_IN}"
    if seconds >= BGM_FADE_IN + BGM_FADE_OUT + 3:
        fades += f",afade=t=out:st={seconds - BGM_FADE_OUT:.2f}:d={BGM_FADE_OUT}"
    return fades


def is_planned_bed(bgm_path: Path | None) -> bool:
    """这条 BGM 是不是 `edit/music.render_bed` 编排好的底床(旁边有 .plan.json)。"""
    return bool(bgm_path) and Path(str(bgm_path) + BED_SIDECAR_SUFFIX).is_file()


def measure_loudness(path: Path, *, timeout: int = 600) -> dict[str, float] | None:
    """整段响度:{"I": 积分响度 LUFS, "TP": 真峰值 dBTP}。测不出(没 ffmpeg、文件坏、全静音)返回 None。

    不走 `_run`:测量是只读的旁路,测试里把 `_run` 换成假的时不该连带它。
    """
    if not config.FFMPEG_PATH or not Path(config.FFMPEG_PATH).is_file() or not Path(path).is_file():
        return None
    try:
        proc = subprocess.run(
            [str(config.FFMPEG_PATH), "-hide_banner", "-nostdin", "-v", "info", "-i", str(path),
             "-vn", "-af", "ebur128=peak=true:framelog=quiet", "-f", "null", "-"],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    res = final_qa.analyse(proc.stderr or "")
    lufs = res.get("lufs")
    if lufs is None or lufs < -70:
        return None
    peak = res.get("true_peak")
    return {"I": float(lufs), "TP": float(peak) if peak is not None else 0.0}


def _build_audio_graph(
    has_narration: bool, has_bgm: bool, narration_duration: float, first_input: int = 1,
    has_sfx: bool = False, pad_to: float = 0.0, *,
    narration_gain_db: float | None = None, bgm_gain_db: float | None = None,
    bgm_fades: bool = True, normalize: bool = True,
) -> tuple[str, str]:
    """构造 filter_complex 的音频部分,返回 (滤镜串, 输出标签)。

    旁白优先:旁白和 BGM 各自对齐电平(BGM 比旁白低 `BGM_UNDER_LU`),再用旁白做侧链压 BGM,
    最后 amix **不做归一化**(默认的 amix 会把每路除以路数,旁白白白少一半)。
    旁白在侧链和混音两处都要用,必须先 asplit。

    - `narration_gain_db` / `bgm_gain_db`:按实测响度算好的静态增益(`_mix_audio` 两遍法)。
      不给时退回动态 loudnorm:旁白不动、BGM 归一到 `FALLBACK_BGM_LUFS`。
    - `normalize`:末尾是否带整体 loudnorm(单遍法)。两遍法第一遍传 False,
      量出响度后再统一做线性增益 + 限幅。
    - `bgm_fades`:原始曲子要首尾淡变;`music.render_bed` 编排好的底床自带,传 False。
    - `first_input`:旁白(没旁白时是 BGM)的输入序号。单独混音时是 0,
      跟视频同一条命令时视频占了 0,就是 1。
    - `pad_to`:大于 0 时把混音补静音到这个总长(片尾卡在旁白之后,BGM 以外是静音)。
    - `has_sfx`:音效轨(sfx.build_track),输入序号排在旁白、BGM 之后,不进侧链,
      直接和旁白 / BGM 一起 amix。音效电平在 sfx 里按旁白 `NARRATION_LUFS` 定好,这里不再动。
    """
    filters: list[str] = []
    tail = f",apad=whole_dur={pad_to:.3f}" if pad_to > 0 else ""
    final = f",loudnorm={TARGET_LOUDNESS}" if normalize else ""
    fmt = "aresample=48000,aformat=sample_fmts=fltp:channel_layouts=stereo"
    sfx_index = first_input + int(has_narration) + int(has_bgm)
    sfx_mix = ""
    if has_sfx and (has_narration or has_bgm):
        filters.append(f"[{sfx_index}:a]{fmt}[sfx]")
        sfx_mix = "[sfx]"
    if has_narration:
        gain = f",volume={narration_gain_db:.2f}dB" if narration_gain_db is not None else ""
        filters.append(f"[{first_input}:a]{fmt}{gain}[narr0]")
    if has_bgm:
        bgm_index = first_input + 1 if has_narration else first_input
        level = (f"volume={bgm_gain_db:.2f}dB" if bgm_gain_db is not None
                 else f"loudnorm=I={FALLBACK_BGM_LUFS:g}:TP=-3:LRA=7")
        fades = _bgm_fades(narration_duration) if bgm_fades else ""
        filters.append(f"[{bgm_index}:a]{fmt},{level}{fades}[bgmraw]")
    if has_narration and has_bgm:
        filters.append("[narr0]asplit=2[narr][narrsc]")
        # 侧链:旁白一出声,BGM 自动让位;停顿时 release 让它缓缓浮回来
        filters.append(
            f"[bgmraw][narrsc]sidechaincompress=threshold={BGM_DUCK_THRESHOLD}:ratio={BGM_DUCK_RATIO}"
            f":attack={BGM_DUCK_ATTACK}:release={BGM_DUCK_RELEASE}:makeup=1[bgm]"
        )
        filters.append(
            f"[narr][bgm]{sfx_mix}amix=inputs={3 if sfx_mix else 2}:duration=first:dropout_transition=0:normalize=0"
            f"{final}{tail}[aout]"
        )
    elif has_narration or has_bgm:
        main = "[narr0]" if has_narration else "[bgmraw]"
        if sfx_mix:
            filters.append(f"{main}{sfx_mix}amix=inputs=2:duration=first:dropout_transition=0:normalize=0"
                           f"{final}{tail}[aout]")
        else:
            filters.append(f"{main}{final.lstrip(',') or 'anull'}{tail}[aout]")
    else:
        # **既没旁白也没 BGM**:TTS 全失败 + BGM 也失败时就是这种。
        #
        # 这里必须自己造一路静音,**不能**碰 loudnorm:
        # - `[bgmraw]` 这个标签这时根本不存在(它只在有 BGM 时才建),
        #   引用它 ffmpeg 会直接报 "Invalid stream specifier";
        # - 即便换成 `[0:a]`,loudnorm 作用在纯数字静音上会算出 NaN,
        #   aac 编码器随后报 "Input contains (near) NaN/+-Inf" 而失败。
        #
        # 归一化时每拍都带了 anullsrc 静音音轨,所以拼接流本身就有音轨,
        # 直接取过来用:成片是"哑片"(有音轨、没声音),但**是能出的片**。
        # 哑片这件事由调用方的警告告诉人,不该让成片直接出不来。
        filters.append(f"[0:a]{fmt}[aout]")
    return ";".join(f for f in filters if f), "[aout]"


def _limit_filter() -> str:
    lin = 10 ** (LIMIT_DBFS / 20)
    # level=0:不自动把输出拉满(否则整体响度会被它改掉);latency=1:补偿前瞻延迟,不让音画错位
    return f"alimiter=limit={lin:.4f}:attack=5:release=50:level=0:latency=1"


def _mix_audio(narration_path: Path | None, bgm_path: Path | None, seconds: float,
               dest: Path, sfx_path: Path | None = None, pad: bool = False, *,
               bgm_planned: bool | None = None) -> tuple[Path | None, str]:
    """旁白 + BGM(+ 音效)单独混成一条 wav,返回 (路径, 错误信息)。

    **不能**跟视频放在同一条 ffmpeg 里混:2026-10-23 实测(ffmpeg 9.0.2),
    视频拼接/烧字幕与 loudnorm 同一条命令时,音频帧会成段丢掉 —— 成片
    音轨时间戳出现一串约 3 秒(正好是 loudnorm 前瞻窗口)的空洞,
    108 秒的片只剩 64 秒声音,播放时就是"好几处没配音"。
    同样的滤镜单独跑一条纯音频命令,一帧不丢。

    两遍法(能测响度时):先测旁白、BGM 各自的积分响度算静态增益,混出 `<dest>.pre.wav`(浮点,
    不削波),再测混音整体响度,线性增益到 TARGET_LUFS + 限幅,写 `dest`。测不出响度就退回
    单遍:BGM 动态归一、末尾整体 loudnorm(老做法)。

    `bgm_planned`:BGM 是否是 `edit/music.render_bed` 编排好的底床(自带淡变、长度正好,
    不再循环、不再淡变)。不传时看旁边有没有 `.plan.json`。
    """
    has_narration = bool(narration_path and Path(narration_path).is_file())
    has_bgm = bool(bgm_path and Path(bgm_path).is_file())
    if not has_narration and not has_bgm:
        return None, ""
    planned = is_planned_bed(bgm_path) if bgm_planned is None else bgm_planned
    inputs: list[str] = []
    if has_narration:
        inputs += ["-i", str(narration_path)]
    if has_bgm:
        # 原始曲子比成片短时循环铺满(-stream_loop -1);编排好的底床长度正好,不循环
        inputs += ([] if planned else ["-stream_loop", "-1"]) + ["-i", str(bgm_path)]
    has_sfx = bool(sfx_path and Path(sfx_path).is_file())
    if has_sfx:
        inputs += ["-i", str(sfx_path)]
    dest.parent.mkdir(parents=True, exist_ok=True)

    narr_loud = measure_loudness(Path(narration_path)) if has_narration else None  # type: ignore[arg-type]
    bgm_loud = measure_loudness(Path(bgm_path)) if has_bgm else None  # type: ignore[arg-type]
    two_pass = (narr_loud is not None or not has_narration) and (bgm_loud is not None or not has_bgm)
    if two_pass:
        narr_gain = NARRATION_LUFS - narr_loud["I"] if narr_loud else None
        bgm_gain = NARRATION_LUFS - BGM_UNDER_LU - bgm_loud["I"] if bgm_loud else None
        pre = dest.with_name(dest.stem + ".pre.wav")
        graph, label = _build_audio_graph(
            has_narration, has_bgm, seconds, first_input=0, has_sfx=has_sfx,
            pad_to=seconds if pad else 0.0, narration_gain_db=narr_gain, bgm_gain_db=bgm_gain,
            bgm_fades=not planned, normalize=False)
        proc = _run(["-y", "-v", "error", *inputs, "-filter_complex", graph, "-map", label,
                     "-t", f"{seconds:.3f}", "-c:a", "pcm_f32le", "-ar", "48000", str(pre)],
                    timeout=900)
        mixed = measure_loudness(pre) if proc.returncode == 0 and pre.is_file() else None
        if mixed is not None:
            gain = config.TARGET_LUFS - mixed["I"]
            proc = _run(["-y", "-v", "error", "-i", str(pre), "-filter_complex",
                         f"[0:a]volume={gain:.2f}dB,{_limit_filter()}[aout]", "-map", "[aout]",
                         "-t", f"{seconds:.3f}", "-c:a", "pcm_s16le", "-ar", "48000", str(dest)],
                        timeout=900)
            pre.unlink(missing_ok=True)
            if proc.returncode == 0 and dest.is_file() and dest.stat().st_size > 0:
                return dest, ""
            log.warning("混音第二遍失败,退回单遍:%s", (proc.stderr or "")[-200:])
            degrade.note("mix.single_pass", f"第二遍失败:{(proc.stderr or '')[-200:]}")
        else:
            pre.unlink(missing_ok=True)
            log.warning("混音第一遍失败或测不出响度,退回单遍:%s", (proc.stderr or "")[-200:])
            degrade.note("mix.single_pass", f"第一遍失败:{(proc.stderr or '')[-200:]}")
    graph, label = _build_audio_graph(has_narration, has_bgm, seconds, first_input=0, has_sfx=has_sfx,
                                      pad_to=seconds if pad else 0.0, bgm_fades=not planned)
    proc = _run(
        ["-y", "-v", "error", *inputs, "-filter_complex", graph, "-map", label,
         "-t", f"{seconds:.3f}", "-c:a", "pcm_s16le", "-ar", "48000", str(dest)],
        timeout=900,
    )
    if proc.returncode != 0 or not dest.is_file() or dest.stat().st_size == 0:
        return None, (proc.stderr or "")[-300:]
    return dest, ""


def compose(
    project_id: str,
    shot_paths: Sequence[Path],
    *,
    ass_path: Path | None = None,
    narration_path: Path | None = None,
    bgm_path: Path | None = None,
    output_name: str = "output.mp4",
    size: tuple[int, int] | None = None,
    durations: Sequence[float] | None = None,
    layer_path: Path | None = None,
    sfx_path: Path | None = None,
    tail_silent: bool = False,
    intro_path: Path | None = None,
    intro_seconds: float = 0.0,
    dissolve_into: Sequence[int] = (),
    host: tuple[Path, int, int] | None = None,
    grade: bool = True,
) -> RenderResult:
    """主合成入口。

    `host` = 虚拟主持人层 (concat 清单, x, y),见 host.py:叠在字幕之上、片头标之下;
    叠失败时整次合成退回不带主持人的版本。

    `intro_path` = 片头品牌条(带透明通道的 PNG,见 intro.py):叠在画面上前 `intro_seconds`
    秒,淡入淡出;不占时长、不挡旁白,叠失败时整次合成退回不带它的版本。

    `dissolve_into` = 这几个镜头开头从上一镜的最后一帧短叠化进来(章节交界),其余硬切。

    `tail_silent` = 最后一个镜头是片尾卡(没有旁白):混音补静音到总长,BGM 托底并淡出。

    `sfx_path` = 音效轨(sfx.build_track),和旁白 / BGM 一起混音;混失败自动不带音效重混。

    `layer_path` = 带透明通道的花字字幕层(webm,见 caption_layer):给了就 overlay 它,
    不再烧 ASS;overlay 这一遍失败会退回 ASS 字幕重合。

    `durations` 给了就是逐镜头时长不等的横屏解说轨:镜头不再截成 6 秒,
    成片总长 = 各镜时长之和。
    """
    project_dir = config.project_dir(project_id)
    output = project_dir / output_name
    # **原子写**:ffmpeg 先写临时文件,解码校验过了再改名。
    #
    # 直接写 output.mp4 的话,进程在编码中途被杀(README:托管后台任务有
    # 1 小时上限)会留下半截文件 —— 而 `has_output` 只看"文件在不在",
    # 会把一个打不开的坏片当成成片报出去。改名是原子的,读者要么看到
    # 完整的成片,要么看不到。
    #
    # 临时文件**保留原扩展名**:ffmpeg 靠扩展名推断封装格式,
    # 叫 "output.mp4.part" 它会直接报 "Unable to find a suitable output format"。
    tmp_output = project_dir / f"{Path(output_name).stem}.part{Path(output_name).suffix}"
    work_dir = project_dir / "_render"
    w, h = _size(size)

    if not shot_paths:
        return RenderResult(ok=False, warnings=["没有可用的成片镜头"])

    normalised, warnings = normalise_all(list(shot_paths), work_dir, size=(w, h),
                                         trim=None if durations else config.SHOT_GRID_SECONDS,
                                         durations=durations)
    if not normalised:
        return RenderResult(ok=False, warnings=warnings + ["所有镜头归一化失败"])

    if narration_path is None:
        # 没有旁白的成片等于没有解说。允许它产出来(TTS 全部失败时就是这种),
        # 但一定要让人看见,而不是事后才发现成片是哑的。
        if bgm_path is None or not Path(bgm_path).is_file():
            warnings.append("没有旁白也没有 BGM,成片是哑片(有音轨、没声音)")
        else:
            warnings.append("没有旁白音轨,成片将只有背景音乐")
    elif not Path(narration_path).is_file():
        warnings.append(f"旁白文件不存在,成片将没有解说声:{narration_path}")

    if len(normalised) == len(shot_paths):
        normalised = _dissolve_heads(normalised, dissolve_into, work_dir, size=(w, h))
    concat_file = timeline_mod.concat_list_file(normalised, work_dir / "concat.txt")
    total_seconds = sum(durations) if durations else config.SHOT_GRID_SECONDS * len(normalised)

    # 音频先单独混好(原因见 _mix_audio),主命令只负责视频 + 封装现成的音轨
    mix_path, mix_err = _mix_audio(narration_path, bgm_path, total_seconds, work_dir / "mix.wav",
                                   sfx_path=sfx_path, pad=tail_silent)
    if mix_err and sfx_path:
        warnings.append("音效混入失败,改为不带音效")
        mix_path, mix_err = _mix_audio(narration_path, bgm_path, total_seconds, work_dir / "mix.wav",
                                       pad=tail_silent)
    if mix_err:
        warnings.append(f"旁白/BGM 混音失败,成片将没有声音:{mix_err}")

    inputs: list[str] = ["-f", "concat", "-safe", "0", "-i", str(concat_file)]
    if mix_path:
        inputs += ["-i", str(mix_path)]

    # 视频滤镜链:拼接后的视频 → 归一化 → 可选烧字幕
    vf_parts = [f"scale={w}:{h}", f"fps={FPS}", "setsar=1", "format=yuv420p"]
    if config.GRADE_ENABLED and grade:
        vf_parts.append(GRADE_FILTER)
    use_layer = bool(layer_path and Path(layer_path).is_file())
    ass_filter = None if use_layer else _build_ass_filter(ass_path, project_dir)
    if ass_filter:
        vf_parts.append(ass_filter)
    vf = ",".join(vf_parts)
    nxt = 2 if mix_path else 1
    chain = f"[0:v]{vf}[v0]"
    if use_layer:
        # 花字层是 vp8+alpha:ffmpeg 自带的 vp8 解码器会丢掉 alpha,必须指定 libvpx
        inputs += ["-c:v", "libvpx", "-i", str(layer_path)]
        chain += f";[v0][{nxt}:v]overlay=eof_action=pass:format=auto[v1]"
        nxt += 1
    else:
        chain += ";[v0]null[v1]"
    use_host = bool(host and Path(host[0]).is_file())
    if use_host and host:
        inputs += ["-f", "concat", "-safe", "0", "-i", str(host[0])]
        chain += (f";[{nxt}:v]format=rgba[host];"
                  f"[v1][host]overlay={host[1]}:{host[2]}:eof_action=pass:format=auto[v1h]")
        nxt += 1
    else:
        chain += ";[v1]null[v1h]"
    use_intro = bool(intro_path and Path(intro_path).is_file() and intro_seconds > 0)
    if use_intro:
        fo = max(0.0, intro_seconds - 0.4)
        inputs += ["-loop", "1", "-t", f"{intro_seconds:.3f}", "-i", str(intro_path)]
        chain += (f";[{nxt}:v]format=rgba,fade=t=in:st=0:d=0.3:alpha=1,"
                  f"fade=t=out:st={fo:.3f}:d=0.4:alpha=1[intro];"
                  "[v1h][intro]overlay=eof_action=pass:format=auto[v2]")
    else:
        chain += ";[v1h]null[v2]"
    filters = chain + ";[v2]format=yuv420p[vout]"
    if mix_path:
        audio_map = "1:a"
    else:
        # 哑片兜底:拼接流里每拍自带的静音轨(见 _build_audio_graph 的说明)
        silent, audio_map = _build_audio_graph(False, False, 0.0)
        filters += f";{silent}"

    args: list[str] = ["-y", "-v", "error", *inputs]
    args += ["-filter_complex", filters, "-map", "[vout]", "-map", audio_map]
    args += [
        "-c:v", "libx264", "-preset", "slow", "-crf", "20",
        "-pix_fmt", "yuv420p", "-movflags", "+faststart",
        "-t", f"{total_seconds:.3f}",
        "-c:a", "aac", "-b:a", "192k", "-ar", "48000",
        str(tmp_output),
    ]

    def _check(path: Path) -> str | None:
        """出片前的两道校验:整片解码 + 音轨完整。返回不合格原因。"""
        if not _decodes_clean(path):
            # 2026-10-21 实测:同样的参数,管线里合出的成片偶发 H.264 NAL 损坏(满屏
            # "Invalid NAL unit size"),重合一次就干净。
            return "成片解码校验未通过"
        if mix_path:
            return _audio_intact(path, total_seconds)
        return None

    proc = _run(args, timeout=2400, cwd=project_dir)
    problem = _check(tmp_output) if proc.returncode == 0 and tmp_output.is_file() else None
    if problem:
        warnings.append(f"{problem},已重新合成一次")
        tmp_output.unlink(missing_ok=True)
        proc = _run(args, timeout=2400, cwd=project_dir)
        problem = _check(tmp_output) if proc.returncode == 0 and tmp_output.is_file() else None
        if problem:
            warnings.append(f"重合后仍不合格:{problem},请检查后重新合成")
    if proc.returncode != 0 or not tmp_output.is_file():
        if use_host:
            # 主持人层只是加分项:叠失败就不带它重合,其余参数原样
            tmp_output.unlink(missing_ok=True)
            warnings.append(f"主持人层叠加失败,成片不带主持人:{(proc.stderr or '').strip()[-300:]}")
            retry = compose(
                project_id, shot_paths,
                ass_path=ass_path, narration_path=narration_path,
                bgm_path=bgm_path, output_name=output_name,
                size=size, durations=durations, layer_path=layer_path, sfx_path=sfx_path,
                tail_silent=tail_silent, intro_path=intro_path, intro_seconds=intro_seconds,
                dissolve_into=dissolve_into,
            )
            retry.warnings = warnings + (retry.warnings or [])
            return retry
        if use_layer:
            # 花字层 overlay 失败:退回 ASS 字幕重合(ASS 烧录自己的失败由递归那次再兜底)
            tmp_output.unlink(missing_ok=True)
            warnings.append(f"花字层叠加失败,改用 ASS 字幕:{(proc.stderr or '').strip()[-300:]}")
            retry = compose(
                project_id, shot_paths,
                ass_path=ass_path, narration_path=narration_path,
                bgm_path=bgm_path, output_name=output_name,
                size=size, durations=durations, layer_path=None, sfx_path=sfx_path,
                tail_silent=tail_silent, intro_path=intro_path, intro_seconds=intro_seconds,
                dissolve_into=dissolve_into, host=host,
            )
            retry.warnings = warnings + (retry.warnings or [])
            return retry
        # 字幕烧录是 Windows 上最容易出问题的一环,失败时退化为"无字幕重试"
        if ass_filter:
            # 递归那次的 warnings 是新的:不把这条带回去,成片会悄悄没字幕、render.json 里却一条警告都没有
            warnings.append(f"烧录字幕失败,已输出无字幕版本:{(proc.stderr or '').strip()[-300:]}")
            retry = compose(
                project_id, shot_paths,
                ass_path=None, narration_path=narration_path,
                bgm_path=bgm_path, output_name=output_name,
                size=size, durations=durations, sfx_path=sfx_path, tail_silent=tail_silent,
                intro_path=intro_path, intro_seconds=intro_seconds,
                dissolve_into=dissolve_into, host=host,
            )
            retry.warnings = warnings + (retry.warnings or [])
            return retry
        # 别把半截文件留在那儿 —— 它不是成片
        tmp_output.unlink(missing_ok=True)
        return RenderResult(
            ok=False, warnings=warnings + [f"合成失败:{proc.stderr[-500:]}"],
            log=proc.stderr[-2000:],
        )

    # 校验过了,改名成正式成片(同卷内是原子替换)
    os.replace(tmp_output, output)
    duration = timeline_mod.probe_safe(output, default=0.0)
    if duration <= 0:
        warnings.append("成片时长探测失败")
    if mix_path:
        warnings.extend(final_qa.inspect(output))

    return RenderResult(
        ok=True, output_path=output, duration=duration, warnings=warnings,
        log=proc.stderr[-500:],
    )


def _decodes_clean(video: Path) -> bool:
    """整片解码一遍,有任何错误输出就算不干净。"""
    proc = _run(["-v", "error", "-i", str(video), "-f", "null", "-"], timeout=900)
    return proc.returncode == 0 and not proc.stderr.strip()


#: 音轨时长至少是预期的这个比例;相邻音频包时间戳差超过这个秒数就算空洞
AUDIO_MIN_RATIO = 0.98
AUDIO_MAX_GAP = 0.1


def _audio_intact(video: Path, expected_seconds: float) -> str | None:
    """音轨是否完整:包时长合计够长、时间戳没有空洞。合格返回 None,否则返回原因。

    `_decodes_clean` 抓不到丢帧 —— 时间戳空洞不算解码错误,播放器在空洞处
    直接静音。所以要单独数音频包。
    """
    if not config.FFPROBE_PATH or not config.FFPROBE_PATH.is_file():
        return None
    try:
        proc = subprocess.run(
            [str(config.FFPROBE_PATH), "-v", "error", "-select_streams", "a:0",
             "-show_entries", "packet=pts_time,duration_time", "-of", "csv=p=0", str(video)],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=300,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return audio_problem(proc.stdout, expected_seconds)


def audio_problem(packets_csv: str, expected_seconds: float) -> str | None:
    """解析 ffprobe 的 `pts_time,duration_time` 逐包输出,找音轨缺口。"""
    total = 0.0
    gaps: list[tuple[float, float]] = []
    prev_end: float | None = None
    for line in packets_csv.splitlines():
        parts = [p for p in line.strip().split(",") if p]
        if len(parts) < 2:
            continue
        try:
            pts, dur = float(parts[0]), float(parts[1])
        except ValueError:
            continue
        total += dur
        if prev_end is not None and pts - prev_end > AUDIO_MAX_GAP:
            gaps.append((prev_end, pts))
        prev_end = pts + dur
    if total <= 0:
        return "成片没有音轨"
    if gaps:
        lost = sum(b - a for a, b in gaps)
        return f"成片音轨有 {len(gaps)} 处断档,共缺 {lost:.1f} 秒(首处 {gaps[0][0]:.1f} 秒)"
    if total < expected_seconds * AUDIO_MIN_RATIO:
        return f"成片音轨只有 {total:.1f} 秒,应为 {expected_seconds:.1f} 秒"
    return None


def _build_ass_filter(ass_path: Path | None, project_dir: Path) -> str | None:
    """构造 ass 字幕滤镜。路径相对于项目目录,规避 Windows 转义问题。"""
    if not ass_path or not Path(ass_path).is_file():
        return None
    try:
        rel = Path(ass_path).resolve().relative_to(project_dir.resolve())
        filename = str(rel).replace("\\", "/")
    except ValueError:
        filename = Path(ass_path).name

    fontsdir = _fontsdir_arg()
    if fontsdir:
        return f"ass={filename}:{fontsdir}"
    return f"ass={filename}"


# --------------------------------------------------------------------------
# 封面
# --------------------------------------------------------------------------
def build_cover(video_path: str | Path, title: str, dest: str | Path,
                subtitle: str = "", size: tuple[int, int] | None = None,
                still: str | Path | None = None, kicker: str = "",
                layout: str = "title", hook: str = "") -> Path | None:
    from .narration import cover_3x4_path, cover_4x3_path, make_cover

    dest = Path(dest)
    out = make_cover(video_path, title, dest, subtitle, size=size, still=still, kicker=kicker,
                     layout=layout, hook=hook)
    out_w, out_h = size or (config.OUTPUT_WIDTH, config.OUTPUT_HEIGHT)
    if out is None or out_h > out_w:  # 竖屏封面自己会带 3:4,不用补
        return out
    # 横屏老封面(没有可抠的白底官图时的兜底)也要有 4:3 / 3:4 的重排版:
    # 抖音网页版要横 4:3 + 竖 3:4 两张,缺了就是把 16:9 居中硬裁、竖封面干脆让平台自动生成。
    short = min(out_w, out_h)
    for path, sz in ((cover_4x3_path(dest), (short * 4 // 3, short)),
                     (cover_3x4_path(dest), (short, short * 4 // 3))):
        try:
            make_cover(video_path, title, path, subtitle, size=sz, still=still, kicker=kicker,
                       layout=layout, hook=hook)
        except Exception as exc:  # noqa: BLE001 - 补的封面失败不能拦住成片
            log.warning("补的封面失败不能拦住成片: %s", exc)
        # 竖屏分支会顺手多写一张 `<名>_3x4`,这里只要主文件
        extra = cover_3x4_path(path)
        if extra != path:
            extra.unlink(missing_ok=True)
    return out


# --------------------------------------------------------------------------
# 自检
# --------------------------------------------------------------------------
def self_check() -> dict[str, Any]:
    """渲染层能力自检:确认编码器与滤镜确实存在。"""
    ok, message = config.ffmpeg_status()
    result: dict[str, Any] = {"ffmpeg_ok": ok, "message": message}
    if not ok:
        return result
    try:
        proc = _run(["-version"], timeout=30)
        result["version"] = (proc.stdout or "").splitlines()[0] if proc.stdout else ""
    except Exception as exc:  # noqa: BLE001
        result["version_error"] = str(exc)
    for name in ("libx264", "aac"):
        try:
            proc = subprocess.run(
                [_ffmpeg(), "-hide_banner", "-encoders"],
                capture_output=True, text=True, timeout=60,
            )
            result[f"encoder_{name}"] = name in (proc.stdout or "")
        except Exception:  # noqa: BLE001
            result[f"encoder_{name}"] = False
    return result
