"""片型多样性:单点深挖 / 问答式 / 1v1 对决 / 槽点争议。

每个片型的段落结构、建项目校验、脚本 heading、对决的配图车。不需要网络与额度。
"""
from __future__ import annotations

import pytest

from app import config
from app.categories import load_profile
from app.models import Fact, ProjectCreate, ScriptSegment, ShotSpec, SubjectEntry
from app.pipeline import explainer, intake, script_gen

NEW = ("deepdive", "qa", "pk", "debate")
PAIR = ["小米YU7", "特斯拉Model Y"]


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


# --------------------------------------------------------------------------
# 结构
# --------------------------------------------------------------------------
@pytest.mark.parametrize("name", NEW)
def test_every_format_fits_every_allowed_duration(auto, name):
    """每个片型 × 画幅 × 允许的时长:拍数对得上,开场在头、结论在尾。"""
    theme = auto.theme_spec(name)
    for a in config.ASPECTS:
        for d in auto.theme_durations(theme, a):
            slots = auto.slots_for(d, name, PAIR if theme.multi else None,
                                   portrait=a == config.PORTRAIT)
            assert len(slots) == auto.shots_for(d), (name, a, d)
            assert "开场" in slots[0].section and "结论" in slots[-1].section, (name, a, d)
            assert all(not s.subject for s in slots)


def test_formats_differ_in_structure(auto):
    """片型之间的段落几乎不重叠 —— 这正是"不是同一套流程"的意思。"""
    plans = {n: set(auto.plan_for(240, n)) for n in ("buyguide", *NEW) if 240 in
             auto.theme_durations(auto.theme_spec(n))}
    for a in plans:
        for b in plans:
            if a < b:
                assert len(plans[a] & plans[b]) == 0, (a, b)


def test_deepdive_caps_duration(auto):
    theme = auto.theme_spec("deepdive")
    assert theme.requires_angle
    assert max(auto.theme_durations(theme, config.LANDSCAPE)) == 240
    assert auto.theme_durations(auto.theme_spec("buyguide"), config.LANDSCAPE) == \
        auto.supported_durations(config.LANDSCAPE)


def test_pk_is_versus_not_roundup(auto):
    pk = auto.theme_spec("pk")
    assert pk.versus and pk.multi and not pk.roundup
    assert pk.subject_count == (2, 2)
    # 对决段算对比段:厂商口径不许当论据
    assert all(script_gen._is_comparison_section(s) for s in auto.plan_for(240, "pk"))


def test_categories_api_flags(auto):
    from app.categories import available_categories

    cat = next(c for c in available_categories() if c["name"] == "auto_long")
    themes = {t["name"]: t for t in cat["themes"]}
    assert themes["pk"]["roundup"] and themes["pk"]["versus"]
    assert themes["deepdive"]["requires_angle"] and themes["deepdive"]["max_duration"] == 240


# --------------------------------------------------------------------------
# 建项目
# --------------------------------------------------------------------------
def test_deepdive_needs_angle(fresh_db):
    from app.pipeline import orchestrator

    with pytest.raises(orchestrator.PipelineError, match="选题角度"):
        orchestrator.create_project(ProjectCreate(subject="小米YU7", theme="deepdive", duration=120))
    pid = orchestrator.create_project(ProjectCreate(subject="小米YU7", theme="deepdive",
                                                    duration=120, angle="800V 到底快在哪"))
    assert fresh_db.get_project(pid)["theme"] == "deepdive"


def test_deepdive_rejects_long_duration(fresh_db):
    from app.pipeline import orchestrator

    with pytest.raises(orchestrator.PipelineError, match="最长 240s"):
        orchestrator.create_project(ProjectCreate(subject="小米YU7", theme="deepdive", duration=480,
                                                  angle="800V"))


def test_pk_stores_two_cars(fresh_db):
    from app.pipeline import orchestrator

    pid = orchestrator.create_project(ProjectCreate(
        subject="小米YU7 vs 特斯拉Model Y", theme="pk", duration=240,
        subjects=[SubjectEntry(name="小米YU7"), SubjectEntry(name="特斯拉Model Y")]))
    row = fresh_db.get_project(pid)
    assert [s["name"] for s in fresh_db.project_subjects(row)] == PAIR
    assert row["title"] == "小米YU7 vs 特斯拉Model Y"
    with pytest.raises(orchestrator.PipelineError, match="2–2 台车"):
        orchestrator.create_project(ProjectCreate(
            subject="三车", theme="pk", subjects=[SubjectEntry(name=n) for n in ("A", "B", "C")]))


def test_topic_hook_saved(fresh_db):
    from app.pipeline import orchestrator

    pid = orchestrator.create_project(ProjectCreate(subject="小米YU7", hook="多花三万买的是快充"))
    assert fresh_db.read_json(pid, "topic.json")["hook"] == "多花三万买的是快充"


