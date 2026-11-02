"""品类 Profile 注册表。

新增品类 = 在 `profiles/` 下放一个 YAML,**核心引擎零改动**。

Profile 声明了什么:
- 事实来源路由(用哪些 iFinD / 企查查 接口)
- 内容方向库(每个方向一套段落结构、视觉指导、脚本规则)
- 每个段落的视觉指导、运镜倾向、参考图视角
- 合规词表与免责声明

为什么不把段落结构交给 LLM 自由发挥:汽车解说的"钩子→卖点→参数→对比→建议"
是行业惯例,写死在 Profile 里能保证每条视频的节奏稳定,而 LLM 只负责
在给定结构内填内容 —— 这是把"结构正确性"从概率问题变成确定性问题。

**方向库(theme)**:一条视频只讲一个内容方向(车型迭代史 / 横向对比 / ...),
而不是把六个方向塞进同一条 60 秒视频。每个方向用一份 `blocks` 权重表声明
"有哪些段落、各占多重的篇幅",再由 `plan_for()` 按目标镜头数**确定性铺展**
成最终的段落序列。这样任意时长都能得到结构正确的序列,不必手写
`duration -> 段落列表` —— 7 个方向 × 5 个时长手写就是 35 条,极易写错。
"""
from __future__ import annotations

from collections.abc import Sequence
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

from .build import (  # noqa: F401
    EXPLAINER_LAYOUTS,
    _build,
    _build_theme,
    _min_supported_shots,
    _rule_text,
    _rule_when,
    _validate,
)
from .profile import (  # noqa: F401
    _ANGLE_NEGATIVE_EN,
    _ANGLE_PROMPT_EN,
    CategoryProfile,
)
from .spec import (  # noqa: F401
    _PRINCIPLE_DIGIT,
    CARD_KINDS,
    COMMON_VIEWS,
    PORTRAIT_SHOTS_PER_SECTION,
    ConsistencySpec,
    Principle,
    ProfileError,
    SectionSpec,
    Slot,
    ThemeSpec,
    TrendSpec,
    Vocab,
    _build_principles,
    _build_trends,
    _build_vocab,
    _takes_remainder,
)

PROFILES_DIR = Path(__file__).resolve().parent / "profiles"


#: `extends` 继承时按键合并(子 profile 只写增改的条目)的字典;其余键整体覆盖。
#: themes 故意整体覆盖:父 profile 的方向是按它的栅格和版式写的,不该悄悄带过来。
_MERGED_KEYS = ("sections", "official_sites", "angle_queries", "vocab", "fact_queries")


def _load_raw(name: str, chain: tuple[str, ...] = ()) -> dict[str, Any]:
    path = PROFILES_DIR / f"{name}.yaml"
    if not path.is_file():
        available = sorted(p.stem for p in PROFILES_DIR.glob("*.yaml"))
        raise ProfileError(f"未知品类 {name!r}。可用品类:{available}")
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    parent = str(raw.pop("extends", "") or "")
    if not parent:
        return raw
    if parent in chain or parent == name:
        raise ProfileError(f"品类 {name}: extends 循环引用 {chain + (name, parent)}")
    base = _load_raw(parent, chain + (name,))
    merged = {**base, **raw}
    for key in _MERGED_KEYS:
        if isinstance(base.get(key), dict) and isinstance(raw.get(key), dict):
            merged[key] = {**base[key], **raw[key]}
    return merged


@lru_cache(maxsize=None)
def load_profile(name: str) -> CategoryProfile:
    raw = _load_raw(name)
    profile = _build(name, raw)
    _validate(profile)
    return profile


def available_categories() -> list[dict[str, Any]]:
    out = []
    for path in sorted(PROFILES_DIR.glob("*.yaml")):
        try:
            profile = load_profile(path.stem)
        except Exception as exc:  # pragma: no cover - 配置错误不应拖垮列表页
            out.append({"name": path.stem, "label": path.stem, "error": str(exc),
                        "skeleton": True, "durations": [], "themes": [],
                        "default_theme": ""})
            continue
        out.append({
            "name": profile.name,
            "label": profile.label,
            "skeleton": profile.skeleton,
            "todo": list(profile.todo),
            # 时长由 profile 声明(缺省用全局档位),不再取 section_plan 的键 ——
            # 方向库模式下 plan 由 blocks 铺展而来,没有"键"可言。
            "durations": list(profile.supported_durations()),
            "durations_by_aspect": {
                a: list(profile.supported_durations(a)) for a in _config_aspects()
            },
            "track": profile.track,
            "aspect_ratio": profile.aspect_ratio,
            "default_theme": profile.default_theme,
            "ui": {
                "subject_label": profile.vocab.ui_subject_label,
                "placeholder": profile.vocab.ui_placeholder,
                "help": profile.vocab.ui_help,
                "trends_label": profile.trends.sources_label,
            },
            "themes": [
                {"name": t.name, "label": t.label, "intent": t.intent,
                 # 前端的 roundup = "要填车单"(横评与对决都要)
                 "roundup": t.multi, "versus": t.versus, "subject_count": list(t.subject_count),
                 "requires_angle": t.requires_angle, "max_duration": t.max_duration,
                 "roster_label": t.roster_label, "roster_placeholder": t.roster_placeholder,
                 # 开头 / 结尾写法下拉(第一项 = 自动)
                 **_variety_options(t),
                 # 横评:每档时长最多放得下几台车(竖屏短档位放不下最多台数)
                 "max_subjects": ({str(d): profile.max_subjects(t, d) for d in profile.all_durations()}
                                  if t.multi else {})}
                for t in profile.themes.values()
            ],
        })
    return out


def _variety_options(theme: ThemeSpec) -> dict[str, Any]:
    from ..pipeline import variety

    return variety.options(theme)


def suggest_theme(profile: CategoryProfile, recent: Sequence[str]) -> str:
    """建项目页默认选哪个片型:和最近几条不一样的那个。

    只在不用额外填东西的片型里挑(不要车单、不要必填角度),免得默认值一选上就要先补表单;
    都用过就回到品类默认。"""
    simple = [t.name for t in profile.themes.values() if not t.multi and not t.requires_angle]
    if profile.default_theme in simple:
        simple.remove(profile.default_theme)
        simple.insert(0, profile.default_theme)
    fresh = [n for n in simple if n not in recent]
    return fresh[0] if fresh else profile.default_theme


def _config_aspects() -> tuple[str, ...]:
    from .. import config

    return config.ASPECTS


def default_category() -> str:
    from .. import config

    try:
        load_profile(config.DEFAULT_CATEGORY)
    except ProfileError:
        return "auto_long"
    return config.DEFAULT_CATEGORY
