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


# --------------------------------------------------------------------------
# 竖屏三段式网格与叠加层
# --------------------------------------------------------------------------
PORTRAIT = (1080, 1920)


def test_portrait_grid_zones_are_ordered_and_band_is_16_9():
    g = aspect.portrait_grid(PORTRAIT)
    assert g.title.top < g.title.bottom == g.band.top < g.band.bottom == g.data.top
    assert g.data.bottom <= g.sub.top < g.sub.bottom <= int(1920 * 0.90)
    assert g.band_size == (1080, 608)
    assert g.right <= int(1080 * 0.88)


def _alpha_bbox(path):
    from PIL import Image

    img = Image.open(path)
    assert img.size == PORTRAIT
    return img.getchannel("A").getbbox()


def test_portrait_overlays_stay_in_their_zones(tmp_path):
    from app.pipeline import overlays

    g = aspect.portrait_grid(PORTRAIT)
    panel = {"title": "三电", "source": "官网",
             "rows": [{"label": "续航", "values": ["835 km"]}, {"label": "功率", "values": ["508 kW"]},
                      {"label": "零百", "values": ["3.23 秒"]}]}
    # 数据区里的东西:不压画面带,不进字幕区
    for p in (overlays.panel(panel, "小米YU7", PORTRAIT, tmp_path / "p.png"),
              overlays.stat({"label": "续航", "value": "835 km", "source": "官网"}, "小米YU7",
                            PORTRAIT, tmp_path / "s.png"),
              overlays.lower_third("小米YU7", "三电", PORTRAIT, tmp_path / "l.png")):
        box = _alpha_bbox(p)
        assert box[1] >= g.data.top and box[3] <= g.sub.top, p.name
    # 标题区里的东西:不压画面带
    for p in (overlays.highlight(["800V 碳化硅", "15 分钟补能"], PORTRAIT, tmp_path / "h.png"),
              overlays.chapter_bar([("开场", 3), ("三电", 10)], 1, PORTRAIT, tmp_path / "b.png",
                                   headline="小米YU7:25万的SUV值不值得买")):
        box = _alpha_bbox(p)
        assert box[1] >= g.title.top and box[3] <= g.band.top, p.name


def test_split_headline_breaks_at_punctuation():
    from app.pipeline.overlays_portrait import split_headline

    assert split_headline("小米YU7:25万的SUV值不值得买") == ["小米YU7", "25万的SUV值不值得买"]
    assert split_headline("短标题") == ["短标题"]
    assert len(split_headline("一二三四五六七八九十十一十二十三")) == 2


def test_card_geometry_leaves_title_band_and_subtitles_free():
    from app.pipeline import cards

    g = aspect.portrait_grid(PORTRAIT)
    geo = cards.geo_for(PORTRAIT)
    assert geo.top > g.band.top and geo.bottom < g.sub.top


def test_landscape_card_geometry_unchanged():
    from app.pipeline import cards

    geo = cards.geo_for((1920, 1080))
    assert (geo.top, geo.bottom, geo.left, geo.right) == (140, 842, 384, 1536)


def test_render_portrait_beat_with_ffmpeg(tmp_path, monkeypatch):
    if not (config.FFMPEG_PATH and config.FFMPEG_PATH.is_file()):
        pytest.skip("没有 ffmpeg")
    from app.models import ShotSpec
    from app.pipeline import timeline as tl

    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    spec = ShotSpec(idx=0, duration=2.0, visual_zh="[stat] x", narration_zh="a", layout="stat",
                    overlay={"kind": "stat", "label": "续航", "value": "835公里", "source": "官网"})
    dest, _ = explainer.render_beat(spec, subject="测试车", size=(540, 960), out_dir=tmp_path,
                                    chapters=[("开头", 2.0)], chapter_idx=0, used={}, card_bg=None,
                                    headline="测试车:值不值")
    assert tl.probe_safe(dest, default=0.0) == pytest.approx(2.0, abs=0.1)
    import subprocess

    from PIL import Image

    from app.pipeline import render as render_mod

    frame = tmp_path / "f.png"
    subprocess.run([render_mod._ffmpeg(), "-y", "-v", "error", "-ss", "1", "-i", str(dest),
                    "-frames:v", "1", str(frame)], check=True)
    assert Image.open(frame).size == (540, 960)


def test_portrait_cover_keeps_whole_still_and_writes_3x4(tmp_path):
    from PIL import Image

    from app.pipeline import narration

    still = tmp_path / "car.jpg"
    Image.new("RGB", (1600, 900), (40, 120, 60)).save(still)
    dest = narration.make_cover(tmp_path / "none.mp4", "小米YU7:值不值得买", tmp_path / "cover_vertical.jpg",
                                subtitle="一句话结论", size=PORTRAIT, still=still, kicker="深度")
    assert dest is not None
    assert Image.open(dest).size == PORTRAIT
    three_four = narration.cover_3x4_path(dest)
    assert Image.open(three_four).size == (1080, 1440)


