"""成片机审:联系表 → 视觉结论 → 复核 → 打回 / 放行。视觉模型和 ffmpeg 全部打桩。"""
from __future__ import annotations

import json
import time

import pytest
from PIL import Image

from app.pipeline import cut_review


@pytest.fixture
def fresh_db(tmp_path, monkeypatch):
    from app import config, db

    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(config, "PROJECTS_DIR", tmp_path / "data" / "projects")
    monkeypatch.setattr(db, "_INITIALISED", False)
    return db


CUES = [{"start": 0.0, "end": 6.0, "text": "银河E5起售价10.98万。"},
        {"start": 6.0, "end": 12.0, "text": "续航530公里。"}]


def _stub(monkeypatch, tmp_path, sheet_verdicts, confirm_verdicts=(), tech=None):
    """抽帧给 8 张纯色图(两张联系表,每张 4 格),视觉判定按给定结论回。"""
    from app.pipeline import final_qa, timeline, vision_verdict
    from app.providers import images

    def frames(video, out_dir, duration, **kw):
        out_dir.mkdir(parents=True, exist_ok=True)
        out = []
        for k in range(8):
            p = out_dir / f"s{k:05d}.jpg"
            Image.new("RGB", (64, 112), (40, 40, 40)).save(p)
            out.append((k + 0.5, p))
        return out

    def grab(video, at, dest, **kw):
        dest.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (64, 112)).save(dest)
        return True

    calls = []

    def judge(urls, prompt, system, llm, errors, *, what="图", contexts=None, max_chars=1500):
        calls.append((what, len(urls), list(contexts or [])))
        verdicts = list(sheet_verdicts if what == "联系表" else confirm_verdicts)
        verdicts = (verdicts + [None] * len(urls))[:len(urls)]
        return verdicts, [""] * len(urls), {"llm": len(urls), "json": 0, "none": 0}

    monkeypatch.setattr(cut_review, "extract_frames", frames)
    monkeypatch.setattr(cut_review, "grab_frame", grab)
    monkeypatch.setattr(vision_verdict, "judge", judge)
    monkeypatch.setattr(images.cli, "upload_temp_url", lambda p: f"data:{p}")
    monkeypatch.setattr(timeline, "probe_safe", lambda p, default=0.0: 12.0)
    monkeypatch.setattr(final_qa, "measure",
                        lambda v, timeout=900: tech or {"lufs": -14.0, "true_peak": -2.0,
                                                        "black": [], "freeze": []})
    video = tmp_path / "out.mp4"
    video.write_bytes(b"x")
    return video, calls


def test_clean_cut_passes(monkeypatch, tmp_path):
    video, calls = _stub(monkeypatch, tmp_path, [{"issues": []}, {"issues": []}])
    rep = cut_review.review(video, tmp_path / "w", subject="银河E5", cues=CUES, llm=object())
    assert rep["passed"] and not rep["inconclusive"] and not rep["issues"]
    assert len(rep["sheets"]) == 2
    assert "续航530公里" in calls[0][2][1]  # 第二张联系表带的是它那段时间的旁白


def test_watermark_needs_confirmation(monkeypatch, tmp_path):
    sheets = [{"issues": [{"kind": "watermark", "at": 3.5, "detail": "右下角 AUTOHOME"}]},
              {"issues": []}]
    video, _ = _stub(monkeypatch, tmp_path, sheets, [{"confirmed": True, "detail": "有汽车之家字样"}])
    rep = cut_review.review(video, tmp_path / "w", subject="银河E5", cues=CUES, llm=object())
    assert not rep["passed"]
    assert rep["bad_ranges"] == [[2.0, 5.0]]
    assert "水印" in rep["summary"]
    # 复核说没有:不打回,只留给终审看
    video, _ = _stub(monkeypatch, tmp_path, sheets, [{"confirmed": False}])
    rep = cut_review.review(video, tmp_path / "w", subject="银河E5", cues=CUES, llm=object())
    assert rep["passed"] and rep["issues"] and not rep["issues"][0]["blocking"]


def test_screen_number_not_said_is_only_a_hint(monkeypatch, tmp_path):
    sheets = [{"issues": [], "screen_numbers": [{"at": 2.5, "value": "10.98"},
                                                {"at": 3.5, "value": "7.9"}]},
              {"issues": [{"kind": "plate", "at": 6.5, "detail": "粤B 可读"}]}]
    video, _ = _stub(monkeypatch, tmp_path, sheets)
    rep = cut_review.review(video, tmp_path / "w", subject="银河E5", cues=CUES, llm=object())
    kinds = [(i["kind"], i["at"]) for i in rep["issues"]]
    assert kinds == [("number_mismatch", 3.5), ("plate", 6.5)]
    assert rep["passed"] and "2 条提示" in rep["summary"]


