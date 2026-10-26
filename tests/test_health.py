"""流程体检 + 白名单自动调参:各阶段耗时 / 失败、门禁拦截原因、素材不够、调参上下限与留痕。"""
from __future__ import annotations

import time

import pytest

from tests.test_coach import _project, _published, fresh_db, no_playbook  # noqa: F401

NOW = time.time()
DAY = 86400


@pytest.fixture(autouse=True)
def defaults():
    """每个测试后把可调参数放回默认值(调参会改模块常量)。"""
    from app.team import health

    yield
    health.restore()
    health._DEFAULTS.clear()


def _ev(db, pid, dst, at, src=None, reason=""):
    db.add_event(pid, dst, src=src, reason=reason)
    with db.connect() as conn:
        conn.execute("UPDATE job_events SET created_at = ? WHERE id = (SELECT MAX(id) FROM job_events)", (at,))


def _shots(db, pid, statuses):
    keys = ("model", "provider_task_id", "submitted_at", "last_polled_at", "draft_path", "final_path", "error")
    db.upsert_shots(pid, [{**dict.fromkeys(keys), "idx": i, "stage": "draft", "status": st,
                           "attempts": 2 if st == "failed" else 1} for i, st in enumerate(statuses)])


def _run(db, pid, t0, *, fail_at=None, done=True):
    """一条项目:采集 10 分钟 → 渲染 30 分钟 → 门禁 A → 完成(或在渲染时失败)。"""
    _project(db, pid)
    _ev(db, pid, "DRAFT_INTENT", t0, src="CREATED")
    _ev(db, pid, "DRAFT_RENDERING", t0 + 600, src="DRAFT_INTENT")
    if fail_at:
        _ev(db, pid, "FAILED", t0 + fail_at, src="DRAFT_RENDERING", reason="第 3 拍没渲染成功")
        return
    _ev(db, pid, "GATE_A", t0 + 2400, src="DRAFT_RENDERING")
    if done:
        _ev(db, pid, "DONE", t0 + 3000, src="GATE_A")


# --------------------------------------------------------------------------
# 量
# --------------------------------------------------------------------------
def test_stages_time_and_failures(fresh_db):
    from app.team import health

    t0 = NOW - DAY
    _run(fresh_db, "p1", t0)
    _run(fresh_db, "p2", t0 + 100)
    _run(fresh_db, "p3", t0 + 200, fail_at=900)
    _run(fresh_db, "old", NOW - 30 * DAY)  # 窗口外的不算
    got = {s["stage"]: s for s in health.stages(health._events_since(NOW - 7 * DAY), NOW - 7 * DAY)}
    assert got["DRAFT_INTENT"]["median_min"] == 10.0 and got["DRAFT_INTENT"]["n"] == 3
    r = got["DRAFT_RENDERING"]
    assert r["label"] == "渲染各拍" and r["failed"] == 1 and r["n"] == 3
    assert r["median_min"] == 30.0 and r["fail_rate"] == pytest.approx(0.333, abs=1e-3)
    assert "DONE" not in got


def test_gate_blocks_normalise_numbers(fresh_db):
    from app.team import health

    _project(fresh_db, "p1")
    _project(fresh_db, "p2")
    _ev(fresh_db, "p1", "team", NOW - 100, src="GATE_A", reason="拦下:门禁 A 没放行:第 3, 7 拍没渲染成功;配音缺 2 句")
    _ev(fresh_db, "p2", "team", NOW - 50, src="GATE_A", reason="拦下:第 12 拍没渲染成功")
    _ev(fresh_db, "p2", "team", NOW - 40, src="GATE_B", reason="拦下:标题太长")
    _ev(fresh_db, "p2", "team", NOW - 30, reason="素材池后台构建已启动")  # 不是拦截
    top = health.gate_blocks(health._events_since(NOW - DAY), NOW - DAY)
    assert top[0] == {"reason": "第 N 拍没渲染成功", "gate": "A", "n": 2}
    assert {"reason": "标题太长", "gate": "B", "n": 1} in top and len(top) == 3


