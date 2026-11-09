"""栏目:选题类型 → 栏目名、期号自增、返工沿用期号、结尾 / 封面 / 提示词。"""
from __future__ import annotations

import datetime as dt

import pytest

from app.models import ProjectCreate


@pytest.fixture
def fresh_db(tmp_path, monkeypatch):
    from app import config, db

    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(config, "PROJECTS_DIR", tmp_path / "data" / "projects")
    monkeypatch.setattr(db, "_INITIALISED", False)
    return db


def test_vocab_has_series():
    from app.categories import load_profile
    from app.pipeline import series

    vocab = load_profile("auto_long").vocab
    assert series.name_for(vocab, "band") == "柒哥帮你挑"
    assert series.name_for(vocab, "") == ""


def test_episode_increments_and_redo_keeps(fresh_db):
    from app.pipeline import orchestrator, variety

    a = orchestrator.create_project(ProjectCreate(subject="小米YU7", series="柒哥帮你挑"))
    b = orchestrator.create_project(ProjectCreate(subject="理想i6", series="柒哥帮你挑"))
    c = orchestrator.create_project(ProjectCreate(subject="理想i6", series="柒哥聊热点"))
    redo = orchestrator.create_project(ProjectCreate(subject="理想i6", series="柒哥帮你挑",
                                                     episode=2))
    va, vb, vc, vr = (variety.parse(fresh_db.get_project(p)["variant"]) for p in (a, b, c, redo))
    assert (va["episode"], vb["episode"], vc["episode"], vr["episode"]) == (1, 2, 1, 2)
    assert va["series"] == "柒哥帮你挑"


def test_series_forces_next_episode_ending(fresh_db):
    from app.categories import load_profile
    from app.pipeline import orchestrator, variety

    theme = load_profile("auto_long").theme_spec(load_profile("auto_long").default_theme)
    pid = orchestrator.create_project(ProjectCreate(subject="小米YU7", series="柒哥一分钟看车"))
    v = variety.parse(fresh_db.get_project(pid)["variant"])
    if "next_episode" in variety.allowed_endings(theme):
        assert v["ending"] == "next_episode"


def test_no_series_variant_unchanged(fresh_db):
    from app.pipeline import orchestrator, variety

    pid = orchestrator.create_project(ProjectCreate(subject="小米YU7"))
    assert "series" not in variety.parse(fresh_db.get_project(pid)["variant"])


def test_rule10_mentions_series_only_when_set():
    from app.pipeline import variety

    assert "栏目" not in variety.rule10({})
    text = variety.rule10({"ending": "next_episode", "series": "柒哥帮你挑", "episode": 3})
    assert "柒哥帮你挑" in text and "第 3 期" in text


def test_chip():
    from app.pipeline import series

    assert series.chip({"series": "柒哥帮你挑", "episode": 3}) == "柒哥帮你挑·第3期"
    assert series.chip({}) == ""


def test_to_payload_picks_series_by_weekday(fresh_db, monkeypatch):
    from app import config
    from app.categories import load_profile
    from app.team import plan

    monkeypatch.setattr(config, "TEAM_WEEK_PLAN",
                        ("model", "band", "model", "topic", "model", "band", "topic"))
    tuesday = dt.datetime(2026, 11, 10, 18, 30).timestamp()  # 周二 = band
    profile = load_profile("auto_long")
    fresh_db.create_plan_item({
        "id": "pl_1", "category": "auto_long", "slot_at": tuesday, "subject": "小米YU7",
        "theme": profile.default_theme, "aspect": "9:16", "duration": 60})
    payload = plan.to_payload(fresh_db.get_plan_item("pl_1"))
    assert payload.series == "柒哥帮你挑"
    assert payload.episode is None
