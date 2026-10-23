"""发布链里的包装:标题 / 封面选择、publications.variant 迁移、A/B 数据汇总、API。"""
from __future__ import annotations

import json
import sqlite3

import pytest

from app import config, db
from app.pipeline import packaging as pk
from app.publish import service
from app.publish.base import PublishResult

GOOD = {
    "titles": [
        {"kind": "stake", "text": "23.35万YU7,买屏还是买电?"},
        {"kind": "number", "text": "YU7补能15分钟,够用吗"},
        {"kind": "person", "text": "天天跑高速,YU7选哪版"},
    ],
    "comment": {"text": "你选标准版还是GT?评论区投一票", "options": ["标准版", "GT"]},
    "replies": ["补能15分钟是官方口径", "价格以官方当期为准", "有出入欢迎指出"],
}


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(config, "PROJECTS_DIR", tmp_path / "data" / "projects")
    db._INITIALISED = False
    db.init_db()
    db.create_project({
        "id": "p_1", "title": "小米YU7 解说视频", "subject": "小米YU7",
        "category": "auto_long", "theme": "buyguide", "angle": "", "subjects": [],
        "duration": 240, "shot_count": 10, "narration_mode": "tts", "state": "DONE",
        "state_reason": None, "quota_budget": 40.0, "spent_cost": 0.0, "est_cost": 1.0,
        "created_at": 0.0, "updated_at": 0.0,
    })
    db.create_account({
        "id": "acc_1", "platform": "bilibili", "name": "测试号", "auth_kind": "cookie",
        "credential": json.dumps({"cookie": "SESSDATA=x"}), "meta": "{}", "enabled": 1,
        "created_at": 0.0, "updated_at": 0.0,
    })
    pdir = config.project_dir("p_1")
    (pdir / "output.mp4").write_bytes(b"fake mp4")
    (pdir / "cover.jpg").write_bytes(b"A")
    yield pdir
    db._INITIALISED = False


def _make_pkg(pdir, with_b=False):
    pkg = pk.assemble(GOOD, "小米YU7", allowed=pk.allowed_figures(["售价 23.35 万元", "15 分钟补能"]),
                      price="23.35万元起")
    pk.save("p_1", pkg)
    if with_b:
        (pdir / "cover_b.jpg").write_bytes(b"B")
    return pkg


def _row():
    return db.list_publications(project_id="p_1")[0]


