"""选题角度(angle):从建项目一路贯穿到事实检索与脚本 prompt。

- 无角度时 prompt 与旧版逐字一致(golden 快照由 test_profile_vocab 守着)。
- 有角度时 SYSTEM / user 都带上它,intake 额外补一次近期定向检索。
不需要网络与额度。
"""
from __future__ import annotations

import sqlite3

import pytest

from app.categories import load_profile
from app.models import Fact, ProjectCreate
from app.pipeline import intake, script_gen
from app.providers import facts as facts_provider


class _Capture:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def structured(self, system, user, required_keys=(), **_):
        self.calls.append((system, user))
        return {k: [] if k != "title" else "t" for k in required_keys}


def _facts() -> list[Fact]:
    return [Fact(id="f_1", claim="示例参数 835", source_tool="t", confidence="high",
                 scope="product", retrieved_at="2026-01-01")]


def _prompts(angle: str) -> tuple[str, str]:
    profile = load_profile("auto_long")
    plan = list(profile.plan_for(profile.supported_durations()[0]))
    llm = _Capture()
    script_gen.generate(subject="小米YU7", profile=profile, facts=_facts(), plan=plan,
                        duration=profile.supported_durations()[0], llm=llm,
                        theme=profile.theme_spec(), angle=angle)
    return llm.calls[-1]


def test_empty_angle_prompt_unchanged():
    assert _prompts("") == _prompts("   ")
    system, user = _prompts("")
    assert "选题角度" not in system
    assert "Topic angle" not in user


def test_angle_reaches_script_prompt():
    system, user = _prompts("月销破 3 万背后:它到底赢在哪")
    assert "**选题角度**:本期选题是「月销破 3 万背后:它到底赢在哪」" in system
    assert "Topic angle: 月销破 3 万背后:它到底赢在哪\n" in user
    # 角度规则排在既有规则之后,不打断事实清单的位置
    assert system.index("选题角度") < system.index("可用事实清单")


def test_angle_terms_strips_punctuation_and_fillers():
    assert intake.angle_terms("月销破 3 万背后:它到底赢在哪?") == "月销破 3 万 赢在哪"
    assert intake.angle_terms("") == ""
    assert len(intake.angle_terms("官降" * 50)) <= 40


def test_intake_adds_angle_search(tmp_path, monkeypatch):
    from app import config

    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(config, "PROJECTS_DIR", tmp_path / "data" / "projects")
    monkeypatch.setattr(intake, "collect_raw",
                        lambda *a, **k: facts_provider.CollectionResult())
    calls = []

    def fake_news(subject, project_dir=None, size=8, days=540, terms=""):
        calls.append((subject, days, terms))
        return facts_provider.RawDoc(tool="t", query=terms, retrieved_at="2026-10-21",
                                     text="", ok=True)

    monkeypatch.setattr(facts_provider, "ifind_news", fake_news)
    profile = load_profile("auto_long")
    intake.run("p_angle", profile, "小米YU7", angle="官降 2 万:现在能买吗")
    assert calls == [("小米YU7", 90, "官降 2 万 现在能买吗")]

    calls.clear()
    intake.run("p_angle2", profile, "小米YU7")
    assert calls == []


@pytest.fixture
def fresh_db(tmp_path, monkeypatch):
    from app import config, db

    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(config, "PROJECTS_DIR", tmp_path / "data" / "projects")
    monkeypatch.setattr(db, "_INITIALISED", False)
    return db


def test_create_project_stores_angle(fresh_db):
    from app.pipeline import orchestrator

    pid = orchestrator.create_project(
        ProjectCreate(subject="小米YU7", angle=" 月销破 3 万背后 ")
    )
    assert fresh_db.get_project(pid)["angle"] == "月销破 3 万背后"
    pid2 = orchestrator.create_project(ProjectCreate(subject="小米YU7"))
    assert fresh_db.get_project(pid2)["angle"] == ""


