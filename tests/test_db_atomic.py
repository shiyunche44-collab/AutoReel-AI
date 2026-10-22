"""落盘原子性与事务语义。

两件事,都是"长任务不丢数据"的地基:

1. **JSON 原子写**。README 自己写着"托管后台任务有 1 小时上限会把服务收走" ——
   写到一半被杀不是假设,是常态。之前 `write_json` 直接 `path.write_text`,
   进程被杀会留下截断的文件;而 `read_json` 对损坏文件是**静默**返回默认值
   的,于是半截的 script.json 被当成"文件不存在",流水线重新生成一份,
   **用户在门禁 A 手改的稿就这么没了**,而且事后完全查不出来。

2. **显式事务**。`job_events` 是本项目唯一的排障依据(状态卡住了靠事件流
   回放卡在哪一步)。但事件与状态是两条独立语句、两次独立连接,进程在
   中间被杀就会留下"状态变了但没事件"或"有事件但状态没变"的裂缝。
"""
from __future__ import annotations

import json
import os

import pytest

from app import config, db


@pytest.fixture()
def store(tmp_path, monkeypatch):
    """把库和项目目录指到临时目录,测试绝不碰真库。"""
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(config, "PROJECTS_DIR", tmp_path / "data" / "projects")
    db._INITIALISED = False
    db.init_db()
    yield db
    db._INITIALISED = False


def _project(pid: str = "p_atomic") -> dict:
    return {
        "id": pid, "title": "原子性测试", "subject": "测试车", "category": "auto_long",
        "theme": "buyguide", "angle": "", "subjects": [], "duration": 240,
        "shot_count": 10, "narration_mode": "tts", "state": "DRAFT_INTENT",
        "state_reason": None, "quota_budget": 40.0, "spent_cost": 0.0,
        "est_cost": 1.0, "created_at": 1.0, "updated_at": 1.0,
    }


# --------------------------------------------------------------------------
# JSON 原子写
# --------------------------------------------------------------------------
def test_write_json_replaces_atomically(store):
    db.write_json("p1", "script.json", [{"idx": 0, "text": "第一稿"}])
    db.write_json("p1", "script.json", [{"idx": 0, "text": "第二稿"}])
    assert db.read_json("p1", "script.json") == [{"idx": 0, "text": "第二稿"}]
    # 临时文件不能留在正式位置
    assert not (config.project_dir("p1") / "script.json.tmp").exists()


def test_write_json_killed_midway_keeps_old_content(store, monkeypatch):
    """写到一半被打断:旧稿必须完好无损。

    模拟点在 fsync —— 数据已经写进临时文件,但还没替换正式文件。
    """
    db.write_json("p1", "script.json", [{"idx": 0, "text": "用户手改的稿"}])

    def boom(_fd):
        raise OSError("模拟进程在 fsync 时被杀")

    # 用 context 局部打补丁:monkeypatch.undo() 会把 fixture 打的
    # DATA_DIR/PROJECTS_DIR 补丁一起撤销,读的就不是临时目录了。
    with monkeypatch.context() as mp:
        mp.setattr(db.os, "fsync", boom)
        with pytest.raises(OSError):
            db.write_json("p1", "script.json", [{"idx": 0, "text": "半截的新稿"}])

    assert db.read_json("p1", "script.json") == [{"idx": 0, "text": "用户手改的稿"}]
    # 半截内容不许留在项目目录里
    assert not (config.project_dir("p1") / "script.json.tmp").exists()


def test_replace_conflict_is_retried(store, monkeypatch):
    """Windows 上目标文件正被读着时 os.replace 会失败,重试即可。"""
    db.write_json("p1", "script.json", [{"idx": 0, "text": "旧"}])
    real_replace = os.replace
    calls = {"n": 0}

    def flaky(src, dst):
        calls["n"] += 1
        if calls["n"] < 3:
            raise PermissionError("另一个进程正开着这个文件")
        return real_replace(src, dst)

    monkeypatch.setattr(db.os, "replace", flaky)
    db.write_json("p1", "script.json", [{"idx": 0, "text": "新"}])
    assert calls["n"] >= 3
    assert db.read_json("p1", "script.json") == [{"idx": 0, "text": "新"}]


def test_corrupt_json_is_quarantined_not_silently_ignored(store):
    """损坏的 JSON 另存一份,不能当作"文件不存在"。

    静默忽略最坏:流水线以为文件没了,重新生成,用户手改的稿被覆盖,
    而且事后查不出发生过什么。
    """
    path = config.project_dir("p1") / "script.json"
    path.write_text('{"broken": ', encoding="utf-8")
    assert db.read_json("p1", "script.json", None) is None
    backups = list(config.project_dir("p1").glob("script.json.corrupt-*"))
    assert backups, "损坏文件应被另存留证"
    assert "broken" in backups[0].read_text(encoding="utf-8")


# --------------------------------------------------------------------------
# 显式事务
# --------------------------------------------------------------------------
def test_transaction_rolls_back_state_and_event(store):
    """事务中途炸了:状态与事件**都**不落库。

    两条语句必须同生共死 —— 事件流是排障时唯一的依据,
    "状态变了但没事件"比没有事件更误导人。
    """
    db.create_project(_project())
    with pytest.raises(RuntimeError):
        with db.transaction() as conn:
            db.update_project("p_atomic", state="SCRIPT_READY", conn=conn)
            db.add_event("p_atomic", "SCRIPT_READY", src="DRAFT_INTENT", conn=conn)
            raise RuntimeError("模拟进程被杀")

    row = db.get_project("p_atomic")
    assert row["state"] == "DRAFT_INTENT", "状态不该被提交"
    assert db.get_events("p_atomic") == [], "事件不该被提交"


def test_transaction_commits_on_success(store):
    db.create_project(_project())
    with db.transaction() as conn:
        db.update_project("p_atomic", state="SCRIPT_READY", conn=conn)
        db.add_event("p_atomic", "SCRIPT_READY", src="DRAFT_INTENT", conn=conn)
    assert db.get_project("p_atomic")["state"] == "SCRIPT_READY"
    assert len(db.get_events("p_atomic")) == 1


def test_connect_is_not_a_transaction(store):
    """回归:connect() 是 autocommit,`with connect() as conn:` 不开事务。

    这条断言把"connect() 不提供原子性"这个约定钉死 ——
    将来谁想在里面写多语句,会先看到这条测试。
    """
    db.create_project(_project())
    with db.connect() as conn:
        conn.execute("UPDATE projects SET state = 'SCRIPT_READY' WHERE id = 'p_atomic'")
        # 没 commit 也没 rollback,但 autocommit 模式下这句已经生效了
    assert db.get_project("p_atomic")["state"] == "SCRIPT_READY"


# --------------------------------------------------------------------------
# 状态 CAS
# --------------------------------------------------------------------------
def test_cas_state_only_applies_to_expected_state(store):
    db.create_project(_project())
    assert db.cas_state("p_atomic", "DRAFT_INTENT", "FACTS_RESOLVED") is True
    assert db.get_project("p_atomic")["state"] == "FACTS_RESOLVED"
    # 状态已经变了,第二次 CAS 必须失败
    assert db.cas_state("p_atomic", "DRAFT_INTENT", "SCRIPT_READY") is False
    assert db.get_project("p_atomic")["state"] == "FACTS_RESOLVED"


def test_update_project_rejects_unknown_column(store):
    """白名单:SET 子句是拼出来的,不能让人往里塞任意列名。"""
    db.create_project(_project())
    with pytest.raises(ValueError):
        db.update_project("p_atomic", not_a_column=1)
