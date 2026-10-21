"""概念动画选风格:风格文件、换皮注入、项目风格与门禁 B 换风格只重渲概念拍。"""
import re

import pytest

from app.models import ProjectCreate
from app.pipeline import motion
from app.state import State

DATA = {"subject": "小米YU7", "title": "电池与续航", "columns": [], "source": "官方",
        "rows": [{"label": "电池容量", "value": "96.3kWh", "values": ["96.3kWh"], "tag": ""}]}


def test_twenty_styles_default_first():
    all_ = motion.styles()
    assert len(all_) == 20
    assert all_[0].id == motion.DEFAULT_STYLE
    assert len({s.id for s in all_}) == 20
    assert all(s.name and s.desc for s in all_)


def test_style_files_only_set_known_variables():
    base = (motion.scenes_dir() / "_base.css").read_text(encoding="utf-8")
    known = set(re.findall(r"(--[\w-]+)\s*:", base.split("}", 1)[0]))
    for s in motion.styles():
        used = set(re.findall(r"(--[\w-]+)\s*:", s.css))
        assert used <= known, (s.id, used - known)


def test_templates_have_no_hardcoded_accent():
    for p in motion.scenes_dir().iterdir():
        if p.suffix in (".html", ".js"):
            text = p.read_text(encoding="utf-8").lower()
            assert "#b8f36b" not in text and "184, 243, 107" not in text.replace("184,243,107", "184, 243, 107"), p.name


def test_default_style_injects_nothing():
    html = motion.compose_html("battery", DATA, 6.0)
    assert "/* style:" not in html
    assert html == motion.compose_html("battery", DATA, 6.0, style="studio")
    # 不认识的风格退回默认
    assert html == motion.compose_html("battery", DATA, 6.0, style="no-such-style")


def test_style_overrides_variables_and_fonts():
    html = motion.compose_html("battery", DATA, 6.0, style="hud")
    assert "/* style: hud */" in html
    # 风格样式在公用样式之后,才能覆盖变量
    assert html.index("--acc: 184, 243, 107") < html.index("/* style: hud */")
    assert '"JetBrains Mono"' in html and motion.NUM_FONT not in html
    # 数据原样不动
    assert "96.3kWh" in html


def test_cache_key_differs_by_style():
    a = motion.cache_key("battery", DATA, 6.0, (1920, 1080), style="studio")
    b = motion.cache_key("battery", DATA, 6.0, (1920, 1080), style="neon")
    assert a != b


def test_snapshot_names_are_content_addressed(tmp_path):
    a = motion.snapshot_names("battery", DATA, 6.0, [3, 5], (1920, 1080), tmp_path, style="ice")
    b = motion.snapshot_names("battery", DATA, 6.0, [3, 5], (1920, 1080), tmp_path, style="ember")
    assert len(a) == 2 and a[0] != b[0] and "_ice_" in a[0].name


@pytest.fixture
def fresh_db(tmp_path, monkeypatch):
    from app import config, db

    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(config, "PROJECTS_DIR", tmp_path / "data" / "projects")
    monkeypatch.setattr(db, "_INITIALISED", False)
    return db


def _project_with_beats(db, state: State) -> str:
    from app.pipeline import orchestrator

    pid = orchestrator.create_project(ProjectCreate(subject="小米YU7"))
    beats = [
        {"idx": 0, "visual_zh": "[broll] 外观", "layout": "broll", "duration": 4.0},
        {"idx": 1, "visual_zh": "[concept] 电池", "layout": "concept", "duration": 6.0,
         "overlay": {"kind": "concept", "scene": "battery", "rows": []}},
        {"idx": 2, "visual_zh": "[concept] 雷达", "layout": "concept", "duration": 6.0,
         "overlay": {"kind": "concept", "scene": "lidar", "rows": []}},
    ]
    db.write_json(pid, "storyboard.json", beats)
    for stage in ("draft", "final"):
        db.upsert_shots(pid, [{"idx": b["idx"], "stage": stage, "status": "succeeded",
                               "draft_path": f"x{b['idx']}.mp4", "model": None, "provider_task_id": None,
                               "submitted_at": None, "last_polled_at": None, "final_path": None,
                               "error": None} for b in beats])
    db.update_project(pid, state=str(state))
    return pid


def test_set_style_before_render_only_saves(fresh_db):
    from app.pipeline import explainer, orchestrator

    pid = _project_with_beats(fresh_db, State.GATE_A)
    assert explainer.concept_style_of(pid) == motion.DEFAULT_STYLE
    res = orchestrator.set_concept_style(pid, "cinema")
    assert res["rerender"] == []
    assert explainer.concept_style_of(pid) == "cinema"
    assert fresh_db.get_project(pid)["state"] == str(State.GATE_A)


def test_set_style_at_gate_b_rerenders_only_concept_beats(fresh_db):
    from app.pipeline import orchestrator

    pid = _project_with_beats(fresh_db, State.GATE_B)
    res = orchestrator.set_concept_style(pid, "neon")
    assert res["rerender"] == [1, 2]
    rows = {r["idx"]: r["status"] for r in fresh_db.get_shots(pid, "draft")}
    assert rows == {0: "succeeded", 1: "pending", 2: "pending"}
    assert fresh_db.get_project(pid)["state"] == str(State.DRAFT_RENDERING)


def test_same_style_at_gate_b_is_noop(fresh_db):
    from app.pipeline import orchestrator

    pid = _project_with_beats(fresh_db, State.GATE_B)
    assert orchestrator.set_concept_style(pid, motion.DEFAULT_STYLE)["rerender"] == []
    assert fresh_db.get_project(pid)["state"] == str(State.GATE_B)


def test_set_style_refused_while_rendering_or_unknown(fresh_db):
    from app.pipeline import orchestrator

    pid = _project_with_beats(fresh_db, State.DRAFT_RENDERING)
    with pytest.raises(orchestrator.PipelineError):
        orchestrator.set_concept_style(pid, "neon")
    pid2 = _project_with_beats(fresh_db, State.GATE_A)
    with pytest.raises(orchestrator.PipelineError):
        orchestrator.set_concept_style(pid2, "no-such-style")


def test_preview_picks_one_beat_per_scene(fresh_db):
    from app.pipeline import concept_styles

    pid = _project_with_beats(fresh_db, State.GATE_A)
    beats = concept_styles.concept_beats(pid)
    assert [b.idx for b in concept_styles._pick_beats(beats)] == [1, 2]
    with pytest.raises(ValueError):
        concept_styles.start_preview(pid, ["no-such-style"], "小米YU7")
