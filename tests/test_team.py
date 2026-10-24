"""AI 团队第一期:节奏、情报员、主编、选题闸、制片。"""
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


# --------------------------------------------------------------------------
# 节奏
# --------------------------------------------------------------------------
def test_daily_due_once_per_slot():
    from app.team import Cadence, is_due, next_slot

    cad = Cadence("daily", ("08:00", "18:00"))
    now = dt.datetime(2026, 10, 24, 9, 0)
    assert is_due(cad, now, None)
    ran_at = dt.datetime(2026, 10, 24, 8, 1).timestamp()
    assert not is_due(cad, now, ran_at)
    # 昨晚跑过、今早 8 点的没跑 → 该跑
    assert is_due(cad, now, dt.datetime(2026, 10, 23, 18, 5).timestamp())
    assert next_slot(cad, now, ran_at) == dt.datetime(2026, 10, 24, 18, 0)


def test_weekly_and_interval():
    from app.team import Cadence, is_due, last_slot

    weekly = Cadence("weekly", ("09:00",), weekday=0)
    sat = dt.datetime(2026, 10, 24, 12, 0)  # 周六
    assert last_slot(weekly, sat) == dt.datetime(2026, 10, 19, 9, 0)
    every = Cadence("interval", minutes=5)
    assert is_due(every, sat, None)
    assert not is_due(every, sat, sat.timestamp() - 60)
    assert is_due(every, sat, sat.timestamp() - 301)


# --------------------------------------------------------------------------
# 情报员
# --------------------------------------------------------------------------
def test_snapshot_rows_rank_per_source():
    from app.team.scout import snapshot_rows

    rows = snapshot_rows([
        {"source": "dongchedi.sales", "car": "A"}, {"source": "news", "title": "x"},
        {"source": "dongchedi.sales", "car": "B"},
    ])
    assert [r["rank"] for r in rows] == [1, 1, 2]


def test_movers_rank_change_and_new_entry():
    from app.team.scout import movers

    before = [{"source": "dongchedi.hot", "car": "A", "rank": 5},
              {"source": "dongchedi.hot", "car": "B", "rank": 1}]
    after = [{"source": "dongchedi.hot", "car": "B", "rank": 2},
             {"source": "dongchedi.hot", "car": "A", "rank": 1},
             {"source": "dongchedi.hot", "car": "C", "rank": 3}]
    got = movers(before, after)
    assert got[0]["car"] == "C" and got[0]["delta"] is None
    assert got[1] == {"source": "dongchedi.hot", "car": "A", "rank": 1, "prev": 5, "delta": 4}
    assert got[2]["car"] == "B" and got[2]["delta"] == -1
    # 没有上一批:不算谁新上榜
    assert movers([], after) == []


def test_rising_compares_with_day_old_batch(fresh_db):
    from app.team import scout

    now = time.time()
    fresh_db.add_trend_batch("auto_long", "b1", now - 25 * 3600,
                             [{"source": "dongchedi.hot", "car": "A", "rank": 9}])
    fresh_db.add_trend_batch("auto_long", "b2", now, [{"source": "dongchedi.hot", "car": "A", "rank": 2}])
    up = scout.rising("auto_long")
    assert up and up[0]["car"] == "A" and up[0]["delta"] == 7


# --------------------------------------------------------------------------
# 主编
# --------------------------------------------------------------------------
def test_slot_times_skip_too_soon():
    from app.team.editor import slot_times

    now = dt.datetime(2026, 10, 24, 12, 0)  # 今天 18:30 离现在只有 6.5h,来不及
    slots = slot_times(now, 3, 1, ("18:30",))
    assert [dt.datetime.fromtimestamp(s) for s in slots] == [
        dt.datetime(2026, 10, 25, 18, 30), dt.datetime(2026, 10, 26, 18, 30)]
    two = slot_times(dt.datetime(2026, 10, 24, 6, 0), 1, 2, ("18:30",))
    assert [dt.datetime.fromtimestamp(s).hour for s in two] == [18, 19]


