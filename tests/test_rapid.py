"""对标分析员(爆款倍数、失败就停)、行业日历(抽取校验、提前 3–7 天)、快反通道(触发、冷却、素材不够)。"""
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


def _aweme(aid, likes, *, desc="", top=False, age_days=1.0):
    return {"aweme_id": aid, "desc": desc, "create_time": int(time.time() - age_days * 86400),
            "duration": 95000, "is_top": 1 if top else 0, "author": {"nickname": "对标号A"},
            "statistics": {"digg_count": likes, "play_count": 0, "comment_count": 3,
                           "share_count": 1, "collect_count": 2}}


# --------------------------------------------------------------------------
# 对标分析员
# --------------------------------------------------------------------------
def test_car_in_title():
    from app.team import benchmark

    cars = ["小米YU7", "Model Y", "小米SU7"]
    assert benchmark.car_in("小米 YU7 落地价算给你听", cars) == "小米YU7"
    assert benchmark.car_in("YU7 和 i8 怎么选", cars) == "小米YU7"
    assert benchmark.car_in("新款model y 来了", cars) == "Model Y"
    assert benchmark.car_in("今天聊聊油价", cars) == ""


def test_burst_ignores_pinned_and_needs_enough_posts():
    from app.team import benchmark

    rows = [benchmark.parse_post(_aweme(f"a{i}", 100), "acc", []) for i in range(5)]
    rows.append(benchmark.parse_post(_aweme("hit", 500), "acc", []))
    rows.append(benchmark.parse_post(_aweme("top", 99999, top=True), "acc", []))
    out, median = benchmark.with_burst(rows)
    assert median == 100
    burst = {r["id"]: r["burst"] for r in out}
    assert burst["hit"] == 5.0 and burst["a0"] == 1.0
    assert rows[0]["duration"] == 95.0
    few, m = benchmark.with_burst(rows[:3])
    assert m == 0 and all(r["burst"] == 0 for r in few)


def test_add_account_parses_link(fresh_db):
    from app.team import benchmark

    a = benchmark.add_account("看这个 https://www.douyin.com/user/MS4wLjABAAAAxyz_1?from=tab", "大号")
    assert a["id"] == "MS4wLjABAAAAxyz_1" and a["note"] == "大号"
    with pytest.raises(benchmark.BenchError, match="已经在"):
        benchmark.add_account("https://www.douyin.com/user/MS4wLjABAAAAxyz_1")
    with pytest.raises(benchmark.BenchError):
        benchmark.add_account("随便写写")


def test_short_link_resolved_by_fetch(fresh_db):
    from app.team import benchmark

    fetch = lambda target: {"sec_uid": "MS4wLjABAAAAshort", "nickname": "短链号",  # noqa: E731
                            "awemes": [_aweme("x1", 10)]}
    a = benchmark.add_account("https://v.douyin.com/abc123/", fetch=fetch)
    assert a["id"] == "MS4wLjABAAAAshort" and a["nickname"] == "短链号"
    assert len(fresh_db.list_benchmark_posts(account_id=a["id"])) == 1


def test_collect_stops_on_first_failure(fresh_db):
    from app.team import benchmark

    for sec in ("MS4wLjABAAAA_a", "MS4wLjABAAAA_b", "MS4wLjABAAAA_c"):
        fresh_db.add_benchmark_account(sec)
        time.sleep(0.01)
    seen, slept = [], []

    def fetch(sec):
        from app.publish.base import PublishError

        seen.append(sec)
        if sec.endswith("_b"):
            raise PublishError("主页没请求作品列表接口", retryable=False)
        return {"sec_uid": sec, "nickname": "号" + sec[-1],
                "awemes": [_aweme(f"{sec}{i}", 100) for i in range(5)]
                + [_aweme(f"{sec}hit", 400, desc="小米YU7 一周体验")]}

    out = benchmark.collect(fetch=fetch, sleep=slept.append)
    assert seen == ["MS4wLjABAAAA_a", "MS4wLjABAAAA_b"]  # b 失败就停,不碰 c
    assert out["accounts"] == 1 and out["hits"] == 1 and "主页没请求" in out["error"]
    assert slept == [benchmark.GAP_SECONDS]
    b = fresh_db.get_benchmark_account("MS4wLjABAAAA_b")
    assert b["last_error"] and b["last_fetched_at"]
    # 下一轮:a、b 都刚看过,只剩 c
    assert [x["id"] for x in benchmark.due_accounts()] == ["MS4wLjABAAAA_c"]
    hit = benchmark.hits()[0]
    assert hit["burst"] == 4.0 and hit["account"] == "号a"


