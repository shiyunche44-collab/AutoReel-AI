"""把 YAML 原始字典构建成 `CategoryProfile`,并做一致性校验。"""
from __future__ import annotations

from typing import Any

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


# --------------------------------------------------------------------------
# 加载
# --------------------------------------------------------------------------
def _build_theme(name: str, raw: dict[str, Any]) -> ThemeSpec:
    plan: dict[int, tuple[str, ...]] = {}
    for key, value in (raw.get("plan") or {}).items():
        plan[int(key)] = tuple(str(v) for v in value)

    def _block(item: Any) -> dict[str, Any]:
        if not isinstance(item, dict):
            raise ProfileError(f"方向 {name}: blocks 的每一项必须是 section/weight 映射")
        return {
            "section": str(item.get("section", "")),
            "weight": int(item.get("weight", 1)),
            # 必须显式搬运:不搬的话 remainder: false 会在解析阶段被丢掉,
            # 配置看起来写了、实际不生效 —— 铺展结果会静默回到旧行为。
            "remainder": bool(item.get("remainder", True)),
            # 横评每车段落的精简顺序:拍数不够时先砍 drop 小的(1 先砍),0 = 必留
            "drop": int(item.get("drop", 0) or 0),
            # 只在这些开头写法下才有的段落(冲突三段式的「兑现承诺」「亮出出处」)
            "only_opening": tuple(str(x) for x in (item.get("only_opening") or ())),
            # 相邻的 movable 段落之间没有先后依赖,每个项目按 order_seed 重排
            "movable": bool(item.get("movable", False)),
        }

    blocks: list[dict[str, Any]] = []
    per_subject: list[dict[str, Any]] = []
    per_subject_at = -1
    for item in (raw.get("blocks") or []):
        if isinstance(item, dict) and "per_subject" in item:
            # 多车横评:这一项是"每台车各来一份"的段落块组
            if per_subject_at >= 0:
                raise ProfileError(f"方向 {name}: per_subject 块最多一个")
            per_subject_at = len(blocks)
            per_subject = [_block(b) for b in (item.get("per_subject") or [])]
            if not per_subject:
                raise ProfileError(f"方向 {name}: per_subject 块不能为空")
            continue
        blocks.append(_block(item))
    versus = bool(raw.get("versus", False))
    count = raw.get("subject_count") or ([2, 5] if per_subject else [2, 2] if versus else [0, 0])
    try:
        lo, hi = (int(x) for x in count)
    except (TypeError, ValueError) as exc:
        raise ProfileError(f"方向 {name}: subject_count 必须是 [最少, 最多]") from exc

    return ThemeSpec(
        name=str(raw.get("name", name)),
        label=str(raw.get("label", name)),
        intent=str(raw.get("intent", "")),
        script_rules=tuple(_rule_text(r) for r in (raw.get("script_rules") or ())),
        rule_when=tuple(_rule_when(r) for r in (raw.get("script_rules") or ())
                        if isinstance(r, dict) and ("opening" in r or "ending" in r)),
        openings=tuple(str(x) for x in (raw.get("openings") or ())),
        endings=tuple(str(x) for x in (raw.get("endings") or ())),
        fact_sources=tuple(str(s) for s in (raw.get("fact_sources") or ())),
        required_scopes=tuple(str(s) for s in (raw.get("required_scopes") or ("product",))),
        allow_subject_critique=bool(raw.get("allow_subject_critique", False)),
        extra_forbidden_terms=tuple(str(t) for t in (raw.get("extra_forbidden_terms") or ())),
        blocks=tuple(blocks),
        plan=plan,
        per_subject=tuple(per_subject),
        per_subject_at=per_subject_at,
        subject_count=(lo, hi),
        versus=versus,
        requires_angle=bool(raw.get("requires_angle", False)),
        max_duration=int(raw.get("max_duration", 0) or 0),
        roster_label=str(raw.get("roster_label", "") or ""),
        roster_placeholder=str(raw.get("roster_placeholder", "") or ""),
    )


def _rule_text(raw: Any) -> str:
    """script_rules 的一项:字符串,或 {rule: ..., opening|ending: id}(只在那种开头 / 结尾下生效)。"""
    return str(raw.get("rule", "")) if isinstance(raw, dict) else str(raw)


