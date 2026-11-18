"""商业包装的统一调色:运行时算一张 3D LUT(.cube),合成前把每刀素材预裁好再套上。

为什么不在 Remotion 里调:CSS 滤镜套不了 LUT。预裁之后 Remotion 只解码用到的那几秒,
顺带把超大素材缩到画幅附近、帧率统一。代价是多一代编码,中间文件用低 crf 压住损失。

LUT 由参数现算(轻 S 曲线对比、暗部偏青高光偏橙、按饱和度反比加鲜艳度、黑位微抬白位微压),
不带第三方文件,没有授权问题。幅度刻意小:只为让不同来源的素材像同一支片,不改车漆颜色的判断。
任何一步失败都退回原色(`degrade.note`),不拦出片。
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import subprocess
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

from ... import config, degrade
from .. import render as render_mod

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Grade:
    #: S 曲线混合比例(0 = 线性)
    contrast: float = 0.0
    #: 黑位抬升 / 白位上限(0 / 1 = 不动)
    lift: float = 0.0
    gain: float = 1.0
    #: 青橙分离:暗部往青、亮部往橙的最大偏移(0..1 的 RGB 值)
    split: float = 0.0
    #: 鲜艳度:越不饱和的颜色加得越多,已经很艳的几乎不动
    vibrance: float = 0.0


GRADES: dict[str, Grade] = {
    "commercial": Grade(contrast=0.22, lift=0.012, gain=0.985, split=0.028, vibrance=0.12),
}
LUT_SIZE = 33
#: LUT 公式 / 预裁参数改了升这个数:缓存文件名带它
GRADE_VERSION = 1
#: 中间文件画质(只多这一代编码);并行几路 ffmpeg
CRF = int(os.getenv("EDIT_V2_GRADE_CRF", "14"))
JOBS = max(1, int(os.getenv("EDIT_V2_GRADE_JOBS", "3")))
#: 比画幅大的素材最多留 1.25 倍:Remotion 里还有去角标裁边和慢推,留点余量不糊
HEADROOM = 1.25
#: 预裁多留一点尾巴,防取整后最后一帧读到片尾外
TAIL = 0.25
PREFIX = "g_"
STILL_SUFFIXES = (".jpg", ".jpeg", ".png", ".webp", ".bmp")


def table(g: Grade, size: int = LUT_SIZE) -> np.ndarray:
    """(size³, 3) 的输出 RGB,按 .cube 的顺序排(R 变得最快,其次 G,B 最慢)。"""
    v = np.linspace(0.0, 1.0, size)
    b, gg, r = np.meshgrid(v, v, v, indexing="ij")
    x = np.stack([r, gg, b], axis=-1).reshape(-1, 3)
    y = x + g.contrast * (x * x * (3 - 2 * x) - x)
    luma = y @ np.array([0.2126, 0.7152, 0.0722])
    d = 2 * luma - 1
    # 中间调最多、纯黑纯白不动(不让黑位 / 白位带色)
    t = g.split * d * (1 - np.abs(d)) * 4
    y = y + np.stack([t, np.zeros_like(t), -t], axis=-1)
    sat = y.max(axis=1) - y.min(axis=1)
    lum2 = (y @ np.array([0.2126, 0.7152, 0.0722]))[:, None]
    y = lum2 + (y - lum2) * (1 + g.vibrance * (1 - np.clip(sat, 0, 1)))[:, None]
    y = g.lift + y * (g.gain - g.lift)
    return np.clip(y, 0.0, 1.0)


def write_cube(path: Path, rows: np.ndarray, size: int, title: str) -> None:
    lines = [f'TITLE "{title}"', f"LUT_3D_SIZE {size}", "DOMAIN_MIN 0.0 0.0 0.0",
             "DOMAIN_MAX 1.0 1.0 1.0"]
    lines += [f"{r:.6f} {g:.6f} {b:.6f}" for r, g, b in rows]
    tmp = path.with_suffix(f".{os.getpid()}.tmp")
    tmp.write_text("\n".join(lines) + "\n", encoding="ascii")
    os.replace(tmp, path)


def lut_file(name: str) -> Path | None:
    """调色名对应的 .cube(按需生成,缓存在 data/_cache/luts)。不认识的名字返回 None。"""
    g = GRADES.get(name)
    if g is None:
        return None
    path = config.DATA_DIR / "_cache" / "luts" / f"{name}_v{GRADE_VERSION}.cube"
    if not path.is_file():
        path.parent.mkdir(parents=True, exist_ok=True)
        write_cube(path, table(g), LUT_SIZE, f"AutoReel {name} v{GRADE_VERSION}")
    return path


def _tagged(src: Path) -> bool:
    """素材带不带色彩空间标记。不带的按 bt709 解(库里绝大多数是 B 站 / 官方号转出的 bt709)。"""
    probe = config.FFPROBE_PATH
    if not probe or not probe.is_file():
        return True
    try:
        out = subprocess.run([str(probe), "-v", "error", "-select_streams", "v:0", "-show_entries",
                              "stream=color_space", "-of", "csv=p=0", str(src)],
                             capture_output=True, text=True, timeout=30).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return True
    return bool(out) and out != "unknown"


def _scale(w: int, h: int) -> str:
    """只缩不放:比画幅(带余量)大的缩到刚好盖满,小的原样(放大交给 Remotion)。"""
    cw, ch = int(w * HEADROOM) // 2 * 2, int(h * HEADROOM) // 2 * 2
    a = f"{w}/{h}"
    return (f"scale=w='if(gt(a,{a}),-2,trunc(min(iw,{cw})/2)*2)'"
            f":h='if(gt(a,{a}),trunc(min(ih,{ch})/2)*2,-2)':flags=lanczos")


def filter_chain(lut_name: str, w: int, h: int, fps: int | None, *, tagged: bool = True) -> str:
    """缩放 → (统一帧率) → 16 位 RGB 套 LUT → 回 bt709 标准范围。LUT 用文件名,ffmpeg 在它的目录里跑
    (Windows 盘符的冒号在滤镜参数里要转义,换目录就不用)。"""
    parts = [] if tagged else ["setparams=colorspace=bt709:color_primaries=bt709:color_trc=bt709:range=tv"]
    parts.append(_scale(w, h))
    if fps:
        parts.append(f"fps={fps}")
    parts += ["format=gbrp16le", f"lut3d=file={lut_name}:interp=tetrahedral"]
    parts.append("scale=out_color_matrix=bt709:out_range=tv" if fps else "format=rgb24")
    if fps:
        parts.append("format=yuv420p")
    return ",".join(parts)


def _key(src: Path, clip: dict[str, Any], lut: Path, w: int, h: int, fps: int) -> str:
    st = src.stat()
    raw = [GRADE_VERSION, lut.name, str(src.resolve()), st.st_mtime_ns, st.st_size,
           round(float(clip.get("src_in") or 0.0), 3), round(float(clip.get("dur") or 0.0), 3),
           clip["kind"], w, h, fps, CRF]
    return hashlib.sha1(json.dumps(raw).encode("utf-8")).hexdigest()[:16]


def _one(src: Path, clip: dict[str, Any], lut: Path, public: Path, w: int, h: int,
         fps: int) -> str | None:
    """预裁 + 调色一刀,返回 public 里的新文件名;失败返回 None。"""
    still = clip["kind"] == "still"
    name = f"{PREFIX}{_key(src, clip, lut, w, h, fps)}{'.jpg' if still else '.mp4'}"
    dest = public.resolve() / name
    if dest.is_file() and dest.stat().st_size > 0:
        return name
    # ffmpeg 在 LUT 的目录里跑,路径都要绝对
    src = src.resolve()
    tmp = dest.with_name(f"{dest.stem}.part{dest.suffix}")
    args = ["-y", "-v", "error"]
    if still:
        args += ["-i", str(src), "-vf", filter_chain(lut.name, w, h, None), "-frames:v", "1",
                 "-q:v", "2", str(tmp)]
    else:
        src_in = max(0.0, float(clip.get("src_in") or 0.0))
        dur = float(clip.get("dur") or 0.0) + TAIL
        args += ["-ss", f"{src_in:.3f}", "-i", str(src), "-t", f"{dur:.3f}", "-an",
                 "-vf", filter_chain(lut.name, w, h, fps, tagged=_tagged(src)),
                 "-c:v", "libx264", "-preset", "veryfast", "-crf", str(CRF), "-pix_fmt", "yuv420p",
                 "-g", str(fps), "-colorspace", "bt709", "-color_primaries", "bt709",
                 "-color_trc", "bt709", "-color_range", "tv", str(tmp)]
    try:
        proc = render_mod._run(args, timeout=300, cwd=lut.parent)
    except (OSError, subprocess.SubprocessError, render_mod.RenderError) as exc:
        log.warning("调色预裁失败:%s %s", src, exc)
        tmp.unlink(missing_ok=True)
        return None
    if proc.returncode != 0 or not tmp.is_file() or tmp.stat().st_size == 0:
        log.warning("调色预裁失败:%s %s", src, (proc.stderr or "")[-300:])
        tmp.unlink(missing_ok=True)
        return None
    os.replace(tmp, dest)
    return name


def apply(props: dict[str, Any], sources: dict[int, Path], public: Path) -> int:
    """给 props 里的实拍 / 静图刀换上调过色的预裁文件(`src_in` 归零)。

    `sources`:刀序号 → 原始素材路径(props 里的 src 已是 public 里的链接名)。
    整屏成品(beat)不调:那是卡片 / 动画,颜色是设计好的。返回失败的刀数(失败的保留原色)。"""
    name = str((props.get("theme") or {}).get("grade") or "")
    if not name:
        return 0
    try:
        lut = lut_file(name)
    except (OSError, ValueError) as exc:
        degrade.note("edit_v2.grade", f"LUT 生成失败,整片用原色:{exc}")
        return len(sources)
    if lut is None:
        log.warning("不认识的调色 %r,用原色", name)
        return 0
    w, h, fps = int(props["width"]), int(props["height"]), int(props.get("fps") or 30)
    todo = [(i, src) for i, src in sources.items() if props["clips"][i]["kind"] in ("video", "still")]
    with ThreadPoolExecutor(max_workers=JOBS) as pool:
        done = list(pool.map(lambda job: _one(job[1], props["clips"][job[0]], lut, public, w, h, fps),
                             todo))
    failed = 0
    for (i, _), out in zip(todo, done):
        if out is None:
            failed += 1
            continue
        props["clips"][i]["src"] = out
        props["clips"][i]["src_in"] = 0.0
    if failed:
        degrade.note("edit_v2.grade", f"{failed}/{len(todo)} 刀调色预裁失败,这几刀用原色")
    return failed


def prune(public: Path, keep: set[str] | None = None) -> int:
    """删掉调色中间文件(`keep` 里的留着)。成片合成完就不需要了:几百刀 crf 14 的中间文件一条片上 G。"""
    n = 0
    for f in public.glob(f"{PREFIX}*"):
        if keep and f.name in keep:
            continue
        f.unlink(missing_ok=True)
        n += 1
    return n
