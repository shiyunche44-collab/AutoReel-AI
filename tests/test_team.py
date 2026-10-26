"""AI 团队:节奏、情报员、主编、选题闸、素材官、制片(含自动过闸)、发布员(成片闸 + 定时发布)、
合规审核官(审核单 + 限流监测)。分析师见 test_analyst.py。"""
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
    assert item["project_id"] == "p_x" and item["status"] == "review"  # 项目已 DONE → 成片待审
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
    assert scheduler.tick(dt.datetime(2026, 10, 24, 9, 30)) == [
        "scout", "events", "benchmark", "rapid", "editor", "asset", "producer", "repurpose", "publisher", "analyst",
        "compliance", "engagement", "activator", "coach"]


# --------------------------------------------------------------------------
# 制片自动过闸
# --------------------------------------------------------------------------
def _project(db, pid="p_x", state="GATE_A"):
    now = time.time()
    db.create_project({"id": pid, "title": "t", "subject": "小米YU7", "category": "auto_long",
                       "theme": "", "duration": 240, "shot_count": 0, "narration_mode": "tts",
                       "state": state, "state_reason": None, "quota_budget": 40, "spent_cost": 0,
                       "est_cost": 0, "created_at": now, "updated_at": now})


def _gates(monkeypatch, fail=None):
    from app.pipeline import orchestrator

    calls = []

    def fake_confirm(pid, req):
        calls.append(req.gate)
        if fail:
            raise orchestrator.PipelineError(fail)
        return {"state": "DRAFT_RENDERING"}

    monkeypatch.setattr(orchestrator, "confirm_gate", fake_confirm)
    return calls


def test_auto_gate_a_passes(fresh_db, monkeypatch):
    from app.team import producer

    _project(fresh_db)
    _item(fresh_db, status="producing", project_id="p_x")
    calls = _gates(monkeypatch)
    producer.sync(background=False)
    assert calls == ["A"]
    assert not fresh_db.get_plan_item("pl_1")["hold"]


def test_auto_gate_blocked_holds_until_state_moves(fresh_db, monkeypatch):
    from app.team import producer

    _project(fresh_db)
    _item(fresh_db, status="producing", project_id="p_x")
    calls = _gates(monkeypatch, fail="门禁 A 未放行:脚本还有 2 处必须修的问题")
    producer.sync(background=False)
    item = fresh_db.get_plan_item("pl_1")
    assert "2 处必须修" in item["hold"] and item["hold_state"] == "GATE_A"
    producer.sync(background=False)
    assert calls == ["A"]  # 拦下后不反复试
    # 人在项目页处理过,项目往前走了 → hold 自动清掉
    fresh_db.update_project("p_x", state="DRAFT_RENDERING")
    producer.sync(background=False)
    assert fresh_db.get_plan_item("pl_1")["hold"] == ""
    # 手动重试也能清
    fresh_db.update_project("p_x", state="GATE_A")
    producer.sync(background=False)
    assert producer.retry_gate("pl_1")["hold"] == ""


def test_auto_gate_b_needs_every_beat(fresh_db, monkeypatch):
    from app.team import producer

    _project(fresh_db, state="GATE_B")
    _item(fresh_db, status="producing", project_id="p_x")
    calls = _gates(monkeypatch)
    monkeypatch.setattr(fresh_db, "read_json", lambda pid, name, default=None:
                        [{"idx": 0, "duration": 5}, {"idx": 1, "duration": 4}, {"idx": 2, "duration": 0}])
    monkeypatch.setattr(fresh_db, "get_shots", lambda pid, stage=None:
                        [{"idx": 0, "status": "succeeded"}, {"idx": 1, "status": "failed"}])
    producer.sync(background=False)
    assert calls == [] and "第 2 拍" in fresh_db.get_plan_item("pl_1")["hold"]


