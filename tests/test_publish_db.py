"""发布域的数据库表测试。

**幂等靠唯一索引**,不是靠代码里的 if —— 索引在,谁来都不会发两次;索引不在,
并发一冲就发两次。所以这条必须锁死。
"""
from __future__ import annotations

import sqlite3

import pytest

from app import config, db


@pytest.fixture()
def store(tmp_path, monkeypatch):
    """把库和项目目录指到临时目录,测试绝不碰真库。"""
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(config, "PROJECTS_DIR", tmp_path / "data" / "projects")
    db._INITIALISED = False
    db.init_db()
    yield
    db._INITIALISED = False


def _project(pid: str = "p_test") -> dict:
    return {
        "id": pid, "title": "测试项目", "subject": "小米YU7", "category": "auto_long",
        "theme": "buyguide", "angle": "", "subjects": [], "duration": 240,
        "shot_count": 10, "narration_mode": "tts", "state": "DONE", "state_reason": None,
        "quota_budget": 40.0, "spent_cost": 0.0, "est_cost": 1.0,
        "created_at": 0.0, "updated_at": 0.0,
    }


def _publication(pid: str, account: str, aspect: str = "16:9") -> dict:
    return {
        "id": f"pub_{pid}_{account}_{aspect}", "project_id": pid, "account_id": account,
        "platform": "bilibili", "aspect": aspect, "artifact": "output.mp4",
        "title": "标题", "description": "简介", "tags": "[]", "state": "pending",
        "remote_id": "", "remote_url": "", "upload_id": "", "error": "", "attempts": 0,
        "published_at": None, "created_at": 0.0, "updated_at": 0.0,
    }


# --------------------------------------------------------------------------
# 建表
# --------------------------------------------------------------------------
def test_publish_tables_exist(store):
    with db.connect() as conn:
        names = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"publish_accounts", "publications", "metric_snapshots"} <= names


def test_init_db_is_idempotent(store):
    db.init_db()
    db.init_db()
    with db.connect() as conn:
        names = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
    assert "publications" in names


# --------------------------------------------------------------------------
# 幂等:唯一索引
# --------------------------------------------------------------------------
def test_same_project_account_aspect_is_unique(store):
    db.create_project(_project())
    db.create_publication(_publication("p_test", "acc_1"))
    with pytest.raises(sqlite3.IntegrityError):
        db.create_publication(_publication("p_test", "acc_1"))


def test_different_aspect_is_allowed(store):
    db.create_project(_project())
    db.create_publication(_publication("p_test", "acc_1", "16:9"))
    db.create_publication(_publication("p_test", "acc_1", "9:16"))
    assert len(db.list_publications(project_id="p_test")) == 2


def test_find_publication_locates_the_row(store):
    db.create_project(_project())
    db.create_publication(_publication("p_test", "acc_1"))
    got = db.find_publication("p_test", "acc_1", "16:9")
    assert got is not None and got["id"] == "pub_p_test_acc_1_16:9"
    assert db.find_publication("p_test", "acc_1", "9:16") is None


# --------------------------------------------------------------------------
# 领任务:同一账号一次只给一条
# --------------------------------------------------------------------------
def test_claim_never_hands_out_two_tasks_of_same_account(store):
    db.create_project(_project())
    db.create_publication(_publication("p_test", "acc_1", "16:9"))
    db.create_publication(_publication("p_test", "acc_1", "9:16"))
    db.create_publication(_publication("p_test", "acc_2", "16:9"))
    rows = db.claim_publications(limit=3)
    accounts = [r["account_id"] for r in rows]
    assert len(accounts) == len(set(accounts)), "同一账号不该同时领到两条"


def test_claim_only_returns_pending_or_uploading(store):
    db.create_project(_project())
    db.create_publication(_publication("p_test", "acc_1"))
    db.update_publication("pub_p_test_acc_1_16:9", state="published")
    assert db.claim_publications(limit=3) == []


# --------------------------------------------------------------------------
# 指标快照
# --------------------------------------------------------------------------
def test_snapshot_roundtrip(store):
    db.create_project(_project())
    db.create_publication(_publication("p_test", "acc_1"))
    db.add_snapshot("pub_p_test_acc_1_16:9", "bilibili", 1000.0,
                    {"views": 10, "likes": 2}, {"raw": 1})
    rows = db.list_snapshots("pub_p_test_acc_1_16:9")
    assert len(rows) == 1
    assert rows[0]["views"] == 10
    assert db.latest_snapshot("pub_p_test_acc_1_16:9")["likes"] == 2


def test_snapshot_keeps_missing_metrics_as_null(store):
    """缺失就是 None —— 不能拿 0 顶替,不然看板会把"没权限"画成"零播放"。"""
    db.create_project(_project())
    db.create_publication(_publication("p_test", "acc_1"))
    db.add_snapshot("pub_p_test_acc_1_16:9", "youtube", 1000.0, {"views": 5})
    row = db.latest_snapshot("pub_p_test_acc_1_16:9")
    assert row["views"] == 5
    assert row["favorites"] is None


def test_snapshot_same_timestamp_is_ignored(store):
    db.create_project(_project())
    db.create_publication(_publication("p_test", "acc_1"))
    db.add_snapshot("pub_p_test_acc_1_16:9", "bilibili", 1000.0, {"views": 1})
    db.add_snapshot("pub_p_test_acc_1_16:9", "bilibili", 1000.0, {"views": 2})
    assert len(db.list_snapshots("pub_p_test_acc_1_16:9")) == 1