def test_login_failure_blocks_until_rescan(fresh_db):
    from app.publish.base import PublishError
    from app.team import Skip, benchmark

    fresh_db.add_benchmark_account("MS4wLjABAAAA_a")

    def bad(sec):
        raise PublishError("抖音登录已失效", retryable=False, credential=True)

    out = benchmark.collect(fetch=bad, sleep=lambda s: None)
    assert "登录已失效" in out["error"] and out["accounts"] == 0
    assert benchmark._login_blocked()
    with pytest.raises(Skip, match="重新扫码"):
        benchmark.run()


def test_benchmark_prompt_and_evidence(fresh_db):
    from app.team import benchmark

    fresh_db.add_benchmark_account("MS4wLjABAAAA_a")
    fresh_db.update_benchmark_account("MS4wLjABAAAA_a", nickname="车评大号")
    fresh_db.upsert_benchmark_posts([
        {"id": "p1", "account_id": "MS4wLjABAAAA_a", "title": "YU7 三个月真实油耗", "car": "小米YU7",
         "duration": 80, "posted_at": time.time() - 86400, "likes": 900, "burst": 6.0},
        {"id": "p2", "account_id": "MS4wLjABAAAA_a", "title": "普通一条", "car": "",
         "duration": 80, "posted_at": time.time() - 86400, "likes": 100, "burst": 1.0},
    ])
    text = benchmark.prompt_text()
    assert "YU7 三个月真实油耗" in text and "6 倍" in text and "普通一条" not in text
    assert benchmark.hot_cars() == {"小米yu7"}
    assert "车评大号" in benchmark.evidence_for("小米 YU7")[0]
    cmp = benchmark.title_compare([{"title": "#汽车 我们的标题很好", "lift": 1.4}])
    assert cmp["theirs"][0]["opening"] == "YU7 三个月真实油耗"
    assert cmp["ours"][0]["opening"] == "我们的标题很好"


# --------------------------------------------------------------------------
# 行业日历
# --------------------------------------------------------------------------
def test_extract_checks_dates_and_cars():
    from app.team import events

    today = dt.date(2026, 10, 24)
    items = [{"title": "小米YU7 将于10月28日正式上市", "metric": "", "url": "u0"},
             {"title": "广州车展 11 月 15 日开幕", "metric": "", "url": "u1"},
             {"title": "某车企 OTA 已推送", "metric": "", "url": "u2"}]

    class LLM:
        def structured(self, system, user, **kw):
            assert "今天是 2026-10-24" in user and "0 | 小米YU7" in user
            return {"events": [
                {"i": 0, "day": "2026-10-28", "kind": "launch", "car": "小米YU7", "title": "小米YU7 上市"},
                {"i": 1, "day": "2026-11-15", "kind": "show", "car": "理想i8", "title": "广州车展开幕"},
                {"i": 2, "day": "2026-10-01", "kind": "ota", "car": "", "title": "已经过去"},
                {"i": 9, "day": "2026-10-30", "kind": "launch", "car": "", "title": "序号不对"},
                {"i": 0, "day": "明天", "kind": "launch", "car": "", "title": "日期不对"},
            ]}

    got = events.extract(items, LLM(), today)
    assert [(e["day"], e["car"], e["url"]) for e in got] == [
        ("2026-10-28", "小米YU7", "u0"), ("2026-11-15", "", "u1")]  # 原文没有的车型不认


def test_harvest_once_per_batch_and_lead_window(fresh_db):
    from app import config
    from app.team import Skip, events

    cat = config.TEAM_CATEGORY
    fresh_db.add_trend_batch(cat, "tb1", time.time(), [
        {"source": "news", "kind": "event", "title": "小米YU7 10月29日上市", "car": "", "rank": None,
         "metric": "", "url": "", "date": ""}])
    calls = []

    class LLM:
        def structured(self, system, user, **kw):
            calls.append(user)
            return {"events": [{"i": 0, "day": "2026-10-29", "kind": "launch", "car": "小米YU7",
                                "title": "小米YU7 上市"}]}

    now = dt.datetime(2026, 10, 24, 9)
    assert events.harvest(cat, llm=LLM(), now=now) == 1
    with pytest.raises(Skip):
        events.harvest(cat, llm=LLM(), now=now)
    assert len(calls) == 1
    events.add_manual(cat, "2026-10-26", "某车发布会", "event", today=now.date())
    with pytest.raises(events.EventError):
        events.add_manual(cat, "2026-10-20", "过去的", today=now.date())
    # 5 天后的进主编提示词,2 天后的不进(太近,交给快反)
    text = events.prompt_text(cat, now.date())
    assert "小米YU7 上市" in text and "5 天后" in text and "某车发布会" not in text
    assert "小米yu7" in events.lead_cars(cat, now.date())


