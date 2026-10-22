"""数据卡片镜头:参数、对比、时间线、柱状图由代码直接渲染,不交给视频模型画。

视频模型画不出准确的数字和汉字 —— "数据界面"类镜头交给 H3,拿回来的是一屏
乱码或编造的读数。而这类镜头恰恰是解说视频最需要"一个字都不能错"的地方。

分工与提示词编译器一致:
- 分镜 LLM 只负责**挑**:从事实层选出要上屏的几行,并给每行挂 `fact_id`;
- `ground()` 负责**核**:每行的数字必须出现在它引用的那条事实里,否则整行丢弃;
- `render()` 负责**画**:纯 PIL + ffmpeg,确定性、零生成额度、秒级出片。

核完剩下的行不够撑起一张卡片时返回 None,镜头退回普通画面 —— 宁可少一张卡,
也不上一张有错数字的卡。
"""
from __future__ import annotations

import logging
import re
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

from PIL import Image, ImageDraw, ImageFilter, ImageFont

from .. import categories, config
from ..models import Fact
from . import render as render_mod
from .grounding import numbers_in

log = logging.getLogger(__name__)

#: 卡片版式(定义在 categories,Profile 加载期据此校验)。段落用 `card:` 声明自己用哪一种。
CARD_KINDS = categories.CARD_KINDS

#: 每种版式至少要几行才值得上屏,最多放几行(9:16 一屏的可读上限)
_MIN_ROWS = {"spec": 1, "compare": 1, "timeline": 2, "bars": 2}
_MAX_ROWS = {"spec": 5, "compare": 5, "timeline": 5, "bars": 6}

#: 版式说明,拼进分镜提示词,告诉 LLM 每种卡片的 rows 怎么填
KIND_GUIDE = {
    "spec": "参数卡:rows 每行 {label: 参数名, values: [取值含单位], fact_id}",
    "compare": "对比卡:columns 给两个对比对象名;rows 每行 {label: 维度, values: [A 的值, B 的值], fact_id}",
    "timeline": "时间线:rows 每行 {label: 年份, values: [这一年发生了什么,15 字内], fact_id}",
    "bars": "柱状图:rows 每行 {label: 项目名, values: [数值含单位], fact_id},数值必须可比(同单位同口径)",
}

W, H = config.OUTPUT_WIDTH, config.OUTPUT_HEIGHT
FPS = 24


@dataclass(frozen=True)
class Geo:
    """画布与内容区。竖屏避开平台 UI(顶部搜索框、底部字幕与文案区、右侧点赞列);
    横屏内容区居中,底部让出字幕和章节条。"""
    w: int
    h: int
    top: int
    bottom: int
    left: int
    right: int


def geo_for(size: tuple[int, int] | None = None) -> Geo:
    w, h = size or (W, H)
    if not config.frame_is_landscape((w, h)):
        # 竖屏残留过一次(缺省画幅是 1080×1920),这里留一道告警:
        # 画幅不对时数据卡会按竖屏的几何排版,内容被裁在不该裁的地方。
        log.warning("数据卡画幅不是 16:9:%sx%s", w, h)
    if w > h:
        return Geo(w, h, top=int(h * 0.13), bottom=int(h * 0.78),
                   left=int(w * 0.20), right=int(w * 0.80))
    return Geo(w, h, top=int(h * 0.15), bottom=int(h * 0.70),
               left=int(w * 0.07), right=int(w * 0.88))


_GEO = geo_for()
_TOP, _BOTTOM, _LEFT, _RIGHT = _GEO.top, _GEO.bottom, _GEO.left, _GEO.right

_BG_TOP = (15, 17, 21)
_BG_BOTTOM = (28, 34, 48)
_PANEL = (255, 255, 255, 22)
_TEXT = (255, 255, 255)
_MUTED = (176, 184, 196)
_ACCENT = (77, 163, 255)
_ACCENT_B = (255, 181, 71)
_RULE = (255, 255, 255, 40)


# --------------------------------------------------------------------------
# 结构清洗与事实核对
# --------------------------------------------------------------------------
def normalise(raw: Any, kind: str) -> dict[str, Any] | None:
    """把 LLM 给的卡片对象洗成固定形状。形状不对返回 None。"""
    if kind not in CARD_KINDS or not isinstance(raw, dict):
        return None
    rows: list[dict[str, Any]] = []
    for r in raw.get("rows") or []:
        if not isinstance(r, dict):
            continue
        values = r.get("values")
        if isinstance(values, str):
            values = [values]
        values = [str(v).strip() for v in (values or []) if str(v).strip()]
        label = str(r.get("label", "")).strip()
        if not label or not values:
            continue
        rows.append({"label": label, "values": values,
                     "fact_id": str(r.get("fact_id", "")).strip()})
    columns = [str(c).strip() for c in (raw.get("columns") or []) if str(c).strip()]
    return {
        "kind": kind,
        "title": str(raw.get("title", "")).strip()[:16],
        "columns": columns[:2],
        "rows": rows,
    }


