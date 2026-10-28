"""阶段 6:互动效果(回复写法 → 评论增量 / 再留言;评论问题聚成选题;活跃官效果 → 额度减半)。"""
from __future__ import annotations

import datetime as dt
import time

import pytest

from tests.test_coach import fresh_db, no_playbook  # noqa: F401 - fixtures
from tests.test_engagement import _pub

H = 3600


def _comment(db, cid, *, at, pub="pub1", author="路人", text="多少钱?", kind="question", likes=0,
             style="", sent_at=None, source="template"):
    db.add_comments([{"id": cid, "publication_id": pub, "author": author, "text": text,
                      "created_at": at, "likes": likes, "kind": kind, "reply_style": style,
                      "reply_draft": "谢谢", "reply_source": source}])
    if sent_at:
        db.update_comment(cid, reply_state="sent", replied_at=sent_at)


def test_pick_style_is_stable_and_follows_weights():
    from app.team import engagement

    assert engagement.pick_style("c1") == engagement.pick_style("c1")
    picks = [engagement.pick_style(f"c{i}") for i in range(300)]
    assert set(picks) == set(engagement.REPLY_STYLES)
    leaned = [engagement.pick_style(f"c{i}", {"ask": 2.0, "info": 0.5, "thanks": 0.5})
              for i in range(300)]
    assert leaned.count("ask") > picks.count("ask") + 50
    assert engagement.template_draft("question", "ask")["reply_draft"] == \
        engagement.STYLE_TEMPLATES["ask"]["question"]
    assert engagement.template_draft("ad", "ask")["reply_state"] == "skipped"


def test_reply_outcomes_count_more_comments_and_return_visits(fresh_db):
    from app.team import engagement

    now = time.time()
    _pub(fresh_db, age_days=5)
    t0 = now - 3 * 86400
    _comment(fresh_db, "a", at=t0, author="甲", style="ask", sent_at=t0 + H)
    _comment(fresh_db, "b", at=t0 + 2 * H, author="乙", kind="praise")  # 回复后 24 小时内
    _comment(fresh_db, "c", at=t0 + 5 * H, author="甲")                 # 甲又来了
    _comment(fresh_db, "d", at=t0 + 40 * H, author="丙")                # 超过 24 小时
    _comment(fresh_db, "fresh", at=now - 2 * H, style="ask", sent_at=now - H)    # 还没满 24 小时
    _comment(fresh_db, "mine", at=t0, style="ask", sent_at=t0 + H, source="user")  # 你写的不算写法
    got = {o["id"]: o for o in engagement.reply_outcomes(now)}
    assert set(got) == {"a"}
    assert got["a"]["more"] == 2 and got["a"]["again"] is True  # b、c 在窗口里,d 晚了


def test_learn_styles_shrinks_and_needs_samples():
    from app.team import engagement

    def outs(style, more, n, again=False):
        return [{"id": f"{style}{i}", "style": style, "kind": "question", "more": more,
                 "again": again, "text": "x"} for i in range(n)]

    few = outs("ask", 4, 3) + outs("info", 1, 6)
    assert {r["value"] for r in engagement.learn_styles(few, "w")} == {"info"}  # 反问只有 3 条
    rows = {r["id"]: r for r in engagement.learn_styles(outs("ask", 4, 10) + outs("info", 1, 10), "w")}
    ask = rows["reply:reply_style:ask:reply_more"]
    # 平均 2.5;收缩:(40 + 5 × 2.5) / 15 / 2.5 = 1.4 → 夹到 1.3
    assert ask["effect"] == 1.3 and ask["n"] == 10 and ask["dimension"] == "reply_style"
    assert rows["reply:reply_style:info:reply_more"]["effect"] == 0.7
    assert not [r for r in rows if r.endswith("reply_again")]  # 没人再留言:没得比
    rep = {s["style"]: s for s in engagement.style_report(outs("ask", 4, 2, again=True))}
    assert rep["ask"] == {"style": "ask", "label": "反问", "n": 2, "more": 4.0, "again": 1.0}


