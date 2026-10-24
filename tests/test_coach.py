"""复盘官:特征抽取、收缩加权和小样本保护、打法手册回灌(变体 / 标题顺序 / 选题排序)、周报。"""
from __future__ import annotations

import datetime as dt
import json
import time

import pytest


@pytest.fixture
def fresh_db(tmp_path, monkeypatch):
    from app import config, db

    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(config, "PROJECTS_DIR", tmp_path / "data" / "projects")
    monkeypatch.setattr(db, "_INITIALISED", False)
    return db


@pytest.fixture(autouse=True)
def no_playbook():
    """每个测试前后都把回灌钩子摘掉,别影响别的测试的变体 / 标题顺序。"""
    from app.team import playbook

    playbook.uninstall()
    yield
    playbook.uninstall()


def _row(pid, views, *, acc="acc1", rate=None, **feat):
    return {"pub_id": pid, "account_id": acc, "project_id": f"p_{pid}", "title": f"作品{pid}",
            "published_at": 0.0, "features": feat,
            "outcome": {"views": views, "completion_rate": rate}}


# --------------------------------------------------------------------------
# 统计
# --------------------------------------------------------------------------
def test_lifts_use_per_account_median():
    from app.team import stats

    rows = [_row("a", 100), _row("b", 200), _row("c", 300),
            _row("x", 10, acc="small"), _row("y", 30, acc="small")]
    lifted = {r["pub_id"]: r["lift"]["views"] for r in stats.lifts(rows)}
    assert lifted["a"] == pytest.approx(0.5) and lifted["c"] == pytest.approx(1.5)
    assert lifted["x"] == pytest.approx(0.5)  # 小号和自己比,不和大号比


def test_learn_shrinks_and_needs_three_samples():
    from app.team import stats

    rows = ([_row(f"s{i}", 300, opening="scene") for i in range(4)]
            + [_row(f"c{i}", 100, opening="conflict") for i in range(4)]
            + [_row("q1", 1000, acc="b", opening="question"), _row("q2", 900, acc="b", opening="question")])
    got = {r["id"]: r for r in stats.learn(rows, "2026-W43")}
    # 中位数 = 200;scene 每条 1.5 倍 → (6 + 2) / (4 + 2) = 1.333
    assert got["opening:scene:views"]["effect"] == pytest.approx(4 / 3, abs=1e-3)
    assert got["opening:scene:views"]["confidence"] == "medium"
    assert got["opening:conflict:views"]["effect"] == pytest.approx(4 / 6, abs=1e-3)
    assert "opening:question:views" not in got  # 只有 2 条,不下结论
    assert len(got["opening:scene:views"]["evidence"]) == 4


def test_learn_ignores_small_effects():
    from app.team import stats

    rows = [_row(f"a{i}", 100 + i, ending="action") for i in range(8)]
    assert stats.learn(rows, "w") == []


# --------------------------------------------------------------------------
# 特征抽取(读库)
# --------------------------------------------------------------------------
def _project(db, pid, *, opening="scene", ending="action", subject="小米YU7"):
    now = time.time()
    db.create_project({"id": pid, "title": "t", "subject": subject, "category": "auto_long",
                       "theme": "single", "duration": 240, "shot_count": 0,
                       "narration_mode": "tts", "state": "DONE", "state_reason": None,
                       "quota_budget": 40, "spent_cost": 0, "est_cost": 0,
                       "variant": json.dumps({"opening": opening, "ending": ending, "look": "ink"}),
                       "created_at": now, "updated_at": now})


def _published(db, pid, *, t0, views, hours=24.0, kind="number", rate=0.1):
    _project(db, f"p_{pid}")
    db.create_publication({"id": pid, "project_id": f"p_{pid}", "account_id": "acc1",
                           "platform": "douyin", "title": f"作品{pid}", "state": "published",
                           "remote_id": f"w_{pid}", "published_at": t0,
                           "variant": json.dumps({"title_kind": kind, "cover": "a"})})
    db.add_snapshot(pid, "douyin", t0 + 3600, {"views": 1, "hours_since_publish": 1.0})
    db.add_snapshot(pid, "douyin", t0 + hours * 3600,
                    {"views": views, "completion_rate": rate, "likes": 5,
                     "hours_since_publish": hours})


