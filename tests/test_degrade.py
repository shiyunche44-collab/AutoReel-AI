"""降级记录(`app/degrade.py`):质量打折但流程照走的路径,每走一次留一行。"""
from __future__ import annotations

from app import db, degrade
from app.llm import ledger


def test_note_picks_up_project_from_context():
    db.init_db()
    with ledger.tag(project_id="p_1"):
        degrade.note("concept.to_card", "第 3 拍 bar_race:node 16")
    degrade.note("cover.to_old", "没有白底官图")
    degrade.note("cover.to_old", "生图失败", project_id="p_2")
    rows = {r["kind"]: r for r in degrade.summary(0)}
    assert rows["concept.to_card"]["n"] == 1 and rows["concept.to_card"]["projects"] == 1
    assert rows["cover.to_old"]["n"] == 2 and rows["cover.to_old"]["projects"] == 1
    assert rows["cover.to_old"]["last_detail"] == "生图失败"
    [item] = degrade.recent(kind="concept.to_card")
    assert item["project_id"] == "p_1"


def test_note_never_raises(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("database is locked")

    monkeypatch.setattr(db, "connect", boom)
    degrade.note("x", "y")


def test_endpoint_and_health():
    from fastapi.testclient import TestClient

    from app.main import app

    db.init_db()
    degrade.note("facts.source_failed", "dongchedi:403", project_id="p_1")
    c = TestClient(app)
    assert c.get("/api/degradations").json()["rows"][0]["kind"] == "facts.source_failed"
    assert len(c.get("/api/degradations", params={"project_id": "p_1"}).json()["items"]) == 1


def test_fallback_paths_are_wired():
    """接线抽查:README 承诺的几条降级路径都会留痕(防止重构时把 note 删丢)。"""
    import inspect

    from app.pipeline import orchestrator
    from app.pipeline.explainer import beat_render

    assert 'degrade.note("concept.to_card"' in inspect.getsource(beat_render)
    assert 'degrade.note("facts.source_failed"' in inspect.getsource(orchestrator)


def test_edit_v2_fallbacks_are_recorded():
    import inspect

    from app.pipeline import explainer
    from app.pipeline.edit import project as edit_project

    assert inspect.getsource(explainer).count('degrade.note("edit_v2.to_v1"') == 2
    assert 'degrade.note("edit_v2.shot_to_beat"' in inspect.getsource(edit_project)
