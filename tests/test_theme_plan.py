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


def single(auto) -> list[str]:
    """单车方向。横评方向的开场段、段落连续性另有专门的测试(见文件末尾)。"""
    return [n for n, t in auto.themes.items() if not t.roundup]


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
    """开场必须在每个单车方向、每个时长的首段出现。"""
    for duration in auto.supported_durations():
        for name in single(auto):
            plan = auto.plan_for(duration, name)
            assert HOOK in plan[0], f"方向 {name} @ {duration}s 首段不是开场"


def test_hook_never_absorbs_remainder(auto):
    """开场声明了 `remainder: false`,不应抢走补位名额。

    最大余数法的标准做法会把余量派给"权重小 → 小数部分大"的块,
    开场权重恒为 1,于是永远排在补位首位,悬念开场会把正片挤掉。
    """
    for name in single(auto):
        blocks = auto.themes[name].blocks
        hook = next(b for b in blocks if HOOK in b["section"])
        assert hook["remainder"] is False, f"方向 {name} 的开场未关闭补位"

    shortest = min(auto.supported_durations())
    for name in single(auto):
        plan = auto.plan_for(shortest, name)
        assert plan.count(plan[0]) == 1, (
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
    for name in single(auto):
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


# ---------------------------------------------------------------------------
# 多车横评:per_subject 块按车单展开
# ---------------------------------------------------------------------------
CARS5 = ["小米YU7", "特斯拉Model Y", "问界M7", "极氪7X", "乐道L60"]


@pytest.mark.parametrize("n", [3, 4, 5])
@pytest.mark.parametrize("duration", [240, 360, 480])
def test_roundup_slots_cover_every_car(auto, n, duration):
    cars = CARS5[:n]
    theme = auto.themes["roundup"]
    slots = auto.slots_for(duration, "roundup", cars)
    assert len(slots) == duration // auto.duration_grid
    assert slots[0] == categories.Slot("横评开场", "")
    assert slots[-1] == categories.Slot("横评结论", "")
    per_car = [b["section"] for b in theme.per_subject]
    for car in cars:
        own = [s for s in slots if s.subject == car]
        # 每台车四个段落都在,按声明顺序
        assert list(dict.fromkeys(s.section for s in own)) == per_car
    # 每台车的拍连成一块,车按车单顺序排
    order = [s.subject for s in slots if s.subject]
    assert list(dict.fromkeys(order)) == cars
    for car in cars:
        idx = [i for i, s in enumerate(slots) if s.subject == car]
        assert idx == list(range(idx[0], idx[-1] + 1))


def test_roundup_plan_for_uses_placeholders(auto):
    """不给车单时 plan_for 按最少台数的占位车铺展,长度照样精确。"""
    for duration in auto.supported_durations():
        assert len(auto.plan_for(duration, "roundup")) == duration // auto.duration_grid


def test_single_theme_slots_match_plan(auto):
    for duration in auto.supported_durations():
        slots = auto.slots_for(duration, "buyguide", ["忽略我"])
        assert [s.section for s in slots] == list(auto.plan_for(duration, "buyguide"))
        assert all(s.subject == "" for s in slots)


@pytest.fixture
def fresh_db(tmp_path, monkeypatch):
    """建项目的测试一律落到临时库,绝不写进 data/aimovice.db。"""
    from app import config, db

    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(config, "PROJECTS_DIR", tmp_path / "data" / "projects")
    monkeypatch.setattr(db, "_INITIALISED", False)
    return db


@pytest.mark.parametrize("n,kept", [
    (3, {"车型亮相", "核心卖点", "参数价格", "适合谁"}),
    (4, {"车型亮相", "核心卖点", "参数价格"}),
    (5, {"车型亮相", "核心卖点"}),
])
def test_roundup_compacts_to_fit_short_duration(auto, n, kept):
    """车多时长短:按 drop 整档精简每车段落(先砍适合谁,再砍参数价格),
    每台车结构一致,开场和结论都还在。"""
    cars = CARS5[:n]
    slots = auto.slots_for(120, "roundup", cars)
    assert len(slots) == 120 // auto.duration_grid
    assert slots[0].section == "横评开场" and slots[-1].section == "横评结论"
    for car in cars:
        assert {s.section for s in slots if s.subject == car} == kept


def test_roundup_long_duration_keeps_full_set(auto):
    slots = auto.slots_for(240, "roundup", CARS5)
    for car in CARS5:
        assert {s.section for s in slots if s.subject == car} == {"车型亮相", "核心卖点", "参数价格", "适合谁"}


def test_every_roster_size_fits_every_duration(auto):
    """配置自检的承诺:车单上限 × 最短时长也能建项目。"""
    lo, hi = auto.themes["roundup"].subject_count
    for duration in auto.supported_durations():
        shots = duration // auto.duration_grid
        for n in range(lo, hi + 1):
            blocks = auto.roundup_blocks(auto.themes["roundup"], CARS5[:n], shots=shots)
            assert len(blocks) <= shots


def test_roundup_create_five_cars_in_two_minutes(fresh_db):
    from app.models import ProjectCreate, SubjectEntry
    from app.pipeline import orchestrator

    pid = orchestrator.create_project(ProjectCreate(
        subject="纯电 SUV 横评", theme="roundup", duration=120,
        subjects=[SubjectEntry(name=c) for c in CARS5]))
    assert fresh_db.get_project(pid)["shot_count"] == 15


def test_roundup_create_checks_car_count(fresh_db):
    from app.models import ProjectCreate, SubjectEntry
    from app.pipeline import orchestrator
    from app.pipeline.orchestrator import PipelineError

    payload = ProjectCreate(subject="横评", theme="roundup", duration=240,
                            subjects=[SubjectEntry(name="小米YU7"), SubjectEntry(name=" 小米YU7 "),
                                      SubjectEntry(name="问界M7")])
    with pytest.raises(PipelineError, match="3–5 台车"):
        orchestrator.create_project(payload)


def test_per_subject_block_must_be_single(tmp_path, monkeypatch):
    from app.categories import _build_theme

    raw = {"label": "T", "blocks": [{"per_subject": [{"section": "a"}]},
                                    {"per_subject": [{"section": "b"}]}]}
    with pytest.raises(ProfileError, match="最多一个"):
        _build_theme("t", raw)
