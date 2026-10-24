"""价位段选购(priceband):竖屏段落、选题规则、指导价核价位段。不需要网络与额度。"""
from __future__ import annotations

import pytest

from app import config
from app.categories import available_categories, load_profile
from app.models import ProjectCreate, SubjectEntry
from app.pipeline import grounding, intake, topics
from app.providers import car_lineup
from app.providers import facts as facts_provider

CARS = ["比亚迪秦L EV", "小鹏MONA M03", "零跑B10"]


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


def _sections(slots):
    out = []
    for s in slots:
        if not out or out[-1] != (s.section, s.subject):
            out.append((s.section, s.subject))
    return out


def test_theme_shape(auto):
    t = auto.theme_spec("priceband")
    assert t.roundup and t.multi and t.requires_angle and not t.versus
    assert t.subject_count == (3, 4)


@pytest.mark.parametrize("duration", [90, 120])
@pytest.mark.parametrize("n", [3, 4])
def test_portrait_keeps_every_car_and_conclusion(auto, duration, n):
    slots = auto.slots_for(duration, "priceband", CARS[:n] + ["极狐T1"][: n - 3], portrait=True)
    assert len(slots) == auto.shots_for(duration)
    secs = _sections(slots)
    assert secs[0] == ("价位段对比开场", "") and secs[-1] == ("价位段对比结论", "")
    # 每台车都有「车型定位」,竖屏短档位砍掉「标准对照」
    assert [s for s in secs if s[1]] == [("车型定位", c) for c in (CARS + ["极狐T1"])[:n]]


def test_long_duration_adds_standard_section(auto):
    secs = _sections(auto.slots_for(240, "priceband", CARS))
    assert ("标准对照", CARS[0]) in secs


def test_create_requires_band_angle(fresh_db):
    from app.pipeline import orchestrator
    from app.pipeline.orchestrator import PipelineError

    subjects = [SubjectEntry(name=c) for c in CARS]
    with pytest.raises(PipelineError, match="价位段"):
        orchestrator.create_project(ProjectCreate(
            subject="10-15万纯电车型怎么选", theme="priceband", duration=90,
            aspect=config.PORTRAIT, subjects=subjects))
    pid = orchestrator.create_project(ProjectCreate(
        subject="10-15万纯电车型怎么选", theme="priceband", duration=90,
        aspect=config.PORTRAIT, angle="10-15万纯电", subjects=subjects))
    row = fresh_db.get_project(pid)
    assert [s["name"] for s in fresh_db.project_subjects(row)] == CARS


def test_price_band_parse():
    assert grounding.price_band("10-15万纯电") == (10.0, 15.0)
    assert grounding.price_band("15~10 万 纯电 SUV") == (10.0, 15.0)
    assert grounding.price_band("12万到18万") == (12.0, 18.0)
    assert grounding.price_band("15万以内纯电") == (0.0, 15.0)
    assert grounding.price_band("续航怎么选") is None


def test_band_check_from_lineup_text():
    text = car_lineup.render("零跑B10", [
        {"year": 2026, "series": "零跑B10", "name": "510 舒享版", "price": "11.98万", "drive": "后驱"},
        {"year": 2026, "series": "零跑B10", "name": "600 智享版", "price": "16.18万", "drive": "后驱"},
    ], "2026-10-24")
    docs = [facts_provider.RawDoc(tool=car_lineup.TOOL, query="零跑B10",
                                  retrieved_at="2026-10-24", text=text)]
    prices = intake.guide_prices(docs)
    assert prices == [11.98, 16.18]
    assert intake.band_check(prices, (10, 15)) == {
        "price_range": "11.98-16.18万", "in_band": True, "over_band": 1}
    assert intake.band_check([25.0], (10, 15))["in_band"] is False


