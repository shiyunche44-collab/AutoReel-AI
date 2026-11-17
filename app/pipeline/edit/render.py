"""剪辑 v2 的合成:时间线 → Remotion 出静音视频 → 与单独混好的 mix.wav 合流 → 校验。

音频仍走 `render._mix_audio`(旁白 + BGM + 音效单独混成 wav,见 CLAUDE.md「渲染与音频」),
Remotion 只出画面,合流用 `-c:v copy`,不在同一条命令里做 loudnorm。
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

from ... import config
from .. import caption_layer, final_qa
from .. import render as render_mod
from .. import timeline as timeline_mod
from . import reframe
from .edl import Timeline

log = logging.getLogger(__name__)

#: 版本进缓存键:组件 / 规则改了升这个数
EDIT_VERSION = 4
RENDER_TIMEOUT = int(os.getenv("EDIT_V2_TIMEOUT", "3600"))


class EditRenderError(RuntimeError):
    pass


def _public_name(path: str) -> str:
    p = Path(path)
    return hashlib.sha1(str(p.resolve()).encode("utf-8")).hexdigest()[:16] + p.suffix.lower()


FONT_SUFFIXES = (".ttf", ".otf", ".woff", ".woff2")


def font_file() -> Path | None:
    """`EDIT_V2_FONT_FILE` 指定的字体(建议思源黑体 Heavy / Noto Sans SC Black,OFL 可商用)。
    没配、文件不在、格式不对都返回 None:Remotion 按系统字体回退。.ttc 浏览器认不了,不收。"""
    raw = os.getenv("EDIT_V2_FONT_FILE", "").strip().strip('"')
    if not raw:
        return None
    path = Path(raw)
    if not path.is_file() or path.suffix.lower() not in FONT_SUFFIXES:
        log.warning("EDIT_V2_FONT_FILE 不可用(不存在或不是 ttf/otf/woff/woff2):%s", raw)
        return None
    return path


def stage_media(tl: Timeline, public: Path) -> dict[str, Any]:
    """把时间线里用到的本地文件链进 Remotion 的 public 目录,返回改好 src 的 props。
    竖屏顺带给每刀做一张模糊底图(`backdrop`)。"""
    public.mkdir(parents=True, exist_ok=True)
    props = tl.to_props()
    portrait = tl.height > tl.width
    font = font_file()
    if font:
        dest = public / f"editfont{font.suffix.lower()}"
        if not dest.exists():
            shutil.copy2(font, dest)
        props["font"] = dest.name
    for clip in props["clips"]:
        src = clip["src"]
        if not src or src.startswith(("http:", "https:", "data:")):
            continue
        if not Path(src).is_file():
            raise EditRenderError(f"素材不存在:{src}")
        name = _public_name(src)
        dest = public / name
        if not dest.exists():
            try:
                os.link(src, dest)
            except OSError:
                shutil.copy2(src, dest)
        clip["src"] = name
        if clip["kind"] != "beat" and portrait:
            _reframe(clip, Path(src), public)
    return props


def _reframe(clip: dict[str, Any], src: Path, public: Path) -> None:
    """竖屏:抽一帧,算主体位置(`focus`)并做模糊底图(`backdrop`)。失败就居中、不要底图,不拦出片。"""
    at = float(clip.get("src_in") or 0.0) + min(1.0, float(clip.get("dur") or 0.0) / 2)
    stem = f"{Path(clip['src']).stem}_{int(at * 10)}"
    frame, bd = public / f"fr_{stem}.jpg", public / f"bd_{stem}.jpg"
    if not frame.is_file():
        args = ["-y", "-v", "error"]
        if src.suffix.lower() not in (".jpg", ".jpeg", ".png", ".webp"):
            args += ["-ss", f"{max(0.0, at):.3f}"]
        args += ["-i", str(src), "-frames:v", "1", "-vf", "scale=480:-2", "-q:v", "3", str(frame)]
        proc = render_mod._run(args, timeout=60)
        if proc.returncode != 0 or not frame.is_file():
            return
    try:
        clip["focus"] = reframe.analyse(frame, None if bd.is_file() else bd)
        clip["ar"] = reframe.aspect_of(frame)
    except (OSError, ValueError):
        log.warning("重构图分析失败:%s", src, exc_info=True)
        return
    if bd.is_file():
        clip["backdrop"] = bd.name


def source_digest() -> str:
    """Remotion 组件源码的哈希:改了组件,旧缓存自动作废(不靠记得手动升 EDIT_VERSION)。"""
    h = hashlib.sha1()
    for f in sorted((caption_layer.REMOTION_DIR / "src").rglob("*")):
        if f.is_file():
            h.update(f.name.encode("utf-8"))
            h.update(f.read_bytes())
    return h.hexdigest()[:12]


def render_video(tl: Timeline, work: Path) -> Path:
    """Remotion 渲出静音 h264,按内容哈希缓存。失败抛 EditRenderError。"""
    ok, why = caption_layer.available()
    if not ok:
        raise EditRenderError(why)
    props = stage_media(tl, work / "public")
    key = hashlib.sha1(json.dumps([EDIT_VERSION, source_digest(), props], sort_keys=True,
                                  ensure_ascii=False).encode("utf-8")).hexdigest()[:16]
    out = work / f"video_{key}.mp4"
    if out.is_file() and out.stat().st_size > 0:
        return out
    props_path = work / f"timeline_{key}.json"
    props_path.write_text(json.dumps(props, ensure_ascii=False), encoding="utf-8")
    tmp = work / f"video_{key}.part.mp4"
    cmd = [
        config.motion_node(), str(caption_layer.REMOTION_DIR / caption_layer._CLI), "render",
        "src/index.jsx", "Edit", tmp.as_posix(),
        # 画质:JPEG 帧质量 95(默认 80 时字边有压缩噪点);输出标准范围 yuv420p + bt709,
        # 默认的 yuvj420p 全范围在平台转码时常被当成标准范围,画面发灰 / 暗部死黑
        "--codec=h264", f"--crf={os.getenv('EDIT_V2_CRF', '16')}", "--muted",
        "--image-format=jpeg", "--jpeg-quality=95", "--pixel-format=yuv420p", "--color-space=bt709",
        f"--x264-preset={os.getenv('EDIT_V2_X264_PRESET', 'slow')}",
        f"--public-dir={(work / 'public').as_posix()}",
        f"--props={props_path.as_posix()}", f"--browser-executable={caption_layer.browser()}",
        f"--concurrency={os.getenv('EDIT_V2_CONCURRENCY', '50%')}",
        "--log=error", "--overwrite",
    ]
    try:
        proc = subprocess.run(cmd, cwd=caption_layer.REMOTION_DIR, env=caption_layer._env(),
                              capture_output=True, text=True, encoding="utf-8", errors="replace",
                              timeout=RENDER_TIMEOUT)
    except (OSError, subprocess.SubprocessError) as exc:
        tmp.unlink(missing_ok=True)
        raise EditRenderError(f"Remotion 调用失败:{exc}") from exc
    if proc.returncode != 0 or not tmp.is_file() or tmp.stat().st_size == 0:
        tmp.unlink(missing_ok=True)
        raise EditRenderError("Remotion 渲染失败:" + (proc.stderr or proc.stdout or "")[-500:])
    os.replace(tmp, out)
    return out


def mux(video: Path, mix: Path | None, dest: Path, seconds: float) -> None:
    """画面 + 混好的音轨合流(视频流直接拷贝)。"""
    args = ["-y", "-v", "error", "-i", str(video)]
    if mix:
        args += ["-i", str(mix), "-map", "0:v:0", "-map", "1:a:0", "-c:a", "aac", "-b:a", "192k"]
    args += ["-c:v", "copy", "-t", f"{seconds:.3f}", "-movflags", "+faststart", str(dest)]
    proc = render_mod._run(args, timeout=900)
    if proc.returncode != 0 or not dest.is_file():
        raise EditRenderError("合流失败:" + (proc.stderr or "")[-300:])


def compose(project_id: str, tl: Timeline, *, narration_path: Path | None,
            bgm_path: Path | None, sfx_path: Path | None,
            output_name: str) -> render_mod.RenderResult:
    project_dir = config.project_dir(project_id)
    work = project_dir / "_edit"
    work.mkdir(parents=True, exist_ok=True)
    warnings = list(tl.notes)
    mix, err = render_mod._mix_audio(narration_path, bgm_path, tl.duration,
                                     project_dir / "_render" / "mix.wav", sfx_path=sfx_path,
                                     pad=True)
    if (narration_path or bgm_path) and not mix:
        raise EditRenderError(f"混音失败:{err}")
    video = render_video(tl, work)
    output = project_dir / output_name
    tmp = project_dir / f"{Path(output_name).stem}.part{Path(output_name).suffix}"
    tmp.unlink(missing_ok=True)
    mux(video, mix, tmp, tl.duration)
    if not render_mod._decodes_clean(tmp):
        tmp.unlink(missing_ok=True)
        raise EditRenderError("成片解码有错误")
    if mix:
        problem = render_mod._audio_intact(tmp, tl.duration)
        if problem:
            tmp.unlink(missing_ok=True)
            raise EditRenderError(problem)
    os.replace(tmp, output)
    duration = timeline_mod.probe_safe(output, default=0.0)
    if mix:
        warnings.extend(final_qa.inspect(output))
    return render_mod.RenderResult(ok=True, output_path=output, duration=duration,
                                   warnings=warnings, log="edit-v2")
