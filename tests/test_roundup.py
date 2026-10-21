"""多车横评(roundup):车单 → 每车各自事实 → 按车分段的脚本 → 按车核对的分镜。

不需要网络与额度:事实来源、LLM 全部用假的。
"""
from __future__ import annotations

import pytest

from app.categories import Slot, load_profile
from app.models import Fact, ProjectCreate, ScriptSegment, ShotSpec, SubjectEntry
from app.pipeline import explainer, intake, script_gen
from app.providers import facts as facts_provider

CARS = [{"name": "小米YU7", "group": "25-30万"}, {"name": "特斯拉Model Y", "group": ""},
        {"name": "问界M7", "group": ""}]
NAMES = [c["name"] for c in CARS]


@pytest.fixture
def fresh_db(tmp_path, monkeypatch):
    from app import config, db

    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(config, "PROJECTS_DIR", tmp_path / "data" / "projects")
    monkeypatch.setattr(db, "_INITIALISED", False)
    return db


# --------------------------------------------------------------------------
# 建项目
# --------------------------------------------------------------------------
def test_create_roundup_stores_subjects(fresh_db):
    from app.pipeline import orchestrator

    pid = orchestrator.create_project(ProjectCreate(
        subject="25 万级纯电 SUV 横评", theme="roundup", duration=240,
        subjects=[SubjectEntry(name=" 小米YU7 ", group="25-30万"),
                  SubjectEntry(name="特斯拉Model Y"), SubjectEntry(name="问界M7")]))
    row = fresh_db.get_project(pid)
    assert fresh_db.project_subjects(row) == CARS
    assert row["title"] == "25 万级纯电 SUV 横评"
    assert row["shot_count"] == 30


def test_buyguide_ignores_subjects(fresh_db):
    from app.pipeline import orchestrator

    pid = orchestrator.create_project(ProjectCreate(
        subject="小米YU7", subjects=[SubjectEntry(name="问界M7")]))
    assert fresh_db.project_subjects(fresh_db.get_project(pid)) == []


def test_roundup_needs_subjects(fresh_db):
    from app.pipeline import orchestrator
    from app.pipeline.orchestrator import PipelineError

    with pytest.raises(PipelineError, match="3–5 台车"):
        orchestrator.create_project(ProjectCreate(subject="横评", theme="roundup"))


# --------------------------------------------------------------------------
# 事实:每车各采各抽
# --------------------------------------------------------------------------
class _ExtractLLM:
    """按 user 里的 Product subject 返回那台车的事实;问界M7 抽取直接失败。"""

    def __init__(self) -> None:
        self.subjects: list[str] = []

    def structured(self, system, user, **_):
        name = user.split("\n", 1)[0].removeprefix("Product subject: ").strip()
        self.subjects.append(name)
        if name == "问界M7":
            raise RuntimeError("rate limited")
        return {"facts": [
            {"claim": f"{name} CLTC 续航 {700 + i} 公里", "source_tool": "t",
             "source_query": name, "confidence": "high", "topic": "续航"}
            for i in range(3)
        ]}


def test_run_roundup_tags_facts_per_car(tmp_path, monkeypatch):
    from app import config, db

    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(config, "PROJECTS_DIR", tmp_path / "data" / "projects")
    seen = []

    def fake_collect(profile, subject, project_dir, theme=None):
        seen.append(subject)
        docs = [facts_provider.RawDoc(tool="t", query=subject, retrieved_at="2026-10-21",
                                      text="正文" * 50)]
        if subject == "特斯拉Model Y":  # 一个来源挂了,另一个还在
            docs.append(facts_provider.RawDoc(tool="t2", query=subject, retrieved_at="2026-10-21",
                                              text="", ok=False, error="down"))
        return facts_provider.CollectionResult(docs=docs)

    monkeypatch.setattr(intake, "collect_raw", fake_collect)
    llm = _ExtractLLM()
    profile = load_profile("auto_long")
    facts, report = intake.run_roundup("p_rr", profile, CARS, theme="roundup", llm=llm)

    assert seen == NAMES
    assert {f.subject for f in facts} == {"小米YU7", "特斯拉Model Y"}
    assert all(f.subject for f in facts)
    assert report["per_subject"]["小米YU7"]["product"] == 3
    assert report["per_subject"]["问界M7"]["facts"] == 0
    assert report["subjects_thin"] == ["问界M7"]
    assert "问界M7" in report["extract_error"]
    assert report["per_subject"]["特斯拉Model Y"]["sources_failed"]
    saved = db.read_json("p_rr", "facts.json")
    assert {f["subject"] for f in saved} == {"小米YU7", "特斯拉Model Y"}