def test_auto_gates_switch(fresh_db, monkeypatch):
    from app import config
    from app.team import producer

    monkeypatch.setattr(config, "TEAM_AUTO_GATES", False)
    _project(fresh_db)
    _item(fresh_db, status="producing", project_id="p_x")
    calls = _gates(monkeypatch)
    producer.sync(background=False)
    assert calls == []


# --------------------------------------------------------------------------
# 发布员:成片闸 + 定时发布
# --------------------------------------------------------------------------
def test_next_publish_time_rolls_forward():
    from app.team.publisher import next_publish_time

    now = dt.datetime(2026, 10, 24, 20, 0).timestamp()
    future = now + 3600
    assert next_publish_time(future, now) == future
    late = next_publish_time(now - 3600, now, ("18:30",))
    assert dt.datetime.fromtimestamp(late) == dt.datetime(2026, 10, 25, 18, 30)
    soon = next_publish_time(now - 3600, now, ("12:00", "20:30"))
    assert dt.datetime.fromtimestamp(soon) == dt.datetime(2026, 10, 24, 20, 30)


def test_claim_respects_scheduled_at(fresh_db):
    now = time.time()
    for pid, acc, at in (("pub_a", "acc1", None), ("pub_b", "acc2", now + 3600)):
        fresh_db.create_publication({"id": pid, "project_id": "p_x", "account_id": acc,
                                     "platform": "douyin", "scheduled_at": at})
    assert [r["id"] for r in fresh_db.claim_publications()] == ["pub_a"]
    assert {r["id"] for r in fresh_db.claim_publications(now=now + 7200)} == {"pub_a", "pub_b"}


def _fake_enqueue(db, monkeypatch):
    from app.publish import service

    def enqueue(project_id, *, account_ids, overrides=None, force=False):
        rows = []
        for aid in account_ids:
            db.create_publication({"id": f"pub_{aid}", "project_id": project_id, "account_id": aid,
                                   "platform": "douyin"})
            rows.append(db.get_publication(f"pub_{aid}"))
        return {"created": rows, "skipped": [], "warnings": []}

    monkeypatch.setattr(service, "enqueue", enqueue)
    _sheet(monkeypatch)


def _sheet(monkeypatch, red=0):
    from app.team import compliance

    items = [{"key": "words", "label": "脚本用语", "level": "red" if red else "ok", "detail": "「最强」"}]
    monkeypatch.setattr(compliance, "checklist", lambda pid, title: {"items": items, "red": red, "warn": 0})


def test_cut_gate_schedules_publications(fresh_db, monkeypatch):
    from app.team import plan, publisher

    _fake_enqueue(fresh_db, monkeypatch)
    slot = time.time() + 86400
    _item(fresh_db, status="review", project_id="p_x", slot_at=slot)
    with pytest.raises(plan.PlanError):
        publisher.approve_cut("pl_1", [])
    out = publisher.approve_cut("pl_1", ["acc1"])
    assert out["item"]["status"] == "scheduled" and out["publish_at"] == slot
    assert fresh_db.get_publication("pub_acc1")["scheduled_at"] == slot
    assert fresh_db.claim_publications() == []  # 没到点不领
    assert fresh_db.get_team_setting("publish_accounts") == ["acc1"]
    with pytest.raises(plan.PlanError):
        publisher.approve_cut("pl_1", ["acc1"])  # 已经排了


def test_cut_gate_reject(fresh_db):
    from app.team import plan, publisher

    _item(fresh_db, status="review", project_id="p_x")
    assert publisher.reject_cut("pl_1", "开头太长")["status"] == "rejected"
    with pytest.raises(plan.PlanError):
        publisher.reject_cut("pl_1")


def test_publisher_sync(fresh_db):
    from app.team import publisher

    _item(fresh_db, status="scheduled", project_id="p_x")
    _item(fresh_db, id="pl_2", status="scheduled", project_id="p_y")
    fresh_db.create_publication({"id": "a", "project_id": "p_x", "account_id": "1",
                                 "platform": "douyin", "state": "published"})
    fresh_db.create_publication({"id": "b", "project_id": "p_y", "account_id": "1",
                                 "platform": "douyin", "state": "failed", "error": "登录失效"})
    assert publisher.sync() == 2
    assert fresh_db.get_plan_item("pl_1")["status"] == "published"
    item = fresh_db.get_plan_item("pl_2")
    assert item["status"] == "failed" and "登录失效" in item["note"]