def test_style_learnings_feed_back_and_describe(fresh_db):
    from app.team import engagement, playbook

    fresh_db.replace_learnings("w", [{
        "id": "reply:reply_style:ask:reply_more", "dimension": "reply_style", "value": "ask",
        "metric": "reply_more", "effect": 1.25, "n": 8, "confidence": "medium", "evidence": []}],
        source="reply")
    r = fresh_db.list_learnings()[0]
    assert playbook.describe(r) == "回复写法 = 反问:回复后 24 小时评论增量 1.25 倍(8 条回复,置信度中)"
    assert engagement._style_weights() == {}
    playbook.install()
    assert engagement._style_weights() == {"ask": 1.25}
    playbook.uninstall()
    assert engagement.style_weights is None


def test_audience_topics_cluster_by_car(fresh_db):
    from app.team import engagement

    now = time.time()
    _pub(fresh_db)
    _comment(fresh_db, "q1", at=now - H, text="理想i8 续航到底多少?")
    _comment(fresh_db, "q2", at=now - 2 * H, text="理想 i8 和问界哪个好", likes=1)
    _comment(fresh_db, "q3", at=now - H, text="小米YU7 啥时候提车?")
    _comment(fresh_db, "q4", at=now - H, text="这台多少钱?", likes=6)   # 没点名,作品也查不到车 → 不算
    _comment(fresh_db, "q5", at=now - 20 * 86400, text="理想i8 呢?")   # 太老
    cars = ["小米YU7", "理想i8"]
    topics = engagement.audience_topics(now, cars=cars)
    assert [(t["car"], t["n"]) for t in topics] == [("理想i8", 2)]  # 小米只被问了 1 条、0 赞
    assert topics[0]["samples"][0] == "理想 i8 和问界哪个好"  # 赞多的在前
    assert engagement.topics_text(topics) == "观众问得最多的车(优先排一期专门回答):理想i8(2 条)"


def test_audience_topics_fall_back_to_the_work_subject(fresh_db, monkeypatch):
    from app.team import engagement

    now = time.time()
    _pub(fresh_db)
    monkeypatch.setattr(fresh_db, "get_project", lambda pid: {"subject": "小米YU7", "subjects": []})
    _comment(fresh_db, "q3", at=now - H, text="小米YU7 啥时候提车?")
    _comment(fresh_db, "q4", at=now - H, text="这台多少钱?")  # 没点名 → 算作品讲的那台
    topics = engagement.audience_topics(now, cars=["小米YU7"])
    assert [(t["car"], t["n"]) for t in topics] == [("小米YU7", 2)]


def test_editor_boosts_and_tags_asked_cars(fresh_db, monkeypatch):
    from app import config
    from app.categories import load_profile
    from app.team import editor
    from tests.test_team import FakeLLM, _seed_trends

    monkeypatch.setattr(config, "TEAM_PLAN_DAYS", 1)
    monkeypatch.setattr(config, "TEAM_DAILY_SLOTS", 1)
    monkeypatch.setattr(config, "TEAM_SLOT_TIMES", ("18:30",))
    _seed_trends(fresh_db)
    _pub(fresh_db)
    # 固定在当天上午:窗口只有 1 天 1 个档期(默认 18:30),用真实时间的话过了档期就排满了
    now = dt.datetime.now().replace(hour=8, minute=0, second=0, microsecond=0).timestamp()
    _comment(fresh_db, "q1", at=now - H, text="理想i8 续航到底多少?")
    _comment(fresh_db, "q2", at=now - 2 * H, text="理想i8 能不能拉露营装备?")
    theme = load_profile("auto_long").default_theme
    llm = FakeLLM([
        {"subject": "小米YU7", "angle": "月销 3 万背后", "theme": theme, "heat": 4},
        {"subject": "理想i8", "angle": "理想纯电 SUV 值不值", "theme": theme, "heat": 3},
    ])
    editor.run("auto_long", llm=llm, now=dt.datetime.fromtimestamp(now))
    items = fresh_db.list_plan_items("auto_long")
    assert [i["subject"] for i in items] == ["理想i8"]  # 观众问的 +1 热度,同分时排前
    assert items[0]["rationale"].startswith("观众问的:2 条评论问到理想i8")


