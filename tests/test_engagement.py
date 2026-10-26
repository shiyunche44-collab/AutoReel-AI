"""运营官:评论分类、拟回复(LLM 不合格退模板)、只发批过的、失败就停、问题回灌主编、断更提醒。"""
from __future__ import annotations

import time

import pytest


@pytest.fixture
def fresh_db(tmp_path, monkeypatch):
    from app import config, db

    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(config, "PROJECTS_DIR", tmp_path / "data" / "projects")
    monkeypatch.setattr(db, "_INITIALISED", False)
    return db


def _pub(db, pid="pub1", *, remote="w1", age_days=1.0, title="小米YU7 值不值"):
    db.create_publication({"id": pid, "project_id": f"p_{pid}", "account_id": "acc1",
                           "platform": "douyin", "title": title, "state": "published",
                           "remote_id": remote, "published_at": time.time() - age_days * 86400})
    return db.get_publication(pid)


def _raw(cid, text, *, author="路人甲", level=1, is_author=False, likes=0):
    return {"comment_id": cid, "text": text, "create_time": str(int(time.time()) - 60),
            "digg_count": str(likes), "level": level, "is_author": is_author,
            "user_info": {"screen_name": author}}


def _fetch(comments, counts=None):
    calls = []

    def fetch(known):
        calls.append(dict(known))
        return {"counts": counts or {w: len(c) for w, c in comments.items()}, "comments": comments}

    fetch.calls = calls
    return fetch


# --------------------------------------------------------------------------
# 分类
# --------------------------------------------------------------------------
@pytest.mark.parametrize("text,kind", [
    ("这车落地多少钱?", "question"),
    ("能不能出一期和理想i8的对比", "question"),
    ("加微信领购车资料", "ad"),
    ("私信我有内部价吗?", "ad"),
    ("讲得很清楚,干货", "praise"),
    ("[赞][赞]", "praise"),
    ("收钱了吧,吹得离谱", "argue"),
    ("哈哈哈", "other"),
])
def test_classify(text, kind):
    from app.team import engagement

    assert engagement.classify(text) == kind


def test_parse_skips_own_and_nested(fresh_db):
    from app.team import engagement

    pub = _pub(fresh_db)
    assert engagement.parse_comment(_raw("c1", "好", is_author=True), pub) is None
    assert engagement.parse_comment(_raw("c2", "好", level=2), pub) is None
    row = engagement.parse_comment(_raw("c3", "加V看车", likes=3), pub)
    assert row["kind"] == "ad" and row["reply_state"] == "skipped" and row["likes"] == 3


# --------------------------------------------------------------------------
# 抓评论 + 拟回复
# --------------------------------------------------------------------------
def test_collect_only_watched_works_and_keeps_review_state(fresh_db):
    from app.team import engagement

    _pub(fresh_db)
    _pub(fresh_db, "old", remote="w_old", age_days=40)
    fetch = _fetch({"w1": [_raw("c1", "这车多少钱?"), _raw("c2", "讲得好")]})
    got = engagement.collect(fetch=fetch)
    assert fetch.calls == [{"w1": 0}]  # 40 天前那条不看
    assert got["added"] == 2
    c1 = fresh_db.get_comment("c1")
    assert c1["kind"] == "question" and c1["reply_state"] == "draft"
    assert c1["reply_style"] in engagement.REPLY_STYLES
    assert c1["reply_draft"] == engagement.STYLE_TEMPLATES[c1["reply_style"]]["question"]
    # 再拉一次:上次的评论数已经记下;状态不被覆盖
    fresh_db.cas_comment_state("c1", ("draft",), "approved")
    fetch2 = _fetch({"w1": [_raw("c1", "这车多少钱?", likes=9)]}, counts={"w1": 3})
    assert engagement.collect(fetch=fetch2)["added"] == 0
    assert fetch2.calls == [{"w1": 2}]
    c1 = fresh_db.get_comment("c1")
    assert c1["reply_state"] == "approved" and c1["likes"] == 9


