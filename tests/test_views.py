"""视图模型层(`app/views.py`)与"需要处理的项目"。

视图模型是纯函数(读库 → 组结构),不碰 HTTP —— 路由把 `ProjectNotFound`
翻成 404 就行。

顺带锁住一条可靠性的底线:**卡住的项目要自己跳出来**,不该要人翻列表。
"""
from __future__ import annotations

import pytest

from app import config, db, views
from app.state import State


@pytest.fixture()
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(config, "PROJECTS_DIR", tmp_path / "data" / "projects")
    db._INITIALISED = False
    db.init_db()
    yield db
    db._INITIALISED = False


def _project(pid: str, state: str, **extra) -> dict:
    row = {
        "id": pid, "title": f"项目 {pid}", "subject": "测试车", "category": "auto_long",
        "theme": "buyguide", "angle": "", "subjects": [], "duration": 240,
        "shot_count": 10, "narration_mode": "tts", "state": state,
        "state_reason": "", "quota_budget": 40.0, "spent_cost": 0.0,
        "est_cost": 1.0, "created_at": 1.0, "updated_at": 1.0,
    }
    row.update(extra)
    return row


# --------------------------------------------------------------------------
# 视图模型
# --------------------------------------------------------------------------
def test_build_summary_carries_the_key_fields(store):
    store.create_project(_project("p1", str(State.DRAFT_INTENT)))
    row = store.get_project("p1")
    s = views.build_summary(row)
    assert s.id == "p1"
    assert s.subject == "测试车"
    assert s.state == str(State.DRAFT_INTENT)
    assert s.theme_label, "方向标签不该是空串"
    assert s.has_output is False


def test_build_detail_raises_project_not_found(store):
    with pytest.raises(views.ProjectNotFound):
        views.build_detail("没有这个项目")


def test_build_detail_reads_creative_content(store):
    store.create_project(_project("p1", str(State.GATE_A)))
    # thesis 是生成脚本之后才写进库的(建项目那一步还没有它)
    store.update_project("p1", thesis="一句判断")
    store.write_json("p1", "script.json", [{"idx": 0, "section": "开场", "text": "第一句"}])
    d = views.build_detail("p1")
    assert d.thesis == "一句判断"
    assert len(d.script) == 1
    assert d.script[0].text == "第一句"


def test_theme_label_falls_back_to_the_raw_name(store):
    """不认识的品类不能把列表页搞挂。"""
    assert views.theme_label("没有这个品类", "某个方向") == "某个方向"


# --------------------------------------------------------------------------
# 需要处理的项目
# --------------------------------------------------------------------------
def test_needs_attention_picks_only_stuck_projects(store):
    for pid, state in (("p_paused", str(State.PAUSED_BUDGET)),
                       ("p_failed", str(State.FAILED)),
                       ("p_ok", str(State.DRAFT_RENDERING)),
                       ("p_done", str(State.DONE)),
                       ("p_cancelled", str(State.CANCELLED))):
        store.create_project(_project(pid, state))
    rows = store.list_projects()
    got = {p.id for p in views.needs_attention(rows)}
    assert got == {"p_paused", "p_failed"}


def test_index_page_shows_stuck_projects(store, monkeypatch):
    """首页要把需要处理的项目顶到最上面。"""
    from fastapi.testclient import TestClient

    from app import main

    store.create_project(_project("p_paused", str(State.PAUSED_BUDGET),
                                  state_reason="预计等效用量 12.00 超过上限 10.00"))
    store.create_project(_project("p_ok", str(State.DRAFT_RENDERING)))

    # HEALTH 只在 lifespan 里填,这里手动填一份
    monkeypatch.setitem(main.HEALTH, "ffmpeg", {"ok": True, "message": "ok"})
    monkeypatch.setitem(main.HEALTH, "connector", {"ok": True, "message": "ok"})
    monkeypatch.setitem(main.HEALTH, "llm", {"active": "deepseek"})

    client = TestClient(main.app)
    resp = client.get("/")
    assert resp.status_code == 200
    html = resp.text
    assert "个项目需要处理" in html
    assert "超过上限" in html, "暂停原因要能看到"
    assert "额度不足已暂停" in html, "状态徽标要显示出来"


def test_index_page_without_stuck_projects_has_no_callout(store, monkeypatch):
    from fastapi.testclient import TestClient

    from app import main

    store.create_project(_project("p_ok", str(State.DRAFT_RENDERING)))
    monkeypatch.setitem(main.HEALTH, "ffmpeg", {"ok": True, "message": "ok"})
    monkeypatch.setitem(main.HEALTH, "connector", {"ok": True, "message": "ok"})
    monkeypatch.setitem(main.HEALTH, "llm", {"active": "deepseek"})

    client = TestClient(main.app)
    resp = client.get("/")
    assert resp.status_code == 200
    assert "个项目需要处理" not in resp.text
