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
# 选片规则
# ==========================================================================
def _specs(count: int = 10) -> list[ShotSpec]:
    angles = ["exterior_45", "interior", "rear", "detail", "industry"]
    return [
        ShotSpec(idx=i, duration=6.0, section="核心卖点", visual_zh="x",
                 angle=angles[i % len(angles)])
        for i in range(count)
    ]


def _cand(url: str, page: str = "") -> clips_mod.ClipCandidate:
    return clips_mod.ClipCandidate(url=url, page_url=page, title=page, local_path="/tmp/x.mp4")


def test_only_exterior_angles_get_real_clips():
    """外观镜头才值得动用真实素材 —— 氛围镜头画错了也看不出来。"""
    # 角度按 exterior_45 / interior / rear / detail / industry 轮转,
    # 所以符合条件的镜头序号是 0(外观)、2(尾部)、3(细节)
    assignment = cr._assign(_specs(), [_cand("a"), _cand("b")])
    assert set(assignment) == {0, 2}


def test_assignment_is_ordered_and_capped_by_candidates():
    specs = [ShotSpec(idx=i, duration=6.0, visual_zh="x", angle="exterior_45")
             for i in range(10)]
    assignment = cr._assign(specs, [_cand("a"), _cand("b")])
    assert list(assignment) == [0, 1]


def test_same_source_clips_stay_together():
    """同一篇文章里的片段拍的是同一台车,连着用就是免费的强一致性。"""
    specs = [ShotSpec(idx=i, duration=6.0, visual_zh="x", angle="exterior_45")
             for i in range(4)]
    pool = [
        _cand("p1a", page="P1"),
        _cand("p2a", page="P2"),
        _cand("p1b", page="P1"),
    ]
    assignment = cr._assign(specs, pool)
    pages = [c.page_url for c in assignment.values()]
    assert pages[0] == "P1" and pages[1] == "P1", pages


def test_no_candidates_means_no_assignment():
    assert cr._assign(_specs(), []) == {}


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
def test_collect_with_no_sources_degrades_gracefully(tmp_path, monkeypatch):
    monkeypatch.setattr(cr.clips_mod, "commons_search_videos_cached", lambda *a, **k: [])
    monkeypatch.setattr(cr.clips_mod, "page_video_urls", lambda *a, **k: [])
    assignment, report = cr.collect(
        subject="比亚迪汉 EV", profile=load_profile("auto"),
        specs=_specs(), project_dir=tmp_path,
    )
    assert assignment == {}
    assert report["enabled"] is False
    assert "生成" in report["summary"]


def test_every_source_failing_does_not_raise(tmp_path, monkeypatch):
    """全部来源抛异常时也必须安静退回生成,而不是把流水线带崩。"""
    def _boom(*a, **k):
        raise RuntimeError("network down")

    monkeypatch.setattr(cr.clips_mod, "commons_search_videos_cached", _boom)
    monkeypatch.setattr(cr.clips_mod, "page_video_urls", _boom)
    assignment, report = cr.collect(
        subject="比亚迪汉 EV", profile=load_profile("auto"),
        specs=_specs(), project_dir=tmp_path, page_urls=["https://xchuxing.com/a"],
    )
    assert assignment == {}
    assert report["errors"]


def test_fetch_failure_keeps_going(tmp_path, monkeypatch):
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
    assert any("片段抓取失败" in e for e in report["errors"])


def _apply_local(cand, tmp_path):
    if "bad" in cand.url:
        raise clips_mod.ClipError("boom")
    cand.local_path = str(_clip_file(tmp_path, cand.url.rsplit("/", 1)[-1]))
    return cand


def test_qc_keeps_everything_when_answer_is_unparseable(tmp_path, monkeypatch):
    """质检链路抖动不能把真实素材通路整个废掉。"""
    from app.pipeline import asset_resolver

    cand = clips_mod.ClipCandidate(url="u", local_path=str(_clip_file(tmp_path)))
    monkeypatch.setattr(cr, "_extract_frame", lambda *a, **k: _clip_file(tmp_path))
    monkeypatch.setattr(cr.images.cli, "upload_temp_url", lambda p: "https://up/1.jpg")
    monkeypatch.setattr(cr.images, "describe", lambda urls, prompt, **k: "我无法分析这些图片。")
    monkeypatch.setattr(asset_resolver, "parse_verdicts", lambda text: [])

    errors: list[str] = []
    assert cr._qc([cand], tmp_path, errors) == [cand]
    assert any("无法解析" in e for e in errors)


def test_qc_drops_cropped_frames(tmp_path, monkeypatch):
    """横屏素材裁 9:16 会把车切掉,这是混合方案最大的画面风险。"""
    cand = clips_mod.ClipCandidate(url="u", local_path=str(_clip_file(tmp_path)))
    monkeypatch.setattr(cr, "_extract_frame", lambda *a, **k: _clip_file(tmp_path))
    monkeypatch.setattr(cr.images.cli, "upload_temp_url", lambda p: "https://up/1.jpg")
    monkeypatch.setattr(
        cr.images, "describe",
        lambda urls, prompt, **k: json.dumps(
            [{"index": 1, "subject_whole": False, "problem": "cropped"}]
        ),
    )
    assert cr._qc([cand], tmp_path, []) == []


def test_qc_only_warns_on_text_overlay(tmp_path, monkeypatch):
    """车媒视频几乎都带台标。按这条过滤会让整条通路空转,功能等于没做。"""
    cand = clips_mod.ClipCandidate(url="u", local_path=str(_clip_file(tmp_path)))
    monkeypatch.setattr(cr, "_extract_frame", lambda *a, **k: _clip_file(tmp_path))
    monkeypatch.setattr(cr.images.cli, "upload_temp_url", lambda p: "https://up/1.jpg")
    monkeypatch.setattr(
        cr.images, "describe",
        lambda urls, prompt, **k: json.dumps(
            [{"index": 1, "subject_whole": True, "problem": "text_overlay"}]
        ),
    )
    kept = cr._qc([cand], tmp_path, [])
    assert len(kept) == 1
    assert "text_overlay" in kept[0].license


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


def test_inject_retries_when_previous_run_applied_nothing(wired, tmp_path):
    """外观镜头常落在第二、三批:第一次跑时没有已批准的外观镜头可用,
    死守"只跑一次"会让后面所有批次永远拿不到真实素材。"""
    orchestrator, specs, box = wired
    box["assignment"] = {0: _mk(_clip_file(tmp_path))}

    # 第一批:镜头 0 是 interior,不在分配范围里 → 没生效
    assert orchestrator._inject_clips("p_clip_test", "BYD Han", None, specs, {1, 2}, []) == 0
    # 第二批:镜头 0 已批准 → 这次生效
    assert orchestrator._inject_clips("p_clip_test", "BYD Han", None, specs,
                                      {0, 1}, []) == 1
    assert box["calls"] == 2


def test_inject_retries_are_bounded(wired, tmp_path):
    """反复没生效也不能每批都把网络重打一遍。"""
    orchestrator, specs, box = wired
    box["assignment"] = {0: _mk(_clip_file(tmp_path))}
    for _ in range(5):
        orchestrator._inject_clips("p_clip_test", "BYD Han", None, specs, {1, 2}, [])
    assert box["calls"] == orchestrator._CLIP_RETRY_LIMIT


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