_BLANK_VALUES = {"", "无", "暂无", "未知", "未公布", "不详", "na", "none", "null", "tbd"}


def _is_blank_value(text: str) -> bool:
    """"—"、"暂无"、"N/A" 这类占位符:另一边其实没有数据。"""
    return re.sub(r"[\W_]+", "", text).lower() in _BLANK_VALUES


def _same_value(a: str, b: str) -> bool:
    """两边取值是否实质相同(忽略空白与标点,"八败两胜" 和 "八败两胜。" 算同一个)。"""
    def key(t: str) -> str:
        return re.sub(r"[\W_]+", "", t)
    return key(a) == key(b)


def _unbacked_text(value: str, claim: str) -> bool:
    """不含数字的取值("全系标配""往往需选装")也得是事实原文里的说法。

    数字核对管不到纯文字:模型给对比卡对手一栏填"往往需选装",数字检查放行,
    屏幕上就多了一句没有出处的断言。占位符("—""暂无")交给对比卡的空值检查。
    """
    if not value or re.search(r"\d", value) or _is_blank_value(value):
        return False
    def key(t: str) -> str:
        return re.sub(r"[\W_]+", "", t).lower()
    return key(value) not in key(claim)


def _bar_value(text: str) -> float | None:
    m = re.search(r"-?\d+(?:\.\d+)?", text.replace(",", ""))
    return float(m.group(0)) if m else None


def ground(card: dict[str, Any] | None, facts: Sequence[Fact]
           ) -> tuple[dict[str, Any] | None, list[str]]:
    """逐行核对卡片数字。返回 (核对后的卡片或 None, 丢弃原因列表)。

    规则:
    - 每行必须引用一条存在且启用的事实,低置信度事实不上屏;
    - 行内(标签 + 取值)出现的每个数字都必须见于该事实原文;
    - 对比卡每行必须正好两个值,柱状图每行必须解析得出数值;
    - 剩余行数不够版式下限 → 整张卡作废。
    """
    if not card:
        return None, []
    kind = card["kind"]
    by_id = {f.id: f for f in facts if f.enabled and f.confidence != "low"}
    kept: list[dict[str, Any]] = []
    dropped: list[str] = []
    for row in card["rows"]:
        fact = by_id.get(row["fact_id"])
        text = row["label"] + " " + " ".join(row["values"])
        if fact is None:
            dropped.append(f"「{text}」引用的事实 {row['fact_id'] or '(空)'} 不存在或置信度低")
            continue
        missing = numbers_in(text) - numbers_in(fact.claim)
        if missing:
            dropped.append(f"「{text}」中的 {sorted(missing)} 不见于 {fact.id}:{fact.claim}")
            continue
        invented = [v for v in row["values"] if _unbacked_text(v, fact.claim)]
        if invented:
            dropped.append(f"「{text}」中的「{invented[0]}」不见于 {fact.id}:{fact.claim}")
            continue
        if kind == "compare" and len(row["values"]) != 2:
            dropped.append(f"「{text}」对比卡每行需要两个值")
            continue
        if kind == "compare" and any(_is_blank_value(v) for v in row["values"]):
            dropped.append(f"「{text}」对比卡有一边没有数据(占位符),放上屏就是半张空卡")
            continue
        if kind == "compare" and _same_value(*row["values"]):
            dropped.append(f"「{text}」两边取值相同,算不上对比(多半是一条事实被抄了两遍)")
            continue
        if kind == "bars" and _bar_value(row["values"][0]) is None:
            dropped.append(f"「{text}」柱状图取值不是数字")
            continue
        kept.append({**row, "values": row["values"][:2] if kind == "compare" else row["values"][:1],
                     "tag": basis_tag(fact)})

    kept = kept[: _MAX_ROWS[kind]]
    if len(kept) < _MIN_ROWS[kind]:
        return None, dropped + [f"核对后剩 {len(kept)} 行,不足 {_MIN_ROWS[kind]} 行,卡片作废"]
    if kind == "compare" and len(card["columns"]) != 2:
        return None, dropped + ["对比卡缺两个对比对象名,卡片作废"]

    used = [by_id[r["fact_id"]] for r in kept]
    return {**card, "rows": kept, "source": source_line(used)}, dropped