# --------------------------------------------------------------------------
# 脚本
# --------------------------------------------------------------------------
def _facts() -> list[Fact]:
    return [
        Fact(id="f_a", claim="小米YU7 CLTC 续航 835 公里", source_tool="t", confidence="high",
             scope="product", subject="小米YU7"),
        Fact(id="f_b", claim="特斯拉Model Y 长续航版 CLTC 续航 719 公里", source_tool="t",
             confidence="high", scope="product", subject="特斯拉Model Y"),
        Fact(id="f_c", claim="问界M7 售价 24.98 万元", source_tool="t", confidence="high",
             scope="product", subject="问界M7"),
    ]


class _Capture:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def structured(self, system, user, required_keys=(), **_):
        self.calls.append((system, user))
        return {k: [] if k != "title" else "t" for k in required_keys}


def test_roundup_script_prompt_groups_facts_and_marks_cars():
    profile = load_profile("auto_long")
    theme = profile.theme_spec("roundup")
    slots = profile.slots_for(240, "roundup", NAMES)
    llm = _Capture()
    script_gen.generate(subject="横评", profile=profile, facts=_facts(),
                        plan=[s.section for s in slots], duration=240, llm=llm, theme=theme,
                        slots=slots, subjects=CARS)
    system, user = llm.calls[-1]
    assert "车单:小米YU7(25-30万)、特斯拉Model Y、问界M7\n" in user
    assert "## 小米YU7(25-30万)\n- f_a" in system
    assert system.index("## 小米YU7") < system.index("## 特斯拉Model Y") < system.index("## 问界M7")
    assert "车型亮相〔小米YU7〕" in system
    assert script_gen.ROUNDUP_SCOPE_LINE in system
    assert "只讲这台车/这个品牌本身" not in system


def test_normalize_segments_writes_subject_from_slots():
    slots = [Slot("横评开场"), Slot("车型亮相", "小米YU7"), Slot("车型亮相", "问界M7")]
    segs = [ScriptSegment(idx=i, section="x", text="一段旁白", subject="乱写的") for i in range(2)]
    out = script_gen.normalize_segments(segs, slots)
    assert [s.subject for s in out] == ["", "小米YU7", "问界M7"]
    assert out[2].section == "车型亮相"


def test_lint_flags_cross_car_fact_refs():
    profile = load_profile("auto_long")
    theme = profile.theme_spec("roundup")
    segs = [
        ScriptSegment(idx=0, section="横评开场", text="续航 835 公里对 719 公里,差距不小",
                      fact_refs=["f_a", "f_b"]),
        ScriptSegment(idx=1, section="核心卖点", text="它的续航有 719 公里", fact_refs=["f_b"],
                      subject="小米YU7"),
        ScriptSegment(idx=2, section="核心卖点", text="它的续航有 835 公里", fact_refs=["f_a"],
                      subject="小米YU7"),
    ]
    issues = script_gen.compliance_lint(segs, profile, _facts(), theme=theme)
    cross = [i for i in issues if "却引用了" in i.message]
    assert len(cross) == 1 and cross[0].segment_idx == 1 and cross[0].is_error
    assert "特斯拉Model Y" in cross[0].message


# --------------------------------------------------------------------------
# 分镜与渲染取材
# --------------------------------------------------------------------------
class _BeatsLLM:
    def __init__(self, beats) -> None:
        self.beats = beats
        self.system = ""

    def structured(self, system, user, **_):
        self.system, self.user = system, user
        return {"beats": self.beats}


def test_build_beats_grounds_against_own_car_only():
    profile = load_profile("auto_long")
    segs = [
        ScriptSegment(idx=0, section="横评开场", text="三台车,续航差了一百多公里"),
        ScriptSegment(idx=1, section="核心卖点", text="续航 835 公里", subject="小米YU7"),
        ScriptSegment(idx=2, section="核心卖点", text="续航 835 公里", subject="特斯拉Model Y"),
    ]
    stat = {"label": "续航", "value": "835公里", "fact_id": "f_a"}
    llm = _BeatsLLM([{"idx": 2, "layout": "stat", "stat": stat},
                     {"idx": 3, "layout": "stat", "stat": stat}])
    specs = explainer.build_beats(subject="横评", profile=profile, segments=segs,
                                  facts=_facts(), llm=llm, subjects=CARS)
    assert specs[1].layout == "stat" and specs[1].subject == "小米YU7"
    # 特斯拉的拍引用了小米的事实 → 核不过,降级
    assert specs[2].layout == "broll" and specs[2].subject == "特斯拉Model Y"
    assert [s.chapter for s in specs] == ["开头", "小米YU7", "特斯拉Model Y"]
    assert "〔小米YU7〕" in llm.user and "[特斯拉Model Y]" in llm.user
    assert "多车横评" in llm.system


