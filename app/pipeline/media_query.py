"""逐镜头检索词:给素材层的每个镜头定"搜什么"。

为什么要单独一层
----------------
过去真实素材只用一个检索词(车型名),所有镜头共享一个候选池。现在每个镜头
都要先去网上找 —— 内饰镜头该搜"比亚迪汉 内饰",车展镜头该搜"车展 人流",
城市夜景镜头去图库得用英文 "city night traffic"。分镜里的 `visual_zh` 是给
人看、给生成模型翻译的长句,直接拿去搜几乎搜不到东西。

所以这里一次 LLM 批量调用,把每个镜头压成:
- `kind`:car(画面主体是目标车型)/ other(氛围、行业、人物等非车画面);
- `query_zh`:短中文检索词,给 B 站 / Bing / 抖音;
- `query_en`:短英文检索词,给 YouTube / Pexels / Pixabay;
顺带解析出 `brand`(品牌),给官方素材渠道用。

LLM 不可用或输出解析失败时退回模板,**不抛异常** —— 检索词差一点只是
命中率低一点,不能为此卡住素材层。
"""
from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from typing import Any, Sequence

from ..categories import CategoryProfile
from ..models import ShotSpec

#: 画面主体是车的视角。其余视角(industry 等)默认是非车画面
CAR_ANGLES: tuple[str, ...] = ("exterior_45", "interior", "rear", "detail", "screen_spec")


@dataclass
class ShotQuery:
    idx: int
    kind: str            # car / other
    query_zh: str
    query_en: str
    angle: str = ""


SYSTEM = (
    "你是视频素材检索助手。为解说视频的每个镜头写**网上搜素材用的检索词**。\n"
    "规则:\n"
    "1. kind:画面主体是目标车型本身(外观、内饰、细节、仪表、驾驶中的这台车)写 car;"
    "氛围、城市、人物、车展人流、充电桩、行业场景等非这台车的画面写 other。\n"
    "2. query_zh:2-6 个词的中文检索词。car 镜头必须以车型名开头,再接视角词"
    "(如 \"比亚迪汉 内饰 中控\");other 镜头只写画面内容,不要写车型名。\n"
    "3. query_en:同义的 2-5 个英文单词,给国外视频站和图库用。"
    "car 镜头以英文车型名开头;other 镜头写通用画面词(如 \"city night traffic\")。\n"
    "4. brand:目标车型所属品牌的中文名(如 比亚迪、特斯拉),不知道就留空。\n"
    "只输出 JSON:{\"brand\": \"...\", \"shots\": [{\"idx\": 0, \"kind\": \"car\", "
    "\"query_zh\": \"...\", \"query_en\": \"...\"}]}"
)


def _fallback(subject: str, profile: CategoryProfile, spec: ShotSpec) -> ShotQuery:
    angle = spec.angle or "exterior_45"
    if angle in CAR_ANGLES:
        return ShotQuery(
            idx=spec.idx, kind="car", angle=angle,
            query_zh=profile.reference_query(subject, angle).replace(" 实拍 ", " "),
            query_en=subject,
        )
    desc = profile.angle_queries.get(angle, "")
    visual = re.sub(r"[,，。;；、:：!！?？\[\]【】\"“”'‘’()（）]+", " ", spec.visual_zh or "")
    visual = " ".join(visual.split()[:3])[:16]
    return ShotQuery(
        idx=spec.idx, kind="other", angle=angle,
        query_zh=" ".join(x for x in (desc, visual) if x).strip() or subject,
        query_en=_EN_FALLBACK.get(angle, "car show crowd"),
    )


#: 非车视角的英文兜底词(图库只认英文)
_EN_FALLBACK = {
    "industry": "auto show crowd",
    "city": "city night traffic",
    "people": "people driving car",
}


def build(subject: str, profile: CategoryProfile, specs: Sequence[ShotSpec],
          llm: Any = None, errors: list[str] | None = None) -> tuple[list[ShotQuery], str]:
    """返回 (逐镜头检索词, 品牌名)。"""
    base = {s.idx: _fallback(subject, profile, s) for s in specs}
    if llm is None or not specs:
        return [base[s.idx] for s in specs], ""

    lines = [
        {"idx": s.idx, "section": s.section, "angle": s.angle,
         "visual": (s.visual_zh or "")[:120]}
        for s in specs
    ]
    user = (
        f"目标车型:{subject}\n镜头列表(JSON):\n"
        + json.dumps(lines, ensure_ascii=False)
    )
    try:
        data = llm.structured(SYSTEM, user, required_keys=["shots"], temperature=0.2)
    except Exception as exc:  # noqa: BLE001
        if errors is not None:
            errors.append(f"检索词生成失败,已用模板:{str(exc)[:120]}")
        return [base[s.idx] for s in specs], ""

    brand = str(data.get("brand") or "").strip()
    merged = dict(base)
    for item in data.get("shots") or []:
        if not isinstance(item, dict):
            continue
        try:
            idx = int(item.get("idx"))
        except (TypeError, ValueError):
            continue
        if idx not in merged:
            continue
        fb = merged[idx]
        kind = str(item.get("kind") or "").strip().lower()
        merged[idx] = ShotQuery(
            idx=idx,
            kind=kind if kind in ("car", "other") else fb.kind,
            query_zh=str(item.get("query_zh") or "").strip()[:40] or fb.query_zh,
            query_en=str(item.get("query_en") or "").strip()[:60] or fb.query_en,
            angle=fb.angle,
        )
    return [merged[s.idx] for s in specs], brand


def to_json(queries: Sequence[ShotQuery]) -> list[dict[str, Any]]:
    return [asdict(q) for q in queries]