#: 来源类型的上屏叫法
SOURCE_KIND_CN = {"official": "官方", "media": "媒体", "derived": "推算"}


def basis_tag(fact: Fact) -> str:
    """一条事实上屏时跟在数字旁边的小标签:口径 + 来源类型,如 "CLTC · 标准版 | 官方"。"""
    kind = SOURCE_KIND_CN.get(fact.source_kind, "")
    return " | ".join(x for x in (fact.basis, kind) if x)


def source_line(used: Sequence[Fact]) -> str:
    """卡片底部来源行:口径汇总 + 各类来源条数 + 采集月份。

    以前只有一行"公开资料整理",看不出是 CLTC 还是 WLTP、是官方还是媒体 ——
    这些恰恰是懂车的观众判断数字靠不靠谱的依据。
    """
    bases: list[str] = []
    for f in used:
        if f.basis and f.basis not in bases:
            bases.append(f.basis)
    kinds: dict[str, int] = {}
    for f in used:
        name = SOURCE_KIND_CN.get(f.source_kind, "公开资料")
        kinds[name] = kinds.get(name, 0) + 1
    dates = sorted({f.retrieved_at[:7] for f in used if f.retrieved_at})
    parts = ["数据来源:" + " · ".join(f"{k} {n}" for k, n in kinds.items())]
    if bases:
        parts.append("口径:" + " / ".join(bases[:2]) + (" 等" if len(bases) > 2 else ""))
    if dates:
        parts.append(dates[-1])
    return " | ".join(parts)


# --------------------------------------------------------------------------
# 绘制
# --------------------------------------------------------------------------
def _font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    fonts = Path(config.FONTS_DIR)
    names = ("msyhbd.ttc", "msyh.ttc", "simhei.ttf") if bold else ("msyh.ttc", "simhei.ttf")
    for name in names:
        p = fonts / name
        if p.is_file():
            return ImageFont.truetype(str(p), size)
    return ImageFont.load_default()


def _text_w(draw: ImageDraw.ImageDraw, text: str, font) -> int:
    return int(draw.textlength(text, font=font))


def _fit(draw: ImageDraw.ImageDraw, text: str, size: int, max_w: int, bold: bool = False):
    """缩小字号直到一行放得下(最小到原字号的 55%),仍放不下就截断。"""
    for s in range(size, int(size * 0.55) - 1, -2):
        f = _font(s, bold)
        if _text_w(draw, text, f) <= max_w:
            return text, f
    f = _font(int(size * 0.55), bold)
    while text and _text_w(draw, text + "…", f) > max_w:
        text = text[:-1]
    return text + "…", f


def _background(bg: Path | None, g: Geo = _GEO) -> Image.Image:
    if bg and Path(bg).is_file():
        try:
            img = Image.open(bg).convert("RGB")
            scale = max(g.w / img.width, g.h / img.height)
            img = img.resize((int(img.width * scale) + 1, int(img.height * scale) + 1))
            left, top = (img.width - g.w) // 2, (img.height - g.h) // 2
            img = img.crop((left, top, left + g.w, top + g.h)).filter(ImageFilter.GaussianBlur(28))
            return Image.blend(img, Image.new("RGB", (g.w, g.h), _BG_TOP), 0.62)
        except OSError:
            pass
    grad = Image.new("RGB", (1, g.h))
    for y in range(g.h):
        t = y / (g.h - 1)
        grad.putpixel((0, y), tuple(int(a + (b - a) * t) for a, b in zip(_BG_TOP, _BG_BOTTOM)))
    return grad.resize((g.w, g.h))


def ease_out(t: float) -> float:
    """缓出(三次):开头快、结尾慢下来停稳,数字滚动和柱子生长都用它。"""
    t = min(1.0, max(0.0, t))
    return 1 - (1 - t) ** 3


#: 柱子生长分几帧、每帧多久。关键帧 + concat 时长,12 帧就够看着顺
GROW_STEPS = 12
GROW_STEP_SECONDS = 0.06


