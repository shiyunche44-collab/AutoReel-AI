"""真实视频片段测试(clips + clip_resolver + 成本模型)。

这一层能不能成立,取决于三个前提,这里分别钉住:
1. 车媒页面里**真的**能抠出可下载的直链(否则 V2 是空想);
2. 片段落盘后**真的**能让生成层"不用再生成这个镜头";
3. 抓不到 / 质检失败时**整条流水线照常跑完**。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from app import config, costs
from app.categories import load_profile
from app.models import ShotSpec
from app.pipeline import clip_resolver as cr
from app.providers import clips as clips_mod


# ==========================================================================
# 直链提取
# ==========================================================================
def test_extracts_absolute_mp4():
    html = '<video src="https://cdn.site.com/a/b/car.mp4"></video>'
    assert clips_mod.DIRECT_LINK_RE.findall(html) == ["https://cdn.site.com/a/b/car.mp4"]


def test_extracts_m3u8():
    html = '<source src="https://cdn.site.com/live/car.m3u8">'
    assert any("car.m3u8" in u for u in clips_mod.DIRECT_LINK_RE.findall(html))


def test_extracts_protocol_relative():
    """很多车媒用 `//cdn/...`,只认绝对地址会漏掉一大半。"""
    assert any("cdn.site.com/a.mp4" in u
               for u in clips_mod.DIRECT_LINK_RE.findall('src="//cdn.site.com/a.mp4"'))


def test_extracts_escaped_json_url():
    """转义过的 JSON 串 `http:\\/\\/cdn\\/a.mp4` 同样要认。

    注意匹配出来仍带转义 —— 还原成可用地址是 `_normalise_link` 的职责,
    抽取与还原分成两步,是因为标签属性里的协议相对地址也要走同一条还原路。
    """
    found = clips_mod.DIRECT_LINK_RE.findall(r'"url":"http:\/\/cdn.site.com\/a.mp4"')
    assert found, "转义 URL 根本没被匹配到"
    # 只还原转义,不擅自把 http 升成 https —— 协议由源站决定
    assert clips_mod._normalise_link(found[0]) == "http://cdn.site.com/a.mp4"


def test_ignores_images_and_other_assets():
    html = '<img src="https://cdn.site.com/a.jpg"><a href="https://x.com/page.html">'
    assert clips_mod.DIRECT_LINK_RE.findall(html) == []


def test_tag_src_regex_catches_relative_attrs():
    assert clips_mod._TAG_SRC_RE.findall(
        '<video poster="x.jpg" src="//cdn.site.com/v.mp4"></video>'
    ) == ["//cdn.site.com/v.mp4"]


@pytest.mark.parametrize("raw,expected", [
    ("//cdn.x.com/a.mp4", "https://cdn.x.com/a.mp4"),
    (r"https:\/\/cdn.x.com\/a.mp4", "https://cdn.x.com/a.mp4"),
    ("http://cdn.x.com/a.mp4", "http://cdn.x.com/a.mp4"),
    ("data:video/mp4;base64,AAAA", ""),
    ("/local/a.mp4", ""),
    ("https://cdn.x.com/a.jpg", ""),
])
def test_link_normalisation(raw: str, expected: str):
    assert clips_mod._normalise_link(raw) == expected


@pytest.mark.parametrize("url", [
    "https://www.youtube.com/watch?v=x",
    "https://www.bilibili.com/video/BV1",
    "https://zhuanlan.zhihu.com/p/1",
    "https://www.tiktok.com/@a/video/1",
])
def test_unreachable_hosts_cost_nothing(url: str):
    """只有 iframe 的站点不���抓,省一次请求。"""
    assert clips_mod.page_video_urls(url) == []


def test_page_fetch_failure_returns_empty(monkeypatch):
    """抓不到是正常结果,不是异常 —— 整条链路不能因此断掉。"""
    import httpx

    def _boom(*a, **k):
        raise httpx.ConnectError("network down")

    monkeypatch.setattr(httpx, "get", _boom)
    assert clips_mod.page_video_urls("https://www.xchuxing.com/article/1") == []


# ==========================================================================
# 维基视频解析
# ==========================================================================
COMMONS_PAYLOAD = {
    "query": {
        "pages": {
            "1": {
                "ns": 6,
                "title": "File:BYD Han EV driving.webm",
                "videoinfo": [{
                    "url": "https://upload.wikimedia.org/x/BYD.webm",
                    "descriptionurl": "https://commons.wikimedia.org/wiki/File:BYD",
                    "duration": 42.5,
                    "derivatives": [
                        {"src": "https://upload.wikimedia.org/x/480p.vp9.webm", "height": 480},
                        {"src": "https://upload.wikimedia.org/x/720p.vp9.webm", "height": 720},
                        {"src": "https://upload.wikimedia.org/x/1080p.vp9.webm", "height": 1080},
                    ],
                    "extmetadata": {
                        "LicenseShortName": {"value": "<span>CC BY-SA 4.0</span>"},
                        "Artist": {"value": "<a>Someone</a>"},
                    },
                }],
            },
            "2": {
                "ns": 6,
                "title": "File:Chart of sales.png",  # 与车型无关,应被标题过滤掉
                "videoinfo": [{
                    "url": "https://upload.wikimedia.org/y/chart.mp4",
                    "derivatives": [],
                }],
            },
        }
    }
}


def test_commons_response_is_parsed():
    cands = clips_mod._commons_candidates(COMMONS_PAYLOAD, "BYD Han EV", 5)
    assert len(cands) == 1
    c = cands[0]
    assert c.origin == "clip_commons"
    assert c.duration == 42.5
    assert c.license == "CC BY-SA 4.0"
    assert c.author == "Someone"
    assert c.page_url.endswith("File:BYD")


def test_derivative_picks_720p_not_1080p():
    """原文件动辄几百 MB,轻量转码版本才是能进成片的。"""
    cands = clips_mod._commons_candidates(COMMONS_PAYLOAD, "BYD Han EV", 5)
    assert "720p" in cands[0].url


def test_derivative_falls_back_when_all_too_large():
    derivatives = [{"src": "a.mp4", "height": 2160}, {"src": "b.mp4", "height": 1440}]
    assert clips_mod._best_derivative(derivatives).endswith("b.mp4")


def test_derivative_prefers_mp4_at_equal_height():
    derivatives = [
        {"src": "a.webm", "height": 720},
        {"src": "b.mp4", "height": 720},
    ]
    assert clips_mod._best_derivative(derivatives).endswith("b.mp4")


def test_derivative_empty_list():
    assert clips_mod._best_derivative([]) == ""


# ==========================================================================
# 逐镜头取材梯子
# ==========================================================================
def _specs(count: int = 10) -> list[ShotSpec]:
    angles = ["exterior_45", "interior", "rear", "detail", "industry"]
    return [
        ShotSpec(idx=i, duration=6.0, section="核心卖点", visual_zh="x",
                 angle=angles[i % len(angles)])
        for i in range(count)
    ]


def _cand(url: str, page: str = "", **kw) -> clips_mod.ClipCandidate:
    return clips_mod.ClipCandidate(url=url, page_url=page, title=page,
                                   local_path="/tmp/x.mp4", **kw)


@pytest.fixture
def offline(monkeypatch):
    """把所有联网来源都换成空 —— 单测里绝不能真的去打 B 站 / Bing。
    返回一个 dict,用例往里塞某一级的候选即可。"""
    from app.providers import official, platform_video, stock, webassets

    box: dict = {}
    monkeypatch.setattr(platform_video, "search",
                        lambda q, **k: list(box.get("platform", [])))
    monkeypatch.setattr(official, "platform_official",
                        lambda *a, **k: list(box.get("official_platform", [])))
    monkeypatch.setattr(official, "gallery_images",
                        lambda *a, **k: list(box.get("gallery", [])))
    monkeypatch.setattr(official, "site_media", lambda *a, **k: ([], []))
    monkeypatch.setattr(webassets, "bing_images_cached",
                        lambda *a, **k: list(box.get("bing", [])))
    monkeypatch.setattr(stock, "search_videos", lambda *a, **k: list(box.get("stock_v", [])))
    monkeypatch.setattr(stock, "search_photos", lambda *a, **k: list(box.get("stock_p", [])))
    monkeypatch.setattr(cr.clips_mod, "commons_search_videos_cached",
                        lambda *a, **k: list(box.get("commons", [])))
    monkeypatch.setattr(cr.clips_mod, "page_media", lambda *a, **k: ([], []))
    monkeypatch.setattr(cr, "_qc", lambda clipped, *a, **k: clipped)
    # 素材池读的是 data/library 下真实建好的池子,单测里换成夹具里给的
    monkeypatch.setattr(cr.library, "candidates",
                        lambda *a, **k: list(box.get("library", [])))
    monkeypatch.setattr(cr.library, "has_usable_video",
                        lambda *a, **k: bool(box.get("library_ready")))
    monkeypatch.setattr(cr.library, "pick",
                        lambda subject, item_id, **k: box.get("pinned", {}).get(item_id))
    box["materialised"] = []

    def _fake_materialise(cand, dest, secs):
        if "bad" in cand.url:
            raise clips_mod.ClipError("boom")
        cand.local_path = str(dest)
        box["materialised"].append(cand.url)
        return cand

    monkeypatch.setattr(cr, "_materialise", _fake_materialise)
    return box


def _hit(url: str, *, title: str = "比亚迪汉 EV 实拍", uploader: str = "某UP主",
         duration: float = 120.0, platform: str = "bilibili", official: bool = False):
    from app.providers import platform_video

    return platform_video.PlatformHit(platform=platform, url=url, title=title,
                                      uploader=uploader, duration=duration, official=official)


def _collect(tmp_path, specs, **kw):
    return cr.collect(subject="比亚迪汉 EV", profile=load_profile("auto"),
                      specs=specs, project_dir=tmp_path, **kw)


def test_official_video_beats_platform_video(offline, tmp_path):
    """车辆镜头先找官方素材:官方号拍的一定是这台车。"""
    offline["official_platform"] = [_hit("https://b/official", uploader="比亚迪汽车",
                                          official=True)]
    offline["platform"] = [_hit("https://b/up")]
    assignment, report = _collect(tmp_path, _specs(1))
    assert assignment[0].url == "https://b/official"
    assert assignment[0].official
    assert report["selected"]["0"]["tier"] == "official_video"
    assert report["official_count"] == 1


def test_official_gallery_still_beats_platform_video(offline, tmp_path):
    from app.providers.images import ImageRef

    offline["gallery"] = [ImageRef(url="https://img/a.jpg", origin="official:autohome",
                                   angle="exterior_45")]
    offline["platform"] = [_hit("https://b/up")]
    assignment, report = _collect(tmp_path, _specs(1))
    assert assignment[0].kind == "still"
    assert report["selected"]["0"]["tier"] == "official_still"


def test_car_shot_falls_back_to_platform_then_web_still(offline, tmp_path):
    from app.providers.images import ImageRef

    offline["bing"] = [ImageRef(url="https://img/b.jpg", origin="bing")]
    assignment, report = _collect(tmp_path, _specs(1))
    assert assignment[0].kind == "still" and assignment[0].origin == "bing"
    assert report["selected"]["0"]["tier"] == "web_still"


def test_platform_titles_must_mention_subject_for_car_shots(offline, tmp_path):
    """车辆镜头不能配一条讲别的车的视频。"""
    offline["platform"] = [_hit("https://b/other", title="特斯拉 Model 3 试驾")]
    assignment, _ = _collect(tmp_path, _specs(1))
    assert assignment == {}


def test_other_shot_uses_platform_then_stock(offline, tmp_path):
    """非车画面:平台视频优先,图库兜底,不要求标题里有车型。"""
    specs = [ShotSpec(idx=0, duration=6.0, visual_zh="车展人流", angle="industry")]
    offline["stock_v"] = [clips_mod.ClipCandidate(url="https://pexels/v.mp4",
                                                  origin="stock:pexels")]
    assignment, report = _collect(tmp_path, specs)
    assert assignment[0].origin == "stock:pexels"
    assert report["selected"]["0"]["tier"] == "stock_video"

    offline["platform"] = [_hit("https://b/expo", title="上海车展现场")]
    assignment, _ = _collect(tmp_path / "2", specs)
    assert assignment[0].url == "https://b/expo"


def test_other_shot_never_uses_car_official_sources(offline, tmp_path):
    specs = [ShotSpec(idx=0, duration=6.0, visual_zh="车展", angle="industry")]
    offline["official_platform"] = [_hit("https://b/official", official=True)]
    assignment, _ = _collect(tmp_path, specs)
    assert assignment == {}


def test_long_video_is_split_across_adjacent_shots(offline, tmp_path):
    """同一条视频的不同起点切给相邻镜头 —— 同源连用就是免费的强一致性。"""
    offline["platform"] = [_hit("https://b/long", duration=300.0)]
    specs = [ShotSpec(idx=i, duration=6.0, visual_zh="x", angle="exterior_45")
             for i in range(3)]
    assignment, _ = _collect(tmp_path, specs)
    assert [c.url for c in assignment.values()] == ["https://b/long"] * 3
    starts = [c.start for c in assignment.values()]
    assert len(set(starts)) == 3, starts


def test_failed_candidate_falls_through_to_next(offline, tmp_path):
    offline["platform"] = [_hit("https://b/bad1"), _hit("https://b/ok")]
    assignment, report = _collect(tmp_path, _specs(1))
    assert assignment[0].url == "https://b/ok"
    assert any("素材落地失败" in e for e in report["errors"])


def test_limit_caps_real_shots(offline, tmp_path):
    offline["platform"] = [_hit(f"https://b/{i}", duration=300.0) for i in range(5)]
    specs = [ShotSpec(idx=i, duration=6.0, visual_zh="x", angle="exterior_45")
             for i in range(6)]
    assignment, report = _collect(tmp_path, specs, limit=2)
    assert len(assignment) == 2
    assert report["fallback"] == [2, 3, 4, 5]


def test_no_candidates_means_everything_generates(offline, tmp_path):
    assignment, report = _collect(tmp_path, _specs())
    assert assignment == {}
    assert report["fallback"] == list(range(10))
    assert report["enabled"] is False


def test_still_shots_can_be_switched_off(offline, tmp_path, monkeypatch):
    from app.providers.images import ImageRef

    monkeypatch.setattr(config, "USE_STILL_SHOTS", False)
    offline["bing"] = [ImageRef(url="https://img/b.jpg", origin="bing")]
    assignment, _ = _collect(tmp_path, _specs(1))
    assert assignment == {}


def test_qc_rejection_sends_shot_back_to_generation(offline, tmp_path, monkeypatch):
    offline["platform"] = [_hit("https://b/cropped")]
    monkeypatch.setattr(cr, "_qc", lambda clipped, *a, **k: [])
    assignment, report = _collect(tmp_path, _specs(1))
    assert assignment == {}
    # 同一条视频的每个起点各落地一次、各被弃用一次,都不过关才退回生成
    assert 1 <= report["qc_rejected"] <= cr.QC_REJECTS_PER_SHOT


def test_qc_rejection_tries_next_candidate(offline, tmp_path, monkeypatch):
    """落地即质检:海报被拦后接着试下一条,而不是整镜退回生成。"""
    offline["platform"] = [_hit("https://b/poster"), _hit("https://b/good")]
    monkeypatch.setattr(cr, "_qc", lambda clipped, *a, **k:
                        [c for c in clipped if "poster" not in c.url])
    assignment, report = _collect(tmp_path, _specs(1))
    assert assignment[0].url == "https://b/good"
    assert report["qc_rejected"] >= 1


def test_time_budget_stops_collection(offline, tmp_path, monkeypatch):
    offline["platform"] = [_hit("https://b/x", duration=300.0)]
    monkeypatch.setattr(config, "MEDIA_TIME_BUDGET", 1)
    ticks = iter([0.0, 0.0, 5.0, 5.0, 5.0])
    monkeypatch.setattr(cr.time, "monotonic", lambda: next(ticks, 5.0))
    specs = [ShotSpec(idx=i, duration=6.0, visual_zh="x", angle="exterior_45")
             for i in range(3)]
    assignment, report = _collect(tmp_path, specs)
    assert list(assignment) == [0]
    assert any("时长预算" in e for e in report["errors"])


# ==========================================================================
# apply_clips:零侵入现有调度的关键
# ==========================================================================
@pytest.fixture
def live_project(tmp_path, monkeypatch):
    from app import db

    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(config, "PROJECTS_DIR", tmp_path / "data" / "projects")
    monkeypatch.setattr(db, "_INITIALISED", False)
    db.init_db()

    pid = "p_clip_test"
    db.create_project({
        "id": pid, "title": "片段测试", "subject": "比亚迪汉 EV", "category": "auto",
        "theme": "overview", "duration": 60, "shot_count": 10,
        "narration_mode": "tts", "state": "GATE_B", "state_reason": None,
        "quota_budget": 100.0, "spent_cost": 0.0, "est_cost": 11.4,
        "created_at": 1.0, "updated_at": 2.0,
    })
    return pid


def _init_final(pid: str, count: int = 10):
    from app import db
    from app.pipeline import generate

    specs = [ShotSpec(idx=i, duration=6.0, section="核心卖点", visual_zh="x",
                      angle="exterior_45" if i < 3 else "interior")
             for i in range(count)]
    generate.initialise_shots(pid, specs, stage="final", ref_limit=5)
    return specs


def _clip_file(tmp_path: Path, name: str = "src.mp4") -> Path:
    p = tmp_path / "clips" / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"fake-mp4-bytes")
    return p


def test_apply_clips_marks_shot_succeeded(live_project, tmp_path):
    from app import db

    _init_final(live_project)
    cand = _cand("https://cdn/a.mp4", page="https://x/article/1")
    cand.local_path = str(_clip_file(tmp_path))
    applied = cr.apply_clips(live_project, {1: cand})

    assert applied == 1
    rows = {r["idx"]: r for r in db.get_shots(live_project, "final")}
    assert rows[1]["status"] == "succeeded"
    assert rows[1]["final_path"].endswith("001.mp4")
    assert Path(rows[1]["final_path"]).is_file()


def test_real_clip_costs_nothing(live_project, tmp_path):
    """真实片段的价值就在这一行:省掉成片档那一次生成。"""
    from app import db

    _init_final(live_project)
    cand = _cand("https://cdn/a.mp4")
    cand.local_path = str(_clip_file(tmp_path))
    cr.apply_clips(live_project, {0: cand})

    rows = {r["idx"]: r for r in db.get_shots(live_project, "final")}
    assert rows[0]["est_cost"] == 0.0
    assert rows[0]["consistency"] == "real"


def test_real_clip_is_not_resubmitted(live_project, tmp_path):
    """done 集合含它 → submit_ready 不会重复提交,这是"零侵入"的核心保证。"""
    from app import db
    from app.pipeline import generate

    _init_final(live_project)
    cand = _cand("https://cdn/a.mp4")
    cand.local_path = str(_clip_file(tmp_path))
    cr.apply_clips(live_project, {0: cand, 1: cand})

    rows = db.get_shots(live_project, "final")
    done = {r["idx"] for r in rows if r["status"] == "succeeded"}
    assert {0, 1} <= done

    chains = generate.chains_mod.chains_for([r["idx"] for r in rows])
    ready = generate.chains_mod.ready_shots(chains, done, done)
    assert 0 not in ready and 1 not in ready


def test_chain_layout_is_untouched(live_project, tmp_path):
    """片段留在原链位,只把这一格"顶掉" —— 链的排布不该因它而变。"""
    from app import db

    specs = _init_final(live_project)
    before = {r["idx"]: (r["chain_id"], r["chain_pos"]) for r in
              db.get_shots(live_project, "final")}
    cand = _cand("https://cdn/a.mp4")
    cand.local_path = str(_clip_file(tmp_path))
    cr.apply_clips(live_project, {0: cand})
    after = {r["idx"]: (r["chain_id"], r["chain_pos"]) for r in
             db.get_shots(live_project, "final")}
    assert before == after


def test_apply_is_idempotent(live_project, tmp_path):
    from app import db

    _init_final(live_project)
    cand = _cand("https://cdn/a.mp4")
    cand.local_path = str(_clip_file(tmp_path))
    cr.apply_clips(live_project, {0: cand})
    first = db.get_shots(live_project, "final")[0]
    cr.apply_clips(live_project, {0: cand})
    second = db.get_shots(live_project, "final")[0]
    assert first["final_path"] == second["final_path"]
    assert second["status"] == "succeeded"
    assert second["attempts"] == 0


def test_other_shots_untouched(live_project, tmp_path):
    from app import db

    _init_final(live_project)
    cand = _cand("https://cdn/a.mp4")
    cand.local_path = str(_clip_file(tmp_path))
    cr.apply_clips(live_project, {0: cand})
    rows = {r["idx"]: r for r in db.get_shots(live_project, "final")}
    assert rows[1]["status"] == "pending"
    assert rows[1]["final_path"] in (None, "")


def test_missing_local_file_is_skipped(live_project, tmp_path):
    from app import db

    _init_final(live_project)
    cand = _cand("https://cdn/a.mp4")
    cand.local_path = str(tmp_path / "does-not-exist.mp4")
    assert cr.apply_clips(live_project, {0: cand}) == 0
    rows = {r["idx"]: r for r in db.get_shots(live_project, "final")}
    assert rows[0]["status"] == "pending"


def test_stage_complete_sees_real_clips(live_project, tmp_path):
    """`stage_complete` 与 `missing_final_shots` 都会因此判过,合成不会卡住。"""
    from app.pipeline import generate, orchestrator

    specs = _init_final(live_project, count=3)
    cand = _cand("https://cdn/a.mp4")
    cand.local_path = str(_clip_file(tmp_path))
    cr.apply_clips(live_project, {0: cand, 1: cand, 2: cand})

    assert generate.stage_complete(live_project, "final")
    db_path = config.project_dir(live_project) / "storyboard.json"
    db_path.write_text(json.dumps([s.model_dump() for s in specs], ensure_ascii=False),
                       encoding="utf-8")
    assert orchestrator.missing_final_shots(live_project) == []


# ==========================================================================
# 降级安全
# ==========================================================================
def test_collect_with_no_sources_degrades_gracefully(offline, tmp_path):
    assignment, report = cr.collect(
        subject="比亚迪汉 EV", profile=load_profile("auto"),
        specs=_specs(), project_dir=tmp_path,
    )
    assert assignment == {}
    assert report["enabled"] is False
    assert "生成" in report["summary"]


def test_every_source_failing_does_not_raise(offline, tmp_path, monkeypatch):
    """全部来源抛异常时也必须安静退回生成,而不是把流水线带崩。"""
    from app.providers import official, platform_video, stock, webassets

    def _boom(*a, **k):
        raise RuntimeError("network down")

    for mod, name in ((platform_video, "search"), (official, "platform_official"),
                      (official, "gallery_images"), (official, "site_media"),
                      (webassets, "bing_images_cached"), (stock, "search_videos"),
                      (stock, "search_photos"),
                      (cr.clips_mod, "commons_search_videos_cached"),
                      (cr.clips_mod, "page_media")):
        monkeypatch.setattr(mod, name, _boom)
    assignment, report = cr.collect(
        subject="比亚迪汉 EV", profile=load_profile("auto"),
        specs=_specs(), project_dir=tmp_path, page_urls=["https://xchuxing.com/a"],
    )
    assert assignment == {}
    assert report["errors"]


def test_fetch_failure_keeps_going(offline, tmp_path, monkeypatch):
    """一条片段抓失败不影响其余候选 —— 素材层永远降级而不是阻断。"""
    monkeypatch.setattr(
        cr.clips_mod, "commons_search_videos_cached",
        lambda *a, **k: [
            clips_mod.ClipCandidate(url="https://cdn/bad1.mp4"),
            clips_mod.ClipCandidate(url="https://cdn/good.mp4"),
        ],
    )
    monkeypatch.setattr(cr, "_materialise", lambda cand, dest, secs: _apply_local(cand, tmp_path))
    monkeypatch.setattr(cr, "_qc", lambda *a, **k: a[0])

    specs = [ShotSpec(idx=i, duration=6.0, visual_zh="x", angle="exterior_45") for i in range(4)]
    assignment, report = cr.collect(
        subject="比亚迪汉 EV", profile=load_profile("auto"),
        specs=specs, project_dir=tmp_path,
    )
    assert len(assignment) == 1, "抓到的那条应该仍然可用"
    assert report["fetched"] == 1
    assert any("素材落地失败" in e for e in report["errors"])


def _apply_local(cand, tmp_path):
    if "bad" in cand.url:
        raise clips_mod.ClipError("boom")
    cand.local_path = str(_clip_file(tmp_path, cand.url.rsplit("/", 1)[-1]))
    return cand


@pytest.fixture
def qc_env(tmp_path, monkeypatch):
    """抽帧 / 上传打桩成功;上传地址带候选序号,便于按片段给回答。"""
    monkeypatch.setattr(cr, "_extract_frame", lambda clip, dest: _clip_file(tmp_path, Path(dest).name))
    monkeypatch.setattr(cr.images.cli, "upload_temp_url", lambda p: f"https://up/{Path(p).name}")
    box: dict = {"calls": [], "answers": {}}

    def _describe(urls, prompt, **k):
        box["calls"].append(list(urls))
        box.setdefault("prompts", []).append(prompt)
        answer = box["answers"].get(urls[0].rsplit("/", 1)[-1], "")
        if isinstance(answer, Exception):
            raise answer
        return answer

    monkeypatch.setattr(cr.images, "describe", _describe)
    return box


class _QcLLM:
    def __init__(self, result):
        self.result = result
        self.calls: list[dict] = []

    def structured(self, system, user, **kw):
        self.calls.append({"system": system, "user": user, **kw})
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def _cands(tmp_path, n):
    return [clips_mod.ClipCandidate(url=f"u{i}", title=f"片段{i}",
                                    local_path=str(_clip_file(tmp_path, f"c{i}.mp4")))
            for i in range(n)]


def test_qc_keeps_everything_when_answer_is_unparseable(tmp_path, qc_env):
    """质检链路抖动不能把真实素材通路整个废掉。"""
    cands = _cands(tmp_path, 1)
    qc_env["answers"]["f000.jpg"] = "我无法分析这些图片。"
    errors: list[str] = []
    assert cr._qc(cands, tmp_path, errors) == cands
    assert any("可用结论" in e for e in errors)


def test_qc_asks_each_frame_separately(tmp_path, qc_env):
    """一次多帧只会描述第一帧 —— 其余片段从没被质检过。"""
    cands = _cands(tmp_path, 12)
    cr._qc(cands, tmp_path, [])
    assert len(qc_env["calls"]) == 12
    assert all(len(c) == 1 for c in qc_env["calls"])


def test_qc_drops_cropped_frames(tmp_path, qc_env):
    """横屏素材裁 9:16 会把车切掉,这是混合方案最大的画面风险。"""
    cands = _cands(tmp_path, 1)
    qc_env["answers"]["f000.jpg"] = json.dumps({"subject_whole": False, "problem": "cropped"})
    assert cr._qc(cands, tmp_path, []) == []


def test_qc_only_warns_on_text_overlay(tmp_path, qc_env):
    """车媒视频几乎都带台标。按这条过滤会让整条通路空转,功能等于没做。"""
    cands = _cands(tmp_path, 1)
    qc_env["answers"]["f000.jpg"] = json.dumps(
        [{"index": 1, "subject_whole": True, "problem": "text_overlay"}])
    kept = cr._qc(cands, tmp_path, [])
    assert len(kept) == 1
    assert "text_overlay" in kept[0].license


def test_qc_prose_is_structured_by_llm(tmp_path, qc_env):
    cands = _cands(tmp_path, 3)
    qc_env["answers"] = {
        "f000.jpg": "车身完整,画面清晰。",
        "f001.jpg": "车头被画面左侧边缘切掉了一半。",
        "f002.jpg": "右下角有懂车帝台标。",
    }
    llm = _QcLLM({"verdicts": [
        {"index": 1, "subject_whole": True, "problem": ""},
        {"index": 2, "subject_whole": False, "problem": "cropped", "reason": "车头被切掉"},
        {"index": 3, "subject_whole": True, "problem": "text_overlay"},
    ]})
    errors: list[str] = []
    kept = cr._qc(cands, tmp_path, errors, llm=llm)

    assert [c.url for c in kept] == ["u0", "u2"]
    assert len(llm.calls) == 1, "散文要合在一次 LLM 调用里转"
    assert "车头被画面左侧边缘切掉" in llm.calls[0]["user"]
    assert any("车头被切掉" in e for e in errors)
    assert "text_overlay" in kept[1].license


def test_qc_llm_failure_keeps_everything(tmp_path, qc_env):
    cands = _cands(tmp_path, 2)
    qc_env["answers"] = {"f000.jpg": "车被切掉了。", "f001.jpg": "很模糊。"}
    errors: list[str] = []
    kept = cr._qc(cands, tmp_path, errors, llm=_QcLLM(RuntimeError("down")))
    assert kept == cands
    assert any("结构化失败" in e for e in errors)


def test_qc_frame_without_verdict_is_kept(tmp_path, qc_env):
    """一条片段没得出结论(调用报错)就丢掉,代价太大。"""
    cands = _cands(tmp_path, 2)
    qc_env["answers"] = {
        "f000.jpg": json.dumps({"subject_whole": False, "problem": "cropped"}),
        "f001.jpg": RuntimeError("timeout"),
    }
    errors: list[str] = []
    kept = cr._qc(cands, tmp_path, errors)
    assert [c.url for c in kept] == ["u1"]
    assert any("调用失败" in e for e in errors)


def test_qc_frame_extract_failure_keeps_clip(tmp_path, qc_env, monkeypatch):
    cands = _cands(tmp_path, 2)
    monkeypatch.setattr(
        cr, "_extract_frame",
        lambda clip, dest: None if clip.endswith("c0.mp4") else _clip_file(tmp_path, Path(dest).name))
    qc_env["answers"]["f001.jpg"] = json.dumps({"subject_whole": False, "problem": "cropped"})
    kept = cr._qc(cands, tmp_path, [])
    assert [c.url for c in kept] == ["u0"]
    assert len(qc_env["calls"]) == 1


def test_collect_passes_llm_to_qc(offline, tmp_path, monkeypatch):
    box: dict = {}

    def _qc(clipped, work_dir, errors, llm=None, subject="", intents=None, car_flags=None):
        box["llm"] = llm
        box["subject"] = subject
        box["intents"] = intents
        return clipped

    monkeypatch.setattr(cr, "_qc", _qc)
    offline["platform"] = [_hit("https://b/x")]
    sentinel = object()
    specs = [ShotSpec(idx=0, duration=6.0, visual_zh="x", angle="exterior_45")]
    cr.collect(subject="比亚迪汉 EV", profile=load_profile("auto"), specs=specs,
               project_dir=tmp_path, llm=sentinel)
    assert box["llm"] is sentinel
    assert box["subject"] == "比亚迪汉 EV"
    assert box["intents"] == ["x"], "镜头意图要交给质检判文不对题"


# ==========================================================================
# 编排接线:_inject_clips
#
# 这层是唯一有"每批门禁都会走到"属性的代码,它的三条约束各钉一个用例:
# 只跑一次 / 失败不阻断 / 只碰已批准的镜头。
# ==========================================================================
@pytest.fixture
def wired(live_project, tmp_path, monkeypatch):
    from app.pipeline import orchestrator

    specs = _init_final(live_project)
    box: dict = {"calls": 0, "assignment": {}}

    def _collect(**kw):
        box["calls"] += 1
        box["kwargs"] = kw
        return dict(box["assignment"]), {
            "enabled": True, "candidates": 1, "fetched": 1, "qc_rejected": 0,
            "selected": {}, "errors": [], "sources_tried": ["stub"],
            "savings_estimate": 0.48,
        }

    monkeypatch.setattr(cr, "collect", _collect)
    monkeypatch.setattr(orchestrator, "clip_resolver", cr)
    return orchestrator, specs, box


def _mk(path: Path) -> clips_mod.ClipCandidate:
    c = clips_mod.ClipCandidate(url="https://cdn/a.mp4", page_url="https://x/1")
    c.local_path = str(path)
    return c


def test_inject_runs_only_once_after_it_worked(wired, tmp_path):
    """已经生效过就不该重复抓取 —— 每批门禁都会走到这里。"""
    orchestrator, specs, box = wired
    cand = _mk(_clip_file(tmp_path))
    box["assignment"] = {0: cand}
    refs = []

    assert orchestrator._inject_clips("p_clip_test", "BYD Han", None, specs,
                                      {0, 1, 2}, refs) == 1
    assert orchestrator._inject_clips("p_clip_test", "BYD Han", None, specs,
                                      {3}, refs) == 0
    assert box["calls"] == 1


def test_inject_reuses_plan_for_later_batches(wired, tmp_path):
    """取材计划只抓一次:后面的批次把同一份计划应用到新批准的镜头上,
    不再重新打网络 —— 外观镜头落在第二、三批也照样能用上真实素材。"""
    orchestrator, specs, box = wired
    box["assignment"] = {0: _mk(_clip_file(tmp_path))}

    # 第一批:镜头 0 还没批准 → 不生效,但计划已经落盘
    assert orchestrator._inject_clips("p_clip_test", "BYD Han", None, specs, {1, 2}, []) == 0
    # 第二批:镜头 0 已批准 → 从计划里应用
    assert orchestrator._inject_clips("p_clip_test", "BYD Han", None, specs,
                                      {0, 1}, []) == 1
    assert box["calls"] == 1


def test_inject_never_refetches(wired, tmp_path):
    """反复没生效也不能每批都把网络重打一遍。"""
    orchestrator, specs, box = wired
    box["assignment"] = {0: _mk(_clip_file(tmp_path))}
    for _ in range(5):
        orchestrator._inject_clips("p_clip_test", "BYD Han", None, specs, {1, 2}, [])
    assert box["calls"] == 1


def test_inject_does_not_reapply_done_shots(wired, tmp_path):
    orchestrator, specs, box = wired
    box["assignment"] = {0: _mk(_clip_file(tmp_path))}
    assert orchestrator._inject_clips("p_clip_test", "BYD Han", None, specs, {0}, []) == 1
    assert orchestrator._inject_clips("p_clip_test", "BYD Han", None, specs, {0, 1}, []) == 0


def test_inject_hard_failure_is_never_retried(wired, tmp_path, monkeypatch):
    from app.pipeline import orchestrator as orch

    def _boom(**kw):
        raise RuntimeError("network down")

    monkeypatch.setattr(cr, "collect", _boom)
    specs = _init_final("p_clip_test")
    assert orch._inject_clips("p_clip_test", "BYD Han", None, specs, {0}, []) == 0
    assert orch._inject_clips("p_clip_test", "BYD Han", None, specs, {0}, []) == 0
    assert cr.load_report("p_clip_test")["status"] == "failed"


def test_inject_only_touches_approved_shots(wired, tmp_path):
    """还没审的镜头不该提前被定死画面。"""
    from app import db

    orchestrator, specs, box = wired
    box["assignment"] = {0: _mk(_clip_file(tmp_path)),
                         3: _mk(_clip_file(tmp_path, "b.mp4"))}
    applied = orchestrator._inject_clips("p_clip_test", "BYD Han", None, specs,
                                         {0, 1, 2}, [])
    assert applied == 1, "镜头 3 不在批准集合内,不该被注入"
    rows = {r["idx"]: r for r in db.get_shots("p_clip_test", "final")}
    assert rows[0]["status"] == "succeeded"
    assert rows[3]["status"] == "pending"


def test_inject_failure_never_blocks_the_pipeline(live_project, tmp_path, monkeypatch):
    """补充通路崩了也不能把整条流水线带走 —— 这就是它该有的地位。"""
    from app.pipeline import orchestrator

    specs = _init_final(live_project)

    def _boom(**kw):
        raise RuntimeError("network down")

    monkeypatch.setattr(cr, "collect", _boom)
    assert orchestrator._inject_clips(live_project, "BYD Han", None, specs,
                                      {0}, []) == 0
    report = cr.load_report(live_project)
    assert report["enabled"] is False
    assert report["errors"]


def test_inject_can_be_disabled(live_project, tmp_path, monkeypatch):
    """USE_REAL_CLIPS=0 时整条流水线行为与引入本特性之前完全一致。"""
    from app import config
    from app.pipeline import orchestrator

    monkeypatch.setattr(config, "USE_REAL_CLIPS", False)
    specs = _init_final(live_project)
    assert orchestrator._inject_clips(live_project, "BYD Han", None, specs,
                                      {0}, []) == 0
    assert cr.load_report(live_project) == {}


# ==========================================================================
# 成本模型
# ==========================================================================
def test_real_clip_shot_costs_zero():
    assert costs.estimate_shot(stage="final", duration=6.0, origin="clip") == 0.0


def test_generated_shot_still_costs():
    assert costs.estimate_shot(stage="final", duration=6.0) > 0


def test_savings_matches_hand_calculation():
    one = costs.estimate_video(config.FINAL_MODEL, config.FINAL_RESOLUTION, 6.0)
    assert costs.savings_from_clips(3) == pytest.approx(round(3 * one, 4))
    assert costs.savings_from_clips(0) == 0.0


def test_project_estimate_with_clip_ratio():
    base = costs.estimate_project(60)
    mixed = costs.estimate_project(60, real_clip_ratio=0.5)
    assert mixed["real_clip_shots"] == 5
    assert mixed["with_retry"] < base["with_retry"]
    assert mixed["savings"] == pytest.approx(
        round(base["final"] - mixed["final"], 4), abs=1e-3
    )
    # 草稿档、TTS、参考图不因真实素材而变
    assert mixed["draft"] == base["draft"]
    assert mixed["tts"] == base["tts"]


def test_clip_ratio_is_clamped():
    assert costs.estimate_project(60, real_clip_ratio=9.0)["real_clip_ratio"] == 1.0
    assert costs.estimate_project(60, real_clip_ratio=-1.0)["real_clip_ratio"] == 0.0


# ==========================================================================
# 新通路:嵌入播放器、静图镜头、草稿档注入、取材计划复用
# ==========================================================================
def test_extracts_embedded_players():
    html = (
        '<iframe src="//player.bilibili.com/player.html?aid=1&bvid=BV1xx411c7mD&page=1">'
        '<iframe src="https://www.youtube.com/embed/dQw4w9WgXcQ?rel=0">'
        '<a href="https://youtu.be/dQw4w9WgXcQ">'
    )
    assert clips_mod.extract_embeds(html) == [
        "https://www.bilibili.com/video/BV1xx411c7mD",
        "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
    ]


def test_article_embeds_become_platform_candidates(monkeypatch):
    """车媒页嵌的 B 站播放器过去被丢掉,现在交给 yt-dlp。"""
    monkeypatch.setattr(cr.clips_mod, "commons_search_videos_cached", lambda *a, **k: [])
    monkeypatch.setattr(cr.clips_mod, "page_media", lambda page, **k: (
        ["https://cdn/a.mp4"], ["https://www.bilibili.com/video/BV1xx411c7mD"]))
    pool, errors = cr._candidate_pool("比亚迪汉", page_urls=["https://news/1"])
    assert pool[0].url == "https://cdn/a.mp4" and not pool[0].platform
    assert pool[1].platform == "bilibili" and pool[1].origin == "clip_media:bilibili"
    assert errors == []


def test_fetch_clip_local_input_has_no_http_options(monkeypatch, tmp_path):
    """yt-dlp 落地的分段是本地文件,带 -user_agent 会被 ffmpeg 拒绝打开。"""
    seen: dict = {}

    def _run(args, **k):
        seen["args"] = args
        out = Path(args[-1])
        out.write_bytes(b"x")

        class P:
            returncode = 0
            stderr = ""
        return P()

    monkeypatch.setattr(clips_mod, "_ffmpeg", lambda: "ffmpeg")
    monkeypatch.setattr(clips_mod.subprocess, "run", _run)
    clips_mod.fetch_clip(str(tmp_path / "in.mp4"), tmp_path / "out.mp4", referer="https://r/")
    assert "-user_agent" not in seen["args"] and "-headers" not in seen["args"]
    clips_mod.fetch_clip("https://cdn/a.mp4", tmp_path / "out2.mp4", referer="https://r/")
    assert "-user_agent" in seen["args"] and "-headers" in seen["args"]


def test_still_filter_keeps_whole_subject_and_moves():
    from app.pipeline import render

    f = render.still_filter("zoom_in", 6)
    assert "force_original_aspect_ratio=decrease" in f, "前景必须完整放进画幅,不能裁车"
    assert "boxblur" in f and "zoompan" in f and "d=144" in f
    assert render.still_filter("pan_left", 6) != render.still_filter("pan_right", 6)


def test_materialise_still_goes_through_ken_burns(monkeypatch, tmp_path):
    from app.pipeline import render

    calls: dict = {}
    monkeypatch.setattr(cr.images, "download",
                        lambda url, dest, **k: calls.setdefault("headers", k.get("headers")))
    monkeypatch.setattr(render, "still_to_clip",
                        lambda img, dest, **k: calls.setdefault("motion", k["motion"]))
    cand = clips_mod.ClipCandidate(url="https://img/a.jpg", page_url="https://car.autohome.com.cn/pic/1",
                                   kind="still")
    out = cr._materialise(cand, tmp_path / "shot_002.mp4", 6.0)
    assert out.local_path.endswith("shot_002.mp4")
    assert calls["headers"]["Referer"] == "https://car.autohome.com.cn/"
    assert calls["motion"] == render.STILL_MOTIONS[2]


def test_materialise_platform_downloads_only_a_segment(monkeypatch, tmp_path):
    seen: dict = {}

    def _dl(url, dest, *, start, seconds):
        seen.update(url=url, start=start, seconds=seconds)
        p = Path(dest)
        p.write_bytes(b"x")
        return p

    monkeypatch.setattr(cr.platform_video, "download_segment", _dl)
    monkeypatch.setattr(cr.clips_mod, "probe_duration", lambda p: 7.0)
    monkeypatch.setattr(cr.clips_mod, "fetch_clip",
                        lambda src, dest, **k: seen.setdefault("loop", k["loop"]) and dest or dest)
    cand = clips_mod.ClipCandidate(url="https://www.bilibili.com/video/BV1", platform="bilibili",
                                   start=42.0)
    cr._materialise(cand, tmp_path / "shot_000.mp4", 6.0)
    assert seen["start"] == 42.0 and seen["seconds"] == 6.0
    assert seen["loop"] is False


def test_apply_clips_to_draft_stage(live_project, tmp_path):
    """门禁 A 之后真实镜头直接顶掉草稿生成,门禁 B 审到的就是真实素材。"""
    from app import db
    from app.pipeline import generate

    specs = [ShotSpec(idx=i, duration=6.0, visual_zh="x") for i in range(3)]
    generate.initialise_shots(live_project, specs, stage="draft")
    cand = _cand("https://cdn/a.mp4", kind="still")
    cand.local_path = str(_clip_file(tmp_path))
    assert cr.apply_clips(live_project, {1: cand}, stage="draft") == 1
    rows = {r["idx"]: r for r in db.get_shots(live_project, "draft")}
    assert rows[1]["status"] == "succeeded" and rows[1]["consistency"] == "real"
    assert "drafts" in rows[1]["draft_path"]
    assert rows[1]["final_path"] in (None, "")
    assert db.get_shots(live_project, "final") == []


def test_plan_round_trips(live_project, tmp_path):
    cand = _cand("https://www.bilibili.com/video/BV1", page="P", platform="bilibili",
                 start=12.5, official=True)
    cr.persist(live_project, {"status": "ok"}, {3: cand})
    plan = cr.load_plan(live_project)
    assert plan[3].start == 12.5 and plan[3].official and plan[3].platform == "bilibili"


def test_reinitialise_keeps_finished_shots(live_project, tmp_path):
    """门禁 B 每批都会重新 initialise_shots —— 不能把前几批做好的镜头打回 pending。"""
    from app import db
    from app.pipeline import generate

    specs = _init_final(live_project, count=3)
    cand = _cand("https://cdn/a.mp4")
    cand.local_path = str(_clip_file(tmp_path))
    cr.apply_clips(live_project, {0: cand})
    db.update_shot(live_project, 1, "final", status="failed", error="boom")
    generate.initialise_shots(live_project, specs, stage="final", ref_limit=5)
    rows = {r["idx"]: r for r in db.get_shots(live_project, "final")}
    assert rows[0]["status"] == "succeeded" and rows[0]["consistency"] == "real"
    assert rows[1]["status"] == "pending", "失败的镜头照旧重置,隐含再试一次"


def test_collect_media_applies_to_drafts(live_project, tmp_path, monkeypatch):
    from app import db
    from app.pipeline import generate, orchestrator

    specs = [ShotSpec(idx=i, duration=6.0, visual_zh="x", angle="exterior_45")
             for i in range(3)]
    generate.initialise_shots(live_project, specs, stage="draft")
    cand = _cand("https://b/1", page="https://b/1", platform="bilibili")
    cand.local_path = str(_clip_file(tmp_path))
    seen: dict = {}

    def _collect(**kw):
        seen.update(kw)
        return {0: cand}, {"errors": [], "summary": "1/3"}

    monkeypatch.setattr(cr, "collect", _collect)
    refs = [{"url": "https://img/a.jpg", "origin": "search", "source_page": "https://news/1"},
            {"url": "https://ai/x.png", "origin": "generate"}]
    assert orchestrator._collect_media(live_project, "比亚迪汉", load_profile("auto"),
                                       specs, refs, llm=None) == 1
    assert [r.url for r in seen["ref_images"]] == ["https://img/a.jpg"], "AI 兜底图不能当真实图片"
    assert seen["page_urls"] == ["https://news/1"]

    # 门禁 A 实际传进来的是 ImageRef 对象,不是 dict
    from app.providers.images import ImageRef

    seen.clear()
    obj_refs = [ImageRef(url="https://img/b.jpg", origin="commons", source_page="https://w/1"),
                ImageRef(url="https://ai/y.png", origin="generate")]
    orchestrator._collect_media(live_project, "比亚迪汉", load_profile("auto"),
                                specs, obj_refs, llm=None)
    assert [r.url for r in seen["ref_images"]] == ["https://img/b.jpg"]
    rows = {r["idx"]: r for r in db.get_shots(live_project, "draft")}
    assert rows[0]["status"] == "succeeded"
    assert cr.load_report(live_project)["applied_draft"] == 1
    assert 0 in cr.load_plan(live_project)


def test_collect_media_respects_master_switch(live_project, monkeypatch):
    from app.pipeline import orchestrator

    monkeypatch.setattr(config, "USE_REAL_CLIPS", False)
    monkeypatch.setattr(cr, "collect", lambda **kw: (_ for _ in ()).throw(AssertionError()))
    assert orchestrator._collect_media(live_project, "x", load_profile("auto"), [], []) == 0


def test_collect_media_failure_does_not_raise(live_project, monkeypatch):
    from app.pipeline import orchestrator

    def _boom(**kw):
        raise RuntimeError("network down")

    monkeypatch.setattr(cr, "collect", _boom)
    assert orchestrator._collect_media(live_project, "x", load_profile("auto"),
                                       _specs(2), []) == 0
    assert cr.load_report(live_project)["status"] == "failed"


def test_extract_frame_creates_missing_directory(tmp_path, monkeypatch):
    """ffmpeg 不建父目录。漏了这一步,每帧都抽失败,质检一路放行、从没跑过。"""
    seen: dict = {}

    class _Proc:
        returncode = 0

    def _run(cmd, **kw):
        seen["parent_exists"] = Path(cmd[-1]).parent.is_dir()
        Path(cmd[-1]).write_bytes(b"jpg")
        return _Proc()

    monkeypatch.setattr(cr.clips_mod, "probe_duration", lambda p: 6.0)
    monkeypatch.setattr(cr.subprocess, "run", _run)
    dest = tmp_path / "work" / "qc_frames" / "f000.jpg"
    assert cr._extract_frame("clip.mp4", dest) == dest
    assert seen["parent_exists"] is True


def test_qc_drops_wrong_model_and_frames_without_a_car(tmp_path, qc_env):
    """平台视频常是同品牌别的车、竞品,甚至根本没车 —— 这两类必须拦。"""
    cands = _cands(tmp_path, 4)
    qc_env["answers"] = {
        "f000.jpg": "一台比亚迪汉 EV,车尾有 HAN 字样,车身完整。",
        "f001.jpg": "一台白色比亚迪海豹 07 DM-i。",
        "f002.jpg": "一个人站在人行道上,背景是灌木和电动车,画面里没有汽车。",
        "f003.jpg": "一台白色轿车,看不出车标。",
    }
    llm = _QcLLM({"verdicts": [
        {"index": 1, "is_target_model": True, "subject_whole": True, "problem": ""},
        {"index": 2, "is_target_model": False, "subject_whole": True,
         "problem": "not_target_model", "reason": "海豹 07"},
        {"index": 3, "is_target_model": "unknown", "subject_whole": True,
         "problem": "no_subject", "reason": "没有汽车"},
        {"index": 4, "is_target_model": "unknown", "subject_whole": True, "problem": ""},
    ]})
    errors: list[str] = []
    kept = cr._qc(cands, tmp_path, errors, llm=llm, subject="比亚迪汉 EV")

    assert [c.url for c in kept] == ["u0", "u3"], "认不出车型的不能误杀"
    assert "比亚迪汉 EV" in llm.calls[0]["system"]
    assert "比亚迪汉 EV" in qc_env["prompts"][0]
    assert any("not_target_model" in e and "海豹 07" in e for e in errors)
    assert any("no_subject" in e for e in errors)


def test_qc_false_target_model_without_problem_is_rejected(tmp_path, qc_env):
    cands = _cands(tmp_path, 1)
    qc_env["answers"]["f000.jpg"] = json.dumps({"is_target_model": False, "problem": ""})
    assert cr._qc(cands, tmp_path, [], subject="比亚迪汉 EV") == []


def test_qc_without_subject_ignores_model_verdict(tmp_path, qc_env):
    """不知道目标车型时,"不是目标车型"无从谈起,不能据此弃用。"""
    cands = _cands(tmp_path, 1)
    qc_env["answers"]["f000.jpg"] = json.dumps(
        {"is_target_model": False, "problem": "not_target_model"})
    assert cr._qc(cands, tmp_path, []) == cands


def test_qc_bare_json_answer_is_asked_again_for_prose(tmp_path, qc_env):
    """只回 JSON、没有描述的帧以前直接"没有结论"放行 —— 海报、网页截图就这样混进成片。"""
    cands = _cands(tmp_path, 1)
    answers = iter([json.dumps({"is_target_model": True, "problem": "text_overlay"}),
                    "这是一张宣传海报,大标题写着 YU7,旁边站着代言人。"])
    qc_env["answers"] = type("A", (dict,), {"get": lambda self, k, d=None: next(answers)})()
    llm = _QcLLM({"verdicts": [{"index": 1, "problem": "poster", "reason": "宣传海报"}]})
    kept = cr._qc(cands, tmp_path, [], llm=llm, subject="小米 YU7")
    assert kept == []
    assert len(qc_env["calls"]) == 2
    assert "不要输出 JSON" in qc_env["prompts"][1]
    assert "宣传海报" in llm.calls[0]["user"]


def test_qc_blocks_poster_and_off_topic(tmp_path, qc_env):
    cands = _cands(tmp_path, 3)
    qc_env["answers"] = {"f000.jpg": "海报", "f001.jpg": "只有一台车", "f002.jpg": "一台小米 YU7"}
    llm = _QcLLM({"verdicts": [
        {"index": 1, "problem": "poster"},
        {"index": 2, "problem": "off_topic", "reason": "要两车同框却只有一台"},
        {"index": 3, "is_target_model": True, "problem": ""},
    ]})
    kept = cr._qc(cands, tmp_path, [], llm=llm, subject="小米 YU7",
                  intents=["内饰", "小米YU7与特斯拉Model Y并排", "外观"])
    assert [c.url for c in kept] == ["u2"]
    assert "小米YU7与特斯拉Model Y并排" in qc_env["prompts"][1]
    assert "【镜头意图】小米YU7与特斯拉Model Y并排" in llm.calls[0]["user"]


def test_qc_non_car_shot_only_checks_other_problems(tmp_path, qc_env):
    """非车辆镜头不判车型 / 被裁,但文不对题(飞机舷窗)要拦。"""
    cands = _cands(tmp_path, 2)
    qc_env["answers"] = {"f000.jpg": "飞机舷窗外的云层", "f001.jpg": "城市街景"}
    llm = _QcLLM({"verdicts": [
        {"index": 1, "problem": "off_topic"},
        {"index": 2, "is_target_model": False, "subject_whole": False, "problem": "no_subject"},
    ]})
    kept = cr._qc(cands, tmp_path, [], llm=llm, subject="小米 YU7",
                  intents=["女性乘客坐在车内", "城市街景"], car_flags=[False, False])
    assert [c.url for c in kept] == ["u1"]
    assert "【非车辆镜头】" in llm.calls[0]["user"]


def test_qc_reject_budget_is_per_tier(offline, tmp_path, monkeypatch):
    """官网图一级被拦光,也要轮到下一级的平台视频。"""
    from app.providers.images import ImageRef

    offline["gallery"] = [ImageRef(url=f"https://img/poster{i}.jpg", origin="official:autohome",
                                   angle="exterior_45")
                          for i in range(cr.QC_REJECTS_PER_SHOT + 2)]
    offline["platform"] = [_hit("https://b/good")]
    monkeypatch.setattr(cr, "_qc", lambda clipped, *a, **k:
                        [c for c in clipped if "poster" not in c.url])
    assignment, _ = _collect(tmp_path, _specs(1))
    assert assignment[0].url == "https://b/good"


# --------------------------------------------------------------------------
# 素材池
# --------------------------------------------------------------------------
def _lib(path: str, start: float, kind: str = "video") -> clips_mod.ClipCandidate:
    return clips_mod.ClipCandidate(url=path, origin="library:bilibili", kind=kind,
                                   start=start, view="exterior_45", official=True)


def test_library_tier_comes_first(offline, tmp_path):
    offline["library"] = [_lib("F:/lib/a.mp4", 0.1)]
    offline["official_platform"] = [_hit("https://b/official", uploader="比亚迪汽车",
                                          official=True)]
    assignment, report = _collect(tmp_path, _specs(1))
    assert assignment[0].url == "F:/lib/a.mp4"
    assert report["selected"]["0"]["tier"] == "library"


def test_library_windows_do_not_overlap_across_shots(offline, tmp_path, monkeypatch):
    pool = [_lib("F:/lib/a.mp4", 0.1), _lib("F:/lib/a.mp4", 2.0), _lib("F:/lib/a.mp4", 8.1)]
    seen_exclude: list = []

    def _cands(subject, angle, *, seconds=6, exclude=()):
        seen_exclude.append(list(exclude))
        return [c for c in pool
                if not any(p == c.url and c.start < e and s < c.start + seconds
                           for p, s, e in exclude)]

    monkeypatch.setattr(cr.library, "candidates", _cands)
    specs = [ShotSpec(idx=i, duration=6.0, section="核心卖点", visual_zh="x",
                      angle="exterior_45") for i in range(2)]
    assignment, _ = _collect(tmp_path, specs)
    assert [assignment[i].start for i in (0, 1)] == [0.1, 8.1]
    assert ("F:/lib/a.mp4", 0.1, 6.1) in seen_exclude[-1]


def test_library_candidates_with_same_file_are_distinct_keys():
    assert cr._key(_lib("F:/lib/a.mp4", 0.1)) != cr._key(_lib("F:/lib/a.mp4", 8.1))


def test_materialise_library_uses_local_file(tmp_path, monkeypatch):
    calls: dict = {}
    monkeypatch.setattr(cr.clips_mod, "fetch_clip",
                        lambda url, dest, **k: calls.update(video=(url, k)) or dest)
    monkeypatch.setattr(cr.render, "still_to_clip",
                        lambda img, dest, **k: calls.update(still=img) or dest)
    monkeypatch.setattr(cr.platform_video, "download_segment",
                        lambda *a, **k: pytest.fail("素材池不该再下载"))
    got = cr._materialise(_lib("F:/lib/a.mp4", 8.1), tmp_path / "shot_000.mp4", 6.0)
    assert calls["video"] == ("F:/lib/a.mp4", {"seconds": 6.0, "start": 8.1})
    assert got.local_path.endswith("shot_000.mp4")
    cr._materialise(_lib("F:/lib/i.jpg", 0, kind="still"), tmp_path / "shot_001.mp4", 6.0)
    assert calls["still"] == "F:/lib/i.jpg"


def test_library_hits_skip_vision_qc_and_follow_intent_pick(offline, tmp_path, monkeypatch):
    pool = [_lib("F:/lib/a.mp4", 0.1), _lib("F:/lib/b.mp4", 0.1)]
    offline["library"] = pool
    monkeypatch.setattr(cr, "_qc", lambda *a, **k: pytest.fail("素材池不该再逐镜头视觉质检"))
    seen: dict = {}

    def _match(cands, intent, llm, errors):
        seen["intent"] = intent
        return [cands[1]]

    monkeypatch.setattr(cr.library, "match_intent", _match)
    specs = [ShotSpec(idx=0, duration=6.0, section="核心卖点", visual_zh="夕阳下的 YU7",
                      angle="exterior_45")]
    assignment, _ = _collect(tmp_path, specs, llm=object())
    assert assignment[0].url == "F:/lib/b.mp4"
    assert seen["intent"] == "夕阳下的 YU7"


def test_library_intent_pick_empty_falls_through(offline, tmp_path, monkeypatch):
    offline["library"] = [_lib("F:/lib/a.mp4", 0.1)]
    offline["platform"] = [_hit("https://b/good")]
    monkeypatch.setattr(cr.library, "match_intent", lambda *a, **k: [])
    assignment, report = _collect(tmp_path, _specs(1), llm=object())
    assert assignment[0].url == "https://b/good"
    assert report["selected"]["0"]["tier"] == "platform_video"


def test_car_shots_skip_platform_search_once_library_is_built(offline, tmp_path, monkeypatch):
    """池子判了"没有合适的",不能再去现搜现切 —— 捡回来的是猎奇视频。"""
    offline["library_ready"] = True
    offline["platform"] = [_hit("https://b/junk")]
    monkeypatch.setattr(cr.library, "match_intent", lambda *a, **k: [])
    offline["library"] = [_lib("F:/lib/a.mp4", 0.1)]
    specs = [ShotSpec(idx=0, duration=6.0, section="核心卖点", visual_zh="x", angle="exterior_45"),
             ShotSpec(idx=1, duration=6.0, section="核心卖点", visual_zh="x", angle="industry")]
    assignment, _ = _collect(tmp_path, specs, llm=object())
    assert 0 not in assignment                       # 车辆镜头:退回生成
    assert assignment[1].url == "https://b/junk"     # 非车镜头不受影响


def test_storyboard_pool_pick_is_used_first_without_intent_match(offline, tmp_path, monkeypatch):
    """分镜点名了素材:直接用它,不再按意图重挑。"""
    offline["library"] = [_lib("F:/lib/a.mp4", 0.1)]
    offline["pinned"] = {"seg_9": _lib("F:/lib/b.mp4", 3.0)}
    monkeypatch.setattr(cr.library, "match_intent",
                        lambda *a, **k: pytest.fail("点名素材可用时不该再按意图挑"))
    specs = [ShotSpec(idx=0, duration=6.0, section="核心卖点", visual_zh="x",
                      angle="exterior_45", pool_id="seg_9")]
    assignment, report = _collect(tmp_path, specs, llm=object())
    assert (assignment[0].url, assignment[0].start) == ("F:/lib/b.mp4", 3.0)
    assert report["selected"]["0"]["tier"] == "library"


def test_storyboard_pool_pick_gone_falls_back_to_intent_match(offline, tmp_path, monkeypatch):
    offline["library"] = [_lib("F:/lib/a.mp4", 0.1)]
    monkeypatch.setattr(cr.library, "match_intent", lambda cands, *a, **k: cands)
    specs = [ShotSpec(idx=0, duration=6.0, section="核心卖点", visual_zh="x",
                      angle="exterior_45", pool_id="gone")]
    assignment, _ = _collect(tmp_path, specs, llm=object())
    assert assignment[0].url == "F:/lib/a.mp4"


def test_storyboard_pool_pick_wins_even_for_non_car_query(offline, tmp_path):
    """检索词分类把镜头判成"非车辆"也不影响:分镜点名的就是池子里这台车的素材。"""
    offline["pinned"] = {"seg_1": _lib("F:/lib/b.mp4", 0.0)}
    specs = [ShotSpec(idx=0, duration=6.0, section="核心卖点", visual_zh="x",
                      angle="industry", pool_id="seg_1")]
    assignment, _ = _collect(tmp_path, specs)
    assert assignment[0].url == "F:/lib/b.mp4"

