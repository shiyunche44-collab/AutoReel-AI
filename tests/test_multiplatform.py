"""多平台数据:B站 view 接口、小红书按单篇笔记对数据、统计按平台 × 主片 / 切片 / 笔记分基线。"""
from __future__ import annotations

import contextlib
import json
import time

import pytest

from tests.test_coach import _project, fresh_db, no_playbook  # noqa: F401


# --------------------------------------------------------------------------
# B站:view 接口(stat 在 data.stat 里,顺带时长 / 发布时间 / mid)
# --------------------------------------------------------------------------
STAT = {"view": 5000, "like": 300, "reply": 40, "favorite": 60, "coin": 25, "share": 9, "danmaku": 12}


def _bili(monkeypatch, view_payload):
    from app.publish.base import Account
    from app.publish.platforms import bilibili

    calls = []

    def fake_request(client, method, url, *, params=None, timeout=0, **kw):
        calls.append((url.rsplit("/x/", 1)[-1], dict(params or {})))
        if url.endswith("/view"):
            return view_payload
        if url.endswith("/archive/stat"):
            return {"code": 0, "data": STAT}
        if url.endswith("/relation/stat"):
            return {"code": 0, "data": {"follower": 777}}
        return {"code": -101}

    monkeypatch.setattr(bilibili, "request_json", fake_request)
    monkeypatch.setattr(bilibili, "new_client", lambda *a, **k: contextlib.nullcontext(object()))
    acc = Account(id="b1", platform="bilibili", name="b")
    return bilibili.BilibiliAdapter(), acc, calls


def test_bilibili_metrics_use_the_view_api(monkeypatch):
    from app.publish.base import PlatformRef

    adapter, acc, calls = _bili(monkeypatch, {"code": 0, "data": {
        "stat": STAT, "duration": 245, "pubdate": 1761000000, "owner": {"mid": 42}}})
    snap = adapter.fetch_metrics(PlatformRef(remote_id="BV1x"), acc)
    assert (snap.views, snap.likes, snap.coins, snap.favorites, snap.comments) == (5000, 300, 25, 60, 40)
    assert snap.followers == 777
    assert snap.raw["duration"] == 245 and snap.raw["pubdate"] == 1761000000
    # mid 从 view 里拿,不用登录查 nav
    assert ("relation/stat", {"vmid": "42"}) in calls and not any(c[0] == "web-interface/nav" for c in calls)


def test_bilibili_metrics_fall_back_to_archive_stat(monkeypatch):
    from app.publish.base import PlatformRef

    adapter, acc, calls = _bili(monkeypatch, {"code": -412, "message": "请求被拦截"})
    snap = adapter.fetch_metrics(PlatformRef(remote_id="BV1x"), acc)
    assert snap.views == 5000 and snap.coins == 25
    assert [c[0] for c in calls][:2] == ["web-interface/view", "web-interface/archive/stat"]


# --------------------------------------------------------------------------
# 小红书:截笔记列表 JSON,按 id / 标题对上这一篇
# --------------------------------------------------------------------------
NOTES = {"code": 0, "data": {"notes": [
    {"id": "n1", "display_title": "理想i8 续航实测!", "view_count": "1.2万", "likes": 30,
     "collected_count": 5, "comments_count": 2, "shared_count": 1, "imp_count": 40000},
    {"id": "n2", "display_title": "小米YU7 怎么选", "view_count": 800, "likes": 9},
    {"id": "n3", "display_title": "小米YU7 怎么选", "view_count": 20},
]}}


def test_parse_xhs_notes_reads_counts_and_impressions():
    from app.metrics.collector import parse_xhs_notes

    notes = {n["id"]: n for n in parse_xhs_notes([{"irrelevant": 1}, NOTES])}
    assert notes["n1"]["metrics"] == {"views": 12000, "likes": 30, "comments": 2, "favorites": 5,
                                      "shares": 1}
    assert notes["n1"]["impressions"] == 40000
    assert notes["n3"]["metrics"]["likes"] is None  # 缺的是 None,不是 0


