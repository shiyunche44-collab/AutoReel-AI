"""时间线(EDL):把分镜 + 旁白字级时间 + 素材,编成一份 Remotion 能直接合成的 JSON。

这里全是纯计算(取材通过注入的 `pick` 回调),配单测。时间一律是**全片绝对秒**。

剪辑规则(对标剪映「图文成片」/ 抖音信息类竖屏):

1. **切点跟着话走**:实拍拍在分句(逗号、句号)处下刀,刀口落在下一分句第一个字念出的
   时刻(用 `char_t`;没量过按字数比例)。每刀 `MIN_SHOT`–`MAX_SHOT` 秒,太长的均分。
2. **每刀按它那半句取材**:`pick` 拿到的是这一刀时间内念到的原文,取材时用它做细节匹配
   (`library.fill(detail=...)`),不再整拍共用一组背景。
3. **开头 `HOOK_SECONDS` 秒是钩子**:≤`HOOK_SHOT` 秒一刀的快切 + 大标题,首刀白闪。
4. **转场有语义**:同拍内硬切;换章 whip;进出整屏卡片 / 概念动画 zoom。
5. **画面一直在动**:视频交替推 / 拉,静图做平移,不出现静止的整屏。
"""
from __future__ import annotations

import math
import os
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from typing import Any

#: 实拍子镜头的时长范围(秒)。竖屏信息类对标中位约 2 秒一刀
MIN_SHOT = 1.2
MAX_SHOT = 2.8
#: 钩子段:开头多少秒、每刀最长多少秒
HOOK_SECONDS = 3.0
HOOK_SHOT = 1.0
#: 章节标牌停留多久
CHAPTER_CHIP_SECONDS = 1.4
#: 这些版式用逐拍渲染好的成品整段放(代码画的卡片 / 动画,不需要再取材)
PRERENDERED = frozenset(("card", "concept", "mech"))
#: 分句断点:在这些标点之后下刀
CLAUSE_BREAKS = "，。！？；：、,.!?;:"
MOTIONS_VIDEO = ("push", "pull")
MOTIONS_STILL = ("pan_l", "pan_r", "push")

Pick = Callable[[Any, float, str, str], list[Mapping[str, Any]]]


def enabled(portrait: bool) -> bool:
    """`EDIT_V2=1` 开启;试点只开竖屏,横屏要显式 `EDIT_V2=all`。"""
    v = os.getenv("EDIT_V2", "0").strip().lower()
    if v in ("all", "both"):
        return True
    return portrait and v in ("1", "true", "on", "portrait")


# --------------------------------------------------------------------------
# 数据结构
# --------------------------------------------------------------------------
@dataclass
class Clip:
    """视频轨上的一刀。`src` 是本地文件路径(合成前由 render 换成 public 里的文件名)。"""
    start: float
    dur: float
    kind: str  # video / still / beat(逐拍成品,整屏放)
    src: str
    src_in: float = 0.0
    motion: str = "none"
    transition: str = "cut"  # cut / flash / whip / zoom
    crop_edge: float = 0.0
    crop_bottom: float = 0.0
    beat: int = -1
    text: str = ""
    item_id: str = ""


@dataclass
class Overlay:
    start: float
    dur: float
    type: str  # hook / keyword / stat / panel / chapter
    props: dict[str, Any] = field(default_factory=dict)


