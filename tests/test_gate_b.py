"""门禁 B 与项目页的回归测试:草稿即成片,门禁 B 整片一次审完,不分批。"""
from __future__ import annotations

import pytest


@pytest.fixture
def project(tmp_path, monkeypatch):
    from app import config, db

    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(config, "PROJECTS_DIR", tmp_path / "data" / "projects")
    monkeypatch.setattr(db, "_INITIALISED", False)
    db.init_db()

    pid = "p_gate_b_test"
    db.create_project({
        "id": pid, "title": "门禁测试", "subject": "测试车", "category": "auto_long",
        "theme": "buyguide", "duration": 240, "shot_count": 27,
        "narration_mode": "tts", "state": "GATE_B", "state_reason": None,
        "quota_budget": 100.0, "spent_cost": 0.0, "est_cost": 1.0,
        "created_at": 1.0, "updated_at": 2.0,
    })
    db.write_json(pid, "storyboard.json", [
        {"idx": i, "duration": 7.0, "section": "开场", "visual_zh": "x",
         "camera_zh": "Static Shot", "layout": "broll"} for i in range(27)
    ])
    return pid


def test_migration_adds_theme_to_existing_db(tmp_path, monkeypatch):
    """老库升级:projects 表建在没有 theme 列时,init_db 必须补上。

    `CREATE TABLE IF NOT EXISTS` 不会给已存在的表加列 —— 没有这段迁移,
    老机器上的 aimovice.db 会直接 "no such column: theme"。
    """
    import sqlite3

    from app import config, db

    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(config, "PROJECTS_DIR", tmp_path / "data" / "projects")
    config.ensure_dirs()

    legacy = sqlite3.connect(config.DATA_DIR / "aimovice.db")
    legacy.execute("DROP TABLE IF EXISTS projects")
    legacy.execute("""
        CREATE TABLE projects (
            id TEXT PRIMARY KEY, title TEXT NOT NULL, subject TEXT NOT NULL,
            category TEXT NOT NULL DEFAULT 'auto', duration INTEGER NOT NULL,
            shot_count INTEGER NOT NULL DEFAULT 0, narration_mode TEXT NOT NULL DEFAULT 'tts',
            state TEXT NOT NULL, state_reason TEXT, quota_budget REAL NOT NULL DEFAULT 40,
            spent_cost REAL NOT NULL DEFAULT 0, est_cost REAL NOT NULL DEFAULT 0,
            created_at REAL NOT NULL, updated_at REAL NOT NULL)
    """)
    legacy.execute(
        "INSERT INTO projects (id,title,subject,duration,state,created_at,updated_at)"
        " VALUES ('old','旧项目','旧车',60,'DONE',1.0,2.0)"
    )
    legacy.commit()
    legacy.close()

    monkeypatch.setattr(db, "_INITIALISED", False)
    db.init_db()

    row = db.get_project("old")
    assert row is not None
    assert row["theme"] == "", "旧行应拿到空串,解析时回退默认方向"


def test_review_is_one_batch(project):
    from app.pipeline import orchestrator

    rv = orchestrator.review_progress(project)
    assert rv["total_batches"] == 1
    assert rv["batch_list"][0]["shots"] == list(range(27))
    assert rv["pending_total"] == 27


def test_gate_b_ignores_partial_approval(project, monkeypatch):
    """promote_all 整片转成片 —— 记录里只批准一部分会和实际产出对不上。"""
    from app.models import GateRequest
    from app.pipeline import orchestrator

    monkeypatch.setattr(orchestrator, "advance", lambda pid, confirmed=False: {"ok": True})
    orchestrator.confirm_gate(project, GateRequest(gate="B", approved=[0, 1, 2]))
    rv = orchestrator.review_progress(project)
    assert rv["approved"] == list(range(27))
    assert rv["pending_total"] == 0


def test_gate_b_rejects_wrong_state(project):
    from app import db
    from app.models import GateRequest
    from app.pipeline import orchestrator

    db.update_project(project, state="GATE_A")
    with pytest.raises(orchestrator.PipelineError):
        orchestrator.confirm_gate(project, GateRequest(gate="B"))


def test_project_page_has_no_generation_estimate(project):
    """项目页只给解说轨的渲染耗时,没有 H3 的草稿/成片墙钟。"""
    from fastapi.testclient import TestClient

    from app.main import app

    html = TestClient(app).get(f"/project/{project}").text
    assert "渲染约" in html
    assert "草稿约" not in html and "成片约" not in html
    assert "H3" not in html