def test_beat_subject_rotates_shared_beats():
    def spec(i, car=""):
        return ShotSpec(idx=i, visual_zh="x", subject=car)

    assert explainer.beat_subject(spec(4, "问界M7"), NAMES, "横评") == "问界M7"
    assert [explainer.beat_subject(spec(i), NAMES, "横评") for i in range(4)] == \
        ["小米YU7", "特斯拉Model Y", "问界M7", "小米YU7"]
    assert explainer.beat_subject(spec(0), [], "小米YU7") == "小米YU7"


def test_render_pending_uses_each_beats_car(fresh_db, monkeypatch):
    fresh_db.create_project({
        "id": "p_rp", "title": "t", "subject": "横评", "category": "auto_long",
        "theme": "roundup", "subjects": CARS, "duration": 240, "shot_count": 2,
        "narration_mode": "tts", "state": "DRAFT_RENDERING", "state_reason": None,
        "quota_budget": 100.0, "spent_cost": 0.0, "est_cost": 1.0,
        "created_at": 1.0, "updated_at": 2.0,
    })
    asked: list[str] = []
    monkeypatch.setattr(explainer, "card_backgrounds", lambda car: asked.append(car) or [])
    rendered: list[tuple[int, str]] = []

    def fake_render(spec, *, subject, out_dir, **_):
        rendered.append((spec.idx, subject))
        out_dir.mkdir(parents=True, exist_ok=True)
        path = out_dir / f"{spec.idx:03d}.mp4"
        path.write_bytes(b"x")
        return path, []

    monkeypatch.setattr(explainer, "render_beat", fake_render)
    specs = [ShotSpec(idx=0, visual_zh="x", duration=3.0, chapter="开头"),
             ShotSpec(idx=1, visual_zh="x", duration=3.0, subject="问界M7", chapter="问界M7")]
    explainer.init_beats("p_rp", specs)
    explainer.render_pending("p_rp", specs, subject="横评", profile=load_profile("auto_long"),
                             limit=5, subjects=NAMES)
    assert rendered == [(0, "小米YU7"), (1, "问界M7")]
    assert asked == ["小米YU7", "问界M7"]


def test_categories_meta_exposes_roundup():
    from app.categories import available_categories

    auto = next(c for c in available_categories() if c["name"] == "auto_long")
    roundup = next(t for t in auto["themes"] if t["name"] == "roundup")
    assert roundup["roundup"] is True and roundup["subject_count"] == [3, 5]


def test_run_roundup_angle_search_and_evidence_per_car(tmp_path, monkeypatch):
    from app import config

    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(config, "PROJECTS_DIR", tmp_path / "data" / "projects")
    monkeypatch.setattr(intake, "collect_raw", lambda *a, **k: facts_provider.CollectionResult())
    searched = []

    def fake_news(subject, project_dir=None, size=8, days=540, terms=""):
        searched.append((subject, days, terms))
        return facts_provider.RawDoc(tool="t", query=terms, retrieved_at="2026-10-21", text="x" * 300)

    monkeypatch.setattr(facts_provider, "ifind_news", fake_news)
    users = []

    class LLM:
        def structured(self, system, user, **_):
            users.append(user)
            return {"facts": []}

    intake.run_roundup("p_ra", load_profile("auto_long"), CARS, theme="roundup",
                       angle="月销前三谁更值", angle_search="SUV 销量 9月",
                       evidence=["[懂车帝] 销量榜第 1 小米YU7", "[懂车帝] 销量榜第 3 问界M7"], llm=LLM())
    assert searched == [(n, 90, "SUV 销量 9月") for n in NAMES]
    assert all(intake.HOT_TOPIC_TOOL in u and "销量榜第 3 问界M7" in u for u in users)
    # 抽取每台车时点名跳过其它车
    assert "Only extract facts about 小米YU7" in users[0] and "问界M7" in users[0].split("Only extract")[1]
