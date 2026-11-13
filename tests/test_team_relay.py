"""机械步骤下沉:门禁被拦自动再试一次、连续被拦和项目失败自动重开、发布网络失败重排、横竖屏对照。"""
from __future__ import annotations

import datetime as dt
import time

import pytest


@pytest.fixture
def fresh_db(tmp_path, monkeypatch):
    from app import config, db

    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(config, "PROJECTS_DIR", tmp_path / "data" / "projects")
    monkeypatch.setattr(db, "_INITIALISED", False)
    return db


def _item(db, **kw):
    row = {"id": "pl_1", "category": "auto_long", "slot_at": time.time() + 86400,
           "subject": "小米YU7", "theme": "buyguide", "aspect": "9:16", "duration": 60,
           "status": "producing", "project_id": "p_x", **kw}
    db.create_plan_item(row)
    return row


def _project(db, pid="p_x", state="GATE_A", reason=None):
    now = time.time()
    db.create_project({"id": pid, "title": "t", "subject": "小米YU7", "category": "auto_long",
                       "theme": "", "duration": 60, "shot_count": 0, "narration_mode": "tts",
                       "state": state, "state_reason": reason, "quota_budget": 40, "spent_cost": 0,
                       "est_cost": 0, "created_at": now, "updated_at": now})


def _blocked_gate(monkeypatch):
    from app.pipeline import orchestrator

    calls = []

    def fake_confirm(pid, req):
        calls.append(req.gate)
        raise orchestrator.PipelineError("门禁 A 未放行:脚本还有 1 处必须修的问题")

    monkeypatch.setattr(orchestrator, "confirm_gate", fake_confirm)
    return calls


def test_blocked_gate_retries_once_then_fails(fresh_db, monkeypatch):
    from app.team import producer

    _project(fresh_db)
    _item(fresh_db)
    calls = _blocked_gate(monkeypatch)
    producer.sync(background=False)
    assert calls == ["A"] and fresh_db.get_plan_item("pl_1")["hold_count"] == 1
    producer.sync(background=False)
    assert calls == ["A"]  # 还没到重试时间
    fresh_db.update_plan_item("pl_1", hold_at=time.time() - 3600)
    producer.sync(background=False)
    item = fresh_db.get_plan_item("pl_1")
    assert calls == ["A", "A"] and item["hold_count"] == 2 and item["status"] == "producing"
    fresh_db.update_plan_item("pl_1", hold_at=time.time() - 3600)
    producer.sync(background=False)
    item = fresh_db.get_plan_item("pl_1")
    assert calls == ["A", "A"] and item["status"] == "failed" and "连续被拦" in item["note"]


def test_blocked_gate_waits_when_relay_off(fresh_db, monkeypatch):
    from app import config
    from app.team import producer

    monkeypatch.setattr(config, "TEAM_AUTO_RELAY", False)
    _project(fresh_db)
    _item(fresh_db)
    calls = _blocked_gate(monkeypatch)
    producer.sync(background=False)
    fresh_db.update_plan_item("pl_1", hold_at=time.time() - 3600)
    producer.sync(background=False)
    assert calls == ["A"] and fresh_db.get_plan_item("pl_1")["status"] == "producing"
    assert producer.relay() == 0


def test_relay_reopens_recent_failures_once(fresh_db):
    from app.team import producer

    _item(fresh_db, status="failed", note="门禁连续被拦:数字没有事实引用")
    _item(fresh_db, id="pl_pub", status="failed", note="发布失败:登录失效")
    _item(fresh_db, id="pl_old", status="failed", note="x")
    assert producer.relay(now=time.time() + producer.RELAY_WINDOW - 60) == 3 - 1
    kids = [i for i in fresh_db.list_plan_items() if i["redo_of"]]
    assert sorted(k["redo_of"] for k in kids) == ["pl_1", "pl_old"]
    assert all(k["status"] == "approved" and k["revision"] == 1 for k in kids)
    assert "事实引用" in next(k for k in kids if k["redo_of"] == "pl_1")["feedback"]
    assert producer.relay() == 0  # 重做过的不再接力
    # 很久以前失败的不翻出来
    _item(fresh_db, id="pl_ancient", status="failed", note="y")
    assert producer.relay(now=time.time() + producer.RELAY_WINDOW + 60) == 0


def test_relay_abandons_at_revision_limit(fresh_db, monkeypatch):
    from app.team import producer, publisher

    monkeypatch.setattr(publisher.config, "TEAM_MAX_REVISIONS", 1)
    _item(fresh_db, status="failed", note="项目失败", revision=1)
    assert producer.relay() == 1
    assert fresh_db.get_plan_item("pl_1")["status"] == "abandoned"
    assert len(fresh_db.list_plan_items()) == 1


def test_network_publish_failure_requeued_once(fresh_db, monkeypatch):
    from app.team import publisher

    updates = []
    monkeypatch.setattr(fresh_db, "update_publication",
                        lambda pid, **f: updates.append((pid, f["state"])))
    monkeypatch.setattr(fresh_db, "add_event", lambda *a, **k: None)
    pubs = [{"id": "pub_1", "project_id": "p_x", "state": "failed", "error": "上传超时"}]
    assert publisher._requeue_network_failures(pubs) is True
    assert updates == [("pub_1", "pending")]
    assert publisher._requeue_network_failures(pubs) is False  # 只重排一次
    login = [{"id": "pub_2", "project_id": "p_x", "state": "failed", "error": "登录态失效,连接超时"}]
    assert publisher._requeue_network_failures(login) is False
    other = [{"id": "pub_3", "project_id": "p_x", "state": "failed", "error": "标题不合规"}]
    assert publisher._requeue_network_failures(other) is False


def test_slot_aspect_follows_weekday_until_end(monkeypatch):
    from app import config
    from app.team import editor

    mon = dt.datetime(2026, 11, 16, 18, 30).timestamp()
    tue = dt.datetime(2026, 11, 17, 18, 30).timestamp()
    late = dt.datetime(2026, 11, 30, 18, 30).timestamp()  # 周一,但对照已结束
    assert editor.slot_aspect(mon) is None  # 默认不做对照
    monkeypatch.setattr(config, "TEAM_LANDSCAPE_WEEKDAYS", (0, 2, 4))
    monkeypatch.setattr(config, "TEAM_LANDSCAPE_UNTIL", "2026-11-27")
    assert editor.slot_aspect(mon) == config.LANDSCAPE
    assert editor.slot_aspect(tue) is None and editor.slot_aspect(late) is None
