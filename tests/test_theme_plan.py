"""内容方向库与段落权重铺展的回归测试。

铺展是手写 plan 的替代方案,也是最容易写错的一环(段落数必须精确等于
时长 / 栅格,否则节拍数与脚本段落会错位)。这里对**每个方向 × 每个时长**逐一断言。
"""
from __future__ import annotations

import pytest

from app import categories
from app.categories import ProfileError


@pytest.fixture(scope="module")
def auto():
    return categories.load_profile("auto_long")


HOOK = "开场"


# ---------------------------------------------------------------------------
# 铺展正确性 —— 逐组合断言
# ---------------------------------------------------------------------------
def test_every_theme_expands_to_exact_shot_count(auto):
    """每个方向 × 每个时长,铺展长度必须精确等于 时长/栅格。"""
    for duration in auto.supported_durations():
        expected = duration // auto.duration_grid
        for name in auto.themes:
            plan = auto.plan_for(duration, name)
            assert len(plan) == expected, (
                f"方向 {name} @ {duration}s 铺展出 {len(plan)} 段,应为 {expected} 段"
            )


def test_expanded_sections_all_defined(auto):
    for duration in auto.supported_durations():
        for name in auto.themes:
            for section in auto.plan_for(duration, name):
                assert section in auto.sections, f"方向 {name} 产出了未定义段落 {section}"


def test_expansion_is_deterministic(auto):
    """同一配置 + 同一时长,结果必须永远相同 —— 铺展是纯函数。"""
    for name in auto.themes:
        first = auto.plan_for(240, name)
        for _ in range(5):
            assert auto.plan_for(240, name) == first


# ---------------------------------------------------------------------------
# 权重语义
# ---------------------------------------------------------------------------
def test_weight_drives_allocation(auto):
    """权重大的段落分到的节拍更多 —— 三电性能(权重 3)必须压过开场。"""
    plan = auto.plan_for(240, "buyguide")
    assert plan.count("三电性能") > plan.count(HOOK)


def test_hook_always_present(auto):
    """开场必须在每个方向、每个时长的首段出现。"""
    for duration in auto.supported_durations():
        for name in auto.themes:
            plan = auto.plan_for(duration, name)
            assert plan[0] == HOOK, f"方向 {name} @ {duration}s 首段不是开场"


def test_hook_never_absorbs_remainder(auto):
    """开场声明了 `remainder: false`,不应抢走补位名额。

    最大余数法的标准做法会把余量派给"权重小 → 小数部分大"的块,
    开场权重恒为 1,于是永远排在补位首位,悬念开场会把正片挤掉。
    """
    for name in auto.themes:
        blocks = auto.themes[name].blocks
        hook = next(b for b in blocks if b["section"] == HOOK)
        assert hook["remainder"] is False, f"方向 {name} 的开场未关闭补位"

    shortest = min(auto.supported_durations())
    for name in auto.themes:
        assert auto.plan_for(shortest, name).count(HOOK) == 1, (
            f"方向 {name} 在 {shortest} 秒下拿到了多个开场"
        )


def test_no_remainder_blocks_never_exceed_floor_share():
    """`remainder: false` 的块不得超过其按比例应得的 floor。"""
    import math

    auto = categories.load_profile("auto_long")
    for name, theme in auto.themes.items():
        weights = [b["weight"] for b in theme.blocks]
        total = sum(weights)
        for duration in auto.supported_durations():
            shots = duration // auto.duration_grid
            plan = auto.plan_for(duration, name)
            for block, weight in zip(theme.blocks, weights):
                if block.get("remainder", True):
                    continue
                floor = max(1, math.floor(shots * weight / total))
                got = plan.count(block["section"])
                assert got <= floor, (
                    f"方向 {name} @ {duration}s:{block['section']} 拿到 {got} 个,"
                    f"超过其 floor 份额 {floor}"
                )


def test_block_order_is_preserved(auto):
    """同一段落的所有副本必须连续 —— 不能被别的段落打断。"""
    for name in auto.themes:
        seen: list[str] = []
        for section in auto.plan_for(240, name):
            if not seen or seen[-1] != section:
                assert section not in seen, f"方向 {name} 的段落 {section} 被拆成多段"
                seen.append(section)


# ---------------------------------------------------------------------------
# 方向解析与回退
# ---------------------------------------------------------------------------
def test_unknown_theme_falls_back_without_raising(auto):
    """未知方向名必须回退默认方向,而不是抛异常卡死已入库的项目。"""
    plan = auto.plan_for(240, "definitely-not-a-theme")
    assert plan == auto.plan_for(240, auto.default_theme)


def test_empty_theme_name_falls_back(auto):
    assert auto.plan_for(120, "") == auto.plan_for(120, auto.default_theme)
    assert auto.plan_for(120, None) == auto.plan_for(120, auto.default_theme)


def test_default_theme_exists(auto):
    assert auto.default_theme in auto.themes


# ---------------------------------------------------------------------------
# 来源路由
# ---------------------------------------------------------------------------
def test_buyguide_keeps_product_news(auto):
    """选购方向必须保留 ifind.product_news,否则产品参数无处可来。"""
    assert "ifind.product_news" in auto.sources_for("buyguide")


def test_unknown_theme_sources_fall_back(auto):
    assert auto.sources_for("nope") == auto.sources_for(auto.default_theme)


# ---------------------------------------------------------------------------
# 校验:配置写错必须在加载期拦下
# ---------------------------------------------------------------------------
def test_too_many_blocks_is_rejected(tmp_path, monkeypatch):
    """块数超过最短时长的镜头数,铺展必然压到 0 —— 必须在加载期报错。"""
    blocks = "\n".join(f"      - {{section: 开场钩子, weight: 1}}" for _ in range(11))
    yaml_text = (
        "name: badcat\nlabel: 坏品类\nduration_grid: 6\ndurations: [60]\n"
        "sections:\n  开场钩子:\n    beat: x\n"
        "default_theme: t\n"
        "themes:\n  t:\n    label: T\n    blocks:\n" + blocks + "\n"
    )
    (tmp_path / "badcat.yaml").write_text(yaml_text, encoding="utf-8")
    monkeypatch.setattr(categories, "PROFILES_DIR", tmp_path)
    with pytest.raises(ProfileError, match="超过最短时长的镜头数"):
        categories.load_profile("badcat")


def test_unknown_section_in_blocks_is_rejected(tmp_path, monkeypatch):
    yaml_text = (
        "name: badcat2\nlabel: 坏品类\nduration_grid: 6\n"
        "sections:\n  开场钩子:\n    beat: x\n"
        "default_theme: t\n"
        "themes:\n  t:\n    label: T\n"
        "    blocks:\n      - {section: 不存在的段落, weight: 1}\n"
    )
    (tmp_path / "badcat2.yaml").write_text(yaml_text, encoding="utf-8")
    monkeypatch.setattr(categories, "PROFILES_DIR", tmp_path)
    with pytest.raises(ProfileError, match="未定义的段落"):
        categories.load_profile("badcat2")