def _rule_when(raw: dict[str, Any]) -> tuple[str, str, tuple[str, ...]]:
    kind = "opening" if "opening" in raw else "ending"
    ids = raw[kind]
    return _rule_text(raw), kind, tuple(str(x) for x in (ids if isinstance(ids, list) else [ids]))


def _build(name: str, raw: dict[str, Any]) -> CategoryProfile:
    sections = {
        key: SectionSpec(
            name=key,
            beat=str(value.get("beat", "")),
            camera=str(value.get("camera", "")),
            angle=str(value.get("angle", "exterior_45")),
            narration_style=str(value.get("narration_style", "")),
            requires_fact=bool(value.get("requires_fact", False)),
            multi_subject=bool(value.get("multi_subject", False)),
            card=str(value.get("card", "") or ""),
            layouts=tuple(str(x) for x in (value.get("layouts") or ())),
            chapter=str(value.get("chapter", "") or ""),
            fact_topic=str(value.get("fact_topic", "") or ""),
            heading=bool(value.get("heading", False)),
        )
        for key, value in (raw.get("sections") or {}).items()
    }
    plan: dict[int, tuple[str, ...]] = {}
    for key, value in (raw.get("section_plan") or {}).items():
        plan[int(key)] = tuple(str(v) for v in value)

    themes = {
        str(key): _build_theme(str(key), value or {})
        for key, value in (raw.get("themes") or {}).items()
    }

    if not themes:
        # 未声明方向的老 profile(beauty / digital / home)合成一个默认方向,
        # 行为与改造前完全一致。
        themes["overview"] = ThemeSpec(
            name="overview",
            label="通用概览",
            fact_sources=tuple(raw.get("fact_sources") or ()),
            plan=dict(plan),
        )

    default_theme = str(raw.get("default_theme") or "")
    if default_theme not in themes:
        default_theme = "overview" if "overview" in themes else next(iter(themes))

    return CategoryProfile(
        name=str(raw.get("name", name)),
        label=str(raw.get("label", name)),
        aspect_ratio=str(raw.get("aspect_ratio", "16:9")),
        duration_grid=int(raw.get("duration_grid", 6)),
        track=str(raw.get("track", "explainer") or "explainer"),
        durations=tuple(int(d) for d in (raw.get("durations") or ())),
        durations_portrait=tuple(int(d) for d in (raw.get("durations_portrait") or ())),
        fact_sources=tuple(raw.get("fact_sources") or ()),
        section_plan=plan,
        sections=sections,
        themes=themes,
        default_theme=default_theme,
        visual_keywords=tuple(raw.get("visual_keywords") or ()),
        reference_query_template=str(
            raw.get("reference_query_template", "{subject} 实拍 {angle}")
        ),
        angle_queries={str(k): str(v) for k, v in (raw.get("angle_queries") or {}).items()},
        official_sites={
            str(brand): {
                key: tuple(str(x) for x in (spec or {}).get(key) or ())
                for key in ("aliases", "sites", "media", "tiktok", "company", "press", "launch", "launch_list")
            }
            for brand, spec in (raw.get("official_sites") or {}).items()
        },
        vocab=_build_vocab(raw.get("vocab") or {}),
        fact_queries={str(k): str(v) for k, v in (raw.get("fact_queries") or {}).items()},
        galleries=tuple(str(g) for g in (raw.get("galleries") or ())),
        voice_id=str(raw.get("voice_id") or ""),
        voices=tuple(str(v) for v in raw.get("voices") or () if v),
        forbidden_terms=tuple(raw.get("forbidden_terms") or ()),
        title_avoid=tuple(str(t) for t in (raw.get("title_avoid") or ())),
        required_disclaimers=tuple(raw.get("required_disclaimers") or ()),
        music_prompt=str(
            raw.get("music_prompt", "instrumental background music, no vocals")
        ),
        bgm_genres=tuple(str(g) for g in (raw.get("bgm_genres") or ())),
        script_style=str(raw.get("script_style", "")),
        principles=_build_principles(raw.get("principles")),
        derive=tuple(str(d) for d in (raw.get("derive") or ())),
        trends=_build_trends(raw.get("trends")),
        skeleton=bool(raw.get("skeleton", False)),
        todo=tuple(str(t) for t in (raw.get("todo") or ())),
    )


