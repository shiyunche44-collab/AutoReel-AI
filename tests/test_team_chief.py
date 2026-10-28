"""总管:连续失败升级、今日简报(模板 / LLM / LLM 出错回退)、角色顺序。"""
from __future__ import annotations

import time

import pytest


@pytest.fixture
def fresh_db(tmp_path, monkeypatch):
    from app import config, db

    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(config, "PROJECTS_DIR", tmp_path / "data" / "projects")
    monkeypatch.setattr(db, "_INITIALISED", False)
    monkeypatch.setattr(config, "TEAM_CATEGORY", "auto_long")
    return db


def _runs(db, role, statuses):
    for s in statuses:
        rid = db.start_team_run(role)
        db.finish_team_run(rid, s, f"{role} {s}")


def _item(db, **kw):
    row = {"id": "pl_1", "category": "auto_long", "slot_at": time.time() + 3600, "subject": "小米YU7",
           "angle": "a", "theme": "buyguide", "aspect": "16:9", "duration": 240, **kw}
    db.create_plan_item(row)
    return row


def test_escalates_after_consecutive_failures(fresh_db):
    from app.team import chief

    _runs(fresh_db, "analyst", ["ok", "failed", "failed", "failed"])
    esc = chief.escalations()
    assert [e["role"] for e in esc] == ["analyst"]
    assert esc[0]["failed"] == 3 and "failed" in esc[0]["last_error"]


def test_recovery_or_too_few_failures_do_not_escalate(fresh_db):
    from app.team import chief

    _runs(fresh_db, "analyst", ["failed", "failed", "failed", "ok"])  # 最近一次成功
    _runs(fresh_db, "engagement", ["failed", "failed"])  # 还不够 3 次
    assert chief.escalations() == []


def test_idle_runs_are_ignored(fresh_db):
    from app.team import chief

    _runs(fresh_db, "producer", ["failed", "idle", "failed", "idle", "failed"])
    assert [e["role"] for e in chief.escalations()] == ["producer"]


def test_snapshot_and_template_brief(fresh_db):
    from app.team import chief

    _item(fresh_db, id="pl_a", status="proposed")
    _item(fresh_db, id="pl_b", status="review", subject="小米SU7")
    snap = chief.snapshot()
    assert snap["counts"]["proposed"] == 1 and snap["counts"]["review"] == 1
    data = chief.brief(llm=None)
    assert data["source"] == "template"
    assert "1 条选题等你批" in data["text"] and "1 条成片等你审" in data["text"]


def test_llm_text_used_and_falls_back_on_error(fresh_db):
    from app.team import chief

    class Good:
        def structured(self, *a, **k):
            return {"summary": "今天有一件事。"}

    class Bad:
        def structured(self, *a, **k):
            raise RuntimeError("boom")

    assert chief.brief(llm=Good())["source"] == "llm"
    assert chief.brief(llm=Bad())["source"] == "template"


def test_llm_switch_off(fresh_db, monkeypatch):
    from app import config
    from app.team import chief

    monkeypatch.setattr(config, "TEAM_CHIEF_LLM", False)

    class Good:
        def structured(self, *a, **k):
            raise AssertionError("不该调用")

    assert chief.brief(llm=Good())["source"] == "template"


def test_run_stores_brief_and_view_shows_it(fresh_db, monkeypatch):
    from app.pipeline import orchestrator
    from app.team import Skip, chief

    monkeypatch.setattr(orchestrator, "_llm", lambda: None)
    with pytest.raises(Skip):  # 没事 → 不占团队动态,但简报照存
        chief.run()
    assert chief.view()["brief"]["text"]

    _item(fresh_db, status="proposed")
    assert "1 件事" in chief.run()


def test_chief_runs_before_editor():
    from app.team import roles

    names = [r.name for r in roles()]
    assert names.index("chief") < names.index("editor")


# --------------------------------------------------------------------------
# 全局仲裁
# --------------------------------------------------------------------------
def _project(db, pid, state="GATE_A", created=None):
    now = created or time.time()
    db.create_project({"id": pid, "title": "t", "subject": "小米YU7", "category": "auto_long",
                       "theme": "", "duration": 240, "shot_count": 0, "narration_mode": "tts",
                       "state": state, "state_reason": None, "quota_budget": 40, "spent_cost": 0,
                       "est_cost": 0, "created_at": now, "updated_at": now})


def _pub(db, pid, at, account="acc_1"):
    db.create_publication({"id": f"pub_{pid}", "project_id": pid, "account_id": account,
                           "platform": "douyin", "scheduled_at": at})


def test_daily_cap_defers_extra_but_not_rapid(fresh_db, monkeypatch):
    from app import config
    from app.team import chief

    monkeypatch.setattr(config, "TEAM_CHIEF_DAILY_MAX", 2)
    _project(fresh_db, "p_1")
    _item(fresh_db, id="pl_old", status="producing", project_id="p_1")  # 今天已开工 1 条
    now = time.time()
    items = [_item(fresh_db, id=f"pl_{k}", status="approved", slot_at=now + 100 + k) for k in range(3)]
    items.append(_item(fresh_db, id="pl_r", status="approved", kind="rapid", slot_at=now + 999))
    ready, later = chief.admit(items, now)
    assert [i["id"] for i in ready] == ["pl_r", "pl_0"]  # 快反在前、不占名额;普通的只剩 1 个名额
    assert [i["id"] for i in later] == ["pl_1", "pl_2"]


