"""概念动画选模版:各套版式各异的模版、项目模版与门禁 B 换模版只重渲概念拍。"""
import os
import re
import shutil
import subprocess
import tempfile

import pytest

from app.models import ProjectCreate
from app.pipeline import motion
from app.state import State

DATA = {"subject": "小米YU7", "title": "电池与续航", "columns": [], "source": "官方",
        "rows": [{"label": "电池容量", "value": "96.3kWh", "values": ["96.3kWh"], "tag": ""}]}


def _fragments() -> list:
    """全部模版片段(scenes + templates)。"""
    out = [p for p in motion.scenes_dir().glob("*.html") if not p.stem.startswith("_")]
    out += [p for p in motion.templates_dir().glob("*.html") if not p.stem.startswith("_")]
    return out


def test_new_shared_files_are_ascii_only():
    """新加的共用文件不能带中文。

    HyperFrames 按页面上出现过的字符裁内嵌字体子集,连注释里的中文也算进去。
    子集一变,字形度量就变,文字会有亚像素位移 —— 实测加一段中文注释就能让
    上千个像素的渲染结果不同。共用文件是所有模版共用的,动了就是全体位移。

    模版片段自己的中文注释是原本就有的,不动它就不会改结果。
    """
    for rel in (motion.SHELL_FILE, "templates/_base.css"):
        text = (motion.config.MOTION_DIR / rel).read_text(encoding="utf-8")
        bad = [(i, ln) for i, ln in enumerate(text.splitlines(), 1) if not ln.isascii()]
        assert not bad, (rel, bad)


def test_shell_is_ascii_only():
    """骨架是共用文件,只能写 ASCII(见 test_new_shared_files_are_ascii_only)。"""
    shell = (motion.config.MOTION_DIR / motion.SHELL_FILE).read_text(encoding="utf-8")
    bad = [(i, ln) for i, ln in enumerate(shell.splitlines(), 1) if not ln.isascii()]
    assert not bad, bad


def test_fragments_keep_no_shell_styles():
    """骨架样式留在片段里就等于没抽出来:画幅、复位、根容器的定位只能有一处。"""
    for p in _fragments():
        css = motion._split_fragment(p.read_text(encoding="utf-8"), p.stem)[1]
        assert "box-sizing: border-box" not in css, p.name
        for m in re.finditer(r"html,\s*body\s*\{([^}]*)\}", css):
            assert "1920px" not in m.group(1) and "1080px" not in m.group(1), p.name
        for m in re.finditer(r"#root\s*\{([^}]*)\}", css):
            assert "position: relative" not in m.group(1), p.name
            assert "width: 100%" not in m.group(1), p.name


def test_compose_html_assembles_the_shell():
    html = motion.compose_html("battery", DATA, 6.0)
    # 骨架负责的:画幅、根容器、脚本挂载
    assert html.startswith("<!doctype html>")
    assert '<div id="root" data-composition-id="main"' in html
    assert 'data-duration="6.000"' in html and 'data-width="1920"' in html
    assert 'data-height="1080"' in html
    assert '<script src="gsap.min.js"></script>' in html
    assert '<script src="_lib.js"></script>' in html
    # 片段负责的:自己的样式和动效都在
    assert "vignette" in html and "HF.register(tl)" in html
    # 槽位全部换掉了
    for token in (motion.WIDTH_TOKEN, motion.HEIGHT_TOKEN,
                  motion.SHELL_META, motion.SHELL_CSS, motion.SHELL_JS):
        assert token not in html, token


@pytest.mark.skipif(not shutil.which("node"), reason="没有 node")
def test_every_fragment_is_valid_javascript():
    bad = []
    for p in _fragments():
        js = p.read_text(encoding="utf-8").split("<script>")[1].rsplit("</script>")[0]
        with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as f:
            f.write(js)
            tmp = f.name
        proc = subprocess.run(["node", "--check", tmp], capture_output=True, text=True)
        os.unlink(tmp)
        if proc.returncode:
            bad.append((p.name, (proc.stderr or "").strip().splitlines()[-1:]))
    assert not bad, bad


def test_templates_default_first():
    """默认模版排第一,其余模版各有自己的文件。

    数量只做**下限**断言:每加一套模版就要改测试里的数字,是"没有 CI"
    时期的典型维护负担 —— 实测已经因为加到第 21 套而红过一次。
    真正的约束是"每套模版有独立文件、id 与名字互不重复",与总数无关。
    """
    all_ = motion.styles()
    assert len(all_) >= 20
    assert all_[0].id == motion.DEFAULT_STYLE and all_[0].path is None
    assert len({s.id for s in all_}) == len(all_)
    assert len({s.name for s in all_}) == len(all_)
    assert all(s.name and s.desc for s in all_)
    assert all(s.path is not None for s in all_[1:])