def test_old_db_gets_angle_column(fresh_db, tmp_path):
    from app import config

    config.ensure_dirs()
    path = config.DATA_DIR / "aimovice.db"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE projects (id TEXT PRIMARY KEY, title TEXT NOT NULL, "
                 "subject TEXT NOT NULL, category TEXT, duration INTEGER NOT NULL, "
                 "shot_count INTEGER, narration_mode TEXT, state TEXT NOT NULL, "
                 "state_reason TEXT, quota_budget REAL, spent_cost REAL, est_cost REAL, "
                 "created_at REAL NOT NULL, updated_at REAL NOT NULL)")
    conn.commit()
    conn.close()
    fresh_db.init_db()
    with fresh_db.connect() as c:
        assert "angle" in fresh_db._column_names(c, "projects")


def test_api_returns_angle(fresh_db, monkeypatch):
    from fastapi.testclient import TestClient

    from app import main

    monkeypatch.setattr(main, "_bootstrap", lambda *a, **k: None, raising=False)
    fresh_db.init_db()
    fresh_db.create_project({
        "id": "p_api_angle", "title": "t", "subject": "小米YU7", "category": "auto_long",
        "theme": "buyguide", "angle": "官降 2 万", "duration": 240, "shot_count": 27,
        "narration_mode": "tts", "state": "GATE_A", "state_reason": None,
        "quota_budget": 100.0, "spent_cost": 0.0, "est_cost": 1.0,
        "created_at": 1.0, "updated_at": 2.0,
    })
    client = TestClient(main.app)
    assert client.get("/api/projects/p_api_angle").json()["angle"] == "官降 2 万"


def test_intake_uses_search_terms_and_evidence(tmp_path, monkeypatch):
    from app import config

    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(config, "PROJECTS_DIR", tmp_path / "data" / "projects")
    monkeypatch.setattr(intake, "collect_raw",
                        lambda *a, **k: facts_provider.CollectionResult())
    terms = []
    monkeypatch.setattr(facts_provider, "ifind_news",
                        lambda subject, project_dir=None, size=8, days=540, terms_="", **k:
                        terms.append(k.get("terms")) or facts_provider.RawDoc(
                            tool="t", query="q", retrieved_at="d", text="", ok=True))
    seen = {}

    class _LLM:
        def structured(self, system, user, **_):
            seen["user"] = user
            return {"facts": []}

    intake.run("p_ev", load_profile("auto_long"), "特斯拉Model Y", angle="召回后还值得买吗",
               angle_search="特斯拉 Model Y 召回", evidence=["[dongchedi.sales] 销量榜第 1 名 | 指标:月销 5.0 万"],
               llm=_LLM())
    assert terms == ["特斯拉 Model Y 召回"]
    assert intake.HOT_TOPIC_TOOL in seen["user"]
    assert "- [dongchedi.sales] 销量榜第 1 名 | 指标:月销 5.0 万" in seen["user"]


def test_create_project_saves_topic(fresh_db):
    from app.pipeline import orchestrator

    pid = orchestrator.create_project(ProjectCreate(
        subject="特斯拉Model Y", angle="召回后还值得买吗",
        angle_search="特斯拉 召回", angle_evidence=["a", "b"]))
    assert fresh_db.read_json(pid, "topic.json") == {
        "angle": "召回后还值得买吗", "search": "特斯拉 召回", "evidence": ["a", "b"]}
    pid2 = orchestrator.create_project(ProjectCreate(subject="特斯拉Model Y", angle_search="x"))
    assert fresh_db.read_json(pid2, "topic.json") is None


def test_hot_topic_facts_are_product_scope():
    class _LLM:
        def structured(self, system, user, **_):
            return {"facts": [{"claim": "2026 年 9 月 Model Y 月销量 5.0 万辆,位居销量榜第 1 名",
                               "source_tool": intake.HOT_TOPIC_TOOL, "source_query": "q",
                               "confidence": "high", "topic": "销量"}]}

    got, _ = intake.extract_facts("ctx", subject="特斯拉Model Y", category="汽车", llm=_LLM(),
                               profile=load_profile("auto_long"))
    assert got[0].scope == "product"