# --------------------------------------------------------------------------
# 脚本:heading
# --------------------------------------------------------------------------
class _ScriptLLM:
    def __init__(self, segments):
        self.segments = segments
        self.calls: list[tuple[str, str]] = []

    def structured(self, system, user, **_):
        self.calls.append((system, user))
        return {"segments": self.segments, "title": "t", "thesis": "x"}


def _fact(fid="f_1", subject="", claim="续航 835 公里"):
    return Fact(id=fid, claim=claim, source_tool="t", confidence="high", scope="product",
                retrieved_at="2026-01-01", subject=subject)


def test_qa_script_carries_heading(auto):
    plan = ["问答开场", "问题一", "问题一", "问答结论"]
    llm = _ScriptLLM([
        {"section": "问答开场", "text": "冬天续航到底够不够?", "fact_refs": []},
        {"section": "问题一", "heading": "冬天续航打几折?", "text": "先说答案:够用。", "fact_refs": []},
        {"section": "问题一", "text": "原因在热泵。", "fact_refs": []},
        {"section": "问答结论", "text": "结论就是够用。", "fact_refs": []},
    ])
    segs, _, _ = script_gen.generate(subject="小米YU7", profile=auto, facts=[_fact()], plan=plan,
                                     duration=120, llm=llm, theme=auto.theme_spec("qa"),
                                     hook="冬天续航够不够")
    system, _ = llm.calls[-1]
    assert "`heading`" in system and "问题一" in system
    assert "冬天续航够不够" in system
    segs = script_gen.normalize_segments(segs, plan)
    assert [s.heading for s in segs] == ["", "冬天续航打几折?", "冬天续航打几折?", ""]
    split = script_gen.split_segments(segs, 6)
    assert all(s.heading == "冬天续航打几折?" for s in split if s.section == "问题一")
    # 章节条显示问题本身
    spec = explainer.fallback_beats(subject="小米YU7", profile=auto, segments=segs)[1]
    assert spec.chapter == "冬天续航打几折?"


def test_buyguide_prompt_has_no_heading_rule(auto):
    llm = _ScriptLLM([])
    script_gen.generate(subject="小米YU7", profile=auto, facts=[_fact()],
                        plan=list(auto.plan_for(120)), duration=120, llm=llm,
                        theme=auto.theme_spec())
    assert "`heading`" not in llm.calls[-1][0]


def test_heading_dropped_for_plain_sections(auto):
    llm = _ScriptLLM([{"section": "开场", "heading": "乱给的", "text": "多花三万买快充。"}])
    segs, _, _ = script_gen.generate(subject="小米YU7", profile=auto, facts=[], plan=["开场"],
                                     duration=120, llm=llm, theme=auto.theme_spec())
    assert segs[0].heading == ""


# --------------------------------------------------------------------------
# 对决:共用段可以引用两台车,配图跟着旁白点名的车走
# --------------------------------------------------------------------------
def test_pk_shared_segment_may_cite_both_cars(auto):
    facts = [_fact("f_a", "小米YU7", "小米YU7 续航 835 公里"),
             _fact("f_b", "特斯拉Model Y", "特斯拉Model Y 续航 719 公里")]
    segs = [ScriptSegment(idx=0, section="对决三电", text="YU7 跑 835 公里,Model Y 是 719 公里。",
                          fact_refs=["f_a", "f_b"])]
    issues = script_gen.compliance_lint(segs, auto, facts, theme=auto.theme_spec("pk"))
    assert not [i for i in issues if i.is_error and "引用" in i.message]


def test_beat_subject_follows_named_car():
    spec = ShotSpec(idx=0, visual_zh="", narration_zh="Model Y 的后排更宽一些")
    assert explainer.beat_subject(spec, PAIR, "x") == "特斯拉Model Y"
    spec = ShotSpec(idx=1, visual_zh="", narration_zh="小米YU7 的补能更快")
    assert explainer.beat_subject(spec, PAIR, "x") == "小米YU7"
    # 两台都点了名 / 都没点名:按拍号轮换
    spec = ShotSpec(idx=1, visual_zh="", narration_zh="YU7 和 Model Y 都是 5 座")
    assert explainer.beat_subject(spec, PAIR, "x") == PAIR[1]


# --------------------------------------------------------------------------
# 槽点争议:争议材料带专门的抽取规则
# --------------------------------------------------------------------------
def test_controversy_rule_only_for_controversy_material():
    docs = [{"tool": "search_news", "query": "小米YU7 争议 槽点 投诉 质疑 车主吐槽 回应"}]
    assert intake.has_controversy(docs)
    assert not intake.has_controversy([{"tool": "search_news", "query": "小米YU7 车型 参数"}])


def test_controversy_fact_tagged_in_prompt(auto):
    llm = _ScriptLLM([])
    f = _fact("f_c", claim="有车主反映冬季续航只有标称六成")
    f.topic = "争议"
    script_gen.generate(subject="小米YU7", profile=auto, facts=[f],
                        plan=list(auto.plan_for(120, "debate")), duration=120, llm=llm,
                        theme=auto.theme_spec("debate"))
    assert "f_c [product · 争议]" in llm.calls[-1][0]
