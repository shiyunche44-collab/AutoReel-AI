"""⑩ 合成渲染:镜头拼接 → 字幕烧录 → 旁白混音 → 响度归一化 → 封面。

渲染层采用**两段式拼接**:
1. 先把每个镜头单独归一化到 1080×1920 / 24fps;
2. 再用 concat demuxer 以 `-c copy` 无损拼接。
比直接 concat 更稳 —— 生成片段的分辨率/帧率/时间戳可能不一致,
直接拼会得到花屏或音画漂移。

ffmpeg 不在系统 PATH,因此全部通过 `config.FFMPEG_PATH` 显式调用;
ffprobe 用于时长探测。所有子进程都用 list 形式传参(不过 shell),
避免 Windows 下的引号与注入问题。
"""
from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

from .. import config
from ..models import SubtitleCue
from . import timeline as timeline_mod

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
def normalise_clip(src: str | Path, dest: str | Path) -> bool:
    """把单个镜头归一化为 1080×1920 / 24fps / yuv420p / 静音音轨。

    竖屏 9:16 素材源分辨率不统一,统一 scale+crop 到目标画幅可保证拼接不花屏。
    统一生成静音音轨是必需的:concat 要求所有片段的流结构一致。
    """
    proc = _run(
        [
            "-y", "-v", "error",
            # ⚠️ 所有 -i 必须排在输出选项之前。ffmpeg 按位置判定选项归属,
            #    把 -vf 放在第二个 -i 之前会被当成输入选项并报错。
            "-i", str(src),
            "-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=48000",
            "-vf",
            (
                f"scale={config.OUTPUT_WIDTH}:{config.OUTPUT_HEIGHT}:force_original_aspect_ratio=increase,"
                f"crop={config.OUTPUT_WIDTH}:{config.OUTPUT_HEIGHT},"
                f"fps={FPS},setsar=1,format=yuv420p"
            ),
            "-map", "0:v:0", "-map", "1:a:0",
            "-shortest",
            "-c:v", "libx264", "-preset", "medium", "-crf", "20",
            "-c:a", "aac", "-b:a", "128k",
            "-t", str(config.SHOT_GRID_SECONDS),
            str(dest),
        ],
        timeout=600,
    )
    return proc.returncode == 0 and Path(dest).is_file() and Path(dest).stat().st_size > 0


#: 静图镜头的动效,按镜头序号轮换,避免满屏都是同一种推近
STILL_MOTIONS: tuple[str, ...] = ("zoom_in", "pan_right", "zoom_out", "pan_left")


