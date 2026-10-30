"""已渲染各拍的素材使用情况统计。"""
from __future__ import annotations

from typing import Any, Sequence
from ... import db
from ...models import ShotSpec

from .beat_render import beat_role, chapters_of


# --------------------------------------------------------------------------
# 接入流水线
# --------------------------------------------------------------------------
def footage_mix(project_id: str, specs: Sequence[ShotSpec]) -> dict[str, Any]:
    """已渲染各拍的背景构成(读 beats/NNN.json):总秒数、静图占比、第三方视频占比,
    以及开场 / 结论段里的第三方秒数(应当是 0)。一拍都没渲过返回空字典。"""
    chapters, where = chapters_of(specs)
    total = still = third = edge = 0.0
    for pos, s in enumerate(specs):
        pieces = db.read_json(project_id, f"beats/{s.idx:03d}.json", None) or []
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
    """成片用了哪些原始素材(读 beats/NNN.json,写 materials_used.json):
    每段的拍号、起止秒、原始来源地址、平台、取材当时的授权状态。版权投诉 / 下架时按来源地址
    反查(`projects_using`)。授权没核验的段汇总在 `uncleared`,发布前人工复核。"""
    rows: list[dict[str, Any]] = []
    for s in specs:
        for p in db.read_json(project_id, f"beats/{s.idx:03d}.json", None) or []:
            if p.get("item_id") == "bg":  # 卡片底图,不是素材池里的素材
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


def projects_using(source_url: str, project_ids: Sequence[str]) -> list[str]:
    """哪些项目的成片用过这个来源地址(读各项目的 materials_used.json)。"""
    return [pid for pid in project_ids
            if any(s.get("source_url") == source_url
                   for s in (db.read_json(pid, "materials_used.json", None) or {}).get("sources", []))]
