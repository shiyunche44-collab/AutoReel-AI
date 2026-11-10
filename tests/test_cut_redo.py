"""成片打回后按意见重做:新开一条已批选题,意见进写稿依据,返工有上限。"""
from __future__ import annotations

import datetime as dt
import time

import pytest

from app.team import plan, publisher


@pytest.fixture
def fresh_db(tmp_path, monkeypatch):
    from app import config, db

    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(config, "PROJECTS_DIR", tmp_path / "data" / "projects")
    monkeypatch.setattr(db, "_INITIALISED", False)
    return db


def _review_item(db, item_id="pl_a", **extra):
    db.create_plan_item({"id": item_id, "category": "auto_long", "slot_at": time.time() + 86400,
                         "subject": "大众帕萨特", "theme": "buyguide", "aspect": "9:16",
                         "duration": 90, "rationale": "新款上市", "status": "review",
                         "project_id": "p_x", **extra})
    return item_id


def test_reject_without_redo_makes_no_new_item(fresh_db):
    pid = _review_item(fresh_db)
    out = publisher.reject_cut(pid, "素材不对")
    assert out["status"] == "rejected" and "redo_item" not in out
    assert [i["id"] for i in fresh_db.list_plan_items()] == [pid]


def test_redo_opens_approved_copy_with_feedback(fresh_db):
    pid = _review_item(fresh_db)
    out = publisher.reject_cut(pid, "开头用了美版旧款画面", redo=True)
    child = fresh_db.get_plan_item(out["redo_item"])
    assert child["status"] == "approved" and child["project_id"] == ""
    assert child["redo_of"] == pid and child["revision"] == 1
    assert child["subject"] == "大众帕萨特"
    # 返工排到最近一个来得及的发布时段,不晚于原时刻
    assert time.time() + 2 * 3600 - 5 <= child["slot_at"] <= out["slot_at"]
    assert "美版旧款" in plan.to_payload(child).angle_why
    assert plan.to_payload(child).angle_why.startswith("新款上市")
    assert fresh_db.get_plan_item(pid)["status"] == "rejected"


def test_redo_stops_at_limit(fresh_db, monkeypatch):
    monkeypatch.setattr(publisher.config, "TEAM_MAX_REVISIONS", 1)
    first = _review_item(fresh_db)
    child_id = publisher.reject_cut(first, "a", redo=True)["redo_item"]
    fresh_db.update_plan_item(child_id, status="review")
    out = publisher.reject_cut(child_id, "b", redo=True)
    assert out["redo_item"] == "" and "上限" in out["redo_stopped"]
    assert len(fresh_db.list_plan_items()) == 2


def _lib(*titles):
    from app.pipeline.library import Item, Library, Source

    lib = Library(subject="x")
    lib.sources = [Source(url=f"https://v/{n}", id=f"s{n}", title=t) for n, t in enumerate(titles)]
    lib.items = [Item(id=f"s{n}_0", kind="video", path="p", source=f"s{n}", url=f"https://v/{n}")
                 for n in range(len(titles))]
    return lib


def test_stale_title():
    from app.pipeline import library

    assert library.stale_title("2017 VW Passat Features", now_year=2026)
    assert not library.stale_title("2025 VW Passat", now_year=2026)
    assert not library.stale_title("Passat Easy Open Trunk", now_year=2026)  # 没年份:放行
    assert not library.stale_title("2017 Passat", now_year=2026, max_age=0)  # 0 = 不筛


def test_screen_drops_stale_and_banned():
    from app.pipeline import library

    lib = _lib("2017 Passat", "Passat 2026", "Passat trunk")
    assert [i.id for i in library.screen(lib, lib.items)] == ["s1_0", "s2_0"]
    with library.banned_materials(["https://v/2"]):
        assert [i.id for i in library.screen(lib, lib.items)] == ["s1_0"]
    with library.banned_materials(["s1_0"]):  # 连拼子段 id 也认
        assert [i.id for i in library.screen(lib, lib.items)] == ["s2_0"]
    assert [i.id for i in library.screen(lib, lib.items)] == ["s1_0", "s2_0"]  # 出了作用域恢复