def test_match_xhs_note_by_id_then_title_and_refuses_ambiguity():
    from app.metrics.collector import match_xhs_note, parse_xhs_notes

    notes = parse_xhs_notes([NOTES])
    assert match_xhs_note(notes, "n2")["id"] == "n2"
    assert match_xhs_note(notes, "", "理想i8续航实测")["id"] == "n1"  # 标点 / 空格不算
    assert match_xhs_note(notes, "", "小米YU7 怎么选") is None  # 两篇同名:宁缺毋错
    assert match_xhs_note(notes, "", "") is None


class _Resp:
    def __init__(self, body):
        self.request = type("R", (), {"resource_type": "xhr"})()
        self._body = body

    def text(self):
        return self._body


class _Session:
    def __init__(self, bodies):
        self.bodies = bodies
        self.handlers = []
        self.page = self
        self.url = "https://creator.xiaohongshu.com/new/note-manager"

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def on(self, event, fn):
        self.handlers.append(fn)

    def goto(self, url, wait_ms=0):
        for b in self.bodies:
            for fn in self.handlers:
                fn(_Resp(b))

    def page_text(self):
        return "笔记管理"


def test_xhs_metrics_are_per_note(monkeypatch):
    from app.publish import browser
    from app.publish.base import Account, PlatformRef, PublishError
    from app.publish.platforms.xiaohongshu import XiaohongshuAdapter

    monkeypatch.setattr(browser, "BrowserSession", lambda key: _Session([json.dumps(NOTES)]))
    acc = Account(id="x1", platform="xiaohongshu", name="x", auth_kind="browser")
    snap = XiaohongshuAdapter().fetch_metrics(PlatformRef("", extra={"title": "理想i8 续航实测!"}), acc)
    assert snap.views == 12000 and snap.favorites == 5
    assert snap.raw["scope"] == "note" and snap.raw["note_id"] == "n1" and snap.raw["impressions"] == 40000
    with pytest.raises(PublishError):  # 对不上不拿账号汇总顶替
        XiaohongshuAdapter().fetch_metrics(PlatformRef("", extra={"title": "没发过的"}), acc)


def test_collector_passes_the_title_for_platforms_without_ids(monkeypatch):
    from app.metrics import collector

    seen = {}

    class A:
        def fetch_metrics(self, ref, account):
            seen["ref"] = ref

    monkeypatch.setattr(collector, "adapter_for", lambda p: A())
    collector.collect({"platform": "xiaohongshu", "title": "理想i8", "published_at": 1.0}, None)
    assert seen["ref"].extra["title"] == "理想i8" and seen["ref"].remote_id == ""


# --------------------------------------------------------------------------
# 特征 / 统计:各平台、切片 / 笔记都进学习,基线各算各的
# --------------------------------------------------------------------------
def _pub(db, pid, *, platform="douyin", piece="", t0, views, likes=5, raw=None, acc=None):
    _project(db, f"p_{pid}")
    db.create_publication({"id": pid, "project_id": f"p_{pid}", "account_id": acc or f"acc_{platform}",
                           "platform": platform, "piece": piece, "title": f"作品{pid}",
                           "state": "published", "remote_id": f"w_{pid}", "published_at": t0,
                           "variant": json.dumps({"title_kind": "number", "cover": "a"})})
    db.add_snapshot(pid, platform, t0 + 24 * 3600,
                    {"views": views, "likes": likes, "hours_since_publish": 24.0}, raw)


def test_rows_cover_every_platform_and_piece(fresh_db):
    from app.team import features

    t0 = time.time() - 3 * 86400
    _pub(fresh_db, "d", t0=t0, views=1000)
    _pub(fresh_db, "b", platform="bilibili", t0=t0, views=300)
    _pub(fresh_db, "c", piece="clip1", t0=t0, views=200)
    _pub(fresh_db, "x", platform="xiaohongshu", piece="note", t0=t0, views=90, raw={"scope": "note"})
    # 小红书以前抓的账号汇总:不当成这一篇的成绩
    _pub(fresh_db, "old", platform="xiaohongshu", piece="note", t0=t0, views=99999, raw={"chars": 300})
    rows = {r["pub_id"]: r for r in features.rows()}
    assert set(rows) == {"d", "b", "c", "x"}
    assert (rows["b"]["platform"], rows["c"]["piece"]) == ("bilibili", "clip1")
    # 切片只学切片也带着的维度:开场 / 结尾说的是主片
    assert rows["c"]["features"]["opening"] == "" and rows["c"]["features"]["title_kind"] == "number"
    assert rows["d"]["features"]["opening"] == "scene"
    assert [r["pub_id"] for r in features.main_rows(list(rows.values()))] == ["d"]