@dataclass
class Timeline:
    width: int
    height: int
    fps: int
    duration: float
    title: str = ""
    accent: str = "#FF5A1F"
    clips: list[Clip] = field(default_factory=list)
    overlays: list[Overlay] = field(default_factory=list)
    captions: list[dict[str, Any]] = field(default_factory=list)
    chapters: list[dict[str, Any]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def to_props(self) -> dict[str, Any]:
        d = asdict(self)
        d.pop("notes", None)
        return d

    def cut_times(self) -> list[float]:
        return [c.start for c in self.clips[1:]]

    def transitions_at(self, kinds: Sequence[str]) -> list[float]:
        return [c.start for c in self.clips if c.transition in kinds and c.start > 0]


# --------------------------------------------------------------------------
# 字级时间
# --------------------------------------------------------------------------
def _get(obj: Any, key: str, default: Any = None) -> Any:
    if isinstance(obj, Mapping):
        return obj.get(key, default)
    return getattr(obj, key, default)


def char_time(cue: Any, i: int) -> float:
    """cue 原文第 i 个字念出的全片绝对秒。没量过(`char_t` 缺失或长度不符)按字数比例。"""
    start, end = float(_get(cue, "start")), float(_get(cue, "end"))
    text = str(_get(cue, "text") or "")
    ct = _get(cue, "char_t")
    if ct and len(ct) == len(text) and 0 <= i < len(text):
        return start + float(ct[i])
    n = max(1, len(text))
    return start + (end - start) * max(0, min(i, n)) / n


def clause_starts(cue: Any) -> list[float]:
    """一句里每个分句开口的时刻(含句首)。"""
    text = str(_get(cue, "text") or "")
    out = [float(_get(cue, "start"))]
    for i, ch in enumerate(text[:-1]):
        if ch in CLAUSE_BREAKS:
            # 小数点、千分位不是断点
            if ch in ".,:" and i > 0 and text[i - 1].isdigit() and text[i + 1].isdigit():
                continue
            out.append(char_time(cue, i + 1))
    return out


#: 切点做过 3 位小数取整:比较时留这点余量,免得刀口那个字被算进上一刀
_EPS = 0.005


def spoken_between(cues: Sequence[Any], a: float, b: float) -> str:
    """[a, b) 之间念到的原文。"""
    parts: list[str] = []
    for c in cues:
        s, e = float(_get(c, "start")), float(_get(c, "end"))
        if e <= a or s >= b:
            continue
        text = str(_get(c, "text") or "")
        chars = [ch for i, ch in enumerate(text) if a - _EPS <= char_time(c, i) < b - _EPS]
        parts.append("".join(chars))
    return "".join(parts).strip(CLAUSE_BREAKS + " ")


# --------------------------------------------------------------------------
# 下刀
# --------------------------------------------------------------------------
def plan_cuts(start: float, end: float, candidates: Sequence[float], *,
              min_shot: float = MIN_SHOT, max_shot: float = MAX_SHOT) -> list[tuple[float, float]]:
    """在 [start, end) 里挑切点:优先落在 `candidates`(分句开口),每刀 [min, max] 秒。

    贪心:离上一刀满 `min_shot` 的第一个候选就下刀;两候选之间超过 `max_shot` 的段均分。
    最后一刀不足 `min_shot` 时并进前一刀。
    """
    if end - start <= 0:
        return []
    cands = sorted(t for t in candidates if start + min_shot <= t <= end - min_shot + 1e-6)
    cuts = [start]
    for t in cands:
        if t - cuts[-1] >= min_shot:
            cuts.append(t)
    cuts.append(end)
    out: list[tuple[float, float]] = []
    for a, b in zip(cuts, cuts[1:], strict=False):
        n = max(1, math.ceil((b - a) / max_shot - 1e-9))
        step = (b - a) / n
        out += [(a + k * step, a + (k + 1) * step) for k in range(n)]
    if len(out) > 1 and out[-1][1] - out[-1][0] < min_shot - 1e-6:
        last = out.pop()
        out[-1] = (out[-1][0], last[1])
    return [(round(a, 3), round(b, 3)) for a, b in out]


# --------------------------------------------------------------------------
# 叠加层
# --------------------------------------------------------------------------
def _rows(ov: Mapping[str, Any]) -> list[dict[str, Any]]:
    rows = []
    for r in ov.get("rows") or []:
        if not isinstance(r, Mapping):
            continue
        vals = r.get("values") or [r.get("value", "")]
        rows.append({"label": str(r.get("label") or ""), "value": str(vals[0] if vals else ""),
                     "basis": str(r.get("basis") or ""),
                     "at": float(r["at"]) if isinstance(r.get("at"), (int, float)) else None})
    return rows


def beat_overlays(spec: Any, start: float, dur: float) -> list[Overlay]:
    """一拍的叠加层(数据都在分镜阶段核对过事实,这里只换形状)。"""
    ov = dict(spec.overlay or {})
    layout = spec.layout
    if layout == "highlight" and ov.get("lines"):
        return [Overlay(start + 0.08, max(0.3, dur - 0.08), "keyword",
                        {"lines": [str(x) for x in ov["lines"]][:2]})]
    if layout == "stat" and ov.get("value"):
        at = float(ov["at"]) if isinstance(ov.get("at"), (int, float)) else 0.2
        return [Overlay(start, dur, "stat", {"label": str(ov.get("label") or ""),
                                             "value": str(ov["value"]),
                                             "note": str(ov.get("note") or ""),
                                             "at": round(min(at, max(0.0, dur - 0.6)), 3)})]
    if layout == "panel" and ov.get("rows"):
        return [Overlay(start, dur, "panel", {"title": str(ov.get("title") or ""),
                                              "rows": _rows(ov)[:5],
                                              "source": str(ov.get("source") or "")})]
    return []


# --------------------------------------------------------------------------
# 组装
# --------------------------------------------------------------------------
def _chapters(specs: Sequence[Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    t = 0.0
    for s in specs:
        name = s.chapter or s.section or "正片"
        if out and out[-1]["name"] == name:
            out[-1]["end"] = round(t + s.duration, 3)
        else:
            out.append({"name": name, "start": round(t, 3), "end": round(t + s.duration, 3)})
        t += s.duration
    return out


def _role(chapter_idx: int, n_chapters: int) -> str:
    if chapter_idx == 0:
        return "hook"
    return "ending" if chapter_idx == n_chapters - 1 else "body"


def _hook_lines(spec: Any, title: str) -> list[str]:
    ov = spec.overlay or {}
    lines = [str(x) for x in (ov.get("lines") or []) if str(x).strip()]
    if lines:
        return lines[:2]
    return [title] if title else []


def build_timeline(specs: Sequence[Any], cues: Sequence[Any], *, size: tuple[int, int],
                   fps: int, pick: Pick, beat_files: Mapping[int, str], title: str = "",
                   accent: str = "#FF5A1F", captions: Sequence[Mapping[str, Any]] = (),
                   outro: tuple[str, float] | None = None) -> Timeline:
    """分镜(已按 TTS 定好时长) + 旁白 cue → 时间线。

    `pick(spec, seconds, text, role)` 返回取材片段([{kind, path, start, length, ...}]),
    一刀可能拿到几段(池子里没有够长的),按顺序铺满这一刀。
    `beat_files`:拍号 → 逐拍渲染的成品(整屏卡片 / 概念动画 / 机构画面,以及取不到素材时兜底)。
    """
    beats = [s for s in specs if s.duration > 0]
    chapters = _chapters(beats)
    total = round(sum(s.duration for s in beats), 3)
    tl = Timeline(width=size[0], height=size[1], fps=fps, duration=total, title=title,
                  accent=accent, captions=list(captions), chapters=chapters)
    by_beat: dict[int, list[Any]] = {}
    for c in cues:
        by_beat.setdefault(int(_get(c, "shot_idx", -1)), []).append(c)

    t = 0.0
    n_clip = 0
    ch_idx = -1
    for s in beats:
        start, end = round(t, 3), round(t + s.duration, 3)
        t += s.duration
        name = s.chapter or s.section or "正片"
        new_chapter = ch_idx < 0 or chapters[ch_idx]["name"] != name
        if new_chapter:
            ch_idx += 1
            if ch_idx > 0:
                tl.overlays.append(Overlay(start, min(CHAPTER_CHIP_SECONDS, end - start), "chapter",
                                           {"n": ch_idx, "name": name}))
        role = _role(ch_idx, len(chapters))
        prev_beat = tl.clips[-1].kind == "beat" if tl.clips else False

        if s.layout in PRERENDERED and beat_files.get(s.idx):
            tl.clips.append(Clip(start, round(end - start, 3), "beat", beat_files[s.idx],
                                 transition="zoom" if tl.clips else "cut", beat=s.idx))
            n_clip += 1
            continue

        mine = by_beat.get(s.idx, [])
        cands = [x for c in mine for x in clause_starts(c)]
        hook_end = min(end, HOOK_SECONDS) if start < HOOK_SECONDS else start
        if 0 < end - hook_end < MIN_SHOT:
            # 钩子之后只剩一小截:并进钩子,别切出一闪而过的碎刀
            hook_end = end
        spans: list[tuple[float, float]] = []
        if hook_end > start:
            spans += plan_cuts(start, hook_end, [], min_shot=min(MIN_SHOT, HOOK_SHOT * 0.6),
                               max_shot=HOOK_SHOT)
        if end > hook_end:
            spans += plan_cuts(hook_end, end, cands)
        for k, (a, b) in enumerate(spans):
            text = spoken_between(mine, a, b) or (s.narration_zh or "")
            got = [p for p in pick(s, b - a, text, "hook" if a < HOOK_SECONDS else role)
                   if p.get("path")]
            if not got:
                if beat_files.get(s.idx):
                    # 取不到素材:退回逐拍成品对应的那一段
                    got = [{"kind": "beat_slice", "path": beat_files[s.idx], "start": a - start,
                            "length": b - a}]
                else:
                    tl.notes.append(f"第 {s.idx} 拍 {a:.1f}s 起没有素材")
                    continue
            cursor = a
            for j, p in enumerate(got):
                left = b - cursor
                if left <= 0.05:
                    break
                length = float(p.get("length") or left)
                d = left if j == len(got) - 1 else min(left, length)
                kind = str(p.get("kind") or "video")
                if k == 0 and j == 0:
                    if not tl.clips:
                        trans = "flash"
                    elif new_chapter:
                        trans = "whip"
                    elif prev_beat:
                        trans = "zoom"
                    else:
                        trans = "cut"
                else:
                    trans = "cut"
                if kind == "beat_slice":
                    clip = Clip(round(cursor, 3), round(d, 3), "video", str(p["path"]),
                                src_in=round(float(p.get("start") or 0.0), 3), motion="none",
                                transition=trans, beat=s.idx, text=text)
                else:
                    motions = MOTIONS_STILL if kind == "still" else MOTIONS_VIDEO
                    clip = Clip(round(cursor, 3), round(d, 3), "still" if kind == "still" else "video",
                                str(p["path"]), src_in=round(float(p.get("start") or 0.0), 3),
                                motion=motions[n_clip % len(motions)], transition=trans,
                                crop_edge=float(p.get("crop_edge") or 0.0),
                                crop_bottom=float(p.get("crop_bottom") or 0.0),
                                beat=s.idx, text=text, item_id=str(p.get("item_id") or ""))
                tl.clips.append(clip)
                n_clip += 1
                cursor += d
        if start < HOOK_SECONDS and (s.layout == "hook" or s is beats[0]):
            lines = _hook_lines(s, title)
            if lines:
                tl.overlays.append(Overlay(start, round(min(end, HOOK_SECONDS + 0.5) - start, 3),
                                           "hook", {"lines": lines}))
        tl.overlays.extend(beat_overlays(s, start, end - start))

    if outro:
        path, secs = outro
        tl.clips.append(Clip(total, round(float(secs), 3), "beat", str(path), transition="zoom"))
        tl.duration = round(total + float(secs), 3)
    return tl
