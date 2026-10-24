"""分发官:切片挑段、笔记正文收口、衍生品发布任务、成片闸一起排。"""
from __future__ import annotations

import json
import time
from pathlib import Path

import pytest


@pytest.fixture
def fresh_db(tmp_path, monkeypatch):
    from app import config, db

    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(config, "PROJECTS_DIR", tmp_path / "data" / "projects")
    monkeypatch.setattr(db, "_INITIALISED", False)
    return db


def _shots(spec):
    """spec: [(章节, 秒数, 事实数, 有卡)]"""
    sb = [{"idx": i, "duration": d, "chapter": c, "card": {"kind": "spec"} if card else None}
          for i, (c, d, _, card) in enumerate(spec)]
    sc = [{"idx": i, "fact_refs": ["f"] * n} for i, (_, _, n, _) in enumerate(spec)]
    return sb, sc


# --------------------------------------------------------------------------
# 切片挑段
# --------------------------------------------------------------------------
def test_windows_skip_opening_and_start_at_chapter():
    from app.pipeline.repurpose import shot_spans, windows

    sb, sc = _shots([("开头", 10, 5, True), ("开头", 10, 5, False),
                     ("空间", 12, 1, False), ("空间", 12, 1, False), ("空间", 12, 1, False),
                     ("价格", 15, 3, True), ("价格", 15, 3, False), ("价格", 15, 0, False)])
    spans = shot_spans(sb, sc)
    assert spans[2]["start"] == 20 and spans[-1]["end"] == 101
    got = windows(spans)
    assert {w["chapter"] for w in got} == {"空间", "价格"}  # 开场那章不切,只从章节开头起
    for w in got:
        assert 30 <= w["duration"] <= 45
    price = next(w for w in got if w["chapter"] == "价格")
    assert price["shots"] == [5, 6, 7] and price["cards"] == 1 and price["refs"] == 6
    assert got[0]["chapter"] == "价格"  # 密度高的在前


def test_pick_clips_non_overlapping_and_needs_facts():
    from app.pipeline.repurpose import pick_clips, shot_spans

    sb, sc = _shots([("开头", 10, 0, False), ("A", 20, 2, False), ("A", 20, 2, False),
                     ("B", 20, 0, False), ("B", 20, 0, False), ("C", 35, 4, True)])
    clips = pick_clips(shot_spans(sb, sc))
    assert [c["chapter"] for c in clips] == ["A", "C"]  # B 一条事实都没挂,不要
    assert all(a["end"] <= b["start"] for a, b in zip(clips, clips[1:]))


def test_clip_filter_landscape_uses_overlay():
    from app.pipeline.repurpose import clip_filter

    graph, overlay = clip_filter(False, 40)
    assert overlay and "boxblur" in graph and "[1:v]overlay" in graph and "st=39.600" in graph
    graph, overlay = clip_filter(True, 40)
    assert not overlay and "boxblur" not in graph


# --------------------------------------------------------------------------
# 图文笔记
# --------------------------------------------------------------------------
def _inp(**kw):
    return {"allowed": {"2399", "950"}, "terms": ("最强",), "title": "片名", "subject": "大唐", **kw}


def test_clean_body_drops_unbacked_numbers():
    from app.pipeline.repurpose import clean_body

    body, dropped = clean_body("▍价格\n23.99 万起。续航 950 公里。百公里 4.2 秒。\n\n收尾问题?", {"2399", "950"})
    assert "4.2" not in body and "950" in body and "23.99" in body
    assert body.startswith("▍价格") and "收尾问题" in body
    assert len(dropped) == 1 and "42" in dropped[0]


