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