def test_rows_take_day_snapshot(fresh_db):
    from app.team import features

    t0 = time.time() - 3 * 86400
    _published(fresh_db, "a", t0=t0, views=500)
    _published(fresh_db, "b", t0=t0, views=999, hours=8)  # 还没有 24 小时那张
    rows = features.rows()
    assert [r["pub_id"] for r in rows] == ["a"]
    r = rows[0]
    assert r["outcome"]["views"] == 500 and r["outcome"]["engagement"] == 0.01
    f = r["features"]
    assert (f["opening"], f["ending"], f["look"], f["title_kind"], f["cover"]) == \
        ("scene", "action", "ink", "number", "a")
    assert f["duration"] == "3-5min" and f["subject"] == "小米YU7" and f["weekday"].startswith("周")
    assert features.value_label("title_kind", "number") == "数字"


# --------------------------------------------------------------------------
# 回灌
# --------------------------------------------------------------------------
def _learn(db, dim, value, effect, metric="views"):
    db.replace_learnings("w", [{"id": f"{dim}:{value}:{metric}", "week": "w", "dimension": dim,
                                "value": value, "metric": metric, "effect": effect, "n": 5,
                                "confidence": "medium", "evidence": []}])


def test_weights_combine_and_clip(fresh_db):
    from app.team import playbook

    rows = [
        {"dimension": "opening", "value": "scene", "metric": "views", "effect": 1.5},
        {"dimension": "opening", "value": "scene", "metric": "completion_rate", "effect": 1.6},
        {"dimension": "opening", "value": "conflict", "metric": "views", "effect": 0.3},
        {"dimension": "ending", "value": "action", "metric": "views", "effect": 1.2},
    ]
    w = playbook.weights("opening", rows)
    assert w == {"scene": 2.0, "conflict": 0.5}
    assert playbook.weights("look", rows) == {}


def test_variety_unchanged_without_playbook(fresh_db):
    """没挂手册 / 挂了但没结论:挑法逐字和以前一样(golden 不动)。"""
    from app.pipeline import variety
    from app.team import playbook

    from app.categories import load_profile

    theme = load_profile("auto_long").themes["buyguide"]
    before = [variety.pick(theme, f"p{i}") for i in range(20)]
    playbook.install()
    assert [variety.pick(theme, f"p{i}") for i in range(20)] == before


def test_variety_leans_on_playbook_but_explores(fresh_db):
    from app.pipeline import variety
    from app.team import playbook

    from app.categories import load_profile

    theme = load_profile("auto_long").themes["buyguide"]
    openings = variety.allowed_openings(theme)
    best = openings[-1]
    playbook.install()
    fresh_db.replace_learnings("w", [
        {"id": f"opening:{o}:views", "week": "w", "dimension": "opening", "value": o,
         "metric": "views", "effect": 2.0 if o == best else 0.5, "n": 6, "confidence": "high",
         "evidence": []} for o in openings])
    picks = [variety.pick(theme, f"p{i}")["opening"] for i in range(200)]
    share = picks.count(best) / len(picks)
    assert share > 0.35  # 均匀挑是 0.2;权重 4 倍 + 30% 探索约 0.5
    assert len(set(picks)) == len(openings)  # 别的也还有机会(探索)
    assert variety.pick(theme, "p1") == variety.pick(theme, "p1")  # 可复现


def test_title_order_follows_playbook(fresh_db):
    from app.pipeline import packaging
    from app.team import playbook

    assert packaging.title_order("p1") == packaging.KINDS
    playbook.install()
    assert packaging.title_order("p1") == packaging.KINDS  # 没结论
    _learn(fresh_db, "title_kind", "person", 2.0)
    firsts = [packaging.title_order(f"p{i}")[0] for i in range(200)]
    assert firsts.count("person") > 80  # 均匀挑约 67;权重 2 + 30% 探索约 90
    titles, _ = packaging.build_titles(None, "小米YU7", allowed=set(),
                                       order=packaging.title_order("p1"))
    assert [t.kind for t in titles] == list(packaging.title_order("p1"))