def test_open_slots_ignore_rejected():
    from app.team.editor import open_slots

    items = [{"slot_at": 100.0, "status": "approved"}, {"slot_at": 200.0, "status": "rejected"}]
    assert open_slots([100.0, 200.0, 300.0], items) == [200.0, 300.0]


def test_rank_ideas_cooldown_and_boost():
    from app.pipeline.topics import TopicIdea
    from app.team.editor import rank_ideas

    ideas = [TopicIdea(subject="小米YU7", angle="a", theme="t", heat=3),
             TopicIdea(subject="理想i8", angle="b", theme="t", heat=3),
             TopicIdea(subject="问界M8", angle="c", theme="t", heat=4)]
    got = rank_ideas(ideas, cooled={"问界m8"}, rising_cars={"理想i8"})
    assert [i.subject for i in got] == ["理想i8", "小米YU7"]


def test_choose_format_fits_theme():
    from app.categories import load_profile
    from app.team.editor import choose_format

    profile = load_profile("auto_long")
    aspect, duration = choose_format(profile, profile.default_theme, "16:9", 250)
    assert aspect == "16:9"
    assert duration in profile.theme_durations(profile.theme_spec(profile.default_theme), "16:9")


class FakeLLM:
    def __init__(self, topics):
        self.topics = topics

    def structured(self, *args, **kwargs):
        return {"topics": self.topics}


def _seed_trends(db):
    db.add_trend_batch("auto_long", "b1", time.time(), [
        {"source": "dongchedi.sales", "kind": "model", "title": "小米YU7 月销 3 万", "car": "小米YU7", "rank": 1},
        {"source": "dongchedi.sales", "kind": "model", "title": "理想i8", "car": "理想i8", "rank": 2},
    ])


def test_editor_fills_open_slots(fresh_db, monkeypatch):
    from app import config
    from app.categories import load_profile
    from app.team import editor

    monkeypatch.setattr(config, "TEAM_PLAN_DAYS", 3)
    monkeypatch.setattr(config, "TEAM_DAILY_SLOTS", 1)
    _seed_trends(fresh_db)
    theme = load_profile("auto_long").default_theme
    llm = FakeLLM([
        {"subject": "小米YU7", "angle": "月销 3 万背后", "theme": theme, "heat": 4, "why_hot": "销量榜第一"},
        {"subject": "理想i8", "angle": "理想纯电 SUV 值不值", "theme": theme, "heat": 3},
        {"subject": "小米YU9", "angle": "编的车", "theme": theme, "heat": 5},
    ])
    now = dt.datetime(2026, 10, 24, 6, 0)
    summary = editor.run("auto_long", llm=llm, now=now)
    items = fresh_db.list_plan_items("auto_long")
    assert [i["subject"] for i in items] == ["小米YU7", "理想i8"]
    assert all(i["status"] == "proposed" for i in items)
    assert "新排 2 条" in summary
    # 再跑一次:已排的车在冷却里,LLM 只给同样的车 → 剩下的空档留空
    from app.team import Skip

    with pytest.raises(Skip):
        editor.run("auto_long", llm=llm, now=now)


# --------------------------------------------------------------------------
# 选题闸 + 制片
# --------------------------------------------------------------------------
def _item(db, **kw):
    from app.categories import load_profile

    profile = load_profile("auto_long")
    row = {"id": "pl_1", "category": "auto_long", "slot_at": time.time() + 3600, "subject": "小米YU7",
           "angle": "月销 3 万背后", "theme": profile.default_theme, "aspect": "16:9",
           "duration": profile.theme_durations(profile.theme_spec(profile.default_theme), "16:9")[0],
           **kw}
    db.create_plan_item(row)
    return row


