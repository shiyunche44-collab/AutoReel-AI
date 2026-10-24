"""项目变体:开头 / 结尾写法、中段顺序、避开最近几条。不需要网络与额度。"""
from __future__ import annotations

import pytest

from app import config
from app.categories import load_profile, suggest_theme
from app.models import ProjectCreate, ScriptSegment
from app.pipeline import script_gen, variety

FORMATS = ("buyguide", "roundup", "priceband", "deepdive", "qa", "pk", "debate")


@pytest.fixture(scope="module")
def auto():
    return load_profile("auto_long")


@pytest.fixture
def fresh_db(tmp_path, monkeypatch):
    from app import db

    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(config, "PROJECTS_DIR", tmp_path / "data" / "projects")
    monkeypatch.setattr(db, "_INITIALISED", False)
    return db


def _seg(idx, section, text):
    return ScriptSegment(idx=idx, section=section, text=text)


# --------------------------------------------------------------------------
# 挑选
# --------------------------------------------------------------------------
def test_pick_is_reproducible(auto):
    theme = auto.theme_spec("buyguide")
    assert variety.pick(theme, "p_abc") == variety.pick(theme, "p_abc")
    picks = {(v["opening"], v["ending"]) for v in (variety.pick(theme, f"p_{i}") for i in range(30))}
    assert len(picks) > 5


def test_pick_avoids_recent(auto):
    theme = auto.theme_spec("buyguide")
    recent = [{"opening": o, "ending": e} for o, e in
              (("conflict", "action"), ("question", "pick_side"), ("scene", "verdict"), ("myth", "next_episode"))]
    for i in range(20):
        v = variety.pick(theme, f"p_{i}", recent)
        assert v["opening"] == "number" and v["ending"] == "decision_card"


def test_pick_all_used_falls_back_to_oldest(auto):
    theme = auto.theme_spec("qa")  # 开头只有 question / scene / number
    recent = [{"opening": "question"}, {"opening": "scene"}, {"opening": "number"}]
    assert variety.pick(theme, "p_x", recent)["opening"] == "number"


def test_pick_respects_user_choice_and_rejects_unknown(auto):
    theme = auto.theme_spec("pk")
    assert variety.pick(theme, "p_x", opening="question", ending="verdict")["opening"] == "question"
    with pytest.raises(ValueError, match="开头可选"):
        variety.pick(theme, "p_x", opening="scene")


def test_every_theme_lists_known_variants(auto):
    for name in FORMATS:
        theme = auto.theme_spec(name)
        assert theme.openings and theme.endings, name
        assert set(theme.openings) <= set(variety.OPENINGS)
        assert set(theme.endings) <= set(variety.ENDINGS)


# --------------------------------------------------------------------------
# 段落铺展
# --------------------------------------------------------------------------
def test_conflict_only_blocks_dropped_for_other_openings(auto):
    conflict = auto.plan_for(240, "buyguide", variant={"opening": "conflict"})
    question = auto.plan_for(240, "buyguide", variant={"opening": "question"})
    assert "亮出出处" in conflict and "兑现承诺" in conflict
    assert "亮出出处" not in question and "兑现承诺" not in question
    assert len(question) == len(conflict)
    # 不给 variant = 老项目,和以前一样
    assert auto.plan_for(240, "buyguide") == conflict


def test_order_seed_only_moves_movable_blocks(auto):
    theme = auto.theme_spec("buyguide")
    base = [b["section"] for b in auto.variant_blocks(theme, {"opening": "conflict"})]
    orders = set()
    for seed in range(1, 40):
        got = [b["section"] for b in auto.variant_blocks(theme, {"opening": "conflict", "order_seed": seed})]
        orders.add(tuple(got))
        for i, b in enumerate(theme.blocks):
            if not b["movable"]:
                assert got[i] == base[i], (seed, got)
        assert sorted(got) == sorted(base)
    assert len(orders) > 5


