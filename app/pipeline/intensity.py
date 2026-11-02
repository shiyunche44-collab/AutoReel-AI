"""情绪曲线:每段(拍)的强度 1 缓 / 2 中 / 3 强。

脚本阶段让模型给每段打强度;没打 / 打得不可用时按这一拍在片子里的角色兜底
(钩子、结论 = 强,数据段 = 中,其余 = 缓),保证没有 LLM 输出也有一条曲线。
渲染时按强度联动拍内换刀频率、转场样式、BGM 起伏(见 `config.INTENSITY_CURVE`)。
"""
from __future__ import annotations

from typing import Sequence

from ..models import ScriptSegment, ShotSpec

CALM, MID, STRONG = 1, 2, 3

#: 数据 / 对比型版式:在讲数,强度至少「中」
_DATA_LAYOUTS = {"card", "stat", "panel", "concept"}


def clamp(value: object) -> int:
    """模型给的强度 → 0(没标 / 不可用)或 1-3。"""
    try:
        n = int(float(str(value).strip()))
    except (TypeError, ValueError):
        return 0
    return n if CALM <= n <= STRONG else 0


def role_default(role: str, layout: str = "") -> int:
    """兜底:钩子 / 结论 = 强,数据版式 = 中,其余 = 缓。`role` 见 `beat_render.beat_role`。"""
    if role in ("hook", "ending"):
        return STRONG
    return MID if layout in _DATA_LAYOUTS else CALM


def of(spec: ShotSpec, role: str) -> int:
    """这一拍的强度:脚本标了就用它,没标按角色兜底。"""
    return clamp(spec.intensity) or role_default(role, spec.layout)


def normalize(segments: Sequence[ScriptSegment]) -> None:
    """就地整理一稿的强度:
    - 全片打得一样(全 3、全 2)= 没有曲线,清成 0 让兜底接手;
    - 「强」超过约三分之一就压平:第一段和最后一段保留,中间靠后的强段降到「中」。"""
    segs = list(segments)
    for s in segs:
        s.intensity = clamp(s.intensity)
    marked = [s.intensity for s in segs if s.intensity]
    if len(set(marked)) <= 1 and len(segs) > 2:
        for s in segs:
            s.intensity = 0
        return
    cap = max(2, len(segs) // 3 + 1)
    strong = [s for s in segs if s.intensity == STRONG]
    for s in reversed(strong[1:-1] if len(strong) > 2 else []):
        if len(strong) <= cap:
            break
        s.intensity = MID
        strong.remove(s)