def test_shell_carries_the_placeholders():
    """骨架是唯一一份:占位符、画幅、复位、根容器都在里面。"""
    shell = (motion.config.MOTION_DIR / motion.SHELL_FILE).read_text(encoding="utf-8")
    for token in (motion.SECONDS_TOKEN, motion.DATA_TOKEN, motion.BACKGROUND_TOKEN,
                  motion.SHELL_META, motion.SHELL_CSS, motion.SHELL_JS,
                  motion.WIDTH_TOKEN, motion.HEIGHT_TOKEN):
        assert token in shell, token
    # 画幅只有骨架这一处说了算(视口 meta + 画布 CSS + 根节点属性)
    assert shell.count("__WIDTH__") == 3 and shell.count("__HEIGHT__") == 3


def test_templates_are_complete_and_deterministic():
    for st in motion.styles()[1:]:
        html = st.path.read_text(encoding="utf-8")
        # 片段只写自己的东西:样式和动效;画幅/复位/脚本挂载归骨架
        assert "<style>" in html and "</style>" in html, st.id
        assert "<script>" in html and "</script>" in html, st.id
        assert "HF.register(tl)" in html, st.id
        # 骨架样式不许留在片段里(留着就等于没抽出来)
        assert "box-sizing: border-box" not in html, st.id
        # 逐帧渲染要可重复:不用随机数和真实时钟
        assert "Math.random" not in html and "Date.now" not in html and "setTimeout" not in html, st.id
        # 拼装后骨架提供的部分必须齐全
        composed = motion.compose_html("battery", DATA, 6.0, style=st.id)
        assert '<script src="_lib.js"></script>' in composed, st.id
        assert 'data-duration="6.000"' in composed and 'data-width="1920"' in composed, st.id
    # 模版之间不是换皮:各自的动效脚本完全不同
    bodies = [st.path.read_text(encoding="utf-8").split("<script>")[1] for st in motion.styles()[1:]]
    assert len(set(bodies)) == len(bodies)


def test_scene_templates_have_no_hardcoded_accent():
    for p in motion.scenes_dir().iterdir():
        if p.suffix in (".html", ".js"):
            text = p.read_text(encoding="utf-8").lower()
            assert "#b8f36b" not in text and "184, 243, 107" not in text.replace("184,243,107", "184, 243, 107"), p.name


def test_default_template_is_the_scene_template():
    html = motion.compose_html("battery", DATA, 6.0)
    assert html == motion.compose_html("battery", DATA, 6.0, style="studio")
    # 不认识的模版退回默认
    assert html == motion.compose_html("battery", DATA, 6.0, style="no-such-style")
    assert '"kicker"' not in html


def test_generic_template_replaces_scene_and_knows_the_scene():
    html = motion.compose_html("battery", DATA, 6.0, style="kinetic")
    assert "@name 巨字快切" in html
    assert '"scene": "battery"' in html and '"kicker": "电池"' in html
    assert "96.3kWh" in html            # 数据原样不动
    with pytest.raises(motion.MotionError):
        motion.compose_html("no-such-scene", DATA, 6.0, style="kinetic")


def test_generic_templates_bring_their_own_background_and_photo_choice():
    assert motion.scene_background("accel") == "footage"
    assert motion.scene_background("accel", "kinetic") == "own"     # 通用模版不铺实拍
    assert motion.scene_photo_views("battery", "kinetic") == []     # 不要车图的模版
    assert motion.scene_photo_views("battery", "editorial") == motion.scene_photo_views("battery")
    assert motion.scene_photo_views("compare", "editorial") == []   # 场景本身不要车图


def test_cache_key_differs_by_style():
    a = motion.cache_key("battery", DATA, 6.0, (1920, 1080), style="studio")
    b = motion.cache_key("battery", DATA, 6.0, (1920, 1080), style="neon")
    assert a != b


def test_snapshot_names_are_content_addressed(tmp_path):
    a = motion.snapshot_names("battery", DATA, 6.0, [3, 5], (1920, 1080), tmp_path, style="kinetic")
    b = motion.snapshot_names("battery", DATA, 6.0, [3, 5], (1920, 1080), tmp_path, style="neon")
    assert len(a) == 2 and a[0] != b[0] and "_kinetic_" in a[0].name


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


def test_every_style_has_fixed_sample():
    from app.pipeline import concept_styles

    g = concept_styles.gallery()
    assert len(g["styles"]) >= 20
    assert all(s["image"] for s in g["styles"]), [s["id"] for s in g["styles"] if not s["image"]]
    assert concept_styles.sample_image("../x.jpg") is None


def test_project_status_counts_concept_beats(fresh_db):
    from app.pipeline import concept_styles

    pid = _project_with_beats(fresh_db, State.GATE_A)
    assert concept_styles.project_status(pid) == {"style": motion.DEFAULT_STYLE, "beats": 2}
