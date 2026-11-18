"""成片的素材使用情况统计。

剪辑 v2 的时间线算数时(`edit.project.active`)按时间线上的刀算:成片用的就是那一组;
否则按逐拍渲染记录 `beats/NNN.json` 算(旧合成)。两者都按拍号归组,见 `beat_pieces`。"""
from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from ... import db
from ...models import ShotSpec
from .beat_render import beat_role, chapters_of

# --------------------------------------------------------------------------
# 接入流水线
# --------------------------------------------------------------------------
#: 静图时长超过这个比例就提醒:这一版画面基本是幻灯片
STILL_RATIO_WARN = 0.5


def _beat_file_pieces(project_id: str, idx: int) -> list[dict[str, Any]]:
    return list(db.read_json(project_id, f"beats/{idx:03d}.json", None) or [])


def _slice(pieces: Sequence[dict[str, Any]], start: float, length: float) -> list[dict[str, Any]]:
    """逐拍成品里 [start, start+length) 这一段用到了哪些素材(按各段时长累加定位),长度截成重叠部分。"""
    out: list[dict[str, Any]] = []
    t = 0.0
    end = start + length
    for p in pieces:
        n = float(p.get("length") or 0.0)
        a, b = max(t, start), min(t + n, end)
        if b - a > 1e-3:
            out.append(dict(p) | {"length": round(b - a, 3)})
        t += n
    return out


def _clip_piece(c: dict[str, Any]) -> dict[str, Any]:
    return {"item_id": c.get("item_id") or "", "kind": c.get("kind"), "path": c.get("src") or "",
            "start": c.get("src_in"), "length": c.get("dur"),
            "source_url": c.get("source_url") or "", "platform": c.get("platform") or "",
            "license_status": c.get("license_status") or "",
            "third_party": bool(c.get("third_party")),
            "crop_bottom": c.get("crop_bottom") or 0.0, "crop_edge": c.get("crop_edge") or 0.0}


def timeline_clips(project_id: str) -> list[dict[str, Any]] | None:
    """算数的剪辑 v2 时间线上的刀(不含片尾);没有算数的时间线返回 None。"""
    from ..edit import project as edit_project

    saved = edit_project.active(project_id)
    if saved is None:
        return None
    return [c for c in saved.get("clips") or [] if int(c.get("beat", -1)) >= 0]


def beat_pieces(project_id: str, specs: Sequence[ShotSpec]) -> dict[int, list[dict[str, Any]]]:
    """每拍成片里实际用到的素材段(拍号 → [{item_id, kind, path, length, source_url, ...}])。

    有算数的时间线:素材刀直接算;整屏卡片 / 概念动画的刀算那一拍逐拍记录里的全部素材;
    取不到素材、借用逐拍成品的刀(fallback)按借用的那一段去逐拍记录里截。
    没有时间线:就是逐拍记录。"""
    idxs = [s.idx for s in specs]
    clips = timeline_clips(project_id)
    if clips is None:
        return {i: _beat_file_pieces(project_id, i) for i in idxs}
    out: dict[int, list[dict[str, Any]]] = {i: [] for i in idxs}
    cache: dict[int, list[dict[str, Any]]] = {}
    for c in clips:
        b = int(c.get("beat", -1))
        if b not in out:
            continue
        if c.get("kind") == "beat":
            out[b] += [dict(p) for p in cache.setdefault(b, _beat_file_pieces(project_id, b))]
        elif c.get("via") == "fallback":
            out[b] += _slice(cache.setdefault(b, _beat_file_pieces(project_id, b)),
                             float(c.get("src_in") or 0.0), float(c.get("dur") or 0.0))
        else:
            out[b].append(_clip_piece(c))
    return out


def cut_count(project_id: str, specs: Sequence[ShotSpec]) -> float:
    """成片一共切了几刀:有时间线按时间线的刀数,否则每拍按背景段数(至少 1)。"""
    clips = timeline_clips(project_id)
    if clips is not None:
        return float(len(clips))
    return float(sum(max(1, len(_beat_file_pieces(project_id, s.idx)))
                     for s in specs if s.duration > 0))


def still_warning(mix: dict[str, Any]) -> str:
    ratio = mix.get("still_ratio") or 0.0
    if ratio <= STILL_RATIO_WARN:
        return ""
    return f"静图占背景时长 {ratio:.0%},超过 {STILL_RATIO_WARN:.0%}:这台车能用的视频不够"