def _min_supported_shots(profile: CategoryProfile) -> int:
    """最短受支持时长对应的镜头数 —— 块数不能超过它,否则铺展会挤爆。"""
    durations = profile.all_durations() or (60,)
    return profile.shots_for(min(durations))


#: 解说轨的版式名。与 pipeline/explainer.LAYOUTS 保持一致(这里不反向导入流水线)
EXPLAINER_LAYOUTS: tuple[str, ...] = ("broll", "panel", "highlight", "card", "stat", "concept", "hook")


def _validate(profile: CategoryProfile) -> None:
    if profile.track != "explainer":
        raise ProfileError(f"品类 {profile.name}: track={profile.track!r} 只能是 explainer"
                           "(竖屏链路已删除)")
    for sname, sec in profile.sections.items():
        bad = [x for x in sec.layouts if x not in EXPLAINER_LAYOUTS]
        if bad:
            raise ProfileError(f"品类 {profile.name} 段落 {sname}: 未知版式 {bad}")
        if "concept" in sec.layouts and not profile.vocab.concept_scenes:
            raise ProfileError(f"品类 {profile.name} 段落 {sname}: 用了 concept 版式,"
                               "但 vocab.concept_scenes 没配任何动画模板")

    # 1. 老写法:手写 section_plan
    for duration, plan in profile.section_plan.items():
        expected = profile.shots_for(duration)
        if len(plan) != expected:
            raise ProfileError(
                f"品类 {profile.name}: 时长 {duration}s 需 {expected} 个段落,"
                f"实际声明 {len(plan)} 个"
            )
        unknown = [s for s in plan if s not in profile.sections]
        if unknown:
            raise ProfileError(
                f"品类 {profile.name}: section_plan 引用了未定义的段落 {unknown}"
            )

    # 2. 方向库
    min_shots = _min_supported_shots(profile)
    from ..pipeline import variety as _variety

    for tname, theme in profile.themes.items():
        bad = [o for o in theme.openings if o not in _variety.OPENINGS]
        bad += [e for e in theme.endings if e not in _variety.ENDINGS]
        bad += [v for _, kind, ids in theme.rule_when for v in ids
                if v not in (_variety.OPENINGS if kind == "opening" else _variety.ENDINGS)]
        if bad:
            raise ProfileError(f"品类 {profile.name} 方向 {tname}: 未知的开头 / 结尾写法 {bad}")
        if theme.roundup:
            lo, hi = theme.subject_count
            if not 1 <= lo <= hi:
                raise ProfileError(
                    f"品类 {profile.name} 方向 {tname}: subject_count 需满足 1 ≤ 最少 ≤ 最多")
            if not any(not b.get("drop") for b in theme.per_subject):
                raise ProfileError(
                    f"品类 {profile.name} 方向 {tname}: per_subject 至少要有一个必留(drop=0)段落")
            # 横屏:最多台数精简到底,也要塞得进横屏最短时长 —— 任何车数 × 任何横屏时长都能建项目。
            # 竖屏档位更短(60s ≈ 8 拍),只保证**最少**台数放得下;车多时建项目会提示加长时长。
            from .. import config as _config

            landscape_min = profile.shots_for(min(profile.supported_durations(_config.LANDSCAPE)))
            need = CategoryProfile.roundup_min_blocks(theme, hi)
            if need > landscape_min:
                raise ProfileError(
                    f"品类 {profile.name} 方向 {tname}: {hi} 台车精简后仍要 {need} 个段落块,"
                    f"超过横屏最短时长的镜头数 {landscape_min}")
            need = CategoryProfile.roundup_min_blocks(theme, lo)
            if need > min_shots:
                raise ProfileError(
                    f"品类 {profile.name} 方向 {tname}: {lo} 台车精简后仍要 {need} 个段落块,"
                    f"超过最短时长的镜头数 {min_shots}")
            for block in (*theme.blocks, *theme.per_subject):
                section = str(block.get("section", ""))
                if section not in profile.sections:
                    raise ProfileError(
                        f"品类 {profile.name} 方向 {tname}: 引用了未定义的段落 {section!r}")
                if int(block.get("weight", 1)) < 1:
                    raise ProfileError(
                        f"品类 {profile.name} 方向 {tname}: 段落块 {section} 的 weight 必须 ≥1")
        elif theme.uses_blocks:
            if theme.versus and not 2 <= theme.subject_count[0] <= theme.subject_count[1]:
                raise ProfileError(
                    f"品类 {profile.name} 方向 {tname}: 对决方向 subject_count 需满足 2 ≤ 最少 ≤ 最多")
            if theme.max_duration and not any(d <= theme.max_duration for d in profile.all_durations()):
                raise ProfileError(
                    f"品类 {profile.name} 方向 {tname}: max_duration={theme.max_duration} 比所有时长档位都短")
            core = CategoryProfile.core_blocks(theme.blocks)
            if core > min_shots:
                raise ProfileError(
                    f"品类 {profile.name} 方向 {tname}: 精简到底仍有 {core} 个段落块,"
                    f"超过最短时长的镜头数 {min_shots},铺展时必然压到 0。"
                    f"给可省的块标 drop(1 先砍)"
                )
            for block in theme.blocks:
                section = str(block.get("section", ""))
                if not section:
                    raise ProfileError(f"品类 {profile.name} 方向 {tname}: 段落块缺少 section")
                if section not in profile.sections:
                    raise ProfileError(
                        f"品类 {profile.name} 方向 {tname}: 引用了未定义的段落 {section!r}"
                    )
                weight = int(block.get("weight", 1))
                if weight < 1:
                    raise ProfileError(
                        f"品类 {profile.name} 方向 {tname}: 段落块 {section} 的 weight 必须 ≥1"
                    )
        else:
            for duration, plan in theme.plan.items():
                expected = profile.shots_for(duration)
                if len(plan) != expected:
                    raise ProfileError(
                        f"品类 {profile.name} 方向 {tname}: 时长 {duration}s 需 {expected} 个段落,"
                        f"实际声明 {len(plan)} 个"
                    )
                unknown = [s for s in plan if s not in profile.sections]
                if unknown:
                    raise ProfileError(
                        f"品类 {profile.name} 方向 {tname}: 引用了未定义的段落 {unknown}"
                    )

    # 3. 卡片版式写错会让该段落静默退回生成,加载期就拦掉
    for sname, sec in profile.sections.items():
        if sec.card and sec.card not in CARD_KINDS:
            raise ProfileError(
                f"品类 {profile.name} 段落 {sname}: card={sec.card!r} 不在 {CARD_KINDS} 之中"
            )

    # 4. 铺展必须落在栅格上 —— 这是最容易写错的环节,在加载期就拦掉
    for duration in profile.all_durations():
        for tname in profile.themes:
            got = len(profile.plan_for(duration, tname))
            if got != profile.shots_for(duration):
                raise ProfileError(
                    f"品类 {profile.name} 方向 {tname}: {duration}s 铺展出 {got} 个段落,"
                    f"应为 {profile.shots_for(duration)} 个"
                )

    # 5. 视角表前后自洽:写错一个视角名,背景会静默挑不到图
    v = profile.vocab
    all_views = set(v.all_views)
    for group, views in v.hint_groups.items():
        bad = [x for x in views if x not in all_views]
        if bad:
            raise ProfileError(f"品类 {profile.name}: hint_groups.{group} 引用了未知视角 {bad}")
    bad = [x for x in v.angle_of_view if x not in all_views]
    if bad:
        raise ProfileError(f"品类 {profile.name}: angle_of_view 引用了未知视角 {bad}")
    for group, views in v.coverage_views.items():
        bad = [x for x in views if x not in all_views]
        if bad:
            raise ProfileError(f"品类 {profile.name}: coverage_views.{group} 引用了未知视角 {bad}")
    bad = [x for x in (*v.hint_of_angle.values(), v.default_hint) if x not in v.hint_groups]
    if bad:
        raise ProfileError(f"品类 {profile.name}: hint_of_angle / default_hint 引用了未知偏好 {bad}")

    if profile.default_theme not in profile.themes:
        raise ProfileError(
            f"品类 {profile.name}: default_theme={profile.default_theme!r} 不在 themes 中"
        )