def test_projects_from_yesterday_do_not_count(fresh_db):
    from app.team import chief

    _project(fresh_db, "p_old", created=time.time() - 3 * 86400)
    _item(fresh_db, status="producing", project_id="p_old")
    assert chief.started_today() == 0


def test_arbitrate_switch_off(fresh_db, monkeypatch):
    from app import config
    from app.team import chief

    monkeypatch.setattr(config, "TEAM_CHIEF_ARBITRATE", False)
    monkeypatch.setattr(config, "TEAM_CHIEF_DAILY_MAX", 1)
    items = [_item(fresh_db, id=f"pl_{k}", status="approved") for k in range(3)]
    assert chief.admit(items) == (items, [])
    assert chief.render_busy() is False
    at = time.time() + 7200
    _pub(fresh_db, "p_1", at)
    assert chief.free_publish_time(at, ["acc_1"]) == at


def test_producer_run_respects_cap(fresh_db, monkeypatch):
    from app import config
    from app.team import Skip, producer

    monkeypatch.setattr(config, "TEAM_CHIEF_DAILY_MAX", 1)
    monkeypatch.setattr(config, "TEAM_LEAD_HOURS", 30)
    started = []
    monkeypatch.setattr(producer, "start", lambda iid, **k: started.append(iid) or "p_" + iid)
    monkeypatch.setattr(producer, "sync", lambda **k: 0)
    now = time.time()
    for k in range(3):
        _item(fresh_db, id=f"pl_{k}", status="approved", slot_at=now + 3600 + k)
    producer.run(now)
    assert started == ["pl_0"]


def test_render_busy_blocks_gate_and_asset(fresh_db, monkeypatch):
    from app import config
    from app.team import Skip, asset, chief, producer

    monkeypatch.setattr(config, "PIPELINE_CONCURRENCY", 1)
    assert chief.render_busy() is False
    _project(fresh_db, "p_r", state="DRAFT_RENDERING")
    assert chief.render_busy() is True
    monkeypatch.setattr(config, "TEAM_ASSET_PREBUILD", True)
    monkeypatch.setattr(config, "USE_LIBRARY", True)
    with pytest.raises(Skip, match="让路"):
        asset.run()
    # 卡死很久的渲染不占名额
    with fresh_db.connect() as conn:
        conn.execute("UPDATE projects SET updated_at = ? WHERE id = 'p_r'", (time.time() - 5 * 3600,))
    assert chief.render_busy() is False

    _project(fresh_db, "p_g")
    _item(fresh_db, status="producing", project_id="p_g")
    _project(fresh_db, "p_r2", state="FINAL_RENDERING")
    monkeypatch.setattr(producer, "_gate_async", lambda *a, **k: pytest.fail("不该放闸"))
    assert producer.sync(background=False) == 0


def test_publish_conflict_shifts_but_not_rapid(fresh_db):
    import datetime as dt

    from app.team import chief

    base = dt.datetime.combine(dt.date.today() + dt.timedelta(days=2), dt.time(12, 0)).timestamp()
    _project(fresh_db, "p_1")
    _pub(fresh_db, "p_1", base)
    moved = chief.free_publish_time(base + 600, ["acc_1"], subject="YU7")
    assert moved == base + 3600  # 隔满 60 分钟
    assert "挪动" in chief.actions()[-1]["text"]
    assert chief.free_publish_time(base + 600, ["acc_2"]) == base + 600  # 别的账号不受影响
    assert chief.free_publish_time(base + 600, ["acc_1"], rapid=True) == base + 600  # 快反不挪


def test_publish_shift_skips_quiet_hours(fresh_db):
    import datetime as dt

    from app.team import chief

    day = dt.date.today() + dt.timedelta(days=2)
    late = dt.datetime.combine(day, dt.time(22, 30)).timestamp()
    _project(fresh_db, "p_1")
    _pub(fresh_db, "p_1", late)
    got = dt.datetime.fromtimestamp(chief.free_publish_time(late, ["acc_1"]))
    assert got == dt.datetime.combine(day + dt.timedelta(days=1), dt.time(8, 0))


def test_brief_mentions_arbitration(fresh_db, monkeypatch):
    from app import config
    from app.team import chief

    monkeypatch.setattr(config, "TEAM_CHIEF_DAILY_MAX", 1)
    monkeypatch.setattr(config, "TEAM_LEAD_HOURS", 30)
    _project(fresh_db, "p_1")
    _item(fresh_db, id="pl_old", status="producing", project_id="p_1")
    _item(fresh_db, id="pl_x", status="approved", slot_at=time.time() + 3600)
    assert "顺延" in chief.brief(llm=None)["text"]