def test_llm_reply_checked_falls_back_to_template(fresh_db):
    from app.team import engagement

    _pub(fresh_db)

    class LLM:
        def structured(self, system, user, **kw):
            style = engagement.REPLY_STYLES[engagement.pick_style("c1")]
            assert f"c1 | {style} | 续航多少" in user and "反问=" in system
            return {"items": [
                {"id": "c1", "kind": "question", "reply": "官方续航 835 公里"},  # 事实层没有这个数
                {"id": "c2", "kind": "praise", "reply": "谢谢,下期见"},
                {"id": "c3", "kind": "ad", "reply": "不回"},
                {"id": "zz", "kind": "praise", "reply": "不是这批的"},
            ]}

    fetch = _fetch({"w1": [_raw("c1", "续航多少"), _raw("c2", "牛"), _raw("c3", "哈哈")]})
    engagement.collect(fetch=fetch, llm=LLM())
    c1, c2, c3 = (fresh_db.get_comment(c) for c in ("c1", "c2", "c3"))
    assert c1["reply_source"] == "template" and c1["kind_source"] == "llm"
    assert (c2["reply_draft"], c2["reply_source"]) == ("谢谢,下期见", "llm")
    assert c3["kind"] == "ad" and c3["reply_state"] == "skipped" and c3["reply_draft"] == ""
    assert fresh_db.get_comment("zz") is None


def test_auto_thanks_off_by_default(fresh_db):
    from app.team import engagement

    _pub(fresh_db)
    engagement.collect(fetch=_fetch({"w1": [_raw("c1", "讲得好")]}))
    assert fresh_db.get_comment("c1")["reply_state"] == "draft"
    fresh_db.set_team_setting(engagement.AUTO_THANKS_KEY, True)
    engagement.collect(fetch=_fetch({"w1": [_raw("c2", "讲得好"), _raw("c3", "多少钱?")]},
                                    counts={"w1": 5}))
    assert fresh_db.get_comment("c2")["reply_state"] == "approved"
    assert fresh_db.get_comment("c3")["reply_state"] == "draft"  # 问题类照样等你批


def test_collect_login_failure_blocks_until_rescan(fresh_db, monkeypatch):
    from app.publish.base import PublishError
    from app.team import TeamError, engagement

    _pub(fresh_db)

    def bad(known):
        raise PublishError("抖音登录已失效", retryable=False, credential=True)

    with pytest.raises(TeamError):
        engagement.collect(fetch=bad)
    assert fresh_db.get_team_setting("engage_login_bad")
    assert engagement._login_blocked()


# --------------------------------------------------------------------------
# 审核 + 发送
# --------------------------------------------------------------------------
def _seed(db, n=3):
    from app.team import engagement

    _pub(db)
    engagement.collect(fetch=_fetch({"w1": [_raw(f"c{i}", f"第{i}条 多少钱?", author=f"u{i}")
                                            for i in range(n)]}))


def test_only_approved_are_sent_and_first_failure_stops(fresh_db):
    from app.team import engagement

    _seed(fresh_db, 4)
    assert engagement.send_approved(send=lambda jobs, done: pytest.fail("没批的不该发")) \
        == {"sent": 0, "failed": 0}
    assert engagement.decide(["c0", "c1", "c2"], "approve") == 3
    seen = []

    def send(jobs, on_done):
        for job in jobs:
            seen.append(job["id"])
            if job["id"] == "c1":
                on_done(job, "抖音没收这条回复")
                return  # 平台函数遇到失败就停
            on_done(job, None)

    out = engagement.send_approved(send=send)
    assert out == {"sent": 1, "failed": 1}
    states = {c: fresh_db.get_comment(c)["reply_state"] for c in ("c0", "c1", "c2", "c3")}
    assert states == {"c0": "sent", "c1": "failed", "c2": "approved", "c3": "draft"}
    assert fresh_db.get_comment("c1")["reply_error"] == "抖音没收这条回复"
    assert fresh_db.get_comment("c0")["replied_at"]
    # 已发的不能再批 / 再改
    assert engagement.decide(["c0"], "approve") == 0
    with pytest.raises(engagement.EngageError):
        engagement.edit("c0", reply="再来一条")


