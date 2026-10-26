"""阶段 4:对标先验(爆款倍数 → 手册先验、和自己的结论合并)+ 拆爆款结构(前 3 秒画面 + 视觉标签)。"""
from __future__ import annotations

import datetime as dt
import time

import pytest

from tests.test_coach import _row, fresh_db, no_playbook  # noqa: F401 - fixtures

ACC = "MS4wLjABAAAA_a"


def _post(db, pid, burst, *, title="这车别买", car="", duration=80, age_days=1.0, now=None,
          video="", pinned=0):
    now = now or time.time()
    db.upsert_benchmark_posts([{"id": pid, "account_id": ACC, "title": title, "car": car,
                                "duration": duration, "posted_at": now - age_days * 86400,
                                "likes": int(burst * 100), "burst": burst, "pinned": pinned,
                                "video_url": video, "cover_url": video and "https://c/x.jpg"}])


def _bench(pid, burst, **feat):
    return {"id": pid, "title": f"对标{pid}", "burst": burst, "features": feat}


# --------------------------------------------------------------------------
# 先验
# --------------------------------------------------------------------------
def test_guess_kind():
    from app.pipeline import packaging

    assert packaging.guess_kind("小米SU7 月销3万背后 #汽车") == "number"
    assert packaging.guess_kind("宝妈通勤选它就够了") == "person"
    assert packaging.guess_kind("2026款理想L6,这车别买") == "stake"  # 年款不算数字


def test_learn_benchmark_shrinks_clamps_and_yields_to_own():
    from app.team import stats

    posts = ([_bench(f"n{i}", 8.0, title_kind="number", duration="≤90s") for i in range(3)]
             + [_bench(f"s{i}", 1.0, title_kind="stake", duration="≤90s") for i in range(6)]
             + [_bench("p0", 3.0, title_kind="person"), _bench("z", 0, title_kind="person")])
    rows = {r["id"]: r for r in stats.learn_benchmark(posts, "2026-W43")}
    # 平均倍数(封顶 4):(12 + 6 + 3) / 10 = 2.1;数字 = (3 × 4/2.1 + 6) / 9 → 夹到 1.25
    assert rows["bench:title_kind:number:views"]["effect"] == 1.25
    assert rows["bench:title_kind:stake:views"]["effect"] == 0.8  # (6 × 0.476 + 6) / 12 → 夹到 0.8
    assert "bench:title_kind:person:views" not in rows  # 只有 1 条(倍数 0 的不算)
    assert rows["bench:title_kind:number:views"]["evidence"][0]["post_id"] == "n0"
    # 时长档:9 条差不多和平均一样,收缩后偏离不到 5%,不算
    assert "bench:duration:≤90s:views" not in rows

    own = [_row(f"o{i}", 100, title_kind="number") for i in range(3)]
    ids = {r["id"] for r in stats.learn_benchmark(posts, "w", own=own)}
    assert "bench:title_kind:number:views" not in ids and "bench:title_kind:stake:views" in ids
    assert stats.learn_benchmark([], "w") == []


def test_sources_expire_separately_and_merge(fresh_db):
    from app.team import playbook

    own = {"id": "title_kind:number:views", "dimension": "title_kind", "value": "number",
           "metric": "views", "effect": 0.7, "n": 4, "confidence": "medium", "evidence": []}
    bench = [{**own, "id": "bench:title_kind:number:views", "effect": 1.25, "n": 9},
             {**own, "id": "bench:title_kind:stake:views", "value": "stake", "effect": 0.8, "n": 6}]
    fresh_db.replace_learnings("w1", [own])
    fresh_db.replace_learnings("w1", bench, source="benchmark")
    assert len(fresh_db.list_learnings()) == 3  # 写先验没把自己的标 stale
    # 同一取值自己有结论 → 只用自己的;自己没有的才用对标的
    assert playbook.weights("title_kind") == {"number": 0.7, "stake": 0.8}
    stake = next(r for r in fresh_db.list_learnings() if r["value"] == "stake")
    assert playbook.describe(stake) == "对标号 标题写法 = 立场:爆款倍数 0.80 倍(6 条对标作品)"

    fresh_db.replace_learnings("w2", [], source="benchmark")
    assert [r["id"] for r in fresh_db.list_learnings()] == ["title_kind:number:views"]

    from app.team import views
    fresh_db.replace_learnings("w3", bench, source="benchmark")
    shadow = {r["id"]: r["shadowed"] for r in views._playbook_views()}
    assert shadow == {"title_kind:number:views": False, "bench:title_kind:number:views": True,
                      "bench:title_kind:stake:views": False}


def test_review_writes_prior_even_without_works(fresh_db):
    from app.team import Skip, coach

    now = dt.datetime(2026, 10, 25, 20, 0)
    ts = now.timestamp()
    fresh_db.add_benchmark_account(ACC)
    for i in range(3):
        _post(fresh_db, f"n{i}", 8.0, title="零百3.2秒,快得离谱", now=ts)
    for i in range(6):
        _post(fresh_db, f"s{i}", 1.0, now=ts)
    _post(fresh_db, "old", 9.0, title="零百3.2秒", now=ts, age_days=90)  # 太久以前
    _post(fresh_db, "top", 9.0, title="零百3.2秒", now=ts, pinned=1)  # 置顶不算
    with pytest.raises(Skip):
        coach.review(now)
    got = {r["id"]: r for r in fresh_db.list_learnings()}
    assert got["bench:title_kind:number:views"]["source"] == "benchmark"
    assert got["bench:title_kind:number:views"]["n"] == 3
    log = fresh_db.list_iteration_log()
    assert any(x["kind"] == "weight" for x in log)  # 先验改了标题写法的权重,留痕


