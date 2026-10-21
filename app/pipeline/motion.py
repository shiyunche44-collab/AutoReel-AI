"""概念动画:用 HyperFrames(HTML + GSAP → mp4)渲染 concept 版式的整屏动画。

解说视频里最难拍的是"看不见的东西"—— 电池怎么排、激光雷达怎么扫、零百加速多快。
官方素材里没有这些画面,只能反复铺几张差不多的官图。这里用程序化动画补上:

- 模板在 `motion/scenes/<名>.html`(品类在 `vocab.concept_scenes` 里声明用哪些);
- 数据由这里写成 `window.__DATA__` 注入模板 —— 只放**核对过事实**的行,模板只负责画,
  图形本身(刻度、曲线、线稿)是示意,不承载任何未核对的数;
- 每次渲染一个独立工作目录(模板 + `_lib.js` + `_base.css` + 本地 gsap),不联网;
- 风格(`motion/styles/*.css`)只换皮:覆盖 `_base.css` 的颜色变量、换字体、加少量附加规则,
  动画结构 7 个模板共用;
- 结果按 (模板内容 + 风格 + 数据 + 时长 + 画幅) 缓存在 `data/_cache/motion/`,重拍、重合成不重渲;
- `snapshot` 只截几个时刻的 PNG(十几秒),给"先看风格再渲染"用。

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

#: 模板目录里每次渲染都要拷进工作目录的公用文件
SHARED_FILES = ("_lib.js", "_base.css")
#: 模板里的占位符:时长写进根节点 data-duration,数据写进 window.__DATA__
SECONDS_TOKEN = "__SECONDS__"
DATA_TOKEN = "/*__DATA__*/{}"
#: 模板里的底占位:有实拍就换成 <video>,没有就换成暗色径向底
BACKGROUND_TOKEN = "<!--__BACKGROUND__-->"


#: 模板里引用公用样式的那一行,渲染前换成内联 <style>
BASE_CSS_LINK = '<link rel="stylesheet" href="_base.css" />'
#: 模板声明自己要什么底:<meta name="hf-bg" content="studio|footage">。
#: studio = 模板自带暗色影棚底(产品镜,不铺实拍);footage = 压在这一拍的一段实拍上(默认)
_BG_META = re.compile(r'<meta\s+name="hf-bg"\s+content="(\w+)"')
#: 模板声明想要哪个视角的车图:<meta name="hf-photo" content="exterior_side,exterior_45">;没有 = 不要车图
_PHOTO_META = re.compile(r'<meta\s+name="hf-photo"\s+content="([\w,]+)"')
#: 车图在渲染工作目录里的文件名
PHOTO_FILE = "car.png"
EDGES_FILE = "car_edges.png"


class MotionError(RuntimeError):
    """概念动画渲染失败。调用方据此降级,不应让整拍失败。"""


# --------------------------------------------------------------------------
# 风格
# --------------------------------------------------------------------------
#: 默认风格:_base.css 自身的取值(实车图 + 标注,暗色影棚 + 荧光绿)
DEFAULT_STYLE = "studio"
#: 模板里默认的字体名字面量;风格换字体时整段替换(HyperFrames 只内嵌页面里写明的字体)
NUM_FONT = '"Montserrat"'
CN_FONT = '"Noto Sans SC", "Microsoft YaHei"'
_META = re.compile(r"@(\w[\w-]*)\s+(.+)")


@dataclass(frozen=True)
class Style:
    id: str
    name: str
    desc: str
    order: int
    css: str
    #: 数字字体(HyperFrames 内置的那几种之一,如 "JetBrains Mono");空 = 不换
    font_num: str = ""
    #: 中文字体栈(本机字体,如 "STSong", "SimSun");空 = 不换
    font_cn: str = ""
    #: 浅色风格(界面上分组用)
    light: bool = False

    def summary(self) -> dict[str, Any]:
        return {"id": self.id, "name": self.name, "desc": self.desc, "light": self.light}


def styles_dir() -> Path:
    return config.MOTION_DIR / "styles"


def _parse_style(path: Path) -> Style:
    css = path.read_text(encoding="utf-8")
    head = css.split("*/", 1)[0] if css.lstrip().startswith("/*") else ""
    meta = {m.group(1): m.group(2).strip() for m in _META.finditer(head)}
    # 只注入头注释之后的规则;只有头注释的风格(默认风格)注入为空,和不选风格一字不差
    body = css.split("*/", 1)[1].strip() if head else css.strip()
    return Style(id=path.stem, name=meta.get("name", path.stem), desc=meta.get("desc", ""),
                 order=int(meta.get("order", "99")), css=body,
                 font_num=meta.get("font-num", ""), font_cn=meta.get("font-cn", ""),
                 light=meta.get("light", "") in ("1", "yes", "true"))


def styles() -> list[Style]:
    """全部风格,按 @order 排。目录不存在时只有默认风格(空 CSS)。"""
    d = styles_dir()
    found = [_parse_style(p) for p in d.glob("*.css") if not p.stem.startswith("_")] if d.is_dir() else []
    if not any(s.id == DEFAULT_STYLE for s in found):
        found.append(Style(id=DEFAULT_STYLE, name="实车标注", desc="", order=0, css=""))
    return sorted(found, key=lambda s: (s.order, s.id))


def get_style(style_id: str | None) -> Style:
    """按 id 取风格;不认识的 id 退回默认风格(风格文件被删了也照常出片)。"""
    all_ = {s.id: s for s in styles()}
    return all_.get(style_id or DEFAULT_STYLE) or all_[DEFAULT_STYLE]


def _apply_style(html: str, style: Style) -> str:
    """风格 CSS 接在公用样式后面(覆盖变量),再换字体字面量。只动模板,不碰数据。"""
    if style.css:
        html = html.replace("</head>", f"<style>\n/* style: {style.id} */\n{style.css}\n</style>\n</head>", 1)
    if style.font_num:
        html = html.replace(NUM_FONT, style.font_num)
    if style.font_cn:
        html = html.replace(CN_FONT, style.font_cn)
    return html


def scenes_dir() -> Path:
    return config.MOTION_DIR / "scenes"


def available_scenes() -> set[str]:
    d = scenes_dir()
    return {p.stem for p in d.glob("*.html") if not p.stem.startswith("_")} if d.is_dir() else set()


def _template(scene: str) -> str:
    path = scenes_dir() / f"{scene}.html"
    return path.read_text(encoding="utf-8") if path.is_file() else ""


def scene_background(scene: str) -> str:
    """模板要的底:studio(自带影棚)或 footage(压在实拍上)。"""
    m = _BG_META.search(_template(scene))
    return m.group(1) if m else "footage"


def scene_photo_views(scene: str) -> list[str]:
    """模板想要的车图视角(按优先级);空 = 模板不用车图。"""
    m = _PHOTO_META.search(_template(scene))
    return [v for v in m.group(1).split(",") if v] if m else []


def cache_dir() -> Path:
    return config.DATA_DIR / "_cache" / "motion"


def _cli() -> Path:
    return config.MOTION_DIR / "node_modules" / "hyperframes" / "bin" / "hyperframes.mjs"


def _gsap() -> Path:
    return config.MOTION_DIR / "node_modules" / "gsap" / "dist" / "gsap.min.js"


def compose_html(scene: str, data: dict[str, Any], seconds: float, background: bool = False,
                 photo: Cutout | None = None, style: str | None = None) -> str:
    """把数据和时长灌进模板,返回完整 HTML。模板缺占位符直接报错(模板写错了)。
    background=True:底是工作目录里的 bg.mp4(这一拍的实拍),否则用暗色径向底。
    photo:抠好的车图,几何信息写进 `__DATA__.photo`,图片本身由 render 拷成 car.png。"""
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
    # 公用样式内联进页面:HyperFrames 只扫页面里的 font-family 来内嵌字体(Montserrat),
    # 外链的 _base.css 它看不到,数字会退回衬线体
    css = scenes_dir() / "_base.css"
    if BASE_CSS_LINK in html and css.is_file():
        html = html.replace(BASE_CSS_LINK, "<style>\n" + css.read_text(encoding="utf-8") + "\n</style>")
    html = _apply_style(html, get_style(style))
    extra: dict[str, Any] = {"seconds": round(seconds, 3)}
    if photo is not None:
        extra["photo"] = {**photo.geometry(), "src": PHOTO_FILE,
                          "edges": EDGES_FILE if photo.edges else ""}
    payload = json.dumps({**data, **extra}, ensure_ascii=False)
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
              background: str | Path | None = None, photo: Cutout | None = None,
              style: str | None = None) -> str:
    bg = Path(background) if background else None
    parts = [compose_html(scene, data, seconds, background=bg is not None, photo=photo, style=style),
             f"{size[0]}x{size[1]}", _file_sha(bg),
             _file_sha(Path(photo.png)) if photo else "nophoto"]
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
    if tuple(size) != (1920, 1080):
        # 模板按 1920×1080 写死布局;别的画幅等到真有需要再做
        raise MotionError(f"概念动画只支持 1920×1080,收到 {size[0]}×{size[1]}")


def _inputs(background: str | Path | None, photo: Cutout | None) -> tuple[Path | None, Cutout | None]:
    bg = Path(background) if background and Path(background).is_file() else None
    if photo is not None and not Path(photo.png).is_file():
        photo = None
    return bg, photo


def _prepare(work: Path, scene: str, data: dict[str, Any], seconds: float, bg: Path | None,
             photo: Cutout | None, style: str | None) -> None:
    """渲染 / 截图共用的工作目录:index.html + 公用文件 + gsap + 底 + 车图。"""
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True, exist_ok=True)
    (work / "index.html").write_text(
        compose_html(scene, data, seconds, background=bg is not None, photo=photo, style=style),
        encoding="utf-8")
    if bg is not None:
        shutil.copyfile(bg, work / "bg.mp4")
    if photo is not None:
        shutil.copyfile(photo.png, work / PHOTO_FILE)
        if photo.edges and Path(photo.edges).is_file():
            shutil.copyfile(photo.edges, work / EDGES_FILE)
    for name in SHARED_FILES:
        shutil.copyfile(scenes_dir() / name, work / name)
    shutil.copyfile(_gsap(), work / "gsap.min.js")


def render(scene: str, data: dict[str, Any], seconds: float, size: tuple[int, int],
           dest: str | Path, background: str | Path | None = None,
           photo: Cutout | None = None, style: str | None = None) -> Path:
    """渲染一段概念动画到 `dest`(mp4,size 画幅,FPS 与成片一致)。失败抛 MotionError。
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
        _prepare(work, scene, data, seconds, bg, photo, style)
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
        _prepare(work, scene, data, seconds, bg, photo, style)
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
