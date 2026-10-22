"""画幅(横屏 16:9 / 竖屏 9:16)测试。

画幅是**项目属性**:新建项目时选,之后不可改。这里锁住:

1. 画幅常量与产物文件名只有一处定义(`pipeline/aspect.py`);
2. 时长档位跟画幅走:竖屏 60/90/120/180,横屏 120/240/360/480;
3. 竖屏短档位的段落精简:结论段永远保留,横屏档位的铺展结果不变;
4. 字幕字号按短边算 —— 横竖两版一样大;
5. 背景不硬裁 —— 16:9 素材放进竖屏走「前景完整 + 模糊铺底」。
"""
from __future__ import annotations

import pytest

from app import config, db
from app.categories import load_profile
from app.models import ProjectCreate, SubtitleCue
from app.pipeline import aspect, explainer, orchestrator, timeline


def _cue(text: str = "小米YU7 是今年最热门的车型") -> SubtitleCue:
    return SubtitleCue(index=0, start=0.0, end=3.0, text=text, shot_idx=0)


@pytest.fixture()
def fresh_db(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(config, "PROJECTS_DIR", tmp_path / "data" / "projects")
    db._INITIALISED = False
    db.init_db()
    yield
    db._INITIALISED = False


# --------------------------------------------------------------------------
# 画幅常量
# --------------------------------------------------------------------------
def test_sizes_share_the_short_edge():
    assert aspect.size_for(config.LANDSCAPE) == (1920, 1080)
    assert aspect.size_for(config.PORTRAIT) == (1080, 1920)
    assert aspect.size_for("4:3") == (1920, 1080)  # 不认识的退回横屏


def test_output_names_follow_aspect():
    assert aspect.output_name_for(config.LANDSCAPE) == "output.mp4"
    assert aspect.output_name_for(config.PORTRAIT) == "output_vertical.mp4"
    assert aspect.cover_name_for(config.PORTRAIT) == "cover_vertical.jpg"


def test_project_row_without_aspect_falls_back_to_profile_default():
    assert aspect.of({"category": "auto_long"}) == config.LANDSCAPE
    assert aspect.of({"category": "auto_long", "aspect": "9:16"}) == config.PORTRAIT


def test_portrait_line_chars_fit_the_canvas():
    """15 个 60px 的字 = 900px,画幅内可用宽 = 1080 - 2×64 = 952px,放得下。"""
    w, h = aspect.size_for(config.PORTRAIT)
    font = int(min(w, h) * 0.056)
    margin = int(w * 0.06)
    assert font * aspect.line_chars_for(config.PORTRAIT) <= w - margin * 2


# --------------------------------------------------------------------------
# 时长档位与段落精简
# --------------------------------------------------------------------------
def test_durations_follow_aspect():
    p = load_profile("auto_long")
    assert p.supported_durations(config.PORTRAIT) == (60, 90, 120, 180)
    assert p.supported_durations(config.LANDSCAPE) == (120, 240, 360, 480)
    assert p.supported_durations() == p.supported_durations(config.LANDSCAPE)


def test_shots_round_to_nearest_beat():
    p = load_profile("auto_long")  # 栅格 8 秒
    assert p.shots_for(60) == 8
    assert p.shots_for(90) == 11
    assert p.shots_for(240) == 30  # 整数倍不变


@pytest.mark.parametrize("category", ["auto_long", "baby_long"])
def test_short_portrait_plans_keep_opening_and_conclusion(category):
    p = load_profile(category)
    for duration in p.supported_durations(config.PORTRAIT):
        plan = p.plan_for(duration, "buyguide")
        assert len(plan) == p.shots_for(duration)
        assert plan[0] == "开场"
        assert plan[-1] == "结论建议"


def test_landscape_plans_do_not_shed_blocks():
    """横屏档位块数放得下,精简规则不能改变原来的铺展。"""
    p = load_profile("auto_long")
    theme = p.theme_spec("buyguide")
    for duration in p.supported_durations(config.LANDSCAPE):
        sections = set(p.plan_for(duration, "buyguide"))
        assert sections == {b["section"] for b in theme.blocks}


def test_roundup_capacity_by_duration():
    p = load_profile("auto_long")
    theme = p.theme_spec("roundup")
    assert p.max_subjects(theme, 60) >= theme.subject_count[0]
    assert p.max_subjects(theme, 120) == theme.subject_count[1]


def test_create_project_validates_duration_by_aspect(fresh_db):
    pid = orchestrator.create_project(ProjectCreate(
        subject="小米YU7", category="auto_long", duration=90, aspect="9:16"))
    assert db.get_project(pid)["aspect"] == "9:16"
    with pytest.raises(orchestrator.PipelineError):
        orchestrator.create_project(ProjectCreate(
            subject="小米YU7", category="auto_long", duration=480, aspect="9:16"))
    with pytest.raises(orchestrator.PipelineError):
        orchestrator.create_project(ProjectCreate(
            subject="小米YU7", category="auto_long", duration=90, aspect="16:9"))
    with pytest.raises(orchestrator.PipelineError):
        orchestrator.create_project(ProjectCreate(
            subject="小米YU7", category="auto_long", duration=120, aspect="4:3"))


def test_create_project_defaults_to_profile_aspect(fresh_db):
    pid = orchestrator.create_project(ProjectCreate(
        subject="小米YU7", category="auto_long", duration=240))
    assert db.get_project(pid)["aspect"] == "16:9"


def test_aspect_is_immutable_after_creation(fresh_db):
    pid = orchestrator.create_project(ProjectCreate(
        subject="小米YU7", category="auto_long", duration=240))
    with pytest.raises(ValueError):
        db.update_project(pid, aspect="9:16")


# --------------------------------------------------------------------------
# 字幕字号
# --------------------------------------------------------------------------
def test_ass_font_size_is_same_for_both_aspects():
    """按短边算字号:横屏 1920×1080 与竖屏 1080×1920 都是 60px。"""
    wide = timeline.format_ass([_cue()], width=1920, height=1080, line_chars=22)
    tall = timeline.format_ass([_cue()], width=1080, height=1920,
                               line_chars=aspect.line_chars_for(config.PORTRAIT))
    assert "PlayResX: 1920" in wide and "PlayResY: 1080" in wide
    assert "PlayResX: 1080" in tall and "PlayResY: 1920" in tall
    assert ",60," in wide
    assert ",60," in tall


# --------------------------------------------------------------------------
# 背景:不硬裁
# --------------------------------------------------------------------------
def test_fit_filter_keeps_whole_frame():
    """前景完整缩进画幅 + 模糊铺底 —— 16:9 源进 9:16 不会切掉车头车尾。"""
    out = explainer._fit_filter(1080, 1920, 0.0)
    assert "force_original_aspect_ratio=decrease" in out
    assert "boxblur" in out
    foreground = out.split("[bg]")[1]
    assert "force_original_aspect_ratio=decrease" in foreground
    assert "crop=" not in foreground.split("[fg]")[0].split("scale=")[1]


def test_fit_filter_with_crop_bottom_fills_the_frame():
    out = explainer._fit_filter(1920, 1080, 0.22)
    assert "crop=iw" in out
    assert "force_original_aspect_ratio=increase" in out
