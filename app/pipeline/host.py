"""虚拟主持人「柒哥」:成片里常驻一个 Q版形象,嘴型跟着旁白动;开场和收尾放大成主角。

素材(`app/assets/host/<id>/` 的 closed / half / open / blink 四张透明 PNG)由
`scripts/host_sprites.py` 一次性生成。这里只做两件事:

1. `mouth_track`:按旁白音量逐帧定嘴型。字幕 cue 区间外一律闭嘴;每 3–5 秒眨一次眼(只在闭嘴时)。
2. `build`:把逐帧状态压成 concat 清单(状态不变的连续帧合成一段),交给 render.compose 叠到角落。

全屏卡片 / 概念动画 / 数字卡拍(`HIDE_LAYOUTS`)期间隐藏,片尾卡不覆盖。任何失败都返回 None,成片照出。

留人用的三样(都只换贴图,不动任何一拍的时间):

- 开场拍:第一句旁白期间(2-3 秒)放大,说完缩回角落。开场是大字钩子(数据区被大字占着)时,
  改成从画面带左下角探出半身。
- 收尾拍:最后的「下期 / 关注」几句期间再放大,旁边一枚「关注」贴纸。
- 表情:`REACTIONS` 里的整图(素材可选,没有就不出),在立场明确的句子开头闪现约 1 秒。

主持人层因此是整幅透明帧(x = y = 0),同一种(姿态, 大小, 贴纸)只画一张。
"""
from __future__ import annotations

import logging
import random
import shutil
import wave
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image

from .. import config
from . import aspect

log = logging.getLogger(__name__)

ASSET_ROOT = Path(__file__).resolve().parents[1] / "assets" / "host"
STATES = ("closed", "half", "open", "blink")
HIDDEN = "hidden"
#: 全屏卡片 / 概念动画会被挡;stat / panel 的数字卡画在竖屏数据区,和主持人同一位置
HIDE_LAYOUTS = frozenset({"card", "concept", "stat", "panel"})
#: 音量(相对 cue 内 90 分位)超过这两个阈值分别是半张 / 张大
HALF_AT, OPEN_AT = 0.18, 0.55
#: 嘴型最少保持帧数,防止逐帧乱跳
MIN_HOLD = 2
BLINK_FRAMES = 3

#: 开场 / 收尾放大倍数(脚不动,往上长)与大小切换的过渡帧数
BIG = 1.7
TWEEN = 5
#: 开场拍跟第一句旁白走,夹在这两个秒数之间
OPEN_MIN, OPEN_MAX = 2.0, 3.2
#: 收尾拍最多往回覆盖这么多秒
END_MAX = 7.0
#: 片子短于这么多秒不做开场 / 收尾放大(两头会撞在一起)
SHORT_FILM = 12.0
#: 大字钩子开场:半身占画面带高度的比例、取素材上部多少当半身
BUST_BAND, BUST_CROP = 0.62, 0.58
#: 表情整图(app/assets/host/<id>/<名字>.png,由 scripts/host_sprites.py expressions 生成)
REACTIONS = ("surprised", "shake", "thumbs", "point")
REACT_SECONDS = 0.9
#: 两次表情至少隔这么多秒,免得变成抽搐
REACT_GAP = 6.0
#: 句子立场 → 表情。先判否定(「不值得」里也有「值得」)
_REACT_WORDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("shake", ("别买", "别冲", "别选", "不值", "不推荐", "不建议", "慎选", "慎重", "慎冲", "踩坑", "坑人", "吃亏", "劝退", "算了")),
    ("thumbs", ("值得", "推荐", "划算", "省心", "放心", "够用", "闭眼", "真香")),
    ("surprised", ("居然", "竟然", "没想到", "翻倍", "对折", "腰斩", "反常")),
    ("point", ("看这", "注意", "重点", "记住", "关键")),
)
_FOLLOW_WORDS = ("关注", "下期", "下一期", "下一集", "下次")


