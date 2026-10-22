"""视觉包:基线不变、每套都画得出来、建项目避开最近几条、换包的状态规则。不需要网络与额度。"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from PIL import Image

from app import config
from app.models import ProjectCreate, SubtitleCue
from app.pipeline import cards, hud, looks, overlays, timeline

LAND, PORT = (1920, 1080), (1080, 1920)
PANEL = {"title": "补能与续航", "rows": [{"label": "CLTC 续航", "values": ["835 km"]}],
         "source": "来源:官网"}
STAT = {"label": "CLTC 续航", "value": "835", "unit": "km", "source": "来源:官网"}


@pytest.fixture
def fresh_db(tmp_path, monkeypatch):
    from app import db

    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(config, "PROJECTS_DIR", tmp_path / "data" / "projects")
    monkeypatch.setattr(db, "_INITIALISED", False)
    return db


# --------------------------------------------------------------------------
# 基线
# --------------------------------------------------------------------------
def test_default_look_is_old_constants():
    """没选 look 的项目 = hud,取值与 look 出现之前的常量逐个相同。"""
    assert looks.current().id == "hud"
    assert (hud.PAPER, hud.INK, hud.KEY, hud.MUTED, hud.ACCENT, hud.PAPER_ALPHA) == (
        (236, 238, 233), (18, 22, 26), (61, 68, 76), (93, 101, 110), (255, 90, 31), 244)
    assert hud.GRID_COLOR == hud.mix(hud.PAPER, hud.INK, 0.07)
    assert hud.ACCENT_DARK == hud.mix(hud.ACCENT, hud.INK, 0.24)


def test_hud_names_follow_current_look():
    with looks.use("cinema") as lk:
        assert hud.PAPER == lk.paper and hud.ACCENT == lk.accent
    assert hud.PAPER == (236, 238, 233)


def test_default_subtitle_style_unchanged():
    cues = [SubtitleCue(index=1, start=0, end=1, text="你好", shot_idx=0)]
    ass = timeline.format_ass(cues, width=1920, height=1080)
    style = next(line for line in ass.splitlines() if line.startswith("Style: Default"))
    assert ",&H00FFFFFF,&H000000FF,&H80000000," in style
    assert style.split(",")[15] == "3"  # BorderStyle 3 = 半透明底条
    assert ass == timeline.format_ass(cues, width=1920, height=1080, look=looks.get("hud"))


def test_subtitle_styles_differ_per_look():
    cues = [SubtitleCue(index=1, start=0, end=1, text="你好", shot_idx=0)]
    styles = {lk.id: next(line for line in timeline.format_ass(cues, width=1920, height=1080, look=lk)
                          .splitlines() if line.startswith("Style: Default"))
              for lk in looks.LOOKS.values()}
    assert styles["cinema"].split(",")[15] == "1"  # 描边无底
    assert len(set(styles.values())) == len(styles)


# --------------------------------------------------------------------------
# 每套都画得出来,而且画出来不一样
# --------------------------------------------------------------------------
@pytest.mark.parametrize("look_id", list(looks.LOOKS))
@pytest.mark.parametrize("size", [LAND, PORT])
def test_every_look_renders(tmp_path, look_id, size):
    with looks.use(look_id):
        outs = [
            overlays.panel(PANEL, "小米YU7", size, tmp_path / "panel.png"),
            overlays.highlight(["15 分钟补 620 公里"], size, tmp_path / "hl.png"),
            overlays.stat(STAT, "小米YU7", size, tmp_path / "stat.png"),
            overlays.chapter_bar([("开头", 1.0), ("三电", 2.0)], 1, size, tmp_path / "bar.png"),
            *(p for p, _ in overlays.chapter_sting_frames(2, "三电性能", size, tmp_path / "sting")),
        ]
        card = {"kind": "compare", "title": "补能对比", "columns": ["A", "B"],
                "rows": [{"label": "峰值充电", "values": ["5.2C", "4C"]}]}
        outs += cards.render_frames(card, "小米YU7", tmp_path / "card", None, size)
    for p in outs:
        assert Image.open(p).size == size


def test_looks_render_differently(tmp_path):
    seen = set()
    for lk in looks.LOOKS:
        with looks.use(lk):
            p = overlays.panel(PANEL, "小米YU7", LAND, tmp_path / f"{lk}.png")
        seen.add(Image.open(p).tobytes())
    assert len(seen) == len(looks.LOOKS)


def test_cover_with_each_look(tmp_path):
    from app.pipeline import narration

    still = tmp_path / "still.jpg"
    Image.new("RGB", (1920, 1080), (90, 96, 104)).save(still)
    for lk in looks.LOOKS:
        with looks.use(lk):
            out = narration.make_cover(tmp_path / "none.mp4", "多花三万,买的是快充", tmp_path / f"{lk}.jpg",
                                       still=still, kicker="小米YU7")
        assert out and out.exists()


def test_samples_exist_for_every_look():
    base = Path(__file__).resolve().parents[1] / "app" / "web" / "static" / "looks"
    for lk in looks.LOOKS:
        assert (base / f"{lk}.jpg").exists() and (base / f"{lk}_v.jpg").exists(), lk


# --------------------------------------------------------------------------
# 挑选 / 存取
# --------------------------------------------------------------------------
def test_pick_is_reproducible_and_avoids_recent():
    assert looks.pick("p_a") == looks.pick("p_a")
    recent = [{"look": "hud"}, {"look": "cinema"}, {"look": "blueprint"}]
    assert all(looks.pick(f"p_{i}", recent) == "editorial" for i in range(10))
    # 全用过:挑最久没用的(recent 新的在前)
    assert looks.pick("p_x", [{"look": "editorial"}, *recent]) == "blueprint"
    assert looks.pick("p_x", recent, chosen="cinema") == "cinema"
    with pytest.raises(ValueError):
        looks.pick("p_x", chosen="nope")


def test_create_project_rotates_look(fresh_db):
    from app.pipeline import orchestrator

    seen = []
    for _ in range(4):
        pid = orchestrator.create_project(ProjectCreate(subject="小米YU7", duration=240))
        seen.append(looks.of_project(pid).id)
    assert sorted(seen) == sorted(looks.LOOKS)

    pid = orchestrator.create_project(ProjectCreate(subject="小米YU7", duration=240, look="cinema"))
    assert looks.of_project(pid).id == "cinema"


def test_concept_follows_look_unless_chosen(fresh_db):
    from app.pipeline import explainer, orchestrator

    pid = orchestrator.create_project(ProjectCreate(subject="小米YU7", duration=240, look="blueprint"))
    assert explainer.concept_style_of(pid) == looks.get("blueprint").concept
    orchestrator.set_look(pid, "editorial")
    assert explainer.concept_style_of(pid) == looks.get("editorial").concept
    fresh_db.write_json(pid, "concept_style.json", {"style": "cinema"})
    orchestrator.set_look(pid, "hud")
    assert explainer.concept_style_of(pid) == "cinema"


def test_set_look_only_before_render(fresh_db):
    from app.pipeline import orchestrator

    pid = orchestrator.create_project(ProjectCreate(subject="小米YU7", duration=240, look="hud"))
    assert orchestrator.set_look(pid, "cinema")["look"] == "cinema"
    assert json.loads(fresh_db.get_project(pid)["variant"])["look"] == "cinema"
    with pytest.raises(orchestrator.PipelineError, match="没有这个视觉包"):
        orchestrator.set_look(pid, "nope")
    fresh_db.update_project(pid, state="DRAFT_RENDERING")
    with pytest.raises(orchestrator.PipelineError, match="渲染已经开始"):
        orchestrator.set_look(pid, "hud")


def test_old_project_without_variant_is_hud(fresh_db):
    from app.pipeline import orchestrator

    pid = orchestrator.create_project(ProjectCreate(subject="小米YU7", duration=240))
    fresh_db.update_project(pid, variant="")
    assert looks.of_project(pid).id == "hud"