def test_early_rows_and_trend_stay_on_the_main_platform(fresh_db):
    from app.team import features

    t0 = time.time() - 3 * 3600
    for pid, platform, piece in (("d", "douyin", ""), ("b", "bilibili", ""), ("c", "douyin", "clip1")):
        _project(fresh_db, f"p_{pid}")
        fresh_db.create_publication({"id": pid, "project_id": f"p_{pid}", "account_id": "a",
                                     "platform": platform, "piece": piece, "title": pid,
                                     "state": "published", "published_at": t0})
        fresh_db.add_snapshot(pid, platform, t0 + 3600, {"views": 9, "bounce_2s": 0.4,
                                                         "hours_since_publish": 1.0})
    assert [r["pub_id"] for r in features.early_rows()] == ["d"]


def test_lifts_keep_platforms_and_pieces_apart():
    from app.team import stats

    def row(pid, platform, piece, acc, views, eng):
        return {"pub_id": pid, "account_id": acc, "platform": platform, "piece": piece,
                "title": pid, "published_at": 0.0, "features": {},
                "outcome": {"views": views, "completion_rate": eng}}

    rows = [row("d1", "douyin", "", "acc_d", 1000, 0.02), row("d2", "douyin", "", "acc_d", 3000, 0.06),
            row("c1", "douyin", "clip1", "acc_d", 100, 0.01), row("c2", "douyin", "clip2", "acc_d", 300, 0.03),
            row("b1", "bilibili", "", "acc_b", 200, 0.10), row("b2", "bilibili", "", "acc_b", 600, 0.30)]
    lift = {r["pub_id"]: r["lift"] for r in stats.lifts(rows)}
    # 切片跟切片比(同一个号),B站的完播跟 B站比
    assert lift["c2"]["views"] == pytest.approx(1.5) and lift["d2"]["views"] == pytest.approx(1.5)
    assert lift["b2"]["completion_rate"] == pytest.approx(1.5)
    assert lift["d2"]["completion_rate"] == pytest.approx(1.5)
    assert lift["c1"]["completion_rate"] == pytest.approx(0.5)


def test_platform_summary_groups_clips_together():
    from app.team import features

    def row(platform, piece, views):
        return {"platform": platform, "piece": piece, "outcome": {"views": views, "engagement": 0.05}}

    got = features.platform_summary([row("douyin", "", 100), row("douyin", "clip1", 10),
                                     row("douyin", "clip2", 30), row("bilibili", "", 50)])
    by = {(g["platform"], g["piece"]): g for g in got}
    assert by[("douyin", "clip")]["works"] == 2 and by[("douyin", "clip")]["median_views"] == 20
    assert by[("douyin", "clip")]["label"] == "抖音切片" and by[("bilibili", "")]["label"] == "B站"


def test_coach_learns_from_other_platforms_but_reports_douyin(fresh_db, monkeypatch):
    from app.team import coach

    t0 = time.time() - 3 * 86400
    for i in range(3):
        _pub(fresh_db, f"d{i}", t0=t0 - i * 3600, views=1000 * (i + 1))
        _pub(fresh_db, f"b{i}", platform="bilibili", t0=t0 - i * 3600, views=100 * (i + 1))
    monkeypatch.setattr(coach, "write_text", lambda report, llm: {"summary": "", "next": []})
    report = coach.review(llm=None)
    assert report["totals"]["works"] == 3  # 总数只算抖音主片
    assert {(p["platform"], p["works"]) for p in report["platforms"]} == {("douyin", 3), ("bilibili", 3)}
    assert "一稿多发" in coach._facts(report)