def test_check_note_limits_and_forbidden():
    from app.pipeline.repurpose import NOTE_TITLE_MAX, check_note

    note = check_note({"title": "这台车到底值不值得买呢我们今天来仔细聊一聊", "body": "最强的车。" + "正文" * 60,
                       "tags": ["#大唐", "大唐", "最强SUV", "买车"]}, _inp(), ["以官方为准"])
    assert len(note["title"]) <= NOTE_TITLE_MAX
    assert any("禁用词" in p for p in note["problems"])
    assert note["body"].endswith("以官方为准")
    assert note["tags"] == ["大唐", "买车"]
    ok = check_note({"title": "大唐怎么选", "body": "续航 950 公里。" + "正文" * 60}, _inp(), [])
    assert ok["problems"] == []


def test_template_note_takes_sections():
    from app.pipeline.repurpose import template_note

    note = template_note(_inp(), [{"section": "空间", "text": "第三排能坐。"},
                                  {"section": "空间", "text": "后备箱大。"},
                                  {"section": "空间", "text": "第三句不要。"},
                                  {"section": "价格", "text": "23.99 万起。"}])
    assert "▍空间\n第三排能坐。后备箱大。" in note["body"] and "第三句" not in note["body"]
    assert note["title"] == "片名"


def test_storyboard_cards_dedupe():
    from app.pipeline.repurpose import storyboard_cards

    card = {"kind": "spec", "title": "尺寸", "rows": [{"label": "车长"}]}
    got = storyboard_cards([{"card": card}, {"card": dict(card)}, {"card": None},
                            {"card": {"kind": "spec", "title": "空", "rows": []}}])
    assert got == [card]


def test_extra_cards_grounded():
    from app.models import Fact
    from app.pipeline.repurpose import extra_cards

    facts = [Fact(id="f1", claim="车长 5263 毫米", source_tool="t", source_query="q",
                  retrieved_at="2026-10-24", confidence="high")]

    class LLM:
        def structured(self, system, user, **kw):
            assert "f1" in user
            return {"cards": [
                {"kind": "spec", "title": "尺寸", "rows": [{"label": "车长", "values": ["5263mm"], "fact_id": "f1"}]},
                {"kind": "spec", "title": "瞎编", "rows": [{"label": "车宽", "values": ["2100mm"], "fact_id": "f1"}]},
            ]}

    got, dropped = extra_cards(facts, [], LLM(), 3)
    assert [c["title"] for c in got] == ["尺寸"] and dropped
    assert extra_cards(facts, [], None, 3) == ([], [])


def test_still_renders_3x4(tmp_path):
    from PIL import Image

    from app.pipeline import cards

    card = {"kind": "spec", "title": "尺寸", "columns": [], "source": "数据来源:官方 1",
            "rows": [{"label": "车长", "values": ["5263mm"], "fact_id": "f1", "tag": ""}]}
    out = cards.still(card, "大唐", tmp_path / "a.jpg")
    assert Image.open(out).size == cards.NOTE_SIZE


# --------------------------------------------------------------------------
# 发布任务
# --------------------------------------------------------------------------
def _accounts(monkeypatch, *accs):
    from app.publish import accounts as accounts_mod
    from app.publish.base import Account

    table = {a[0]: Account(id=a[0], platform=a[1], name=a[0]) for a in accs}
    monkeypatch.setattr(accounts_mod, "get_account", lambda aid: table.get(aid))
    from app.team import publisher

    monkeypatch.setattr(publisher, "accounts", lambda: [
        {"id": a.id, "platform": a.platform, "platform_label": a.platform, "name": a.name,
         "default": a.platform == "douyin", "paused": False, "paused_reason": ""} for a in table.values()])


def test_piece_column_allows_derivatives_same_aspect(fresh_db):
    base = {"project_id": "p_x", "account_id": "a", "platform": "douyin", "aspect": "9:16"}
    fresh_db.create_publication({"id": "pub_main", **base})
    fresh_db.create_publication({"id": "pub_c1", **base, "piece": "clip1"})
    assert fresh_db.find_publication("p_x", "a", "9:16")["id"] == "pub_main"
    assert fresh_db.find_publication("p_x", "a", "9:16", "clip1")["id"] == "pub_c1"
    import sqlite3

    with pytest.raises(sqlite3.IntegrityError):
        fresh_db.create_publication({"id": "pub_c1b", **base, "piece": "clip1"})


