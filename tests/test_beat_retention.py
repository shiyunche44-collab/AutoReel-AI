"""阶段 5:逐拍掉人(进度条分析的跳过率 → 每拍相对跳过率 → 按版式学 → 回灌分镜 / 切片)。"""
from __future__ import annotations

import time

from tests.test_coach import fresh_db, no_playbook  # noqa: F401 - fixtures

#: 4 拍:开头章 0–10 秒 hook、正文 10–30 秒 panel + 30–42 秒 concept、结论章 42–50 秒 broll
BOARD = [
    {"idx": 1, "duration": 10, "section": "开场", "chapter": "开场", "layout": "hook"},
    {"idx": 2, "duration": 20, "section": "参数", "chapter": "参数", "layout": "panel"},
    {"idx": 3, "duration": 12, "section": "参数", "chapter": "参数", "layout": "concept"},
    {"idx": 4, "duration": 8, "section": "结论", "chapter": "结论", "layout": "broll"},
]


def _points(*skips):
    """进度条分析的点:4、12、20 … 秒,每点跳过率按给的顺序。"""
    return [[4.0 + 8 * i, float(s), 0.0] for i, s in enumerate(skips)]


def _project(db, pid, board=BOARD, *, third=()):
    from app.pipeline import explainer

    db.write_json(pid, "storyboard.json", board)
    explainer.beats_dir(pid)
    for idx in third:
        db.write_json(pid, f"beats/{idx:03d}.json", [{"kind": "video", "length": 5, "third_party": True}])


def test_parse_progress():
    from app.publish.platforms import douyin

    body = {"jump_forward": [{"key": "12", "value": 30}, {"key": "4", "value": 10}, {"key": "x"}],
            "jump_backward": [{"key": "4", "value": 2}]}
    assert douyin.parse_progress(body) == [[4.0, 10.0, 2.0], [12.0, 30.0, 0.0]]
    assert douyin.parse_progress({}) == []


def test_beat_map_and_skips(fresh_db):
    from app.team import beat_retention as br

    _project(fresh_db, "p1", third=(3,))
    beats = br.beat_map("p1")
    assert [(b["start"], b["end"], b["role"]) for b in beats] == [
        (0, 10, "hook"), (10, 30, "body"), (30, 42, "body"), (42, 50, "ending")]
    assert [b["footage"] for b in beats] == ["rendered", "rendered", "third_party", "rendered"]
    # 点:4→拍1,12/20/28→拍2,36→拍3,44→拍4,52 超出片长不算
    scored = br.beat_skips(_points(10, 10, 10, 10, 50, 10, 99), beats)
    mean = (10 * 5 + 50) / 6
    assert [b["rel"] for b in scored] == [round(10 / mean, 3)] * 2 + [round(50 / mean, 3),
                                                                    round(10 / mean, 3)]
    assert br.beat_skips(_points(0, 0, 0), beats) == []  # 没人拖 ≠ 都很好
    # 降级:平均观看 33 秒 → 第 3 拍
    left = br.leave_beat(beats, 33.0)
    assert left["pos"] == 3 and left["layout"] == "concept"
    assert br.leave_beat(beats, 80) is None and br.leave_beat(beats, None) is None


def test_learn_groups_shrinks_and_needs_two_works():
    from app.team import beat_retention as br

    def work(pid, rels):
        layouts = ["concept", "panel", "panel", "broll"]
        return {"pub_id": pid, "title": f"片{pid}", "beats": [
            {"layout": lay, "role": "body", "footage": "official", "section": "", "rel": r}
            for lay, r in zip(layouts, rels)]}

    one = [work("a", [3.0, 0.5, 0.5, 1.0]) for _ in range(3)]
    assert not [r for r in br.learn(one, "w") if r["dimension"] == "beat_layout"]  # 只有 1 条片

    rows = {r["id"]: r for r in br.learn([work("a", [3.0, 0.5, 0.5, 1.0]),
                                          work("b", [2.0, 0.6, 0.4, 1.0])] * 2, "w")}
    concept = rows["beat:beat_layout:concept:hold"]
    # 4 拍平均 2.5,收缩:(10 + 4) / 8 = 1.75 → 留人 0.571 → 夹到 0.7
    assert concept["effect"] == 0.7 and concept["n"] == 4 and concept["metric"] == "hold"
    assert concept["evidence"][0]["pub_id"] == "a"  # 少做的结论先列最掉人的那条
    panel = rows["beat:beat_layout:panel:hold"]
    assert panel["effect"] == 1.3  # 8 拍平均 0.5:(4 + 4) / 12 → 留人 1.5 → 夹到 1.3
    assert "beat:beat_layout:broll:hold" not in rows  # 和平均一样
    assert br.learn([], "w") == []


