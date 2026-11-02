"""跨项目镜头去重:新项目首次渲染时,同主体近期成片用过的素材排到后面。"""
from __future__ import annotations

import pytest

from app import config, db
from app.categories import load_profile
from app.models import ShotSpec
from app.pipeline import explainer


@pytest.fixture()
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(config, "PROJECTS_DIR", tmp_path / "data" / "projects")
    db._INITIALISED = False
    db.init_db()
    yield db
    db._INITIALISED = False


def _project(pid: str, *, state: str = "DONE", subject: str = "测试车", updated: float = 1.0) -> dict:
    return {
        "id": pid, "title": pid, "subject": subject, "category": "auto_long",
        "theme": "buyguide", "angle": "", "subjects": [], "duration": 240,
        "shot_count": 1, "narration_mode": "tts", "state": state,
        "state_reason": None, "quota_budget": 40.0, "spent_cost": 0.0,
        "est_cost": 1.0, "created_at": updated, "updated_at": updated,
    }


def _used_by(pid: str, *item_ids: str) -> None:
    db.write_json(pid, "materials_used.json",
                  {"pieces": [{"beat": 0, "item_id": i} for i in item_ids]})


def _spec() -> ShotSpec:
    return ShotSpec(idx=0, duration=6.0, section="开场", visual_zh="测试",
                    narration_zh="测试旁白", layout="broll")


def _pending(pid: str) -> None:
    db.upsert_shots(pid, [{"idx": 0, "stage": "draft", "status": "pending",
                           "model": "explainer", "provider_task_id": None,
                           "submitted_at": None, "last_polled_at": None,
                           "draft_path": None, "final_path": None, "error": None}])


def test_seed_counts_projects_that_used_each_clip(store):
    store.create_project(_project("p_old1", updated=1.0))
    store.create_project(_project("p_old2", updated=2.0))
    store.create_project(_project("p_new", state="DRAFT_RENDERING", updated=3.0))
    _used_by("p_old1", "a", "b", "a", "bg")  # 同一项目里重复用只算一次;卡片底图不算
    _used_by("p_old2", "a", "c")
    assert explainer.cross_project_seed("p_new", "测试车") == {"a": 2, "b": 1, "c": 1}


def test_seed_ignores_other_subjects_unfinished_and_self(store):
    store.create_project(_project("p_other", subject="别的车"))
    store.create_project(_project("p_wip", state="DRAFT_RENDERING"))
    store.create_project(_project("p_self", updated=5.0))
    for pid in ("p_other", "p_wip", "p_self"):
        _used_by(pid, "x")
    assert explainer.cross_project_seed("p_self", "测试车") == {}


def test_seed_only_looks_at_the_last_n_projects(store, monkeypatch):
    monkeypatch.setattr(config, "CROSS_PROJECT_RECENT", 2)
    for n in range(4):
        store.create_project(_project(f"p{n}", updated=float(n + 1)))
        _used_by(f"p{n}", f"clip{n}")
    store.create_project(_project("p_new", state="DRAFT_RENDERING", updated=9.0))
    assert set(explainer.cross_project_seed("p_new", "测试车")) == {"clip3", "clip2"}


def test_seed_can_be_switched_off(store, monkeypatch):
    store.create_project(_project("p_old"))
    store.create_project(_project("p_new", state="DRAFT_RENDERING", updated=2.0))
    _used_by("p_old", "a")
    monkeypatch.setattr(config, "CROSS_PROJECT_DEDUP", False)
    assert explainer.cross_project_seed("p_new", "测试车") == {}


def test_render_pending_passes_seed_but_does_not_persist_it(store, monkeypatch):
    store.create_project(_project("p_old"))
    store.create_project(_project("p_new", state="DRAFT_RENDERING", updated=2.0))
    _used_by("p_old", "a", "b")
    _pending("p_new")
    db.write_json("p_new", "beats_used.json", {"b": 1})
    seen: dict = {}

    def fake_render(spec, **kwargs):
        seen.update(kwargs["used"])
        kwargs["used"]["c"] = kwargs["used"].get("c", 0) + 1  # 这拍真的用了 c
        return config.project_dir("p_new") / "drafts" / "000.mp4", []

    monkeypatch.setattr(explainer, "render_beat", fake_render)
    explainer.render_pending("p_new", [_spec()], subject="测试车",
                             profile=load_profile("auto_long"))
    assert seen == {"a": 1, "b": 2}  # 种子并进了排序用的 used
    # 写回的只有本项目自己用的:种子不污染,重拍的 +3 也就仍然是在自己的基数上加
    assert db.read_json("p_new", "beats_used.json", {}) == {"b": 1, "c": 1}


def test_rerender_penalty_unchanged(store):
    store.create_project(_project("p_new", state="GATE_B"))
    _pending("p_new")
    explainer.beats_dir("p_new")
    db.write_json("p_new", "beats/000.json", [{"item_id": "a"}])
    db.write_json("p_new", "beats_used.json", {"a": 1})
    explainer.rerender("p_new", 0)
    assert db.read_json("p_new", "beats_used.json", {}) == {"a": 4}
