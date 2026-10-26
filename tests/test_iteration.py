"""迭代记录:按周分桶(空周占格)、权重 / 实验变更进日志、复盘官顺手记、页面带上这块。"""
from __future__ import annotations

import datetime as dt

from tests.test_coach import _published, fresh_db, no_playbook  # noqa: F401 - fixtures


def test_weekly_buckets_and_gaps(fresh_db):
    from app.team import iteration

    now = dt.datetime(2026, 10, 25, 20, 0).timestamp()  # 周日
    t0 = now - 2 * 86400  # 本周五
    _published(fresh_db, "a", t0=t0, views=100)
    _published(fresh_db, "b", t0=t0 - 3600, views=300)
    _published(fresh_db, "old", t0=now - 3 * 7 * 86400, views=50)  # 三周前
    fresh_db.add_snapshot("a", "douyin", t0 + 25 * 3600,
                          {"views": 120, "bounce_2s": 0.4, "avg_watch_sec": 9,
                           "followers_gained": 3, "hours_since_publish": 25.0})
    for i, status in enumerate(("approved", "rejected", "rejected", "proposed")):
        fresh_db.create_plan_item({"id": f"i{i}", "category": "auto_long", "slot_at": t0,
                                   "subject": "x", "status": status})

    weeks = iteration.weekly(now)
    assert len(weeks) == iteration.WEEKS
    assert [w["week"] for w in weeks] == sorted(w["week"] for w in weeks)
    cur = weeks[-1]
    assert cur["week"] == "2026-W43" and cur["start"] == "2026-10-19"
    assert cur["works"] == 2 and cur["measured"] == 2
    assert cur["median_views"] == 200  # 第一张落在 24h 窗口里的快照
    assert cur["reject_rate"] == round(2 / 3, 3) and cur["decided"] == 3  # 没表态的不算
    assert weeks[-4]["works"] == 1 and weeks[-4]["median_views"] == 50
    assert weeks[-2]["works"] == 0 and weeks[-2]["median_views"] is None  # 空周留空,不当 0
    assert weeks[-2]["reject_rate"] is None


def test_weight_changes_logged_above_step(fresh_db):
    from app.team import iteration

    before = {"opening": {"conflict": 1.0, "scene": 1.3}}
    after = {"opening": {"conflict": 1.4, "scene": 1.35}, "pace": {"fast": 0.7}}
    assert iteration.log_weight_changes(before, after, ts=1.0) == 2
    rows = fresh_db.list_iteration_log()
    assert {r["detail"]["value"] for r in rows} == {"conflict", "fast"}
    up = next(r for r in rows if r["detail"]["value"] == "conflict")
    assert up["kind"] == "weight" and "1.00 → 1.40" in up["title"] and "多用" in up["title"]
    down = next(r for r in rows if r["detail"]["value"] == "fast")
    assert down["detail"]["before"] == 1.0 and "少用" in down["title"]


def test_plan_changes_logged(fresh_db):
    from app.team import iteration

    old = [{"dimension": "opening", "value": "conflict", "size": 3, "used": 3, "done": True},
           {"dimension": "pace", "value": "fast", "size": 3, "used": 1, "done": False},
           {"dimension": "look", "value": "hud", "size": 3, "used": 2, "done": False}]
    new = [{"dimension": "look", "value": "hud", "size": 3},
           {"dimension": "speed", "value": "brisk", "size": 3}]
    assert iteration.log_plan_changes(old, new, ts=5.0) == 3
    events = {r["detail"]["dimension"]: r["detail"]["event"] for r in fresh_db.list_iteration_log()}
    assert events == {"opening": "end", "pace": "drop", "speed": "start"}


def test_log_is_newest_first_and_since(fresh_db):
    for i in range(3):
        fresh_db.add_iteration_log("param", f"t{i}", ts=100.0 + i)
    rows = fresh_db.list_iteration_log()
    assert [r["title"] for r in rows] == ["t2", "t1", "t0"] and rows[0]["detail"] == {}
    assert [r["title"] for r in fresh_db.list_iteration_log(since=101.0)] == ["t2", "t1"]


def test_review_logs_weights_and_experiments(fresh_db):
    from app.team import coach, iteration

    now = dt.datetime(2026, 10, 25, 20, 0)
    t0 = now.timestamp() - 2 * 86400
    for i in range(4):
        _published(fresh_db, f"n{i}", t0=t0 - i * 3600, views=100, kind="number")
        _published(fresh_db, f"p{i}", t0=t0 - i * 3600, views=400, kind="person")
    coach.review(now)
    rows = fresh_db.list_iteration_log()
    weights = [r for r in rows if r["kind"] == "weight"]
    assert {r["detail"]["value"] for r in weights if r["detail"]["dimension"] == "title_kind"} \
        >= {"person", "number"}
    starts = [r for r in rows if r["kind"] == "experiment"]
    assert starts and all(r["detail"]["event"] == "start" for r in starts)

    coach.review(now)  # 数据没变:权重不动、实验接着跑 → 不再记
    assert len(fresh_db.list_iteration_log()) == len(rows)

    view = iteration.overview(now.timestamp())
    assert len(view["weeks"]) == iteration.WEEKS and view["weeks"][-1]["works"] == 8
    assert view["experiments"] and all("label" in e and "left" in e for e in view["experiments"])
    assert view["log"][0]["kind_label"] in iteration.KIND_LABEL.values()


def test_team_overview_has_iteration(fresh_db):
    from app.team import views

    data = views.overview()
    assert set(data["iteration"]) == {"weeks", "experiments", "log", "script_hint"}
    assert data["iteration"]["log"] == [] and len(data["iteration"]["weeks"]) == 8
