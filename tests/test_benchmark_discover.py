"""对标号自动发现 + 差距报告。全部用临时库和注入的假搜索,不开浏览器。"""
from __future__ import annotations

import pytest

from app.team import benchmark, benchmark_discover as disc


@pytest.fixture
def fresh_db(tmp_path, monkeypatch):
    from app import config, db

    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(config, "PROJECTS_DIR", tmp_path / "data" / "projects")
    monkeypatch.setattr(db, "_INITIALISED", False)
    return db


def _aweme(sec, nick="某号", likes=2000, followers=50_000, aid=None):
    return {"aweme_id": aid or f"{sec}-{likes}", "statistics": {"digg_count": likes},
            "author": {"sec_uid": sec, "nickname": nick, "follower_count": followers}}


def test_rank_filters_and_orders():
    batches = [
        [_aweme("A", "测评君", 3000), _aweme("B", "大V", 9000, followers=5_000_000),
         _aweme("C", "某某旗舰店", 8000), _aweme("D", "小号", 100), _aweme("E", "新手", 800, 3000)],
        [_aweme("A", "测评君", 1500, aid="a2"), _aweme("F", "路人", 900)],
    ]
    ranked = disc.rank_authors(batches, exclude={"F"})
    # B 粉丝太多、C 是门店、D 赞太少、E 粉丝太少、F 已在库
    assert [r["sec_uid"] for r in ranked] == ["A"]
    assert ranked[0]["videos"] == 2


def test_rank_unknown_followers_kept():
    ranked = disc.rank_authors([[_aweme("A", followers=0)]], exclude=set())
    assert [r["sec_uid"] for r in ranked] == ["A"]


def test_discover_adds_capped(fresh_db, monkeypatch):
    monkeypatch.setattr(disc, "keywords", lambda category, limit=5: ["k1", "k2"])
    calls = []

    def search(kw):
        calls.append(kw)
        return [_aweme(f"S{i}", f"号{i}", 1000 + i * 100) for i in range(8)]

    out = disc.discover("auto_long", search=search, sleep=lambda s: None, max_new=3)
    assert calls == ["k1", "k2"]
    assert len(out["added"]) == 3 and out["error"] == ""
    assert len(fresh_db.list_benchmark_accounts()) == 3
    # 再跑一轮不会重复加已有的
    out2 = disc.discover("auto_long", search=search, sleep=lambda s: None, max_new=3)
    ids = {a["id"] for a in fresh_db.list_benchmark_accounts()}
    assert len(ids) == 6 and len(out2["added"]) == 3


def test_discover_stops_on_error_and_flags_login(fresh_db, monkeypatch):
    from app.publish.base import PublishError

    monkeypatch.setattr(disc, "keywords", lambda category, limit=5: ["k1", "k2"])
    calls = []

    def search(kw):
        calls.append(kw)
        raise PublishError("登录失效", retryable=False, credential=True)

    out = disc.discover("auto_long", search=search, sleep=lambda s: None)
    assert calls == ["k1"] and "登录失效" in out["error"]
    assert fresh_db.get_team_setting(benchmark._LOGIN_KEY, None)


def test_discover_respects_account_cap(fresh_db, monkeypatch):
    monkeypatch.setattr(benchmark, "MAX_ACCOUNTS", 1)
    fresh_db.add_benchmark_account("X")
    out = disc.discover("auto_long", search=lambda kw: [], sleep=lambda s: None)
    assert out["added"] == [] and "已满" in out["error"]


def test_gap_suggestions():
    struct = {"claim_share": 0.6, "dims": [
        {"dimension": "opening", "label": "开场画面",
         "theirs": [{"value": "text", "label": "大字", "share": 0.7}],
         "ours": [{"value": "car", "label": "实拍车", "share": 0.9}]},
        {"dimension": "pace", "label": "剪辑节奏",
         "theirs": [{"value": "fast", "label": "快", "share": 0.3}],
         "ours": [{"value": "slow", "label": "慢", "share": 0.8}]},
        {"dimension": "duration", "label": "时长",
         "theirs": [{"value": "m", "label": "中", "share": 0.5}],
         "ours": [{"value": "m", "label": "中", "share": 0.5}]},
    ]}
    s = benchmark.gap_suggestions(struct)
    assert len(s) == 2
    assert "大字" in s[0] and "70%" in s[0] and "实拍车" in s[0]
    assert "判断" in s[1]


def test_captcha_page_detected():
    from app.publish.platforms.douyin.profile import _search_items, is_captcha_page

    assert is_captcha_page("验证码中间页", "")
    assert is_captcha_page("", "请拖动滑块完成验证")
    assert not is_captcha_page("抖音-记录美好生活", "精选 推荐")
    body = {"data": [{"aweme_info": {"aweme_id": "1"}}, {"aweme_id": "2"}, {"x": 1}],
            "aweme_list": [{"aweme_id": "3"}]}
    assert [a["aweme_id"] for a in _search_items(body)] == ["1", "2", "3"]


def test_gap_report_empty(fresh_db):
    r = benchmark.gap_report()
    assert r["accounts"] == 0 and r["top"] == [] and r["suggestions"] == []
