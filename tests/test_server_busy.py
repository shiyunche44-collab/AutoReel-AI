"""scripts/server_busy.py:重启前的忙碌判断。只用临时库,不碰真实服务。"""
from __future__ import annotations

import sqlite3

import pytest

from scripts import server_busy


@pytest.fixture
def tmp_db(tmp_path, monkeypatch):
    db = tmp_path / "AutoReel-AI.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE publications (id TEXT, platform TEXT, state TEXT, updated_at REAL)")
    conn.execute("CREATE TABLE team_runs (role TEXT, status TEXT, started_at REAL)")
    conn.commit()
    conn.close()
    monkeypatch.setattr(server_busy, "DB", db)
    return db


def _insert(db, sql, row):
    conn = sqlite3.connect(db)
    conn.execute(sql, row)
    conn.commit()
    conn.close()


def _health(monkeypatch, busy=(), running=(), background=True):
    monkeypatch.setattr(server_busy, "health", lambda port, timeout: {
        "background": {"ok": background},
        "scheduler": {"busy_projects": list(busy), "running": list(running)},
    })


def test_idle(tmp_db, monkeypatch):
    _health(monkeypatch)
    code, out = server_busy.check(8848, since=100.0, timeout=1)
    assert code == 0 and out["verdict"] == "idle"


def test_scheduler_project_is_busy(tmp_db, monkeypatch):
    _health(monkeypatch, busy=["p_1"], running=["p_2"])
    code, out = server_busy.check(8848, since=100.0, timeout=1)
    assert code == 1 and out["projects"] == ["p_1", "p_2"]


def test_uploading_and_team_runs_after_start_are_busy(tmp_db, monkeypatch):
    _health(monkeypatch)
    _insert(tmp_db, "INSERT INTO publications VALUES (?,?,?,?)", ("pub_a", "douyin", "uploading", 200.0))
    _insert(tmp_db, "INSERT INTO team_runs VALUES (?,?,?)", ("analyst", "running", 150.0))
    code, out = server_busy.check(8848, since=100.0, timeout=1)
    assert code == 1
    assert out["uploading"] == ["pub_a(douyin)"] and out["team_running"] == ["analyst"]


def test_rows_left_by_a_killed_server_do_not_block(tmp_db, monkeypatch):
    # 被强杀的旧进程留下的 running / uploading 行早于本次启动,不算忙
    _health(monkeypatch)
    _insert(tmp_db, "INSERT INTO publications VALUES (?,?,?,?)", ("pub_old", "douyin", "uploading", 50.0))
    _insert(tmp_db, "INSERT INTO team_runs VALUES (?,?,?)", ("coach", "running", 50.0))
    code, _ = server_busy.check(8848, since=100.0, timeout=1)
    assert code == 0


def test_read_only_web_ignores_db_rows(tmp_db, monkeypatch):
    _health(monkeypatch, background=False)
    _insert(tmp_db, "INSERT INTO team_runs VALUES (?,?,?)", ("analyst", "running", 150.0))
    code, _ = server_busy.check(8848, since=100.0, timeout=1)
    assert code == 0


def test_no_answer_is_unknown(tmp_db, monkeypatch):
    monkeypatch.setattr(server_busy, "health", lambda port, timeout: None)
    code, out = server_busy.check(8848, since=100.0, timeout=1)
    assert code == 2 and out["verdict"] == "unknown"