def sprite_dir(host_id: str | None = None) -> Path:
    return ASSET_ROOT / (host_id or config.HOST_ID)


def available(host_id: str | None = None) -> bool:
    d = sprite_dir(host_id)
    return all((d / f"{s}.png").is_file() for s in STATES)


def _rms(wav_path: Path, fps: int, n_frames: int) -> np.ndarray:
    """逐视频帧的音量(RMS)。读不了返回全 0。"""
    try:
        with wave.open(str(wav_path), "rb") as w:
            ch, width, rate = w.getnchannels(), w.getsampwidth(), w.getframerate()
            raw = w.readframes(w.getnframes())
    except (OSError, wave.Error, EOFError):
        return np.zeros(n_frames)
    dtype = {1: np.int8, 2: np.int16, 4: np.int32}.get(width)
    if dtype is None:
        return np.zeros(n_frames)
    pcm = np.frombuffer(raw, dtype=dtype).astype(np.float32)
    if ch > 1:
        pcm = pcm[: len(pcm) // ch * ch].reshape(-1, ch).mean(axis=1)
    hop = rate / fps
    out = np.zeros(n_frames)
    for i in range(n_frames):
        seg = pcm[int(i * hop): int((i + 1) * hop)]
        if len(seg):
            out[i] = float(np.sqrt(np.mean(seg * seg)))
    return out


def mouth_track(rms: Sequence[float], cues: Sequence[dict], fps: int,
                seed: int = 0) -> list[str]:
    """逐帧嘴型:cue 外闭嘴;cue 内按相对音量分 closed / half / open;闭嘴时偶尔眨眼。"""
    n = len(rms)
    level = np.asarray(rms, dtype=float)
    speaking = np.zeros(n, bool)
    for c in cues:
        a = max(0, int(float(c.get("start", 0)) * fps))
        b = min(n, int(float(c.get("end", 0)) * fps + 0.5))
        speaking[a:b] = True
    ref = float(np.percentile(level[speaking], 90)) if speaking.any() else 0.0
    states: list[str] = []
    for i in range(n):
        if not speaking[i] or ref <= 0:
            states.append("closed")
            continue
        r = level[i] / ref
        states.append("open" if r >= OPEN_AT else "half" if r >= HALF_AT else "closed")
    # 最短保持:短于 MIN_HOLD 的段并入前一段
    i = 0
    while i < n:
        j = i
        while j < n and states[j] == states[i]:
            j += 1
        if j - i < MIN_HOLD and i > 0:
            for k in range(i, j):
                states[k] = states[i - 1]
        i = j
    rng = random.Random(seed)
    t = int(fps * rng.uniform(1.5, 3.0))
    while t < n:
        if all(s == "closed" for s in states[t:t + BLINK_FRAMES]):
            for k in range(t, min(n, t + BLINK_FRAMES)):
                states[k] = "blink"
        t += int(fps * rng.uniform(3.0, 5.0))
    return states


def hide_spans(layouts: Sequence[str], durations: Sequence[float], fps: int,
               n_frames: int) -> np.ndarray:
    """全屏拍对应的帧标 True。"""
    mask = np.zeros(n_frames, bool)
    t = 0.0
    for lay, dur in zip(layouts, durations, strict=False):
        if lay in HIDE_LAYOUTS:
            mask[int(t * fps): min(n_frames, int((t + dur) * fps))] = True
        t += dur
    return mask


def placement(size: tuple[int, int], sprite_h: int, sprite_w: int) -> tuple[int, int]:
    """左下角,避开右侧平台按钮区。竖屏:站在实拍带下方的数据区(不挡实拍),脚落在字幕区上沿;
    横屏:左下留边。"""
    w, h = size
    if aspect.is_portrait_size(size):
        g = aspect.portrait_grid(size)
        return int(w * aspect.PORTRAIT_MARGIN), g.data.bottom - sprite_h
    return int(w * 0.03), h - sprite_h - int(h * 0.03)


def sprite_height(size: tuple[int, int]) -> int:
    w, h = size
    if aspect.is_portrait_size(size):
        g = aspect.portrait_grid(size)
        return g.data.bottom - g.band.bottom - 8
    return int(h * 0.30)


def _segments(states: Sequence[Any], fps: int) -> list[tuple[Any, float]]:
    segs: list[tuple[Any, float]] = []
    for s in states:
        if segs and segs[-1][0] == s:
            segs[-1] = (s, segs[-1][1] + 1 / fps)
        else:
            segs.append((s, 1 / fps))
    return segs


def reaction_for(text: str) -> str:
    """这句话配哪个表情;立场不明确 = 空串。"""
    for name, words in _REACT_WORDS:
        if any(w in (text or "") for w in words):
            return name
    return ""


def opening_span(cues: Sequence[dict], layouts: Sequence[str], total: float) -> float:
    """开场拍到第几秒为止;0 = 不做(没旁白、片子太短、第一拍本来就要隐藏)。"""
    if not cues or total < SHORT_FILM or not layouts or layouts[0] in HIDE_LAYOUTS:
        return 0.0
    return min(OPEN_MAX, max(OPEN_MIN, float(cues[0].get("end", 0) or 0)))


def ending_span(cues: Sequence[dict], total: float) -> float | None:
    """收尾拍从第几秒开始:末尾连续几句「下期 / 关注」的第一句;一句都没有就从最后一句起。"""
    if not cues or total < SHORT_FILM:
        return None
    # 最后一句太短时往前多留一点,贴纸至少露 2.5 秒
    start = min(float(cues[-1].get("start", 0) or 0), total - 2.5)
    for c in reversed(cues):
        at = float(c.get("start", 0) or 0)
        if at < total - END_MAX or not any(w in str(c.get("text") or "") for w in _FOLLOW_WORDS):
            break
        start = at
    start = max(start, total - END_MAX)
    return start if total - start >= 1.5 else None


def scale_track(n: int, fps: int, open_end: float, end_start: float | None) -> list[float]:
    """逐帧大小:开场 / 收尾是 BIG,中间是 1,交界用 TWEEN 帧过渡(第一帧直接是大的)。"""
    a = int(open_end * fps)
    b = int(end_start * fps) if end_start is not None else n + TWEEN
    out = []
    for i in range(n):
        if i < a or i >= b + TWEEN:
            k = 1.0
        elif i >= b:
            k = (i - b + 1) / TWEEN
        elif a and i < a + TWEEN:
            k = 1 - (i - a + 1) / TWEEN
        else:
            k = 0.0
        out.append(round(1 + (BIG - 1) * k, 3))
    return out


def reaction_track(cues: Sequence[dict], fps: int, n: int, have: Sequence[str],
                   end_start: float | None = None) -> dict[int, str]:
    """帧 → 表情名。只用 `have` 里有素材的;收尾拍开头固定来一个指向 / 点赞。"""
    out: dict[int, str] = {}
    last = -REACT_GAP
    picks: list[tuple[float, str]] = []
    for c in cues:
        at = float(c.get("start", 0) or 0)
        name = reaction_for(str(c.get("text") or ""))
        if end_start is not None and at >= end_start:
            continue
        if name in have and at - last >= REACT_GAP and at > OPEN_MAX:
            picks.append((at, name))
            last = at
    if end_start is not None:
        closing = next((r for r in ("point", "thumbs") if r in have), "")
        if closing:
            picks = [(t, r) for t, r in picks if end_start - t >= REACT_SECONDS + 0.5]
            picks.append((end_start, closing))
    for at, name in picks:
        for i in range(int(at * fps), min(n, int((at + REACT_SECONDS) * fps))):
            out[i] = name
    return out


def _font(px: int):
    from PIL import ImageFont

    from .narration import _font_file

    path = _font_file(bold=True)
    try:
        return ImageFont.truetype(path, px) if path else ImageFont.load_default(size=px)
    except OSError:
        return ImageFont.load_default(size=px)


def follow_lines(name: str = "", series: str = "") -> tuple[str, str]:
    """收尾贴纸的两行字:关注引导 + 下期在哪见。"""
    return (f"+ 关注{name}" if name else "+ 点个关注",
            f"下期「{series}」见" if series else "下期接着聊")


def _sticker(canvas: Image.Image, box: tuple[int, int, int, int], lines: tuple[str, str],
             accent: tuple[int, int, int]) -> None:
    """在 box(左, 上, 右, 下)里画关注贴纸:强调色圆角条 + 一行白字说明。放不下就缩字。"""
    from PIL import ImageDraw

    x0, y0, x1, y1 = box
    room = x1 - x0
    if room < 120:
        return
    draw = ImageDraw.Draw(canvas)
    px = max(28, min(84, (y1 - y0) // 4))
    while True:
        big, small = _font(px), _font(int(px * 0.66))
        pad = px // 2
        if px <= 24 or (draw.textlength(lines[0], font=big) + 2 * pad <= room
                        and draw.textlength(lines[1], font=small) + pad <= room):
            break
        px -= 4
    tw = int(draw.textlength(lines[0], font=big))
    pill_h = px + 2 * pad
    gap = px // 3
    block = pill_h + gap + int(px * 0.66 * 1.3)
    y = (y0 + y1 - block) // 2
    draw.rounded_rectangle([x0, y, x0 + tw + 2 * pad, y + pill_h], radius=pill_h // 2,
                           fill=(*accent, 255))
    draw.text((x0 + pad, y + pad - px // 8), lines[0], font=big, fill=(255, 255, 255, 255))
    draw.text((x0 + pad // 2, y + pill_h + gap), lines[1], font=small, fill=(255, 255, 255, 255),
              stroke_width=max(2, px // 14), stroke_fill=(0, 0, 0, 220))


class _Painter:
    """按 (姿态, 大小, 位置, 带不带贴纸) 画整幅透明帧,同一种只画一次。"""

    def __init__(self, out: Path, size: tuple[int, int], host_id: str | None,
                 lines: tuple[str, str], accent: tuple[int, int, int]) -> None:
        self.out, self.size, self.lines, self.accent = out, size, lines, accent
        d = sprite_dir(host_id)
        self.sprites = {s: Image.open(d / f"{s}.png").convert("RGBA") for s in STATES}
        for r in REACTIONS:
            if (d / f"{r}.png").is_file():
                self.sprites[r] = Image.open(d / f"{r}.png").convert("RGBA")
        self.sh = sprite_height(size)
        base = self.sprites["closed"]
        self.sw = round(base.width * self.sh / base.height)
        self.x, y = placement(size, self.sh, self.sw)
        self.floor = y + self.sh
        self.files: dict[Any, Path] = {}

    @property
    def reactions(self) -> list[str]:
        return [r for r in REACTIONS if r in self.sprites]

    def file(self, key: tuple[str, float, str, bool]) -> Path:
        if key not in self.files:
            path = self.out / ("hidden.png" if key[0] == HIDDEN else f"f{len(self.files):03d}.png")
            self._paint(*key).save(path)
            self.files[key] = path
        return self.files[key]

    def _paint(self, pose: str, scale: float, anchor: str, sticker: bool) -> Image.Image:
        w, h = self.size
        canvas = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        if pose == HIDDEN:
            return canvas
        src = self.sprites[pose]
        if anchor == "band":
            # 大字钩子开场:数据区被大字占着,从画面带左下角探出半身
            g = aspect.portrait_grid(self.size)
            bh = int(g.band.height * BUST_BAND)
            full = round(bh / BUST_CROP)
            im = src.resize((round(src.width * full / src.height), full), Image.LANCZOS)
            im = im.crop((0, 0, im.width, bh))
            canvas.alpha_composite(im, (self.x, g.band.bottom - bh))
            return canvas
        fh = round(self.sh * scale)
        im = src.resize((round(src.width * fh / src.height), fh), Image.LANCZOS)
        canvas.alpha_composite(im, (self.x, self.floor - fh))
        if sticker:
            left = self.x + im.width + int(w * 0.02)
            if aspect.is_portrait_size(self.size):
                g = aspect.portrait_grid(self.size)
                box = (left, g.data.top, g.right, g.data.bottom)
            else:
                box = (left, self.floor - fh, int(w * 0.62), self.floor)
            _sticker(canvas, box, self.lines, self.accent)
        return canvas


def build(work_dir: Path, nar: dict, size: tuple[int, int], durations: Sequence[float],
          layouts: Sequence[str], fps: int, seed: int = 0,
          host_id: str | None = None, name: str = "", series: str = "",
          accent: tuple[int, int, int] = (255, 90, 31)) -> tuple[Path, int, int] | None:
    """出主持人层:(concat 清单, x, y)。`durations`/`layouts` 只含正片各拍(不含片尾卡)。

    `name` / `series`:收尾贴纸上的称呼和栏目名。帧是整幅的,所以 x = y = 0。"""
    if not config.HOST_ENABLED or not available(host_id):
        return None
    try:
        total = float(sum(durations))
        n = max(1, int(round(total * fps)))
        cues = nar.get("cues") or []
        wav = Path(nar.get("narration_path") or "")
        rms = _rms(wav, fps, n) if wav.is_file() else np.zeros(n)
        states = mouth_track(rms, cues, fps, seed=seed)
        hide = hide_spans(layouts, durations, fps, n)

        out = work_dir / "_host"
        shutil.rmtree(out, ignore_errors=True)
        out.mkdir(parents=True, exist_ok=True)
        painter = _Painter(out, size, host_id, follow_lines(name, series), accent)

        portrait = aspect.is_portrait_size(size)
        open_end = opening_span(cues, layouts, total)
        # 大字钩子开场:竖屏改探半身,横屏大字在画面中间,不放大
        bust = bool(open_end) and layouts[0] == "hook"
        if bust and not portrait:
            open_end, bust = 0.0, False
        end_start = ending_span(cues, total)
        scales = scale_track(n, fps, 0.0 if bust else open_end, end_start)
        react = reaction_track(cues, fps, n, painter.reactions, end_start)
        b0 = int(end_start * fps) if end_start is not None else n

        keys: list[tuple[str, float, str, bool]] = []
        for i in range(n):
            if hide[i]:
                keys.append((HIDDEN, 1.0, "data", False))
            elif bust and i < int(open_end * fps):
                keys.append((states[i], 1.0, "band", False))
            else:
                keys.append((react.get(i, states[i]), scales[i], "data", i >= b0 + TWEEN))
        segs = _segments(keys, fps)
        lines: list[str] = []
        for key, dur in segs:
            lines += [f"file '{painter.file(key).as_posix()}'", f"duration {dur:.4f}"]
        lines.append(f"file '{painter.file(segs[-1][0]).as_posix()}'")
        listing = out / "host.txt"
        listing.write_text("\n".join(lines), encoding="utf-8")
        return listing, 0, 0
    except Exception:  # noqa: BLE001 —— 主持人只是加分项,不能让成片失败
        log.warning("主持人层生成失败,成片不带主持人", exc_info=True)
        return None


def for_compose(project_dir: Path, nar: dict, size: tuple[int, int], beats: Sequence[Any],
                fps: int, seed: int = 0,
                variant: Mapping[str, Any] | None = None) -> tuple[Path, int, int] | None:
    """explainer.compose / recompose 的入口:当前 look 开了 host 才出。

    `variant`:项目变体,收尾贴纸从里面取栏目名。"""
    from . import looks, series, variety

    lk = looks.current()
    if not lk.host:
        return None
    return build(project_dir / "_render", nar, size, [b.duration for b in beats],
                 [str(getattr(b, "layout", "") or "") for b in beats], fps, seed=seed,
                 name=variety._host_name(), series=series.of_variant(variant)[0],
                 accent=tuple(lk.accent))