# --------------------------------------------------------------------------
# 快反
# --------------------------------------------------------------------------
def _idea(subject="小米YU7", theme=None):
    from app.categories import load_profile
    from app import config
    from app.pipeline.topics import TopicIdea

    profile = load_profile(config.TEAM_CATEGORY)
    theme = theme or next(n for n, t in profile.themes.items() if not t.multi)
    return TopicIdea(subject=subject, angle="刚上市:到底值不值", theme=theme, hook="YU7 今天上市",
                     heat=4, evidence=["懂车帝热度榜第 2"])


def _rapid_env(monkeypatch, rising, ideas=None, secs=200.0):
    from app.pipeline import topics
    from app.team import rapid, scout

    monkeypatch.setattr(scout, "rising", lambda category, limit=8: rising)
    monkeypatch.setattr(scout, "fresh_items", lambda category: [
        {"source": "dongchedi.hot", "kind": "model", "title": "小米YU7", "car": "小米YU7"}])
    seen = {}
    monkeypatch.setattr(topics, "suggest",
                        lambda items, **kw: seen.update(kw, items=items) or (ideas or [_idea()]))
    monkeypatch.setattr(rapid, "video_seconds", lambda car: secs)
    return seen


def test_triggers_rank_and_climb(fresh_db, monkeypatch):
    from app import config
    from app.team import rapid

    _rapid_env(monkeypatch, [
        {"source": "dongchedi.hot", "car": "小米YU7", "rank": 2, "prev": 9, "delta": 7},
        {"source": "dongchedi.hot", "car": "理想i8", "rank": 4, "prev": 5, "delta": 1},   # 涨得少
        {"source": "dongchedi.sales", "car": "问界M8", "rank": 8, "prev": 20, "delta": 12},  # 不在前 5
        {"source": "dongchedi.sales", "car": "极氪9X", "rank": 3, "prev": None, "delta": None},
    ])
    got = rapid.triggers(config.TEAM_CATEGORY, dt.datetime(2026, 10, 24, 10))
    assert [t["car"] for t in got] == ["小米YU7", "极氪9X"]
    assert "第 9 名升到第 2 名" in got[0]["why"] and "新上榜" in got[1]["why"]


def test_rapid_makes_portrait_item_and_respects_caps(fresh_db, monkeypatch):
    from app import config
    from app.team import Skip, rapid

    seen = _rapid_env(monkeypatch, [
        {"source": "dongchedi.hot", "car": "小米YU7", "rank": 2, "prev": 9, "delta": 7}])
    now = dt.datetime(2026, 10, 24, 10, 20)
    out = rapid.run(llm=object(), now=now)
    assert "1 条快反" in out
    item = fresh_db.list_plan_items(config.TEAM_CATEGORY, limit=10)[0]
    assert item["kind"] == "rapid" and item["heat"] == 5 and item["aspect"] == config.PORTRAIT
    assert item["duration"] in (60, 90)
    assert item["slot_at"] == dt.datetime(2026, 10, 24, 17).timestamp()  # 现在 + 6h 取整点
    assert "快反" in item["rationale"] and item["note"] == ""
    assert "只围绕这台车" in seen["playbook"]
    # 同车冷却中:不再出
    with pytest.raises(Skip):
        rapid.run(llm=object(), now=now)


def test_rapid_slot_skips_night():
    from app.team import rapid

    assert rapid.slot_for(dt.datetime(2026, 10, 24, 10, 20)) == dt.datetime(2026, 10, 24, 17).timestamp()
    assert rapid.slot_for(dt.datetime(2026, 10, 24, 21, 10)) == dt.datetime(2026, 10, 25, 8).timestamp()
    assert rapid.slot_for(dt.datetime(2026, 10, 24, 0, 30)) == dt.datetime(2026, 10, 24, 8).timestamp()


def test_rapid_daily_cap(fresh_db, monkeypatch):
    from app.team import Skip, rapid

    _rapid_env(monkeypatch, [
        {"source": "dongchedi.hot", "car": c, "rank": 1, "prev": None, "delta": None}
        for c in ("小米YU7", "理想i8", "问界M8")],
        ideas=[_idea("小米YU7"), _idea("理想i8"), _idea("问界M8")])
    now = dt.datetime(2026, 10, 24, 10)
    assert "2 条快反" in rapid.run(llm=object(), now=now)
    with pytest.raises(Skip, match="今天已经出了"):
        rapid.run(llm=object(), now=now)