@pytest.mark.parametrize("name", FORMATS)
def test_every_variant_fits_every_duration(auto, name):
    theme = auto.theme_spec(name)
    subjects = theme.placeholder_subjects() if theme.multi else None
    for a in config.ASPECTS:
        for d in auto.theme_durations(theme, a):
            for o in theme.openings:
                v = {"opening": o, "order_seed": 7}
                slots = auto.slots_for(d, name, subjects, portrait=a == config.PORTRAIT, variant=v)
                assert len(slots) == auto.shots_for(d), (name, a, d, o)
                assert "开场" in slots[0].section and "结论" in slots[-1].section


# --------------------------------------------------------------------------
# 提示词
# --------------------------------------------------------------------------
class _LLM:
    def __init__(self):
        self.system = ""

    def structured(self, system, user, **_):
        self.system = system
        return {"segments": [], "title": "t", "thesis": "x"}


def _prompt(auto, theme, variant):
    llm = _LLM()
    script_gen.generate(subject="小米YU7", profile=auto, facts=[], plan=list(auto.plan_for(120, theme)),
                        duration=120, llm=llm, theme=auto.theme_spec(theme), variant=variant)
    return llm.system


def test_default_variant_prompt_unchanged(auto):
    assert _prompt(auto, "buyguide", None) == _prompt(
        auto, "buyguide", {"opening": "conflict", "ending": "action", "order_seed": 3})


def test_variant_prompt_swaps_rule10_and_theme_rules(auto):
    system = _prompt(auto, "buyguide", {"opening": "scene", "ending": "decision_card"})
    assert "开头用场景冷开,结尾给决策卡" in system
    assert "先给冲突" not in system and "评论区说」)\n" not in system
    # buyguide 里只属于冲突开头 / 行动结尾的规则不进提示词
    assert "亮出\"每个数字都能查到出处\"" not in system
    assert "结尾要给行动:最后一句要么提问" not in system
    assert "开场要是立场不是选题" not in system
    # 与写法无关的规则还在
    assert "每段开头一句就亮观点" in system


# --------------------------------------------------------------------------
# lint
# --------------------------------------------------------------------------
def test_ending_lint_follows_variant():
    segs = [_seg(0, "结论建议", "所以选长续航版。你选标准版还是长续航版?评论区说。")]
    assert script_gen.ending_lint(segs, variety.ENDINGS["pick_side"]) == []
    assert script_gen.ending_lint(segs, variety.ENDINGS["next_episode"])
    assert script_gen.ending_lint([_seg(0, "结论建议", "它适合天天跑高速的人。")],
                                  variety.ENDINGS["verdict"]) == []
    # 默认 = 以前的行动结尾
    assert script_gen.ending_lint([_seg(0, "结论建议", "就这样。")])


def test_stance_lint_skips_question_opening():
    segs = [_seg(0, "开场", "冬天它到底能跑多远?")]
    assert script_gen.stance_lint(segs)
    assert script_gen.stance_lint(segs, variety.OPENINGS["question"]) == []


# --------------------------------------------------------------------------
# 建项目
# --------------------------------------------------------------------------
def test_create_project_stores_and_rotates_variant(fresh_db):
    from app.pipeline import orchestrator

    seen = []
    for _ in range(4):
        pid = orchestrator.create_project(ProjectCreate(subject="小米YU7", theme="buyguide", duration=240))
        seen.append(variety.parse(fresh_db.get_project(pid)["variant"]))
    assert len({v["opening"] for v in seen}) == 4
    assert len({v["ending"] for v in seen}) == 4

    pid = orchestrator.create_project(ProjectCreate(subject="小米YU7", theme="buyguide", duration=240,
                                                    opening="myth", ending="verdict"))
    v = variety.parse(fresh_db.get_project(pid)["variant"])
    assert (v["opening"], v["ending"]) == ("myth", "verdict")
    with pytest.raises(orchestrator.PipelineError, match="结尾可选"):
        orchestrator.create_project(ProjectCreate(subject="小米YU7", theme="pk", ending="action",
                                                  subjects=[{"name": "小米YU7"}, {"name": "特斯拉Model Y"}]))


def test_suggest_theme_avoids_recent(auto):
    assert suggest_theme(auto, []) == "buyguide"
    got = suggest_theme(auto, ["buyguide"])
    assert got != "buyguide" and not auto.theme_spec(got).multi
    assert not auto.theme_spec(got).requires_angle