def test_thin_signals_from_features_gates_and_notes(fresh_db):
    from app.team import health

    _project(fresh_db, "thin")
    _ev(fresh_db, "thin", "DONE", NOW - 100, src="GATE_B")
    fresh_db.write_json("thin", "features.json", {"official_video_ratio": 0.2, "still_ratio": 0.1})
    _project(fresh_db, "rich")
    _ev(fresh_db, "rich", "DONE", NOW - 100, src="GATE_B")
    fresh_db.write_json("rich", "features.json", {"official_video_ratio": 0.8, "still_ratio": 0.05})
    _project(fresh_db, "gate")
    _ev(fresh_db, "gate", "team", NOW - 100, src="GATE_A", reason="拦下:官方素材太少")
    fresh_db.create_plan_item({"id": "it1", "category": "auto_long", "slot_at": NOW, "subject": "x",
                               "note": "素材不够,先搁着"})
    got = health.thin_signals(health._events_since(NOW - DAY), NOW - DAY)
    assert len(got) == 3
    assert any(s.startswith("thin:") for s in got) and any(s.startswith("gate:") for s in got)
    assert any(s.startswith("it1:") for s in got)


def test_render_and_cost(fresh_db):
    from app.team import health

    _run(fresh_db, "p1", NOW - DAY)
    fresh_db.update_project("p1", spent_cost=0.5)
    _shots(fresh_db, "p1", ["succeeded"] * 8 + ["failed"] * 2)
    ev = health._events_since(NOW - 7 * DAY)
    assert health.render_stats(ev) == {"shots": 10, "failed": 2, "retried": 2, "fail_rate": 0.2}
    assert health.costs(ev, NOW - 7 * DAY) == {"done": 1, "per_video": 0.5, "total": 0.5, "budget": 40.0}


def test_hold_records_a_gate_event(fresh_db):
    from app.team import producer

    _project(fresh_db, "p1")
    fresh_db.create_plan_item({"id": "it1", "category": "auto_long", "slot_at": NOW, "subject": "x",
                               "project_id": "p1"})
    producer._hold("it1", "GATE_A", "第 3 拍没渲染成功")
    ev = [e for e in fresh_db.get_events("p1") if e["dst"] == "team"]
    assert ev and ev[0]["src"] == "GATE_A" and ev[0]["reason"] == "拦下:第 3 拍没渲染成功"
    assert fresh_db.get_plan_item("it1")["hold"] == "第 3 拍没渲染成功"


# --------------------------------------------------------------------------
# 调
# --------------------------------------------------------------------------
def _h(thin=0, tts=0, shots=0, failed=0):
    return {"stages": [], "gates": [], "thin": ["x"] * thin, "tts": tts, "roles": [],
            "render": {"shots": shots, "failed": failed, "fail_rate": failed / shots if shots else 0.0},
            "cost": {"done": 0, "per_video": None, "total": 0, "budget": None}}


def test_tune_steps_within_bounds_once_a_week(fresh_db):
    from app import config
    from app.team import health

    base = config.TEAM_LEAD_HOURS
    h = _h(thin=2, tts=2, shots=40, failed=10)
    changes = {c["key"]: c for c in health.tune(h, "w1", NOW)}
    assert set(changes) == {"lead_hours", "video_target", "tts_rounds", "motion_workers"}
    assert config.TEAM_LEAD_HOURS == base + 6
    assert changes["motion_workers"]["new"] == changes["motion_workers"]["old"] - 1
    assert health.tune(h, "w1", NOW) == []  # 同一周不再动
    for i in range(2, 10):
        health.tune(h, f"w{i}", NOW)
    got = fresh_db.get_team_setting(health.TUNING_KEY)
    assert got["lead_hours"] == 48 and got["video_target"] == 300 and got["tts_rounds"] == 9
    assert got["motion_workers"] == 1
    assert health.tune(h, "w99", NOW) == []  # 全到边界:不再动、不再记
    logs = [l for l in fresh_db.list_iteration_log(limit=200) if l["kind"] == "param"]
    assert any("开工提前量" in l["title"] and "素材不够" in l["title"] for l in logs)


