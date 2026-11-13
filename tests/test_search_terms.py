"""搜索词进选题;限流连续两条才暂停;数据点延到 30 天。"""
from __future__ import annotations

import time

import pytest


@pytest.fixture
def fresh_db(tmp_path, monkeypatch):
    from app import config, db

    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(config, "PROJECTS_DIR", tmp_path / "data" / "projects")
    monkeypatch.setattr(db, "_INITIALISED", False)
    return db


def test_terms_accumulate_and_feed_the_editor(fresh_db):
    from app.team import search_terms

    assert search_terms.prompt_text() == ""
    n = search_terms.record([{"term": "元UP 落地价", "views": 320, "title": "比亚迪元UP,该不该买"},
                             {"term": "腾势N8和理想L8对比", "views": 900}, {"term": "x"}])
    assert n == 2
    search_terms.record([{"term": "元UP 落地价", "views": 80}])
    top = search_terms.top()
    assert [t["term"] for t in top] == ["腾势N8和理想L8对比", "元UP 落地价"]
    assert top[1]["views"] == 400
    text = search_terms.prompt_text()
    assert "腾势N8和理想L8对比" in text and "400 次播放" in text
    # 过期的不再给
    assert search_terms.prompt_text(now=time.time() + 60 * 86400) == ""


def test_analyst_keeps_sampling_to_30_days():
    from app.team import analyst

    t0 = 1_800_000_000.0
    pub = {"published_at": t0}
    week = [{"hours_since_publish": 168.5}]
    assert analyst.due_milestone(pub, week, t0 + 15 * 86400.0) == 336.0
    assert analyst.due_milestone(pub, week, t0 + 30.5 * 86400.0) == 720.0
    assert analyst.due_milestone(pub, [{"hours_since_publish": 721}], t0 + 30.5 * 86400.0) is None


def test_title_may_run_to_30_chars():
    from app.pipeline import packaging as pk

    title = "比亚迪元UP和吉利星愿对比,预算8万到底该选谁"
    assert 20 < pk.spoken_len(title) <= pk.TITLE_MAX_CHARS
    assert not pk.check_title(title, allowed={"8"}, name="比亚迪元UP")