# --------------------------------------------------------------------------
# 素材官
# --------------------------------------------------------------------------
def test_asset_wants_thin_pools_only(monkeypatch):
    from app.pipeline import library
    from app.team import asset

    thin = {"小米YU7": True, "理想i8": False, "问界M8": True, "乐道L90": True}
    monkeypatch.setattr(asset, "_thin", lambda name, target: thin[name])
    monkeypatch.setattr(library, "is_building", lambda name: name == "乐道L90")
    items = [{"subject": "小米YU7", "subjects": [], "category": "auto_long"},
             {"subject": "SUV 横评", "subjects": ["理想i8", "问界M8", "乐道L90", "小米YU7"],
              "category": "auto_long"}]
    got = asset.wanted(items)
    assert [(n, t) for n, _, t in got] == [("小米YU7", library.CONFIRMED_VIDEO_TARGET),
                                           ("问界M8", library.ROUNDUP_ENOUGH_SECONDS)]


def test_asset_run_skips_when_off(fresh_db, monkeypatch):
    from app import config
    from app.team import Skip, asset

    monkeypatch.setattr(config, "TEAM_ASSET_PREBUILD", False)
    with pytest.raises(Skip):
        asset.run()


# --------------------------------------------------------------------------
# 合规审核官:发布前审核单
# --------------------------------------------------------------------------
def test_checklist_flags_script_title_and_footage(fresh_db, monkeypatch):
    from app.pipeline import explainer
    from app.team import compliance

    _project(fresh_db, state="DONE")
    fresh_db.write_json("p_x", "script.json", [
        {"idx": 0, "text": "这是同级最强的一台车"},
        {"idx": 1, "text": "零百加速只要 3.6 秒"}])
    monkeypatch.setattr(explainer, "footage_mix", lambda pid, specs: {
        "seconds": 200, "third_party_ratio": 0.3, "third_party_edge_seconds": 0})
    sheet = compliance.checklist("p_x", "小米YU7 真实续航到底有多少")
    by = {i["key"]: i for i in sheet["items"]}
    assert by["words"]["level"] == "red" and "最强" in by["words"]["detail"]
    assert by["sources"]["level"] == "red" and "第 2 段" in by["sources"]["detail"]
    assert by["title"]["level"] == "ok"
    assert by["footage"]["level"] == "warn"
    assert sheet["red"] == 2
    bad = compliance.checklist("p_x", "全网第一的 SUV 只要 9 万")
    title = {i["key"]: i for i in bad["items"]}["title"]
    assert title["level"] == "red" and "第一" in title["detail"]


def test_footage_edge_is_red(fresh_db, monkeypatch):
    from app.pipeline import explainer
    from app.team import compliance

    monkeypatch.setattr(explainer, "footage_mix", lambda pid, specs: {
        "seconds": 200, "third_party_ratio": 0.1, "third_party_edge_seconds": 4})
    assert compliance._footage_entry("p_x")["level"] == "red"
    monkeypatch.setattr(explainer, "footage_mix", lambda pid, specs: {})
    assert compliance._footage_entry("p_x")["level"] == "ok"


def test_cut_gate_blocked_by_red_items(fresh_db, monkeypatch):
    from app.team import plan, publisher

    _fake_enqueue(fresh_db, monkeypatch)
    _sheet(monkeypatch, red=1)
    _item(fresh_db, status="review", project_id="p_x")
    with pytest.raises(plan.PlanError, match="红项"):
        publisher.approve_cut("pl_1", ["acc1"], title="随便")
    assert fresh_db.get_plan_item("pl_1")["status"] == "review"
    assert fresh_db.list_publications() == []


