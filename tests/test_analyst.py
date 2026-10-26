"""分析师:抖音创作者中心作品列表解析、对作品、1h/24h/72h/7d 落快照、登录失效不重试。"""
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


def _work(wid="7700158815051861248", title="买大唐,是买9分钟还是买950km?", at=0.0, views=5,
          **status):
    """`work_list` 返回里的一条(按 2026-10 真实探针的结构,数字换过)。"""
    return {
        "id": wid, "create_time": int(at), "description": f"{title} ",
        "metrics": {"view_count": str(views), "like_count": "1", "comment_count": "0",
                    "favorite_count": "2", "share_count": "0", "danmaku_count": "0",
                    "completion_rate": "0.055556", "avg_view_second": "11.500000",
                    "bounce_rate_2s": "0.125000", "subscribe_count": "3",
                    "completion_rate_5s": "0.5"},
        "review": {"status": 2},
        "aweme": {"aweme_id": wid, "item_title": title, "author": {"follower_count": 690},
                  "status": {"in_reviewing": False, "is_delete": False, "is_prohibited": False,
                             "self_see": False, **status}},
    }


# --------------------------------------------------------------------------
# 纯函数
# --------------------------------------------------------------------------
def test_parse_douyin_creator():
    from app.metrics.collector import douyin_work_state, parse_douyin_creator

    m = parse_douyin_creator(_work(views=40))
    assert m["views"] == 40 and m["likes"] == 1 and m["favorites"] == 2
    assert m["completion_rate"] == pytest.approx(0.055556)
    assert m["avg_watch_sec"] == 11.5 and m["bounce_2s"] == 0.125
    assert m["followers_gained"] == 3 and m["followers"] == 690
    assert m["watch_minutes"] == pytest.approx(11.5 * 40 / 60, abs=0.01)
    assert m["coins"] is None
    assert parse_douyin_creator({"id": "1"}) == {}
    assert douyin_work_state(_work()) == "published"
    assert douyin_work_state(_work(in_reviewing=True)) == "reviewing"
    assert douyin_work_state(_work(is_prohibited=True)) == "removed"


def test_match_work_by_title_and_time():
    from app.team import analyst

    t0 = 1_792_830_000.0
    pub = {"title": "买大唐，是买9分钟还是买950km？", "updated_at": t0, "remote_id": ""}
    near = _work("a", at=t0 + 600)
    far = _work("b", at=t0 - 2 * 86400)
    other = _work("c", title="23.35万起,买续航还是买加速?", at=t0)
    assert analyst.match_work(pub, [far, other, near])["id"] == "a"
    assert analyst.match_work(pub, [far, other]) is None
    assert analyst.match_work(pub, [near], taken={"a"}) is None
    # 平台标题框 30 字截过也认
    long_pub = {**pub, "title": "买大唐，是买9分钟还是买950km？看完这条再决定"}
    assert analyst.match_work(long_pub, [near])["id"] == "a"
    assert analyst.match_work({**pub, "remote_id": "c"}, [near, other])["id"] == "c"


def test_due_milestone():
    from app.team import analyst

    now = 1_800_000_000.0
    pub = {"published_at": now - 30 * 3600}
    assert analyst.due_milestone(pub, [], now) == 24
    assert analyst.due_milestone(pub, [{"hours_since_publish": 25}], now) is None
    assert analyst.due_milestone(pub, [{"hours_since_publish": 2}], now) == 24
    assert analyst.due_milestone({"published_at": now - 600}, [], now) is None  # 不到 1 小时
    assert analyst.due_milestone({"published_at": now - 200 * 3600}, [], now) is None  # 过了 7 天 + 宽限
    assert analyst.due_milestone({"published_at": None}, [], now) is None