def test_old_db_gets_piece_column(tmp_path, monkeypatch):
    import sqlite3

    from app import config, db

    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(config, "PROJECTS_DIR", tmp_path / "data" / "projects")
    (tmp_path / "data").mkdir()
    conn = sqlite3.connect(tmp_path / "data" / "aimovice.db")
    conn.executescript("""CREATE TABLE publications (id TEXT PRIMARY KEY, project_id TEXT NOT NULL,
        account_id TEXT NOT NULL, platform TEXT NOT NULL, aspect TEXT NOT NULL DEFAULT '16:9',
        artifact TEXT NOT NULL DEFAULT '', title TEXT NOT NULL DEFAULT '', description TEXT NOT NULL DEFAULT '',
        tags TEXT NOT NULL DEFAULT '[]', state TEXT NOT NULL DEFAULT 'pending', remote_id TEXT NOT NULL DEFAULT '',
        remote_url TEXT NOT NULL DEFAULT '', upload_id TEXT NOT NULL DEFAULT '', error TEXT NOT NULL DEFAULT '',
        attempts INTEGER NOT NULL DEFAULT 0, published_at REAL, created_at REAL NOT NULL, updated_at REAL NOT NULL);
        CREATE UNIQUE INDEX idx_pub_unique ON publications(project_id, account_id, aspect);""")
    conn.close()
    monkeypatch.setattr(db, "_INITIALISED", False)
    db.init_db()
    base = {"project_id": "p_x", "account_id": "a", "platform": "douyin", "aspect": "9:16"}
    db.create_publication({"id": "pub_main", **base})
    db.create_publication({"id": "pub_c1", **base, "piece": "clip1"})
    assert db.get_publication("pub_c1")["piece"] == "clip1"


def test_enqueue_piece_and_request(fresh_db, monkeypatch):
    from app import config
    from app.publish import service

    _accounts(monkeypatch, ("xhs", "xiaohongshu"), ("dy", "douyin"))
    root = config.project_dir("p_x") / "repurpose"
    root.mkdir(parents=True)
    for name in ("note_00.jpg", "note_01.jpg", "clip1.mp4"):
        (root / name).write_bytes(b"x")
    fresh_db.write_json("p_x", "repurpose.json", {"note": {"images": ["repurpose/note_00.jpg",
                                                                      "repurpose/note_01.jpg"]}})
    row = service.enqueue_piece("p_x", "xhs", "note", aspect="3:4", artifact="repurpose/note_00.jpg",
                                title="大唐怎么选", description="正文", tags=["大唐"], scheduled_at=123.0)
    assert row["piece"] == "note" and row["scheduled_at"] == 123.0
    assert service.enqueue_piece("p_x", "xhs", "note", aspect="3:4", artifact="x", title="t") is None
    req = service._request_for(row, service.accounts_mod.get_account("xhs"))
    assert [p.name for p in req.extra["images"]] == ["note_00.jpg", "note_01.jpg"]
    assert req.cover_path.name == "note_00.jpg"
    service._ensure_artifact(row)
    clip = service.enqueue_piece("p_x", "dy", "clip1", aspect="9:16", artifact="repurpose/clip1.mp4",
                                 title="大唐:空间")
    assert service.piece_path(clip) == root / "clip1.mp4"
    req = service._request_for(clip, service.accounts_mod.get_account("dy"))
    assert req.video_path == root / "clip1.mp4" and "images" not in req.extra and req.cover_path is None


def test_xhs_adapter_routes_note(monkeypatch):
    from app.publish.base import PublishRequest
    from app.publish.platforms.xiaohongshu import XiaohongshuAdapter

    ad = XiaohongshuAdapter()
    monkeypatch.setattr(ad, "_publish_note", lambda req: "note")
    assert ad.publish(PublishRequest(project_id="p", account_id="a", platform="xiaohongshu",
                                     extra={"images": [Path("x.jpg")]}), None) == "note"