def test_cut_gate_refuses_paused_account(fresh_db, monkeypatch):
    from app.team import plan, publisher

    _fake_enqueue(fresh_db, monkeypatch)
    _item(fresh_db, status="review", project_id="p_x")
    fresh_db.pause_account("acc1", "疑似限流")
    with pytest.raises(plan.PlanError, match="暂停"):
        publisher.approve_cut("pl_1", ["acc1"])


# --------------------------------------------------------------------------
# 合规审核官:限流监测
# --------------------------------------------------------------------------
def test_paused_account_not_claimed(fresh_db):
    for pid, acc in (("pub_a", "acc1"), ("pub_b", "acc2")):
        fresh_db.create_publication({"id": pid, "project_id": "p_x", "account_id": acc, "platform": "douyin"})
    fresh_db.pause_account("acc1", "疑似限流")
    assert [r["id"] for r in fresh_db.claim_publications()] == ["pub_b"]
    assert fresh_db.resume_account("acc1") and not fresh_db.resume_account("acc1")
    assert {r["id"] for r in fresh_db.claim_publications()} == {"pub_a", "pub_b"}


def _published(db, pid, acc, at, views, *, state="published"):
    db.create_publication({"id": pid, "project_id": f"p_{pid}", "account_id": acc, "platform": "douyin",
                           "state": state, "title": pid, "published_at": at if state == "published" else None})
    if views is not None:
        db.add_snapshot(pid, "douyin", at + 24 * 3600, {"views": views})


def test_verdict_rules(fresh_db, monkeypatch):
    from app import config
    from app.team import compliance

    now = time.time()
    day = 86400
    for k, v in enumerate((1000, 1200, 800)):
        _published(fresh_db, f"old{k}", "acc1", now - (10 - k) * day, v)
    old = [fresh_db.get_publication(f"old{k}") for k in range(3)]
    _published(fresh_db, "low", "acc1", now - 2 * day, 100)
    _published(fresh_db, "fine", "acc1", now - 2 * day, 900)
    assert compliance.verdict(fresh_db.get_publication("low"), old, now)[0] == "pause"
    assert compliance.verdict(fresh_db.get_publication("fine"), old, now) == ("pass", "")
    assert compliance.verdict(fresh_db.get_publication("low"), old[:2], now) == ("pass", "")  # 样本不够
    _published(fresh_db, "fresh", "acc1", now - 3600, None)
    assert compliance.verdict(fresh_db.get_publication("fresh"), old, now) == ("wait", "")
    _published(fresh_db, "stuck", "acc1", now, None, state="submitted")
    stuck = fresh_db.get_publication("stuck")
    assert compliance.verdict(stuck, old, now)[0] == "wait"
    later = now + (config.TEAM_REVIEW_STUCK_HOURS + 1) * 3600
    what, why = compliance.verdict(stuck, old, later)
    assert what == "pause" and "审核" in why


def test_monitor_pauses_once_and_resume_sticks(fresh_db):
    from app.team import Skip, compliance

    now = time.time()
    day = 86400
    for k, v in enumerate((1000, 1200, 800)):
        _published(fresh_db, f"old{k}", "acc1", now - (10 - k) * day, v)
    _published(fresh_db, "low", "acc1", now - 2 * day, 50)
    _published(fresh_db, "other", "acc2", now - 2 * day, 50)  # 别的账号没基线,不判
    out = compliance.monitor(now)
    assert len(out) == 1 and "low" in out[0]
    assert fresh_db.get_account_health("acc1")["paused"] == 1
    assert fresh_db.get_account_health("acc2") is None
    assert [h["account_id"] for h in compliance.health()] == ["acc1"]
    fresh_db.resume_account("acc1")
    assert compliance.monitor(now) == []  # 同一条不再判
    assert not fresh_db.get_account_health("acc1")["paused"]
    with pytest.raises(Skip):
        compliance.run()
