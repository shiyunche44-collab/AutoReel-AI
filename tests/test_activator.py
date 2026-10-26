"""活跃官:当日计划、额度、候选过滤、评论文案硬规则、状态机、失败即停。"""
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


DAY = "2026-10-25"


def _bench_post(db, aweme_id="aw1", *, title="小米YU7 续航实测", account="sec1", likes=5000,
                pinned=0, posted_at=None):
    db.upsert_benchmark_posts([{
        "id": aweme_id, "account_id": account, "title": title, "car": "小米YU7",
        "duration": 60.0, "posted_at": posted_at or (time.time() - 3600), "views": 100000,
        "likes": likes, "comments": 10, "shares": 5, "collects": 3, "pinned": pinned,
        "burst": 1.0, "fetched_at": time.time(),
    }])


# --------------------------------------------------------------------------
# 当日计划
# --------------------------------------------------------------------------
def test_plan_is_deterministic(fresh_db):
    from app.team import activator

    a = activator.build_plan(DAY)
    b = activator.build_plan(DAY)
    assert [i["at"] for i in a["items"]] == [i["at"] for i in b["items"]]
    assert [i["action"] for i in a["items"]] == [i["action"] for i in b["items"]]
    # 不同的一天计划不同
    c = activator.build_plan("2026-10-26")
    assert [i["at"] for i in a["items"]] != [i["at"] for i in c["items"]]


def test_plan_counts_match_quotas(fresh_db):
    from app.team import activator

    plan = activator.build_plan(DAY, {activator.COMMENT: 10, activator.LIKE: 30,
                                      activator.SHARE: 8, activator.FOLLOW: 3})
    counts = {}
    for item in plan["items"]:
        counts[item["action"]] = counts.get(item["action"], 0) + 1
    assert counts[activator.COMMENT] == 10
    assert counts[activator.LIKE] == 30
    assert counts[activator.SHARE] == 8
    assert counts[activator.FOLLOW] == 3


def test_plan_stays_in_window_and_spaces_comments(fresh_db):
    from app import config
    from app.team import activator

    plan = activator.build_plan(DAY)
    start_h, end_h = config.TEAM_ACTIVE_WINDOW
    base = dt.datetime.fromisoformat(DAY).replace(hour=0, minute=0).timestamp()
    comments = sorted(i["at"] for i in plan["items"] if i["action"] == activator.COMMENT)
    assert comments, "计划里应该有评论"
    for at in plan["items"]:
        minute = (at["at"] - base) / 60
        assert start_h * 60 <= minute <= end_h * 60, f"{minute} 分钟不在窗口里"
    gaps = [(b - a) / 60 for a, b in zip(comments, comments[1:])]
    assert min(gaps) >= config.TEAM_ACTIVE_COMMENT_GAP - 1e-6, f"评论间隔太近:{min(gaps)}"


def test_plan_cached_in_settings(fresh_db):
    from app.team import activator

    plan = activator.plan_for(DAY)
    again = activator.plan_for(DAY)
    assert plan["items"] == again["items"]
    assert activator.db.get_team_setting(activator.PLAN_KEY.format(day=DAY))


# --------------------------------------------------------------------------
# 额度
# --------------------------------------------------------------------------
def test_quota_blocks_more_than_limit(fresh_db):
    from app.team import activator

    fresh_db.set_team_setting("active_quota_likes", 2)
    assert activator.quota(activator.LIKE) == 2
    fresh_db.set_team_setting("active_quota_comments", 5)
    assert activator.quota(activator.COMMENT) == 5


def test_used_today_counts_done_only(fresh_db):
    from app.team import activator

    fresh_db.add_outbound_actions([
        {"id": "a1", "day": DAY, "aweme_id": "x1", "target": "x1", "action": "like",
         "state": "done", "created_at": time.time()},
        {"id": "a2", "day": DAY, "aweme_id": "x2", "target": "x2", "action": "like",
         "state": "failed", "created_at": time.time()},
    ])
    assert activator.used_today(DAY, activator.LIKE) == 1