# --------------------------------------------------------------------------
# 拆结构
# --------------------------------------------------------------------------
def _frames(tmp, shots):
    """每个数字是一个镜头:同一个数字画面相同,换数字就是切了一刀。"""
    import random

    from PIL import Image

    out = []
    for i, shot in enumerate(shots):
        rnd = random.Random(shot)
        im = Image.frombytes("L", (9, 8), bytes(rnd.randrange(256) for _ in range(72)))
        p = tmp / f"f_{i + 1}.png"
        im.resize((90, 80), Image.NEAREST).save(p)
        out.append(p)
    return out


def test_cut_speed(tmp_path):
    from app.team import benchmark

    assert benchmark.cut_speed(_frames(tmp_path, [0, 1, 0, 1, 0, 1])) == "fast"
    assert benchmark.cut_speed(_frames(tmp_path, [0, 0, 0, 1, 1, 1])) == "mid"
    assert benchmark.cut_speed(_frames(tmp_path, [0, 0, 0, 0, 0, 0])) == "slow"
    assert benchmark.cut_speed([]) == ""


def test_parse_post_keeps_play_and_cover_url():
    from app.team import benchmark

    a = {"aweme_id": "1", "desc": "t", "create_time": 1, "statistics": {},
         "video": {"duration": 60000, "play_addr": {"url_list": ["https://v/1.mp4", "b"]},
                   "cover": {"url_list": ["https://c/1.jpg"]}}}
    row = benchmark.parse_post(a, "acc", [])
    assert row["video_url"] == "https://v/1.mp4" and row["cover_url"] == "https://c/1.jpg"
    assert benchmark.parse_post({"aweme_id": "2"}, "acc", [])["video_url"] == ""


def test_tag_posts_picks_fresh_hits_and_records_failures(fresh_db, tmp_path):
    from app.team import benchmark

    fresh_db.add_benchmark_account(ACC)
    for i in range(7):
        _post(fresh_db, f"h{i}", 2.0 + i, video="https://v/h.mp4")
    _post(fresh_db, "c1", 0.8, video="https://v/c.mp4")
    _post(fresh_db, "mid", 1.5, video="https://v/m.mp4")  # 既不是爆款也不是对照
    _post(fresh_db, "nourl", 9.0)
    picked = [p["id"] for p in benchmark.tag_candidates()]
    assert picked == ["h6", "h5", "h4", "h3", "h2", "c1"]

    calls = []

    def grab(video, cover, workdir):
        if video.endswith("c.mp4"):
            raise RuntimeError("403")
        return _frames(workdir, [0, 1, 0, 1, 0, 1]), None

    def ask(paths):
        calls.append(len(paths))
        return {"opening": "TEXT", "claim": "true", "cover": "poster"}

    assert benchmark.tag_posts(grab=grab, ask=ask) == 5
    assert calls == [3] * 5
    tags = fresh_db.list_benchmark_tags()
    assert tags["h6"]["tags"] == {"opening": "text", "cover": "", "claim": True, "cuts": "fast"}
    assert tags["c1"]["error"] == "403" and tags["c1"]["tags"] == {}
    # 拆过(包括失败的)不再拆;剩下的爆款下一轮接着拆
    assert [p["id"] for p in benchmark.tag_candidates()] == ["h1", "h0"]


def test_structure_compare_and_prior_features(fresh_db):
    from app.team import benchmark, stats

    fresh_db.add_benchmark_account(ACC)
    for i in range(4):
        _post(fresh_db, f"h{i}", 5.0, title="零百3.2秒")
        fresh_db.save_benchmark_tag(f"h{i}", {"opening": "text" if i else "car", "claim": i < 3,
                                              "cover": "text", "cuts": "fast"})
    _post(fresh_db, "plain", 1.0)
    own = [_row("o1", 100, hook_visual="broll", pace="slow", title_kind="stake", duration="≤90s")]
    st = benchmark.structure_compare(own)
    assert st["n_theirs"] == 4 and st["n_ours"] == 1 and st["claim_share"] == 0.75
    dims = {d["dimension"]: d for d in st["dims"]}
    assert dims["opening"]["theirs"][0] == {"value": "text", "label": "大字", "share": 0.75}
    assert dims["opening"]["ours"] == [{"value": "car", "label": "实拍车", "share": 1.0}]
    assert dims["pace"]["theirs"][0]["share"] == 1.0 and dims["cover"]["ours"] == []
    assert benchmark.structure_compare([], time.time() + 200 * 86400)["dims"] == []

    rows = {r["id"]: r["features"] for r in benchmark.prior_rows()}
    assert rows["h1"]["hook_visual"] == "hook" and rows["h1"]["pace"] == "fast"
    assert rows["plain"]["hook_visual"] == "" and rows["plain"]["title_kind"] == "stake"
    ids = {r["id"] for r in stats.learn_benchmark(benchmark.prior_rows(), "w")}
    assert "bench:hook_visual:hook:views" in ids or "bench:title_kind:number:views" in ids
