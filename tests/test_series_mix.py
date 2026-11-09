"""栏目片发布后加进抖音同名合集:挑哪条、什么时候试、结果怎么记。"""
from __future__ import annotations

import time

import pytest

from app.models import ProjectCreate


@pytest.fixture
def fresh_db(tmp_path, monkeypatch):
    from app import config, db

    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(config, "PROJECTS_DIR", tmp_path / "data" / "projects")
    monkeypatch.setattr(db, "_INITIALISED", False)
    monkeypatch.setattr(config, "DOUYIN_COLLECTIONS", True)
    return db


def _pub(db, pid, *, pub_id="pub_1", platform="douyin", state="submitted",
         published_at=None, piece="", title="小米YU7 25万起,值不值"):
    db.create_publication({
        "id": pub_id, "project_id": pid, "account_id": "acc", "platform": platform,
        "state": state, "piece": piece, "title": title,
        "published_at": time.time() - 3600 if published_at is None else published_at})


def _project(series="柒哥帮你挑"):
    from app.pipeline import orchestrator

    return orchestrator.create_project(ProjectCreate(subject="小米YU7", series=series or None))


def test_search_key_and_card_count():
    from app.publish.platforms.douyin import collection

    assert collection.search_key("小米YU7 25万起,值不值 #小米YU7") == "小米YU725万起,值不"
    assert collection.card_count("柒哥帮你挑\n3个作品") == 3
    assert collection.card_count("没有数字") is None


def test_adds_once_and_records(fresh_db):
    from app.team import series_mix

    pid = _project()
    _pub(fresh_db, pid)
    calls = []

    def adder(name, title):
        calls.append((name, title))
        return "added", "已加进合集"

    assert series_mix.run_once(adder)
    assert calls == [("柒哥帮你挑", "小米YU7 25万起,值不值")]
    st = fresh_db.read_json(pid, series_mix.STATE_FILE)
    assert st["result"] == "added" and st["attempts"] == 1
    # 加过了就不再碰
    assert series_mix.run_once(adder) == ""
    assert len(calls) == 1


def test_skips_fresh_non_douyin_pieces_and_no_series(fresh_db):
    from app.team import series_mix

    _pub(fresh_db, _project(), pub_id="p_new", published_at=time.time())
    _pub(fresh_db, _project(), pub_id="p_bili", platform="bilibili")
    _pub(fresh_db, _project(), pub_id="p_clip", piece="clip1")
    _pub(fresh_db, _project(), pub_id="p_pending", state="pending")
    _pub(fresh_db, _project(series=""), pub_id="p_noseries")
    assert series_mix.due() == []


def test_not_found_retries_after_gap_then_gives_up(fresh_db):
    from app.team import series_mix

    pid = _project()
    _pub(fresh_db, pid)
    now = time.time()
    for i in range(series_mix.MAX_ATTEMPTS):
        t = now + i * series_mix.RETRY_GAP
        assert series_mix.run_once(lambda n, t_: ("not_found", "搜不到"), now=t)
        # 间隔没到不重试
        assert series_mix.due(now=t + 60) == []
    assert series_mix.due(now=now + 99 * series_mix.RETRY_GAP) == []


def test_switch_off(fresh_db, monkeypatch):
    from app import config
    from app.team import series_mix

    _pub(fresh_db, _project())
    monkeypatch.setattr(config, "DOUYIN_COLLECTIONS", False)
    assert series_mix.run_once(lambda n, t: ("added", "")) == ""