# --------------------------------------------------------------------------
# 候选过滤
# --------------------------------------------------------------------------
def test_candidates_filter_out_pinned_done_ours_and_tiny(fresh_db):
    from app.team import activator

    fresh_db.add_benchmark_account("sec1", url="u", note="对标")
    _bench_post(fresh_db, "ok1", title="比亚迪汉 空间很大")
    _bench_post(fresh_db, "pinned1", title="比亚迪汉 空间很大", pinned=1)
    _bench_post(fresh_db, "tiny1", title="比亚迪汉 空间很大", likes=10)
    _bench_post(fresh_db, "huge1", title="比亚迪汉 空间很大", likes=999999)
    # 做过的
    fresh_db.add_outbound_actions([{"id": "a1", "day": DAY, "aweme_id": "done1", "target": "done1",
                                    "action": "like", "state": "done", "created_at": time.time()}])
    _bench_post(fresh_db, "done1", title="比亚迪汉 空间很大")
    # 我们自己发过的
    fresh_db.create_publication({"id": "pub1", "project_id": "p1", "account_id": "acc1",
                                 "platform": "douyin", "title": "我们的", "state": "published",
                                 "remote_id": "ours1", "published_at": time.time()})
    _bench_post(fresh_db, "ours1", title="比亚迪汉 空间很大")

    ids = {r["id"] for r in activator.candidates()}
    assert "ok1" in ids
    assert "pinned1" not in ids
    assert "tiny1" not in ids
    assert "huge1" not in ids
    assert "done1" not in ids
    assert "ours1" not in ids


def test_per_account_daily_cap(fresh_db):
    from app.team import activator

    fresh_db.add_benchmark_account("sec1")
    for i in range(3):
        fresh_db.add_outbound_actions([{"id": f"a{i}", "day": DAY, "aweme_id": f"x{i}",
                                        "target": f"x{i}", "sec_uid": "sec1",
                                        "action": "like", "state": "done",
                                        "created_at": time.time()}])
    assert activator._per_account_ok(DAY, "sec1") is False
    assert activator._per_account_ok(DAY, "sec2") is True


# --------------------------------------------------------------------------
# 评论文案
# --------------------------------------------------------------------------
@pytest.mark.parametrize("text", [
    "950公里续航真香",              # 带数字
    "这车不错加微信聊",            # 引流
    "全网最强的续航",              # 绝对化
    "关注我看更多解析",            # 引导关注
    "我试驾过这台车挺舒服",        # 假亲身体验
    "垃圾恰饭",                    # 引战
    "好视频",                      # 跟作品没关系
    "太短",                        # 太短
])
def test_bad_comments_are_rejected(fresh_db, text):
    from app.team import activator

    assert activator.check_text(text, "小米YU7 续航实测")


def test_good_comment_passes(fresh_db):
    from app.team import activator

    assert not activator.check_text("YU7 的续航讲得挺实在", "小米YU7 续航实测")
    assert not activator.check_text("续航这块说清楚了", "小米YU7 续航实测")


def test_pick_text_falls_back_to_templates(fresh_db):
    from app.team import activator

    # 没有 LLM → 用模板;模板也得过规则
    text = activator.pick_text("小米YU7 续航实测", None, ["小米YU7"])
    assert text
    assert not activator.check_text(text, "小米YU7 续航实测")


def test_pick_text_falls_back_to_templates_when_llm_is_bad(fresh_db):
    from app.team import activator

    class BadLLM:
        def structured(self, *a, **k):
            return {"items": [{"text": "加微信有内部价"}, {"text": "950公里续航无敌"}]}

    text = activator.pick_text("小米YU7 续航实测", BadLLM(), ["小米YU7"])
    # LLM 的两条都不合格 → 走模板(模板自己也过硬规则)
    assert text
    assert not activator.check_text(text, "小米YU7 续航实测")
    assert "加微信" not in text and "950" not in text


def test_pick_text_returns_empty_when_nothing_qualifies(fresh_db):
    from app.team import activator

    class BadLLM:
        def structured(self, *a, **k):
            return {"items": [{"text": "加微信有内部价"}, {"text": "950公里续航无敌"}]}

    # 标题里挑不出实义词 → 模板也造不出来 → 这次评论跳过(不硬凑)
    assert activator.pick_text("!!!", BadLLM(), []) == ""


def test_pick_text_prefers_llm_candidate(fresh_db):
    from app.team import activator

    class LLM:
        def structured(self, *a, **k):
            return {"items": [{"text": "YU7 的取舍说得清楚"},
                               {"text": "续航这块讲得挺实在"}]}

    text = activator.pick_text("小米YU7 续航实测", LLM(), ["小米YU7"])
    assert text in ("YU7 的取舍说得清楚", "续航这块讲得挺实在")