# --------------------------------------------------------------------------
# 概念动画:竖屏模版降级
# --------------------------------------------------------------------------
def test_portrait_concept_style_falls_back(monkeypatch):
    from app.pipeline import motion

    ready: set[str] = set()
    monkeypatch.setattr(motion, "supports_portrait", lambda style, scene=None: (style or "studio") in ready)
    # 横屏:原样
    assert explainer.portrait_concept_style("battery", "racing", (1920, 1080)) == ("racing", "")
    # 竖屏、都没适配:退回数据卡
    style, why = explainer.portrait_concept_style("battery", "racing", PORTRAIT)
    assert style is None and "数据卡" in why
    # 默认模版有竖屏版:改用默认
    ready.add("studio")
    style, why = explainer.portrait_concept_style("battery", "racing", PORTRAIT)
    assert style == "studio" and "默认模版" in why
    # 选定模版有竖屏版:直接用
    ready.add("racing")
    assert explainer.portrait_concept_style("battery", "racing", PORTRAIT) == ("racing", "")


def test_estimate_api_takes_aspect():
    from fastapi.testclient import TestClient

    from app import main

    client = TestClient(main.app)
    land = client.get("/api/estimate", params={"duration": 90, "aspect": "16:9"}).json()
    tall = client.get("/api/estimate", params={"duration": 90, "aspect": "9:16"}).json()
    junk = client.get("/api/estimate", params={"duration": 90, "aspect": "4:3"}).json()
    assert tall["aspect"] == "9:16" and junk["aspect"] == "16:9"
    assert tall["shot_count"] == land["shot_count"]
    assert tall["wall_clock"]["render_minutes"] >= 1


def test_portrait_plan_gives_each_section_two_beats():
    """竖屏每段至少约两拍:90 秒不再铺 10 段一拍一段(LLM 会照样每段写满,总字数超七成)。"""
    from app.categories import PORTRAIT_SHOTS_PER_SECTION, load_profile

    p = load_profile("auto_long")
    for d in p.supported_durations("9:16"):
        sl = p.slots_for(d, "buyguide", portrait=True)
        sections = list(dict.fromkeys(s.section for s in sl))
        assert len(sl) == p.shots_for(d)
        assert sections[0] == "开场" and sections[-1] == "结论建议"
        core = p.core_blocks(p.theme_spec("buyguide").blocks)
        assert len(sections) <= max(core, p.shots_for(d) // PORTRAIT_SHOTS_PER_SECTION)
    # 横屏不受影响
    assert p.slots_for(240, "buyguide") == p.slots_for(240, "buyguide", portrait=False)


class _ShortenLLM:
    def __init__(self, reply):
        self.reply, self.calls = reply, 0

    def structured(self, system, user, **kw):
        self.calls += 1
        if isinstance(self.reply, Exception):
            raise self.reply
        return self.reply


def _segs(*pairs):
    from app.models import ScriptSegment
    return [ScriptSegment(idx=i, section=s, text=t, fact_refs=[]) for i, (s, t) in enumerate(pairs)]


def test_fit_length_shortens_only_when_far_over_budget():
    from app.pipeline import script_gen

    long = _segs(("开场", "甲" * 40), ("三电", "乙" * 60), ("结论", "丙" * 40))
    short_reply = {"segments": [{"section": "开场", "text": "甲" * 20},
                                {"section": "三电", "text": "乙" * 30, "fact_refs": ["f_1"]},
                                {"section": "结论", "text": "丙" * 20}]}
    llm = _ShortenLLM(short_reply)
    out = script_gen.fit_length(long, 100, llm)
    assert llm.calls == 1 and script_gen.spoken_chars(out) == 70
    assert [s.idx for s in out] == [0, 1, 2] and out[1].fact_refs == ["f_1"]
    # 没超出容差(140 字 / 目标 130):不调 LLM
    llm = _ShortenLLM(short_reply)
    assert script_gen.fit_length(long, 130, llm) is long and llm.calls == 0


def test_fit_length_keeps_original_when_result_is_bad():
    from app.pipeline import script_gen

    long = _segs(("开场", "甲" * 40), ("三电", "乙" * 60), ("结论", "丙" * 40))
    bad = [
        RuntimeError("超时"),
        {"segments": []},
        {"segments": [{"section": "三电", "text": "乙" * 10}, {"section": "结论", "text": "丙"}]},  # 丢了开场
        {"segments": [{"section": "开场", "text": "甲"}, {"section": "瞎编", "text": "丁"}]},  # 丢了结论
        {"segments": [{"section": "开场", "text": "甲" * 80}, {"section": "结论", "text": "丙" * 80}]},  # 没变短
    ]
    for reply in bad:
        assert script_gen.fit_length(long, 50, _ShortenLLM(reply)) is long