def _steps(card: dict[str, Any]) -> list[tuple[int, float, bool, float]]:
    """卡片的动画步骤:[(显示几行, 柱子生长进度, 最后一行只亮左列, 这一帧停多久)]。

    - 参数卡 / 时间线:逐行出现;
    - 对比卡:每行先亮左边的值,再亮右边的值,一左一右像在过招;
    - 柱状图:所有行一起从 0 长到各自长度(缓出)。
    第 0 步只有标题。最后一步的时长由 `render` 改成"定格到结束"。
    """
    n = len(card["rows"])
    steps: list[tuple[int, float, bool, float]] = [(0, 1.0, False, 0.4)]
    if card["kind"] == "bars":
        steps += [(n, ease_out(k / GROW_STEPS), False, GROW_STEP_SECONDS)
                  for k in range(1, GROW_STEPS + 1)]
    elif card["kind"] == "compare":
        for r in range(1, n + 1):
            steps += [(r, 1.0, True, 0.3), (r, 1.0, False, 0.3)]
    else:
        steps += [(r, 1.0, False, 0.45) for r in range(1, n + 1)]
    return steps


def _frame(card: dict[str, Any], subject: str, visible_rows: int, base: Image.Image,
           g: Geo = _GEO, progress: float = 1.0, half: bool = False) -> Image.Image:
    img = base.copy().convert("RGBA")
    overlay = Image.new("RGBA", (g.w, g.h), (0, 0, 0, 0))
    d = ImageDraw.Draw(overlay)
    d.rounded_rectangle((g.left - 30, g.top - 40, g.right + 30, g.bottom + 30), radius=36, fill=_PANEL)

    inner_w = g.right - g.left
    y = g.top
    tag, tag_f = _fit(d, subject, 36, inner_w)
    d.text((g.left, y), tag, font=tag_f, fill=_ACCENT)
    y += 58
    title, title_f = _fit(d, card.get("title") or "关键数据", 66, inner_w, bold=True)
    d.text((g.left, y), title, font=title_f, fill=_TEXT)
    y += 110

    rows = card["rows"][:visible_rows]
    kind = card["kind"]
    area = g.bottom - 60 - y
    if kind == "compare":
        y = _draw_compare(d, card, rows, y, area, g, half=half)
    elif kind == "timeline":
        _draw_timeline(d, card, rows, y, area, g)
    elif kind == "bars":
        _draw_bars(d, card, rows, y, area, g, progress=progress)
    else:
        _draw_spec(d, card, rows, y, area, g)

    src, src_f = _fit(d, card.get("source", ""), 26, inner_w)
    d.text((g.left, g.bottom - 20), src, font=src_f, fill=_MUTED)
    return Image.alpha_composite(img, overlay).convert("RGB")


