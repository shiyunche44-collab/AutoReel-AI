"""成片精修(OpenFilm)导出 / 读回:只测我们这一侧,不起 Studio、不联网。"""
from __future__ import annotations

import pytest

from app import config, db
from app.models import ProjectCreate
from app.pipeline import orchestrator, refine
from app.pipeline.refine import Cue


@pytest.fixture()
def fresh_db(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(config, "PROJECTS_DIR", tmp_path / "data" / "projects")
    db._INITIALISED = False
    db.init_db()
    yield
    db._INITIALISED = False


CUES = [Cue(0.0, 1.58, "23.35万起。"), Cue(1.8, 2.85, "它真能闭眼挑吗?"), Cue(3.07, 4.75, "5个版本横跨两代。")]


def _project(aspect="16:9", duration=240) -> str:
    return orchestrator.create_project(ProjectCreate(
        subject="小米YU7", category="auto_long", duration=duration, aspect=aspect))


def _stage(pid: str, durations=(2.0, 3.0, 0.0, 4.0)):
    """造一个已出完成片的项目:几拍假 mp4、旁白、字幕 cue。时长 0 的拍不进成片。"""
    pdir = config.project_dir(pid)
    (pdir / "drafts").mkdir(parents=True, exist_ok=True)
    story, shots = [], []
    for i, d in enumerate(durations):
        f = pdir / "drafts" / f"{i:03d}.mp4"
        f.write_bytes(b"beat%d" % i)
        story.append({"idx": i, "duration": d})
        shots.append({"idx": i, "stage": "final", "status": "succeeded", "final_path": str(f),
                      "model": "x", "provider_task_id": None, "submitted_at": None,
                      "last_polled_at": None, "draft_path": None, "error": None})
    db.upsert_shots(pid, shots)
    db.write_json(pid, "storyboard.json", story)
    wav = pdir / "audio" / "narration.wav"
    wav.parent.mkdir(parents=True, exist_ok=True)
    wav.write_bytes(b"wav")
    db.write_json(pid, "narration.json", {
        "narration_path": str(wav),
        "cues": [{"index": i + 1, "start": c.start, "end": c.end, "text": c.text, "shot_idx": 0}
                 for i, c in enumerate(CUES)]})


def test_vtt_roundtrip_and_note_header():
    text = refine.to_vtt(CUES)
    assert text.startswith("WEBVTT")
    assert refine.VTT_NOTE in text  # 告诉 Studio 一条 cue 就是一条字幕
    assert refine.parse_vtt(text) == CUES


def test_parse_vtt_tolerates_studio_style_output():
    text = ("WEBVTT\n\nNOTE Each cue is one subtitle, as cut by hand in OpenFilm Studio.\n\n"
            "1\n00:00:00.000 --> 00:00:01.580\n<v 旁白>23.35<00:00:00.500>万起。</v>\n\n"
            "2\n00:01.800 --> 00:02.850\n它真能\n闭眼挑吗?\n")
    got = refine.parse_vtt(text)
    assert [c.text for c in got] == ["23.35万起。", "它真能 闭眼挑吗?"]
    assert got[1].start == 1.8


def test_diff_reports_only_changed_lines():
    orig = [{"index": i + 1, "text": c.text} for i, c in enumerate(CUES)]
    cur = [CUES[0], Cue(1.8, 2.85, "它真能闭眼挑吗?!"), CUES[2]]
    edits = refine.diff_cues(orig, cur)
    assert [(e.index, e.new) for e in edits.changed] == [(2, "它真能闭眼挑吗?!")]
    assert not edits.structural
    # 空白差异不算改动
    assert not refine.diff_cues(orig, [Cue(0, 1, " 23.35万起。 "), CUES[1], CUES[2]]).any


def test_diff_flags_added_or_removed_lines():
    orig = [{"index": i + 1, "text": c.text} for i, c in enumerate(CUES)]
    edits = refine.diff_cues(orig, CUES[:2])
    assert edits.structural and edits.any and (edits.original_count, edits.current_count) == (3, 2)


def test_film_html_landscape_and_portrait():
    land = refine.build_film_html((1920, 1080), [("assets/beat_000.mp4", 0.0, 2.0, 2.0)], "assets/mix.wav", 2.0)
    assert 'content="width=1920, height=1080"' in land
    assert 'src="assets/beat_000.mp4#t=0,2.000" at="0.000"' in land and "<audio" in land
    port = refine.build_film_html((1080, 1920), [("assets/beat_000.mp4", 0.0, 2.0, 2.0)], "assets/mix.wav", 2.0)
    assert 'content="width=1080, height=1920"' in port


def test_film_html_clamps_out_point_to_real_file_length():
    html = refine.build_film_html((1920, 1080), [("assets/beat_000.mp4", 0.0, 5.138, 5.125),
                                                 ("assets/beat_001.mp4", 5.138, 2.0, 0.0)],
                                  "assets/mix.wav", 7.138)
    assert 'src="assets/beat_000.mp4#t=0,5.125" at="0.000"' in html  # 出点不超过文件
    assert 'src="assets/beat_001.mp4#t=0,2.000" at="5.138"' in html  # 探不到长度就用分镜时长,起点仍按分镜累加


def test_export_lays_out_folder_and_skips_zero_length_beats(fresh_db, monkeypatch):
    monkeypatch.setattr(refine.timeline, "probe_safe", lambda p, default=0.0: 0.0)
    pid = _project()
    _stage(pid)
    s = refine.export(pid)
    assert (s.beats, s.cues, s.seconds) == (3, 3, 9.0)
    folder = refine.film_dir(pid)
    html = (folder / "film.html").read_text(encoding="utf-8")
    assert html.count("<video") == 3
    assert 'at="2.000"' in html and 'at="5.000"' in html  # 首尾相接
    assert (folder / "assets" / "beat_002.mp4").read_bytes() == b"beat3"  # 第 2 拍(时长 0)被跳过
    assert (folder / "assets" / "mix.wav").is_file()
    assert refine.parse_vtt((folder / "assets" / "mix.vtt").read_text(encoding="utf-8")) == CUES
    assert not refine.read_edits(pid).any


def test_export_uses_project_aspect(fresh_db, monkeypatch):
    monkeypatch.setattr(refine.timeline, "probe_safe", lambda p, default=0.0: 0.0)
    pid = _project(aspect="9:16", duration=90)
    _stage(pid)
    refine.export(pid)
    assert 'content="width=1080, height=1920"' in (refine.film_dir(pid) / "film.html").read_text(encoding="utf-8")


def test_read_edits_after_a_studio_edit(fresh_db, monkeypatch):
    monkeypatch.setattr(refine.timeline, "probe_safe", lambda p, default=0.0: 0.0)
    pid = _project()
    _stage(pid)
    refine.export(pid)
    vtt = refine.film_dir(pid) / "assets" / "mix.vtt"
    vtt.write_text(vtt.read_text(encoding="utf-8").replace("5个版本横跨两代。", "5个版本跨两代。"), encoding="utf-8")
    edits = refine.read_edits(pid)
    assert [(e.index, e.old, e.new) for e in edits.changed] == [(3, "5个版本横跨两代。", "5个版本跨两代。")]


def test_reexport_refuses_to_drop_pending_edits(fresh_db, monkeypatch):
    monkeypatch.setattr(refine.timeline, "probe_safe", lambda p, default=0.0: 0.0)
    pid = _project()
    _stage(pid)
    refine.export(pid)
    vtt = refine.film_dir(pid) / "assets" / "mix.vtt"
    vtt.write_text(vtt.read_text(encoding="utf-8").replace("23.35万起。", "23.35万。"), encoding="utf-8")
    with pytest.raises(refine.RefineError):
        refine.export(pid)
    refine.export(pid, force=True)
    assert not refine.read_edits(pid).any


def test_reexport_keeps_openfilm_dot_folder(fresh_db, monkeypatch):
    monkeypatch.setattr(refine.timeline, "probe_safe", lambda p, default=0.0: 0.0)
    pid = _project()
    _stage(pid)
    refine.export(pid)
    dot = refine.film_dir(pid) / ".film"
    dot.mkdir()
    (dot / "id").write_text("abc", encoding="utf-8")
    (refine.film_dir(pid) / "assets" / "stale.mp4").write_bytes(b"x")
    refine.export(pid, force=True)
    assert (dot / "id").read_text(encoding="utf-8") == "abc"  # Studio 认这个 id,不能丢
    assert not (refine.film_dir(pid) / "assets" / "stale.mp4").exists()  # 旧素材要清掉


def test_export_errors_are_readable(fresh_db):
    pid = _project()
    with pytest.raises(refine.RefineError, match="没有成片镜头"):
        refine.export(pid)
    _stage(pid)
    db.write_json(pid, "narration.json", {"cues": []})
    with pytest.raises(refine.RefineError, match="字幕 cue"):
        refine.export(pid)


def test_studio_url_parses_cli_output_and_ignores_exit_code(fresh_db, monkeypatch):
    monkeypatch.setattr(refine.timeline, "probe_safe", lambda p, default=0.0: 0.0)
    pid = _project()
    _stage(pid)
    refine.export(pid)
    monkeypatch.setattr(config, "motion_node", lambda: "C:/node/node.exe")
    monkeypatch.setattr(refine, "_npx", lambda: "npx")
    seen = {}

    class P:
        returncode = 3221226505  # Windows 上 libuv 断言的退出码
        stdout = "project  x\nStudio   http://127.0.0.1:4747/open?key=abc&to=%2Fp\n"
        stderr = ""

    def fake_run(cmd, **kw):
        seen["cmd"], seen["env"] = cmd, kw["env"]
        return P()

    monkeypatch.setattr(refine.subprocess, "run", fake_run)
    assert refine.studio_url(pid) == "http://127.0.0.1:4747/open?key=abc&to=%2Fp"
    assert seen["cmd"][-1] == "open"
    assert f"openfilm@{config.OPENFILM_VERSION}" in seen["cmd"]
    assert seen["env"]["OPENFILM_NO_BROWSER"] == "1"


def test_studio_url_without_node_says_so(fresh_db, monkeypatch):
    monkeypatch.setattr(refine.timeline, "probe_safe", lambda p, default=0.0: 0.0)
    pid = _project()
    _stage(pid)
    refine.export(pid)
    monkeypatch.setattr(config, "motion_node", lambda: "")
    with pytest.raises(refine.RefineError, match="node"):
        refine.studio_url(pid)


# --------------------------------------------------------------------------
# apply:检查 → 写回字幕 → 重出成片
# --------------------------------------------------------------------------
def _ready_to_apply(pid, monkeypatch, *, state="DONE"):
    """导出过、有事实、成片已完成;重合成换成假的,记下被调用的次数。"""
    monkeypatch.setattr(refine.timeline, "probe_safe", lambda p, default=0.0: 0.0)
    _stage(pid)
    db.write_json(pid, "facts.json", [{"id": "f1", "claim": "起售价23.35万,共5个版本", "enabled": True}])
    ass = config.project_dir(pid) / "audio" / "subs.ass"
    ass.write_text("old ass", encoding="utf-8")
    nar = db.read_json(pid, "narration.json", {})
    db.write_json(pid, "narration.json", {**nar, "ass_path": str(ass)})
    db.update_project(pid, state=state)
    refine.export(pid)
    calls = []

    def fake_recompose(project_id, specs):
        calls.append(project_id)
        return {"duration": 9.0, "warnings": []}

    import app.pipeline.explainer as explainer

    monkeypatch.setattr(explainer, "recompose", fake_recompose)
    monkeypatch.setattr(orchestrator, "_specs", lambda project_id: [])  # 假分镜不是完整 ShotSpec
    return ass, calls


def _edit_vtt(pid, old, new):
    vtt = refine.film_dir(pid) / "assets" / "mix.vtt"
    vtt.write_text(vtt.read_text(encoding="utf-8").replace(old, new), encoding="utf-8")


def test_apply_rewrites_cue_and_ass_then_recomposes(fresh_db, monkeypatch):
    pid = _project()
    ass, calls = _ready_to_apply(pid, monkeypatch)
    _edit_vtt(pid, "它真能闭眼挑吗?", "它真能闭眼选吗?")
    res = refine.apply(pid)
    assert res["changed"] == 1 and calls == [pid]
    nar = db.read_json(pid, "narration.json", {})
    assert [c["text"] for c in nar["cues"]] == ["23.35万起。", "它真能闭眼选吗?", "5个版本横跨两代。"]
    assert nar["cues"][1]["start"] == 1.8 and nar["cues"][1]["shot_idx"] == 0  # 时间轴不动
    assert "它真能闭眼选吗?" in ass.read_text(encoding="utf-8")
    assert not refine.read_edits(pid).any  # 基线更新:再应用就是无改动
    assert refine.apply(pid)["changed"] == 0 and calls == [pid]


def test_apply_without_edits_does_nothing(fresh_db, monkeypatch):
    pid = _project()
    _, calls = _ready_to_apply(pid, monkeypatch)
    assert refine.apply(pid) == {"changed": 0, "edits": [], "warnings": []}
    assert calls == []


def test_apply_blocks_unsourced_number_and_forbidden_word(fresh_db, monkeypatch):
    pid = _project()
    _, calls = _ready_to_apply(pid, monkeypatch)
    _edit_vtt(pid, "5个版本横跨两代。", "9个版本横跨两代。")
    with pytest.raises(refine.RefineError, match="没有事实依据的数字"):
        refine.apply(pid)
    _edit_vtt(pid, "9个版本横跨两代。", "5个版本横跨两代,全网最强。")
    with pytest.raises(refine.RefineError, match="禁用词"):
        refine.apply(pid)
    assert calls == []
    assert "9个" not in str(db.read_json(pid, "narration.json", {}))  # 被拦下的改动没写进去


def test_apply_blocks_empty_line_and_structural_change(fresh_db, monkeypatch):
    pid = _project()
    _ready_to_apply(pid, monkeypatch)
    _edit_vtt(pid, "它真能闭眼挑吗?", "")
    with pytest.raises(refine.RefineError, match="空句|句数"):
        refine.apply(pid)


def test_apply_needs_finished_cut(fresh_db, monkeypatch):
    pid = _project()
    _, calls = _ready_to_apply(pid, monkeypatch, state="COMPOSING")
    _edit_vtt(pid, "它真能闭眼挑吗?", "它真能闭眼选吗?")
    with pytest.raises(refine.RefineError, match="还没完成"):
        refine.apply(pid)
    assert calls == []


def test_apply_restores_subtitles_when_recompose_fails(fresh_db, monkeypatch):
    pid = _project()
    ass, _ = _ready_to_apply(pid, monkeypatch)
    import app.pipeline.explainer as explainer

    def boom(project_id, specs):
        raise RuntimeError("ffmpeg 挂了")

    monkeypatch.setattr(explainer, "recompose", boom)
    _edit_vtt(pid, "它真能闭眼挑吗?", "它真能闭眼选吗?")
    with pytest.raises(refine.RefineError, match="字幕已还原"):
        refine.apply(pid)
    assert "闭眼挑" in str(db.read_json(pid, "narration.json", {}))
    assert ass.read_text(encoding="utf-8") == "old ass"
    assert refine.read_edits(pid).any  # 改动还在,可以再点一次


def test_apply_warns_on_large_rewrite_but_goes_through(fresh_db, monkeypatch):
    pid = _project()
    _ready_to_apply(pid, monkeypatch)
    _edit_vtt(pid, "它真能闭眼挑吗?", "这台车到底值不值得买呢?")
    res = refine.apply(pid)
    assert res["changed"] == 1 and any("改动超过一半" in w for w in res["warnings"])


def test_apply_refuses_when_project_is_busy(fresh_db, monkeypatch):
    from app.pipeline import locks

    pid = _project()
    _, calls = _ready_to_apply(pid, monkeypatch)
    _edit_vtt(pid, "它真能闭眼挑吗?", "它真能闭眼选吗?")
    with locks.project_lock(pid):
        with pytest.raises(locks.ProjectBusy):
            refine.apply(pid)
    assert calls == []


# --------------------------------------------------------------------------
# 成片闸:只有「成片待审」且开了精修才能用
# --------------------------------------------------------------------------
def test_gate_refine_guards(fresh_db, monkeypatch):
    from app.team import publisher
    from app.team.plan import PlanError

    pid = _project()
    item = {"id": "it1", "project_id": pid, "status": "review", "subject": "小米YU7"}
    monkeypatch.setattr(db, "get_plan_item", lambda i: item if i == "it1" else None)
    monkeypatch.setattr(config, "OPENFILM_ENABLED", False)
    with pytest.raises(PlanError, match="没开启"):
        publisher.refine_open("it1")
    monkeypatch.setattr(config, "OPENFILM_ENABLED", True)
    with pytest.raises(PlanError, match="选题不存在"):
        publisher.refine_apply("nope")
    item["status"] = "scheduled"
    with pytest.raises(PlanError, match="不在成片待审"):
        publisher.refine_apply("it1")


def test_gate_refine_open_reuses_pending_edits(fresh_db, monkeypatch):
    from app.team import publisher

    pid = _project()
    _ready_to_apply(pid, monkeypatch)
    item = {"id": "it1", "project_id": pid, "status": "review"}
    monkeypatch.setattr(db, "get_plan_item", lambda i: item)
    monkeypatch.setattr(config, "OPENFILM_ENABLED", True)
    monkeypatch.setattr(refine, "studio_url", lambda p: "http://127.0.0.1:4747/x")
    exports = []
    real_export = refine.export
    monkeypatch.setattr(refine, "export", lambda p, **kw: exports.append(p) or real_export(p, **kw))
    assert publisher.refine_open("it1") == {"url": "http://127.0.0.1:4747/x", "pending": 0}
    assert exports == [pid]
    _edit_vtt(pid, "它真能闭眼挑吗?", "它真能闭眼选吗?")
    assert publisher.refine_open("it1")["pending"] == 1
    assert exports == [pid]  # 有没应用的改动:不重导,接着改


def test_gate_refine_apply_records_preference(fresh_db, monkeypatch):
    from app.team import preference, publisher

    pid = _project()
    _ready_to_apply(pid, monkeypatch)
    item = {"id": "it1", "project_id": pid, "status": "review"}
    monkeypatch.setattr(db, "get_plan_item", lambda i: item)
    monkeypatch.setattr(config, "OPENFILM_ENABLED", True)
    seen = []
    monkeypatch.setattr(preference, "record", lambda it, stage, decision, **kw: seen.append((stage, decision, kw)))
    _edit_vtt(pid, "它真能闭眼挑吗?", "它真能闭眼选吗?")
    out = publisher.refine_apply("it1")
    assert out["changed"] == 1
    assert seen[0][:2] == ("cut", "refine") and "闭眼挑" in seen[0][2]["note"] and "闭眼选" in seen[0][2]["note"]


def test_retext_ass_keeps_original_style_and_timing(tmp_path):
    from app.models import SubtitleCue
    from app.pipeline import narration

    ass = tmp_path / "subs.ass"
    ass.write_text("[Script Info]\n\nStyle: Default,Font,60,LIGHT-BOX\n\n[Events]\nFormat: x\n"
                   "Dialogue: 0,0:00:00.00,0:00:01.58,Default,,0,0,0,,23.35万起。\n"
                   "Dialogue: 0,0:00:01.80,0:00:02.85,Default,,0,0,0,,它真能闭眼挑吗,真的,假的\n", encoding="utf-8")
    cues = [SubtitleCue(index=1, start=0, end=1.58, text="23.35万起。", shot_idx=0),
            SubtitleCue(index=2, start=1.8, end=2.85, text="它真能闭眼选吗,真的,假的", shot_idx=0)]
    narration.retext_ass(ass, cues, line_chars=30)
    out = ass.read_text(encoding="utf-8")
    assert "LIGHT-BOX" in out  # 样式没被当前外观包覆盖
    assert "Dialogue: 0,0:00:01.80,0:00:02.85,Default,,0,0,0,,它真能闭眼选吗,真的,假的" in out  # 文字里的逗号不影响
    assert out.count("Dialogue:") == 2