def test_gate_approve_reject_patch(fresh_db):
    from app.team import plan

    _item(fresh_db)
    with pytest.raises(plan.PlanError):
        plan.patch("pl_1", {"duration": 7})
    with pytest.raises(plan.PlanError):
        plan.patch("pl_1", {"theme": "nope"})
    assert plan.patch("pl_1", {"angle": "新角度"})["angle"] == "新角度"
    assert plan.approve("pl_1")["status"] == "approved"
    with pytest.raises(plan.PlanError):
        plan.approve("pl_1")
    assert plan.reject("pl_1", "不想做")["status"] == "rejected"


def test_to_payload_carries_topic_fields(fresh_db):
    from app.team import plan

    _item(fresh_db, evidence=["销量榜第 1"], search_terms="小米YU7 销量", hook="三万台")
    payload = plan.to_payload(fresh_db.get_plan_item("pl_1"))
    assert payload.subject == "小米YU7" and payload.angle_evidence == ["销量榜第 1"]
    assert payload.angle_search == "小米YU7 销量" and payload.hook == "三万台"


def test_producer_starts_due_and_syncs(fresh_db, monkeypatch):
    from app import config
    from app.pipeline import orchestrator
    from app.team import producer

    monkeypatch.setattr(config, "TEAM_LEAD_HOURS", 30)
    _item(fresh_db, status="approved")
    _item(fresh_db, id="pl_late", status="approved", slot_at=time.time() + 5 * 86400)
    created = []

    def fake_create(payload):
        created.append(payload.subject)
        fresh_db.create_project({"id": "p_x", "title": "t", "subject": payload.subject,
                                 "category": "auto_long", "theme": "", "duration": payload.duration,
                                 "shot_count": 0, "narration_mode": "tts", "state": "DONE",
                                 "state_reason": None, "quota_budget": 40, "spent_cost": 0,
                                 "est_cost": 0, "created_at": time.time(), "updated_at": time.time()})
        return "p_x"

    monkeypatch.setattr(orchestrator, "create_project", fake_create)
    monkeypatch.setattr(producer, "_bootstrap", lambda pid: None)
    summary = producer.run()
    assert created == ["小米YU7"]
    item = fresh_db.get_plan_item("pl_1")
    assert item["project_id"] == "p_x" and item["status"] == "ready"  # 项目已 DONE → 同步成已出片
    assert fresh_db.get_plan_item("pl_late")["status"] == "approved"
    assert "开工 1 条" in summary


def test_producer_records_create_failure(fresh_db, monkeypatch):
    from app.pipeline import orchestrator
    from app.team import producer

    _item(fresh_db, status="approved")

    def boom(payload):
        raise orchestrator.PipelineError("时长不支持")

    monkeypatch.setattr(orchestrator, "create_project", boom)
    assert producer.start("pl_1", background=False) == ""
    item = fresh_db.get_plan_item("pl_1")
    assert item["status"] == "failed" and "时长不支持" in item["note"]


# --------------------------------------------------------------------------
# 运行记录 / 调度
# --------------------------------------------------------------------------
def test_run_role_records_and_blocks_duplicate(fresh_db, monkeypatch):
    from app import team

    assert fresh_db.start_team_run("scout") is not None
    assert team.run_role("scout")["status"] == "busy"


def test_run_role_idle_rows_pruned(fresh_db, monkeypatch):
    from app import team
    from app.team import producer

    monkeypatch.setattr(producer, "due", lambda now=None: [])
    for _ in range(3):
        assert team.run_role("producer")["status"] == "idle"
    assert len(fresh_db.list_team_runs("producer")) == 1


def test_tick_respects_pause(fresh_db, monkeypatch):
    from app import config, team
    from app.team import scheduler

    monkeypatch.setattr(config, "TEAM_ENABLED", True)
    calls = []
    monkeypatch.setattr(scheduler, "run_role", lambda name: calls.append(name))
    team.set_paused(True)
    assert scheduler.tick(dt.datetime(2026, 10, 24, 9, 30)) == []
    team.set_paused(False)
    assert scheduler.tick(dt.datetime(2026, 10, 24, 9, 30)) == ["scout", "editor", "producer"]