def test_editor_rank_uses_weights_and_off_learnings_dont_count(fresh_db):
    from types import SimpleNamespace

    from app.team import editor, playbook

    a = SimpleNamespace(subject="小米YU7", subjects=[], theme="single", heat=4, why_hot="")
    b = SimpleNamespace(subject="理想i8", subjects=[], theme="single", heat=3, why_hot="")
    assert editor.rank_ideas([a, b], set(), set()) == [a, b]
    _learn(fresh_db, "subject", "理想i8", 1.8)
    w = {d: playbook.weights(d) for d in ("theme", "subject")}
    assert editor.rank_ideas([a, b], set(), set(), w) == [b, a]
    assert "加权" in editor._rationale(b, False, editor.learned_weight(b, w))
    assert "理想i8" in playbook.prompt_text()
    fresh_db.set_learning_status("subject:理想i8:views", "off")
    assert playbook.weights("subject") == {} and playbook.prompt_text() == ""
    # 下周重算时停用的保持停用
    _learn(fresh_db, "subject", "理想i8", 1.9)
    assert fresh_db.list_learnings(("off",))[0]["effect"] == 1.9
    assert fresh_db.list_learnings() == []


def test_replace_learnings_marks_missing_stale(fresh_db):
    _learn(fresh_db, "opening", "scene", 1.5)
    _learn(fresh_db, "ending", "action", 0.7)
    assert [r["id"] for r in fresh_db.list_learnings()] == ["ending:action:views"]
    assert [r["id"] for r in fresh_db.list_learnings(("stale",))] == ["opening:scene:views"]


# --------------------------------------------------------------------------
# 周报
# --------------------------------------------------------------------------
def test_review_writes_playbook_and_report(fresh_db):
    from app.team import Skip, coach

    now = dt.datetime(2026, 10, 25, 20, 0)
    with pytest.raises(Skip):
        coach.review(now)
    t0 = now.timestamp() - 2 * 86400
    for i in range(4):
        _published(fresh_db, f"n{i}", t0=t0 - i * 3600, views=100, kind="number")
        _published(fresh_db, f"p{i}", t0=t0 - i * 3600, views=400, kind="person")
    report = coach.review(now)  # 没有 LLM → 模板
    assert report["source"] == "template" and "本周新发 8 条" in report["summary"]
    assert report["totals"]["works"] == 8 and report["top"][0]["views"] == 400
    ids = {r["id"] for r in fresh_db.list_learnings()}
    assert {"title_kind:person:views", "title_kind:number:views"} <= ids
    assert any("标题写法 = 人群" in x for x in report["learnings"])
    exp = {e["dimension"]: e for e in report["experiments"]}
    assert exp["title_kind"]["value"] == "stake" and exp["title_kind"]["why"] == "还没试过"
    saved = fresh_db.latest_report()
    assert saved["week"] == "2026-W43" and saved["summary"] == report["summary"]


def test_review_llm_text_and_fallback(fresh_db):
    from app.team import coach

    report = {"window_days": 30, "learnings": [], "top": [], "bottom": [], "experiments": [],
              "totals": {"works": 2, "week_works": 1, "week_views": 50, "week_followers": 0,
                         "median_views": 40, "completion_rate": None}}

    class Good:
        def structured(self, system, user, **kw):
            assert "本周新发 1 条" in user
            return {"summary": "本周发了 1 条。", "next": ["多试人群标题"]}

    class Bad:
        def structured(self, *a, **kw):
            raise RuntimeError("boom")

    assert coach.write_text(report, Good()) == {"summary": "本周发了 1 条。",
                                                "next": ["多试人群标题"], "source": "llm"}
    assert coach.write_text(report, Bad())["source"] == "template"
    assert coach.week_key(dt.datetime(2026, 1, 1)) == "2026-W01"
