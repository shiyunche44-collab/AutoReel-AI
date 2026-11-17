"""一步流水线的产物和状态转移一起生效:抢输的那一路不留任何痕迹。

2026-11-17:两个进程同时给 p_70415cf39369 写稿,抢输的那一路先把 script.json /
lint.json 覆盖了,才在 CAS 改状态时发现已被推进。状态里写着「lint 错误 8 条」,
磁盘上却是另一版稿。
"""
from __future__ import annotations

import threading

import pytest

from app import config, db
from app.pipeline import orchestrator
from app.state import State


@pytest.fixture()
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(config, "PROJECTS_DIR", tmp_path / "data" / "projects")
    db._INITIALISED = False
    db.init_db()
    db.create_project({
        "id": "p_stage", "title": "暂存测试", "subject": "测试车", "category": "auto_long",
        "theme": "buyguide", "angle": "", "subjects": [], "duration": 240,
        "shot_count": 10, "narration_mode": "tts", "state": "FACTS_RESOLVED",
        "state_reason": None, "quota_budget": 40.0, "spent_cost": 0.0,
        "est_cost": 1.0, "created_at": 1.0, "updated_at": 1.0,
    })
    yield db
    db._INITIALISED = False


def _leftovers() -> list[str]:
    return [p.name for p in config.project_dir("p_stage").iterdir() if ".staged-" in p.name]


def _kinds() -> list[str]:
    return [e["dst"] for e in reversed(db.get_events("p_stage"))]  # get_events 是新的在前


def test_winner_commits_files_and_events_together(store):
    orchestrator._commit_stage(
        "p_stage", State.FACTS_RESOLVED, State.SCRIPT_READY, "脚本 2 段,lint 错误 0 条",
        files={"script.json": ["a", "b"], "lint.json": []},
        before=[("script", "写稿自修 1 处:第 1 段")])
    assert db.get_project("p_stage")["state"] == "SCRIPT_READY"
    assert db.read_json("p_stage", "script.json") == ["a", "b"]
    assert db.read_json("p_stage", "lint.json") == []
    assert _kinds()[-2:] == ["script", "SCRIPT_READY"]  # 自修记在状态事件前面
    assert _leftovers() == []


def test_loser_leaves_winner_files_and_events_alone(store):
    orchestrator._commit_stage("p_stage", State.FACTS_RESOLVED, State.SCRIPT_READY, "赢家",
                               files={"script.json": ["winner"], "lint.json": ["w"]})
    before = _kinds()
    with pytest.raises(orchestrator.StateRaced):
        orchestrator._commit_stage(
            "p_stage", State.FACTS_RESOLVED, State.SCRIPT_READY, "输家",
            files={"script.json": ["loser"], "lint.json": ["l"], "repair.json": ["x"]},
            before=[("script", "写稿自修 2 处")])
    assert db.read_json("p_stage", "script.json") == ["winner"]
    assert db.read_json("p_stage", "lint.json") == ["w"]
    assert db.read_json("p_stage", "repair.json") is None
    assert db.get_project("p_stage")["state_reason"] == "赢家"
    assert _kinds() == before  # 输家的「写稿自修」不进事件流
    assert _leftovers() == []


def test_two_threads_racing_leave_one_consistent_result(store):
    barrier = threading.Barrier(2)
    results: dict[str, str] = {}

    def run(name: str) -> None:
        barrier.wait()
        try:
            orchestrator._commit_stage("p_stage", State.FACTS_RESOLVED, State.SCRIPT_READY,
                                       name, files={"script.json": [name]})
            results[name] = "won"
        except orchestrator.StateRaced:
            results[name] = "lost"

    threads = [threading.Thread(target=run, args=(n,)) for n in ("A", "B")]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(results.values()) == ["lost", "won"]
    winner = next(n for n, r in results.items() if r == "won")
    assert db.read_json("p_stage", "script.json") == [winner]
    assert db.get_project("p_stage")["state_reason"] == winner
    assert _leftovers() == []


def test_staged_names_do_not_collide_and_map_back(store):
    got: list = []

    def stage() -> None:
        got.append(db.stage_json("p_stage", "script.json", ["x"]))

    t = threading.Thread(target=stage)
    t.start()
    t.join()
    stage()
    assert got[0] != got[1]
    assert db.commit_staged(got[0]).name == "script.json"
    db.discard_staged(got[1])
    db.discard_staged(got[1])  # 丢两次不报错
    assert _leftovers() == []