# --------------------------------------------------------------------------
# 成片闸
# --------------------------------------------------------------------------
def _manifest(db, problems=()):
    db.write_json("p_x", "repurpose.json", {
        "made_at": time.time(), "errors": [],
        "note": {"title": "大唐怎么选", "body": "正文", "tags": ["大唐"], "problems": list(problems),
                 "images": ["repurpose/note_00.jpg"]},
        "clips": [{"id": "clip1", "file": "repurpose/clip1.mp4", "title": "大唐:空间", "why": "w",
                   "duration": 40.0, "chapter": "空间", "chapters": ["空间"]},
                  {"id": "clip2", "file": "repurpose/clip2.mp4", "title": "大唐:价格", "why": "w",
                   "duration": 35.0, "chapter": "价格", "chapters": ["价格"]}]})


def _project(db, aspect="16:9"):
    now = time.time()
    db.create_project({"id": "p_x", "title": "大唐", "subject": "大唐", "category": "auto_long",
                       "theme": "", "duration": 240, "shot_count": 0, "narration_mode": "tts",
                       "state": "DONE", "state_reason": None, "quota_budget": 40, "spent_cost": 0,
                       "est_cost": 0, "created_at": now, "updated_at": now, "aspect": aspect})


def test_view_defaults(fresh_db, monkeypatch):
    from app.team import repurpose

    _accounts(monkeypatch, ("xhs", "xiaohongshu"), ("dy", "douyin"), ("bili", "bilibili"))
    _project(fresh_db)
    item = {"project_id": "p_x"}
    assert repurpose.view(item) == {"ready": False, "pieces": []}
    _manifest(fresh_db)
    v = repurpose.view(item)
    note, clip = v["pieces"][0], v["pieces"][1]
    assert [a["id"] for a in note["accounts"]] == ["xhs"] and note["accounts"][0]["default"]
    assert note["images"] == ["/api/projects/p_x/repurpose/note_00.jpg"]
    assert {a["id"]: a["default"] for a in clip["accounts"]} == {"xhs": False, "dy": True}


def test_view_portrait_clip_not_default(fresh_db, monkeypatch):
    from app.team import repurpose

    _accounts(monkeypatch, ("dy", "douyin"))
    _project(fresh_db, "9:16")
    _manifest(fresh_db)
    clip = repurpose.view({"project_id": "p_x"})["pieces"][1]
    assert clip["accounts"][0]["default"] is False  # 竖屏主片切片发同号 = 自己抄自己


def test_approve_schedules_and_validates(fresh_db, monkeypatch):
    from app.team import repurpose
    from app.team.plan import PlanError

    _accounts(monkeypatch, ("xhs", "xiaohongshu"), ("dy", "douyin"), ("bili", "bilibili"))
    _project(fresh_db)
    _manifest(fresh_db)
    item = {"project_id": "p_x"}
    with pytest.raises(PlanError):
        repurpose.approve(item, {"note": ["dy"]}, 1000.0)  # 笔记只能发小红书
    with pytest.raises(PlanError):
        repurpose.approve(item, {"clip1": ["bili"]}, 1000.0)
    with pytest.raises(PlanError):
        repurpose.approve(item, {"clip9": ["dy"]}, 1000.0)
    assert repurpose.approve(item, {"note": ["xhs"]}, 1000.0, dry_run=True) == []
    assert fresh_db.list_publications() == []
    done = repurpose.approve(item, {"note": ["xhs"], "clip1": ["dy"], "clip2": ["dy"], "x": []}, 1000.0)
    assert len(done) == 3
    pubs = {p["piece"]: p for p in fresh_db.list_publications()}
    assert pubs["note"]["scheduled_at"] == 1000.0 + 3 * 3600
    assert pubs["clip1"]["scheduled_at"] == 1000.0 + 86400
    assert pubs["clip2"]["scheduled_at"] == 1000.0 + 2 * 86400
    assert pubs["clip1"]["artifact"] == "repurpose/clip1.mp4" and pubs["clip1"]["aspect"] == "9:16"
    assert "完整版看主页" in pubs["clip1"]["description"]