def still_filter(motion: str, seconds: float) -> str:
    """静图 → 9:16 动效镜头的滤镜图。

    两步:
    1. **模糊铺底 + 完整前景**:横图直接裁 9:16 会把车头车尾切掉(和横屏视频
       同样的问题),所以前景按"完整放进画幅"缩放,空出的上下用同一张图
       放大模糊铺满。
    2. **zoompan 推拉 / 平移**:先放大到 2 倍再 zoompan,否则逐帧取整会抖。
    """
    w, h = config.OUTPUT_WIDTH, config.OUTPUT_HEIGHT
    frames = max(1, int(round(seconds * FPS)))
    center_x, center_y = "iw/2-(iw/zoom/2)", "ih/2-(ih/zoom/2)"
    if motion == "zoom_out":
        z, x, y = f"1.15-0.15*on/{frames}", center_x, center_y
    elif motion == "pan_left":
        z, x, y = "1.12", f"(iw-iw/zoom)*on/{frames}", center_y
    elif motion == "pan_right":
        z, x, y = "1.12", f"(iw-iw/zoom)*(1-on/{frames})", center_y
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
                  motion: str = "zoom_in") -> Path:
    """把一张图片做成定长 9:16 动效镜头。失败抛 RenderError。"""
    seconds = float(seconds or config.SHOT_GRID_SECONDS)
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    proc = _run(
        [
            "-y", "-v", "error",
            "-i", str(image),
            "-filter_complex", still_filter(motion, seconds),
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


def normalise_all(shot_paths: Sequence[Path], work_dir: Path) -> tuple[list[Path], list[str]]:
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
            if normalise_clip(src, dest):
                ok.append(dest)
            else:
                warnings.append(f"镜头 {i} 归一化失败,已跳过")
        except Exception as exc:  # noqa: BLE001
            warnings.append(f"镜头 {i} 归一化异常:{exc}")
    return ok, warnings


# --------------------------------------------------------------------------
# 第二段:拼接 + 字幕 + 混音
# --------------------------------------------------------------------------
def _build_audio_graph(
    has_narration: bool, has_bgm: bool, narration_duration: float
) -> tuple[str, str]:
    """构造 filter_complex 的音频部分,返回 (滤镜串, 输出标签)。"""
    filters: list[str] = []
    if has_narration:
        filters.append("[1:a]aresample=48000,aformat=sample_fmts=fltp:channel_layouts=stereo[narr]")
    if has_bgm:
        bgm_index = 2 if has_narration else 1
        filters.append(f"[{bgm_index}:a]aresample=48000,volume=0.16[bgmraw]")
        if has_narration:
            # 侧链闪避:旁白一出声,BGM 自动让位
            filters.append(
                "[bgmraw][narr]sidechaincompress=threshold=0.05:ratio=8:attack=20:release=400[bgm]"
            )
        else:
            filters.append("[bgmraw]anull[bgm]")

    if has_narration and has_bgm:
        filters.append(
            "[narr][bgm]amix=inputs=2:duration=first:dropout_transition=0,"
            f"loudnorm={TARGET_LOUDNESS}[aout]"
        )
    elif has_narration:
        filters.append(f"[narr]loudnorm={TARGET_LOUDNESS}[aout]")
    else:
        filters.append(f"[bgm]loudnorm={TARGET_LOUDNESS}[aout]")
    return ";".join(f for f in filters if f), "[aout]"


def compose(
    project_id: str,
    shot_paths: Sequence[Path],
    *,
    ass_path: Path | None = None,
    narration_path: Path | None = None,
    bgm_path: Path | None = None,
    output_name: str = "output.mp4",
) -> RenderResult:
    """主合成入口。"""
    project_dir = config.project_dir(project_id)
    output = project_dir / output_name
    work_dir = project_dir / "_render"

    if not shot_paths:
        return RenderResult(ok=False, warnings=["没有可用的成片镜头"])

    normalised, warnings = normalise_all(list(shot_paths), work_dir)
    if not normalised:
        return RenderResult(ok=False, warnings=warnings + ["所有镜头归一化失败"])

    if narration_path is None:
        # 没有旁白的成片等于没有解说。允许它产出来(native 模式就是这种),
        # 但一定要让人看见,而不是事后才发现成片是哑的。
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
    vf_parts = [f"scale={config.OUTPUT_WIDTH}:{config.OUTPUT_HEIGHT}",
                f"fps={FPS}", "setsar=1", "format=yuv420p"]
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
        "-t", str(config.SHOT_GRID_SECONDS * len(normalised)),
    ]
    if audio_filter:
        args += ["-c:a", "aac", "-b:a", "192k", "-ar", "48000"]
    args += [str(output)]

    proc = _run(args, timeout=2400, cwd=project_dir)
    if proc.returncode != 0 or not output.is_file():
        # 字幕烧录是 Windows 上最容易出问题的一环,失败时退化为"无字幕重试"
        if ass_filter:
            warnings.append("烧录字幕失败,已输出无字幕版本")
            return compose(
                project_id, shot_paths,
                ass_path=None, narration_path=narration_path,
                bgm_path=bgm_path, output_name=output_name,
            )
        return RenderResult(
            ok=False, warnings=warnings + [f"合成失败:{proc.stderr[-500:]}"],
            log=proc.stderr[-2000:],
        )

    duration = timeline_mod.probe_safe(output, default=0.0)
    if duration <= 0:
        warnings.append("成片时长探测失败")

    return RenderResult(
        ok=True, output_path=output, duration=duration, warnings=warnings,
        log=proc.stderr[-500:],
    )


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
                subtitle: str = "") -> Path | None:
    from .narration import make_cover

    return make_cover(video_path, title, dest, subtitle)


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
