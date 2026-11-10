"""发布员:主片发出去后发第一条评论(默认关、只管新片、失败有限次重试)。"""
from __future__ import annotations

import time

import pytest

from app import config


@pytest.fixture
def fresh_db(tmp_path, monkeypatch):
    from app import db

    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(config, "PROJECTS_DIR", tmp_path / "data" / "projects")
    monkeypatch.setattr(db, "_INITIALISED", False)
    return db


def _pub(db, pid="pub1", *, age=3600.0, remote="w1", piece=None, comment="选低配还是顶配?"):
    proj = f"p_{pid}"
    db.create_publication({"id": pid, "project_id": proj, "account_id": "acc1",
                           "platform": "douyin", "title": "星愿到底该不该买", "state": "published",
                           "remote_id": remote, "published_at": time.time() - age,
                           **({"piece": piece} if piece else {})})
    if comment:
        db.write_json(proj, "packaging.json", {"titles": [], "replies": [],
                                               "comment": {"text": comment, "options": []}})
    return proj


def test_off_by_default(fresh_db):
    from app.team import first_comment

    _pub(fresh_db)
    sent = []
    assert config.DOUYIN_FIRST_COMMENT is False
    assert first_comment.run_once(lambda a, t: sent.append((a, t))) == "" and not sent


def test_sends_once_and_remembers(fresh_db, monkeypatch):
    from app.team import first_comment

    monkeypatch.setattr(config, "DOUYIN_FIRST_COMMENT", True)
    proj = _pub(fresh_db)
    sent = []
    out = first_comment.run_once(lambda a, t: sent.append((a, t)))
    assert sent == [("w1", "选低配还是顶配?")] and "已发" in out
    assert fresh_db.read_json(proj, first_comment.STATE_FILE, {})["result"] == "sent"
    assert first_comment.run_once(lambda a, t: sent.append((a, t))) == "" and len(sent) == 1


def test_only_recent_main_pieces_with_text(fresh_db, monkeypatch):
    from app.team import first_comment

    monkeypatch.setattr(config, "DOUYIN_FIRST_COMMENT", True)
    _pub(fresh_db, "fresh", age=60, remote="w0")                    # 刚发,还在审核
    _pub(fresh_db, "old", age=5 * 86400, remote="w2")               # 老作品不补发
    _pub(fresh_db, "nomatch", remote="")                            # 还没对上作品
    _pub(fresh_db, "notext", remote="w4", comment="")               # 没有文案
    assert first_comment.due() == []
    _pub(fresh_db, "ok", remote="w5")
    assert [p["id"] for p, _, _ in first_comment.due()] == ["ok"]


def test_failure_retries_after_gap_then_gives_up(fresh_db, monkeypatch):
    from app.publish.base import PublishError
    from app.team import first_comment

    monkeypatch.setattr(config, "DOUYIN_FIRST_COMMENT", True)
    _pub(fresh_db)
    calls = []

    def boom(a, t):
        calls.append(a)
        raise PublishError("评论区没有输入框", retryable=False)

    now = time.time()
    assert "没发出去" in first_comment.run_once(boom, now)
    assert first_comment.run_once(boom, now + 60) == ""             # 没到间隔
    for i in range(1, 6):
        first_comment.run_once(boom, now + i * (first_comment.RETRY_GAP + 1))
    assert len(calls) == first_comment.MAX_ATTEMPTS


def test_verification_pauses_for_a_day(fresh_db, monkeypatch):
    from app.publish.platforms.douyin_outreach import VerificationNeeded
    from app.team import first_comment

    monkeypatch.setattr(config, "DOUYIN_FIRST_COMMENT", True)
    _pub(fresh_db, "a", remote="w1")
    _pub(fresh_db, "b", remote="w2", age=7200)
    calls = []

    def verify(a, t):
        calls.append(a)
        raise VerificationNeeded("弹了滑块")

    now = time.time()
    assert "验证" in first_comment.run_once(verify, now)
    assert first_comment.run_once(verify, now + 3 * 3600) == "" and len(calls) == 1
