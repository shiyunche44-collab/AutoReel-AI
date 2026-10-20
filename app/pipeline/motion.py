"""概念动画:用 HyperFrames(HTML + GSAP → mp4)渲染 concept 版式的整屏动画。

解说视频里最难拍的是"看不见的东西"—— 电池怎么排、激光雷达怎么扫、零百加速多快。
官方素材里没有这些画面,只能反复铺几张差不多的官图。这里用程序化动画补上:

- 模板在 `motion/scenes/<名>.html`(品类在 `vocab.concept_scenes` 里声明用哪些);
- 数据由这里写成 `window.__DATA__` 注入模板 —— 只放**核对过事实**的行,模板只负责画,
  图形本身(刻度、曲线、线稿)是示意,不承载任何未核对的数;
- 每次渲染一个独立工作目录(模板 + `_lib.js` + `_base.css` + 本地 gsap),不联网;
- 结果按 (模板内容 + 数据 + 时长 + 画幅) 缓存在 `data/_cache/motion/`,重拍、重合成不重渲。

渲染不了(没装 node / HyperFrames / Chrome,或渲染报错)抛 `MotionError`,
调用方退回数据卡,流水线照常出片。
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

from .. import config

#: 模板目录里每次渲染都要拷进工作目录的公用文件
SHARED_FILES = ("_lib.js", "_base.css")
#: 模板里的占位符:时长写进根节点 data-duration,数据写进 window.__DATA__
SECONDS_TOKEN = "__SECONDS__"
DATA_TOKEN = "/*__DATA__*/{}"
#: 模板里的底占位:有实拍就换成 <video>,没有就换成暗色径向底
BACKGROUND_TOKEN = "<!--__BACKGROUND__-->"


class MotionError(RuntimeError):
    """概念动画渲染失败。调用方据此降级,不应让整拍失败。"""


def scenes_dir() -> Path:
    return config.MOTION_DIR / "scenes"


def available_scenes() -> set[str]:
    d = scenes_dir()
    return {p.stem for p in d.glob("*.html") if not p.stem.startswith("_")} if d.is_dir() else set()


def cache_dir() -> Path:
    return config.DATA_DIR / "_cache" / "motion"


def _cli() -> Path:
    return config.MOTION_DIR / "node_modules" / "hyperframes" / "bin" / "hyperframes.mjs"


def _gsap() -> Path:
    return config.MOTION_DIR / "node_modules" / "gsap" / "dist" / "gsap.min.js"


def compose_html(scene: str, data: dict[str, Any], seconds: float, background: bool = False) -> str:
    """把数据和时长灌进模板,返回完整 HTML。模板缺占位符直接报错(模板写错了)。
    background=True:底是工作目录里的 bg.mp4(这一拍的实拍),否则用暗色径向底。"""
    path = scenes_dir() / f"{scene}.html"
    if not path.is_file():
        raise MotionError(f"没有动画模板 {scene}({path})")
    html = path.read_text(encoding="utf-8")
    if SECONDS_TOKEN not in html or DATA_TOKEN not in html or BACKGROUND_TOKEN not in html:
        raise MotionError(f"模板 {scene} 缺少占位符 {SECONDS_TOKEN} / {DATA_TOKEN} / {BACKGROUND_TOKEN}")
    if background:
        bg = (f'<div class="bgwrap" id="bgwrap"><video id="bg" class="clip" src="bg.mp4" data-start="0" '
              f'data-duration="{seconds:.3f}" muted playsinline></video></div>')
    else:
        bg = '<div class="bgwrap nobg" id="bgwrap"></div>'
    html = html.replace(BACKGROUND_TOKEN, bg)
    payload = json.dumps({**data, "seconds": round(seconds, 3)}, ensure_ascii=False)
    # 防止数据里出现 </script> 把脚本块提前截断
    payload = payload.replace("</", "<\\/")
    return html.replace(SECONDS_TOKEN, f"{seconds:.3f}").replace(DATA_TOKEN, payload)


def _file_sha(path: Path | None) -> str:
    if path is None:
        return "nobg"
    h = hashlib.sha1()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def cache_key(scene: str, data: dict[str, Any], seconds: float, size: tuple[int, int],
              background: str | Path | None = None) -> str:
    bg = Path(background) if background else None
    parts = [compose_html(scene, data, seconds, background=bg is not None),
             f"{size[0]}x{size[1]}", _file_sha(bg)]
    for name in SHARED_FILES:
        p = scenes_dir() / name
        parts.append(p.read_text(encoding="utf-8") if p.is_file() else "")
    return hashlib.sha1("\n".join(parts).encode("utf-8")).hexdigest()[:20]


def _env() -> dict[str, str]:
    env = {**os.environ, "HYPERFRAMES_NO_TELEMETRY": "1", "DO_NOT_TRACK": "1"}
    if config.MOTION_BROWSER:
        env["HYPERFRAMES_BROWSER_PATH"] = config.MOTION_BROWSER
    if config.FFMPEG_PATH:
        env["PATH"] = str(config.FFMPEG_PATH.parent) + os.pathsep + env.get("PATH", "")
    return env


def render(scene: str, data: dict[str, Any], seconds: float, size: tuple[int, int],
           dest: str | Path, background: str | Path | None = None) -> Path:
    """渲染一段概念动画到 `dest`(mp4,size 画幅,FPS 与成片一致)。失败抛 MotionError。
    background:这一拍的实拍底(mp4),动画压在它上面;不给就用暗色径向底。"""
    from .render import FPS

    ok, why = config.motion_status()
    if not ok:
        raise MotionError(why)
    w, h = size
    if (w, h) != (1920, 1080):
        # 模板按 1920×1080 写死布局;别的画幅等到真有需要再做
        raise MotionError(f"概念动画只支持 1920×1080,收到 {w}×{h}")
    seconds = max(1.0, float(seconds))
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    bg = Path(background) if background and Path(background).is_file() else None
    key = cache_key(scene, data, seconds, size, bg)
    cached = cache_dir() / f"{scene}_{key}.mp4"
    if cached.is_file() and cached.stat().st_size > 0:
        shutil.copyfile(cached, dest)
        return dest

    work = cache_dir() / f"_work_{key}"
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True, exist_ok=True)
    try:
        (work / "index.html").write_text(compose_html(scene, data, seconds, background=bg is not None),
                                         encoding="utf-8")
        if bg is not None:
            shutil.copyfile(bg, work / "bg.mp4")
        for name in SHARED_FILES:
            shutil.copyfile(scenes_dir() / name, work / name)
        shutil.copyfile(_gsap(), work / "gsap.min.js")
        out = work / "out.mp4"
        cmd = ["node", str(_cli()), "render", str(work), "-o", str(out), "--fps", str(FPS),
               "--workers", str(config.MOTION_WORKERS), "--quiet"]
        try:
            proc = subprocess.run(cmd, env=_env(), capture_output=True, text=True,
                                  encoding="utf-8", errors="replace",
                                  timeout=config.MOTION_TIMEOUT)
        except subprocess.TimeoutExpired as exc:
            raise MotionError(f"概念动画 {scene} 渲染超时({config.MOTION_TIMEOUT}s)") from exc
        if proc.returncode != 0 or not out.is_file() or out.stat().st_size == 0:
            tail = (proc.stderr or proc.stdout or "")[-400:]
            raise MotionError(f"概念动画 {scene} 渲染失败:{tail}")
        shutil.copyfile(out, cached)
        shutil.copyfile(out, dest)
        return dest
    finally:
        shutil.rmtree(work, ignore_errors=True)


def scene_data(overlay: dict[str, Any], subject: str) -> dict[str, Any]:
    """concept 拍的叠加数据 → 模板数据。rows 只取第一列(对比模板取两列)。"""
    rows = []
    for r in overlay.get("rows") or []:
        values = list(r.get("values") or [])
        if not values:
            continue
        rows.append({"label": r.get("label", ""), "value": values[0], "values": values[:2]})
    return {"subject": subject, "title": overlay.get("title") or "",
            "columns": list(overlay.get("columns") or [])[:2], "rows": rows,
            "source": overlay.get("source", "")}
