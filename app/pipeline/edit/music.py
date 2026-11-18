"""配乐编辑:按情绪选曲、让起势点(drop)落在钩子结束处、在小节线上收尾、片子比曲子长时在章节交界换曲。

输入都是基本类型(秒数、列表、曲目 dict),不依赖时间线类:

    tracks = music.tracks_in(BGM_DIR / "auto_long")          # 读 _licenses.json(含 beat_track 的结果)
    bgm = music.plan(tl.duration, hook_end=3.0, chapter_cuts=[...], tracks=tracks, mood="科技",
                     seed=project_id)
    cut = music.snap(cut, bgm["beats"])                         # 换章切点吸到最近的拍上(±0.15 秒)
    bed = music.render_bed(bgm, project_dir / "_render" / "bgm_bed.wav")

`render_bed` 出的 wav 长度正好等于成片,首尾淡变 / 交叉都已做好,旁边写一份 `.plan.json`;
`render._mix_audio` 见到它就不再循环、不再淡变,只按实测响度把它压到旁白下面。

节拍信息缺失(曲子没跑过 `python -m app.pipeline.beat_track`)时退回简单淡入淡出,
记 `degrade.note("bgm.no_beats", ...)`。
"""
from __future__ import annotations

import bisect
import hashlib
import json
import logging
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from ... import degrade

_log = logging.getLogger(__name__)

#: 片尾:最后一个小节线起淡出这么久
FADE_OUT = 2.5
#: 从曲子中段起播时的短淡入(起点不一定在拍上,淡一下盖住)
FADE_IN = 0.5
#: 没有节拍信息时的淡入
SIMPLE_FADE_IN = 1.5
#: 两首交叉的总长,以换章切点为中心
XFADE = 2.0
#: 曲子最后这几秒多是收尾 / 渐弱,不拿来铺正片
TAIL_GUARD = 6.0
#: 换曲前这一首至少放这么久
MIN_SEGMENT = 20.0
#: drop 比钩子结束还早时,音乐最多晚进这么久(再晚开场就太空了,宁可从 drop 之后起播)
MAX_ENTRY_DELAY = 1.0
#: 律动清晰度(beat_track 的 pulse)低于这个的曲子,拍点只作参考,选曲时排在后面
PULSE_MIN = 0.3
#: 只认抬升不低于最强起势点这个比例的 drop(弱的多半是配器变化,不是"起")
DROP_MIN_SHARE = 0.5
#: 底床里各段先对齐到这个响度,两首交叉时不忽大忽小;混音时再整体压到旁白下面
BED_LUFS = -20.0
SNAP_TOL = 0.15
PLAN_VERSION = 1
LICENSES = "_licenses.json"
AUDIO_EXTS = (".mp3", ".m4a", ".wav", ".aac", ".ogg", ".flac")


