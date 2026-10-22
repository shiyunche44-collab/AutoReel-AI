"""概念动画:用 HyperFrames(HTML + GSAP → mp4)渲染 concept 版式的整屏动画。

解说视频里最难拍的是"看不见的东西"—— 电池怎么排、激光雷达怎么扫、零百加速多快。
官方素材里没有这些画面,只能反复铺几张差不多的官图。这里用程序化动画补上:

- 模板在 `motion/scenes/<名>.html`(品类在 `vocab.concept_scenes` 里声明用哪些);
- 数据由这里写成 `window.__DATA__` 注入模板 —— 只放**核对过事实**的行,模板只负责画,
  图形本身(刻度、曲线、线稿)是示意,不承载任何未核对的数;
- 每次渲染一个独立工作目录(模板 + `_lib.js` + 本地 gsap),不联网;
- 模版:默认用每个场景的专用模板;另有一批通用模版(`motion/templates/*.html`),
  一个 HTML 套所有场景,版式、动效、字体、配色各不相同(不是换皮);
- 结果按 (模板内容 + 风格 + 数据 + 时长 + 画幅) 缓存在 `data/_cache/motion/`,重拍、重合成不重渲;
- `snapshot` 只截几个时刻的 PNG(十几秒),给"先看风格再渲染"用。

骨架:模版文件只写自己的东西(说明与 meta、样式、动效),HTML 外壳只有一份 ——
`motion/_shell.html` 负责画幅、复位、根容器和脚本挂载,`motion/_lib.js` 是共用的小工具
(起手用 `HF.boot()`),家族样式在 `scenes/_base.css` 和 `templates/_base.css`。
渲染时 `compose_html` 把骨架和模版片段拼成完整 HTML 再交给 HyperFrames。
**骨架文件里只能写 ASCII**:HyperFrames 按页面上出现过的字符裁内嵌字体子集,
连注释里的中文也算,子集一变字形度量就变,文字会有亚像素位移。

渲染不了(没装 node / HyperFrames / Chrome,或渲染报错)抛 `MotionError`,
调用方退回数据卡,流水线照常出片。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .. import config
from .motion_assets import Cutout

#: 渲染时要拷进工作目录的公用文件(模版片段之外、页面要单独引的)
SHARED_FILES = ("_lib.js",)
#: 模版片段里的占位符:时长写进根节点 data-duration,数据写进 window.__DATA__
SECONDS_TOKEN = "__SECONDS__"
DATA_TOKEN = "/*__DATA__*/{}"
#: 模版片段里的底占位:有实拍就换成 <video>,没有就换成暗色径向底
BACKGROUND_TOKEN = "<!--__BACKGROUND__-->"

#: 概念动画画幅:横屏 16:9 / 竖屏 9:16,尺寸取自 config(画幅的单一来源)。
#: 骨架(_shell.html)里的画幅占位按传入的 size 替换,根节点带 data-aspect 告诉模版是横是竖。
#: (getattr 兜底:config 的画幅常量和本模块分两步合入,单独跑这个模块时也能用)
LANDSCAPE = getattr(config, "LANDSCAPE", "16:9")
PORTRAIT = getattr(config, "PORTRAIT", "9:16")
_SIZES = {LANDSCAPE: getattr(config, "LANDSCAPE_SIZE", (1920, 1080)),
          PORTRAIT: getattr(config, "PORTRAIT_SIZE", (1080, 1920))}
#: 横屏画幅(默认)。老调用方仍用 motion.SIZE
SIZE = _SIZES[LANDSCAPE]
#: 根节点 data-aspect 的取值
ASPECT_ATTR = {LANDSCAPE: "landscape", PORTRAIT: "portrait"}
#: HTML 骨架:所有模版共用这一份(模版片段只写自己的 meta / 样式 / 动效)
SHELL_FILE = "_shell.html"
#: 骨架里的槽位
SHELL_META = "<!--__META__-->"
SHELL_CSS = "<!--__CSS__-->"
SHELL_JS = "<!--__JS__-->"
#: 骨架里的画幅占位
WIDTH_TOKEN = "__WIDTH__"
HEIGHT_TOKEN = "__HEIGHT__"
ASPECT_TOKEN = "__ASPECT__"


def size_for(aspect: str | None) -> tuple[int, int]:
    """画幅名 -> 概念动画像素尺寸。未知画幅退回横屏。"""
    fn = getattr(config, "size_for_aspect", None)
    if fn is not None:
        return tuple(fn(aspect))
    return _SIZES.get(aspect or LANDSCAPE, SIZE)


def aspect_of(size: tuple[int, int]) -> str:
    """像素尺寸 -> 画幅名:竖着的就是竖屏。"""
    w, h = size
    return PORTRAIT if w and h and h > w else LANDSCAPE

#: 模版声明自己要什么底:<meta name="hf-bg" content="studio|footage">。
#: studio = 模版自带暗色影棚底(产品镜,不铺实拍);footage = 压在这一拍的一段实拍上(默认)
_BG_META = re.compile(r'<meta\s+name="hf-bg"\s+content="(\w+)"')
#: 模版声明想要哪个视角的车图:<meta name="hf-photo" content="exterior_side,exterior_45">;没有 = 不要车图
_PHOTO_META = re.compile(r'<meta\s+name="hf-photo"\s+content="([\w,]+)"')
#: 车图在渲染工作目录里的文件名
PHOTO_FILE = "car.png"
EDGES_FILE = "car_edges.png"


class MotionError(RuntimeError):
    """概念动画渲染失败。调用方据此降级,不应让整拍失败。"""


# --------------------------------------------------------------------------
# 风格
# --------------------------------------------------------------------------
#: 默认模版:每个场景各自的专用模板(`motion/scenes/<场景>.html`,实车图 + 标注)
DEFAULT_STYLE = "studio"
#: 通用模版声明自己要不要车图:<meta name="hf-photo" content="scene"> = 用这个场景模板要的视角
_SCENE_PHOTO = "scene"
_META = re.compile(r"@(\w[\w-]*)\s+(.+)")
#: 场景的中文栏目名(通用模版的眉标用)
KICKERS = {"accel": "加速", "battery": "电池", "charging": "补能", "chip": "智驾芯片",
           "compare": "对比", "dims": "尺寸", "lidar": "感知"}


@dataclass(frozen=True)
class Style:
    """概念动画模版。默认模版用每个场景的专用模板;其余是通用模版(一个 HTML 套所有场景),
    版式、动效、字体、配色各成一套,不是换皮。"""
    id: str
    name: str
    desc: str
    order: int
    #: 通用模版的 HTML;None = 默认模版(用场景专用模板)
    path: Path | None = None
    #: 浅色模版(界面上分组用)
    light: bool = False
    #: 通用模版要不要车图(要的话按场景模板声明的视角取)
    photo: bool = False
    #: 截示例图的时刻(秒,7 秒示例里画面最完整的一刻);0 = 用默认时刻
    sample_at: float = 0.0
    #: 布局适配过竖屏(注释 meta 里写 `@portrait yes`)
    portrait: bool = False

    def summary(self) -> dict[str, Any]:
        return {"id": self.id, "name": self.name, "desc": self.desc, "light": self.light}


def templates_dir() -> Path:
    return config.MOTION_DIR / "templates"


def _header_meta(html: str) -> dict[str, str]:
    """模版文件头第一段注释里的 `@键 值`。"""
    m = re.search(r"<!--(.*?)-->", html, re.S)
    return {k.group(1): k.group(2).strip() for k in _META.finditer(m.group(1))} if m else {}


def _yes(value: str | None) -> bool:
    return (value or "").strip().lower() in ("1", "yes", "true")


def _parse_template(path: Path) -> Style:
    html = path.read_text(encoding="utf-8")
    meta = _header_meta(html)
    pm = _PHOTO_META.search(html)
    return Style(id=path.stem, name=meta.get("name", path.stem), desc=meta.get("desc", ""),
                 order=int(meta.get("order", "99")), path=path,
                 light=_yes(meta.get("light")),
                 photo=bool(pm and pm.group(1) == _SCENE_PHOTO),
                 sample_at=float(meta.get("sample", "0") or 0),
                 portrait=_yes(meta.get("portrait")))


def styles() -> list[Style]:
    """全部模版,默认模版排第一,其余按 @order 排。"""
    d = templates_dir()
    found = [_parse_template(p) for p in d.glob("*.html") if not p.stem.startswith("_")] if d.is_dir() else []
    found = [s for s in found if s.id != DEFAULT_STYLE]
    found.append(Style(id=DEFAULT_STYLE, name="实车标注",
                       desc="影棚实车 + X 光扫描 + 引线标注,每个场景专门设计的动画", order=0))
    return sorted(found, key=lambda s: (s.order, s.id))


def get_style(style_id: str | None) -> Style:
    """按 id 取模版;不认识的 id 退回默认模版(模版文件被删了也照常出片)。"""
    all_ = {s.id: s for s in styles()}
    return all_.get(style_id or DEFAULT_STYLE) or all_[DEFAULT_STYLE]


def scenes_dir() -> Path:
    return config.MOTION_DIR / "scenes"


def available_scenes() -> set[str]:
    d = scenes_dir()
    return {p.stem for p in d.glob("*.html") if not p.stem.startswith("_")} if d.is_dir() else set()


def _template(scene: str) -> str:
    path = scenes_dir() / f"{scene}.html"
    return path.read_text(encoding="utf-8") if path.is_file() else ""


def supports_portrait(style: str | None, scene: str | None = None) -> bool:
    """这个模版的布局适配过竖屏没有(文件头注释里写 `@portrait yes`)。
    通用模版看模版文件自己;默认模版用场景专用模板,看那个场景文件的文件头(不给 scene 算没适配)。
    没适配的模版也能按竖屏尺寸渲,只是版式还是横屏那套,会挤会出界。"""
    st = get_style(style)
    if st.path is not None:
        return st.portrait
    return bool(scene) and _yes(_header_meta(_template(scene)).get("portrait"))


def scene_background(scene: str, style: str | None = None) -> str:
    """模板要的底:studio(自带影棚)或 footage(压在实拍上);通用模版自带底(own),不铺实拍。"""
    if get_style(style).path is not None:
        return "own"
    m = _BG_META.search(_template(scene))
    return m.group(1) if m else "footage"


def scene_photo_views(scene: str, style: str | None = None) -> list[str]:
    """模板想要的车图视角(按优先级);空 = 模板不用车图。通用模版要车图时用场景模板声明的视角。"""
    st = get_style(style)
    if st.path is not None and not st.photo:
        return []
    m = _PHOTO_META.search(_template(scene))
    return [v for v in m.group(1).split(",") if v] if m else []


def cache_dir() -> Path:
    return config.DATA_DIR / "_cache" / "motion"


def _cli() -> Path:
    return config.MOTION_DIR / "node_modules" / "hyperframes" / "bin" / "hyperframes.mjs"


def _gsap() -> Path:
    return config.MOTION_DIR / "node_modules" / "gsap" / "dist" / "gsap.min.js"


def _three() -> Path:
    return config.MOTION_DIR / "node_modules" / "three" / "build" / "three.min.js"


#: 模版页面里引用了它才拷 three.js(真 3D 模版用,600KB,别的模版不带)
THREE_TAG = 'src="three.min.js"'


def _shell() -> str:
    """HTML 骨架:所有模版共用这一份。"""
    path = config.MOTION_DIR / SHELL_FILE
    if not path.is_file():
        raise MotionError(f"缺少骨架 {path}")
    return path.read_text(encoding="utf-8")


def _split_fragment(frag: str, name: str) -> tuple[str, str, str]:
    """模版片段 -> (说明与 meta, 样式, 动效)。片段写坏了直接报错。"""
    if "<style>" not in frag or "</style>" not in frag or "<script>" not in frag or "</script>" not in frag:
        raise MotionError(f"模版 {name} 不是合法片段:要有 <style> 和 <script>")
    head, _, rest = frag.partition("<style>")
    css, _, rest = rest.partition("</style>")
    js = rest.partition("<script>")[2].rpartition("</script>")[0]
    return head.strip(), css, js


def _family_css(family: str) -> str:
    """家族样式:场景专用(scenes)或通用模版专用(templates)。缩进对齐片段里的样式。"""
    path = config.MOTION_DIR / family / "_base.css"
    if not path.is_file():
        return ""
    css = path.read_text(encoding="utf-8")
    return "\n".join("    " + ln if ln.strip() else ln for ln in css.splitlines()) + "\n"


def compose_html(scene: str, data: dict[str, Any], seconds: float, background: bool = False,
                 photo: Cutout | None = None, style: str | None = None,
                 size: tuple[int, int] = SIZE) -> str:
    """把数据和时长灌进模版片段,按骨架拼成完整 HTML。

    模版片段(`motion/scenes/*.html`、`motion/templates/*.html`)只写自己的 meta、样式、动效;
    骨架(`motion/_shell.html`)负责画幅、复位、根容器和脚本挂载 —— 骨架里改一处,全部模版跟着变。

    background=True:底是工作目录里的 bg.mp4(这一拍的实拍),否则用暗色径向底。
    photo:抠好的车图,几何信息写进 `__DATA__.photo`,图片本身由 render 拷成 car.png。
    size:画幅像素尺寸,写进骨架的宽高占位;根节点 data-aspect 标出横竖。"""
    path = scenes_dir() / f"{scene}.html"
    if not path.is_file():
        raise MotionError(f"没有动画模板 {scene}({path})")
    st = get_style(style)
    tpl = st.path or path
    meta, css, js = _split_fragment(tpl.read_text(encoding="utf-8"), tpl.stem)
    family = "templates" if st.path is not None else "scenes"

    html = _shell()
    missing = [t for t in (SECONDS_TOKEN, DATA_TOKEN, BACKGROUND_TOKEN, SHELL_META, SHELL_CSS,
                           SHELL_JS, WIDTH_TOKEN, HEIGHT_TOKEN, ASPECT_TOKEN) if t not in html]
    if missing:
        raise MotionError(f"骨架缺少占位符 {' / '.join(missing)}")
    html = html.replace(SHELL_META, meta)
    html = html.replace(SHELL_CSS, _family_css(family) + css)
    html = html.replace(SHELL_JS, js)
    if background:
        bg = (f'<div class="bgwrap" id="bgwrap"><video id="bg" class="clip" src="bg.mp4" data-start="0" '
              f'data-duration="{seconds:.3f}" muted playsinline></video></div>')
    else:
        bg = '<div class="bgwrap nobg" id="bgwrap"></div>'
    html = html.replace(BACKGROUND_TOKEN, bg)
    extra: dict[str, Any] = {"seconds": round(seconds, 3)}
    if st.path is not None:
        # 通用模版不知道自己在画哪个场景:告诉它场景名和栏目名
        extra.update(scene=scene, kicker=KICKERS.get(scene, ""))
    if photo is not None:
        extra["photo"] = {**photo.geometry(), "src": PHOTO_FILE,
                          "edges": EDGES_FILE if photo.edges else ""}
    payload = json.dumps({**data, **extra}, ensure_ascii=False)
    # 防止数据里出现 </script> 把脚本块提前截断
    payload = payload.replace("</", "<\\/")
    html = (html.replace(SECONDS_TOKEN, f"{seconds:.3f}")
                .replace(WIDTH_TOKEN, str(size[0]))
                .replace(HEIGHT_TOKEN, str(size[1]))
                .replace(ASPECT_TOKEN, ASPECT_ATTR[aspect_of(size)])
                .replace(DATA_TOKEN, payload))
    # 骨架里的槽位必须全部换掉(替换是纯文本,写在注释里也会被换 —— 残留就是槽位写错了)
    leftover = [t for t in (WIDTH_TOKEN, HEIGHT_TOKEN, ASPECT_TOKEN, SHELL_META, SHELL_CSS, SHELL_JS)
                if t in html]
    if leftover:
        raise MotionError(f"拼装后还有没换掉的槽位 {' / '.join(leftover)}")
    return html


def _file_sha(path: Path | None) -> str:
    if path is None:
        return "nobg"
    h = hashlib.sha1()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def cache_key(scene: str, data: dict[str, Any], seconds: float, size: tuple[int, int],
              background: str | Path | None = None, photo: Cutout | None = None,
              style: str | None = None) -> str:
    bg = Path(background) if background else None
    parts = [compose_html(scene, data, seconds, background=bg is not None, photo=photo, style=style,
                          size=size),
             f"{size[0]}x{size[1]}", aspect_of(size), _file_sha(bg),
             _file_sha(Path(photo.png)) if photo else "nophoto"]
    for name in SHARED_FILES:
        p = config.MOTION_DIR / name
        parts.append(p.read_text(encoding="utf-8") if p.is_file() else "")
    return hashlib.sha1("\n".join(parts).encode("utf-8")).hexdigest()[:20]


def _env() -> dict[str, str]:
    env = {**os.environ, "HYPERFRAMES_NO_TELEMETRY": "1", "DO_NOT_TRACK": "1"}
    if config.MOTION_BROWSER:
        env["HYPERFRAMES_BROWSER_PATH"] = config.MOTION_BROWSER
    if config.FFMPEG_PATH:
        env["PATH"] = str(config.FFMPEG_PATH.parent) + os.pathsep + env.get("PATH", "")
    return env


#: 浏览器 GPU 这一进程里是不是已经卡死过。卡过一次后面的直接走软件渲染,不再每拍白等一轮超时
_gpu_hung = False


def _run_cli(scene: str, work: Path, out: Path, seconds: float) -> subprocess.CompletedProcess:
    """跑 HyperFrames。默认让它自己探测浏览器 GPU;GPU 卡死(实测:锁屏 / 远程会话下
    d3d11 初始化挂在 page.goto,一帧都不出)就换软件渲染(SwiftShader)重跑一次。
    软件渲染慢得多(实测约 40 秒出 1 秒画面,压实拍底更慢;多开 worker 反而更慢),
    超时按时长放宽。"""
    from .render import FPS

    global _gpu_hung
    base = ["node", str(_cli()), "render", str(work), "-o", str(out), "--fps", str(FPS), "--quiet"]
    # 软件渲染下页面要先装上几百个中文字体分片,默认 60 秒导航超时常常不够
    software = base + ["--workers", "1", "--no-browser-gpu", "--browser-timeout", "240",
                       "--player-ready-timeout", "180000"]
    sw_timeout = max(config.MOTION_TIMEOUT, int(120 + 90 * seconds))
    attempts = [] if _gpu_hung or config.MOTION_BROWSER_GPU == "off" else [
        (base + ["--workers", str(config.MOTION_WORKERS)], config.MOTION_TIMEOUT, True)]
    attempts.append((software, sw_timeout, False))
    for n, (cmd, timeout, gpu) in enumerate(attempts):
        try:
            return subprocess.run(cmd, env=_env(), capture_output=True, text=True,
                                  encoding="utf-8", errors="replace", timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            if gpu:
                _gpu_hung = True
                continue
            raise MotionError(f"概念动画 {scene} 渲染超时({timeout}s,软件渲染)") from exc
    raise MotionError(f"概念动画 {scene} 没有可用的渲染方式")  # pragma: no cover


def _check(size: tuple[int, int]) -> None:
    ok, why = config.motion_status()
    if not ok:
        raise MotionError(why)
    if tuple(size) not in _SIZES.values():
        # 只认两种成片画幅:模版按这两种尺寸写版式,别的尺寸渲出来也是错位的
        ok_sizes = " / ".join(f"{w}×{h}" for w, h in _SIZES.values())
        raise MotionError(f"概念动画只支持 {ok_sizes},收到 {size[0]}×{size[1]}")


def _inputs(background: str | Path | None, photo: Cutout | None) -> tuple[Path | None, Cutout | None]:
    bg = Path(background) if background and Path(background).is_file() else None
    if photo is not None and not Path(photo.png).is_file():
        photo = None
    return bg, photo


def _prepare(work: Path, scene: str, data: dict[str, Any], seconds: float, bg: Path | None,
             photo: Cutout | None, style: str | None, size: tuple[int, int]) -> None:
    """渲染 / 截图共用的工作目录:index.html + 公用文件 + gsap + 底 + 车图。"""
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True, exist_ok=True)
    (work / "index.html").write_text(
        compose_html(scene, data, seconds, background=bg is not None, photo=photo, style=style,
                     size=size),
        encoding="utf-8")
    if bg is not None:
        shutil.copyfile(bg, work / "bg.mp4")
    if photo is not None:
        shutil.copyfile(photo.png, work / PHOTO_FILE)
        if photo.edges and Path(photo.edges).is_file():
            shutil.copyfile(photo.edges, work / EDGES_FILE)
    for name in SHARED_FILES:
        shutil.copyfile(config.MOTION_DIR / name, work / name)
    shutil.copyfile(_gsap(), work / "gsap.min.js")
    if THREE_TAG in (work / "index.html").read_text(encoding="utf-8"):
        if not _three().is_file():
            raise MotionError(f"模版要用 three.js,但没装:到 motion/ 下跑 npm install({_three()})")
        shutil.copyfile(_three(), work / "three.min.js")


def render(scene: str, data: dict[str, Any], seconds: float, size: tuple[int, int],
           dest: str | Path, background: str | Path | None = None,
           photo: Cutout | None = None, style: str | None = None) -> Path:
    """渲染一段概念动画到 `dest`(mp4,size 画幅,FPS 与成片一致)。失败抛 MotionError。
    size:横屏 1920×1080 或竖屏 1080×1920(`size_for(aspect)`),别的尺寸拒绝。
    background:这一拍的实拍底(mp4),动画压在它上面;不给就用暗色径向底(或模板自带影棚)。
    photo:抠好的车图(`motion_assets.hero_still`),不给时模板走无车图版本。
    style:风格 id(`styles()`),不给 / 不认识用默认风格。"""
    _check(size)
    seconds = max(1.0, float(seconds))
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    bg, photo = _inputs(background, photo)
    key = cache_key(scene, data, seconds, size, bg, photo, style)
    cached = cache_dir() / f"{scene}_{key}.mp4"
    if cached.is_file() and cached.stat().st_size > 0:
        shutil.copyfile(cached, dest)
        return dest

    work = cache_dir() / f"_work_{key}"
    try:
        _prepare(work, scene, data, seconds, bg, photo, style, size)
        out = work / "out.mp4"
        proc = _run_cli(scene, work, out, seconds)
        if proc.returncode != 0 or not out.is_file() or out.stat().st_size == 0:
            tail = (proc.stderr or proc.stdout or "")[-400:]
            raise MotionError(f"概念动画 {scene} 渲染失败:{tail}")
        shutil.copyfile(out, cached)
        shutil.copyfile(out, dest)
        return dest
    finally:
        shutil.rmtree(work, ignore_errors=True)


#: 截图超时(秒)。GPU 下一次十几秒;卡死的 GPU 换软件渲染再试一次
SNAPSHOT_TIMEOUT = 120
_FRAME_RE = re.compile(r"frame-(\d+)-at-")


def _snap_times(at: list[float], seconds: float) -> list[float]:
    return [round(min(max(0.0, t), seconds - 0.05), 2) for t in at] or [round(seconds * 0.7, 2)]


def snapshot_names(scene: str, data: dict[str, Any], seconds: float, at: list[float],
                   size: tuple[int, int], dest_dir: str | Path, background: str | Path | None = None,
                   photo: Cutout | None = None, style: str | None = None) -> list[Path]:
    """`snapshot` 会写到哪几个文件(不截图)。名字含内容哈希:模板、风格、数据任一变了就是新文件。"""
    seconds = max(1.0, float(seconds))
    at = _snap_times(at, seconds)
    bg, photo = _inputs(background, photo)
    key = cache_key(scene, data, seconds, size, bg, photo, style)
    tag = hashlib.sha1(",".join(f"{t:g}" for t in at).encode()).hexdigest()[:6]
    return [Path(dest_dir) / f"{scene}_{get_style(style).id}_{key}_{tag}_{n}.png" for n in range(len(at))]


def snapshot(scene: str, data: dict[str, Any], seconds: float, at: list[float],
             size: tuple[int, int], dest_dir: str | Path, background: str | Path | None = None,
             photo: Cutout | None = None, style: str | None = None) -> list[Path]:
    """只截几个时刻的 PNG(不渲整段视频),给选风格用。返回按 `at` 顺序的图片路径。
    同样按内容缓存:同一拍同一风格再看一次不重截。失败抛 MotionError。"""
    _check(size)
    seconds = max(1.0, float(seconds))
    at = _snap_times(at, seconds)
    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    bg, photo = _inputs(background, photo)
    key = cache_key(scene, data, seconds, size, bg, photo, style)
    tag = "_".join(f"{t:g}" for t in at)
    names = snapshot_names(scene, data, seconds, at, size, dest_dir, bg, photo, style)
    if all(p.is_file() and p.stat().st_size > 0 for p in names):
        return names
    work = cache_dir() / f"_snap_{key}_{tag}"
    try:
        _prepare(work, scene, data, seconds, bg, photo, style, size)
        shots = work / "snaps"
        base = ["node", str(_cli()), "snapshot", str(work), "-o", str(shots),
                "--at", ",".join(f"{t:g}" for t in at), "--no-end", "--describe", "false",
                "--timeout", "20000"]
        gpu = not (_gpu_hung or config.MOTION_BROWSER_GPU == "off")
        attempts = ([base] if gpu else []) + [base + ["--no-browser-gpu"]]
        proc = None
        for cmd in attempts:
            try:
                proc = subprocess.run(cmd, env=_env(), capture_output=True, text=True,
                                      encoding="utf-8", errors="replace", timeout=SNAPSHOT_TIMEOUT)
            except subprocess.TimeoutExpired:
                continue
            if proc.returncode == 0:
                break
        frames = sorted(shots.glob("frame-*.png"), key=lambda f: int(_FRAME_RE.search(f.name).group(1))) \
            if shots.is_dir() else []
        if len(frames) < len(at):
            tail = ((proc.stderr or proc.stdout) if proc else "超时")[-400:]
            raise MotionError(f"概念动画 {scene} 截图失败:{tail}")
        for src, dst in zip(frames, names):
            shutil.copyfile(src, dst)
        return names
    finally:
        shutil.rmtree(work, ignore_errors=True)


def scene_data(overlay: dict[str, Any], subject: str) -> dict[str, Any]:
    """concept 拍的叠加数据 → 模板数据。rows 只取第一列(对比模板取两列)。"""
    rows = []
    for r in overlay.get("rows") or []:
        values = list(r.get("values") or [])
        if not values:
            continue
        rows.append({"label": r.get("label", ""), "value": values[0], "values": values[:2],
                     "tag": r.get("tag", "")})
    return {"subject": subject, "title": overlay.get("title") or "",
            "columns": list(overlay.get("columns") or [])[:2], "rows": rows,
            "source": overlay.get("source", "")}