def test_layout_hint_and_describe(fresh_db):
    from app.team import beat_retention as br, playbook

    rows = [{"id": "beat:beat_layout:concept:hold", "dimension": "beat_layout", "value": "concept",
             "metric": "hold", "effect": 0.7, "n": 6, "confidence": "medium", "evidence": []},
            {"id": "beat:beat_layout:panel:hold", "dimension": "beat_layout", "value": "panel",
             "metric": "hold", "effect": 1.2, "n": 8, "confidence": "medium", "evidence": []}]
    assert br.layout_hint([]) == ""
    hint = br.layout_hint(rows)
    assert "拖过去的版式:概念动画" in hint and "留得住人的版式:参数面板" in hint
    fresh_db.replace_learnings("w", rows, source="beat")
    got = {r["value"]: r for r in fresh_db.list_learnings()}
    assert playbook.describe(got["concept"]) == "逐拍·版式 = 概念动画:留人 0.70 倍(6 拍,置信度中)"
    assert playbook.weights("beat_layout") == {"concept": 0.7, "panel": 1.2}
    # 写自己的结论不会把逐拍结论标 stale
    fresh_db.replace_learnings("w2", [])
    assert len(fresh_db.list_learnings()) == 2


def test_hooks_feed_build_beats_and_pick_clips(fresh_db):
    from app.pipeline import explainer, repurpose
    from app.team import beat_retention as br, playbook

    spans = [
        {"idx": 1, "start": 0, "end": 10, "chapter": "开场", "refs": 1, "card": False, "layout": "hook"},
        {"idx": 2, "start": 10, "end": 30, "chapter": "A", "refs": 2, "card": False, "layout": "concept"},
        {"idx": 3, "start": 30, "end": 50, "chapter": "B", "refs": 2, "card": False, "layout": "panel"},
    ]
    plain = repurpose.windows(spans, lo=15, hi=25)
    assert [w["start"] for w in plain] == [10, 30]  # 一样密,按先后
    assert explainer._layout_hint() == ""

    fresh_db.replace_learnings("w", [
        {"id": "beat:beat_layout:concept:hold", "dimension": "beat_layout", "value": "concept",
         "metric": "hold", "effect": 0.7, "n": 6, "confidence": "medium", "evidence": []}],
        source="beat")
    playbook.install()
    assert explainer.layout_hint is br.layout_hint
    assert "概念动画" in explainer._layout_hint()
    weighted = repurpose.windows(spans, lo=15, hi=25)
    assert [w["start"] for w in weighted] == [30, 10]  # 概念动画那段观众爱拖,排到后面
    assert weighted[1]["score"] == round(2 / 20 * 0.7, 4)
    playbook.uninstall()
    assert repurpose.layout_weights is None and explainer.layout_hint is None


def test_analyst_fetches_progress_after_a_day(fresh_db):
    from app.team import analyst
    from tests.test_analyst import _pub, _work

    now = time.time()
    _pub(fresh_db, "day", state="published", title="老作品", remote_id="w_day",
         published_at=now - 25 * 3600)
    _pub(fresh_db, "new", state="published", title="新作品", remote_id="w_new",
         published_at=now - 3600)
    works = [_work("w_day", title="老作品", at=now - 25 * 3600, views=300),
             _work("w_new", title="新作品", at=now - 3600, views=9)]
    asked = []

    def progress(ids):
        asked.extend(ids)
        return {"w_day": _points(1, 2, 3)}

    got = analyst.collect(now, fetch=lambda since: works, fetch_progress=progress)
    assert got["curves"] == 1 and asked == ["w_day"]  # 才发 1 小时的不抓
    curve = fresh_db.list_progress_curves()["day"]
    assert curve["points"][1] == [12.0, 2.0, 0.0] and curve["views"] == 300


def test_review_learns_beats_and_reports(fresh_db):
    import datetime as dt

    from app.team import coach

    now = dt.datetime(2026, 10, 25, 20, 0)
    ts = now.timestamp()
    for n in range(4):  # 每条片 1 拍概念动画,攒够 4 拍
        pid = f"b{n}"
        _project(fresh_db, f"p_{pid}")
        fresh_db.create_publication({"id": pid, "project_id": f"p_{pid}", "account_id": "acc1",
                                     "platform": "douyin", "title": f"作品{n}", "state": "published",
                                     "remote_id": f"w{n}", "published_at": ts - (3 + n) * 86400})
        fresh_db.add_snapshot(pid, "douyin", ts - (2 + n) * 86400,
                              {"views": 100 + n, "avg_watch_sec": 33.0, "hours_since_publish": 24}, {})
        # 第 3 拍(概念动画,36 秒那个点)被拖得最多
        fresh_db.save_progress_curve(pid, _points(10, 10, 10, 10, 60, 10), 100, ts=ts - 86400)
    report = coach.review(now)
    learned = {r["id"]: r for r in fresh_db.list_learnings()}
    assert learned["beat:beat_layout:concept:hold"]["source"] == "beat"
    assert learned["beat:beat_layout:concept:hold"]["effect"] < 1
    beats = report["beats"]
    assert beats[0]["source"] == "progress" and beats[0]["worst"][0]["pos"] == 3
    assert beats[0]["leave"]["pos"] == 3
    facts = coach._facts(report)
    assert "逐拍:《作品0》平均观众看到第 3/4 拍" in facts and "全片平均的" in facts