# --------------------------------------------------------------------------
# 粉丝快照 + 活跃官效果
# --------------------------------------------------------------------------
def test_analyst_records_daily_followers_even_without_due_works(fresh_db):
    from app.team import Skip, analyst
    from tests.test_analyst import _work

    now = time.time()
    works = [_work("w1", title="随便", at=now - 40 * 86400, views=5)]
    calls = []

    def fetch(since):
        calls.append(since)
        return works

    got = analyst.collect(now, fetch=fetch)
    assert got["daily"] == 1 and calls  # 没到点的作品,也单独开一次记粉丝
    assert fresh_db.list_account_daily("douyin") == {dt.date.fromtimestamp(now).isoformat(): 690}
    with pytest.raises(Skip):  # 今天记过了,又没别的事:不开浏览器
        analyst.collect(now + 60, fetch=fetch)
    assert len(calls) == 1


def _history(db, gains, actions, today):
    """今天往前 len(gains) 天:每天的互动数 + 当天到第二天的涨粉。"""
    n = len(gains)
    start = today - dt.timedelta(days=n)
    followers = 1000
    db.save_account_daily("douyin", start.isoformat(), followers)
    for i, (gain, acts) in enumerate(zip(gains, actions)):
        day = (start + dt.timedelta(days=i)).isoformat()
        followers += gain
        db.save_account_daily("douyin", (start + dt.timedelta(days=i + 1)).isoformat(), followers)
        db.add_outbound_actions([{"id": f"{day}-{k}", "day": day, "action": "like", "target": f"{day}-{k}",
                                  "state": "done", "created_at": time.time() - (n - i) * 86400}
                                 for k in range(acts)])


def test_effect_compares_busy_and_quiet_days(fresh_db):
    from app.team import activator

    now = time.time()
    today = dt.date.fromtimestamp(now)
    assert activator.effect(now)["verdict"] == "unknown"
    _history(fresh_db, [5, 1] * 5, [6, 1] * 5, today)
    got = activator.effect(now)
    assert got["verdict"] == "effective"
    assert got["high"] == {"days": 5, "actions": 6, "gain": 5} and got["low"]["gain"] == 1


def test_effect_unknown_when_every_day_is_the_same(fresh_db):
    from app.team import activator

    now = time.time()
    _history(fresh_db, [2] * 10, [5] * 10, dt.date.fromtimestamp(now))
    got = activator.effect(now)
    assert got["verdict"] == "unknown" and "差不多" in got["why"]


def test_four_weeks_without_effect_halve_quotas(fresh_db):
    from app.team import activator

    now = time.time()
    _history(fresh_db, [2] * 10, [6, 1] * 5, dt.date.fromtimestamp(now))
    assert activator.effect(now)["verdict"] == "none"
    for i in range(3):
        got = activator.weekly_effect(now, f"2026-W{i}")
        assert got["streak"] == i + 1 and not got["halved"]
    assert activator.weekly_effect(now, "2026-W2")["streak"] == 3  # 同一周重复跑只算一次
    got = activator.weekly_effect(now, "2026-W3")
    assert got["halved"] == {"comment": [10, 5], "like": [30, 15], "share": [8, 4], "follow": [3, 1]}
    assert got["streak"] == 0 and activator.quota("like") == 15
    log = fresh_db.list_iteration_log()
    assert log[0]["kind"] == "param" and "额度减半" in log[0]["title"]
    assert "额度已减半" in activator.effect_lines(got)[0]
    assert activator.stats(now)["effect"]["last"]["verdict"] == "none"


def test_review_reports_engage_effects(fresh_db):
    from app.team import coach

    now = dt.datetime.now()
    ts = now.timestamp()
    _pub(fresh_db, age_days=10)
    for i in range(6):
        t0 = ts - (3 + i) * 86400
        _comment(fresh_db, f"r{i}", at=t0, author=f"人{i}", style="ask", sent_at=t0 + H)
        _comment(fresh_db, f"n{i}", at=t0 + 2 * H, author=f"新{i}", kind="praise")
    fresh_db.create_publication({"id": "x", "project_id": "p_x", "account_id": "acc1",
                                 "platform": "douyin", "title": "作品", "state": "published",
                                 "remote_id": "wx", "published_at": ts - 3 * 86400})
    fresh_db.add_snapshot("x", "douyin", ts - 2 * 86400,
                          {"views": 100, "hours_since_publish": 24}, {})
    report = coach.review(now)
    styles = report["engage"]["styles"]
    assert styles[0]["style"] == "ask" and styles[0]["n"] == 6
    assert report["engage"]["active"]["verdict"] == "unknown"
    facts = coach._facts(report)
    assert "回复写法「反问」:发了 6 条" in facts and "活跃官效果:比不出来" in facts