def test_run_roundup_flags_off_band_car(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(config, "PROJECTS_DIR", tmp_path / "data" / "projects")
    prices = {"比亚迪秦L EV": "11.98万", "小鹏MONA M03": "11.98万", "零跑B10": "22.98万"}

    def fake_collect(profile, subject, project_dir, theme=None):
        text = f"- 2026款 {subject} 标准版:厂商指导价 {prices[subject]},前驱"
        return facts_provider.CollectionResult(docs=[facts_provider.RawDoc(
            tool=car_lineup.TOOL, query=subject, retrieved_at="2026-10-24", text=text)])

    monkeypatch.setattr(intake, "collect_raw", fake_collect)
    monkeypatch.setattr(facts_provider, "ifind_news", lambda name, *a, **k: facts_provider.RawDoc(
        tool="ifind.news", query=name, retrieved_at="2026-10-24", text="", ok=False, error="off"))
    _, report = intake.run_roundup("p_pb", load_profile("auto_long"),
                                   [{"name": c} for c in CARS], theme="priceband",
                                   angle="10-15万纯电")
    assert report["subjects_off_band"] == ["零跑B10"]
    assert "零跑B10(22.98-22.98万)" in report["band_note"]
    assert report["per_subject"]["比亚迪秦L EV"]["in_band"] is True


def test_roundup_without_band_has_no_band_keys(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(config, "PROJECTS_DIR", tmp_path / "data" / "projects")
    monkeypatch.setattr(intake, "collect_raw", lambda *a, **k: facts_provider.CollectionResult(docs=[]))
    _, report = intake.run_roundup("p_pb2", load_profile("auto_long"),
                                   [{"name": c} for c in CARS], theme="roundup")
    assert "subjects_off_band" not in report


class _LLM:
    def __init__(self, out):
        self.out, self.calls = out, []

    def structured(self, system, user, required_keys=(), **_):
        self.calls.append(system)
        return {"topics": self.out}


def test_topics_prompt_has_band_rule_and_keeps_roundup_rule():
    llm = _LLM([])
    trends = [{"source": "dongchedi.sales", "kind": "model", "title": "销量榜第 1",
               "car": "小鹏MONA M03", "metric": "", "url": "", "date": "2026-10"}]
    topics.suggest(trends, profile=load_profile("auto_long"), llm=llm, n=6)
    system = llm.calls[0]
    assert "theme=roundup" in system and "3-5 款车" in system
    assert "theme=priceband" in system and "3-4 款" in system
    # 价位段选购必须写角度,但不是单点深挖那种"只讲一个点"
    assert "theme=priceband 的 angle 写这期只讲" not in system
    assert topics.roundup_theme(load_profile("auto_long")).name == "roundup"


def test_categories_api_sends_roster_wording():
    cat = next(c for c in available_categories() if c["name"] == "auto_long")
    themes = {t["name"]: t for t in cat["themes"]}
    assert themes["priceband"]["roster_label"] == "价位段选题"
    assert themes["priceband"]["roundup"] and themes["priceband"]["max_subjects"]
    assert themes["roundup"]["roster_label"] == ""


def test_strip_band_only_removes_the_angle_band():
    band = (10.0, 15.0)
    assert grounding.strip_band("10-15万纯电怎么选", band) == "纯电怎么选"
    assert grounding.strip_band("10到15万,先看补能", band) == ",先看补能"
    # 别的价格不是选题本身,留着让 lint 查出处
    assert "11.98" in grounding.strip_band("11.98万到13.98万", band)
    assert grounding.strip_band("10-15万", None) == "10-15万"


def test_lint_lets_band_digits_through_but_not_others(auto):
    from app.models import ScriptSegment
    from app.pipeline import script_gen

    theme = auto.theme_spec("priceband")
    segs = [ScriptSegment(idx=0, section="价位段对比开场", text="10-15万纯电,最怕买错版本。"),
            ScriptSegment(idx=1, section="价位段对比开场", text="补能看30%-80%快充时间。")]

    def numeric_errors(angle):
        issues = script_gen.compliance_lint(segs, auto, [], theme=theme, angle=angle)
        return {i.segment_idx for i in issues if i.is_error and "事实引用" in i.message}

    assert numeric_errors("") == {0, 1}
    assert numeric_errors("10-15万纯电") == {1}