def _row_h(card: dict[str, Any], area: int, extra: int = 0) -> int:
    return min(190, area // max(1, len(card["rows"]) + extra))


def _draw_spec(d, card, rows, y, area, g: Geo = _GEO) -> None:
    rh = _row_h(card, area)
    half = (g.right - g.left) // 2
    for r in rows:
        label, lf = _fit(d, r["label"], 40, half - 20)
        d.text((g.left, y + rh // 2), label, font=lf, fill=_MUTED, anchor="lm")
        value, vf = _fit(d, r["values"][0], 64, half + 20, bold=True)
        d.text((g.right, y + rh // 2), value, font=vf, fill=_TEXT, anchor="rm")
        d.line((g.left, y + rh, g.right, y + rh), fill=_RULE, width=2)
        y += rh


def _draw_compare(d, card, rows, y, area, g: Geo = _GEO, half: bool = False) -> int:
    """`half`:最后一行只画左列的值(右列下一帧再亮)。"""
    col_w = (g.right - g.left) // 2
    a, b = card["columns"]
    a, af = _fit(d, a, 42, col_w - 30, bold=True)
    b, bf = _fit(d, b, 42, col_w - 30, bold=True)
    d.text((g.left + col_w // 2, y + 30), a, font=af, fill=_ACCENT, anchor="mm")
    d.text((g.right - col_w // 2, y + 30), b, font=bf, fill=_ACCENT_B, anchor="mm")
    y += 80
    rh = _row_h(card, area - 80)
    for n, r in enumerate(rows):
        label, lf = _fit(d, r["label"], 32, g.right - g.left)
        d.text(((g.left + g.right) // 2, y + 26), label, font=lf, fill=_MUTED, anchor="mm")
        va, vaf = _fit(d, r["values"][0], 54, col_w - 30, bold=True)
        vb, vbf = _fit(d, r["values"][1], 54, col_w - 30, bold=True)
        d.text((g.left + col_w // 2, y + rh * 0.62), va, font=vaf, fill=_TEXT, anchor="mm")
        if not (half and n == len(rows) - 1):
            d.text((g.right - col_w // 2, y + rh * 0.62), vb, font=vbf, fill=_TEXT, anchor="mm")
        d.line((g.left, y + rh, g.right, y + rh), fill=_RULE, width=2)
        y += rh
    return y


def _draw_timeline(d, card, rows, y, area, g: Geo = _GEO) -> None:
    rh = _row_h(card, area)
    x_line = g.left + 20
    if rows:
        d.line((x_line, y + rh // 2, x_line, y + rh * (len(rows) - 1) + rh // 2), fill=_RULE, width=4)
    for r in rows:
        cy = y + rh // 2
        d.ellipse((x_line - 14, cy - 14, x_line + 14, cy + 14), fill=_ACCENT)
        year, yf = _fit(d, r["label"], 46, 220, bold=True)
        d.text((x_line + 44, cy), year, font=yf, fill=_ACCENT, anchor="lm")
        ev, ef = _fit(d, r["values"][0], 40, g.right - x_line - 290)
        d.text((x_line + 270, cy), ev, font=ef, fill=_TEXT, anchor="lm")
        y += rh


def _draw_bars(d, card, rows, y, area, g: Geo = _GEO, progress: float = 1.0) -> None:
    """`progress`(0–1):柱子长到最终长度的几成。数值文字跟着柱头走,长满前不显示,
    免得观众先看到一个没对上柱长的数。"""
    rh = _row_h(card, area)
    peak = max((_bar_value(r["values"][0]) or 0 for r in card["rows"]), default=0) or 1
    bar_w = g.right - g.left
    for i, r in enumerate(rows):
        label, lf = _fit(d, r["label"], 36, bar_w)
        d.text((g.left, y + 8), label, font=lf, fill=_MUTED)
        v = max(0.0, _bar_value(r["values"][0]) or 0)
        top, bottom = y + 60, y + min(rh - 20, 60 + 54)
        length = max(12, int((bar_w - 220) * v / peak * progress))
        d.rounded_rectangle((g.left, top, g.left + length, bottom), radius=10,
                            fill=_ACCENT if i == 0 else (*_ACCENT, 150))
        if progress >= 0.999:
            value, vf = _fit(d, r["values"][0], 40, 210, bold=True)
            d.text((g.left + length + 16, (top + bottom) // 2), value, font=vf, fill=_TEXT,
                   anchor="lm")
        y += rh


def render_frames(card: dict[str, Any], subject: str, out_dir: Path,
                  background: Path | None = None,
                  size: tuple[int, int] | None = None) -> list[Path]:
    """动画关键帧,与 `_steps(card)` 一一对应:第 0 帧只有标题,之后按版式出行 / 生长。"""
    out_dir.mkdir(parents=True, exist_ok=True)
    g = geo_for(size)
    base = _background(background, g)
    frames = []
    for n, (rows, progress, half, _) in enumerate(_steps(card)):
        p = out_dir / f"step_{n:02d}.png"
        _frame(card, subject, rows, base, g, progress=progress, half=half).save(p)
        frames.append(p)
    return frames


def render(card: dict[str, Any], subject: str, dest: str | Path, *,
           seconds: float | None = None, background: Path | None = None,
           size: tuple[int, int] | None = None) -> Path:
    """渲染成定长镜头(默认 9:16,无音轨,合成层会统一补静音轨)。失败抛 RuntimeError。"""
    seconds = float(seconds or config.SHOT_GRID_SECONDS)
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="card_") as tmp:
        frames = render_frames(card, subject, Path(tmp), background, size)
        # 节奏见 `_steps`:动画走完后最后一帧定格到结束
        # 拍很短时动画按比例压快,保证至少留三成时间看完整的卡
        durations = [s[3] for s in _steps(card)][:-1]
        budget = seconds * 0.7
        if sum(durations) > budget > 0:
            durations = [d * budget / sum(durations) for d in durations]
        durations.append(max(1.0, seconds - sum(durations)))
        lines = []
        for f, dur in zip(frames, durations):
            lines += [f"file '{f.as_posix()}'", f"duration {dur:.3f}"]
        lines.append(f"file '{frames[-1].as_posix()}'")  # concat 要求末帧重复一次
        listing = Path(tmp) / "list.txt"
        listing.write_text("\n".join(lines), encoding="utf-8")
        proc = subprocess.run(
            [render_mod._ffmpeg(), "-y", "-v", "error",
             "-f", "concat", "-safe", "0", "-i", str(listing),
             "-vf", f"fps={FPS},format=yuv420p",
             "-t", f"{seconds:.3f}", "-an",
             "-c:v", "libx264", "-preset", "medium", "-crf", "18",
             "-movflags", "+faststart", str(dest)],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=300,
        )
    if proc.returncode != 0 or not dest.is_file() or dest.stat().st_size == 0:
        raise RuntimeError(f"卡片渲染失败:{(proc.stderr or '')[-300:]}")
    return dest