# --------------------------------------------------------------------------
# 状态机与留痕
# --------------------------------------------------------------------------
def test_unique_target_once(fresh_db):
    from app.team import activator

    n = fresh_db.add_outbound_actions([
        {"id": "a1", "day": DAY, "aweme_id": "aw1", "target": "aw1", "action": "like",
         "state": "planned", "created_at": time.time()},
    ])
    again = fresh_db.add_outbound_actions([
        {"id": "a2", "day": DAY, "aweme_id": "aw1", "target": "aw1", "action": "like",
         "state": "planned", "created_at": time.time()},
    ])
    assert n == 1 and again == 0
    # 不同的动作不受影响
    assert fresh_db.add_outbound_actions([
        {"id": "a3", "day": DAY, "aweme_id": "aw1", "target": "aw1", "action": "comment",
         "state": "planned", "created_at": time.time()}]) == 1


def test_cas_state_single_winner(fresh_db):
    from app.team import activator

    fresh_db.add_outbound_actions([{"id": "a1", "day": DAY, "aweme_id": "aw1", "target": "aw1",
                                    "action": "like", "state": "planned",
                                    "created_at": time.time()}])
    assert activator.db.cas_outbound_state("a1", ("planned",), "doing") is True
    assert activator.db.cas_outbound_state("a1", ("planned",), "doing") is False
    assert (activator.db.get_outbound_action("a1") or {})["state"] == "doing"


def test_stuck_doing_is_reset(fresh_db):
    from app.team import activator

    old = time.time() - 3600
    fresh_db.add_outbound_actions([{"id": "a1", "day": DAY, "aweme_id": "aw1", "target": "aw1",
                                    "action": "like", "state": "doing", "started_at": old,
                                    "created_at": old}])
    assert fresh_db.reset_stuck_outbound(600) == 1
    assert (fresh_db.get_outbound_action("a1") or {})["state"] == "planned"


def test_failure_stops_the_day(fresh_db, monkeypatch):
    from app.publish.platforms import douyin_outreach
    from app.team import activator

    fresh_db.add_benchmark_account("sec1")
    _bench_post(fresh_db, "aw1", title="比亚迪汉 空间很大")
    fresh_db.set_team_setting("active_quota_likes", 1)

    def boom(jobs, on_done):
        on_done(jobs[0], "点赞按钮不可见(页面改版了?)")

    monkeypatch.setattr(douyin_outreach, "run_actions", boom)
    out = activator.execute_batch([{"at": time.time(), "action": "like", "batch": 1}],
                                   DAY, time.time(), llm=None)
    assert out["failed"] == 1
    assert activator.paused_day() == dt.date.today().isoformat()


def test_disabled_role_does_nothing(fresh_db, monkeypatch):
    from app.team import activator

    activator.set_enabled(False)
    with pytest.raises(activator.Skip):
        activator.run()


def test_batch_uses_different_targets(fresh_db, monkeypatch):
    """同一批里的三个赞不能全点同一个视频(否则两个会被唯一索引挡掉)。"""
    from app.publish.platforms import douyin_outreach
    from app.team import activator

    fresh_db.add_benchmark_account("sec1")
    for i in range(4):
        _bench_post(fresh_db, f"aw{i}", title=f"比亚迪汉 空间很大 {i}")

    picked = []

    def fake(jobs, on_done):
        for job in jobs:
            picked.append(job["aweme_id"])
            on_done(job, None)

    monkeypatch.setattr(douyin_outreach, "run_actions", fake)
    activator.execute_batch([{"at": time.time(), "action": "like", "batch": 1},
                             {"at": time.time(), "action": "like", "batch": 1},
                             {"at": time.time(), "action": "like", "batch": 1}],
                            DAY, time.time(), llm=None)
    assert len(picked) == 3
    assert len(set(picked)) == 3, f"挑重了:{picked}"


def test_same_video_can_be_liked_and_commented(fresh_db):
    """点赞过不妨碍再评论一句 —— 排除只按动作算。"""
    from app.team import activator

    fresh_db.add_benchmark_account("sec1")
    _bench_post(fresh_db, "aw1", title="比亚迪汉 空间很大")
    fresh_db.add_outbound_actions([{"id": "a1", "day": DAY, "aweme_id": "aw1", "target": "aw1",
                                    "action": "like", "state": "done",
                                    "created_at": time.time()}])
    ids = {r["id"] for r in activator.candidates(exclude=activator.acted_targets("comment"))}
    assert "aw1" in ids
    assert "aw1" not in {r["id"] for r in
                         activator.candidates(exclude=activator.acted_targets("like"))}


def test_verification_overlay_is_detected(fresh_db):
    """抖音的二次验证遮罩(页面上没有「验证码」三个字)也要认出来。"""
    from app.publish.platforms import douyin_outreach

    class FakePage:
        def __init__(self, count):
            self._count = count

        def locator(self, selector):
            return self

        def count(self):
            return self._count

        def inner_text(self, _body):
            return "作品评论区"

    class FakeSession:
        def __init__(self, count):
            self.page = FakePage(count)

    with pytest.raises(douyin_outreach.VerificationNeeded):
        douyin_outreach._guard(FakeSession(1))
    # 没有遮罩、正文里也没验证码 → 正常放行
    douyin_outreach._guard(FakeSession(0))