def test_approve_red_note_rejected(fresh_db, monkeypatch):
    from app.team import repurpose
    from app.team.plan import PlanError

    _accounts(monkeypatch, ("xhs", "xiaohongshu"))
    _project(fresh_db)
    _manifest(fresh_db, problems=["命中禁用词:最强"])
    with pytest.raises(PlanError, match="红项"):
        repurpose.approve({"project_id": "p_x"}, {"note": ["xhs"]}, 1000.0)
    assert repurpose.view({"project_id": "p_x"})["pieces"][0]["accounts"][0]["default"] is False


def test_cut_gate_with_pieces_and_sync_ignores_them(fresh_db, monkeypatch):
    from app.publish import service
    from app.team import compliance, plan, publisher

    _accounts(monkeypatch, ("xhs", "xiaohongshu"), ("dy", "douyin"))
    _project(fresh_db)
    _manifest(fresh_db, problems=["禁用词"])

    def enqueue(project_id, *, account_ids, overrides=None, force=False):
        rows = []
        for aid in account_ids:
            fresh_db.create_publication({"id": f"pub_{aid}", "project_id": project_id, "account_id": aid,
                                         "platform": "douyin"})
            rows.append(fresh_db.get_publication(f"pub_{aid}"))
        return {"created": rows, "skipped": [], "warnings": []}

    monkeypatch.setattr(service, "enqueue", enqueue)
    monkeypatch.setattr(compliance, "checklist", lambda pid, title: {"items": [], "red": 0, "warn": 0})
    fresh_db.create_plan_item({"id": "pl_1", "category": "auto_long", "slot_at": time.time() + 86400,
                               "subject": "大唐", "status": "review", "project_id": "p_x"})
    with pytest.raises(plan.PlanError):  # 笔记红项 → 整单不过,主片也没排
        publisher.approve_cut("pl_1", ["dy"], pieces={"note": ["xhs"]})
    assert fresh_db.get_plan_item("pl_1")["status"] == "review" and fresh_db.list_publications() == []
    out = publisher.approve_cut("pl_1", ["dy"], pieces={"clip1": ["dy"]})
    assert out["pieces"] == ["切片 1 × 1"]
    fresh_db.update_publication("pub_dy", state="published")
    assert publisher.sync() == 1  # 切片还在排队,主片发了选题就算已发布
    assert fresh_db.get_plan_item("pl_1")["status"] == "published"


def test_run_skips_and_builds(fresh_db, monkeypatch):
    from app import config, team
    from app.pipeline import repurpose as media
    from app.team import repurpose

    with pytest.raises(team.Skip):
        repurpose.run(llm=object())
    _project(fresh_db)
    out = config.project_dir("p_x") / "output.mp4"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(b"x")
    fresh_db.create_plan_item({"id": "pl_1", "category": "auto_long", "slot_at": time.time(),
                               "subject": "大唐", "status": "review", "project_id": "p_x"})
    assert [i["id"] for i in repurpose.pending()] == ["pl_1"]

    def build(pid, llm=None):
        _manifest(fresh_db)
        return media.load(pid)

    monkeypatch.setattr(media, "build", build)
    msg = repurpose.run(llm=object())
    assert "笔记 1 张图" in msg and "2 条切片" in msg
    assert repurpose.pending() == []


def test_stats_and_monitor_skip_pieces(fresh_db):
    from app.team import compliance, features

    now = time.time()
    fresh_db.create_publication({"id": "pub_c", "project_id": "p_x", "account_id": "a", "platform": "douyin",
                                 "piece": "clip1", "state": "published", "published_at": now - 86400 * 2})
    assert features.rows() == []
    assert compliance.monitor(now) == []