def test_publications_variant_column_migrates_old_db(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(config, "PROJECTS_DIR", tmp_path / "data" / "projects")
    config.ensure_dirs()
    conn = sqlite3.connect(config.DATA_DIR / "aimovice.db")
    conn.execute("CREATE TABLE publications (id TEXT PRIMARY KEY, project_id TEXT NOT NULL, "
                 "account_id TEXT NOT NULL, platform TEXT NOT NULL, aspect TEXT NOT NULL DEFAULT '16:9', "
                 "artifact TEXT NOT NULL DEFAULT '', title TEXT NOT NULL DEFAULT '', "
                 "description TEXT NOT NULL DEFAULT '', tags TEXT NOT NULL DEFAULT '[]', "
                 "state TEXT NOT NULL DEFAULT 'pending', remote_id TEXT NOT NULL DEFAULT '', "
                 "remote_url TEXT NOT NULL DEFAULT '', upload_id TEXT NOT NULL DEFAULT '', "
                 "error TEXT NOT NULL DEFAULT '', attempts INTEGER NOT NULL DEFAULT 0, "
                 "published_at REAL, created_at REAL NOT NULL, updated_at REAL NOT NULL)")
    conn.execute("INSERT INTO publications (id, project_id, account_id, platform, created_at, updated_at) "
                 "VALUES ('old','p','a','bilibili',0,0)")
    conn.commit()
    conn.close()
    db._INITIALISED = False
    db.init_db()
    assert db.get_publication("old")["variant"] == "{}"
    assert service._variant_of(db.get_publication("old")) == {}
    db._INITIALISED = False


def test_enqueue_without_packaging_keeps_original_title(env):
    service.enqueue("p_1", account_ids=["acc_1"])
    row = _row()
    assert row["title"].startswith("小米YU7")
    assert service._variant_of(row) == {"title_kind": "", "title_source": "original", "cover": "a"}


def test_enqueue_uses_picked_title_and_cover(env):
    _make_pkg(env, with_b=True)
    pk.set_picked("p_1", title=1, cover="b")
    service.enqueue("p_1", account_ids=["acc_1"])
    row = _row()
    assert row["title"] == "YU7补能15分钟,够用吗"
    assert service._variant_of(row) == {"title_kind": "number", "title_source": "picked", "cover": "b"}
    assert service.publication_view(row)["variant"]["cover"] == "b"


def test_enqueue_override_beats_picked_and_records_kind(env):
    _make_pkg(env)
    pk.set_picked("p_1", title=1)
    service.enqueue("p_1", account_ids=["acc_1"], overrides={"bilibili": {"title": "自己改的标题"}})
    row = _row()
    assert row["title"] == "自己改的标题"
    assert service._variant_of(row)["title_kind"] == "custom"
    assert service._variant_of(row)["title_source"] == "override"
    # 手改成某个候选原文,仍记它的写法
    service.enqueue("p_1", account_ids=["acc_1"], force=True,
                    overrides={"bilibili": {"title": "天天跑高速,YU7选哪版"}})
    assert service._variant_of(_row())["title_kind"] == "person"


def test_cover_b_missing_falls_back_to_a(env):
    _make_pkg(env, with_b=True)
    pk.set_picked("p_1", cover="b")
    (env / "cover_b.jpg").unlink()
    assert pk.publish_choice("p_1")["cover"] == "a"
    assert service._cover_for("p_1", "16:9", "bilibili", "b").name == "cover.jpg"


def test_cover_for_variant_b(env):
    _make_pkg(env, with_b=True)
    assert service._cover_for("p_1", "16:9", "bilibili", "b").name == "cover_b.jpg"
    assert service._cover_for("p_1", "16:9", "bilibili", "a").name == "cover.jpg"


def test_set_picked_validates(env):
    _make_pkg(env)
    with pytest.raises(ValueError):
        pk.set_picked("p_1", title=7)
    with pytest.raises(ValueError):
        pk.set_picked("p_1", cover="b")  # 没有 B 文件
    with pytest.raises(ValueError):
        pk.set_picked("p_1", cover="c")
    assert pk.set_picked("nope", title=0) is None


def test_overview_groups_by_title_kind_and_cover(env, monkeypatch):
    from app.metrics import store

    _make_pkg(env, with_b=True)
    pk.set_picked("p_1", title=1, cover="b")
    service.enqueue("p_1", account_ids=["acc_1"])
    pub = _row()
    db.add_snapshot(pub["id"], "bilibili", 1.0, {"views": 100, "likes": 10, "comments": 2,
                                                  "favorites": 0, "shares": 0}, {})
    ov = store.overview()
    kinds = {b["title_kind"]: b for b in ov["by_title_kind"]}
    assert kinds["number"]["count"] == 1 and kinds["number"]["avg_views"] == 100
    assert kinds["number"]["label"] == "数字"
    assert ov["by_cover"][0]["cover"] == "b"


def test_overview_old_rows_are_unknown(env):
    from app.metrics import store

    service.enqueue("p_1", account_ids=["acc_1"])
    db.update_publication(_row()["id"], variant="{}")
    ov = store.overview()
    assert ov["by_title_kind"][0]["title_kind"] == "unknown"
    assert ov["by_cover"][0]["cover"] == "a"


# --------------------------------------------------------------------------
# API
# --------------------------------------------------------------------------
@pytest.fixture()
def client(env):
    from fastapi.testclient import TestClient

    from app import main

    return TestClient(main.app)


def test_api_packaging_roundtrip(env, client):
    assert client.get("/api/projects/p_1/packaging").json() is None
    _make_pkg(env, with_b=True)
    got = client.get("/api/projects/p_1/packaging").json()
    assert len(got["titles"]) == 3 and got["covers"] == {"a": True, "b": True}
    put = client.put("/api/projects/p_1/packaging", json={"title": 2, "cover": "b"}).json()
    assert put["picked"] == {"title": 2, "cover": "b"}
    assert client.put("/api/projects/p_1/packaging", json={"title": 9}).status_code == 400
    assert client.put("/api/projects/nope/packaging", json={"title": 0}).status_code == 404


def test_api_pick_without_packaging_is_404(env, client):
    assert client.put("/api/projects/p_1/packaging", json={"title": 0}).status_code == 404


def test_api_cover_variants(env, client):
    assert client.get("/api/projects/p_1/cover").content == b"A"
    assert client.get("/api/projects/p_1/cover?variant=b").status_code == 404
    (env / "cover_b.jpg").write_bytes(b"B")
    assert client.get("/api/projects/p_1/cover?variant=b").content == b"B"
    assert client.get("/api/projects/p_1/cover?variant=z").status_code == 400
    assert client.get("/api/projects/p_1/cover?ratio=3x4").status_code == 404


def test_api_regenerate_keeps_cover_pick(env, client, monkeypatch):
    class _LLM:
        def structured(self, *a, **k):
            return GOOD

    monkeypatch.setattr(pk, "default_llm", lambda: _LLM())
    db.write_json("p_1", "script.json", [{"text": "补能15分钟"}])
    db.write_json("p_1", "facts.json", [{"id": "f", "claim": "YU7 售价 23.35 万元起", "scope": "product",
                                        "confidence": "high", "enabled": True},
                                       {"id": "g", "claim": "15 分钟补能 405 公里", "scope": "product",
                                        "confidence": "high", "enabled": True}])
    _make_pkg(env, with_b=True)
    pk.set_picked("p_1", cover="b")
    got = client.post("/api/projects/p_1/packaging/regenerate").json()
    assert got["source"] == "llm" and got["picked"]["cover"] == "b"


def test_api_preview_includes_packaging(env, client):
    _make_pkg(env)
    assert client.get("/api/projects/p_1/publish/preview").json()["packaging"]["titles"]