# --------------------------------------------------------------------------
# 曲库
# --------------------------------------------------------------------------
def tracks_in(folder: Path) -> list[dict[str, Any]]:
    """读一个曲库目录:`_licenses.json` 里登记且文件还在的曲子,附上 `path`。按文件名排序。"""
    try:
        ledger = json.loads((Path(folder) / LICENSES).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        ledger = {}
    out = []
    for name, meta in sorted(ledger.items()):
        f = Path(folder) / name
        if isinstance(meta, dict) and f.suffix.lower() in AUDIO_EXTS and f.is_file():
            out.append({**meta, "path": str(f), "name": name})
    return out


def library(root: Path) -> list[dict[str, Any]]:
    """一个品类的整个曲库:`root` 和各子目录(视觉包专用曲库、`pool/` 情绪曲库)里登记过的曲子。
    同一首(同 `source`)在几个目录都有时只留一份,优先留打过节拍的。"""
    seen: dict[str, dict[str, Any]] = {}
    root = Path(root)
    folders = [root, *sorted(p.parent for p in root.glob(f"*/**/{LICENSES}"))]
    for folder in folders:
        for t in tracks_in(folder):
            key = str(t.get("source") or t["name"])
            if key not in seen or (t.get("beats") and not seen[key].get("beats")):
                seen[key] = t
    return sorted(seen.values(), key=lambda t: t["name"])


def _floats(v: Any) -> list[float]:
    return sorted(float(x) for x in v) if isinstance(v, (list, tuple)) else []


def _norm(t: Mapping[str, Any]) -> dict[str, Any]:
    length = float(t.get("duration") or t.get("seconds") or 0.0)
    beats, downs = _floats(t.get("beats")), _floats(t.get("downbeats"))
    drops = [float(x) for x in t.get("drops") or []]
    strength = [float(x) for x in t.get("drop_db") or []]
    if len(strength) != len(drops):
        strength = [1.0] * len(drops)
    bpm = float(t.get("bpm") or 0.0)
    return {
        "path": str(t.get("path") or ""), "title": str(t.get("title") or t.get("name") or ""),
        "mood": t.get("mood"), "length": length, "bpm": bpm,
        "pulse": float(t.get("pulse") if t.get("pulse") is not None else 1.0),
        "beats": beats, "downbeats": downs, "drops": list(zip(drops, strength, strict=True)),
        "lufs": t.get("lufs"), "has_beats": bool(beats and downs and bpm > 0),
    }


def _rotate(items: list[dict[str, Any]], seed: str) -> list[dict[str, Any]]:
    if not items or not seed:
        return items
    k = int(hashlib.sha1(seed.encode("utf-8")).hexdigest(), 16) % len(items)
    return items[k:] + items[:k]


def _pool(tracks: Sequence[Mapping[str, Any]], mood: str | None, seed: str) -> list[dict[str, Any]]:
    """可选曲目,按"情绪对得上 > 律动清楚 > 有节拍 > 没节拍"排,同档内按 seed 轮换。"""
    all_ = [_norm(t) for t in tracks]
    all_ = [t for t in all_ if t["path"] and t["length"] > 0]
    if mood and any(t["mood"] == mood for t in all_):
        all_ = [t for t in all_ if t["mood"] == mood]
    strong = [t for t in all_ if t["has_beats"] and t["pulse"] >= PULSE_MIN]
    weak = [t for t in all_ if t["has_beats"] and t["pulse"] < PULSE_MIN]
    none = [t for t in all_ if not t["has_beats"]]
    return _rotate(strong, seed) + _rotate(weak, seed) + _rotate(none, seed)


# --------------------------------------------------------------------------
# 对拍
# --------------------------------------------------------------------------
def snap(t: float, beats: Sequence[float], tol: float = SNAP_TOL) -> float:
    """把时刻 `t` 吸到最近的拍点上;最近的拍超出 `tol` 秒就原样返回。`beats` 须升序。"""
    if not beats:
        return float(t)
    i = bisect.bisect_left(beats, t)
    best = min((beats[j] for j in (i - 1, i) if 0 <= j < len(beats)), key=lambda b: abs(b - t))
    return float(best) if abs(best - t) <= tol else float(t)


def snap_all(times: Sequence[float], beats: Sequence[float], tol: float = SNAP_TOL) -> list[float]:
    return [snap(t, beats, tol) for t in times]


def _pick_drop(track: Mapping[str, Any], lead: float) -> float | None:
    """选一个 drop:够强、并且它前面留得出 `lead - MAX_ENTRY_DELAY` 秒的前奏;取最早的那个。"""
    drops = track["drops"]
    if not drops:
        return None
    top = max(s for _, s in drops)
    ok = [d for d, s in drops if s >= DROP_MIN_SHARE * top and d >= lead - MAX_ENTRY_DELAY]
    return min(ok) if ok else None


def _place(track: Mapping[str, Any], start: float, align: float, first: bool) -> dict[str, Any]:
    """把曲子摆进时间线:从 `start` 起出声,并让它的 drop(没有就用强拍)正好落在 `align`。"""
    lead = max(0.0, align - start)
    target = None
    if track["has_beats"]:
        target = _pick_drop(track, lead)
        if target is None:
            downs = [d for d in track["downbeats"] if d >= lead - MAX_ENTRY_DELAY]
            target = downs[0] if downs else None
    if target is None:
        # 没有节拍信息(或者找不到能对的点):从头放,长淡入
        return {"start": start, "offset": 0.0, "drop_at": None,
                "fade_in": SIMPLE_FADE_IN if first else XFADE}
    offset = target - lead
    entry = start
    if offset < 0:  # drop 太靠前:晚一点进,让它照样落在 align
        entry, offset = start - offset, 0.0
    fade = (FADE_IN if offset > 0 else 0.05) if first else max(0.05, align + XFADE / 2 - entry)
    return {"start": round(entry, 3), "offset": round(offset, 3),
            "drop_at": round(align, 3) if target in [d for d, _ in track["drops"]] else None,
            "fade_in": round(min(fade, XFADE), 3)}


def _to_timeline(times: Sequence[float], seg: Mapping[str, Any], lo: float, hi: float) -> list[float]:
    shift = seg["start"] - seg["offset"]
    return [round(b + shift, 3) for b in times if lo <= b + shift <= hi]


def _span(track: Mapping[str, Any], place: Mapping[str, Any]) -> tuple[float, float, float, float]:
    """曲子按 `place` 摆好后:(时间线偏移, 放完的时刻, 可换曲区间 lo, hi)。"""
    shift = place["start"] - place["offset"]
    raw_end = track["length"] + shift
    usable = raw_end - (TAIL_GUARD if raw_end - TAIL_GUARD > place["start"] + XFADE * 2 else 0.0)
    hi = usable - XFADE / 2
    lo = min(place["start"] + MIN_SEGMENT, max(place["start"] + XFADE, (place["start"] + hi) / 2))
    return shift, raw_end, lo, max(lo, hi)


def _switch_point(cuts: Sequence[float], downs: Sequence[float], lo: float, hi: float) -> float:
    """换曲点:区间内最晚的章节交界;没有就取最晚的强拍;再没有就取区间右端。"""
    inside = [c for c in cuts if lo <= c <= hi]
    if inside:
        return inside[-1]
    bars = [d for d in downs if lo <= d <= hi]
    return bars[-1] if bars else hi


def plan(duration: float, hook_end: float, chapter_cuts: Sequence[float],
         tracks: Sequence[Mapping[str, Any]], mood: str | None = None, *,
         seed: str = "") -> dict[str, Any]:
    """给一支 `duration` 秒的片子排配乐,返回 bgm_plan(可 JSON 化):

    - `segments`:[{path, title, mood, bpm, start, end, offset, fade_in, fade_out, drop_at}],
      `start`/`end` 是时间线秒(出声到静音),`offset` 是 `start` 那一刻对应的曲内秒数;
    - `beats` / `downbeats`:映射到时间线上的拍点 / 小节线(给换章切点吸附用);
    - `switches`:换曲的时间线时刻(章节交界);`fade_out_start`:片尾淡出起点(小节线);
    - `mode`:"beats"(对过拍)、"simple"(有曲子没节拍信息,简单淡入淡出)、"none"(没曲子)。

    `seed`(比如项目 id)决定同档曲子里先用哪首,不同项目轮着用。`mood` 对不上任何曲子时不过滤。
    """
    duration = max(0.0, float(duration))
    hook_end = min(max(0.0, float(hook_end)), duration)
    out: dict[str, Any] = {"version": PLAN_VERSION, "duration": round(duration, 3), "mood": mood,
                           "hook_end": round(hook_end, 3), "mode": "none", "segments": [],
                           "beats": [], "downbeats": [], "switches": [], "drop_at": None,
                           "fade_out_start": None}
    pool = _pool(tracks, mood, seed)
    if duration <= 0 or not pool:
        return out
    cuts = sorted(float(c) for c in chapter_cuts if 0 < float(c) < duration)
    segs: list[dict[str, Any]] = []
    beats: list[float] = []
    downs: list[float] = []
    start, align, own_from = 0.0, hook_end, 0.0
    simple: list[str] = []
    used: list[int] = []
    for _ in range(12):  # 最多 12 段,防呆
        # 没用过的先用(都用过了才重复,且不紧接着重复上一首);同档里优先挑撑得到
        # 下一个章节交界(或片尾)的,这样换曲尽量落在换章处而不是章节中间
        cands = [i for i in range(len(pool)) if i not in used] \
            or [i for i in range(len(pool)) if i != used[-1]] or [0]
        tier = pool[cands[0]]["has_beats"]
        options = []
        for i in cands:
            place = _place(pool[i], start, align, first=not segs)
            _, end_i, lo_i, hi_i = _span(pool[i], place)
            fits = end_i >= duration or any(lo_i <= c <= hi_i for c in cuts)
            options.append((i, place, fits and pool[i]["has_beats"] == tier))
        i, place, _ = next((o for o in options if o[2]), options[0])
        tr = pool[i]
        seg = {"path": tr["path"], "title": tr["title"], "mood": tr["mood"], "bpm": tr["bpm"],
               "lufs": tr["lufs"], **place}
        if not tr["has_beats"]:
            simple.append(tr["title"] or Path(tr["path"]).name)
        if i in used:
            seg["reused"] = True
        used.append(i)
        shift, raw_end, lo, hi = _span(tr, place)  # raw_end:曲子放完的时间线时刻
        tl_downs = [d + shift for d in tr["downbeats"]]
        if raw_end >= duration:
            # 最后一段:最后一个能放下完整淡出的小节线起淡出
            last = [d for d in tl_downs if own_from <= d <= duration - FADE_OUT]
            fade_at = last[-1] if last and duration - FADE_OUT - last[-1] <= 4 * 60 / max(tr["bpm"], 30) \
                else max(seg["start"], duration - FADE_OUT)
            seg["end"] = round(min(duration, fade_at + FADE_OUT, raw_end), 3)
            seg["fade_out"] = round(max(0.05, seg["end"] - fade_at), 3)
            out["fade_out_start"] = round(fade_at, 3)
            segs.append(seg)
            beats += _to_timeline(tr["beats"], seg, own_from, seg["end"])
            downs += _to_timeline(tr["downbeats"], seg, own_from, seg["end"])
            break
        # 放不满:在曲子收尾段(TAIL_GUARD)之前找换曲点
        c = _switch_point(cuts, tl_downs, lo, hi)
        c = max(c, seg["start"] + XFADE)
        seg["end"] = round(c + XFADE / 2, 3)
        seg["fade_out"] = XFADE
        segs.append(seg)
        beats += _to_timeline(tr["beats"], seg, own_from, c)
        downs += _to_timeline(tr["downbeats"], seg, own_from, c)
        out["switches"].append(round(c, 3))
        start, align, own_from = c - XFADE / 2, c, c
    if out["fade_out_start"] is None:
        # 12 段都没铺满(曲子都极短):最后一段照样在片尾淡出
        last = segs[-1]
        last["end"] = min(last["end"], duration)
        last["fade_out"] = round(min(FADE_OUT, max(0.05, last["end"] - last["start"])), 3)
        out["fade_out_start"] = round(last["end"] - last["fade_out"], 3)
    out["segments"] = segs
    out["beats"], out["downbeats"] = sorted(set(beats)), sorted(set(downs))
    out["drop_at"] = segs[0].get("drop_at")
    out["mode"] = "simple" if simple else "beats"
    if simple:
        degrade.note("bgm.no_beats", f"曲子没有节拍信息,退回简单淡入淡出:{'、'.join(simple[:3])}")
    return out


# --------------------------------------------------------------------------
# 渲染底床
# --------------------------------------------------------------------------
def _segment_gain(seg: Mapping[str, Any]) -> float:
    from .. import render

    lufs = seg.get("lufs")
    if lufs is None:
        loud = render.measure_loudness(Path(seg["path"]))
        lufs = loud["I"] if loud else None
    return BED_LUFS - float(lufs) if lufs is not None else 0.0


def bed_graph(segments: Sequence[Mapping[str, Any]], duration: float,
              gains: Sequence[float]) -> str:
    """底床的 filter_complex:每段裁好、对齐电平、首尾淡变、按 start 延后,再不归一地叠起来。"""
    parts, labels = [], []
    for i, (seg, g) in enumerate(zip(segments, gains, strict=True)):
        length = max(0.05, float(seg["end"]) - float(seg["start"]))
        fi = min(float(seg.get("fade_in") or 0.05), length / 2)
        fo = min(float(seg.get("fade_out") or 0.05), length / 2)
        ms = int(round(float(seg["start"]) * 1000))
        parts.append(
            f"[{i}:a]aresample=48000,aformat=sample_fmts=fltp:channel_layouts=stereo,"
            f"atrim=0:{length:.3f},asetpts=PTS-STARTPTS,volume={g:.2f}dB,"
            f"afade=t=in:st=0:d={fi:.3f},afade=t=out:st={length - fo:.3f}:d={fo:.3f},"
            f"adelay=delays={ms}:all=1[s{i}]")
        labels.append(f"[s{i}]")
    lim = "alimiter=limit=0.89:attack=5:release=50:level=0:latency=1"
    if len(labels) == 1:
        parts.append(f"{labels[0]}apad=whole_dur={duration:.3f},{lim}[bed]")
    else:
        parts.append(f"{''.join(labels)}amix=inputs={len(labels)}:duration=longest:"
                     f"dropout_transition=0:normalize=0,apad=whole_dur={duration:.3f},{lim}[bed]")
    return ";".join(parts)


def render_bed(bgm_plan: Mapping[str, Any], dest: Path) -> Path | None:
    """把 plan 渲成与成片等长的 48k 立体声 wav,旁边写 `<dest>.plan.json`。失败返回 None。"""
    from .. import render

    segs = list(bgm_plan.get("segments") or [])
    duration = float(bgm_plan.get("duration") or 0.0)
    if not segs or duration <= 0:
        return None
    inputs: list[str] = []
    for seg in segs:
        length = max(0.05, float(seg["end"]) - float(seg["start"]))
        inputs += ["-ss", f"{float(seg['offset']):.3f}", "-t", f"{length + 0.5:.3f}", "-i", str(seg["path"])]
    graph = bed_graph(segs, duration, [_segment_gain(s) for s in segs])
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        proc = render._run(["-y", "-v", "error", *inputs, "-filter_complex", graph, "-map", "[bed]",
                            "-t", f"{duration:.3f}", "-c:a", "pcm_s16le", "-ar", "48000", str(dest)],
                           timeout=600)
    except (render.RenderError, OSError) as exc:
        degrade.note("bgm.bed_failed", str(exc))
        return None
    if proc.returncode != 0 or not dest.is_file() or dest.stat().st_size == 0:
        _log.warning("BGM 底床渲染失败:%s", (proc.stderr or "")[-300:])
        degrade.note("bgm.bed_failed", (proc.stderr or "")[-300:])
        return None
    sidecar = Path(str(dest) + render.BED_SIDECAR_SUFFIX)
    sidecar.write_text(json.dumps(dict(bgm_plan), ensure_ascii=False, indent=1), encoding="utf-8")
    return dest