def footage_mix(project_id: str, specs: Sequence[ShotSpec]) -> dict[str, Any]:
    """成片的背景构成(见 `beat_pieces`):总秒数、静图占比、第三方视频占比,
    以及开场 / 结论段里的第三方秒数(应当是 0)。一拍都没渲过返回空字典。"""
    chapters, where = chapters_of(specs)
    by_beat = beat_pieces(project_id, specs)
    total = still = third = edge = 0.0
    for pos, s in enumerate(specs):
        pieces = by_beat.get(s.idx) or []
        role = beat_role(where, pos, len(chapters))
        for p in pieces:
            n = float(p.get("length") or 0.0)
            total += n
            if p.get("kind") == "still":
                still += n
            if p.get("third_party"):
                third += n
                if role != "body":
                    edge += n
    if not total:
        return {}
    return {"seconds": round(total, 1), "still_ratio": round(still / total, 3),
            "third_party_ratio": round(third / total, 3), "third_party_edge_seconds": round(edge, 1)}


#: 使用记录里不算"授权通过"的状态(没写状态的老记录也算)
_UNCLEARED = {"", "unverified", "pending", "denied", "expired"}


def materials_used(project_id: str, specs: Sequence[ShotSpec]) -> dict[str, Any]:
    """成片用了哪些原始素材(见 `beat_pieces`,写 materials_used.json):
    每段的拍号、起止秒、原始来源地址、平台、取材当时的授权状态。版权投诉 / 下架时按来源地址
    反查(`projects_using`)。授权没核验的段汇总在 `uncleared`,发布前人工复核。"""
    rows: list[dict[str, Any]] = []
    by_beat = beat_pieces(project_id, specs)
    for s in specs:
        for p in by_beat.get(s.idx) or []:
            if p.get("item_id") in ("bg", "", None):  # 卡片底图,不是素材池里的素材
                continue
            rows.append({"beat": s.idx, "item_id": p.get("item_id"), "kind": p.get("kind"),
                         "start": p.get("start"), "length": p.get("length"),
                         "source_url": p.get("source_url") or "", "platform": p.get("platform") or "",
                         "license_status": p.get("license_status") or "",
                         "cropped": bool(p.get("crop_bottom") or p.get("crop_edge"))})
    sources: dict[str, dict[str, Any]] = {}
    for r in rows:
        key = r["source_url"] or r["item_id"]
        agg = sources.setdefault(key, {"source_url": r["source_url"], "platform": r["platform"],
                                       "license_status": r["license_status"], "seconds": 0.0,
                                       "beats": []})
        agg["seconds"] = round(agg["seconds"] + float(r["length"] or 0.0), 3)
        if r["beat"] not in agg["beats"]:
            agg["beats"].append(r["beat"])
    out = {"pieces": rows, "sources": list(sources.values()),
           "uncleared": [s for s in sources.values() if s["license_status"] in _UNCLEARED]}
    db.write_json(project_id, "materials_used.json", out)
    return out


#: 这些平台的素材来自车企自己的官网 / 媒体中心,才能说"官方"。图库的 Item.official 也是真,
#: 那只表示"不算第三方水印片",不代表图是厂家发的
_OFFICIAL_PLATFORMS = {"site", "official_site", "press"}
_PLATFORM_NAMES = {"autohome": "汽车之家", "dongchedi": "懂车帝", "yiche": "易车",
                   "bilibili": "哔哩哔哩", "douyin": "抖音", "youtube": "YouTube",
                   "tiktok": "TikTok", "vimeo": "Vimeo"}


def _source_label(piece: dict[str, Any]) -> str:
    """一段素材在来源声明里怎么称呼。说不清来自哪就不说官方。"""
    platform = str(piece.get("platform") or "")
    video = piece.get("kind") == "video"
    if platform in _OFFICIAL_PLATFORMS or (video and piece.get("third_party") is False):
        return "品牌官方视频" if video else "品牌官方图片"
    name = _PLATFORM_NAMES.get(platform)
    if name:
        return name + ("视频" if video else "图库")
    return "网络公开素材"


def footage_source_note(project_id: str, specs: Sequence[ShotSpec], subject_label: str) -> str:
    """简介里的来源声明:按成片实际用到的素材写(见 `beat_pieces`),用得多的来源在前。
    一段素材都没用(全是卡片)返回空串。卡片底图 "bg" 不算。"""
    seconds: dict[str, float] = {}
    by_beat = beat_pieces(project_id, specs)
    for s in specs:
        for p in by_beat.get(s.idx) or []:
            if p.get("item_id") not in ("bg", "", None):
                label = _source_label(p)
                seconds[label] = seconds.get(label, 0.0) + float(p.get("length") or 0.0)
    if not seconds:
        return ""
    labels = sorted(seconds, key=lambda k: -seconds[k])
    return f"画面素材:{'、'.join(labels)},仅作{subject_label}展示"


def projects_using(source_url: str, project_ids: Sequence[str]) -> list[str]:
    """哪些项目的成片用过这个来源地址(读各项目的 materials_used.json)。"""
    return [pid for pid in project_ids
            if any(s.get("source_url") == source_url
                   for s in (db.read_json(pid, "materials_used.json", None) or {}).get("sources", []))]