def test_redo_excludes_materials_in_bad_ranges(fresh_db):
    from app import db

    pid = "p_bad"
    (db.config.PROJECTS_DIR / pid).mkdir(parents=True, exist_ok=True)
    db.write_json(pid, "storyboard.json", [{"idx": 0, "duration": 3}, {"idx": 1, "duration": 3},
                                           {"idx": 2, "duration": 3}])
    db.write_json(pid, "materials_used.json", {"pieces": [
        {"beat": 0, "item_id": "a_0~1", "source_url": "https://v/a"},
        {"beat": 1, "item_id": "b_0", "source_url": "https://v/b"},
        {"beat": 2, "item_id": "c_0", "source_url": "https://v/c"}]})
    item = _review_item(fresh_db, project_id=pid)
    out = publisher.reject_cut(item, "x", redo=True, bad_ranges=[[0, 4]])
    child = fresh_db.get_plan_item(out["redo_item"])
    assert child["exclude"] == ["a_0", "b_0", "https://v/a", "https://v/b"]
    # 再返工一次:上一版排除的继续带着
    fresh_db.update_plan_item(child["id"], status="review", project_id=pid)
    out = publisher.reject_cut(child["id"], "y", redo=True, bad_ranges=[[7, 9]])
    assert "https://v/a" in fresh_db.get_plan_item(out["redo_item"])["exclude"]
    assert "c_0" in fresh_db.get_plan_item(out["redo_item"])["exclude"]


def test_redo_failed_item_opens_copy(fresh_db):
    pid = _review_item(fresh_db, status="failed", note="制作失败:渲染挂了")
    out = publisher.redo(pid)
    child = fresh_db.get_plan_item(out["redo_item"])
    assert child["status"] == "approved" and child["redo_of"] == pid and child["revision"] == 1
    assert "渲染挂了" in child["feedback"]
    with pytest.raises(plan.PlanError):
        publisher.redo(pid)  # 已经重做过


def test_redo_rejects_live_item(fresh_db):
    pid = _review_item(fresh_db)
    with pytest.raises(plan.PlanError):
        publisher.redo(pid)


def test_redo_limit_abandons_and_frees_slot(fresh_db, monkeypatch):
    from app.team import editor

    monkeypatch.setattr(publisher.config, "TEAM_MAX_REVISIONS", 0)
    pid = _review_item(fresh_db, status="failed")
    out = publisher.redo(pid, "还是不行")
    assert out["redo_item"] == "" and out["status"] == "abandoned"
    item = fresh_db.get_plan_item(pid)
    assert item["status"] == "abandoned"
    assert editor.open_slots([item["slot_at"]], [item]) == [item["slot_at"]]


def test_reject_cut_at_limit_marks_abandoned(fresh_db, monkeypatch):
    monkeypatch.setattr(publisher.config, "TEAM_MAX_REVISIONS", 0)
    pid = _review_item(fresh_db)
    out = publisher.reject_cut(pid, "x", redo=True)
    assert out["status"] == "abandoned" and "上限" in out["redo_stopped"]


def test_redo_slot_moves_when_too_late(fresh_db):
    pid = _review_item(fresh_db, status="expired", slot_at=time.time() + 600)
    child = fresh_db.get_plan_item(publisher.redo(pid)["redo_item"])
    assert child["slot_at"] >= time.time() + 2 * 3600 - 5


def test_redo_slot_never_waits_days(fresh_db):
    """原时刻在 5 天后(深蓝S05 返工被排到 11-15)、空档又被待批选题占满:返工仍排在两天内。"""
    from app.team import editor

    now = time.time()
    for k, at in enumerate(editor.slot_times(dt.datetime.fromtimestamp(now),
                                             publisher.config.TEAM_PLAN_DAYS,
                                             publisher.config.TEAM_DAILY_SLOTS,
                                             publisher.config.TEAM_SLOT_TIMES)):
        fresh_db.create_plan_item({"id": f"pl_hold{k}", "category": "auto_long", "slot_at": at,
                                   "subject": f"占位{k}", "theme": "buyguide", "status": "proposed"})
    pid = _review_item(fresh_db, slot_at=now + 5 * 86400)
    child = fresh_db.get_plan_item(publisher.reject_cut(pid, "水印", redo=True)["redo_item"])
    assert now + 2 * 3600 - 5 <= child["slot_at"] <= now + 2 * 86400


def test_redo_is_due_without_waiting_for_lead_time(fresh_db, monkeypatch):
    """返工的题不等开工时间;普通已批选题照旧等。"""
    from app.team import producer

    monkeypatch.setattr(producer.config, "TEAM_LEAD_HOURS", 6.0)
    far = time.time() + 5 * 86400
    _review_item(fresh_db, "pl_new", status="approved", slot_at=far)
    _review_item(fresh_db, "pl_redo", status="approved", slot_at=far, revision=1, redo_of="pl_x")
    assert [i["id"] for i in producer.due()] == ["pl_redo"]


def test_started_today_ignores_redo(fresh_db, monkeypatch):
    from app.team import chief

    monkeypatch.setattr(fresh_db, "get_project", lambda pid: {"created_at": time.time()})
    _review_item(fresh_db, "pl_1", project_id="p1")
    _review_item(fresh_db, "pl_2", project_id="p2", redo_of="pl_1")
    assert chief.started_today() == 1