def test_old_db_gets_the_new_table(fresh_db):
    """老库(没有 outbound_actions 这张表)启动后要有 —— 新表走 CREATE TABLE IF NOT EXISTS。"""
    import sqlite3

    from app import config

    path = config.DATA_DIR / "aimovice.db"
    fresh_db.init_db()
    with sqlite3.connect(path) as conn:
        conn.execute("DROP TABLE IF EXISTS outbound_actions")
    fresh_db._INITIALISED = False
    fresh_db.init_db()
    with sqlite3.connect(path) as conn:
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "outbound_actions" in tables


def test_done_batch_does_not_block_the_next_one(fresh_db, monkeypatch):
    """一批做完以后,下一轮要挑**下一批** —— 不然当天的计划只跑得完第一批。"""
    from app.publish.platforms import douyin_outreach
    from app.team import activator

    fresh_db.add_benchmark_account("sec1")
    for i in range(6):
        _bench_post(fresh_db, f"aw{i}", title=f"比亚迪汉 空间很大 {i}")

    def fake(jobs, on_done):
        for job in jobs:
            on_done(job, None)

    monkeypatch.setattr(douyin_outreach, "run_actions", fake)
    plan = activator.build_plan(DAY)
    first_batch = plan["items"][0]["batch"]
    second_batch = next(i["batch"] for i in plan["items"] if i["batch"] != first_batch)

    now = time.time()
    batch_items = [i for i in plan["items"] if i["batch"] == first_batch]
    activator.execute_batch(batch_items, DAY, now, llm=None)

    # 第二轮:到点的槽位里,第一个批次已经做完了,应该挑到别的批次
    due = [i for i in plan["items"] if i["at"] <= now]
    done_slots = {str(r.get("slot") or "") for r in
                   fresh_db.list_outbound_actions(day=DAY, limit=100) if r.get("slot")}
    pending = [i for i in due if str(i.get("slot") or "") not in done_slots]
    assert pending, "不该只剩已做过的槽位"
    assert int(pending[0]["batch"]) == second_batch


def test_skipped_slot_is_not_retried_forever(fresh_db, monkeypatch):
    """跳过的计划项要记下来,别一轮一轮地重挑。"""
    from app.team import activator

    fresh_db.add_benchmark_account("sec1")
    activator.mark_skipped(DAY, "3:like")
    assert "3:like" in activator.skipped_slots(DAY)


def test_verification_stops_the_day(fresh_db, monkeypatch):
    """撞上二次验证 → 当天停手,原因写清楚(不是当普通失败吞掉)。"""
    from app.publish.platforms import douyin_outreach
    from app.team import activator

    fresh_db.add_benchmark_account("sec1")
    _bench_post(fresh_db, "aw1", title="比亚迪汉 空间很大")

    def boom(jobs, on_done):
        raise douyin_outreach.VerificationNeeded("抖音弹了二次验证")

    monkeypatch.setattr(douyin_outreach, "run_actions", boom)
    with pytest.raises(douyin_outreach.VerificationNeeded):
        activator.execute_batch([{"at": time.time(), "action": "like", "batch": 1, "slot": "0:like"}],
                                DAY, time.time(), llm=None)
    # 留在 doing 的要落成 failed,不能吊着
    rows = fresh_db.list_outbound_actions(day=DAY, limit=10)
    assert rows and all(r["state"] == "failed" for r in rows)


def test_run_stops_day_on_verification(fresh_db, monkeypatch):
    from app.pipeline import orchestrator
    from app.publish.platforms import douyin_outreach
    from app.team import activator

    monkeypatch.setattr(orchestrator, "_llm", lambda: None)
    fresh_db.add_benchmark_account("sec1")
    _bench_post(fresh_db, "aw1", title="比亚迪汉 空间很大")

    def boom(jobs, on_done):
        raise douyin_outreach.VerificationNeeded("抖音弹了二次验证")

    monkeypatch.setattr(douyin_outreach, "run_actions", boom)
    with pytest.raises(activator.TeamError):
        activator.run()
    assert activator.paused_day() == dt.date.today().isoformat()


def test_stats_shape(fresh_db):
    from app.team import activator

    stats = activator.stats()
    assert stats["enabled"] is True
    assert set(stats["quotas"]) == {"comment", "like", "share", "follow"}
    assert stats["counts"]["comment"] == 0