def test_black_frames_block_and_missing_verdicts_are_inconclusive(monkeypatch, tmp_path):
    tech = {"lufs": -14.0, "true_peak": -2.0, "black": [(4.0, 1.2)], "freeze": [(6.0, 5.0)]}
    video, _ = _stub(monkeypatch, tmp_path, [{"issues": []}, {"issues": []}], tech=tech)
    rep = cut_review.review(video, tmp_path / "w", subject="银河E5", cues=CUES, llm=object())
    assert not rep["passed"] and [i["detail"] for i in rep["issues"]] == ["黑场 1.2 秒"]
    # 视觉模型一张都没给结论:不能算通过,也不打回
    video, _ = _stub(monkeypatch, tmp_path, [None, None])
    rep = cut_review.review(video, tmp_path / "w", subject="银河E5", cues=CUES, llm=object())
    assert rep["inconclusive"] and rep["passed"] and rep["summary"].startswith("机审没看完")


def test_no_frames_is_inconclusive(monkeypatch, tmp_path):
    video, _ = _stub(monkeypatch, tmp_path, [])

    def boom(*a, **kw):
        raise RuntimeError("抽帧失败,一帧都没有")

    monkeypatch.setattr(cut_review, "extract_frames", boom)
    rep = cut_review.review(video, tmp_path / "w", subject="银河E5", llm=object())
    assert rep["inconclusive"] and "抽帧失败" in rep["errors"][0]


def _plan(db, state="DONE"):
    now = time.time()
    db.create_project({"id": "p_x", "title": "t", "subject": "银河E5", "category": "auto_long",
                       "theme": "", "duration": 60, "shot_count": 0, "narration_mode": "tts",
                       "state": state, "state_reason": None, "quota_budget": 40, "spent_cost": 0,
                       "est_cost": 0, "created_at": now, "updated_at": now})
    db.create_plan_item({"id": "pl_1", "category": "auto_long", "slot_at": now + 86400,
                         "subject": "银河E5", "theme": "buyguide", "aspect": "9:16",
                         "duration": 60, "status": "producing", "project_id": "p_x"})


def test_producer_sends_passed_cut_to_review(fresh_db, monkeypatch):
    from app import config
    from app.team import producer, publisher

    monkeypatch.setattr(config, "TEAM_MACHINE_REVIEW", True)
    _plan(fresh_db)
    monkeypatch.setattr(cut_review, "review_project", lambda pid, llm=None: {
        "passed": True, "inconclusive": False, "summary": "机审通过", "issues": []})
    assert producer.sync(background=False) == 1
    item = fresh_db.get_plan_item("pl_1")
    assert item["status"] == "review"
    assert publisher.machine_verdict(item)["summary"] == "机审通过"


def test_producer_rejects_failed_cut_and_reopens(fresh_db, monkeypatch):
    from app import config
    from app.team import producer

    monkeypatch.setattr(config, "TEAM_MACHINE_REVIEW", True)
    _plan(fresh_db)
    monkeypatch.setattr(cut_review, "review_project", lambda pid, llm=None: {
        "passed": False, "inconclusive": False, "bad_ranges": [[2.0, 5.0]],
        "summary": "机审不通过:3.5 秒 别家水印 / 台标:右下角 AUTOHOME",
        "issues": [{"kind": "watermark", "at": 3.5, "detail": "x", "blocking": True}]})
    producer.sync(background=False)
    old = fresh_db.get_plan_item("pl_1")
    assert old["status"] == "rejected" and "机审不通过" in old["note"]
    assert json.loads(old["machine_review"])["passed"] is False
    redo = [i for i in fresh_db.list_plan_items(statuses=("approved",), limit=10)
            if i["id"] != "pl_1"]
    assert len(redo) == 1 and redo[0]["subject"] == "银河E5"


def test_sheet_the_llm_skipped_counts_as_clean_when_it_was_described(monkeypatch, tmp_path):
    """LLM 只回了有问题的那张:另一张视觉模型描述过,算没问题,不算没看完。"""
    from app.pipeline import vision_verdict

    video, _ = _stub(monkeypatch, tmp_path, [])

    def judge(urls, prompt, system, llm, errors, *, what="图", contexts=None, max_chars=1500):
        if what != "联系表":
            return [{"confirmed": False}] * len(urls), ["x"] * len(urls), {"llm": len(urls)}
        return ([{"issues": [{"kind": "plate", "at": 1.5, "detail": "可读"}]}, None],
                ["第一张的描述", "第二张的描述"], {"llm": 1, "json": 0, "none": 1})

    monkeypatch.setattr(vision_verdict, "judge", judge)
    rep = cut_review.review(video, tmp_path / "w", subject="银河E5", cues=CUES, llm=object())
    assert not rep["inconclusive"] and rep["passed"] and len(rep["issues"]) == 1