def test_rapid_thin_pool_is_flagged_not_forced(fresh_db, monkeypatch):
    from app import config
    from app.team import rapid

    _rapid_env(monkeypatch, [{"source": "dongchedi.hot", "car": "小米YU7", "rank": 1,
                              "prev": None, "delta": None}], secs=20)
    rapid.run(llm=object(), now=dt.datetime(2026, 10, 24, 10))
    item = fresh_db.list_plan_items(config.TEAM_CATEGORY, limit=10)[0]
    assert item["status"] == "proposed"  # 只出选题等你批,不自动开工
    assert "素材不够" in item["note"] and "20 秒" in item["note"]


def test_event_day_triggers_rapid_once(fresh_db, monkeypatch):
    from app import config
    from app.team import events, rapid

    cat = config.TEAM_CATEGORY
    seen = _rapid_env(monkeypatch, [])
    now = dt.datetime(2026, 10, 24, 8)
    ev = events.add_manual(cat, "2026-10-24", "小米YU7 上市", "launch", "小米YU7", today=now.date())
    rapid.run(llm=object(), now=now)
    assert seen["items"][0]["title"] == "小米YU7 上市"
    item_id = fresh_db.get_industry_event(ev["id"])["plan_item_id"]
    assert fresh_db.get_plan_item(item_id)["kind"] == "rapid"
    assert rapid.triggers(cat, now) == []


def test_rapid_ideas_must_be_single_car(fresh_db, monkeypatch):
    from app.team import TeamError, rapid

    _rapid_env(monkeypatch, [{"source": "dongchedi.hot", "car": "小米YU7", "rank": 1,
                              "prev": None, "delta": None}], ideas=[_idea("理想i8")])
    with pytest.raises(TeamError):
        rapid.run(llm=object(), now=dt.datetime(2026, 10, 24, 10))


# --------------------------------------------------------------------------
# 主编 / 复盘官接入
# --------------------------------------------------------------------------
def test_editor_uses_calendar_and_benchmark(fresh_db, monkeypatch):
    from app import config
    from app.pipeline import topics
    from app.team import editor, events, scout

    cat = config.TEAM_CATEGORY
    now = dt.datetime(2026, 10, 24, 9)
    events.add_manual(cat, "2026-10-29", "小米YU7 上市", "launch", "小米YU7", today=now.date())
    fresh_db.add_benchmark_account("MS4wLjABAAAA_a")
    fresh_db.upsert_benchmark_posts([{"id": "p1", "account_id": "MS4wLjABAAAA_a",
                                      "title": "YU7 真实油耗", "car": "小米YU7", "duration": 80,
                                      "posted_at": now.timestamp() - 3600, "likes": 900, "burst": 5.0}])
    seen = {}
    monkeypatch.setattr(scout, "fresh_items", lambda category: [{"title": "x"}])
    monkeypatch.setattr(scout, "rising", lambda category, limit=8: [])
    monkeypatch.setattr(topics, "suggest", lambda items, **kw: seen.update(kw) or [_idea()])
    editor.run(cat, llm=object(), now=now)
    assert "行业日历" in seen["playbook"] and "YU7 真实油耗" in seen["playbook"]
    item = fresh_db.list_plan_items(cat, limit=10)[0]
    assert "5 天后上市:小米YU7 上市" in item["rationale"] and "对标号同车型出了爆款" in item["rationale"]
    assert any("对标爆款" in e for e in item["evidence"])
    assert item["heat"] == 4  # 存的是 LLM 给的热度,加成只影响排序


def test_coach_report_has_benchmark_compare(fresh_db):
    from app.team import coach

    fresh_db.add_benchmark_account("MS4wLjABAAAA_a")
    fresh_db.upsert_benchmark_posts([{"id": "p1", "account_id": "MS4wLjABAAAA_a",
                                      "title": "YU7 真实油耗", "car": "小米YU7", "duration": 80,
                                      "posted_at": time.time() - 3600, "likes": 900, "burst": 5.0}])
    report = coach.build_report([], [], time.time())
    assert report["benchmark"]["theirs"][0]["title"] == "YU7 真实油耗"
    assert "对标爆款:《YU7 真实油耗》" in coach._facts(report)