def test_nothing_wrong_means_no_change(fresh_db):
    from app.team import health

    assert health.tune(_h(thin=1, tts=1, shots=10, failed=5), "w1", NOW) == []
    assert fresh_db.get_team_setting(health.TUNING_KEY) == {}


def test_apply_restore_and_reset(fresh_db):
    from app import config
    from app.pipeline import library
    from app.team import health

    base_lead, base_target = config.TEAM_LEAD_HOURS, library.CONFIRMED_VIDEO_TARGET
    fresh_db.set_team_setting(health.TUNING_KEY, {"lead_hours": 42, "video_target": 240})
    health.apply()
    assert (config.TEAM_LEAD_HOURS, library.CONFIRMED_VIDEO_TARGET) == (42, 240)
    view = {t["key"]: t for t in health.tuned_view()}
    assert view["lead_hours"]["default"] == base_lead and view["lead_hours"]["bound"] == 48
    assert health.reset("lead_hours", NOW) is True
    assert config.TEAM_LEAD_HOURS == base_lead and health.reset("lead_hours") is False
    assert "lead_hours" not in fresh_db.get_team_setting(health.TUNING_KEY)
    health.restore()
    assert library.CONFIRMED_VIDEO_TARGET == base_target


def test_reset_endpoint(fresh_db):
    from fastapi.testclient import TestClient

    from app.main import app
    from app.team import health

    fresh_db.set_team_setting(health.TUNING_KEY, {"tts_rounds": 7})
    health.apply()
    c = TestClient(app)
    assert c.post("/api/team/tuning/nope/reset").status_code == 404
    assert c.post("/api/team/tuning/tts_rounds/reset").json() == {"ok": True}
    assert c.post("/api/team/tuning/tts_rounds/reset").status_code == 404


# --------------------------------------------------------------------------
# 周报
# --------------------------------------------------------------------------
def test_advice_and_report_lines():
    from app.team import health

    h = _h(thin=2)
    h["stages"] = [{"stage": "DRAFT_RENDERING", "label": "渲染各拍", "n": 5, "median_min": 30.0,
                    "failed": 2, "fail_rate": 0.4}]
    h["gates"] = [{"reason": "第 N 拍没渲染成功", "gate": "A", "n": 3}]
    h["roles"] = [{"role": "analyst", "label": "分析师", "runs": 9, "failed": 2, "last_error": "连接断了"}]
    h["cost"] = {"done": 2, "per_video": 35.0, "total": 70.0, "budget": 40.0}
    h["advice"] = health.advice(h)
    h["changes"] = [{"key": "lead_hours", "label": "开工提前量", "unit": "小时", "old": 30.0, "new": 36.0,
                     "why": "这周 2 次素材不够"}]
    assert len(h["advice"]) == 4
    text = "\n".join(health.report_lines(h))
    assert "最慢的一段是「渲染各拍」" in text and "素材不够 2 次" in text
    assert "自动调参:开工提前量 30→36 小时" in text and "分析师这周失败 2 次" in text
    assert health.report_lines(None) == []


def test_coach_report_carries_health(fresh_db, monkeypatch):
    from app.team import coach

    _run(fresh_db, "p1", time.time() - DAY)
    _published(fresh_db, "w1", t0=time.time() - 3 * DAY, views=1000)
    monkeypatch.setattr(coach, "write_text", lambda report, llm: {"summary": "", "next": []})
    report = coach.review(llm=None)
    assert report["health"]["cost"]["done"] == 1
    assert any(s["stage"] == "DRAFT_RENDERING" for s in report["health"]["stages"])