def test_edit_checks_reply(fresh_db):
    from app.team import engagement

    _seed(fresh_db, 1)
    with pytest.raises(engagement.EngageError, match="禁用词"):
        engagement.edit("c0", reply="这是同级第一的车")
    with pytest.raises(engagement.EngageError, match="超过"):
        engagement.edit("c0", reply="很长" * 40)
    c = engagement.edit("c0", reply="落地价看地区,建议去门店问问", kind="other")
    assert (c["reply_source"], c["kind"], c["kind_source"]) == ("user", "other", "user")
    assert c["reply_state"] == "draft"  # 改完仍要批


def test_ad_without_reply_cannot_be_approved(fresh_db):
    from app.team import engagement

    _pub(fresh_db)
    engagement.collect(fetch=_fetch({"w1": [_raw("ad", "加微信")]}))
    assert engagement.decide(["ad"], "reset") == 1
    assert engagement.decide(["ad"], "approve") == 0


# --------------------------------------------------------------------------
# 回灌 + 断更 + 指标
# --------------------------------------------------------------------------
def test_questions_feed_editor_prompt(fresh_db):
    from app.team import engagement

    _seed(fresh_db, 2)
    text = engagement.audience_text(mark=True)
    assert "观众在评论里问" in text and "第0条 多少钱" in text and "《小米YU7 值不值》" in text
    assert fresh_db.get_comment("c0")["topic_state"] == "pooled"
    engagement.set_topic("c0", False)
    assert "第0条" not in engagement.audience_text()


def test_editor_passes_audience_questions(fresh_db, monkeypatch):
    import datetime as dt

    from app import config
    from app.pipeline import topics
    from app.team import editor, scout

    _seed(fresh_db, 1)
    seen = {}
    monkeypatch.setattr(scout, "fresh_items", lambda category: [{"title": "x"}])
    monkeypatch.setattr(topics, "suggest", lambda items, **kw: seen.update(kw) or [])
    with pytest.raises(Exception):
        editor.run(config.TEAM_CATEGORY, llm=object(), now=dt.datetime.now())
    assert "第0条 多少钱" in seen["playbook"]


def test_idle_reminder_once_a_day(fresh_db):
    from app.team import engagement

    now = time.time()
    assert engagement.check_idle(now) == ""  # 从没发过
    _pub(fresh_db, age_days=4)
    text = engagement.check_idle(now)
    assert "4 天没发" in text
    assert engagement.check_idle(now + 60) == ""  # 今天提醒过了


def test_stats_reply_rate_and_first_hour(fresh_db):
    from app.team import engagement

    _seed(fresh_db, 2)
    fresh_db.cas_comment_state("c0", ("draft",), "sent")
    pub = fresh_db.get_publication("pub1")
    fresh_db.add_snapshot("pub1", "douyin", pub["published_at"] + 3600,
                          {"views": 200, "likes": 8, "comments": 2, "hours_since_publish": 1.0})
    s = engagement.stats()
    assert s["reply_rate"] == 0.5 and s["kinds"]["question"] == 2 and s["pending"] == 1
    assert s["first_hour"] == 0.05 and s["first_hour_n"] == 1


def test_run_sends_then_collects(fresh_db, monkeypatch):
    from app.pipeline import orchestrator
    from app.publish.platforms import douyin
    from app.team import engagement

    _seed(fresh_db, 1)
    engagement.decide(["c0"], "approve")
    monkeypatch.setattr(orchestrator, "_llm", lambda: None)
    monkeypatch.setattr(douyin, "send_comment_replies",
                        lambda jobs, on_done: [on_done(j, None) for j in jobs])
    monkeypatch.setattr(douyin, "fetch_creator_comments",
                        _fetch({"w1": [_raw("c9", "还有别的颜色吗?")]}, counts={"w1": 9}))
    out = engagement.run()
    assert "发出 1 条回复" in out and "新增 1 条评论" in out
    assert fresh_db.get_comment("c0")["reply_state"] == "sent"
