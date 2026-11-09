"""涨粉看板 growth.summary:粉丝曲线、主片漏斗、账号状态。"""
from __future__ import annotations

import datetime as dt
import time

import pytest


@pytest.fixture
def fresh_db(tmp_path, monkeypatch):
    from app import config, db

    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(config, "PROJECTS_DIR", tmp_path / "data" / "projects")
    monkeypatch.setattr(db, "_INITIALISED", False)
    return db


def _pub(db, pid, at, *, piece="", platform="douyin", state="published"):
    db.create_publication({"id": pid, "project_id": f"p_{pid}", "account_id": "acc1",
                           "platform": platform, "state": state, "title": pid, "piece": piece,
                           "published_at": at})


def test_summary_followers_and_funnel(fresh_db):
    from app.team import growth

    now = time.time()
    today = dt.date.fromtimestamp(now)
    for k, n in enumerate((100, 103, 110)):
        fresh_db.save_account_daily("douyin", (today - dt.timedelta(days=2 - k)).isoformat(), n)
    _pub(fresh_db, "w1", now - 2 * 86400)
    fresh_db.add_snapshot("w1", "douyin", now - 86400, {"views": 300, "bounce_2s": 0.4})
    fresh_db.add_snapshot("w1", "douyin", now - 3600,
                          {"views": 500, "likes": 9, "completion_rate": 0.05,
                           "bounce_2s": 0.38, "followers_gained": 4})
    _pub(fresh_db, "clip", now - 86400, piece="clip1")          # 切片不算
    _pub(fresh_db, "bili", now - 86400, platform="bilibili")    # 别的平台不算
    _pub(fresh_db, "old", now - 40 * 86400)                     # 超出窗口
    fresh_db.pause_account("acc1", "疑似限流")

    out = growth.summary(days=14, now=now)
    assert [f["followers"] for f in out["followers"]] == [100, 103, 110]
    assert [f["gain"] for f in out["followers"]] == [None, 3, 7]
    assert out["followers_now"] == 110 and out["followers_change"] == 10
    assert [w["publication_id"] for w in out["works"]] == ["w1"]
    w = out["works"][0]
    assert w["views"] == 500 and w["views_24h"] == 300 and w["followers_gained"] == 4
    assert out["totals"] == {"works": 1, "views": 500, "followers_gained": 4}
    assert out["accounts"][0]["paused"] is True


def test_summary_empty(fresh_db):
    from app.team import growth

    out = growth.summary()
    assert out["works"] == [] and out["followers"] == []
    assert out["followers_now"] is None and out["followers_change"] is None
