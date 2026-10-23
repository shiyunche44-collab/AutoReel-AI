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
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

from .. import config
from ..models import SubtitleCue
from . import timeline as timeline_mod

log = logging.getLogger(__name__)

FPS = 24
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


def normalise_clip(src: str | Path, dest: str | Path, *, size: tuple[int, int] | None = None,
                   trim: float | None = config.SHOT_GRID_SECONDS) -> bool:
    """把单个镜头归一化为目标画幅(默认 1080×1920)/ 24fps / yuv420p / 静音音轨。

    竖屏 9:16 素材源分辨率不统一,统一 scale+crop 到目标画幅可保证拼接不花屏。
    统一生成静音音轨是必需的:concat 要求所有片段的流结构一致。
    `trim` 为 None 时不截断(横屏解说轨的节拍时长各不相同,已经在渲染时定好)。
    """
    w, h = _size(size)
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


#: 静图镜头的动效。连拼背景里按片段 id 散开挑、且不与上一刀相同(explainer.motion_for),
#: 避免满屏都是同一种推近:
#:   zoom_in / zoom_out   居中推近 / 拉远
#:   pan_left / pan_right / pan_up   放大后横移 / 上移
#:   push_diag            左上 → 右下的对角推近
#:   punch_in             0.45 秒内快推到 1.28 后停住(强调)
#:   drift_zoom           边推近边往右漂
STILL_MOTIONS: tuple[str, ...] = ("zoom_in", "pan_right", "zoom_out", "pan_left",
                                  "push_diag", "punch_in", "pan_up", "drift_zoom")


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
    center_x, center_y = "iw/2-(iw/zoom/2)", "ih/2-(ih/zoom/2)"
    if motion == "zoom_out":
        z, x, y = f"1.15-0.15*on/{frames}", center_x, center_y
    elif motion == "pan_left":
        z, x, y = "1.12", f"(iw-iw/zoom)*on/{frames}", center_y
    elif motion == "pan_right":
        z, x, y = "1.12", f"(iw-iw/zoom)*(1-on/{frames})", center_y
    elif motion == "pan_up":
        z, x, y = "1.12", center_x, f"(ih-ih/zoom)*(1-on/{frames})"
    elif motion == "push_diag":
        z, x, y = f"1.04+0.16*on/{frames}", f"(iw-iw/zoom)*on/{frames}", f"(ih-ih/zoom)*on/{frames}"
    elif motion == "punch_in":
        punch = max(1, min(frames, int(round(FPS * 0.45))))
        z = f"1+0.28*(1-pow(1-min(1,on/{punch}),3))"
        x, y = center_x, center_y
    elif motion == "drift_zoom":
        z, x, y = f"1.06+0.12*on/{frames}", f"(iw-iw/zoom)*(0.2+0.6*on/{frames})", center_y
    else:  # zoom_in
        z, x, y = f"1+0.15*on/{frames}", center_x, center_y
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
                  trim: float | None = config.SHOT_GRID_SECONDS) -> tuple[list[Path], list[str]]:
    """归一化全部镜头。返回 (成功列表, 警告列表)。"""
    work_dir.mkdir(parents=True, exist_ok=True)
    ok: list[Path] = []
    warnings: list[str] = []
    for i, src in enumerate(shot_paths):
        dest = work_dir / f"norm_{i:03d}.mp4"
        if dest.is_file() and dest.stat().st_size > 0:
            ok.append(dest)
            continue
        try:
            if normalise_clip(src, dest, size=size, trim=trim):
                ok.append(dest)
            else:
                warnings.append(f"镜头 {i} 归一化失败,已跳过")
        except Exception as exc:  # noqa: BLE001
            warnings.append(f"镜头 {i} 归一化异常:{exc}")
    return ok, warnings


# --------------------------------------------------------------------------
# 第二段:拼接 + 字幕 + 混音
# --------------------------------------------------------------------------
#: BGM 的静态音量(相对旁白)。旁白停顿时 BGM 以这个电平托底
BGM_BED_GAIN = 0.35
#: 旁白出声时 BGM 额外压到这个增益以下(侧链压缩的目标)
BGM_DUCK_RATIO = 12
BGM_DUCK_THRESHOLD = 0.01


