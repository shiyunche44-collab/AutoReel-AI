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


# --------------------------------------------------------------------------
# 两周目标
# --------------------------------------------------------------------------
def _w(views, bounce, c5, *, hours=48.0, at=1_000_000.0):
    return {"views": views, "bounce_2s": bounce, "completion_5s": c5,
            "hours_since_publish": hours, "published_at": at}


def test_goals_hold_until_enough_works():
    from app.team import growth

    g = growth.goals([_w(50, 0.43, 0.29)] * 3 + [_w(9999, 0.1, 0.9, hours=2.0)])
    assert g["works"] == 3 and g["hold"] is True and g["ready"] is False   # 不满 24 小时的不算
    assert "3/10" in g["verdict"] and "先别改打法" in g["verdict"]
    by = {m["key"]: m for m in g["metrics"]}
    assert by["views"]["value"] == 50 and by["views"]["met"] is False
    assert by["bounce_2s"]["met"] is False and by["completion_5s"]["met"] is False
    assert growth.goals([])["verdict"].startswith("还没有")


def test_goals_met_and_missed_layers():
    from app.team import growth

    good = growth.goals([_w(320, 0.25, 0.5)] * 10)
    assert good["ready"] and all(m["met"] for m in good["metrics"]) and "达标" in good["verdict"]
    # 边界:播放正好 300 算达标;跳出正好 30% 不算
    edge = {m["key"]: m["met"] for m in growth.goals([_w(300, 0.30, 0.45)] * 10)["metrics"]}
    assert edge == {"views": True, "bounce_2s": False, "completion_5s": False}
    bad = growth.goals([_w(320, 0.41, 0.5)] * 10)
    assert bad["verdict"].startswith("没达标") and "开头" in bad["verdict"]
    assert "选题" not in bad["verdict"]


def test_goals_since_drops_older_works():
    import datetime as dt

    from app.team import growth

    t0 = dt.datetime(2026, 11, 10).timestamp()
    works = [_w(20, 0.5, 0.2, at=t0 - 86400)] * 5 + [_w(400, 0.2, 0.6, at=t0 + 86400)] * 2
    g = growth.goals(works, "2026-11-10")
    assert g["works"] == 2 and g["since"] == "2026-11-10"
    assert {m["key"]: m["value"] for m in g["metrics"]}["views"] == 400
    assert growth.goals(works, "不是日期")["works"] == 7       # 写错了就当没填


def test_summary_reads_5s_completion_from_raw(fresh_db):
    from app.team import growth

    now = time.time()
    _pub(fresh_db, "w1", now - 2 * 86400)
    fresh_db.add_snapshot("w1", "douyin", now - 3600, {
        "views": 86, "bounce_2s": 0.43, "hours_since_publish": 47.0},
        raw={"metrics": {"completion_rate_5s": "0.294118"}})
    out = growth.summary(days=14, now=now)
    assert out["works"][0]["completion_5s"] == pytest.approx(0.294118)
    assert out["goals"]["works"] == 1 and out["goals"]["hold"] is True


def test_summary_conversion_from_homepage_visits(fresh_db):
    from app.team import growth

    now = time.time()
    _pub(fresh_db, "w1", now - 2 * 86400)
    fresh_db.add_snapshot("w1", "douyin", now - 3600, {"views": 400, "followers_gained": 2},
                          raw={"metrics": {"homepage_visit_count": "21"}})
    _pub(fresh_db, "w2", now - 3 * 86400)
    fresh_db.add_snapshot("w2", "douyin", now - 3600, {"views": 50, "followers_gained": 0},
                          raw={"metrics": {"homepage_visit_count": "4"}})
    _pub(fresh_db, "w3", now - 4 * 86400)                       # 没有主页访问数的不计入
    fresh_db.add_snapshot("w3", "douyin", now - 3600, {"views": 30, "followers_gained": 1})
    out = growth.summary(days=14, now=now)
    assert {w["publication_id"]: w["homepage_visits"] for w in out["works"]} == {
        "w1": 21.0, "w2": 4.0, "w3": None}
    assert out["conversion"] == {"homepage_visits": 25, "followers_gained": 2, "works": 2,
                                 "visit_to_follow": 0.08}


def test_summary_conversion_empty(fresh_db):
    from app.team import growth

    assert growth.summary()["conversion"] == {
        "homepage_visits": 0, "followers_gained": 0, "works": 0, "visit_to_follow": None}