# --------------------------------------------------------------------------
# 一轮抓取
# --------------------------------------------------------------------------
def _pub(db, pid, *, state="submitted", title="买大唐,是买9分钟还是买950km?", **kw):
    db.create_publication({"id": pid, "project_id": f"p_{pid}", "account_id": "acc1",
                           "platform": "douyin", "title": title, "state": state, **kw})
    return db.get_publication(pid)


def test_collect_claims_and_records(fresh_db, monkeypatch):
    from app.team import analyst

    now = time.time()
    _pub(fresh_db, "web")  # 网页版刚提交,没有作品 id
    _pub(fresh_db, "lost", state="failed", title="23.35万起,买续航还是买加速?",
         error="网页版发布后没找到成功提示,可能是页面改版或需要验证码")
    _pub(fresh_db, "old", state="published", title="老作品", remote_id="w_old",
         published_at=now - 25 * 3600)
    works = [_work("w_web", at=now - 300, views=7),
             _work("w_lost", title="23.35万起,买续航还是买加速?", at=now - 900, views=9),
             _work("w_old", title="老作品", at=now - 25 * 3600, views=300)]
    seen = {}
    got = analyst.collect(now, fetch=lambda since: seen.setdefault("since", since) and works)
    assert got == {"claimed": 2, "recorded": 3, "works": 3, "curves": 0}
    web = fresh_db.get_publication("web")
    assert web["state"] == "published" and web["remote_id"] == "w_web"
    assert web["remote_url"].endswith("/video/w_web")
    lost = fresh_db.get_publication("lost")
    assert lost["state"] == "published" and lost["error"] == ""
    snap = fresh_db.latest_snapshot("old")
    assert snap["views"] == 300 and snap["completion_rate"] == pytest.approx(0.055556)
    assert snap["bounce_2s"] == 0.125 and snap["followers_gained"] == 3
    assert snap["hours_since_publish"] == pytest.approx(25, abs=0.01)
    assert seen["since"] <= now - 25 * 3600
    # 同一时刻不重复抓:下一轮没有到点的
    from app.team import Skip

    with pytest.raises(Skip):
        analyst.collect(now + 60, fetch=lambda since: works)


def test_collect_keeps_reviewing_as_submitted(fresh_db):
    from app.team import analyst

    now = time.time()
    _pub(fresh_db, "web")
    analyst.collect(now, fetch=lambda since: [_work("w1", at=now, in_reviewing=True)])
    row = fresh_db.get_publication("web")
    assert row["state"] == "submitted" and row["remote_id"] == "w1"


def test_login_failure_blocks_until_rescan(fresh_db, monkeypatch):
    from app.publish import browser
    from app.publish.base import PublishError
    from app.publish.platforms import douyin
    from app.team import Skip, TeamError, analyst

    now = time.time()
    _pub(fresh_db, "web")

    def expired(since):
        raise PublishError("抖音登录已失效", retryable=False, credential=True)

    with pytest.raises(TeamError):
        analyst.collect(now, fetch=expired)
    calls = []
    monkeypatch.setattr(douyin, "fetch_creator_works", lambda since: calls.append(1) or [])
    with pytest.raises(Skip, match="扫码"):
        analyst.collect(now + 60)
    assert calls == []  # 没再开浏览器
    path = browser.state_path("douyin")
    path.write_text("{}", encoding="utf-8")  # 重新扫码后登录态文件会更新
    import os

    os.utime(path, (now + 120, now + 120))
    with pytest.raises(Skip):  # 能开浏览器了,只是没对上作品
        analyst.run()
    assert calls == [1]


def test_metrics_worker_leaves_douyin_to_analyst(fresh_db, monkeypatch):
    from app import config
    from app.metrics import collector, store

    _pub(fresh_db, "d", state="published", remote_id="w1", published_at=time.time())
    called = []
    monkeypatch.setattr(collector, "collect_safe", lambda pub, acc: called.append(pub["id"]) or (None, "x"))
    monkeypatch.setattr(config, "TEAM_ENABLED", True)
    assert store.record_all() == {"ok": 0, "failed": 0}
    assert called == []