def _build_audio_graph(
    has_narration: bool, has_bgm: bool, narration_duration: float
) -> tuple[str, str]:
    """构造 filter_complex 的音频部分,返回 (滤镜串, 输出标签)。

    旁白优先:先把旁白和 BGM 各自响度对齐(BGM 比旁白低),再用旁白做侧链压 BGM,
    最后 amix **不做归一化**(默认的 amix 会把每路除以路数,旁白白白少一半),
    整体只过一遍 loudnorm。旁白在侧链和混音两处都要用,必须先 asplit。
    """
    filters: list[str] = []
    if has_narration:
        filters.append("[1:a]aresample=48000,aformat=sample_fmts=fltp:channel_layouts=stereo[narr0]")
    if has_bgm:
        bgm_index = 2 if has_narration else 1
        filters.append(
            f"[{bgm_index}:a]aresample=48000,aformat=sample_fmts=fltp:channel_layouts=stereo,"
            f"loudnorm=I=-26:TP=-3:LRA=7,volume={BGM_BED_GAIN}[bgmraw]"
        )
    if has_narration and has_bgm:
        filters.append("[narr0]asplit=2[narr][narrsc]")
        # 侧链:旁白一出声,BGM 自动让位;停顿时 attack/release 让它缓缓浮回来
        filters.append(
            f"[bgmraw][narrsc]sidechaincompress=threshold={BGM_DUCK_THRESHOLD}:ratio={BGM_DUCK_RATIO}"
            ":attack=15:release=500:makeup=1[bgm]"
        )
        filters.append(
            "[narr][bgm]amix=inputs=2:duration=first:dropout_transition=0:normalize=0,"
            f"loudnorm={TARGET_LOUDNESS}[aout]"
        )
    elif has_narration:
        filters.append(f"[narr0]loudnorm={TARGET_LOUDNESS}[aout]")
    elif has_bgm:
        filters.append(f"[bgmraw]loudnorm={TARGET_LOUDNESS}[aout]")
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
        filters.append(
            "[0:a]aresample=48000,aformat=sample_fmts=fltp:channel_layouts=stereo[aout]"
        )
    return ";".join(f for f in filters if f), "[aout]"


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
) -> RenderResult:
    """主合成入口。

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
                                         trim=None if durations else config.SHOT_GRID_SECONDS)
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

    concat_file = timeline_mod.concat_list_file(normalised, work_dir / "concat.txt")

    inputs: list[str] = ["-f", "concat", "-safe", "0", "-i", str(concat_file)]
    has_narration = bool(narration_path and Path(narration_path).is_file())
    has_bgm = bool(bgm_path and Path(bgm_path).is_file())
    if has_narration:
        inputs += ["-i", str(narration_path)]
    if has_bgm:
        # -stream_loop -1:BGM 比成片短时循环铺满
        inputs += ["-stream_loop", "-1", "-i", str(bgm_path)]

    # 视频滤镜链:拼接后的视频 → 可选烧字幕
    vf_parts = [f"scale={w}:{h}", f"fps={FPS}", "setsar=1", "format=yuv420p"]
    ass_filter = _build_ass_filter(ass_path, project_dir)
    if ass_filter:
        vf_parts.append(ass_filter)

    audio_filter, audio_label = _build_audio_graph(
        has_narration, has_bgm, 0.0
    )
    # 视频滤镜链:拼接后的视频 → 归一化 → 可选烧字幕
    vf = ",".join(vf_parts)
    filters = f"[0:v]{vf}[vout]" + (f";{audio_filter}" if audio_filter else "")

    args: list[str] = ["-y", "-v", "error", *inputs]
    args += ["-filter_complex", filters, "-map", "[vout]"]
    if audio_filter:
        args += ["-map", audio_label]
    args += [
        "-c:v", "libx264", "-preset", "slow", "-crf", "20",
        "-pix_fmt", "yuv420p", "-movflags", "+faststart",
        "-t", f"{sum(durations):.3f}" if durations
        else str(config.SHOT_GRID_SECONDS * len(normalised)),
    ]
    if audio_filter:
        args += ["-c:a", "aac", "-b:a", "192k", "-ar", "48000"]
    args += [str(tmp_output)]

    proc = _run(args, timeout=2400, cwd=project_dir)
    if proc.returncode == 0 and tmp_output.is_file() and not _decodes_clean(tmp_output):
        # 2026-10-21 实测:同样的参数,管线里合出的成片偶发 H.264 NAL 损坏(满屏
        # "Invalid NAL unit size"),重合一次就干净。出片前整片解码验一遍,坏了重合。
        warnings.append("成片解码校验未通过,已重新合成一次")
        tmp_output.unlink(missing_ok=True)
        proc = _run(args, timeout=2400, cwd=project_dir)
        if proc.returncode == 0 and tmp_output.is_file() and not _decodes_clean(tmp_output):
            warnings.append("成片重合后仍有解码错误,请检查后重新合成")
    if proc.returncode != 0 or not tmp_output.is_file():
        # 字幕烧录是 Windows 上最容易出问题的一环,失败时退化为"无字幕重试"
        if ass_filter:
            warnings.append("烧录字幕失败,已输出无字幕版本")
            return compose(
                project_id, shot_paths,
                ass_path=None, narration_path=narration_path,
                bgm_path=bgm_path, output_name=output_name,
                size=size, durations=durations,
            )
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

    return RenderResult(
        ok=True, output_path=output, duration=duration, warnings=warnings,
        log=proc.stderr[-500:],
    )


def _decodes_clean(video: Path) -> bool:
    """整片解码一遍,有任何错误输出就算不干净。"""
    proc = _run(["-v", "error", "-i", str(video), "-f", "null", "-"], timeout=900)
    return proc.returncode == 0 and not proc.stderr.strip()


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
    from .narration import make_cover

    return make_cover(video_path, title, dest, subtitle, size=size, still=still, kicker=kicker,
                      layout=layout, hook=hook)


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
